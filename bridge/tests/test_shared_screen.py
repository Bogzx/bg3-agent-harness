"""Offline tests: a server playing one side of a two-team match (BG3AI_SIDE) shares the one game screen with the other
side, so it makes no clicks or key presses: the input shim is never opened, and every route that would click refuses
at once, before it does anything, with world_tools.SHARED_SCREEN. Without a side each route goes on as before (its
own tests: test_flee, test_reactclick, test_boxclick, test_clicker, test_hotbar_route, test_turn_rpcs ...).

Run: cd bridge && uv run python -m unittest tests/test_shared_screen.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import hotbar, twostep
from bg3ai.game import GameError, JobResult
from bg3ai.render import Handles
from bg3ai.tools import (
    combat,
    core,
    dialogue,
    plumbing,
    queueing,
    reporting,
    turn_plan,
    turn_slots,
    uitools,
    world_tools,
)

P1, P2, FOE = (f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2, 50))
SHARED = world_tools.SHARED_SCREEN


class G:
    """A game that records every op and answers from `replies` / `jobs`."""

    port, pool_cap, job_push = 1, 1, False

    def __init__(self, side: str | None):
        self.side = side
        self.ops: list[str] = []
        self.replies: dict = {
            "state.status": {"party": [{"uuid": P1, "name": "Tav"}], "active": P1, "in_combat": True},
            "state.combat": {
                "active": True,
                "round": 1,
                "current": P1,
                "turn_group": [P1],
                "order": [
                    {"uuid": P1, "name": "Tav", "relation": "party"},
                    {"uuid": FOE, "name": "Lae'zel", "relation": "hostile", "from": {P1: [40.0, 1]}},
                ],
            },
        }
        self.jobs: dict[str, JobResult] = {}

    def try_rpc(self, op, args=None, context="server", default=None):
        self.ops.append(op)
        return self.replies.get(op, default)

    def rpc(self, op, args=None, context="server", **k):
        self.ops.append(op)
        if op not in self.replies:
            raise GameError(f"unknown op '{op}'")
        return self.replies[op]

    def drain_events(self, ctx):
        return []

    def run_job(self, op, args, context="server", wait_s=25.0, **k):
        self.ops.append(op)
        return self.jobs.get(op, JobResult("done", {}))


class Shared(unittest.TestCase):
    def setUp(self):
        saved = (core.H, core.game, queueing._watch_game, dict(world_tools._inp_client))

        def restore():
            core.H, core.game, queueing._watch_game = saved[:3]
            world_tools._inp_client.clear()
            world_tools._inp_client.update(saved[3])

        self.addCleanup(restore)
        core.H = Handles()
        self.shim = object()  # an open input shim: returned by _inp() only without a side
        world_tools._inp_client["c"] = self.shim

    def use(self, side: str | None) -> G:
        core.game = g = G(side)
        return g

    def test_no_input_shim_with_a_side(self):
        self.use(None)
        self.assertIs(world_tools._inp(), self.shim)
        self.use("A")
        self.assertIsNone(world_tools._inp())

    def test_click_on(self):
        g = self.use("A")
        out = world_tools.click_on("e1")
        self.assertIn(f"click_on is {SHARED}", out)
        self.assertEqual(g.ops, [])
        self.use(None)
        world_tools._inp_client["c"] = None
        with mock.patch.object(world_tools, "_inp", lambda: None):
            self.assertIn("input shim not available", world_tools.click_on("e1"))

    def test_click_actions(self):  # loot_corpse, lockpick, disarm, pickpocket's walk-up-and-click
        g = self.use("A")
        with mock.patch("bg3ai.clicker.run_click_action") as run:
            out = world_tools._click_act("loot_corpse", "act.loot_corpse", {"char": P1, "target": FOE})
            self.assertEqual(out.split("\n")[0], f"loot_corpse: FAILED — {SHARED}")
            run.assert_not_called()
            self.assertFalse([op for op in g.ops if op.startswith(("ui.", "act."))], g.ops)
            self.use(None)
            run.return_value = JobResult("done", {"opened": True})
            world_tools._click_act("loot_corpse", "act.loot_corpse", {"char": P1, "target": FOE})
            run.assert_called_once()

    def test_scroll(self):
        g = self.use("A")
        scroll = {"uuid": P2, "name": "Scroll of Revivify", "owner": P1, "cat": "MagicScroll_3"}
        with self.assertRaisesRegex(GameError, f"reading a scroll is {SHARED}"):
            world_tools._use_scroll(P1, scroll, "")
        self.assertEqual(g.ops, [])
        with self.assertRaisesRegex(GameError, f"using Alchemist's Fire is {SHARED}"):  # thrown: its target is clicked
            world_tools._use_scroll(P1, {"uuid": P2, "name": "Alchemist's Fire", "owner": P1, "cat": "Throwable"}, FOE)
        self.assertEqual(g.ops, [])

    def test_potion_is_drunk_in_a_match(self):
        # live 10 m2: "Potion of Healing: FAILED: reading a scroll is not available in a two-team match"
        g = self.use("A")
        potion = {"uuid": P2, "name": "Potion of Healing", "owner": P1, "cat": "PotionHealing"}

        def act(tool, op, args, **k):  # the mod drinks it (ActionsInteract.lua: result.drunk)
            g.ops.append(op)
            return "use_ability: done — drunk"

        with mock.patch.object(plumbing, "_act", act):
            out = world_tools._use_scroll(P1, potion, "")
        self.assertEqual(out, "use_ability: done — drunk")
        self.assertIn("act.interact", g.ops)
        self.assertFalse([op for op in g.ops if op.startswith("ui.") and op != "ui.selected"], g.ops)

    def test_flee(self):
        g = self.use("A")
        out = combat.flee()
        self.assertEqual(out, f"FAILED: flee is {SHARED}")
        self.assertFalse([op for op in g.ops if op.startswith(("ui.", "act."))], g.ops)
        self.use(None)
        with mock.patch.object(world_tools, "_inp", lambda: None):
            self.assertIn("the input helper is not running", combat.flee())

    def test_react_row_click_is_the_one_click_a_side_makes(self):
        """The mod said the prompt is this side's: live L5, Counterspell and Cutting Words answer only to a row click,
        so without it a side could never use them. Every other route stays refused (the tests around this one)."""
        for side in ("A", None):
            self.use(side)
            with mock.patch.object(world_tools, "_inp", lambda own_prompt=False: None):
                self.assertIn("the input shim is not available", dialogue._react_click({"use": True}, 2, 0.0))
        self.use("A")
        self.assertIsNone(world_tools._inp())
        got = []
        with (
            mock.patch.object(world_tools, "_inp", lambda own_prompt=False: got.append(own_prompt) or object()),
            mock.patch(
                "bg3ai.reactclick.click_row",
                lambda rpc, inp, which: {"clicked": which, "at": (0.5, 0.5), "closed": True},
            ),
        ):
            self.assertIn("react: done", dialogue._react_click({"use": True}, 1, 0.0))
        self.assertEqual(got, [True])

    def test_end_turn_goes_through_the_mod_only(self):
        for side, hud_calls in (("A", 0), (None, 1)):
            g = self.use(side)
            g.jobs["act.end_turn"] = JobResult("failed", None, combat.END_IGNORED)
            with (
                mock.patch.object(plumbing, "_active", lambda: P1),
                mock.patch.object(turn_plan, "_party_set", lambda: {P1}),
                mock.patch.object(combat, "END_HUD_FIRST", True),
                mock.patch.object(combat, "_end_turn_by_hud", return_value=JobResult("done", {"ended": True})) as hud,
            ):
                out = combat.end_turn(wait=False)
            self.assertEqual(hud.call_count, hud_calls, side)
            if side:
                self.assertIn(combat.END_IGNORED, out)

    def test_end_turn_waits_out_the_mods_group_end(self):
        """The mod's last end of a turn group settles up to 20 s later (S087: 18 s): with a side the job is waited on
        that long, not answered "not finished" at 15 s; without a side the wait is 15 s as before."""
        for side, want_s in (("A", combat.END_ARENA_WAIT_S), (None, 15)):
            g = self.use(side)
            waits = []

            def run_job(op, args, context="server", wait_s=25.0, waits=waits, **k):
                waits.append(wait_s)  # a job that settles 18 s after it starts
                return JobResult("done", {"ended": True}) if wait_s >= 18 else JobResult("still_running", None)

            g.run_job = run_job
            with (
                mock.patch.object(plumbing, "_active", lambda: P1),
                mock.patch.object(turn_plan, "_party_set", lambda: {P1}),
            ):
                out = combat.end_turn(wait=False)
            self.assertEqual(waits, [want_s], side)
            self.assertEqual(reporting._batch["last"], "done" if side else "still_running", out)

    def test_passing_members_end_waits_out_the_mods_group_end(self):
        for side, want_s in (("A", combat.END_ARENA_WAIT_S), (None, 5)):
            g = self.use(side)
            waits = []
            g.run_job = lambda op, args, wait_s=25.0, waits=waits, **k: waits.append(wait_s) or JobResult("done", {})
            row = {"uuid": P1, "passes": "Stunned", "is_current": True, "ended": True, "relation": "party"}
            cs = {"current": P1, "order": [row]}  # S087: a Stunned member's open turn the game itself passes
            turn_slots._end_passing(cs, -1e9, set(), None)
            self.assertEqual(waits, [want_s], side)

    def test_multi_target_cast_takes_no_hotbar_route(self):
        for side, routed in (("A", False), (None, True)):
            self.use(side)
            acts: list[dict] = []
            with (
                mock.patch.object(hotbar, "HOTBAR_CAST_ENABLED", True),
                mock.patch.object(twostep, "TWOSTEP_ENABLED", True),
                mock.patch.object(plumbing, "_who", lambda w: P1),
                mock.patch.object(plumbing, "_uuid", lambda r: r),
                mock.patch("bg3ai.tools.overview._ability_id", lambda c, a: f"Target_{a}"),
                mock.patch("bg3ai.tools.overview._own_attack", lambda c, a: (a, "")),
                mock.patch.object(combat, "_hotbar_use", return_value="use_ability: done (hotbar)") as hb,
                mock.patch.object(combat, "_twostep_use", return_value="use_ability: done (two-step)") as ts,
                mock.patch.object(
                    plumbing, "_act", lambda *a, acts=acts, **k: acts.append(a[2]) or "use_ability: done"
                ),
            ):
                combat.use_ability("Bless", targets="a,b,c", wait=False)
                combat.use_ability("Revivify", target="a", wait=False)
            self.assertEqual((hb.called, ts.called), (routed, routed), side)
            if not routed:  # as with the routes off: the 2-target cast, and Revivify to the mod (which refuses it)
                self.assertEqual(acts[0]["targets"], ["a", "b"])
                self.assertEqual(acts[1]["spell"], "Target_Revivify")

    def test_box_button_click(self):
        for side in ("A", None):
            self.use(side)

            def act(tool, op, args, **k):
                reporting._batch["result"] = {"needs_click": "Yes"}
                return "confirm: FAILED — the button press didn't land"

            with mock.patch.object(plumbing, "_act", act), mock.patch.object(world_tools, "_inp", lambda: None):
                out = world_tools._box_act("confirm", "ui.confirm", {"answer": "yes"}, wait_s=1)
            self.assertEqual(f"clicking the box's button is {SHARED}" in out, side == "A", out)

    def test_raw_input(self):
        g = self.use("A")
        self.assertEqual(uitools.press_key("SPACE"), f"press_key: FAILED — {SHARED}")
        self.assertIn(f"click is {SHARED}", uitools.click(0.5, 0.5))
        self.assertEqual(g.ops, [])


if __name__ == "__main__":
    unittest.main()
