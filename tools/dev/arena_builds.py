"""Arena build files (schema arena-builds.v1, docs BUILDS.md step 4, plan option A): one tier's team of builds, each the
choices a character of that class makes, checked against the game's data (tools/dev/arena_static.json), and the
reference profile a build is verified against.

    uv run --project bridge python tools/dev/make_arena_save.py validate packages/bench/builds/<name>.json

A build (A1, orchestrator D411: its race and background are its class's hireling's, so a respecced hireling of that
class can be its reference):

    {"id": "fighter", "class": "Fighter", "race": "HalfElf_Wood", "background": "Soldier", "role": "frontline",
     "script": {"cc": {"abilities": {...6}, "ability_bonus": [+2, +1], "skills": [...], "expertise": [...],
                       "race_skills": [...], "passives": [[...] per SelectPassives],
                       "spells": [[...] per SelectSpells]},
                "level_ups": [{"level": 2, "passives": [...], "spells": [...]}]},
     "gear": ["WPN_Longsword", {"item": "WPN_Handaxe", "equip": false}, ...],
     "reference": null | {"profile": {...}, "game_build": "25605617", "hireling": "<origin uuid>", "evidence": "...",
                          "accepted"?: [{"field": ..., "only_twin": [...], "only_reference": [...], "why": ...}]}}

The script is what the respec page is driven through (character creation's choices, then one entry per level-up up to
the file's level, each holding only that level's selectors) and also what the per-match copies' kit is made from
(route S, as live 15): one source, so the reference and the copies can't be given different choices. `abilities` is
the point-buy and must equal our stat entry's (Shared/ArenaStats.lua: the copies spawn with it). `role` is a
description for people, never prompt text. A file is rated only when it has no `draft` and every build has a reference
read on the file's game build; a draft may lack references (plan step 3 reads them). `accepted` names the entries of a
Symmetry field a build's twins may differ by from the reference, each with why (arena.setup fails on any other entry).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import make_arena_save as mas

SCHEMA = "arena-builds.v1"
BUILD_KEYS = {"id", "class", "subclass", "race", "background", "role", "script", "gear", "reference"}
CC_KEYS = {"abilities", "ability_bonus", "skills", "expertise", "race_skills", "race_spells", "passives", "spells"}
UP_KEYS = {"level", "passives", "spells", "skills", "expertise", "improvement"}
KINDS = ("passives", "spells", "skills", "expertise")
REFERENCE_KEYS = {"profile", "game_build", "hireling", "evidence"}
REFERENCE_OPTIONAL = {"accepted"}
ACCEPTED_KEYS = {"field", "only_twin", "only_reference", "why"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
STATS_LUA = mas.REPO / "mod/Mods/BG3AI/ScriptExtender/Lua/Shared/ArenaStats.lua"
# Server/ArenaSymmetry.lua Sym.FIELDS: what a reference holds and arena.setup compares
REFERENCE_FIELDS = (
    "race",
    "level",
    "classes",
    "abilities",
    "max_hp",
    "ac",
    "equipment",
    "inventory",
    "spells",
    "prepared",
    "passives",
    "statuses",
    "resources",
    "proficiencies",
    "skills",
    "boosts",
    "spellcast",
    "derived",
    "resource_max",
    "passive_state",
)


class BuildError(mas.SpecError):
    pass


def stat_abilities(path: Path = STATS_LUA) -> dict[str, dict[str, int]]:
    """Our stat entries' point-buy per class, read from Shared/ArenaStats.lua (AS.CLASSES)."""
    text = path.read_text()
    out = {}
    for m in re.finditer(r"(\w+) = \{ table = \"[^\"]+\", casting = \"\w+\",\s*abilities = \{ ([\d, ]+) \} \}", text):
        out[m.group(1)] = dict(zip(mas.ABILITIES, (int(x) for x in m.group(2).split(",")), strict=True))
    if not out:
        raise BuildError(f"no class abilities read from {path}")
    return out


