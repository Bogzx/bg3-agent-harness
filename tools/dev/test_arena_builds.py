"""Offline tests for the A1 races in the kit (make_arena_save.race_plan, orchestrator D411) and the arena build files
(tools/dev/arena_builds.py, schema arena-builds.v1) against the game's data (tools/dev/arena_static.json).
Run: uv run pytest -q tools/dev/test_arena_builds.py"""

from __future__ import annotations

import copy
import io
import json
import re
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

    def test_a_race_row_above_level_1_counts_from_its_level(self):
        # The game's Drow table: Faerie Fire at 3, Darkness at 5. A real Drow of that level has them, so its twin must.
        lights, fire, dark = "Target_DancingLights", "Target_FaerieFire_DrowMagic", "Target_Darkness_DrowMagic"
        for level, want in ((2, [lights]), (3, [lights, fire]), (4, [lights, fire]), (5, [lights, fire, dark])):
            with self.subTest(level=level):
                part = mas.race_plan(kit("Ranger", "Drow_Seldarine"), STATIC, "x", "Wisdom", level)[1]
                self.assertEqual(part["spells"], want)
                self.assertEqual(part["spell_abilities"], dict.fromkeys(want, "Charisma"), "the row's own")

    def test_a_race_row_above_level_1_with_a_choice_a_kit_cant_make_is_refused(self):
        st = copy.deepcopy(STATIC)
        st["races"]["Drow_Seldarine"]["above"][0]["Selectors"] = "SelectPassives(aaaaaaaa,1)"
        self.assertEqual(
            mas.race_plan(kit("Ranger", "Drow_Seldarine"), st, "x", None, 2)[1]["spells"], ["Target_DancingLights"]
        )
        with self.assertRaisesRegex(mas.SpecError, r"race Drow_Seldarine has SelectPassives\(aaaaaaaa"):
            mas.race_plan(kit("Ranger", "Drow_Seldarine"), st, "x", None, 3)

    def test_a_kit_of_a_tier_holds_its_races_spells_of_that_tier(self):
        r = mas.kit_plan(kit("Ranger", "Drow_Seldarine"), "Ranger", 2, STATIC)["kit"]["race"]
        self.assertEqual(r["spells"], ["Target_DancingLights"], "L2's Ranger has no row above")
        pair = next(p for p in json.loads((REPO / "packages/bench/arenas/builds-l5-4v4-team2-v1.json").read_text())
                    ["mirror"]["pairs"] if p["class"] == "Ranger")  # fmt: skip
        self.assertEqual(
            pair["kit"]["race"]["spells"],
            ["Target_DancingLights", "Target_FaerieFire_DrowMagic", "Target_Darkness_DrowMagic"],
        )
        self.assertEqual(pair["kit"]["spell_abilities"]["Target_Darkness_DrowMagic"], "Charisma")

    def test_a_spell_keeps_the_ability_its_row_gives_else_its_classs(self):
        doc = json.loads((REPO / "packages/bench/builds/l2_hirelings_v1.json").read_text())
        b = next(x for x in doc["builds"] if x["class"] == "Ranger")
        r = mas.kit_plan(ab.kit_of(b, 2, STATIC), "Ranger", 2, STATIC)
        self.assertEqual(
            r["kit"]["spell_abilities"],
            {"Target_DancingLights": "Charisma", "Target_HuntersMark": "Wisdom", "Target_CureWounds": "Wisdom"},
        )
        self.assertNotIn("spell_abilities", r["kit"]["race"])
        self.assertTrue(all("ability" not in c for c in r["kit"]["choices"]), "nothing the mod doesn't read")

    def test_a_kit_with_no_spells_of_its_own_has_no_spell_abilities(self):
        r = mas.kit_plan(kit("Fighter", "HalfElf_Wood"), "Fighter", 2, STATIC)
        self.assertNotIn("spell_abilities", r["kit"])

    def test_a_subclass_rows_spell_with_no_ability_anywhere_reads_the_stats(self):
        """S4, live 2026-11-07: a Thief's 9th level, AddSpells with no ability, a class with none."""
        self.assertEqual(plan(tier(12), "rogue")["spell_abilities"], {"Shout_SupremeSneak": mas.STATS_CASTING})

    def test_a_row_that_names_no_ability_and_a_class_with_none_writes_nothing(self):
        part = mas.race_plan(kit("Fighter", "Drow_Seldarine"), STATIC, "x")[1]
        self.assertEqual(part["spell_abilities"], {"Target_DancingLights": "Charisma"}, "the row's own")
        self.assertIsNone(mas._row_ability(["list", "", ""], 2))
        self.assertIsNone(mas._row_ability(["list"], 2))

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
        d.pop("draft", None)
        for b in d["builds"]:
            b["reference"] = None  # a live read may already be in the file
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
        bad(
            lambda d: build(3, d)["script"]["cc"]["skills"].__setitem__(2, "Investigation"),
            "Investigation is picked, but passive FavoredEnemy_BountyHunter already gives it",
        )
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
        with tempfile.TemporaryDirectory() as t:
            draft = Path(t) / "draft.json"
            draft.write_text(json.dumps({**doc(), "draft": "references not read yet"}))
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(mas.main(["validate", str(draft)]), 0)
                self.assertEqual(mas.main(["validate", "--rated", str(draft)]), 1)
                self.assertEqual(mas.main(["validate", "--rated", str(BUILDS)]), 0, "all four references are in")
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


