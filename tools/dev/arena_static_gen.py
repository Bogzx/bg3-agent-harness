"""Regenerate tools/dev/arena_static.json's game data from the installed game's paks (read-only), for the arena's
build files and kits at levels 1-12 (ops/logs/arena-4v4-builds.md G1, website repo).

    uv run python tools/dev/arena_static_gen.py "<BG3>/Data" [--out tools/dev/arena_static.json] [--dry-run]

Reads Shared.pak, Gustav.pak and GustavX.pak in memory (tools/pak.py): per module Public/<module>/ Progressions,
ClassDescriptions, Races, Backgrounds, Origins, Feats, Lists (Spell/Passive/Skill), Stats/Generated/Equipment.txt and
Data/{Weapon,Armor,Object,XPData}.txt. Modules are read in MODULES order; a later module's row with the same UUID (or
stat entry with the same name) wins.

What it writes into the static file (merged, never dropping a key it doesn't make):
- classes[C] for CLASSES: table, levels "1".."12" (the non-multiclass row: Boosts, PassivesAdded, Selectors, uuid,
  subclasses = how many subclasses the row offers, plus AllowImprovement / PassivesRemoved where the row has them),
  equipment (EQP_CC_<C>: item, type, root), casting (ClassDescription's SpellCastingAbility, by name);
- subclasses[S] for every subclass of CLASSES: class, uuid, table, levels (the rows it has);
- races[R] for the hirelings' races: race (the most specific id: the subrace's, else the race's) and rows (the parent
  race's level-1 row first, then the subrace's);
- backgrounds, feats (name, uuid, selectors, passives, boosts, repeatable), lists (uuid -> {kind, name, ids}) for
  every list a written row or feat selects from, xp (total XP per level), items (each EQP_CC item and its _1/_2
  entries, the healing potions: type, root, rarity), hirelings (new classes only: origin, race, background; their
  template stays null until read, the root templates are in binary .lsf files).

An existing value the generator makes again must come out the same (the L1-2 rows read by hand before, races,
backgrounds, hirelings it would add): a difference stops it before writing, naming the key (--dry-run prints the
diff only). Hand-read keys it can't make (hirelings' templates, `todo`s, *_source text) are kept as they are.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pak  # tools/pak.py

STATIC = Path(__file__).resolve().parent / "arena_static.json"
PAKS = ("Shared.pak", "Gustav.pak", "GustavX.pak")
MODULES = ("Shared", "SharedDev", "Gustav", "GustavDev", "GustavX")
CLASSES = ("Fighter", "Rogue", "Barbarian", "Ranger", "Cleric", "Wizard")
MAX_LEVEL = 12
POTIONS = ("OBJ_Potion_Healing", "OBJ_Potion_Healing_Greater", "OBJ_Potion_Healing_Superior")
NULL = "00000000-0000-0000-0000-000000000000"
SOURCE = (
    "game data of the installed BG3 (build in game_build), read-only from Shared.pak, Gustav.pak and GustavX.pak "
    "(modules " + ", ".join(MODULES) + ", a later module's same UUID or entry wins) by tools/dev/arena_static_gen.py"
)

Files = dict[str, bytes]  # "Public/<module>/<rel>" -> bytes


def read_paks(data_dir: Path) -> Files:
    want = re.compile(
        r"Public/[^/]+/(Progressions/Progressions\.lsx|ClassDescriptions/ClassDescriptions\.lsx|Races/Races\.lsx"
        r"|Backgrounds/Backgrounds\.lsx|Origins/Origins\.lsx|Feats/Feats\.lsx|Lists/(Spell|Passive|Skill)Lists\.lsx"
        r"|Stats/Generated/Equipment\.txt|Stats/Generated/Data/(Weapon|Armor|Object|XPData)\.txt)"
    )
    out: Files = {}
    for name in PAKS:
        p = pak.Package(data_dir / name)
        for e in p.find("Public/*"):
            if want.fullmatch(e.name):
                out[e.name] = p.read(e)
    return out


def _each(files: Files, rel: str) -> list[tuple[str, bytes]]:
    """The modules' copies of one file, in MODULES order."""
    return [(m, files[f"Public/{m}/{rel}"]) for m in MODULES if f"Public/{m}/{rel}" in files]


def lsx_nodes(files: Files, rel: str, node: str) -> dict[str, dict[str, Any]]:
    """UUID -> the node's attributes (+ SubClasses: [ids] where it has them), a later module's same UUID winning."""
    out: dict[str, dict[str, Any]] = {}
    for module, data in _each(files, rel):
        for n in ET.fromstring(data).iter("node"):
            if n.get("id") != node:
                continue
            a: dict[str, Any] = {str(x.get("id")): x.get("value") for x in n.findall("attribute")}
            a["_module"] = module
            for c in n.findall("children/node"):
                if c.get("id") == "SubClasses":
                    a["SubClasses"] = [s.find("attribute").get("value") for s in c.findall("children/node")]  # type: ignore[union-attr]
            if a.get("UUID"):
                out[a["UUID"]] = a
    return out


