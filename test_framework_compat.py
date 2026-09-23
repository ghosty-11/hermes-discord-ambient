"""Reject ambient activation when the paired framework capabilities are absent.

The probe reads a candidate source tree; it never imports that tree or changes
Hermes state. Framework behavior is exercised separately by its own tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent / "deploy" / "deploy"


class FrameworkCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.framework = self.home / "framework"
        files = {
            "agent/turn_context.py": (
                "SUPPORTS_EPHEMERAL_CONTEXT = True\n"
                "SUPPORTS_COMPANION_ZERO_RETENTION = True\n"
                "def _append_ephemeral_to_wire_copy(row, text): pass\n"
                "def build_api_messages(): _append_ephemeral_to_wire_copy({}, '')\n"
            ),
            "gateway/turn_origin.py": (
                "class TurnOrigin: pass\n"
                "def turn_origin_allows_silence(origin): return True\n"
            ),
            "gateway/platforms/base.py": (
                "class BasePlatformAdapter:\n"
                "    async def _process_message_background(self):\n"
                "        self._finalize_turn_delivery_record()\n"
                "    def _finalize_turn_delivery_record(self): pass\n"
                "    def _turn_delivery_outcome(self): pass\n"
            ),
            "hermes_state_messages.py": (
                "class SessionMessagesMixin:\n"
                "    def record_message_delivery(self): pass\n"
            ),
            "tools/session_search_tool.py": "def _companion_scope_decision(): pass\n",
        }
        for name, content in files.items():
            path = self.framework / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def run_probe(self, command="compat"):
        return subprocess.run(
            [sys.executable, str(DEPLOY), command],
            env={**os.environ, "HERMES_HOME": str(self.home),
                 "HERMES_FRAMEWORK_PATH": str(self.framework)},
            capture_output=True, text=True, timeout=15,
        )

    def test_paired_source_passes_without_touching_host_state(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / "state").exists())

    def test_old_framework_refused_before_apply(self):
        (self.framework / "gateway/turn_origin.py").unlink()
        result = self.run_probe("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gateway/turn_origin.py", result.stderr)
        self.assertNotIn("deployment engine not found", result.stderr)
        self.assertFalse((self.home / "state").exists())

    def test_missing_zero_retention_seam_refuses_older_paired_source(self):
        path = self.framework / "agent/turn_context.py"
        path.write_text(path.read_text().replace(
            "SUPPORTS_COMPANION_ZERO_RETENTION = True",
            "SUPPORTS_COMPANION_ZERO_RETENTION = False",
        ), encoding="utf-8")
        result = self.run_probe("compat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("zero-retention", result.stderr)

    def test_shadowed_runtime_method_is_refused(self):
        path = self.framework / "gateway/platforms/base.py"
        path.write_text(path.read_text() + (
            "\nclass ShadowAdapter(BasePlatformAdapter):\n"
            "    async def _process_message_background(self): pass\n"
        ), encoding="utf-8")
        # A sibling class is fine; only a second definition on the BASE class
        # would shadow the instrumented path.
        self.assertEqual(self.run_probe().returncode, 0)
        path.write_text(path.read_text().replace(
            "    def _turn_delivery_outcome(self): pass\n",
            "    def _turn_delivery_outcome(self): pass\n"
            "    async def _process_message_background(self): pass\n",
        ), encoding="utf-8")
        result = self.run_probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("_process_message_background", result.stderr)


if __name__ == "__main__":
    unittest.main()
