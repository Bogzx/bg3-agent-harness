"""Mirror-team arenas (SPEC v2.2 §5): the pairs spec, each pair's kit checked against the game's data, the arena
file's `mirror` section (arena.setup makes the twins for each match from it: a save keeps neither our stat entries'
grants nor the kit's boosts, live check 12 step 13; orchestrator D308), and the symmetry check.

    uv run --project bridge python tools/dev/make_arena_save.py validate <spec.json | builds.json> [--rated]
    uv run --project bridge python tools/dev/make_arena_save.py mirror <spec.json>    # {level, teams, mirror}
    uv run --project bridge python tools/dev/make_arena_save.py builds-mirror <builds.json> Ash:Birch ...
    uv run --project bridge python tools/dev/make_arena_save.py builds-arena <builds.json> <base.json> <name> \\
        --spawn-a x,y,z --spawn-b x,y,z [--line-gap m] --out <arena.json> Ash:Birch ...
    uv run --project bridge python tools/dev/make_arena_save.py symmetry <uuidA>:<uuidB> [...]
    uv run --project bridge python tools/dev/make_arena_save.py reference-read <builds.json> <build id> <uuid> \\
        --evidence <text>                                       # the running game's profile -> the build file
    uv run --project bridge python tools/dev/make_arena_save.py reference-diff <builds.json> <build id> <twin uuid>

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

kit.race names an arena_static.json race; its part of the kit is that race's level-1 rows, the parent race's first
(a Drow's Dancing Lights included), and names the race id, which arena.setup checks against the pair's race. A race
with a skill choice (Human) takes kit.race_skills. kit.background (optional) names a background: its passive joins
the player passives, and no skill choice may pick a skill the race, background or a picked passive already gives.
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
# through its spawn, LINE_GAP_M (or the arena's location.line_gap) apart; the spawns under SPAWN_DY_MAX_M apart in
# height; every distance across the lines the same both ways (A's member i to B's member j = B's i to A's j)
# within PAIR_DIST_TOL_M.
LINE_GAP_M = 1.5
SPAWN_DY_MAX_M = 0.3
PAIR_DIST_TOL_M = 0.1
# The arena's classes: each needs our stat entry (Shared/ArenaStats.lua) and its hireling in arena_static.json. The
# Cleric (subclass at 1) and Wizard (at 2) need the kit's subclass part (ops/logs/arena-4v4-builds.md G5).
CLASSES = ("Fighter", "Rogue", "Barbarian", "Ranger", "Cleric", "Wizard")
LEVELS = (1, 2)  # the arena's level: what the twins read after MakePlayer (setup fails on any other)
TIERS = (2, 5, 12)  # a build file's level (arena-4v4-builds.md; arena.setup's tier-XP step: mirror.party_xp)
PREPARERS = ("Cleric", "Wizard")  # prepare casting-modifier + level spells (min 1) from what they have
# A preparer whose spells are all selector picks (no class row AddSpells a spell list): its kit adds only the prepared
# picks, so a copy's book is what the build plays. A Cleric's class rows add its whole list, which no call removes (S1
# P3): its `prepared` stays in the kit, named not_applied by arena.mirror_kit.
PICKS_ARE_ITS_BOOK = ("Wizard",)
ALL_ABILITIES = "b9149c8e-52c8-46e5-9cb6-fc39301c05fe"  # the AbilityList every ASI and the +2/+1 pick from
ENCHANT = {5: 1, 12: 2}  # a tier's items: each weapon and armour as its +N entry (<item>_N) where the game has one
EXTRA_POTIONS = {5: ("OBJ_Potion_Healing_Greater",), 12: ("OBJ_Potion_Healing_Greater", "OBJ_Potion_Healing_Superior")}
# What every player character has and a copy lacks: Lae'zel's DeathSavingThrows (its boost is DownedStatus(DOWNED));
# no progression in the game data grants it (live check 12 step 10).
PLAYER_PASSIVES = ("DeathSavingThrows",)
STATIC = Path(__file__).resolve().parent / "arena_static.json"
# Character creation's items a kit leaves out: the Revivify scroll (orchestrator D300), what plays no part in a fight,
# and the knock-down sources (Fable REVIEW-12 option (d), 2026-11-04: Prone can cost a whole phase in the shared turn
# group, and no turn order fixes it; the Rogue's grease bottles and the Ranger's Arrow of Ice make PRONE surfaces).
EXCLUDED = (
    "OBJ_Scroll_Revivify",
    "OBJ_GreaseBottle",
    "OBJ_ArrowOfIce",
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


def _line(spot: list[float], n: int, gap: float) -> list[list[float]]:
    return [[spot[0] + (i - (n - 1) / 2) * gap, spot[1], spot[2]] for i in range(n)]


def check_geometry(a: list[float], b: list[float], n: int, gap: float = LINE_GAP_M) -> None:
    """SpecError unless both teams stand on the same terms (arena.setup refuses the same spawns)."""
    dy = abs(a[1] - b[1])
    if dy >= SPAWN_DY_MAX_M:
        raise SpecError(
            f"location.spawns A and B are {dy:.2f} m apart in height (y); they must be under {SPAWN_DY_MAX_M} m"
        )
    la, lb = _line(a, n, gap), _line(b, n, gap)
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


def scores(kit: dict[str, Any], base: dict[str, int], level: int) -> dict[str, int]:
    """The ability scores at `level`: the point-buy, the +2/+1, then each improvement's ASI points (unchecked here)."""
    out = dict(base)
    ab = kit.get("ability_bonus") or []
    for a, n in zip(ab, (2, 1), strict=False):
        out[a] = out.get(a, 0) + n
    for imp in kit.get("improvements") or []:
        for a, n in ((imp or {}).get("asi") or (imp or {}).get("abilities") or {}).items():
            out[a] = out.get(a, 0) + n
    return out