def stat_entries(files: Files, rel: str) -> dict[str, dict[str, str]]:
    """A stats .txt's entries, `using` resolved (the parent's data first, the entry's own over it)."""
    raw: dict[str, dict[str, str]] = {}
    for _, data in _each(files, rel):
        cur: dict[str, str] | None = None
        for line in data.decode("utf-8", "replace").splitlines():
            if m := re.match(r'new entry "([^"]+)"', line):
                cur = raw[m.group(1)] = {}
            elif cur is not None and (m := re.match(r'(type|using) "([^"]*)"', line)):
                cur["@" + m.group(1)] = m.group(2)
            elif cur is not None and (m := re.match(r'data "([^"]+)" "([^"]*)"', line)):
                cur[m.group(1)] = m.group(2)

    def resolved(name: str, seen: tuple[str, ...] = ()) -> dict[str, str]:
        e = raw[name]
        base = e.get("@using")
        out = resolved(base, (*seen, name)) if base in raw and base not in seen else {}
        return {**out, **e}

    return {n: resolved(n) for n in raw}


def equipment(files: Files) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for _, data in _each(files, "Stats/Generated/Equipment.txt"):
        cur: list[str] | None = None
        for line in data.decode("utf-8", "replace").splitlines():
            if m := re.match(r'new equipment "([^"]+)"', line):
                cur = out[m.group(1)] = []
            elif cur is not None and (m := re.match(r'add equipment entry "([^"]+)"', line)):
                cur.append(m.group(1))
    return out


def xp_totals(files: Files) -> dict[str, int]:
    """XPData.txt's LevelN is the XP from level N to N+1: the total for level L is the sum of Level1..Level(L-1)."""
    step: dict[int, int] = {}
    for _, data in _each(files, "Stats/Generated/Data/XPData.txt"):
        for m in re.finditer(r'key "Level(\d+)","(\d+)"', data.decode()):
            step[int(m.group(1))] = int(m.group(2))
    out, total = {"1": 0}, 0
    for lv in range(2, MAX_LEVEL + 1):
        if lv - 1 not in step:
            raise SystemExit(f"XPData has no Level{lv - 1}")
        total += step[lv - 1]
        out[str(lv)] = total
    return out


def _row(p: dict[str, Any]) -> dict[str, Any]:
    out = {
        "Boosts": p.get("Boosts") or "",
        "PassivesAdded": p.get("PassivesAdded") or "",
        "Selectors": p.get("Selectors") or "",
        "uuid": p["UUID"],
        "subclasses": len(p.get("SubClasses") or []),
    }
    if p.get("AllowImprovement") == "true":
        out["AllowImprovement"] = True
    if p.get("PassivesRemoved"):
        out["PassivesRemoved"] = p["PassivesRemoved"]
    return out


REPLACED: list[str] = []  # rows a later module's row of the same table and level replaced, with different content


