"""A move's reactions in its result (Actions.lua reaction_annotate): window 8 seed 7 m1 (arena-fight-analysis.md S2),
Ash's move_to answered "done, hp 20/20" 0.6 s before Birch's opportunity attack (critical 13) landed; the mod now
holds the answer until the reaction resolves and the bridge names it as a reaction."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bg3ai import render
from bg3ai.game import JobResult
from bg3ai.tools import core, plumbing

ASH = "9d06e57d-0000-0000-0000-0000000000a1"
BIRCH = "2557e149-0000-0000-0000-0000000000b1"


class MoveReactions(unittest.TestCase):
    def setUp(self):
        core.H.get(ASH, "Ash")
        core.H.get(BIRCH, "Birch")

    def test_the_opportunity_attack_the_move_set_off_is_named_as_a_reaction_with_its_damage(self):
        res = {
            "dist": 1,
            "target": BIRCH,
            "after": {"hp": 7, "max_hp": 20, "in_combat": True, "movement": 1.4},
            "reactions": [
                {
                    "by": {"uuid": BIRCH, "name": "Birch"},
                    "name": "Main Hand Attack",
                    "damage": 13,
                    "type": "Slashing",
                    "resolved": True,
                }
            ],
        }
        text = plumbing._job_text("move_to", JobResult("done", res), ASH)
        head, tail = text.split("\n", 1)
        self.assertTrue(head.startswith("move_to: done"), text)
        self.assertNotIn("reactions", head)
        b, a = core.H.get(BIRCH), core.H.get(ASH)
        self.assertEqual(tail, f"REACTIONS to the move\n  reaction {b} Birch: Main Hand Attack -> {a} Ash 13 Slashing")

    def test_a_reaction_cut_by_the_wait_says_so_and_one_without_damage_names_none(self):
        res = {
            "dist": 1,
            "reactions": [{"by": {"uuid": BIRCH, "name": "Birch"}, "name": "Main Hand Attack", "damage": 0}],
        }
        text = plumbing._job_text("move_to", JobResult("done", res), ASH)
        self.assertTrue(text.endswith("Main Hand Attack (still resolving when this answered)"), text)

    def test_a_reactor_the_side_cant_see_is_not_named_only_the_damage_shows(self):
        res = {"dist": 1, "reactions": [{"unseen": True, "damage": 7, "type": "Fire", "resolved": True}]}
        text = plumbing._job_text("move_to", JobResult("done", res), ASH)
        a = core.H.get(ASH)
        self.assertTrue(text.endswith(f"REACTIONS to the move\n  reaction by someone unseen -> {a} Ash 7 Fire"), text)

    def test_no_reactions_no_block(self):
        text = plumbing._job_text("move_to", JobResult("done", {"dist": 1}), ASH)
        self.assertNotIn("REACTIONS", text)


class PathPreviewProvokes(unittest.TestCase):
    """Window 9 MR2: the game's path preview marks where the path leaves an enemy's reach ring ("Provokes Attack of
    Opportunity"), nothing once that enemy's reaction is spent; path_preview says the same fact (nav.path provokes)."""

    def test_path_preview_names_each_enemy_whose_reach_the_path_leaves(self):
        h = render.Handles()
        res = {
            "reachable": True,
            "length_m": 6.0,
            "within_movement": True,
            "movement_left": 9.0,
            "provokes": [{"uuid": BIRCH, "name": "Birch", "at": [-0.1, 0.0, 0.0], "along_m": 1.1}],
        }
        out = render.render_path(res, h)
        self.assertEqual(
            out,
            "reachable: walk 6m fits movement left 9m | provokes an attack of opportunity from "
            "e1 Birch where it leaves its reach @(-0.1,0,0), 1.1m along",
        )
        self.assertNotIn("provokes", render.render_path({"reachable": True, "length_m": 6.0}, h))


if __name__ == "__main__":
    unittest.main()
