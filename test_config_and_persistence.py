"""Fail-first checks for config coercion, multiplex state, and request-only injection.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_config_and_persistence.py
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import urllib.parse
import urllib.request


sys.path.insert(0, "/var/lib/hermes/.hermes/hermes-agent")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-config-test-")

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402


discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_config_test", PLUGIN)
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


def make_adapter(over=None, user_id=999):
    block = {
        "enabled": True,
        "channels": ["*"],
        "speaker_identity": True,
    }
    if over:
        block.update(over)
    adapter = ambient.AmbientDiscordAdapter(
        types.SimpleNamespace(extra={"ambient_presence": block})
    )
    adapter._client = types.SimpleNamespace(user=Author(user_id, "Muse"))
    adapter._get_parent_channel_id = lambda channel: None
    return adapter


def message_in(channel_id, author_id=1, content="hello there"):
    return types.SimpleNamespace(
        channel=types.SimpleNamespace(id=channel_id),
        author=Author(author_id, "alice"),
        content=content,
    )


def check_channel_allowlist():
    print("\n-- channel allowlist accepts hermes config set shapes --")
    a = make_adapter({"channels": "123,456"})
    check(
        "a comma string admits the first listed channel",
        a._channel_allowed(message_in(123)) is True,
    )
    check(
        "a comma string admits the second listed channel",
        a._channel_allowed(message_in(456)) is True,
    )
    check(
        "a comma string still refuses an unlisted channel",
        a._channel_allowed(message_in(789)) is False,
    )

    a = make_adapter({"channels": 123})
    check(
        "a bare integer id does not raise",
        a._channel_allowed(message_in(123)) is True,
    )
    check(
        "a bare integer id still refuses another channel",
        a._channel_allowed(message_in(456)) is False,
    )

    a = make_adapter({"channels": "*"})
    check(
        "a bare '*' string is the wildcard",
        a._channel_allowed(message_in(9999)) is True,
    )


def check_name_triggers_are_normalized_and_bounded():
    print("\n-- name triggers require complete configured names --")
    scalar = make_adapter({
        "name_triggers": "bot-name",
        "name_cooldown_seconds": 0,
        "probability": 0,
    })
    scalar._basic_ambient_eligible = lambda message: True
    scalar._ambient_last = 0
    check(
        "a scalar trigger is not split into matching characters",
        scalar._join_reason(message_in(1, content="hello everyone")) is None,
    )
    check(
        "a scalar trigger still recognizes the complete name",
        scalar._join_reason(message_in(1, content="hello bot-name")) == "named",
    )

    bounded = make_adapter({
        "name_triggers": ["cat"],
        "name_cooldown_seconds": 0,
        "probability": 0,
    })
    bounded._basic_ambient_eligible = lambda message: True
    bounded._ambient_last = 0
    check(
        "a trigger does not match inside another word",
        bounded._join_reason(message_in(1, content="education")) is None,
    )
    check(
        "a list trigger still matches as a complete word",
        bounded._join_reason(message_in(1, content="hello cat")) == "named",
    )


def check_image_authorization_is_dispatch_scoped():
    print("\n-- image authorization uses the current author, not speaker presentation --")
    original_cfg = ambient._image_gate_cfg
    original_recovered = discord_adapter.DiscordAdapter._dispatch_recovered_message
    ambient._image_gate_cfg = lambda: {
        "enabled": True,
        "allowed_users": ["42"],
    }
    decisions = []

    async def inspect(message):
        decisions.append(
            ambient._on_pre_tool_call("image_generate", {"prompt": "test"})
        )
        return True

    async def exercise():
        adapter = make_adapter({"speaker_identity": False})
        adapter._dispatch_inner = inspect
        await adapter._dispatch_discord_message(message_in(1, author_id=42))
        await adapter._dispatch_discord_message(message_in(1, author_id=99))

        async def recovered(_self, _message):
            decisions.append(
                ambient._on_pre_tool_call("image_generate", {"prompt": "test"})
            )
            return True

        discord_adapter.DiscordAdapter._dispatch_recovered_message = recovered
        adapter._bounce_pre_dispatch = lambda message: None

        async def no_wait(message):
            return False

        adapter._standby_wait = no_wait
        await adapter._dispatch_recovered_message(message_in(1, author_id=42))

    token = ambient._current_speaker_id.set("")
    try:
        asyncio.run(exercise())
    finally:
        ambient._current_speaker_id.reset(token)
        ambient._image_gate_cfg = original_cfg
        discord_adapter.DiscordAdapter._dispatch_recovered_message = original_recovered

    check(
        "an allowed author is authorized with speaker tags disabled",
        decisions[0] is None,
        repr(decisions[0]),
    )
    check(
        "the next unauthorized author cannot inherit authorization",
        isinstance(decisions[1], dict) and decisions[1].get("decision") == "block",
        repr(decisions[1]),
    )
    check(
        "recovered dispatch authorizes independently of speaker tags",
        decisions[2] is None,
        repr(decisions[2]),
    )
    check(
        "authorization identity is cleared after dispatch",
        ambient._current_speaker_id.get("") == "",
        repr(ambient._current_speaker_id.get("")),
    )


def check_last_seen_is_per_bot():
    print("\n-- last-seen state is keyed on the bot account, not shared --")
    one = make_adapter(user_id=111)
    two = make_adapter(user_id=222)
    path_one = one._ambient_seen_path()
    path_two = two._ambient_seen_path()
    check("each bot has a last-seen path", bool(path_one) and bool(path_two))
    check(
        "two Discord identities do not share one last-seen file",
        path_one != path_two,
        f"{path_one!r} vs {path_two!r}",
    )
    check(
        "the path includes the bot user id",
        path_one is not None and "111" in os.path.basename(path_one),
        repr(path_one),
    )

    legacy = os.path.join(os.path.dirname(path_one), "ambient-last-seen.json")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w") as fh:
        json.dump({"42": 1234.5}, fh)
    migrated = make_adapter(user_id=333)
    migrated._ensure_seen_loaded()
    check(
        "legacy shared state is retained during per-bot cutover",
        migrated._last_seen.get("42") == 1234.5,
        repr(migrated._last_seen),
    )
    check(
        "legacy state is copied to the bot-specific path",
        os.path.exists(migrated._ambient_seen_path()),
        repr(migrated._ambient_seen_path()),
    )


def check_gif_state_is_per_profile():
    print("\n-- GIF pending/rate-limit state is isolated per profile --")
    first = ambient._gif_bucket("profile-a")
    second = ambient._gif_bucket("profile-b")
    check("two profiles get distinct GIF buckets", first is not second)
    first["pending"] = ("https://example.test/a.webp", 1.0)
    check(
        "a pending GIF on one profile does not appear on another",
        second.get("pending") is None,
        repr(second.get("pending")),
    )


def check_gif_state_uses_turn_profile():
    print("\n-- production GIF lookup uses the active Discord identity --")
    profile_context = getattr(ambient, "_gif_profile_context", None)
    check(
        "a turn-scoped GIF profile context exists",
        profile_context is not None,
    )
    if profile_context is None:
        return

    first_token = profile_context.set("111")
    try:
        first = ambient._gif_bucket()
    finally:
        profile_context.reset(first_token)
    second_token = profile_context.set("222")
    try:
        second = ambient._gif_bucket()
    finally:
        profile_context.reset(second_token)

    check(
        "the no-argument production path isolates two bot identities",
        first is not second,
    )




def check_gif_request_uses_public_plugin_identity():
    print("\n-- GIF requests expose only the public plugin identity --")
    original_cfg = ambient._gif_config
    original_key = ambient._gif_key
    original_urlopen = urllib.request.urlopen
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return (
                b'{"data":{"data":[{"file":{"md":{"webp":'
                b'{"url":"https://example.test/reaction.webp"}}}}]}}'
            )

    ambient._gif_config = lambda: {
        "min_interval_seconds": 0,
        "max_per_day": 20,
    }
    ambient._gif_key = lambda: "test-key"
    urllib.request.urlopen = lambda request, timeout=0: (
        captured.append(request.full_url) or Response()
    )
    token = ambient._gif_profile_context.set("999")
    try:
        ambient._gif_handle({"query": "hello", "customer_id": "private-profile"})
    finally:
        ambient._gif_profile_context.reset(token)
        ambient._gif_config = original_cfg
        ambient._gif_key = original_key
        urllib.request.urlopen = original_urlopen

    query = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0]).query)
    check(
        "customer_id names the public plugin, not a private profile",
        query.get("customer_id") == ["hermes-discord-ambient"],
        repr(query.get("customer_id")),
    )


def check_speaker_is_request_only():
    print("\n-- speaker identity stays off the persisted user turn --")
    adapter = make_adapter()
    inbound = message_in("channel-1", author_id=42, content="hey")
    adapter._apply_speaker_tag(inbound)
    check(
        "the inbound Discord text is left unchanged",
        inbound.content == "hey",
        repr(inbound.content),
    )
    token = ambient._speaker_context.set((" [speaker @alice id:42]", None))
    try:
        request = {"messages": [{"role": "user", "content": "hey"}]}
        patched = ambient._on_llm_request_ambient_hint(request=request)
        messages = ((patched or {}).get("request") or {}).get("messages") or []
        check(
            "the model still receives the speaker tag",
            any("[speaker @alice id:42]" in str(m.get("content", "")) for m in messages),
            repr(messages),
        )
        check(
            "the speaker tag is a request-only user row, not a trailing system row",
            bool(messages)
            and messages[-1].get("role") == "user"
            and not any(m.get("role") == "system" for m in messages[1:]),
            repr([m.get("role") for m in messages]),
        )
    finally:
        ambient._speaker_context.reset(token)


def check_quiet_resume_is_request_only():
    print("\n-- quiet-resume is request-only, not a persisted user injection --")
    original_cfg = ambient._ambient_cfg
    ambient._ambient_cfg = lambda key, default=None: True if key == "quiet_resume" else default
    try:
        hook = ambient._on_pre_llm_call_quiet_resume(
            user_message="The previous turn was interrupted. The gateway is now back online.",
            conversation_history=[],
        )
        check(
            "pre_llm_call no longer injects quiet-resume into user context",
            hook is None,
            repr(hook),
        )
    finally:
        ambient._ambient_cfg = original_cfg

    token = ambient._quiet_resume_pending.set(True)
    try:
        request = {"messages": [{"role": "user", "content": "hello"}]}
        patched = ambient._on_llm_request_ambient_hint(request=request)
        messages = ((patched or {}).get("request") or {}).get("messages") or []
        check(
            "the resume directive reaches the request payload",
            any("gateway restarted" in str(m.get("content", "")).lower() for m in messages),
            repr(messages),
        )
        check(
            "the resume directive is a request-only user row",
            bool(messages) and messages[-1].get("role") == "user",
            repr(messages[-1] if messages else None),
        )
    finally:
        ambient._quiet_resume_pending.reset(token)


def check_local_fallback_window_covers_the_local_floor():
    print("\n-- a fallback to any local model opens the standby window --")
    prefix = "🔄 Switched to fallback model: Qwen/Qwen3.6-35B-A3B-FP8 via custom:hetzner → "
    block = {"standby": {"enabled": True, "only_when_local": True}}

    for target, label in (
        ("gpt-oss:20b via custom", "the first local model"),
        ("qwen3:8b via custom", "the second local model"),
    ):
        a = make_adapter(dict(block))
        a._note_fallback_notice(prefix + target)
        check(
            f"{label} opens only_when_local standby",
            a._standby_engaged() is True,
            target,
        )

    cloud = make_adapter(dict(block))
    cloud._note_fallback_notice(prefix + "google/gemma-4-31b-it:free via openrouter")
    check(
        "a cloud fallback leaves standby dormant",
        cloud._standby_engaged() is False,
        "openrouter hop",
    )


def main():
    check_channel_allowlist()
    check_name_triggers_are_normalized_and_bounded()
    check_image_authorization_is_dispatch_scoped()
    check_last_seen_is_per_bot()
    check_gif_state_is_per_profile()
    check_gif_state_uses_turn_profile()
    check_gif_request_uses_public_plugin_identity()
    check_speaker_is_request_only()
    check_quiet_resume_is_request_only()
    check_local_fallback_window_covers_the_local_floor()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
