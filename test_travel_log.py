"""The travel log: durable, idle-based continuity across sessions and spaces.

Covers the frozen travel-log contract: the two clock touch points (inbound
observe, her own send), the closure table (quiet = AND of both idles, or the
lurk cap measured from opened_at when she never spoke), restart backfill,
horizon pruning on save, the projection block (merged same-lane beats,
lurk-only rendering, budgets, the occupancy line, echo strippability),
request-only delivery, the persisted room-talk snapshot, the no-disk hot
path, and config-shape tolerance. Everything staged is request-only:
nothing may be written onto the persisted user turn.

Run from the framework checkout:

  cd /var/lib/hermes/.hermes/hermes-agent
  venv/bin/python /path/to/hermes-discord-ambient/test_travel_log.py
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import types
from contextlib import contextmanager

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="travel-log-test-")

import discord  # noqa: E402
import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_travel_log_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


@contextmanager
def case(name):
    """One contract case. An unexpected exception is a red case, not a crash:
    the suite lands beside (not after) the implementation, so it must run to
    the end and name every gap while the feature is missing."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — a missing feature must not kill the run
        check(name, False, f"raised {type(exc).__name__}: {exc}")


class Author:
    def __init__(self, uid, name, display=None, bot=False):
        self.id = uid
        self.name = name
        self.display_name = display or name
        self.global_name = None
        self.nick = None
        self.bot = bot


class Chan:
    def __init__(self, cid, name, guild=None, topic=None, parent=None):
        self.id = cid
        self.name = name
        self.guild = guild
        self.topic = topic
        self.parent = parent


class Guild:
    def __init__(self, gid, name):
        self.id = gid
        self.name = name


class Message:
    def __init__(self, mid, author, channel, content="", reference=None):
        self.id = mid
        self.author = author
        self.channel = channel
        self.content = content
        self.reference = reference
        self.mentions = []
        self.attachments = []


HOME = Guild(777, "Home Server")
ANARCHY = Guild(888, "Community Server")
GENERAL = Chan(501, "general", guild=ANARCHY, topic="agents hanging out")
RANDOM = Chan(506, "random", guild=HOME)
SHRINE = Chan(502, "shrine", guild=HOME)
OFFTOPIC = Chan(507, "off-topic", guild=HOME)

ALICE = Author(101, "alice", display="Alice A")
BOB = Author(102, "bob", display="Bob B")
HER = Author(999, "traveltest", bot=True)

MIN = 60  # every contract clock is written in minutes

TRAVEL_ON = {"enabled": True}  # partial block: the contract defaults fill the rest


def build_adapter(travel=TRAVEL_ON, room=None):
    # travel=None omits the block entirely (stock behaviour); anything else is
    # the travel_log dict as YAML would deliver it.
    room_cfg = {"enabled": True, "recent_messages": 6, "max_chars": 700}
    if room:
        room_cfg.update(room)
    presence = {
        "enabled": True,
        "channels": ["*"],
        "probability": 0.0,
        "room_context": room_cfg,
    }
    if travel is not None:
        presence["travel_log"] = travel
    adapter = ambient.AmbientDiscordAdapter(
        types.SimpleNamespace(extra={"ambient_presence": presence})
    )
    adapter._client = types.SimpleNamespace(user=HER)
    adapter._ambient_last = 0.0
    adapter._last_seen = {}
    adapter._seen_loaded = True
    return adapter


@contextmanager
def state_home(prefix="travel-state-"):
    """A private HERMES_HOME per case. Travel state is keyed on the bot's own
    id, so without this every case would read and write its siblings' lanes."""
    home = tempfile.mkdtemp(prefix=prefix)
    old = os.environ["HERMES_HOME"]
    os.environ["HERMES_HOME"] = home
    try:
        yield home
    finally:
        os.environ["HERMES_HOME"] = old


def state_file(home, name):
    return os.path.join(home, "state", f"{name}-999.json")


