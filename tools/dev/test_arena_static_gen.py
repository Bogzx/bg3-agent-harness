"""Offline tests for tools/dev/arena_static_gen.py on a tiny fake game (three paks written with tools/pak.py).
Run: uv run pytest -q tools/dev/test_arena_static_gen.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arena_static_gen as gen
import pak

FIGHTER_T, BM_T, RACE_T, SUB_T = (f"{i}0000000-0000-0000-0000-000000000000" for i in "1234")
FIGHTER, BM, RACE, SUB, BG, ORIGIN, LIST, SKILLS = (f"a{i}000000-0000-0000-0000-000000000000" for i in "12345678")


def lsx(region: str, nodes: list[tuple[str, dict[str, str], list[str]]]) -> bytes:
    """An .lsx with one node per (id, attributes, subclass ids)."""
    out = []
    for nid, attrs, subs in nodes:
        a = "".join(f'<attribute id="{k}" type="LSString" value="{v}"/>' for k, v in attrs.items())
        kids = "".join(f'<node id="SubClass"><attribute id="Object" type="guid" value="{s}"/></node>' for s in subs)
        sub = f'<children><node id="SubClasses"><children>{kids}</children></node></children>' if subs else ""
        out.append(f'<node id="{nid}">{a}{sub}</node>')
    return (
        f'<?xml version="1.0"?><save><region id="{region}"><node id="root"><children>{"".join(out)}'
        "</children></node></region></save>"
    ).encode()


def row(table: str, level: int, uuid: str, **kw: str) -> tuple[str, dict[str, str], list[str]]:
    subs = kw.pop("subs", "")
    return (
        "Progression",
        {"TableUUID": table, "Level": str(level), "UUID": uuid, "Name": "x", **kw},
        subs.split(";") if subs else [],
    )


def game(tmp: Path, *, shared_rows=(), dev_rows=(), x_rows=()) -> Path:
    """A Data folder: Shared.pak (Shared + SharedDev), Gustav.pak (GustavDev), GustavX.pak."""
    fighter_rows = [
        row(
            FIGHTER_T,
            1,
            "r1",
            Boosts="Proficiency(HeavyArmor)",
            Selectors=f"SelectSkills({SKILLS},2);SelectPassives({LIST},1,FightingStyle)",
        ),
        row(FIGHTER_T, 1, "r1m", IsMulticlass="true", Selectors=f"SelectPassives({LIST},1)"),
        row(FIGHTER_T, 3, "r3", subs=BM),
        row(FIGHTER_T, 4, "r4", AllowImprovement="true"),
        row(BM_T, 3, "b3s", Boosts="ActionResource(SuperiorityDie,4,0)"),
        row(RACE_T, 1, "race1", Boosts="ActionResource(Movement,9,0)", PassivesAdded="Darkvision"),
        row(SUB_T, 1, "sub1", Boosts="ProficiencyBonus(Skill,Stealth)"),
    ]
    stats = (
        'new entry "WPN_Longsword"\ntype "Weapon"\ndata "RootTemplate" "root-ls"\n\n'
        'new entry "WPN_Longsword_1"\ntype "Weapon"\nusing "WPN_Longsword"\ndata "RootTemplate" "root-ls1"\n'
        'data "Rarity" "Uncommon"\n'
    )
    passives = (
        'new entry "Trained"\ntype "PassiveData"\ndata "Boosts" "ProficiencyBonus(Skill,Stealth);Tag(X)"\n\n'
        'new entry "Plain"\ntype "PassiveData"\ndata "Boosts" "ProficiencyBonus(SavingThrow,Wisdom)"\n\n'
    )
    objects = "".join(f'new entry "{p}"\ntype "Object"\ndata "RootTemplate" "root-{p}"\n\n' for p in gen.POTIONS)
    xp = "".join(f'key "Level{i}","{i * 100}"\n\n' for i in range(1, 12))
    shared = {
        "Public/Shared/Progressions/Progressions.lsx": lsx("Progressions", [*fighter_rows, *shared_rows]),
        "Public/SharedDev/Progressions/Progressions.lsx": lsx("Progressions", list(dev_rows)),
        "Public/Shared/ClassDescriptions/ClassDescriptions.lsx": lsx(
            "ClassDescriptions",
            [
                (
                    "ClassDescription",
                    {"Name": "Fighter", "UUID": FIGHTER, "ProgressionTableUUID": FIGHTER_T, "SpellCastingAbility": "4"},
                    [],
                ),
                (
                    "ClassDescription",
                    {"Name": "BattleMaster", "UUID": BM, "ParentGuid": FIGHTER, "ProgressionTableUUID": BM_T},
                    [],
                ),
            ],
        ),
        "Public/Shared/Races/Races.lsx": lsx(
            "Races",
            [
                ("Race", {"Name": "HalfElf", "UUID": RACE, "ProgressionTableUUID": RACE_T}, []),
                ("Race", {"Name": "HalfElf_Wood", "UUID": SUB, "ParentGuid": RACE, "ProgressionTableUUID": SUB_T}, []),
            ],
        ),
        "Public/Shared/Backgrounds/Backgrounds.lsx": lsx(
            "Backgrounds", [("Background", {"UUID": BG, "Passives": "Background_Soldier"}, [])]
        ),
        "Public/SharedDev/Origins/Origins.lsx": lsx(
            "Origins",
            [
                (
                    "Origin",
                    {
                        "Name": "Hireling_Fighter",
                        "UUID": ORIGIN,
                        "RaceUUID": RACE,
                        "SubRaceUUID": SUB,
                        "BackgroundUUID": BG,
                    },
                    [],
                )
            ],
        ),
        "Public/SharedDev/Feats/Feats.lsx": lsx(
            "Feats",
            [
                ("Feat", {"Name": "Tough", "UUID": "f1", "PassivesAdded": "Tough"}, []),
                (
                    "Feat",
                    {
                        "Name": "AbilityScoreIncrease",
                        "UUID": "f2",
                        "CanBeTakenMultipleTimes": "true",
                        "Selectors": "SelectAbilities(b9149c8e-52c8-46e5-9cb6-fc39301c05fe,2,2,FeatASI)",
                    },
                    [],
                ),
            ],
        ),
        "Public/Shared/Lists/PassiveLists.lsx": lsx(
            "PassiveLists",
            [
                (
                    "PassiveList",
                    {"UUID": LIST, "Name": "styles", "Passives": "FightingStyle_Defense,FightingStyle_Archery"},
                    [],
                )
            ],
        ),
        "Public/Shared/Lists/SkillLists.lsx": lsx(
            "SkillLists",
            [("SkillList", {"UUID": SKILLS, "Name": "fighter skills", "Skills": "Athletics,Perception"}, [])],
        ),
        "Public/Shared/Stats/Generated/Equipment.txt": (
            b'new equipment "EQP_CC_Fighter"\nadd equipmentgroup\nadd equipment entry "WPN_Longsword"\n'
            b'add equipment entry "OBJ_Potion_Healing"\n'
        ),
        "Public/Shared/Stats/Generated/Data/Weapon.txt": stats.encode(),
        "Public/Shared/Stats/Generated/Data/Object.txt": objects.encode(),
        "Public/Shared/Stats/Generated/Data/Passive.txt": passives.encode(),
        "Public/Shared/Stats/Generated/Data/XPData.txt": xp.encode(),
    }
    data = tmp / "Data"
    data.mkdir()
    pak.write_package(data / "Shared.pak", list(shared.items()))
    pak.write_package(data / "Gustav.pak", [("Public/GustavDev/Stats/Generated/Data/Armor.txt", b"")])
    pak.write_package(
        data / "GustavX.pak", [("Public/GustavX/Progressions/Progressions.lsx", lsx("Progressions", list(x_rows)))]
    )
    return data


def run(tmp: Path, **kw) -> dict:
    return gen.generate(gen.read_paks(game(tmp, **kw)), ("Fighter",))


def test_class_rows_skip_the_multiclass_row_and_keep_improvements_subclass_counts_and_casting(tmp_path):
    g = run(tmp_path)
    f = g["classes"]["Fighter"]
    assert list(f["levels"]) == ["1", "3", "4"] and f["casting"] == "Intelligence"
    assert f["levels"]["1"]["uuid"] == "r1" and "SelectSkills" in f["levels"]["1"]["Selectors"]
    assert f["levels"]["3"]["subclasses"] == 1 and f["levels"]["4"]["AllowImprovement"] is True
    assert g["subclasses"]["BattleMaster"]["levels"]["3"]["Boosts"] == "ActionResource(SuperiorityDie,4,0)"
    assert f["equipment"] == [
        {"item": "WPN_Longsword", "type": "Weapon", "root": "root-ls"},
        {"item": "OBJ_Potion_Healing", "type": "Object", "root": "root-OBJ_Potion_Healing"},
    ]
    # the +1 entry inherits its type through `using` and is in the catalog with its own root and rarity
    assert g["items"]["WPN_Longsword_1"] == {"type": "Weapon", "root": "root-ls1", "rarity": "Uncommon"}
    assert g["passive_skills"] == {"Trained": ["Stealth"]}
    assert set(gen.POTIONS) <= set(g["items"])


def test_race_rows_parent_first_background_hireling_feats_lists_and_xp_totals(tmp_path):
    g = run(tmp_path)
    r = g["races"]["HalfElf_Wood"]
    assert r["race"] == SUB and [x["race"] for x in r["rows"]] == ["HalfElf", "HalfElf_Wood"]
    assert g["hirelings"]["Fighter"] == {"origin": ORIGIN, "race": "HalfElf_Wood", "background": "Soldier"}
    assert g["backgrounds"]["Soldier"] == {"uuid": BG, "passive": "Background_Soldier"}
    assert g["feats"]["AbilityScoreIncrease"]["repeatable"] is True and g["feats"]["Tough"]["passives"] == "Tough"
    assert g["lists"][LIST] == {
        "kind": "passives",
        "name": "styles",
        "ids": ["FightingStyle_Defense", "FightingStyle_Archery"],
    }  # comma-separated
    assert g["lists"][SKILLS]["ids"] == ["Athletics", "Perception"]
    assert g["xp"]["2"] == 100 and g["xp"]["3"] == 300 and g["xp"]["12"] == sum(i * 100 for i in range(1, 12))


def test_a_later_modules_row_at_the_same_level_replaces_one_module_twice_is_an_error(tmp_path):
    (tmp_path / "a").mkdir()
    g = run(tmp_path / "a", x_rows=[row(BM_T, 3, "b3x", Boosts="ActionResource(SuperiorityDie,5,0)")])
    assert g["subclasses"]["BattleMaster"]["levels"]["3"]["uuid"] == "b3x"
    assert [f"{BM_T} level 3: Shared b3s -> GustavX b3x"] == gen.REPLACED  # the content differed
    (tmp_path / "b").mkdir()
    with pytest.raises(SystemExit, match="two Shared rows at level 3"):
        run(tmp_path / "b", shared_rows=[row(BM_T, 3, "b3dup")])


def test_merge_keeps_hand_read_keys_reports_differences_and_adds_new_hirelings_without_a_template(tmp_path):
    g = run(tmp_path)
    old = {
        "races_source": "hand",
        "hirelings": {},
        "classes": {
            "Fighter": {
                "table": FIGHTER_T,
                "levels": {"1": {**g["classes"]["Fighter"]["levels"]["1"], "Selectors": "old"}},
                "equipment": [],
            }
        },
    }
    new, diffs = gen.merge(old, g)
    assert new["races_source"] == "hand" and new["classes"]["Fighter"]["levels"]["3"]["subclasses"] == 1
    assert [d.split(":")[0] for d in diffs] == ["classes.Fighter.levels.1.Selectors", "classes.Fighter.equipment"]
    assert new["hirelings"]["Fighter"]["template"] is None and "todo" in new["hirelings"]["Fighter"]
    kept, _ = gen.merge(
        {"hirelings": {"Fighter": {"origin": ORIGIN, "race": "X", "background": "Soldier", "template": "t"}}}, g
    )
    assert kept["hirelings"]["Fighter"]["template"] == "t"  # a hand-read hireling is never overwritten


def test_main_writes_only_when_every_difference_is_accepted(tmp_path, monkeypatch, capsys):
    data = game(tmp_path)
    monkeypatch.setattr(gen, "CLASSES", ("Fighter",))
    out = tmp_path / "static.json"
    out.write_text(json.dumps({"classes": {"Fighter": {"table": FIGHTER_T, "levels": {}, "equipment": []}}}))
    assert gen.main([str(data), "--out", str(out)]) == 1
    assert "nothing written" in capsys.readouterr().err and "xp" not in json.loads(out.read_text())
    assert gen.main([str(data), "--out", str(out), "--accept", "classes.Fighter.equipment"]) == 0
    assert json.loads(out.read_text())["xp"]["12"] > 0
