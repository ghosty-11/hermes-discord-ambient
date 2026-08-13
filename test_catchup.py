"""Behavioural test for the catch-up (idle check-in) extension.

Run with the framework venv (system python cannot resolve the adapter import):

    cd /var/lib/hermes/.hermes/hermes-agent
    venv/bin/python /path/to/hermes-discord-ambient/test_catchup.py

Asserts DECISIONS — whether she speaks, where, and how often — rather than
plumbing. The fakes subclass the real discord.py classes so the production code
keeps its precise isinstance checks instead of being loosened for the test.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")

# State must not land in a real HERMES_HOME.
_STATE_DIR = tempfile.mkdtemp(prefix="catchup-test-")
os.environ["HERMES_HOME"] = _STATE_DIR

import discord  # noqa: E402
import plugins.platforms.discord.adapter as _ad  # noqa: E402

# Neutralise the stock __init__ (it wants a live token/session); everything
# below still runs the REAL ambient __init__ on top of it.
_ad.DiscordAdapter.__init__ = lambda self, config: setattr(self, "config", config)

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
spec = importlib.util.spec_from_file_location("ambient_under_test", PLUGIN)
amb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amb)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class FakeAuthor:
    def __init__(self, uid, name, bot=False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.bot = bot

    def __eq__(self, other):
        return getattr(other, "id", None) == self.id


class FakePerms:
    def __init__(self, ok=True):
        self.view_channel = ok
        self.read_message_history = ok
        self.send_messages = ok


class FakeGuild:
    def __init__(self):
        self.me = object()


class FakeChannel(discord.TextChannel):
    """A real TextChannel by type, with only the attributes the code reads.

    Subclassed rather than duck-typed so `_catchup_candidates` can keep its
    exact `isinstance(ch, discord.TextChannel)` check — the thing that keeps
    categories, voice channels and threads out of the wildcard.
    """

    def __init__(self, cid, msgs, name="general", can_speak=True, last_age=None):
        self.id = cid
        self.name = name
        self._msgs = msgs  # newest first
        self.guild = FakeGuild()
        self._can_speak = can_speak
        age = last_age if last_age is not None else (msgs[0].age_s if msgs else None)
        self.last_message_id = _snowflake(age) if age is not None else None
        for m in msgs:
            m.channel = self

    def permissions_for(self, _member):
        return FakePerms(self._can_speak)

    def history(self, limit=50):
        msgs = self._msgs[:limit]

        class _It:
            def __init__(self):
                self.i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.i >= len(msgs):
                    raise StopAsyncIteration
                m = msgs[self.i]
                self.i += 1
                return m

        return _It()


def _snowflake(age_s):
    """A Discord id whose encoded creation time is `age_s` seconds ago."""
    when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return ((int(when.timestamp() * 1000) - 1420070400000) << 22) | 1


class FakeMsg:
    def __init__(self, mid, author, content, age_s):
        self.id = mid
        self.author = author
        self.content = content
        self.age_s = age_s
        self.type = discord.MessageType.default
        self.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.attachments = []
        self.channel = None


BOT = FakeAuthor(999, "TestBot")
ALICE = FakeAuthor(1, "alice")
BOB = FakeAuthor(2, "bob")


class FakeClient:
    def __init__(self, channels):
        self.user = BOT
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(int(cid))

    def get_all_channels(self):
        return list(self._channels.values())


BASE = dict(
    enabled=True,
    channels=[4242],
    probability=1.0,
    min_gap_seconds=0,
    max_per_day=5,
    min_quiet_seconds=300,
    max_age_seconds=5400,
    min_messages=3,
    transcript_messages=8,
    startup_grace_seconds=0,   # tested explicitly below; off elsewhere
)


def make_adapter(catch_up=None, ambient_over=None, channels=None):
    extra = {
        "ambient_presence": {
            "enabled": True,
            "channels": ["*"],
            "probability": 0.15,
            "cooldown_seconds": 1800,
            "max_per_day": 10,
            "silent_marker": "[SILENT]",
            "speaker_identity": True,
            "catch_up": dict(catch_up or {}),
        }
    }
    extra["ambient_presence"].update(ambient_over or {})
    a = amb.AmbientDiscordAdapter(types.SimpleNamespace(extra=extra))
    a._dedup = set()
    a._get_parent_channel_id = lambda ch: None
    if channels is not None:
        a._client = FakeClient(channels)
    a._catchup_state_loaded = True  # opt in per test; most do not want disk
    return a


def stub_dispatch():
    seen = {"n": 0}

    async def fake(self, message):
        seen["n"] += 1
        seen["msg"] = message
        seen["hint"] = amb._ambient_hint.get("")
        seen["open"] = amb._ambient_open.get()
        seen["speaker"] = amb._speaker_context.get(("", None))
        return True

    _ad.DiscordAdapter._dispatch_discord_message = fake
    return seen


def live_room(cid=4242, name="general"):
    """Three humans talking, newest 10 minutes ago, bot silent before them."""
    return FakeChannel(cid, [
        FakeMsg(cid * 10 + 3, BOB, "yeah the deploy went fine in the end", 600),
        FakeMsg(cid * 10 + 2, ALICE, "did anyone look at the staging box", 700),
        FakeMsg(cid * 10 + 1, BOB, "morning all", 800),
        FakeMsg(cid * 10 + 0, BOT, "I was asleep", 9000),
    ], name=name)


async def main():
    print("\n-- qualifying room: she reads it and gets the transcript --")
    ch = live_room()
    a = make_adapter(BASE, channels=[ch])
    d = stub_dispatch()
    fired = await a._catchup_scan_channel(ch)
    check("dispatches on a conversation she missed", fired)
    hint = d.get("hint", "")
    check("transcript carries the actual messages",
          "bob: yeah the deploy went fine in the end" in hint and
          "alice: did anyone look at the staging box" in hint, repr(hint[:120]))
    check("her own last line is included so she does not repeat it",
          "TestBot: I was asleep" in hint)
    check("mention gates were open for the re-dispatch", d.get("open") is True)
    check("target is the newest human message", getattr(d.get("msg"), "id", None) == 42423)
    check("silence is offered as the default", "[SILENT]" in hint)
    check(
        "speaker identity reaches the catch-up request",
        d.get("speaker", ("", None))[0].startswith("[speaker @bob id:2]"),
        repr(d.get("speaker")),
    )
    check(
        "catch-up speaker identity is cleared after dispatch",
        amb._speaker_context.get(("", None)) == ("", None),
        repr(amb._speaker_context.get(("", None))),
    )

    print("\n-- the echo rule must catch every bracketed line of that hint --")
    for i, line in enumerate(hint.splitlines()):
        if line.startswith("[ambient"):
            stripped, n = amb._screen_ambient_reply(line, "[SILENT]")
            check(f"wrapper line {i} is strippable", n >= 1 and stripped is None,
                  repr(line[:60]))

    print("\n-- the per-message gates --")
    cases = [
        ("no check-in when hers is the newest message",
         FakeChannel(1, [FakeMsg(14, BOT, "sounds good", 300)] + live_room()._msgs)),
        ("no check-in while the conversation is in flight",
         FakeChannel(2, [FakeMsg(23, BOB, "still here", 30),
                         FakeMsg(22, ALICE, "hi", 40), FakeMsg(21, BOB, "hey", 50)])),
        ("no necromancy on a dead channel",
         FakeChannel(3, [FakeMsg(33, BOB, "night", 20000),
                         FakeMsg(32, ALICE, "bye", 20100),
                         FakeMsg(31, BOB, "see you", 20200)])),
    ]
    for label, chan in cases:
        a = make_adapter(BASE, channels=[chan])
        stub_dispatch()
        check(label, not await a._catchup_scan_channel(chan))

    ch = live_room()
    a = make_adapter(dict(BASE, min_messages=5), channels=[ch])
    stub_dispatch()
    check("no check-in below min_messages", not await a._catchup_scan_channel(ch))

    print("\n-- anti-repeat: one check-in per conversation, ever --")
    ch = live_room()
    a = make_adapter(BASE, channels=[ch])
    stub_dispatch()
    await a._catchup_scan_channel(ch)
    a._catchup_last = 0.0          # neutralise the gap: ONLY the seen-guard under test
    a._catchup_hits.clear()
    check("second pass on the same newest message is refused",
          not await a._catchup_scan_channel(ch))

    print("\n-- wildcard: any channel she can see --")
    rooms = [live_room(4242, "general"), live_room(5252, "random"),
             live_room(6262, "offtopic")]
    a = make_adapter(dict(BASE, channels=["*"]), channels=rooms)
    check("'*' resolves to every visible text channel", len(a._catchup_candidates()) == 3)
    muted = live_room(7272, "announcements")
    muted._can_speak = False
    a = make_adapter(dict(BASE, channels=["*"]), channels=rooms + [muted])
    ids = {c.id for c in a._catchup_candidates()}
    check("a channel she cannot speak in is excluded", 7272 not in ids)
    a = make_adapter(dict(BASE, channels=["*"], exclude_channels=[5252]),
                     channels=rooms)
    check("exclude_channels is honoured",
          {c.id for c in a._catchup_candidates()} == {4242, 6262})
    a = make_adapter(dict(BASE, channels=["*"]), channels=rooms,
                     ambient_over={"system_notices": {"reroute_channel": 6262}})
    check("the system-notice channel is auto-excluded",
          {c.id for c in a._catchup_candidates()} == {4242, 5252})
    a = make_adapter(dict(BASE, channels=[4242, 5252]), channels=rooms)
    check("an explicit list still means exactly those",
          {c.id for c in a._catchup_candidates()} == {4242, 5252})

    print("\n-- the scope log must distinguish 'watching none' from 'none qualified' --")
    import logging

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())  # already interpolates args

    cap = _Capture()
    amb.logger.addHandler(cap)
    amb.logger.setLevel(logging.INFO)
    try:
        # A wildcard over three rooms must not report "1 channel" just because
        # the CONFIG has one entry — the bug the first deploy shipped with.
        rooms = [live_room(4242, "general"), live_room(5252, "random"),
                 live_room(6262, "offtopic")]
        a = make_adapter(dict(BASE, channels=["*"], probability=0.0), channels=rooms)
        await a._catchup_pass()
        scope = [ln for ln in cap.lines if "watching" in ln]
        check("the resolved channel count is logged", len(scope) == 1, cap.lines)
        check("...and it is the RESOLVED count, not the config entry count",
              scope and "watching 3 channel(s)" in scope[0], scope)
        check("...naming them, so a wrong room is visible",
              scope and "#general" in scope[0] and "#offtopic" in scope[0], scope)
        cap.lines.clear()
        await a._catchup_pass()
        check("logged once per process, not once per pass",
              not [ln for ln in cap.lines if "watching" in ln])

        # The regression this ordering fixes: the scope line described a static
        # fact but sat behind the startup grace, so on the real box it would
        # not have printed for 30 minutes after a restart. It must arrive on
        # the first pass regardless of any gate that stops her SPEAKING.
        cap.lines.clear()
        g = make_adapter(dict(BASE, channels=["*"], startup_grace_seconds=1800),
                         channels=rooms)
        d = stub_dispatch()
        await g._catchup_pass()
        scope = [ln for ln in cap.lines if "watching" in ln]
        check("scope is logged even inside the startup grace",
              scope and "watching 3 channel(s)" in scope[0], cap.lines)
        check("...while the grace still suppresses the check-in itself", d["n"] == 0)

        cap.lines.clear()
        q = make_adapter(dict(BASE, channels=["*"], min_gap_seconds=7200),
                         channels=rooms)
        q._catchup_last = time.time() - 60          # budget spent
        await q._catchup_pass()
        check("scope is logged even when the budget is spent",
              [ln for ln in cap.lines if "watching 3 channel(s)" in ln], cap.lines)

        cap.lines.clear()
        mute = live_room(7272, "no-perms")
        mute._can_speak = False
        b = make_adapter(dict(BASE, channels=["*"], probability=0.0), channels=[mute])
        await b._catchup_pass()
        scope = [ln for ln in cap.lines if "watching" in ln]
        check("a scanner watching NOTHING says so explicitly",
              scope and "watching 0 channel(s)" in scope[0] and "none" in scope[0],
              scope)
    finally:
        amb.logger.removeHandler(cap)

    print("\n-- the free activity pre-filter (no API call) --")
    a = make_adapter(dict(BASE, channels=["*"]), channels=[
        live_room(1111, "quiet-enough"),
        FakeChannel(2222, [FakeMsg(1, BOB, "talking now", 10)], name="live"),
        FakeChannel(3333, [FakeMsg(1, BOB, "old", 40000)], name="dead"),
        FakeChannel(4444, [], name="never-used", last_age=None),
    ])
    ids = {c.id for c in a._catchup_candidates()}
    check("a live channel is skipped before any history read", 2222 not in ids)
    check("a cold channel is skipped before any history read", 3333 not in ids)
    check("a settled channel survives the filter", 1111 in ids)
    check("an unknown last_message_id falls through rather than being dropped",
          4444 in ids)

    print("\n-- one check-in per pass, however many channels qualify --")
    rooms = [live_room(1000 + i, f"room{i}") for i in range(8)]
    a = make_adapter(dict(BASE, channels=["*"], max_channels_per_pass=8),
                     channels=rooms)
    d = stub_dispatch()
    await a._catchup_pass()
    check("eight qualifying rooms produce exactly ONE check-in", d["n"] == 1)
    check("and it spends exactly one from the daily cap", len(a._catchup_hits) == 1)
    a = make_adapter(dict(BASE, channels=["*"], max_channels_per_pass=2, probability=0.0),
                     channels=rooms)
    cands = a._catchup_candidates()
    check("max_channels_per_pass does not shrink the candidate set", len(cands) == 8)

    print("\n-- startup grace: a restart must not be a reason to speak --")
    a = make_adapter(dict(BASE, channels=["*"], startup_grace_seconds=1800),
                     channels=[live_room()])
    d = stub_dispatch()
    await a._catchup_pass()
    check("no check-in inside the grace window", d["n"] == 0)
    check("_in_startup_grace agrees", a._in_startup_grace())
    a._catchup_started_at = time.time() - 3600
    check("...and it lapses", not a._in_startup_grace())
    await a._catchup_pass()
    check("a check-in is allowed once it has", d["n"] == 1)

    print("\n-- budget survives a restart --")
    ch = live_room()
    a = make_adapter(BASE, channels=[ch])
    a._catchup_state_loaded = False
    a._catchup_load_state()
    stub_dispatch()
    await a._catchup_scan_channel(ch)
    path = a._catchup_state_path()
    check("state was written", path and os.path.exists(path), str(path))
    saved = json.load(open(path))
    check("it records the check-in", len(saved["hits"]) == 1 and saved["last"] > 0)
    check("and the conversation it already read", str(42423) in saved["seen"])
    b = make_adapter(dict(BASE, min_gap_seconds=7200), channels=[ch])
    b._catchup_state_loaded = False
    b._catchup_load_state()
    check("a fresh process restores the gap", not b._catchup_quota_ok())
    check("a fresh process restores the already-read set",
          str(42423) in list(b._catchup_seen))
    os.remove(path)

    print("\n-- budget: the sub-cap and the shared ambient budget --")
    a = make_adapter(dict(BASE, min_gap_seconds=7200))
    a._catchup_last = time.time() - 60
    check("min_gap_seconds blocks", not a._catchup_quota_ok())
    a = make_adapter(dict(BASE, max_per_day=2))
    a._catchup_hits.extend([time.time(), time.time()])
    check("its own daily cap blocks", not a._catchup_quota_ok())
    a = make_adapter(BASE)
    a._ambient_last = time.time() - 60          # inside the 1800s ambient cooldown
    check("the SHARED ambient cooldown blocks a check-in", not a._catchup_quota_ok())
    a = make_adapter(dict(BASE, respect_ambient_budget=False))
    a._ambient_last = time.time() - 60
    check("...and only opting out lifts it", a._catchup_quota_ok())
    a = make_adapter(BASE)
    a._ambient_hits.extend([time.time()] * 10)  # ambient daily cap of 10 spent
    check("the shared daily cap blocks too", not a._catchup_quota_ok())

    print("\n-- quiet hours: off unless asked for (clock pinned to 03:00) --")
    a = make_adapter(BASE)
    check("absent from config means never quiet", not a._in_quiet_hours())

    class _TimeShim:
        """Real `time`, except localtime() reports a fixed hour. Without pinning
        it, the wrap-around branch is only exercised on whatever hour the suite
        happens to run at — which is how a green run means nothing."""

        def __init__(self, hour):
            self._h = hour

        def __getattr__(self, n):
            return getattr(time, n)

        def localtime(self, *a):
            t = time.localtime(*a)
            return time.struct_time(
                (t.tm_year, t.tm_mon, t.tm_mday, self._h, 0, 0,
                 t.tm_wday, t.tm_yday, t.tm_isdst)
            )

    real_time = amb.time
    amb.time = _TimeShim(3)
    try:
        for window, want, label in [
            ([1, 9], True, "03:00 inside a plain 01-09 window"),
            ([9, 17], False, "03:00 outside a daytime window"),
            ([22, 6], True, "03:00 inside a window that wraps midnight"),
            ([22, 2], False, "03:00 outside a wrapping window that ended at 02"),
            ([3, 3], False, "a degenerate start==end window is never quiet"),
            ("1,9", True, "a comma string from `hermes config set` parses"),
        ]:
            check(label, make_adapter(dict(BASE, quiet_hours=window))._in_quiet_hours()
                  is want)
        a = make_adapter(dict(BASE, quiet_hours=[22, 6]), channels=[live_room()])
        d = stub_dispatch()
        await a._catchup_pass()
        check("a full pass dispatches nothing during quiet hours", d["n"] == 0)
    finally:
        amb.time = real_time

    print("\n-- charge at send, not at dispatch --")
    ch = live_room()
    a = make_adapter(BASE, channels=[ch])
    stub_dispatch()
    await a._catchup_scan_channel(ch)
    check("dispatch alone does not spend the shared budget", a._ambient_last == 0.0)
    check("but it does spend a check-in", len(a._catchup_hits) == 1)
    a._catchup_count_sent("42423", None)
    check("speaking spends the shared budget", a._ambient_last > 0)
    ch2 = live_room()
    b = make_adapter(BASE, channels=[ch2])
    await b._catchup_scan_channel(ch2)
    b._catchup_discard_pending("42423")
    b._catchup_count_sent("42423", None)
    check("a [SILENT] check-in never spends it", b._ambient_last == 0.0)

    print("\n-- echo guard: never repost the room's own words --")
    ch = live_room()
    a = make_adapter(BASE, channels=[ch])
    stub_dispatch()
    await a._catchup_scan_channel(ch)
    leak = "bob: yeah the deploy went fine in the end\nalice: did anyone look at the staging box"
    check("two verbatim transcript lines are suppressed", a._catchup_echo_leak(leak))
    check("one quoted line is allowed through",
          not a._catchup_echo_leak("bob: yeah the deploy went fine in the end - glad it worked"))
    check("an ordinary reply is untouched",
          not a._catchup_echo_leak("mm. staging is always the one that breaks."))

    print("\n-- config shapes `hermes config set` actually produces --")
    check("a comma string parses",
          make_adapter(dict(BASE, channels="4242,777"))._catchup_channels()
          == ["4242", "777"])
    check("a bare int id parses",
          make_adapter(dict(BASE, channels=4242))._catchup_channels() == ["4242"])
    # The shape `hermes config set ... .channels '*'` actually writes: a bare
    # string, not a list. Deployed live on 2026-08-09, so it is worth asserting
    # rather than assuming the list form covers it.
    a = make_adapter(dict(BASE, channels="*"), channels=[live_room(), live_room(5252)])
    check("a bare '*' string is the wildcard", a._catchup_channels() == ["*"])
    check("...and resolves to every visible channel",
          len(a._catchup_eligible_channels()) == 2)

    print("\n-- off by default --")
    check("no catch_up block means disabled", not make_adapter(None)._catchup_enabled())
    check("ambient off means catch-up off",
          not make_adapter(dict(BASE), ambient_over={"enabled": False})._catchup_enabled())
    check("no channels means no candidates",
          make_adapter(dict(BASE, channels=[]), channels=[live_room()])
          ._catchup_candidates() == [])

    print("\n-- transcript bounds --")
    a = make_adapter(dict(BASE, transcript_max_chars=200, transcript_message_chars=50))
    t = a._render_transcript([FakeMsg(i, ALICE, "x" * 500, 100) for i in range(10)])
    check("total cap holds", len(t) <= 201, len(t))
    check("per-message cap holds", all(len(ln) <= 70 for ln in t.splitlines()))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("all checks passed")


asyncio.run(main())
