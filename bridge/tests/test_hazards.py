"""Hazard policy (claude-run-2 S001: stuck >2.5 min at the Nautiloid's burning corridor): travel_to takes the
least-exposure crossing when no clear way leads on (unless HP is low or avoid_hazards=strict), jump_to moves
landings out of fire, and get_frontier / look list the hazard patches in view."""
from __future__ import annotations

import json
import time
import unittest

from test_turn_macros import Base, P1  # noqa: F401  (sets BG3AI_RUNTIME first)

from bg3ai import macros, render, server  # noqa: E402

FIRE = {"m": 1.5, "surface": "Fire", "patches": 1, "longest_m": 1.5, "pos": [5.0, 0.0, 0.0], "at_m": 4.0}


class CrossingPolicy(unittest.TestCase):
    def test_text_quotes_the_cost(self):
        self.assertEqual(macros.exposure_text(FIRE), "1.5m of Fire (~1-4 dmg + Burning)")
        self.assertIn("in 2 patches", macros.exposure_text(dict(FIRE, patches=2)))
        self.assertIn("deadly", macros.exposure_text(dict(FIRE, surface="Lava")))

    def test_auto_crosses_at_good_hp_not_at_low_hp(self):
        self.assertEqual(macros.crossing_ok(FIRE, 22, 22), (True, ""))
        ok, why = macros.crossing_ok(FIRE, 11, 22)  # S001: 11/22 after the fire; worst case ~12
        self.assertFalse(ok)
        self.assertIn("HP 11/22 is too low", why)

    def test_strict_never_cross_forces(self):
        self.assertFalse(macros.crossing_ok(FIRE, 22, 22, "strict")[0])
        self.assertTrue(macros.crossing_ok(FIRE, 5, 22, "cross")[0])
        self.assertFalse(macros.crossing_ok(dict(FIRE, surface="Lava"), 22, 22, "cross")[0])
        self.assertFalse(macros.crossing_ok(dict(FIRE, m=7.0), 22, 22, "cross")[0])

    def test_harmless_surfaces_need_no_hp(self):
        self.assertTrue(macros.crossing_ok(dict(FIRE, surface="Grease"), 2, 22)[0])


