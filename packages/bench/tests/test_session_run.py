# pyright: basic
"""adapters/session_run.SessionRun: the matrix's run_one on run_session with a fake host (a scripted agent and game
on a fake clock): a clean run, a crash (invalid, no resume, the matrix reruns it), a failed start, an edited scenario,
and the matrix's budget for a paid and a subscription client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_run_artifacts import assert_valid, readout

from bg3bench import conditions, matrix, pins, report, results, scenario
from bg3bench.adapters.session_run import NO_RESUME, SessionRun, Start, series_name
from bg3bench.matrix import Matrix, RunResult

HERE = Path(__file__).resolve().parent
SC = scenario.load(HERE / "fixtures" / "e2e" / "e2e_sell_idol.json")
T0 = 1_800_000_000.0


@dataclass
class Result:
    reason: str
    exit_code: int | None
    started: float
    ended: float
    claude_session: str
    fault_at: float | None = None


class Hooks:
    """run_session's hooks for one run: each agent process plays the next (reason, minutes) of `script`."""

    def __init__(self, script: list[tuple[str, float]], clock: list[float]):
        self.script, self.clock = list(script), clock
        self.runs: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.recovered: list[str] = []

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str) -> Result:
        reason, runs_min = self.script.pop(0)
        start = self.clock[0]
        self.clock[0] += runs_min * 60
        self.runs.append({"prompt": prompt, "minutes": minutes, "session": client_session, "resume": resume})
        fault = self.clock[0] if reason != "exited" else None
        return Result(reason, 0 if reason == "exited" else None, start, self.clock[0], client_session or "c", fault)

    def session_end(self, planned_end: float) -> float:
        return planned_end

    def stop_check(self, session_end: float) -> str | None:
        return None

    def game_ok(self) -> bool:
        return False

    def recover(self, why: str):
        self.recovered.append(why)

    def loaded(self) -> str | None:
        return None

    def aborted(self) -> bool:
        return False

    def stop_requested(self) -> bool:
        return False

    def notes(self) -> list[str]:
        return []

    def resume_prompt(self, why: str, notes: list[str]) -> str:
        return "RESUME"

    def agent_resume_prompt(self, notes: list[str]) -> str:
        return "RESUME"

    def stop_resume_prompt(self, why: str) -> str:
        return "RESUME"

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def log(self, msg: str) -> None:
        pass

    def now(self) -> float:
        return self.clock[0]


class Host:
    """The harness's side: each start plays the next script of `scripts`; the record is a passed or failed run."""

    def __init__(
        self,
        scripts: list[list[tuple[str, float]]],
        cost: float | None = 0.4,
        fail_start: bool = False,
        served: dict | None = None,
    ):
        self.scripts, self.cost, self.fail_start, self.served = list(scripts), cost, fail_start, served
        self.clock = [T0]
        self.started: list[tuple[dict, dict, Path]] = []
        self.hooks: list[Hooks] = []

    def start(self, job: dict, sc: dict, run_dir: Path) -> Start:
        if self.fail_start:
            raise RuntimeError("the start save would not load")
        self.started.append((job, sc, run_dir))
        self.hooks.append(Hooks(self.scripts.pop(0), self.clock))
        return Start("KICKOFF", self.hooks[-1], "conv-1", "a" * 64, ({"file": "rules.md", "sha256": "b" * 64},), "0.1")

    def record(self, job: dict, sc: dict, run_dir: Path, outcome) -> dict:
        rec = {"outcome": "gave_up" if outcome.invalid else "timeout", "cost_usd": self.cost, "tokens": {}}
        rec["served"] = self.served
        return rec | readout(sc["game_settings"].get("seed"))


def job(client: str = "pi", condition: str = "rules", repeat: int = 1) -> dict:
    model = {"slug": "anthropic/stub", "client": client, "effort": "high"}
    return matrix.plan([model], ["e2e_sell_idol"], [condition], repeat, max_cost={"e2e_sell_idol": 1.0})[-1]


def run_one(tmp_path: Path, host: Host) -> SessionRun:
    return SessionRun("m", host, scenarios={"e2e_sell_idol": SC}, sessions=tmp_path / "sessions")


