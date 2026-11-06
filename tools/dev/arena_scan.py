"""What a build can reach in the game's stats, and whether any of it knocks down or changes sides. The arena's one bar
(website ops/logs/REVIEW-4v4.md, orchestrator D130, 2026-11-06): knock-downs and side-changing spells are banned;
incapacitating spells are allowed, GuardCast already refuses those. A build's ids don't show this: a Quarterstaff
unlocks Topple and a Cleric's class rows add Command, so the walk follows items, class rows, kit, race and passives
through spells, statuses and passives.

    python tools/dev/arena_scan.py graph "<BG3>/Data"   # arena_stat_graph.json from the paks (read-only, in memory)
    python tools/dev/arena_scan.py check                # every build's hits, from the checked-in graph

CI has no paks, so the graph is checked in: per stats entry, what it refers to and what it does against the bar.
test_arena_scan.py walks every build over it. An id the graph lacks fails: regenerate it.

Not seen: the common actions every copy's book holds (Shove and so on), surfaces made other than by CreateSurface, and
summons: the walk stops at a summon's spell (Find Familiar, Dancing Lights), and what the summon can do is in its
template, unread here. A live book read is the real check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arena_builds as ab
import make_arena_save as mas
import pak  # tools/pak.py

GRAPH = Path(__file__).resolve().parent / "arena_stat_graph.json"
BUILDS = Path(__file__).resolve().parents[2] / "packages/bench/builds"
PAKS = ("Shared.pak", "Gustav.pak", "GustavX.pak")
# Later wins, as the game loads them. Honour first: its entries are the mode's, the others' are what a run plays.
MODULES = ("Honour", "HonourX", "Shared", "SharedDev", "Gustav", "GustavDev", "GustavX", "PhotoMode")
FOLLOWED = {"SpellData", "StatusData", "PassiveData", "Weapon", "Armor", "Object", "InterruptData"}
# Text fields, and fields that hold conditions or looks: a name in them is no effect of the entry.
SKIP = {
    "TargetConditions", "RequirementConditions", "CycleConditions", "ConcentrationSpellID", "TooltipPermanentWarnings",
    "Icon", "DisplayName", "Description", "ExtraDescription", "ExtraDescriptionParams", "DescriptionParams",
    "TooltipDamageList", "TooltipAttackSave", "TooltipUpcastDescription", "TooltipUpcastDescriptionParams",
    "TooltipStatusApply", "AIFlags", "SpellAnimation", "HitAnimationType", "RootTemplate", "ManagedStatusEffectGroup",
    "ManagedStatusEffectType", "StillAnimationType", "StillAnimationPriority", "AiCalculationSpellOverride", "Ability",
    "Requirements", "Flags", "SpellFlags", "Using", "Conditions", "TickFunctors_Conditions", "RemoveConditions",
    "RemoveEvents", "RollConditions", "ProjectileTerrainOffset", "PersonalStatusImmunities", "StackId",
}  # fmt: skip
LOOKS = ("Effect", "Sound", "Animation")
KNOCK_SURFACE = re.compile(r"Grease|Frozen|\bIce\b")  # surfaces that make PRONE (REVIEW-12 option (d))
# What turns a twin on its own side. SG_Charmed alone doesn't: FRIENDS holds it and only helps in dialogue.
SIDE_GROUPS = ("SG_Dominated", "SG_Possessed", "SG_Mad")
SIDE_BOOST = re.compile(r"\b(FactionOverride|CannotHarmCauseEntity)\(")
NOT_DONE = re.compile(r"\b(Remove\w*Status|\w*StatusImmunity)\([^)]*\)")  # it names a status it takes away or refuses
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Entries = dict[str, dict[str, Any]]
Graph = dict[str, dict[str, list[str]]]


def parse(text: str, into: Entries) -> None:
    """A stats .txt's entries into `into`, a later file's same name replacing the entry."""
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if m := re.match(r'new entry "([^"]+)"', line):
            cur = into[m[1]] = {"data": {}}
        elif cur is None:
            continue
        elif m := re.match(r'type "([^"]+)"', line):
            cur["type"] = m[1]
        elif m := re.match(r'using "([^"]+)"', line):
            cur["using"] = m[1]
        elif m := re.match(r'data "([^"]+)" "(.*)"$', line):
            cur["data"][m[1]] = m[2]


def fields(entries: Entries, name: str, seen: tuple[str, ...] = ()) -> dict[str, str]:
    """An entry's data, `using` resolved: the parent's first, its own over it."""
    e = entries.get(name)
    if e is None or name in seen:
        return {}
    base = fields(entries, e["using"], (*seen, name)) if e.get("using") else {}
    return {**base, **e["data"]}


def _plain(value: str) -> str:
    """A field without its strings, IF(...) conditions and removals: what stays is what the entry does."""
    return NOT_DONE.sub(" ", re.sub(r"IF\((?:[^()]|\([^()]*\))*\):", " ", re.sub(r"'[^']*'", " ", value)))


def _effects(data: dict[str, str]) -> Iterable[tuple[str, str]]:
    for k, v in data.items():
        if k not in SKIP and not any(w in k for w in LOOKS):
            yield k, _plain(v)


def refs(entries: Entries, name: str) -> list[str]:
    out = set()
    for _, v in _effects(fields(entries, name)):
        for t in TOKEN.findall(v):
            if t != name and entries.get(t, {}).get("type") in FOLLOWED:
                out.add(t)
    return sorted(out)


def hits(entries: Entries, name: str) -> list[str]:
    """What the entry itself does against the bar."""
    d = fields(entries, name)
    groups = d.get("StatusGroups", "")
    out = set()
    if d.get("StatusType") == "KNOCKED_DOWN":
        out.add("knock-down: StatusType KNOCKED_DOWN")
    if "SG_Prone" in groups:
        out.add("knock-down: SG_Prone")
    out.update(f"side: {g}" for g in SIDE_GROUPS if g in groups)
    for k, v in _effects(d):
        if re.search(r"\bPRONE\b", v):
            out.add(f"knock-down: PRONE in {k}")
        if re.search(r"\bForce\(", v):
            out.add(f"knock-down: Force in {k}")
        if m := SIDE_BOOST.search(v):
            out.add(f"side: {m[1]} in {k}")
        for m in re.finditer(r"CreateSurface\(([^)]*)\)", v):
            if KNOCK_SURFACE.search(m[1]):
                out.add(f"knock-down: CreateSurface({m[1]})")
    return sorted(out)


def graph(entries: Entries) -> Graph:
    out: Graph = {}
    for name in sorted(entries):
        if entries[name].get("type") in FOLLOWED:
            r, h = refs(entries, name), hits(entries, name)
            out[name] = {**({"refs": r} if r else {}), **({"hits": h} if h else {})}
    return out


def read_stats(data_dir: Path) -> Entries:
    entries: Entries = {}
    texts: dict[str, list[tuple[str, bytes]]] = {m: [] for m in MODULES}
    want = re.compile(r"Public/([^/]+)/Stats/Generated/Data/[^/]+\.txt")
    for name in PAKS:
        p = pak.Package(data_dir / name)
        for e in p.find("Public/*"):
            m = want.fullmatch(e.name)
            if m and m[1] in texts and not e.name.endswith("/XPData.txt"):
                texts[m[1]].append((e.name, p.read(e)))
    for module in MODULES:
        for _, data in sorted(texts[module]):
            parse(data.decode("utf-8", "replace"), entries)
    return entries


def write_graph(g: Graph, path: Path = GRAPH) -> None:
    """One entry a line: a game patch's diff names the entries it changed."""
    body = ",\n".join(f"{json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}" for k, v in g.items())
    path.write_text("{\n" + body + "\n}\n")


