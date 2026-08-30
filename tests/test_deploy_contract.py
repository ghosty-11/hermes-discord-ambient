from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeployContractTest(unittest.TestCase):
    def test_manifest_packages_only_runtime_plugin_files(self) -> None:
        manifest = json.loads((ROOT / "deploy" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("hermes-discord-ambient", manifest["module"])
        self.assertEqual(
            [
                {"source": "__init__.py", "destination": "plugins/discord-ambient/__init__.py", "kind": "file"},
                {"source": "plugin.yaml", "destination": "plugins/discord-ambient/plugin.yaml", "kind": "file"},
            ],
            manifest["artifacts"],
        )


if __name__ == "__main__":
    unittest.main()
