"""Regression checks for direct Discord replies that must never disappear.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_direct_reply.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types


sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="direct-reply-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from gateway.platforms.base import MessageEvent  # noqa: E402
from gateway import response_filters  # noqa: E402


# Avoid opening a live Discord client while retaining the real adapter class.
discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_direct_reply_test", PLUGIN)
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
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.display_name = name


class Reference:
    def __init__(self, message_id, resolved):
        self.message_id = message_id
        self.resolved = resolved


class RepliedMessage:
    def __init__(self, author, content):
        self.author = author
        self.content = content


class RawMessage:
    def __init__(self, reference, message_id="200", author=None, mentions=None):
        self.reference = reference
        self.id = message_id
        self.content = "good girl"
        self.author = author
        self.mentions = mentions or []
        self.channel = types.SimpleNamespace(id="channel-1")


class HistoryChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self._messages = messages

    def history(self, *, limit):
        async def rows():
            for message in self._messages[:limit]:
                message.channel = self
                yield message

        return rows()


def make_adapter():
    config = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "silent_marker": "[SILENT]",
                "direct_silence_fallback": "I'm here.",
                "text_hygiene": {"resolve_plain_mentions": True},
                "reply_style": {
                    "enabled": True,
                    "standalone_marker": "[STANDALONE]",
                },
            }
        }
    )
    adapter = ambient.AmbientDiscordAdapter(config)
    adapter._client = types.SimpleNamespace(user=Author(999, "Muse"))
    return adapter


async def main():
    adapter = make_adapter()
    replied = RepliedMessage(adapter._client.user, "photo response")
    raw = RawMessage(Reference("199", replied))
    event = MessageEvent(
        text="good girl",
        raw_message=raw,
        message_id="200",
        reply_to_message_id="199",
        reply_to_text="photo response",
        source=types.SimpleNamespace(chat_type="group"),
    )

    print("\n-- reply ownership survives normalization --")
    adapter._annotate_reply_context(event)
    check("reply author id is preserved", event.reply_to_author_id == "999")
    check("reply author name is preserved", event.reply_to_author_name == "Muse")
    check("reply to this bot is marked as own", event.reply_to_is_own_message is True)
    check("reply to this bot is a direct turn", adapter._event_is_direct(event) is True)
    check(
        "own reply text is not relabeled as another person",
        event.reply_to_text == "photo response",
        repr(event.reply_to_text),
    )

    other_event = MessageEvent(
        text="good point",
        raw_message=RawMessage(
            Reference("198", RepliedMessage(Author(123, "Alice"), "earlier text")),
            message_id="201",
        ),
        message_id="201",
        reply_to_message_id="198",
        reply_to_text="earlier text",
        source=types.SimpleNamespace(chat_type="group"),
    )
    adapter._annotate_reply_context(other_event)
    check(
        "replying to another person is not marked as own",
        other_event.reply_to_is_own_message is False,
    )
    check(
        "replying to another person is not promoted to a direct turn",
        adapter._event_is_direct(other_event) is False,
    )
    check(
        "reply context names the other person",
        other_event.reply_to_text == "Alice wrote: earlier text",
        repr(other_event.reply_to_text),
    )

    print("\n-- direct scope reaches request middleware and response filter --")
    seen = {}

    async def handler(scoped_event):
        request = {"messages": [{"role": "user", "content": scoped_event.text}]}
        patched = ambient._on_llm_request_ambient_hint(request=request)
        seen["request"] = patched
        seen["suppressed"] = response_filters.is_intentional_silence_agent_result(
            {"failed": False}, "[SILENT]"
        )
        return None

    ambient._install_direct_silence_filter()
    adapter.set_message_handler(handler)
    await adapter._message_handler(event)
    messages = ((seen.get("request") or {}).get("request") or {}).get("messages", [])
    check(
        "model receives a request-only direct-address directive",
        bool(messages) and "direct" in messages[-1].get("content", "").lower(),
        repr(messages),
    )
    check(
        "the directive is not a non-leading system row (Hetzner system-first 400)",
        bool(messages) and messages[-1].get("role") == "user"
        and not any(m.get("role") == "system" for m in messages[1:]),
        repr([m.get("role") for m in messages]),
    )
    check(
        "gateway does not swallow a direct silence token",
        seen.get("suppressed") is False,
        repr(seen.get("suppressed")),
    )
    check(
        "ambient silence remains suppressible outside direct scope",
        response_filters.is_intentional_silence_agent_result(
            {"failed": False}, "[SILENT]"
        ) is True,
    )

    print("\n-- reply placement guidance is request-only --")
    original_cfg = ambient._ambient_cfg
    ambient._ambient_cfg = lambda key, default=None: (
        {
            "enabled": True,
            "standalone_marker": "[STANDALONE]",
        }
        if key == "reply_style"
        else default
    )
    try:
        request = {"messages": [{"role": "user", "content": "hello"}]}
        patched = ambient._on_llm_request_ambient_hint(request=request)
        messages = ((patched or {}).get("request") or {}).get("messages", [])
        check(
            "the model receives standalone-placement guidance",
            bool(messages)
            and "[STANDALONE]" in messages[-1].get("content", "")
            and "room-wide" in messages[-1].get("content", "").lower(),
            repr(messages),
        )
        check(
            "reply-placement guidance does not mutate the persisted request",
            len(request["messages"]) == 1,
            repr(request["messages"]),
        )
    finally:
        ambient._ambient_cfg = original_cfg

    print("\n-- outbound direct silence becomes an acknowledgment --")
    sent = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content, kwargs.get("reply_to")))
        return types.SimpleNamespace(success=True, message_id="sent-1")

    discord_adapter.DiscordAdapter.send = fake_send
    adapter._direct_note_event(event)
    result = await adapter.send("channel-1", "[SILENT]", reply_to="200")
    check("one Discord message is sent", len(sent) == 1, repr(sent))
    check(
        "the control token is replaced by configured fallback",
        bool(sent) and sent[0][1] == "I'm here.",
        repr(sent),
    )
    check("send reports success", getattr(result, "success", False) is True)
    await adapter.send("channel-1", "[SILENT]", reply_to="200")
    check(
        "the completed direct marker is consumed exactly once",
        len(sent) == 1,
        repr(sent),
    )

    print("\n-- Discord replies are default; room-wide remarks may stand alone --")
    await adapter.send("channel-1", "A direct answer.", reply_to="placement-1")
    check(
        "ordinary output keeps its Discord reply reference",
        sent[-1] == ("channel-1", "A direct answer.", "placement-1"),
        repr(sent[-1]),
    )

    accounted = []
    original_bounce_count = adapter._bounce_count_sent
    original_catchup_count = adapter._catchup_count_sent
    original_direct_count = adapter._direct_count_sent
    adapter._bounce_count_sent = lambda anchor, result: accounted.append(("bounce", anchor))
    adapter._catchup_count_sent = lambda anchor, result: accounted.append(("catchup", anchor))
    adapter._direct_count_sent = lambda anchor, result: accounted.append(("direct", anchor))
    try:
        await adapter.send(
            "channel-1",
            "[STANDALONE] The whole room needed to hear that.",
            reply_to="placement-2",
        )
    finally:
        adapter._bounce_count_sent = original_bounce_count
        adapter._catchup_count_sent = original_catchup_count
        adapter._direct_count_sent = original_direct_count
    check(
        "the standalone marker is stripped and the Discord reference is omitted",
        sent[-1]
        == ("channel-1", "The whole room needed to hear that.", None),
        repr(sent[-1]),
    )
    check(
        "standalone delivery still accounts against the original inbound anchor",
        accounted
        == [
            ("bounce", "placement-2"),
            ("catchup", "placement-2"),
            ("direct", "placement-2"),
        ],
        repr(accounted),
    )

    adapter._direct_pending["placement-3"] = ambient.time.time()
    await adapter.send("channel-1", "[STANDALONE]", reply_to="placement-3")
    check(
        "a marker-only direct turn falls back to an anchored acknowledgment",
        sent[-1] == ("channel-1", "I'm here.", "placement-3"),
        repr(sent[-1]),
    )
    before = len(sent)
    await adapter.send("channel-1", "[STANDALONE]", reply_to="placement-4")
    check(
        "a marker-only ambient turn stays silent",
        len(sent) == before,
        repr(sent[before:]),
    )

    print("\n-- known Discord handles become real mentions --")
    speaker = Author(123, "alice.account")
    speaker.display_name = "Alice"
    inbound = RawMessage(None, author=speaker)
    adapter._remember_discord_identities(inbound)
    await adapter.send("channel-1", "Thanks, @Alice!", reply_to="202")
    check(
        "a known display name is rendered as a Discord user mention",
        sent[-1][1] == "Thanks, <@123>!",
        repr(sent[-1]),
    )
    await adapter.send("channel-1", "Hello, @unknown!", reply_to="203")
    check(
        "an unknown handle is left as ordinary text",
        sent[-1][1] == "Hello, @unknown!",
        repr(sent[-1]),
    )
    older = Author(321, "garden.account")
    older.display_name = "GardenMuse"
    history_channel = HistoryChannel(
        "channel-1", [RawMessage(None, author=older)],
    )
    adapter._client = types.SimpleNamespace(
        user=Author(999, "Muse"),
        get_channel=lambda channel_id: history_channel,
    )
    await adapter.send(
        "channel-1", "The room saved a story for @GardenMuse!", reply_to="203b",
    )
    check(
        "an ambient name from recent channel history becomes a real mention",
        sent[-1][1] == "The room saved a story for <@321>!",
        repr(sent[-1]),
    )
    collision = Author(456, "second.account")
    collision.display_name = "Alice"
    adapter._remember_discord_identities(RawMessage(None, author=collision))
    await adapter.send("channel-1", "Thanks, @Alice!", reply_to="204")
    check(
        "a reused display name is left ambiguous instead of guessed",
        sent[-1][1] == "Thanks, @Alice!",
        repr(sent[-1]),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
