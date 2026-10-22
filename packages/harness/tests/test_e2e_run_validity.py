# pyright: basic
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""Offline E2E of a bench run's validity (harness analysis O1, bug H2): bg3bench's SessionRun -> LiveRunHost.start ->
run_session over the runner's own session hooks -> a scripted play process per ending -> LiveRunHost.record ->
runs.jsonl and result.json. A run is invalid exactly when the harness failed it (the legacy series' classify_run
table plus the session's own endings that are ours); a time-out, a cap or a full context scores the model.

Before the fix only a crash/hang/agent hang (run_session's `invalid`) made a session-path run invalid: a toolless
agent (mcp_failed), a play command that didn't start, a settings mismatch that aborted the run, an API limit or a
claude that exited at once were all graded as the model's failures."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from bg3bench import matrix, scenario
from bg3bench.adapters.session_run import SessionRun, series_name
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.runner import state as rstate
from test_run_host import FakeRunner, Rig, rig  # noqa: F401  (rig: the fixture)

pytestmark = pytest.mark.e2e

BENCH_FIXTURES = Path(__file__).resolve().parents[2] / "bench" / "tests" / "fixtures"
SC: dict[str, Any] = scenario.load(BENCH_FIXTURES / "e2e" / "e2e_sell_idol.json")
T0 = 1000.0  # the rig's clock: the run starts here


@dataclass(frozen=True)
class Play:
    """One scripted play process: how it ended, how long it ran, what it left in the run dir."""

    reason: str
    minutes: float = 20
    exit_code: int | None = 0
    detail: str = ""
    stream: tuple[dict[str, Any], ...] = ()  # S001.jsonl lines (claude stream-json)
    events: tuple[dict[str, Any], ...] = ()  # runner events the runner wrote meanwhile
    abort: bool = False  # the runner's abort flag (a settings mismatch found mid-run)


def init(status: str = "connected") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "c-1",
        "tools": [],
        "mcp_servers": [{"name": "bg3", "status": status}],
    }


def api_error(text: str) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": text,
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }


class ScriptedRunner(FakeRunner):
    """test_run_host's FakeRunner with a play process: run_claude plays `play` (its files, its result), the events go
    to runner_events.jsonl as the SessionRunner writes them."""

    play: Play = Play("exited")

    def __init__(self, opt: rstate.Options, calls: list[str]):
        super().__init__(opt, calls)
        self.state["oom_streak"] = None
        self.abort, self.pending_notes = False, []
        self.state.dir.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        super().event(kind, **fields)
        with open(self.state.dir / "runner_events.jsonl", "a") as f:
            f.write(json.dumps({"t": fields.pop("t", T0 + 1), "kind": kind, **fields}) + "\n")

    def run_claude(
        self, sid: str, prompt: str, minutes: float, session: str | None, resume: bool = False, tag: str = ""
    ) -> rstate.SessionResult:
        p = self.play
        (self.state.dir / f"{sid}{tag}.jsonl").write_text("".join(json.dumps(ln) + "\n" for ln in p.stream))
        for e in p.events:
            self.event(**e)
        self.abort = p.abort
        end = T0 + p.minutes * 60
        fault = end if p.reason in ("crash", "hang", "agent_hang") else None
        return rstate.SessionResult(sid, "c-1", p.exit_code, p.reason, T0, end, detail=p.detail, fault_at=fault)

    def stop_check(self, sid: str, session_end: float, minutes: float) -> str | None:
        return None

    def game_alive(self) -> bool:
        return True


def job() -> dict[str, Any]:
    model = {"slug": "opus", "client": "claude", "effort": "high"}
    return matrix.plan([model], ["e2e_sell_idol"], ["rules"], 1, max_cost={"e2e_sell_idol": 1.0})[-1]


def run_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
    return {"tools_expected": 90}


def bench_run(rig: Rig, play: Play, prepare: Callable[[ScriptedRunner], None] | None = None) -> dict[str, Any]:
    """One `bg3bench run --runner session` job through the real LiveRunHost: its runs.jsonl line and result.json."""

    def make(opt: rstate.Options) -> ScriptedRunner:
        r = ScriptedRunner(opt, rig.calls)
        r.play = play
        if prepare:
            prepare(r)
        rig.runners.append(r)
        return r

    host = H.LiveRunHost(make, run_files=run_files, env=rig.env, clock=lambda: T0)
    sessions = renv.RUNTIME / "sessions"
    res = SessionRun("m", cast(Any, host), scenarios={"e2e_sell_idol": SC}, sessions=sessions)(job())
    name = series_name("m", job())
    rec = json.loads((sessions / name / "runs.jsonl").read_text().splitlines()[-1])
    result = json.loads((sessions / rec["playthrough"] / "result.json").read_text())
    assert (result["valid"], res.valid) == (res.valid, res.valid) and host.runs == {}
    return rec | {"_result": result}


