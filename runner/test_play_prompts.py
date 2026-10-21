#!/usr/bin/env python3
"""runner/play.py's prompts to the play agent: client-neutral wording (every client reads them).

python3 -m unittest runner/test_play_prompts.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")
import play as P  # noqa: N812

# packages/harness/prompts/tests/test_prompts.py's rule: "turn" is Claude Code's word; pi has no turns to end.
TURN_RE = re.compile(r"your turn", re.IGNORECASE)


class ClientNeutral(unittest.TestCase):
    def test_no_turn_wording(self):  # live3 / BACKLOG: "end your turn" reached the pi agent
        texts = {
            "kickoff": P.build_kickoff("S001", "pt", "save", ["note"], briefing="MEMORY"),
            "kickoff without briefing": P.build_kickoff("S001", "pt", None, []),
            "fix notes": P.fix_notes_prompt("lint: bad"),
            "stop resume": P.stop_resume_prompt("Finish the wrap-up."),
        }
        for name, text in texts.items():
            self.assertIsNone(TURN_RE.search(text), f"{name}: {text}")
        self.assertIn("stop (no more tool calls)", texts["kickoff"])
        self.assertIn("stop (no more tool calls)", texts["fix notes"])


if __name__ == "__main__":
    unittest.main()