def _rows(prog: dict[str, dict[str, Any]], table: str) -> dict[str, dict[str, Any]]:
    """level -> the table's row at that level (the non-multiclass one). A later module's row at the same level replaces
    an earlier module's (Battle Master's level 3 is in Shared and again, another UUID, in SharedDev); two in one
    module are an error."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for p in sorted(prog.values(), key=lambda p: MODULES.index(p["_module"])):
        if p.get("TableUUID") != table or p.get("IsMulticlass") == "true" or int(p["Level"]) > MAX_LEVEL:
            continue
        if p["Level"] in out:
            module, prev = out[p["Level"]]
            if module == p["_module"]:
                raise SystemExit(
                    f"table {table} has two {module} rows at level {p['Level']}: {prev['uuid']}, {p['UUID']}"
                )
            if {k: v for k, v in prev.items() if k != "uuid"} != {k: v for k, v in _row(p).items() if k != "uuid"}:
                REPLACED.append(f"{table} level {p['Level']}: {module} {prev['uuid']} -> {p['_module']} {p['UUID']}")
        out[p["Level"]] = (p["_module"], _row(p))
    return {lv: row for lv, (_, row) in sorted(out.items(), key=lambda kv: int(kv[0]))}


SELECTOR_LIST = re.compile(r"(Select\w+|AddSpells)\(([0-9a-f-]{36})")
ABILITY = {
    "0": None,
    "1": "Strength",
    "2": "Dexterity",
    "3": "Constitution",
    "4": "Intelligence",
    "5": "Wisdom",
    "6": "Charisma",
}


def generate(files: Files, classes: tuple[str, ...] = CLASSES) -> dict[str, Any]:
    REPLACED.clear()
    prog = lsx_nodes(files, "Progressions/Progressions.lsx", "Progression")
    cls = lsx_nodes(files, "ClassDescriptions/ClassDescriptions.lsx", "ClassDescription")
    races = lsx_nodes(files, "Races/Races.lsx", "Race")
    bgs = lsx_nodes(files, "Backgrounds/Backgrounds.lsx", "Background")
    origins = lsx_nodes(files, "Origins/Origins.lsx", "Origin")
    feats = lsx_nodes(files, "Feats/Feats.lsx", "Feat")
    eqp = equipment(files)
    stats = {
        **stat_entries(files, "Stats/Generated/Data/Object.txt"),
        **stat_entries(files, "Stats/Generated/Data/Armor.txt"),
        **stat_entries(files, "Stats/Generated/Data/Weapon.txt"),
    }
    by_name = {c["Name"]: c for c in cls.values()}
    items: dict[str, dict[str, Any]] = {}

    def item(name: str) -> dict[str, Any]:
        e = stats.get(name)
        if e is None or not e.get("RootTemplate"):
            raise SystemExit(f"no stat entry with a RootTemplate for item {name}")
        items[name] = {"type": e.get("@type"), "root": e["RootTemplate"], "rarity": e.get("Rarity") or "Common"}
        return items[name]

    out: dict[str, Any] = {"classes": {}, "subclasses": {}}
    for c in classes:
        d = by_name[c]
        eq = []
        for name in eqp[f"EQP_CC_{c}"]:
            it = item(name)
            eq.append({"item": name, "type": it["type"], "root": it["root"]})
            for n in (f"{name}_1", f"{name}_2"):
                if n in stats and stats[n].get("RootTemplate"):
                    item(n)
        out["classes"][c] = {
            "table": d["ProgressionTableUUID"],
            "casting": ABILITY.get(d.get("SpellCastingAbility") or "0"),
            "levels": _rows(prog, d["ProgressionTableUUID"]),
            "equipment": eq,
        }
        for sub in (s for s in cls.values() if s.get("ParentGuid") == d["UUID"]):
            out["subclasses"][sub["Name"]] = {
                "class": c,
                "uuid": sub["UUID"],
                "table": sub["ProgressionTableUUID"],
                "levels": _rows(prog, sub["ProgressionTableUUID"]),
            }
    for p in POTIONS:
        item(p)
    out["items"] = dict(sorted(items.items()))
    out["xp"] = xp_totals(files)

    hire = {o["Name"].removeprefix("Hireling_"): o for o in origins.values() if o["Name"].startswith("Hireling_")}
    out["hirelings"] = {}
    out["races"] = {}
    out["backgrounds"] = {}
    for c in classes:
        o = hire[c]
        race = races[o["RaceUUID"]]
        sub = races.get(o.get("SubRaceUUID") or NULL)
        rname = (sub or race)["Name"]
        bg = bgs[o["BackgroundUUID"]]
        bname = bg["Passives"].removeprefix("Background_")
        out["hirelings"][c] = {"origin": o["UUID"], "race": rname, "background": bname}
        rows = []
        for r in (race, sub):
            if r is None:
                continue
            row = next(
                (p for p in prog.values() if p.get("TableUUID") == r.get("ProgressionTableUUID") and p["Level"] == "1"),
                None,
            )
            if row is not None:
                rows.append(
                    {
                        "race": r["Name"],
                        "table": r["ProgressionTableUUID"],
                        "uuid": row["UUID"],
                        "Boosts": row.get("Boosts") or "",
                        "PassivesAdded": row.get("PassivesAdded") or "",
                        "Selectors": row.get("Selectors") or "",
                    }
                )
        out["races"][rname] = {"race": (sub or race)["UUID"], "rows": rows}
        out["backgrounds"][bname] = {"uuid": bg["UUID"], "passive": bg["Passives"]}
    out["feats"] = {
        f["Name"]: {
            "uuid": f["UUID"],
            "selectors": f.get("Selectors") or "",
            "passives": f.get("PassivesAdded") or "",
            "boosts": f.get("Boosts") or "",
            "repeatable": f.get("CanBeTakenMultipleTimes") == "true",
        }
        for f in sorted(feats.values(), key=lambda f: f["Name"])
    }
    lists = {
        **{u: ("spells", a) for u, a in lsx_nodes(files, "Lists/SpellLists.lsx", "SpellList").items()},
        **{u: ("passives", a) for u, a in lsx_nodes(files, "Lists/PassiveLists.lsx", "PassiveList").items()},
        **{u: ("skills", a) for u, a in lsx_nodes(files, "Lists/SkillLists.lsx", "SkillList").items()},
    }
    used = set()
    texts = [r["Selectors"] for c in out["classes"].values() for r in c["levels"].values()]
    texts += [r["Selectors"] for s in out["subclasses"].values() for r in s["levels"].values()]
    texts += [r["Selectors"] for x in out["races"].values() for r in x["rows"]]
    texts += [f["selectors"] for f in out["feats"].values()]
    for t in texts:
        used |= {u for _, u in SELECTOR_LIST.findall(t)}
    out["lists"] = {}
    for u in sorted(used):
        if u not in lists:
            continue  # SelectAbilities / SelectAbilityBonus name AbilityLists: not a pick list
        kind, a = lists[u]
        ids = a.get("Spells") or a.get("Passives") or a.get("Skills") or ""
        out["lists"][u] = {
            "kind": kind,
            "name": a.get("Name") or a.get("Comment") or "",
            "ids": [x.strip() for x in re.split(r"[;,]", ids) if x.strip()],
        }
    return out


Path_ = tuple[str, ...]


def compare(old: Any, new: Any, path: Path_ = ()) -> list[str]:
    """Where `new` differs from `old`, for keys both have (new keys are fine; keys only `old` has are kept)."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [d for k in old if k in new for d in compare(old[k], new[k], (*path, str(k)))]
    if old != new:
        return [f"{'.'.join(path)}: was {json.dumps(old)[:120]}, the game data says {json.dumps(new)[:120]}"]
    return []