def seed_travel_store(home, beats, lanes):
    os.makedirs(os.path.join(home, "state"), exist_ok=True)
    with open(state_file(home, "ambient-travel-log"), "w") as fh:
        json.dump({"beats": beats, "lanes": {str(k): v for k, v in lanes.items()}}, fh)


def seed_room_talk_store(home, room_talk, touched):
    os.makedirs(os.path.join(home, "state"), exist_ok=True)
    with open(state_file(home, "ambient-room-talk"), "w") as fh:
        json.dump({"room_talk": room_talk, "touched": touched}, fh)


def arrive(adapter, message):
    """One inbound message through both room-talk capture and travel observe,
    in the order _dispatch_inner runs them."""
    adapter._remember_room_talk(message)
    adapter._travel_observe(message)


def age(lane, opened_m, obs_m, spoke_m):
    """Wind a lane's three clocks to fixed ages in minutes.

    spoke_m=None means NULL: she has not spoken in this beat. The passage of
    time is the only thing injected here — the fields are the contract's own.
    """
    now = time.time()
    lane["opened_at"] = now - opened_m * MIN
    lane["last_observed_at"] = now - obs_m * MIN
    lane["last_spoke_at"] = None if spoke_m is None else now - spoke_m * MIN


def mk_lane(chan, opened_m, obs_m, spoke_m, her=0, parts=None):
    now = time.time()
    guild = getattr(chan, "guild", None)
    parent = getattr(chan, "parent", None)
    return {
        "channel_id": chan.id,
        "guild_id": getattr(guild, "id", None),
        "guild_name": getattr(guild, "name", None),
        "channel_name": chan.name,
        "chat_type": "channel",
        "thread_parent": getattr(parent, "id", None) if parent is not None else None,
        "opened_at": now - opened_m * MIN,
        "last_observed_at": now - obs_m * MIN,
        "obs_count": 4,
        "last_spoke_at": None if spoke_m is None else now - spoke_m * MIN,
        "her_msg_count": her,
        "participants": parts if parts is not None else [[101, "alice"]],
    }


def mk_beat(bid, chan, opened_m, closed_m, her=1, parts=None, snippets=None, reason="quiet"):
    lane = mk_lane(chan, opened_m, opened_m, None, her, parts)
    return {
        "id": bid,
        "guild_id": lane["guild_id"],
        "guild_name": lane["guild_name"],
        "channel_id": lane["channel_id"],
        "channel_name": lane["channel_name"],
        "chat_type": lane["chat_type"],
        "thread_parent": lane["thread_parent"],
        "opened_at": lane["opened_at"],
        "closed_at": time.time() - closed_m * MIN,
        "close_reason": reason,
        "obs_count": lane["obs_count"],
        "her_msg_count": her,
        "participants": lane["participants"],
        "snippets": ["we shipped the deploy"] if snippets is None else snippets,
    }


def beat_for(adapter, chan):
    return next(
        (b for b in adapter._travel_beats if str(b.get("channel_id")) == str(chan.id)),
        None,
    )


# A time figure ("3h", "45 min", "1.5h" …) — the contract requires a time-ago
# and a duration on every beat line, so a line without one is a missing fact.
TIME_FIGURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s|days?|d)\b",
    re.IGNORECASE,
)


print("config gating and defaults")
with case("no travel_log block means stock behaviour"):
    with state_home():
        a = build_adapter(travel=None)
        a._travel_observe(Message(1, ALICE, GENERAL, "hi"))
        check("no lane opens without travel_log", not getattr(a, "_travel_lanes", {}), a._travel_lanes)
        check("nothing is projected without travel_log",
              a._travel_rows(Message(2, ALICE, GENERAL, "hi")) == [])

with case("travel_log.enabled false is stock behaviour too"):
    with state_home():
        a = build_adapter(travel={"enabled": False})
        a._travel_observe(Message(3, ALICE, GENERAL, "hi"))
        check("disabled means no lane", not getattr(a, "_travel_lanes", {}), a._travel_lanes)

