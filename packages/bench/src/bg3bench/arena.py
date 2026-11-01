# pyright: basic
"""Arenas: two models fight each other in one turn-based fight. Arena files, the match grader and the ratings.

An arena file (`arenas/<name>.json`, schema/arena.v1.schema.json; `load`, `validate`) fixes one fight: the start save
(name + sha1), the level and the two spawn points, each side's team (character names, optional templates), the dice
seed, the round cap (default 20), the wall-clock cap in minutes (default 30), the condition and the name of the
bridge's tools-off list (TOOLS_OFF_LISTS). `draft` says why an arena can't be played yet (its spots are
placeholders); the pairing planner (arena_matrix) gives a draft arena no matches. Arena files are not suite files:
nothing here moves a bench-vX.Y score.

A **match** is one fight: model X plays side A, model Y side B. A **pairing** is two matches of one arena, seed and
teams with the sides swapped, so the side advantage cancels.

`grade(records, arena, record=None)` grades a match from its facts.jsonl: the referee's events (schema/
arena-facts.v1.schema.json) `arena_start {arena, seed, teams, sides}`, `arena_turn {side, character, round}` and
`arena_end {winner, rounds, hp_left_A, hp_left_B, reason}`. A side wins when every character of the other side is
down or dead (reason `wipe`); at the round cap or the wall cap the side with the higher remaining HP fraction wins and
an exact tie is a draw. A match is invalid, rerun and never rated when its run record is (results.validity: runner
error, harness failure, operator, off route, unseeded), when the harness ended it (`abort`, `fault`), or when the facts
don't match the arena or contradict themselves (INVALID).

`results(matches, seed)` rates the models over complete pairings only (both matches valid; a pairing short of one
is counted as incomplete and left out, since its side advantage doesn't cancel): per model pair win/draw/loss, per
model a Bradley-Terry strength (draw = half a win; one virtual draw against a fixed reference player keeps an
undefeated model finite) with a percentile bootstrap interval over resampled pairings (seeded), shown on the Elo
scale (400 log10 strength, centred on 1500 over the rated models). A match a model plays against itself is graded but
never rated.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeGuard

from bg3bench import conditions, scenario, stats
from bg3bench.results import validity

SCHEMA = "arena.v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # packages/bench
ARENA_DIR = PACKAGE_ROOT / "arenas"
SIDES = ("A", "B")
TEAM_MAX = 4
ROUND_CAP, WALL_CAP_MIN = 20, 30
TOOLS_OFF_LISTS = ("arena-v1",)  # the bridge's named arena tools-off lists (BG3AI_TOOLS_OFF values)
KEYS = {
    "schema", "name", "title", "about", "draft", "save", "location", "teams", "seed", "round_cap", "wall_cap_min",
    "condition", "tools_off",
}  # fmt: skip
SHA1_LEN = 40
END_REASONS = ("wipe", "round_cap", "wall_cap", "abort", "fault")
CAP_REASONS = ("round_cap", "wall_cap")
FAULT_REASONS = ("abort", "fault")  # the harness ended the match: invalid, rerun
INVALID = ("no_start", "wrong_arena", "unseeded", "off_side_turn", "no_end", "past_round_cap", "inconsistent_end")
ELO_BASE, ELO_SCALE = 1500.0, 400.0


class ArenaError(ValueError):
    pass


def _num(v: Any) -> TypeGuard[float]:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _int(v: Any) -> TypeGuard[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _check_save(save: Any, draft: bool) -> list[str]:
    if not isinstance(save, dict) or set(save) - {"name", "sha1"}:
        return ["save must be {name, sha1}"]
    name, sha1 = save.get("name"), save.get("sha1")
    errs = []
    if (name is None or sha1 is None) and not draft:
        errs.append("save.name and save.sha1 are required unless the arena is a draft")
    if name is not None and scenario.save_owner(name) is None:
        errs.append(f"save.name {name!r} is not a save folder name (<owner>-<id>__<label>)")
    if sha1 is not None and not (
        isinstance(sha1, str) and len(sha1) == SHA1_LEN and all(c in "0123456789abcdef" for c in sha1)
    ):
        errs.append("save.sha1 must be 40 lowercase hex digits")
    return errs


def _check_location(loc: Any) -> list[str]:
    if not isinstance(loc, dict) or set(loc) != {"level", "spawns"}:
        return ["location must be {level, spawns}"]
    errs = [] if _str(loc["level"]) else ["location.level must be the level name"]
    spawns = loc["spawns"]
    if not isinstance(spawns, dict) or set(spawns) != set(SIDES):
        return [*errs, "location.spawns must give A and B a point"]
    for side in SIDES:
        p = spawns[side]
        if not (isinstance(p, list) and len(p) == 3 and all(_num(x) for x in p)):
            errs.append(f"location.spawns.{side} must be [x, y, z] in metres")
    if not errs and spawns["A"] == spawns["B"]:
        errs.append("location.spawns: A and B start on the same point")
    return errs


def _check_teams(teams: Any) -> list[str]:
    if not isinstance(teams, dict) or set(teams) != set(SIDES):
        return ["teams must list side A's and side B's characters"]
    errs, seen = [], set()
    for side in SIDES:
        team = teams[side]
        if not isinstance(team, list) or not 1 <= len(team) <= TEAM_MAX:
            errs.append(f"teams.{side} must list 1 to {TEAM_MAX} characters")
            continue
        for m in team:
            if not isinstance(m, dict) or not _str(m.get("name")) or set(m) - {"name", "template"}:
                errs.append(f"teams.{side}: a character is {{name, template?}}: {m!r}")
                continue
            if "template" in m and not _str(m["template"]):
                errs.append(f"teams.{side}: {m['name']}'s template must be a template id")
            if m["name"] in seen:
                errs.append(f"teams: {m['name']} is listed twice")
            seen.add(m["name"])
    return errs


def validate(a: Any, name: str) -> list[str]:
    """What is wrong with an arena file named `name` (its file stem); [] = valid."""
    if not isinstance(a, dict):
        return ["an arena file is a JSON object"]
    errs = []
    if a.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    if unknown := sorted(set(a) - KEYS):
        errs.append(f"unknown keys: {', '.join(unknown)}")
    if a.get("name") != name or not scenario.NAME_RE.match(name):
        errs.append(f"name must be the file's name {name!r} (a lowercase slug)")
    for k in ("title", "about"):
        if not _str(a.get(k)):
            errs.append(f"{k} must be a non-empty string")
    if "draft" in a and not _str(a["draft"]):
        errs.append("draft must say why the arena can't be played yet")
    errs += _check_save(a.get("save"), "draft" in a)
    errs += _check_location(a.get("location"))
    errs += _check_teams(a.get("teams"))
    seed = a.get("seed")
    if not _int(seed) or not 0 <= seed <= scenario.SEED_MAX:
        errs.append(f"seed must be an integer 0..{scenario.SEED_MAX}")
    rc = a.get("round_cap", ROUND_CAP)
    if not _int(rc) or rc < 1:
        errs.append("round_cap must be an integer >= 1")
    wc = a.get("wall_cap_min", WALL_CAP_MIN)
    if not _num(wc) or wc <= 0:
        errs.append("wall_cap_min must be a number of minutes > 0")
    try:
        if conditions.parse(a.get("condition", "rules")).reloads:
            errs.append("condition: a match has no reloads")
    except (ValueError, AttributeError) as e:
        errs.append(f"condition: {e}")
    if a.get("tools_off") not in TOOLS_OFF_LISTS:
        errs.append(f"tools_off must name one of the bridge's arena lists: {', '.join(TOOLS_OFF_LISTS)}")
    return errs


def with_defaults(a: Mapping[str, Any]) -> dict[str, Any]:
    return {"round_cap": ROUND_CAP, "wall_cap_min": WALL_CAP_MIN, "condition": "rules", **a}


def load(path: str | Path) -> dict[str, Any]:
    """An arena file, validated, with its defaults filled in; ArenaError names the file and every problem."""
    path = Path(path)
    try:
        a = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ArenaError(f"{path}: {e}") from None
    if errs := validate(a, path.stem):
        raise ArenaError(f"{path}: " + "; ".join(errs))
    return with_defaults(a)


def load_named(name: str, root: Path = ARENA_DIR) -> dict[str, Any]:
    return load(root / f"{name}.json")


def team_names(arena: Mapping[str, Any]) -> dict[str, list[str]]:
    return {side: [m["name"] for m in arena["teams"][side]] for side in SIDES}


# ---------------------------------------------------------------------------------------------------- the grader


def _events(records: Iterable[dict], ev: str) -> list[dict]:
    return [r for r in records if r.get("kind") == "event" and r.get("ev") == ev]


def _cap_result(hp: Mapping[str, float]) -> str:
    return "A" if hp["A"] > hp["B"] else "B" if hp["B"] > hp["A"] else "draw"


def _facts_problem(records: list[dict], arena: Mapping[str, Any]) -> str | None:
    """The first INVALID reason the facts give, None when they are a whole, consistent match of this arena."""
    starts = _events(records, "arena_start")
    if not starts:
        return "no_start"
    st = starts[0]
    teams = {s: list((st.get("teams") or {}).get(s) or []) for s in SIDES}
    sides = st.get("sides") or {}
    if (
        len(starts) > 1
        or st.get("arena") != arena["name"]
        or teams != team_names(arena)
        or not all(_str(sides.get(s)) for s in SIDES)
    ):
        return "wrong_arena"
    if st.get("seed") != arena["seed"]:
        return "unseeded"
    ends = _events(records, "arena_end")
    end_t = ends[0]["t"] if ends else math.inf
    for r in _events(records, "arena_turn"):
        if r["t"] <= end_t and r.get("character") not in teams.get(r.get("side") or "", ()):
            return "off_side_turn"
    if not ends:
        return "no_end"
    end = ends[0]
    if end.get("reason") in FAULT_REASONS:
        return None  # the harness's own end: graded as such
    if (
        len(ends) > 1
        or end.get("reason") not in END_REASONS
        or end.get("winner", "-") not in (*SIDES, None)
        or not _int(end.get("rounds"))
        or not all(_num(end.get(f"hp_left_{s}")) and 0 <= end[f"hp_left_{s}"] <= 1 for s in SIDES)
    ):
        return "inconsistent_end"
    if end["rounds"] > arena["round_cap"]:
        return "past_round_cap"
    hp = {s: end[f"hp_left_{s}"] for s in SIDES}
    if end["reason"] == "wipe":
        loser = {"A": "B", "B": "A"}.get(end["winner"])
        if loser is None or hp[loser] != 0:
            return "inconsistent_end"
    elif (end["winner"] or "draw") != _cap_result(hp):
        return "inconsistent_end"
    return None


def grade(records: Iterable[dict], arena: Mapping[str, Any], record: Mapping[str, Any] | None = None) -> dict:
    """One match from its facts records (facts.check'd) and, when given, its run record (results.validity with the
    arena's seed): {arena, valid, invalid_reason, sides {A, B: player}, result (A, B, draw; None when invalid),
    reason, rounds, hp_left {A, B}, turns {A, B}}."""
    records = list(records)
    valid, why = validity(dict(record), arena["seed"]) if record is not None else (True, None)
    if valid:
        why = _facts_problem(records, arena)
    starts, ends = _events(records, "arena_start"), _events(records, "arena_end")
    end = ends[0] if ends else {}
    if why is None and end.get("reason") in FAULT_REASONS:
        why = end["reason"]
    sides = (starts[0].get("sides") or {}) if starts else {}
    end_t = end.get("t", math.inf)
    turns = {s: sum(1 for r in _events(records, "arena_turn") if r.get("side") == s and r["t"] <= end_t) for s in SIDES}
    ok = why is None
    return {
        "arena": arena["name"],
        "valid": ok,
        "invalid_reason": why,
        "sides": {s: sides.get(s) for s in SIDES},
        "result": (end["winner"] or "draw") if ok else None,
        "reason": end.get("reason"),
        "rounds": end.get("rounds"),
        "hp_left": {s: end.get(f"hp_left_{s}") for s in SIDES},
        "turns": turns,
    }


# ---------------------------------------------------------------------------------------------------- the ratings


def _score(m: Mapping[str, Any]) -> dict[str, float]:
    """{player: points} of a valid match: 1 for a win, 0.5 each for a draw."""
    a, b = m["sides"]["A"], m["sides"]["B"]
    r = m["result"]
    return {a: 1.0 if r == "A" else 0.5 if r == "draw" else 0.0, b: 1.0 if r == "B" else 0.5 if r == "draw" else 0.0}


def pairings(matches: Iterable[Mapping[str, Any]]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """(complete, counts): complete = pairing id -> its two valid matches (the sides swapped); counts = matches,
    invalid, self (a player against itself), incomplete (pairings short of a valid match). A pairing with two valid
    matches that don't swap the same two players raises."""
    by: dict[str, list[dict]] = {}
    counts = {"matches": 0, "invalid": 0, "self": 0, "incomplete": 0}
    for m in matches:
        counts["matches"] += 1
        if not m["valid"]:
            counts["invalid"] += 1
        elif m["sides"]["A"] == m["sides"]["B"]:
            counts["self"] += 1
        else:
            by.setdefault(m["pairing"], []).append(dict(m))
    complete = {}
    for pid, ms in sorted(by.items()):
        if len(ms) == 1:
            counts["incomplete"] += 1
            continue
        a, b = ms[0]["sides"], ms[-1]["sides"]
        if len(ms) != 2 or a["A"] != b["B"] or a["B"] != b["A"] or ms[0]["arena"] != ms[1]["arena"]:
            raise ArenaError(f"pairing {pid}: its valid matches are not one arena with the sides swapped")
        complete[pid] = ms
    return complete, counts


