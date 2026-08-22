"""Exercise a profile no-thread policy through upstream's real Discord handler.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_no_threads.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-no-thread-test-")

from gateway.config import PlatformConfig  # noqa: E402
import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402


PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_no_thread_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class FakeTextChannel:
    def __init__(self, channel_id=800, name="general"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name="Agent Anarchy")
        self.topic = None


class FakeThread:
    def __init__(self, channel_id=900, parent=None, name="thread"):
        self.id = channel_id
        self.name = name
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.guild = getattr(parent, "guild", None)
        self.topic = None


def make_message(channel, message_id):
    return SimpleNamespace(
        id=message_id,
        content="hello there",
        mentions=[],
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=SimpleNamespace(id=42, display_name="TestUser", name="test.user"),
    )


def make_adapter(no_threads):
    config = PlatformConfig(enabled=True, token="fake-token")
    config.extra = {
        "ambient_presence": {
            "enabled": True,
            "no_threads": no_threads,
        }
    }
    adapter = ambient.AmbientDiscordAdapter(config)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    adapter._standby_wait = AsyncMock(return_value=False)
    return adapter


async def exercise():
    print("\n-- profile no_threads overrides process-wide auto-threading --")
    channel = FakeTextChannel()

    with (
        patch.object(discord_adapter.discord, "DMChannel", type("FakeDM", (), {})),
        patch.object(discord_adapter.discord, "Thread", FakeThread),
        patch.dict(
            os.environ,
            {
                "DISCORD_AUTO_THREAD": "true",
                "DISCORD_REQUIRE_MENTION": "false",
                "DISCORD_NO_THREAD_CHANNELS": "",
                "DISCORD_FREE_RESPONSE_CHANNELS": "",
                "DISCORD_IGNORED_CHANNELS": "",
            },
        ),
    ):
        control = make_adapter(no_threads=False)
        control._auto_create_thread = AsyncMock(
            return_value=FakeThread(channel_id=901, parent=channel)
        )
        control_message = make_message(channel, 1001)
        control_token = control._ambient_no_thread_token(control_message)
        try:
            await control._handle_message(control_message)
        finally:
            if control_token is not None:
                ambient._no_thread_keys.reset(control_token)
        check(
            "the negative control reaches upstream auto-thread creation",
            control._auto_create_thread.await_count == 1,
            repr(control._auto_create_thread.await_count),
        )

        protected = make_adapter(no_threads=True)
        protected._auto_create_thread = AsyncMock(
            return_value=FakeThread(channel_id=902, parent=channel)
        )
        protected_message = make_message(channel, 1002)
        token = protected._ambient_no_thread_token(protected_message)
        try:
            await protected._handle_message(protected_message)
        finally:
            if token is not None:
                ambient._no_thread_keys.reset(token)
        check(
            "the profile policy prevents upstream thread creation",
            protected._auto_create_thread.await_count == 0,
            repr(protected._auto_create_thread.await_count),
        )
        check(
            "the protected message still reaches inline agent dispatch",
            protected.handle_message.await_count == 1,
            repr(protected.handle_message.await_count),
        )
        if protected.handle_message.await_args is not None:
            event = protected.handle_message.await_args.args[0]
            check(
                "inline dispatch remains anchored to the parent channel",
                event.source.chat_id == str(channel.id)
                and event.source.parent_chat_id is None,
                repr(event.source),
            )


asyncio.run(exercise())
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    raise SystemExit(1)
print("all checks passed")