def unread(d: dict[str, Any]) -> dict[str, Any]:
    """A tier's file as it was before its references: a draft."""
    for b in d["builds"]:
        b["reference"] = None
    return d | {"draft": "no reference profiles yet"}


def tbuild(d: dict[str, Any], bid: str) -> dict[str, Any]:
    return next(b for b in d["builds"] if b["id"] == bid)


def plan(d: dict[str, Any], bid: str) -> dict[str, Any]:
    b = tbuild(d, bid)
    return mas.kit_plan(
        ab.kit_of(b, d["level"], STATIC), b["class"], d["level"], STATIC, f"build {bid}",
        base=ab.stat_abilities()[b["class"]],
    )["kit"]  # fmt: skip


# The Bard's levelled spell lists: every SelectSpells a bard row has but its cantrips'
LEVELLED = {
    uuid
    for uuid, v in STATIC["lists"].items()
    if str(v.get("name")).startswith(("Bard spells", "Bard SLevel")) or "Magical Secrets" in str(v.get("name"))
}


class TierTest(unittest.TestCase):
    """The 4v4 tiers (website repo ops/logs/arena-4v4-builds.md): Fighter (Battle Master), a healer, Wizard (Evocation),
    Rogue (Thief) at levels 2, 5 and 12; subclass rows, improvements and prepared spells checked. The healer is a Bard
    (Lore) at 5 and 12 (owner D114): a Cleric copy's class rows add its whole list; l2_4v4_v1 keeps its Cleric draft."""

    def test_the_three_tier_files_are_valid_and_hold_the_same_four_builds(self):
        for lv in (2, 5, 12):
            with self.subTest(level=lv):
                d = ab.check_builds(tier(lv))
                # L2's Cleric draft; L5's and L12's Fighter reference is read again after Goading Attack came out
                # (owner, 2026-11-08): those two are rated again once it's in and S4 passes
                self.assertIn("draft", d)
                healer = "Cleric" if lv == 2 else "Bard"
                self.assertEqual([b["class"] for b in d["builds"]], ["Fighter", healer, "Wizard", "Rogue"])
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
        self.assertEqual(man[0]["ids"], ["PrecisionAttack", "Riposte", "SweepingAttack"])
        cl = plan(tier(2), "cleric")
        self.assertIn("DiscipleOfLife", cl["grants"]["passives"])
        self.assertTrue({"Target_Bless", "Target_CureWounds"} <= set(cl["grants"]["spells"]))
        self.assertEqual(len(cl["prepared"]), 5)  # Wis 17 (+3) + level 2
        self.assertEqual(cl["choices"][0], {"kind": "spells", "list": "3cae2e56-9871-4cef-bba6-96845ea765fa",
                                            "n": 1, "exact": True, "ids": ["Projectile_FireBolt"]})  # fmt: skip

    def test_a_bards_picks_are_its_book_its_college_a_grant_and_its_instrument_no_choice(self):
        for lv, picks in ((5, 8), (12, 19)):
            k = plan(tier(lv), "bard")
            self.assertEqual(k["subclass"], STATIC["subclasses"]["LoreCollege"]["uuid"])
            self.assertIn("CuttingWords", k["grants"]["passives"])
            self.assertNotIn("prepared", k)  # no MustPrepareSpells: nothing the mod has to name as not_applied
            leveled = [i for c in k["choices"] if c["kind"] == "spells" and c["list"] in LEVELLED for i in c["ids"]]
            self.assertEqual(len(leveled), picks)
            self.assertIn("Target_HealingWord", leveled)
            self.assertFalse({i["item"] for i in k["items"]} & {"OBJ_Scroll_Revivify", "OBJ_Keychain"})
        self.assertIn("SelectEquipment", STATIC["classes"]["Bard"]["levels"]["1"]["Selectors"])

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
        unread = copy.deepcopy(STATIC)
        unread["hirelings"]["Cleric"]["template"] = None
        with self.assertRaisesRegex(ab.BuildError, "build cleric: the Cleric hireling's root template is not read"):
            ab.mirror_section(ab.check_builds(tier(2)), names, unread)

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
        bad(lambda d: tbuild(d, "cleric")["script"]["prepared"].pop(), "prepared has 4, a level-2 Cleric prepares 5", 2)
        bad(
            lambda d: tbuild(d, "cleric")["script"]["prepared"].__setitem__(0, "Projectile_Fireball"),
            "prepared Projectile_Fireball is not a spell the Cleric has to prepare",
            2,
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
            2,
        )
        bad(
            lambda d: tbuild(d, "bard")["script"]["cc"]["spells"][1].__setitem__(0, "Projectile_Fireball"),
            "Projectile_Fireball is not in the game's list Bard spells level 1",
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


def ref_of(b: dict[str, Any], **over: Any) -> dict[str, Any]:
    prof = {f: [] for f in ab.REFERENCE_FIELDS} | {"level": 5, "name": "Lia"}
    return {
        "profile": prof,
        "game_build": "25605617",
        "hireling": STATIC["hirelings"][b["class"]]["origin"],
        "evidence": "live.md test",
    } | over


NAMES4 = [("Red Ash", "Blue Ash"), ("Red Bay", "Blue Bay"), ("Red Elm", "Blue Elm"), ("Red Oak", "Blue Oak")]


class ReferenceAndArenaFileTest(unittest.TestCase):
    """G6/G7: the tier's XP window, the references in the arena file; reference-read and -diff against a fake game."""

    def test_party_xp_is_the_tiers_window_from_the_games_table(self):
        self.assertEqual(ab.party_xp(2, STATIC), {"total": 300, "below": 900})
        self.assertEqual(ab.party_xp(5, STATIC), {"total": 6500, "below": 13000})
        self.assertEqual(ab.party_xp(12, STATIC), {"total": 100000})

    def test_the_mirror_section_has_party_xp_and_each_read_reference(self):
        d = unread(tier(5))
        d["builds"][0]["reference"] = ref_of(d["builds"][0])
        m = ab.mirror_section(ab.check_builds(d), NAMES4, STATIC)["mirror"]
        self.assertEqual(m["party_xp"], {"total": 6500, "below": 13000})
        self.assertEqual(
            m["pairs"][0]["reference"], {"profile": d["builds"][0]["reference"]["profile"], "accepted": []}
        )
        self.assertNotIn("reference", m["pairs"][1])

    def test_accepted_differences_need_a_field_entries_and_why_once_per_field(self):
        d = tier(5)
        b = d["builds"][0]
        good = {"field": "classes", "only_twin": ["a"], "only_reference": ["b"], "why": "P2"}
        b["reference"] = ref_of(b, accepted=[good])
        ab.check_builds(d)
        for bad, want in (
            ([{**good, "why": " "}], "accepted difference"),
            ([{**good, "only_twin": [], "only_reference": []}], "accepted difference"),
            ([good, good], "twice"),
            ("x", "must list"),
        ):
            b["reference"] = ref_of(b, accepted=bad)
            with self.assertRaisesRegex(ab.BuildError, want):
                ab.check_builds(d)

    def test_arena_file_takes_the_bases_rules_and_the_builds_and_checks_the_spawns(self):
        base = json.loads((REPO / "packages/bench/arenas/arena-v0.1.json").read_text())
        a = ab.arena_file(ab.check_builds(unread(tier(5))), base, "arena-4v4-l5", NAMES4, [213.0, 10.98, 288.0],
                          [213.0, 10.98, 296.0], STATIC)  # fmt: skip
        self.assertEqual((a["level"], a["save"], a["name"]), (5, base["save"], "arena-4v4-l5"))
        self.assertEqual(a["location"]["level"], base["location"]["level"])
        self.assertIn("draft", a)  # a draft build file can't make a playable arena
        with self.assertRaisesRegex(mas.SpecError, "height"):
            ab.arena_file(tier(5), base, "x", NAMES4, [0.0, 0.0, 0.0], [0.0, 1.0, 8.0], STATIC)
        sys.path.insert(0, str(REPO / "packages/bench/src"))
        from bg3bench import arena as barena

        self.assertEqual(barena.validate(a, "arena-4v4-l5"), [])
        self.assertNotIn("line_gap", a["location"])  # the default isn't written
        played = copy.deepcopy(a)
        del played["draft"]
        self.assertTrue(any("has no reference" in e for e in barena.validate(played, "arena-4v4-l5")))
        for p, b in zip(played["mirror"]["pairs"], tier(5)["builds"], strict=True):
            p["reference"] = {"profile": ref_of(b)["profile"], "accepted": []}
        self.assertEqual(barena.validate(played, "arena-4v4-l5"), [])
        del played["mirror"]["party_xp"]
        self.assertTrue(any("needs mirror.party_xp" in e for e in barena.validate(played, "arena-4v4-l5")))

    def test_every_builds_arena_is_what_its_build_file_generates(self):
        # Names, spawns, line gap and the two caps are builds-arena's arguments, so they come from the file.
        arenas = REPO / "packages/bench/arenas"
        paths = sorted(arenas.glob("builds-*.json"))
        self.assertEqual(len(paths), 4)  # a glob that finds nothing would pass
        for path in paths:
            with self.subTest(path.name):
                a = json.loads(path.read_text())
                m = re.fullmatch(r"The build file (\S+) .* on (\S+)'s save\.", a["about"])
                assert m is not None, a["about"]
                doc = ab.load_builds(REPO / f"packages/bench/builds/{m[1]}.json")
                base = json.loads((arenas / f"{m[2]}.json").read_text())
                names = [(p["names"]["A"], p["names"]["B"]) for p in a["mirror"]["pairs"]]
                loc = a["location"]
                want = ab.arena_file(
                    doc, base, a["name"], names, loc["spawns"]["A"], loc["spawns"]["B"], STATIC, loc.get("line_gap")
                )
                self.assertEqual(a, want | {"round_cap": a["round_cap"], "wall_cap_min": a["wall_cap_min"]})

    def test_arena_file_has_the_builds_turn_cap_not_the_bases(self):
        base = json.loads((REPO / "packages/bench/arenas/crash-mirror-v1.json").read_text())
        self.assertEqual(base["turn_cap_s"], 60)  # a base whose cap differs, or this test shows nothing
        a = ab.arena_file(
            ab.check_builds(tier(5)), base, "x", NAMES4, [213.0, 10.98, 288.0], [213.0, 10.98, 296.0], STATIC
        )
        self.assertEqual(a["turn_cap_s"], 90)

    def test_arena_file_writes_a_line_gap_and_checks_the_spawns_with_it(self):
        base = json.loads((REPO / "packages/bench/arenas/arena-v0.1.json").read_text())
        spots = [212.875, 10.98, 288.0], [212.875, 10.98, 296.0]
        a = ab.arena_file(ab.check_builds(tier(5)), base, "x", NAMES4, *spots, STATIC, line_gap=1.25)
        self.assertEqual(a["location"]["line_gap"], 1.25)
        with self.assertRaisesRegex(mas.SpecError, "must be the same within"):  # 0.1 m off along x: 4 m lines tell
            mas.check_geometry([0.0, 0.0, 0.0], [0.15, 0.0, 8.0], 4, 3.0)
        mas.check_geometry([0.0, 0.0, 0.0], [0.15, 0.0, 8.0], 4, 0.25)  # the same offset on short lines passes

    def test_reference_read_writes_the_compared_fields_and_refuses_a_wrong_or_partial_read(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "b.json"
            path.write_text(json.dumps(tier(5)))
            prof = {f: [] for f in ab.REFERENCE_FIELDS} | {"level": 5, "name": "Lia", "hp": 3}
            ab.reference_read(lambda op, a: prof, path, "fighter", "u1", "live.md X")
            got = json.loads(path.read_text())["builds"][0]["reference"]
            self.assertEqual(set(got["profile"]), set(ab.REFERENCE_FIELDS))  # no name or current HP
            self.assertEqual(got["hireling"], STATIC["hirelings"]["Fighter"]["origin"])
            ab.check_builds(json.loads(path.read_text()))
            with self.assertRaisesRegex(ab.BuildError, "reads level 2"):
                ab.reference_read(lambda op, a: prof | {"level": 2}, path, "fighter", "u1", "x")
            with self.assertRaisesRegex(ab.BuildError, "spells doesn't read"):
                ab.reference_read(lambda op, a: prof | {"spells": None}, path, "fighter", "u1", "x")
            with self.assertRaisesRegex(ab.BuildError, "no build"):
                ab.reference_read(lambda op, a: prof, path, "nobody", "u1", "x")

    def test_reference_diff_sends_the_reference_and_needs_one(self):
        d = unread(tier(5))
        with self.assertRaisesRegex(ab.BuildError, "no reference yet"):
            ab.reference_diff(lambda op, a: {}, d, "fighter", "u1")
        d["builds"][0]["reference"] = ref_of(d["builds"][0])
        asked = []
        r = ab.reference_diff(lambda op, a: asked.append((op, a)) or {"ok": True}, d, "fighter", "u1")
        self.assertTrue(r["ok"])
        self.assertEqual(asked[0][0], "arena.reference_diff")
        self.assertEqual(asked[0][1]["uuid"], "u1")

    def test_a_kits_upcasts_are_the_rows_its_reference_holds_with_their_abilities(self):
        ups = {x for v in STATIC["upcasts"].values() for ids in v.values() for x in ids}
        doc = tier(5)
        for b, want in zip(doc["builds"], (0, 12, 14, 0), strict=True):
            with self.subTest(b["id"]):
                k = plan(doc, b["id"])
                got = set(k.get("spell_abilities", {})) & ups
                self.assertEqual(len(got), want)
                if b["reference"] is None:  # the Fighter's, read again (Goading Attack out, 2026-11-08)
                    continue
                held = ups & set(b["reference"]["profile"]["spells"])
                # the Wizard's learned, unprepared spells stay out with their upcasts: its twin holds neither
                learned = [x for ids in b["script"]["cc"].get("spells", []) for x in ids]
                learned += [x for u in b["script"]["level_ups"] for ids in u.get("spells", []) for x in ids]
                unprepared = [x for x in learned if "prepared" in b["script"] and x not in b["script"]["prepared"]]
                out = {x for sid in unprepared for v in STATIC["upcasts"].get(sid, {}).values() for x in v}
                self.assertEqual(got, held - out)
                casting = STATIC["classes"][b["class"]]["casting"]
                self.assertEqual({k["spell_abilities"][x] for x in got} - {casting}, set())

    def test_the_l5_wizards_accepted_rows_are_its_unprepared_spells_and_their_prepare_type_only(self):
        wizard = next(b for b in tier(5)["builds"] if b["id"] == "wizard")
        accepted = {a["field"]: a for a in wizard["reference"]["accepted"]}
        self.assertEqual(set(accepted), {"derived", "spells", "spellcast", "prepared"})
        self.assertEqual(accepted["prepared"]["only_twin"], ["Shout_ArcaneRecovery_2", "Shout_ArcaneRecovery_3"])
        self.assertEqual(accepted["prepared"]["only_reference"], [])
        learned = [x for ids in wizard["script"]["cc"].get("spells", []) for x in ids]
        learned += [x for u in wizard["script"]["level_ups"] for ids in u.get("spells", []) for x in ids]
        held = [x for c in plan(tier(5), "wizard")["choices"] if c["kind"] == "spells" for x in c["ids"]]
        out = [x for x in learned if x not in held]
        self.assertEqual(len(out), 5)
        slots = {"2", "3"}  # a level-5 Wizard's upcast slots
        ups = [x for s in out for p, ids in STATIC["upcasts"].get(s, {}).items() if p in slots for x in ids]
        self.assertEqual(sorted(accepted["spells"]["only_reference"]), sorted(out + ups))
        self.assertEqual(accepted["spells"]["only_twin"], [])
        cast = accepted["spellcast"]
        self.assertEqual(
            sorted(r.replace("|AlwaysPrepared|", "|RequiresPreparation|") for r in cast["only_twin"]),
            sorted(r for r in cast["only_reference"] if r.split("|")[0] not in out + ups),
        )
        self.assertEqual(len(cast["only_reference"]) - len(cast["only_twin"]), len(out + ups))

    def test_the_l12_wizards_accepted_spells_are_its_unprepared_spells_upcasts_and_fire_shields_variants(self):
        wizard = next(b for b in tier(12)["builds"] if b["id"] == "wizard")
        accepted = {a["field"]: a for a in wizard["reference"]["accepted"]}
        self.assertEqual(set(accepted), {"derived", "spells", "spellcast", "prepared"})
        self.assertEqual(accepted["prepared"]["only_twin"], [f"Shout_ArcaneRecovery_{n}" for n in (2, 3, 4, 5)])
        learned = [x for ids in wizard["script"]["cc"].get("spells", []) for x in ids]
        learned += [x for u in wizard["script"]["level_ups"] for ids in u.get("spells", []) for x in ids]
        held = [x for c in plan(tier(12), "wizard")["choices"] if c["kind"] == "spells" for x in c["ids"]]
        out = [x for x in learned if x not in held]
        self.assertEqual(len(out), 11)
        self.assertIn("Shout_FireShield", out, "so its rows are D152's, not a kit bug (D170)")
        roots = [*out, "Shout_FireShield_Chill", "Shout_FireShield_Warm"]
        slots = {"2", "3", "4", "5", "6"}  # a level-12 Wizard's upcast slots
        ups = [x for s in roots for p, ids in STATIC["upcasts"].get(s, {}).items() if p in slots for x in ids]
        self.assertEqual(sorted(accepted["spells"]["only_reference"]), sorted(roots + ups))
        self.assertEqual(accepted["spells"]["only_twin"], [])
        cast = accepted["spellcast"]
        self.assertEqual(
            sorted(r.replace("|AlwaysPrepared|", "|RequiresPreparation|") for r in cast["only_twin"]),
            sorted(r for r in cast["only_reference"] if r.split("|")[0] not in roots + ups),
        )
        self.assertEqual(len(cast["only_reference"]) - len(cast["only_twin"]), len(roots + ups))

    def test_a_kits_own_spell_with_upcasts_is_unlocked_and_a_class_rows_keeps_its_upcasts_added(self):
        ups = {x for v in STATIC["upcasts"].values() for ids in v.values() for x in ids}
        bard, wizard = plan(tier(5), "bard"), plan(tier(5), "wizard")
        self.assertEqual(bard["unlocks"]["Target_Bane"], "Charisma")
        self.assertEqual(wizard["unlocks"]["Projectile_MagicMissile"], "Intelligence")
        self.assertNotIn("Shout_ArcaneRecovery", wizard["unlocks"])  # the class row gives the root
        self.assertEqual(set(wizard["grants"]["spells"]) & ups, {"Shout_ArcaneRecovery_2", "Shout_ArcaneRecovery_3"})
        self.assertEqual(set(bard["grants"]["spells"]) & ups, set())
        self.assertNotIn("Target_ViciousMockery", bard["unlocks"])  # no upcasts: AddSpell's

    def test_upcasts_stop_at_the_highest_slot_the_rows_open(self):
        d = ab.load_builds(REPO / "packages/bench/builds/l5_4v4_team2_v1.json")
        ups = {x for v in STATIC["upcasts"].values() for ids in v.values() for x in ids}
        spells = set(plan(d, "ranger")["spell_abilities"]) & ups  # a half caster's level 5: 2nd-level slots
        self.assertTrue(spells and all(x.endswith("_2") for x in spells), spells)
        self.assertIn("Target_Bane_3", plan(tier(5), "bard")["spell_abilities"])
        self.assertNotIn("Target_Bane_4", plan(tier(5), "bard")["spell_abilities"])
        self.assertIn("Target_Bane_6", plan(tier(12), "bard")["spell_abilities"])

    def test_the_second_team_is_valid_and_no_build_of_it_removes_a_passive(self):
        d = ab.load_builds(REPO / "packages/bench/builds/l5_4v4_team2_v1.json")
        self.assertIn("draft", d)
        self.assertEqual([b["class"] for b in d["builds"]], ["Fighter", "Bard", "Wizard", "Ranger"])
        __import__("jsonschema").validate(
            d, json.loads((REPO / "packages/bench/schema/arena-builds.v1.schema.json").read_text())
        )
        k = plan(d, "fighter")  # Champion: its level 3 adds a passive and removes none
        self.assertEqual(k["grants"]["passives"], ["ImprovedCritical"])
        self.assertIn(
            ["FightingStyle_GreatWeaponFighting"], [c["ids"] for c in k["choices"] if c["kind"] == "passives"]
        )
        v = plan(d, "bard")  # Lore, as team 1's, with its own picks
        self.assertEqual(v["grants"]["passives"], ["CuttingWords"])
        self.assertIn(["Insight", "Persuasion", "Stealth"], [c["ids"] for c in v["choices"] if c["kind"] == "skills"])
        for b in d["builds"]:
            self.assertNotIn("passives_removed", plan(d, b["id"]).get("grants", {}), b["id"])

    def test_a_build_whose_subclass_rows_remove_a_passive_is_refused(self):
        barbarian = {
            "id": "barbarian", "class": "Barbarian", "subclass": "TotemWarriorPath", "race": "Dwarf_GoldHill",
            "background": "Outlander", "role": "frontline", "reference": None,
            "gear": ["WPN_Greataxe_1", "OBJ_Potion_Healing", "OBJ_Potion_Healing", "ARM_Shoes_Barbarian",
                     "ARM_Barbarian", {"item": "WPN_Handaxe_1", "equip": False},
                     {"item": "WPN_Handaxe_1", "equip": False}, "OBJ_Potion_Healing_Greater"],
            "script": {
                "cc": {
                    "abilities": {"Strength": 15, "Dexterity": 13, "Constitution": 14, "Intelligence": 10,
                                  "Wisdom": 12, "Charisma": 8},
                    "ability_bonus": ["Strength", "Constitution"], "skills": ["Intimidation", "Perception"],
                },
                "level_ups": [{"level": 2}, {"level": 3, "passives": [["TotemSpirit_Bear"]]},
                              {"level": 4, "improvement": {"asi": {"Strength": 2}}}, {"level": 5}],
            },
        }  # fmt: skip
        d = {"level": 5, "builds": [barbarian]}
        with self.assertRaisesRegex(mas.SpecError, r"TotemWarriorPath level 3 removes RageUnlock: a twin can't"):
            plan(d, "barbarian")

    def test_a_wizards_kit_adds_only_its_prepared_picks_and_a_clerics_keeps_prepared_for_the_mod_to_name(self):
        for lv, n in ((2, 5), (5, 9), (12, 17)):
            with self.subTest(level=lv):
                d = tier(lv)
                k, want = plan(d, "wizard"), set(tbuild(d, "wizard")["script"]["prepared"])
                self.assertNotIn("prepared", k)
                added = {i for c in k["choices"] if c["kind"] == "spells" and not c["exact"] for i in c["ids"]}
                self.assertEqual(added, want)
                self.assertEqual(len(want), n)
                self.assertTrue(
                    any(c["kind"] == "spells" and c["exact"] for c in k["choices"]), "the cantrips stay exact"
                )
                if lv == 2:
                    self.assertIn("prepared", plan(d, "cleric"))

    def test_the_cleric_and_wizard_templates_are_read_and_the_races_are_their_builds(self):
        m = ab.mirror_section(ab.check_builds(tier(2)), NAMES4)["mirror"]["pairs"]
        self.assertEqual(m[1]["template"], "4b0a059b-4bae-4884-8bdf-a683c5f2cbea")
        self.assertTrue(m[1]["race"].startswith("4fda6bce"))
        self.assertEqual(m[2]["template"], "3d24f5eb-7220-4e00-bc02-3e6ac819ac26")
        self.assertTrue(m[2]["race"].startswith("2cf6c770"))

    def test_no_build_picks_a_knock_down_source_in_its_script_or_kit(self):
        # What a build picks. A Cleric copy's class rows add its whole list, Command included, and neither shows here:
        # the reference compare fails that, and only the l2_4v4_v1 draft holds a Cleric. Sleet Storm, Ice Storm and
        # Freezing Sphere: ice surfaces, from play. Thunderous Smite, Eyebite: PRONE_THUNDEROUS_SMITE, SLEEPING_EYEBITE
        # (the paks). Chromatic Orb: its cold variant makes WaterFrozen from nothing (the paks), Arrow of Ice's reason.
        # What these ids can reach is test_arena_scan's.
        banned = {"Target_Grease", "Projectile_IceKnife", "Zone_Thunderwave", "Target_HideousLaughter", "Target_Sleep",
                  "TripAttack", "PushingAttack", "MenacingAttack", "TotemSpirit_Elk", "Target_Command_Container",
                  "Target_SleetStorm", "Target_IceStorm", "Target_FreezingSphere", "Target_Smite_Thunderous",
                  "Target_Eyebite", "Projectile_ChromaticOrb", "Projectile_ChromaticOrb_Cold", "WPN_Quarterstaff",
                  "WPN_Quarterstaff_1", "WPN_Quarterstaff_2"}  # fmt: skip
        for path in sorted((REPO / "packages/bench/builds").glob("l*_4v4_*.json")):
            d = json.loads(path.read_text())
            for b in d["builds"]:
                with self.subTest(file=path.name, build=b["id"]):
                    held = json.dumps(b["script"]) + json.dumps(plan(d, b["id"]))
                    self.assertFalse(banned & set(re.findall(r"[A-Za-z_]+", held)))
