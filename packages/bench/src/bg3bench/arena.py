# pyright: basic
"""Arenas: two models fight each other in one turn-based fight. Arena files, the match grader and the ratings.

An arena file (`arenas/<name>.json`, schema/arena.v1.schema.json; `load`, `validate`) fixes one fight: the start save
(name + sha1), the game level and the two spawn points, each side's team (display names, optional templates and
uuids: arena.setup fails when a name and its uuid disagree), the character level every member is set to (`level`,
default 2), the dice seed, Karmic Dice off (`karmic: false`), the round cap (default 20), the wall-clock cap in minutes (default 30), the
model-time cap per turn in seconds (`turn_cap_s`), the condition and the name of the bridge's tools-off list
(TOOLS_OFF_LISTS). `draft` says why an arena can't be played yet (its spots are placeholders); the pairing planner
(arena_matrix) gives a draft arena no matches. Arena files are not suite files: nothing here moves a bench-vX.Y score.

A **match** is one fight: model X plays side A, model Y side B. A **pairing** is two matches of one arena, seed and
teams with the sides swapped, so the side advantage cancels.

`grade(records, arena, record=None, rolls=None)` grades a match from its facts.jsonl: the referee's events (schema/
arena-facts.v1.schema.json) `arena_start {arena, seed, teams, sides, uuids?}`, `arena_turn {side, character,
members, round, phase, group, shared}`, `arena_turn_forced {side, character}` and `arena_end {winner, reason, rounds,
hp_left_A, hp_left_B, forfeit?, cause?}`. A turn is a phase: one same-side run of the rolled order inside a turn group
(the game puts both teams' adjacent characters into one shared group, which the mod plays side by side). A phase is
off its side only when it names a character of the other team; a name on neither team (a summon) counts on the
phase's side. A side wins when every character of the other side is down or dead (`wipe`) or when the other side forfeits
(`forfeit`: three of its turns in a row forced to an end at the per-turn cap, cause `forced_turns`, which the grader
checks against the arena_turn_forced events; or its client's fault after one resume, cause `client_fault`). The round
cap and the wall cap are draws (`round_cap`, `wall_cap`; the HP fractions are recorded, never a tiebreak). A match is
invalid, rerun and never rated when its run record is (results.validity: runner error, harness failure, operator, off
route, unseeded), when the harness ended it (`abort`) or the game failed (`fault`: a crash or hang), or when the facts
don't match the arena or contradict themselves (INVALID). With the match's rolls.jsonl rows, `luck` is each side's
stats.luck_z over its own characters' d20 faces (a row's `roller` uuid in arena_start's `uuids`).

`results(matches, seed)` is the v1 table, over valid matches with the pairing as the resampled unit:

- `pairs`: per model pair over complete pairings (both matches valid; a pairing short of one is `incomplete` and left
  out, its side advantage doesn't cancel): pairings won / split / lost by the first model (more, equal or fewer of
  the pairing's two points: a 2-0 is won, a 1-1 split), matches w/d/l, the match win rate (a draw = half a win) with
  its Wilson interval, n_pairings, n_matches, and `rated` (n_pairings >= MIN_PAIRINGS; else a pilot row).
- `side_a`: side A's win rate over every valid match (a model against itself too) with a bootstrap interval over
  pairings: an interval that excludes 0.5 says the arena favours a side.
- `models`: per model its matches and points over complete pairings; a model in a rated pair has a Bradley-Terry
  strength fit over the rated pairs' pairings (a draw = half a win to each; one virtual draw against a fixed
  reference player keeps an undefeated model finite) with a percentile bootstrap interval over those pairings
  (seeded), shown on the Elo scale (400 log10 strength, centred on 1500 over the rated models); any other model is a
  pilot row with no rating. A match a model plays against itself is graded but never rated.
"""

from __future__ import annotations

