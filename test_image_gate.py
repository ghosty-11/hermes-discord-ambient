"""Regression for AM-01: image gate uses the installed resolver contract."""

import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH", "/var/lib/hermes/.hermes/hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="ambient-image-gate-")

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


def main():
    manager = PluginManager(scope_key="image-gate-test")
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
        token = ambient._current_speaker_id.set(speaker)
        try:
            block, _ = plugins_mod._dispatch_pre_tool_call_hooks(
                "image_generate", {"prompt": "test"}, session_id="test"
            )
        finally:
            ambient._current_speaker_id.reset(token)
        if block is None:
            image_backend({"prompt": "test"})
        return block

    try:
        refused = dispatch("99")
        allowed = dispatch("42")
    finally:
        plugins_mod._plugin_manager = old_manager
        ambient._image_gate_cfg = old_cfg

    check("unauthorized image call is visibly refused", isinstance(refused, str), repr(refused))
    check("unauthorized image call never reaches backend", len(calls) == 1, repr(calls))
    check("allowed speaker reaches recorder backend", allowed is None and len(calls) == 1, repr((allowed, calls)))
    if FAILURES:
        raise SystemExit(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    print("image gate regression passed")


if __name__ == "__main__":
    main()
