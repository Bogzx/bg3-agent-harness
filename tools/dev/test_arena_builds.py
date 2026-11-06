"""Offline tests for the A1 races in the kit (make_arena_save.race_plan, orchestrator D411) and the arena build files
(tools/dev/arena_builds.py, schema arena-builds.v1) against the game's data (tools/dev/arena_static.json).
Run: uv run pytest -q tools/dev/test_arena_builds.py"""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arena_builds as ab
import make_arena_save as mas

STATIC = mas.load_static()
REPO = Path(__file__).resolve().parents[2]
BUILDS = REPO / "packages/bench/builds/l2_hirelings_v1.json"
DOC: dict[str, Any] = json.loads(BUILDS.read_text())
WOOD, HUMAN, GOLD, SELDARINE = (STATIC["races"][r]["race"] for r in ("HalfElf_Wood", "Human", "Dwarf_GoldHill",
                                                                      "Drow_Seldarine"))  # fmt: skip


def doc(**over: Any) -> dict[str, Any]:
    d = copy.deepcopy(DOC)
    d.update(over)
    return d


def build(i: int, d: dict[str, Any]) -> dict[str, Any]:
    return d["builds"][i]


def kit(cls: str, race: str, **over: Any) -> dict[str, Any]:
    """The build file's kit for that class (its own race and background), the race swapped, `over` on top."""
    b = next(b for b in DOC["builds"] if b["class"] == cls)
    k = ab.kit_of(b, 2, STATIC)
    k["race"] = race
    k.update(over)
    return {x: v for x, v in k.items() if v is not None}