import json
import math
import random
import re
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
LEVEL, LEVEL_MAX = 2, 12  # every member's character level after arena.setup
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MIN_PAIRINGS = 15  # a pair's pairings before its models are rated; below it, a pilot row
FORFEIT_TURNS = 3  # forced turns in a row that forfeit the match
TOOLS_OFF_LISTS = ("arena-v1",)  # the bridge's named arena tools-off lists (BG3AI_TOOLS_OFF values)
KEYS = {
    "schema", "name", "title", "about", "draft", "save", "location", "teams", "level", "seed", "karmic",
    "round_cap", "wall_cap_min", "turn_cap_s", "condition", "tools_off",
}  # fmt: skip
SHA1_LEN = 40
END_REASONS = ("wipe", "forfeit", "round_cap", "wall_cap", "abort", "fault")
CAP_REASONS = ("round_cap", "wall_cap")  # draws
FAULT_REASONS = ("abort", "fault")  # the harness ended the match or the game failed: invalid, rerun
FORFEIT_CAUSES = ("forced_turns", "client_fault")
INVALID = (
    "no_start", "wrong_arena", "unseeded", "off_side_turn", "inconsistent_turns", "no_end", "past_round_cap",
    "inconsistent_end",
)  # fmt: skip
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
    errs, seen, uuids = [], set(), set()
    for side in SIDES:
        team = teams[side]
        if not isinstance(team, list) or not 1 <= len(team) <= TEAM_MAX:
            errs.append(f"teams.{side} must list 1 to {TEAM_MAX} characters")
            continue
        for m in team:
            if not isinstance(m, dict) or not _str(m.get("name")) or set(m) - {"name", "template", "uuid"}:
                errs.append(f"teams.{side}: a character is {{name, template?, uuid?}}: {m!r}")
                continue
            if "template" in m and not _str(m["template"]):
                errs.append(f"teams.{side}: {m['name']}'s template must be a template id")
            if "uuid" in m:
                if not (isinstance(m["uuid"], str) and UUID_RE.match(m["uuid"])):
                    errs.append(f"teams.{side}: {m['name']}'s uuid must be a lowercase uuid")
                elif m["uuid"] in uuids:
                    errs.append(f"teams: uuid {m['uuid']} is listed twice")
                uuids.add(m.get("uuid"))
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
    level = a.get("level", LEVEL)
    if not _int(level) or not 1 <= level <= LEVEL_MAX:
        errs.append(f"level must be an integer 1..{LEVEL_MAX}")
    seed = a.get("seed")
    if not _int(seed) or not 0 <= seed <= scenario.SEED_MAX:
        errs.append(f"seed must be an integer 0..{scenario.SEED_MAX}")
    rc = a.get("round_cap", ROUND_CAP)
    if not _int(rc) or rc < 1:
        errs.append("round_cap must be an integer >= 1")
    wc = a.get("wall_cap_min", WALL_CAP_MIN)
    if not _num(wc) or wc <= 0:
        errs.append("wall_cap_min must be a number of minutes > 0")
    tc = a.get("turn_cap_s")
    if not _num(tc) or tc <= 0:
        errs.append("turn_cap_s must be the model seconds a turn may take (> 0)")
    if a.get("karmic") is not False:
        errs.append("karmic must be false: Karmic Dice are off in every arena")
    try:
        if conditions.parse(a.get("condition", "rules")).reloads:
            errs.append("condition: a match has no reloads")
    except (ValueError, AttributeError) as e:
        errs.append(f"condition: {e}")
    if a.get("tools_off") not in TOOLS_OFF_LISTS:
        errs.append(f"tools_off must name one of the bridge's arena lists: {', '.join(TOOLS_OFF_LISTS)}")
    return errs


def with_defaults(a: Mapping[str, Any]) -> dict[str, Any]:
    return {"level": LEVEL, "round_cap": ROUND_CAP, "wall_cap_min": WALL_CAP_MIN, "condition": "rules", **a}


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


def _other(side: Any) -> str | None:
    return {"A": "B", "B": "A"}.get(side)


