"""The matrix CLI: `python -m bg3bench.matrix plan|run|status|resume --matrix <file.toml> [--runner session|legacy]`.

The matrix file (paths relative to the repo):

    name = "night1"                                   # runtime/matrix/<name>/ holds the queue and progress.json
    models = ["profiles/opus-5.5-pi.toml"]            # client profiles: client, model, effort
    scenarios = ["nautiloid"]                         # packages/bench/scenarios/<name>.json (v2)
    conditions = ["rules"]
    repeats = 3
    allow_gaps = false                                # true: a pilot with conditions play.py can't play exactly yet
    allow_unverified = false                          # true: profiles whose status is "offline" (never played live)

    [budget]
    file = "runtime/budget.json"                      # the machine's shared budget (the default)
    max_cost = { nautiloid = 5.0 }                    # USD per run of a scenario: the reserve and a paid run's cap

Cells that differ by model (e.g. 3 paid runs × 2 conditions plus 1 subscription run) go in `[[group]]` tables, each
with its own models, scenarios, conditions and repeats, instead of the top-level four. The groups' jobs merge
repeat-major (every group's repeat 1 first).

Each profile is checked as the run will load it (bg3harness.profiles.load). One whose status is "unsupported" is
refused; one that has never played live ("offline", the default) only with allow_unverified.

A scenario that isn't ready (bench.scenario.not_ready: a save start without its save, or start.not_ready) gets no
jobs: plan and run list it as skipped with the reason.

- plan: a dry run. The jobs as a table, the reserve, the budget left, the skipped scenarios and what play.py lacks
  for each condition; writes nothing.
- run: writes the plan (or keeps the same one) and runs it until done or a stop. `--runner session` (the default):
  each job is one run_session (adapters/session_run.py; the v2 scenario goes to the RunHost the caller passes in as
  `main(host=...)`: the harness has none to offer yet, so the CLI alone refuses it). `--runner legacy` (the fallback
  until Phase D): each scenario is resolved (bg3bench.scenario.resolve: the runner's v1 shape plus game_settings and
  timing; a goal that isn't one milestone is refused), written to runtime/matrix/<name>/scenarios/ and played by
  play.py run --scenario-file (adapters/legacy_cli.py); a condition play.py lacks is refused unless allow_gaps.
- resume: run, for a matrix that already has a queue (refused otherwise).
- status: the jobs' state from the queue: status, attempts, the last result.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from bg3harness import profiles

from bg3bench import matrix, scenario
from bg3bench.adapters import legacy_cli, play_flags, session_run

BUDGET = "runtime/budget.json"
SESSIONS = Path("runtime/sessions")
SCENARIOS = Path("packages/bench/scenarios")  # the v2 set
GROUP_KEYS = ("models", "scenarios", "conditions", "repeats")


class MatrixFileError(ValueError):
    pass


def _list(raw: dict, key: str, path: Path) -> list[str]:
    v = raw.get(key)
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise MatrixFileError(f"{path}: {key} must be a non-empty list of strings")
    return v


def load_profile(path: Path, allow_unverified: bool = False) -> dict:
    """A client profile (profiles/*.toml) as a matrix model: {"slug", "client", "effort", "profile"}."""
    try:
        p = profiles.load(path)
    except OSError as e:
        raise MatrixFileError(f"{path}: {e}") from None
    except profiles.ProfileError as e:  # it names the file
        raise MatrixFileError(str(e)) from None
    if p.status == "unsupported" or (p.status != "verified" and not allow_unverified):
        why = "" if p.status == "unsupported" else " (allow_unverified = true runs it)"
        raise MatrixFileError(f"{path}: profile status {p.status!r}{why}")
    return {"slug": p.model, "client": p.client, "effort": p.effort, "profile": str(path)}


def _scenarios(raw: dict, path: Path, repo: Path) -> list[str]:
    """One grid's (the top level's or a [[group]]'s) repeats and scenarios, checked; its scenarios."""
    repeats = raw.get("repeats", 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise MatrixFileError(f"{path}: repeats must be an integer >= 1")
    scenarios = _list(raw, "scenarios", path)
    for s in scenarios:
        if not (repo / SCENARIOS / f"{s}.json").is_file():
            raise MatrixFileError(f"{path}: no scenario {s!r} ({SCENARIOS}/{s}.json)")
    return scenarios


def _load(name: str, path: Path, repo: Path) -> dict:
    try:
        return scenario.load(repo / SCENARIOS / f"{name}.json")
    except scenario.ScenarioError as e:
        raise MatrixFileError(f"{path}: {e}") from None


def resolved(m: dict) -> dict[str, dict]:
    """Each scenario with jobs as the legacy runner reads it (scenario.resolve); a goal it can't stop at raises."""
    try:
        return {s: scenario.resolve(sc) for s, sc in m["scenarios"].items() if s not in m["skipped"]}
    except scenario.ScenarioError as e:
        raise MatrixFileError(f"{m['path']}: {e}") from None


def _group(raw: dict, path: Path, repo: Path, max_cost: dict, allow_unverified: bool) -> tuple[list[dict], list[str]]:
    """One grid's jobs and conditions (after _scenarios checked it)."""
    scenarios, repeats = raw["scenarios"], raw.get("repeats", 1)
    models = [load_profile(repo / p, allow_unverified) for p in _list(raw, "models", path)]
    conds = _list(raw, "conditions", path)
    for c in conds:
        play_flags.entry(c)  # a malformed condition id raises here
    try:
        jobs = matrix.plan(models, scenarios, conds, repeats, max_cost={k: float(v) for k, v in max_cost.items()})
    except ValueError as e:
        raise MatrixFileError(f"{path}: {e}") from None
    return jobs, conds


def load(path: str | Path, repo: Path) -> dict:
    """The matrix file -> {"name", "path", "jobs", "budget", "conditions", "allow_gaps", "scenarios", "skipped"}
    (scenarios: name -> the v2 scenario, scenario.load; skipped: name -> why it isn't ready, its jobs left out);
    checks profiles and scenarios."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MatrixFileError(f"{path}: {e}") from None
    name = raw.get("name")
    if not isinstance(name, str) or not name or name != matrix._slug(name):
        raise MatrixFileError(f"{path}: name must be a non-empty slug")
    budget = raw.get("budget", {})
    max_cost = budget.get("max_cost", {})
    if not isinstance(max_cost, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in max_cost.values()
    ):
        raise MatrixFileError(f"{path}: budget.max_cost maps scenarios to USD > 0")
    groups = raw.get("group")
    if groups is None:
        groups = [raw]
    elif (
        not isinstance(groups, list)
        or not groups
        or any(k in raw for k in GROUP_KEYS)
        or not all(isinstance(g, dict) for g in groups)
    ):
        raise MatrixFileError(f"{path}: give either [[group]] tables or top-level {', '.join(GROUP_KEYS)}")
    named = {s for g in groups for s in _scenarios(g, path, repo)}
    if unknown := sorted(set(max_cost) - named):
        raise MatrixFileError(f"{path}: budget.max_cost names scenarios not in the matrix: {unknown}")
    loaded = {s: _load(s, path, repo) for s in sorted(named)}
    skipped = {s: why for s, sc in loaded.items() if (why := scenario.not_ready(sc))}
    jobs, conds = [], []
    for g in groups:
        g_jobs, g_conds = _group(g, path, repo, max_cost, raw.get("allow_unverified") is True)
        jobs += g_jobs
        conds += [c for c in g_conds if c not in conds]
    if len({j["id"] for j in jobs}) != len(jobs):
        raise MatrixFileError(f"{path}: duplicate jobs: two groups share a model, scenario and condition")
    jobs = [j for j in jobs if j["scenario"] not in skipped]
    jobs.sort(key=lambda j: j["repeat"])  # stable: repeat-major across groups
    for j in jobs:
        if j["client"] in matrix.PAID_CLIENTS and j["max_cost"] is None:
            raise MatrixFileError(f"{path}: {j['client']} spends money: give budget.max_cost for {j['scenario']}")
    return {
        "name": name,
        "path": path,
        "jobs": jobs,
        "budget": repo / budget.get("file", BUDGET),
        "conditions": conds,
        "allow_gaps": bool(raw.get("allow_gaps", False)),
        "scenarios": loaded,
        "skipped": skipped,
    }


def write_scenarios(root: Path, resolved: dict[str, dict]) -> dict[str, Path]:
    """Each resolved scenario as root/scenarios/<name>.json (what play.py run --scenario-file reads)."""
    (root / "scenarios").mkdir(parents=True, exist_ok=True)
    files = {}
    for name, sc in resolved.items():
        files[name] = root / "scenarios" / f"{name}.json"
        files[name].write_text(json.dumps(sc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return files


def _table(rows: list[list[str]], out: TextIO) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip(), file=out)


def _usd(x: float | None) -> str:
    return "-" if x is None else f"${x:.2f}"


def print_plan(m: dict, out: TextIO) -> None:
    jobs = m["jobs"]
    paid = [j for j in jobs if j["client"] in matrix.PAID_CLIENTS]
    # max_cost: the run's cap; reserve: what the budget holds back for it (a subscription run is never charged)
    rows = [["job", "client", "effort", "profile", "scenario", "condition", "max_cost", "reserve"]]
    rows += [
        [
            j["id"],
            j["client"],
            j["effort"] or "-",
            Path(j["profile"]).name if j["profile"] else "-",
            j["scenario"],
            j["condition"],
            _usd(j["max_cost"]),
            _usd(j["max_cost"]) if j in paid else "-",
        ]
        for j in jobs
    ]
    _table(rows, out)
    reserve = sum(j["max_cost"] for j in paid)
    print(f"\n{len(jobs)} jobs ({len(paid)} paid), reserve {_usd(reserve)}", file=out)
    try:
        b = matrix.read_budget(m["budget"])
        print(f"budget {m['budget']}: {_usd(b['spent'])} of {_usd(b['cap'])} spent", file=out)
    except (OSError, ValueError) as e:
        print(f"budget {m['budget']}: unreadable ({e}): run stops before its first job", file=out)
    for name, why in m["skipped"].items():
        print(f"scenario {name}: skipped, not ready: {why}", file=out)
    for c in m["conditions"]:
        gaps = play_flags.entry(c).gaps
        if gaps:
            how = "a pilot (allow_gaps)" if m["allow_gaps"] else "refused by run --runner legacy"
            print(f"condition {c}: {how}; play.py lacks " + "; ".join(gaps), file=out)
    for name, sc in m["scenarios"].items():
        if name in m["skipped"]:
            continue
        try:
            scenario.resolve(sc)
        except scenario.ScenarioError as e:
            print(f"scenario {name}: refused by run --runner legacy ({e})", file=out)


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
    host: session_run.RunHost | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.matrix", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("plan", "run", "status", "resume"))
    ap.add_argument("--matrix", required=True, metavar="TOML")
    ap.add_argument("--runner", choices=("session", "legacy"), default="session")
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
    for name, why in m["skipped"].items():
        print(f"scenario {name}: skipped, not ready: {why}", file=out)
    if not m["jobs"]:
        print(f"refusing: matrix {m['name']} has no job: no scenario of it is ready", file=sys.stderr)
        return 2
    if run_one is None and args.runner == "session":
        if host is None:
            print("refusing: no run_session host (the harness has none yet): use --runner legacy", file=sys.stderr)
            return 2
        run_one = session_run.SessionRun(m["name"], host, scenarios=m["scenarios"], sessions=repo / SESSIONS)
    elif run_one is None:
        try:
            flags, env = play_flags.table(m["conditions"], allow_gaps=m["allow_gaps"])
            files = write_scenarios(root, resolved(m))
        except ValueError as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 2
        run_one = legacy_cli.LegacyCli(m["name"], flags, scenario_files=files, condition_env=env, repo=repo)
    stopped = (lambda: getattr(host, "stopped", False) is True) if host is not None else None  # the live host's stop
    mx = matrix.Matrix(root, run_one, m["budget"], now=now, sleep=sleep, stopped=stopped)
    try:
        mx.create(m["name"], m["jobs"])
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    stop = mx.run()
    print_status(mx, out)
    print(f"\nstopped: {stop}", file=out)
    return 0 if stop == matrix.FINISHED else 1
