# pyright: basic
"""Matrix runner: models × scenarios × conditions × repeats as one persistent queue, run unattended, one at a time.

Files in the matrix folder (runtime/matrix/<name>/):

- `queue.jsonl`: append-only. Line 1 is the plan `{"type": "plan", "schema": 1, "name", "created", "jobs": [...]}`;
  then `{"type": "start", "job", "attempt", "t"}` and `{"type": "end", "job", "attempt", "t", "result"}` per attempt.
  The state is the fold of the file, so a crash of the matrix runner itself loses nothing: on the next start an
  attempt that started and never ended is closed as invalid (`interrupted`) and the job is run again.
- `progress.json`: rewritten after every change, for the dashboard.

The budget file is the harness's (bg3harness.spend, which reads and updates it here too): one per machine,
runtime/budget.json `{"cap": USD, "spent": USD}`, shared by every matrix and every paid play.py run, updated under the
same lock (`budget.json.lock`). A matrix may also name `shared` budget files: then its own file caps this matrix
and each shared one caps everything that charges it (several matrices sharing one API key's money, each with its
own share): every file is checked before a job and charged when it ends. A job does not
start when `spent + its reserve > cap` of any of them (reserve = a paid client's per-run `max_cost` for the scenario, 0 when it has
none: then the overshoot is at most one run; 0 for a subscription client, never charged); a missing or broken file
stops the matrix (`budget`), also one gone or broken when a run's cost is added (the end record keeps the cost). A run's cost is added as it ends unless the runner already settled it (`RunResult.charged`:
play.py adds a paid run's cost itself, and subscription runs aren't billed). A paid run whose cost is unknown stops
the matrix: the budget can't be enforced past it until a person fixes `spent`. A subscription run is never charged,
not even one that crashed or was interrupted.

Invalid runs (results.validity: runner error, harness failure, operator) are rerun, at most `max_reruns` (2) times
per job; then the job is `gave_up`. Order: the plan's (repeat 1 of every (model, scenario, condition) first, then
repeat 2, ... so a stopped matrix has even coverage; or cell by cell, matrix_cli's `order = "cell"`), a rerun at once. `run_one(job) -> RunResult` does one run (adapters/legacy_cli.py today).
`stopped()` (the host's operator stop: a signal or runtime/STOP) is asked before each run: once it says so, no new
run starts and the matrix stops (`stopped`); the run it interrupted is recorded by its own result.
`disk(job)` (bg3harness.diskguard.start_refusal on the runtime filesystem) is asked before each run too: too little
free space for the job's recording and video stops the matrix like the budget does (`disk: <why>`, no attempt
recorded: a later `bench run` resumes it).
A start the host refused before any play (`RunResult.refused`: the runner's own launch refusal, e.g. Steam not
offline; no game, no agent, $0) is recorded but is not a run attempt: it never counts toward `max_reruns` and the
matrix stops (`refused: <why>`), so a later `bench run` resumes the job once a person has fixed the host.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from bg3harness import spend

SCHEMA = 1
MAX_RERUNS = 2
PAID_CLIENTS = spend.PAID_CLIENTS  # the clients billed per token (the others run on subscriptions)
FINISHED, BUDGET, GUARD, COST_UNKNOWN, STOPPED = "finished", "budget", "guard", "cost unknown", "stopped"
DISK, REFUSED = "disk", "refused"


@dataclass(frozen=True)
class RunResult:
    run_id: str | None
    valid: bool
    passed: bool | None  # None: a maximise scenario's run, which has no pass (its score is `score`)
    cost_usd: float | None
    invalid_reason: str | None = None
    charged: bool = False  # the runner already settled cost_usd with the budget file (or it isn't billed)
    score: float | None = None  # a maximise scenario's gain (results.py score.gain)
    refused: bool = False  # the host refused the start before any play: not a run attempt (invalid_reason: why)
    invalid_cause: list[dict[str, Any]] | None = None  # an invalid run's result.json invalid_cause


class RunOne(Protocol):
    def __call__(self, job: dict[str, Any]) -> RunResult: ...


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.+@-]+", "_", text)


def job_id(model: str, scenario: str, condition: str, repeat: int) -> str:
    return f"{_slug(model)}~{scenario}~{_slug(condition)}~r{repeat}"


def plan(
    models: Sequence[Mapping[str, Any]],
    scenarios: Sequence[str],
    conditions: Sequence[str],
    repeats: int,
    *,
    max_cost: Mapping[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    """The jobs, repeat-major. A model is {"slug", "client"?, "effort"?, "profile"?}; max_cost per scenario is the
    reserve (and a profile job's per-run cap)."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    max_cost = max_cost or {}
    jobs = [
        {
            "id": job_id(m["slug"], s, c, r),
            "model": m["slug"],
            "client": m.get("client", "claude"),
            "effort": m.get("effort"),
            "profile": m.get("profile"),
            "scenario": s,
            "condition": c,
            "repeat": r,
            "max_cost": max_cost.get(s),
        }
        for r in range(1, repeats + 1)
        for m in models
        for s in scenarios
        for c in conditions
    ]
    if len({j["id"] for j in jobs}) != len(jobs):
        raise ValueError("duplicate jobs: a model, scenario or condition is listed twice")
    return jobs


def reserve(job: Mapping[str, Any]) -> float:
    """What the budget holds back before the job starts: its max_cost for a paid client, 0 for a subscription one."""
    return (job["max_cost"] or 0.0) if job["client"] in PAID_CLIENTS else 0.0


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_budget(path: Path) -> dict:
    """The harness's budget.json (spend.read_budget: {"cap", "spent", ...}); a missing file raises too, since the matrix
    never runs without a budget."""
    b = spend.read_budget(path)
    if b is None:
        raise spend.BudgetError(f"{path}: no budget file")
    return b


class Matrix:
    def __init__(
        self,
        root: str | Path,
        run_one: RunOne,
        budget: str | Path,
        *,
        now: Callable[[], float],
        sleep: Callable[[float], None],
        guard: Callable[[], str | None] | None = None,
        stopped: Callable[[], bool] | None = None,
        disk: Callable[[dict[str, Any]], str | None] | None = None,
        shared: Sequence[str | Path] = (),
        max_reruns: int = MAX_RERUNS,
        guard_poll_s: float = 60.0,
        guard_max_wait_s: float = 3600.0,
    ):
        self.root, self.run_one, self.budget = Path(root), run_one, Path(budget)
        self.budgets = [self.budget, *(Path(p) for p in shared)]  # the matrix's own first: each is checked, charged
        self.now, self.sleep, self.guard, self.stopped, self.disk = now, sleep, guard, stopped, disk
        self.max_reruns, self.guard_poll_s, self.guard_max_wait_s = max_reruns, guard_poll_s, guard_max_wait_s
        self.queue = self.root / "queue.jsonl"
        self.progress_path = self.root / "progress.json"

    # ------------------------------------------------------------------ the queue file
    def create(self, name: str, jobs: Iterable[dict[str, Any]]) -> None:
        """Write the plan. An existing queue with the same jobs is kept (resume); a different one is refused."""
        jobs = list(jobs)
        if self.queue.exists():
            if self._plan()["jobs"] != jobs:
                raise ValueError(f"{self.queue} holds another plan: use a new matrix name")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._append({"type": "plan", "schema": SCHEMA, "name": name, "created": self.now(), "jobs": jobs})

    def _append(self, rec: dict) -> None:
        with self.queue.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _records(self) -> list[dict]:
        # a torn last line (crash while writing) is ignored: its attempt reads as started-not-ended or not started
        out = []
        for line in self.queue.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _plan(self) -> dict:
        recs = self._records()
        if not recs or recs[0].get("type") != "plan" or recs[0].get("schema") != SCHEMA:
            raise ValueError(f"{self.queue}: no v{SCHEMA} plan on line 1")
        return recs[0]

    def state(self) -> dict[str, dict[str, Any]]:
        """job id -> {"job", "attempts": [{"attempt", "result" or None}], "status"}."""
        recs = self._records()
        jobs = {j["id"]: {"job": j, "attempts": []} for j in self._plan()["jobs"]}
        for rec in recs[1:]:
            st = jobs.get(rec.get("job"))
            if st is None:
                continue
            if rec["type"] == "start":
                st["attempts"].append({"attempt": rec["attempt"], "result": None})
            elif rec["type"] == "end" and st["attempts"] and st["attempts"][-1]["attempt"] == rec["attempt"]:
                st["attempts"][-1]["result"] = rec["result"]
        for st in jobs.values():
            st["status"] = self._status(st["attempts"])
        return jobs

    def _status(self, attempts: list[dict]) -> str:
        if attempts and attempts[-1]["result"] is None:
            return "interrupted"
        if any(a["result"]["valid"] for a in attempts):
            return "done"
        runs = [a for a in attempts if not a["result"].get("refused")]
        return "gave_up" if len(runs) > self.max_reruns else "pending"

    # ------------------------------------------------------------------ progress and budget
    def _progress(self, jobs: dict[str, dict], state: str, current: dict | None = None, stop: str | None = None):
        results = [r for st in jobs.values() for a in st["attempts"] if (r := a["result"]) and not r.get("refused")]
        try:
            budget = read_budget(self.budget)
        except (OSError, ValueError):
            budget = {"cap": None, "spent": None}
        counts = {s: sum(1 for st in jobs.values() if st["status"] == s) for s in ("done", "pending", "gave_up")}
        _write_json(
            self.progress_path,
            {
                "schema": SCHEMA,
                "name": self._plan()["name"],
                "updated": self.now(),
                "state": state,
                "stop_reason": stop,
                "current": current,
                "jobs": len(jobs),
                **counts,
                "runs": len(results),
                "invalid_runs": sum(1 for r in results if not r["valid"]),
                "passed": sum(1 for r in results if r["valid"] and r["passed"]),
                "cap_usd": budget["cap"],
                "spent_usd": budget["spent"],
            },
        )

    def _end(self, job: dict, attempt: int, result: RunResult) -> bool:
        """Records the attempt's end and adds its cost to the budget unless charged; False when the cost couldn't be
        added (the budget file gone or broken since the run started: the end record keeps the cost)."""
        self._append({"type": "end", "job": job["id"], "attempt": attempt, "t": self.now(), "result": asdict(result)})
        if result.cost_usd and not result.charged:
            ok = True
            for path in self.budgets:  # every one, even after a failure: each keeps what it can
                try:
                    b = spend.add_spend(path, result.cost_usd, self.now(), matrix=self._plan()["name"], job=job["id"])
                except (OSError, ValueError):
                    b = None
                ok = ok and b is not None
            return ok
        return True

    def _wait_for_guard(self, jobs: dict, job: dict) -> str | None:
        if self.guard is None:
            return None
        waited = 0.0
        while (why := self.guard()) is not None:
            if waited >= self.guard_max_wait_s:
                return why
            self._progress(jobs, "waiting", current=job, stop=None)
            self.sleep(self.guard_poll_s)
            waited += self.guard_poll_s
        return None

    # ------------------------------------------------------------------ the loop
    def run(self) -> str:
        """Run until every job is done or gave up, or a stop: returns FINISHED, BUDGET, COST_UNKNOWN, STOPPED,
        'disk: <why>', 'refused: <why>' or 'guard: <why>'."""
        jobs = self.state()
        for st in jobs.values():
            if st["status"] == "interrupted":
                # the matrix runner died mid-run: what a paid run spent is unknown unless the job has a reserve
                job, att = st["job"], st["attempts"][-1]["attempt"]
                paid = job["client"] in PAID_CLIENTS
                cost = job["max_cost"] if paid else None
                if not self._end(job, att, RunResult(None, False, False, cost, "interrupted", charged=not paid)):
                    self._progress(self.state(), "stopped", stop=BUDGET)
                    return BUDGET
                if paid and cost is None:
                    self._progress(self.state(), "stopped", stop=COST_UNKNOWN)
                    return COST_UNKNOWN
        while True:
            jobs = self.state()
            todo = [st for st in jobs.values() if st["status"] == "pending"]
            if not todo:
                self._progress(jobs, "finished", stop=FINISHED)
                return FINISHED
            if self.stopped is not None and self.stopped():  # an operator stop: no new run (each would fail its start)
                self._progress(jobs, "stopped", stop=STOPPED)
                return STOPPED
            st = todo[0]
            job = st["job"]
            try:
                budgets = [read_budget(p) for p in self.budgets]
            except (OSError, ValueError):
                self._progress(jobs, "stopped", stop=BUDGET)
                return BUDGET
            if any(b["spent"] + reserve(job) > b["cap"] for b in budgets):
                self._progress(jobs, "stopped", stop=BUDGET)
                return BUDGET
            if self.disk is not None and (why := self.disk(job)) is not None:
                self._progress(jobs, "stopped", stop=f"{DISK}: {why}")
                return f"{DISK}: {why}"
            if (why := self._wait_for_guard(jobs, job)) is not None:
                self._progress(jobs, "stopped", stop=f"{GUARD}: {why}")
                return f"{GUARD}: {why}"
            attempt = len(st["attempts"]) + 1
            self._append({"type": "start", "job": job["id"], "attempt": attempt, "t": self.now()})
            self._progress(jobs, "running", current={**job, "attempt": attempt})
            try:
                result = self.run_one(job)
            except Exception as e:  # noqa: BLE001 - any adapter failure is an invalid attempt, a paid one's cost unknown
                why = f"error: {type(e).__name__}: {e}"
                result = RunResult(None, False, False, None, why, charged=job["client"] not in PAID_CLIENTS)
            if not self._end(job, attempt, result):
                self._progress(self.state(), "stopped", stop=BUDGET)
                return BUDGET
            if result.cost_usd is None and not result.charged:
                self._progress(self.state(), "stopped", stop=COST_UNKNOWN)
                return COST_UNKNOWN
            if result.refused:  # the next job would be refused the same way: stop until a person fixes the host
                self._progress(self.state(), "stopped", stop=f"{REFUSED}: {result.invalid_reason}")
                return f"{REFUSED}: {result.invalid_reason}"


if __name__ == "__main__":  # pragma: no cover - python -m bg3bench.matrix
    from bg3bench import matrix_cli

    raise SystemExit(matrix_cli.main())
