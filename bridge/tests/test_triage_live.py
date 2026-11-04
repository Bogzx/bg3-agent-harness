"""Offline checks for live-triage fixes (path_preview rendering, hazard advice)."""
import unittest

from bg3ai import macros, render


class RenderPathTest(unittest.TestCase):
    def setUp(self):
        self.H = render.Handles()

    def test_unreachable_is_explicit(self):
        out = render.render_path({"reachable": False, "straight_m": 18.9, "error_cause": 3, "ms": 1}, self.H)
        self.assertTrue(out.startswith("UNREACHABLE"))
        self.assertNotIn("error_cause", out)

    def test_reachable_with_movement(self):
        out = render.render_path({"reachable": True, "length_m": 7.2, "needs_jump": True,
                                  "within_movement": False, "movement_left": 3.9}, self.H)
        self.assertIn("walk 7.2m", out)
        self.assertIn("does NOT fit movement left 3.9m", out)

    def test_w12_path_that_ends_short_of_the_target_says_so(self):
        # w12 L1a: Karlach @(-601.29,-142.08,-1431.14), landing (-601.94,-142.08,-1434.58) 3.5 m away: "reachable:
        # walk 0.1m". The mod's nav.path flagged the path partial (its end > 2 m from the target); it wasn't shown.
        out = render.render_path({"reachable": True, "length_m": 0.1, "partial": True, "end_off": 3.4,
                                  "end_pos": [-601.3, -142.1, -1431.2], "within_movement": True,
                                  "movement_left": 12}, self.H)
        self.assertEqual(out, "NOT REACHED: the walkable path ends @(-601.3,-142.1,-1431.2), 3.4m short of the target: "
                              "walk 0.1m fits movement left 12m")
        self.assertNotIn("reachable", out)


class HazardWayOutTest(unittest.TestCase):
    def test_chasm_never_burns_out(self):
        self.assertEqual(macros.hazard_fact({"surface": "Chasm"}), "it does not burn out")  # D630: facts, no advice
        # REVIEW-13 item 3: no duration claim for any other surface (Web, Grease, ice, Spike Growth don't burn out)
        for s in ("Fire", "Web", "Grease", "WaterFrozen", "SpikeGrowth", "BlackTentacles", "Acid", "CausticBrine"):
            self.assertEqual(macros.hazard_fact({"surface": s}), "", s)
        self.assertEqual(macros.hazard_fact(None), "")


if __name__ == "__main__":
    unittest.main()
