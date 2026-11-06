"""The arena's bar over every build: knock-downs and side-changing spells banned, incapacitating spells allowed
(orchestrator D130, 2026-11-06), followed through items, class rows, kit, race and passives. Offline: the walk runs on
the checked-in arena_stat_graph.json (tools/dev/arena_scan.py), no paks."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arena_scan as scan
import make_arena_save as mas

GRAPH = scan.load_graph()
STATIC = mas.load_static()
BUILD_FILES = sorted(scan.BUILDS.glob("l*.json"))
KNOCK_DOWNS = ("Target_Command_Container", "Target_Grease", "Zone_Thunderwave", "Target_HideousLaughter",
               "Projectile_IceKnife", "TripAttack", "PushingAttack", "Target_Topple", "Projectile_ChromaticOrb",
               "Target_SleetStorm", "TotemSpirit_Elk", "WPN_Quarterstaff", "WPN_Quarterstaff_1",
               "WPN_Quarterstaff_2")  # fmt: skip
SIDE_CHANGES = ("Target_CharmPerson", "Target_DominatePerson", "Target_CrownOfMadness")
CLEAN = ("Projectile_MagicMissile", "Projectile_FireBolt", "Target_CureWounds", "Target_Bless", "Target_Friends",
         "DeathSavingThrows", "Target_DeathWard", "FeyAncestry", "WPN_Dagger", "WPN_Dagger_1",
         "WPN_Dagger_2")  # fmt: skip
# D130's ruling by name: GuardCast refuses incapacitating spells, so these stay.
INCAPACITATING = ("Target_Haste", "Target_HoldMonster", "Target_Banishment", "Target_Cloudkill", "Target_BestowCurse")
# The one draft nothing plays: a Cleric's class rows add its whole list, which no call removes (S1 P3; REVIEW-12), so
# the L5 and L12 teams hold a Bard (owner, D114). What it reaches is named so anything new here still fails.
ACCEPTED = {
    ("l2_4v4_v1.json", "cleric"): {
        "COMMAND_GROVEL: knock-down: SG_Prone",
        "COMMAND_GROVEL: knock-down: StatusType KNOCKED_DOWN",
        "KNOCKED_DOWN: knock-down: SG_Prone",
        "KNOCKED_DOWN: knock-down: StatusType KNOCKED_DOWN",
        "SANCTUARY_AURA: side: CannotHarmCauseEntity in Boosts",
    }
}
STATS = """
// a comment
new entry "Base"
type "SpellData"
data "SpellSuccess" "ApplyStatus(HURT,100,1)"
data "Icon" "PRONE"

new entry "Child"
type "SpellData"
using "Base"
data "SpellFail" "ApplyStatus(NOPE,100,1)"

new entry "HURT"
type "StatusData"
data "OnApplyFunctors" "RemoveStatus(FALLEN)"
data "Boosts" "StatusImmunity(FALLEN);Advantage(AttackRoll)"

new entry "NOPE"
type "StatusData"
data "StatusGroups" "SG_Charmed"
data "Boosts" "Advantage(SourceDialogue,Charisma)"

new entry "FALLEN"
type "StatusData"
data "StatusType" "KNOCKED_DOWN"
data "StatusGroups" "SG_Prone"

new entry "Trip"
type "SpellData"
data "SpellSuccess" "IF(not HasStatus('PRONE')):ApplyStatus(FALLEN,100,1)"

new entry "Guarded"
type "SpellData"
data "SpellSuccess" "IF(not Tagged('PRONE')):DealDamage(1d4,Fire)"

new entry "Push"
type "SpellData"
data "SpellSuccess" "Force(6)"
data "SpellFail" "CreateSurface(2,2,Fire)"

new entry "Slick"
type "SpellData"
data "SpellSuccess" "CreateSurface(4,10,Grease)"

new entry "Rimed"
type "SpellData"
data "SpellSuccess" "CreateSurface(2,2,WaterFrozen)"

