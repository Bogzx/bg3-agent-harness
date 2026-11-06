"""The matrix CLI: `python -m bg3bench.matrix plan|run|status|resume --matrix <file.toml> [--runner session|legacy]`.

The matrix file (paths relative to the repo):

    name = "night1"                                   # runtime/matrix/<name>/ holds the queue and progress.json
    models = ["profiles/opus-5.5-pi.toml"]            # client profiles: client, model, effort
    scenarios = ["nautiloid"]                         # packages/bench/scenarios/<name>.json (v2)
    conditions = ["rules"]
    repeats = 3
    allow_gaps = false                                # true: a pilot with conditions a run can't play exactly yet
    allow_unverified = false                          # true: profiles whose status is "offline" (never played live)
    order = "repeat"                                  # or "cell": every repeat of a cell before the next cell
    ticket = "required"                               # optional: each run takes a ticket from the site (tickets.py)

    [budget]
    file = "runtime/budget.json"                      # the machine's shared budget (the default)
    shared = ["runtime/budget-anthropic.json"]        # optional: budgets this matrix also counts against
    max_cost = { nautiloid = 5.0 }                    # USD per run of a scenario: the reserve and a paid run's cap

With `shared`, `file` is this matrix's own share: a job starts only if it fits every file, and its cost goes into
every file (bench.matrix).

Cells that differ by model (e.g. 3 paid runs × 2 conditions plus 1 subscription run) go in `[[group]]` tables, each
with its own models, scenarios, conditions and repeats, instead of the top-level four. The groups' jobs merge
repeat-major (every group's repeat 1 first). A group may have a `[group.budget]` of its own: `max_cost` for its
scenarios (over the top level's) and `shared`, budget files its jobs also count against (on top of `file` and the
top level's `shared`): a matrix of several models gives each model its own reserves and its own API's budgets.

`order = "cell"` runs every repeat of a (model, scenario, condition) cell before the next cell, the cells in the file's
order (groups, then models, scenarios, conditions as listed): a budget that runs out leaves whole cells, the last
one short at most, instead of every cell short of its repeats.

Each profile is checked as the run will load it (bg3harness.profiles.load). One whose status is "unsupported" is
refused; one that has never played live ("offline", the default) only with allow_unverified.

A scenario that isn't ready (bench.scenario.not_ready: a save start without its save, or start.not_ready) gets no
jobs: plan and run list it as skipped with the reason.

`ticket = "required"` makes every attempt take a ticket from the site ($BG3BENCH_SITE) with the site token from its
file: a suite whose release has `tickets` takes only such runs. Without the key the matrix plays as it always did, and
its runs can't be submitted to that suite. A refused ticket stops the matrix before the run, as a refused start does.

- plan: a dry run. The jobs as a table, the reserve, the budget left, the skipped scenarios and what a session run
  (conditions.session_gaps, per client) and play.py lack for each condition; writes nothing.
- run: writes the plan (or keeps the same one) and runs it until done or a stop. `--runner session` (the default):
  each job is one run_session (adapters/session_run.py; the v2 scenario goes to the RunHost the caller passes in as
  `main(host=...)`: the harness has none to offer yet, so the CLI alone refuses it); a job whose condition the session
  can't play exactly through its client is refused unless allow_gaps, and its runs are labelled pilot in
  result.json. `--runner legacy` (the fallback
  until Phase D): each scenario is resolved (bg3bench.scenario.resolve: the runner's v1 shape plus game_settings and
  timing; a goal that isn't one milestone is refused), written to runtime/matrix/<name>/scenarios/ and played by
  play.py run --scenario-file (adapters/legacy_cli.py); a condition play.py lacks is refused unless allow_gaps.
- resume: run, for a matrix that already has a queue (refused otherwise).
- status: the jobs' state from the queue: status, attempts, the last result.

`start-states [...]` (no matrix) records the save scenarios' start-state fingerprints on the caller's host without a
model (bg3bench.submission.start_states).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO, cast

from bg3harness import diskguard, profiles

from bg3bench import conditions, matrix, scenario, tickets
from bg3bench.adapters import legacy_cli, play_flags, session_run
from bg3bench.submission import start_states

BUDGET = "runtime/budget.json"
SESSIONS = Path("runtime/sessions")
SCENARIOS = Path("packages/bench/scenarios")  # the v2 set
GROUP_KEYS = ("models", "scenarios", "conditions", "repeats")
ORDERS = ("repeat", "cell")
TICKET_REQUIRED = "required"
START_STATES = "start-states"


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


def _budget(budget: object, path: Path, where: str) -> tuple[dict, list[str]]:
    """A [budget] table's (the top level's or a group's) max_cost and shared, checked."""
    if not isinstance(budget, dict):
        raise MatrixFileError(f"{path}: {where} must be a table")
    if where != "budget" and (extra := sorted(set(budget) - {"max_cost", "shared"})):
        raise MatrixFileError(f"{path}: {where} takes max_cost and shared only, not {', '.join(extra)}")
    max_cost = budget.get("max_cost", {})
    if not isinstance(max_cost, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in max_cost.values()
    ):
        raise MatrixFileError(f"{path}: {where}.max_cost maps scenarios to USD > 0")
    shared = budget.get("shared", [])
    if not isinstance(shared, list) or not all(isinstance(f, str) and f for f in shared):
        raise MatrixFileError(f"{path}: {where}.shared lists budget files")
    return max_cost, shared


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
    max_cost, shared = _budget(budget, path, "budget")
    own = budget.get("file", BUDGET)
    if own in shared or len(set(shared)) != len(shared):
        raise MatrixFileError(f"{path}: budget.shared lists a budget file twice")
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
    for i, g in enumerate(groups):
        g_max, g_shared = ({}, []) if g is raw else _budget(g.get("budget", {}), path, f"group[{i}].budget")
        if unknown := sorted(set(g_max) - set(g["scenarios"])):
            raise MatrixFileError(f"{path}: group[{i}].budget.max_cost names scenarios not in the group: {unknown}")
        if set(g_shared) & {own, *shared} or len(set(g_shared)) != len(g_shared):
            raise MatrixFileError(f"{path}: group[{i}].budget.shared lists a budget file twice")
        g_jobs, g_conds = _group(g, path, repo, {**max_cost, **g_max}, raw.get("allow_unverified") is True)
        for j in g_jobs if g_shared else ():
            j["shared"] = [str(repo / f) for f in g_shared]
        jobs += g_jobs
        conds += [c for c in g_conds if c not in conds]
    if len({j["id"] for j in jobs}) != len(jobs):
        raise MatrixFileError(f"{path}: duplicate jobs: two groups share a model, scenario and condition")
    jobs = [j for j in jobs if j["scenario"] not in skipped]
    order = raw.get("order", "repeat")
    if order not in ORDERS:
        raise MatrixFileError(f"{path}: order must be one of {', '.join(ORDERS)}")
    if order == "cell":
        cells = {(j["model"], j["scenario"], j["condition"]): i for i, j in reversed(list(enumerate(jobs)))}
        jobs.sort(key=lambda j: (cells[j["model"], j["scenario"], j["condition"]], j["repeat"]))
    else:
        jobs.sort(key=lambda j: j["repeat"])  # stable: repeat-major across groups
    ticket = raw.get("ticket")
    if ticket not in (None, TICKET_REQUIRED):
        raise MatrixFileError(f"{path}: ticket must be {TICKET_REQUIRED!r} or left out")
    for j in jobs:
        if j["client"] in matrix.PAID_CLIENTS and j["max_cost"] is None:
            raise MatrixFileError(f"{path}: {j['client']} spends money: give budget.max_cost for {j['scenario']}")
    return {
        "name": name,
        "path": path,
        "jobs": jobs,
        "budget": repo / own,
        "shared": [repo / f for f in shared],
        "conditions": conds,
        "allow_gaps": bool(raw.get("allow_gaps", False)),
        "scenarios": loaded,
        "skipped": skipped,
        "ticket": ticket,
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
    # max_cost: a paid run's cap; reserve: what the budget holds back for it. A subscription run is never charged and
    # not capped by it (run_host.options_for: only its scenario's own caps.max_cost), so both are "-"
    rows = [["job", "client", "effort", "profile", "scenario", "condition", "max_cost", "reserve"]]
    rows += [
        [
            j["id"],
            j["client"],
            j["effort"] or "-",
            Path(j["profile"]).name if j["profile"] else "-",
            j["scenario"],
            j["condition"],
            _usd(j["max_cost"]) if j in paid else "-",
            _usd(j["max_cost"]) if j in paid else "-",
        ]
        for j in jobs
    ]
    _table(rows, out)
    reserve = sum(j["max_cost"] for j in paid)
    print(f"\n{len(jobs)} jobs ({len(paid)} paid), reserve {_usd(reserve)}", file=out)
    for path in dict.fromkeys([m["budget"], *m["shared"], *(Path(f) for j in jobs for f in j.get("shared", ()))]):
        try:
            b = matrix.read_budget(path)
            print(f"budget {path}: {_usd(b['spent'])} of {_usd(b['cap'])} spent", file=out)
        except (OSError, ValueError) as e:
            print(f"budget {path}: unreadable ({e}): run stops before its first job", file=out)
    if m["ticket"]:
        print(f"each run takes a ticket from the site (${tickets.SITE_ENV}); a refused one stops the matrix", file=out)
    for name, why in m["skipped"].items():
        print(f"scenario {name}: skipped, not ready: {why}", file=out)
    for c, client in sorted({(j["condition"], j["client"]) for j in m["jobs"]}):
        if gaps := conditions.session_gaps(c, client):
            how = "a pilot (allow_gaps)" if m["allow_gaps"] else "refused by run"
            print(f"condition {c} through {client}: {how}; a session run lacks " + "; ".join(gaps), file=out)
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
            if last["passed"] is None:  # a maximise run: its score, not a pass
                verdict = "score " + ("?" if last.get("score") is None else f"{last['score']:+g}")
            else:
                verdict = "pass" if last["passed"] else "fail"
            text = f"{verdict} {_usd(last['cost_usd'])} {last['run_id']}"
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
    environ: Mapping[str, str] = os.environ,
    urlopen: tickets.Open | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [START_STATES]:
        live = cast(start_states.LiveHost, host) if host is not None else None
        return start_states.main(argv[1:], repo=repo, host=live, sleep=sleep, out=out)
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
        gapped = sorted({j["condition"] for j in m["jobs"] if conditions.session_gaps(j["condition"], j["client"])})
        if gapped and not m["allow_gaps"]:
            print(
                f"refusing: a session run can't play {', '.join(gapped)} exactly (bench plan says why): set "
                "allow_gaps = true for a pilot",
                file=sys.stderr,
            )
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
    ticketed: tickets.Ticketed | None = None
    if m["ticket"]:
        if getattr(run_one, "run_dir", None) is None:  # the legacy runner: its run dir is play.py's to choose
            print("refusing: a ticketed matrix needs --runner session", file=sys.stderr)
            return 2
        try:
            run_one = ticketed = tickets.from_env(cast(tickets.Inner, run_one), repo, environ, urlopen, now)
        except ValueError as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 2
    stopped = (lambda: getattr(host, "stopped", False) is True) if host is not None else None  # the live host's stop
    runtime = repo / "runtime" if (repo / "runtime").is_dir() else repo  # where the run's files go

    def disk(job: dict) -> str | None:  # the job's recording and video must fit (bg3harness.diskguard)
        return diskguard.start_refusal(runtime, m["scenarios"][job["scenario"]]["caps"]["run_hours"] * 60)

    mx = matrix.Matrix(root, run_one, m["budget"], now=now, sleep=sleep, stopped=stopped, disk=disk, shared=m["shared"])
    try:
        mx.create(m["name"], m["jobs"])
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    if ticketed is not None:
        ticketed.close_interrupted(mx.state())
    stop = mx.run()
    print_status(mx, out)
    print(f"\nstopped: {stop}", file=out)
    return 0 if stop == matrix.FINISHED else 1