with case("a partial block fills the contract defaults"):
    with state_home():
        a = build_adapter(travel={"enabled": True})
        cfg = a._travel_cfg()
        check("defaults land in the parsed config",
              cfg.get("idle_minutes") == 60 and cfg.get("lurk_max_minutes") == 360
              and cfg.get("horizon_hours") == 32 and cfg.get("max_events") == 8
              and cfg.get("include_lurk_only") is True, cfg)


print("the two clocks: arrival and her own send")
with case("an inbound message opens and feeds the lane"):
    with state_home():
        a = build_adapter()
        arrive(a, Message(10, ALICE, GENERAL, "hello room"))
        check("lane keyed by channel id", "501" in a._travel_lanes, list(a._travel_lanes))
        lane = a._travel_lanes.get("501", {})
        check("arrival stamps last_observed_at",
              lane and time.time() - lane["last_observed_at"] < 5, lane.get("last_observed_at"))
        check("arrival counts the observation", lane and lane["obs_count"] == 1, lane.get("obs_count"))
        check("participants collect the speaker",
              lane and any(str(p[0]) == "101" for p in lane["participants"]),
              lane.get("participants"))
        check("she has not spoken: last_spoke_at stays NULL",
              lane and lane["last_spoke_at"] is None, lane.get("last_spoke_at"))
        arrive(a, Message(11, BOB, GENERAL, "hey"))
        check("a second arrival updates, never duplicates",
              len(a._travel_lanes) == 1 and a._travel_lanes["501"]["obs_count"] == 2,
              list(a._travel_lanes))

with case("her send updates both clocks and her count"):
    with state_home():
        a = build_adapter()
        arrive(a, Message(12, ALICE, GENERAL, "hi"))
        # an old observation must not survive as "the room is fresh" — her own
        # send refreshes BOTH clocks, never relying on the MESSAGE_CREATE echo.
        a._travel_lanes["501"]["last_observed_at"] = time.time() - 30 * MIN
        a._travel_spoke("501")
        lane = a._travel_lanes["501"]
        check("send stamps last_spoke_at",
              lane["last_spoke_at"] and time.time() - lane["last_spoke_at"] < 5,
              lane.get("last_spoke_at"))
        check("send advances her_msg_count", lane["her_msg_count"] == 1, lane.get("her_msg_count"))
        check("send also refreshes last_observed_at",
              time.time() - lane["last_observed_at"] < 5, lane.get("last_observed_at"))
        check("she is never a participant",
              all(str(p[0]) != "999" for p in lane["participants"]), lane.get("participants"))

with case("her send into a room with no open lane opens one"):
    with state_home():
        a = build_adapter()
        a._travel_spoke("501")
        lane = a._travel_lanes.get("501")
        check("a self-initiated lane exists and counts her message",
              lane is not None and lane["her_msg_count"] == 1, lane)


print("closure table")
with case("a: a long chat she joined, room quiet 61 min -> closed 'quiet'"):
    with state_home():
        a = build_adapter()
        for i in range(5):
            arrive(a, Message(20 + i, ALICE if i % 2 else BOB, GENERAL, f"chat line number {i}"))
        # her own words are in the room buffer (the gateway echoes her) but
        # must never surface as a snippet of the room's talk.
        a._remember_room_talk(Message(29, HER, GENERAL, "HEROWNWORDSFORTHEVISIT"))
        a._travel_spoke("501")
        # Production persists mid-visit every sweep tick — a 5h chat has been
        # on disk ~60 times before it goes quiet. Persisting here also keeps
        # the restart backfill (a LOAD-time pass) out of a live-closure case.
        a._travel_sweep()
        age(a._travel_lanes["501"], opened_m=300, obs_m=61, spoke_m=100)
        a._travel_sweep()
        beats = [b for b in a._travel_beats if str(b.get("channel_id")) == "501"]
        check("exactly one closed beat", len(beats) == 1, a._travel_beats)
        beat = beats[0] if beats else {}
        check("closed quiet", beat.get("close_reason") == "quiet", beat.get("close_reason"))
        check("her messages counted", (beat.get("her_msg_count") or 0) >= 1, beat.get("her_msg_count"))
        check("observations carried into the beat", beat.get("obs_count") == 5, beat.get("obs_count"))
        check("participants are the humans, never her",
              beat and {str(p[0]) for p in beat["participants"]} == {"101", "102"},
              beat.get("participants"))
        check("snippets come from the room, bounded and flat",
              beat and 1 <= len(beat["snippets"]) <= 4
              and all(len(s) <= 90 for s in beat["snippets"]),
              beat.get("snippets"))
        check("her own words never become a snippet",
              beat and not any("HEROWNWORDS" in s for s in beat["snippets"]),
              beat.get("snippets"))
        check("the beat spans the visit, not just the sweep moment",
              beat and beat["closed_at"] - beat["opened_at"] >= 3 * 3600,
              beat and (beat["closed_at"] - beat["opened_at"]))
        check("the lane is gone once closed", "501" not in a._travel_lanes, list(a._travel_lanes))

