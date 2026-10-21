"""The matrix CLI: `python -m bg3bench.matrix plan|run|status|resume --matrix <file.toml>`.

The matrix file (paths relative to the repo):

    name = "night1"                                   # runtime/matrix/<name>/ holds the queue and progress.json
    models = ["profiles/opus-5.5-pi.toml"]            # client profiles: client, model, effort
    scenarios = ["nautiloid"]                         # scenarios/<name>.json, as play.py --scenario reads them
    conditions = ["rules"]
    repeats = 3
    allow_gaps = false                                # true: a pilot with conditions play.py can't play exactly yet

    [budget]
    file = "runtime/budget.json"                      # the machine's shared budget (the default)
    max_cost = { nautiloid = 5.0 }                    # USD per run of a scenario: the reserve and a paid run's cap

- plan: a dry run. The jobs as a table, the reserve, the budget left and what play.py lacks for each condition;
  writes nothing.
- run: writes the plan (or keeps the same one) and runs it through the legacy CLI adapter until done or a stop.
- resume: run, for a matrix that already has a queue (refused otherwise).
- status: the jobs' state from the queue: status, attempts, the last result.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from bg3bench import matrix
from bg3bench.adapters import legacy_cli, play_flags

BUDGET = "runtime/budget.json"


class MatrixFileError(ValueError):
    pass


def _list(raw: dict, key: str, path: Path) -> list[str]:
    v = raw.get(key)
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise MatrixFileError(f"{path}: {key} must be a non-empty list of strings")
    return v


def load_profile(path: Path) -> dict:
    """A client profile (profiles/*.toml) as a matrix model: {"slug", "client", "effort", "profile"}."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MatrixFileError(f"{path}: {e}") from None
    if not isinstance(raw.get("client"), str) or not isinstance(raw.get("model"), str):
        raise MatrixFileError(f"{path}: a profile needs client and model")
    return {"slug": raw["model"], "client": raw["client"], "effort": raw.get("effort"), "profile": str(path)}


def load(path: str | Path, repo: Path) -> dict:
    """The matrix file -> {"name", "jobs", "budget", "conditions", "allow_gaps"}; checks profiles and scenarios."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MatrixFileError(f"{path}: {e}") from None
    name = raw.get("name")
    if not isinstance(name, str) or not name or name != matrix._slug(name):
        raise MatrixFileError(f"{path}: name must be a non-empty slug")
    repeats = raw.get("repeats", 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise MatrixFileError(f"{path}: repeats must be an integer >= 1")
    scenarios = _list(raw, "scenarios", path)
    for s in scenarios:
        if not (repo / "scenarios" / f"{s}.json").is_file():
            raise MatrixFileError(f"{path}: no scenario {s!r} (scenarios/{s}.json)")
    budget = raw.get("budget", {})
    max_cost = budget.get("max_cost", {})
    if not isinstance(max_cost, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in max_cost.values()
    ):
        raise MatrixFileError(f"{path}: budget.max_cost maps scenarios to USD > 0")
    if unknown := sorted(set(max_cost) - set(scenarios)):
        raise MatrixFileError(f"{path}: budget.max_cost names scenarios not in the matrix: {unknown}")
    models = [load_profile(repo / p) for p in _list(raw, "models", path)]
    conds = _list(raw, "conditions", path)
    for c in conds:
        play_flags.entry(c)  # a malformed condition id raises here
    try:
        jobs = matrix.plan(models, scenarios, conds, repeats, max_cost={k: float(v) for k, v in max_cost.items()})
    except ValueError as e:
        raise MatrixFileError(f"{path}: {e}") from None
    for j in jobs:
        if j["client"] in matrix.PAID_CLIENTS and j["max_cost"] is None:
            raise MatrixFileError(f"{path}: {j['client']} spends money: give budget.max_cost for {j['scenario']}")
    return {
        "name": name,
        "jobs": jobs,
        "budget": repo / budget.get("file", BUDGET),
        "conditions": conds,
        "allow_gaps": bool(raw.get("allow_gaps", False)),
    }


def _table(rows: list[list[str]], out: TextIO) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip(), file=out)


def _usd(x: float | None) -> str:
    return "-" if x is None else f"${x:.2f}"


def print_plan(m: dict, out: TextIO) -> None:
    jobs = m["jobs"]
    rows = [["job", "client", "effort", "max_cost"]]
    rows += [[j["id"], j["client"], j["effort"] or "-", _usd(j["max_cost"])] for j in jobs]
    _table(rows, out)
    paid = [j for j in jobs if j["client"] in matrix.PAID_CLIENTS]
    reserve = sum(j["max_cost"] for j in paid)
    print(f"\n{len(jobs)} jobs ({len(paid)} paid), reserve {_usd(reserve)}", file=out)
    try:
        b = matrix.read_budget(m["budget"])
        print(f"budget {m['budget']}: {_usd(b['spent'])} of {_usd(b['cap'])} spent", file=out)
    except (OSError, ValueError) as e:
        print(f"budget {m['budget']}: unreadable ({e}): run stops before its first job", file=out)
    for c in m["conditions"]:
        gaps = play_flags.entry(c).gaps
        if gaps:
            how = "a pilot (allow_gaps)" if m["allow_gaps"] else "refused by run"
            print(f"condition {c}: {how}; play.py lacks " + "; ".join(gaps), file=out)


def print_status(mx: matrix.Matrix, out: TextIO) -> None:
    rows = [["job", "status", "attempts", "last"]]
    for jid, st in mx.state().items():
        last = st["attempts"][-1]["result"] if st["attempts"] else None
        if last is None:
            text = "running or interrupted" if st["attempts"] else "-"
        elif not last["valid"]:
            text = f"invalid: {last['invalid_reason']}"
        else:
            text = ("pass" if last["passed"] else "fail") + f" {_usd(last['cost_usd'])} {last['run_id']}"
        rows.append([jid, st["status"], str(len(st["attempts"])), text])
    _table(rows, out)


def _no_run(job: dict) -> matrix.RunResult:  # pragma: no cover - status never runs a job
    raise AssertionError("status runs nothing")


def main(
    argv: Sequence[str] | None = None,
    *,
    repo: Path = legacy_cli.REPO,
    run_one: matrix.RunOne | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.matrix", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("plan", "run", "status", "resume"))
    ap.add_argument("--matrix", required=True, metavar="TOML")
    args = ap.parse_args(argv)
    try:
        m = load(args.matrix, repo)
    except (MatrixFileError, ValueError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    if args.command == "plan":
        print_plan(m, out)
        return 0
    root = repo / "runtime" / "matrix" / m["name"]
    if args.command in ("status", "resume") and not (root / "queue.jsonl").is_file():
        print(f"refusing: matrix {m['name']} has no queue ({root / 'queue.jsonl'}): run it first", file=sys.stderr)
        return 2
    if args.command == "status":
        print_status(matrix.Matrix(root, _no_run, m["budget"], now=now, sleep=sleep), out)
        return 0
    if run_one is None:
        try:
            flags, env = play_flags.table(m["conditions"], allow_gaps=m["allow_gaps"])
        except ValueError as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 2
        run_one = legacy_cli.LegacyCli(m["name"], flags, condition_env=env, repo=repo)
    mx = matrix.Matrix(root, run_one, m["budget"], now=now, sleep=sleep)
    try:
        mx.create(m["name"], m["jobs"])
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    stop = mx.run()
    print_status(mx, out)
    print(f"\nstopped: {stop}", file=out)
    return 0 if stop == matrix.FINISHED else 1