def test_a_clean_run_is_one_session_and_its_artifacts(tmp_path):
    host = Host([[("exited", 5)]])
    res = run_one(tmp_path, host)(job())
    series = series_name("m", job())
    assert res == RunResult(f"{series}-r001", True, False, 0.4, None, False)  # pi: the matrix settles the cost
    (_, sc, run_dir), (hooks,) = host.started[0], host.hooks
    assert sc is SC and run_dir == tmp_path / "sessions" / series  # the v2 scenario, run 1 is the series itself
    assert hooks.runs == [{"prompt": "KICKOFF", "minutes": 15.0, "session": "conv-1", "resume": False}]  # run_hours
    rec = json.loads((run_dir / "runs.jsonl").read_text())
    assert (rec["series"], rec["playthrough"], rec["run"], rec["started"]) == (series, series, 1, T0)
    assert rec["pins"]["versions"] == {**pins.versions(), "harness": "0.1"}  # the host's harness version
    meta = json.loads((run_dir / "series.json").read_text())
    assert meta["scenario"]["spec"] == SC and meta["scenario"]["digest"] == scenario.digest(SC)
    man = json.loads((run_dir / "manifest.json").read_text())
    assert man["condition"] == "rules" and man["seed"] == 7 and man["versions"]["harness"] == "0.1"
    assert man["prompt"]["sha256"] == "a" * 64
    assert [m["run_id"] for m, _ in report.load_runs(tmp_path / "sessions")] == [f"{series}-r001"]


def test_the_host_gets_the_tools_the_condition_removes(tmp_path):
    # the session runner served wiki and load_game under "rules" (live7): the condition's tools go to the host
    host = Host([[("exited", 5)], [("exited", 5)]])
    one = run_one(tmp_path, host)
    one(job())
    one(job(condition="rules+wiki+reloads:2"))
    assert [j["tools_off"] for j, _, _ in host.started] == [["wiki_search", "wiki_page", "load_game"], []]


def test_a_crash_ends_the_run_invalid_without_a_resume(tmp_path):
    host = Host([[("crash", 3)], [("exited", 5)]])
    one = run_one(tmp_path, host)
    res = one(job())
    assert NO_RESUME.max_resumes == 0
    assert not res.valid and res.invalid_reason == "harness_failure: crash (no resume)"
    assert host.hooks[0].recovered == [] and len(host.hooks[0].runs) == 1  # the game left as it is, no resume
    assert ("session_invalid", {"fault": "crash", "session": series_name("m", job())}) in host.hooks[0].events
    series = series_name("m", job())
    rec = json.loads((tmp_path / "sessions" / series / "runs.jsonl").read_text())
    assert (rec["harness_failures"], rec["session_invalid"]) == (1, "crash")
    # the rerun is the series' next run
    again = one(job())
    assert again.valid and again.run_id == f"{series}-r002" and host.started[1][2].name == f"{series}-r2"


def test_the_matrix_reruns_a_crashed_job_and_settles_paid_costs(tmp_path):
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"cap": 10.0, "spent": 0.0}))
    host = Host([[("hang", 2)], [("exited", 5)]])
    mx = Matrix(tmp_path / "mx", run_one(tmp_path, host), budget, now=lambda: T0, sleep=lambda s: None)
    mx.create("m", [job()])
    assert mx.run() == matrix.FINISHED
    (st,) = mx.state().values()
    assert st["status"] == "done" and [a["result"]["valid"] for a in st["attempts"]] == [False, True]
    assert json.loads(budget.read_text())["spent"] == 0.8  # both runs billed, by the matrix


def test_a_subscription_run_is_not_billed(tmp_path):
    res = run_one(tmp_path, Host([[("exited", 5)]], cost=None))(job(client="claude"))
    assert res.charged and res.cost_usd is None


def test_a_failed_start_is_invalid_and_free(tmp_path):
    host = Host([[("exited", 5)]], fail_start=True)
    one = run_one(tmp_path, host)
    why = "start failed: RuntimeError: the start save would not load"
    assert one(job()) == RunResult(None, False, False, 0.0, why, True)
    series = series_name("m", job())
    (rec,) = [json.loads(ln) for ln in (tmp_path / "sessions" / series / "runs.jsonl").read_text().splitlines()]
    assert (rec["run"], rec["outcome"], rec["reason"], rec["cost_usd"]) == (1, "runner_error", why, 0.0)
    assert results.validity(rec) == (False, "runner_error")
    # the start may have written run 1's facts.jsonl (its run_start is time zero): the rerun is run 2, a fresh dir
    host.fail_start = False
    again = one(job())
    assert again.valid and again.run_id == f"{series}-r002" and host.started[0][2].name == f"{series}-r2"


