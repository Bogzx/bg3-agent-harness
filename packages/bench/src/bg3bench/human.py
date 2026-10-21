"""Human baseline results (schema/human.v1.schema.json, docs/HUMAN-BASELINE.md) -> leaderboard runs.

A human results file lists every attempt one player made at the benchmark scenarios through the normal game, from
the scenario's SCN start save: the attempt number (1, 2, ... per scenario, no gaps, so a failed attempt can't be
left out), the wall time, whether and when the goal was met, the reloads, and the picks of a choice scene. An
attempt is `void` (with the reason) when it was not a fair try, such as a game crash; void attempts are invalid
runs, like a model's runner error.

Scoring follows the model headline (docs/BENCHMARK-PLAN.md): first-try success with no reload, inside the
scenario's run cap. An attempt the player reports as passed counts as passed only with `reloads == 0` and
`t_pass_s` within `caps.run_hours`; otherwise it is a failed run (its reloads and time are kept in the file).

`pairs(data, scenarios)` turns the file into (manifest, result) pairs that results.build_results takes next to the
models' runs, so the player is one more leaderboard row, "human (<player>)", with no cost and no tokens.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = 1
MODEL_PREFIX = "human"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NO_TOKENS = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
_KEYS = {"scenario", "attempt", "date", "passed", "time_s", "t_pass_s", "reloads", "void", "picks", "notes"}


def slug(player: str) -> str:
    return f"{MODEL_PREFIX} ({player})"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _check_attempt(a: Any, where: str, scenarios: Mapping[str, Mapping]) -> list[str]:
    if not isinstance(a, dict):
        return [f"{where}: not an object"]
    errs = [f"{where}: unknown key {k!r}" for k in sorted(set(a) - _KEYS)]
    if a.get("scenario") not in scenarios:
        errs.append(f"{where}: unknown scenario {a.get('scenario')!r}")
    if not isinstance(a.get("attempt"), int) or isinstance(a.get("attempt"), bool) or a["attempt"] < 1:
        errs.append(f"{where}: attempt must be an integer >= 1")
    if not isinstance(a.get("date"), str) or not _DATE.fullmatch(a["date"]):
        errs.append(f"{where}: date must be YYYY-MM-DD")
    if not isinstance(a.get("passed"), bool):
        errs.append(f"{where}: passed must be true or false")
    if not _num(a.get("time_s")):
        errs.append(f"{where}: time_s must be a number >= 0")
    if not isinstance(a.get("reloads"), int) or isinstance(a.get("reloads"), bool) or a["reloads"] < 0:
        errs.append(f"{where}: reloads must be an integer >= 0")
    t = a.get("t_pass_s")
    if a.get("passed") is True:
        if not _num(t):
            errs.append(f"{where}: a passed attempt needs t_pass_s")
        elif _num(a.get("time_s")) and t > a["time_s"]:
            errs.append(f"{where}: t_pass_s is after the attempt ended (time_s)")
    elif t is not None:
        errs.append(f"{where}: t_pass_s is only set on a passed attempt")
    if a.get("void") is not None and not (isinstance(a["void"], str) and a["void"].strip()):
        errs.append(f"{where}: void must be null or the reason")
    picks = a.get("picks")
    if picks is not None:
        sc = scenarios.get(a.get("scenario")) or {}
        if sc.get("family") != "choice":
            errs.append(f"{where}: picks only belong to a choice scenario")
        ok = isinstance(picks, list) and all(
            isinstance(p, dict) and set(p) == {"scene", "pick"} and all(isinstance(v, str) for v in p.values())
            for p in picks
        )
        if not ok:
            errs.append(f"{where}: picks must be a list of {{scene, pick}} strings")
    if a.get("notes") is not None and not isinstance(a["notes"], str):
        errs.append(f"{where}: notes must be a string")
    return errs


def validate(data: Any, scenarios: Mapping[str, Mapping]) -> list[str]:
    """Problems with a human results file (empty: valid). `scenarios`: name -> loaded scenario (scenario.load)."""
    if not isinstance(data, dict):
        return ["not an object"]
    errs = [f"unknown key {k!r}" for k in sorted(set(data) - {"schema", "player", "game_version", "attempts"})]
    if data.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    if not isinstance(data.get("player"), str) or not re.fullmatch(r"[a-z0-9_-]+", data["player"]):
        errs.append("player must be a short lowercase id, e.g. owner")
    if data.get("game_version") is not None and not isinstance(data["game_version"], str):
        errs.append("game_version must be a string or null")
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        return [*errs, "attempts must be a list"]
    seen: dict[str, list[int]] = {}
    for i, a in enumerate(attempts):
        a_errs = _check_attempt(a, f"attempts[{i}]", scenarios)
        errs += a_errs
        if not a_errs:
            seen.setdefault(a["scenario"], []).append(a["attempt"])
    for name, nums in sorted(seen.items()):
        if sorted(nums) != list(range(1, len(nums) + 1)):
            errs.append(f"{name}: attempts must be numbered 1..{len(nums)} with no gap or repeat, got {sorted(nums)}")
    return errs


def load(path: str | Path, scenarios: Mapping[str, Mapping]) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate(data, scenarios)
    if errs:
        raise ValueError(f"{path}: " + "; ".join(errs))
    return data


def headline_pass(attempt: Mapping, scenario: Mapping) -> bool:
    """Passed as the model headline counts it: reported passed, no reload, goal met inside the run cap."""
    if not attempt["passed"] or attempt["reloads"]:
        return False
    cap = (scenario.get("caps") or {}).get("run_hours")
    return cap is None or attempt["t_pass_s"] <= cap * 3600


def run_id(player: str, attempt: Mapping) -> str:
    return f"human-{player}-{attempt['scenario']}-a{attempt['attempt']:02d}"


def pairs(data: Mapping, scenarios: Mapping[str, Mapping], bench_version: str) -> list[tuple[dict, dict]]:
    """A validated human results file -> (manifest, result) pairs for results.build_results."""
    out = []
    for a in data["attempts"]:
        sc = scenarios[a["scenario"]]
        rid = run_id(data["player"], a)
        passed = a["void"] is None and headline_pass(a, sc)
        man = {
            "run_id": rid,
            "model": {"slug": slug(data["player"])},
            "condition": None,
            "versions": {"harness": None, "bench": bench_version},
            "scenario": {"name": a["scenario"], "timing": sc["timing"]},
        }
        res = {
            "run_id": rid,
            "valid": a["void"] is None,
            "invalid_reason": None if a["void"] is None else "operator",
            "passed": passed,
            "t_pass_s": a["t_pass_s"] if passed else None,
            "cost_usd": None,
            "wall_s": float(a["time_s"]),
            "think_s": None,
            "luck": None,
            "reloads": a["reloads"],
            "tokens": dict(_NO_TOKENS),
        }
        if a.get("picks") is not None:
            res["choices"] = {"picks": a["picks"], "fit": None}
        out.append((man, res))
    return out