def bradley_terry(games: Sequence[tuple[str, str, float]], tol: float = 1e-10, max_iter: int = 10000) -> dict:
    """{player: log strength} from (i, j, i's points) games by Hunter's MM iteration, each player with one virtual
    draw against a reference player of strength 1 (so every strength is finite and the scale is fixed)."""
    players = sorted({p for i, j, _ in games for p in (i, j)})
    wins = {p: 0.5 for p in players}  # the virtual draw's half point
    for i, j, s in games:
        wins[i] += s
        wins[j] += 1.0 - s
    p = dict.fromkeys(players, 1.0)
    for _ in range(max_iter):
        new = {}
        for k in players:
            den = 1.0 / (p[k] + 1.0)  # the virtual game
            den += sum(1.0 / (p[i] + p[j]) for i, j, _ in games if k in (i, j))
            new[k] = wins[k] / den
        done = all(abs(new[k] - p[k]) <= tol * p[k] for k in players)
        p = new
        if done:
            break
    return {k: math.log(v) for k, v in p.items()}


def _elo(theta: Mapping[str, float]) -> dict[str, float]:
    mean = sum(theta.values()) / len(theta)
    return {k: ELO_BASE + ELO_SCALE * (v - mean) / math.log(10) for k, v in theta.items()}


def _games(pairs: Iterable[list[dict]]) -> list[tuple[str, str, float]]:
    out = []
    for ms in pairs:
        for m in ms:
            a, b = m["sides"]["A"], m["sides"]["B"]
            out.append((a, b, _score(m)[a]))
    return out


