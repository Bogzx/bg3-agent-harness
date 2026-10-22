import json
from pathlib import Path

import pytest

from bg3bench import matrix
from bg3bench.adapters import legacy_cli
from bg3bench.matrix import RunResult

HERE = Path(__file__).resolve().parent
MODELS = [{"slug": "opus", "effort": "medium"}, {"slug": "openrouter/deepseek/v4", "client": "pi"}]


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class FakeRun:
    """run_one: a scripted result per (job id, attempt); default a valid pass costing $1."""

    def __init__(self, script=None, clock=None):
        self.script, self.clock, self.calls = script or {}, clock, []

    def __call__(self, job):
        self.calls.append(job["id"])
        n = self.calls.count(job["id"])
        if self.clock:
            self.clock.t += 600
        out = self.script.get((job["id"], n), RunResult(f"{job['id']}#{n}", True, True, 1.0))
        if isinstance(out, BaseException):
            raise out
        return out


def make(tmp_path, run_one, cap=100.0, spent=0.0, jobs=None, clock=None, **kw):
    budget = tmp_path / "budget.json"
    if not budget.exists():
        budget.write_text(json.dumps({"cap": cap, "spent": spent}))
    clock = clock or Clock()
    m = matrix.Matrix(tmp_path / "m", run_one, budget, now=clock.now, sleep=clock.sleep, **kw)
    m.create("night1", jobs if jobs is not None else matrix.plan(MODELS, ["nautiloid", "grove"], ["rules"], 2))
    return m, budget, clock


def progress(tmp_path):
    return json.loads((tmp_path / "m" / "progress.json").read_text())


def test_plan_is_repeat_major_and_unique():
    jobs = matrix.plan(MODELS, ["nautiloid", "grove"], ["rules", "rules+coaching@1a2b"], 2, max_cost={"grove": 50})
    assert len(jobs) == 16
    assert [j["repeat"] for j in jobs] == [1] * 8 + [2] * 8
    assert jobs[0] == {
        "id": "opus~nautiloid~rules~r1",
        "model": "opus",
        "client": "claude",
        "effort": "medium",
        "profile": None,
        "scenario": "nautiloid",
        "condition": "rules",
        "repeat": 1,
        "max_cost": None,
    }
    assert jobs[2]["max_cost"] == 50 and jobs[4]["id"] == "openrouter_deepseek_v4~nautiloid~rules~r1"
    assert jobs[4]["client"] == "pi" and jobs[1]["id"] == "opus~nautiloid~rules+coaching@1a2b~r1"
    with pytest.raises(ValueError, match="duplicate"):
        matrix.plan([MODELS[0], MODELS[0]], ["grove"], ["rules"], 1)
    with pytest.raises(ValueError, match="repeats"):
        matrix.plan(MODELS, ["grove"], ["rules"], 0)


def test_runs_every_job_once_and_charges_the_budget(tmp_path):
    run = FakeRun()
    m, budget, _ = make(tmp_path, run)
    assert m.run() == matrix.FINISHED
    assert len(run.calls) == 8 and len(set(run.calls)) == 8
    b = json.loads(budget.read_text())
    assert (b["cap"], b["spent"], b["last"]) == (100.0, 8.0, {"usd": 1.0, "matrix": "night1", "job": run.calls[-1]})
    p = progress(tmp_path)
    assert (p["state"], p["done"], p["pending"], p["runs"], p["passed"], p["spent_usd"]) == (
        "finished",
        8,
        0,
        8,
        8,
        8.0,
    )
    assert m.run() == matrix.FINISHED and len(run.calls) == 8  # nothing left to do


def test_invalid_runs_are_rerun_at_most_twice(tmp_path):
    first = "opus~nautiloid~rules~r1"
    second = "opus~grove~rules~r1"
    bad = RunResult("x", False, False, 0.5, "runner_error")
    run = FakeRun({(first, 1): bad, (second, 1): bad, (second, 2): bad, (second, 3): bad})
    m, budget, _ = make(tmp_path, run)
    assert m.run() == matrix.FINISHED
    assert run.calls.count(first) == 2 and run.calls.count(second) == 3
    st = m.state()
    assert st[first]["status"] == "done" and st[second]["status"] == "gave_up"
    p = progress(tmp_path)
    assert (p["done"], p["gave_up"], p["invalid_runs"], p["runs"]) == (7, 1, 4, 11)
    assert json.loads(budget.read_text())["spent"] == 7 * 1.0 + 4 * 0.5  # invalid runs still cost money


