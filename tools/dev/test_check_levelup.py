"""Offline tests for tools/dev/check_levelup.py (no game needed).

Run: uv run --project bridge python -m unittest tools/dev/test_check_levelup.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_levelup as C  # noqa: E402


class SessionGuard(unittest.TestCase):
    def _status(self, d: str, **kw) -> Path:
        p = Path(d) / "runner.status.json"
        p.write_text(json.dumps(kw))
        return p

    def test_running_session_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._status(d, runner_pid=os.getpid(), session="S020", session_ended=None, hard_stop=2e9)
            self.assertEqual(C.session_running(p, now=1e9), "S020")

    def test_between_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            ended = self._status(d, runner_pid=os.getpid(), session="S020", session_ended=1e9, hard_stop=2e9)
            self.assertIsNone(C.session_running(ended, now=1e9))
            past = self._status(d, runner_pid=os.getpid(), session="S020", session_ended=None, hard_stop=1e9)
            self.assertIsNone(C.session_running(past, now=2e9))
            dead = self._status(d, runner_pid=2**22 + 12345, session="S020", session_ended=None, hard_stop=2e9)
            self.assertIsNone(C.session_running(dead, now=1e9))
        self.assertIsNone(C.session_running(Path("/nonexistent/runner.status.json")))


class Summary(unittest.TestCase):
    def test_feat_level_page(self):
        o = {"open": True, "tab": "featTab", "class": "Cleric", "class_level": 3, "can_feat": True, "feat": "Alert",
             "feat_panel": True, "feats": [{"i": 1, "name": "Ability Improvement"}, {"i": 2, "name": "Heavy Armour Master", "off": True}],
             "ability_improvement": [{"a": "Wisdom", "v": 17, "plus": 1}], "ability_points": {"max": 2, "spent": 1},
             "spells": [{"title": "cantrips", "open": 1, "picked": [], "can_pick": ["Toll the Dead", "Light"], "cantrips": True}],
             "passives": []}
        text = "\n".join(C.summarize_options(o))
        self.assertIn("tab=featTab", text)
        self.assertIn("offered=1", text)          # the unavailable feat is not counted
        self.assertIn('"max": 2', text)
        self.assertIn("Toll the Dead", text)


if __name__ == "__main__":
    unittest.main()