def results(matches: Iterable[Mapping[str, Any]], seed: int, b: int = 2000, alpha: float = 0.05) -> dict:
    """Matches (grade's dicts, each with its `pairing` id) -> {seed, b, alpha, counts, pairs, models}. pairs: per
    model pair (sorted) the first's wins, draws and losses over complete pairings; models: per player n_matches,
    n_pairings, points, w/d/l, Bradley-Terry log strength (`bt`), `elo` and `elo_ci` from a bootstrap over the complete
    pairings (random.Random(seed)), sorted by elo."""
    complete, counts = pairings(matches)
    games = _games(complete.values())
    pairs: dict[tuple[str, str], dict[str, int]] = {}
    per: dict[str, dict[str, Any]] = {}
    for pid, ms in complete.items():
        for m in ms:
            pts = _score(m)
            x, y = sorted(pts)
            cell = pairs.setdefault((x, y), {"w": 0, "d": 0, "l": 0, "n_matches": 0, "n_pairings": 0})
            cell["n_matches"] += 1
            cell["w" if pts[x] == 1.0 else "d" if pts[x] == 0.5 else "l"] += 1
            for p, s in pts.items():
                row = per.setdefault(p, {"n_matches": 0, "pairings": set(), "points": 0.0, "w": 0, "d": 0, "l": 0})
                row["n_matches"] += 1
                row["pairings"].add(pid)
                row["points"] += s
                row["w" if s == 1.0 else "d" if s == 0.5 else "l"] += 1
        x, y = sorted(_score(ms[0]))
        pairs[x, y]["n_pairings"] += 1
    models = []
    if games:
        theta = bradley_terry(games)
        elo = _elo(theta)
        rng, ids = random.Random(seed), sorted(complete)
        draws: dict[str, list[float]] = {p: [] for p in theta}
        for _ in range(b):
            picked = [complete[i] for i in rng.choices(ids, k=len(ids))]
            boot = _elo(bradley_terry(_games(picked)))
            for p in theta:
                if p in boot:  # a player whose every pairing missed this draw has no rating in it
                    draws[p].append(boot[p])
        for p in sorted(theta, key=lambda k: (-elo[k], k)):
            row = per[p]
            ci = (stats.quantile(draws[p], alpha / 2), stats.quantile(draws[p], 1 - alpha / 2)) if draws[p] else None
            models.append(
                {
                    "player": p,
                    "n_matches": row["n_matches"],
                    "n_pairings": len(row["pairings"]),
                    "points": row["points"],
                    "w": row["w"],
                    "d": row["d"],
                    "l": row["l"],
                    "bt": theta[p],
                    "elo": elo[p],
                    "elo_ci": list(ci) if ci else None,
                }
            )
    return {
        "seed": seed,
        "b": b,
        "alpha": alpha,
        "counts": {**counts, "pairings": len(complete)},
        "pairs": [{"a": x, "b": y, **cell} for (x, y), cell in sorted(pairs.items())],
        "models": models,
    }
