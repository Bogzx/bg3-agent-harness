"""Offline tests: combat_turn moving into reach (within, routes stopping short, line of sight) and its fallbacks
(ACT-1 the planned target's side, ACT-2 who was left out, ACT-3 a refused swing steps closer). Fixtures:
test_turn_macros.py."""

from __future__ import annotations

import json
import threading
import unittest

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    CHEST,
    GOB,
    IMP,
    NOT_JOBS,
    P1,
    P2,
    CombatTurnBase,
    batch,
    combat,
    combat_tools,
    core,
    turn,
    turn_slots,
    turn_tool,
    uitools,
)


class CombatTurn(CombatTurnBase):
    # S002: "move within 1.5 m of Commander Zhalk" said done 6-16 m away, then the melee swing failed.
    def _walk_to(self, dist_after: float) -> None:
        def advance(op, args):
            if op == "act.move_to":
                for o in self.g.combat["order"]:
                    if o["uuid"] == GOB:
                        o["from"][P1] = [dist_after, 1]
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = advance

    def test_within_follows_the_route_and_says_why_it_stopped_short(self):
        self.g.combat = combat(gob_dist=16.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.8, "need_m": 18.5,
                           "route_m": 20.0, "limit": "movement", "movement_left": 9.0, "end_m": 9.9}
        self._walk_to(9.9)
        self.g.refuse_if = lambda op, a: ("target is out of range (9.9 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_MainHandAttack" else None)
        self.g.actions = [{"spell": "Target_MainHandAttack"}, {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("nav.approach")[0][1]["within"], 1.5)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [-4.0, 0.0, 5.0])
        self.assertIn("now 9.9m from", out)
        self.assertIn("stopped short of ≤1.5m: out of movement (9m left, the route needs 18.5m)"
                      " — melee is out of reach this turn", out)
        # acceptance 13:3x: no melee attack sent into a sure range miss; skipped, then the ranged fallback
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])
        self.assertIn(f"skipped ({self.gob} Goblin is out of range for melee after the move (9.9m))", out)
        self.assertIn(f"action (auto fallback): Projectile_MainHandAttack {self.gob}: done", out)

    def test_tight_route_short_by_a_metre_skips_melee_for_the_planned_ranged(self):
        # acceptance 13:3x: "12m left, the route needs 12m" ended 3.1 m away; the plan's own ranged alt runs
        self.g.combat = combat(gob_dist=13.9)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 12.0, "need_m": 12.0,
                           "limit": "movement", "movement_left": 12.0}
        self._walk_to(3.1)
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])
        self.assertIn("melee is out of reach this turn", out)
        self.assertNotIn("FAILED", out)

    def test_route_ends_short_skips_a_useless_dash(self):
        # live 14:5x: "no walkable route gets closer than 2.5m", in_reach=false; Dash + walk on stood still
        self.g.combat = combat(gob_dist=8.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 5.0, "limit": "route_end",
                           "end_m": 2.5}
        self.g.result_for = lambda op, a: ({"dist": 2.9, "target": GOB, "in_reach": False} if op == "act.move_to"
                                           else {"dist": 1.0})
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("melee is out of reach this turn", out)
        self.assertIn("Shout_Dash: skipped (more movement gets no closer", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_blocked_melee_walk_skips_melee_and_the_dash(self):
        # live 15:1x: "move e74: FAILED — could not get closer to the destination — dist=3.5"
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.0, "need_m": 4.0,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=3.5"
                                          if op == "act.move_to" else None)
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("melee is out of reach this turn", out)
        self.assertIn("is out of range for melee after the move (3.5m)", out)
        self.assertIn("Shout_Dash: skipped (more movement gets no closer", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_walk_that_never_starts_runs_the_rest_from_here(self):
        # S074 23:21:13: Claude's move stood still (Lethargic) 5.1 s, "no path"; the mod now gives up after 2.5 s
        self.g.combat = combat(gob_dist=8.0)
        self.g.refuse_if = lambda op, a: ("never started moving (2.5 s, the route was there); the character is "
                                          "Lethargic" if op == "act.move_to" else None)
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn("never started moving", out)
        self.assertIn("melee is out of reach this turn", out)
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])

    def test_a_lethargic_member_stays_put_at_once(self):
        # S091 08:30:24: Claude Lethargic (`DISABLED … Lethargic` beside `Move 9/9m`), the plan walked off the
        # Orb of Negation's platform: no walk is sent, the plan goes on from where he stands
        self.g.combat = combat(gob_dist=8.0)
        self.g.combat["order"][0]["disabled"] = [{"name": "Lethargic", "turns": 1,
                                                 "effect": "cannot move; its turn passes once the rest of its turn"
                                                           " group has ended (act with it first)"}]
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn(f"move {self.gob}: stays put — Lethargic: cannot move", out)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])

    def test_a_walk_refused_as_blocked_runs_the_rest_from_here(self):
        # the mod's own refusal (Actions.lua combat_budget, Guard.move_blocked) when no kept combat read said so
        self.g.combat = combat(gob_dist=8.0)
        self.g.refuse_if = lambda op, a: ("cannot move while Lethargic (the status keeps it in place this turn)"
                                          if op == "act.move_to" else None)
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn("cannot move while Lethargic", out)
        self.assertIn("melee is out of reach this turn", out)
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])

    def test_turn_passing_during_the_walk_stops_the_plan(self):
        # S074 23:21:50: "action: it is no longer e5 Claude's turn" came only from the next step; the walk says it now
        self.g.combat = combat(gob_dist=8.0)
        self.g.refuse_if = lambda op, a: ("not this character's turn any more: the game passed the turn on during "
                                          "the walk (travelled 0.0 m)" if op == "act.move_to" else None)
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn("the game passed the turn on during the walk", out)
        self.assertEqual(self.casts(), [])

    def test_blocked_spot_beside_target_walks_the_route_instead(self):
        # S032 17:44: post-Dash walk to the spot beside e743 ended where it started (5.7 m, 9 m unspent)
        self.g.combat = combat(gob_dist=5.7)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.5, "need_m": 4.5,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=5.7"
                                          if op == "act.move_to" and a.get("target") else None)
        self._walk_to(1.2)
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        moves = self.g.ops("act.move_to")
        self.assertEqual(moves[-1][1]["pos"], [-4.0, 0.0, 5.0])
        # naut3 15:40:52: the range asked for was reached, so the move line says done, not "FAILED — could not …"
        self.assertIn(f"move {core.H.get(GOB)}: done", out)
        self.assertIn("now 1.2m from", out)
        self.assertIn("(along the route; the walk to the spot beside it stopped 5.7m away)", out)
        self.assertNotIn("could not get closer", out)
        self.assertNotIn("melee is out of reach", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack"])

    def test_route_walk_that_stays_out_of_range_keeps_the_first_failure(self):
        self.g.combat = combat(gob_dist=5.7)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.5, "need_m": 4.5,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=5.7"
                                          if op == "act.move_to" and a.get("target") else None)
        self._walk_to(3.5)
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}))
        self.assertIn("FAILED — could not get closer to the destination", out)
        self.assertIn("→ along the route: done", out)

    def test_melee_short_but_within_reach_pad_still_attacks(self):
        # 1.5 m wanted, 2.2 m centre to centre: inside the edge-to-edge reach, the game decides
        self.g.combat = combat(gob_dist=10.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.0, "need_m": 8.7,
                           "limit": "movement", "movement_left": 8.0}
        self._walk_to(2.2)
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.casts(), ["Target_MainHandAttack"])
        self.assertNotIn("melee is out of reach", out)

    def test_move_target_vanished_retargets_the_plan(self):
        # acceptance 13:3x: the Shadow went into Shadow Blend during Claude's walk: both attacks skipped
        # 'not visible', the Dash walked nowhere, 9 m unused
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.discard(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.8, 1]
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash"]))
        self.assertIn(f"target: {self.gob} Goblin is out of sight → the plan now aims at the nearest visible "
                      f"hostile {self.imp} Imp (1.8m)", out)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [IMP])
        self.assertEqual(len(self.g.ops("act.move_to")), 2)  # walked on toward the imp (movement left)

    def test_retarget_without_movement_skips_melee_on_the_far_new_target(self):
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.discard(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        self.g.result_for = lambda op, a: {"dist": 1.0, "after": {"movement": 0, "action": 1}}
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertEqual(len(self.g.ops("act.move_to")), 1)  # no movement left: no walk on
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Projectile_MainHandAttack", IMP)])
        self.assertIn(f"{self.imp} Imp is out of range for melee after the move (12m)", out)

    def test_move_target_dead_before_the_turn_aims_at_the_nearest(self):
        self.g.dead.add(GOB)
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("is dead → the plan now aims at the nearest visible hostile", out)
        self.assertEqual(self.g.ops("act.move_to")[0][1].get("target"), IMP)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [IMP])
        core.H.get(GOB, "Goblin")

    def test_move_target_gone_and_nobody_in_sight_skips_the_dash(self):
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.clear()
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("no other hostile is in sight", out)
        self.assertIn("Shout_Dash: skipped (the move's target", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_target_gone_before_the_turn_no_walk_one_line(self):
        # S028: "target: e742 Shadow is out of sight ... skipped" x3, and the move still ran (FAILED not visible)
        self.g.visible.clear()
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        out = turn_tool.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dodge"]))
        self.assertEqual(out.count("no other hostile is in sight"), 1, out)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_retarget_false_keeps_a_vanished_move_target(self):
        self.g.dead.add(GOB)
        out = turn_tool.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"], retarget=False))
        self.assertNotIn("the plan now aims", out)

    def test_short_move_without_ranged_dashes_and_walks_on(self):
        self.g.combat = combat(gob_dist=16.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.8, "need_m": 14.0,
                           "limit": "movement", "movement_left": 9.0}
        self._walk_to(6.0)
        self.g.refuse_if = lambda op, a: ("target is out of range (6 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_MainHandAttack" else None)
        self.g.actions = [{"spell": "Target_MainHandAttack"}, {"spell": "Shout_Dash"}]
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.casts(), ["Shout_Dash"])  # melee skipped: out of reach after the move
        self.assertEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn("action (auto fallback): Shout_Dash: done", out)

    def test_no_route_closer_stays_put_and_says_so(self):
        self.g.combat = combat(gob_dist=8.0)
        self.g.approach = {"reachable": True, "pos": [0.0, 0.0, 0.0], "walk_m": 0.2, "limit": "route_end",
                           "end_m": 7.8}
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}, action=["Shout_Dodge"]))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("stays put, 8m away — no walkable route gets closer than 7.8m", out)

    def test_fire_on_the_route_is_the_reason_and_avoid_false_is_passed(self):
        self.g.combat = combat(gob_dist=10.0)
        self.g.approach = {"reachable": True, "pos": [-3.0, 0.0, 0.0], "walk_m": 3.0, "limit": "hazard",
                           "hazard": {"surface": "Fire", "at_m": 4.2}}
        self._walk_to(7.0)
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}, action=["Shout_Dodge"]),
                                 end=False)
        self.assertIn("stopped short of ≤1.5m: kept out of Fire on the route 4.2m ahead", out)
        turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 1.5, "avoid": False},
                                     action=["Shout_Dodge"]), end=False)
        self.assertIs(self.g.ops("nav.approach")[-1][1]["avoid"], False)

    def test_dash_then_move_again(self):
        n = {"moves": 0}

        def refuse(op, a):
            if op == "act.move_to":
                n["moves"] += 1
                return "no movement left" if n["moves"] == 1 else None
            return None
        self.g.refuse_if = refuse
        out = turn_tool.combat_turn(self.plan(move=self.gob, action=[{"ability": "Shout_Dash", "then_move": True}]))
        self.assertEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn(f"move {self.gob}: FAILED — no movement left", out)
        self.assertIn("action: Shout_Dash: done", out)
        self.assertTrue(out.index("action:") < out.rindex(f"move {self.gob}: done"))

    def test_plain_dash_walks_the_move_on(self):
        # acceptance 12:5x: "Dash" (no then_move) after a move out of movement dashed and stood still
        n = {"moves": 0}

        def refuse(op, a):
            if op == "act.move_to":
                n["moves"] += 1
                return "no movement left" if n["moves"] == 1 else None
            return None
        self.g.refuse_if = refuse
        turn_tool.combat_turn(self.plan(move=self.gob, action=["Shout_Dash"]))
        self.assertEqual(len(self.g.ops("act.move_to")), 2)

    def test_dash_then_move_false_stays(self):
        p = turn.parse_plan(json.dumps({"move": "e1", "action": [{"ability": "Dash", "then_move": False}, "Dash",
                                                                  "Shout_Dash_CunningAction", "Shout_Dodge"]}),
                            batch._BATCHABLE)
        self.assertEqual([a.then_move for a in p.slots["action"]], [False, True, True, False])

    def test_plan_level_avoid_folds_into_move(self):
        # S061: {"move": ..., "avoid": false} → "unknown plan key(s) avoid" (our refusal says "move avoid:false")
        p = turn.parse_plan(json.dumps({"move": "e1", "avoid": False, "action": ["Dash"]}), batch._BATCHABLE)
        self.assertFalse(p.move.avoid)
        p = turn.parse_plan(json.dumps({"move": {"target": "e1", "avoid": True}, "avoid": False}), batch._BATCHABLE)
        self.assertTrue(p.move.avoid)  # the move's own key wins
        p = turn.parse_plan(json.dumps({"avoid": False, "action": ["Dash"]}), batch._BATCHABLE)
        self.assertIsNone(p.move)

    def test_point_target_becomes_xyz(self):
        # S034 Balthazar: Fireball {"target": "-828,8.5,-783"} → "unknown target" ×N, the agent's lesson said
        # combat_turn can't aim point spells
        p = turn.parse_plan(json.dumps({"action": [{"ability": "Fireball", "target": "-828,8.5,-783"},
                                                   {"ability": "Fireball", "target": [1, 2, 3]},
                                                   {"ability": "Fireball", "pos": [4, 5, 6]},
                                                   {"ability": "Fireball", "x,y,z": "7,8,9"}]}),
                            batch._BATCHABLE)
        args = [a.step.args for a in p.slots["action"]]
        self.assertEqual((args[0].get("x"), args[0].get("y"), args[0].get("z")), (-828.0, 8.5, -783.0))
        self.assertNotIn("target", args[0])
        self.assertEqual((args[1]["x"], args[2]["z"], args[3]["y"]), (1.0, 6.0, 8.0))
        turn_tool.combat_turn(self.plan(action={"ability": "Shout_Dodge", "target": "1,0,1"}))
        self.assertEqual([a.get("pos") for _, a in self.g.ops("act.cast")][:1], [[1.0, 0.0, 1.0]])

    def test_reaction_prompt_answered_by_policy(self):
        self.g.dur["act.cast"] = 0.4

        def prompt(op, args):
            if op == "act.cast":
                threading.Timer(0.05, lambda: self.g.ui.update(screen="reaction")).start()
            if op == "ui.react":
                self.g.ui["screen"] = "game"
        self.g.on_start = prompt
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           on_reaction="use"), end=False)
        self.assertEqual(self.g.ops("ui.react")[0][1], {"use": True})
        self.assertIn("reaction prompt: used", out)
        self.assertIn(f"action: Target_MainHandAttack {self.gob}: done", out)
        self.assertNotIn("STOPPED", out)

    def test_reaction_prompt_stops_by_default(self):
        self.g.dur["act.cast"] = 0.4
        self.g.on_start = lambda op, a: op == "act.cast" and threading.Timer(
            0.05, lambda: self.g.ui.update(screen="reaction")).start()
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("STOPPED: reaction on screen", out)
        self.assertEqual(self.g.ops("ui.react"), [])

    # S094 10:05:14: a reaction prompt already open when a turn starts was the watcher's baseline screen, so the turn
    # played into it (Wyll's move 'never started moving', Dash 'never started', the turn lost).
    def test_a_reaction_prompt_open_at_the_start_plays_nothing(self):
        self.g.ui.update(screen="reaction", reaction={
            "prompt": "Mind Flayer used Tentacle Whip on Claude. | Choose how to react. | 77/205 | Shield Blow"})
        out = turn_tool.combat_turn(self.plan(move=self.gob, action={"ability": "Shout_Dash"}))
        self.assertTrue(out.startswith(turn_tool.TURN_NOT_YET), out)
        self.assertIn("e4 Claude's turn not started: reaction on screen: Mind Flayer used Tentacle Whip on Claude.",
                      out)
        self.assertIn("Nothing was played or spent", out)
        self.assertEqual([op for op, _ in self.g.calls if op.startswith("act.") and op != "act.watch"], [])
        self.assertEqual(self.g.ops("ui.react"), [])

    def test_a_reaction_prompt_open_at_the_start_is_answered_by_the_plan(self):
        self.g.ui["screen"] = "reaction"
        self.g.on_start = lambda op, a: op == "ui.react" and self.g.ui.update(screen="game")
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           on_reaction="skip"), end=False)
        self.assertEqual(self.g.ops("ui.react")[0][1], {"use": False})
        self.assertIn("reaction prompt: declined", out)
        self.assertIn(f"action: Target_MainHandAttack {self.gob}: done", out)
        self.assertNotIn("STOPPED", out)

    def test_a_chain_stops_on_the_prompt_the_last_turn_left_open(self):
        # S094 [.., Claude, Wyll, the Emperor]: Claude's walk opened Shield Blow as his turn ended; Wyll and the
        # Emperor were played into it. Now the chain stops at Wyll, nothing of his sent.
        self.g.combat["turn_group"] = [P1, P2]

        def end(op, args):
            if op == "act.end_turn" and args.get("char") == P1:
                self.g.combat["turn_group"] = [P2]
                self.g.ui.update(screen="reaction", reaction={"prompt": "Goblin attacked Claude. | Shield Blow"})
        self.g.on_start = end
        out = turn_tool.combat_turn(json.dumps([
            {"who": core.H.get(P1), "plan": {"action": [{"ability": "Target_MainHandAttack", "target": self.gob}]}},
            {"who": core.H.get(P2), "plan": {"move": self.gob, "action": ["Shout_Dash"]}}]))
        self.assertIn(f"action: Target_MainHandAttack {self.gob}: done", out)
        self.assertIn("CHAIN STOPPED after 1 of 2: e5: not played — e5 Lae'zel's turn not started: reaction on screen:"
                      " Goblin attacked Claude.", out)
        self.assertIn("NOT PLAYED: e5 —", out)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.cast")], [P1])
        self.assertEqual(self.g.ops("act.move_to"), [])

    # S018/S025/S029: Bless on 3 targets was refused ("more than two targets"): the action went to the fallback.
    def test_three_targets_cast_on_the_first_two(self):
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_Bless", "targets": f"{self.gob},{self.imp},{core.H.get(P1)}"}))
        casts = [a for _, a in self.g.ops("act.cast")]
        self.assertEqual(casts[0]["targets"], [GOB, IMP])
        self.assertIn("targets: cast on the first 2 only", out)
        self.assertIn("left out: ", out)
        self.assertIn("action: Target_Bless", out)
        self.assertNotIn("FAILED", out)

    def test_use_ability_takes_a_list_of_targets(self):
        # ACT-8: a client sending targets as a JSON list reached targets.split(",")
        out = combat_tools.use_ability("Target_Bless", targets=[self.gob, " ", self.imp])
        self.assertNotIn("split", out)
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB, IMP]])

    def test_reactions_prep(self):
        turn_tool.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(self.g.ops("ui.reactions")[0][1], {"char": P1, "set": {"Divine Smite": "always"}})

    # S041 22:41:22: the server said P1 is active (host fallback) but the client's ClientControl was elsewhere.
    def test_reactions_prep_selects_and_retries_on_client_refusal(self):
        from bg3ai.game import GameError
        orig, refused = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions" and not refused:
                refused.append(1)
                self.g.calls.append((op, args))
                raise GameError('[string "BG3AI/Client/UISettings.lua"]:192: char is not the selected character; '
                                'select it first (act.select)')
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(len(self.g.ops("ui.reactions")), 2)
        self.assertIn(("act.select", {"char": P1}), [(o, a) for o, a in self.g.calls if o == "act.select"])
        self.assertIn("reactions set: Divine Smite=always", out)
        self.assertNotIn("reactions: FAILED", out)

    # S069 20:45:31: act.select now waits for ClientControl; when the game keeps another member selected its reason
    # is the prep's failure (no 5 futile retries of the client)
    def test_reactions_prep_names_who_the_game_kept_selected(self):
        from bg3ai.game import GameError
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions":
                self.g.calls.append((op, args))
                raise GameError('[string "BG3AI/Client/UISettings.lua"]:192: char is not the selected character; '
                                'select it first (act.select)')
            if op == "act.select":
                self.g.calls.append((op, args))
                return {"status": "failed", "error": "the game kept Karlach selected (MakePlayerActive twice, once"
                        " via another member)"}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(len(self.g.ops("ui.reactions")), 1)
        self.assertIn("could not be selected: the game kept Karlach selected", out)
        self.assertIn("action: Shout_Dodge: done", out)

    # A16 (S074 23:06:52): the server said Claude was selected, the HUD had another member: select before the prep
    def test_reactions_prep_selects_first_when_the_hud_shows_another_member(self):
        self.g.hud = P2
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        sel = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.select"]
        reac = [i for i, (o, _) in enumerate(self.g.calls) if o == "ui.reactions"]
        self.assertEqual(len(reac), 1)
        self.assertTrue(sel and sel[0] < reac[0], self.g.calls)
        self.assertIn("reactions set: Divine Smite=always", out)

    # S081 #8 02:43:22: set_reactions(Wyll) under an open Counterspell prompt → "Wyll could not be selected: the HUD
    # kept another member selected". The prompt is named and nothing is selected.
    def test_set_reactions_under_a_reaction_prompt_names_it(self):
        self.g.hud = P2
        self.g.ui = {"screen": "reaction", "reaction": {
            "prompt": "Sharran Fidelian Lamona cast Bone Chill on Claude. | Choose how to react. | 108/160 | Counterspell"}}
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("a reaction prompt is on screen (Sharran Fidelian Lamona cast Bone Chill on Claude.)", out)
        self.assertIn("react(use=true|false)", out)
        self.assertEqual(self.g.ops("act.select"), [])
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S087 06:10:18: set_reactions(Wyll) in the enemies' turns: the select fails; the reason and when it works are named
    def test_set_reactions_in_an_enemy_turn_says_why_the_select_failed(self):
        self.g.hud = P2
        self.g.combat["current"] = GOB      # an enemy acts: no party member's turn
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "act.select":
                self.g.calls.append((op, args))
                return {"status": "failed", "error": "the HUD kept another member selected"}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("could not be selected: the HUD kept another member selected", out)
        self.assertIn("It is the enemies' turn", out)
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S081 #258: a mode change answers with a job (the commands apply a frame later): the result printed `job=1`
    def test_set_reactions_waits_for_the_change(self):
        orig, polls = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions":
                self.g.calls.append((op, args))
                return {"job": 1, "status": "running"}
            if op == "job.poll" and context == "client":
                polls.append(1)
                return {"status": "done", "result": {"char": P1, "reactions": [{"name": "Counterspell", "mode": "never"}]}}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertTrue(polls)
        self.assertIn("Counterspell", out)
        self.assertIn("never", out)
        self.assertNotIn("job=1", out)

    def test_next_party_member_already_up_gets_actions(self):
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("'s turn: plan it", out)
        self.assertIn("YOUR ACTIONS", out)

    def test_extra_attack_slot_and_order(self):
        turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                     bonus="Shout_SecondWind",
                                     extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]],
                                     order="bonus,action,extra"))
        self.assertEqual(self.casts(), ["Shout_SecondWind", "Target_MainHandAttack", "Target_MainHandAttack"])

    def test_extra_attack_skipped_after_a_dash(self):
        # S032 17:44: "extra 1: Target_MainHandAttack e743: FAILED — not enough action" after the action went to Dash
        out = turn_tool.combat_turn(self.plan(action="Shout_Dash",
                                           extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]]))
        self.assertEqual(self.casts(), ["Shout_Dash"])
        self.assertIn("extra: skipped (the action went to Shout_Dash", out)
        self.assertEqual(turn.action_without_attack(
            "action: use_ability e743: skipped (out of range) → Shout_Dash: done — statuses=[]"), "Shout_Dash")
        self.assertIsNone(turn.action_without_attack("action: Target_MainHandAttack e743: done — cast=x"))
        self.assertIsNone(turn.action_without_attack("action: Shout_Dash: FAILED — no"))

    def test_melee_step_closer_failure_is_named(self):
        # S042 r4: Karlach's melee "out of range (2.7 m, range 1.5 m)" ×3 with movement left, no word why
        self.g.refuse_if = lambda op, a: (
            "target is out of range (2.7 m, range 1.5 m): move closer first" if op == "act.cast"
            else "no walkable path there (may need a jump or another route)" if op == "act.move_to" else None)
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("(no step closer: ", out)
        self.assertIn("no walkable path there", out)

    def test_extra_attack_used_by_an_earlier_slot_skips_the_extra(self):
        # S042 r4: "bonus (auto fallback): Projectile_MainHandAttack … done … extra_attack", then "extra 1: … not
        # enough action" ×3
        real = self.g.result_for
        self.g.result_for = lambda op, a: ({"cast": a.get("spell"), "extra_attack": True}
                                           if op == "act.cast" and a.get("spell") == "Projectile_MainHandAttack"
                                           else real(op, a))
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           bonus={"ability": "Projectile_MainHandAttack", "target": self.gob},
                                           extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]]))
        self.assertIn("extra 1: skipped (the Extra Attack already went to the bonus attack)", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack", "Projectile_MainHandAttack"])

    # ---- ACT-1: fallbacks keep to the planned target's side (S063 11:00 / 11:13) ----

    def _ally_at(self, dist: float, **kw) -> dict:
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(**{"from": {P1: [dist, 1]}}, **kw)
        self.g.visible.add(P2)
        return lae

    def test_heal_on_an_ally_out_of_range_never_becomes_an_attack_on_it(self):
        # (a) Lay on Hands on ally e3 stopped short → "auto fallback: Projectile_MainHandAttack e3 … damage"
        self._ally_at(9.0)
        self.g.actions = [{"spell": "Target_LayOnHands", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        self.g.refuse_if = lambda op, a: ("target is out of range (9.0 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_LayOnHands" else
                                          "beyond movement (path 7.6 m, 2.0 m left)" if op == "act.move_to" else None)
        p2 = core.H.get(P2)
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": p2}))
        self.assertEqual(self.casts(), ["Target_LayOnHands"])
        self.assertNotIn("auto fallback", out)

    def test_touch_heal_on_a_downed_ally_beyond_3m_steps_in_and_casts(self):
        # S080 02:41:15: Lay on Hands on downed Gale 8.1 m away failed "out of range" with Claude's move unused
        self._ally_at(8.1)
        self.g.actions = [{"spell": "Target_LayOnHands", "range": 1.5}]
        n = {"cast": 0}

        def refuse(op, a):
            if op == "act.cast":
                n["cast"] += 1
                return "target is out of range (8.1 m, range 1.5 m): move closer first" if n["cast"] == 1 else None
            return None
        self.g.refuse_if = refuse
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": core.H.get(P2)}))
        mv = self.g.ops("act.move_to")
        self.assertEqual([(a.get("target"), a.get("within")) for _, a in mv], [(P2, 1.5)])
        self.assertEqual(self.casts(), ["Target_LayOnHands", "Target_LayOnHands"])
        self.assertIn("end_turn: done", out)

    def test_turn_where_every_planned_step_failed_is_not_ended(self):
        # S080 02:41:15: Lay on Hands and the swing out of range, Shield of Faith out of slots → end_turn threw the
        # action and the movement away
        self._ally_at(8.1)
        self.g.actions = [{"spell": "Target_LayOnHands", "range": 1.5}, {"spell": "Target_MainHandAttack", "range": 1.5},
                          {"spell": "Target_ShieldOfFaith", "range": 18}]

        def refuse(op, a):
            if op == "act.move_to":
                return "beyond movement (path 7.6 m, 2.0 m left)"
            if a.get("spell") == "Target_ShieldOfFaith":
                return "not enough spell slot"
            return "target is out of range (8.1 m, range 1.5 m): move closer first" if op == "act.cast" else None
        self.g.refuse_if = refuse
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Target_LayOnHands", "target": core.H.get(P2)},
                                                   {"ability": "Target_MainHandAttack", "target": self.gob}],
                                           bonus={"ability": "Target_ShieldOfFaith", "target": core.H.get(P2)},
                                           retarget=False))
        self.assertEqual(self.g.ops("act.end_turn"), [])
        self.assertIn("turn NOT ended: nothing done (every planned action and bonus step failed", out)
        # one step that worked: the turn ends as planned
        self.g.refuse_if = lambda op, a: refuse(op, a) if a.get("spell") != "Target_ShieldOfFaith" else None
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": core.H.get(P2)},
                                           bonus={"ability": "Target_ShieldOfFaith", "target": core.H.get(P2)},
                                           retarget=False))
        self.assertIn("end_turn: done", out)

    def test_heal_on_an_ally_out_of_range_is_never_retargeted_at_a_hostile(self):
        # (b) Healing Word (L3 slot) planned on an ally went to ENEMY Devastator Stropes
        self._ally_at(20.0)
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}]
        self.g.refuse_if = lambda op, a: ("target is out of range (20.0 m, range 18.0 m): move closer first"
                                          if a.get("spell") == "Target_HealingWord" and a.get("target") == P2 else None)
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_HealingWord", "target": core.H.get(P2)}))
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [P2])
        self.assertNotIn(GOB, [a.get("target") for _, a in self.g.ops("act.cast")])
        self.assertNotIn("auto fallback", out)
        # a heal may re-aim at another (downed) party member in reach
        p3 = "33333333-2222-3333-4444-555555555555"
        core.H.get(p3, "Gale")
        self.g.combat["order"].append({"uuid": p3, "name": "Gale", "relation": "party", "hp_pct": 0,
                                       "downed": True, "from": {P1: [5.0, 1]}})
        self.g.combat["current"] = P1
        n = len(self.g.ops("act.cast"))
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_HealingWord", "target": core.H.get(P2)},
                                           auto_bonus=False))
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")][n:], [P2, p3])
        self.assertIn("(auto fallback)", out)

    def test_attack_on_a_hostile_still_retargets_hostiles(self):
        self.g.dead.add(GOB)
        self._ally_at(3.0)
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        turn_tool.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])

    def test_revive_on_a_dead_ally_is_cast_and_never_reaimed(self):
        # (c) _turn_check skipped every dead target, then the retarget aimed Revivify at the nearest hostile
        self._ally_at(1.2)
        self.g.dead.add(P2)
        next(o for o in self.g.combat["order"] if o["uuid"] == GOB)["from"][P1] = [1.8, 1]
        self.g.actions = [{"spell": "Teleportation_Revivify", "range": 1.5}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Teleportation_Revivify", "target": core.H.get(P2)}))
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Teleportation_Revivify", P2)])
        self.assertNotIn("already dead", out)
        self.g.refuse_if = lambda op, a: ("no line of sight to target" if op == "act.cast" else None)
        self.g.combat["current"] = P1
        turn_tool.combat_turn(self.plan(action={"ability": "Teleportation_Revivify", "target": core.H.get(P2)}))
        self.assertEqual({a.get("target") for _, a in self.g.ops("act.cast")}, {P2})

    def test_move_to_a_dead_ally_is_not_reaimed_at_a_hostile(self):
        # (d) "move to dead Shadowheart, Revivify her" walked toward the nearest enemy and cast Revivify on it
        self._ally_at(4.0)
        self.g.dead.add(P2)
        self.g.actions = [{"spell": "Teleportation_Revivify", "range": 1.5}]
        p2 = core.H.get(P2)
        out = turn_tool.combat_turn(self.plan(move=p2, action={"ability": "Teleportation_Revivify", "target": p2}))
        self.assertNotIn("now aims at", out)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.move_to")], [P2])
        self.assertEqual({a.get("target") for _, a in self.g.ops("act.cast")}, {P2})

    def test_side(self):
        cs = self.g.rpc("state.combat")
        party = {P1, P2}
        self.assertEqual(turn.side(cs, P2, party), "party")
        self.assertEqual(turn.side(cs, GOB, party), "hostile")
        self.assertIsNone(turn.side(cs, CHEST, party))
        self.assertEqual(turn.side(cs, CHEST, party, {CHEST: "neutral"}), "neutral")
        self.g.visible.discard(GOB)  # out of sight now: what an earlier read saw
        self.assertEqual(turn.side(self.g.rpc("state.combat"), GOB, party, turn.sides(cs)), "hostile")

    # ---- ACT-2: who left out (S065: Claude's turn "skipped" twice) ----

    def _shared_group(self) -> None:
        self.g.combat["turn_group"] = [P1, P2]
        attack = {"spell": "Target_MainHandAttack", "name": "Attack"}
        self.g.actions_by = {P1: [attack], P2: [attack, {"spell": "Shout_ActionSurge", "name": "Action Surge"}]}

    def test_shared_turn_without_who_plays_the_member_the_plan_fits(self):
        self._shared_group()  # Claude (P1) is selected; only Lae'zel has Action Surge
        out = turn_tool.combat_turn(self.plan(action="Action Surge"))
        self.assertEqual([(a.get("spell"), a.get("char")) for _, a in self.g.ops("act.cast")],
                         [("Shout_ActionSurge", P2)])
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.end_turn")], [P2])
        self.assertIn("who: ", out)
        self.assertIn("Lae'zel (the only one acting", out)

    def test_shared_turn_without_who_refuses_a_plan_both_could_play(self):
        self._shared_group()
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("REFUSED: several of your characters can act now", out)
        self.assertIn("give who=", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])
        out = turn_tool.combat_turn(self.plan())  # an empty plan would end an unknown member's turn
        self.assertIn("REFUSED", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_sent_ahead_without_who_is_not_taken_by_another_member(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P2)).start()
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 wait_turn=3)
        self.assertEqual(self.g.ops("act.cast"), [])  # Lae'zel's turn came first: not hers to take
        self.assertIn("(yours) must act first", out)
        # the selected member's own turn plays it
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P1)).start()
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 wait_turn=3)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.cast")], [P1])
        self.assertIn("sent ahead: the character selected when it was sent", out)

    def test_sent_ahead_without_who_infers_the_only_member_with_the_ability(self):
        self.g.actions_by = {P1: [{"spell": "Target_MainHandAttack", "name": "Attack"}],
                             P2: [{"spell": "Shout_ActionSurge", "name": "Action Surge"}]}
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P2)).start()
        out = turn_tool.combat_turn(self.plan(action="Shout_ActionSurge"), wait_turn=3)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.cast")], [P2])
        self.assertIn("the only one of yours whose actions the plan fits", out)

    # ---- ACT-3: a melee swing refused without a reason steps closer once (S065 ×9) ----

    NO_REASON = ("the game refused the cast (no reason given): a melee swing from 2.1 m centre to centre "
                 "(our reach 2.5 m: range 1.5 m + both bounding radii), probably just outside the engine's reach")

    def test_melee_refused_without_a_reason_steps_in_and_swings_again(self):
        n = {"cast": 0}

        def refuse(op, a):
            if op == "act.cast":
                n["cast"] += 1
                return self.NO_REASON if n["cast"] == 1 else None
            return None
        self.g.refuse_if = refuse
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS],
                         ["act.cast", "act.move_to", "act.cast", "act.end_turn"])
        mv = self.g.ops("act.move_to")[0][1]
        self.assertEqual((mv.get("target"), mv.get("within")), (GOB, turn_slots.MELEE_STEP_IN_M))
        self.assertIn("stepped closer for the swing", out)
        self.assertIn(f"Target_MainHandAttack {self.gob}: done", out)

    def test_melee_refused_steps_in_only_once_and_not_for_other_reasons(self):
        self.g.refuse_if = lambda op, a: self.NO_REASON if op == "act.cast" else None
        turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                     retarget=False))
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertEqual(len(self.g.ops("act.cast")), 2)
        for why in ("the game refused the cast (no reason given): the target is out of sight now",
                    "the game refused the cast (no reason given): no line of sight to the target now"):
            self.g.combat["current"] = P1
            self.g.refuse_if = lambda op, a, why=why: why if op == "act.cast" else None
            n = len(self.g.ops("act.move_to"))
            turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                         retarget=False))
            self.assertEqual(len(self.g.ops("act.move_to")), n, why)
        # a ranged ability refused without a reason: no step in
        self.g.combat["current"] = P1
        self.g.actions = [{"spell": "Projectile_FireBolt", "range": 18}]
        self.g.refuse_if = lambda op, a: "the game refused the cast (no reason given): usually the shot's path" \
            if op == "act.cast" else None
        n = len(self.g.ops("act.move_to"))
        turn_tool.combat_turn(self.plan(action={"ability": "Projectile_FireBolt", "target": self.gob}, retarget=False))
        self.assertEqual(len(self.g.ops("act.move_to")), n)

    def test_plan_errors(self):
        for bad, msg in (({"actoin": "x"}, "unknown plan key"),
                         ({"action": {"tool": "end_turn"}}, "can't be a turn step"),
                         ({"action": {"tool": "save_game"}}, "can't be batched"),
                         ({"action": "Shout_Dodge", "on_reaction": "maybe"}, "on_reaction"),
                         ({"action": "Shout_Dodge", "order": "action,fly"}, "order")):
            self.assertIn(msg, turn_tool.combat_turn(json.dumps(bad)), bad)


if __name__ == "__main__":
    unittest.main()