def _per_level(cls: str, subclass: str | None, level: int, static: dict[str, Any], what: str) -> dict[int, dict]:
    """Per level: how many selectors of each kind the class's and subclass's rows have, and whether the level is an
    improvement (AllowImprovement: an ASI or a feat)."""
    out = {lv: {**dict.fromkeys(KINDS, 0), "improvement": False} for lv in range(1, level + 1)}
    for r in mas.selector_rows(cls, subclass, level, static, what):
        for n, _ in mas.selectors(r["row"]["Selectors"]):
            if n in mas.CHOICE:
                out[r["level"]][mas.CHOICE[n]] += 1
        if r["source"] == "class" and r["row"].get("AllowImprovement"):
            out[r["level"]]["improvement"] = True
    return out


def _lists(v: Any, n: int, what: str) -> list[list[str]]:
    if not isinstance(v, list) or len(v) != n or not all(isinstance(x, list) for x in v):
        raise BuildError(f"{what} must have {n} lists (one per selector at that level)")
    return v


def kit_of(build: dict[str, Any], level: int, static: dict[str, Any]) -> dict[str, Any]:
    """The arena-mirror kit (make_arena_save.kit_plan's input) the script and gear make; BuildError on a script that
    doesn't fit the class's (and subclass's) rows level by level. Per level-up: one list per selector of each kind
    (passives, spells, skills, expertise) at that level, and `improvement` exactly at the improvement levels."""
    what = f"build {build.get('id')}"
    script = build.get("script")
    if not isinstance(script, dict) or set(script) - {"cc", "level_ups", "prepared"} or "level_ups" not in script:
        raise BuildError(f"{what}: script must be {{cc, level_ups, prepared?}}")
    cc, ups = script["cc"], script["level_ups"]
    if not isinstance(cc, dict) or set(cc) - CC_KEYS or "abilities" not in cc:
        raise BuildError(f"{what}: script.cc must have abilities and only {', '.join(sorted(CC_KEYS))}")
    counts = _per_level(build["class"], build.get("subclass"), level, static, what)
    if (
        not isinstance(ups, list)
        or not all(isinstance(u, dict) for u in ups)
        or [u.get("level") for u in ups] != list(range(2, level + 1))
    ):
        raise BuildError(f"{what}: script.level_ups must list levels 2..{level} in order")
    kit: dict[str, Any] = {k: cc[k] for k in ("ability_bonus", "race_skills", "race_spells") if k in cc}
    for kind in ("passives", "spells"):
        kit[kind] = list(_lists(cc.get(kind, []), counts[1][kind], f"{what}: script.cc.{kind}"))
    for kind in ("skills", "expertise"):
        kit[kind] = [cc[kind]] if counts[1][kind] and kind in cc else []
        if kind in cc and not counts[1][kind]:
            raise BuildError(f"{what}: script.cc.{kind}: {build['class']} has no {kind} choice at level 1")
    kit["improvements"] = []
    for u in ups:
        lv = u["level"]
        if extra := sorted(set(u) - UP_KEYS):
            raise BuildError(f"{what}: level-up {lv} has choices a kit can't make ({', '.join(extra)})")
        for kind in KINDS:
            kit[kind] += _lists(u.get(kind, []), counts[lv][kind], f"{what}: level-up {lv} {kind}")
        if counts[lv]["improvement"] != ("improvement" in u):
            raise BuildError(
                f"{what}: level-up {lv} "
                + (
                    "is an improvement level: it needs improvement {asi} or {feat}"
                    if counts[lv]["improvement"]
                    else "has no improvement in the game's rows"
                )
            )
        if "improvement" in u:
            kit["improvements"].append(u["improvement"])
    for kind in ("skills", "expertise"):
        if not kit[kind]:
            del kit[kind]
    if not kit["improvements"]:
        del kit["improvements"]
    if "prepared" in script:
        kit["prepared"] = script["prepared"]
    if build.get("subclass") is not None:
        kit["subclass"] = build["subclass"]
    kit.update(race=build["race"], background=build["background"], items=build["gear"])
    return kit


