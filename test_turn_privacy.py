"""Turn-scoped Discord identity and context at Hermes' real plugin boundaries."""

import importlib.util
import os
import socket
import sys
import tempfile
import types
from unittest import mock


class _DeniedSocket(socket.socket):
    def connect(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")

    def connect_ex(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")


socket.socket = _DeniedSocket
_fixture_home = tempfile.TemporaryDirectory(prefix="ambient-turn-home-")
_fixture_hermes = tempfile.TemporaryDirectory(prefix="ambient-turn-hermes-")
os.environ["HOME"] = _fixture_home.name
os.environ["HERMES_HOME"] = _fixture_hermes.name
sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from hermes_cli import plugins as plugins_mod  # noqa: E402
from hermes_cli.middleware import apply_llm_request_middleware  # noqa: E402
from gateway.config import Platform  # noqa: E402
from gateway.run import GatewayRunner, _dequeue_pending_event  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
from gateway import session_context  # noqa: E402
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest  # noqa: E402


discord_adapter.DiscordAdapter.__init__ = lambda self, config: setattr(self, "config", config)
PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
spec = importlib.util.spec_from_file_location("ambient_turn_privacy_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)


class Author:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.display_name = name.title()
        self.global_name = None
        self.nick = None
        self.bot = False


class Guild:
    def __init__(self, gid, name):
        self.id = gid
        self.name = name
        self.default_role = object()


class Channel:
    def __init__(self, cid, name, guild):
        self.id = cid
        self.name = name
        self.guild = guild
        self.topic = None
        self.parent = None
        self.visible_to_everyone = True

    def permissions_for(self, role):
        return types.SimpleNamespace(view_channel=self.visible_to_everyone)


class Message:
    def __init__(self, mid, author, channel, content):
        self.id = mid
        self.author = author
        self.channel = channel
        self.guild = channel.guild
        self.content = content
        self.reference = None
        self.mentions = []
        self.attachments = []


FAILURES = []

def check(name, condition, detail=""):
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name}: {detail}")
        return
    print(f"  PASS  {name}")


def main():
    presence = {
        "enabled": True,
        "channels": ["*"],
        "probability": 0,
        "speaker_identity": True,
        "room_context": {"enabled": True, "recent_messages": 2, "max_chars": 400},
        "travel_log": {"enabled": True},
        "image_gen_gate": {"enabled": True, "allowed_users": ["101"]},
    }
    adapter = ambient.AmbientDiscordAdapter(types.SimpleNamespace(extra={"ambient_presence": presence}))
    adapter._client = types.SimpleNamespace(user=Author(999, "agent"))
    guild_a = Guild(1, "Guild A")
    channel_a = Channel(10, "hall", guild_a)
    alice = Message(1, Author(101, "alice"), channel_a, "first")
    bob = Message(2, Author(202, "bob"), channel_a, "queued second")
    private_reset = Message(3, Author(101, "alice"), Channel(30, "dm", None), "/reset")
    public_after_reset = Message(4, Author(101, "alice"), channel_a, "after reset")
    reverse_a = Message(5, Author(101, "alice"), channel_a, "reverse alice")
    reverse_b = Message(6, Author(202, "bob"), channel_a, "reverse bob")
    catchup = Message(7, Author(202, "bob"), channel_a, "older message")
    threaded = Message(8, Author(202, "bob"), channel_a, "thread me")

    manager = PluginManager(
        scope_key=os.path.join(os.environ["HERMES_HOME"], "turn-privacy-test")
    )
    manifest = PluginManifest(
        name="ambient-turn-test",
        provides_hooks=["pre_llm_call", "pre_tool_call"],
        key="ambient-turn-test",
    )
    ctx = PluginContext(manifest, manager)
    ambient.register(ctx)
    recall_plugin = os.environ.get("HERMES_RECALL_PLUGIN")
    recall_requests, persistable = {}, []
    if recall_plugin:
        recall_spec = importlib.util.spec_from_file_location("coupled_recall_fixture", recall_plugin)
        recall = importlib.util.module_from_spec(recall_spec)
        recall_spec.loader.exec_module(recall)
        recall._available = lambda: True
        recall._cfg = lambda: {"speaker_scoped": True}
        recall._run = lambda args, cap: {
            "id:101": "ALICE_PRIVATE_RECALL", "id:202": "BOB_PRIVATE_RECALL"
        }.get(args[-1], "")
        stage, inject = recall.on_pre_llm_call, recall.on_llm_request

        def capture_stage(**kwargs):
            result = stage(**kwargs)
            persistable.append(result)
            return result

        def capture_request(**kwargs):
            result = inject(**kwargs)
            recall_requests[kwargs.get("turn_id")] = repr(result or kwargs.get("request"))
            return result

        recall.on_pre_llm_call = capture_stage
        recall.on_llm_request = capture_request
        recall.register(ctx)
    old_manager = plugins_mod._plugin_manager
    plugins_mod._plugin_manager = manager
    old_cfg = ambient._image_gate_cfg
    runner = object.__new__(GatewayRunner)
    runner.config = types.SimpleNamespace(multiplex_profiles=False)
    runner.adapters = {Platform.DISCORD: adapter}
    observed = {}
    adapter._pending_messages = {}

    async def fake_run_agent_inner(message, context_prompt, history, source, session_id, **kwargs):
        plugins_mod.invoke_hook(
            "pre_llm_call",
            turn_id=f"runner-{source.message_id}",
            sender_id="101", platform="discord",
        )
        request = apply_llm_request_middleware(
            {"messages": [{"role": "user", "content": message}]},
            turn_id=f"runner-{source.message_id}",
            platform="discord",
        ).payload
        gate, _ = plugins_mod._dispatch_pre_tool_call_hooks(
            "image_generate", {"prompt": "x"}, turn_id=f"runner-{source.message_id}"
        )
        observed[source.message_id] = {
            "request": request, "gate": gate,
            "framework_user": session_context.get_session_env("HERMES_SESSION_USER_ID"),
            "framework_key": session_context.get_session_env("HERMES_SESSION_KEY"),
            "framework_message": session_context.get_session_env("HERMES_SESSION_MESSAGE_ID"),
        }
        if source.message_id == "1" and not kwargs.get("_interrupt_depth"):
            # Match gateway admission: the second dispatch returns after enqueue,
            # before the first run recurses into the pending event's own turn.
            await adapter._dispatch_stock_with_turn_context(bob)
            pending = _dequeue_pending_event(adapter, "shared-session")
            assert pending is not None
            await runner._run_agent(
                message=pending.text, context_prompt="", history=[],
                source=pending.source, session_id="shared-session",
                session_key="shared-session", _interrupt_depth=1,
                event_message_id=pending.message_id,
            )
        return {"final_response": "ok"}

    runner._run_agent_inner = fake_run_agent_inner
    runner._adapter_for_source = lambda source: adapter

    async def run_at_real_boundary(message, source):
        return await runner._run_agent(
            message.content, "", [], source, "shared-session",
            session_key="shared-session", inbound_message_id=str(message.id),
        )
    ambient._image_gate_cfg = lambda: {"enabled": True, "allowed_users": ["101"]}
    alice_source = SessionSource(
        platform=Platform.DISCORD, chat_id="10", chat_type="channel",
        user_id="101", user_name="alice", scope_id="1", message_id="1",
    )
    bob_source = SessionSource(
        platform=Platform.DISCORD, chat_id="10", chat_type="channel",
        user_id="202", user_name="bob", scope_id="1", message_id="2",
    )
    import asyncio
    try:
        original_dispatch = discord_adapter.DiscordAdapter._dispatch_discord_message

        async def admitted_dispatch(_self, message):
            if message.id == alice.id:
                await run_at_real_boundary(message, alice_source)
                plugins_mod.invoke_hook(
                    "pre_llm_call", turn_id="after-return", sender_id="101", platform="discord")
                gate, _ = plugins_mod._dispatch_pre_tool_call_hooks(
                    "image_generate", {"prompt": "x"}, turn_id="after-return")
                observed["after-return"] = {"gate": gate}
            else:
                event = discord_adapter.MessageEvent(
                    text=message.content, source=bob_source,
                    message_id=str(message.id), raw_message=message,
                )
                runner._enqueue_fifo("shared-session", event, adapter)
            return True

        discord_adapter.DiscordAdapter._dispatch_discord_message = admitted_dispatch
        try:
            asyncio.run(adapter._dispatch_stock_with_turn_context(alice))
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = original_dispatch

        original_dispatch = discord_adapter.DiscordAdapter._dispatch_discord_message
        async def handled_command(_self, _message):
            return True
        discord_adapter.DiscordAdapter._dispatch_discord_message = handled_command
        try:
            asyncio.run(adapter._dispatch_stock_with_turn_context(private_reset))
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = original_dispatch

        public_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", chat_type="channel",
            user_id="101", user_name="alice", scope_id="1", message_id="4",
        )
        adapter._prepare_turn_context(public_after_reset)
        asyncio.run(run_at_real_boundary(public_after_reset, public_source))

        adapter._prepare_turn_context(reverse_a)
        adapter._prepare_turn_context(reverse_b)
        for item, actor in ((reverse_b, "202"), (reverse_a, "101")):
            source = SessionSource(
                platform=Platform.DISCORD, chat_id="10", chat_type="channel",
                user_id=actor, scope_id="1", message_id=str(item.id),
            )
            asyncio.run(run_at_real_boundary(item, source))

        hint_token = ambient._ambient_hint.set("CATCHUP_SENTINEL")
        try:
            adapter._prepare_turn_context(catchup)
            source = SessionSource(
                platform=Platform.DISCORD, chat_id="10", chat_type="channel",
                user_id="202", scope_id="1", message_id="7",
            )
            asyncio.run(run_at_real_boundary(catchup, source))
        finally:
            ambient._ambient_hint.reset(hint_token)

        adapter._prepare_turn_context(threaded)
        thread_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", chat_type="channel",
            user_id="202", scope_id="1", message_id="8",
            prospective_thread_id="8008",
        )
        asyncio.run(run_at_real_boundary(threaded, thread_source))

        missing_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", chat_type="channel",
            user_id="101", scope_id="1", message_id="99",
        )
        asyncio.run(runner._run_agent(
            "no bundle", "", [], missing_source, "shared-session",
            session_key="shared-session", inbound_message_id="99",
        ))
        # A reply anchor is not the id of the incoming message. It must not
        # consume a retained private command's bundle as current authorization.
        anchor_only_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", chat_type="channel",
            user_id="101", scope_id="1",
        )
        asyncio.run(runner._run_agent(
            "missing inbound identity", "", [], anchor_only_source, "shared-session",
            session_key="shared-session", event_message_id="3",
        ))
        conflict = Message(84, Author(101, "alice"), channel_a, "conflicting inbound id")
        adapter._prepare_turn_context(conflict)
        asyncio.run(runner._run_agent(
            conflict.content, "", [], SessionSource(
                platform=Platform.DISCORD, chat_id="10", user_id="101", message_id="84"),
            "shared-session", inbound_message_id="85"))
        redelivered = Message(90, Author(101, "alice"), channel_a, "redelivered while queued")
        adapter._prepare_turn_context(redelivered)
        refuse_dispatch = discord_adapter.DiscordAdapter._dispatch_discord_message

        async def dedup_refuses(_self, _message):
            return False

        discord_adapter.DiscordAdapter._dispatch_discord_message = dedup_refuses
        try:
            asyncio.run(adapter._dispatch_stock_with_turn_context(redelivered))
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = refuse_dispatch
        asyncio.run(run_at_real_boundary(redelivered, SessionSource(
            platform=Platform.DISCORD, chat_id="10", user_id="101", message_id="90")))


        delayed = Message(80, Author(101, "alice"), channel_a, "queued for a long turn")
        adapter._prepare_turn_context(delayed)
        delayed_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", user_id="101", message_id="80")
        clock = ambient.time.monotonic
        with mock.patch.object(ambient.time, "monotonic", side_effect=lambda: clock() + 3600):
            asyncio.run(run_at_real_boundary(delayed, delayed_source))

        no_key = Message(81, Author(101, "alice"), channel_a, "keep the session key")
        adapter._prepare_turn_context(no_key)
        key_token = session_context._VAR_MAP["HERMES_SESSION_KEY"].set("kept-session")
        try:
            asyncio.run(runner._run_agent(
                no_key.content, "", [], SessionSource(
                    platform=Platform.DISCORD, chat_id="10", user_id="101", message_id="81"),
                "shared-session"))
        finally:
            key_token.var.reset(key_token)

        cancelled = Message(82, Author(101, "alice"), channel_a, "accepted before cancellation")
        cancelled_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", user_id="101", message_id="82")
        original_dispatch = discord_adapter.DiscordAdapter._dispatch_discord_message
        async def enqueue_then_cancel(_self, message):
            runner._enqueue_fifo("shared-session", discord_adapter.MessageEvent(
                text=message.content, source=cancelled_source,
                message_id=str(message.id), raw_message=message), adapter)
            raise asyncio.CancelledError()
        discord_adapter.DiscordAdapter._dispatch_discord_message = enqueue_then_cancel
        try:
            try:
                asyncio.run(adapter._dispatch_stock_with_turn_context(cancelled))
            except asyncio.CancelledError:
                pass
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = original_dispatch
        pending = _dequeue_pending_event(adapter, "shared-session")
        assert pending is not None
        asyncio.run(run_at_real_boundary(cancelled, pending.source))

        with mock.patch("gateway.run.GatewayRunner", None):
            ambient._install_gateway_turn_scope()
            broken_gate, _ = plugins_mod._dispatch_pre_tool_call_hooks(
                "image_generate", {"prompt": "x"}, turn_id="runner-1")
        ambient._install_gateway_turn_scope()
        observed["binding-error"] = {"gate": broken_gate}

        presence["speaker_identity"] = False
        old_dispatch = discord_adapter.DiscordAdapter._dispatch_discord_message
        old_recovered = discord_adapter.DiscordAdapter._dispatch_recovered_message

        async def unlabelled_dispatch(_self, message):
            source = SessionSource(
                platform=Platform.DISCORD, chat_id="10", chat_type="channel",
                user_id=str(message.author.id), message_id=str(message.id))
            await run_at_real_boundary(message, source)
            return True

        discord_adapter.DiscordAdapter._dispatch_discord_message = unlabelled_dispatch
        discord_adapter.DiscordAdapter._dispatch_recovered_message = unlabelled_dispatch
        try:
            asyncio.run(adapter._dispatch_stock_with_turn_context(
                Message(77, Author(101, "alice"), channel_a, "without a speaker tag")))
            asyncio.run(adapter._dispatch_recovered_with_turn_context(
                Message(78, Author(101, "alice"), channel_a, "recovered without a tag")))
        finally:
            discord_adapter.DiscordAdapter._dispatch_discord_message = old_dispatch
            discord_adapter.DiscordAdapter._dispatch_recovered_message = old_recovered
            presence["speaker_identity"] = True

        second_manager = PluginManager(
            scope_key=os.path.join(os.environ["HERMES_HOME"], "second-profile"))
        second_manifest = PluginManifest(
            name="ambient-turn-test", key="ambient-turn-test",
            path=os.path.dirname(PLUGIN),
            provides_hooks=["pre_llm_call", "pre_tool_call"],
        )
        second_module = second_manager._load_directory_module(second_manifest)
        second_module.register(PluginContext(second_manifest, second_manager))
        second_module._image_gate_cfg = ambient._image_gate_cfg
        second_adapter = second_module.AmbientDiscordAdapter(
            types.SimpleNamespace(extra={"ambient_presence": presence}))
        second_adapter._client = types.SimpleNamespace(user=Author(888, "other-agent"))
        second_message = Message(88, Author(202, "bob"), channel_a, "second profile")
        second_adapter._prepare_turn_context(second_message)
        second_source = SessionSource(
            platform=Platform.DISCORD, chat_id="10", chat_type="channel",
            user_id="202", scope_id="1", message_id="88",
        )
        plugins_mod._plugin_manager = second_manager
        runner._adapter_for_source = lambda source: second_adapter
        try:
            asyncio.run(run_at_real_boundary(second_message, second_source))
        finally:
            plugins_mod._plugin_manager = manager
            runner._adapter_for_source = lambda source: adapter
    finally:
        ambient._image_gate_cfg = old_cfg
        plugins_mod._plugin_manager = old_manager

    req_a = observed["1"]["request"]
    req_b = observed["2"]["request"]
    wire_a = "\n".join(str(row.get("content", "")) for row in req_a["messages"])
    wire_b = "\n".join(str(row.get("content", "")) for row in req_b["messages"])
    check(
        "GatewayRunner current source overrides cached sender",
        "id:202" in wire_b and "id:101" not in wire_b,
        wire_b,
    )
    check("first queued-chain turn has Alice identity", "id:101" in wire_a and "id:202" not in wire_a, wire_a)
    check("second queued-chain turn has Bob identity", "id:202" in wire_b and "id:101" not in wire_b, wire_b)
    check("Alice retains image authorization", observed["1"]["gate"] is None, repr(observed["1"]["gate"]))
    check("Bob cannot inherit Alice image authorization", isinstance(observed["2"]["gate"], str), repr(observed["2"]["gate"]))

    reset_wire = "\n".join(
        str(row.get("content", "")) for row in observed["4"]["request"]["messages"]
    )
    check("private command bundle cannot leak into later public turn", "dm" not in reset_wire.lower() and "id:101" in reset_wire, reset_wire)
    reverse_a_wire = "\n".join(str(r.get("content", "")) for r in observed["5"]["request"]["messages"])
    reverse_b_wire = "\n".join(str(r.get("content", "")) for r in observed["6"]["request"]["messages"])
    check("reversed same-session order keeps Alice exact", "id:101" in reverse_a_wire and "id:202" not in reverse_a_wire, reverse_a_wire)
    check("reversed same-session order keeps Bob exact", "id:202" in reverse_b_wire and "id:101" not in reverse_b_wire, reverse_b_wire)
    catchup_wire = "\n".join(str(r.get("content", "")) for r in observed["7"]["request"]["messages"])
    check("catch-up context survives the real runner boundary", "CATCHUP_SENTINEL" in catchup_wire, catchup_wire)
    check("thread echo binds actual delivery thread", "8008" in adapter._room_echo, repr(adapter._room_echo))
    missing_wire = "\n".join(str(r.get("content", "")) for r in observed["99"]["request"]["messages"])
    check("no-bundle turn withholds private context", "[speaker " not in missing_wire and isinstance(observed["99"]["gate"], str), missing_wire)
    check("reply anchor cannot substitute for incoming identity",
          isinstance(observed[None]["gate"], str), repr(observed[None]["gate"]))
    second_wire = "\n".join(str(row.get("content", ""))
                            for row in observed["88"]["request"]["messages"])
    check("profile-qualified plugin import binds its own turn",
          "id:202" in second_wire, second_wire)
    check("completed dispatch cannot authorize a later unbound turn",
          isinstance(observed["after-return"]["gate"], str))
    for message_id in ("77", "78"):
        wire = repr(observed[message_id]["request"])
        check(f"verified turn {message_id} authorizes with speaker tags disabled",
              observed[message_id]["gate"] is None and "[speaker " not in wire, wire)
    check("long queued wait retains exact turn authorization", observed["80"]["gate"] is None)
    check("accepted event survives transport cancellation", observed["82"]["gate"] is None)
    check("unbound ambient context does not erase framework user identity",
          observed["99"]["framework_user"] == "101", observed["99"]["framework_user"])
    check("omitted session key preserves the existing binding",
          observed["81"]["framework_key"] == "kept-session", observed["81"]["framework_key"])
    check("conflicting ids deny private authority without erasing source metadata",
          isinstance(observed["84"]["gate"], str)
          and observed["84"]["framework_message"] == "84", observed["84"]["framework_message"])
    check("dedup-refused redelivery cannot strand a queued turn",
          observed["90"]["gate"] is None, repr(observed["90"]["gate"]))
    check("binding failure is an availability refusal, not user denial",
          isinstance(observed["binding-error"]["gate"], str)
          and "identity binding" in observed["binding-error"]["gate"].lower(),
          repr(observed["binding-error"]["gate"]))

    # No verified actor for a new turn must clear, not inherit, authorization.
    plugins_mod._plugin_manager = manager
    ambient._image_gate_cfg = lambda: {"enabled": True, "allowed_users": ["101"]}
    plugins_mod.invoke_hook("pre_llm_call", turn_id="turn-unknown", sender_id="")
    deny_unknown, _ = plugins_mod._dispatch_pre_tool_call_hooks(
        "image_generate", {"prompt": "x"}, turn_id="turn-unknown"
    )
    check("unverified turn cannot inherit image authorization", isinstance(deny_unknown, str), repr(deny_unknown))
    ambient._image_gate_cfg = old_cfg
    plugins_mod._plugin_manager = old_manager

    public = {
        "id": 1, "guild_id": "2", "guild_name": "Guild B", "channel_id": "20",
        "channel_name": "commons", "chat_type": "channel", "admitted": True,
        "visibility": "public",
        "opened_at": ambient.time.time() - 60, "closed_at": ambient.time.time() - 30,
        "participants": [["303", "carol"]], "snippets": ["PUBLIC_SENTINEL"],
        "her_msg_count": 1,
    }
    private = dict(public, id=2, channel_id="21", chat_type="dm", snippets=["PRIVATE_SENTINEL"])
    unadmitted = dict(public, id=3, channel_id="22", admitted=False, snippets=["UNADMITTED_SENTINEL"])
    adapter._travel_beats = [public, private, unadmitted]
    rows = adapter._travel_rows(Message(3, Author(101, "alice"), channel_a, "where?"))
    rendered = "\n".join(rows)
    check("admitted public cross-guild travel remains visible", "Guild B" in rendered and "PUBLIC_SENTINEL" in rendered, rendered)
    check("private and unadmitted travel stay absent", "PRIVATE_SENTINEL" not in rendered and "UNADMITTED_SENTINEL" not in rendered, rendered)
    restricted = Channel(400, "staff-private", guild_a)
    restricted.visible_to_everyone = False
    restricted_message = Message(400, Author(101, "alice"), restricted, "PRIVATE_ROLE_SENTINEL")
    adapter._remember_room_talk(restricted_message)
    adapter._travel_admit(restricted_message)
    restricted_lane = adapter._travel_lanes.get("400")
    if restricted_lane is not None:
        adapter._travel_close_lane("400", restricted_lane, ambient.time.time(), "quiet")
    projected = "\n".join(adapter._travel_rows(alice))
    check("role-restricted guild source never projects as public",
          "staff-private" not in projected and "PRIVATE_ROLE_SENTINEL" not in projected,
          projected)

    private_thread = Channel(401, "private-thread", guild_a)
    private_thread.is_private = lambda: True
    adapter._travel_admit(Message(401, Author(101, "alice"), private_thread, "thread secret"))
    lane = adapter._travel_lanes.get("401")
    if lane is not None:
        adapter._travel_close_lane("401", lane, ambient.time.time(), "quiet")
    check("private thread never projects through public parent permissions",
          "private-thread" not in "\n".join(adapter._travel_rows(alice)))

    public_send = Channel(410, "public-self-start", guild_a)
    private_send = Channel(411, "private-self-start", guild_a)
    private_send.visible_to_everyone = False
    adapter._client.get_channel = lambda cid: {410: public_send, 411: private_send}.get(cid)
    for cid in (410, 411):
        adapter._travel_spoke(str(cid))
        lane = adapter._travel_lanes[str(cid)]
        adapter._travel_close_lane(str(cid), lane, ambient.time.time(), "quiet")
    projected = "\n".join(adapter._travel_rows(alice))
    check("own public send admits presence without admitting a private send",
          "public-self-start" in projected and "private-self-start" not in projected, projected)

    presence["travel_log"]["channels"] = ["10"]
    excluded = Message(100, Author(101, "alice"), Channel(11, "excluded", guild_a), "work")
    excluded_bundle = adapter._prepare_turn_context(excluded)
    check("travel allowlist applies while bundling", excluded_bundle["travel_context"] == "", excluded_bundle["travel_context"])
    adapter._discard_prepared_turn(excluded_bundle)

    adapter._room_echo = ambient.OrderedDict({
        "10": (ambient.time.time(), {"same alpha", "same beta"}),
    })
    check("channel A echo is suppressed", adapter._room_echo_leak("same alpha\nsame beta", "10"))
    check("channel A echo does not suppress channel B", not adapter._room_echo_leak("same alpha\nsame beta", "11"))
    for channel_number in range(200):
        adapter._bind_room_echo(
            str(1000 + channel_number),
            {"room_context": f"line {channel_number}\nother {channel_number}"},
        )
    check("channel echo map stays bounded", len(adapter._room_echo) == 128, len(adapter._room_echo))
    if recall_plugin:
        check("coupled recall returns no persistable hook context",
              bool(persistable) and all(value is None for value in persistable))
        check("coupled first turn recalls only Alice",
              "ALICE_PRIVATE_RECALL" in recall_requests["runner-1"]
              and "BOB_PRIVATE_RECALL" not in recall_requests["runner-1"])
        check("coupled queued turn recalls only Bob",
              "BOB_PRIVATE_RECALL" in recall_requests["runner-2"]
              and "ALICE_PRIVATE_RECALL" not in recall_requests["runner-2"])
        check("coupled unbound turns withhold recalled facts",
              all("PRIVATE_RECALL" not in recall_requests[turn]
                  for turn in ("runner-99", "runner-None")))
    if FAILURES:
        raise AssertionError("; ".join(FAILURES))
    print("turn privacy regression passed")


if __name__ == "__main__":
    main()
