# pyright: basic
"""`runner/play.py arena-matrix --matrix <file.toml> [--plan | --table [--seed N]]`: an arena pairing matrix
(bg3bench.arena_matrix's file, plan, reserves and charges) played match by match on this machine's game.

- `--plan` prints the matches, each one's reserves and the estimate: what every budget file the matrix charges would
  hold back over the whole matrix (each paid side at its max_cost, its model's or its arena's: the most it can
  spend) against what the file has left. Writes nothing.
- no flag plays the queue (runtime/matrix/<name>/queue.jsonl: bench.matrix's file and fold, so a stopped matrix
  resumes from it) under one runner lock, one match at a time, in the plan's order: a pairing's two matches (the
  sides swapped, the same arena and seed) back to back, an invalid match rerun at once (at most matrix.MAX_RERUNS
  times, then the job gave up). Before each match: runtime/STOP stops the matrix (`stopped`); every budget file the
  match charges (the matrix's own, each side's model files) must hold its reserves (arena_matrix.reserves:
  spent + reserve <= cap; a missing or broken file stops it too: `budget`), and runtime/budget.json the match's cap
  (live.play refuses a match it can't hold and charges it; the matrix only checks it); the disk guard
  (bg3harness.diskguard, the arena's wall cap) must pass (`disk: <why>`). The match plays through
  bg3harness.arena.live.play with each paid side capped at its reserve (side_caps) and the match at their sum
  (max_cost), and ends with its game stopped, so the next one, a rerun too, launches its own. As it ends it is
  graded from its facts (bg3bench.arena.grade) and both sides' costs go to the matrix's files
  (arena_matrix.charges). A match refused before it played (live.play's ArenaSetupError: an API key unset,
  runtime/budget.json short, a game up the runner didn't launch, ...) is recorded, isn't an attempt, and stops the
  matrix (`refused: <why>`). A match the matrix runner died in (started, never ended) is closed as `interrupted` on
  the next start, each paid side charged what its events in the attempt's run dir read (read_costs: what each pi
  process billed before it died; $0 for a side whose process never started, as when it was killed while the game
  loaded), and played again; without a sessions dir to read, each paid side's whole reserve. That charge goes to
  runtime/budget.json too, unless the run dir's arena.json says live.play got to its end and charged it (D822). The
  process that died leaves progress.json `stopped` ("aborted: ..."), never `running`. A match that ended
  over_budget is not rerun (`over_budget`: a rerun would spend as much again). A paid side's cost unknown (the
  harness failed mid-match) stops the matrix (`cost unknown`). Each played match's pins (its arena.json's, live.play:
  run_host.machine_pins) are recorded in its result and compared with the matrix's first recorded pins
  (bg3bench.pins.pin_drift; REVIEW-16 A2): a match that played with others is recorded with `version_drift`, invalid
  (`version_drift: ...`, so it plays again once the pins are restored; REVIEW-18 5a; it isn't one of the job's
  reruns and the table never counts it, REVIEW-18g), and stops the matrix (`drift: ...`); with `allow_drift = true`
  in the matrix file it stays valid, flagged, and the matrix goes on. A match whose pins don't read counts as drifted
  once a first match recorded them.
- A queue line `{"type": "operator_stop", "job", "attempt", "t", "by", "why"}` closes the matrix: a start refuses it
  (exit 2) before anything is charged or played, because an open attempt would otherwise be charged again and replayed.
  It is for a runner killed after a match whose spend was then written back by hand; it is no end record, and the
  attempt stays unended in the queue. A deliberate replay is a new matrix name.
- `--table` grades every played match (the last attempt of each, its facts and rolls) and prints bg3bench.arena's
  results table (pairings, pairs, side A, models) as JSON.

Exit 0 when the matrix finished, 1 when it stopped, 2 refused before it began.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import _harness  # noqa: F401  (packages/harness/src, packages/bench/src and bridge/src on sys.path)
from bg3bench import arena as barena
from bg3bench import arena_matrix, matrix
from bg3bench import pins as bpins
from bg3bench.matrix_cli import MatrixFileError
from bg3harness import diskguard, events, spend
from bg3harness.arena import ArenaSpec, Side
from bg3harness.arena.match import ArenaSetupError
from bg3harness.runner import env as renv

SIDES = ("A", "B")
OVER_BUDGET = "over_budget"
OPERATOR_STOP = "operator_stop"  # a queue line that closes the matrix (module doc)
DRIFT = "drift"
SEED = 2026  # the table's bootstrap seed


@dataclass(frozen=True)
class Played:
    run_dir: Path
    invalid: str | None  # the harness's own outcome (ArenaOutcome.invalid)
    costs: dict[str, float | None]  # each side's cost, as charged to runtime/budget.json


class RefusedError(Exception):
    """The match was refused before the game started (nothing played, nothing spent)."""


Play = Callable[[dict[str, Any], str], Played]  # (job, run name) -> the match; raises RefusedError


def side_caps(job: Mapping[str, Any]) -> dict[str, float]:
    """Each paid side's cap (its reserve): its model's max_cost_models entry, else its arena's max_cost."""
    paid = [s for s in SIDES if job["sides"][s]["client"] in matrix.PAID_CLIENTS]
    return {s: float(arena_matrix.side_cap(job, s) or 0.0) for s in paid}


