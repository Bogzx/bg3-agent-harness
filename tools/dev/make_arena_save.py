"""Mirror-team arena save (SPEC v2.2 §5): the pairs spec and the symmetry check. The spawn/level/equip steps come
after the live probe (hireling templates, the level call) and are not here yet.

    uv run --project bridge python tools/dev/make_arena_save.py validate <spec.json>
    uv run --project bridge python tools/dev/make_arena_save.py symmetry <uuidA>:<uuidB> [...]

The spec (schema arena-mirror.v1) names 4 pairs; each pair is two copies of one hireling root template, one per team,
same class, level, gear (and later the same level-up choices):

    {"schema": "arena-mirror.v1", "name": "mirror_crash_site", "level": 4,
     "location": {"level": "WLD_Main_A", "spawns": {"A": [x, y, z], "B": [x, y, z]}},
     "pairs": [{"class": "Fighter", "template": "<root template>" | null, "names": {"A": "Ash", "B": "Birch"},
                "items": ["<item template>", ...]}, ... x4]}

`template` may be null until the probe has listed the hireling templates (validate accepts it; building will not).
Pair i is teams.A[i] and teams.B[i] in the arena file. `symmetry` asks the game's arena.symmetry (the game runs with
BG3AI_ARENA=1) and exits 1 on any problem: a field that differs within a pair, a field that doesn't read, or one
display name for two members.
"""

from __future__ import annotations

import sys

# Run as a script, this folder is sys.path[0]: keep its modules from shadowing the stdlib's.
if sys.path and sys.path[0] and __file__.startswith(sys.path[0]) and __name__ == "__main__":
    del sys.path[0]

import argparse
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "arena-mirror.v1"
SIDES = ("A", "B")
PAIRS = 4
CLASSES = ("Fighter", "Barbarian", "Rogue", "Sorcerer")  # no summons or shapeshifts at level 4 (orchestrator D250)
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


def check_spec(spec: Any, need_templates: bool = False) -> dict[str, Any]:
    """The spec, checked; SpecError names the first problem."""
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a JSON object")
    if spec.get("schema") != SCHEMA:
        raise SpecError(f"schema must be {SCHEMA!r}")
    if not isinstance(spec.get("name"), str) or not NAME_RE.match(spec["name"]):
        raise SpecError("name must be lower-case letters, digits and _ (1..36, a letter first)")
    lv = spec.get("level")
    if not isinstance(lv, int) or isinstance(lv, bool) or not 1 <= lv <= 12:
        raise SpecError("level must be an integer 1..12")
    loc = spec.get("location")
    if not isinstance(loc, dict) or not isinstance(loc.get("level"), str) or not loc["level"]:
        raise SpecError("location.level must name the game level")
    spawns = loc.get("spawns")
    if not isinstance(spawns, dict):
        raise SpecError("location.spawns must have A and B")
    for s in SIDES:
        _vec3(spawns.get(s), f"location.spawns.{s}")
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
        items = p.get("items", [])
        if not isinstance(items, list) or not all(isinstance(x, str) and x for x in items):
            raise SpecError(f"pair {i}: items must list item template names")
    return spec


def load_spec(path: Path, need_templates: bool = False) -> dict[str, Any]:
    try:
        spec = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SpecError(f"cannot read {path}: {e}") from e
    return check_spec(spec, need_templates)


def arena_teams(spec: dict[str, Any], uuids: dict[str, list[str]]) -> dict[str, list[dict[str, str]]]:
    """The arena file's teams ({name, uuid} per member, pair i at index i on both sides) once the copies exist."""
    out: dict[str, list[dict[str, str]]] = {}
    for s in SIDES:
        if len(uuids.get(s) or []) != PAIRS:
            raise SpecError(f"team {s} needs {PAIRS} uuids")
        out[s] = [{"name": p["names"][s], "uuid": uuids[s][i]} for i, p in enumerate(spec["pairs"])]
    return out


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
    ap = argparse.ArgumentParser(description="Mirror-team arena save: spec check and the symmetry check.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check a pairs spec (templates may still be null)")
    v.add_argument("spec", type=Path)
    y = sub.add_parser("symmetry", help="ask the running game whether each pair is a mirror")
    y.add_argument("pairs", nargs="+", type=_pair_arg)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            spec = load_spec(args.spec)
            todo = sum(1 for p in spec["pairs"] if p.get("template") is None)
            print(f"{spec['name']}: ok" + (f" ({todo} templates still null)" if todo else ""))
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
