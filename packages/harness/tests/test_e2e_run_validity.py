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
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from bg3bench import matrix, scenario
from bg3bench.adapters.session_run import SessionRun, series_name
from bg3harness import profiles
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.runner import state as rstate
from test_run_host import FakeRunner, Rig, outcome, rig, run_dir  # noqa: F401  (rig: the fixture)

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
    raises: Exception | None = None  # the play process didn't start (agent_side re-raises Popen's OSError)


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
        if p.raises is not None:
            raise p.raises
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


def job(name: str = "e2e_sell_idol") -> dict[str, Any]:
    model = {"slug": "opus", "client": "claude", "effort": "high"}
    return matrix.plan([model], [name], ["rules"], 1, max_cost={name: 1.0})[-1]


def run_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
    return {"tools_expected": 90}


def bench_run(
    rig: Rig, play: Play, prepare: Callable[[ScriptedRunner], None] | None = None, sc: dict[str, Any] = SC
) -> dict[str, Any]:
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
    res = SessionRun("m", cast(Any, host), scenarios={sc["name"]: sc}, sessions=sessions)(job(sc["name"]))
    name = series_name("m", job(sc["name"]))
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


def save_scenario(tmp_path: Path) -> dict[str, Any]:
    """The e2e scenario as a save start (its nav files are moved for the run and put back)."""
    spec = json.loads((BENCH_FIXTURES / "e2e" / "e2e_sell_idol.json").read_text())
    note = {"where": "The trader", "objective": "Sell the idol"}
    spec |= {"name": "e2e_save", "start": {"kind": "save", "save": "Tav-3__SCN-e2e_save", "note": note}}
    spec["profile"]["character"]["name"] = "Tav"
    path = tmp_path / "e2e_save.json"
    path.write_text(json.dumps(spec))
    return scenario.load(path)


def test_a_play_process_that_cant_start_is_recorded_invalid_and_the_matrix_goes_on(rig: Rig, tmp_path: Path):
    # H3: Popen's OSError escaped run_session: no runs.jsonl line, the game up, the nav files moved, `bg3bench run` dead
    sc = save_scenario(tmp_path)
    nav = sorted(p.name for p in renv.SE_BG3AI_DIR.iterdir())
    missing = FileNotFoundError(2, "No such file or directory", "claude")
    rec = bench_run(rig, Play("exited", raises=missing), sc=sc)
    assert (rec["outcome"], rec["reason"], rec["harness_failures"]) == ("gave_up", "setup_failed", 2)
    assert {f["kind"] for f in rec["failures"]} == {"setup_failed", "gave_up"}
    assert "FileNotFoundError" in rec["failures"][0]["detail"] and rec["reset"]["nav_files"]
    assert (rec["_result"]["valid"], rec["_result"]["invalid_reason"]) == (False, "harness_failure")
    assert rig.calls[-2:] == ["end_facts(gave_up)", "end_run_game(run end)"]  # the game stopped
    assert sorted(p.name for p in renv.SE_BG3AI_DIR.iterdir()) == nav and not H.nav_marker().exists()
    rec = bench_run(rig, Play("exited"), sc=sc)  # the matrix's rerun plays
    assert (rec["run"], rec["outcome"], rec["_result"]["valid"]) == (2, "timeout", True)


def test_a_claude_run_stopped_before_its_result_has_an_unknown_cost_not_zero(rig: Rig):
    # H4: the hard stop ended the run's one claude process before its result; the record said cost_usd 0
    from bg3harness import clients, events

    def stopped_stream(r: ScriptedRunner) -> None:
        play = r.run_claude

        def run_claude(sid: str, *a: Any, **k: Any) -> rstate.SessionResult:
            res = play(sid, *a, **k)
            log = events.EventLog(r.state.dir / f"{sid}.events.jsonl")
            log.write(clients.Event("usage", message_id="m1", tokens=clients.tokens(208000, 900, 0, 0)))
            log.end("hard_stop", -2, res.ended)
            return res

        r.run_claude = run_claude  # pyright: ignore[reportAttributeAccessIssue]

    rec = bench_run(rig, Play("hard_stop", 18, None, stream=(init(),)), stopped_stream)
    assert (rec["cost_usd"], rec["cost_unknown"], rec["turns"]) == (None, True, 1)
    assert sum(rec["tokens"].values()) == 208900
    assert rec["_result"]["cost_usd"] is None and rec["_result"]["valid"]  # graded; its cost unknown, not free


def test_a_jobs_env_never_reaches_the_next_job_nor_os_environ(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # H1: profiles.tool_env only ever added to BG3AI_TOOLS_OFF, so an image model run after deepseek-v4-pro (text only)
    # in one `bg3bench run` played without screenshots; and every job wrote its vars (API key, condition, tools) into
    # os.environ, where the next job (and its bg3 server) still saw them
    profiles_dir = Path(__file__).resolve().parents[3] / "profiles"
    monkeypatch.setattr(renv, "REPO", tmp_path)  # the keys come from this .env, never the repo's
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=key-ds\nOPENROUTER_API_KEY=key-or\n")
    before = dict(os.environ)
    seen: list[dict[str, str]] = []

    def files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        seen.append(dict(host.env))  # what the client's run files (manifest, tool count) are dumped with
        return {}

    runners: list[ScriptedRunner] = []

    def make(opt: rstate.Options) -> ScriptedRunner:
        runners.append(ScriptedRunner(opt, rig.calls))
        return runners[-1]

    base = {"PATH": "/usr/bin"}
    host = H.LiveRunHost(make, run_files=files, env=base, clock=lambda: T0)
    for name in ("deepseek-v4-pro.toml", "opus-5.5-pi.toml"):
        model = profiles.load(profiles_dir / name).model
        job = {
            "model": model,
            "client": "pi",
            "effort": None,
            "condition": "rules",
            "prompt_condition": "rules",  # the condition as the session runner plays it (9500d78)
            "profile": str(profiles_dir / name),
        }
        d = run_dir(f"m-{name}")
        host.start(job, SC, d)
        assert runners[-1].run_env is host.env  # the play process and its bg3 server get this job's env
        host.record(job, SC, d, outcome())
    assert [e["BG3AI_TOOLS_OFF"] for e in seen] == ["memory,screenshots", "memory"]
    assert ("DEEPSEEK_API_KEY" in seen[0], "OPENROUTER_API_KEY" in seen[0]) == (True, False)
    assert ("DEEPSEEK_API_KEY" in seen[1], "OPENROUTER_API_KEY" in seen[1]) == (False, True)
    assert dict(os.environ) == before and base == {"PATH": "/usr/bin"}
