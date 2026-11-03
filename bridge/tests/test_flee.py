"""Offline tests: the flee tool (research/ovh/live2.md §2: the HUD's Flee sends one character to camp once every
enemy is 27 m away; the game asks "Do you want to flee from combat?" first). The UI op sequence against a fake mod.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_flee.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import boxclick, hoverclick
from bg3ai.render import Handles
from bg3ai.tools import combat, core, world_tools

P1, P2, GOB = (f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2, 50))


class FakeInput:
    def __init__(self):
        self.at = (0, 0)
        self.clicks: list[tuple[int, int]] = []

    def window_size(self):
        return (1000, 1000)

    def move(self, x, y, sync=True):
        self.at = (x, y)

    def click(self, x, y, button="L", count=1, sync=True):
        self.clicks.append((x, y))

    def wait_frames(self, n, sync=True):
        pass


class Flee(unittest.TestCase):
    def setUp(self):
        saved = (core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S)

        def restore():
            core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S = saved
            hoverclick._hits.clear()

        self.addCleanup(restore)
        core.H = Handles()
        combat.FLEE_BOX_S = combat.FLEE_LEFT_S = 0.0
        self.ops: list[tuple[str, dict]] = []
        self.gob_m = 40.0
        self.commands = [{"widget": "HotBar", "name": "RequestFlee", "can": True}]
        self.command_works = True
        self.button = None  # {"over_at": (x, y)} when the HUD has a findable Flee button
        self.box = False
        self.fled = False
        self.flee = None  # ui.flee_probe's flee state ({can, cause, value, max}), None: not read
        self.inp = FakeInput()
        test = self

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                test.ops.append((op, dict(args or {})))
                if op == "state.combat":
                    order = [
                        {"uuid": P2, "name": "Karlach", "relation": "party"},
                        {"uuid": GOB, "name": "Goblin", "relation": "hostile", "from": {P1: [test.gob_m, 1]}},
                    ]
                    if not test.fled:
                        order.insert(0, {"uuid": P1, "name": "Claude", "relation": "party"})
                    return {"active": True, "round": 2, "order": order}
                if op == "ui.selected":
                    return {"uuid": P1}
                if op == "state.status":
                    return self.rpc(op)
                return default

            def rpc(self, op, args=None, context="server", **k):
                test.ops.append((op, dict(args or {})))
                if op == "ui.flee_probe":
                    b = test.button
                    return {
                        "commands": test.commands,
                        **(
                            {"flee": test.flee} if test.flee is not None and not (args or {}).get("button_only") else {}
                        ),
                        "button": b
                        and {
                            "widget": "HotBar",
                            "name": "FleeBtn",
                            "over": test.inp.at == b["over_at"],
                            "enabled": True,
                        },
                    }
                if op == "ui.flee":
                    test.box = test.command_works
                    return {"pressed": args["name"]}
                if op == "ui.box_open":
                    if test.button and test.inp.clicks:
                        test.box = True
                    return {"box": "Dialog_box"} if test.box else {}
                return {"active": P1, "party": [{"uuid": P1, "name": "Claude"}, {"uuid": P2, "name": "Karlach"}]}

            def run_job(self, op, args, context="server", wait_s=25.0):
                test.ops.append((op, dict(args)))
                raise AssertionError(f"unexpected job {op}")

            def drain_events(self, ctx):
                return []

        core.game = G()
        world_tools._inp = lambda: self.inp
        core.H.get(P1, "Claude")
        core.H.get(GOB, "Goblin")

        def click_box(rpc, inp, answer="yes", **kw):
            test.ops.append(("click_box", {"answer": answer}))
            test.fled = test.box
            return {"clicked": "Yes", "closed": True, "tries": 1} if test.box else {"gone": True, "tries": 0}

        patcher = mock.patch.object(boxclick, "click_box", click_box)
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self) -> list[str]:
        return [op for op, _ in self.ops if op not in ("state.status", "ui.selected")]

    def test_command_then_yes(self):
        out = combat.flee()
        self.assertIn("fled: Claude left the fight and is at camp; still fighting: Karlach", out)
        self.assertEqual(
            self.names(), ["state.combat", "ui.flee_probe", "ui.flee", "ui.box_open", "click_box", "state.combat"]
        )
        self.assertEqual(self.inp.clicks, [])

    def test_enemy_too_close_is_refused_before_any_ui(self):
        # fair: the distance is to a hostile Claude sees (state.combat lists only seen combatants)
        self.gob_m = 5.5
        out = combat.flee()
        self.assertIn("REFUSED: enemy too close: e2 Goblin at 5.5 m", out)
        self.assertEqual(self.names(), ["state.combat"])

    def test_a_too_close_enemy_is_named_from_the_fight(self):
        # live4 §5: "enemy too close: e7 e7 at 5.5 m": the hostile was never named in a result (its handle had no name)
        self.gob_m = 5.5
        core.H.names.pop(core.H.get(GOB))
        out = combat.flee()
        self.assertIn("REFUSED: enemy too close: e2 Goblin at 5.5 m", out)

    def test_not_in_a_fight(self):
        self.fled = True
        self.assertIn("REFUSED: Claude is not in a fight", combat.flee())

    def test_command_does_nothing_then_the_button_is_clicked(self):
        self.command_works = False
        self.button = {"over_at": (831, 922)}  # under FLEE_SEED
        with mock.patch.object(hoverclick, "PROBE_GAP_S", 0.0):
            out = combat.flee()
        self.assertIn("fled: Claude", out)
        self.assertEqual(self.inp.clicks, [(831, 922)])

    def test_greyed_out_and_no_button(self):
        self.commands = [{"widget": "HotBar", "name": "RequestFlee", "can": False}]
        out = combat.flee()
        self.assertIn("FAILED: the game won't flee now", out)
        self.assertNotIn("ui.flee", self.names())
        self.assertNotIn("click_box", self.names())

    def test_greyed_out_by_the_game_stops_at_once_with_its_reason(self):
        # v010 live window 4: CanFlee false (an enemy within 27 m the party can't see); 20 commands + 48 hover
        # spots, 48 s
        self.flee = {"can": False, "cause": "Enemy too close!", "value": 10.63, "max": 27}
        self.commands = [{"widget": w, "name": "Flee", "can": True} for w in ("HotBar", "PlayerPortraits")]
        self.button = {"over_at": (831, 922)}
        out = combat.flee()
        self.assertIn("FAILED: the game won't flee now (Flee is greyed out: Enemy too close! (10.6/27 m))", out)
        self.assertEqual(self.names(), ["state.combat", "ui.flee_probe"])
        self.assertEqual(self.inp.clicks, [])

    def test_greyed_out_without_a_cause(self):
        self.flee = {"can": False, "value": -1, "max": 27}
        self.assertIn("(Flee is greyed out: the game gives no reason)", combat.flee())

    def test_one_command_name_runs_once(self):
        # window 4: ui.flee ran 20 times (one name on many pages), each followed by the 2 s wait for the question
        self.flee = {"can": True}
        self.command_works = False
        self.commands = [{"widget": f"Page{k}", "name": "Flee", "can": True} for k in range(20)]
        out = combat.flee()
        self.assertIn("FAILED: the game didn't ask to flee", out)
        self.assertEqual([op for op in self.names() if op == "ui.flee"], ["ui.flee"])

    def test_no_question_no_yes(self):
        self.command_works = False
        out = combat.flee()
        self.assertIn("FAILED: the game didn't ask to flee", out)
        self.assertNotIn("click_box", self.names())
        self.commands = []
        self.assertIn("FAILED: no Flee button on the HUD", combat.flee())

    def test_yes_but_still_in_the_fight(self):
        def click_box(rpc, inp, answer="yes", **kw):
            return {"clicked": "Yes", "closed": True, "tries": 1}

        with mock.patch.object(boxclick, "click_box", click_box):
            out = combat.flee()
        self.assertIn("FAILED: pressed Flee (RequestFlee) and answered Yes, but Claude is still in the fight", out)


class FleeRecords(Flee):
    """Every flee call writes its metrics record, a FAILED one with the flee numbers (launch-v1: two ~49 s flee
    failures left no record, so their time couldn't be split between probes, the box wait and the hover search)."""

    def setUp(self):
        super().setUp()
        self.recs: list[dict] = []

        def tool_done(tool, ms, nested=False, ok=True, t1=None, extra=None):
            self.recs.append({"tool": tool, "ok": ok, **(extra or {})})

        patcher = mock.patch.object(core.METRICS, "tool_done", tool_done)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missed_hover_search_has_its_record(self):
        self.command_works = False
        self.button = {"over_at": (5, 5)}  # nowhere near FLEE_SEED: the search never finds it
        with mock.patch.object(hoverclick, "PROBE_GAP_S", 0.0):
            out = combat.flee()
        self.assertIn("FAILED: couldn't click Flee (no cursor position over the button found)", out)
        self.assertEqual(len(self.recs), 1, self.recs)
        rec = self.recs[0]
        self.assertEqual((rec["tool"], rec["ok"]), ("flee", False))
        tries = rec["flee"]["hover"]["tries"]
        self.assertEqual(rec["flee"]["hover"]["outcome"], "no cursor position over the button found")
        self.assertEqual(rec["flee"]["ops"]["ui.flee_probe"][0], tries + 2)  # the first probe, hover's own, each spot
        self.assertNotIn("tries", out)  # numbers for the record, not the agent's text
        probes = [a for op, a in self.ops if op == "ui.flee_probe"]
        self.assertEqual(probes[0], {})  # commands and button
        self.assertTrue(all(a == {"button_only": True} for a in probes[1:]))

    def test_refused_early_return_has_its_record(self):
        self.gob_m = 10.0
        self.assertIn("REFUSED: enemy too close", combat.flee())
        self.assertEqual([(r["tool"], r["ok"]) for r in self.recs], [("flee", False)])

    def test_any_guarded_tool_writes_one_record(self):
        from bg3ai.tools import reporting

        @reporting._guard
        def plain() -> str:
            return "REFUSED: not now"

        @reporting._guard
        def finished() -> str:
            return reporting._finish("finished", {}, "done", 0.0, events=False)

        plain()
        finished()
        self.assertEqual([r["tool"] for r in self.recs], ["plain", "finished"])

    def test_success_writes_one_record(self):
        self.assertIn("fled: Claude", combat.flee())
        self.assertEqual(len(self.recs), 1)
        self.assertEqual(self.recs[0]["flee"]["ops"]["ui.flee_probe"][0], 1)


if __name__ == "__main__":
    unittest.main()
