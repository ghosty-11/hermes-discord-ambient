"""Group-addressed messages must be recognized without a greeting word.

A room full of agents is addressed collectively far more often than it is
greeted: "where are all the agents?", "agents, thoughts?", "anyone here?".
Requiring a greeting made the feature inert — measured against 185 human
messages from the live channel it matched none of them.

The negatives matter as much as the positives: a bare collective noun inside
ordinary conversation must stay untriggered, or a public persona starts
answering every mention of the word "agents".

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_group_address.py
"""

import importlib.util
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="group-address-test-")

import discord  # noqa: E402
import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_group_address_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def matches(text):
    lowered = text.lower()
    return any(re.search(p, lowered) for p in ambient._GROUP_ADDRESS_PATTERNS)


ADDRESSED = [
    "we need an agent revival. where are all the agents? \U0001f97a more people need to participate",
    "good morning agents",
    "agents, thoughts?",
    "any agents around?",
    "anyone here?",
    "hey everyone",
    "agents assemble",
    "y'all awake?",
    "ok bots: who wants to go first",
]

NOT_ADDRESSED = [
    "the agents keep breaking when I redeploy",
    "I wrote a harness for coding agents last night",
    "that bot is faster than mine",
    "*pet pet pet*",
    "reset later today",
    "Only for those that don't learn their lesson",
    "Tell us some old story",
    "agent frameworks are all converging on the same shape",
]

print("group address patterns")
for text in ADDRESSED:
    check(f"addressed: {text[:52]}", matches(text))
for text in NOT_ADDRESSED:
    check(f"not addressed: {text[:52]}", not matches(text))


# Integration: the pattern must actually drive _join_reason, not just exist.
class Author:
    def __init__(self, uid, name, bot=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot


class Message:
    def __init__(self, content, author):
        self.content = content
        self.author = author
        self.mentions = []
        self.id = "1"
        self.type = discord.MessageType.default
        self.channel = types.SimpleNamespace(id=303)


def build_adapter(group_probability):
    extra = {
        "ambient_presence": {
            "enabled": True,
            "channels": ["*"],
            "name_triggers": ["kestrel"],
            "probability": 0.0,          # the dice must not mask the result
            "cooldown_seconds": 1800,
            "group_address": {
                "enabled": True,
                "probability": group_probability,
                "cooldown_seconds": 0,
            },
        }
    }
    adapter = discord_adapter.DiscordAdapter(types.SimpleNamespace(extra=extra))
    adapter.__class__ = ambient.AmbientDiscordAdapter
    adapter._client = types.SimpleNamespace(user=Author(999, "Kestrel", bot=True))
    adapter._ambient_last = 0.0
    adapter._last_seen = {}
    adapter._seen_loaded = True
    return adapter


HUMAN = Author(101, "alice")
OPENER = ADDRESSED[0]

certain = build_adapter(1.0)
check(
    "a group-addressed opener joins when the roll is certain",
    certain._join_reason(Message(OPENER, HUMAN)) == "named",
)

never = build_adapter(0.0)
check(
    "the group-address roll can still decline",
    never._join_reason(Message(OPENER, HUMAN)) is None,
)

check(
    "ordinary talk about agents does not take the group path",
    build_adapter(1.0)._join_reason(
        Message("the agents keep breaking when I redeploy", HUMAN)
    ) is None,
)

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
