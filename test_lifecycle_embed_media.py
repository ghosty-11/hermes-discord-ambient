"""Regression checks for gateway lifecycle messages and Discord embed video ingress.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_lifecycle_embed_media.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from unittest.mock import patch


sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-lifecycle-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402


discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_lifecycle_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def make_adapter():
    cfg = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "gateway_lifecycle": {
                    "enabled": True,
                    "shrine_channel": "shrine",
                    "departure_messages": ["curling up beside the shrine flame"],
                    "return_messages": ["the shrine flame is awake again"],
                    "public_return": {
                        "channel": "general",
                        "probability": 0.5,
                        "messages": ["a familiar tail slips back into the room"],
                    },
                },
                "embed_media": {
                    "enabled": True,
                    "auto_transcribe": True,
                    "max_videos_per_message": 1,
                    "transcript_max_chars": 4000,
                },
            }
        }
    )
    adapter = ambient.AmbientDiscordAdapter(cfg)
    adapter.gateway_runner = types.SimpleNamespace(_draining=False)
    return adapter


async def check_lifecycle():
    print("\n-- gateway lifecycle messages --")
    sent = []
    base_disconnects = []

    async def base_connect(self, *, is_reconnect=False):
        return True

    async def base_disconnect(self):
        base_disconnects.append(True)

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content))
        return types.SimpleNamespace(success=True, message_id=f"sent-{len(sent)}")

    with (
        patch.object(discord_adapter.DiscordAdapter, "connect", base_connect),
        patch.object(discord_adapter.DiscordAdapter, "disconnect", base_disconnect),
        patch.object(ambient.AmbientDiscordAdapter, "send", fake_send),
        patch.object(ambient.random, "random", return_value=0.49),
    ):
        adapter = make_adapter()
        await adapter.connect(is_reconnect=False)
        check(
            "initial connect always greets the shrine",
            ("shrine", "the shrine flame is awake again") in sent,
            repr(sent),
        )
        check(
            "a roll below 0.5 also greets public general",
            ("general", "a familiar tail slips back into the room") in sent,
            repr(sent),
        )
        before = list(sent)
        await adapter.connect(is_reconnect=True)
        check("transient reconnect emits no lifecycle greeting", sent == before, repr(sent))

        adapter.gateway_runner._draining = False
        await adapter.disconnect()
        check("non-gateway disconnect emits no departure", sent == before, repr(sent))
        check("base disconnect still runs", len(base_disconnects) == 1)

    sent.clear()
    base_disconnects.clear()
    with (
        patch.object(discord_adapter.DiscordAdapter, "connect", base_connect),
        patch.object(discord_adapter.DiscordAdapter, "disconnect", base_disconnect),
        patch.object(ambient.AmbientDiscordAdapter, "send", fake_send),
        patch.object(ambient.random, "random", return_value=0.50),
    ):
        adapter = make_adapter()
        await adapter.connect(is_reconnect=False)
        check(
            "the 50 percent boundary does not post publicly",
            all(chat_id != "general" for chat_id, _ in sent),
            repr(sent),
        )
        adapter.gateway_runner._draining = True
        await adapter.disconnect()
        check(
            "graceful gateway drain announces departure in the shrine",
            ("shrine", "curling up beside the shrine flame") in sent,
            repr(sent),
        )
        before = list(sent)
        await adapter.disconnect()
        check("departure is emitted at most once", sent == before, repr(sent))
        check("base disconnect runs on every teardown call", len(base_disconnects) == 2)


async def check_embed_media():
    print("\n-- Discord rich-embed video ingress --")
    adapter = make_adapter()
    good_video = types.SimpleNamespace(
        proxy_url="https://images-ext-1.discordapp.net/external/redacted/https/api.fxtwitter.com/2/go",
        url="https://api.fxtwitter.com/2/go",
        content_type="video/mp4",
        width=960,
        height=720,
    )
    untrusted_video = types.SimpleNamespace(
        proxy_url="https://example.invalid/not-discord.mp4",
        url="https://example.invalid/not-discord.mp4",
        content_type="video/mp4",
    )
    resolved = types.SimpleNamespace(
        attachments=[],
        embeds=[types.SimpleNamespace(video=good_video)],
    )
    message = types.SimpleNamespace(
        id=123,
        attachments=[],
        embeds=[types.SimpleNamespace(video=untrusted_video)],
        reference=types.SimpleNamespace(resolved=resolved),
    )

    additions = adapter._inject_discord_embed_videos(message)
    check("untrusted non-Discord proxy is ignored", not message.attachments, repr(message.attachments))
    check("referenced FixupX-style video becomes one attachment", len(resolved.attachments) == 1)
    if resolved.attachments:
        att = resolved.attachments[0]
        check("synthetic attachment keeps video MIME", att.content_type == "video/mp4")
        check("synthetic attachment uses only Discord proxy", "discordapp.net" in att.url)
        check("synthetic attachment has an MP4 filename", att.filename.endswith(".mp4"))
    adapter._remove_discord_embed_videos(additions)
    check("temporary synthetic attachment is removed after dispatch", not resolved.attachments)

    event = types.SimpleNamespace(
        text="what happens in this clip?",
        raw_message=message,
        media_urls=["/tmp/embedded_video.mp4"],
        media_types=["video/mp4"],
    )
    message._ambient_embedded_video = True
    fake_transcription = types.SimpleNamespace(
        transcribe_audio=lambda path: {
            "success": True,
            "transcript": "the speaker says the agent escaped the sandbox",
        }
    )
    with patch.dict(sys.modules, {"tools.transcription_tools": fake_transcription}):
        await adapter._transcribe_embedded_video_event(event)
    check(
        "local transcript is injected into the same user turn",
        "transcript from embedded video" in event.text.lower()
        and "escaped the sandbox" in event.text,
        repr(event.text),
    )


async def main():
    await check_lifecycle()
    await check_embed_media()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
