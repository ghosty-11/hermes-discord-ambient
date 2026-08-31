from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED_ACTION = re.compile(r"uses:\s+[^@\s]+@[0-9a-f]{40}(?:\s|$)")


class HostedCiTest(unittest.TestCase):
    def test_workflow_is_hosted_read_only_and_pinned(self) -> None:
        text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for required in ("pull_request:", "push:", "contents: read", "persist-credentials: false", "runs-on: ubuntu-24.04", "python3 -m unittest"):
            self.assertIn(required, text)
        self.assertNotIn("self-hosted", text)
        uses = [line.strip().removeprefix("- ") for line in text.splitlines() if line.strip().startswith(("uses:", "- uses:"))]
        self.assertTrue(uses)
        for line in uses:
            self.assertRegex(line, PINNED_ACTION)


if __name__ == "__main__":
    unittest.main()