def drift_invalid(result: Mapping[str, Any]) -> bool:
    """The match played with other pins than the matrix's first and the drift wasn't allowed (it plays again)."""
    return str(result.get("invalid_reason") or "").startswith("version_drift")


def status(attempts: list[dict]) -> str:
    """A job's state from its attempts: interrupted, done (a valid match), over_budget (its last match ended at its
    money cap: never rerun), gave_up (more than MAX_RERUNS invalid matches) or pending."""
    if attempts and attempts[-1]["result"] is None:
        return "interrupted"
    results = [a["result"] for a in attempts]
    if any(r["valid"] for r in results):
        return "done"
    runs = [r for r in results if not r.get("refused") and not drift_invalid(r)]  # REVIEW-18g: a drift isn't a rerun
    if runs and runs[-1].get("invalid_reason") == OVER_BUDGET:
        return OVER_BUDGET
    return "gave_up" if len(runs) > matrix.MAX_RERUNS else "pending"


def job_arena(job: Mapping[str, Any], arena: Mapping[str, Any]) -> dict[str, Any]:
    """The arena as the job plays it: on the job's seed (arena_matrix.plan: the arena's + repeat - 1). A queue planned
    before jobs had a seed played the arena's."""
    return {**arena, "seed": job.get("seed", arena["seed"])}