def _turns(records: list[dict], end_t: float) -> list[dict] | None:
    """The turns up to the end, in order, each {side, character, members, forced}; None when an arena_turn_forced
    doesn't name the turn in progress (its side and character)."""
    turns: list[dict] = []
    for r in records:
        if r["t"] > end_t or r.get("kind") != "event":
            continue
        if r.get("ev") == "arena_turn":
            turns.append(
                {"side": r.get("side"), "character": r.get("character"), "members": r.get("members"), "forced": False}
            )
        elif r.get("ev") == "arena_turn_forced":
            now = turns[-1] if turns else None
            if now is None or now["forced"] or (now["side"], now["character"]) != (r.get("side"), r.get("character")):
                return None
            now["forced"] = True
    return turns


def _forced_out(turns: list[dict]) -> str | None:
    """The first side whose own turns were forced to an end FORFEIT_TURNS times in a row, None if neither."""
    streak = dict.fromkeys(SIDES, 0)
    for turn in turns:
        streak[turn["side"]] = streak[turn["side"]] + 1 if turn["forced"] else 0
        if streak[turn["side"]] >= FORFEIT_TURNS:
            return turn["side"]
    return None


def _end_problem(end: dict, ends: int, arena: Mapping[str, Any], forced_out: str | None) -> str | None:
    if (
        ends > 1
        or end.get("reason") not in END_REASONS
        or end.get("winner", "-") not in (*SIDES, None)
        or not _int(end.get("rounds"))
        or not all(_num(end.get(f"hp_left_{s}")) and 0 <= end[f"hp_left_{s}"] <= 1 for s in SIDES)
    ):
        return "inconsistent_end"
    if end["rounds"] > arena["round_cap"]:
        return "past_round_cap"
    reason, winner = end["reason"], end["winner"]
    if reason == "forfeit":
        side, cause = end.get("forfeit"), end.get("cause")
        if side not in SIDES or cause not in FORFEIT_CAUSES or winner != _other(side):
            return "inconsistent_end"
        if cause == "forced_turns" and forced_out != side:
            return "inconsistent_end"
        return None if forced_out in (None, side) else "inconsistent_end"
    if forced_out is not None:  # the match went on past a forfeit
        return "inconsistent_end"
    if reason == "wipe":
        loser = _other(winner)
        return None if loser is not None and end[f"hp_left_{loser}"] == 0 else "inconsistent_end"
    return None if winner is None else "inconsistent_end"  # a cap: a draw


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
    turns = _turns(records, ends[0]["t"] if ends else math.inf)
    if turns is None:
        return "inconsistent_turns"
    if any(
        turn["side"] not in SIDES
        or not isinstance(turn["members"], list)
        or any(n in teams[_other(turn["side"]) or ""] for n in [turn["character"], *turn["members"]])
        for turn in turns
    ):
        return "off_side_turn"
    if not ends:
        return "no_end"
    if ends[0].get("reason") in FAULT_REASONS:
        return None  # the harness's own end or the game's failure: graded as such
    return _end_problem(ends[0], len(ends), arena, _forced_out(turns))


def _counted(roll: Mapping[str, Any]) -> bool:
    """A roll whose d20 face is uniform: seen, without advantage or disadvantage (bg3bench.luck's rule)."""
    return roll.get("natural") is not None and not roll.get("advantage") and not roll.get("disadvantage")


def luck(rolls: Iterable[Mapping[str, Any]], uuids: Mapping[str, Sequence[str]]) -> dict:
    """{A: {n, z}, B: {n, z}, other}: each side's stats.luck_z over the counted d20 faces its own characters rolled
    (a row's `roller` in that side's uuids); other = counted rows that neither side rolled."""
    faces: dict[str, list[int]] = {s: [] for s in SIDES}
    other = 0
    for r in rolls:
        if not _counted(r):
            continue
        side = next((s for s in SIDES if r.get("roller") in uuids.get(s, ())), None)
        if side is None:
            other += 1
        else:
            faces[side].append(r["natural"])
    return {**{s: {"n": len(f), "z": round(stats.luck_z(f), 4)} for s, f in faces.items()}, "other": other}


