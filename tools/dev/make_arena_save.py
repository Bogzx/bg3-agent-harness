"""Mirror-team arenas (SPEC v2.2 §5): the pairs spec, each pair's kit checked against the game's data, the arena
file's `mirror` section (arena.setup makes the twins for each match from it: a save keeps neither our stat entries'
grants nor the kit's boosts, live check 12 step 13; orchestrator D308), and the symmetry check.

    uv run --project bridge python tools/dev/make_arena_save.py validate <spec.json>
    uv run --project bridge python tools/dev/make_arena_save.py mirror <spec.json>    # {level, teams, mirror}
    uv run --project bridge python tools/dev/make_arena_save.py symmetry <uuidA>:<uuidB> [...]

The spec (schema arena-mirror.v1) names 4 pairs; each pair is two copies of one hireling root template, one per team,
spawned with our level-1 stat entry for its class (Shared/ArenaStats.lua: BG3AI_Arena_<class>_L1; MakePlayer then gives
the copy the party's XP, level 2 in the crash-site save, and the class table's level-2 row), then
given one kit, the choices character creation would make, every one checked against the class's progression rows up
to the level (tools/dev/arena_static.json, from the game's data):

    {"schema": "arena-mirror.v1", "name": "mirror_crash_site", "level": 2,
     "location": {"level": "WLD_Main_A", "spawns": {"A": [x, y, z], "B": [x, y, z]}},
     "pairs": [{"class": "Fighter", "template": "<root template>" | null, "names": {"A": "Ash", "B": "Birch"},
                "kit": {"race": "HalfElf", "skills": ["Athletics", "Intimidation"], "expertise": [],
                        "ability_bonus": ["Strength", "Constitution"], "passives": [["FightingStyle_Defense"]],
                        "spells": [], "items": ["WPN_Longsword", {"item": "WPN_Handaxe", "equip": false}, ...]}},
               ... x4]}

kit.passives and kit.spells have one list per SelectPassives / SelectSpells selector, in the rows' order (a Ranger:
favoured enemy, natural explorer, then its level-2 fighting style). Every kit also gets PLAYER_PASSIVES. kit.items is
the class's character-creation equipment (EQP_CC_<class>) less EXCLUDED, as a multiset; weapons and armour are equipped
unless the entry says "equip": false. The game's lists themselves (which skills, styles, spells a selector offers) are
checked live by arena.mirror_kit. `template` may be null (validate accepts it; `mirror` will not). Pair i is teams.A[i]
and teams.B[i] in the arena file. `symmetry` asks the game's arena.symmetry (the game runs with BG3AI_ARENA=1) and exits
1 on any problem: a field that differs within a pair, a field that doesn't read, or one display name for two members.
"""

from __future__ import annotations

import sys

# Run as a script, this folder is sys.path[0]: keep its modules from shadowing the stdlib's.
if sys.path and sys.path[0] and __file__.startswith(sys.path[0]) and __name__ == "__main__":
    del sys.path[0]

import argparse
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "arena-mirror.v1"
SIDES = ("A", "B")
PAIRS = 4
# Spawn geometry, as arena.setup checks it (Server/ArenaSetup.lua Setup.check_geometry): each team on a line along x
# through its spawn, LINE_GAP_M apart; the spawns under SPAWN_DY_MAX_M apart in height; every distance across the lines
# the same both ways (A's member i to B's member j = B's i to A's j) within PAIR_DIST_TOL_M.
LINE_GAP_M = 1.5
SPAWN_DY_MAX_M = 0.3
PAIR_DIST_TOL_M = 0.1
# no subclass by level 2, every choice a selector list (orchestrator D300, D308: the Wizard picks a subclass at 2)
CLASSES = ("Fighter", "Rogue", "Barbarian", "Ranger")
LEVELS = (1, 2)  # the arena's level: what the twins read after MakePlayer (setup fails on any other)
# What every player character has and a copy lacks: Lae'zel's DeathSavingThrows (its boost is DownedStatus(DOWNED));
# no progression in the game data grants it (live check 12 step 10).
PLAYER_PASSIVES = ("DeathSavingThrows",)
STATIC = Path(__file__).resolve().parent / "arena_static.json"
# Character creation's items a kit leaves out: the Revivify scroll (orchestrator D300) and what plays no part in a
# fight.
EXCLUDED = (
    "OBJ_Scroll_Revivify",
    "OBJ_Keychain",
    "OBJ_Bag_AlchemyPouch",
    "ARM_Camp_Body",
    "ARM_Camp_Shoes",
    "OBJ_Backpack_CampSupplies",
)
SKILLS = (
    "Deception",
    "Intimidation",
    "Performance",
    "Persuasion",
    "Acrobatics",
    "SleightOfHand",
    "Stealth",
    "Arcana",
    "History",
    "Investigation",
    "Nature",
    "Religion",
    "Athletics",
    "AnimalHandling",
    "Insight",
    "Medicine",
    "Perception",
    "Survival",
)
ABILITIES = ("Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma")
CHOICE = {
    "SelectSkills": "skills",
    "SelectSkillsExpertise": "expertise",
    "SelectPassives": "passives",
    "SelectSpells": "spells",
}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,35}$")  # SCN-<name> stays within the bridge's 40-char save label
MEMBER_RE = re.compile(r"^[A-Z][A-Za-z' -]{0,23}$")  # a display name (scn.rename)