def test_stops_at_the_budget_with_the_reserve(tmp_path):
    jobs = matrix.plan(MODELS[1:], ["nautiloid", "grove"], ["rules"], 3, max_cost={"grove": 4.0})
    run = FakeRun()
    m, budget, _ = make(tmp_path, run, cap=5.0, jobs=jobs)
    # naut r1 ($1) -> grove r1 (1 + 4 <= 5: runs, $1) -> naut r2 ($1, spent 3) -> grove r2: 3 + 4 > 5: stop
    assert m.run() == matrix.BUDGET
    ds = "openrouter_deepseek_v4"
    assert run.calls == [f"{ds}~nautiloid~rules~r1", f"{ds}~grove~rules~r1", f"{ds}~nautiloid~rules~r2"]
    assert progress(tmp_path)["stop_reason"] == "budget" and progress(tmp_path)["pending"] == 3
    budget.write_text(json.dumps({"cap": 50.0, "spent": 3.0}))  # a person raises the cap
    assert m.run() == matrix.FINISHED and len(run.calls) == 6


def test_a_subscription_job_holds_back_no_reserve(tmp_path):
    # pi's grove run spends 4 of 5: Claude Code's grove run (never charged) still starts, though its max_cost of 4
    # would be over the cap as a reserve; pi's next one doesn't
    jobs = matrix.plan(MODELS[::-1], ["grove"], ["rules"], 2, max_cost={"grove": 4.0})
    pi, claude = "openrouter_deepseek_v4~grove~rules", "opus~grove~rules"
    run = FakeRun(
        {
            (f"{pi}~r1", 1): RunResult("p", True, True, 4.0),
            (f"{claude}~r1", 1): RunResult("c", True, True, 0.0, charged=True),
        }
    )
    m, budget, _ = make(tmp_path, run, cap=5.0, jobs=jobs)
    assert m.run() == matrix.BUDGET
    assert run.calls == [f"{pi}~r1", f"{claude}~r1"]
    assert json.loads(budget.read_text())["spent"] == 4.0
    assert matrix.reserve(jobs[0]) == 4.0 and matrix.reserve(jobs[1]) == 0.0


def test_unknown_cost_stops_the_matrix(tmp_path):
    first = "opus~nautiloid~rules~r1"
    run = FakeRun({(first, 1): RunResult("a", True, False, None)})
    m, budget, _ = make(tmp_path, run)
    assert m.run() == matrix.COST_UNKNOWN and run.calls == [first]
    assert json.loads(budget.read_text())["spent"] == 0.0
    assert m.state()[first]["status"] == "done"
    assert m.run() == matrix.FINISHED  # after a person checked it, the rest runs


PI = "openrouter_deepseek_v4"  # MODELS[1]'s job id slug: a paid client
CLAUDE_RUN = RunResult("c", True, True, 0.4, None, charged=True)  # as session_run reports a subscription run


def test_an_adapter_exception_is_an_invalid_attempt_and_stops(tmp_path):
    first = f"{PI}~nautiloid~rules~r1"
    run = FakeRun({(first, 1): RuntimeError("play.py not found")})
    m, _, _ = make(tmp_path, run, jobs=matrix.plan(MODELS[1:], ["nautiloid", "grove"], ["rules"], 2))
    assert m.run() == matrix.COST_UNKNOWN
    (att,) = m.state()[first]["attempts"]
    assert (
        att["result"]["valid"] is False and att["result"]["invalid_reason"] == "error: RuntimeError: play.py not found"
    )
    assert m.run() == matrix.FINISHED and run.calls.count(first) == 2


def test_a_subscription_job_whose_adapter_raises_is_rerun_and_the_matrix_goes_on(tmp_path):
    first = "opus~nautiloid~rules~r1"
    run = FakeRun({(first, 1): RuntimeError("claude not found"), (first, 2): CLAUDE_RUN})
    m, budget, _ = make(tmp_path, run, jobs=matrix.plan(MODELS[:1], ["nautiloid"], ["rules"], 1))
    assert m.run() == matrix.FINISHED  # a claude run is never charged: its cost is not unknown
    atts = m.state()[first]["attempts"]
    assert [a["result"]["invalid_reason"] for a in atts] == ["error: RuntimeError: claude not found", None]
    assert json.loads(budget.read_text())["spent"] == 0.0


