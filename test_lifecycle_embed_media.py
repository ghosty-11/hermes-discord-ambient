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


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
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
                "probability": 0.15,
                "no_threads": True,
                "catch_up": {"enabled": False},
                "reply_style": {
                    "enabled": True,
                    "standalone_marker": "[STANDALONE]",
                },
                "gateway_lifecycle": {
                    "enabled": True,
                    "shrine_channel": "shrine",
                    "shrine_probability": 0.4,
                    "daily_max": 0,  # this suite tests send mechanics, not budget
                    "inference": {
                        "enabled": True,
                        "task": "title_generation",
                        "timeout_seconds": 20,
                        "persona_prompt": "A warm community regular with playful energy.",
                    },
                    "public_return": {
                        "channel": "general",
                        "probability": 0.25,
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
    activation_logs = []

    async def base_connect(self, *, is_reconnect=False):
        return True

    async def base_disconnect(self):
        base_disconnects.append(True)

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content))
        return types.SimpleNamespace(success=True, message_id=f"sent-{len(sent)}")

    generated = {
        "shrine_return": "generated shrine return",
        "public_return": "generated public return",
        "shrine_departure": "generated shrine departure",
    }

    def fake_generate(self, events):
        return {event: generated[event] for event in events}

    async def finish_generation(adapter):
        task = getattr(adapter, "_lifecycle_generation_task", None)
        if task is not None:
            await task

    def capture_info(message, *args):
        activation_logs.append(message % args if args else str(message))

    with (
        patch.object(discord_adapter.DiscordAdapter, "connect", base_connect),
        patch.object(discord_adapter.DiscordAdapter, "disconnect", base_disconnect),
        patch.object(ambient.AmbientDiscordAdapter, "send", fake_send),
        patch.object(
            ambient.AmbientDiscordAdapter,
            "_generate_lifecycle_copy",
            fake_generate,
            create=True,
        ),
        patch.object(ambient.random, "random", side_effect=[0.39, 0.24, 0.39]),
        patch.object(ambient.logger, "info", capture_info),
    ):
        adapter = make_adapter()
        await adapter.connect(is_reconnect=False)
        check(
            "connect reports the active ambient routing policy",
            any(
                "ambient: active" in line
                and "probability=0.15" in line
                and "no_threads=True" in line
                and "reply_style=True" in line
                for line in activation_logs
            ),
            repr(activation_logs),
        )
        await finish_generation(adapter)
        check(
            "a roll below 0.4 sends a model-generated shrine return",
            ("shrine", "generated shrine return") in sent,
            repr(sent),
        )
        check(
            "a roll below 0.25 sends a model-generated public return",
            ("general", "generated public return") in sent,
            repr(sent),
        )
        before = list(sent)
        await adapter.connect(is_reconnect=True)
        check("transient reconnect emits no lifecycle greeting", sent == before, repr(sent))

        adapter.gateway_runner._draining = False
        await adapter.disconnect()
        check("non-gateway disconnect emits no departure", sent == before, repr(sent))

        adapter.gateway_runner._draining = True
        await adapter.disconnect()
        check(
            "a roll below 0.4 sends the cached model-generated shrine departure",
            ("shrine", "generated shrine departure") in sent,
            repr(sent),
        )
        check("base disconnect still runs", len(base_disconnects) == 2)

    sent.clear()
    base_disconnects.clear()
    with (
        patch.object(discord_adapter.DiscordAdapter, "connect", base_connect),
        patch.object(discord_adapter.DiscordAdapter, "disconnect", base_disconnect),
        patch.object(ambient.AmbientDiscordAdapter, "send", fake_send),
        patch.object(
            ambient.AmbientDiscordAdapter,
            "_generate_lifecycle_copy",
            fake_generate,
            create=True,
        ),
        patch.object(ambient.random, "random", side_effect=[0.40, 0.25, 0.40]),
    ):
        adapter = make_adapter()
        await adapter.connect(is_reconnect=False)
        await finish_generation(adapter)
        check(
            "the exact 40 and 25 percent boundaries stay silent",
            sent == [],
            repr(sent),
        )
        adapter.gateway_runner._draining = True
        await adapter.disconnect()
        check(
            "a losing shrine departure roll stays silent",
            sent == [],
            repr(sent),
        )
        before = list(sent)
        await adapter.disconnect()
        check("departure is emitted at most once", sent == before, repr(sent))
        check("base disconnect runs on every teardown call", len(base_disconnects) == 2)


def check_lifecycle_generation():
    print("\n-- gateway lifecycle inference --")
    adapter = make_adapter()
    generator = getattr(adapter, "_generate_lifecycle_copy", None)
    check(
        "lifecycle copy has a model-inference path",
        callable(generator),
        repr(generator),
    )
    if not callable(generator):
        return

    calls = []

    def run_oneshot(**kwargs):
        calls.append(kwargs)
        return (
            '{"shrine_return":"A new shrine line",'
            '"public_return":"A new public line"}'
        )

    fake_oneshot = types.SimpleNamespace(run_oneshot=run_oneshot)
    with patch.dict(sys.modules, {"agent.oneshot": fake_oneshot}):
        copy = generator(["shrine_return", "public_return"])
    check("one model call generates all selected events", len(calls) == 1, repr(calls))
    check(
        "the configured auxiliary task is used",
        calls and calls[0].get("task") == "title_generation",
        repr(calls),
    )
    check(
        "private persona context reaches the model prompt",
        calls
        and "A warm community regular with playful energy."
        in calls[0].get("instructions", ""),
        repr(calls),
    )
    check(
        "generated JSON becomes event-specific copy",
        copy
        == {
            "shrine_return": "A new shrine line",
            "public_return": "A new public line",
        },
        repr(copy),
    )


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
    check_lifecycle_generation()
    await check_embed_media()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
