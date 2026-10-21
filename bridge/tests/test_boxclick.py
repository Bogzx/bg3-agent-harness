"""Offline tests: hover-checked click on a Yes/No box whose button ignores its Command (live S013/S014:
long_rest "too many confirmations; still asking: [ForceUpdate] [Yes/No]", ~80 s of >10 s gaps).

Run: cd bridge && uv run python -m unittest tests.test_boxclick
"""

from __future__ import annotations

import json
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import boxclick  # noqa: E402
from bg3ai.tools import world_tools, inventory, uitools  # noqa: E402
from bg3ai import game as game_mod
from bg3ai.game import JobResult, NotReady  # noqa: E402

W, H = 1920, 1080
YES = (780, 560, 900, 610)  # Yes button rect (px)
NO = (1020, 560, 1140, 610)
# This far button is the 88th point tried: with probes >= PROBE_GAP_S apart (A18) the default 8 s budget covers ~80.
FAR_BUDGET_S = 20.0


class FakeClock:
    """boxclick's `time`: sleep advances the clock, so the poll spacing is exact and the tests don't wait."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakeScreen:
    """The box, a cursor and the input shim; clicks land only inside a button."""

    def __init__(self, yes=YES, no=NO, clock=None):
        self.clock = clock
        self.log: list[tuple[str, float]] = []   # (op, time) of every rpc, "click" for each click
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
        self.log.append(("click", self.now()))
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

    def now(self):
        return self.clock.t if self.clock else 0.0

    def rpc(self, op, args):
        self.log.append((op, self.now()))
        if op == "ui.box_open":
            return {"state": "Running", "box": bool(self.box)}
        if op == "sys.info":   # server reads of the transition wait after a rest/travel click (A23)
            return {"game_state": "Running"}
        if op == "camp.rest_flow":
            return {"steps": ["end_day_ok"]}
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
        self.clock = FakeClock()
        self.addCleanup(setattr, boxclick, "time", boxclick.time)
        boxclick.time = self.clock
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
        first = boxclick.click_box(s.rpc, s, "yes", budget_s=FAR_BUDGET_S)
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
        r = boxclick.click_box(s.rpc, s, "yes", budget_s=FAR_BUDGET_S)
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
        s = FakeScreen(clock=self.clock)
        s.next_box = True
        r = boxclick.click_box(s.rpc, s, "yes")
        self.assertTrue(r["shown"], r)
        self.assertFalse(r["closed"])
        self.assertIn("a question box still shows", boxclick.describe(r))

    def test_box_never_read_after_the_click(self):
        # S072/S077/S078: the game crashed 6.7-7.5 s after the probe-click began on the rest/travel box, while
        # confirm_probe walked the buttons + IsMouseOver of a box the transition was tearing down (every 30 ms).
        # After the click only ui.box_open is read; the transition (UnloadLevel) ends the watch as closed.
        s = FakeScreen(clock=self.clock)
        clicked = []
        polls = []
        orig = s.click

        def click(x, y, **kw):
            orig(x, y, **kw)
            s.box = True              # the box stays drawn while the game tears it down
            clicked.append(True)
        s.click = click

        def rpc(op, args):
            if clicked and op != "ui.box_open":
                raise AssertionError(f"{op} after the click: the box is being torn down")
            if op == "ui.box_open":
                polls.append(self.clock.t)
                if len(polls) >= 3:
                    return {"state": "UnloadLevel", "box": False}
                return {"state": "Running", "box": True}
            return s.rpc(op, args)
        r = boxclick.click_box(rpc, s, "yes")
        self.assertTrue(r["closed"], r)
        self.assertEqual(len(polls), 3)   # nothing read once the transition showed
        self.assertTrue(all(b - a >= boxclick.CLOSE_POLL_S >= 0.1 for a, b in pairwise(polls)), polls)

    def test_probes_before_the_click_are_spaced(self):
        s = FakeScreen(yes=(500, 640, 600, 670), clock=self.clock)   # far from the seed: a search
        r = boxclick.click_box(s.rpc, s, "yes", budget_s=FAR_BUDGET_S)
        self.assertTrue(r["closed"], r)
        probes = [t for op, t in s.log if op == "ui.confirm_probe"]
        self.assertGreater(len(probes), 3)
        self.assertTrue(all(b - a >= boxclick.PROBE_GAP_S >= 0.1 for a, b in pairwise(probes)), probes)
        after = s.log[[op for op, _ in s.log].index("click") + 1:]
        self.assertEqual({op for op, _ in after}, {"ui.box_open"})

    def test_return_fallback_watches_without_probing(self):
        s = FakeScreen(yes=(0, 0, 1, 1), clock=self.clock)
        s.enter_default = True
        boxclick.click_box(s.rpc, s, "yes", budget_s=0.5)
        ops = [op for op, _ in s.log]
        self.assertNotIn("ui.confirm_probe", ops[ops.index("ui.box_open"):])

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
        self.saved_inp = world_tools._inp
        world_tools._inp = lambda: self.scr
        self.addCleanup(setattr, world_tools, "_inp", self.saved_inp)
        self.g.rpc = lambda op, args=None, context="server", **kw: self.scr.rpc(op, args or {})
        self.addCleanup(setattr, world_tools, "TRANSITION_SETTLE_S", world_tools.TRANSITION_SETTLE_S)
        world_tools.TRANSITION_SETTLE_S = 0.0

    def test_long_rest_clicks_and_reruns(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(op)
            if len(calls) == 1:
                return JobResult("failed", {"needs_click": "yes"}, "the Yes press didn't close the game's question")
            return JobResult("done", {"rest": "full", "supplies_used": 40})
        self.g.run_job = run_job
        out = inventory.long_rest()
        self.assertEqual(calls, ["ui.long_rest", "ui.long_rest"])
        self.assertEqual(self.scr.pressed, "Yes")
        # S033 18:36:20: a "(the button press didn't land: clicked Yes …)" prefix on a SUCCESS read as a failure;
        # the click note shows only when the call did not end done.
        self.assertNotIn("didn't land", out)
        self.assertTrue(out.startswith("long_rest: done"), out)

    def test_long_rest_no_click_without_needs_click(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "failed", None, "not at camp")
        out = inventory.long_rest()
        self.assertEqual(self.scr.clicks, [])
        self.assertIn("not at camp", out)

    def test_confirm_dead_box_clicked(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "done", {"pressed": "No", "via": "command", "still_open": True, "needs_click": "no",
                     "buttons": ["Yes", "No"]})
        out = uitools.confirm("no")
        self.assertEqual(self.scr.pressed, "No")
        self.assertIn("confirm: done", out)
        self.assertIn("clicked No", out)

    def test_confirm_normal_untouched(self):
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "done", {"pressed": "Yes", "buttons": ["Yes", "No"]})
        out = uitools.confirm("yes")
        self.assertEqual(self.scr.clicks, [])
        self.assertIn("pressed=Yes", out)


class RestTransition(Base):
    """A23 (S086 05:17:45 hang): after the click on the rest/travel box, no client call at all until the server says
    the transition is over (rest-flow answer, a conversation, a load back to Running, the party moved)."""

    def setUp(self):
        super().setUp()
        boxclick._hits.clear()
        self.scr = FakeScreen()
        self.saved_inp = world_tools._inp
        world_tools._inp = lambda: self.scr
        self.addCleanup(setattr, world_tools, "_inp", self.saved_inp)
        for k, v in (("TRANSITION_POLL_S", 0.0), ("TRANSITION_SETTLE_S", 0.0)):
            self.addCleanup(setattr, world_tools, k, getattr(world_tools, k))
            setattr(world_tools, k, v)
        self.addCleanup(game_mod.release_client)
        self.clicked = False
        orig = self.scr.click

        def click(x, y, **kw):
            orig(x, y, **kw)
            self.clicked = True
        self.scr.click = click
        self.log: list[tuple[str, str, bool]] = []   # (op, context, client held at the time)
        self.flow = [{"steps": []}, {"steps": ["end_day_ok"]}]
        self.state = ["Running"]

        def rpc(op, args=None, context="server", **kw):
            held = bool(game_mod.client_held())
            self.log.append((op, context, held))
            if context == "client":
                if self.clicked and held:
                    raise AssertionError(f"client call {op} during the transition")
                if self.clicked and op in ("ui.box_open", "ui.confirm_probe"):
                    raise AssertionError(f"{op} after the click: the box is being torn down")
                return self.scr.rpc(op, args or {})
            if op == "sys.info":
                st = self.state.pop(0) if len(self.state) > 1 else self.state[0]
                if st is None:
                    raise NotReady("loading")
                return {"game_state": st}
            if op == "camp.rest_flow":
                return self.flow.pop(0) if len(self.flow) > 1 else self.flow[0]
            if op == "state.status":
                return {"party": []}
            return {}
        self.g.rpc = rpc
        self.jobs: list[tuple[str, bool]] = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            self.jobs.append((op, bool(game_mod.client_held())))
            if len(self.jobs) == 1:
                return JobResult("failed", {"needs_click": "yes", "question": "[ForceUpdate]"},
                                 "the Yes press didn't close the game's question")
            return JobResult("done", {"rest": "full", "supplies_used": 40})
        self.g.run_job = run_job

    def after_click(self):
        i = next(i for i, (op, _c, _h) in enumerate(self.log) if op == "sys.info")
        return self.log[i:]

    def test_s086_long_rest_waits_on_the_server_only_after_the_click(self):
        out = inventory.long_rest()
        self.assertEqual(self.scr.pressed, "Yes")
        self.assertTrue(out.startswith("long_rest: done"), out)
        self.assertEqual([op for op, _ in self.jobs], ["ui.long_rest", "ui.long_rest"])
        self.assertFalse(self.jobs[1][1], "the follow-up job starts after the hold is released")
        after = self.after_click()
        self.assertEqual({c for _op, c, _h in after if _h}, {"server"}, after)
        self.assertNotIn("ui.box_open", [op for op, _c, _h in self.log])
        self.assertIn("camp.rest_flow", [op for op, _c, _h in after])
        self.assertEqual(world_tools._TRANSITION_LAST.get("transition"), "the game answered (end_day_ok)")
        self.assertEqual(game_mod.client_held(), "")

    def test_s086_a_camp_scene_after_the_yes_stops_without_reading_the_ui(self):
        self.flow = [{"steps": [], "can_rest": {"blocked": [{"name": "Claude", "flags": ["Dialog"]}]}}]
        out = inventory.long_rest()
        self.assertEqual([op for op, _ in self.jobs], ["ui.long_rest"])
        self.assertIn("a conversation started (a camp scene)", out)
        self.assertTrue(out.startswith("long_rest: STOPPED"), out)

    def test_a_load_back_to_running_ends_the_wait(self):
        self.state = ["Running", "UnloadLevel", None, "LoadLevel", "Running"]
        r = world_tools._transition_wait("fast_travel")
        self.assertEqual(r["transition"], "the game loaded")

    def test_no_word_from_the_game_goes_on_after_the_budget(self):
        self.flow = [{"steps": ["requested", "gathered"]}]
        self.addCleanup(setattr, world_tools, "TRANSITION_MAX_S", world_tools.TRANSITION_MAX_S)
        world_tools.TRANSITION_MAX_S = 0.05
        r = world_tools._transition_wait("long_rest")
        self.assertEqual(r["transition"], "no word from the game")

    def test_the_hold_refuses_client_evals_at_once(self):
        g = game_mod.Game(port=1)   # nothing listens: a client eval would try to connect
        game_mod.hold_client(5.0, "long_rest: test")
        with self.assertRaises(game_mod.NotReady) as cm:
            g.eval("return 1", "client")
        self.assertIn("held", str(cm.exception))
        game_mod.release_client()
        self.assertEqual(game_mod.client_held(), "")


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
        saved = world_tools._inp
        world_tools._inp = lambda: Inp()
        self.addCleanup(setattr, world_tools, "_inp", saved)

    def test_none_only_hovers(self):
        out = uitools.click(0.5, 0.5, "none")
        self.assertEqual(self.ops, [("move", 500, 250)])
        self.assertIn("nothing clicked", out)

    def test_left_and_right(self):
        uitools.click(0.1, 0.2)
        uitools.click(0.1, 0.2, "Right")
        self.assertEqual(self.ops, [("click", "L", 100, 100), ("click", "R", 100, 100)])

    def test_left_click_says_the_spell_now_in_hand(self):
        # S083 03:55:16: a blind hotbar click picked up Netherese Orb Blast and the reply said only "clicked left"
        from bg3ai import hotbar
        states = [{"armed": True, "armed_by": "counter", "spell": "Projectile_NethereseOrbBlast"}, {"armed": False}]
        saved = hotbar.cast_state
        hotbar.cast_state = lambda game, char: states.pop(0)
        self.addCleanup(setattr, hotbar, "cast_state", saved)
        out = uitools.click(0.459, 0.93)
        self.assertIn("IN HAND: Projectile_NethereseOrbBlast", out)
        self.assertIn("press_key ESCAPE", out)
        out = uitools.click(0.6, 0.4)
        self.assertNotIn("IN HAND", out)
        self.assertEqual(len(states), 0)
        uitools.click(0.6, 0.4, "right")   # a right click or a hover reads nothing
        uitools.click(0.6, 0.4, "none")

    def test_a_world_click_while_walking_is_no_spell_in_hand(self):
        # S086-r1 05:35-05:38 / S087 (14x): move clicks on the world read `IN HAND: a spell` (armed by a weak signal,
        # no spell named) while the party simply walked
        from bg3ai import hotbar
        states = [{"armed": True, "armed_by": "preview", "spell": None},
                  {"armed": True, "armed_by": "spell_task"},
                  {"armed": False, "upcast_open": True, "spell": "Target_CureWounds"}]
        saved = hotbar.cast_state
        hotbar.cast_state = lambda game, char: states.pop(0)
        self.addCleanup(setattr, hotbar, "cast_state", saved)
        self.assertNotIn("IN HAND", uitools.click(0.62, 0.2))
        self.assertNotIn("IN HAND", uitools.click(0.47, 0.22))
        self.assertIn("IN HAND: Target_CureWounds", uitools.click(0.5, 0.9))   # the upcast picker is open: a spell

    def test_the_spell_in_hand_is_named_from_the_server_map(self):
        # S091 11/11 `IN HAND: a spell` (Guiding Bolt, Magic Missile from the hotbar in the psyche): no id on the
        # slot VM, no client map; the HUD character's server map names the active cell, by its shown name
        from bg3ai import hotbar
        calls = []

        def cast_state(game, char, m=None):
            calls.append((char, m))
            if m is None:
                return {"armed": True, "armed_by": "spell_task", "upcast_open": True, "spell": None, "char": "c-gale"}
            return {"armed": True, "upcast_open": True, "spell": "Projectile_MagicMissile",
                    "spell_name": "Magic Missile", "char": "c-gale"}
        maps = {"c-gale": {"ClassHotBar": {"4": "Projectile_MagicMissile"}}}
        for name, fn in (("cast_state", cast_state), ("server_map", lambda game, char: maps.get(char))):
            self.addCleanup(setattr, hotbar, name, getattr(hotbar, name))
            setattr(hotbar, name, fn)
        out = uitools.click(0.603, 0.93)
        self.assertIn("IN HAND: Magic Missile (the next left click", out)
        self.assertEqual(calls, [(None, None), ("c-gale", maps["c-gale"])])
        maps.clear()   # no server map either: still in hand, unnamed
        calls.clear()
        self.assertIn("IN HAND: a spell", uitools.click(0.75, 0.55))
        self.assertEqual(calls, [(None, None)])

    def test_unknown_button_refused(self):
        out = uitools.click(0.1, 0.2, "middle")
        self.assertEqual(self.ops, [])
        self.assertIn("unknown button", out)


if __name__ == "__main__":
    unittest.main()