@pytest.mark.parametrize("max_cost", [2.0, None])
def test_an_interrupted_subscription_run_is_not_charged(tmp_path, max_cost):
    jobs = matrix.plan(MODELS[:1], ["nautiloid"], ["rules"], 1, max_cost={"nautiloid": max_cost})
    first = "opus~nautiloid~rules~r1"
    m, budget, _ = make(tmp_path, FakeRun({(first, 1): KeyboardInterrupt()}), jobs=jobs)
    with pytest.raises(KeyboardInterrupt):
        m.run()
    m2, _, _ = make(tmp_path, FakeRun({(first, 1): CLAUDE_RUN}), jobs=jobs)  # a new process: its call 1
    assert m2.run() == matrix.FINISHED  # no reserve charged (2.0), no cost-unknown stop (None)
    assert [a["result"]["invalid_reason"] for a in m2.state()[first]["attempts"]] == ["interrupted", None]
    assert json.loads(budget.read_text())["spent"] == 0.0


def test_resumes_after_its_own_crash(tmp_path):
    jobs = matrix.plan(MODELS[1:], ["nautiloid", "grove"], ["rules"], 1, max_cost={"grove": 2.5})
    crash = f"{PI}~grove~rules~r1"
    run = FakeRun({(crash, 1): KeyboardInterrupt()})
    m, budget, _ = make(tmp_path, run, jobs=jobs)
    with pytest.raises(KeyboardInterrupt):
        m.run()
    assert m.state()[crash]["status"] == "interrupted"
    # a fresh process: the same files, a new Matrix object
    run2 = FakeRun()
    m2, _, _ = make(tmp_path, run2, jobs=jobs)
    assert m2.run() == matrix.FINISHED and run2.calls == [crash]
    atts = m2.state()[crash]["attempts"]
    assert [a["result"]["invalid_reason"] for a in atts] == ["interrupted", None]
    # the interrupted run is charged its reserve; then the rerun's $1
    assert json.loads(budget.read_text())["spent"] == 1.0 + 2.5 + 1.0


def test_an_interrupted_run_without_a_reserve_stops_once(tmp_path):
    jobs = matrix.plan(MODELS[1:], ["nautiloid"], ["rules"], 1)
    m, _, _ = make(tmp_path, FakeRun({(f"{PI}~nautiloid~rules~r1", 1): KeyboardInterrupt()}), jobs=jobs)
    with pytest.raises(KeyboardInterrupt):
        m.run()
    m2, _, _ = make(tmp_path, FakeRun(), jobs=jobs)
    assert m2.run() == matrix.COST_UNKNOWN
    assert m2.run() == matrix.FINISHED


def test_a_torn_last_line_is_ignored(tmp_path):
    jobs = matrix.plan(MODELS[:1], ["nautiloid"], ["rules"], 2)
    m, _, _ = make(tmp_path, FakeRun(), jobs=jobs)
    with (tmp_path / "m" / "queue.jsonl").open("a") as f:
        f.write('{"type": "start", "job": "opus~nautil')
    assert m.run() == matrix.FINISHED


def test_guard_waits_then_runs_or_stops(tmp_path):
    clock = Clock()
    answers = iter(["RAM 1.2 GB free", "thermal 92C", None] + [None] * 20)
    run = FakeRun()
    m, _, _ = make(tmp_path, run, clock=clock, guard=lambda: next(answers), guard_poll_s=60, guard_max_wait_s=600)
    assert m.run() == matrix.FINISHED
    assert clock.slept == [60, 60] and len(run.calls) == 8

    hot = tmp_path / "hot"
    hot.mkdir()
    run = FakeRun()
    m, _, clock = make(hot, run, guard=lambda: "thermal 95C", guard_poll_s=60, guard_max_wait_s=180)
    assert m.run() == "guard: thermal 95C"
    assert clock.slept == [60, 60, 60] and run.calls == []
    assert progress(hot)["stop_reason"] == "guard: thermal 95C"