class RaceTest(unittest.TestCase):
    def test_a_subrace_kit_carries_both_rows_parent_first_and_names_its_race(self):
        r = mas.kit_plan(kit("Fighter", "HalfElf_Wood"), "Fighter", 2, STATIC)
        self.assertEqual(r["race"], WOOD)
        self.assertEqual(
            r["kit"]["race"],
            {
                "race": WOOD,
                "boosts": "ActionResource(Movement,9,0);ActionResource(Movement,1.5,0);ProficiencyBonus(Skill,Stealth)",
                "passives": ["HumanMilitia", "Darkvision", "FeyAncestry"],
            },
        )
        g = mas.kit_plan(kit("Barbarian", "Dwarf_GoldHill"), "Barbarian", 2, STATIC)["kit"]["race"]
        self.assertEqual(g["race"], GOLD)
        self.assertEqual(g["boosts"], "ActionResource(Movement,7.5,0);IncreaseMaxHP(Level)")
        self.assertEqual(g["passives"], ["Dwarf_DwarvenCombatTraining", "Darkvision", "Dwarf_DwarvenResilience"])

    def test_a_race_spell_list_becomes_the_race_parts_spells(self):
        r = mas.kit_plan(kit("Ranger", "Drow_Seldarine"), "Ranger", 2, STATIC)["kit"]["race"]
        self.assertEqual((r["race"], r["spells"]), (SELDARINE, ["Target_DancingLights"]))
        self.assertIn("ProficiencyBonus(Skill,Perception)", r["boosts"])

    def test_a_race_spell_list_not_in_the_data_is_refused(self):
        st = copy.deepcopy(STATIC)
        del st["spell_lists"]["d707ec70-da8a-457b-9b69-7cf5205eef0e"]
        with self.assertRaisesRegex(mas.SpecError, r"race Drow_Seldarine has AddSpells\(d707ec70"):
            mas.kit_plan(kit("Ranger", "Drow_Seldarine"), "Ranger", 2, st)

    def test_humans_skill_choice_comes_first_and_must_be_made(self):
        k = mas.kit_plan(kit("Rogue", "Human"), "Rogue", 2, STATIC)["kit"]
        self.assertEqual(
            k["choices"][0],
            {"kind": "skills", "list": "f974ebd6-3725-4b90-bb5c-2b647d41615d", "n": 1, "exact": True,
             "ids": ["Athletics"]},
        )  # fmt: skip
        with self.assertRaisesRegex(mas.SpecError, "race_skills has 0, the race Human's selector takes 1"):
            mas.kit_plan(kit("Rogue", "Human", race_skills=[]), "Rogue", 2, STATIC)
        with self.assertRaisesRegex(mas.SpecError, "race HalfElf_Wood has no skill choice"):
            mas.kit_plan(kit("Fighter", "HalfElf_Wood", race_skills=["Arcana"]), "Fighter", 2, STATIC)

    def test_a_skill_the_race_or_background_gives_cant_be_picked_again(self):
        cases = [
            (kit("Fighter", "HalfElf_Wood", skills=["Stealth", "Survival"]), "Fighter", "race HalfElf_Wood"),
            (kit("Fighter", "HalfElf_Wood", skills=["Athletics", "Survival"]), "Fighter", "background Soldier"),
            (kit("Rogue", "Human", race_skills=["Perception"]), "Rogue", "Perception is picked by two"),
        ]
        for k, cls, why in cases:
            with self.subTest(why=why), self.assertRaisesRegex(mas.SpecError, why):
                mas.kit_plan(k, cls, 2, STATIC)

    def test_expertise_may_be_in_a_background_skill_and_the_background_passive_is_a_player_passive(self):
        k = mas.kit_plan(kit("Rogue", "Human"), "Rogue", 2, STATIC)["kit"]
        self.assertEqual(k["player"], {"passives": ["DeathSavingThrows", "Background_Urchin"]})
        with self.assertRaisesRegex(mas.SpecError, "expertise in Arcana"):
            mas.kit_plan(kit("Rogue", "Human", expertise=["Arcana", "Stealth"]), "Rogue", 2, STATIC)
        with self.assertRaisesRegex(mas.SpecError, "background must be one of"):
            mas.kit_plan(kit("Rogue", "Human", background="Noble"), "Rogue", 2, STATIC)

    def test_a_race_choice_a_kit_cant_make_is_refused(self):
        st = copy.deepcopy(STATIC)
        st["races"]["Human"]["rows"][0]["Selectors"] = "SelectAbilities(3d6edcc3,2,1,HalfElfAbilityScore)"
        with self.assertRaisesRegex(mas.SpecError, r"race Human has SelectAbilities\(3d6edcc3\)"):
            mas.kit_plan(kit("Rogue", "Human"), "Rogue", 2, st)

    def test_every_hireling_names_a_known_race_background_and_template(self):
        for cls, h in STATIC["hirelings"].items():
            with self.subTest(cls=cls):
                self.assertIn(h["race"], STATIC["races"])
                self.assertIn(h["background"], STATIC["backgrounds"])
                if h["template"] is None:  # not read yet (a binary .lsf): arena_static_gen says so in its todo
                    self.assertIn("root template", h["todo"])
                else:
                    self.assertRegex(h["template"], r"^[0-9a-f-]{36}$")
        self.assertEqual(STATIC["hirelings"]["Fighter"]["template"], "d9401d40-453b-4512-84ee-d1c6ab29afea")


