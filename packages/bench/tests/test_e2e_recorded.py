# pyright: basic
"""A recorded live run through the bench end to end: live7's nautiloid run (2026-10-22, Opus 5.5 on Claude Code,
condition rules; research/ovh/live7.md §1) replayed by a host that hands SessionRun the files the live runner wrote
(facts.jsonl, rolls.jsonl) and the record LiveRunHost.record returns for a run stopped at its until milestone. The
real SessionRun, grader, artifacts, results and report run on it: real game_t clock, real marks and snapshots.

Fixture: tests/fixtures/live7_nautiloid/ (facts.jsonl's first 296 lines: run_start to escaping the nautiloid and
the beach, no run_end; rolls.jsonl). Escape the Nautiloid completed at game time 727.7 s."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_run_artifacts import assert_valid
from test_session_run import T0, Hooks

from bg3bench import matrix, report, results, scenario
from bg3bench.adapters.session_run import SessionRun, Start, series_name

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
LIVE7 = HERE / "fixtures" / "live7_nautiloid"
NAUT = scenario.load(HERE.parent / "scenarios" / "nautiloid.json")
T_ESCAPED = 727.7


class Replay:
    """The harness side of one recorded run: the live files land in the run dir as the runner wrote them."""

    def __init__(self, record: dict, files: tuple[str, ...] = ("facts.jsonl",)):
        self.rec, self.files, self.clock = record, files, [T0]
        self.started: list[tuple[dict, Path]] = []

    def start(self, job: dict, sc: dict, run_dir: Path) -> Start:
        self.started.append((job, run_dir))
        for name in self.files:
            shutil.copy(LIVE7 / name, run_dir / name)
        return Start("KICKOFF", Hooks([("milestone", 12.5)], self.clock), "conv-1", "e" * 64, (), "0.4")

    def record(self, job: dict, sc: dict, run_dir: Path, outcome) -> dict:
        return dict(self.rec, wall_min=round((outcome.result.ended - outcome.started) / 60, 2))


def claude_job() -> dict:
    model = {"slug": "claude-opus-5-5", "client": "claude", "effort": "high"}
    return matrix.plan([model], ["nautiloid"], ["rules"], 1)[0]


def test_the_live7_nautiloid_run_grades_passed_at_its_escape(tmp_path):
    rec = {"outcome": "reached", "milestone": "nautiloid_escaped", "reason": "milestone", "cost_usd": 0.0}
    host = Replay(rec | {"tokens": {}, "turns": 40, "sessions": 1, "cap_hit": None})
    res = SessionRun("live7", host, scenarios={"nautiloid": NAUT}, sessions=tmp_path / "sessions")(claude_job())
    name = series_name("live7", claude_job())
    assert res == matrix.RunResult(f"{name}-r001", True, True, 0.0, None, True)  # claude: never charged
    ((job, run_dir),) = host.started
    assert job["tools_off"] == ["wiki_search", "wiki_page", "load_game"]  # rules: closed book, no reloads
    result = results.load(run_dir / "result.json")
    assert (result["passed"], result["t_pass_s"], result["outcome"]) == (True, T_ESCAPED, "reached")
    assert result["grade"]["violations"] == []
    assert result["wall_s"] == 750.0
    man = json.loads((run_dir / "manifest.json").read_text())
    assert (man["condition"], man["seed"]) == ("rules", NAUT["game_settings"]["seed"])
    assert_valid("result.v1.schema.json", result)
    assert_valid("manifest.v1.schema.json", man)
    rows = report.build(report.load_runs(tmp_path / "sessions"))["runs"]
    assert [(r["run_id"], report.verdict(r)) for r in rows] == [(f"{name}-r001", "pass")]


def test_the_grade_not_the_record_decides(tmp_path):
    # a record that says timeout (the session ran out) still passes on the facts: the grader latches 727.7 s
    host = Replay({"outcome": "timeout", "reason": "exited", "cost_usd": 0.0, "tokens": {}, "sessions": 1})
    res = SessionRun("live7", host, scenarios={"nautiloid": NAUT}, sessions=tmp_path / "sessions")(claude_job())
    assert res.valid and res.passed
    result = results.load(host.started[0][1] / "result.json")
    assert (result["t_pass_s"], result["outcome"]) == (T_ESCAPED, "timeout")


def pi_job() -> dict:
    model = {"slug": "deepseek-v4.1-flash", "client": "pi", "effort": "high"}
    return matrix.plan([model], ["nautiloid"], ["rules"], 1, max_cost={"nautiloid": 0.25})[0]


def test_a_roll_log_the_bench_refuses_makes_the_run_invalid_not_the_matrix_stop(tmp_path):
    # live7's rolls.jsonl: two rolls without `natural` (bridge referee.to_roll drops None fields; rolls.v1 requires
    # the key, null when unknown). write_run_artifacts raised after the run, so the matrix lost its known cost and
    # stopped on "cost unknown"; now the run is invalid with its cost and rerun
    (tmp_path / "budget.json").write_text(json.dumps({"cap": 1.0, "spent": 0.0}))
    rec = {"outcome": "reached", "reason": "milestone", "cost_usd": 0.19, "tokens": {}, "sessions": 1}
    host = Replay(rec)
    one = SessionRun("live7", host, scenarios={"nautiloid": NAUT}, sessions=tmp_path / "sessions")
    logs = iter([("facts.jsonl", "rolls.jsonl"), ("facts.jsonl",)])  # the rerun's roll log is clean

    def run_one(job: dict) -> matrix.RunResult:
        host.files = next(logs)
        return one(job)

    mx = matrix.Matrix(tmp_path / "m", run_one, tmp_path / "budget.json", now=lambda: T0, sleep=lambda s: None)
    mx.create("live7", [pi_job()])
    assert mx.run() == matrix.FINISHED
    (st,) = mx.state().values()
    first, again = (a["result"] for a in st["attempts"])
    assert (first["valid"], first["cost_usd"], first["charged"]) == (False, 0.19, False)
    assert first["invalid_reason"].startswith("artifacts failed: RollLogError: ")
    assert "natural is required" in first["invalid_reason"]
    assert again["valid"] and again["passed"] and st["status"] == "done"
    assert json.loads((tmp_path / "budget.json").read_text())["spent"] == pytest.approx(0.38)  # both runs settled
