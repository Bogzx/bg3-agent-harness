"""Offline tests: hover-checked click on a row of a reaction prompt with one row per spell level (act A28).

Live S091/S092: Gale's Shield prompt (rows I-VI) never answered to ui.react's commands (TIMEOUT 4 of 5, by name and
by number), while every hand click on a Shield row cast it at once.

Run: cd bridge && uv run python -m unittest tests.test_reactclick
"""

from __future__ import annotations

import unittest

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import reactclick, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

W, H = 1920, 1080


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakePrompt:
    """The reaction popup: `rows` rows, row 1 at `top` (window fraction), one every 0.05, x 0.43-0.61; a click on
    a row answers it (the popup goes, or the next prompt takes its place)."""

    def __init__(self, rows=6, top=0.457, events=1, answers=True):
        self.rows, self.top, self.events, self.answers = rows, top, events, answers
        self.open = True
        self.cur = (0, 0)
        self.clicked: list[int] = []
        self.ops: list[tuple[str, dict]] = []

    def row_at(self, x, y):
        fx, fy = x / W, y / H
        if not 0.43 <= fx <= 0.61:
            return None
        for i in range(1, self.rows + 1):
            if abs(fy - (self.top + 0.05 * (i - 1))) <= 0.02:
                return i
        return None

    # input shim
    def window_size(self):
        return W, H

    def move(self, x, y, sync=True):
        self.cur = (x, y)

    def wait_frames(self, n, sync=True):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        k = self.row_at(x, y)
        if k and self.open and self.answers:
            self.clicked.append(k)
            self.events -= 1
            self.open = self.events > 0

    # mod
    def rpc(self, op, args):
        self.ops.append((op, dict(args)))
        assert op == "ui.react_probe", op
        out = {"open": self.open, "events": self.events if self.open else 0}
        if not self.open or args.get("watch"):
            return out
        out["rows"] = self.rows
        k = self.row_at(*self.cur)
        if k is not None:
            if k == args.get("which"):
                out["over"] = True
            else:
                out["over_row"] = k
        return out


class ClickRow(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        saved = reactclick.time
        reactclick.time = self.clock
        self.addCleanup(setattr, reactclick, "time", saved)

    def test_the_s092_layout_is_hit_at_the_seed(self):
        for rows, which in ((6, 1), (6, 4), (5, 1), (4, 1)):   # S092: rows 1 of 6/5/4 at 0.457/0.507/0.557
            p = FakePrompt(rows=rows, top=0.457 + 0.05 * (6 - rows))
            r = reactclick.click_row(p.rpc, p, which)
            self.assertTrue(r.get("closed"), r)
            self.assertEqual((p.clicked, r["tries"]), ([which], 1))

    def test_a_shifted_panel_is_found_from_the_row_under_the_cursor(self):
        # another window size / one more line of prompt text: the seed lands on row 1 while row 3 is wanted
        p = FakePrompt(rows=6, top=0.457 - 0.1)
        r = reactclick.click_row(p.rpc, p, 1)
        self.assertEqual(p.clicked, [1], r)
        self.assertLessEqual(r["tries"], 3)

    def test_never_clicks_off_the_row(self):
        p = FakePrompt(rows=6, top=0.9)   # rows off the searched band: no hover anywhere
        r = reactclick.click_row(p.rpc, p, 2, budget_s=3.0)
        self.assertIn("no cursor position over reaction row 2", r.get("error", ""))
        self.assertEqual(p.clicked, [])

    def test_gone_and_bad_row(self):
        p = FakePrompt()
        p.open = False
        self.assertEqual(reactclick.click_row(p.rpc, p, 1), {"gone": True, "tries": 0})
        p = FakePrompt(rows=4)
        self.assertIn("no reaction row 6 (4 shown)", reactclick.click_row(p.rpc, p, 6)["error"])

    def test_the_watch_reads_no_rows(self):
        p = FakePrompt(events=2)   # the next prompt waits behind this one
        r = reactclick.click_row(p.rpc, p, 2)
        self.assertTrue(r["closed"])
        self.assertEqual(r["pending"], 1)
        after = p.ops[[op for op, _ in p.ops].index("ui.react_probe"):]
        self.assertTrue(all(a.get("watch") for _, a in after[-1:]))
        self.assertIn("1 more prompt(s) waiting", reactclick.describe(r))

    def test_a_click_that_answers_nothing_says_so(self):
        p = FakePrompt(answers=False)
        r = reactclick.click_row(p.rpc, p, 1)
        self.assertTrue(r.get("shown"), r)
        self.assertIn("still shows", reactclick.describe(r))


class ReactTool(Base):
    def setUp(self):
        super().setUp()
        self.p = FakePrompt()
        saved = server._inp
        server._inp = lambda: self.p
        self.addCleanup(setattr, server, "_inp", saved)
        base = self.g.rpc
        self.g.rpc = lambda op, args=None, context="server", **kw: (
            self.p.rpc(op, args or {}) if op == "ui.react_probe" else base(op, args, context, **kw))
        self.clock = FakeClock()
        saved_t = reactclick.time
        reactclick.time = self.clock
        self.addCleanup(setattr, reactclick, "time", saved_t)
        self.jobs: list[tuple[str, dict]] = []

    def test_a_multi_level_prompt_is_answered_by_a_row_click(self):
        # S092 08:43:38 react(use=true, which="Shield") -> TIMEOUT; now the mod hands the row to the bridge
        def run_job(op, args, context="server", wait_s=25.0, **kw):
            self.jobs.append((op, dict(args)))
            return JobResult("done", {"needs_click": int(args.get("which") or 1), "rows": 6})
        self.g.run_job = run_job
        out = server.react(True, "3")
        self.assertTrue(out.startswith("react: done — clicked reaction row 3"), out)
        self.assertEqual(self.p.clicked, [3])
        self.assertEqual(self.jobs, [("ui.react", {"use": True, "which": "3"})])

    def test_a_single_row_prompt_is_untouched(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult("done", {})
        out = server.react(True, "1")
        self.assertIn("react: done", out)
        self.assertEqual(self.p.clicked, [])

    def test_do_not_react_left_up_says_what_answers_it(self):
        # S092 08:55:23: react(use=false) TIMEOUT on the 4-row Shield prompt, which then still showed
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult("timeout", None, "timed out")
        ui = {"screen": "reaction", "reaction": {"options": [{"i": k, "name": "Shield", "level": k + 2}
                                                             for k in range(1, 5)]}}
        saved = self.g.rpc
        self.g.rpc = lambda op, args=None, context="server", **kw: (ui if op == "ui.state"
                                                                    else saved(op, args, context))
        out = server.react(False)
        self.assertIn("react: TIMEOUT", out)
        self.assertIn("Do Not React has never closed a prompt with one row per level", out)
        self.assertIn('set_reactions {"<name>": "never"}', out)

    def test_combat_turn_reaction_answer_clicks_the_row(self):
        saved = server._watch_conn
        server._watch_conn = lambda: self.g
        self.addCleanup(setattr, server, "_watch_conn", saved)
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "done", {"needs_click": 1, "rows": 6})
        out = server._answer_reaction(True)
        self.assertIn("reaction prompt: used (clicked reaction row 1", out)
        self.assertEqual(self.p.clicked, [1])


if __name__ == "__main__":
    unittest.main()
