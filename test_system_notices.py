"""Regression checks for operator notices on a public Discord profile.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_system_notices.py
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="system-notices-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402


discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_system_notices_test", PLUGIN)
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
                "suppress_fallback_notice": True,
                "standby": {"enabled": True, "only_when_local": True},
                "system_notices": {"reroute_channel": "private-room"},
            }
        }
    )
    return ambient.AmbientDiscordAdapter(config)


async def main():
    sent = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((str(chat_id), content))
        return types.SimpleNamespace(success=True, message_id="sent-1")

    discord_adapter.DiscordAdapter.send = fake_send
    adapter = make_adapter()

    notices = [
        "⚠ Context compression timed out after 120.0s with no output.",
        "⚠ Context is over the compression threshold (~65,158 tokens >= 24,000).",
        "⚠️ Provider unreachable - switching to fallback provider...",
        "⚠️ Rate limited - switching to fallback provider...",
        (
            "⚠️ Model fallback: Qwen/Qwen3.6-35B-A3B-FP8 via custom unavailable "
            "(request timeout); using poolside/laguna-s-2.1:free via nous."
        ),
        "⚠️ The model provider failed after retries. Check gateway logs for diagnostics.",
        "Context length exceeded (154,261 tokens). Cannot compress further.",
        "🔄 Session auto-reset - the conversation exceeded the maximum context size.",
        "⚠ Compression aborted: Request timed out. No messages were dropped.",
    ]

    print("\n-- operator diagnostics leave the public room --")
    for notice in notices:
        before = len(sent)
        await adapter.send("public-room", notice)
        delivered = sent[before:]
        check(
            notice.split(" ", 2)[0] + " is private",
            delivered == [("private-room", notice)],
            repr(delivered),
        )

    before = len(sent)
    progress = "Let me think about our conversation for a minute..."
    await adapter.send("public-room", progress)
    check(
        "compression progress is logged but not posted",
        sent[before:] == [],
        repr(sent[before:]),
    )

    ordinary = "Morning. I was napping."
    before = len(sent)
    await adapter.send("public-room", ordinary, reply_to="message-1")
    check(
        "ordinary replies still use the public room",
        sent[before:] == [("public-room", ordinary)],
        repr(sent[before:]),
    )

    cloud = make_adapter()
    cloud_notice = (
        "⚠️ Model fallback: Qwen/Qwen3.6-35B-A3B-FP8 via custom unavailable "
        "(request timeout); using poolside/laguna-s-2.1:free via nous."
    )
    await cloud.send("public-room", cloud_notice)
    check(
        "a cloud fallback does not open local standby",
        cloud._standby_engaged() is False,
    )

    local = make_adapter()
    local_notice = (
        "⚠️ Model fallback: google/gemma-4-31b-it:free via openrouter unavailable "
        "(rate limit); using gptoss via custom."
    )
    await local.send("public-room", local_notice)
    check(
        "a local fallback opens local standby",
        local._standby_engaged() is True,
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