def grade(
    records: Iterable[dict],
    arena: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
    rolls: Iterable[Mapping[str, Any]] | None = None,
) -> dict:
    """One match from its facts records (facts.check'd), its run record when given (results.validity with the
    arena's seed) and its rolls.jsonl rows when given: {arena, valid, invalid_reason, sides {A, B: player}, result
    (A, B, draw; None when invalid), reason, forfeit {side, cause} or None, rounds, hp_left {A, B}, turns {A, B},
    forced {A, B}, luck (None without rolls or arena_start uuids)}."""
    records = list(records)
    valid, why = validity(dict(record), arena["seed"]) if record is not None else (True, None)
    if valid:
        why = _facts_problem(records, arena)
    starts, ends = _events(records, "arena_start"), _events(records, "arena_end")
    start, end = starts[0] if starts else {}, ends[0] if ends else {}
    if why is None and end.get("reason") in FAULT_REASONS:
        why = end["reason"]
    turns = _turns(records, end.get("t", math.inf)) or []
    ok = why is None
    uuids = start.get("uuids")
    return {
        "arena": arena["name"],
        "valid": ok,
        "invalid_reason": why,
        "sides": {s: (start.get("sides") or {}).get(s) for s in SIDES},
        "result": (end["winner"] or "draw") if ok else None,
        "reason": end.get("reason"),
        "forfeit": {"side": end["forfeit"], "cause": end["cause"]} if ok and end["reason"] == "forfeit" else None,
        "rounds": end.get("rounds"),
        "hp_left": {s: end.get(f"hp_left_{s}") for s in SIDES},
        "turns": {s: sum(1 for turn in turns if turn["side"] == s) for s in SIDES},
        "forced": {s: sum(1 for turn in turns if turn["side"] == s and turn["forced"]) for s in SIDES},
        "luck": luck(rolls, uuids) if rolls is not None and isinstance(uuids, dict) else None,
    }


# ---------------------------------------------------------------------------------------------------- the ratings


def _points(m: Mapping[str, Any], side: str) -> float:
    """A side's points in a valid match: 1 for a win, 0.5 for a draw."""
    return 1.0 if m["result"] == side else 0.5 if m["result"] == "draw" else 0.0


def _score(m: Mapping[str, Any]) -> dict[str, float]:
    """{player: points} of a valid match between two players."""
    return {m["sides"][s]: _points(m, s) for s in SIDES}


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
    met: dict[tuple[str, str], int] = {}  # games per pair of players
    for i, j, s in games:
        wins[i] += s
        wins[j] += 1.0 - s
        met[min(i, j), max(i, j)] = met.get((min(i, j), max(i, j)), 0) + 1
    mine = {k: [(i, j, n) for (i, j), n in met.items() if k in (i, j)] for k in players}
    p = dict.fromkeys(players, 1.0)
    for _ in range(max_iter):
        new = {}
        for k in players:
            den = 1.0 / (p[k] + 1.0)  # the virtual game
            den += sum(n / (p[i] + p[j]) for i, j, n in mine[k])
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
    return [(m["sides"]["A"], m["sides"]["B"], _points(m, "A")) for ms in pairs for m in ms]


def _ci(draws: list[float], alpha: float) -> list[float] | None:
    return [stats.quantile(draws, alpha / 2), stats.quantile(draws, 1 - alpha / 2)] if draws else None


def _side_a(matches: list[Mapping[str, Any]], rng: random.Random, b: int, alpha: float) -> dict:
    """Side A's win rate over valid matches, its interval from a bootstrap over their pairings."""
    by: dict[str, list[float]] = {}
    for m in matches:
        by.setdefault(m["pairing"], []).append(_points(m, "A"))
    ids = sorted(by)
    n = sum(len(v) for v in by.values())
    rate = sum(sum(v) for v in by.values()) / n if n else None
    draws = []
    for _ in range(b if ids else 0):
        picked = [by[i] for i in rng.choices(ids, k=len(ids))]
        draws.append(sum(sum(v) for v in picked) / sum(len(v) for v in picked))
    return {"n_matches": n, "n_pairings": len(ids), "rate": rate, "ci": _ci(draws, alpha)}


