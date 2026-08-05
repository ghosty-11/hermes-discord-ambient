"""Discord ambient-presence plugin for Hermes Agent.

WHY
---
Hermes' chat gate is binary: a bot answers every message in a channel, or only
when @mentioned. Neither models a person who lurks in a community and chimes in
occasionally. Gateway hooks can't help (fire-and-forget observers; only
pre_tool_call can veto) and `[SILENT]` suppression exists only in cron.

This subclasses the bundled Discord adapter — never forks it, so upstream fixes
to that 10k-line file keep flowing — and adds six opt-in behaviours:

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
   the topology as unsupported with no breaker. After 3-5 replies to a given
   bot in a given conversation (rolled per conversation so her patience
   varies), the last reply carries a goodbye hint and every later message from
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
10. SLASH-COMMAND POLICY — chat admission and slash auth share one gate
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
  * self._dedup.discard/contains/is_duplicate(message_id) / self._client
  * standby busy probe (all optional — absence degrades to "not busy"):
      - BasePlatformAdapter.gateway_runner (base.py declares it; run.py stamps
        `adapter.gateway_runner = self` on every registry-created adapter)
      - runner._running_agents / runner._running_agents_ts (profile-namespaced
        session keys; pending sentinel placed synchronously at turn start)
      - cron.scheduler.get_running_job_ids() + jobs.json "no_agent" field
      - tools.async_delegation.active_count()
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

# True only inside one ambient re-dispatch. ContextVars are per-task, so a
# concurrent stock message on another task is unaffected — and the backfill
# helper that shares _discord_free_response_channels() never sees it.
_ambient_open: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_ambient_open", default=False
)

# Channel keys for which threading is suppressed, scoped to one dispatch task.
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

    def _ambient_quota_ok(self) -> bool:
        cfg = self._ambient_cfg()
        now = time.time()
        if now - self._ambient_last < float(cfg.get("cooldown_seconds", 1800)):
            return False
        cutoff = now - 86400
        while self._ambient_hits and self._ambient_hits[0] < cutoff:
            self._ambient_hits.popleft()
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
            if not self._ambient_quota_ok():
                return None
            return "random" if random.random() < float(cfg.get("probability", 0.12)) else None
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
                message.content = f"{hint}\n\n{getattr(message, 'content', '')}"
                logger.info(
                    "ambient.bot_bounce: last allowed reply to %s — goodbye hint injected",
                    who,
                )
            except Exception:
                pass  # frozen message object; the breaker still trips next round
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
        try:
            return await self._dispatch_inner(message)
        finally:
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
            try:
                message.content = f"{hint}\n\n{getattr(message, 'content', '')}"
            except Exception:
                pass  # some message objects are frozen; the nudge is optional

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
        def _ids(v) -> set:
            # Accept a YAML list or a comma-separated string — `hermes config
            # set` can only write scalars, so the string form must work.
            parts = v.split(",") if isinstance(v, str) else (v if isinstance(v, (list, tuple)) else [])
            return {str(p).strip() for p in parts if str(p).strip()}

        try:
            sc = self._sub("slash_commands")
            chans = _ids(sc.get("allowed_channels"))
            users = _ids(sc.get("allowed_users"))
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
        return ok

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
        reply_to = kwargs.get("reply_to", args[0] if args else None)
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
        if self._ambient_enabled() and isinstance(content, str):
            marker = self._ambient_marker()
            stripped = content.strip()
            if stripped == marker or (
                stripped.startswith(marker) and len(stripped) <= len(marker) + 8
            ):
                logger.info("ambient: response suppressed by %s sentinel", marker)
                # A swallowed reply was never sent: it must not count against
                # the pair, nor sit around to be charged to a later send.
                self._bounce_discard_pending(reply_to)
                try:
                    from gateway.platforms.base import SendResult  # type: ignore

                    return SendResult(success=True, message_id=None)
                except Exception:
                    return None
        result = await super().send(chat_id, content, *args, **kwargs)
        self._bounce_count_sent(reply_to, result)
        return result


def register(ctx) -> None:
    """Install the stock Discord platform entry, then swap in our subclass.

    Reusing the bundled register() matters: platform_registry.register()
    REPLACES the whole entry, so hand-rolling it would silently drop setup_fn,
    apply_yaml_config_fn, standalone_sender_fn, cron_deliver_env_var,
    max_message_length and the auth env bindings. Calling it also consumes the
    deferred bundled loader, preventing the adapter module being imported twice
    under two names.
    """
    _bundled.register(ctx)
    from gateway.platform_registry import platform_registry  # type: ignore

    entry = platform_registry.get("discord")
    if entry is None:
        logger.error("discord-ambient: no 'discord' platform entry to extend")
        return
    entry.adapter_factory = lambda cfg: AmbientDiscordAdapter(cfg)
    logger.info("discord-ambient: AmbientDiscordAdapter installed for platform 'discord'")