def selector_rows(cls: str, subclass: str | None, level: int, static: dict[str, Any], what: str) -> list[dict]:
    """The rows a character of `cls` (and `subclass`) gets at levels 1..level, in order: per level the class's row,
    then the subclass's. -> [{level, source ("class" | "subclass"), row}]. SpecError on a missing class row, a
    subclass the class offers but the kit doesn't name (or one it names that isn't the class's)."""
    c = static["classes"][cls]
    sub = None
    if subclass is not None:
        sub = static.get("subclasses", {}).get(subclass)
        if sub is None or sub["class"] != cls:
            raise SpecError(f"{what}: subclass {subclass} is not a {cls} subclass in the game data")
    out: list[dict[str, Any]] = []
    offered = None
    for lv in range(1, level + 1):
        row = c["levels"].get(str(lv))
        if row is None:
            raise SpecError(f"{what}: no {cls} row for level {lv} in the game data")
        out.append({"level": lv, "source": "class", "row": row})
        if row.get("subclasses") and offered is None:
            offered = lv
        if sub is not None and str(lv) in sub["levels"]:
            out.append({"level": lv, "source": "subclass", "row": sub["levels"][str(lv)]})
    if offered is not None and sub is None:
        raise SpecError(f"{what}: {cls} picks a subclass at level {offered}: the kit must name one")
    if offered is None and sub is not None:
        raise SpecError(f"{what}: {cls} picks no subclass by level {level}")
    return out


def _in_list(ids: list[str], list_id: str, static: dict[str, Any], what: str) -> None:
    """Every id is in the game's list `list_id` (when the static data has the list)."""
    lst = static.get("lists", {}).get(list_id)
    if lst is None:
        return
    bad = [x for x in ids if x not in lst["ids"]]
    if bad:
        raise SpecError(f"{what}: {', '.join(bad)} is not in the game's list {lst['name'] or list_id}")


def _spells_of(list_id: str, static: dict[str, Any]) -> list[str]:
    lst = static.get("lists", {}).get(list_id)
    return list(lst["ids"]) if lst else []