class TravelCrossing(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.watch["party"][0].update(hp=22, max_hp=22)
        self.g.result_for = lambda op, a: ({"after": {"pos": a.get("pos"), "hp": 21, "max_hp": 22}}
                                           if op == "act.move_to" else {})
        self.legs = 0

        def reach(a):
            self.legs += 1
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            if self.legs == 1:  # S001: every clear walk stops at the fire; the least-exposure crossing
                return {"reachable": True, "pos": [10.0, 0.0, -5.0], "exposure": FIRE, "progress_m": 9}
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_takes_the_crossing_and_says_what_it_costs(self):
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertEqual(self.g.ops("nav.reach_toward")[0][1]["avoid"], "least")
        self.assertIn("leg 1: crossing 1.5m of Fire (~1-4 dmg + Burning) @(5,0,0), the shortest way on: move_to: done", out)
        self.assertIn("arrived", out)

    def test_burning_on_the_crossing_does_not_stop_it(self):
        def burn(op, a):
            if op == "act.move_to" and len(self.g.ops("act.move_to")) == 1:
                self.g.watch["party"][0].update(surface="Fire", hp=19)
            elif op == "act.move_to":
                self.g.watch["party"][0].update(surface=None, hp=17)  # Burning ticks on the next leg
        self.g.on_start = burn
        self.g.dur["act.move_to"] = 0.15
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertNotIn("STOPPED", out)
        self.assertIn("arrived", out)

    def test_low_hp_stops_before_the_crossing(self):
        self.g.watch["party"][0].update(hp=11)
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: the way on crosses 1.5m of Fire", out)
        self.assertIn("HP 11/22 is too low", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_strict_asks_for_no_crossing(self):
        self.g.reach = {"reachable": False, "hazard": {"surface": "Fire", "pos": [5, 0, 0], "at_m": 5}}
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True, avoid_hazards="strict")
        self.assertNotIn("avoid", self.g.ops("nav.reach_toward")[0][1])
        self.assertIn("STOPPED: the way on crosses Fire 5m ahead", out)

    def test_detour_is_named(self):
        self.g.reach = lambda a: ({"reachable": True, "pos": [50.0, 0.0, -8.0], "detour": True, "dir": "SE",
                                   "straight": {"hazard": {"surface": "Fire"}}})
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("leg 1: around Fire (heading SE): move_to: done", out)

    def test_bad_policy(self):
        self.assertIn("avoid_hazards must be", server.travel_to(x=1.0, y=0.0, z=0.0, avoid_hazards="yolo"))

    def test_async_passes_the_policy_to_the_queue(self):
        server.travel_to(x=50.0, y=0.0, z=0.0, avoid_hazards="cross")
        q = server._queues().queues.get(P1)
        self.assertIsNotNone(q)
        for _ in range(200):
            if not server._queues().busy(P1):
                break
            time.sleep(0.01)
        self.assertTrue(q.last_report and q.last_report.lines)
        self.assertIn("travel_to", q.last_report.lines[0])


class JumpLanding(Base):
    def test_landing_in_fire_moves_to_the_suggested_spot(self):
        self.g.landing = {"safe": False, "surface": "Fire", "dist_m": 4.0, "reliable_m": 4.5,
                          "suggest": [-4.0, 0.0, 1.5], "suggest_off_m": 1.5, "suggest_dist_m": 4.3}
        out = server.jump_to(-4.0, 0.0, 0.0)
        self.assertIn("landing (-4,0,0) is in Fire: jumping to (-4,0,1.5) instead (1.5m from it, 4.3m jump, clear)", out)
        self.assertEqual(self.g.ops("act.cast")[0][1]["pos"], [-4.0, 0.0, 1.5])

    def test_long_jump_that_may_fall_short(self):
        self.g.landing = {"safe": False, "surface": "Fire", "dist_m": 7.8, "reliable_m": 4.5, "short_pos": [-4.5, 0, 0],
                          "suggest": [-2.0, 0.0, 0.0], "suggest_off_m": 5.8, "suggest_dist_m": 2.0}
        out = server.jump_to(-7.8, 0.0, 0.0)
        self.assertIn("7.8m away, past reliable reach (4.5m): it may land short in Fire", out)

    def test_no_clear_spot_refuses(self):
        self.g.landing = {"safe": False, "surface": "Fire", "dist_m": 4.0, "reliable_m": 4.5}
        out = server.jump_to(-4.0, 0.0, 0.0)
        self.assertIn("jump_to: FAILED", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_far_alternative_is_named_not_taken(self):
        self.g.landing = {"safe": False, "blocked": True, "dist_m": 10.3, "reliable_m": 4.5,
                          "nearest_clear": [-40.7, 7.8, -361.0], "nearest_clear_off_m": 6.8}
        out = server.jump_to(-36.0, 9.5, -356.0)
        self.assertIn("is on a blocked tile (wall or hole)", out)
        self.assertIn("nearest clear landing in reach is (-40.7,7.8,-361), 6.8m from it", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_force_and_safe_and_old_mod(self):
        self.g.landing = {"safe": False, "surface": "Fire", "dist_m": 4.0, "reliable_m": 4.5}
        server.jump_to(-4.0, 0.0, 0.0, force=True)
        self.g.landing = {"safe": True, "dist_m": 4.0}
        server.jump_to(-3.0, 0.0, 0.0)
        self.g.landing = None  # nav.landing unknown (mod not reloaded): jump as asked
        server.jump_to(-2.0, 0.0, 0.0)
        self.assertEqual([a["pos"] for _, a in self.g.ops("act.cast")],
                         [[-4.0, 0.0, 0.0], [-3.0, 0.0, 0.0], [-2.0, 0.0, 0.0]])

    def test_refusal_fails_the_batch_step(self):
        self.g.landing = {"safe": False, "surface": "Fire", "dist_m": 4.0, "reliable_m": 4.5}
        out = server.do_actions(json.dumps([{"tool": "jump_to", "x": -4, "y": 0, "z": 0},
                                            {"tool": "move_to", "x": 1, "y": 0, "z": 0}]))
        self.assertIn("STOPPED", out)
        self.assertEqual(self.g.ops("act.move_to"), [])


PATCHES = [{"surface": "Fire", "dist": 3.0, "dir": "W", "w_m": 2.0, "d_m": 4.0, "center": [-4, 0, 0]},
           {"surface": "Acid", "dist": 9.5, "dir": "SW", "w_m": 3.0, "d_m": 3.0, "center": [-7, 0, -7]}]


class HazardLines(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render.render_hazards(PATCHES), ["HAZARD Fire 3m W (2x4m)", "HAZARD Acid 9.5m SW (3x3m)"])
        self.assertEqual(render.render_hazards([{"surface": "Fire", "under": True, "w_m": 2.0, "d_m": 2.0}]),
                         ["HAZARD Fire here (you stand in it; 2x2m)"])
        self.assertEqual(render.render_hazards(None), [])

    def test_frontier_lists_hazards_after_leads(self):
        out = render.render_frontier({"leads": [], "hazards": PATCHES[:1]}, server.H)
        self.assertEqual(out.splitlines(), ["No leads in view. Try another direction, a door, or get_map.",
                                            "HAZARD Fire 3m W (2x4m)"])

    def test_look_delta_shows_them_only_when_they_change(self):
        server._hazards_seen["sig"] = None
        self.assertEqual(server._hazard_lines(PATCHES), render.render_hazards(PATCHES))
        self.assertEqual(server._hazard_lines(PATCHES), [])
        self.assertEqual(server._hazard_lines(PATCHES, always=True), render.render_hazards(PATCHES))
        self.assertEqual(server._hazard_lines([]), ["HAZARD none in view now"])
        self.assertEqual(server._hazard_lines([]), [])
        self.assertEqual(server._hazard_lines(None), [])


if __name__ == "__main__":
    unittest.main()
