"""Offline tests for tools/dev/check_plans.py (plan line vs the game's offers; no game)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_plans as CP  # noqa: E402

# Live shapes (13:4x probe): Wyll L5 = 1 Fiend 3rd-level spell + 1 invocation; Gale L6 = 2 Wizard SLevel 3.
WYLL5 = {"level": 5, "feat": False, "skills": 0,
         "spells": [{"amount": 1, "list": "Warlock Fiend SLevel 3",
                     "options": [{"name": "Fireball", "level": 3}, {"name": "Hypnotic Pattern", "level": 3},
                                 {"name": "Scorching Ray", "level": 2}]}],
         "passives": [{"amount": 1, "list": "Warlock invocations L5",
                       "options": ["Devil's Sight", "Fiendish Vigour", "Agonising Blast"]}]}
GALE6 = {"level": 6, "feat": False, "skills": 0, "passives": [],
         "spells": [{"amount": 2, "list": "Wizard SLevel 3",
                     "options": [{"name": "Haste", "level": 3}, {"name": "Slow", "level": 3},
                                 {"name": "Misty Step", "level": 2}]}]}
SH4 = {"level": 4, "feat": True, "skills": 0, "passives": [],
       "spells": [{"amount": 1, "list": "Cleric cantrips (Wisdom)", "options": [{"name": "Toll the Dead", "level": 0}]}]}


class CheckLevel(unittest.TestCase):
    def test_valid_lines(self):
        self.assertEqual(CP.check_level(WYLL5, {"passives": ["Fiendish Vigour"], "spells": ["Hypnotic Pattern"]}, set()), [])
        self.assertEqual(CP.check_level(GALE6, {"spells": ["Haste", "Slow"]}, {"Misty Step"}), [])

    def test_the_lines_the_game_refused(self):
        p = CP.check_level(WYLL5, {"passives": ["Thirsting Blade"]}, set())
        self.assertIn("passive Thirsting Blade: not offered", p)
        self.assertIn("spells: 0 of 1 picks", p)
        self.assertIn("spell Misty Step: already known", CP.check_level(GALE6, {"spells": ["Haste", "Misty Step"]}, {"Misty Step"}))

    def test_cleric_cantrip_and_feat(self):
        self.assertEqual(CP.check_level(SH4, {"feat": "Ability Improvement"}, set()), ["cantrips: 0 of 1 picks"])
        self.assertIn("feat Alert: this level has no feat", CP.check_level(GALE6, {"feat": "Alert", "spells": ["Haste", "Slow"]}, set()))

    def test_placeholder_on_a_feat_or_skill_level_is_refused(self):
        # S053: "Karlach L10: {}" placeholders "assuming no picks" passed the end check
        feat_lv = {"level": 12, "feat": True, "skills": 0, "spells": [], "passives": []}
        self.assertEqual(CP.check_level(feat_lv, {}, set()),
                         ['feat: this level picks a feat (plan.feat: a feat name, or "Ability Improvement" + abilities)'])
        self.assertEqual(CP.check_level(feat_lv, {"feat": "Great Weapon Master"}, set()), [])
        skill_lv = {"level": 3, "feat": False, "skills": 1, "spells": [], "passives": []}
        self.assertEqual(CP.check_level(skill_lv, {}, set()), ["skills: 0 of 1 picks"])
        self.assertEqual(CP.check_level(skill_lv, {"skills": ["Stealth"]}, set()), [])
        self.assertEqual(CP.check_level({"level": 10, "feat": False, "skills": 0, "spells": [], "passives": []}, {}, set()),
                         [])                                          # a level with no choices: {} is right
        self.assertIn("a feat", CP.check_level(feat_lv, None, set())[0])

    def test_spelling_variants(self):
        self.assertEqual(CP.check_level(WYLL5, {"passives": ["Agonizing Blast"], "spells": ["fireball"]}, set()), [])

    def test_missing_line(self):
        self.assertEqual(CP.check_level({"level": 6, "spells": [], "passives": [], "feat": False, "skills": 0}, None, set()), [])
        self.assertTrue(CP.check_level(GALE6, None, set())[0].startswith("no plan line"))


class KnownPassives(unittest.TestCase):
    """S028: the keeper rewrote Wyll L7 as {"spells":["Banishment"],"passives":["Agonising Blast"]} from an offer list
    that still showed Agonising Blast (taken at L5), and the check passed it."""
    WYLL7 = {"level": 7, "feat": False, "skills": 0,
             "spells": [{"amount": 1, "list": "Warlock Fiend SLevel 4", "options": [{"name": "Banishment", "level": 4}]}],
             "passives": [{"amount": 1, "list": "Warlock invocations L7",
                           "options": ["Agonising Blast", "Sculptor of Flesh", "Dreadful Word"]}]}

    def test_known_passive_refused(self):
        p = CP.check_level(self.WYLL7, {"spells": ["Banishment"], "passives": ["Agonising Blast"]}, {"Agonising Blast"})
        self.assertIn("passive Agonising Blast: already known", p)

    def test_offer_text_hides_known_passives(self):
        t = CP.offer_text(self.WYLL7, {"Agonizing Blast"})
        self.assertNotIn("Agonising Blast", t)
        self.assertIn("Sculptor of Flesh", t)


if __name__ == "__main__":
    unittest.main()