with case("b: others talk for hours, she never speaks -> lurk cap at opened+360"):
    with state_home():
        a = build_adapter()
        for i in range(3):
            arrive(a, Message(30 + i, BOB, GENERAL, f"still going {i}"))
        a._travel_sweep()  # persisted mid-visit, as any 8h room would be
        age(a._travel_lanes["501"], opened_m=480, obs_m=61, spoke_m=None)
        a._travel_sweep()
        beat = beat_for(a, GENERAL)
        check("closed by the lurk cap", beat and beat["close_reason"] == "lurk_cap",
              beat and beat.get("close_reason"))
        check("she said nothing", beat and beat["her_msg_count"] == 0, beat and beat.get("her_msg_count"))
        check("the beat is dated where the cap passed, not at sweep time",
              beat and abs(beat["closed_at"] - (beat["opened_at"] + 360 * MIN)) <= 5,
              beat and (beat["closed_at"] - (beat["opened_at"] + 360 * MIN)))

with case("c: room idle 90 min but she spoke 10 min ago -> lane stays OPEN"):
    with state_home():
        # Regression pair with d: an earlier rule draft closed on EITHER
        # clock. The AND must hold — her speaking keeps the visit alive even
        # when the room has gone quiet around her.
        a = build_adapter()
        arrive(a, Message(40, ALICE, GENERAL, "hi"))
        a._travel_spoke("501")
        a._travel_sweep()  # persisted mid-visit, as production would have
        age(a._travel_lanes["501"], opened_m=200, obs_m=90, spoke_m=10)
        a._travel_sweep()
        check("still open: the AND must hold", "501" in a._travel_lanes, list(a._travel_lanes))
        check("nothing closed", not a._travel_beats, a._travel_beats)

with case("d: room fresh 45 min but she has been silent 400 min -> lurk cap"):
    with state_home():
        a = build_adapter()
        arrive(a, Message(41, ALICE, GENERAL, "hi"))
        a._travel_spoke("501")
        a._travel_sweep()  # persisted mid-visit, as production would have
        age(a._travel_lanes["501"], opened_m=400, obs_m=45, spoke_m=400)
        a._travel_sweep()
        beat = beat_for(a, GENERAL)
        check("closed via the cap despite a fresh room",
              beat and beat["close_reason"] == "lurk_cap", beat and beat.get("close_reason"))

with case("e: NULL last_spoke_at, room fresh, opened 400 min ago -> cap from opened_at"):
    with state_home():
        a = build_adapter()
        arrive(a, Message(42, ALICE, GENERAL, "hi"))
        a._travel_sweep()  # persisted mid-visit, as production would have
        age(a._travel_lanes["501"], opened_m=400, obs_m=0, spoke_m=None)
        a._travel_sweep()
        beat = beat_for(a, GENERAL)
        check("a never-speaking visit closes via the cap",
              beat and beat["close_reason"] == "lurk_cap", beat and beat.get("close_reason"))
        check("the cap was measured from opened_at",
              beat and abs(beat["closed_at"] - (beat["opened_at"] + 360 * MIN)) <= 5,
              beat and (beat["closed_at"] - (beat["opened_at"] + 360 * MIN)))


