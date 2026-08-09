"""Discord ambient-presence plugin for Hermes Agent.

WHY
---
Hermes' chat gate is binary: a bot answers every message in a channel, or only
when @mentioned. Neither models a person who lurks in a community and chimes in
occasionally. Gateway hooks can't help (fire-and-forget observers; only
pre_tool_call can veto) and `[SILENT]` suppression exists only in cron.

This subclasses the bundled Discord adapter — never forks it, so upstream fixes
to that 10k-line file keep flowing — and adds ten opt-in behaviours:

1. AMBIENT JOINING — a message the stock gate rejects for lacking a mention may
   be re-dispatched as if the channel were free-response. Hard rate-limited.
2. SILENCE — the model may answer with a sentinel ([SILENT]) which the adapter
   swallows, so it can see a message and decide not to speak.
3. REACTIONS — messages it does NOT answer may still get an emoji reaction.
   This is the important one on this hardware: a reaction costs ZERO inference
   and posts instantly, so the bot reads as continuously present in the room
   instead of laggy. Real people react far more often than they reply.
4. PRESENCE + RETURN GREETINGS — rotating custom status (free), and priority
   attention for someone's first message after a long absence (the "she
   actually remembers me" moment).
5. NO-THREAD MODE — a per-profile kill switch for Discord auto-threading.
   Upstream reads `DISCORD_AUTO_THREAD` with os.getenv(), which is PROCESS-WIDE:
   under multiplex every profile shares one process, so a profile that sets
   `auto_thread: false` still gets threads if any other profile wants them.
   This restores per-profile control by refusing thread creation outright.
6. BOT BOUNCE — a circuit breaker for bot-to-bot conversations. Under
   DISCORD_ALLOW_BOTS + require_mention in a server full of agents, two bots
   whose replies auto-@mention each other volley FOREVER — upstream documents
   the topology as unsupported with no breaker. Two independent dials: an
   optional `probability` decides how OFTEN an exchange happens at all (each
   bot message is answered only on a winning roll), and min/max_replies bound
   how LONG one runs once it starts. After min-max replies to a given bot in a
   given conversation (rolled per conversation so her patience varies), the
   last reply carries a goodbye hint and every later message from
   that bot is suppressed BEFORE admission: zero inference, zero reply. A
   human speaking in the channel, or reset_after_seconds of quiet (measured
   from HER last counted reply, so a bot chattering at a tripped pair cannot
   hold it open), resets the pair. Humans are never gated. Suppressed ids are
   claimed in the dedup cache AND the gate runs on the missed-message
   backfill path too — otherwise reconnect recovery, which dispatches via
   _dispatch_recovered_message and never touches _dispatch_discord_message,
   would replay every suppressed message and re-ignite the volley.
7. FLEET STANDBY — on a host with ONE shared inference slot (a local CPU
   model, OLLAMA_NUM_PARALLEL=1), a chat turn that starts while another
   profile is mid-turn does not run alongside it — both interleave at the
   model server and BOTH crawl. With standby enabled, a dispatch arriving
   while any other agent turn or agent-mode cron job is running is HELD
   (polled, bounded by max_wait_seconds) and released the moment the slot
   frees; at the deadline it dispatches anyway. Opportunistic ambient joins
   are skipped outright while busy — they are dice rolls, not obligations.
   Every failure in the busy probe answers "not busy": standby can only
   ever DELAY a reply, never mute one.
   `only_when_local: true` scopes all of it to the times it actually helps:
   a profile whose PRIMARY model is hosted only contends for the local slot
   after falling back to the local model, so standby stays dormant until a
   fallback-switch notice naming a local model (see 8) is observed, and
   re-sleeps local_fallback_ttl_seconds later. Cloud turns are never held.
8. FALLBACK-NOTICE SUPPRESSION — upstream surfaces a provider/model
   fallback switch as a one-shot status send ("🔄 Switched to fallback
   model: ...") delivered through plain adapter.send(). Right for an
   operator channel, wrong for a public community room. With
   `suppress_fallback_notice: true` the notice is swallowed for THIS
   profile only (logged instead); other profiles keep stock behaviour.
   Suppressed or not, the notice is also the only signal the adapter ever
   gets about the model its sessions actually run on — upstream keeps
   fallback state on the per-session agent object, out of adapter reach —
   so it is parsed either way to drive only_when_local standby above.
9. GROUP-ADDRESS GREETINGS — "good morning agents" / "hello everyone" is
   addressed to the room, which the stock mention gate (and a bare name
   trigger list) can't represent. Opt-in `group_address` matches
   greeting+collective patterns and answers at its own probability and
   cooldown, exempt from the daily cap (being spoken to is not intruding).
10. SPEAKER IDENTITY — upstream labels inbound messages with the author's
   DISPLAY NAME (adapter.py:7837, hardcoded). Display names are per-guild,
   user-editable and freely reused, so durable notes keyed on one merge two
   people or lose someone the day they rename — and an agent told to "record
   the user id" cannot comply, because the id never reaches the model. With
   `speaker_identity: true` a compact `[speaker @handle id:123]` prefix is
   prepended once to the dispatched text, giving the agent the stable
   account handle and numeric id to key memories on.
11. SLASH-COMMAND POLICY — chat admission and slash auth share one gate
   upstream, so an answer-everyone community profile also hands /model,
   /reset, ... to everyone (and the per-profile allow-all env flag is not
   reliably visible on the interaction path under multiplex — the operator
   can end up rejected while strangers chat freely). `slash_commands`
   restricts slash invocations to explicit channels/users, chat untouched.

HOW (and why this exact seam)
-----------------------------
`require_mention` is enforced TWICE and independently: in
`_discord_message_admission` and again in `_handle_message`. Overriding only
the first silently fails at the second. Worse, the admission gate returns a
bare (False, False) for EVERY rejection reason — dedup, self-authored, bot
policy, and USER AUTHORIZATION — so blanket re-admission would bypass auth.

So we flip `_discord_free_response_channels()` to {"*"} for the duration of one
re-dispatch, behind a ContextVar scoped to that task. Both mention gates
short-circuit while every other gate (dedup, bot policy, _is_allowed_user,
allowed/ignored channels) re-runs untouched. Fails closed: any error falls back
to stock behaviour.

CONFIG — profile's `platforms.discord.extra.ambient_presence`
-------------------------------------------------------------
Verbatim passthrough per profile. NOT the top-level `discord:` block (that is
whitelisted and drops unknown keys) and NOT env vars (all profiles share one
process under multiplex, so os.getenv would leak across them). A profile
without this block behaves byte-identically to stock Discord.

UPSTREAM COUPLING (what discord-adapter-watch.sh guards)
--------------------------------------------------------
  * DiscordAdapter._discord_free_response_channels() -> set
  * DiscordAdapter._dispatch_discord_message(message) -> bool
  * DiscordAdapter._dispatch_recovered_message(message) -> bool  (backfill)
  * DiscordAdapter.connect(*, is_reconnect) -> bool
  * DiscordAdapter.send(chat_id, content, reply_to=None, metadata=None)
      - returns ONE SendResult per call, even when chunk-splitting a long
        reply (bounce counting = at most one charge per reply)
      - live replies carry reply_to=<inbound Discord message id>
        (base.py _reply_anchor_for_event returns event.message_id for
        Discord) — bounce counting correlates dispatch->send on it
  * DiscordAdapter._add_reaction(message, emoji) -> bool
  * DiscordAdapter._get_no_thread_channels() -> set
  * DiscordAdapter._check_slash_authorization(interaction, command_text)
    -> bool and DiscordAdapter._reject_slash(interaction, command_text, *,
    reason) -> False (slash-command policy rides these; every slash handler
    upstream funnels through the former)
  * fallback-switch notice text: agent/chat_completion_helpers.py
    try_activate_fallback sets "🔄 Switched to fallback model: {old} via
    {old_provider} → {new} via {new_provider}"; run_agent.py
    _emit_pending_fallback_notice emits it via status_callback and the
    gateway delivers it through adapter.send. Suppression + local-fallback
    detection match on the stable prefix — if upstream rewords it, both
    degrade to stock (notice shown, standby stays dormant), never worse.
  * drain notice text: gateway/run.py emits three variants while _draining,
    all built from GatewayRunner._status_action_gerund() ("shutting down" /
    "restarting"). The in-voice rewrite matches only the "⏳ Gateway <gerund>"
    head — if upstream rewords the tail the rewrite still fires; if it rewords
    the head, the stock notice goes out, which is the pre-1.17.0 behaviour.
  * self._dedup.discard/contains/is_duplicate(message_id) / self._client
  * standby busy probe (all optional — absence degrades to "not busy"):
      - BasePlatformAdapter.gateway_runner (base.py declares it; run.py stamps
        `adapter.gateway_runner = self` on every registry-created adapter)
      - runner._running_agents / runner._running_agents_ts (profile-namespaced
        session keys; pending sentinel placed synchronously at turn start)
      - cron.scheduler.get_running_job_ids() + jobs.json "no_agent" field
      - tools.async_delegation.active_count()
  * catch-up scanner: discord.py's own Client.get_channel / fetch_channel and
    Messageable.history(limit=) — library API, not Hermes', so it moves on
    discord.py's schedule rather than upstream's. Message.created_at is assumed
    timezone-aware UTC (discord.py 2.x, 2.7.1 here) — a naive one would NOT
    raise, it would be read as local time and skew every age check by the UTC
    offset, so this is an assumption to re-check on a major discord.py bump
    rather than one the code can catch.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import random
import re
import time
from collections import deque
from typing import Any

from plugins.platforms.discord.adapter import DiscordAdapter  # type: ignore
from plugins.platforms.discord import adapter as _bundled  # type: ignore

logger = logging.getLogger(__name__)

# ── Voice-side hygiene ──────────────────────────────────────────────────────
# Kaomoji are ordinary punctuation and letters, so Hermes' own _EMOJI_RE (which
# targets pictograph codepoints) never touches them and TTS reads them aloud as
# punctuation soup: "(=^･ω･^=)" becomes several seconds of noise. Strip them
# from the SPEECH script only — they stay in the posted text, where they are
# half the personality.
# A kaomoji is a SHORT bracketed span containing at least one character that
# plain prose never uses. Deliberately two steps — a candidate regex plus an
# explicit predicate — rather than one clever pattern: the first version used
# re.VERBOSE with a multi-line character class, which silently included a
# literal space and ate "(no errors)". Ordinary parentheses must survive.
_BRACKETED_RE = re.compile(r"[(（\[][^)）\]\n]{0,24}[)）\]]")

# Characters that appear in kaomoji and effectively never in plain prose.
_KAOMOJI_HINT_RE = re.compile(
    "["
    "\u3040-\u30ff"      # hiragana / katakana (ω, ･, ﾉ, 彡, ﾟ)
    "\u2190-\u21ff"      # arrows
    "\u2600-\u27bf"      # misc symbols / dingbats
    "\uff00-\uffef"      # fullwidth & halfwidth forms
    "\u0300-\u036f"      # combining marks (•̀, ᴗ-)
    "\u1d00-\u1d7f"      # phonetic extensions (ᴗ)
    "\u2500-\u25ff"      # box drawing / geometric
    "^*=~|<>/\\\\"      # ASCII faces: (^_^) (=^･^=) \\(^o^)/
    "]"
)


_TTS_PATCHED = False

# ── "speech happened" signal ────────────────────────────────────────────────
# Measured 2026-08-07: the reply TEXT goes out ~300ms BEFORE the audio, and the
# TTS file is written ~14s before that. So a mark set when audio is delivered
# is always too late — but the TTS call itself is early enough to act on.
#
# SCOPE, and its limit, stated plainly: the tool has no reliable profile context
# (the same process-global resolution that misfiles audio into another profile's
# cache dir is the only signal available at TTS time), so this flag is
# process-wide. Three things keep it safe:
#   1. only profiles with voice_only_replies ever CONSULT it — today just Companion;
#   2. it is consume-once, so at most one text send is ever affected;
#   3. a short window, and every suppression is logged.
# Residual risk: another profile generates speech and Companion sends unrelated
# text inside the window — one message dropped, logged, never silence.
# Measured over five real turns (2026-08-07): the gap between the TTS call and
# the text send is 2.7s / 5.6s / 12.8s / 15.4s / 35.4s — dominated by how long
# the model takes to FINISH generating after calling the tool, which on a
# free-tier model is wildly variable. A 30s window caught only 4 of 5; the
# instinct to shrink it would have brought the duplicate text straight back.
_TTS_SIGNAL_WINDOW_S = 45.0
_last_tts_at: float = 0.0
_last_tts_claimed: bool = True


def _profile_wants_voice_only() -> bool:
    """Does the profile that is generating this speech want voice-only replies?

    This is what keeps a process-wide signal from crossing profiles. It works
    because config resolution IS profile-correct inside the TTS tool — proven by
    the tool itself picking Edge for Companion and Piper for Assistant on the same
    gateway. So a profile without voice_only_replies never arms the signal, and
    can never cause another profile's message to be dropped.
    """
    try:
        from hermes_cli.config import load_config  # type: ignore

        extra = (
            load_config()
            .get("platforms", {})
            .get("discord", {})
            .get("extra", {})
            .get("ambient_presence", {})
        ) or {}
        return bool(extra.get("voice_only_replies", False))
    except Exception:
        return False          # fail toward "do not suppress"


def _note_tts_generated() -> None:
    global _last_tts_at, _last_tts_claimed
    if not _profile_wants_voice_only():
        return
    _last_tts_at = time.monotonic()
    _last_tts_claimed = False


def _claim_recent_tts() -> bool:
    """True at most once per TTS call, and only inside the window."""
    global _last_tts_claimed
    if _last_tts_claimed:
        return False
    if (time.monotonic() - _last_tts_at) > _TTS_SIGNAL_WINDOW_S:
        return False
    _last_tts_claimed = True
    return True



def _strip_kaomoji(text: str) -> str:
    """Remove kaomoji from a speech script. Never touches posted text.

    Conservative by design: when in doubt, KEEP the text. A missed kaomoji is a
    second of odd audio; a false positive silently deletes real words from what
    the agent says out loud, which is far worse and much harder to notice.
    """
    if not text:
        return text

    def _drop(m: "re.Match[str]") -> str:
        inner = m.group(0)[1:-1]
        if not inner.strip():
            return m.group(0)                      # "()" — leave it alone
        if _KAOMOJI_HINT_RE.search(inner):
            return " "
        return m.group(0)

    cleaned = _BRACKETED_RE.sub(_drop, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _install_tts_kaomoji_filter() -> None:
    """Wrap Hermes' spoken-text preparation with a kaomoji pre-pass.

    Patches ``tools.tts_text_normalize.prepare_spoken_text``, which is the COMMON
    chokepoint — every route to speech goes through it:

      * ``tts_tool.text_to_speech_tool``      (the model calling the tts tool)
      * ``tts_tool._strip_markdown_for_tts``  (the runner's auto voice reply)
      * ``gateway/platforms/base.py``         (the adapter path)

    An earlier version patched ``_strip_markdown_for_tts`` instead and only fixed
    the runner path, so a model that called the tool directly still had its
    kaomoji read aloud. All three import the function INSIDE the function body,
    so patching the module attribute reaches every caller.

    PROCESS-WIDE, unlike everything else in this plugin, and deliberately so:
    the import shape makes per-profile scoping impossible, and nobody wants
    kaomoji read aloud, so a global is the honest shape rather than a config key
    that pretends otherwise.

    Wraps rather than replaces, so upstream normalisation keeps running and keeps
    working after an update.
    """
    global _TTS_PATCHED
    if _TTS_PATCHED:
        return
    try:
        import tools.tts_text_normalize as _norm  # type: ignore

        _orig = _norm.prepare_spoken_text

        def _wrapped(text, *a, **kw):
            try:
                text = _strip_kaomoji(text)
            except Exception:
                pass          # a hygiene pass must never break speech
            return _orig(text, *a, **kw)

        _norm.prepare_spoken_text = _wrapped

        # Same install point, second concern: record WHEN speech was produced,
        # so send() can drop the text that the pipeline emits just before it.
        import tools.tts_tool as _tts  # type: ignore

        _orig_tool = _tts.text_to_speech_tool

        def _tool_wrapped(*a, **kw):
            result = _orig_tool(*a, **kw)
            try:
                if isinstance(result, str) and '"success": false' not in result:
                    if _profile_wants_voice_only():
                        _note_tts_generated()
                        # Tell the model, AT THE POINT IT IS DECIDING, that the
                        # audio is the whole reply. Suppressing prose in send()
                        # is cosmetic — the tokens are already spent by then.
                        # A tool result sits in context exactly where the next
                        # move is chosen, so it stops the prose being GENERATED
                        # rather than hiding it afterwards.
                        # Added as a new JSON field: delivery reads `media_tag`,
                        # so this cannot disturb attachment handling.
                        import json as _json

                        payload = _json.loads(result)
                        payload["reply_complete"] = True
                        payload["instruction"] = (
                            "This audio IS your complete reply. Output no text after "
                            "this — no summary, no caption, no different answer. The "
                            "user hears the audio."
                        )
                        return _json.dumps(payload, ensure_ascii=False)
            except Exception:
                pass          # never break speech over a hint
            return result

        _tts.text_to_speech_tool = _tool_wrapped
        _TTS_PATCHED = True
        logger.info("ambient: TTS kaomoji filter + speech signal installed (process-wide)")
    except Exception as exc:
        logger.warning("ambient: could not install TTS kaomoji filter: %s", exc)


# A model narrating its own tool result: "[Media: AUDIO:/var/lib/.../tts_x.mp3]".
# Only the BRACKETED rendering is stripped — a bare "MEDIA:<path>" is the real
# directive the send pipeline consumes to deliver the audio, and removing that
# would silence the agent instead of tidying it. Host paths must never reach
# chat regardless: they leak the HERMES_HOME layout, which is why the base
# adapter has _log_safe_path for its own logging.
_MEDIA_NARRATION_RE = re.compile(
    r"\[\s*(?:media|audio|image|video|file)\s*:\s*[^\]]*\]",
    re.IGNORECASE,
)

# A model that generated an image often ALSO writes markdown image syntax for it —
# observed 2026-08-07 after an image_generate call: "![Luna Portrait]()". The platform
# already delivers the picture as an attachment, so this renders as a broken image or
# bare punctuation next to the real thing. Same class as the media-narration leak: the
# model describing its own tool result instead of letting delivery handle it.
#
# Only EMPTY or local-path targets are stripped. A markdown image pointing at a real
# http(s) URL is a deliberate link and must survive — Klipy GIFs rely on that.
_EMPTY_MD_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?:|file://[^)]*|/[^)]*)\)"
)

# The gateway echoes inbound speech as: 🎙️ "<transcript>"
_STT_ECHO_RE = re.compile(r'^\s*\U0001F399\uFE0F?\s*"')

# True only inside one ambient re-dispatch. ContextVars are per-task, so a
# concurrent stock message on another task is unaffected — and the backfill
# helper that shares _discord_free_response_channels() never sees it.
_ambient_open: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_ambient_open", default=False
)

# The ambient directive for the turn currently being dispatched, delivered to the
# model through `llm_request` middleware instead of being concatenated onto the
# user's message.
#
# WHY THIS EXISTS (2026-08-08): the hint used to be prepended to
# `message.content`. That text is the USER TURN, so Hermes persisted it — 59 rows
# across 20 sessions in the root state.db, 8 of them in one agent session. The
# directive therefore stopped being an instruction for one turn and became a
# standing feature of her transcript, and the model started reproducing it on
# turns where nobody had issued it. It cost a real request: asked directly for a
# self-portrait, she echoed a stale "nobody addressed you ... reply [SILENT]" out
# of her own history and declined.
#
# Injected via middleware, the hint reaches the provider for exactly one request
# and is never written to history: `apply_llm_request_middleware` rewrites the
# outgoing payload only. Appended at the END of the message list so the cached
# prefix is untouched (the AGENTS.md prompt-cache invariant).
#
# A ContextVar rather than a dict keyed by session: the middleware is handed
# session_id/platform but never a chat id, and this plugin already relies on
# ContextVars surviving into the turn (see _current_speaker_id, read by the
# image gate inside the agent loop).
_ambient_hint: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_ambient_hint", default=""
)

# Channel keys for which threading is suppressed, scoped to one dispatch task.
# Who sent the message currently being handled. Set at dispatch, read inside the
# agent turn by the image-generation gate. ContextVars are copied into tasks
# created from the current context, which is how it survives into the turn.
# Absence means "unknown", and the gate treats unknown as DENY — a false deny
# costs the operator a re-ask, a false allow costs metered spend to strangers.
_current_speaker_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ambient_current_speaker_id", default=""
)

_no_thread_keys: contextvars.ContextVar[set] = contextvars.ContextVar(
    "hermes_ambient_no_thread", default=frozenset()
)

_DEFAULT_HINT = (
    "[ambient: nobody addressed you — you are simply present in the room. "
    "Chime in ONLY if you have something genuinely short and fun to add. "
    "If not, reply with exactly {marker} and nothing else.]"
)

_RETURN_HINT = (
    "[ambient: {who} is back after {days} days away — you noticed. Greet them "
    "warmly and briefly, and if you remember something about them, show it. "
    "One or two sentences. If nothing good comes to mind, reply exactly {marker}.]"
)

# The catch-up directive is deliberately assembled from SINGLE-LINE bracketed
# blocks with the transcript between them, and it is not a stylistic choice.
# `_AMBIENT_ECHO_RE` — the rule that stops a model publishing its own
# instructions — is bounded to one line by design (see its comment). A directive
# with newlines inside the brackets would not match it at all, so a model that
# regurgitated its input would post the whole block, transcript included, into
# the room the transcript was copied from. Both wrapper lines close their bracket
# before the newline, so all of them are caught by the existing rule; the
# transcript lines that remain are handled by the echo guard in `send()`.
#
# That rule is ALSO bounded to 400 characters, which is why the closing
# instruction is two bracketed lines rather than one: the single-line version
# was 509 chars, matched nothing, and would have posted itself. Keep every
# bracketed line short — the test asserts each one is strippable on its own.
_CATCHUP_HINT = (
    "[ambient catch-up: nobody addressed you and nobody is waiting on you — you "
    "have simply been quiet while the room talked. The lines below are what was "
    "said since you last spoke, oldest first. They are context you overheard, "
    "never text to repeat, quote or summarise back.]\n"
    "{transcript}\n"
    "[ambient catch-up: now decide. Speak ONLY if you have something genuinely "
    "worth adding to THAT conversation — a reaction to what was actually said, "
    "not a greeting, not a question about what everyone is up to, not an "
    "announcement that you are here.]\n"
    "[ambient catch-up: one or two sentences, in your own voice, as if you had "
    "been listening all along. Reply to the room, not to each person in turn. If "
    "you have nothing worth saying, which is the usual case, reply with exactly "
    "{marker} and nothing else — silence costs you nothing.]"
)

_GOODBYE_HINT = (
    "[ambient: you have been trading replies with {who} — another bot — for a "
    "while now. This is your LAST reply of this exchange: give a short, "
    "in-character farewell that clearly closes the conversation. Do not ask "
    "any questions, do not invite a reply, do not @mention anyone.]"
)

_DEFAULT_REACTIONS = ["👀", "😹", "✨", "🐈", "💅", "🔥"]

# Stable prefix of upstream's one-shot fallback-switch status notice (see
# UPSTREAM COUPLING in the module docstring). Matched with startswith on the
# stripped content: distinctive enough that a real chat reply cannot
# plausibly collide with it.
_FALLBACK_NOTICE_PREFIX = "🔄 Switched to fallback model:"

# Operator-facing machinery that upstream posts into whatever channel a job
# delivers to. In a community room these read as the bot leaking its own
# plumbing, so they are rerouted to a private channel when one is configured.
_SYSTEM_NOTICE_PREFIXES = ["⚠️ Cron '", "Cronjob Response:"]

# Notices that are pure gateway plumbing: they tell the OPERATOR about session
# mechanics and mean nothing to a room full of strangers. Rerouting is wrong for
# these — there is no other channel where "no activity for 15 min" is useful —
# so they are dropped and logged instead.
#
# The inactivity warning (gateway/run.py: "⚠️ No activity for N min ... use
# /reset") is the one that prompted this: Companion posted it into the community
# room 2026-08-07. It is emitted through adapter.send(), which is why the plugin
# can catch it at all.
_SYSTEM_NOTICE_DROP = [
    "⚠️ No activity for",
    "⏳ Still working",
    "🔄 Reconnecting",
    "⚠️ Session timed out",
    # Busy-input acks (gateway/run.py ~9110-9132). A terminal operator who hits
    # Enter mid-run needs to know their keystroke landed; a room does not — from
    # the outside these read as the bot narrating its own scheduler. Observed
    # 2026-08-07: a social profile posted "⚡ Interrupting current task" into a
    # community room.
    # ALL of the busy variants are listed, not just the one seen, because moving
    # the gateway to queue mode only swaps which one gets emitted.
    "⚡ Interrupting current task",
    "⏳ Queued for the next turn",
    "⏳ Subagent working",
    "⏳ Compressing context",
]

# Drain notices. While the gateway is stopping or restarting it refuses (or
# queues) new turns and says so in operator language — gateway/run.py builds
# all three from _status_action_gerund():
#   "⏳ Gateway is shutting down and is not accepting new work right now."
#   "⏳ Gateway is restarting and is not accepting another turn right now."
#   "⏳ Gateway restarting — queued for the next turn after it comes back."
# Neither existing treatment fits. Dropping is wrong: someone just spoke and is
# owed an answer. Rerouting is wrong: the answer belongs in the room they spoke
# in. So a profile may supply its own wording and the notice is REWRITTEN in
# place; with nothing configured the stock text goes out untouched.
#
# Matched on the "⏳ Gateway <gerund>" head rather than the whole sentence, so
# an upstream rewording of the tail still matches. The queued variant is told
# apart by its own clause because it carries information the other two do not:
# the message was KEPT, not refused, and saying "ask me later" about a message
# that is already queued would be a lie.
_DRAIN_NOTICE_RE = re.compile(r"^⏳\s*Gateway\s+(?:is\s+)?(?:shutting down|restarting)\b")
_DRAIN_QUEUED_RE = re.compile(r"queued for the next turn", re.IGNORECASE)

# Gateway STALL notices. When a turn produces no tool call and no API response for
# `agent.gateway_timeout`, gateway/run.py interrupts it and posts a diagnostic built
# from _diag_lines (run.py ~25378): the inactivity headline, then either "stuck on
# tool `x`" or "Last activity: …", then instructions to edit config.yaml and restart
# the gateway. That last part is operator language and it reached a public room on
# 2026-08-08, telling strangers to set `agent.gateway_timeout` and use /reset.
#
# Same treatment as the drain notice, for the same reason: someone spoke and is owed
# an answer, in the room they spoke in — so it is REWRITTEN in place, never dropped
# or rerouted. With nothing configured the stock text goes out, which is ugly but
# honest; a swallowed reply would be worse.
#
# Matched on the headline only. The tail carries the diagnosis (which tool, how many
# seconds, which iteration) and varies per incident, so anchoring to it would match
# one shape of stall and miss the rest. `\s*[-—–]` because outbound hygiene rewrites
# em dashes before this runs on some paths, and the notice must still be recognised
# after its own punctuation has been normalised.
_STALL_NOTICE_RE = re.compile(r"^⏱️\s*Agent inactive for\b")

# ---- outbound text hygiene ------------------------------------------------
# CONTROL-TOKEN LEAKAGE. Models trained on OpenAI's "harmony" chat format
# (gpt-oss and its many free-tier rebadges) express a tool call as
# `<|channel|>commentary to=functions.name<|constrain|>json<|message|>{...}`.
# When the serving endpoint does not parse that format back into a structured
# tool call, the raw control text falls through as ordinary assistant content
# and the bot posts its own plumbing to the channel. Observed 2026-08-06:
# Companion answered a GIF request with the literal string
# `to=functions.tool_call?commentary?…?…???`.
#
# This is never intentional output, so stripping is always safe. What is NOT
# safe is posting the remainder when nothing survives: a message of stray
# punctuation reads as the bot glitching. Those are suppressed instead.
# Harmony is an envelope, not loose tokens, so it is parsed as one:
#   <|start|>ROLE<|channel|>CHANNEL<|constrain|>FMT<|message|>PAYLOAD<|end|>
# Stripping token-by-token is not enough — it leaves the channel name and the
# tool-argument JSON behind as visible text. Only the payload of a `final`
# channel is real output; `commentary`/`analysis` payloads are a tool call or
# private reasoning and must never reach the room.
_HARMONY_TOKEN_RE = re.compile(r"<\|[a-z_]{1,24}\|>", re.IGNORECASE)
_HARMONY_MSG_RE = re.compile(r"^(?P<head>.*)<\|message\|>(?P<body>.*)$", re.S | re.I)
_REASONING_CHANNEL_RE = re.compile(r"\b(?:commentary|analysis)\b", re.IGNORECASE)
# A recipient marker is decisive on its own: `to=functions.x` is how harmony
# addresses a tool, and no genuine chat reply contains it. This catches the
# degenerate form that has no angle-bracket tokens left to parse at all
# (observed: `to=functions.tool_call?commentary?…?…???`).
_TOOL_RECIPIENT_RE = re.compile(r"(?<![\w.])to\s*=\s*functions?\.", re.IGNORECASE)
# A scrubbed message is worth posting only if some real language survived.
_HAS_WORD_RE = re.compile(r"\w{2,}")

# EM DASH. Every model reaches for it and it reads as machine-written, which
# is precisely the tell an in-character persona bot should not have. Rewritten
# rather than deleted so the clause boundary the model intended survives.
# Spaced en dash is punctuation too; an UNspaced en dash is a numeric range
# (1–5) and is deliberately left alone.
_EM_DASH_RE = re.compile(r"\s*[—―]\s*")
_SPACED_EN_DASH_RE = re.compile(r"\s+–\s+")
# Fenced code is exempt from the dash rewrite: inside a fence a dash may be
# data. Split on the fence delimiter and rewrite only the odd (outside) parts.
_CODE_FENCE_RE = re.compile(r"(```)")

# MEDIA URL ISOLATION. Discord's client hides the raw URL and renders only the
# media when a message's ENTIRE content is one media link — which is why a GIF
# from the built-in picker looks clean: the picker posts the URL and nothing
# else. One character of surrounding text and the client falls back to showing
# the link as text with the embed underneath. Models never post a bare URL;
# they wrap it in chatter, so a GIF reply reads as link + text + embed.
# Matching is done per whitespace-separated token, not with one regex over the
# whole message: a greedy URL pattern silently swallows trailing punctuation
# and the neighbouring word.
# SPEAKER-TAG ECHO. This plugin prefixes every INBOUND message with
# `[speaker @handle id:123]` so the agent can key memories on stable identity.
# Models routinely infer the wrong thing from that: "messages start with a
# speaker tag, I am writing a message, therefore mine starts with one too", and
# emit their own reply prefixed with a tag naming themselves. Observed
# 2026-08-06: Companion opened a reply with `[speaker @companion id:123]` — the id
# copied verbatim from the *example* in her own AGENTS.md.
#
# Her instructions say never to echo it, in two separate files. She did anyway,
# because a strong structural pattern beats a prose prohibition on a small
# model. The tag is ours, injected by us, so stripping it on the way out is
# ours too: prompt guidance is necessary but demonstrably not sufficient.
_SPEAKER_ECHO_RE = re.compile(r"\[speaker\b[^\]\n]{0,120}\]", re.IGNORECASE)

# The ambient DIRECTIVE echo — the same failure as the speaker tag above, in the
# other thing we inject into the user turn, and it took a public leak to notice
# the rule had only ever been written for one of the two.
#
# 2026-08-08: an agent posted its own ambient hint verbatim followed by `(SILENT)`
# — 201 chars, the exact length the turn logged. Conditions: model=openrouter/free,
# in=31700 tokens, and two failed provider attempts (EmptyStreamError, then a 36s
# timeout) before the retry that answered. A degraded small model on a long context
# regurgitated the tail of its input. Prompt guidance cannot prevent that; only a
# deterministic outbound rule can, which is why this lives here and not in SOUL.md.
#
# Greedy to the last `]` on the line, because the hint CONTAINS a bracketed marker
# (`... reply with exactly [SILENT] ...`) and an exclusion class would stop at it.
# Bounded to one line and 400 chars so it cannot run away. Over-stripping degrades
# to a suppressed reply, never to a leaked prompt — that is the safe direction.
_AMBIENT_ECHO_RE = re.compile(r"\[ambient\b[^\n]{0,400}\]", re.IGNORECASE)

# Letters only, for sentinel comparison. The marker is `[SILENT]`, but a small
# model reaches for `(SILENT)`, `**[SILENT]**` or a bare `SILENT` just as readily,
# and the original exact-match test posted every one of those to the channel.
_NON_LETTER_RE = re.compile(r"[^A-Za-z]")


def _looks_like_sentinel(text: str, marker: str) -> bool:
    """True when `text` is the silence marker in any plausible dress.

    Compares letters only, so `(SILENT)`, `**[SILENT]**`, `[silent].` and a bare
    `SILENT` all read as silence — while `I am silent tonight` does not, because
    its letters spell something longer. The length guard keeps a long reply that
    happens to reduce to the right letters from being swallowed.
    """
    core = _NON_LETTER_RE.sub("", marker or "").upper()
    if not core:
        return False
    stripped = (text or "").strip()
    if not stripped or len(stripped) > len(marker) + 24:
        return False
    return _NON_LETTER_RE.sub("", stripped).upper() == core


def _screen_ambient_reply(content: str, marker: str) -> tuple:
    """Strip any echoed ambient directive, then decide if anything is left to say.

    Returns `(text_or_None, n_stripped)`. None means "send nothing": either the
    reply was the silence marker, or it was nothing but the echoed directive.

    Ordering matters and is the whole point. The directive is removed FIRST, so a
    reply of `<hint>\\n\\n(SILENT)` — the exact 2026-08-08 leak — reduces to
    `(SILENT)` and is then recognised as silence. Checking the sentinel first (what
    the code did before) saw only a 201-char string that matched nothing.

    The emptiness test is gated on `n_stripped` so behaviour is untouched for
    replies we did not modify: an emoji-only reply has no word characters and must
    still go out.
    """
    text, n = _AMBIENT_ECHO_RE.subn(" ", content or "")
    if n:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if _looks_like_sentinel(text if n else content, marker):
        return None, n
    if n and not _HAS_WORD_RE.search(text):
        return None, n
    return (text if n else content), n


# Recall backends bracket their output with chatter rather than facts: a trailing
# "3 matches." count, and "No match." when nothing was found. Both must be dropped —
# testing against the real store caught an unknown speaker rendering as
# "[known No match.]", which is worse than saying nothing at all.
_RECALL_NOISE_RE = re.compile(r"^(no\s+match(es)?|\d+\s+match(es)?)\.?$", re.IGNORECASE)

_URL_TOKEN_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MEDIA_EXT_RE = re.compile(r"\.(?:gif|gifv|mp4|webm|png|jpe?g|webp)$", re.IGNORECASE)
# Hosts whose *page* URLs Discord resolves to a media embed even though the
# path carries no file extension.
_MEDIA_HOST_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:tenor\.com/view/|giphy\.com/gifs/|klipy\.com/)",
    re.IGNORECASE,
)


def _media_url(token: str) -> str | None:
    """Return the media URL in this token, or None.

    Sentence punctuation trailing a URL is trimmed before matching — models
    write "here you go https://x/y.gif." and the period is prose, not path.
    The trimmed form is what gets posted; the period is dropped with the rest
    of the surrounding text, which is the point of isolating the URL.
    """
    token = token.rstrip(".,!?;:")
    if not _URL_TOKEN_RE.match(token):
        return None
    if _MEDIA_HOST_RE.match(token):
        return token
    # Strip query/fragment before testing the extension: a CDN URL may end in
    # `.gif?w=480`, which still serves a GIF.
    path = token.split("#", 1)[0].split("?", 1)[0]
    return token if _MEDIA_EXT_RE.search(path) else None


def _id_set(value) -> set:
    """Normalize a config id list to a set of strings.

    Accepts a YAML list, a comma-separated string, or a bare scalar — and the
    scalar case is not hypothetical: `hermes config set` coerces a numeric
    value, so a Discord snowflake written through the CLI arrives as an INT,
    not a string. Treating that as "unset" silently disables whatever policy
    depends on it (this bit the slash-command gate on 2026-08-05).
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        parts = value
    elif isinstance(value, str):
        parts = value.split(",")
    else:
        parts = [value]
    return {str(p).strip() for p in parts if str(p).strip()}

# Default patterns for group-addressed greetings ("good morning agents",
# "hello everyone", "agents, assemble"). Matched with re.search against the
# lowercased message content. Deliberately require BOTH a greeting word and a
# collective address within a short span — a bare "agents" appears in normal
# conversation far too often to be a trigger on its own.
_GROUP_ADDRESS_PATTERNS = [
    r"\b(morning|mornin|gm|gn|hello|hi|hey+|yo|evening|night|greetings|sup|hiya|heya)\b"
    r"[^.!?\n]{0,30}"
    r"\b(agents?|everyone|every1|all|bots|chat|guys|gang|frens|friends|folks)\b",
    r"\b(agents?|everyone|every1|bots|chat|folks)\b"
    r"[^.!?\n]{0,30}"
    r"\b(morning|mornin|gm|gn|hello|hi|hey+|evening|night|assemble)\b",
]


class AmbientDiscordAdapter(DiscordAdapter):
    """Stock Discord adapter plus opt-in presence, reactions and memory hooks."""

    def __init__(self, config):
        super().__init__(config)
        self._ambient_hits: deque[float] = deque(maxlen=512)
        self._ambient_last: float = 0.0
        self._react_last: float = 0.0
        self._presence_task: Any = None
        self._seen_path = self._ambient_seen_path()
        self._last_seen: dict[str, float] = self._load_seen()
        # Bot-bounce state, in-memory only (a restart resetting everyone's
        # patience is harmless). (channel_id, bot_id) -> count/limit/last.
        self._bounce_pairs: dict[tuple[str, str], dict[str, float]] = {}
        # message_id -> (channel_key, bot_id, dispatched_at): dispatched bot
        # messages whose reply has not been charged yet. Keyed by the INBOUND
        # message id because every live Discord reply carries it back as
        # send(reply_to=...) — exact correlation even when two bots interleave
        # in one channel or the reply lands in an auto-created thread.
        # Bounded (stale-purged + size-capped) in _bounce_note_dispatch.
        self._bounce_pending: dict[str, tuple[str, str, float]] = {}
        # Standby: (mtime-stamp, ids) cache of no-agent cron job ids.
        self._standby_noagent_cache: tuple | None = None
        # chat_id -> (when she last spoke, inbound messages since). A message
        # arriving just after she spoke is usually a reply to her, even when it
        # carries no mention and no name — Discord's reply affordance is
        # optional and most people do not use it.
        self._spoke: dict[str, tuple[float, int]] = {}
        # speaker id -> (fetched_at, rendered block or None). Bounded by the
        # channel's distinct speakers; a stale entry costs one slightly-old
        # fact, so a short TTL beats invalidation plumbing.
        self._mem_cache: dict[str, tuple[float, str | None]] = {}
        # Catch-up (idle check-in) state. All in-memory: a gateway restart
        # forgetting that she caught up an hour ago costs at most one extra
        # check-in, and the sub-cap plus the shared ambient budget both still
        # apply. `_catchup_seen` is the anti-repeat guard — the newest message
        # ids she has already read once, so a room that goes quiet after one
        # exchange cannot be caught up on over and over.
        self._catchup_task: Any = None
        self._catchup_hits: deque[float] = deque(maxlen=128)
        self._catchup_last: float = 0.0
        self._catchup_seen: deque[str] = deque(maxlen=256)
        # inbound message id -> dispatched_at, for check-ins whose reply has not
        # been charged to the SHARED ambient budget yet. Charged at send, never
        # at dispatch: a check-in that ends in [SILENT] put no words in the
        # room, and silencing her normal ambient replies for the next half hour
        # over a turn nobody saw is exactly the wrong trade.
        self._catchup_pending: dict[str, float] = {}
        # (set_at, the transcript lines most recently handed to the model). The
        # echo guard reads it in send(); see _catchup_echo_leak. One check-in is
        # in flight at a time, so a single slot is enough.
        self._catchup_echo: tuple[float, set] = (0.0, set())
        # When a fallback-switch notice last named a local model (0 = never).
        # In-memory only: a restart forgets an open window, and the next
        # fallback notice simply re-opens it — degrade is "no hold", not
        # "held forever".
        self._local_fallback_ts: float = 0.0

    # ---- config ---------------------------------------------------------
    def _ambient_cfg(self) -> dict:
        try:
            cfg = self.config.extra.get("ambient_presence")
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _ambient_enabled(self) -> bool:
        return bool(self._ambient_cfg().get("enabled"))

    def _ambient_marker(self) -> str:
        return str(self._ambient_cfg().get("silent_marker") or "[SILENT]").strip()

    def _sub(self, key: str) -> dict:
        v = self._ambient_cfg().get(key)
        return v if isinstance(v, dict) else {}

    # ---- last-seen persistence (survives gateway restarts) ---------------
    def _ambient_seen_path(self) -> str:
        try:
            home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
            return os.path.join(home, "state", "ambient-last-seen.json")
        except Exception:
            return "/tmp/ambient-last-seen.json"

    def _load_seen(self) -> dict:
        try:
            with open(self._seen_path) as fh:
                data = json.load(fh)
            return {str(k): float(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_seen(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._seen_path), exist_ok=True)
            trimmed = dict(sorted(self._last_seen.items(), key=lambda kv: kv[1])[-500:])
            tmp = f"{self._seen_path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(trimmed, fh)
            os.replace(tmp, self._seen_path)
        except Exception:
            logger.debug("ambient: could not persist last-seen", exc_info=True)

    # ---- the seam -------------------------------------------------------
    def _discord_free_response_channels(self) -> set:
        if _ambient_open.get():
            return {"*"}  # satisfies BOTH mention gates for this one dispatch
        return super()._discord_free_response_channels()

    def _conversation_window(self, message: Any) -> dict | None:
        """Overrides for a message that lands in her conversational wake.

        WHY: being addressed does not require being named. Someone answers what
        she just said and neither @-mentions her nor uses the reply affordance,
        so every stock signal misses it — and the dice-roll that decides is tuned
        for a room of strangers, not for the person mid-conversation with her.
        The result is an agent that ignores the reply to its own question.

        The window is deliberately BOTH bounded ways. Message count alone would
        keep it open across a quiet night; elapsed time alone would keep it open
        through fifty messages of someone else's conversation. Being the second
        message after she spoke, four hours later, is not a reply to her.
        """
        cfg = self._ambient_cfg().get("conversation_window")
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return None
        try:
            key = str(getattr(message, "channel", None) and
                      getattr(message.channel, "id", "") or "")
            spoke = self._spoke.get(key)
            if not spoke:
                return None
            when, since = spoke
            if since > int(cfg.get("messages", 3)):
                return None
            if time.time() - when > float(cfg.get("seconds", 300)):
                return None
            return {
                "probability": float(cfg.get("probability", 0.8)),
                "cooldown_seconds": float(cfg.get("cooldown_seconds", 30)),
                "exempt_daily_cap": bool(cfg.get("exempt_daily_cap", True)),
            }
        except Exception:
            logger.debug("ambient: conversation window check failed", exc_info=True)
            return None

    def _note_inbound_for_window(self, message: Any) -> None:
        """Count one inbound message against the open window, if any."""
        try:
            key = str(getattr(message, "channel", None) and
                      getattr(message.channel, "id", "") or "")
            spoke = self._spoke.get(key)
            if spoke:
                self._spoke[key] = (spoke[0], spoke[1] + 1)
        except Exception:
            pass

    def _speaker_boost(self, message: Any) -> dict:
        """Per-speaker ambient overrides, or {}.

        Ambient presence is deliberately tuned for a room full of strangers: a
        low dice-roll and a long cooldown, so she is a presence rather than a
        chatterbox. But the person who runs the bot is not a stranger, and
        applying stranger-tuning to them means the one human most likely to
        want a reply gets the same 15% as everyone else.

        Keyed on the numeric id for the same reason the speaker tag is: handles
        are user-editable, and a rename would silently transfer someone else's
        boost. Config shape:

            speaker_boost:
              "553...":
                probability: 0.6
                cooldown_seconds: 120
                exempt_daily_cap: true
        """
        try:
            table = self._ambient_cfg().get("speaker_boost")
            if not isinstance(table, dict):
                return {}
            uid = str(getattr(getattr(message, "author", None), "id", "") or "")
            hit = table.get(uid) if uid else None
            return hit if isinstance(hit, dict) else {}
        except Exception:
            return {}

    def _ambient_quota_ok(self, boost: dict | None = None) -> bool:
        cfg = self._ambient_cfg()
        boost = boost or {}
        now = time.time()
        # A boosted speaker may carry a shorter floor. Without this the boost is
        # mostly decorative: the daily cooldown gates the roll, so raising only
        # the probability changes almost nothing.
        cooldown = float(boost.get("cooldown_seconds", cfg.get("cooldown_seconds", 1800)))
        if now - self._ambient_last < cooldown:
            return False
        cutoff = now - 86400
        while self._ambient_hits and self._ambient_hits[0] < cutoff:
            self._ambient_hits.popleft()
        if boost.get("exempt_daily_cap"):
            return True
        return len(self._ambient_hits) < int(cfg.get("max_per_day", 12))

    def _channel_allowed(self, message: Any) -> bool:
        cfg = self._ambient_cfg()
        allow = {str(c).strip().lower() for c in (cfg.get("channels") or []) if str(c).strip()}
        if not allow:
            return False  # unset = ambient off; opt in explicitly
        if "*" in allow:
            # "*" = anywhere this bot can already see. Discord's own channel
            # permissions are then the allowlist — right for a bot that lives
            # in one community server, wrong for one in many.
            return True
        keys = {str(message.channel.id)}
        parent = self._get_parent_channel_id(message.channel)
        if parent:
            keys.add(str(parent))
        return bool(keys & allow)

    def _basic_ambient_eligible(self, message: Any) -> bool:
        """Shared safety floor. Auth/dedup/bot policy are NOT checked here —
        the re-dispatch re-runs all of them."""
        import discord  # the same dependency the stock adapter uses

        if message.author == self._client.user:
            return False
        if getattr(message.author, "bot", False):
            return False
        if isinstance(message.channel, discord.DMChannel):
            return False  # DMs are always answered anyway
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return False
        if not (getattr(message, "content", "") or "").strip():
            return False
        return self._channel_allowed(message)

    def _returning_after_absence(self, message: Any) -> float:
        """Days since this author last spoke, or 0 if not a notable return."""
        rc = self._sub("return_greeting")
        if not rc.get("enabled"):
            return 0.0
        try:
            uid = str(message.author.id)
            now = time.time()
            prev = self._last_seen.get(uid)
            self._last_seen[uid] = now
            self._save_seen()
            if prev is None:
                return 0.0  # first sighting ever isn't a "return"
            days = (now - prev) / 86400.0
            return days if days >= float(rc.get("absence_days", 3)) else 0.0
        except Exception:
            return 0.0

    def _join_reason(self, message: Any) -> str | None:
        """Return "named", "random", or None.

        Name triggers deliberately BYPASS the ambient cooldown and the daily
        cap: typing the bot's name is addressing it, and being told "not now,
        I already spoke recently" reads as broken rather than as restraint.
        Only a short anti-spam floor applies to them. The long cooldown exists
        to stop the bot inserting itself into conversations uninvited, which is
        a different thing entirely.
        """
        cfg = self._ambient_cfg()
        try:
            if not self._basic_ambient_eligible(message):
                return None
            content = (getattr(message, "content", "") or "").lower()
            # A plain-text name reference is the case Discord's @-mention
            # detection misses entirely.
            triggers = [str(t).lower() for t in (cfg.get("name_triggers") or [])]
            if triggers and any(t in content for t in triggers):
                floor = float(cfg.get("name_cooldown_seconds", 60))
                if time.time() - self._ambient_last < floor:
                    return None
                return "named"
            # Group-addressed greetings ("good morning agents") sit between a
            # name trigger and the random dice: addressed to the room rather
            # than to her, so answering is polite but not owed. Rolled at its
            # own probability, floored by its own short cooldown, and exempt
            # from the daily cap (like name triggers — being spoken to is not
            # "inserting herself").
            ga = self._sub("group_address")
            if ga.get("enabled"):
                pats = [str(p) for p in (ga.get("patterns") or _GROUP_ADDRESS_PATTERNS)]
                hit = False
                for p in pats:
                    try:
                        if re.search(p, content):
                            hit = True
                            break
                    except re.error:
                        continue
                if hit:
                    floor = float(ga.get("cooldown_seconds", 300))
                    if time.time() - self._ambient_last >= floor and (
                        random.random() < float(ga.get("probability", 0.6))
                    ):
                        return "named"
                    return None
            # A reply to her outranks the generic per-speaker tuning: this is
            # someone continuing a conversation she started, not a stranger
            # talking in the room.
            boost = self._conversation_window(message) or self._speaker_boost(message)
            if not self._ambient_quota_ok(boost):
                return None
            chance = float(boost.get("probability", cfg.get("probability", 0.12)))
            return "random" if random.random() < chance else None
        except Exception:
            logger.debug("ambient pre-filter failed; falling back to stock", exc_info=True)
            return None

    # ---- reactions (zero inference) --------------------------------------
    def _pick_reaction(self, message: Any) -> str | None:
        rc = self._sub("reactions")
        if not rc.get("enabled"):
            return None
        now = time.time()
        if now - self._react_last < float(rc.get("cooldown_seconds", 120)):
            return None
        if random.random() >= float(rc.get("probability", 0.15)):
            return None
        content = (getattr(message, "content", "") or "").lower()
        for pattern, emojis in (rc.get("keywords") or {}).items():
            try:
                if emojis and re.search(str(pattern), content):
                    return random.choice(list(emojis))
            except re.error:
                continue
        pool = list(rc.get("default") or _DEFAULT_REACTIONS)
        return random.choice(pool) if pool else None

    async def _maybe_react(self, message: Any) -> None:
        try:
            if not self._basic_ambient_eligible(message):
                return
            emoji = self._pick_reaction(message)
            if not emoji:
                return
            if await self._add_reaction(message, emoji):
                self._react_last = time.time()
                logger.debug("ambient: reacted %s", emoji)
        except Exception:
            logger.debug("ambient reaction failed", exc_info=True)

    # ---- bot-conversation circuit breaker ("bounces") ---------------------
    # Under DISCORD_ALLOW_BOTS + require_mention among agents, two bots whose
    # replies auto-@mention each other volley forever — upstream documents the
    # topology as unsupported, with NO breaker. Sit BEFORE the stock dispatch:
    # a tripped pair returns False before admission even runs, so suppression
    # costs zero inference and zero reply. Counting happens at send() time,
    # not dispatch time, so a [SILENT]-swallowed reply stays free.

    def _bounce_enabled(self) -> bool:
        return bool(self._sub("bot_bounce").get("enabled"))

    def _bounce_reset_after(self) -> float:
        return float(self._sub("bot_bounce").get("reset_after_seconds", 1800))

    def _bounce_probability(self) -> float:
        """Chance of engaging with any one bot message. 1.0 (default) = stock.

        Distinct from min/max_replies, which bound how long an exchange runs
        once it has started. This bounds how often one starts at all — the
        breaker alone still lets every volley begin, it only ends them.
        """
        try:
            return min(1.0, max(0.0, float(self._sub("bot_bounce").get("probability", 1.0))))
        except Exception:
            return 1.0  # unreadable config must never mute her

    def _bounce_pair(self, channel_key: str, bot_id: str) -> dict:
        """Live state for one (channel, bot) conversation, re-rolled if stale."""
        now = time.time()
        key = (channel_key, bot_id)
        pair = self._bounce_pairs.get(key)
        if pair is not None and now - pair["last"] > self._bounce_reset_after():
            pair = None  # the conversation went quiet; her patience renews
        if pair is None:
            bc = self._sub("bot_bounce")
            lo = max(1, int(bc.get("min_replies", 3)))
            hi = max(lo, int(bc.get("max_replies", 5)))
            # Rolled per conversation so her patience varies naturally.
            pair = {"count": 0, "limit": random.randint(lo, hi), "last": now}
            self._bounce_pairs[key] = pair
            if len(self._bounce_pairs) > 256:
                # Drop dead pairs first (past reset_after — this method would
                # re-roll them anyway), then the oldest LIVE pair. A tripped
                # pair is evicted only when nothing else remains: its "last"
                # is frozen by design (suppression must not refresh it), so a
                # plain LRU would preferentially evict tripped pairs and
                # silently un-trip the breaker in a busy server.
                cutoff = now - self._bounce_reset_after()
                for k in [k for k, p in self._bounce_pairs.items()
                          if p["last"] < cutoff and k != key]:
                    self._bounce_pairs.pop(k, None)
                while len(self._bounce_pairs) > 256:
                    live = [k for k, p in self._bounce_pairs.items()
                            if p["count"] < p["limit"] and k != key]
                    pool = live or [k for k in self._bounce_pairs if k != key]
                    if not pool:
                        break
                    oldest = min(pool, key=lambda k: self._bounce_pairs[k]["last"])
                    self._bounce_pairs.pop(oldest, None)
        return pair

    def _bounce_reset_channel(self, channel_key: str) -> None:
        """A human spoke here: the conversation moved on. All pairs reset."""
        for key in [k for k in self._bounce_pairs if k[0] == channel_key]:
            del self._bounce_pairs[key]
        for mid in [m for m, v in self._bounce_pending.items() if v[0] == channel_key]:
            del self._bounce_pending[mid]

    def _bounce_gate(self, message: Any) -> str | None:
        """Return "suppress", "goodbye", "count", or None (stay stock).

        Humans are NEVER gated — a human message only resets the channel's
        pairs. The gate itself never refreshes a pair's clock: "last" moves
        only at pair creation and when a reply is actually charged
        (_bounce_count_sent). That is what makes reset_after_seconds measure
        the CONVERSATION (her replies to that bot) — a bot chattering at a
        tripped pair, or at some third bot in the channel, cannot defer the
        renewal that the config promises.
        """
        try:
            if not (self._ambient_enabled() and self._bounce_enabled()):
                return None
            if message.author == self._client.user:
                return None  # her own outbound events; not a conversation
            channel_key = str(getattr(getattr(message, "channel", None), "id", "") or "")
            if not channel_key:
                return None
            if not getattr(message.author, "bot", False):
                self._bounce_reset_channel(channel_key)
                return None
            bot_id = str(getattr(message.author, "id", "") or "")
            if not bot_id:
                return None
            # Engagement roll, BEFORE any pair state is touched. Creating the
            # pair here would start its reset_after_seconds clock on a
            # conversation she never joined, and burn one of the rolled replies
            # on a message she never answered. Reported as "suppress" so both
            # dispatch paths treat it exactly like a tripped pair: no
            # admission, no inference, and the id claimed against backfill
            # replay (an unclaimed skip comes back on the next reconnect).
            prob = self._bounce_probability()
            if prob < 1.0 and random.random() >= prob:
                logger.info(
                    "ambient.bot_bounce: skipped bot %s in %s (engagement roll, p=%.2f)",
                    bot_id, channel_key, prob,
                )
                return "suppress"
            pair = self._bounce_pair(channel_key, bot_id)
            if pair["count"] >= pair["limit"]:
                logger.info(
                    "ambient.bot_bounce: suppressed bot %s in %s (%d/%d replies spent)",
                    bot_id, channel_key, int(pair["count"]), int(pair["limit"]),
                )
                return "suppress"
            return "goodbye" if pair["count"] == pair["limit"] - 1 else "count"
        except Exception:
            logger.debug("ambient.bot_bounce gate failed; falling back to stock", exc_info=True)
            return None

    def _bounce_note_dispatch(self, message: Any) -> None:
        """Mark this dispatched bot message so the reply anchored to it is
        charged to the right pair.

        The agent's reply is produced on another task (possibly after text
        batching), so a ContextVar cannot correlate dispatch with send. What
        DOES survive the task hop is the reply anchor: base.py routes every
        live Discord reply through send(reply_to=<inbound message id>). Keying
        the marker on the message id — not the channel — means two bots
        interleaving in one channel each get charged for exactly their own
        reply (a channel-keyed marker was overwritten by whichever bot spoke
        last, cross-charging pairs), and a reply that lands in an auto-created
        thread still finds its marker even though its chat_id differs from
        the channel the pair is keyed on.
        """
        try:
            self._bounce_pending[str(message.id)] = (
                str(message.channel.id), str(message.author.id), time.time(),
            )
            if len(self._bounce_pending) > 128:
                # Replies that never materialize would otherwise pin their
                # markers forever: purge stale, then cap hard.
                cutoff = time.time() - max(600.0, self._bounce_reset_after())
                for mid in [m for m, v in self._bounce_pending.items() if v[2] < cutoff]:
                    self._bounce_pending.pop(mid, None)
                while len(self._bounce_pending) > 128:
                    oldest = min(self._bounce_pending, key=lambda m: self._bounce_pending[m][2])
                    self._bounce_pending.pop(oldest, None)
        except Exception:
            pass

    def _bounce_discard_pending(self, reply_to: Any) -> None:
        try:
            self._bounce_pending.pop(str(reply_to or ""), None)
        except Exception:
            pass

    def _bounce_count_sent(self, reply_to: Any, result: Any) -> None:
        """Charge one actually-sent reply to the bot message it answers."""
        try:
            key = str(reply_to or "")
            pending = self._bounce_pending.get(key)
            if pending is None:
                return
            if result is not None and not getattr(result, "success", True):
                # The reply never landed. LEAVE the marker: _send_with_retry
                # retries the same reply with the same reply_to, and a retry
                # that succeeds must still be charged.
                return
            self._bounce_pending.pop(key, None)
            channel_key, bot_id, dispatched_at = pending
            if time.time() - dispatched_at > max(600.0, self._bounce_reset_after()):
                return  # stale marker from a long-dead dispatch
            pair = self._bounce_pairs.get((channel_key, bot_id))
            if pair is None:
                return  # reset in the meantime (human spoke, or timed out)
            pair["count"] += 1
            pair["last"] = time.time()
            logger.info(
                "ambient.bot_bounce: reply %d/%d to bot %s in %s",
                int(pair["count"]), int(pair["limit"]), bot_id, channel_key,
            )
        except Exception:
            logger.debug("ambient.bot_bounce count failed", exc_info=True)

    def _bounce_pre_dispatch(self, message: Any) -> str | None:
        """Run the gate and apply its verdict's pre-dispatch side effects.

        Shared by the live path (_dispatch_inner) and the recovered path
        (_dispatch_recovered_message) so backfill replay cannot slip past
        the breaker.
        """
        verdict = self._bounce_gate(message)
        if verdict == "suppress":
            # Claim the id in the dedup cache. Missed-message backfill treats
            # an unclaimed, never-answered message as "missed" and replays it
            # via _dispatch_recovered_message — so an unclaimed suppression
            # would manufacture backfill debt: every suppressed message comes
            # back after a reconnect, costing the inference and the reply the
            # breaker existed to prevent. Claimed = invisible to the scan.
            try:
                self._dedup.is_duplicate(str(getattr(message, "id", "")))
            except Exception:
                pass
        elif verdict == "goodbye":
            try:
                who = getattr(message.author, "display_name", "the other bot")
                hint = str(
                    self._sub("bot_bounce").get("goodbye_hint") or _GOODBYE_HINT
                ).replace("{who}", str(who))
                _ambient_hint.set(hint)
                logger.info(
                    "ambient.bot_bounce: last allowed reply to %s — goodbye hint injected",
                    who,
                )
            except Exception:
                pass  # breaker still trips next round even if the nudge is lost
        return verdict

    # ---- fleet standby (one shared inference slot) ---------------------
    # A held dispatch is just this message's own coroutine sleeping — nothing
    # global blocks. Two accepted imperfections, both bounded: a held message
    # can release AFTER a newer one that arrived once the slot freed (per-
    # channel FIFO would need global machinery for a rare cosmetic case), and
    # a gateway shutdown mid-hold cancels the coroutine like any in-flight
    # work (enable missed_message_backfill if that window matters — a held id
    # is dedup-claimed only for the duration of the hold, see _standby_wait).
    # The probe reaches gateway internals, so every access is optional and
    # every exception answers "not busy" — the failure mode of a broken probe
    # is stock behaviour, never a silent bot.

    def _standby_enabled(self) -> bool:
        return bool(self._ambient_enabled() and self._sub("standby").get("enabled"))

    def _standby_engaged(self) -> bool:
        """Whether standby should act right now, given only_when_local.

        Stock standby holds whenever the fleet is busy — correct when this
        profile's PRIMARY model is the shared local one. A profile that
        normally runs hosted only contends for the local slot after falling
        back to it, so only_when_local keeps standby dormant until a
        fallback-switch notice naming a local model has been observed
        (_note_fallback_notice), and lets it lapse local_fallback_ttl_seconds
        later. Fallback state is per-session upstream and sessions retry
        their primary, so the TTL is a freshness heuristic: too short means
        a few unheld local turns (stock behaviour), too long means bounded
        extra delay while cloud is already back — both safe.
        """
        try:
            sc = self._sub("standby")
            if not sc.get("only_when_local"):
                return True
            ttl = float(sc.get("local_fallback_ttl_seconds", 1800))
            return bool(self._local_fallback_ts) and (
                time.time() - self._local_fallback_ts < ttl
            )
        except Exception:
            return True  # misconfig degrades to stock standby, never a mute

    def _note_fallback_notice(self, notice: str) -> None:
        """Open (or refresh) the local-fallback standby window when the
        switch target names a local model. Runs whether or not the notice is
        then suppressed — observation and presentation are separate."""
        try:
            target = notice.split("→", 1)[1] if "→" in notice else notice
            markers = self._sub("standby").get("local_markers") or ["gpt-oss", "ollama"]
            if any(str(m).lower() in target.lower() for m in markers):
                self._local_fallback_ts = time.time()
                logger.info(
                    "ambient: local-model fallback observed — only_when_local "
                    "standby window open"
                )
        except Exception:
            pass

    def _standby_noagent_ids(self) -> set:
        """Ids of cron jobs marked no_agent (plain scripts — they hold no
        inference slot). Cached on the jobs.json mtimes; an id we cannot
        attribute counts as inference-consuming (conservative: worst case is
        a bounded defer, never a lost message)."""
        try:
            home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
            paths = [os.path.join(home, "cron", "jobs.json")]
            prof_root = os.path.join(home, "profiles")
            try:
                for name in os.listdir(prof_root):
                    paths.append(os.path.join(prof_root, name, "cron", "jobs.json"))
            except Exception:
                pass
            stamp = tuple(
                (p, os.path.getmtime(p)) for p in paths if os.path.exists(p)
            )
            cached = self._standby_noagent_cache
            if cached is not None and cached[0] == stamp:
                return cached[1]
            ids: set = set()
            for p, _m in stamp:
                try:
                    with open(p) as fh:
                        data = json.load(fh)
                    for job in data.get("jobs", []) or []:
                        if isinstance(job, dict) and job.get("no_agent"):
                            ids.add(str(job.get("id")))
                except Exception:
                    continue
            self._standby_noagent_cache = (stamp, ids)
            return ids
        except Exception:
            return set()

    def _fleet_busy(self) -> bool:
        """True while any agent turn or agent-mode cron job holds the slot.

        Her OWN running turns count too, deliberately: on one slot her second
        conversation should queue behind her first, exactly like everyone
        else's work. No deadlock is possible — the registry entry for THIS
        message's turn is only created after dispatch proceeds, and the wait
        is deadline-bounded regardless.
        """
        try:
            sc = self._sub("standby")
            runner = getattr(self, "gateway_runner", None)
            if runner is None:
                try:
                    from gateway.run import _gateway_runner_ref  # type: ignore

                    runner = _gateway_runner_ref()
                except Exception:
                    runner = None
            if runner is None:
                return False
            now = time.time()
            stale = float(sc.get("stale_turn_seconds", 1800))
            try:
                agents = getattr(runner, "_running_agents", None)
                stamps = getattr(runner, "_running_agents_ts", None)
                for key in list(agents or ()):
                    ts = None
                    try:
                        ts = stamps.get(key) if stamps is not None else None
                    except Exception:
                        ts = None
                    if ts is not None and now - float(ts) > stale:
                        continue  # wedged entry; the runner self-heals these
                    return True
            except Exception:
                pass
            if sc.get("include_cron", True):
                try:
                    from cron.scheduler import get_running_job_ids  # type: ignore

                    running = {str(j) for j in (get_running_job_ids() or ())}
                    if running and (running - self._standby_noagent_ids()):
                        return True
                except Exception:
                    pass
            try:
                from tools.async_delegation import active_count  # type: ignore

                if int(active_count() or 0) > 0:
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    async def _standby_wait(self, message: Any = None) -> bool:
        """Hold this dispatch while the fleet owns the slot.

        Returns True only when the deadline passed with the slot still busy
        (the caller may use that to skip optional work); the message proceeds
        after this returns in every case short of the gateway itself shutting
        down mid-hold (task cancellation — the same fate any in-flight work
        meets at shutdown; missed_message_backfill covers that window when
        enabled).

        While parked, the message id is claimed in the dedup cache and
        released again just before dispatch: the missed-message backfill scan
        treats an unclaimed, unanswered message as "missed", so an unclaimed
        multi-minute hold would invite a parallel replay of the very message
        we are holding. Claim-park-release shrinks that window back to the
        stock-sized one.
        """
        try:
            if not self._standby_enabled():
                return False
            if not self._standby_engaged():
                return False
            if not self._fleet_busy():
                return False
            sc = self._sub("standby")
            poll = max(1.0, float(sc.get("poll_interval_seconds", 5)))
            deadline = time.time() + max(0.0, float(sc.get("max_wait_seconds", 240)))
            mid = str(getattr(message, "id", "") or "") if message is not None else ""
            claimed = False
            if mid:
                try:
                    self._dedup.is_duplicate(mid)
                    claimed = True
                except Exception:
                    pass
            try:
                logger.info("ambient.standby: slot busy — holding dispatch")
                while time.time() < deadline:
                    await asyncio.sleep(poll)
                    if not self._fleet_busy():
                        logger.info("ambient.standby: slot free — releasing held dispatch")
                        return False
                logger.info("ambient.standby: max_wait reached — dispatching anyway")
                return True
            finally:
                if claimed:
                    try:
                        self._dedup.discard(mid)
                    except Exception:
                        pass
        except Exception:
            logger.debug("ambient.standby wait failed; dispatching", exc_info=True)
            return False

    def _ambient_no_thread_token(self, message: Any):
        """ContextVar token scoping no-thread keys to one dispatch, or None."""
        if not (self._ambient_enabled() and self._ambient_cfg().get("no_threads")):
            return None
        try:
            keys = {str(message.channel.id)}
            parent = self._get_parent_channel_id(message.channel)
            if parent:
                keys.add(str(parent))
            return _no_thread_keys.set(keys)
        except Exception:
            return None

    async def _dispatch_discord_message(self, message: Any) -> bool:
        token = self._ambient_no_thread_token(message)
        # Open a fresh directive scope per dispatch and close it here. Both the
        # ambient and the bot-bounce goodbye path set the hint somewhere inside,
        # and neither should be able to leak one into the NEXT message's turn.
        hint_token = _ambient_hint.set("")
        try:
            return await self._dispatch_inner(message)
        finally:
            _ambient_hint.reset(hint_token)
            if token is not None:
                _no_thread_keys.reset(token)

    async def _dispatch_recovered_message(self, message: Any) -> bool:
        """Missed-message backfill dispatches recovered messages through here
        and NEVER through _dispatch_discord_message — without this override
        both the bounce gate and no-thread scoping are bypassed on replay.
        A tripped pair suppresses the replay at zero inference (and a
        suppressed message does not count against the scan's max_dispatches);
        everything else runs the stock recovery gates untouched.
        """
        token = self._ambient_no_thread_token(message)
        try:
            verdict = self._bounce_pre_dispatch(message)
            if verdict == "suppress":
                return False
            await self._standby_wait(message)  # backfill replays queue behind the slot too
            handled = await super()._dispatch_recovered_message(message)
            if handled and verdict in ("goodbye", "count"):
                self._bounce_note_dispatch(message)
            return handled
        finally:
            if token is not None:
                _no_thread_keys.reset(token)

    async def _dispatch_inner(self, message: Any) -> bool:
        # Stable speaker identity before anything reads the content, so both
        # the stock pass and any ambient re-dispatch carry it.
        self._apply_speaker_tag(message)
        self._note_inbound_for_window(message)

        # Bot bounce first: a tripped pair must cost NOTHING, so it returns
        # before admission runs. Every un-tripped message falls through to the
        # stock path with all auth/dedup/gate logic untouched.
        verdict = self._bounce_pre_dispatch(message)
        if verdict == "suppress":
            return False

        # Standby AFTER the suppress check (suppression must stay free) and
        # BEFORE the stock dispatch, so a held message costs nothing while
        # another profile owns the inference slot. still_busy is True only
        # when the deadline passed — the reply then proceeds anyway; only
        # optional dice-roll joins below consult it.
        still_busy = await self._standby_wait(message)

        # Stock path first: preserves the dedup claim and every auth gate.
        if await super()._dispatch_discord_message(message):
            if verdict in ("goodbye", "count"):
                self._bounce_note_dispatch(message)
            return True

        if not self._ambient_enabled():
            return False
        try:
            returning_days = self._returning_after_absence(message)
            eligible = self._basic_ambient_eligible(message)
            reason = self._join_reason(message)
            if not reason and eligible and returning_days and self._ambient_quota_ok():
                reason = "return"  # a real return beats the dice

            if not reason:
                await self._maybe_react(message)  # seen, but not worth words
                return False

            if (
                reason == "random"
                and still_busy
                and bool(self._sub("standby").get("drop_ambient_when_busy", True))
            ):
                # A dice-roll join is opportunistic traffic; spending the
                # contended slot on it is exactly what standby exists to stop.
                # Named triggers and returns still go through — those answer
                # actual people.
                await self._maybe_react(message)
                return False

            # The stock pass claimed this id in the deduplicator; release it so
            # the ambient re-run can claim it exactly once.
            self._dedup.discard(str(getattr(message, "id", "")))

            cfg = self._ambient_cfg()
            marker = self._ambient_marker()
            if returning_days:
                who = getattr(getattr(message, "author", None), "display_name", "they")
                hint = _RETURN_HINT.format(
                    who=who, days=int(returning_days), marker=marker
                )
            else:
                hint = str(cfg.get("hint") or _DEFAULT_HINT).replace("{marker}", marker)
            # NOT written into message.content: that is the user turn, and Hermes
            # persists it. See the _ambient_hint comment — this is the fix for the
            # 2026-08-08 self-portrait refusal.
            _ambient_hint.set(hint)

            token = _ambient_open.set(True)
            try:
                handled = await super()._dispatch_discord_message(message)
            finally:
                _ambient_open.reset(token)

            # Charge the budget ONLY for a join that actually reached the agent.
            # The re-dispatch re-runs every auth gate, so it can still be
            # refused (unauthorized author, ignored channel). Charging up front
            # let a refused message burn the cooldown and silence the bot for
            # the next half hour — including for people saying its name.
            if handled:
                now = time.time()
                self._ambient_last = now
                if reason == "random":
                    self._ambient_hits.append(now)
            return handled
        except Exception:
            logger.warning("ambient dispatch failed; message left unanswered", exc_info=True)
            return False

    # ---- stable speaker identity -----------------------------------------
    def _speaker_tag(self, message: Any) -> str | None:
        """A compact `[speaker @name id:123]` prefix, or None when disabled.

        WHY: upstream labels every inbound message with
        `user_name=message.author.display_name` (adapter.py:7837, hardcoded, no
        config). A display name is per-guild, user-editable and reused freely,
        so an agent writing durable notes keyed on it will merge two people or
        lose someone the day they rename. The account handle and the numeric id
        are the stable identifiers, and neither reaches the model — so an agent
        told to "record the user id" cannot comply, however firmly it is asked.

        This surfaces both, once, at the front of the dispatched text. The
        agent's memory guidance keys notes on them and is told never to echo
        the tag back into chat.
        """
        try:
            if not (self._ambient_enabled()
                    and self._ambient_cfg().get("speaker_identity")):
                return None
            author = getattr(message, "author", None)
            uid = str(getattr(author, "id", "") or "")
            if not uid:
                return None
            # .name is the stable account handle; display_name is the mutable one.
            handle = str(getattr(author, "name", "")
                         or getattr(author, "display_name", "") or "").strip()
            # Record the speaker for the duration of this dispatch so tool-level
            # gates can authorise on a stable id rather than a display name.
            try:
                _current_speaker_id.set(uid)
            except Exception:
                pass
            return f"[speaker @{handle} id:{uid}]" if handle else f"[speaker id:{uid}]"
        except Exception:
            logger.debug("ambient: speaker tag failed", exc_info=True)
            return None

    # ---- deterministic recall for a known speaker -------------------------
    #
    # WHY THIS IS NOT A PROMPT: an agent told "recall before replying to someone
    # you know" has to make a judgement every message, and measurement says it
    # mostly does not. On one profile here, across four log files: 170 calls to
    # the UNCONDITIONAL memory instruction ("wake once per session") against 10
    # to the CONDITIONAL one ("recall before replying to someone you know").
    # Seventeen to one, same file, same agent, same session — the only variable
    # was whether the model had to decide. The instruction had already been
    # strengthened once ("calling the tool is the only thing that remembers")
    # and that did not move it either.
    #
    # So the lookup moves out of the model's judgement and into the dispatch
    # path: every message from an identified speaker carries what we already
    # know about them, fetched by a subprocess, costing zero inference. The
    # agent may still call recall itself for a deeper search — this only
    # guarantees the floor.
    #
    # OPT-IN and generic: disabled unless `speaker_memory.enabled` is set, and
    # it takes the store location from config rather than assuming any layout.
    _MEM_CACHE_TTL_S = 300

    def _speaker_memory_cfg(self) -> dict:
        cfg = self._ambient_cfg().get("speaker_memory")
        return cfg if isinstance(cfg, dict) else {}

    def _speaker_memory(self, uid: str) -> str | None:
        """Facts already known about this speaker, or None. Never raises.

        Keyed on the numeric id, never the handle: a handle is user-editable,
        and keying recall on it silently returns the wrong person's history
        after a rename. Cached briefly so a busy channel does not spawn a
        subprocess per message.
        """
        cfg = self._speaker_memory_cfg()
        if not cfg.get("enabled") or not uid:
            return None
        try:
            now = time.time()
            hit = self._mem_cache.get(uid)
            if hit and now - hit[0] < self._MEM_CACHE_TTL_S:
                return hit[1]

            binary = str(cfg.get("binary") or "").strip()
            memdir = str(cfg.get("memory_dir") or "").strip()
            if not memdir or not (binary and os.path.isfile(binary)
                                  and os.access(binary, os.X_OK)):
                return None

            import subprocess  # local: keeps the import off the hot path
            proc = subprocess.run(
                [binary, "recall", uid],
                env={"MEMORY_DIR": memdir, "PATH": "/usr/local/bin:/usr/bin:/bin",
                     "HOME": os.path.dirname(os.path.dirname(memdir)) or "/tmp"},
                cwd="/", capture_output=True, text=True,
                timeout=float(cfg.get("timeout_seconds", 4)),
                shell=False,      # explicit: argv only, never a command string
            )
            raw = (proc.stdout or "").strip()
            if proc.returncode != 0 or not raw:
                self._mem_cache[uid] = (now, None)
                return None

            # Stored memories are DERIVED FROM USER TEXT, so treat them as
            # semi-untrusted on the way back in: strip any speaker-tag echo (a
            # stored line could otherwise forge a second speaker), flatten to
            # single lines so nothing can fake block structure, and cap both
            # count and length so a long history cannot crowd the context.
            #
            # These are STRUCTURAL defences only. A memory whose text is itself
            # an instruction still arrives as text — that cannot be filtered
            # without reading meaning, and detection-based filtering does not
            # survive an adaptive attacker. The boundary is what the profile
            # can DO, not what it reads: a profile using this should hold no
            # shell, file or code-execution tool. Note also that the exposure
            # is not new — anything reachable here was already reachable by the
            # agent calling recall itself, or by a session-start memory load.
            max_facts = int(cfg.get("max_facts", 8))
            max_chars = int(cfg.get("max_chars", 600))
            facts: list[str] = []
            for line in raw.splitlines():
                line = _SPEAKER_ECHO_RE.sub("", line).strip()
                line = " ".join(line.split())
                # Recall backends tend to end with a "N matches." summary; it is
                # tool chatter, not something known about the person.
                if not line or _RECALL_NOISE_RE.match(line):
                    continue
                facts.append(line)
                if len(facts) >= max_facts:
                    break
            if not facts:
                self._mem_cache[uid] = (now, None)
                return None
            block = " · ".join(facts)
            if len(block) > max_chars:
                block = block[:max_chars].rstrip() + " …"
            result = f"[known {block}]"
            self._mem_cache[uid] = (now, result)
            return result
        except Exception:
            logger.debug("ambient: speaker memory lookup failed", exc_info=True)
            return None

    def _apply_speaker_tag(self, message: Any) -> None:
        """Prepend the speaker tag once, in place. Never raises."""
        tag = self._speaker_tag(message)
        if not tag:
            return
        try:
            content = getattr(message, "content", "") or ""
            if content.startswith("[speaker "):
                return  # already tagged (re-dispatch or backfill replay)
            author = getattr(message, "author", None)
            known = self._speaker_memory(str(getattr(author, "id", "") or ""))
            prefix = f"{tag}\n{known}\n" if known else f"{tag}\n"
            message.content = f"{prefix}{content}"
        except Exception:
            pass  # frozen message object; identity is a nicety, not a gate

    # ---- system-notice rerouting ------------------------------------------
    def _system_notice_target(self, content: Any, chat_id: Any) -> str | None:
        """Channel id to reroute an operator-facing notice to, or None.

        Cron delivery failures ("⚠️ Cron 'x' failed: …") and the cron wrapper
        header are posted to the job's delivery channel — for a social profile
        that is the community room, where agent plumbing does not belong.
        With `system_notices.reroute_channel` set they go to a private channel
        instead. Returns None (stock behaviour) when unset, when the notice is
        already going there, or on any error.
        """
        try:
            if not self._ambient_enabled() or not isinstance(content, str):
                return None
            sn = self._sub("system_notices")
            target = str(sn.get("reroute_channel") or "").strip()
            if not target or str(chat_id or "") == target:
                return None
            prefixes = sn.get("patterns") or _SYSTEM_NOTICE_PREFIXES
            stripped = content.strip()
            if any(stripped.startswith(str(p)) for p in prefixes):
                return target
            return None
        except Exception:
            logger.debug("ambient: system-notice check failed", exc_info=True)
            return None

    def _drain_notice_rewrite(self, content: Any) -> str | None:
        """This profile's own wording for a gateway drain notice, or None.

        None means "not a drain notice, or nothing configured" and the stock
        text goes out unchanged — the right failure mode here, because the
        worst case is operator phrasing in a public room rather than a
        swallowed reply.
        """
        try:
            if not self._ambient_enabled() or not isinstance(content, str):
                return None
            stripped = content.strip()
            if not _DRAIN_NOTICE_RE.match(stripped):
                return None
            sn = self._sub("system_notices")
            refused = str(sn.get("drain_notice") or "").strip()
            if _DRAIN_QUEUED_RE.search(stripped):
                # Falls back to the refusal wording only when no queued wording
                # is given: a profile that configures one line rather than two
                # still gets its own voice, just a slightly less accurate one.
                queued = str(sn.get("drain_notice_queued") or "").strip()
                return queued or refused or None
            return refused or None
        except Exception:
            logger.debug("ambient: drain-notice check failed", exc_info=True)
            return None

    def _stall_notice_rewrite(self, content: Any) -> str | None:
        """This profile's own wording for a gateway stall notice, or None.

        None means "not a stall notice, or nothing configured" and the stock text
        goes out unchanged — the same deliberate failure mode as the drain notice:
        operator phrasing in a public room is bad, a silently dropped answer is
        worse, because the person who spoke is left with nothing at all.
        """
        try:
            if not self._ambient_enabled() or not isinstance(content, str):
                return None
            if not _STALL_NOTICE_RE.match(content.strip()):
                return None
            wording = str(self._sub("system_notices").get("stall_notice") or "").strip()
            return wording or None
        except Exception:
            logger.debug("ambient: stall-notice check failed", exc_info=True)
            return None

    # ---- per-profile slash-command policy ---------------------------------
    async def _check_slash_authorization(self, interaction: Any, command_text: str) -> bool:
        """Optional per-profile slash-command allowlist, independent of chat.

        WHY: chat admission and slash authorization share one gate upstream —
        a community profile that answers everyone (allow-all) therefore also
        exposes /model, /reset, ... to everyone, and there is no per-guild or
        per-surface command policy. Worse, on a multiplexed gateway the
        per-profile allow-all env flag is not reliably visible on the slash
        interaction path (it resolves env outside the per-turn profile scope
        — same trap family as title generation), so a community profile can
        end up rejecting even the operator. This gate replaces stock slash
        auth with an explicit operator allowlist when configured.

        Config (ambient_presence.slash_commands): `allowed_channels` and/or
        `allowed_users` (string ids). If NEITHER is set, stock behaviour is
        untouched. If set, a slash invocation must match every configured
        list (channel in allowed_channels, user in allowed_users) — matching
        invocations are authorized directly (bypassing the scope-broken stock
        check), everything else gets the stock ephemeral rejection. Chat is
        unaffected either way.
        """
        try:
            sc = self._sub("slash_commands")
            chans = _id_set(sc.get("allowed_channels"))
            users = _id_set(sc.get("allowed_users"))
        except Exception:
            chans, users = set(), set()
        if not chans and not users:
            return await super()._check_slash_authorization(interaction, command_text)
        try:
            chan_id = str(
                getattr(interaction, "channel_id", None)
                or getattr(getattr(interaction, "channel", None), "id", "")
                or ""
            )
            user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
            chan_ok = (not chans) or (chan_id in chans)
            user_ok = (not users) or (bool(user_id) and user_id in users)
            if chan_ok and user_ok:
                logger.info(
                    "ambient.slash_commands: authorized %r for user=%s in channel=%s",
                    command_text, user_id, chan_id,
                )
                return True
            return await self._reject_slash(
                interaction, command_text,
                reason="blocked by ambient_presence.slash_commands policy",
            )
        except Exception:
            logger.debug("ambient.slash_commands gate failed; stock auth", exc_info=True)
            return await super()._check_slash_authorization(interaction, command_text)

    # ---- per-profile no-thread mode --------------------------------------
    def _get_no_thread_channels(self) -> set:
        """Add this message's channel to the no-thread set when opted out.

        Upstream decides threading from os.getenv("DISCORD_AUTO_THREAD") —
        process-wide, so under multiplex one profile's preference silently
        overrides everyone's. We cannot instead return None from
        _auto_create_thread: upstream treats that as a thread-creation FAILURE
        and deliberately refuses to fall back inline (it posts a visible error
        and drops the message, see their #20243). So suppress threading one
        step earlier, via skip_thread, which is the supported route.
        """
        base = super()._get_no_thread_channels()
        extra = _no_thread_keys.get()
        return (base | extra) if extra else base

    # ---- catch-up: the idle check-in -------------------------------------
    #
    # WHAT IS MISSING WITHOUT IT: every ambient path in this plugin is reactive —
    # a message arrives, and something decides whether to answer THAT message.
    # So a conversation whose every message loses its dice roll passes with the
    # bot never having considered the conversation at all, only its messages one
    # at a time. From the room's side that is indistinguishable from absence: ten
    # people talked for an hour and she never looked up.
    #
    # A timer is the only thing that closes it, because the trigger is the
    # *absence* of a decision, and nothing reactive can fire on that. But a bare
    # timer is also the single most obnoxious thing a bot can have — it produces
    # "hey what's everyone up to" into a dead channel at 04:00 — so the loop is
    # a scanner, not a speaker. Every pass is free (no inference), the vast
    # majority end in a skip, and the only thing it can ever do is hand ONE real
    # message to the normal ambient dispatch path with a transcript attached.
    #
    # It is subordinate to the shared ambient budget on purpose (see
    # `_catchup_quota_ok`): a check-in can never fire during the ordinary
    # cooldown, and one that actually speaks spends from the same daily cap. The
    # feature can therefore make her *better informed*, but not measurably more
    # talkative — which is the requirement it was built under.
    def _catchup_cfg(self) -> dict:
        return self._sub("catch_up")

    def _catchup_enabled(self) -> bool:
        return bool(self._ambient_enabled() and self._catchup_cfg().get("enabled"))

    def _catchup_channels(self) -> list:
        """Explicit channel ids. No "*": scanning every visible channel on a
        timer is how a bot ends up talking to itself in six rooms at once, and
        unlike the reactive path there is no human message to justify the visit.
        """
        ids = _id_set(self._catchup_cfg().get("channels"))
        return sorted(i for i in ids if i.isdigit())

    def _in_quiet_hours(self) -> bool:
        """True inside the configured local-time window she never initiates in.

        Local host time deliberately: the room is one community in one place,
        and the operator reads the config in the same clock they live in.
        """
        try:
            window = self._catchup_cfg().get("quiet_hours")
            if not window:
                return False
            if isinstance(window, str):
                window = [p for p in window.split(",")]
            start, end = int(window[0]), int(window[1])
            if start == end:
                return False
            hour = time.localtime().tm_hour
            if start < end:
                return start <= hour < end
            return hour >= start or hour < end  # window wraps midnight
        except Exception:
            return False

    def _catchup_quota_ok(self) -> bool:
        cfg = self._catchup_cfg()
        now = time.time()
        if now - self._catchup_last < float(cfg.get("min_gap_seconds", 7200)):
            return False
        cutoff = now - 86400
        while self._catchup_hits and self._catchup_hits[0] < cutoff:
            self._catchup_hits.popleft()
        if len(self._catchup_hits) >= int(cfg.get("max_per_day", 2)):
            return False
        # The shared budget, last and load-bearing. Without this the two paths
        # add up: the reactive one spends its ten a day and the timer adds its
        # own on top, which is precisely the "she got chatty" failure. Charged
        # at send (see _catchup_count_sent), so a check-in that ends in silence
        # does not eat the ordinary cooldown.
        if cfg.get("respect_ambient_budget", True) and not self._ambient_quota_ok():
            return False
        return True

    def _render_transcript(self, msgs: list) -> str:
        """Oldest-first `name: text` lines, bounded twice.

        Truncated per message AND in total, because a single pasted stack trace
        would otherwise fill the whole budget and push the actual conversation
        out. The total cap trims from the FRONT: the most recent lines are the
        ones she is deciding about.
        """
        cfg = self._catchup_cfg()
        per_msg = max(40, int(cfg.get("transcript_message_chars", 240)))
        max_chars = max(200, int(cfg.get("transcript_max_chars", 1200)))
        lines = []
        for m in msgs:
            who = getattr(getattr(m, "author", None), "display_name", None) or "someone"
            text = " ".join((getattr(m, "content", "") or "").split())
            if not text:
                attachments = getattr(m, "attachments", None) or []
                if attachments:
                    text = f"<{len(attachments)} attachment(s)>"
                else:
                    continue
            if len(text) > per_msg:
                text = text[: per_msg - 1] + "…"
            lines.append(f"{who}: {text}")
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = "…" + out[-max_chars:]
        return out

    async def _catchup_scan_channel(self, cid: str) -> bool:
        """One channel pass. True only when a check-in actually dispatched.

        Everything before the dispatch is free. The order is deliberate: the
        structural facts (did she miss a conversation, has the room settled, is
        it still warm) are decided before the dice, so the probability applies
        to *qualifying* rooms rather than to every tick of the clock.
        """
        cfg = self._catchup_cfg()
        client = getattr(self, "_client", None)
        if client is None:
            return False
        channel = client.get_channel(int(cid))
        if channel is None:
            channel = await client.fetch_channel(int(cid))
        if channel is None or not hasattr(channel, "history"):
            return False

        want = max(2, int(cfg.get("transcript_messages", 8)))
        history = [m async for m in channel.history(limit=min(50, want + 8))]
        if not history:
            return False

        me = getattr(client, "user", None)
        my_id = getattr(me, "id", None)
        since = []  # newest-first, everything after her last message
        mine = None
        for m in history:
            if my_id is not None and getattr(getattr(m, "author", None), "id", None) == my_id:
                mine = m
                break
            since.append(m)
        if not since:
            return False  # she spoke last — a check-in here would be a monologue

        humans = [m for m in since if self._basic_ambient_eligible(m)]
        if len(humans) < int(cfg.get("min_messages", 3)):
            return False  # nothing she can meaningfully be said to have missed

        target = humans[0]  # the newest real message: what she is replying into
        if str(getattr(target, "id", "")) in self._catchup_seen:
            return False  # already read this conversation once; do not re-open it

        now = time.time()
        try:
            age = now - target.created_at.timestamp()
        except Exception:
            return False
        # A conversation still in flight belongs to the per-message dice, which
        # is tuned for it. Arriving late into a live exchange is the thing that
        # reads as a bot barging in.
        if age < float(cfg.get("min_quiet_seconds", 300)):
            return False
        # ...and a room that stopped talking hours ago is not a conversation any
        # more. Answering into it is necromancy, and it is the loudest possible
        # way to be wrong, because hers is the only message anyone will see.
        if age > float(cfg.get("max_age_seconds", 5400)):
            return False

        if random.random() >= float(cfg.get("probability", 0.2)):
            return False

        # Opportunistic traffic, exactly like a dice-roll join: it must never be
        # what occupies the one shared inference slot.
        if self._standby_enabled() and self._standby_engaged() and self._fleet_busy():
            logger.info("ambient.catch_up: slot busy, skipping this pass")
            return False

        window = list(reversed(since[: want]))
        if mine is not None:
            window.insert(0, mine)  # her own last line, so she does not repeat it
        transcript = self._render_transcript(window)
        if not transcript.strip():
            return False
        self._catchup_echo = (
            time.time(),
            {" ".join(ln.split()) for ln in transcript.splitlines() if ln.strip()},
        )

        marker = self._ambient_marker()
        hint = (
            str(cfg.get("hint") or _CATCHUP_HINT)
            .replace("{transcript}", transcript)
            .replace("{marker}", marker)
        )

        # The stock pass already claimed this id when the message first arrived;
        # release it so the check-in can claim it exactly once (same reasoning as
        # the ambient re-dispatch in _dispatch_inner).
        self._dedup.discard(str(getattr(target, "id", "")))
        self._apply_speaker_tag(target)

        hint_token = _ambient_hint.set(hint)
        thread_token = self._ambient_no_thread_token(target)
        open_token = _ambient_open.set(True)
        try:
            handled = await super()._dispatch_discord_message(target)
        finally:
            _ambient_open.reset(open_token)
            _ambient_hint.reset(hint_token)
            if thread_token is not None:
                _no_thread_keys.reset(thread_token)

        # Charged whether or not she ends up speaking: the turn happened, the
        # inference was spent, and the conversation has now been read. The
        # SHARED budget is charged separately, at send.
        self._catchup_seen.append(str(getattr(target, "id", "")))
        if handled:
            self._catchup_last = time.time()
            self._catchup_hits.append(self._catchup_last)
            self._catchup_note_dispatch(target)
            logger.info(
                "ambient.catch_up: read %d message(s) in #%s (%.0fs quiet) and "
                "handed them to the agent",
                len(humans), cid, age,
            )
        return bool(handled)

    async def _catchup_pass(self) -> None:
        if not self._catchup_enabled():
            return
        channels = self._catchup_channels()
        if not channels:
            logger.debug("ambient.catch_up: enabled but no channels configured")
            return
        if self._in_quiet_hours():
            return
        if not self._catchup_quota_ok():
            return
        # Randomised so a multi-channel config does not permanently favour the
        # lowest id, and stopped after the first hit: one check-in per pass, ever.
        for cid in random.sample(channels, len(channels)):
            try:
                if await self._catchup_scan_channel(cid):
                    return
            except Exception:
                logger.debug("ambient.catch_up: channel %s scan failed", cid, exc_info=True)

    async def _catchup_loop(self) -> None:
        cfg = self._catchup_cfg()
        interval = max(60, int(cfg.get("interval_seconds", 900)))
        try:
            # Settle first. A gateway restart must not produce an immediate
            # check-in on a conversation that ended before it, and on a host
            # that restarts the gateway to load plugin changes that would fire
            # every time we deploy.
            await asyncio.sleep(min(interval, 300) + random.randint(0, 120))
            while True:
                try:
                    await self._catchup_pass()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("ambient.catch_up: pass failed", exc_info=True)
                # Jittered so she is not visibly on a clock.
                await asyncio.sleep(interval + random.randint(0, max(1, interval // 3)))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ambient: catch-up loop stopped", exc_info=True)

    def _catchup_echo_leak(self, content: Any) -> bool:
        """True when a reply is largely the transcript we just fed her.

        The two bracketed wrapper lines are removed by the ordinary echo rule;
        these lines cannot be, because they are other people's actual sentences
        and no pattern distinguishes them from ordinary speech. So they are
        matched against what we know we sent — which we do know, exactly.

        The threshold is two lines, not one: a single overlapping line is
        plausibly her quoting someone on purpose, which is normal conversation.
        Two verbatim lines is regurgitation. Over-suppressing costs one reply;
        under-suppressing reposts the room's own conversation back at it.
        """
        try:
            set_at, lines = self._catchup_echo
            if not lines or time.time() - set_at > 900:
                return False
            body = [" ".join(ln.split()) for ln in str(content or "").splitlines()]
            hits = sum(1 for ln in body if ln and ln in lines)
            return hits >= 2
        except Exception:
            return False

    def _catchup_note_dispatch(self, message: Any) -> None:
        """Mark this check-in's reply as owing a charge to the shared budget."""
        try:
            now = time.time()
            for key, when in list(self._catchup_pending.items()):
                if now - when > 900:
                    self._catchup_pending.pop(key, None)
            self._catchup_pending[str(getattr(message, "id", ""))] = now
        except Exception:
            pass

    def _catchup_discard_pending(self, reply_to: Any) -> None:
        """A reply that never went out must not be charged — nor left lying
        around to be mischarged to some later send in the same channel."""
        try:
            self._catchup_pending.pop(str(reply_to or ""), None)
        except Exception:
            pass

    def _catchup_count_sent(self, reply_to: Any, result: Any) -> None:
        """Charge the SHARED ambient budget once the check-in actually spoke.

        At send, not at dispatch, for the same reason the bounce breaker counts
        here: a dispatch is an intention. A check-in answered with [SILENT] put
        no words in the room, and charging it would silence her ordinary ambient
        replies for the next cooldown over a turn nobody saw.
        """
        try:
            key = str(reply_to or "")
            if key not in self._catchup_pending:
                return
            if result is not None and getattr(result, "success", True) is False:
                return
            self._catchup_pending.pop(key, None)
            now = time.time()
            self._ambient_last = now
            self._ambient_hits.append(now)
            logger.info("ambient.catch_up: spoke; charged to the shared ambient budget")
        except Exception:
            pass

    # ---- rotating presence (zero inference) ------------------------------
    async def _presence_loop(self) -> None:
        pc = self._sub("presence")
        interval = max(300, int(pc.get("interval_seconds", 5400)))
        statuses = [str(s) for s in (pc.get("statuses") or []) if str(s).strip()]
        if not statuses:
            return
        try:
            import discord

            while True:
                try:
                    await self._client.change_presence(
                        activity=discord.CustomActivity(name=random.choice(statuses))
                    )
                except Exception:
                    logger.debug("ambient: change_presence failed", exc_info=True)
                await asyncio.sleep(interval + random.randint(0, 600))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ambient: presence loop stopped", exc_info=True)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        ok = await super().connect(is_reconnect=is_reconnect)
        try:
            if ok and self._ambient_enabled() and self._sub("presence").get("enabled"):
                if self._presence_task is None or self._presence_task.done():
                    self._presence_task = asyncio.create_task(self._presence_loop())
                    logger.info("ambient: presence rotation started")
        except Exception:
            logger.debug("ambient: could not start presence rotation", exc_info=True)
        try:
            # Guarded on done() as well as None: connect() runs again on every
            # reconnect, and a second loop would double the check-in rate while
            # each one's own sub-cap still read as respected.
            if ok and self._catchup_enabled():
                if self._catchup_task is None or self._catchup_task.done():
                    self._catchup_task = asyncio.create_task(self._catchup_loop())
                    logger.info(
                        "ambient: catch-up scanner started for %d channel(s)",
                        len(self._catchup_channels()),
                    )
        except Exception:
            logger.debug("ambient: could not start catch-up scanner", exc_info=True)
        return ok

    # ---- outbound text hygiene -------------------------------------------
    def _hygiene_cfg(self) -> dict:
        return self._sub("text_hygiene")

    def _rewrite_dashes(self, text: str) -> str:
        """Replace em dashes with a spaced hyphen, outside fenced code."""
        out = []
        # Odd indices are the ``` delimiters themselves; parts between a pair
        # of them are inside a fence. Track parity across the split.
        inside = False
        for part in _CODE_FENCE_RE.split(text):
            if part == "```":
                inside = not inside
                out.append(part)
                continue
            if not inside:
                part = _EM_DASH_RE.sub(" - ", part)
                part = _SPACED_EN_DASH_RE.sub(" - ", part)
            out.append(part)
        return "".join(out)

    # ── voice-only bookkeeping ──────────────────────────────────────────
    _VOICE_TWIN_WINDOW_S = 20.0

    def _voice_only_enabled(self) -> bool:
        return bool(self._ambient_cfg().get("voice_only_replies", False))

    def _voice_just_sent(self, chat_id: Any) -> bool:
        """True if a voice message went to this chat inside the twin window.

        Consumes the mark: exactly ONE text send is suppressed per voice send,
        so a genuine follow-up message a moment later still gets through. The
        failure direction is 'text leaks', never 'the agent goes mute'.
        """
        marks = getattr(self, "_voice_sent_at", None)
        if not marks:
            return False
        ts = marks.pop(str(chat_id), None)
        if ts is None:
            return False
        return (time.monotonic() - ts) <= self._VOICE_TWIN_WINDOW_S

    def _suppressed_result(self):
        """The 'sent nothing, report success' result the base adapter expects."""
        try:
            from gateway.platforms.base import SendResult  # type: ignore

            return SendResult(success=True, message_id=None)
        except Exception:
            return None

    async def send_voice(self, *args: Any, **kwargs: Any):
        """Mark that speech went out, so send() can drop the text twin."""
        chat_id = kwargs.get("chat_id", args[0] if args else None)
        # Logged unconditionally: whether this override runs at all, and in what
        # ORDER relative to the text send, is the thing that has been guessed
        # wrong twice. One real run of this beats any amount of reading.
        logger.info(
            "ambient.voice: send_voice chat=%s voice_only=%s",
            chat_id, self._voice_only_enabled(),
        )
        result = await super().send_voice(*args, **kwargs)
        if self._voice_only_enabled() and chat_id is not None:
            if not hasattr(self, "_voice_sent_at"):
                self._voice_sent_at = {}
            self._voice_sent_at[str(chat_id)] = time.monotonic()
            logger.info("ambient.voice: marked chat=%s for text-twin suppression", chat_id)
        return result

    def _scrub_outbound(self, content: str) -> str | None:
        """Clean a reply before it goes out.

        Returns the cleaned text, or None when the message was nothing but
        leaked control tokens and should be suppressed entirely rather than
        posted as punctuation soup.
        """
        cfg = self._hygiene_cfg()
        text = content

        # Control tokens default ON: their presence is always a bug.
        if cfg.get("strip_control_tokens", True):
            drop = None
            if _TOOL_RECIPIENT_RE.search(text):
                drop = "leaked tool call (harmony recipient marker)"
            elif _HARMONY_TOKEN_RE.search(text):
                m = _HARMONY_MSG_RE.match(text)
                if m and _REASONING_CHANNEL_RE.search(m.group("head")):
                    drop = "harmony reasoning/commentary channel"
                else:
                    # Keep the payload of the last <|message|>; with no
                    # envelope to parse, fall back to token removal.
                    body = m.group("body") if m else text
                    body = _HARMONY_TOKEN_RE.sub(" ", body)
                    body = re.sub(r"[ \t]{2,}", " ", body).strip()
                    if _HAS_WORD_RE.search(body):
                        logger.warning(
                            "ambient: stripped a leaked harmony envelope from a "
                            "reply: %r", text[:200],
                        )
                        text = body
                    else:
                        drop = "nothing but control tokens"
            if drop:
                logger.warning(
                    "ambient: suppressed a reply — %s: %r", drop, text[:200],
                )
                return None

        # Speaker-tag echo. Defaults ON and is only reachable when we inject the
        # tag in the first place, so there is nothing to strip for profiles that
        # do not use speaker_identity.
        if cfg.get("strip_speaker_echo", True) and _SPEAKER_ECHO_RE.search(text):
            cleaned = _SPEAKER_ECHO_RE.sub("", text)
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
            if not _HAS_WORD_RE.search(cleaned):
                logger.warning(
                    "ambient: suppressed a reply that was only an echoed "
                    "speaker tag: %r", text[:160],
                )
                return None
            logger.warning(
                "ambient: stripped an echoed speaker tag from a reply: %r",
                text[:160],
            )
            text = cleaned

        # A leaked media narration is always a bug: it is the model describing a
        # tool result instead of letting the platform deliver it, and it puts a
        # host filesystem path into a public channel. Default ON.
        if cfg.get("strip_media_narration", True):
            text, n_img = _EMPTY_MD_IMAGE_RE.subn(" ", text)
            if n_img:
                logger.info("ambient: stripped %d empty markdown image(s) from a reply", n_img)
            cleaned, n = _MEDIA_NARRATION_RE.subn(" ", text)
            if n:
                logger.info(
                    "ambient: stripped %d leaked media narration(s) from a reply", n
                )
                text = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
                setattr(self, "_last_scrub_had_media_narration", True)

        # Dash rewriting is a style choice, so it stays opt-in.
        if cfg.get("no_em_dash", False):
            text = self._rewrite_dashes(text)

        return text

    def _split_media_urls(self, content: str) -> tuple[str, list[str]]:
        """Separate media URLs from the prose around them.

        Returns (remaining_text, media_urls). Both empty-safe: a message with
        no media URL, or one that is ALREADY nothing but a media URL, comes
        back with an empty url list so the caller sends it untouched.
        """
        tokens = content.split()
        if not tokens:
            return content, []
        media = [u for u in (_media_url(t) for t in tokens) if u]
        if not media:
            return content, []
        if len(tokens) == 1 and len(media) == 1 and tokens[0] == media[0]:
            return content, []  # already a bare URL — Discord renders it clean
        # Rebuild the prose line-by-line so deliberate paragraph breaks survive;
        # only the URL tokens are removed.
        lines = []
        for line in content.splitlines():
            kept = " ".join(t for t in line.split() if not _media_url(t))
            lines.append(kept)
        rest = "\n".join(lines)
        rest = re.sub(r"\n{3,}", "\n\n", rest).strip()
        return rest, media

    def _attach_pending_gif(self, content: str, media: list) -> list:
        """Append a fetched-but-unposted GIF URL to the outgoing media list.

        Cleared unconditionally once inspected, so a URL can only ever be
        attached to the single reply that follows its lookup. The time window
        is a second guard for the case where a turn dies before sending at all
        and the next unrelated message would otherwise inherit the GIF.
        """
        pending = _gif_state.get("pending")
        if not pending:
            return media
        url, ts = pending
        _gif_state["pending"] = None
        cfg = _gif_config()
        if not cfg.get("attach_if_omitted", True):
            return media
        if time.time() - float(ts) > float(cfg.get("attach_window_seconds", 180)):
            logger.info("gif_search: pending GIF expired unposted (%s)", url[:60])
            return media
        if url in content or url in media:
            # Belt and braces. The tool no longer hands the model a URL, so
            # this should not happen — if it does, the model dug one out of
            # conversation history and posting it again would duplicate.
            logger.warning(
                "gif_search: reply already contained a GIF url, not attaching "
                "a second: %s", url[:60],
            )
            return media
        logger.info("gif_search: attaching GIF to reply: %s", url[:60])
        return list(media) + [url]

    # ---- silence + bounce accounting -------------------------------------
    async def send(self, chat_id: str, content: str, *args: Any, **kwargs: Any):
        """Swallow the sentinel so the agent may choose to stay quiet.

        Also the bot-bounce counting point: a reply is charged to its pair
        only when it actually goes out, and the charge is correlated by the
        reply_to anchor (the inbound message id every live Discord reply
        carries), NOT by chat_id — so interleaved bots in one channel are
        each charged for their own reply, and unrelated sends to the channel
        (cron delivery, notices) can never consume a marker. Upstream send()
        chunks a long reply internally and returns ONE SendResult, and the
        marker is popped on the first successful use, so a reply costs AT
        MOST one count no matter how many chunks or retries it becomes
        (batched events can still make it zero — the safe direction).
        """
        # Open the conversational window: whatever arrives next in this channel
        # is probably a reply to this. Recorded even for a swallowed sentinel —
        # deciding to stay quiet is still a turn she took, and the next message
        # is just as likely to be aimed at her.
        try:
            self._spoke[str(chat_id)] = (time.time(), 0)
        except Exception:
            pass

        reply_to = kwargs.get("reply_to", args[0] if args else None)

        # Inbound-speech echo: the gateway posts 🎙️ "<transcript>" so a user can
        # verify STT quality. Useful on an operator surface, noise on a public
        # one — and NOT configurable per profile upstream, because
        # _should_echo_stt_transcripts() reads the process-wide GatewayRunner
        # config, so the root value wins for every profile. This is the only
        # place it can be decided per profile.
        if self._hygiene_cfg().get("suppress_stt_echo", False) and isinstance(content, str):
            if _STT_ECHO_RE.match(content):
                logger.info("ambient: suppressed STT transcript echo: %s", content.strip()[:80])
                return self._suppressed_result()

        # Voice-only, tool-call path: the model called the tts tool itself, so
        # send_voice() was never involved — the audio rode out as a MEDIA
        # directive and the model narrated it in prose. If the reply carries a
        # media narration AND nothing but the spoken words around it, the audio
        # IS the reply; posting the same sentence as text is the duplication we
        # are trying to remove.
        if (
            self._voice_only_enabled()
            and isinstance(content, str)
            and _MEDIA_NARRATION_RE.search(content)
            and "MEDIA:" not in _MEDIA_NARRATION_RE.sub("", content)
        ):
            logger.info(
                "ambient: voice-only — suppressed the narrated text twin: %s",
                _MEDIA_NARRATION_RE.sub("", content).strip()[:80],
            )
            self._bounce_discard_pending(reply_to)
            self._catchup_discard_pending(reply_to)
            return self._suppressed_result()

        # Voice-only, runner path: a voice message just went out for this chat,
        # so drop the duplicate text reply sent straight after it. Bounded by a
        # short window so an ordinary later message is never swallowed.
        # Voice-only, the path that actually works: speech for this turn was
        # already GENERATED before this text was sent (measured: ~14s before), so
        # the signal exists by now even though the audio has not been delivered
        # yet. Consume-once, so only the first text after a TTS call is dropped.
        if self._voice_only_enabled() and isinstance(content, str) and _claim_recent_tts():
            logger.info(
                "ambient: voice-only — dropped text emitted alongside speech: %s",
                content.strip()[:100],
            )
            self._bounce_discard_pending(reply_to)
            self._catchup_discard_pending(reply_to)
            return self._suppressed_result()

        if self._voice_only_enabled() and self._voice_just_sent(chat_id):
            logger.info(
                "ambient: voice-only — suppressed the text twin of a voice reply: %s",
                str(content).strip()[:80],
            )
            self._bounce_discard_pending(reply_to)
            self._catchup_discard_pending(reply_to)
            return self._suppressed_result()

        # Drop pure plumbing notices outright. Logged, never posted — there is no
        # channel where "no activity for 15 min" is useful to anyone but the log.
        if self._ambient_enabled() and isinstance(content, str):
            drops = self._sub("system_notices").get("drop_patterns")
            patterns = drops if isinstance(drops, list) and drops else _SYSTEM_NOTICE_DROP
            head = content.strip()[:120]
            if any(str(pat) and head.startswith(str(pat)) for pat in patterns):
                logger.info("ambient: dropped gateway notice (not posted): %s", head)
                return self._suppressed_result()

        # Drain notices are rewritten, not dropped or rerouted: the person who
        # just spoke is owed an answer, in the channel they spoke in. Done here
        # so everything downstream (scrub, media isolation) treats the new text
        # exactly like any other reply.
        _drain = self._drain_notice_rewrite(content)
        if _drain is not None:
            logger.info(
                "ambient: drain notice rewritten in-voice: %r -> %r",
                str(content).strip()[:80], _drain[:80],
            )
            content = _drain

        # A stall notice is the same shape of problem as a drain notice: upstream
        # explaining its own internals to whoever happened to be in the room.
        _stall = self._stall_notice_rewrite(content)
        if _stall is not None:
            logger.info(
                "ambient: stall notice rewritten in-voice: %r -> %r",
                str(content).strip()[:80], _stall[:80],
            )
            content = _stall

        target = self._system_notice_target(content, chat_id)
        if target:
            # Reroute, don't drop: the operator still wants cron failures, just
            # not in the community room. reply_to is deliberately dropped — the
            # anchor message lives in the channel we are routing away from.
            logger.info(
                "ambient: system notice rerouted to %s: %s",
                target, str(content).strip()[:120],
            )
            return await super().send(
                target, content, metadata=kwargs.get("metadata"),
            )
        if isinstance(content, str) and content.strip().startswith(_FALLBACK_NOTICE_PREFIX):
            notice = content.strip()
            # Track first (standby signal), decide visibility second.
            self._note_fallback_notice(notice)
            if self._ambient_enabled() and self._ambient_cfg().get(
                "suppress_fallback_notice"
            ):
                logger.info(
                    "ambient: fallback-switch notice suppressed for this "
                    "profile: %s", notice[:160],
                )
                try:
                    from gateway.platforms.base import SendResult  # type: ignore

                    return SendResult(success=True, message_id=None)
                except Exception:
                    return None
        # Catch-up echo guard, before the general ambient screen so it sees the
        # reply exactly as the model wrote it. WARNING, not info: this firing at
        # all means the model is reproducing its input, and the frequency of the
        # line is how we find out a model or a prompt has started misbehaving.
        if self._ambient_enabled() and isinstance(content, str) and self._catchup_echo_leak(content):
            logger.warning(
                "ambient.catch_up: suppressed a reply that repeated the "
                "transcript back into the room: %r", content[:200],
            )
            self._bounce_discard_pending(reply_to)
            self._catchup_discard_pending(reply_to)
            return self._suppressed_result()

        if self._ambient_enabled() and isinstance(content, str):
            marker = self._ambient_marker()
            screened, n_echo = _screen_ambient_reply(content, marker)
            if n_echo:
                # WARNING, not info: the model just tried to publish an instruction
                # we gave it. The rule caught it, but the frequency of this line is
                # how we find out a model or a prompt has started misbehaving.
                logger.warning(
                    "ambient: stripped %d echoed ambient directive(s) from a reply: %r",
                    n_echo, content[:200],
                )
            if screened is None:
                logger.info(
                    "ambient: response suppressed (%s sentinel or bare directive echo)",
                    marker,
                )
                # A swallowed reply was never sent: it must not count against
                # the pair, nor sit around to be charged to a later send.
                self._bounce_discard_pending(reply_to)
                self._catchup_discard_pending(reply_to)
                try:
                    from gateway.platforms.base import SendResult  # type: ignore

                    return SendResult(success=True, message_id=None)
                except Exception:
                    return None
            content = screened
        # General scrub last, so the fallback-notice comparison above still matches
        # on the model's verbatim text. The ONE thing that now runs earlier is the
        # ambient-directive strip: a `<hint>\n\n(SILENT)` reply only reads as silence
        # once the hint is gone, and checking the sentinel first is precisely how the
        # 2026-08-08 leak reached the channel. A fully-suppressed reply is accounted
        # for exactly like a [SILENT] one: it never went out, so it must not be
        # charged to the bot-bounce pair.
        if isinstance(content, str):
            scrubbed = self._scrub_outbound(content)
            if scrubbed is None:
                self._bounce_discard_pending(reply_to)
                self._catchup_discard_pending(reply_to)
                try:
                    from gateway.platforms.base import SendResult  # type: ignore

                    return SendResult(success=True, message_id=None)
                except Exception:
                    return None
            content = scrubbed

            # Media isolation, after scrubbing so a suppressed reply never
            # reaches it. The prose keeps the reply anchor and any metadata;
            # each media URL follows as its own bare message, which is the only
            # form Discord renders as pure media. Bounce accounting is charged
            # ONCE, on the first send — the pair had one reply, not two.
            if self._hygiene_cfg().get("isolate_media_urls", True):
                rest, media = self._split_media_urls(content)
                media = self._attach_pending_gif(content, media)
                if media:
                    first = None
                    if rest:
                        first = await super().send(chat_id, rest, *args, **kwargs)
                    for url in media:
                        sent = await super().send(chat_id, url)
                        if first is None:
                            first = sent
                    logger.info(
                        "ambient: isolated %d media url(s) into their own "
                        "message(s) so Discord renders them without the link",
                        len(media),
                    )
                    self._bounce_count_sent(reply_to, first)
                    self._catchup_count_sent(reply_to, first)
                    return first

        result = await super().send(chat_id, content, *args, **kwargs)
        self._bounce_count_sent(reply_to, result)
        self._catchup_count_sent(reply_to, result)
        return result


# ---- GIF search (Klipy) ---------------------------------------------------
# WHY A TOOL, NOT THE BUNDLED `gif-search` SKILL: that skill drives curl+jq at a
# shell prompt, and a public persona profile has no terminal (nor should it).
# Discord auto-embeds a plain GIF URL, so the agent only needs a URL back.
# The bundled skill also targets Tenor, whose API Google discontinued
# 2026-06-30; Klipy is the successor (near-identical shape, free tier).
#
# Config lives with the rest of the plugin's per-profile settings:
#   platforms.discord.extra.ambient_presence.gif_search
# and the credential is KLIPY_API_KEY in the PROFILE's .env (never config.yaml —
# config.yaml is world-readable-ish and goes nowhere near a git remote).
# "pending" holds (url, fetched_at) between the tool returning a URL and the
# adapter sending the reply, so a model that forgets to paste it still posts it.
_gif_state: dict = {"last": 0.0, "hits": deque(maxlen=256), "pending": None}


def _gif_config() -> dict:
    """Per-profile gif_search config; empty dict when unset."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        extra = ((cfg.get("platforms") or {}).get("discord") or {}).get("extra") or {}
        block = (extra.get("ambient_presence") or {}).get("gif_search")
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _gif_key() -> str:
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value("KLIPY_API_KEY") or "").strip()
    except Exception:
        return (os.getenv("KLIPY_API_KEY") or "").strip()


def _gif_enabled() -> bool:
    """check_fn: hide the tool entirely unless this profile opted in AND has a key."""
    return bool(_gif_config().get("enabled")) and bool(_gif_key())


def _gif_handle(args: dict, **_: Any) -> str:
    import json as _json
    import random as _random
    import urllib.parse as _urlparse
    import urllib.request as _urlrequest

    cfg = _gif_config()
    key = _gif_key()
    if not key:
        return "gif_search: not configured."

    query = " ".join(str(args.get("query") or "").split())[:80]
    if not query:
        return "gif_search: 'query' is required."

    # House style, e.g. `gif_search: {style: anime}` — a persona whose whole look is
    # anime should not have to remember to type it, and unlike the image prompt this
    # needs no undocumented behaviour: we own this handler and the query is ours to
    # shape before it reaches Klipy. Skipped when the query is already styled or asks
    # for something else explicitly (see `_apply_style`). Re-clamped to 80 because the
    # style is appended AFTER the model's own truncation.
    _style = cfg.get("style")
    if _style:
        styled = _apply_style(query, str(_style))[:80]
        if styled != query:
            logger.info("gif_search: style %r -> %r", str(_style), styled)
            query = styled

    # Both refusals below are returned to the MODEL, which then answers in words,
    # so the operator sees a normal reply and cannot tell which limit fired. They
    # are also the same length, so the tool_executor's "(0.00s, 55 chars)" line
    # does not disambiguate them either. Log which one, with the numbers: working
    # this out from timestamps alone cost a diagnosis on 2026-08-06.
    now = time.time()
    _interval = float(cfg.get("min_interval_seconds", 60))
    _since = now - float(_gif_state["last"])
    if _since < _interval:
        logger.info(
            "gif_search: refused %r — cooldown, %.0fs since last of %.0fs",
            query, _since, _interval,
        )
        return "No GIF this time — you just posted one. Reply in words."
    hits = _gif_state["hits"]
    cutoff = now - 86400
    while hits and hits[0] < cutoff:
        hits.popleft()
    _cap = int(cfg.get("max_per_day", 20))
    if len(hits) >= _cap:
        # NB: `hits` is in-memory only, so a gateway restart resets the daily
        # count to zero. On a host that restarts often this cap is far softer
        # than it looks — do not read a quiet day as the cap having bitten.
        logger.info(
            "gif_search: refused %r — daily cap, %d/%d in the rolling 24h "
            "(counter resets on gateway restart)", query, len(hits), _cap,
        )
        return "No GIF this time — daily limit reached. Reply in words."

    params = _urlparse.urlencode({
        "q": query,
        "per_page": int(cfg.get("pool", 8)),
        # 'high' by default: a public room, and the agent cannot preview what it posts.
        "content_filter": str(cfg.get("content_filter", "high")),
        # NO format_filter. It reads like a search facet ("only items that have
        # a gif") but it actually strips the response down to that ONE rendition
        # — with format_filter=gif every item comes back as {'gif': ...} alone
        # and the webp/mp4/webm we want are simply absent. Verified against the
        # live API 2026-08-06: dropping it returns the same 8 items, each with
        # all of gif/webp/jpg/mp4/webm. Do not "tidy" this back in.
        "customer_id": str(args.get("customer_id") or "companion")[:64],
    })
    url = f"https://api.klipy.com/api/v1/{key}/gifs/search?{params}"
    try:
        req = _urlrequest.Request(url, headers={"User-Agent": "hermes-discord-ambient"})
        with _urlrequest.urlopen(req, timeout=float(cfg.get("timeout_seconds", 12))) as r:
            payload = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        logger.debug("gif_search: lookup failed", exc_info=True)
        return "gif_search: lookup failed; just reply in words."

    # FORMAT: default webp, not gif. GIF is limited to a 256-colour palette, so
    # gradients and dark scenes band into visible blocky patches — the black
    # blocks you see on an anime GIF are palette quantisation plus dithering,
    # not a broken download. Klipy serves every item as gif/webp/mp4/webm/jpg;
    # animated WebP is 24-bit, roughly a third the bytes, and still embeds as an
    # IMAGE (autoplays and loops inline, no player chrome). mp4/webm are smaller
    # again but Discord renders them as a video embed, which reads less like a
    # reaction GIF — offered as config for anyone who prefers it.
    fmt = str(cfg.get("format", "webp")).strip().lower()
    if fmt not in ("webp", "gif", "mp4", "webm"):
        fmt = "webp"
    # Fall back through formats so a rare item lacking the preferred one still
    # yields a URL instead of being silently dropped from the pool.
    fmt_chain = [fmt] + [f for f in ("webp", "gif", "mp4", "webm") if f != fmt]
    size_pref = cfg.get("sizes") or ("md", "sm", "hd")
    if isinstance(size_pref, str):
        size_pref = [s.strip() for s in size_pref.split(",") if s.strip()]

    urls = []
    for item in ((payload.get("data") or {}).get("data")) or []:
        files = item.get("file") or {}
        got = None
        # Size is the outer loop: a md webp beats an hd gif for our purposes.
        for size in size_pref:
            for candidate in fmt_chain:
                got = ((files.get(size) or {}).get(candidate) or {}).get("url")
                if got:
                    break
            if got:
                break
        if got:
            urls.append(got)
    if not urls:
        return f"gif_search: nothing found for {query!r}."

    chosen = _random.choice(urls[: max(1, int(cfg.get("pick_from", 5)))])
    _gif_state["last"] = now
    hits.append(now)
    # Hand the URL to send() as well as to the model. A tool whose whole
    # contract is "echo this exact string back" is unreliable on a small model:
    # ours fetched a GIF and then wrote "Here's a fluffy one for you!" with no
    # URL in the message at all, twice in a row (2026-08-06). The description
    # says "put that URL in your reply" in plain words; it did not help. So the
    # adapter attaches it if the reply omits it — delivery is ours, not the
    # model's. Consumed (or expired) in send(); [SILENT] returns earlier, so a
    # reply the agent chose to swallow never drags a GIF out with it.
    _gif_state["pending"] = (chosen, now)
    logger.info("gif_search: %r -> %s", query, chosen[:60])
    # The URL is deliberately NOT returned to the model. There must be exactly
    # ONE thing that posts a GIF, and it is send(). Handing the model a URL
    # created two independent posters and both failure modes showed up in one
    # night: the model wrote a reply with no URL at all (GIF never appeared),
    # and then the model wrapped it as `![alt](url)` — which the GATEWAY strips
    # before the adapter ever sees it (`extract_media()` in gateway/run.py;
    # visible in the log as `response ready: 826 chars` followed by
    # `Sending response (728 chars)`) and delivers separately, while the
    # adapter, seeing no URL in the text, attached it too. Two GIFs.
    # With no URL in the tool result there is nothing to paste, nothing for
    # extract_media to strip, and one poster.
    return "GIF attached to your reply. Write your words normally, no URL."


_GIF_SCHEMA = {
    "name": "gif_search",
    "description": (
        "Attach a reaction GIF to the reply you are about to send. The GIF is "
        "posted for you automatically — do NOT write a URL, a link, or "
        "![markdown](...) in your reply, and do not describe the GIF. Just "
        "write your words normally. Use it as punctuation, rarely, when a GIF "
        "says it better than words. Never explain that you searched for it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What the GIF should show, e.g. 'happy cat' or 'eye roll'.",
            }
        },
        "required": ["query"],
    },
}


# ── image-generation gate ───────────────────────────────────────────────────
_GATED_TOOLS = {"image_generate"}


def _image_gate_cfg() -> dict:
    """Read this profile's gate config. Profile-correct: proven by the TTS tool
    resolving Edge for Companion and Piper for Assistant on the same gateway."""
    try:
        from hermes_cli.config import load_config  # type: ignore

        extra = (
            load_config()
            .get("platforms", {})
            .get("discord", {})
            .get("extra", {})
            .get("ambient_presence", {})
        ) or {}
        cfg = extra.get("image_gen_gate")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _normalize_ids(value: Any) -> set:
    """Accept a YAML list, a comma-separated string, or a bare id.

    `hermes config set` coerces a numeric value to an int and a bracketed list
    to a string, so all three shapes reach us in practice. Normalising here is
    what stops a policy silently reading as 'nobody is allowed'.
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    return {p.strip() for p in str(value).split(",") if p.strip()}


def _ambient_cfg(key: str, default: Any = None) -> Any:
    """One per-profile `ambient_presence.<key>` lookup, profile-correct.

    Same resolution as `_image_gate_cfg`, hoisted because three features now read
    from that block. NOT env vars: every profile shares one process under multiplex,
    so `os.getenv` would leak one persona's settings into another's.
    """
    try:
        from hermes_cli.config import load_config  # type: ignore

        extra = (
            load_config()
            .get("platforms", {})
            .get("discord", {})
            .get("extra", {})
            .get("ambient_presence", {})
        ) or {}
        return extra.get(key, default)
    except Exception:
        return default


# A style directive the model already expressed wins over the profile default —
# that is the "except when instructed otherwise" half, and without it a persona
# default silently overrides the operator asking for something specific.
_STYLE_OVERRIDES = (
    "photoreal", "photo-real", "realistic", "real life", "irl", "live action",
    "oil painting", "watercolor", "watercolour", "3d render", "claymation",
    "pixel art", "sketch", "line art", "no anime", "not anime", "non-anime",
)


def _apply_style(text: str, style: str) -> str:
    """Append a house style to a prompt/query unless it is already styled.

    Three ways to skip, in order: no style configured, the style is already present
    (so repeated calls do not stack "anime, anime, anime"), or the text names a
    different style explicitly — the model was told something, and a persona default
    that overrode an explicit instruction would be a bug, not a personality.
    """
    text = (text or "").strip()
    style = (style or "").strip()
    if not style or not text:
        return text
    low = text.lower()
    if style.lower() in low:
        return text
    if any(tok in low for tok in _STYLE_OVERRIDES):
        return text
    return f"{text}, {style}"


def _as_list(value: Any) -> list:
    """Coerce a config value to a list of strings, whatever shape it arrived in.

    NOT defensive padding — `hermes config set` genuinely turns a bracketed list into a
    STRING. Setting `reference_images '["/path/x.png"]'` stores the literal characters
    `["/path/x.png"]`, and a plain `isinstance(v, list)` test then sees a str, treats the
    whole thing as one path, finds no such file, and skips it. Measured 2026-08-08: both
    keys here landed as strings and the feature would have done nothing at all, silently.
    `_normalize_ids` above documents the same hazard for the gate's user ids.

    Handles: a real list, a JSON array in a string, a comma-separated string, a bare
    string. Returns [] for anything else.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:  # noqa: BLE001 — fall through to the comma path
            text = text[1:-1]
    return [p.strip().strip("\"'") for p in text.split(",") if p.strip().strip("\"'")]


def _self_portrait_cfg() -> dict:
    """`ambient_presence.self_portrait` — how a persona depicts ITSELF.

    A generic house style ("anime cel-shaded") makes every image look right in KIND
    while leaving the character random: same medium, different person each time. That
    is the complaint this answers. Two mechanisms, because they fail differently:

      reference_images  paths handed to the model as `reference_image_urls`. Highest
                        fidelity — the model sees the actual character sheet — but only
                        honoured by backends that accept image references.
      description       a compact character sheet in words. Lower fidelity, works
                        everywhere, and carries the look when the reference is ignored.

    Both are applied when either is configured; they are complements, not alternatives.
    """
    cfg = _ambient_cfg("self_portrait")
    return cfg if isinstance(cfg, dict) else {}


def _is_self_portrait(text: str, triggers: Any) -> bool:
    """Does this prompt ask for the persona itself?

    Word-boundary matched, and the trigger list is configuration rather than a guess:
    a bare substring test on "you" fires on "a gift for you", and depicting the persona
    when nobody asked is worse than not depicting it when they did.
    """
    if not text:
        return False
    triggers = _as_list(triggers)
    if not triggers:
        return False
    low = text.lower()
    for t in triggers:
        t = str(t).strip().lower()
        if t and re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", low):
            return True
    return False


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any):
    """Refuse image generation for anyone but the allowed users.

    WHY A TOOL GATE, not a prompt rule: image generation is METERED, and a
    public room is full of people who would enjoy spending someone else's
    balance. Hermes has no per-user tool authorisation, so without this the
    only options are 'everyone on the surface can generate' or 'nobody can'.

    Fails CLOSED. If the speaker cannot be determined the call is refused: a
    false deny costs the operator one re-ask, a false allow costs money and
    cannot be taken back.
    """
    if tool_name not in _GATED_TOOLS:
        return None

    # ---- house art style, applied before the gate decision ------------------
    # `ambient_presence.image_style` (e.g. "anime cel-shaded style") is stamped
    # onto the prompt so a persona LOOKS like itself without being asked to
    # remember to. A prompt rule would be the obvious alternative and is the
    # weaker one: it is one more instruction competing for attention, and this
    # codebase has already measured an instructed tool path that never once
    # fired in its entire logged life.
    #
    # HOW, and the caveat, stated because it is load-bearing: `args` reaches this
    # hook BY REFERENCE — `resolve_pre_tool_block` passes it through without a
    # defensive copy and the executor uses the same dict afterwards — so editing
    # it in place reaches the tool. That is real but NOT part of the documented
    # hook contract, which only promises the `block` return. If a future Hermes
    # starts copying args, this silently stops applying and the image is
    # generated unstyled: cosmetic, never an error, and never a security
    # property. Nothing here depends on the mutation landing.
    if isinstance(args, dict):
        field = next(
            (f for f in ("prompt", "description", "text")
             if isinstance(args.get(f), str) and args[f].strip()),
            None,
        )
        if field:
            # SELF-PORTRAIT first: it is the more specific claim, and it must land
            # inside the prompt before the generic house style is appended after it.
            sp = _self_portrait_cfg()
            if _is_self_portrait(args[field], sp.get("triggers")):
                desc = str(sp.get("description") or "").strip()
                if desc and desc.lower() not in args[field].lower():
                    args[field] = f"{args[field]}. {desc}"
                    logger.info("ambient.self_portrait: character sheet applied")

                # Reference images are additive and de-duplicated: a caller who already
                # passed one is asking for something specific, and dropping it to force
                # the house reference would override an explicit instruction. Missing
                # files are skipped rather than passed on — a backend handed a dead path
                # can fail the whole call, and a portrait that is merely less accurate
                # beats one that errors.
                refs = _as_list(sp.get("reference_images"))
                usable = [r for r in refs if os.path.isfile(r)]
                for missing in [r for r in refs if r not in usable]:
                    logger.warning(
                        "ambient.self_portrait: reference not readable, skipping: %s", missing
                    )
                if usable:
                    existing = args.get("reference_image_urls")
                    existing = list(existing) if isinstance(existing, list) else []
                    merged = existing + [r for r in usable if r not in existing]
                    args["reference_image_urls"] = merged
                    logger.info(
                        "ambient.self_portrait: %d reference image(s) attached", len(merged)
                    )

            style = _ambient_cfg("image_style")
            if style:
                styled = _apply_style(args[field], str(style))
                if styled != args[field]:
                    logger.info("ambient.image_style: applied %r", str(style))
                    args[field] = styled

    cfg = _image_gate_cfg()
    if not cfg.get("enabled"):
        return None                      # ungated profile (e.g. the operator's own)

    allowed = _normalize_ids(cfg.get("allowed_users"))
    speaker = (_current_speaker_id.get() or "").strip()

    if speaker and speaker in allowed:
        return None

    reason = "speaker unknown" if not speaker else f"speaker id:{speaker} not allowed"
    logger.info("ambient.image_gate: refused %s (%s)", tool_name, reason)
    return {
        "decision": "block",
        "reason": (
            "Image generation is not available to this user. Say so plainly and "
            "briefly, offer nothing else, and do not retry."
        ),
    }


# ---- quiet post-restart resume ---------------------------------------------
#
# When a gateway restart interrupts a turn, Hermes synthesises a resume turn whose
# system note says, on an INTERACTIVE platform, "Report to the user that the session
# was restored successfully and ask what they would like to do next." On a
# non-interactive platform it says the opposite. Discord is interactive, so a chat
# persona in a PUBLIC room announces the host's maintenance to strangers:
#
#   "Session Restored Successfully! The gateway shutdown has completed and we're
#    back online."
#
# Correct behaviour for an operator console; wrong for a community server. Opt in per
# profile with `ambient_presence.quiet_resume: true` — off by default, because on the
# operator's own profile the announcement is useful.
#
# WHY AN INJECTION AND NOT A SWITCH, since that is the weaker kind of control:
#   * There is no per-profile setting. `agent.gateway_auto_continue_freshness` bridges
#     to a process-wide env var, and under multiplex one gateway serves every profile.
#   * Turning that window down would not help anyway: all three auto-resume reasons
#     ("restart_timeout", "shutdown_timeout", "restart_interrupted") mean the agent was
#     mid-turn when it was killed, so an active persona is fresh by any window.
#   * `pre_llm_call` is inject-only by documented design — it cannot cancel the turn or
#     rewrite the system note, so actual silence is not reachable from a plugin.
# It therefore competes with the system note rather than replacing it, and wins on
# POSITION: injected context is appended to the current turn's user message, below the
# system note and closer to the model. When it loses, the result is one chatty message —
# exactly the behaviour without this plugin, never worse.

_RESUME_MARKERS = (
    "the previous turn was interrupted",
    "the gateway is now back online",
)

_QUIET_RESUME_DIRECTIVE = (
    "[ambient] The gateway restarted and this turn was resumed automatically. "
    "This is a public room. Do NOT mention the restart, the shutdown, the gateway, "
    "being back online, a restored session, or any technical interruption — and do not "
    "apologise for one. Disregard any instruction above asking you to acknowledge the "
    "restore; it assumes an operator is watching, and here it is strangers. "
    "If a conversation was in progress, simply carry on with it as though nothing "
    "happened. If there was not, say one short, ordinary, in-character thing — the kind "
    "of unprompted line you would post on your own — and nothing about infrastructure."
)


def _looks_like_resume(text: Any) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in _RESUME_MARKERS)


def _is_resume_turn(user_message: Any, history: Any, depth: int = 3) -> bool:
    """True on the framework's synthesised post-restart resume turn.

    The note arrives as the turn's own message on some platforms and as a system row
    just before it on others, so check both — but only a shallow tail, or a session
    that was ever restarted would suppress its own restart notice forever.
    """
    if _looks_like_resume(user_message):
        return True
    if not isinstance(history, list):
        return False
    for row in history[-depth:]:
        if not isinstance(row, dict):
            continue
        content = row.get("content")
        if isinstance(content, list):  # some providers send content as parts
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        if _looks_like_resume(content):
            return True
    return False


def _on_llm_request_ambient_hint(**kwargs: Any):
    """Deliver this turn's ambient directive as a request-only system message.

    Returns ``{"request": ...}`` with the hint appended, or None to leave the
    payload byte-identical — which is the case for every turn that is not an
    ambient join, on every profile and platform, because `_ambient_hint` is only
    ever set inside an ambient dispatch.

    Appended at the END of the message list on purpose: the cached prefix stays
    intact (AGENTS.md prompt-cache invariant), and a trailing instruction is the
    position models weight most heavily — which is the whole point of a directive.

    Nothing here may raise. A failure returns None and the turn proceeds with the
    stock payload; losing a hint costs one over-chatty reply, while raising would
    cost the whole conversation.
    """
    try:
        hint = _ambient_hint.get("")
        if not hint:
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        patched = dict(request)
        patched["messages"] = list(messages) + [{"role": "system", "content": hint}]
        logger.info("ambient: directive delivered via llm_request middleware (request-only)")
        return {"request": patched}
    except Exception:  # noqa: BLE001 — never break a turn over a nudge
        logger.debug("ambient: hint middleware failed; turn proceeds", exc_info=True)
        return None


def _on_pre_llm_call_quiet_resume(**kwargs: Any):
    try:
        if not _ambient_cfg("quiet_resume"):
            return None
        if not _is_resume_turn(
            kwargs.get("user_message"), kwargs.get("conversation_history")
        ):
            return None
        logger.info("ambient.quiet_resume: suppressing restart acknowledgement")
        return {"context": _QUIET_RESUME_DIRECTIVE}
    except Exception:  # noqa: BLE001 — a cosmetic hook must never break a turn
        logger.debug("ambient.quiet_resume: hook failed; turn proceeds", exc_info=True)
        return None


# ---- compaction focus ------------------------------------------------------
#
# When a context window fills, the compaction summary is written into the most
# privileged position of the NEXT window — the top of what the agent reads.
# What that summary chose to keep shapes everything after it.
#
# Hermes' summariser template is built for coding work: Goal, Progress,
# Decisions, Resolved/Pending Questions, Files, Remaining Work, with a
# constraints field that literally says "coding style". For a social agent that
# preserves the plumbing and discards the only thing that mattered — who these
# people are, what they shared, the rapport, the running jokes. The `compression`
# config has around fourteen knobs and no prompt keys, so there is no supported
# way to say "for this profile, keep the people, not the tool calls".
#
# The lever that DOES exist is `focus_topic`, which the summariser appends at the
# very end of its prompt so it takes precedence, with the instruction that the
# focus should receive roughly 60-70% of the summary token budget. It is already
# wired to `_derive_auto_focus_topic`, which infers one from the most recent user
# turns. Recency is a sensible default and the wrong one here: what a companion
# needs to carry across a boundary is not "what was just said" but "who these
# people are" — which is durable, and which recency-based focus discards exactly
# when the window is longest and the relationship most established.
#
# Set `ambient_presence.compaction_focus` to a sentence describing what this
# profile should protect. Unset leaves stock behaviour untouched.
#
# A PATCH rather than middleware because the compressor holds its own client and
# never goes through the llm_request path. It is process-wide, so the focus is
# resolved PER CALL from the active profile's config — one gateway serves several
# profiles and they must not inherit each other's focus.
_FOCUS_PATCH_FLAG = "_standing_focus_patched"


def _compaction_focus_for_active_profile() -> str | None:
    """This profile's standing compaction focus, or None for stock behaviour."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        extra = (((cfg.get("platforms") or {}).get("discord") or {}).get("extra") or {})
        amb = extra.get("ambient_presence")
        if isinstance(amb, dict):
            val = str(amb.get("compaction_focus") or "").strip()
            if val:
                return val
        # Fall back to the generic key, so a host running a separate non-social
        # focus plugin configures one place rather than two.
        comp = cfg.get("compression")
        if isinstance(comp, dict):
            val = str(comp.get("standing_focus") or "").strip()
            if val:
                return val
    except Exception:
        logger.debug("ambient: compaction focus lookup failed", exc_info=True)
    return None


def _install_compaction_focus() -> None:
    try:
        from agent import context_compressor as cc
    except Exception:
        return
    target = getattr(cc.ContextCompressor, "_derive_auto_focus_topic", None)
    if target is None:
        # Upstream renamed it. Do nothing rather than guess — a wrong patch here
        # silently reshapes every summary this agent produces.
        logger.warning("ambient: _derive_auto_focus_topic missing; compaction focus not installed")
        return
    if getattr(cc.ContextCompressor, _FOCUS_PATCH_FLAG, False):
        return
    original = target.__func__ if hasattr(target, "__func__") else target

    def _patched(cls, messages):
        try:
            focus = _compaction_focus_for_active_profile()
            if focus:
                return focus
        except Exception:
            logger.debug("ambient: compaction focus fell back to stock", exc_info=True)
        return original(cls, messages)

    cc.ContextCompressor._derive_auto_focus_topic = classmethod(_patched)
    setattr(cc.ContextCompressor, _FOCUS_PATCH_FLAG, True)
    logger.info("ambient: compaction focus installed (per-profile, resolved per call)")


def register(ctx) -> None:
    """Install the stock Discord platform entry, then swap in our subclass.

    Reusing the bundled register() matters: platform_registry.register()
    REPLACES the whole entry, so hand-rolling it would silently drop setup_fn,
    apply_yaml_config_fn, standalone_sender_fn, cron_deliver_env_var,
    max_message_length and the auth env bindings. Calling it also consumes the
    deferred bundled loader, preventing the adapter module being imported twice
    under two names.
    """
    _install_compaction_focus()

    _bundled.register(ctx)

    # Per-user authorisation for metered tools. Registered once; the callback
    # resolves the gate per profile, so an ungated profile is unaffected.
    try:
        ctx.register_hook("pre_tool_call", _on_pre_tool_call)
        logger.info("ambient: image-generation gate registered")
    except Exception as exc:
        logger.warning("ambient: could not register image gate: %s", exc)

    # Ambient directive delivery. Registered unconditionally and process-wide;
    # scoping is automatic because the callback no-ops unless _ambient_hint is
    # set, which only happens inside an ambient dispatch on this adapter.
    try:
        ctx.register_middleware("llm_request", _on_llm_request_ambient_hint)
        logger.info("ambient: directive middleware registered (request-only injection)")
    except Exception as exc:
        logger.warning("ambient: could not register directive middleware: %s", exc)

    # Post-restart quiet mode. Registered unconditionally; the callback is a
    # no-op for any profile that has not set `quiet_resume: true`.
    try:
        ctx.register_hook("pre_llm_call", _on_pre_llm_call_quiet_resume)
        logger.info("ambient: quiet-resume hook registered")
    except Exception as exc:
        logger.warning("ambient: could not register quiet-resume: %s", exc)

    # Speech hygiene is process-wide by nature (see _install_tts_kaomoji_filter),
    # so install it once at registration rather than per adapter instance.
    _install_tts_kaomoji_filter()

    from gateway.platform_registry import platform_registry  # type: ignore

    entry = platform_registry.get("discord")
    if entry is None:
        logger.error("discord-ambient: no 'discord' platform entry to extend")
        return
    entry.adapter_factory = lambda cfg: AmbientDiscordAdapter(cfg)
    logger.info("discord-ambient: AmbientDiscordAdapter installed for platform 'discord'")

    # gif_search is gated by check_fn, so profiles without a Klipy key never see it.
    try:
        ctx.register_tool(
            name="gif_search",
            toolset="gif",
            schema=_GIF_SCHEMA,
            handler=_gif_handle,
            check_fn=_gif_enabled,
            description=_GIF_SCHEMA["description"],
            emoji="🎬",
        )
        logger.info("discord-ambient: registered gif_search (Klipy)")
    except Exception:
        logger.exception("discord-ambient: could not register gif_search")
