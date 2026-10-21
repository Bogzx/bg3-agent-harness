"""Offline tests: use_ability / combat_turn on >2 targets through the hotbar route (audit ACT-8, act half), and the
END_HUD_FIRST switch (proposal "ignored EndTurn -> HUD at once"). hotbar.hotbar_cast is faked: its own clicks are
test_hotbar.py's.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_hotbar_route.py'
"""

from __future__ import annotations

import json
import unittest

from test_turn_macros import GOB, IMP, P1, P2, Base, combat  # noqa: F401 (sets BG3AI_RUNTIME first)

from bg3ai import hotbar
from bg3ai.tools import core, reporting, world_tools, combat as combat_tools, batch, turn_tool
from bg3ai.game import GameError


class FakeHotbar:
    """Stands in for hotbar.hotbar_cast: records the call and whether the mod's job was running then."""

    def __init__(self, g, ok=True, reason=None, stage=None, picked=None, raises=False):
        self.g, self.ok, self.reason, self.stage, self.picked, self.raises = g, ok, reason, stage, picked, raises
        self.calls: list[tuple] = []
        self.kw: list[dict] = []
        self.began = False   # the mod's ui job saw CastSpell and then went interrupted (Actions.lua on_cancel)

    def __call__(self, game, inp, caster, spell, targets, select=True, should_stop=None):
        if select and self.g.active != caster:
            game.run_job("act.select", {"char": caster}, wait_s=5)   # like hotbar.arm
        running = [j for j in self.g.jobs.values() if j["op"] == "act.cast" and j["status"] == "running"]
        self.calls.append((caster, spell, list(targets), [j["args"].get("ui") for j in running]))
        self.kw.append({"select": select, "should_stop": should_stop})
        if self.began:
            for j in running:
                j["status"], j["result"], j["error"] = "interrupted", {"began": True}, "cancelled"
        if isinstance(self.raises, Exception):
            raise self.raises
        if self.raises:
            raise GameError("ui.hotbar_slot: no such op")
        n = len(targets) if self.picked is None else self.picked
        return hotbar.HotbarCastResult(self.ok, reason=self.reason, stage=self.stage, picked=n, elapsed_s=1.2,
                                       budget_s=1.0 + 0.5 * len(targets),
                                       detail={"why": f"{self.reason} (fake)"} if self.reason else {})


