"""Offline tests for tools/dev/probe_walk_stop.py (no game needed): the variants' geometry, the Lua, the summary.

Run: uv run pytest -q tools/dev/test_probe_walk_stop.py
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_walk_stop as probe


def _xz(a, b) -> float:
    return math.hypot(a[0] - b[0], a[2] - b[2])


class SetupTest(unittest.TestCase):
    def test_a_is_the_n15_order_from_the_n10_positions(self):
        s = probe.setup_for("a")
        self.assertEqual((s["claude"], s["wyll"]), (probe.CLAUDE0, probe.WYLL0))
        self.assertEqual(s["order"], {"target": "$wyll", "range": 2.5})
        self.assertFalse(s["unchain_others"] or s["raw"])
        for o in s["others"]:  # behind Claude, away from Wyll
            self.assertGreater(_xz(o, probe.WYLL0), _xz(probe.CLAUDE0, probe.WYLL0))

    def test_b_walks_to_a_point_2_m_beside_wyll_off_the_line(self):
        pos = probe.setup_for("b")["order"]["pos"]
        self.assertAlmostEqual(_xz(pos, probe.WYLL0), 2.0, places=1)
        ux, uz = probe._unit_xz(probe.CLAUDE0, probe.WYLL0)
        along = (pos[0] - probe.WYLL0[0]) * ux + (pos[2] - probe.WYLL0[2]) * uz
        self.assertAlmostEqual(along, 0.0, places=1)  # perpendicular to the line

    def test_c_moves_wyll_3_m_aside_and_d_r_flag_their_change(self):
        self.assertAlmostEqual(_xz(probe.setup_for("c")["wyll"], probe.WYLL0), 3.0, places=6)
        self.assertTrue(probe.setup_for("d")["unchain_others"])
        self.assertTrue(probe.setup_for("r")["raw"])

    def test_start_code_fills_every_slot_and_teleports_nobody(self):
        code = probe.start_code(probe.setup_for("a"))
        self.assertNotIn("%s", code)
        self.assertNotIn("%d", code)
        self.assertNotIn("TeleportToPosition", code + probe.INSTALL)


PARTY = [
    {"uuid": "c", "name": "Claude", "pos": list(probe.CLAUDE0)},
    {"uuid": "w", "name": "Wyll", "pos": [-40.0, 26.4, -46.0]},
    {"uuid": "k", "name": "Karlach", "pos": [-42.5, 26.4, -43.5]},
    {"uuid": "s", "name": "Shadowheart", "pos": [-41.0, 26.4, -43.5]},
]


class PlaceTest(unittest.TestCase):
    """N16 step 4: a teleport placement left every order unstarted; placement is N15's: walks, Claude not moved."""

    def test_a_fresh_load_walks_only_wyll_and_chains_the_others(self):
        steps = probe.place_steps(probe.setup_for("a"), PARTY)
        self.assertEqual(
            [lab for lab, _, _ in steps],
            ["unchain Wyll", "chain Karlach to Claude", "chain Shadowheart to Claude", "walk Wyll"],
        )
        self.assertEqual(steps[-1][1:], ("act.move_to", {"char": "w", "pos": list(probe.WYLL0)}))

    def test_claude_walks_back_when_off_his_start_and_d_walks_the_unchained_others(self):
        party = [dict(m) for m in PARTY]
        party[0]["pos"] = [-39.5, 26.4, -49.7]  # where the last variant's walk ended
        party[1]["pos"] = list(probe.WYLL0)
        labels = [lab for lab, _, _ in probe.place_steps(probe.setup_for("d"), party)]
        self.assertEqual(
            labels,
            [
                "unchain Wyll",
                "unchain Karlach",
                "unchain Shadowheart",
                "walk Claude back",
                "walk Karlach behind",
                "walk Shadowheart behind",
            ],
        )

    def test_still_needs_every_member_under_5_cm(self):
        moved = [dict(m) for m in PARTY]
        self.assertTrue(probe.still(PARTY, moved))
        moved[2] = dict(moved[2], pos=[-42.5, 26.4, -43.4])
        self.assertFalse(probe.still(PARTY, moved))


class SummaryTest(unittest.TestCase):
    def test_names_the_stop_and_what_changed_just_before_it(self):
        w = [-38.5, 26.4, -53.5]
        log = [{"k": "order", "t": 0, "plan": {"pos": [-37.6, 26.4, -55.8]}}]
        for i in range(8):  # 0.5 m a tick toward Wyll, then still
            log.append(
                {"k": "tick", "t": 100 * i, "c": [-42.0 + 0.2 * min(i, 5), 26.4, -44.7 - 0.5 * min(i, 5)], "w": w}
            )
        log.append({"k": "osi_event", "t": 450, "name": "EntityEvent"})
        log.append({"k": "state", "t": 2000, "key": "steer"})
        log.append({"k": "job_finish", "t": 520, "status": "failed", "err": "the walk ended 6.9 m from the target"})
        line = probe.summarize("a", log)
        self.assertIn("a: order at 0 ms, first step at 100 ms; walked 2.7 m, stopped at 0.50 s", line)
        self.assertIn("job failed: the walk ended 6.9 m from the target", line)
        self.assertIn("around the stop: osi_event:EntityEvent", line)
        self.assertNotIn("state:steer", line)
        json.dumps(log)

    def test_an_order_that_never_starts_is_a_suspect_setup_not_a_result(self):
        w = [-38.5, 26.4, -53.5]
        log = [{"k": "order", "t": 22}] + [
            {"k": "tick", "t": 100 * i, "c": list(probe.CLAUDE0), "w": w} for i in range(80)
        ]
        self.assertEqual(
            probe.summarize("a", log), "a: SETUP SUSPECT: order at 22 ms, Claude never moved; start 9.47 m from Wyll"
        )
        log[50] = dict(log[50], c=[-41.9, 26.4, -44.9])
        self.assertIn("first step only at 4900 ms", probe.summarize("a", log))


if __name__ == "__main__":
    unittest.main()