def _per_selector(v: Any, n: int, what: str) -> list[list[str]]:
    """A kit's picks for `n` selectors of one kind: a list per selector; one selector may take a flat list."""
    if v is None:
        v = []
    if isinstance(v, list) and n == 1 and all(isinstance(x, str) for x in v):
        v = [v]
    if not isinstance(v, list) or len(v) != n or not all(isinstance(x, list) for x in v):
        raise SpecError(f"{what} must have {n} lists (one per selector)")
    return v


def _row_ability(args: list[str], i: int) -> str | None:
    """The casting ability a selector's row names at args[i] (AddSpells(list,,Charisma,,AlwaysPrepared): 2,
    SelectSpells(list,1,0,HighElfCantrip,Intelligence): 4), or None: a real character's spell then has its class's."""
    return (args[i] or None) if len(args) > i else None


def improvement_plan(imp: Any, lv: int, static: dict[str, Any], taken: list[str], what: str) -> dict[str, Any]:
    """One AllowImprovement level: {"asi": {ability: points}} (2 points, at most 2 in one) or {"feat": name, picks per
    the feat's selectors: "abilities", "passives", "spells", "skills"}. -> {boosts: [...], passives, spells, choices}"""
    w = f"{what}: improvement at level {lv}"
    if not isinstance(imp, dict) or len({"asi", "feat"} & set(imp)) != 1:
        raise SpecError(f"{w} must be {{asi}} or {{feat, ...}}")
    out: dict[str, Any] = {"boosts": [], "passives": [], "spells": [], "choices": []}
    if "asi" in imp:
        pts = imp["asi"]
        if set(imp) != {"asi"} or not isinstance(pts, dict) or set(pts) - set(ABILITIES):
            raise SpecError(f"{w}: asi must map abilities to points")
        if sum(pts.values()) != 2 or not all(isinstance(n, int) and 1 <= n <= 2 for n in pts.values()):
            raise SpecError(f"{w}: asi must give 2 points, 1 or 2 per ability")
        out["boosts"] = [f"Ability({a},{n})" for a, n in pts.items()]
        return out
    name = imp["feat"]
    feat = static.get("feats", {}).get(name)
    if feat is None:
        raise SpecError(f"{w}: no feat {name!r} in the game data")
    if name in taken and not feat["repeatable"]:
        raise SpecError(f"{w}: {name} is taken twice")
    taken.append(name)
    allowed = {"feat"}
    out["passives"] = [x for x in feat["passives"].split(";") if x]
    out["boosts"] = [x for x in feat["boosts"].split(";") if x]
    for sel, args in selectors(feat["selectors"]):
        if sel == "SelectAbilities":
            allowed.add("abilities")
            pts = imp.get("abilities")
            if args[0] != ALL_ABILITIES or not isinstance(pts, dict) or set(pts) - set(ABILITIES):
                raise SpecError(f"{w}: {name}'s ability pick is checked only on the all-abilities list")
            if sum(pts.values()) != int(args[1]) or any(n > int(args[2]) for n in pts.values()):
                raise SpecError(f"{w}: {name} gives {args[1]} points, at most {args[2]} per ability")
            out["boosts"] += [f"Ability({a},{n})" for a, n in pts.items()]
        elif sel in CHOICE:
            kind = CHOICE[sel]
            allowed.add(kind)
            ids = _names(imp.get(kind, []), SKILLS if kind in ("skills", "expertise") else None, f"{w}: {kind}")
            if len(ids) != int(args[1]):
                raise SpecError(f"{w}: {name} takes {args[1]} {kind}")
            _in_list(ids, args[0], static, f"{w}: {kind}")
            x = {"kind": kind, "list": args[0], "n": int(args[1]), "exact": True, "ids": ids}
            if kind == "spells":
                x["ability"] = _row_ability(args, 4)
            out["choices"].append(x)
        else:
            raise SpecError(f"{w}: {name} has {sel}, a choice a kit can't make")
    if extra := sorted(set(imp) - allowed):
        raise SpecError(f"{w}: {name} has no pick for {', '.join(extra)}")
    return out


