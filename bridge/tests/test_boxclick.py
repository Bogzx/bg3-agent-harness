"""Offline tests: hover-checked click on a Yes/No box whose button ignores its Command (live S013/S014:
long_rest "too many confirmations; still asking: [ForceUpdate] [Yes/No]", ~80 s of >10 s gaps).

Run: cd bridge && uv run python -m unittest tests.test_boxclick
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import boxclick, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

W, H = 1920, 1080
YES = (780, 560, 900, 610)  # Yes button rect (px)
NO = (1020, 560, 1140, 610)


class FakeScreen:
    """The box, a cursor and the input shim; clicks land only inside a button."""

    def __init__(self, yes=YES, no=NO):
        self.cur = (0, 0)
        self.box = True
        self.rects = {"Yes": yes, "No": no}
        self.clicks: list[tuple[int, int]] = []
        self.pressed = None
        self.next_box = None
        self.keys: list[str] = []
        self.enter_default = False  # RETURN answers the default (first) button

    def key(self, keys, sync=True):
        self.keys.append(keys)
        if keys == "RETURN" and self.enter_default and self.box:
            self.pressed, self.box = "Yes", False

    def window_size(self):
        return W, H

    def move(self, x, y, sync=True):
        self.cur = (x, y)

    def wait_frames(self, n, sync=True):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        self.clicks.append((x, y))
        for label, (x0, y0, x1, y1) in self.rects.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.pressed = label
                if self.next_box:
                    self.next_box, self.box = None, "next"
                else:
                    self.box = False

    def _in(self, label):
        x0, y0, x1, y1 = self.rects[label]
        return x0 <= self.cur[0] <= x1 and y0 <= self.cur[1] <= y1

    def rpc(self, op, args):
        if op != "ui.confirm_probe":
            return {}  # incidental reads (_finish's level-up nudge reads state.status once a minute)
        if not self.box:
            return {"box": False}
        want = "No" if str(args.get("answer")).lower() == "no" else "Yes"
        other = "Yes" if want == "No" else "No"
        text = "Someone wants to talk" if self.box == "next" else None
        v = {"box": True, "title": "[ForceUpdate]", "text": text, "buttons": ["Yes", "No"], "label": want,
             "over": self._in(want)}
        if self._in(other):
            v["over_other"] = 2 if other == "No" else 1
        return v


class ClickBox(unittest.TestCase):
    def setUp(self):
        boxclick._hits.clear()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.addCleanup(setattr, boxclick, "HITS_PATH", boxclick.HITS_PATH)
        boxclick.HITS_PATH = Path(d.name) / "boxclick_hits.json"

    def test_seed_hits_first_try(self):
        s = FakeScreen()
        r = boxclick.click_box(s.rpc, s, "yes")
        self.assertTrue(r["closed"], r)
        self.assertEqual(s.pressed, "Yes")
        self.assertEqual(r["tries"], 1)
        self.assertEqual(len(s.clicks), 1)

    def test_a_hit_is_kept_for_its_box_across_processes(self):
        # naut2: "Reset Tutorials" is seen once per run, by a new MCP server each time: searched from the seed
        # every run. Now its hit is stored per answer|title and the next process clicks it on the first try.
        far = (500, 640, 600, 670)
        s = FakeScreen(yes=far)
        first = boxclick.click_box(s.rpc, s, "yes")
        self.assertGreater(first["tries"], 1)
        stored = json.loads(boxclick.HITS_PATH.read_text())
        self.assertEqual(list(stored), ["yes|[ForceUpdate]"])
        boxclick._hits.clear()                               # a new process
        s2 = FakeScreen(yes=far)
        self.assertEqual(boxclick.click_box(s2.rpc, s2, "yes")["tries"], 1)
        boxclick._hits.clear()
        self.assertEqual(boxclick.candidates("yes", "Another box")[0], boxclick.SEEDS["yes"])   # per title
        boxclick.HITS_PATH.write_text("not json")
        self.assertEqual(boxclick.candidates("yes", "[ForceUpdate]")[0], boxclick.SEEDS["yes"])  # ignored

    def test_describe_counts_tries(self):
        self.assertEqual(boxclick.describe({"clicked": "No", "closed": True, "tries": 3}),
                         "clicked No on the question box (closed, 3 tries)")
        self.assertEqual(boxclick.describe({"clicked": "No", "closed": True, "tries": 1}),
                         "clicked No on the question box (closed, 1 try)")

    def test_searches_and_never_clicks_off_button(self):
        s = FakeScreen(yes=(500, 640, 600, 670))  # far from the seed
        r = boxclick.click_box(s.rpc, s, "yes")
        self.assertTrue(r.get("closed"), r)
        self.assertEqual(len(s.clicks), 1)  # only the hover-checked click
        self.assertGreater(r["tries"], 1)
        # remembered: the next box is hit at once
        s2 = FakeScreen(yes=(500, 640, 600, 670))
        self.assertEqual(boxclick.click_box(s2.rpc, s2, "yes")["tries"], 1)

    def test_no_answer(self):
        s = FakeScreen()
        r = boxclick.click_box(s.rpc, s, "no")
        self.assertEqual(s.pressed, "No")
        self.assertTrue(r["closed"])

    def test_next_question_reported(self):
        s = FakeScreen()
        s.next_box = True
        r = boxclick.click_box(s.rpc, s, "yes")
        self.assertIn("next", r)
        self.assertIn("Someone wants to talk", boxclick.describe(r))

    def test_not_found_no_click(self):
        s = FakeScreen(yes=(0, 0, 1, 1))
        r = boxclick.click_box(s.rpc, s, "yes", budget_s=0.5)
        self.assertIn("error", r)
        self.assertIn("RETURN didn't answer", r["error"])
        self.assertEqual(s.clicks, [])
        self.assertEqual(s.keys, ["RETURN"])

    def test_no_hover_falls_back_to_return(self):
        # S026 09:03: no cursor position read as over Yes (96 tries); RETURN answers the default button
        s = FakeScreen(yes=(0, 0, 1, 1))
        s.enter_default = True
        r = boxclick.click_box(s.rpc, s, "yes", budget_s=0.5)
        self.assertTrue(r["closed"], r)
        self.assertEqual(r["via"], "key")
        self.assertEqual(s.pressed, "Yes")
        self.assertEqual(s.clicks, [])
        self.assertIn("Yes (RETURN key)", boxclick.describe(r))

    def test_no_answer_never_presses_return(self):
        s = FakeScreen(no=(0, 0, 1, 1))
        s.enter_default = True
        r = boxclick.click_box(s.rpc, s, "no", budget_s=0.5)
        self.assertIn("error", r)
        self.assertEqual(s.keys, [])
        self.assertTrue(s.box)

    def test_no_box(self):
        s = FakeScreen()
        s.box = False
        self.assertTrue(boxclick.click_box(s.rpc, s, "yes")["gone"])


class BoxAct(Base):
    def setUp(self):
        super().setUp()
        boxclick._hits.clear()
        self.scr = FakeScreen()
        self.saved_inp = server._inp
        server._inp = lambda: self.scr
        self.addCleanup(setattr, server, "_inp", self.saved_inp)
        self.g.rpc = lambda op, args=None, context="server", **kw: self.scr.rpc(op, args or {})

    def test_long_rest_clicks_and_reruns(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(op)
            if len(calls) == 1:
                return JobResult("failed", {"needs_click": "yes"}, "the Yes press didn't close the game's question")
            return JobResult("done", {"rest": "full", "supplies_used": 40})
        self.g.run_job = run_job
        out = server.long_rest()
        self.assertEqual(calls, ["ui.long_rest", "ui.long_rest"])
        self.assertEqual(self.scr.pressed, "Yes")
        # S033 18:36:20: a "(the button press didn't land: clicked Yes …)" prefix on a SUCCESS read as a failure;
        # the click note shows only when the call did not end done.
        self.assertNotIn("didn't land", out)
        self.assertTrue(out.startswith("long_rest: done"), out)

    def test_long_rest_no_click_without_needs_click(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "failed", None, "not at camp")
        out = server.long_rest()
        self.assertEqual(self.scr.clicks, [])
        self.assertIn("not at camp", out)

    def test_confirm_dead_box_clicked(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "done", {"pressed": "No", "via": "command", "still_open": True, "needs_click": "no",
                     "buttons": ["Yes", "No"]})
        out = server.confirm("no")
        self.assertEqual(self.scr.pressed, "No")
        self.assertIn("confirm: done", out)
        self.assertIn("clicked No", out)

    def test_confirm_normal_untouched(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "done", {"pressed": "Yes", "buttons": ["Yes", "No"]})
        out = server.confirm("yes")
        self.assertEqual(self.scr.clicks, [])
        self.assertIn("pressed=Yes", out)


class ClickButton(Base):
    """click(button=...): "none" hovers only (S039 21:47:42 it committed a left click = a 5 m jump under Z)."""

    def setUp(self):
        super().setUp()
        self.ops = []
        test = self

        class Inp:
            def window_size(self):
                return 1000, 500

            def move(self, x, y, sync=True):
                test.ops.append(("move", x, y))

            def click(self, x, y, button="L", count=1, sync=True):
                test.ops.append(("click", button, x, y))
        saved = server._inp
        server._inp = lambda: Inp()
        self.addCleanup(setattr, server, "_inp", saved)

    def test_none_only_hovers(self):
        out = server.click(0.5, 0.5, "none")
        self.assertEqual(self.ops, [("move", 500, 250)])
        self.assertIn("nothing clicked", out)

    def test_left_and_right(self):
        server.click(0.1, 0.2)
        server.click(0.1, 0.2, "Right")
        self.assertEqual(self.ops, [("click", "L", 100, 100), ("click", "R", 100, 100)])

    def test_unknown_button_refused(self):
        out = server.click(0.1, 0.2, "middle")
        self.assertEqual(self.ops, [])
        self.assertIn("unknown button", out)


if __name__ == "__main__":
    unittest.main()