class BuildsTest(unittest.TestCase):
    def test_the_l2_file_is_a_valid_draft_with_a_hireling_race_per_build(self):
        d = ab.check_builds(doc())
        self.assertEqual(
            [b["race"] for b in d["builds"]], ["HalfElf_Wood", "Human", "Dwarf_GoldHill", "Drow_Seldarine"]
        )

    def test_a_rated_file_needs_every_reference_on_its_game_build(self):
        d = doc()
        del d["draft"]
        with self.assertRaisesRegex(ab.BuildError, "build fighter: no reference profile"):
            ab.check_builds(d)
        for b in d["builds"]:
            b["reference"] = {
                "profile": {"level": 2},
                "game_build": "25605617",
                "hireling": STATIC["hirelings"][b["class"]]["origin"],
                "evidence": "runtime/x.json",
            }
        self.assertIs(ab.check_builds(d), d)
        d["builds"][3]["reference"]["game_build"] = "25600000"
        with self.assertRaisesRegex(ab.BuildError, "build ranger: its reference was read on game build 25600000"):
            ab.check_builds(d)

    def test_a_reference_must_be_the_classs_hireling(self):
        d = doc()
        build(0, d)["reference"] = {
            "profile": {},
            "game_build": "25605617",
            "hireling": STATIC["hirelings"]["Rogue"]["origin"],
            "evidence": "x",
        }
        with self.assertRaisesRegex(ab.BuildError, "reference must be null or .* of the Fighter hireling"):
            ab.check_builds(d)

    def test_refusals(self):
        def bad(change, why):
            d = doc()
            change(d)
            with self.subTest(why=why), self.assertRaisesRegex(mas.SpecError, why):
                ab.check_builds(d)

        bad(lambda d: build(0, d).update(race="Human"), "race is Human, the Fighter hireling's is HalfElf_Wood")
        bad(lambda d: build(1, d).update(background="Soldier"), "background is Soldier, the Rogue hireling's is Urchin")
        bad(lambda d: build(0, d)["script"]["cc"]["abilities"].update(Strength=16), "stat entry's point-buy")
        bad(lambda d: build(3, d)["script"]["level_ups"][0].pop("spells"), "level-up 2 spells must have 1 lists")
        bad(lambda d: build(3, d)["script"]["cc"]["passives"].pop(), r"script.cc.passives must have 2 lists")
        bad(lambda d: build(0, d)["script"].update(level_ups=[]), "level_ups must list levels 2..2")
        bad(lambda d: build(0, d)["script"]["cc"].update(subclass="x"), "script.cc must have abilities and only")
        bad(lambda d: build(1, d).update(id="fighter"), "build id fighter is used twice")
        bad(lambda d: build(2, d).pop("reference"), "a build must be")
        bad(lambda d: build(2, d)["gear"].pop(), "items must be EQP_CC_Barbarian")
        bad(lambda d: d["builds"].pop(), "builds must list 4")
        bad(lambda d: d.update(tier="L5"), "tier L<level>")
        bad(lambda d: d.update(draft=" "), "draft must say why")
        bad(lambda d: d.update(extra=1), "unknown keys: extra")

    def test_the_l2_file_fits_the_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        schema = json.loads((REPO / "packages/bench/schema/arena-builds.v1.schema.json").read_text())
        jsonschema.validate(DOC, schema)
        bad = doc()
        build(0, bad)["script"]["cc"]["subclass"] = "x"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)

    def test_the_abilities_are_read_from_our_stat_entries(self):
        self.assertEqual(ab.stat_abilities()["Rogue"]["Dexterity"], 15)
        self.assertEqual(set(ab.stat_abilities()), set(mas.CLASSES))

    def test_validate_cli_draft_ok_rated_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(mas.main(["validate", str(BUILDS)]), 0)
            self.assertEqual(mas.main(["validate", "--rated", str(BUILDS)]), 1)
        self.assertIn("l2_hirelings_v1: ok (draft)", out.getvalue())
        self.assertIn("l2_hirelings_v1 is a draft", err.getvalue())
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "b.json"
            p.write_text(json.dumps(doc(builds=[])))
            with redirect_stderr(io.StringIO()) as e:
                self.assertEqual(mas.main(["validate", str(p)]), 1)
            self.assertIn("builds must list 4", e.getvalue())

    def test_the_mirror_section_takes_each_pairs_template_race_and_kit_from_its_build(self):
        names = [("Red Ash", "Blue Ash"), ("Red Birch", "Blue Birch"), ("Red Cedar", "Blue Cedar"),
                 ("Red Dune", "Blue Dune")]  # fmt: skip
        m = ab.mirror_section(ab.check_builds(doc()), names)
        pairs = m["mirror"]["pairs"]
        self.assertEqual([p["race"] for p in pairs], [WOOD, HUMAN, GOLD, SELDARINE])
        self.assertEqual([p["kit"]["race"]["race"] for p in pairs], [WOOD, HUMAN, GOLD, SELDARINE])
        self.assertEqual(
            [p["template"] for p in pairs],
            [STATIC["hirelings"][c]["template"] for c in ("Fighter", "Rogue", "Barbarian", "Ranger")],
        )
        self.assertEqual([p["build"] for p in pairs], ["fighter", "rogue", "barbarian", "ranger"])
        self.assertEqual(m["teams"]["B"][3], {"name": "Blue Dune"})
        with self.assertRaisesRegex(ab.BuildError, "unique across both teams"):
            ab.mirror_section(ab.check_builds(doc()), [("Red Ash", "red ash"), *names[1:]])


