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
    def __init__(self, reference, message_id="200"):
        self.reference = reference
        self.id = message_id
        self.content = "good girl"
        self.mentions = []


def make_adapter():
    config = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "silent_marker": "[SILENT]",
                "direct_silence_fallback": "I'm here.",
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

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
