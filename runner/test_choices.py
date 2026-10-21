#!/usr/bin/env python3
"""runner/choices.py: dialogue choices from an event log, as the bridge renders DIALOGUE blocks.

    python3 -m unittest runner/test_choices.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import choices as C  # noqa: E402
from clients import Event  # noqa: E402

SHOWN = ("interact: done — used @(-766.7,-25.9,-727.7)\nDIALOGUE Shadowheart: Another trial. Allow me - please.\n"
         "  1. Go on.\n  2. [Persuasion] No, you're not doing this.  ← Persuasion DC 10\n"
         "STOPPED: a real choice (2 options)\nEVENTS\n  dialogue_line text=x speaker=Narrator\n  3. not an answer")


class Choices(unittest.TestCase):
    def test_shown(self):
        self.assertEqual(C.shown(SHOWN), {"scene": "Shadowheart: Another trial. Allow me - please.",
                                          "options": {1: "Go on.", 2: "[Persuasion] No, you're not doing this."}})
        self.assertIsNone(C.shown("move_to: done"))

    def test_choices(self):
        evs = [Event("tool_result", text=SHOWN, t=10.0),
               Event("thinking", text="She knows these altars.", t=11.0),
               Event("text", text="Letting her try.", t=12.0),
               Event("tool_call", tool="choose_dialogue", args={"option": 1}, t=13.0),
               Event("tool_result", text="choose_dialogue: done — ended screen=game", t=14.0),
               Event("tool_call", tool="choose_dialogue", args={"option": 2}, t=15.0),   # no reason, same list
               Event("tool_call", tool="choose_dialogue", args={"option": "x"}, t=16.0),
               Event("tool_call", tool="move_to", args={}, t=17.0)]
        out = C.choices(evs)
        opts = [{"i": 1, "text": "Go on."}, {"i": 2, "text": "[Persuasion] No, you're not doing this."}]
        self.assertEqual(out, [
            {"t": 13.0, "ev": "choice", "scene": "Shadowheart: Another trial. Allow me - please.", "options": opts,
             "picked": 1, "picked_text": "Go on.", "reason": "She knows these altars.\nLetting her try."},
            {"t": 15.0, "ev": "choice", "scene": "Shadowheart: Another trial. Allow me - please.", "options": opts,
             "picked": 2, "picked_text": "[Persuasion] No, you're not doing this."}])

    def test_nothing_shown(self):
        out = C.choices([Event("tool_call", tool="choose_dialogue", args={"option": 3})])
        self.assertEqual(out, [{"t": None, "ev": "choice", "scene": "", "options": [], "picked": 3}])


if __name__ == "__main__":
    unittest.main()
