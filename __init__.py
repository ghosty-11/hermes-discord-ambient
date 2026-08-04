"""Discord ambient-presence plugin for Hermes Agent.

WHY
---
Hermes' chat gate is binary: a bot answers every message in a channel, or only
when @mentioned. Neither models a person who lurks in a community and chimes in
occasionally. Gateway hooks can't help (fire-and-forget observers; only
pre_tool_call can veto) and `[SILENT]` suppression exists only in cron.

This subclasses the bundled Discord adapter — never forks it, so upstream fixes
to that 10k-line file keep flowing — and adds four opt-in behaviours:

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
  * DiscordAdapter.connect(*, is_reconnect) -> bool
  * DiscordAdapter.send(...)  (delegated via *args/**kwargs)
  * DiscordAdapter._add_reaction(message, emoji) -> bool
  * self._dedup.discard(message_id) / self._client
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

_DEFAULT_REACTIONS = ["👀", "😹", "✨", "🐈", "💅", "🔥"]


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
            home = os.getenv("HERMES_HOME") or "/var/lib/hermes/.hermes"
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

    def _should_join_ambiently(self, message: Any) -> bool:
        cfg = self._ambient_cfg()
        try:
            if not self._basic_ambient_eligible(message):
                return False
            if not self._ambient_quota_ok():
                return False
            content = (getattr(message, "content", "") or "").lower()
            # A plain-text name reference is the case Discord's @-mention
            # detection misses entirely — always worth considering.
            triggers = [str(t).lower() for t in (cfg.get("name_triggers") or [])]
            if triggers and any(t in content for t in triggers):
                return True
            return random.random() < float(cfg.get("probability", 0.12))
        except Exception:
            logger.debug("ambient pre-filter failed; falling back to stock", exc_info=True)
            return False

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

    async def _dispatch_discord_message(self, message: Any) -> bool:
        # Stock path first: preserves the dedup claim and every auth gate.
        if await super()._dispatch_discord_message(message):
            return True

        if not self._ambient_enabled():
            return False
        try:
            returning_days = self._returning_after_absence(message)
            eligible = self._basic_ambient_eligible(message)
            join = False
            if eligible and returning_days and self._ambient_quota_ok():
                join = True  # a real return beats the dice
            elif self._should_join_ambiently(message):
                join = True

            if not join:
                await self._maybe_react(message)  # seen, but not worth words
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

            now = time.time()
            self._ambient_last = now
            self._ambient_hits.append(now)

            token = _ambient_open.set(True)
            try:
                return await super()._dispatch_discord_message(message)
            finally:
                _ambient_open.reset(token)
        except Exception:
            logger.warning("ambient dispatch failed; message left unanswered", exc_info=True)
            return False

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

    # ---- silence --------------------------------------------------------
    async def send(self, chat_id: str, content: str, *args: Any, **kwargs: Any):
        """Swallow the sentinel so the agent may choose to stay quiet."""
        if self._ambient_enabled() and isinstance(content, str):
            marker = self._ambient_marker()
            stripped = content.strip()
            if stripped == marker or (
                stripped.startswith(marker) and len(stripped) <= len(marker) + 8
            ):
                logger.info("ambient: response suppressed by %s sentinel", marker)
                try:
                    from gateway.platforms.base import SendResult  # type: ignore

                    return SendResult(success=True, message_id=None)
                except Exception:
                    return None
        return await super().send(chat_id, content, *args, **kwargs)


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
