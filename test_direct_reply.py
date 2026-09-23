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


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
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
                "channels": ["*"],
                "probability": 0,
                "silent_marker": "[SILENT]",
                "text_hygiene": {"resolve_plain_mentions": True},
                "reply_style": {
                    "enabled": True,
                    "standalone_marker": "[STANDALONE]",
                },
                "speaker_identity": True,
                "room_context": {"enabled": False},
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
    print("\n-- direct scope binds state; delivery is ephemeral-only --")
    seen = {}

    async def handler(scoped_event):
        request = {"messages": [{"role": "user", "content": scoped_event.text}]}
        patched = ambient._on_llm_request_bind_turn_scope(request=request)
        seen["request"] = patched
        seen["suppressed"] = response_filters.is_intentional_silence_agent_result(
            {"failed": False}, "[SILENT]"
        )
        return None

    adapter.set_message_handler(handler)
    await adapter._message_handler(event)
    check(
        "the binding middleware never appends provider rows",
        seen.get("request") is None,
        repr(seen.get("request")),
    )
    check(
        "no adapter patch retains direct silence markers any more",
        not hasattr(ambient, "_install_direct_silence_filter"),
    )
    check(
        "gateway silence shaping stays stock: the marker is intentional silence",
        seen.get("suppressed") is True,
        repr(seen.get("suppressed")),
    )
    check(
        "ambient silence shaping is equally stock",
        response_filters.is_intentional_silence_agent_result(
            {"failed": False}, "[SILENT]"
        ) is True,
    )

    print("\n-- the direct-address directive rides the frozen envelope --")
    direct_msg = RawMessage(Reference("199", replied), message_id="200")
    bundle = adapter._prepare_turn_context(direct_msg)
    from gateway import session_context as _session_ctx
    import contextvars as _cv

    _identity = getattr(_session_ctx, "_ambient_turn_identity", None)
    if _identity is None:
        _identity = _cv.ContextVar("ambient_turn_identity", default=None)
        _session_ctx._ambient_turn_identity = _identity
    scope_token = ambient._verified_run_context.set(bundle)
    _id_token = _identity.set((bundle.get("message_id"), bundle.get("speaker_id")))
    try:
        ambient._on_pre_llm_call_turn_scope(turn_id="direct-turn")
    finally:
        _identity.reset(_id_token)
        ambient._verified_run_context.reset(scope_token)
    ambient._EPHEMERAL_OVERRIDE = True
    try:
        delivered = ambient._on_pre_llm_call_ambient_context(
            turn_id="direct-turn", platform="discord"
        )
    finally:
        ambient._EPHEMERAL_OVERRIDE = None
    envelope = (delivered or {}).get("ephemeral_context") or ""
    check(
        "the model receives the direct-address directive inside the envelope",
        envelope.startswith("<ambient_context ") and "direct" in envelope.lower(),
        envelope[:200],
    )
    check(
        "the envelope is ephemeral-only: never a persistent context key",
        isinstance(delivered, dict) and "context" not in delivered,
        repr(delivered),
    )
    # Seam-absent withhold: the paired framework advertises the seam, so the
    # override must be forced False for THIS assertion only, then restored.
    ambient._EPHEMERAL_OVERRIDE = False
    try:
        withheld_check = ambient._on_pre_llm_call_ambient_context(
            turn_id="direct-turn", platform="discord"
        )
    finally:
        ambient._EPHEMERAL_OVERRIDE = None
    check(
        "with the seam absent the envelope is withheld, not row-appended",
        not (
            isinstance(withheld_check, dict)
            and withheld_check.get("ephemeral_context")
        ),
        repr(withheld_check),
    )

    placement_cfg = dict(adapter.config.extra["ambient_presence"])
    placement_cfg["reply_style"] = {"enabled": True, "standalone_marker": "[STANDALONE]"}
    adapter.config.extra = {"ambient_presence": placement_cfg}
    placement_msg = RawMessage(Reference("199", replied), message_id="210")
    placement_bundle = adapter._prepare_turn_context(placement_msg)
    scope_token = ambient._verified_run_context.set(placement_bundle)
    _id_token = _identity.set(
        (placement_bundle.get("message_id"), placement_bundle.get("speaker_id"))
    )
    try:
        ambient._on_pre_llm_call_turn_scope(turn_id="placement-turn")
    finally:
        _identity.reset(_id_token)
        ambient._verified_run_context.reset(scope_token)
    ambient._EPHEMERAL_OVERRIDE = True
    try:
        placement = ambient._on_pre_llm_call_ambient_context(
            turn_id="placement-turn", platform="discord"
        )
    finally:
        ambient._EPHEMERAL_OVERRIDE = None
    placement_env = (placement or {}).get("ephemeral_context") or ""
    check(
        "the model receives standalone-placement guidance in the envelope",
        "[STANDALONE]" in placement_env and "room-wide" in placement_env.lower(),
        placement_env[:300],
    )
    check(
        "a turn with no scope delivers nothing (cron/webhook turns get no envelope)",
        ambient._on_pre_llm_call_ambient_context(
            turn_id="", platform="cron"
        )
        in (None, {}),
    )

    print("\n-- a direct silence marker sends nothing: no fallback sentence --")
    sent = []

    async def fake_send(self, chat_id, content, *args, **kwargs):
        sent.append((chat_id, content, kwargs.get("reply_to")))
        return types.SimpleNamespace(success=True, message_id="sent-1")

    discord_adapter.DiscordAdapter.send = fake_send
    adapter._direct_note_event(event)
    result = await adapter.send("channel-1", "[SILENT]", reply_to="200")
    check("no replacement message is sent", not sent, repr(sent))
    check(
        "the silent outcome reports success with no delivered text",
        getattr(result, "success", False) is True
        and getattr(result, "delivered_text", "missing") == "",
        repr(getattr(result, "delivered_text", "missing")),
    )
    check(
        "the adapter no longer fabricates presence text of any wording",
        not hasattr(adapter, "_direct_silence_fallback"),
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
    adapter._bounce_count_sent = lambda anchor, result: accounted.append(("bounce", anchor))
    adapter._catchup_count_sent = lambda anchor, result: accounted.append(("catchup", anchor))
    try:
        await adapter.send(
            "channel-1",
            "[STANDALONE] The whole room needed to hear that.",
            reply_to="placement-2",
        )
    finally:
        adapter._bounce_count_sent = original_bounce_count
        adapter._catchup_count_sent = original_catchup_count
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
        ],
        repr(accounted),
    )

    await adapter.send(
        "channel-1",
        "The whole room needed to hear that.\n\n[STANDALONE]",
        reply_to="placement-2b",
    )
    check(
        "a marker placed after the remark is stripped, not delivered",
        sent[-1] == ("channel-1", "The whole room needed to hear that.", None),
        repr(sent[-1]),
    )
    await adapter.send(
        "channel-1",
        "Moons have phases. **[standalone]**",
        reply_to="placement-2c",
    )
    check(
        "a dressed trailing marker is stripped in any case",
        sent[-1] == ("channel-1", "Moons have phases.", None),
        repr(sent[-1]),
    )

    before = len(sent)
    await adapter.send("channel-1", "[STANDALONE]", reply_to="placement-3")
    check(
        "a marker-only direct turn also sends nothing",
        len(sent) == before,
        repr(sent[before:]),
    )
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