CHECKED: tuple[tuple[str, Callable[[dict[str, Any]], Any]], ...] = (
    # existing hand-read keys the generator remakes: (what, the old file's part in the generated shape)
    (
        "classes",
        lambda o: {
            c: {"table": v["table"], "levels": v["levels"], "equipment": v["equipment"]}
            for c, v in o.get("classes", {}).items()
        },
    ),
    ("races", lambda o: {r: v for r, v in o.get("races", {}).items() if "rows" in v}),
    (
        "backgrounds",
        lambda o: {b: {"uuid": v["uuid"], "passive": v["passive"]} for b, v in o.get("backgrounds", {}).items()},
    ),
    (
        "hirelings",
        lambda o: {c: {k: v[k] for k in ("origin", "race", "background")} for c, v in o.get("hirelings", {}).items()},
    ),
)


def merge(old: dict[str, Any], gen: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The static file with the generated data in, and the differences on keys both have."""
    diffs = []
    for key, part in CHECKED:
        diffs += compare({key: part(old)}, {key: gen[key]})
    out = dict(old)
    for c, v in gen["classes"].items():
        out.setdefault("classes", {})[c] = {**old.get("classes", {}).get(c, {}), **v}
    for key in ("races", "backgrounds"):
        for k, v in gen[key].items():
            out.setdefault(key, {})[k] = {**old.get(key, {}).get(k, {}), **v}
    for c, v in gen["hirelings"].items():
        if c not in old.get("hirelings", {}):
            out.setdefault("hirelings", {})[c] = {
                **v,
                "template": None,
                "template_name": None,
                "todo": "the root template (S_GLO_Hirelings_<Class> TemplateName, a binary .lsf) is not read yet: "
                "arena.mirror_spawn needs it; read it live (Ext.Template) or with an .lsf reader",
            }
    for key in ("subclasses", "items", "xp", "feats", "lists"):
        out[key] = gen[key]
    out["source"] = SOURCE
    return out, diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate tools/dev/arena_static.json's game data from the paks.")
    ap.add_argument("data", type=Path, help="the game's Data folder (Shared.pak, Gustav.pak, GustavX.pak)")
    ap.add_argument("--out", type=Path, default=STATIC)
    ap.add_argument("--dry-run", action="store_true", help="print the differences only, write nothing")
    ap.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="PATH",
        help="a difference at PATH (dotted, e.g. classes.Rogue.levels.1.Selectors) is known: classes take the game "
        "data, hirelings keep the hand-read value; repeatable",
    )
    a = ap.parse_args(argv)
    old = json.loads(a.out.read_text()) if a.out.exists() else {}
    new, diffs = merge(old, generate(read_paks(a.data), CLASSES))
    stop = [d for d in diffs if not any(d.startswith(p + ":") or d.startswith(p + ".") for p in a.accept)]
    for d in diffs:
        print("DIFF" if d in stop else "DIFF (accepted)", d, file=sys.stderr)
    for r in REPLACED:
        print("REPLACED (content differs)", r, file=sys.stderr)
    if a.dry_run or stop:
        print(f"{len(stop)} difference(s) not accepted; nothing written", file=sys.stderr)
        return 1 if stop else 0
    a.out.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n")
    print(
        f"wrote {a.out}: classes {', '.join(new['classes'])}; {len(new['subclasses'])} subclasses, "
        f"{len(new['lists'])} lists, {len(new['feats'])} feats, {len(new['items'])} items"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
