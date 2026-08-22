"""Being talked ABOUT is not being talked TO.

`_event_is_direct` treated any occurrence of a name trigger in the body as a
personal address, so "what do you think about <name>?" — asked of a DIFFERENT
bot, using the reply affordance — was handed the direct-address directive
("this person is speaking to you personally. Answer them normally"). The
profile then answered as though the question had been put to it, and referred
to itself in the third person in the same breath.

A name in the body is an address only when the message is not aimed at someone
else. A reply to another person, or an inline @mention of one, makes this
profile a subject rather than an addressee. It may still choose to chime in —
that is the ambient path — but not as the person being spoken to.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_overheard_name.py
"""

import importlib.util
import os
import sys
import tempfile
import types

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="overheard-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_overheard_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


SELF_ID = 909
OTHER_BOT_ID = 202
HUMAN_ID = 101


class Author:
    def __init__(self, uid, name, bot=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot


SELF = Author(SELF_ID, "Kestrel", bot=True)
OTHER_BOT = Author(OTHER_BOT_ID, "OtherBot", bot=True)
HUMAN = Author(HUMAN_ID, "alice")


class Reference:
    def __init__(self, resolved):
        self.message_id = "50"
        self.resolved = resolved


class Replied:
    def __init__(self, author, content="prior"):
        self.author = author
        self.content = content


class Raw:
    def __init__(self, content, mentions=(), reference=None):
        self.content = content
        self.mentions = list(mentions)
        self.reference = reference
        self.id = "1"


class Event:
    def __init__(self, raw, chat_type="group"):
        self.raw_message = raw
        self.text = raw.content
        self.message_id = "1"
        self.source = types.SimpleNamespace(chat_type=chat_type)
        self.reply_to_text = "prior"


def build_adapter():
    extra = {
        "ambient_presence": {
            "enabled": True,
            "channels": ["*"],
            "name_triggers": ["kestrel", "kes"],
        }
    }
    adapter = discord_adapter.DiscordAdapter(types.SimpleNamespace(extra=extra))
    adapter.__class__ = ambient.AmbientDiscordAdapter
    adapter._client = types.SimpleNamespace(user=SELF)
    adapter._direct_pending = {}
    return adapter


def is_direct(raw, chat_type="group"):
    return build_adapter()._direct_note_event(Event(raw, chat_type))


print("overheard name vs direct address")

# The reported case: asked of another bot, via the reply affordance.
check(
    "a question about her, asked of another bot, is NOT direct",
    is_direct(
        Raw(
            "what do you think about Kestrel?",
            mentions=[OTHER_BOT],
            reference=Reference(Replied(OTHER_BOT)),
        )
    )
    is False,
)

check(
    "an inline @mention of another bot is NOT direct",
    is_direct(Raw(f"<@{OTHER_BOT_ID}> is Kestrel any good?", mentions=[OTHER_BOT]))
    is False,
)

check(
    "naming her with nobody else addressed IS direct",
    is_direct(Raw("Kestrel, what do you make of this?")) is True,
)

check(
    "a reply to her own message IS direct",
    is_direct(
        Raw("and what about now?", mentions=[SELF], reference=Reference(Replied(SELF)))
    )
    is True,
)

check(
    "an inline @mention of her IS direct",
    is_direct(Raw(f"<@{SELF_ID}> thoughts?", mentions=[SELF])) is True,
)

check(
    "replying to another bot while addressing her inline IS direct",
    is_direct(
        Raw(
            f"<@{SELF_ID}> do you agree?",
            mentions=[OTHER_BOT, SELF],
            reference=Reference(Replied(OTHER_BOT)),
        )
    )
    is True,
)

# The trigger match must respect word boundaries, as _join_reason already does.
check(
    "a trigger buried inside a longer word is not an address",
    is_direct(Raw("that was a keste of time honestly")) is False,
)

check(
    "a reply to a human talking about her is NOT direct",
    is_direct(
        Raw("Kestrel would love that", reference=Reference(Replied(Author(303, "bob"))))
    )
    is False,
)

# The hint the model receives must match the situation.
adapter = build_adapter()
overheard = Raw(
    "what do you think about Kestrel?",
    mentions=[OTHER_BOT],
    reference=Reference(Replied(OTHER_BOT)),
)
hint = adapter._ambient_hint_for(overheard, marker="[SILENT]")
check(
    "an overheard mention gets the overheard hint, not the bystander default",
    "talking about you" in hint.lower(),
    f"(hint={hint[:90]!r})",
)
check(
    "the overheard hint forbids answering as the addressee",
    "not addressed" in hint.lower() or "were not addressed" in hint.lower(),
    f"(hint={hint[:90]!r})",
)

plain = Raw("anyone seen the new release?")
check(
    "an ordinary ambient join keeps the default hint",
    "nobody addressed you" in adapter._ambient_hint_for(plain, marker="[SILENT]"),
)

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
