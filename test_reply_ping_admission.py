"""A human replying to ANOTHER bot must not silence an ambient join.

Discord adds the replied-to author to ``message.mentions``. Stock admission
refuses any message that mentions a different bot without an inline mention of
this one (adapter.py: ``other_bots_mentioned and not raw_self_mention``), and
that refusal happens BEFORE the free-response-channel bypass the ambient
adapter already installs. So in an agent-heavy room, every human reply to
another bot is unanswerable — including one that says this profile's name.

The ambient re-dispatch has already decided to join and re-runs admission, so
the relaxation applies only while ``_ambient_open`` is set and only for a
human author. Dedup, own-message, message type, bot policy and the allowed-user
check stay authoritative.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_reply_ping_admission.py
"""

import importlib.util
import os
import sys
import tempfile
import types

sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="reply-ping-test-")
os.environ.setdefault("DISCORD_IGNORE_NO_MENTION", "true")

import discord  # noqa: E402
import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_reply_ping_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class Author:
    def __init__(self, uid, name, bot=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot


class Message:
    def __init__(self, content, author, mentions=(), message_id="1"):
        self.content = content
        self.author = author
        self.mentions = list(mentions)
        self.id = message_id
        self.type = discord.MessageType.reply
        self.channel = types.SimpleNamespace(id=303)
        self.guild = types.SimpleNamespace(id=404)


class Dedup:
    def is_duplicate(self, _mid):
        return False

    def contains(self, _mid):
        return False

    def discard(self, _mid):
        pass


def build_adapter():
    extra = {
        "ambient_presence": {
            "enabled": True,
            "channels": ["*"],
            "name_triggers": ["kestrel"],
            "probability": 0.15,
        }
    }
    adapter = discord_adapter.DiscordAdapter(types.SimpleNamespace(extra=extra))
    adapter.__class__ = ambient.AmbientDiscordAdapter
    adapter._dedup = Dedup()
    adapter._client = types.SimpleNamespace(user=Author(999, "Kestrel", bot=True))
    adapter._allowed_role_ids = set()
    adapter._is_allowed_user = lambda *a, **k: True
    adapter._get_parent_channel_id = lambda _c: None
    return adapter


HUMAN = Author(101, "alice")
OTHER_BOT = Author(202, "OtherBot", bot=True)

NAMED = "I know you're here, Kestrel is too. pet pet pet"

print("reply-ping admission")

adapter = build_adapter()
reply_to_bot = Message(NAMED, HUMAN, mentions=[OTHER_BOT])

admitted, _ = adapter._discord_message_admission(reply_to_bot, claim=False)
check(
    "stock pass still refuses a reply aimed at another bot",
    admitted is False,
    f"(admitted={admitted})",
)

token = ambient._ambient_open.set(True)
try:
    admitted, _ = adapter._discord_message_admission(reply_to_bot, claim=False)
finally:
    ambient._ambient_open.reset(token)
check(
    "ambient re-dispatch admits it so the name trigger can answer",
    admitted is True,
    f"(admitted={admitted})",
)

check(
    "mentions are restored after the masked admission",
    reply_to_bot.mentions == [OTHER_BOT],
    f"(mentions={reply_to_bot.mentions})",
)

# An inline @OtherBot is a deliberate address, not a reply artifact. Relaxing
# it would let this profile answer a question explicitly put to another bot.
inline = Message(
    "<@202> what do you think about Kestrel?", HUMAN,
    mentions=[OTHER_BOT], message_id="4",
)
token = ambient._ambient_open.set(True)
try:
    admitted, _ = adapter._discord_message_admission(inline, claim=False)
finally:
    ambient._ambient_open.reset(token)
check(
    "an explicit inline @mention of another bot stays refused",
    admitted is False,
    f"(admitted={admitted})",
)

# The relaxation must not become a bot-policy bypass: a bot-authored message
# reaching the ambient re-dispatch (catch-up replay) keeps stock admission.
adapter_bots_mentions_only = build_adapter()
adapter_bots_mentions_only._get_allow_bots = lambda: "mentions"
from_bot = Message("Kestrel, thoughts?", OTHER_BOT, mentions=[], message_id="2")
token = ambient._ambient_open.set(True)
try:
    admitted, _ = adapter_bots_mentions_only._discord_message_admission(
        from_bot, claim=False
    )
finally:
    ambient._ambient_open.reset(token)
check(
    "bot author still obeys allow_bots policy under ambient",
    admitted is False,
    f"(admitted={admitted})",
)

# An unauthorized human stays refused: this relaxes addressing, never auth.
adapter_denied = build_adapter()
adapter_denied._is_allowed_user = lambda *a, **k: False
adapter_denied._warn_if_fail_closed_default = lambda: None
stranger = Message(NAMED, Author(1, "stranger"), mentions=[OTHER_BOT], message_id="3")
token = ambient._ambient_open.set(True)
try:
    admitted, _ = adapter_denied._discord_message_admission(stranger, claim=False)
finally:
    ambient._ambient_open.reset(token)
check(
    "unauthorized author stays refused under ambient",
    admitted is False,
    f"(admitted={admitted})",
)

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