if __name__ == "__main__":
    unittest.main()


def tier(level: int) -> dict[str, Any]:
    return json.loads((REPO / f"packages/bench/builds/l{level}_4v4_v1.json").read_text())


def tbuild(d: dict[str, Any], bid: str) -> dict[str, Any]:
    return next(b for b in d["builds"] if b["id"] == bid)


def plan(d: dict[str, Any], bid: str) -> dict[str, Any]:
    b = tbuild(d, bid)
    return mas.kit_plan(
        ab.kit_of(b, d["level"], STATIC), b["class"], d["level"], STATIC, f"build {bid}",
        base=ab.stat_abilities()[b["class"]],
    )["kit"]  # fmt: skip


class TierTest(unittest.TestCase):
    """The 4v4 tiers (website repo ops/logs/arena-4v4-builds.md): Fighter (Battle Master), Cleric (Life), Wizard
    (Evocation), Rogue (Thief) at levels 2, 5 and 12; subclass rows, improvements and prepared spells checked."""

    def test_the_three_tier_files_are_valid_drafts_of_the_same_four_builds(self):
        for lv in (2, 5, 12):
            with self.subTest(level=lv):
                d = ab.check_builds(tier(lv))
                self.assertIn("draft", d)
                self.assertEqual([b["class"] for b in d["builds"]], ["Fighter", "Cleric", "Wizard", "Rogue"])
                schema = json.loads((REPO / "packages/bench/schema/arena-builds.v1.schema.json").read_text())
                __import__("jsonschema").validate(d, schema)
        self.assertEqual([b["subclass"] for b in tier(2)["builds"]], [None, "LifeDomain", "EvocationSchool", None])

    def test_subclass_rows_are_the_kits_grants_and_their_selectors_its_choices(self):
        k = plan(tier(5), "fighter")
        self.assertEqual(k["subclass"], STATIC["subclasses"]["BattleMaster"]["uuid"])
        # Battle Master 3: 4 superiority dice (a fixed part the game won't apply to a copy) and 3 maneuvers
        self.assertIn("ActionResource(SuperiorityDie,4,0)", k["grants"]["boosts"])
        self.assertIn("Ability(Strength,2)", k["grants"]["boosts"])  # level 4's ASI
        man = [c for c in k["choices"] if c["kind"] == "passives" and c["n"] == 3]
        self.assertEqual(man[0]["ids"], ["PrecisionAttack", "Riposte", "GoadingAttack"])
        cl = plan(tier(5), "cleric")
        self.assertIn("DiscipleOfLife", cl["grants"]["passives"])
        self.assertTrue({"Target_Bless", "Target_CureWounds", "Shout_Aid", "Shout_BeaconOfHope"}
                        <= set(cl["grants"]["spells"]))  # fmt: skip
        self.assertEqual(len(cl["prepared"]), 9)  # Wis 19 (+4) + level 5
        self.assertEqual(cl["choices"][0], {"kind": "spells", "list": "3cae2e56-9871-4cef-bba6-96845ea765fa",
                                            "n": 1, "exact": True, "ids": ["Projectile_FireBolt"]})  # fmt: skip

    def test_improvements_feats_and_tier_items(self):
        k12 = plan(tier(12), "fighter")
        self.assertIn("Tough", k12["grants"]["passives"])
        self.assertTrue({"Sentinel_Attack", "Sentinel_ZeroSpeed"} <= set(k12["grants"]["passives"]))
        self.assertEqual({i["item"] for i in plan(tier(5), "fighter")["items"]},
                         {"WPN_Longsword_1", "WPN_Shortbow_1", "ARM_ScaleMail_Body_1", "ARM_Boots_Leather",
                          "OBJ_Potion_Healing", "OBJ_Potion_Healing_Greater"})  # fmt: skip
        self.assertIn("OBJ_Potion_Healing_Superior", {i["item"] for i in k12["items"]})
        self.assertIn("WPN_Longsword_2", {i["item"] for i in k12["items"]})
        r = plan(tier(12), "rogue")
        self.assertEqual([c["ids"] for c in r["choices"] if c["kind"] == "expertise"],
                         [["Stealth", "Perception"], ["Acrobatics", "Athletics"]])  # fmt: skip

    def test_no_mirror_section_while_a_builds_hireling_template_is_unread(self):
        names = [(f"Red {n}", f"Blue {n}") for n in ("Ash", "Birch", "Cedar", "Dune")]
        with self.assertRaisesRegex(ab.BuildError, "build cleric: the Cleric hireling's root template is not read"):
            ab.mirror_section(ab.check_builds(tier(2)), names)

    def test_l2_kits_without_a_subclass_or_preparer_are_unchanged_in_shape(self):
        k = plan(tier(2), "rogue")
        self.assertEqual(set(k), {"race", "player", "ability_bonus", "choices", "items"})

    def test_refusals(self):
        def bad(fn, why, level=5):
            d = copy.deepcopy(tier(level))
            fn(d)
            with self.subTest(why=why), self.assertRaisesRegex(mas.SpecError, why):
                ab.check_builds(d)

        bad(lambda d: tbuild(d, "fighter").update(subclass=None), "Fighter picks a subclass at level 3")
        bad(lambda d: tbuild(d, "fighter").update(subclass="LifeDomain"), "LifeDomain is not a Fighter subclass")
        bad(lambda d: tbuild(d, "fighter").update(subclass="BattleMaster"), "picks no subclass by level 2", 2)
        bad(lambda d: tbuild(d, "rogue")["script"]["level_ups"][2].pop("improvement"), "level-up 4 is an improvement")
        bad(
            lambda d: tbuild(d, "rogue")["script"]["level_ups"][1].update(improvement={"asi": {"Dexterity": 2}}),
            "level-up 3 has no improvement",
        )
        bad(
            lambda d: tbuild(d, "rogue")["script"]["level_ups"][2].update(improvement={"asi": {"Dexterity": 3}}),
            "asi must give 2 points",
        )
        bad(
            lambda d: tbuild(d, "rogue")["script"]["level_ups"][2].update(improvement={"feat": "Flying"}),
            "no feat 'Flying'",
        )
        bad(
            lambda d: tbuild(d, "rogue")["script"]["level_ups"][8].update(improvement={"feat": "Tough"}),
            "Tough is taken twice",
            12,
        )
        bad(lambda d: tbuild(d, "cleric")["script"]["prepared"].pop(), "prepared has 8, a level-5 Cleric prepares 9")
        bad(
            lambda d: tbuild(d, "cleric")["script"]["prepared"].__setitem__(0, "Projectile_Fireball"),
            "prepared Projectile_Fireball is not a spell the Cleric has to prepare",
        )
        bad(
            lambda d: tbuild(d, "wizard")["script"]["prepared"].__setitem__(0, "Target_Haste"),
            "prepared Target_Haste is not a spell",
            5,
        )  # Haste is in the spellbook only from level 6
        bad(
            lambda d: tbuild(d, "fighter")["script"]["level_ups"][1]["passives"][0].__setitem__(0, "Fireball"),
            "Fireball is not in the game's list Battlemaster Maneuvers",
        )
        bad(
            lambda d: tbuild(d, "cleric")["script"]["cc"].update(race_spells=["Target_SacredFlame"]),
            "Target_SacredFlame is not in the game's list Wizard cantrips",
        )
        bad(
            lambda d: tbuild(d, "rogue").update(script={**tbuild(d, "rogue")["script"], "prepared": []}),
            "Rogue doesn't prepare spells",
        )
        bad(lambda d: tbuild(d, "fighter")["gear"].__setitem__(0, "WPN_Longsword"), "not that: WPN_Longsword")
        bad(
            lambda d: tbuild(d, "wizard")["script"]["level_ups"][6].update(improvement={"asi": {"Intelligence": 2}}),
            "Intelligence above 20",
            12,
        )