def kit_plan(
    kit: Any,
    cls: str,
    level: int,
    static: dict[str, Any],
    what: str = "kit",
    base: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The kit checked against the class's (and its subclass's) progression rows 1..level, its race's level-1 rows and
    the game's lists; SpecError names the first problem. -> {race (uuid), kit (arena.mirror_kit's)}. `base` (the
    point-buy) is needed only for prepared spells (the casting ability's modifier). The game applies the class rows'
    fixed parts itself (our stat entry's Progressions and the party level); the subclass rows' and the improvements'
    fixed parts are the kit's `grants`."""
    if not isinstance(kit, dict):
        raise SpecError(f"{what} must be an object")
    c = static["classes"][cls]
    rows = selector_rows(cls, kit.get("subclass"), level, static, what)
    choices: list[dict[str, Any]] = []
    grants: dict[str, list[str]] = {"boosts": [], "passives": [], "passives_removed": [], "spells": []}
    pool: list[str] = []  # spells a preparer prepares from (no AlwaysPrepared flag)
    abilities: dict[str, str] = {}  # spell -> its ability: AddSpell gives none, ArenaMirror writes the rows
    improvements = kit.get("improvements") or []
    if not isinstance(improvements, list):
        raise SpecError(f"{what}: improvements must list one entry per improvement level")
    imp_levels = [r["level"] for r in rows if r["source"] == "class" and r["row"].get("AllowImprovement")]
    if len(improvements) != len(imp_levels):
        raise SpecError(
            f"{what}: improvements must have {len(imp_levels)} (levels {imp_levels}), not {len(improvements)}"
        )
    taken: list[str] = []
    imps = iter(improvements)
    bonus = False
    for r in rows:
        lv, row = r["level"], r["row"]
        if r["source"] == "subclass":
            grants["boosts"] += [b for b in (row.get("Boosts") or "").split(";") if b]
            grants["passives"] += [x for x in (row.get("PassivesAdded") or "").split(";") if x]
            grants["passives_removed"] += [x for x in (row.get("PassivesRemoved") or "").split(";") if x]
        for name, args in selectors(row["Selectors"]):
            prepared_flag = len(args) > 6 and args[6] == "AlwaysPrepared"
            if name == "AddSpells":
                if r["source"] == "subclass":
                    ids = _spells_of(args[0], static)  # the class's own: the game's (stat entry)
                    grants["spells"] += ids
                    if spell_ab := _row_ability(args, 2) or c["casting"]:
                        abilities.update(dict.fromkeys(ids, spell_ab))
                elif not (len(args) > 4 and args[4] == "AlwaysPrepared"):
                    pool += _spells_of(args[0], static)
                continue
            if name == "ReplacePassives":  # the game offers a swap of an earlier pick: a build keeps its pick
                continue
            if name == "SelectAbilityBonus":
                bonus = True
            elif name in CHOICE:  # every pick exact
                x = {"kind": CHOICE[name], "list": args[0], "n": int(args[1]), "exact": True, "level": lv}
                x["pool"] = CHOICE[name] == "spells" and not prepared_flag
                if CHOICE[name] == "spells":
                    x["ability"] = _row_ability(args, 4)
                choices.append(x)
            else:
                raise SpecError(f"{what}: {cls} level {lv} has {name}, a choice a kit can't make")
        if r["source"] == "class" and row.get("AllowImprovement"):
            imp = improvement_plan(next(imps), lv, static, taken, what)
            grants["boosts"] += imp["boosts"]
            grants["passives"] += imp["passives"]
            for x in imp["choices"]:
                x["level"], x["pool"] = lv, False
            choices += imp["choices"]
    for kind, sel in (("spells", "SelectSpells"), ("passives", "SelectPassives")):
        mine = [x for x in choices if x["kind"] == kind and "ids" not in x]
        lists = kit.get(kind, [])
        if not isinstance(lists, list) or len(lists) != len(mine):
            raise SpecError(f"{what}: {kind} must have {len(mine)} lists (one per {sel})")
        for x, ids in zip(mine, lists, strict=True):
            x["ids"] = _names(ids, None, f"{what}: {kind}")
    for kind in ("skills", "expertise"):
        mine = [x for x in choices if x["kind"] == kind and "ids" not in x]
        if kit.get(kind) and not mine:
            raise SpecError(f"{what}: {cls} has no {kind} choice up to level {level}")
        for x, ids in zip(mine, _per_selector(kit.get(kind), len(mine), f"{what}: {kind}"), strict=True):
            x["ids"] = _names(ids, SKILLS, f"{what}: {kind}")
    for x in choices:
        n = len(x["ids"])
        if (x["exact"] and n != x["n"]) or (not x["exact"] and not 1 <= n <= x["n"]):
            raise SpecError(
                f"{what}: {x['kind']} has {n}, the game's selector takes {'' if x['exact'] else '1..'}{x['n']}"
            )
        x["from_pool"] = x.pop("pool")
        if x["from_pool"]:
            pool += x["ids"]
    race, rpart, rchoices, fixed = race_plan(kit, static, what, c["casting"])
    abilities.update(rpart.pop("spell_abilities", {}))
    book_only = cls in PICKS_ARE_ITS_BOOK and kit.get("prepared") is not None
    pool_picks = [x for x in choices if x.pop("from_pool", False)]
    bg = kit.get("background")
    player = list(PLAYER_PASSIVES)
    if bg is not None:
        b = static["backgrounds"].get(bg)
        if b is None:
            raise SpecError(f"{what}: background must be one of {', '.join(static['backgrounds'])}")
        player.append(b["passive"])
        fixed += [(s, f"background {bg}") for s in b.get("skills", [])]
    # A passive's skill leaves the class group once it's picked (live 2026-11-06: Bounty Hunter, Investigation)
    gives = static.get("passive_skills", {})
    for x in (c for c in choices if c["kind"] == "passives"):
        fixed += [(s, f"passive {p}") for p in x["ids"] for s in gives.get(p, [])]
    choices = rchoices + choices
    picked = [s for x in choices if x["kind"] == "skills" for s in x["ids"]]
    for s in picked:
        if picked.count(s) > 1:
            raise SpecError(f"{what}: {s} is picked by two skill choices")
        src = next((f for k, f in fixed if k == s), None)
        if src:
            raise SpecError(f"{what}: {s} is picked, but {src} already gives it (the game doesn't offer it)")
    for x in choices:  # after the overlap checks: their errors say more than "not in the list"
        if x["kind"] != "expertise":
            _in_list(x["ids"], x["list"], static, f"{what}: {x['kind']} (level {x.get('level', 1)})")
        x.pop("level", None)
    held = picked + [k for k, _ in fixed]
    expert = [s for x in choices if x["kind"] == "expertise" for s in x["ids"]]
    loose = [s for s in expert if s not in held]
    if loose:
        raise SpecError(f"{what}: expertise in {', '.join(loose)}, which the kit's skills don't hold")
    if twice := sorted({s for s in expert if expert.count(s) > 1}):
        raise SpecError(f"{what}: expertise in {', '.join(twice)} is picked twice")
    ab = kit.get("ability_bonus")
    if bonus != (ab is not None):
        raise SpecError(
            f"{what}: ability_bonus "
            + ("must name the +2 and the +1 ability" if bonus else f"has no selector in the {cls} rows")
        )
    if ab is not None and (len(_names(ab, ABILITIES, f"{what}: ability_bonus")) != 2):
        raise SpecError(f"{what}: ability_bonus must name the +2 and the +1 ability")
    prepared = kit.get("prepared")
    if cls in PREPARERS:
        if base is None:
            raise SpecError(f"{what}: {cls} prepares spells: the point-buy is needed to count them")
        sc = scores(kit, base, level)
        if over := sorted(a for a, v in sc.items() if v > 20):
            raise SpecError(f"{what}: {', '.join(over)} above 20 at level {level}")
        n = max(1, (sc[c["casting"]] - 10) // 2 + level)
        ids = _names(prepared if prepared is not None else [], None, f"{what}: prepared")
        if len(ids) != n:
            raise SpecError(f"{what}: prepared has {len(ids)}, a level-{level} {cls} prepares {n}")
        if bad := [x for x in ids if x not in pool]:
            raise SpecError(f"{what}: prepared {', '.join(bad)} is not a spell the {cls} has to prepare")
    elif prepared is not None:
        raise SpecError(f"{what}: {cls} doesn't prepare spells (prepared must be left out)")
    if book_only:
        for x in pool_picks:
            x["ids"], x["exact"] = [i for i in x["ids"] if i in prepared], False
        choices = [x for x in choices if x["ids"]]
    for x in choices:
        spell_ab = x.pop("ability", None) or c["casting"]
        if x["kind"] == "spells" and spell_ab:
            abilities.update(dict.fromkeys(x["ids"], spell_ab))
    out_kit: dict[str, Any] = {
        "race": rpart,
        "player": {"passives": player},
        "ability_bonus": ab,
        "choices": choices,
        "items": _items(kit.get("items"), c["equipment"], cls, what, level, static),
    }
    if kit.get("subclass") is not None:
        out_kit["subclass"] = static["subclasses"][kit["subclass"]]["uuid"]
    if any(grants.values()):
        out_kit["grants"] = {
            "boosts": ";".join(grants["boosts"]),
            "passives": grants["passives"],
            "spells": grants["spells"],
        }
        if grants["passives_removed"]:  # a Berserker's RageFrenzyUnlock replaces the class row's RageUnlock
            out_kit["grants"]["passives_removed"] = grants["passives_removed"]
    if prepared is not None and not book_only:
        out_kit["prepared"] = list(prepared)
    if abilities:
        out_kit["spell_abilities"] = abilities
    return {"race": race, "kit": out_kit}


def race_plan(
    kit: dict[str, Any], static: dict[str, Any], what: str, casting: str | None = None
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[tuple[str, str]]]:
    """The kit's race part from the race's level-1 rows, its parent race's first (A1, D411: a build's race, so a Wood
    Half-Elf also gets the subrace's Movement and Stealth). -> (race id, the kit's race part {race, boosts, passives,
    spells?, spell_abilities?}, the race's choices (Human: one skill, from kit.race_skills), [(skill, where from)] the
    rows give). `casting`: the class's, for a row that names no ability."""
    name = kit.get("race")
    race = static["races"].get(name)
    if race is None:
        raise SpecError(f"{what}: race must be one of {', '.join(static['races'])}")
    rows = race.get("rows") or [race["levels"]["1"]]
    boosts = [b for r in rows for b in (r["Boosts"] or "").split(";") if b]
    passives = [x for r in rows for x in (r["PassivesAdded"] or "").split(";") if x]
    spells: list[str] = []
    abilities: dict[str, str] = {}
    choices: list[dict[str, Any]] = []
    for r in rows:
        for sel, args in selectors(r["Selectors"]):
            if sel == "AddSpells" and args[0] in static.get("spell_lists", {}):
                spells += static["spell_lists"][args[0]]["spells"]
                if ab := _row_ability(args, 2) or casting:
                    abilities.update(dict.fromkeys(static["spell_lists"][args[0]]["spells"], ab))
            elif sel == "SelectSpells":
                ids = _names(kit.get("race_spells", []), None, f"{what}: race_spells")
                if len(ids) != int(args[1]):
                    raise SpecError(f"{what}: race_spells has {len(ids)}, the race {name}'s selector takes {args[1]}")
                _in_list(ids, args[0], static, f"{what}: race_spells")
                choices.append(
                    {
                        "kind": "spells",
                        "list": args[0],
                        "n": int(args[1]),
                        "exact": True,
                        "ids": ids,
                        "ability": _row_ability(args, 4),
                    }
                )
            elif sel == "SelectSkills":
                ids = _names(kit.get("race_skills", []), SKILLS, f"{what}: race_skills")
                if len(ids) != int(args[1]):
                    raise SpecError(f"{what}: race_skills has {len(ids)}, the race {name}'s selector takes {args[1]}")
                choices.append({"kind": "skills", "list": args[0], "n": int(args[1]), "exact": True, "ids": ids})
            else:
                raise SpecError(f"{what}: race {name} has {sel}({args[0]}), a choice a kit can't make")
    for k, kind in (("race_skills", "skills"), ("race_spells", "spells")):
        if kit.get(k) and not any(x["kind"] == kind for x in choices):
            raise SpecError(f"{what}: race {name} has no {kind[:-1]} choice ({k} must be left out)")
    fixed = [(m.group(1), f"race {name}") for b in boosts if (m := re.fullmatch(r"ProficiencyBonus\(Skill,(\w+)\)", b))]
    part: dict[str, Any] = {"race": race["race"], "boosts": ";".join(boosts), "passives": passives}
    if spells:
        part["spells"] = spells
    if abilities:
        part["spell_abilities"] = abilities
    return race["race"], part, choices, fixed


def tier_items(cc: list[dict[str, Any]], level: int, static: dict[str, Any]) -> list[dict[str, Any]]:
    """The items a kit of this level must hold, as a multiset of {item, type, root}: the class's character-creation
    equipment less EXCLUDED, each item as its +N entry (`<item>_N`, N = ENCHANT[level]) where the game has one, then
    EXTRA_POTIONS[level] (ops/logs/arena-4v4-builds.md §2: the same items at every tier, the enchantment grows)."""
    cat = static.get("items", {})
    n = ENCHANT.get(level, 0)
    out = []
    for e in cc:
        if e["item"] in EXCLUDED:
            continue
        up = f"{e['item']}_{n}"
        if n and up in cat:
            out.append({"item": up, "type": cat[up]["type"], "root": cat[up]["root"]})
        else:
            out.append(e)
    for name in EXTRA_POTIONS.get(level, ()):
        out.append({"item": name, "type": cat[name]["type"], "root": cat[name]["root"]})
    return out


def _items(
    items: Any, cc: list[dict[str, Any]], cls: str, what: str, level: int = 2, static: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise SpecError(f"{what}: items must list the class's items")
    allowed = (
        tier_items(cc, level, static or {})
        if ENCHANT.get(level) or EXTRA_POTIONS.get(level)
        else [e for e in cc if e["item"] not in EXCLUDED]
    )
    want = sorted(e["item"] for e in allowed)
    by = {e["item"]: e for e in allowed}
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
        tier = (
            f" at level {level} (+{ENCHANT[level]} entries, potions {', '.join(EXTRA_POTIONS[level])})"
            if (ENCHANT.get(level) or EXTRA_POTIONS.get(level))
            else ""
        )
        raise SpecError(
            f"{what}: items must be EQP_CC_{cls} less {', '.join(EXCLUDED)}{tier}"
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


def _builds() -> Any:
    """tools/dev/arena_builds.py. It imports this module by name: run as a script, this module is __main__, so it is
    registered under its name first (one SpecError class, not two copies)."""
    sys.modules.setdefault("make_arena_save", sys.modules[__name__])
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.append(here)
    import arena_builds

    return arena_builds


def _spawn_arg(s: str) -> list[float]:
    try:
        v = [float(x) for x in s.split(",")]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"want x,y,z, got {s!r}") from e
    if len(v) != 3:
        raise argparse.ArgumentTypeError(f"want x,y,z, got {s!r}")
    return v


def _schema_of(path: Path) -> Any:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SpecError(f"cannot read {path}: {e}") from e
    return doc.get("schema") if isinstance(doc, dict) else None


def _pair_arg(s: str) -> tuple[str, str]:
    a, sep, b = s.partition(":")
    if not sep or not a or not b:
        raise argparse.ArgumentTypeError(f"want <uuidA>:<uuidB>, got {s!r}")
    return a, b


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mirror-team arenas: spec check, the arena's mirror section, symmetry.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check a pairs spec (templates may still be null) or an arena-builds.v1 file")
    v.add_argument("spec", type=Path)
    v.add_argument("--rated", action="store_true", help="a build file must be rated (no draft, every reference read)")
    bm = sub.add_parser("builds-mirror", help="print the arena file's level, teams and mirror section for a build file")
    bm.add_argument("builds", type=Path)
    bm.add_argument("names", nargs="+", type=_pair_arg, help="each pair's twins' names, A:B, in the builds' order")
    m = sub.add_parser("mirror", help="print the arena file's level, teams and mirror section for a pairs spec")
    m.add_argument("spec", type=Path)
    ba = sub.add_parser("builds-arena", help="write an arena file: a base arena's save and caps, a build file's pairs")
    ba.add_argument("builds", type=Path)
    ba.add_argument("base", type=Path, help="the arena file whose save, seed and caps it takes")
    ba.add_argument("name", help="the new arena's name (its file's stem)")
    ba.add_argument("names", nargs="+", type=_pair_arg, help="each pair's twins' names, A:B, in the builds' order")
    ba.add_argument("--spawn-a", required=True, type=_spawn_arg, help="x,y,z")
    ba.add_argument("--spawn-b", required=True, type=_spawn_arg, help="x,y,z")
    ba.add_argument("--line-gap", type=float, help=f"members' gap along a line, m (default {LINE_GAP_M})")
    ba.add_argument("--round-cap", type=int, help="else the base's")
    ba.add_argument("--wall-cap-min", type=float, help="else the base's")
    ba.add_argument("--out", required=True, type=Path)
    y = sub.add_parser("symmetry", help="ask the running game whether each pair is a mirror")
    y.add_argument("pairs", nargs="+", type=_pair_arg)
    rr = sub.add_parser("reference-read", help="read a hireling's profile from the running game into the build file")
    rr.add_argument("builds", type=Path)
    rr.add_argument("build")
    rr.add_argument("uuid")
    rr.add_argument("--evidence", required=True, help="where the read is logged (live.md section, files)")
    rd = sub.add_parser(
        "reference-diff", help="a twin in the running game against its build's reference; exit 1 on a diff"
    )
    rd.add_argument("builds", type=Path)
    rd.add_argument("build")
    rd.add_argument("uuid")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "builds-mirror":
            arena_builds = _builds()
            print(json.dumps(arena_builds.mirror_section(arena_builds.load_builds(args.builds), args.names), indent=1))
            return 0
        if args.cmd == "builds-arena":
            arena_builds = _builds()
            base = json.loads(args.base.read_text())
            a = arena_builds.arena_file(
                arena_builds.load_builds(args.builds),
                base,
                args.name,
                args.names,
                args.spawn_a,
                args.spawn_b,
                line_gap=args.line_gap,
            )
            if args.round_cap is not None:
                a["round_cap"] = args.round_cap
            if args.wall_cap_min is not None:
                a["wall_cap_min"] = args.wall_cap_min
            args.out.write_text(json.dumps(a, indent=1) + "\n")
            print(f"{args.out}: {args.name}, {len(a['mirror']['pairs'])} pairs, level {a['level']}")
            return 0
        if args.cmd == "validate" and _schema_of(args.spec) == "arena-builds.v1":
            arena_builds = _builds()
            doc = arena_builds.load_builds(args.spec)
            if args.rated and "draft" in doc:
                raise SpecError(f"{doc['name']} is a draft: {doc['draft']}")
            print(f"{doc['name']}: ok" + (" (draft)" if "draft" in doc else " (rated)"))
            return 0
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

        def rpc(op: str, a: dict[str, Any]) -> Any:
            return core.game.rpc(op, a)

        try:
            if args.cmd in ("reference-read", "reference-diff"):
                arena_builds = _builds()
                if args.cmd == "reference-read":
                    print(arena_builds.reference_read(rpc, args.builds, args.build, args.uuid, args.evidence))
                    return 0
                d = arena_builds.reference_diff(rpc, arena_builds.load_builds(args.builds), args.build, args.uuid)
                print(json.dumps(d, indent=1))
                return 0 if d.get("ok") else 1
            r = check_symmetry(rpc, args.pairs)
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