def check_build(build: Any, level: int, static: dict[str, Any], abilities: dict[str, dict[str, int]]) -> dict:
    if not isinstance(build, dict) or set(build) != BUILD_KEYS:
        raise BuildError(f"a build must be {{{', '.join(sorted(BUILD_KEYS))}}}: {build!r:.80}")
    what = f"build {build['id']}"
    if not isinstance(build["id"], str) or not ID_RE.match(build["id"]):
        raise BuildError(f"build id {build['id']!r} must be lower-case letters, digits and _")
    cls = build["class"]
    if cls not in mas.CLASSES:
        raise BuildError(f"{what}: class must be one of {', '.join(mas.CLASSES)}")
    h = static["hirelings"][cls]
    for k in ("race", "background"):
        if build[k] != h[k]:
            raise BuildError(
                f"{what}: {k} is {build[k]}, the {cls} hireling's is {h[k]} (A1: the reference is a hireling)"
            )
    if not isinstance(build["role"], str) or not build["role"]:
        raise BuildError(f"{what}: role must describe the build's part in the team")
    if build["subclass"] is not None and not isinstance(build["subclass"], str):
        raise BuildError(f"{what}: subclass must be a subclass name or null")
    kit = kit_of(build, level, static)
    if build["script"]["cc"]["abilities"] != abilities.get(cls):
        raise BuildError(f"{what}: script.cc.abilities must be our {cls} stat entry's point-buy {abilities.get(cls)}")
    plan = mas.kit_plan(kit, cls, level, static, what, base=abilities[cls])
    ref = build["reference"]
    if ref is not None and (
        not isinstance(ref, dict)
        or not REFERENCE_KEYS <= set(ref) <= REFERENCE_KEYS | REFERENCE_OPTIONAL
        or not isinstance(ref["profile"], dict)
        or ref["hireling"] != h["origin"]
    ):
        raise BuildError(
            f"{what}: reference must be null or {{{', '.join(sorted(REFERENCE_KEYS))}, accepted?}} "
            f"of the {cls} hireling"
        )
    if ref is not None:
        _check_accepted(ref.get("accepted", []), what)
    return plan


def _check_accepted(accepted: Any, what: str) -> None:
    """Each accepted difference names one field once, the entries on each side, and why."""
    seen: set[str] = set()
    if not isinstance(accepted, list):
        raise BuildError(f"{what}: reference.accepted must list differences")
    for a in accepted:
        if (
            not isinstance(a, dict)
            or set(a) != ACCEPTED_KEYS
            or not isinstance(a["only_twin"], list)
            or not isinstance(a["only_reference"], list)
            or not (a["only_twin"] or a["only_reference"])
            or not (isinstance(a["why"], str) and a["why"].strip())
        ):
            raise BuildError(
                f"{what}: an accepted difference must be {{{', '.join(sorted(ACCEPTED_KEYS))}}}: {a!r:.120}"
            )
        if a["field"] in seen:
            raise BuildError(f"{what}: reference.accepted names {a['field']} twice")
        seen.add(a["field"])


