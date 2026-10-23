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

Check a file (docs/HUMAN-BASELINE.md):

    uv run --project packages/bench python -m bg3bench.human check FILE [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO, cast

from bg3bench import scenario

SCHEMA = 1
MODEL_PREFIX = "human"
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NO_TOKENS = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
_KEYS = {"scenario", "attempt", "date", "passed", "time_s", "t_pass_s", "reloads", "void", "picks", "notes"}


def slug(player: str) -> str:
    return f"{MODEL_PREFIX} ({player})"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _pick_ok(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    p = cast(dict[str, Any], obj)
    return set(p) == {"scene", "pick"} and all(isinstance(v, str) for v in p.values())


def _check_attempt(obj: Any, where: str, scenarios: Mapping[str, Mapping[str, Any]]) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where}: not an object"]
    a = cast(dict[str, Any], obj)
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
        sc = scenarios.get(a.get("scenario", "")) or {}
        if sc.get("family") != "choice":
            errs.append(f"{where}: picks only belong to a choice scenario")
        ok = isinstance(picks, list) and all(_pick_ok(p) for p in cast(list[Any], picks))
        if not ok:
            errs.append(f"{where}: picks must be a list of {{scene, pick}} strings")
    if a.get("notes") is not None and not isinstance(a["notes"], str):
        errs.append(f"{where}: notes must be a string")
    return errs


def validate(obj: Any, scenarios: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Problems with a human results file (empty: valid). `scenarios`: name -> loaded scenario (scenario.load)."""
    if not isinstance(obj, dict):
        return ["not an object"]
    data = cast(dict[str, Any], obj)
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
    for i, a in enumerate(cast(list[Any], attempts)):
        a_errs = _check_attempt(a, f"attempts[{i}]", scenarios)
        errs += a_errs
        if not a_errs:
            seen.setdefault(a["scenario"], []).append(a["attempt"])
    for name, nums in sorted(seen.items()):
        if sorted(nums) != list(range(1, len(nums) + 1)):
            errs.append(f"{name}: attempts must be numbered 1..{len(nums)} with no gap or repeat, got {sorted(nums)}")
    return errs


def load(path: str | Path, scenarios: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate(data, scenarios)
    if errs:
        raise ValueError(f"{path}: " + "; ".join(errs))
    return data


def headline_pass(attempt: Mapping[str, Any], scenario: Mapping[str, Any]) -> bool:
    """Passed as the model headline counts it: reported passed, no reload, goal met inside the run cap."""
    if not attempt["passed"] or attempt["reloads"]:
        return False
    caps: Mapping[str, Any] = scenario.get("caps") or {}
    cap = caps.get("run_hours")
    return cap is None or attempt["t_pass_s"] <= cap * 3600


def run_id(player: str, attempt: Mapping[str, Any]) -> str:
    return f"human-{player}-{attempt['scenario']}-a{attempt['attempt']:02d}"


def pairs(
    data: Mapping[str, Any], scenarios: Mapping[str, Mapping[str, Any]], bench_version: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A validated human results file -> (manifest, result) pairs for results.build_results."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
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


def load_suite(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """name -> scenario.load for every *.json in the suite dir (this package's scenarios/ unless given)."""
    d = Path(root) if root is not None else SCENARIOS_DIR
    files = sorted(p for p in d.glob("*.json") if p.is_file())
    if not files:
        raise ValueError(f"{d}: no scenario files")
    load_fn: Any = cast(Any, scenario).load
    out: dict[str, dict[str, Any]] = {}
    for p in files:
        out[p.stem] = cast(dict[str, Any], load_fn(p))
    return out


def report(data: Any, scenarios: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a loaded human results value. `ok` is true when `errors` is empty."""
    errors = validate(data, scenarios)
    if not isinstance(data, dict):
        return {"ok": not errors, "errors": errors, "player": None, "attempts": None}
    obj = cast(dict[str, Any], data)
    player, attempts = obj.get("player"), obj.get("attempts")
    return {
        "ok": not errors,
        "errors": errors,
        "player": player if isinstance(player, str) else None,
        "attempts": len(cast(list[Any], attempts)) if isinstance(attempts, list) else None,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    ap = argparse.ArgumentParser(
        prog="python -m bg3bench.human",
        description="Validate a human baseline results file (docs/HUMAN-BASELINE.md).",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    ck = sub.add_parser("check", help="print ok, or the problems (exit 2)")
    ck.add_argument("file", type=Path, help="the player's results JSON")
    ck.add_argument("--scenarios", type=Path, help="directory of scenario files (default: this package's scenarios/)")
    ck.add_argument("--json", action="store_true", help="print {ok, errors, player, attempts}")
    args = ap.parse_args(argv)
    try:
        scenarios = load_suite(args.scenarios)
    except (OSError, ValueError) as e:
        print(f"refusing: {e}", file=err)
        return 2
    try:
        data = json.loads(args.file.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"refusing: {e}", file=err)
        return 2
    except json.JSONDecodeError as e:
        print(f"refusing: {args.file}: not JSON ({e.msg})", file=err)
        return 2
    got = report(data, scenarios)
    if args.json:
        print(json.dumps(got), file=out)
    elif got["ok"]:
        n = got["attempts"] or 0
        print(f"ok: {got['player']}, {n} attempt{'s' if n != 1 else ''}", file=out)
    else:
        for e in got["errors"]:
            print(f"refusing: {e}", file=err)
    return 0 if got["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - python -m bg3bench.human
    raise SystemExit(main())