HARNESS_ENDINGS = {
    "crash": Play(
        "crash",
        4,
        None,
        "game process exited (status 139)",
        events=({"kind": "fault", "fault": "crash", "session": "S001"},),
    ),
    "hang": Play(
        "hang",
        4,
        None,
        "port open but 3 liveness evals failed",
        events=({"kind": "fault", "fault": "hang", "session": "S001"},),
    ),
    "agent_hang": Play(
        "agent_hang",
        12,
        None,
        "no claude output for 10.0 min",
        events=({"kind": "fault", "fault": "agent_hang", "session": "S001"},),
    ),
    "mcp_failed (init)": Play("mcp_failed", 0.5, 1, "the bg3 MCP server is failed", stream=(init("failed"),)),
    "mcp_failed (tool count)": Play(
        "mcp_failed",
        0.5,
        None,
        "bg3 serves 12 tools, 90 expected",
        stream=(init(),),
        events=({"kind": "mcp_failed", "session": "S001"},),
    ),
    "setup_failed": Play("setup_failed", 0, None, "[Errno 2] No such file or directory: 'claude'"),
    "aborted by settings": Play(
        "aborted", 3, None, abort=True, events=({"kind": "settings_mismatch", "problems": ["difficulty Tactician"]},)
    ),
    "api_limit": Play("api_limit", 2, 1, "usage limit reached", stream=(init(), api_error("API Error: usage limit"))),
    "quick exit": Play("exited", 0.5, 1),
    "api error in the stream": Play("exited", 20, stream=(init(), api_error("API Error: 529 overloaded"))),
}
MODEL_ENDINGS = {
    "time out": (Play("hard_stop", 18, None), "timeout"),
    "exited at the end": (Play("exited", 20), "timeout"),
    "cost cap": (Play("cap", 9, None), "over_budget"),
    "context full": (Play("context_overflow", 14, 0, "the context is full (error_context_overflow)"), "timeout"),
}


@pytest.mark.parametrize("case", list(HARNESS_ENDINGS))
def test_a_run_the_harness_failed_is_invalid(rig: Rig, case: str):
    rec = bench_run(rig, HARNESS_ENDINGS[case])
    assert rec["harness_failures"] >= 1, rec["failures"]
    assert (rec["_result"]["valid"], rec["_result"]["invalid_reason"]) == (False, "harness_failure")
    assert all(f["class"] in ("harness", "agent", "unknown") for f in rec["failures"])
    assert rig.calls[-2:] == ["end_facts(" + rec["outcome"] + ")", "end_run_game(run end)"]  # the game stopped


@pytest.mark.parametrize("case", list(MODEL_ENDINGS))
def test_a_run_that_ended_on_the_model_scores_it(rig: Rig, case: str):
    play, outcome = MODEL_ENDINGS[case]

    def capped(r: ScriptedRunner) -> None:
        if play.reason == "cap":
            r.cap_hit = {"cap": "cost", "cost": 1.0, "tokens": 9, "why": "the run's cost cap"}

    rec = bench_run(rig, play, capped)
    assert (rec["outcome"], rec["harness_failures"]) == (outcome, 0), rec["failures"]
    assert (rec["_result"]["valid"], rec["_result"]["invalid_reason"]) == (True, None)
    kinds = [f["kind"] for f in rec["failures"]]
    assert kinds == (["cost_cap"] if outcome == "over_budget" else ["timeout"])  # the agent's, as the legacy tags it


def test_the_harness_failure_names_what_went_wrong(rig: Rig):
    rec = bench_run(rig, HARNESS_ENDINGS["setup_failed"])
    (setup,) = [f for f in rec["failures"] if f["kind"] == "setup_failed"]
    assert setup["class"] == "harness" and "No such file" in setup["detail"]
    rec = bench_run(rig, HARNESS_ENDINGS["mcp_failed (init)"])
    assert [f["kind"] for f in rec["failures"] if f["class"] == "harness"].count("mcp_failed") == 1  # listed once
