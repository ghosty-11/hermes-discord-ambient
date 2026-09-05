"""Turn-scoped Discord identity and context at Hermes' real plugin boundaries."""

import importlib.util
import os
import socket
import sys
import tempfile
import types


class _DeniedSocket(socket.socket):
    def connect(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")

    def connect_ex(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")


socket.socket = _DeniedSocket
os.environ["HOME"] = tempfile.mkdtemp(prefix="ambient-turn-home-")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-turn-hermes-")
sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from hermes_cli import plugins as plugins_mod  # noqa: E402
from hermes_cli.middleware import apply_llm_request_middleware  # noqa: E402
from gateway.config import Platform  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402
from gateway.session import SessionSource  # noqa: E402
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


class Channel:
    def __init__(self, cid, name, guild):
        self.id = cid
        self.name = name
        self.guild = guild
        self.topic = None
        self.parent = None


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


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
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
    old_manager = plugins_mod._plugin_manager
    plugins_mod._plugin_manager = manager
    old_cfg = ambient._image_gate_cfg
    runner = object.__new__(GatewayRunner)
    runner.config = types.SimpleNamespace(multiplex_profiles=False)
    runner.adapters = {Platform.DISCORD: adapter}
    observed = {}

    async def fake_run_agent_inner(message, context_prompt, history, source, session_id, **kwargs):
        plugins_mod.invoke_hook(
            "pre_llm_call",
            turn_id=f"runner-{source.message_id}",
            sender_id="101",
        )
        request = apply_llm_request_middleware(
            {"messages": [{"role": "user", "content": message}]},
            turn_id=f"runner-{source.message_id}",
            platform="discord",
        ).payload
        gate, _ = plugins_mod._dispatch_pre_tool_call_hooks(
            "image_generate", {"prompt": "x"}, turn_id=f"runner-{source.message_id}"
        )
        observed[source.message_id] = {"request": request, "gate": gate}
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
        adapter._prepare_turn_context(alice)
        asyncio.run(run_at_real_boundary(alice, alice_source))
        adapter._prepare_turn_context(bob)
        asyncio.run(run_at_real_boundary(bob, bob_source))

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
    print("turn privacy regression passed")


if __name__ == "__main__":
    main()