print("restart backfill")
with case("a lane already obs-idle at load closes as 'restart'"):
    with state_home() as home:
        stale = mk_lane(GENERAL, opened_m=200, obs_m=90, spoke_m=95, her=1)
        fresh = mk_lane(RANDOM, opened_m=40, obs_m=5, spoke_m=None)
        seed_travel_store(home, [], {"501": stale, "506": fresh})
        a = build_adapter()
        a._travel_load_state()
        check("gateway downtime counts as idle: the stale lane closed as restart",
              len(a._travel_beats) == 1 and str(a._travel_beats[0].get("channel_id")) == "501"
              and a._travel_beats[0].get("close_reason") == "restart", a._travel_beats)
        check("the fresh lane stayed open", set(a._travel_lanes) == {"506"}, list(a._travel_lanes))

with case("beat ids continue past the persisted maximum"):
    with state_home() as home:
        seed_travel_store(home, [mk_beat(3, GENERAL, 300, 290), mk_beat(7, RANDOM, 100, 90)], {})
        a = build_adapter()
        a._travel_load_state()
        arrive(a, Message(50, ALICE, OFFTOPIC, "back again"))
        a._travel_spoke("507")
        age(a._travel_lanes["507"], opened_m=200, obs_m=61, spoke_m=61)
        a._travel_sweep()
        beat = beat_for(a, OFFTOPIC)
        check("the next id continues the sequence", beat and beat["id"] == 8, beat and beat.get("id"))


print("horizon pruning")
with case("a 40h-old beat leaves the store on save and never projects"):
    with state_home() as home:
        old = mk_beat(1, RANDOM, opened_m=40 * 60 + 30, closed_m=40 * 60)
        recent = mk_beat(2, GENERAL, 200, 100)
        seed_travel_store(home, [old, recent], {})
        a = build_adapter()
        a._travel_load_state()
        rows = a._travel_rows(Message(60, ALICE, GENERAL, "hi"))
        check("older than the horizon: never projected",
              not any("#random" in ln for ln in rows), rows)
        check("the recent beat still projects", any("#general" in ln for ln in rows), rows)
        a._travel_sweep()
        with open(state_file(home, "ambient-travel-log")) as fh:
            stored = json.load(fh)
        check("pruned from the store on save",
              {str(b.get("channel_id")) for b in stored.get("beats", [])} == {"501"},
              stored.get("beats"))


print("projection")
PROJ_BEATS = [
    mk_beat(1, GENERAL, 580, 450, her=1, parts=[[101, "alice"]], snippets=["an older visit"]),
    mk_beat(2, GENERAL, 200, 100, her=2, parts=[[101, "alice"], [102, "bob"]],
            snippets=["we shipped the deploy", "staging looks fine now"]),
    mk_beat(3, GENERAL, 70, 10, her=1, parts=[[101, "alice"]],
            snippets=["one more thing before lunch"]),
    mk_beat(4, RANDOM, 400, 350, her=0, parts=[[102, "bob"]], snippets=[]),
]

