"""Matrix runner: models × scenarios × conditions × repeats as one persistent queue, run unattended, one at a time.

Files in the matrix folder (runtime/matrix/<name>/):

- `queue.jsonl`: append-only. Line 1 is the plan `{"type": "plan", "schema": 1, "name", "created", "jobs": [...]}`;
  then `{"type": "start", "job", "attempt", "t"}` and `{"type": "end", "job", "attempt", "t", "result"}` per attempt.
  The state is the fold of the file, so a crash of the matrix runner itself loses nothing: on the next start an
  attempt that started and never ended is closed as invalid (`interrupted`) and the job is run again.
- `progress.json`: rewritten after every change, for the dashboard.

The budget file (global, may be shared by several matrices) is `{"cap_usd": X, "spent_usd": Y}`; each run's billed
cost is added as it ends. A job does not start when `spent + its reserve > cap` (reserve = the scenario's per-run
`max_cost`, 0 when it has none: then the overshoot is at most one run). A run whose cost is unknown stops the
matrix: the budget can't be enforced past it until a person fixes `spent_usd`.

Invalid runs (results.validity: runner error, harness failure, operator) are rerun, at most `max_reruns` (2) times
per job; then the job is `gave_up`. Order: repeat 1 of every (model, scenario, condition) first, then repeat 2, ...
so a stopped matrix has even coverage. `run_one(job) -> RunResult` does one run (adapters/legacy_cli.py today).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

SCHEMA = 1
MAX_RERUNS = 2
FINISHED, BUDGET, GUARD, COST_UNKNOWN = "finished", "budget", "guard", "cost unknown"


@dataclass(frozen=True)
class RunResult:
    run_id: str | None
    valid: bool
    passed: bool
    cost_usd: float | None
    invalid_reason: str | None = None


class RunOne(Protocol):
    def __call__(self, job: dict) -> RunResult: ...


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.+@-]+", "_", text)


def job_id(model: str, scenario: str, condition: str, repeat: int) -> str:
    return f"{_slug(model)}~{scenario}~{_slug(condition)}~r{repeat}"


def plan(
    models: Sequence[Mapping],
    scenarios: Sequence[str],
    conditions: Sequence[str],
    repeats: int,
    *,
    max_cost: Mapping[str, float | None] | None = None,
) -> list[dict]:
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


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_budget(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("cap_usd"), int | float) or not isinstance(data.get("spent_usd"), int | float):
        raise ValueError(f"{path}: needs numbers cap_usd and spent_usd")  # noqa: TRY004 - bad file content
    return data


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
        max_reruns: int = MAX_RERUNS,
        guard_poll_s: float = 60.0,
        guard_max_wait_s: float = 3600.0,
    ):
        self.root, self.run_one, self.budget = Path(root), run_one, Path(budget)
        self.now, self.sleep, self.guard = now, sleep, guard
        self.max_reruns, self.guard_poll_s, self.guard_max_wait_s = max_reruns, guard_poll_s, guard_max_wait_s
        self.queue = self.root / "queue.jsonl"
        self.progress_path = self.root / "progress.json"

    # ------------------------------------------------------------------ the queue file
    def create(self, name: str, jobs: Iterable[dict]) -> None:
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

    def state(self) -> dict[str, dict]:
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
        return "gave_up" if len(attempts) > self.max_reruns else "pending"

    # ------------------------------------------------------------------ progress and budget
    def _progress(self, jobs: dict[str, dict], state: str, current: dict | None = None, stop: str | None = None):
        results = [a["result"] for st in jobs.values() for a in st["attempts"] if a["result"] is not None]
        try:
            budget = read_budget(self.budget)
        except (OSError, ValueError):
            budget = {"cap_usd": None, "spent_usd": None}
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
                "cap_usd": budget["cap_usd"],
                "spent_usd": budget["spent_usd"],
            },
        )

    def _charge(self, cost: float) -> None:
        budget = read_budget(self.budget)
        _write_json(self.budget, {**budget, "spent_usd": round(budget["spent_usd"] + cost, 6)})

    def _end(self, job: dict, attempt: int, result: RunResult) -> None:
        self._append({"type": "end", "job": job["id"], "attempt": attempt, "t": self.now(), "result": asdict(result)})
        if result.cost_usd is not None:
            self._charge(result.cost_usd)

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
        """Run until every job is done or gave up, or a stop: returns FINISHED, BUDGET, COST_UNKNOWN or
        'guard: <why>'."""
        jobs = self.state()
        for st in jobs.values():
            if st["status"] == "interrupted":
                # the matrix runner died mid-run: what that run spent is unknown unless the job has a reserve
                att = st["attempts"][-1]["attempt"]
                self._end(st["job"], att, RunResult(None, False, False, st["job"]["max_cost"], "interrupted"))
                if st["job"]["max_cost"] is None:
                    self._progress(self.state(), "stopped", stop=COST_UNKNOWN)
                    return COST_UNKNOWN
        while True:
            jobs = self.state()
            todo = [st for st in jobs.values() if st["status"] == "pending"]
            if not todo:
                self._progress(jobs, "finished", stop=FINISHED)
                return FINISHED
            st = todo[0]
            job = st["job"]
            budget = read_budget(self.budget)
            if budget["spent_usd"] + (job["max_cost"] or 0.0) > budget["cap_usd"]:
                self._progress(jobs, "stopped", stop=BUDGET)
                return BUDGET
            if (why := self._wait_for_guard(jobs, job)) is not None:
                self._progress(jobs, "stopped", stop=f"{GUARD}: {why}")
                return f"{GUARD}: {why}"
            attempt = len(st["attempts"]) + 1
            self._append({"type": "start", "job": job["id"], "attempt": attempt, "t": self.now()})
            self._progress(jobs, "running", current={**job, "attempt": attempt})
            try:
                result = self.run_one(job)
            except Exception as e:  # noqa: BLE001 - any adapter failure is an invalid attempt, cost unknown
                result = RunResult(None, False, False, None, f"error: {type(e).__name__}: {e}")
            self._end(job, attempt, result)
            if result.cost_usd is None:
                self._progress(self.state(), "stopped", stop=COST_UNKNOWN)
                return COST_UNKNOWN
