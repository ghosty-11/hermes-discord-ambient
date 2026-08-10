"""Regression checks for Discord voice-only reply correlation.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_voice_only.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types


sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="voice-only-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402


# Avoid opening a live Discord client while retaining the real adapter class.
discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_voice_only_test", PLUGIN)
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
    config = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "voice_only_replies": True,
                "suppress_fallback_notice": True,
            }
        }
    )
    return ambient.AmbientDiscordAdapter(config)


async def main():
    sent = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content))
        return types.SimpleNamespace(success=True, message_id="sent-1")

    discord_adapter.DiscordAdapter.send = fake_send
    adapter = make_adapter()

    # This is the observed 2026-08-10 ordering: TTS succeeds, a fallback
    # status races through send(), the model emits its placeholder, then the
    # MEDIA attachment is delivered. The internal notice must not consume the
    # one text-suppression claim intended for the model's companion output.
    ambient._profile_wants_voice_only = lambda: True
    ambient._note_tts_generated()

    fallback = (
        "🔄 Switched to fallback model: old/free via openrouter → "
        "new/paid via openrouter"
    )
    fallback_result = await adapter.send("channel-1", fallback)
    placeholder_result = await adapter.send(
        "channel-1", "Empty response.", reply_to="message-1"
    )

    print("\n-- fallback status cannot steal the voice-only claim --")
    check(
        "fallback notice is suppressed",
        getattr(fallback_result, "success", False) is True,
    )
    check(
        "voice companion text is suppressed after the notice",
        getattr(placeholder_result, "success", False) is True and not sent,
        repr(sent),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