def check_builds(doc: Any, static: dict[str, Any] | None = None) -> dict[str, Any]:
    """The build file, checked; BuildError names the first problem. A file without `draft` is rated: every build needs
    a reference read on the file's game build."""
    static = static if static is not None else mas.load_static()
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise BuildError(f"schema must be {SCHEMA!r}")
    known = {"schema", "name", "title", "tier", "level", "game_build", "draft", "builds"}
    if extra := sorted(set(doc) - known):
        raise BuildError(f"unknown keys: {', '.join(extra)}")
    if not isinstance(doc.get("name"), str) or not mas.NAME_RE.match(doc["name"]):
        raise BuildError("name must be lower-case letters, digits and _ (1..36, a letter first)")
    if not isinstance(doc.get("title"), str) or not doc["title"]:
        raise BuildError("title must be a non-empty string")
    lv = doc.get("level")
    if lv not in mas.TIERS or doc.get("tier") != f"L{lv}":
        raise BuildError(f"level must be one of {mas.TIERS} and tier L<level>")
    if not isinstance(doc.get("game_build"), str) or not doc["game_build"].isdigit():
        raise BuildError("game_build must be the game's build number (digits)")
    if "draft" in doc and not (isinstance(doc["draft"], str) and doc["draft"].strip()):
        raise BuildError("draft must say why the builds can't be rated yet")
    builds = doc.get("builds")
    if not isinstance(builds, list) or len(builds) != mas.PAIRS:
        raise BuildError(f"builds must list {mas.PAIRS} builds (one team)")
    abilities, ids = stat_abilities(), set()
    for b in builds:
        check_build(b, lv, static, abilities)
        if b["id"] in ids:
            raise BuildError(f"build id {b['id']} is used twice")
        ids.add(b["id"])
    if "draft" not in doc:
        for b in builds:
            ref = b["reference"]
            if ref is None:
                raise BuildError(f"build {b['id']}: no reference profile (only a draft may lack one)")
            if ref["game_build"] != doc["game_build"]:
                raise BuildError(
                    f"build {b['id']}: its reference was read on game build {ref['game_build']}, "
                    f"the file's is {doc['game_build']}"
                )
    return doc


