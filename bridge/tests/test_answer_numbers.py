"""Offline tests: the answer numbers the agent is shown are the ones choose_dialogue picks (act A19).

S081 03:01-03:03 (Karlach's camp menu, swap_party #95 + 10 hand calls, ~1.5 min): get_dialogue listed 7 answers with
no `more below`, choose_dialogue(7) → `FAILED: no option 7 (6 shown)`, later `no option 7 (5 shown)` with
`7. Come on, we're heading out.` listed; the join answer was found with DOWN x6 + a click. The mod's answer window
takes the first realized row's height as every row's: Karlach's first answer wraps to two lines, so the list's size
came out 5 (no `more below`) and its numbers drifted once the list was scrolled.

Run: cd bridge && uv run python -m unittest tests.test_answer_numbers
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_clicker import FakeInput
from test_turn_macros import (
    Base,  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
)

from bg3ai import clicker
from bg3ai.tools import core, reporting, world_tools, dialogue
from bg3ai.game import GameError, JobResult

KARLACH = [   # S081's menu as the agent finally found it (the first answer wraps to two lines)
    ("Karlach, I'm worried about what Dammon said about your engine. We need to get you back to Avernus, or it's "
     "going to blow."),
    "That fight at the tollhouse really... heated you up, didn't it?",
    "In your expert opinion, what's the best way to kill a devil?",
    "You and Mizora seem to know each other. How?",
    "So you had a friend in Avernus. You never said.",
    "Tell me more about your relationship with Gortash.",
    "Tell me again why you want to collect soul coins.",
    "Come on, we're heading out.",
    "I'll be back.",
]
HEIGHTS = [168.0] + [84.0] * (len(KARLACH) - 1)
VIEW = 504.0                       # the list's window: 6 one-line rows
EXTENT = sum(HEIGHTS)
SCROLLABLE = EXTENT - VIEW
NOTCH_PX = 48.0


class Menu:
    """The mod's view of a scrolled answer list (UIState listWindow: first = floor(offset / first row's height) + 1,
    total = extent / that height) and ui.dialogue_choose's lookup by those numbers."""

    def __init__(self, inp: FakeInput) -> None:
        self.inp, self.off, self.seen, self.cursor = inp, 0.0, 0, (0, 0)
        self.chosen: list[str] = []
        self.jobs: list[dict] = []

    def _apply(self) -> None:
        for ev in self.inp.log[self.seen:]:
            if ev[0] == "wheel":
                self.off = min(SCROLLABLE, max(0.0, self.off - NOTCH_PX * ev[1]))
            elif ev[0] == "move":
                self.cursor = (ev[1], ev[2])
        self.seen = len(self.inp.log)

    def realized(self) -> list[int]:
        top, out = 0.0, []
        for k, h in enumerate(HEIGHTS):
            if top + h > self.off and top < self.off + VIEW:
                out.append(k)
            top += h
        return out

    def window(self) -> tuple[int, int]:
        row_h = HEIGHTS[self.realized()[0]]
        return int(self.off // row_h) + 1, int(EXTENT / row_h + 0.5)

    def options(self) -> list[dict]:
        self._apply()
        first, _ = self.window()
        return [{"i": first + n, "text": KARLACH[k]} for n, k in enumerate(self.realized())]

    def try_rpc(self, op, args=None, context="server", default=None):
        self._apply()
        if op == "ui.state":
            first, total = self.window()
            opts = self.options()
            d = {"speaker": "Karlach", "text": "Soldier?", "options": opts, "scrollable": True}
            below = total - (first - 1) - len(opts)
            if below > 0:
                d["more_below"] = below
            return {"screen": "dialogue", "dialogue": d}
        if op == "ui.answer_list":
            first, total = self.window()
            return {"open": True, "over": 700 <= self.cursor[1] <= 1000 and 700 <= self.cursor[0] <= 1200,
                    "first": first, "total": total, "offset": self.off, "scrollable": SCROLLABLE}
        return default

    rpc = try_rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        assert op == "ui.dialogue_choose", op
        self.jobs.append(dict(args))
        shown = self.options()
        a = next((o for o in shown if o["i"] == args["i"]), None)
        if a is None:
            raise GameError(f"no option {args['i']} ({len(shown)} shown)")
        if args.get("text") and clicker.answer_key(a["text"]) != clicker.answer_key(args["text"]):
            raise GameError(f"option {args['i']} reads '{a['text']}', not '{args['text']}' (the list changed)")
        self.chosen.append(a["text"])
        return JobResult(status="done", result={"screen": "game"})


class AnswerNumbers(unittest.TestCase):
    def setUp(self) -> None:
        clicker._list_hit["at"] = None
        reporting._answer_pages.clear()
        reporting._listed.clear()
        self.inp = FakeInput()
        self.g = Menu(self.inp)
        for target, name, val in ((core, "game", self.g), (world_tools, "_inp", lambda: self.inp),
                                  (clicker.time, "sleep", lambda s: None)):
            p = unittest.mock.patch.object(target, name, val)
            p.start()
            self.addCleanup(p.stop)

    def listing(self) -> list[str]:
        return [ln for ln in reporting._render_ui(self.g.try_rpc("ui.state")) if ln[:4].strip()[:1].isdigit()]

    def test_s081_the_whole_menu_is_listed_in_order(self):
        rows = self.listing()
        self.assertEqual(len(rows), 9, rows)
        self.assertEqual(rows[7], "  8. Come on, we're heading out.")
        self.assertEqual(rows[8], "  9. I'll be back.")
        self.assertNotIn("more answers below", "\n".join(reporting._render_ui(self.g.try_rpc("ui.state"))))
        self.assertEqual(self.g.off, 0.0)   # wheeled back to the top

    def test_s081_choose_picks_the_answer_listed_under_that_number(self):
        self.listing()
        r = dialogue._choose_answer(8)       # below the fold; the mod numbers its rows differently once scrolled
        self.assertEqual(r.status, "done")
        self.assertEqual(self.g.chosen, ["Come on, we're heading out."])
        self.assertEqual(self.g.jobs[-1]["text"], "Come on, we're heading out.")

    def test_s081_a_visible_answer_is_chosen_without_scrolling(self):
        self.listing()
        n = len(self.inp.log)
        dialogue._choose_answer(3)
        self.assertEqual(self.g.chosen, ["In your expert opinion, what's the best way to kill a devil?"])
        self.assertFalse([e for e in self.inp.log[n:] if e[0] == "wheel"])

    def test_s081_scrolled_list_choose_by_listed_number(self):
        # the agent's listing came from the top; the list is now scrolled (the mod's numbers drifted by one)
        self.listing()
        self.inp.wheel(-6)
        self.g.options()
        self.assertNotEqual([o["i"] for o in self.g.options() if o["text"] == KARLACH[6]], [7])
        dialogue._choose_answer(7)
        self.assertEqual(self.g.chosen, ["Tell me again why you want to collect soul coins."])

    def test_another_node_uses_the_mods_numbers(self):
        self.listing()
        reporting._listed["node"] = ("Karlach", "an earlier line")
        dialogue._choose_answer(2)
        self.assertEqual(self.g.jobs[-1], {"i": 2})


if __name__ == "__main__":
    unittest.main()
