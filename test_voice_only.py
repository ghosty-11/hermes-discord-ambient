"""Regression checks for Discord voice-only reply correlation (S02 scope).

The TTS "speech happened" signal is bound to the verified turn that
generated it — keyed by (bot id, inbound message id) — so an interleaved
second seat can never have its text suppressed by another profile's
speech (plan V10). The claim remains consume-once within the turn and
expires with the window.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_voice_only.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
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
        print(f"  FAIL  {name}: {detail}")
        FAILURES.append(name)


def make_adapter(bot_id="999"):
    config = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "voice_only_replies": True,
                "suppress_fallback_notice": True,
            }
        }
    )
    adapter = ambient.AmbientDiscordAdapter(config)
    adapter._client = types.SimpleNamespace(
        user=types.SimpleNamespace(id=bot_id, name=f"bot-{bot_id}")
    )
    return adapter


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
    # one text-suppression claim intended for the model's transcript.
    ambient._profile_wants_voice_only = lambda: True
    bundle = {
        "message_id": "message-1",
        "speaker_id": "101",
        "gif_profile": "999",
    }
    scope_token = ambient._verified_run_context.set(bundle)
    ambient._note_tts_generated()
    ambient._verified_run_context.reset(scope_token)

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
        "voice transcript is suppressed after the notice",
        getattr(placeholder_result, "success", False) is True and not sent,
        repr(sent),
    )

    print("\n-- the TTS signal is owned by the turn that generated it --")
    ambient._tts_signals.clear()
    bundle_a = {"message_id": "msg-a", "speaker_id": "1", "gif_profile": "111"}
    scope_a = ambient._verified_run_context.set(bundle_a)
    ambient._note_tts_generated()
    ambient._verified_run_context.reset(scope_a)
    check(
        "the signal is keyed by seat and turn, not process-global",
        ("111", "msg-a") in ambient._tts_signals, dict(ambient._tts_signals),
    )

    seat_b = make_adapter(bot_id="222")
    suppressed_b = await seat_b.send("channel-9", "other seat text", reply_to="msg-a")
    check(
        "another seat's text is never suppressed by this seat's speech",
        ("channel-9", "other seat text") in sent,
        repr(sent),
    )
    check("the interleaved seat's own send succeeded",
          getattr(suppressed_b, "success", False) is True)

    seat_a = make_adapter(bot_id="111")
    before = len(sent)
    await seat_a.send("channel-8", "twin text", reply_to="msg-a")
    check("the owning seat's text twin is suppressed",
          len(sent) == before, repr(sent[before:]))

    await seat_a.send("channel-8", "unrelated later text", reply_to="msg-b")
    check(
        "the claim is consume-once: later text in the window still goes out",
        sent and sent[-1][1] == "unrelated later text", repr(sent[-2:]),
    )

    ambient._tts_signals.clear()
    scope_token = ambient._verified_run_context.set(
        {"message_id": "msg-late", "speaker_id": "1", "gif_profile": "111"}
    )
    ambient._note_tts_generated()
    ambient._verified_run_context.reset(scope_token)
    key = ("111", "msg-late")
    ambient._tts_signals[key] = ambient.time.monotonic() - (
        ambient._TTS_SIGNAL_WINDOW_S + 10
    )
    before = len(sent)
    await seat_a.send("channel-8", "after the window", reply_to="msg-late")
    check("an expired signal never suppresses", len(sent) == before + 1, repr(sent[before:]))

    ambient._tts_signals.clear()
    ambient._note_tts_generated()
    check(
        "a TTS call outside any verified turn arms nothing",
        not ambient._tts_signals, dict(ambient._tts_signals),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("ALL PASSED")


asyncio.run(main())
