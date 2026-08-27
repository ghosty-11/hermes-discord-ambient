"""Lifecycle inference must run inside a profile secret scope.

Root cause this pins, observed on a multiplexed deployment:

    agent.secret_scope.UnscopedSecretError: get_secret('..._KEY') called with
    no profile secret scope active while multiplexing is on.

raised through _prepare_gateway_lifecycle -> _generate_lifecycle_copy ->
run_oneshot -> resolve_provider_client -> get_secret, and swallowed as
"ambient.lifecycle: inference failed; staying silent".

The adapter's own comment states the intended contract: "one background
one-shot whose task inherits the profile runtime scope". That holds for
SECONDARY profiles -- gateway/run.py creates and connects their adapters
inside _profile_runtime_scope (:15672, :15756). The PRIMARY profile's startup
path has no such wrapper (_connect_one_startup, :13100), so its lifecycle task
inherits an unscoped context and every credential read fails closed.

Two cases, and the second is the one that must not regress: when a scope IS
already inherited, the plugin must use it rather than replacing it with one
built from whatever home happens to resolve -- replacing it could serve one
profile's turn with another profile's credentials.

Run with the framework venv:

    /var/lib/hermes/.hermes/hermes-agent/venv/bin/python test_lifecycle_secret_scope.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import types


sys.path.insert(0, os.environ.get("HERMES_FRAMEWORK_PATH",
                                  "/var/lib/hermes/.hermes/hermes-agent"))
_HOME = tempfile.mkdtemp(prefix="ambient-scope-test-")
os.environ["HERMES_HOME"] = _HOME

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402

discord_adapter.DiscordAdapter.__init__ = (
    lambda self, config: setattr(self, "config", config)
)

import agent.oneshot as oneshot_mod  # noqa: E402
from agent.secret_scope import (  # noqa: E402
    current_secret_scope,
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)

PLUGIN = os.environ.get("AMBIENT_PLUGIN_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "__init__.py"
)
spec = importlib.util.spec_from_file_location("ambient_scope_test", PLUGIN)
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
    cfg = types.SimpleNamespace(
        extra={
            "ambient_presence": {
                "enabled": True,
                "gateway_lifecycle": {
                    "enabled": True,
                    "shrine_channel": "1",
                    "inference": {
                        "enabled": True,
                        "persona_prompt": "a test persona",
                        "task": "title_generation",
                        "timeout_seconds": 5,
                    },
                },
            }
        }
    )
    return ambient.AmbientDiscordAdapter(cfg)


# Every call records whether a secret scope was visible at inference time,
# which is exactly what the real credential read needs.
SEEN = []


def fake_run_oneshot(**kwargs):
    SEEN.append(current_secret_scope())
    return json.dumps({"shrine_return": "back at the shrine"})


def main():
    set_multiplex_active(True)          # the deployment mode that fails closed
    oneshot_mod.run_oneshot = fake_run_oneshot
    adapter = make_adapter()

    # ---- case 1: no inherited scope (the primary-profile startup path) ----
    SEEN.clear()
    check("case 1 precondition: no scope active", current_secret_scope() is None)
    out = adapter._generate_lifecycle_copy(["shrine_return"])
    check("case 1: copy generated", out.get("shrine_return") == "back at the shrine",
          f"got {out!r}")
    check("case 1: a secret scope was active during inference",
          bool(SEEN) and SEEN[0] is not None,
          "inference ran unscoped -> real credential reads raise "
          "UnscopedSecretError and the lifecycle stays silent")

    # ---- case 2: an inherited scope must be preserved, not replaced ----
    SEEN.clear()
    sentinel = {"SENTINEL_KEY": "inherited-profile-value"}
    token = set_secret_scope(sentinel)
    try:
        out2 = adapter._generate_lifecycle_copy(["shrine_return"])
    finally:
        reset_secret_scope(token)
    check("case 2: copy generated", out2.get("shrine_return") == "back at the shrine",
          f"got {out2!r}")
    check("case 2: the INHERITED scope was used, not a substitute",
          bool(SEEN) and SEEN[0] is sentinel,
          f"saw {SEEN[0]!r}; replacing an inherited scope would let one "
          f"profile's turn read another profile's credentials")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
