"""Behavioural test for the catch-up (idle check-in) extension.

Run with the framework venv:
  cd /var/lib/hermes/.hermes/hermes-agent && venv/bin/python <this file>

Exercises the gates that decide whether she speaks, because those are the ones
that make the difference between "present" and "obnoxious". Every case asserts
the DECISION, not the plumbing.
"""
import asyncio
import importlib.util
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")

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


class FakeChannel:
    def __init__(self, cid, msgs):
        self.id = cid
        self._msgs = msgs  # newest first

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


class FakeMsg:
    def __init__(self, mid, author, content, age_s):
        self.id = mid
        self.author = author
        self.content = content
        self.type = discord.MessageType.default
        self.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.attachments = []
        self.channel = None


BOT = FakeAuthor(999, "TestBot")
ALICE = FakeAuthor(1, "alice")
BOB = FakeAuthor(2, "bob")


class FakeClient:
    def __init__(self, channel):
        self.user = BOT
        self._channel = channel

    def get_channel(self, cid):
        return self._channel if int(cid) == self._channel.id else None


def make_adapter(catch_up=None, ambient_over=None):
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
    cfg = types.SimpleNamespace(extra=extra)
    a = amb.AmbientDiscordAdapter(cfg)
    a._dedup = set()
    a._get_parent_channel_id = lambda ch: None
    return a


def scenario(msgs, catch_up=None, ambient_over=None, cid=4242):
    ch = FakeChannel(cid, msgs)
    for m in msgs:
        m.channel = ch
    a = make_adapter(catch_up, ambient_over)
    a._client = FakeClient(ch)
    dispatched = {}

    async def fake_dispatch(self, message):
        dispatched["msg"] = message
        dispatched["hint"] = amb._ambient_hint.get("")
        dispatched["open"] = amb._ambient_open.get()
        return True

    _ad.DiscordAdapter._dispatch_discord_message = fake_dispatch
    return a, dispatched


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
)


def live_room():
    """Three humans talking, last message 10 minutes ago, bot silent before."""
    return [
        FakeMsg(103, BOB, "yeah the deploy went fine in the end", 600),
        FakeMsg(102, ALICE, "did anyone look at the staging box", 700),
        FakeMsg(101, BOB, "morning all", 800),
        FakeMsg(100, BOT, "I was asleep", 9000),
    ]