Rpc = Callable[[str, dict[str, Any]], Any]


class SpecError(ValueError):
    pass


class AsymmetryError(Exception):
    """arena.symmetry found problems: the save must not be made (or the loaded save is not a mirror)."""


def _vec3(v: Any, what: str) -> list[float]:
    if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, int | float) for x in v)):
        raise SpecError(f"{what} must be [x, y, z]")
    return [float(x) for x in v]


def _line(spot: list[float], n: int) -> list[list[float]]:
    return [[spot[0] + (i - (n - 1) / 2) * LINE_GAP_M, spot[1], spot[2]] for i in range(n)]


def check_geometry(a: list[float], b: list[float], n: int) -> None:
    """SpecError unless both teams stand on the same terms (arena.setup refuses the same spawns)."""
    dy = abs(a[1] - b[1])
    if dy >= SPAWN_DY_MAX_M:
        raise SpecError(
            f"location.spawns A and B are {dy:.2f} m apart in height (y); they must be under {SPAWN_DY_MAX_M} m"
        )
    la, lb = _line(a, n), _line(b, n)
    for i in range(n):
        for j in range(n):
            ab, ba = math.dist(la[i], lb[j]), math.dist(lb[i], la[j])
            if abs(ab - ba) > PAIR_DIST_TOL_M:
                raise SpecError(
                    f"location.spawns: A's member {i + 1} is {ab:.2f} m from B's member {j + 1}, B's member {i + 1} "
                    f"{ba:.2f} m from A's member {j + 1}; they must be the same within {PAIR_DIST_TOL_M} m"
                )


def check_spec(spec: Any, need_templates: bool = False, static: dict[str, Any] | None = None) -> dict[str, Any]:
    """The spec, checked; SpecError names the first problem."""
    static = static if static is not None else load_static()
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a JSON object")
    if spec.get("schema") != SCHEMA:
        raise SpecError(f"schema must be {SCHEMA!r}")
    if not isinstance(spec.get("name"), str) or not NAME_RE.match(spec["name"]):
        raise SpecError("name must be lower-case letters, digits and _ (1..36, a letter first)")
    lv = spec.get("level")
    if not isinstance(lv, int) or isinstance(lv, bool) or lv not in LEVELS:
        raise SpecError(f"level must be one of {', '.join(map(str, LEVELS))} (the levels our stat entries have)")
    loc = spec.get("location")
    if not isinstance(loc, dict) or not isinstance(loc.get("level"), str) or not loc["level"]:
        raise SpecError("location.level must name the game level")
    spawns = loc.get("spawns")
    if not isinstance(spawns, dict):
        raise SpecError("location.spawns must have A and B")
    at = {s: _vec3(spawns.get(s), f"location.spawns.{s}") for s in SIDES}
    check_geometry(at["A"], at["B"], PAIRS)
    pairs = spec.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != PAIRS:
        raise SpecError(f"pairs must list {PAIRS} pairs")
    names: set[str] = set()
    for i, p in enumerate(pairs, 1):
        if not isinstance(p, dict):
            raise SpecError(f"pair {i} must be an object")
        if p.get("class") not in CLASSES:
            raise SpecError(f"pair {i}: class must be one of {', '.join(CLASSES)}")
        tpl = p.get("template")
        if tpl is None and need_templates:
            raise SpecError(f"pair {i}: no template yet (the probe lists the hireling templates)")
        if tpl is not None and (not isinstance(tpl, str) or not tpl):
            raise SpecError(f"pair {i}: template must be a root template name or null")
        pn = p.get("names")
        if not isinstance(pn, dict):
            raise SpecError(f"pair {i}: names must have A and B")
        for s in SIDES:
            n = pn.get(s)
            if not isinstance(n, str) or not MEMBER_RE.match(n):
                raise SpecError(f"pair {i}: names.{s} must be a display name (a capital first, up to 24 chars)")
            if n.lower() in names:
                raise SpecError(f"pair {i}: the name {n} is used twice (names must be unique across both teams)")
            names.add(n.lower())
        kit_plan(p.get("kit"), p["class"], lv, static, f"pair {i}: kit")
    return spec


