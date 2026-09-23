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


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
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
    tag = adapter._speaker_tag(inbound)
    check(
        "the inbound Discord text is left unchanged",
        inbound.content == "hey",
        repr(inbound.content),
    )
    check(
        "the identity tag carries the stable numeric id",
        isinstance(tag, str) and "id:42" in tag,
        repr(tag),
    )
    bundle = adapter._prepare_turn_context(inbound)
    envelope = bundle.get("envelope") or ""
    check(
        "the prepared turn freezes the identity into the envelope",
        'speaker_id="42"' in envelope,
        envelope[:300],
    )
    check(
        "no adapter-produced private recall block exists",
        "memory_observation" not in envelope and "[known " not in envelope,
        envelope[:300],
    )
    check(
        "the adapter owns no synchronous speaker-memory lookup any more",
        not hasattr(adapter, "_speaker_memory"),
    )
    from gateway import session_context as _session_ctx
    import contextvars as _cv

    identity_ctx = getattr(_session_ctx, "_ambient_turn_identity", None)
    if identity_ctx is None:
        identity_ctx = _cv.ContextVar("ambient_turn_identity", default=None)
        _session_ctx._ambient_turn_identity = identity_ctx
    scope_token = ambient._verified_run_context.set(bundle)
    id_token = identity_ctx.set(
        (bundle.get("message_id"), bundle.get("speaker_id"))
    )
    try:
        ambient._on_pre_llm_call_turn_scope(turn_id="speaker-turn")
    finally:
        if id_token is not None:
            identity_ctx.reset(id_token)
        ambient._verified_run_context.reset(scope_token)
    request = {"messages": [{"role": "user", "content": "hey"}]}
    patched = ambient._on_llm_request_bind_turn_scope(
        request=request, turn_id="speaker-turn", platform="discord"
    )
    check(
        "the binding middleware never appends rows",
        patched is None and len(request["messages"]) == 1,
        (repr(patched), request),
    )
    ambient._EPHEMERAL_OVERRIDE = True
    try:
        delivered = ambient._on_pre_llm_call_ambient_context(
            turn_id="speaker-turn", platform="discord"
        )
    finally:
        ambient._EPHEMERAL_OVERRIDE = None
    envelope = (delivered or {}).get("ephemeral_context") or ""
    check(
        "the speaker identity rides the ephemeral envelope, never persisted prose",
        envelope.startswith("<ambient_context ") and 'speaker_id="42"' in envelope,
        envelope[:300],
    )
    check(
        "the envelope carries no persistent context key",
        isinstance(delivered, dict) and "context" not in delivered,
        repr(delivered),
    )


def check_quiet_resume_is_request_only():
    print("\n-- quiet-resume follows the typed origin, never user text --")
    original_cfg = ambient._ambient_cfg
    ambient._ambient_cfg = lambda key, default=None: True if key == "quiet_resume" else default
    try:
        ambient._quiet_resume_pending.set(False)
        typed = ambient._on_pre_llm_call_quiet_resume(
            user_message="completely ordinary text",
            conversation_history=[],
            turn_origin={"kind": "resume", "response_policy": "required"},
        )
        check(
            "a typed resume origin stages the directive",
            typed is None and ambient._quiet_resume_pending.get(False) is True,
            repr(ambient._quiet_resume_pending.get(False)),
        )

        ambient._quiet_resume_pending.set(False)
        typed_other = ambient._on_pre_llm_call_quiet_resume(
            user_message="completely ordinary text",
            conversation_history=[],
            turn_origin={"kind": "direct", "response_policy": "discretionary"},
        )
        check(
            "a non-resume origin stages nothing",
            typed_other is None and ambient._quiet_resume_pending.get(False) is False,
            repr(ambient._quiet_resume_pending.get(False)),
        )

        ambient._quiet_resume_pending.set(False)
        phrases = ambient._on_pre_llm_call_quiet_resume(
            user_message="The previous turn was interrupted. The gateway is now back online.",
            conversation_history=[],
        )
        check(
            "a user typing the old resume phrases does not stage quiet mode",
            phrases is None and ambient._quiet_resume_pending.get(False) is False,
            repr(ambient._quiet_resume_pending.get(False)),
        )

        ambient._quiet_resume_pending.set(False)
        legacy_history = ambient._on_pre_llm_call_quiet_resume(
            user_message="hello!",
            conversation_history=[
                {"role": "system", "content": "The gateway is now back online."}
            ],
        )
        check(
            "history rows carrying the old phrases do not stage quiet mode either",
            legacy_history is None and ambient._quiet_resume_pending.get(False) is False,
            repr(ambient._quiet_resume_pending.get(False)),
        )

        # Delivery is ephemeral-only: with the seam present the staged
        # directive rides the envelope; the middleware never emits rows.
        ambient._quiet_resume_pending.set(True)
        ambient._EPHEMERAL_OVERRIDE = True
        try:
            delivered = ambient._on_pre_llm_call_ambient_context(
                turn_id="resume-turn-unbound", platform="discord",
                turn_origin={"kind": "resume", "response_policy": "required"},
            )
        finally:
            ambient._EPHEMERAL_OVERRIDE = None
            ambient._quiet_resume_pending.set(False)
        envelope = (delivered or {}).get("ephemeral_context") or ""
        check(
            "a staged resume rides the ephemeral envelope",
            envelope.startswith("<ambient_context ") and "gateway restarted" in envelope,
            envelope[:200],
        )
        request = {"messages": [{"role": "user", "content": "hello"}]}
        patched = ambient._on_llm_request_bind_turn_scope(request=request)
        check(
            "no provider row carries the resume directive on any path",
            patched is None and len(request["messages"]) == 1,
            (repr(patched), request),
        )
    finally:
        ambient._ambient_cfg = original_cfg


def check_local_fallback_window_covers_the_local_floor():
    print("\n-- a fallback to any local model opens the standby window --")
    prefix = "🔄 Switched to fallback model: bb-chat via custom:gateway → "
    block = {"standby": {"enabled": True, "only_when_local": True}}

    for target, label in (
        ("gptoss via custom", "the first local model"),
        ("nemo4 via custom", "the second local model"),
    ):
        a = make_adapter(dict(block))
        a._note_fallback_notice(prefix + target)
        check(
            f"{label} opens only_when_local standby",
            a._standby_engaged() is True,
            target,
        )

    for target, label in (
        ("gpt-oss:20b via custom", "retired ollama gpt-oss"),
        ("qwen3:8b via custom", "retired ollama qwen3"),
        ("google/gemma-4-31b-it:free via openrouter", "a cloud fallback"),
    ):
        cloud = make_adapter(dict(block))
        cloud._note_fallback_notice(prefix + target)
        check(
            f"{label} leaves standby dormant",
            cloud._standby_engaged() is False,
            target,
        )


def main():
    check_channel_allowlist()
    check_name_triggers_are_normalized_and_bounded()
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