def test_an_edited_scenario_is_refused_by_its_series(tmp_path):
    host = Host([[("exited", 5)], [("exited", 5)]])
    run_one(tmp_path, host)(job())
    edited = {**SC, "caps": {**SC["caps"], "run_hours": 1.0}}
    with pytest.raises(ValueError, match="changed since its first run"):
        SessionRun("m", host, scenarios={"e2e_sell_idol": edited}, sessions=tmp_path / "sessions")(job())
    with pytest.raises(ValueError, match="no scenario 'grove'"):
        run_one(tmp_path, host)({**job(), "scenario": "grove"})


def test_the_manifest_names_the_profile_a_run_played(tmp_path):
    # two profiles that differ only in their rates are two different setups (pkg-bench-analysis E7)
    flash = (HERE.parents[2] / "profiles" / "deepseek-flash.toml").read_text()
    paths = {"a": tmp_path / "a.toml", "a2": tmp_path / "a2.toml", "b": tmp_path / "b.toml"}
    paths["a"].write_text(flash)
    paths["a2"].write_text("# the same profile, reworded\n" + flash)
    paths["b"].write_text(flash.replace("cache_read = 0.006", "cache_read = 0.012"))
    shas = {}
    for k, path in paths.items():
        host = Host([[("exited", 5)]])
        j = {**job(), "profile": str(path)}
        res = SessionRun("m", host, scenarios={"e2e_sell_idol": SC}, sessions=tmp_path / k)(j)
        man = json.loads((tmp_path / k / series_name("m", j) / "manifest.json").read_text())
        assert res.valid and man["profile"]["name"] == path.name
        assert_valid("manifest.v1.schema.json", man)
        shas[k] = man["profile"]["sha256"]
    assert shas["a"] == shas["a2"] != shas["b"]
    assert run_one(tmp_path, Host([[("exited", 5)]]))(job()).valid  # a job without a profile: null
    man = json.loads((tmp_path / "sessions" / series_name("m", job()) / "manifest.json").read_text())
    assert man["profile"] is None
    assert_valid("manifest.v1.schema.json", man)


def test_a_run_served_outside_its_provider_pin_is_invalid_and_the_manifest_says_what_was_served(tmp_path):
    opus = HERE.parents[2] / "profiles" / "opus-5.5-pi.toml"  # [routing] only = ["anthropic"]
    j = {**job(), "profile": str(opus)}
    for k, providers, reason in (("pinned", ["Anthropic"], None), ("off", ["Anthropic", "Google Vertex"], "off_route")):
        served = {"effort": ["high", "medium"], "providers": providers}
        res = SessionRun(
            "m", Host([[("exited", 5)]], served=served), scenarios={"e2e_sell_idol": SC}, sessions=tmp_path / k
        )(j)
        assert (res.valid, res.invalid_reason, res.cost_usd) == (reason is None, reason, 0.4)  # its cost still counts
        run_dir = tmp_path / k / series_name("m", j)
        man = json.loads((run_dir / "manifest.json").read_text())
        assert man["model"]["served"] == served
        assert_valid("manifest.v1.schema.json", man)
        assert_valid("result.v1.schema.json", json.loads((run_dir / "result.json").read_text()))
    rec = json.loads((tmp_path / "off" / series_name("m", j) / "runs.jsonl").read_text())
    assert rec["off_route"] == ["Google Vertex"]


def test_the_host_gets_the_prompt_condition_and_a_gapped_run_is_a_pilot(tmp_path):
    host = Host([[("exited", 5)], [("exited", 5)]])
    sr = run_one(tmp_path, host)
    sr(job(condition="rules+coaching@1a2b3c4d+wiki"))
    sr(job(condition="rules+wiki"))
    (coached, _, coached_dir), (plain, _, plain_dir) = host.started
    assert (coached["prompt_condition"], plain["prompt_condition"]) == ("rules+coaching", "rules")
    assert coached["tools_off"] == ["load_game"]
    res = json.loads((coached_dir / "result.json").read_text())
    assert res["pilot"] == [conditions.GAP_COACHING]
    assert_valid("result.v1.schema.json", res)
    assert "pilot" not in json.loads((plain_dir / "result.json").read_text())  # pi under rules+wiki: exact