def test_progress_while_running(tmp_path):
    seen = []

    def run_one(job):
        seen.append(progress(tmp_path))
        return RunResult("r", True, False, 0.25)

    jobs = matrix.plan(MODELS[:1], ["nautiloid"], ["rules"], 1)
    m, _, _ = make(tmp_path, run_one, jobs=jobs)
    assert m.run() == matrix.FINISHED
    (p,) = seen
    assert p["state"] == "running" and p["current"]["id"] == "opus~nautiloid~rules~r1" and p["current"]["attempt"] == 1
    assert progress(tmp_path)["passed"] == 0 and progress(tmp_path)["spent_usd"] == 0.25


def test_plan_files(tmp_path):
    m, budget, _ = make(tmp_path, FakeRun())
    m.create("night1", matrix.plan(MODELS, ["nautiloid", "grove"], ["rules"], 2))  # same plan: resume
    with pytest.raises(ValueError, match="another plan"):
        m.create("night1", matrix.plan(MODELS, ["nautiloid"], ["rules"], 2))
    (tmp_path / "m" / "queue.jsonl").write_text('{"type": "start"}\n')
    with pytest.raises(ValueError, match="no v1 plan"):
        m.state()
    for bad in ('{"cap": "lots", "spent": 0}', '{"spent": 1}', '{"cap": -1}', '{"cap": true}', "[]"):
        budget.write_text(bad)
        with pytest.raises(ValueError):
            matrix.read_budget(budget)
    budget.write_text('{"cap": 5}')
    assert matrix.read_budget(budget) == {"cap": 5.0, "spent": 0.0}


def test_progress_without_a_readable_budget(tmp_path):
    m, budget, _ = make(tmp_path, FakeRun(), jobs=[])
    budget.unlink()
    assert m.run() == matrix.FINISHED
    assert progress(tmp_path)["cap_usd"] is None


def test_runs_the_runner_charged_are_not_added_again(tmp_path):
    # play.py added the paid run's cost itself (or the run isn't billed): no double count, no cost-unknown stop
    jobs = matrix.plan(MODELS[:1], ["nautiloid", "grove"], ["rules"], 1)
    run = FakeRun({(j["id"], 1): RunResult("r", True, True, c, None, True) for j, c in zip(jobs, (2.0, None))})
    m, budget, _ = make(tmp_path, run, spent=1.5, jobs=jobs)
    assert m.run() == matrix.FINISHED and len(run.calls) == 2
    assert json.loads(budget.read_text()) == {"cap": 100.0, "spent": 1.5}


def test_a_missing_or_broken_budget_stops_before_a_job(tmp_path):
    run = FakeRun()
    m, budget, _ = make(tmp_path, run)
    budget.unlink()
    assert m.run() == matrix.BUDGET and run.calls == []
    budget.write_text('{"cap": "lots"}')
    assert m.run() == matrix.BUDGET and progress(tmp_path)["stop_reason"] == "budget"


def test_shares_the_harness_budget_file(tmp_path):
    spend = _load_spend()
    run = FakeRun()
    m, budget, _ = make(tmp_path, run, cap=10.0, spent=0.25)
    budget.write_text(json.dumps({"cap": 10, "spent": 0.25, "note": "keep"}))
    spend.add_spend(budget, 0.5, now=1.0, session="S001")  # a paid play.py run on the same machine
    assert m.run() == matrix.FINISHED
    b = spend.read_budget(budget)  # the harness still reads what the matrix wrote
    assert (b["spent"], b["note"], spend.run_cap("pi", 5.0, b)) == (8.75, "keep", (1.25, None))
    assert (budget.parent / "budget.json.lock").exists()


def _load_spend():
    from bg3harness import spend  # runner/spend.py moved into the harness (Phase D.3)

    return spend


def test_records_of_unknown_jobs_and_stray_ends_are_ignored(tmp_path):
    jobs = matrix.plan(MODELS[:1], ["nautiloid"], ["rules"], 1)
    m, _, _ = make(tmp_path, FakeRun(), jobs=jobs)
    with (tmp_path / "m" / "queue.jsonl").open("a") as f:
        f.write(json.dumps({"type": "start", "job": "other", "attempt": 1}) + "\n")
        f.write(json.dumps({"type": "end", "job": jobs[0]["id"], "attempt": 1, "result": {}}) + "\n")
    assert m.state()[jobs[0]["id"]]["status"] == "pending"


# ---------------------------------------------------------------- the legacy CLI adapter


GROVE = {"grove": "grove.resolved.json"}