async def main():
    print("\n-- qualifying room: she reads it and gets the transcript --")
    a, d = scenario(live_room(), BASE)
    fired = await a._catchup_scan_channel("4242")
    check("dispatches on a conversation she missed", fired)
    hint = d.get("hint", "")
    check("transcript carries the actual messages",
          "bob: yeah the deploy went fine in the end" in hint and
          "alice: did anyone look at the staging box" in hint, repr(hint[:120]))
    check("her own last line is included so she does not repeat it",
          "TestBot: I was asleep" in hint)
    check("mention gates were open for the re-dispatch", d.get("open") is True)
    check("target is the newest human message", getattr(d.get("msg"), "id", None) == 103)
    check("silence is offered as the default", "[SILENT]" in hint)

    print("\n-- the echo rule must catch every bracketed line of that hint --")
    for i, line in enumerate(hint.splitlines()):
        if line.startswith("[ambient"):
            stripped, n = amb._screen_ambient_reply(line, "[SILENT]")
            check(f"wrapper line {i} is strippable", n >= 1 and stripped is None,
                  repr(line[:60]))

    print("\n-- she spoke last: never talk to yourself --")
    msgs = live_room()
    msgs.insert(0, FakeMsg(104, BOT, "sounds good", 300))
    a, d = scenario(msgs, BASE)
    check("no check-in when hers is the newest message",
          not await a._catchup_scan_channel("4242"))

    print("\n-- nothing was missed --")
    a, d = scenario(live_room(), dict(BASE, min_messages=5))
    check("no check-in below min_messages", not await a._catchup_scan_channel("4242"))

    print("\n-- room still live: that belongs to the per-message dice --")
    a, d = scenario(
        [FakeMsg(203, BOB, "still here", 30), FakeMsg(202, ALICE, "hi", 40),
         FakeMsg(201, BOB, "hey", 50)], BASE)
    check("no check-in while the conversation is in flight",
          not await a._catchup_scan_channel("4242"))

    print("\n-- room went cold hours ago --")
    a, d = scenario(
        [FakeMsg(303, BOB, "night", 20000), FakeMsg(302, ALICE, "bye", 20100),
         FakeMsg(301, BOB, "see you", 20200)], BASE)
    check("no necromancy on a dead channel",
          not await a._catchup_scan_channel("4242"))

    print("\n-- anti-repeat: one check-in per conversation, ever --")
    a, d = scenario(live_room(), BASE)
    await a._catchup_scan_channel("4242")
    a._catchup_last = 0.0          # neutralise the gap so ONLY the seen-guard is under test
    a._catchup_hits.clear()
    check("second pass on the same newest message is refused",
          not await a._catchup_scan_channel("4242"))

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

    print("\n-- quiet hours (clock pinned to 03:00, so the wrap branch is real) --")

    class _TimeShim:
        """Real `time`, except localtime() always reports a fixed hour. Without
        pinning it, the wrap-around branch is only exercised on whatever hour
        the suite happens to run at — which is how a green run means nothing."""

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
        cases = [
            ([1, 9], True, "03:00 inside a plain 01-09 window"),
            ([9, 17], False, "03:00 outside a daytime window"),
            ([22, 6], True, "03:00 inside a window that wraps midnight"),
            ([22, 2], False, "03:00 outside a wrapping window that ended at 02"),
            ([3, 3], False, "a degenerate start==end window is never quiet"),
            ("1,9", True, "a comma string from `hermes config set` parses"),
        ]
        for window, want, label in cases:
            a = make_adapter(dict(BASE, quiet_hours=window))
            check(label, a._in_quiet_hours() is want)
        a = make_adapter(dict(BASE, quiet_hours=[22, 6]))
        a._catchup_last = 0.0
        fired = {"n": 0}

        async def never(self, message):
            fired["n"] += 1
            return True

        _ad.DiscordAdapter._dispatch_discord_message = never
        ch = FakeChannel(4242, live_room())
        for m in ch._msgs:
            m.channel = ch
        a._client = FakeClient(ch)
        await a._catchup_pass()
        check("a full pass dispatches nothing during quiet hours", fired["n"] == 0)
    finally:
        amb.time = real_time

    print("\n-- charge at send, not at dispatch --")
    a, d = scenario(live_room(), BASE)
    await a._catchup_scan_channel("4242")
    check("dispatch alone does not spend the shared budget", a._ambient_last == 0.0)
    check("but it does spend a check-in", len(a._catchup_hits) == 1)
    a._catchup_count_sent("103", None)
    check("speaking spends the shared budget", a._ambient_last > 0)
    a2, _ = scenario(live_room(), BASE)
    await a2._catchup_scan_channel("4242")
    a2._catchup_discard_pending("103")
    a2._catchup_count_sent("103", None)
    check("a [SILENT] check-in never spends it", a2._ambient_last == 0.0)

    print("\n-- echo guard: never repost the room's own words --")
    a, d = scenario(live_room(), BASE)
    await a._catchup_scan_channel("4242")
    leak = "bob: yeah the deploy went fine in the end\nalice: did anyone look at the staging box"
    check("two verbatim transcript lines are suppressed", a._catchup_echo_leak(leak))
    check("one quoted line is allowed through",
          not a._catchup_echo_leak("bob: yeah the deploy went fine in the end — glad it worked"))
    check("an ordinary reply is untouched",
          not a._catchup_echo_leak("mm. staging is always the one that breaks."))

    print("\n-- channels: no wildcard on the timer path --")
    a = make_adapter(dict(BASE, channels=["*"]))
    check("'*' is refused as a catch-up channel", a._catchup_channels() == [])
    a = make_adapter(dict(BASE, channels="4242,777"))
    check("a comma string from `hermes config set` still parses",
          a._catchup_channels() == ["4242", "777"])
    a = make_adapter(dict(BASE, channels=4242))
    check("a bare int id still parses", a._catchup_channels() == ["4242"])

    print("\n-- off by default --")
    a = make_adapter(None)
    check("no catch_up block means disabled", not a._catchup_enabled())
    a = make_adapter(dict(BASE), ambient_over={"enabled": False})
    check("ambient off means catch-up off", not a._catchup_enabled())

    print("\n-- transcript bounds --")
    a = make_adapter(dict(BASE, transcript_max_chars=200, transcript_message_chars=50))
    long_msgs = [FakeMsg(i, ALICE, "x" * 500, 100) for i in range(10)]
    t = a._render_transcript(long_msgs)
    check("total cap holds", len(t) <= 201, len(t))
    check("per-message cap holds", all(len(ln) <= 70 for ln in t.splitlines()))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("all checks passed")


asyncio.run(main())