class HotbarRoute(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat()
        self.g.watch["combat"] = True
        self.g.dur["act.cast"] = 0.05
        self.g.on_start = lambda op, a: self.g.combat.update(current=GOB) if op == "act.end_turn" else None
        saved = (hotbar.HOTBAR_CAST_ENABLED, hotbar.hotbar_cast, world_tools._inp)
        self.addCleanup(lambda: (setattr(hotbar, "HOTBAR_CAST_ENABLED", saved[0]),
                                 setattr(hotbar, "hotbar_cast", saved[1]), setattr(world_tools, "_inp", saved[2])))
        world_tools._inp = lambda: object()
        self.fake = FakeHotbar(self.g)
        hotbar.hotbar_cast = self.fake
        self.p1 = core.H.get(P1)

    def bless3(self, **kw) -> str:
        return combat_tools.use_ability("Target_Bless", targets=[self.p1, self.gob, self.imp], who=self.p1, **kw)

    def cast_args(self) -> list[dict]:
        return [a for _, a in self.g.ops("act.cast")]

    def test_off_casts_on_two_and_says_so(self):
        hotbar.HOTBAR_CAST_ENABLED = False
        out = self.bless3()
        self.assertEqual(self.fake.calls, [])
        (a,) = self.cast_args()
        self.assertEqual(a["targets"], [P1, GOB])
        self.assertNotIn("ui", a)
        self.assertIn("targets: cast on the first 2 only, 1 of 3 NOT targeted (the hotbar route for more is off)", out)
        self.assertIn("left out: ", out)

    def test_on_three_targets_go_the_hotbar_route_once_the_job_runs(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        out = self.bless3()
        (a,) = self.cast_args()
        self.assertTrue(a["ui"])
        self.assertEqual(a["targets"], [P1, GOB, IMP])
        self.assertNotIn("target", a)
        # the clicks ran while the mod's ui job was watching (one running act.cast, ui=true)
        self.assertEqual(self.fake.calls, [(P1, "Target_Bless", [P1, GOB, IMP], [True])])
        self.assertEqual(self.g.ops("job.cancel"), [])
        self.assertIn("use_ability: done", out)
        self.assertIn("hotbar: 3 of 3 targets clicked in 1.2s", out)
        self.assertNotIn("first 2 only", out)

    def test_on_target_plus_targets_and_repeats_keep_click_order(self):
        # Magic Missile: the same target twice = 2 darts
        hotbar.HOTBAR_CAST_ENABLED = True
        combat_tools.use_ability("Target_Bless", target=self.gob, targets=[self.gob, self.imp], who=self.p1)
        self.assertEqual(self.cast_args()[0]["targets"], [GOB, GOB, IMP])

    def test_on_two_targets_stay_osiris(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        combat_tools.use_ability("Target_Bless", targets=[self.gob, self.imp], who=self.p1)
        (a,) = self.cast_args()
        self.assertNotIn("ui", a)
        self.assertEqual(self.fake.calls, [])

    def test_on_upcast_stays_osiris_and_says_why(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        out = self.bless3(upcast=2)
        (a,) = self.cast_args()
        self.assertNotIn("ui", a)
        self.assertIn("an upcast or a position goes by the 2-target cast", out)

    def test_off_three_beams_on_one_target_cast_at_it_alone(self):
        # S066 #59 r1 Wyll: Eldritch Blast targets=[e356]x3 → UseSpell(Orin, Orin): "the game never started it"
        hotbar.HOTBAR_CAST_ENABLED = False
        out = combat_tools.use_ability("Projectile_EldritchBlast", targets=[self.gob] * 3, who=self.p1)
        (a,) = self.cast_args()
        self.assertEqual(a["target"], GOB)
        self.assertNotIn("targets", a)
        self.assertIn(f"targets: 3 picks on {self.gob}", out)
        self.assertIn("cast at it alone (one target takes every beam/dart)", out)
        self.assertNotIn("NOT targeted", out)
        self.assertNotIn("left out", out)

    def test_two_beams_on_one_target_too_and_a_later_one_is_named(self):
        hotbar.HOTBAR_CAST_ENABLED = False
        combat_tools.use_ability("Projectile_EldritchBlast", target=self.gob, targets=[self.gob], who=self.p1)
        out = combat_tools.use_ability("Projectile_EldritchBlast", targets=[self.gob, self.gob, self.imp], who=self.p1)
        a1, a2 = self.cast_args()
        self.assertEqual((a1["target"], a1.get("targets")), (GOB, None))
        self.assertEqual((a2["target"], a2.get("targets")), (GOB, None))
        self.assertIn(f"left out: {self.imp}", out)

    def test_distinct_picks_keep_the_two_target_cast(self):
        hotbar.HOTBAR_CAST_ENABLED = False
        combat_tools.use_ability("Projectile_EldritchBlast", targets=[self.gob, self.imp, self.gob], who=self.p1)
        (a,) = self.cast_args()
        self.assertEqual(a["targets"], [GOB, IMP])

    def test_route_failure_with_one_target_repeated_falls_back_to_it_alone(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.ok, self.fake.reason, self.fake.stage = False, "cell_not_found", "locate"
        self.g.on_start = lambda op, a: self.g.dur.update({"act.cast": 0.05}) if a.get("ui") is None else None
        out = combat_tools.use_ability("Projectile_EldritchBlast", targets=[self.gob] * 3, who=self.p1)
        a1, a2 = self.cast_args()
        self.assertEqual(a1["targets"], [GOB] * 3)        # the route clicks the repeats (valid by hand)
        self.assertEqual((a2["target"], a2.get("targets")), (GOB, None))
        self.assertIn("cast at it alone", out)

    def test_route_failure_cancels_and_casts_on_two_by_osiris(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0   # the ui job would watch for a long time: the cancel ends it
        self.fake.ok, self.fake.reason, self.fake.stage = False, "cell_not_found", "locate"
        self.g.on_start = lambda op, a: self.g.dur.update({"act.cast": 0.05}) if a.get("ui") is None else None
        out = self.bless3()
        a1, a2 = self.cast_args()
        self.assertTrue(a1["ui"])
        self.assertNotIn("ui", a2)
        self.assertEqual((a2["target"], a2["targets"]), (P1, [GOB]))
        self.assertEqual(len(self.g.ops("job.cancel")), 1)
        self.assertEqual(len(self.fake.calls), 1)   # the Osiris retry doesn't click
        self.assertIn("use_ability: done", out)
        self.assertIn("the hotbar route failed at locate: cell_not_found (fake)", out)
        self.assertIn("cast on the first 2 only", out)

    def test_hotbar_error_falls_back_too(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.fake.raises = True
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 2)
        self.assertIn("the hotbar route failed at slot", out)

    def test_fired_early_is_not_cast_again(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.fake.ok, self.fake.reason, self.fake.stage, self.fake.picked = False, "fired_early", "target", 2
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 1)
        self.assertEqual(self.g.ops("job.cancel"), [])
        self.assertIn("hotbar: 2 of 3 targets clicked", out)
        self.assertIn("the game cast after 2 (the spell's max)", out)

    def test_mod_refusal_means_no_clicks_and_no_retry(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.refuse_if = lambda op, a: "it is not this character's turn" if op == "act.cast" else None
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 1)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("FAILED — it is not this character's turn", out)

    def test_combat_turn_step_takes_the_route(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        out = turn_tool.combat_turn(json.dumps({"action": {"ability": "Target_Bless",
                                                        "targets": [self.p1, self.gob, self.imp]}}))
        a = self.cast_args()[0]
        self.assertTrue(a["ui"])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("action: Target_Bless", out)
        self.assertNotIn("FAILED", out)

    # --- review fixes (2026-10-16): H1 select first, M1 stop hook, M2 any exception, M3 no second cast

    def test_supersede_rule_of_the_fake(self):
        self.g.supersede = True
        jid = self.g.rpc("act.cast", {"char": P1, "spell": "Target_Bless", "ui": True})["job"]
        self.g.run_job("act.select", {"char": P1})
        self.assertEqual(self.g.jobs[jid]["status"], "interrupted")
        self.assertEqual(self.g.jobs[jid]["error"], "superseded by a new action")

    def test_h1_unselected_caster_is_selected_before_the_ui_job(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.supersede = True
        self.g.active = P2
        out = self.bless3()
        names = [op for op, _ in self.g.calls if op in ("act.select", "act.cast")]
        self.assertEqual(names, ["act.select", "act.cast"])
        self.assertEqual(self.fake.kw[0]["select"], False)
        self.assertEqual(self.fake.calls[0][3], [True])        # the ui job still ran while the clicks went in
        self.assertIn("use_ability: done", out)
        self.assertNotIn("superseded", out)
        self.assertIn("selected e", out)

    def test_h1_selected_caster_no_select(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.supersede = True
        self.bless3()
        self.assertEqual(self.g.ops("act.select"), [])

    def test_m1_stop_hook_follows_the_executor(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.assertFalse(combat_tools._click_stop()())               # a plain call: nothing to follow
        out = batch.do_actions(json.dumps([{"tool": "use_ability", "ability": "Target_Bless",
                                             "targets": [self.p1, self.gob, self.imp], "who": self.p1}]))
        stop = self.fake.kw[0]["should_stop"]
        self.assertFalse(stop())
        self.assertNotIn("FAILED", out)

        class Ex:
            interrupt = None
            stop = __import__("threading").Event()
        ex = Ex()
        reporting._batch["exec"] = ex
        try:
            s = combat_tools._click_stop()
            self.assertFalse(s())
            ex.interrupt = ("combat", "combat started")
            self.assertTrue(s())
        finally:
            reporting._batch["exec"] = None

    def test_m2_input_error_cancels_and_is_not_cast_again(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.raises = ConnectionError("input shim closed the connection")
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 1)             # stage unknown: no Osiris redo
        self.assertEqual(len(self.g.ops("job.cancel")), 1)
        self.assertIn("route failed at error (ConnectionError: input shim closed the connection)", out)

    def test_m3_failure_after_the_targets_is_not_cast_again(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.g.dur["act.cast"] = 5.0
        self.fake.ok, self.fake.reason, self.fake.stage = False, "not_started", "start"
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 1)
        self.assertIn("not cast again by Osiris", out)
        self.assertNotIn("first 2 only", out)

    def test_m3_began_cast_is_not_cast_again(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.fake.ok, self.fake.reason, self.fake.stage = False, "not_armed", "arm"
        self.fake.began = True
        out = self.bless3()
        self.assertEqual(len(self.cast_args()), 1)
        self.assertIn("not cast again by Osiris", out)

    def test_m3_cancelled_route_is_not_cast_again(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        self.fake.ok, self.fake.reason, self.fake.stage = False, "cancelled", "arm"
        self.bless3()
        self.assertEqual(len(self.cast_args()), 1)

    def test_cast_log_line(self):
        hotbar.HOTBAR_CAST_ENABLED = True
        log = core.RUNTIME / "hotbar_cast.jsonl"
        n0 = len(log.read_text().splitlines()) if log.exists() else 0
        self.bless3()
        rows = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertEqual(len(rows), n0 + 1)
        self.assertEqual((rows[-1]["spell"], rows[-1]["ok"], rows[-1]["picked"]), ("Target_Bless", True, 3))


class EndHudFirst(Base):
    """Shared turn group: Lae'zel (P2) ended first (requested), Claude (P1) still to act."""

    def setUp(self) -> None:
        super().setUp()
        cs = combat()
        cs["turn_group"] = [P1]
        for o in cs["order"]:
            if o["uuid"] in (P1, P2):
                o["is_current"] = True
            if o["uuid"] == P2:
                o["ended"] = True
        self.g.combat = cs
        self.g.watch["combat"] = True
        saved = combat_tools.END_HUD_FIRST
        self.addCleanup(setattr, combat_tools, "END_HUD_FIRST", saved)
        self.pressed: list = []
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.command":
                self.pressed.append(args)
                if self.hud_works:
                    self.g.combat.update(current=GOB, turn_group=[GOB])
                return {"done": True}
            if op == "act.end_turn":
                self.g.combat.update(current=GOB, turn_group=[GOB])
            return real(op, args, context, timeout)
        self.hud_works = True
        self.g.rpc = rpc

    def test_off_orders_osiris(self):
        combat_tools.END_HUD_FIRST = False
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)
        self.assertEqual(self.pressed, [])
        self.assertIn("end_turn", out)

    def test_on_presses_the_hud_at_once(self):
        combat_tools.END_HUD_FIRST = True
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertEqual(self.g.ops("act.end_turn"), [])
        self.assertEqual(self.pressed, [{"widget": "*", "command": "EndTurn"}])
        self.assertIn("the HUD's End Turn at once (e", out)
        self.assertNotIn("FAILED", out)

    def test_on_without_a_peer_that_ended_orders_osiris(self):
        combat_tools.END_HUD_FIRST = True
        for o in self.g.combat["order"]:
            o.pop("ended", None)
        self.g.combat["turn_group"] = [P1, P2]
        combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)
        self.assertEqual(self.pressed, [])

    def test_on_hud_that_does_nothing_falls_back_to_osiris(self):
        combat_tools.END_HUD_FIRST = True
        self.hud_works = False
        saved, combat_tools.END_HUD_WAIT_S = combat_tools.END_HUD_WAIT_S, 0.2
        self.addCleanup(setattr, combat_tools, "END_HUD_WAIT_S", saved)
        out = combat_tools.end_turn(who=core.H.get(P1), wait=False)
        self.assertEqual(len(self.pressed), 1)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)
        self.assertNotIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