def load_static(path: Path = STATIC) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def selectors(text: str) -> list[tuple[str, list[str]]]:
    """A progression row's Selectors string -> [(name, args)], in order."""
    out = []
    for part in filter(None, (x.strip() for x in (text or "").split(";"))):
        m = re.fullmatch(r"(\w+)\((.*)\)", part)
        if not m:
            raise SpecError(f"a selector the validator can't read: {part!r}")
        out.append((m.group(1), [a.strip() for a in m.group(2).split(",")]))
    return out


def _names(v: Any, allowed: Sequence[str] | None, what: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise SpecError(f"{what} must list names")
    if len(set(v)) != len(v):
        raise SpecError(f"{what} lists a name twice")
    bad = [x for x in v if allowed is not None and x not in allowed]
    if bad:
        raise SpecError(f"{what}: {', '.join(bad)} is not one of {', '.join(allowed or ())}")
    return list(v)


def kit_plan(kit: Any, cls: str, level: int, static: dict[str, Any], what: str = "kit") -> dict[str, Any]:
    """The kit checked against the class's progression rows 1..level and its race's level-1 row; SpecError names
    the first problem. -> {race (uuid), kit (arena.mirror_kit's)}."""
    if not isinstance(kit, dict):
        raise SpecError(f"{what} must be an object")
    c = static["classes"][cls]
    choices: list[dict[str, Any]] = []
    bonus = False
    for lv in range(1, level + 1):
        row = c["levels"].get(str(lv))
        if row is None:
            raise SpecError(f"{what}: no {cls} row for level {lv} in the game data")
        if row.get("subclasses"):
            raise SpecError(f"{what}: {cls} picks a subclass at level {lv}: no game call sets one")
        for name, args in selectors(row["Selectors"]):
            if name == "AddSpells":
                continue  # the game's own grant (the stat entry's Progressions table)
            if name == "SelectAbilityBonus":
                bonus = True
            elif name in CHOICE:  # every pick exact: the classes left choose known spells, not prepared ones
                choices.append({"kind": CHOICE[name], "list": args[0], "n": int(args[1]), "exact": True})
            else:
                raise SpecError(f"{what}: {cls} level {lv} has {name}, a choice a kit can't make")
    for kind, sel in (("spells", "SelectSpells"), ("passives", "SelectPassives")):
        mine = [x for x in choices if x["kind"] == kind]
        lists = kit.get(kind, [])
        if not isinstance(lists, list) or len(lists) != len(mine):
            raise SpecError(f"{what}: {kind} must have {len(mine)} lists (one per {sel})")
        for x, ids in zip(mine, lists, strict=True):
            x["ids"] = _names(ids, None, f"{what}: {kind}")
    for x in choices:
        if x["kind"] in ("skills", "expertise"):
            if sum(1 for y in choices if y["kind"] == x["kind"]) > 1:
                raise SpecError(f"{what}: {cls} has two {x['kind']} choices up to level {level}: not supported")
            x["ids"] = _names(kit.get(x["kind"], []), SKILLS, f"{what}: {x['kind']}")
        n = len(x["ids"])
        if (x["exact"] and n != x["n"]) or (not x["exact"] and not 1 <= n <= x["n"]):
            raise SpecError(
                f"{what}: {x['kind']} has {n}, the game's selector takes {'' if x['exact'] else '1..'}{x['n']}"
            )
    for k in ("skills", "expertise"):
        if kit.get(k) and not any(x["kind"] == k for x in choices):
            raise SpecError(f"{what}: {cls} has no {k} choice up to level {level}")
    kinds = {x["kind"]: x["ids"] for x in choices}
    loose = [s for s in kinds.get("expertise", []) if s not in kinds.get("skills", [])]
    if loose:
        raise SpecError(f"{what}: expertise in {', '.join(loose)}, which the kit's skills don't hold")
    ab = kit.get("ability_bonus")
    if bonus != (ab is not None):
        raise SpecError(
            f"{what}: ability_bonus "
            + ("must name the +2 and the +1 ability" if bonus else f"has no selector in the {cls} rows")
        )
    if ab is not None and (len(_names(ab, ABILITIES, f"{what}: ability_bonus")) != 2):
        raise SpecError(f"{what}: ability_bonus must name the +2 and the +1 ability")
    race = static["races"].get(kit.get("race"))
    if race is None:
        raise SpecError(f"{what}: race must be one of {', '.join(static['races'])}")
    rrow = race["levels"]["1"]
    if rrow["Selectors"]:
        raise SpecError(f"{what}: race {kit['race']} has choices a kit can't make")
    return {
        "race": race["race"],
        "kit": {
            "race": {"boosts": rrow["Boosts"], "passives": [x for x in rrow["PassivesAdded"].split(";") if x]},
            "player": {"passives": list(PLAYER_PASSIVES)},
            "ability_bonus": ab,
            "choices": choices,
            "items": _items(kit.get("items"), c["equipment"], cls, what),
        },
    }


def _items(items: Any, cc: list[dict[str, Any]], cls: str, what: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise SpecError(f"{what}: items must list the class's items")
    want = sorted(e["item"] for e in cc if e["item"] not in EXCLUDED)
    by = {e["item"]: e for e in cc}
    got, out = [], {}
    for x in items:
        entry = {"item": x} if isinstance(x, str) else x
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), str):
            raise SpecError(f"{what}: an item must be a name or {{item, equip}}")
        name = entry["item"]
        got.append(name)
        if name not in by:
            continue  # named below
        e = out.setdefault(
            name,
            {
                "item": name,
                "root": by[name]["root"],
                "count": 0,
                "equip": entry.get("equip", by[name]["type"] in ("Weapon", "Armor")),
            },
        )
        e["count"] += 1
    if sorted(got) != want:
        extra = [g for g in got if got.count(g) > want.count(g)]
        short = [w for w in want if want.count(w) > got.count(w)]
        raise SpecError(
            f"{what}: items must be EQP_CC_{cls} less {', '.join(EXCLUDED)}"
            + (f"; not that: {', '.join(sorted(set(extra)))}" if extra else "")
            + (f"; missing: {', '.join(sorted(set(short)))}" if short else "")
        )
    return list(out.values())