with case("two lanes of history render as bounded, factual lines"):
    with state_home() as home:
        # beat 3 opens 30 min after beat 2 closed (gap < idle 60) -> merged;
        # beat 1 sits 250 min away -> its own line.
        seed_travel_store(home, PROJ_BEATS, {"501": mk_lane(GENERAL, 80, 2, 50, her=3)})
        a = build_adapter()
        a._travel_load_state()
        rows = a._travel_rows(Message(61, ALICE, GENERAL, "hi"))
        check("header opens the block and names its purpose",
              rows and rows[0].startswith("[ambient travel log"), rows[:1])
        check("header is single-line and fully strippable by the echo rule",
              rows and "\n" not in rows[0] and len(rows[0]) <= 400
              and ambient._AMBIENT_ECHO_RE.sub("", rows[0]).strip() == "", rows[:1])
        check("an open current lane gets the occupancy line, last",
              rows and "you have been in this room since" in rows[-1], rows[-1:])
        # A beat renders as one place line ("Guild #channel · …") with up to
        # two snippet lines riding after it, so count places, not raw rows.
        place = [ln for ln in rows if "#" in ln]
        general = [ln for ln in place if "#general" in ln]
        lurked = [ln for ln in place if "#random" in ln]
        check("closure f: adjacent same-lane beats (gap < idle) render merged",
              len(place) == 3 and len(general) == 2, place)
        check("newest first",
              place and place.index(general[0]) < place.index(lurked[0])
              < place.index(general[1]) if general and lurked else False, place)
        check("the merged line names place and company",
              general and "Community Server" in general[0]
              and "alice" in general[0] and "bob" in general[0], general[:1])
        check("the merged visit carries at most two snippets",
              general and 0 <= rows.index(lurked[0]) - rows.index(general[0]) - 1 <= 2,
              rows[rows.index(general[0]) + 1:rows.index(lurked[0])])
        check("the older visit renders its own line and snippet",
              len(general) == 2 and "an older visit" in "\n".join(rows), general[1:])
        check("every place line carries a time figure",
              place and all(TIME_FIGURE.search(ln) for ln in place), place)
        check("a lurk-only beat renders its place softly",
              len(lurked) == 1 and ("listening" in lurked[0].lower()
                                    or "hung around" in lurked[0].lower()), lurked)
        after_lurk = rows[rows.index(lurked[0]) + 1:]
        to_next_place = next((i for i, ln in enumerate(after_lurk) if "#" in ln),
                             len(after_lurk))
        check("a lurk-only beat carries no snippet lines",
              lurked and not [ln for ln in after_lurk[:to_next_place]
                              if "[" not in ln and "you have been" not in ln],
              after_lurk[:to_next_place])

with case("include_lurk_only false drops the listening visits"):
    with state_home() as home:
        seed_travel_store(home, PROJ_BEATS, {})
        a = build_adapter(travel={"enabled": True, "include_lurk_only": False})
        a._travel_load_state()
        rows = a._travel_rows(Message(62, ALICE, GENERAL, "hi"))
        check("lurk-only beat omitted", not any("#random" in ln for ln in rows), rows)
        check("spoken beats remain", any("#general" in ln for ln in rows), rows)

with case("budgets bound the projection"):
    with state_home() as home:
        filler = "x" * 90
        budget_beats = [
            mk_beat(1, GENERAL, 60, 30, her=2, snippets=[filler]),
            mk_beat(2, RANDOM, 70, 40, her=2, snippets=[filler]),
            mk_beat(3, SHRINE, 80, 50, her=2, snippets=[filler]),
            mk_beat(4, OFFTOPIC, 90, 60, her=2, snippets=[filler]),
        ]
        seed_travel_store(home, budget_beats, {})
        a = build_adapter(travel={"enabled": True, "max_events": 2})
        a._travel_load_state()
        rows = a._travel_rows(Message(63, ALICE, GENERAL, "hi"))
        check("max_events caps the rendered beats",
              len([ln for ln in rows if "#" in ln]) == 2, rows)
        a = build_adapter(travel={"enabled": True, "max_chars": 250})
        a._travel_load_state()
        rows = a._travel_rows(Message(64, ALICE, GENERAL, "hi"))
        check("max_chars caps the whole block",
              len("\n".join(rows)) <= 250 and len(rows) > 1,
              (len("\n".join(rows)), len(rows)))


print("request-only delivery through the middleware")
with case("the travel block rides as one request-only row at the END"):
    with state_home() as home:
        seed_travel_store(home, PROJ_BEATS, {})
        a = build_adapter()
        a._travel_load_state()
        m = Message(70, ALICE, GENERAL, "turn text")
        a._stage_room_context(m)
        a._stage_travel_log(m)
        check("staging never touches message.content", m.content == "turn text", m.content)
        block = ambient._travel_context.get("")
        check("the staged block is bracketed travel", block.startswith("[ambient travel log"), block[:40])
        request = {"model": "m", "messages": [{"role": "user", "content": "turn text"}]}
        out = ambient._on_llm_request_ambient_hint(request=request, platform="discord")
        check("middleware patched the request", out is not None and "request" in out, out)
        msgs = out["request"]["messages"]
        check("rows appended at the end only",
              len(msgs) == 3 and msgs[0] == {"role": "user", "content": "turn text"}, msgs)
        check("the travel row is the LAST row, one element, verbatim",
              msgs[-1] == {"role": "user", "content": block}, msgs[-1:])
        check("the original request dict is unmutated",
              request == {"model": "m", "messages": [{"role": "user", "content": "turn text"}]},
              request)
        check("the patched request is a copy, not the same object",
              out["request"] is not request and out["request"]["messages"] is not request["messages"])

