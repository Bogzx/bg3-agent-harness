"""Offline tests for tools/dev/make_arena_save.py: the pairs spec (arena-mirror.v1), the kits against the game's data
(tools/dev/arena_static.json) and the symmetry check against a fake rpc.
Run: uv run pytest -q tools/dev/test_make_arena_save.py"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_arena_save as mas

POTIONS = ["OBJ_Potion_Healing", "OBJ_Potion_Healing"]
KITS: dict[str, dict[str, Any]] = {
    "Fighter": {
        "race": "HalfElf",
        "skills": ["Athletics", "Intimidation"],
        "ability_bonus": ["Strength", "Constitution"],
        "passives": ["FightingStyle_Defense"],
        "items": ["WPN_Longsword", "WPN_Shortbow", "ARM_ScaleMail_Body", "ARM_Boots_Leather", *POTIONS],
    },
    "Barbarian": {
        "race": "HalfElf",
        "skills": ["Athletics", "Survival"],
        "ability_bonus": ["Strength", "Constitution"],
        "items": [
            "WPN_Greataxe",
            {"item": "WPN_Handaxe", "equip": False},
            {"item": "WPN_Handaxe", "equip": False},
            "ARM_Barbarian",
            "ARM_Shoes_Barbarian",
            *POTIONS,
        ],
    },
    "Rogue": {
        "race": "HalfElf",
        "skills": ["Stealth", "Acrobatics", "Perception", "SleightOfHand"],
        "expertise": ["Stealth", "Perception"],
        "ability_bonus": ["Dexterity", "Constitution"],
        "items": [
            "WPN_Shortsword",
            "WPN_Shortbow",
            "ARM_Leather_Body_Rogue",
            "ARM_Boots_Leather",
            "OBJ_Kit_ThievesTools",
            "OBJ_GreaseBottle",
            "OBJ_GreaseBottle",
            *POTIONS,
        ],
    },
    "Wizard": {
        "race": "HalfElf",
        "skills": ["Arcana", "History"],
        "ability_bonus": ["Intelligence", "Constitution"],
        "spells": [
            ["Target_FireBolt", "Target_RayOfFrost", "Target_MageHand"],
            ["Target_MagicMissile", "Shout_Shield_Wizard", "Target_Sleep", "Projectile_IceKnife"],
        ],
        "items": ["WPN_Quarterstaff", "ARM_Robe_Body", "ARM_Shoes", *POTIONS],
    },
}
SPEC: dict[str, Any] = {
    "schema": "arena-mirror.v1",
    "name": "mirror_crash_site",
    "level": 1,
    "location": {"level": "WLD_Main_A", "spawns": {"A": [229.8, 5.1, 268.2], "B": [229.8, 5.6, 278.7]}},
    "pairs": [
        {"class": "Fighter", "template": None, "names": {"A": "Ash", "B": "Birch"}, "kit": KITS["Fighter"]},
        {"class": "Barbarian", "template": None, "names": {"A": "Alder", "B": "Bramble"}, "kit": KITS["Barbarian"]},
        {"class": "Rogue", "template": "Hireling_Rogue_x", "names": {"A": "Aspen", "B": "Beech"}, "kit": KITS["Rogue"]},
        {"class": "Wizard", "template": None, "names": {"A": "Acer", "B": "Briar"}, "kit": KITS["Wizard"]},
    ],
}
STATIC = mas.load_static()


def kit(cls: str, **over: Any) -> dict[str, Any]:
    k = copy.deepcopy(KITS[cls])
    k.update(over)
    return k


def spec(**over: Any) -> dict[str, Any]:
    s = copy.deepcopy(SPEC)
    s.update(over)
    return s


class SpecTest(unittest.TestCase):
    def test_valid_spec_with_null_templates(self):
        self.assertIs(mas.check_spec(spec())["name"], "mirror_crash_site")

    def test_building_needs_every_template(self):
        with self.assertRaisesRegex(mas.SpecError, "pair 1: no template yet"):
            mas.check_spec(spec(), need_templates=True)

    def test_each_problem_is_named(self):
        bad: list[tuple[dict[str, Any], str]] = [
            (spec(schema="arena.v1"), "schema must be"),
            (spec(name="Mirror"), "name must be"),
            (spec(level=0), "level must be one of 1, 2"),
            (spec(level=True), "level must be"),
            (spec(level=3), "level must be one of 1, 2"),
            (spec(location={"level": "WLD_Main_A", "spawns": {"A": [1, 2, 3]}}), "location.spawns.B"),
            (spec(pairs=SPEC["pairs"][:3]), "pairs must list 4"),
        ]
        druid = spec()
        druid["pairs"][3]["class"] = "Cleric"  # its domain is a subclass at level 1 (D300)
        bad.append((druid, "pair 4: class must be one of"))
        twice = spec()
        twice["pairs"][2]["names"]["B"] = "ASH"
        bad.append((twice, "pair 3: the name ASH is used twice"))
        lower = spec()
        lower["pairs"][0]["names"]["A"] = "ash"
        bad.append((lower, "pair 1: names.A must be a display name"))
        nokit = spec()
        del nokit["pairs"][1]["kit"]
        bad.append((nokit, "pair 2: kit must be an object"))
        for s, want in bad:
            with self.subTest(want=want), self.assertRaisesRegex(mas.SpecError, want):
                mas.check_spec(s)

    def test_load_spec_reads_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(SPEC))
            self.assertEqual(mas.load_spec(p)["level"], 1)
            p.write_text("{")
            with self.assertRaisesRegex(mas.SpecError, "cannot read"):
                mas.load_spec(p)

    def test_arena_teams_pair_i_at_index_i(self):
        uu = {"A": ["a1", "a2", "a3", "a4"], "B": ["b1", "b2", "b3", "b4"]}
        t = mas.arena_teams(spec(), uu)
        self.assertEqual(t["A"][0], {"name": "Ash", "uuid": "a1"})
        self.assertEqual(t["B"][3], {"name": "Briar", "uuid": "b4"})
        with self.assertRaisesRegex(mas.SpecError, "team B needs 4"):
            mas.arena_teams(spec(), {"A": uu["A"], "B": uu["B"][:3]})


class KitTest(unittest.TestCase):
    def test_selectors_parse_in_order(self):
        got = mas.selectors("SelectSkills(l1,2);AddSpells(l2);SelectSpells(l3,3,0,,,,AlwaysPrepared)")
        self.assertEqual(
            got,
            [
                ("SelectSkills", ["l1", "2"]),
                ("AddSpells", ["l2"]),
                ("SelectSpells", ["l3", "3", "0", "", "", "", "AlwaysPrepared"]),
            ],
        )

    def test_fighter_plan_from_the_game_rows(self):
        p = mas.kit_plan(KITS["Fighter"], "Fighter", 1, STATIC)
        self.assertEqual(p["race"], "76057327-da03-4398-aaf0-c59345ef3a8b")
        k = p["kit"]
        self.assertEqual(
            k["race"],
            {"boosts": "ActionResource(Movement,9,0)", "passives": ["HumanMilitia", "Darkvision", "FeyAncestry"]},
        )
        self.assertEqual(k["ability_bonus"], ["Strength", "Constitution"])
        self.assertEqual(
            k["choices"],
            [
                {
                    "kind": "skills",
                    "list": "6a4f8598-3a17-42d1-beac-d7ced582bafa",
                    "n": 2,
                    "exact": True,
                    "ids": ["Athletics", "Intimidation"],
                },
                {
                    "kind": "passives",
                    "list": "da3203d8-750a-4de1-b8eb-1eccfccddf46",
                    "n": 1,
                    "exact": True,
                    "ids": ["FightingStyle_Defense"],
                },
            ],
        )
        items = {i["item"]: (i["count"], i["equip"], bool(i["root"])) for i in k["items"]}
        self.assertEqual(items["OBJ_Potion_Healing"], (2, False, True))
        self.assertEqual(items["WPN_Longsword"], (1, True, True))
        self.assertEqual(len(k["items"]), 5)

    def test_every_class_kit_and_the_wizards_prepared_spells(self):
        for cls in mas.CLASSES:
            with self.subTest(cls=cls):
                mas.kit_plan(KITS[cls], cls, 1, STATIC)
        sp = [c for c in mas.kit_plan(KITS["Wizard"], "Wizard", 1, STATIC)["kit"]["choices"] if c["kind"] == "spells"]
        self.assertEqual([(c["n"], c["exact"], len(c["ids"])) for c in sp], [(3, True, 3), (6, False, 4)])
        axes = [
            i
            for i in mas.kit_plan(KITS["Barbarian"], "Barbarian", 1, STATIC)["kit"]["items"]
            if i["item"] == "WPN_Handaxe"
        ]
        self.assertEqual((axes[0]["count"], axes[0]["equip"]), (2, False))

    def test_level_2_rows_add_no_choice_but_a_wizard_subclass_refuses(self):
        mas.kit_plan(KITS["Fighter"], "Fighter", 2, STATIC)
        with self.assertRaisesRegex(mas.SpecError, "Wizard picks a subclass at level 2"):
            mas.kit_plan(KITS["Wizard"], "Wizard", 2, STATIC)

    def test_each_kit_problem_is_named(self):
        bad: list[tuple[str, dict[str, Any], str]] = [
            ("Fighter", kit("Fighter", skills=["Athletics"]), "skills has 1, the game's selector takes 2"),
            ("Fighter", kit("Fighter", skills=["Athletics", "Swimming"]), "Swimming is not one of"),
            ("Fighter", kit("Fighter", skills=["Athletics", "Athletics"]), "lists a name twice"),
            ("Fighter", kit("Fighter", expertise=["Athletics"]), "Fighter has no expertise choice"),
            ("Barbarian", kit("Barbarian", passives=["FightingStyle_Defense"]), "Barbarian has no passives choice"),
            ("Rogue", kit("Rogue", expertise=["Stealth", "Athletics"]), "expertise in Athletics, which the kit's"),
            ("Fighter", kit("Fighter", ability_bonus=None), "ability_bonus must name the +2 and the +1"),
            ("Fighter", kit("Fighter", ability_bonus=["Strength"]), "must name the +2 and the +1"),
            ("Fighter", kit("Fighter", ability_bonus=["Strength", "Luck"]), "Luck is not one of"),
            ("Wizard", kit("Wizard", spells=[["Target_FireBolt"]]), "spells must have 2 lists"),
            (
                "Wizard",
                kit("Wizard", spells=[["Target_FireBolt"], ["Target_Sleep"]]),
                "spells has 1, the game's selector takes 3",
            ),
            ("Fighter", kit("Fighter", race="Githyanki"), "race must be one of HalfElf"),
            (
                "Fighter",
                kit("Fighter", items=[*KITS["Fighter"]["items"], "OBJ_Scroll_Revivify"]),
                "not that: OBJ_Scroll_Revivify",
            ),
            ("Fighter", kit("Fighter", items=KITS["Fighter"]["items"][:-1]), "missing: OBJ_Potion_Healing"),
        ]
        for cls, k, want in bad:
            with self.subTest(want=want), self.assertRaisesRegex(mas.SpecError, re.escape(want)):
                mas.kit_plan(k, cls, 1, STATIC)


class SymmetryTest(unittest.TestCase):
    def test_ok_passes_the_answer_through(self):
        sent: list[tuple[str, dict[str, Any]]] = []

        def rpc(op: str, args: dict[str, Any]) -> Any:
            sent.append((op, args))
            return {"ok": True, "pairs": [{}, {}], "problems": []}

        self.assertTrue(mas.check_symmetry(rpc, [("a1", "b1"), ("a2", "b2")])["ok"])
        self.assertEqual(sent, [("arena.symmetry", {"pairs": [["a1", "b1"], ["a2", "b2"]]})])

    def test_any_problem_refuses(self):
        def rpc(op: str, args: dict[str, Any]) -> Any:
            return {"ok": False, "problems": ["pair 1 (Ash, Birch): race differs", "Birch: ac doesn't read"]}

        with self.assertRaisesRegex(mas.AsymmetryError, r"race differs; Birch: ac doesn't read"):
            mas.check_symmetry(rpc, [("a1", "b1")])

    def test_an_odd_answer_refuses(self):
        with self.assertRaisesRegex(mas.AsymmetryError, "answered None"):
            mas.check_symmetry(lambda op, a: None, [("a1", "b1")])

    def test_cli_validate_and_bad_pair_argument(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(SPEC))
            self.assertEqual(mas.main(["validate", str(p)]), 0)
            p.write_text(json.dumps(spec(level=99)))
            self.assertEqual(mas.main(["validate", str(p)]), 1)
        with self.assertRaises(SystemExit):
            mas.main(["symmetry", "a1b1"])


if __name__ == "__main__":
    unittest.main()