def load_builds(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BuildError(f"cannot read {path}: {e}") from e
    return check_builds(doc)


def party_xp(level: int, static: dict[str, Any]) -> dict[str, int]:
    """arena.setup's XP window for a tier: {total, below} from the game's XP table (no below at its last level)."""
    xp = static["xp"]
    out = {"total": xp[str(level)]}
    if str(level + 1) in xp:
        out["below"] = xp[str(level + 1)]
    return out


def mirror_section(doc: dict[str, Any], names: list[tuple[str, str]], static: dict[str, Any] | None = None) -> dict:
    """The arena file's level, teams and mirror section for a checked build file: pair i is build i, its twins named
    names[i] (A, B), spawned from the build's class's hireling template (its race) with the build's kit and, once read,
    its reference (profile and accepted differences; arena.setup compares). The mirror's party_xp is the tier's."""
    static = static if static is not None else mas.load_static()
    if len(names) != len(doc["builds"]):
        raise BuildError(f"names must give {len(doc['builds'])} pairs")
    pairs = []
    for b, (a, bn) in zip(doc["builds"], names, strict=True):
        if not static["hirelings"][b["class"]].get("template"):
            raise BuildError(
                f"build {b['id']}: the {b['class']} hireling's root template is not read yet (arena_static.json "
                "hirelings): no arena can spawn its pairs"
            )
        plan = mas.kit_plan(
            kit_of(b, doc["level"], static),
            b["class"],
            doc["level"],
            static,
            f"build {b['id']}",
            base=stat_abilities()[b["class"]],
        )
        pair = {
            "class": b["class"],
            "build": b["id"],
            "template": static["hirelings"][b["class"]]["template"],
            "race": plan["race"],
            "stats": f"BG3AI_Arena_{b['class']}_L1",
            "names": {"A": a, "B": bn},
            "kit": plan["kit"],
        }
        if b["reference"] is not None:
            pair["reference"] = {"profile": b["reference"]["profile"], "accepted": b["reference"].get("accepted", [])}
        pairs.append(pair)
    seen = [n.lower() for p in pairs for n in p["names"].values()]
    if len(set(seen)) != len(seen) or not all(mas.MEMBER_RE.match(n) for p in pairs for n in p["names"].values()):
        raise BuildError("names must be display names, unique across both teams")
    teams = {s: [{"name": p["names"][s]} for p in pairs] for s in mas.SIDES}
    return {
        "level": doc["level"],
        "teams": teams,
        "mirror": {"party_xp": party_xp(doc["level"], static), "pairs": pairs},
    }


def arena_file(
    doc: dict[str, Any],
    base: dict[str, Any],
    name: str,
    names: list[tuple[str, str]],
    spawn_a: list[float],
    spawn_b: list[float],
    static: dict[str, Any] | None = None,
    line_gap: float | None = None,
) -> dict[str, Any]:
    """An arena file for a checked build file: `base`'s save, seed, caps and rule keys, the builds' level, teams and
    mirror, these spawns on the base's game level, line_gap when it isn't the default. A draft build file makes a draft
    arena (it can't be played)."""
    keep = ("schema", "save", "seed", "karmic", "round_cap", "wall_cap_min", "turn_cap_s", "condition", "tools_off")
    a = {k: base[k] for k in keep if k in base}
    a["name"], a["title"] = name, doc["title"]
    a["about"] = (
        f"The build file {doc['name']} (tools/dev/make_arena_save.py builds-arena), each pair's twins made per match, "
        f"on {base['name']}'s save."
    )
    if "draft" in doc:
        a["draft"] = doc["draft"]
    a["location"] = {"level": base["location"]["level"], "spawns": {"A": spawn_a, "B": spawn_b}}
    if line_gap is not None:
        a["location"]["line_gap"] = line_gap
    mas.check_geometry(spawn_a, spawn_b, len(doc["builds"]), mas.LINE_GAP_M if line_gap is None else line_gap)
    return a | mirror_section(doc, names, static)


def _build_in(doc: dict[str, Any], build_id: str) -> dict[str, Any]:
    for b in doc["builds"]:
        if b["id"] == build_id:
            return b
    raise BuildError(f"{doc['name']} has no build {build_id}")


def reference_read(rpc: mas.Rpc, path: Path, build_id: str, uuid: str, evidence: str) -> str:
    """The character's profile from the running game (arena.profile) as the build's reference, written to the file. A
    character of another class count or level than the build is refused; a build's accepted differences stay."""
    doc = json.loads(Path(path).read_text())
    b = _build_in(doc, build_id)
    prof = rpc("arena.profile", {"uuid": uuid})
    if not isinstance(prof, dict) or prof.get("level") != doc["level"]:
        got = prof.get("level") if isinstance(prof, dict) else prof
        raise BuildError(f"{uuid} reads level {got}, the file's tier is {doc['level']}: not its reference")
    unread = [f for f in REFERENCE_FIELDS if prof.get(f) is None]
    if unread:
        raise BuildError(f"{uuid}: {', '.join(unread)} doesn't read: a reference can't hide a field")
    ref = {
        "profile": {k: prof[k] for k in REFERENCE_FIELDS},
        "game_build": doc["game_build"],
        "hireling": mas.load_static()["hirelings"][b["class"]]["origin"],
        "evidence": evidence,
    }
    if b["reference"] and b["reference"].get("accepted"):
        ref["accepted"] = b["reference"]["accepted"]
    b["reference"] = ref
    check_builds(doc)
    Path(path).write_text(json.dumps(doc, indent=1) + "\n")
    return f"{doc['name']}: {build_id}'s reference read from {uuid} ({len(json.dumps(ref))} bytes)"


def reference_diff(rpc: mas.Rpc, doc: dict[str, Any], build_id: str, uuid: str) -> dict[str, Any]:
    """arena.reference_diff of a twin against the build's reference; BuildError when the build has none yet."""
    ref = _build_in(doc, build_id)["reference"]
    if ref is None:
        raise BuildError(f"build {build_id} has no reference yet")
    r = rpc(
        "arena.reference_diff",
        {"uuid": uuid, "reference": {"profile": ref["profile"], "accepted": ref.get("accepted", [])}},
    )
    if not isinstance(r, dict) or "ok" not in r:
        raise BuildError(f"arena.reference_diff answered {r!r}")
    return r