def load_graph(path: Path = GRAPH) -> Graph:
    return json.loads(path.read_text())


def _list_spells(uuid: str, static: dict[str, Any]) -> list[str] | None:
    for table in (static.get("lists", {}), static.get("spell_lists", {})):
        if uuid in table:
            return list(table[uuid].get("ids") or table[uuid].get("spells") or [])
    return None


def row_seeds(rows: list[dict[str, Any]], static: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """What class and subclass rows give whoever holds them, id -> where from; the lists nothing here can read."""
    seeds: dict[str, str] = {}
    unread = []
    for r in rows:
        row, where = r["row"], f"{r['source']} L{r['level']}"
        for x in (row.get("PassivesAdded") or "").split(";"):
            if x:
                seeds[x] = f"{where} PassivesAdded"
        for m in re.finditer(r"UnlockSpell\(([^,)]+)", row.get("Boosts") or ""):
            seeds[m[1]] = f"{where} Boosts"
        for m in re.finditer(r"AddSpells\(([0-9a-f-]+)", row.get("Selectors") or ""):
            spells = _list_spells(m[1], static)
            if spells is None:
                unread.append(f"{where} AddSpells {m[1]}")
            for x in spells or []:
                seeds[x] = f"{where} AddSpells {m[1][:8]}"
    return seeds, unread


def kit_seeds(kit: dict[str, Any]) -> dict[str, str]:
    """What a kit picks and carries, id -> where from."""
    seeds: dict[str, str] = {}
    for c in kit.get("choices", []):
        if c["kind"] in ("spells", "passives"):
            seeds.update(dict.fromkeys(c["ids"], f"kit {c['kind']}"))
    grants = kit.get("grants") or {}
    seeds.update(dict.fromkeys(grants.get("passives", []) + grants.get("spells", []), "kit grants"))
    for m in re.finditer(r"UnlockSpell\(([^,)]+)", grants.get("boosts") or ""):
        seeds[m[1]] = "kit grants boost"
    race = kit.get("race") or {}
    seeds.update(dict.fromkeys(race.get("passives", []) + race.get("spells", []), "race"))
    seeds.update(dict.fromkeys((kit.get("player") or {}).get("passives", []), "player"))
    seeds.update({it["item"]: "item" for it in kit.get("items", [])})
    if isinstance(kit.get("prepared"), list):
        seeds.update(dict.fromkeys(kit["prepared"], "prepared"))
    return seeds


def build_seeds(build: dict[str, Any], doc: dict[str, Any], static: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    what = f"build {build['id']}"
    level, cls, sub = doc["level"], build["class"], build.get("subclass")
    kit = ab.kit_of(build, level, static)
    plan = mas.kit_plan(kit, cls, level, static, what, base=ab.stat_abilities()[cls])
    seeds, unread = row_seeds(mas.selector_rows(cls, sub, level, static, what), static)
    return {**seeds, **kit_seeds(plan["kit"])}, unread


def walk(g: Graph, seeds: Iterable[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Every hit reached: 'entry: hit' -> the path from its seed; and the ids the graph doesn't hold."""
    found: dict[str, list[str]] = {}
    missing = []
    seen = set()
    todo: list[tuple[str, tuple[str, ...]]] = [(s, (s,)) for s in seeds]
    while todo:
        name, path = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in g:
            missing.append(name)
            continue
        for h in g[name].get("hits", []):
            found.setdefault(f"{name}: {h}", list(path))
        todo.extend((r, (*path, r)) for r in g[name].get("refs", []))
    return found, sorted(missing)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The arena's knock-down and side-change scan.")
    sub = ap.add_subparsers(dest="step", required=True)
    gen = sub.add_parser("graph", help="regenerate arena_stat_graph.json from the paks")
    gen.add_argument("data", type=Path, help="the game's Data folder")
    gen.add_argument("--out", type=Path, default=GRAPH)
    sub.add_parser("check", help="print every build's hits")
    a = ap.parse_args(argv)
    if a.step == "graph":
        g = graph(read_stats(a.data))
        write_graph(g, a.out)
        print(f"{a.out}: {len(g)} entries, {sum('hits' in v for v in g.values())} with a hit")
        return 0
    g, static, bad = load_graph(), mas.load_static(), 0
    for path in sorted(BUILDS.glob("l*.json")):
        doc = json.loads(path.read_text())
        for b in doc["builds"]:
            seeds, unread = build_seeds(b, doc, static)
            found, missing = walk(g, seeds)
            bad += bool(found or missing or unread)
            for hit, way in sorted(found.items()):
                print(f"{path.name} {b['id']}: {hit} <- {seeds[way[0]]}: {' > '.join(way)}")
            for x in [*missing, *unread]:
                print(f"{path.name} {b['id']}: not read: {x}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