def load_spec(path: Path, need_templates: bool = False) -> dict[str, Any]:
    try:
        spec = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SpecError(f"cannot read {path}: {e}") from e
    return check_spec(spec, need_templates)


def mirror_section(spec: dict[str, Any], static: dict[str, Any] | None = None) -> dict[str, Any]:
    """The arena file's level, teams (names, pair i at index i on both sides) and mirror section (arena.setup's)."""
    static = static if static is not None else load_static()
    check_spec(spec, need_templates=True, static=static)
    pairs = []
    for p in spec["pairs"]:
        plan = kit_plan(p["kit"], p["class"], spec["level"], static)
        pairs.append(
            {
                "class": p["class"],
                "template": p["template"],
                "race": plan["race"],
                "stats": f"BG3AI_Arena_{p['class']}_L1",
                "names": dict(p["names"]),
                "kit": plan["kit"],
            }
        )
    teams = {s: [{"name": p["names"][s]} for p in spec["pairs"]] for s in SIDES}
    return {"level": spec["level"], "teams": teams, "mirror": {"pairs": pairs}}


def check_symmetry(rpc: Rpc, pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """arena.symmetry for these pairs; AsymmetryError with every problem unless the game finds none."""
    r = rpc("arena.symmetry", {"pairs": [list(p) for p in pairs]})
    if not isinstance(r, dict) or "ok" not in r:
        raise AsymmetryError(f"arena.symmetry answered {r!r}")
    if not r["ok"]:
        raise AsymmetryError("; ".join(r.get("problems") or ["not symmetric"]))
    return r


def _pair_arg(s: str) -> tuple[str, str]:
    a, sep, b = s.partition(":")
    if not sep or not a or not b:
        raise argparse.ArgumentTypeError(f"want <uuidA>:<uuidB>, got {s!r}")
    return a, b


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mirror-team arenas: spec check, the arena's mirror section, symmetry.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check a pairs spec (templates may still be null)")
    v.add_argument("spec", type=Path)
    m = sub.add_parser("mirror", help="print the arena file's level, teams and mirror section for a pairs spec")
    m.add_argument("spec", type=Path)
    y = sub.add_parser("symmetry", help="ask the running game whether each pair is a mirror")
    y.add_argument("pairs", nargs="+", type=_pair_arg)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            spec = load_spec(args.spec)
            todo = sum(1 for p in spec["pairs"] if p.get("template") is None)
            print(f"{spec['name']}: ok" + (f" ({todo} templates still null)" if todo else ""))
            return 0
        if args.cmd == "mirror":
            print(json.dumps(mirror_section(load_spec(args.spec)), indent=1))
            return 0
        os.environ.setdefault("BG3AI_RUNTIME", str(REPO / "runtime" / "make_arena_save"))
        sys.path.insert(0, str(REPO / "bridge" / "src"))
        from bg3ai.game import GameError
        from bg3ai.tools import core

        try:
            r = check_symmetry(lambda op, a: core.game.rpc(op, a), args.pairs)
        except GameError as e:
            print(f"make_arena_save: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "pairs": len(r.get("pairs") or [])}))
        return 0
    except (SpecError, AsymmetryError) as e:
        print(f"make_arena_save: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
