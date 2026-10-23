"""Regression for AM-01: image gate uses the installed resolver contract."""

import asyncio
import importlib.util
import os
import sys
import socket
import tempfile
from unittest import mock

class _DeniedSocket(socket.socket):
    def connect(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")

    def connect_ex(self, address):
        raise AssertionError(f"network denied in fixture: {address!r}")


socket.socket = _DeniedSocket
os.environ["HOME"] = tempfile.mkdtemp(prefix="ambient-image-home-")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-image-gate-")
sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))

from hermes_cli import plugins as plugins_mod  # noqa: E402
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest  # noqa: E402

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")
spec = importlib.util.spec_from_file_location("ambient_image_gate_test", PLUGIN)
ambient = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambient)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


async def async_connect(self, *, is_reconnect=False):
    return True


def main():
    manager = PluginManager(
        scope_key=os.path.join(os.environ["HERMES_HOME"], "image-gate-test")
    )
    manifest = PluginManifest(
        name="discord-ambient-test",
        provides_hooks=["pre_tool_call"],
        key="discord-ambient-test",
    )
    ctx = PluginContext(manifest, manager)
    ctx.register_hook("pre_tool_call", ambient._on_pre_tool_call)
    old_manager = plugins_mod._plugin_manager
    old_cfg = ambient._image_gate_cfg
    calls = []
    ambient._image_gate_cfg = lambda: {"enabled": True, "allowed_users": ["42"]}
    plugins_mod._plugin_manager = manager

    def image_backend(args):
        calls.append(args)
        return "generated"

    def dispatch(speaker):
        turn_id = f"turn-{speaker}" if speaker else ""
        if turn_id:
            with ambient._turn_scopes_lock:
                ambient._turn_scopes[turn_id] = {"speaker_id": speaker}
        block, _ = plugins_mod._dispatch_pre_tool_call_hooks(
            "image_generate", {"prompt": "test"}, session_id="test",
            turn_id=turn_id,
        )
        if block is None:
            image_backend({"prompt": "test"})
        return block

    try:
        pending = dispatch("42")
        pending_calls = len(calls)
        adapter = object.__new__(ambient.AmbientDiscordAdapter)
        with mock.patch.object(ambient.DiscordAdapter, "connect",
                               mock.AsyncMock(return_value=False)):
            asyncio.run(adapter.connect())
        refused = dispatch("99")
        refused_calls = len(calls)
        allowed = dispatch("42")
        missing = dispatch("")
    finally:
        plugins_mod._plugin_manager = old_manager
        ambient._image_gate_cfg = old_cfg

    check("allowed actor cannot reach backend before identity binding",
          isinstance(pending, str) and pending_calls == 0, repr((pending, pending_calls)))
    check("unauthorized image call is visibly refused", isinstance(refused, str), repr(refused))
    check("unauthorized image call never reaches backend", refused_calls == 0, repr(calls))
    check("allowed speaker reaches recorder backend", allowed is None and len(calls) == 1, repr((allowed, calls)))
    check("missing turn id is visibly refused", isinstance(missing, str), repr(missing))
    from hermes_cli import config as config_mod
    with mock.patch.object(config_mod, "load_config", side_effect=RuntimeError("unavailable")):
        unavailable = ambient._on_pre_tool_call("image_generate", {"prompt": "x"})
    check("unavailable gate configuration refuses image generation",
          isinstance(unavailable, dict) and unavailable.get("action") == "block")
    ambient._image_gate_cfg = lambda: {}
    check("absent gate leaves image generation ungated",
          ambient._on_pre_tool_call("image_generate", {"prompt": "x"}) is None)
    ambient._image_gate_cfg = lambda: {"enabled": False}
    check("disabled gate leaves image generation ungated",
          ambient._on_pre_tool_call("image_generate", {"prompt": "x"}) is None)
    ambient._image_gate_cfg = old_cfg
    from plugins.platforms.discord import adapter as stock
    from types import SimpleNamespace
    with mock.patch.object(ambient, "_require_companion_framework"), \
         mock.patch.object(stock.DiscordAdapter, "__init__", lambda self, config: setattr(self, "config", config)), \
         mock.patch.object(ambient, "_bundled") as bundled, \
         mock.patch.object(ambient, "_install_compaction_focus"), \
         mock.patch.object(ambient, "_install_gateway_turn_scope"), \
         mock.patch.object(stock.DiscordAdapter, "connect", new=async_connect):
        class FailedHook:
            def register_hook(self, name, callback):
                if name == "pre_tool_call":
                    raise RuntimeError("hook unavailable")
            def register_middleware(self, *args):
                pass
        ambient.register(FailedHook())
        gated = ambient.AmbientDiscordAdapter(SimpleNamespace(
            extra={"ambient_presence": {"image_gen_gate": {"enabled": True}}}))
        plain = ambient.AmbientDiscordAdapter(SimpleNamespace(extra={"ambient_presence": {}}))
        check("missing image hook refuses gated connection",
              asyncio.run(gated.connect(is_reconnect=True)) is False)
        check("missing image hook permits ungated connection",
              asyncio.run(plain.connect(is_reconnect=True)) is True)
        ambient._image_gate_hook_registered = True
    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    print("image gate regression passed")


if __name__ == "__main__":
    main()