new entry "Turncoat"
type "StatusData"
data "Boosts" "FactionOverride(Source)"

new entry "Thrall"
type "StatusData"
data "StatusGroups" "SG_Dominated;SG_Condition"

new entry "Crook"
type "Weapon"
data "BoostsOnEquipMainHand" "UnlockSpell(Trip)"
data "PersonalStatusImmunities" "FALLEN"

new entry "Hero"
type "Character"
data "Passives" "Trip"
"""


def entries() -> scan.Entries:
    out: scan.Entries = {}
    scan.parse(STATS, out)
    return out


class Parse(unittest.TestCase):
    def test_using_gives_the_parents_fields_and_a_later_file_replaces_an_entry(self):
        e = entries()
        self.assertEqual(sorted(scan.fields(e, "Child")), ["Icon", "SpellFail", "SpellSuccess"])
        scan.parse('new entry "Base"\ntype "SpellData"\ndata "SpellSuccess" "ApplyStatus(NOPE,100,1)"\n', e)
        self.assertEqual(scan.refs(e, "Child"), ["NOPE"])

    def test_a_field_refers_to_what_it_does_not_what_it_takes_away_refuses_or_asks(self):
        e = entries()
        self.assertEqual(scan.refs(e, "Child"), ["HURT", "NOPE"])
        self.assertEqual(scan.refs(e, "HURT"), [])
        self.assertEqual(scan.refs(e, "Crook"), ["Trip"])
        self.assertEqual(scan.refs(e, "Trip"), ["FALLEN"])

    def test_what_counts_against_the_bar(self):
        e = entries()
        self.assertEqual(scan.hits(e, "FALLEN"), ["knock-down: SG_Prone", "knock-down: StatusType KNOCKED_DOWN"])
        self.assertEqual(scan.hits(e, "Push"), ["knock-down: Force in SpellSuccess"])
        self.assertEqual(scan.hits(e, "Slick"), ["knock-down: CreateSurface(4,10,Grease)"])
        self.assertEqual(scan.hits(e, "Rimed"), ["knock-down: CreateSurface(2,2,WaterFrozen)"])
        self.assertEqual(scan.hits(e, "Turncoat"), ["side: FactionOverride in Boosts"])
        self.assertEqual(scan.hits(e, "Thrall"), ["side: SG_Dominated"])
        for clean in ("Base", "Guarded", "HURT", "NOPE"):  # a look, a condition, a removal, a dialogue charm
            self.assertEqual(scan.hits(e, clean), [], clean)

    def test_the_graph_holds_what_is_followed_and_survives_its_file(self):
        g = scan.graph(entries())
        self.assertNotIn("Hero", g)
        self.assertEqual(g["Crook"], {"refs": ["Trip"]})
        self.assertEqual(g["HURT"], {})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "g.json"
            scan.write_graph(g, path)
            self.assertEqual(scan.load_graph(path), g)


class Walk(unittest.TestCase):
    def test_it_follows_an_item_to_a_status_and_says_the_way(self):
        found, missing = scan.walk(scan.graph(entries()), ["Crook"])
        self.assertEqual(found["FALLEN: knock-down: SG_Prone"], ["Crook", "Trip", "FALLEN"])
        self.assertEqual(missing, [])

    def test_an_id_the_graph_lacks_is_named(self):
        self.assertEqual(scan.walk(scan.graph(entries()), ["Nowhere"]), ({}, ["Nowhere"]))


class Seeds(unittest.TestCase):
    def test_a_row_gives_its_passives_unlocks_and_lists_and_names_a_list_it_cant_read(self):
        static = {"lists": {"aaaa-1": {"ids": ["Listed"]}}, "spell_lists": {"bbbb-2": {"spells": ["Added"]}}}
        row = {"PassivesAdded": "Own;Other", "Boosts": "UnlockSpell(Open,x)",
               "Selectors": "AddSpells(aaaa-1);AddSpells(bbbb-2,,,);AddSpells(cccc-3)"}  # fmt: skip
        seeds, unread = scan.row_seeds([{"source": "class", "level": 1, "row": row}], static)
        self.assertEqual(sorted(seeds), ["Added", "Listed", "Open", "Other", "Own"])
        self.assertEqual(unread, ["class L1 AddSpells cccc-3"])

    def test_a_kit_gives_its_picks_grants_race_player_and_items(self):
        kit = {
            "choices": [{"kind": "spells", "ids": ["Pick"]}, {"kind": "skills", "ids": ["Stealth"]}],
            "grants": {"passives": ["Granted"], "spells": ["Cast"], "boosts": "UnlockSpell(Open)"},
            "race": {"passives": ["Racial"], "spells": []},
            "player": {"passives": ["DeathSavingThrows"]},
            "items": [{"item": "WPN_Dagger"}],
            "prepared": ["Ready"],
        }
        want = ["Cast", "DeathSavingThrows", "Granted", "Open", "Pick", "Racial", "Ready", "WPN_Dagger"]
        self.assertEqual(sorted(scan.kit_seeds(kit)), want)


class Bar(unittest.TestCase):
    def test_the_controls_hit(self):
        for name in KNOCK_DOWNS:
            with self.subTest(name):
                self.assertTrue([h for h in scan.walk(GRAPH, [name])[0] if ": knock-down: " in h])
        for name in SIDE_CHANGES:
            with self.subTest(name):
                self.assertTrue([h for h in scan.walk(GRAPH, [name])[0] if ": side: " in h])

    def test_a_clean_spell_doesnt(self):
        for name in CLEAN:
            with self.subTest(name):
                self.assertEqual(scan.walk(GRAPH, [name]), ({}, []))

    def test_incapacitating_spells_are_allowed(self):
        for name in INCAPACITATING:
            with self.subTest(name):
                self.assertEqual(scan.walk(GRAPH, [name]), ({}, []))

    def test_no_build_reaches_a_knock_down_or_a_side_change(self):
        self.assertTrue(BUILD_FILES)
        seen = 0
        for path in BUILD_FILES:
            doc = json.loads(path.read_text())
            for b in doc["builds"]:
                with self.subTest(file=path.name, build=b["id"]):
                    seeds, unread = scan.build_seeds(b, doc, STATIC)
                    found, missing = scan.walk(GRAPH, seeds)
                    self.assertEqual((unread, missing), ([], []))
                    self.assertEqual(set(found), ACCEPTED.get((path.name, b["id"]), set()))
                    seen += 1
        self.assertGreaterEqual(seen, 20)

    def test_a_wizard_with_its_staff_back_would_be_caught(self):
        doc = json.loads((scan.BUILDS / "l5_4v4_v1.json").read_text())
        wizard = next(b for b in doc["builds"] if b["class"] == "Wizard")
        seeds, _ = scan.build_seeds(wizard, doc, STATIC)
        found, _ = scan.walk(GRAPH, [*seeds, "WPN_Quarterstaff_1"])
        self.assertIn("Target_Topple: knock-down: PRONE in SpellSuccess", found)
        wizard["gear"][wizard["gear"].index("WPN_Dagger_1")] = "WPN_Quarterstaff_1"
        with self.assertRaisesRegex(mas.SpecError, "items must be EQP_CC_Wizard"):
            scan.build_seeds(wizard, doc, STATIC)

    def test_the_kits_added_weapon_is_one_its_class_can_use(self):
        boosts = STATIC["classes"]["Wizard"]["levels"]["1"]["Boosts"]
        self.assertIn("Proficiency(Daggers)", boosts)
        self.assertEqual(mas.KIT_ADDS, {"Wizard": ("WPN_Dagger",)})


if __name__ == "__main__":
    unittest.main()