def _pair_rows(complete: dict[str, list[dict]], min_pairings: int) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    for ms in complete.values():
        x, y = sorted(_score(ms[0]))
        row = rows.setdefault((x, y), {"won": 0, "split": 0, "lost": 0, "w": 0, "d": 0, "l": 0, "ids": []})
        pts = sum(_score(m)[x] for m in ms)
        row["won" if pts > 1 else "split" if pts == 1 else "lost"] += 1
        row["ids"].append(ms[0]["pairing"])
        for m in ms:
            s = _score(m)[x]
            row["w" if s == 1.0 else "d" if s == 0.5 else "l"] += 1
    for row in rows.values():
        n = row["w"] + row["d"] + row["l"]
        k = row["w"] + row["d"] / 2
        row.update(n_matches=n, n_pairings=len(row["ids"]), win_rate=k / n, wilson=list(stats.wilson(k, n)))  # pyright: ignore[reportArgumentType]
        row["rated"] = row["n_pairings"] >= min_pairings
    return rows


def results(
    matches: Iterable[Mapping[str, Any]],
    seed: int,
    b: int = 2000,
    alpha: float = 0.05,
    min_pairings: int = MIN_PAIRINGS,
) -> dict:
    """Matches (grade's dicts, each with its `pairing` id) -> {seed, b, alpha, min_pairings, counts, pairs, side_a,
    models}: the v1 table (module docstring). The bootstraps (side A's rate, then the ratings) draw from one
    random.Random(seed)."""
    matches = list(matches)
    complete, counts = pairings(matches)
    rng = random.Random(seed)
    side_a = _side_a([m for m in matches if m["valid"]], rng, b, alpha)
    rows = _pair_rows(complete, min_pairings)
    per: dict[str, dict[str, Any]] = {}
    for pid, ms in complete.items():
        for m in ms:
            for p, s in _score(m).items():
                row = per.setdefault(p, {"n_matches": 0, "pairings": set(), "points": 0.0, "w": 0, "d": 0, "l": 0})
                row["n_matches"] += 1
                row["pairings"].add(pid)
                row["points"] += s
                row["w" if s == 1.0 else "d" if s == 0.5 else "l"] += 1
    rated = sorted(pid for row in rows.values() if row["rated"] for pid in row["ids"])
    theta = bradley_terry(_games(complete[i] for i in rated)) if rated else {}
    elo = _elo(theta) if theta else {}
    draws: dict[str, list[float]] = {p: [] for p in theta}
    for _ in range(b if rated else 0):
        boot = _elo(bradley_terry(_games(complete[i] for i in rng.choices(rated, k=len(rated)))))
        for p, e in boot.items():  # a model whose every pairing missed this draw has no rating in it
            draws[p].append(e)
    models = [
        {
            "player": p,
            "n_matches": row["n_matches"],
            "n_pairings": len(row["pairings"]),
            "points": row["points"],
            "w": row["w"],
            "d": row["d"],
            "l": row["l"],
            "rated": p in theta,
            "bt": theta.get(p),
            "elo": elo.get(p),
            "elo_ci": _ci(draws[p], alpha) if p in theta else None,
        }
        for p, row in per.items()
    ]
    models.sort(key=lambda r: (not r["rated"], -(r["elo"] or 0.0), -r["points"] / r["n_matches"], r["player"]))
    return {
        "seed": seed,
        "b": b,
        "alpha": alpha,
        "min_pairings": min_pairings,
        "counts": {**counts, "pairings": len(complete)},
        "pairs": [
            {"a": x, "b": y, **{k: v for k, v in row.items() if k != "ids"}} for (x, y), row in sorted(rows.items())
        ],
        "side_a": side_a,
        "models": models,
    }