def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "runtime" / "sessions").mkdir(parents=True)
    return repo


def test_legacy_cli_runs_one_more_run_of_the_series(tmp_path):
    repo = fake_repo(tmp_path)
    record = json.loads((HERE / "fixtures" / "naut2" / "runs.jsonl").read_text().splitlines()[0])
    cmds, envs = [], []

    def execute(cmd, env):
        cmds.append(cmd)
        envs.append(env)
        series = cmd[cmd.index("--playthrough") + 1]
        d = repo / "runtime" / "sessions" / series
        d.mkdir(exist_ok=True)
        n = int(cmd[cmd.index("--runs") + 1])
        with (d / "runs.jsonl").open("a") as f:
            f.write(json.dumps({**record, "series": series, "run": n}) + "\n")
        return 0

    run_one = legacy_cli.LegacyCli(
        "night1",
        {"legacy": ["--no-keeper"]},
        scenario_files={"nautiloid": tmp_path / "nautiloid.resolved.json"},
        condition_env={"legacy": {"BG3AI_TOOLS_OFF": "x"}},
        repo=repo,
        execute=execute,
    )
    job = matrix.plan([{"slug": "opus", "effort": "medium"}], ["nautiloid"], ["legacy"], 1)[0]
    res = run_one(job)
    assert res == RunResult("night1-nautiloid-opus-claude-medium-legacy-r001", True, True, 3.349, None, True)
    assert cmds[0] == [
        "python3", str(repo / "runner" / "play.py"), "run", "--scenario-file", str(tmp_path / "nautiloid.resolved.json"),
        "--playthrough", "night1-nautiloid-opus-claude-medium-legacy", "--client", "claude", "--model", "opus",
        "--runs", "1", "--effort", "medium", "--no-keeper",
    ]  # fmt: skip
    assert run_one(job).run_id.endswith("-r002") and cmds[1][cmds[1].index("--runs") + 1] == "2"
    assert envs == [{"BG3AI_TOOLS_OFF": "x"}] * 2
    with pytest.raises(ValueError, match="condition"):
        run_one({**job, "condition": "rules"})
    with pytest.raises(ValueError, match="no resolved file for scenario 'grove'"):
        run_one({**job, "scenario": "grove"})


def test_legacy_cli_without_a_new_record(tmp_path):
    repo = fake_repo(tmp_path)
    run_one = legacy_cli.LegacyCli("m", {"legacy": []}, scenario_files=GROVE, repo=repo, execute=lambda cmd, env: 2)
    job = matrix.plan([{"slug": "x/y"}], ["grove"], ["legacy"], 1)[0]
    assert run_one(job) == RunResult(None, False, False, None, "no run record (play.py exit 2)")  # not charged: stops
    assert "--effort" not in run_one.command(job, "s", 1)
    assert legacy_cli.series_name("M", job) == "m-grove-x-y-claude-legacy"
    assert legacy_cli.REPO == HERE.parents[2]
    assert (legacy_cli.REPO / "runner" / "play.py").is_file()


def test_legacy_cli_profile_jobs(tmp_path):
    run_one = legacy_cli.LegacyCli(
        "m", {"rules": []}, scenario_files=GROVE, repo=fake_repo(tmp_path), execute=lambda cmd, env: 0
    )
    model = {"slug": "anthropic/claude-opus-5.5", "client": "pi", "profile": "profiles/opus-5.5-pi.toml"}
    job = matrix.plan([model], ["grove"], ["rules"], 1, max_cost={"grove": 12.5})[0]
    cmd = run_one.command(job, "s", 1)
    assert cmd[cmd.index("--client") + 1] == "pi"
    assert cmd[-5:] == ["--profile", "profiles/opus-5.5-pi.toml", "--max-cost", "12.5", "--no-keeper"]
    assert run_one.env(job) == {}
    with pytest.raises(ValueError, match="paid job needs a max_cost"):
        run_one.command({**job, "max_cost": None}, "s", 1)
    sub = {"slug": "claude-opus-5-5", "client": "claude", "profile": "profiles/opus-5.5-claude-code.toml"}
    cmd = run_one.command(matrix.plan([sub], ["grove"], ["rules"], 1)[0], "s", 1)
    assert cmd[-2:] == ["--profile", "profiles/opus-5.5-claude-code.toml"] and "--max-cost" not in cmd