def graded(job: Mapping[str, Any], arena: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    """bg3bench.arena.grade of the match in run_dir (its facts.jsonl, rolls.jsonl) against the job's seed, with its
    pairing id."""

    def rows(name: str) -> list[dict] | None:
        try:
            text = (run_dir / name).read_text(encoding="utf-8")
        except OSError:
            return None
        return [json.loads(x) for x in text.splitlines() if x.strip()]

    g = barena.grade(rows("facts.jsonl") or [], job_arena(job, arena), rolls=rows("rolls.jsonl"))
    return {**g, "pairing": job["pairing"]}


def match_pins(run_dir: Path) -> dict[str, Any] | None:
    """The pins the match played with (its arena.json's "pins", live.play: run_host.machine_pins); None when they
    don't read (no arena.json, or the pins read failed: {"error": ...})."""
    try:
        p = json.loads((run_dir / "arena.json").read_text(encoding="utf-8"))["pins"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return p if isinstance(p, dict) and "error" not in p else None


def version_drift(first: Mapping[str, Any] | None, now: Mapping[str, Any] | None) -> list[str]:
    """`key: first → now` per bg3bench.pins.DRIFT_KEYS pin that differs from the matrix's first recorded pins; none
    before a first match recorded them; unread pins after one did: drifted (nothing shows they are the same)."""
    if first is None:
        return []
    if now is None:
        return ["pins: not read (arena.json)"]
    return bpins.pin_drift(dict(first), dict(now))


class ArenaQueue:
    def __init__(
        self,
        m: Mapping[str, Any],
        root: Path,
        base: Path,
        play: Play | None,
        *,
        now: Callable[[], float] = time.time,
        stopped: Callable[[], bool] = lambda: False,
        disk: Callable[[dict[str, Any]], str | None] = lambda job: None,
        log: Callable[[str], None] = lambda msg: None,
        global_budget: Path | None = None,
        sessions: Path | None = None,
    ):
        self.m, self.root, self.base, self.play = m, root, base, play  # base: the budget files' paths start there
        self.global_budget = global_budget  # runtime/budget.json: live.play checks and charges it; checked here too
        self.now, self.stopped, self.disk, self.log = now, stopped, disk, log
        self.sessions = sessions  # where an attempt's run dir is (<sessions>/<its name>): an interrupted one's costs
        self.q = arena_matrix.open_queue(root, base / m["budget"]["file"], now)

    def path(self, f: str) -> Path:
        return self.base / f

    def name(self, job: Mapping[str, Any], attempt: int) -> str:
        """The attempt's run name (its run dir's name under the sessions dir)."""
        return f"{self.m['name']}~{job['id']}~a{attempt}"

    def _append(self, rec: dict) -> None:  # bench.matrix's queue line
        with (self.root / "queue.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def first_pins(self) -> dict[str, Any] | None:
        """The pins of the matrix's first match that recorded them (queue order); None before one did."""
        for line in (self.root / "queue.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:  # a torn last line
                continue
            r = rec.get("result") if rec.get("type") == "end" else None
            if isinstance(r, dict) and isinstance(r.get("pins"), dict):
                return r["pins"]
        return None

    def operator_stopped(self) -> str | None:
        """Why the operator closed the matrix (an OPERATOR_STOP queue line), None when it is open."""
        for line in (self.root / "queue.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:  # a torn last line
                continue
            if rec.get("type") == OPERATOR_STOP:
                return str(rec.get("why"))
        return None

    def state(self) -> dict[str, dict[str, Any]]:
        jobs = self.q.state()
        for st in jobs.values():
            st["status"] = status(st["attempts"])
        return jobs

    def _progress(self, state: str, stop: str | None = None, current: dict | None = None) -> None:
        jobs = self.state()
        results = [a["result"] for st in jobs.values() for a in st["attempts"] if a["result"] is not None]
        results = [r for r in results if not r.get("refused")]
        try:
            own = matrix.read_budget(self.path(self.m["budget"]["file"]))
        except (OSError, ValueError):
            own = {"cap": None, "spent": None}
        counts = {s: sum(1 for st in jobs.values() if st["status"] == s) for s in ("done", "pending", "gave_up")}
        progress = {
            "schema": matrix.SCHEMA,
            "name": self.m["name"],
            "updated": self.now(),
            "state": state,
            "stop_reason": stop,
            "current": current,
            "jobs": len(jobs),
            **counts,
            OVER_BUDGET: sum(1 for st in jobs.values() if st["status"] == OVER_BUDGET),
            "runs": len(results),
            "invalid_runs": sum(1 for r in results if not r["valid"]),
            "cap_usd": own["cap"],
            "spent_usd": own["spent"],
        }
        tmp = self.root / "progress.json.tmp"
        tmp.write_text(json.dumps(progress, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.root / "progress.json")

    def _stop(self, why: str) -> str:
        self._progress("stopped", stop=why)
        self.log(f"arena matrix {self.m['name']}: stopped: {why}")
        return why

    def _end(self, job: dict, attempt: int, result: dict[str, Any]) -> str | None:
        """Records the match's end and charges its costs to the matrix's files; the stop it calls for, else None."""
        self._append({"type": "end", "job": job["id"], "attempt": attempt, "t": self.now(), "result": result})
        try:
            amounts = arena_matrix.charges(job, result["costs"])
        except ValueError:  # a paid side's cost is unknown: the budget can't be enforced past it
            return matrix.COST_UNKNOWN
        ok = True
        for f, usd in sorted(amounts.items()):  # every file, even after a failure: each keeps what it can
            if usd <= 0:
                continue
            try:
                b = spend.add_spend(self.path(f), usd, self.now(), matrix=self.m["name"], job=job["id"])
            except (OSError, ValueError):
                b = None
            ok = ok and b is not None
        return None if ok else matrix.BUDGET

    def run(self) -> str:
        """Play until every match is done, gave up or ended over budget, or a stop (module doc); the stop reason. A
        process that dies here (a signal, an exception) leaves progress.json stopped, never running."""
        self.q.create(self.m["name"], self.m["jobs"])
        if why := self.operator_stopped():
            raise ValueError(f"{self.root / 'queue.jsonl'} was closed by the operator: {why}; use a new matrix name")
        try:
            return self._run()
        except BaseException as e:
            with contextlib.suppress(Exception):
                self._stop(f"aborted: {type(e).__name__}: {e}")
            raise

    def _global_short(self, job: Mapping[str, Any]) -> str | None:
        """Why runtime/budget.json can't hold the match's cap (live.play would refuse it), else None."""
        need = sum(side_caps(job).values())
        if self.global_budget is None or need <= 0:
            return None
        try:
            b = matrix.read_budget(self.global_budget)
        except (OSError, ValueError) as e:
            return str(e)
        left = round(b["cap"] - b["spent"], 6)
        return None if left >= need else f"{self.global_budget}: ${left:.2f} left, the match's cap is ${need:g}"

    def _charge_global(self, job: Mapping[str, Any], name: str, costs: Mapping[str, float]) -> None:
        """An interrupted match's spend into runtime/budget.json, the figure its matrix file is charged: live.play
        charges it only as a match ends, so a killed match never reached it (D822)."""
        usd = arena_matrix.charges(job, costs).get(job["budgets"]["own"], 0.0)
        if self.global_budget is None or usd <= 0:
            return
        try:
            spend.add_spend(self.global_budget, usd, self.now(), playthrough=name, matrix=self.m["name"])
        except (OSError, ValueError) as e:
            self.log(f"arena matrix {self.m['name']}: {job['id']}: budget update failed ({e}); cost ${usd:.4f}")

    def _run(self) -> str:
        for st in self.state().values():
            if st["status"] == "interrupted":
                job, attempt = st["job"], st["attempts"][-1]["attempt"]
                run_dir = self.sessions / self.name(job, attempt) if self.sessions is not None else None
                if run_dir is not None:  # what each side billed before the runner died (D192: $0, killed at load)
                    costs = {s: c if s in side_caps(job) else 0.0 for s, c in read_costs(run_dir).items()}
                else:  # unknown: each paid side's whole reserve
                    costs = {s: side_caps(job).get(s, 0.0) for s in SIDES}
                result = {"valid": False, "invalid_reason": "interrupted", "costs": costs, "run_dir": None}
                if run_dir is None or not (run_dir / "arena.json").is_file():  # else live.play charged it (D822)
                    self._charge_global(job, self.name(job, attempt), costs)
                if why := self._end(job, attempt, result):
                    return self._stop(why)
        while True:
            jobs = self.state()
            todo = [st for st in jobs.values() if st["status"] == "pending"]
            if not todo:
                self._progress("finished", stop=matrix.FINISHED)
                return matrix.FINISHED
            if self.stopped():
                return self._stop(matrix.STOPPED)
            st = todo[0]
            job = st["job"]
            reserves = arena_matrix.reserves(job)
            try:
                budgets = {f: matrix.read_budget(self.path(f)) for f in reserves}
            except (OSError, ValueError) as e:
                self.log(f"arena matrix {self.m['name']}: {e}")
                return self._stop(matrix.BUDGET)
            if short := arena_matrix.over_budget(job, budgets):
                self.log(f"arena matrix {self.m['name']}: {job['id']}'s reserves don't fit in {', '.join(short)}")
                return self._stop(matrix.BUDGET)
            if why := self._global_short(job):
                self.log(f"arena matrix {self.m['name']}: {job['id']}: {why}")
                return self._stop(matrix.BUDGET)
            if (why := self.disk(job)) is not None:
                return self._stop(f"{matrix.DISK}: {why}")
            attempt = len(st["attempts"]) + 1
            self._append({"type": "start", "job": job["id"], "attempt": attempt, "t": self.now()})
            self._progress("running", current={"id": job["id"], "attempt": attempt})
            name = self.name(job, attempt)
            assert self.play is not None, "a queue opened to read can't play"
            try:
                played = self.play(job, name)
            except RefusedError as e:  # nothing played: not an attempt; the next match would be refused the same way
                costs = dict.fromkeys(SIDES, 0.0)
                self._end(job, attempt, {"valid": False, "invalid_reason": str(e), "refused": True, "costs": costs})
                return self._stop(f"{matrix.REFUSED}: {e}")
            except Exception as e:  # the harness failed mid-match: a paid side's cost is unknown
                costs = {s: (None if s in side_caps(job) else 0.0) for s in SIDES}
                why = f"error: {type(e).__name__}: {e}"
                self.log(f"arena matrix {self.m['name']}: {job['id']}: {why}")
                stop = self._end(job, attempt, {"valid": False, "invalid_reason": why, "costs": costs, "run_dir": None})
                if stop:
                    return self._stop(stop)
                continue
            g = graded(job, self.m["arenas"][job["arena"]], played.run_dir)
            valid = g["valid"] and played.invalid is None
            pins = match_pins(played.run_dir)
            drift = version_drift(self.first_pins(), pins)
            stop_drift = bool(drift) and not self.m.get("allow_drift")
            if stop_drift:  # REVIEW-18 5a: invalid, so the job plays again once the pins are restored
                valid, g = False, {**g, "invalid_reason": f"version_drift: {'; '.join(drift)}"}
            result = {
                "valid": valid,
                "invalid_reason": None if valid else g["invalid_reason"] or played.invalid,
                "harness_invalid": played.invalid,
                "result": g["result"],
                "reason": g["reason"],
                "costs": played.costs,
                "run_dir": str(played.run_dir),
                "pins": pins,
            }
            if drift:
                result["version_drift"] = drift
            self.log(f"arena matrix {self.m['name']}: {job['id']}: {result['result'] or result['invalid_reason']}")
            if stop := self._end(job, attempt, result):
                return self._stop(stop)
            if stop_drift:
                return self._stop(
                    f"{DRIFT}: {job['id']} played with other versions than the matrix's first match "
                    f"({'; '.join(drift)}); restore them, or set allow_drift = true to record the drift"
                )
            if drift:
                self.log(f"arena matrix {self.m['name']}: {job['id']}: version drift (allowed): {'; '.join(drift)}")

    @staticmethod
    def _counted(result: Mapping[str, Any] | None) -> bool:
        """A played attempt the table counts: not one recorded drift-invalid (REVIEW-18g; a matrix stopped on the
        drift and never restarted would grade it). Read from the recorded result, not the matrix's allow_drift now, so
        flipping the flag mid-matrix neither drops a done match nor brings a drift-invalid one back (REVIEW-18j)."""
        if result is None or not result.get("run_dir"):  # not played (refused, interrupted before a run dir)
            return False
        return not drift_invalid(result)

    def matches(self) -> list[dict[str, Any]]:
        """Each played job's match (its valid attempt, else its last one with a run dir), graded."""
        out = []
        for st in self.state().values():
            done = [a["result"] for a in st["attempts"] if self._counted(a["result"])]
            if not done:
                continue
            pick = next((r for r in done if r["valid"]), done[-1])
            job = st["job"]
            out.append(graded(job, self.m["arenas"][job["arena"]], Path(pick["run_dir"])))
        return out

    def attempts(self) -> list[dict[str, Any]]:
        """Every played attempt's match (a rerun job's invalid ones too), graded: the table's per-model failure rate
        (bg3bench.arena.failures) counts a model's client faults over all of them."""
        out = []
        for st in self.state().values():
            job = st["job"]
            for a in st["attempts"]:
                if self._counted(a["result"]):
                    out.append(graded(job, self.m["arenas"][job["arena"]], Path(a["result"]["run_dir"])))
        return out


def read_costs(run_dir: Path) -> dict[str, float]:
    """Each side's cost as its events in run_dir read (<side>.events.jsonl): per process, the running cost of its
    last usage or result event (spend.ProcessCost), summed; $0 for a side with no events (its process never started)."""
    out = {}
    for s in SIDES:
        per: dict[str, float] = {}
        for ev in events.load_events(run_dir / s, kinds=("usage", "result")):
            if ev.cost_usd is not None:
                k = ev.session_id or ""
                per[k] = max(per.get(k, 0.0), float(ev.cost_usd))
        out[s] = round(float(sum(per.values())), 6)
    return out


def print_estimate(m: Mapping[str, Any], repo: Path, out: TextIO) -> None:
    """Every budget file's reserves over the whole matrix (each paid side at its max_cost) against what it has."""
    total: dict[str, float] = {}
    for j in m["jobs"]:
        for f, r in arena_matrix.reserves(j).items():
            total[f] = total.get(f, 0.0) + r
    out.write("estimate (each paid side at its max_cost, the most the matrix can spend):\n")
    for f, usd in sorted(total.items()):
        try:
            b = matrix.read_budget(repo / f)
            left = f"${b['cap'] - b['spent']:.2f} left of ${b['cap']:g}"
        except (OSError, ValueError) as e:
            left = f"unreadable: {e}"
        out.write(f"  {f}: up to ${usd:.2f} ({left})\n")


def live_play(m: Mapping[str, Any], repo: Path, env: Mapping[str, str]) -> Play:
    """The match on the live game: live.play with the playthrough Runner."""
    import legacy_playthrough as lpt
    from bg3harness.arena import live

    def play(job: dict[str, Any], name: str) -> Played:
        spec = ArenaSpec.from_dict(job_arena(job, m["arenas"][job["arena"]]))  # arena.json records spec.seed
        sides = {
            s: Side(s, d["player"], str(repo / d["profile"]), d["client"], d["slug"]) for s, d in job["sides"].items()
        }
        caps = side_caps(job)
        try:
            outcome, run_dir = live.play(
                spec, sides, lpt.Runner, name, env, max_cost=sum(caps.values()) or None, side_caps=caps or None
            )
        except ArenaSetupError as e:
            raise RefusedError(str(e)) from e
        return Played(run_dir, outcome.invalid, {s: outcome.cost(s) for s in SIDES})

    return play


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="play.py arena-matrix", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--matrix", required=True, metavar="TOML", help="the matrix file (bg3bench.arena_matrix)")
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--plan", action="store_true", help="print the matches, reserves and estimate; play nothing")
    what.add_argument("--table", action="store_true", help="print the results table of the matches played so far")
    ap.add_argument("--seed", type=int, default=SEED, help="--table's bootstrap seed")
    return ap.parse_args(argv)


def main(argv: list[str], repo: Path | None = None, play: Play | None = None, out: TextIO = sys.stdout) -> int:
    a = parse(argv)
    repo = repo or renv.REPO
    try:
        m = arena_matrix.load(repo / a.matrix, repo)
    except (MatrixFileError, ValueError) as e:
        print(f"play.py arena-matrix refused: {e}", file=sys.stderr)
        return 2
    if a.plan:
        arena_matrix.print_plan(m, out)
        print_estimate(m, repo, out)
        return 0
    root = renv.RUNTIME / "matrix" / m["name"]
    if a.table:
        if not (root / "queue.jsonl").is_file():
            print(f"play.py arena-matrix refused: no {root / 'queue.jsonl'}: play the matrix first", file=sys.stderr)
            return 2
        q = ArenaQueue(m, root, repo, None)
        table = barena.results(q.matches(), a.seed, attempts=q.attempts())
        out.write(json.dumps(table, indent=1, default=str) + "\n")
        return 0
    if not m["jobs"]:
        print(f"play.py arena-matrix refused: matrix {m['name']} has no match", file=sys.stderr)
        return 2
    import legacy_cli as lcli  # the runner's lock

    lock = lcli.acquire_lock()  # noqa: F841 - held (runtime/runner.lock) until the process exits
    from bg3harness.runner import run_host

    def disk(job: dict[str, Any]) -> str | None:  # the match's recording must fit (its wall cap)
        return diskguard.start_refusal(renv.RUNTIME, float(m["arenas"][job["arena"]]["wall_cap_min"]))

    q = ArenaQueue(
        m,
        root,
        repo,
        play or live_play(m, repo, dict(os.environ)),
        stopped=lambda: run_host.stop_file().exists(),
        disk=disk,
        log=renv.log,
        global_budget=renv.RUNTIME / "budget.json",
        sessions=renv.RUNTIME / "sessions",
    )
    try:
        stop = q.run()
    except ValueError as e:  # the queue holds another plan
        print(f"play.py arena-matrix refused: {e}", file=sys.stderr)
        return 2
    out.write(f"arena matrix {m['name']}: {stop}\n")
    for st in q.state().values():
        last = st["attempts"][-1]["result"] if st["attempts"] else None
        what = "" if last is None else f": {last.get('result') or last.get('invalid_reason')}"
        out.write(f"  {st['job']['id']}: {st['status']} ({len(st['attempts'])} attempts){what}\n")
    return 0 if stop == matrix.FINISHED else 1
