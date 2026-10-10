"""Offline tests: memory/lessons.md parsing, trigger matching and the bridge's LESSONS lines at fight start and
on entering a place (docs/ARCHITECTURE.md "Learning from fights").

Run: cd bridge && uv run python -m unittest tests.test_lessons
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import lessons, server  # noqa: E402

LESSONS_MD = """# Lessons from fights

<!--
Format: - WHEN <trigger>, <trigger>: <what failed> → <what to do instead> (Sxxx) [seen]
- WHEN Example: commented out → never shown (S000) [seen]
-->

## Lessons
- WHEN Justiciar Soulhunter, ranged: 8 Soulhunters shot the party down from 20 m → Fireball the archer cluster first, fight from cover (S033) [seen]
- WHEN Undead: Ghouls paralysed Karlach → Turn Undead first, keep the frontline out of claw reach (S023) [seen]
- WHEN Gauntlet of Shar: walked into the antechamber at half slots → long rest + Aid before the antechamber (S033) [seen]
- WHEN Imp: imps teleport behind the casters → keep Gale beside Claude (S010) [seen]
not a lesson line
"""


class Parse(unittest.TestCase):
    def test_parse_skips_comments_and_prose(self):
        ls = lessons.parse(LESSONS_MD)
        self.assertEqual([l.triggers[0] for l in ls], ["Justiciar Soulhunter", "Undead", "Gauntlet of Shar", "Imp"])
        self.assertEqual(ls[0].triggers, ["Justiciar Soulhunter", "ranged"])
        self.assertIn("Fireball the archer cluster", ls[0].body)

    def test_whole_word_match(self):
        ls = lessons.parse(LESSONS_MD)
        hit = lessons.match(ls, ["Justiciar Soulhunter", "Human"])
        self.assertEqual([l.triggers[0] for l in hit], ["Justiciar Soulhunter"])
        self.assertEqual(lessons.match(ls, ["Imperial Guard"]), [])          # 'Imp' never inside a word
        self.assertEqual([l.triggers[0] for l in lessons.match(ls, ["Imp"])], ["Imp"])
        self.assertEqual([l.triggers[0] for l in lessons.match(ls, ["Ghoul", "undead"])], ["Undead"])  # case

    def test_limit_and_order(self):
        ls = lessons.parse(LESSONS_MD)
        hit = lessons.match(ls, ["Justiciar Soulhunter", "Undead", "Gauntlet of Shar", "Imp"], limit=3)
        self.assertEqual(len(hit), 3)
        self.assertEqual(hit[0].triggers[0], "Imp")   # one trigger each: the newest (later in the file) first


class Store(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.p = self.d / "lessons.md"
        self.p.write_text(LESSONS_MD)
        self.s = lessons.Store(self.p)

    def test_fight_lines_once_per_fight(self):
        en = [{"name": "Justiciar Soulhunter", "race": "Human"}, {"name": "Ghoul", "race": "Undead"}]
        out = self.s.fight_lines(en, ["Gauntlet of Shar", "Shadow-Cursed Lands"])
        self.assertTrue(out.startswith("LESSONS"))
        self.assertEqual(out.count("\n- WHEN"), 3)
        self.assertEqual(self.s.fight_lines(en, ["Gauntlet of Shar"]), "")   # same fight: nothing again
        self.s.fight_over()
        self.assertIn("Soulhunter", self.s.fight_lines(en, []))            # the next fight (a retry): shown again

    def test_region_once_per_entry_and_reload_on_change(self):
        self.assertIn("antechamber", self.s.region_lines(["Gauntlet of Shar", "Shadow-Cursed Lands"]))
        self.assertEqual(self.s.region_lines(["Gauntlet of Shar", "Shadow-Cursed Lands"]), "")
        self.assertEqual(self.s.region_lines(["Last Light Inn", "Shadow-Cursed Lands"]), "")
        self.p.write_text(LESSONS_MD + "- WHEN Last Light Inn: x → y (S034) [seen]\n")
        os.utime(self.p, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.s.region_lines(["Last Light Inn", "Shadow-Cursed Lands"]), "")   # same place: quiet
        self.s.region_lines(["Shadow-Cursed Lands"])                                            # left the inn
        self.assertIn("x → y", self.s.region_lines(["Last Light Inn", "Shadow-Cursed Lands", ""]))  # re-read file

    def test_missing_file_is_quiet(self):
        s = lessons.Store(self.d / "nope.md")
        self.assertEqual(s.fight_lines([{"name": "Ghoul", "race": "Undead"}], []), "")


class PlaceInsideTrigger(unittest.TestCase):
    def test_a_longer_trigger_naming_the_place_matches(self):
        # S038 20:44: the agent's note "WHEN Nightsong's Prison arrival ledge: …"; WHERE shows "Nightsong's Prison"
        ls = lessons.parse("- WHEN Nightsong's Prison arrival ledge: forced jump died → find the leap point (S038)\n"
                           "- WHEN Wyll: x → y (S035)\n")
        self.assertEqual(len(lessons.match(ls, ["Nightsong's Prison", "Shadow-Cursed Lands"],
                                           places=["Nightsong's Prison", "Shadow-Cursed Lands"])), 1)
        self.assertEqual(lessons.match(ls, ["Nightsong"], places=[]), [])            # no reverse match for enemies
        self.assertEqual(lessons.match(ls, ["Prison"], places=["Prison"])[0].triggers[0][:9], "Nightsong")  # whole word
        self.assertEqual(lessons.match(ls, ["Nights"], places=["Nights"]), [])         # never part of a word


class ServerHook(unittest.TestCase):
    """server._lessons_text: a fight start reads state.combat + state.status once and shows matching lessons."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "lessons.md").write_text(LESSONS_MD)
        self.saved = (server.LESSONS, server.game)
        server.LESSONS = lessons.Store(d / "lessons.md")
        self.reads = []
        self.status = {"region": "Shadow-Cursed Lands", "subregion": "Gauntlet of Shar"}
        test = self

        class G:
            def try_rpc(self, op, args=None, **kw):
                test.reads.append(op)
                if op == "state.combat":
                    return {"active": True, "order": [
                        {"name": "Claude", "relation": "party"},
                        {"name": "Justiciar Soulhunter", "relation": "hostile", "race": "Human"},
                        {"name": "Candle", "relation": "neutral", "item": True}]}
                if op == "state.status":
                    return test.status
                return None
        server.game = G()

    def tearDown(self):
        server.LESSONS, server.game = self.saved

    def test_fight_start_then_quiet_then_end(self):
        out = server._lessons_text("use_ability", "use_ability: done", "EVENTS\n  combat_started")
        self.assertIn("Soulhunter", out)
        self.assertIn("antechamber", out)          # the place matches too
        self.assertNotIn("Undead", out)
        self.assertEqual(self.reads, ["state.combat", "state.status"])
        self.assertEqual(server._lessons_text("combat_turn", "TURN …", ""), "")
        self.assertEqual(len(self.reads), 2)       # no reads while the fight runs
        server._lessons_text("combat_turn", "…", "EVENTS\n  combat_ended")
        self.assertIn("Soulhunter", server._lessons_text("get_combat_state", "COMBAT round=1", ""))

    def test_no_reads_without_a_fight(self):
        self.assertEqual(server._lessons_text("move_to", "move_to: done", ""), "")
        self.assertEqual(self.reads, [])

    def test_place_lessons_from_the_status_where_line(self):
        out = server._lessons_text("get_status", "WHERE Gauntlet of Shar · Shadow-Cursed Lands | gold 5\nPARTY …", "")
        self.assertIn("antechamber", out)
        self.assertEqual(self.reads, [])

    def test_fight_start_matches_the_place_before_the_fight(self):
        # S035 19:32: combat_started and WHERE Self-Same Trial · … -> Shadow-Cursed Lands in the same result.
        server.LESSONS.region = "Gauntlet of Shar · Shadow-Cursed Lands"
        body = "move_to: INTERRUPTED — combat started\nCHANGES\n  WHERE Gauntlet of Shar · Shadow-Cursed Lands -> Shadow-Cursed Lands"
        self.status = {"region": "Shadow-Cursed Lands", "subregion": ""}
        out = server._lessons_text("move_to", body, "EVENTS\n  combat_started")
        self.assertIn("antechamber", out)

    def test_place_lessons_from_a_move_results_changes(self):
        # S035: the Gauntlet was entered through move_to results only (2 get_status calls in 25 min).
        body = ("move_to: done — travelled=12.0\nCHANGES\n  WHERE Self-Same Trial · Shadow-Cursed Lands -> "
                "Gauntlet of Shar · Shadow-Cursed Lands\n  e5 Karlach moved 6m SW")
        out = server._lessons_text("move_to", body, "")
        self.assertIn("LESSONS for Gauntlet of Shar · Shadow-Cursed Lands", out)
        self.assertNotIn("Karlach", out.split("\n")[0])
        self.assertEqual(server._lessons_text("move_to", body, ""), "")   # same place: once
        self.assertEqual(self.reads, [])


if __name__ == "__main__":
    unittest.main()