with case("nothing staged means no patched request"):
    room_token = ambient._room_context.set("")
    travel_token = ambient._travel_context.set("")
    try:
        out = ambient._on_llm_request_ambient_hint(
            request={"messages": [{"role": "user", "content": "hi"}]}, platform="discord"
        )
    finally:
        ambient._room_context.reset(room_token)
        ambient._travel_context.reset(travel_token)
    check("no staged blocks means no patched request", out is None, out)


print("echo protection for the travel block")
with case("repeating the travel block back is caught by the room-echo guard"):
    with state_home() as home:
        seed_travel_store(home, PROJ_BEATS, {})
        a = build_adapter()
        a._travel_load_state()
        a._stage_travel_log(Message(71, ALICE, GENERAL, "hi"))
        lines = [
            ln for ln in ambient._travel_context.get("").splitlines()
            if not ln.startswith("[") and "you have been in this room since" not in ln
        ]
        check("two copied travel lines are a leak",
              len(lines) >= 2 and a._room_echo_leak("\n".join(lines[:2])), lines[:2])
        check("one quoted travel line is allowed", not a._room_echo_leak(lines[0] if lines else "x"))


print("room-talk snapshot reload")
with case("the snapshot round-trips entries with their true timestamps"):
    with state_home() as home:
        a = build_adapter(room={"persist_room_talk": True})
        for i in range(3):
            a._remember_room_talk(Message(80 + i, ALICE, GENERAL, f"remembered line {i}"))
        before = [list(e) for e in a._room_talk["501"]]
        a._travel_sweep()
        path = state_file(home, "ambient-room-talk")
        check("the sweep flushed the snapshot", os.path.exists(path))
        with open(path) as fh:
            snap = json.load(fh)
        check("snapshot shape is room_talk + touched",
              "room_talk" in snap and "touched" in snap, sorted(snap))
        b = build_adapter(room={"persist_room_talk": True})
        b._travel_load_state()
        after = [list(e) for e in b._room_talk.get("501", [])]
        check("entries survive with their original timestamps",
              after == before, (before, after))
        check("touched survives", set(b._room_talk_touched) == set(a._room_talk_touched),
              b._room_talk_touched)

with case("persist_room_talk false never writes the snapshot"):
    with state_home():
        a = build_adapter(room={"persist_room_talk": False})
        a._remember_room_talk(Message(84, ALICE, GENERAL, "not persisted"))
        a._travel_sweep()
        check("no room-talk file",
              not os.path.exists(state_file(os.environ["HERMES_HOME"], "ambient-room-talk")))
        check("the travel store still wrote",
              os.path.exists(state_file(os.environ["HERMES_HOME"], "ambient-travel-log")))

def room_entry(mid, ts):
    return [str(mid), "101", "alice", False, f"crafted line {mid}", None, ts]

with case("a reload keeps the per-channel cap and the 128-channel bound"):
    with state_home() as home:
        base_ts = time.time() - 200  # fixed once: asserts must not re-derive "now"
        fat = {str(1000 + c): [room_entry(5000 + c, base_ts + c)] for c in range(140)}
        # one channel carrying 30 entries, timestamps strictly increasing
        fat["1139"] = [room_entry(6000 + i, base_ts + 500 + i) for i in range(30)]
        seed_room_talk_store(home, fat, {k: base_ts for k in fat})
        b = build_adapter(room={"persist_room_talk": True})
        b._travel_load_state()
        b._travel_sweep()  # the flush must renormalise, not archive the bloat
        with open(state_file(home, "ambient-room-talk")) as fh:
            snap = json.load(fh)
        check("memory honours the 128-channel LRU", len(b._room_talk) <= 128, len(b._room_talk))
        check("the flushed file honours it too",
              len(snap.get("room_talk", {})) <= 128, len(snap.get("room_talk", {})))
        kept = b._room_talk.get("1139", [])
        check("the per-channel cap holds after reload", len(kept) <= 24, len(kept))
        check("the newest talk survives the trim",
              any(e[-1] == base_ts + 529 for e in kept)
              and not any(e[-1] == base_ts + 500 for e in kept), [e[-1] for e in kept])


print("hot path: no disk I/O on an inbound message")
with case("one dispatch touches no state file"):
    with state_home() as home:
        a = build_adapter(room={"persist_room_talk": True})
        arrive(a, Message(90, ALICE, GENERAL, "warm the lane"))
        a._travel_sweep()  # both files exist before the measured window
        travel_path = state_file(home, "ambient-travel-log")
        talk_path = state_file(home, "ambient-room-talk")
        pre = {p: os.stat(p).st_mtime_ns for p in (travel_path, talk_path)}
        pre_dir = sorted(os.listdir(os.path.join(home, "state")))

        seen = {}

        async def fake_dispatch(self, message):
            # The stock dispatch, stubbed: what matters is everything the
            # ambient wrapper did to the ContextVars before this point.
            seen["msg"] = message
            seen["travel"] = ambient._travel_context.get("")
            seen["room"] = ambient._room_context.get("")
            return True
        original = discord_adapter.DiscordAdapter._dispatch_discord_message
        discord_adapter.DiscordAdapter._dispatch_discord_message = fake_dispatch
        try:
            asyncio.run(a._dispatch_discord_message(Message(91, BOB, GENERAL, "the measured message")))
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = original
        post = {p: os.stat(p).st_mtime_ns for p in (travel_path, talk_path)}
        check("no state file was rewritten", pre == post, (pre, post))
        check("no temp files were left behind",
              sorted(os.listdir(os.path.join(home, "state"))) == pre_dir)
        check("the dispatch still fed the lane",
              a._travel_lanes.get("501", {}).get("obs_count") == 2, a._travel_lanes.get("501"))
        check("the dispatch staged the travel block in memory",
              seen.get("travel", "").startswith("[ambient travel log"), seen.get("travel", "")[:40])
        check("the dispatch staged the room block too",
              seen.get("room", "").startswith("[ambient room:"), seen.get("room", "")[:40])


print("config shape tolerance")
with case("numeric config accepts int and string forms identically"):
    for form in ({"enabled": True, "lurk_max_minutes": 360},
                 {"enabled": True, "lurk_max_minutes": "360"}):
        with state_home():
            a = build_adapter(travel=dict(form))
            arrive(a, Message(95, ALICE, GENERAL, "hi"))
            a._travel_sweep()  # persisted mid-visit, as production would have
            age(a._travel_lanes["501"], opened_m=480, obs_m=61, spoke_m=None)
            a._travel_sweep()
            beat = beat_for(a, GENERAL)
            check(f"lurk_max_minutes as {type(form['lurk_max_minutes']).__name__} closes at the cap",
                  beat is not None and beat["close_reason"] == "lurk_cap"
                  and abs(beat["closed_at"] - (beat["opened_at"] + 360 * MIN)) <= 5,
                  beat)

with case("channel ids coerce across the restart boundary"):
    with state_home() as home:
        # The snapshot's lane key is a JSON string; an arriving message carries
        # channel.id as an int. One lane must come out of that, not two.
        seed_travel_store(home, [], {"501": mk_lane(GENERAL, 80, 5, None)})
        a = build_adapter()
        a._travel_load_state()
        arrive(a, Message(96, ALICE, GENERAL, "back again"))
        check("the persisted lane was updated, not duplicated",
              len(a._travel_lanes) == 1 and a._travel_lanes["501"]["obs_count"] == 5,
              {k: v.get("obs_count") for k, v in a._travel_lanes.items()})


print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
