# pyright: basic
import json
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from bg3bench import matrix_cli
from bg3bench.serve import launch, panel

REPO = Path(__file__).resolve().parents[3]
SCENARIOS = REPO / "packages" / "bench" / "scenarios"


class FakeProc:
    def __init__(self) -> None:
        self.pid, self.signals = 4242, []
        self.rc: int | None = None

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)


class FakePopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        self.procs.append(FakeProc())
        return self.procs[-1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout as matrix_cli reads it: the real profiles and scenarios, a budget file."""
    r = tmp_path / "repo"
    shutil.copytree(REPO / "profiles", r / "profiles")
    shutil.copytree(SCENARIOS, r / "packages" / "bench" / "scenarios")
    (r / "runtime").mkdir()
    (r / "runtime" / "budget.json").write_text(json.dumps({"cap": 10.0, "spent": 1.0}))
    return r


def plan(repo: Path, **form):
    form = {"profiles": ["deepseek-flash"], "scenarios": ["act1_get_item"], "max_cost_usd": 1.5, **form}
    return panel.plan_run(
        form,
        scenarios=panel.load_scenarios(repo / "packages" / "bench" / "scenarios"),
        prof_dir=repo / "profiles",
        budget=repo / "runtime" / "budget.json",
        youtube_verified=True,
    )


@pytest.mark.parametrize(
    "form", [{"repeats": 3}, {"profiles": ["deepseek-flash", "opus-5.5-pi"], "allow_unverified": True}]
)
def test_matrix_file_plays_exactly_the_plans_jobs(repo, form):
    p = plan(repo, **form)
    budget = repo / "runtime" / "budget.json"
    text = launch.matrix_toml(
        "panel-20261028-180000", p, budget=budget, allow_unverified=bool(form.get("allow_unverified"))
    )
    f = repo / "m.toml"
    f.write_text(text)
    m = matrix_cli.load(f, repo)
    # matrix_cli names a model by its profile's path, the panel by the profile's name
    assert [{**j, "profile": Path(j["profile"]).stem} for j in m["jobs"]] == p["jobs"]
    assert {Path(j["profile"]).parent for j in m["jobs"]} == {repo / "profiles"}
    assert m["budget"] == budget and m["allow_gaps"] is False and m["skipped"] == {}


def test_refusal_of_plans_that_cant_start(repo):
    ok = plan(repo)
    assert launch.refusal(ok) is None

    def why(plan) -> str:
        return launch.refusal(plan) or ""

    assert "no job" in why({**ok, "jobs": []})
    assert "cost cap" in why({**ok, "needs_max_cost": ["a"]})
    assert "budget file" in why({**ok, "budget": {**ok["budget"], "error": "gone"}})
    assert "doesn't fit" in why({**ok, "fits_budget": False})
    assert "pilot" in why({**ok, "gaps": {"deepseek-flash": ["memory"]}})


def test_matrix_process_start_state_stop(repo):
    popen = FakePopen()
    clock = [100.0]
    mp = launch.MatrixProcess(
        repo, popen=popen, which=lambda n: f"/usr/bin/{n}", now=lambda: clock[0], environ={"DISPLAY": ":1"}
    )
    assert mp.state() is None
    with pytest.raises(panel.PanelError, match="no matrix"):
        mp.stop()
    log = repo / "runtime" / "x.log"
    mp.start("panel-x", repo / "runtime" / "x.toml", log, record=False)
    ((argv, kw),) = popen.calls
    assert argv == ["/usr/bin/uv", "run", "python", "runner/play.py", "bench", "run", "--matrix",
                    str(repo / "runtime" / "x.toml"), "--runner", "session"]  # fmt: skip
    assert kw["cwd"] == repo and kw["start_new_session"] and kw["stderr"] == subprocess.STDOUT and log.is_file()
    assert kw["env"] == {"DISPLAY": ":1", "BG3AI_BENCH_RECORD": "0"}  # Record off reaches the run host
    st = mp.state()
    assert st == {"name": "panel-x", "pid": 4242, "started": 100.0, "log": str(log), "running": True,
                  "returncode": None, "stopping": False, "signalled": False}  # fmt: skip
    assert mp.escalate() is False  # no Stop yet
    with pytest.raises(panel.PanelError, match="is running"):
        mp.start("panel-y", repo / "y.toml", log, record=True)
    mp.stop()  # runtime/STOP, the graceful stop play.py bench reads; no signal yet
    assert (repo / "runtime" / "STOP").read_text() == "100.0" and popen.procs[0].signals == []
    assert st_of(mp)["stopping"] and not st_of(mp)["signalled"]
    with pytest.raises(panel.PanelError, match="stopping already"):
        mp.stop()
    clock[0] = 100.0 + launch.STOP_GRACE_S - 1
    assert mp.escalate() is False and popen.procs[0].signals == []
    clock[0] = 100.0 + launch.STOP_GRACE_S
    assert mp.escalate() is True and popen.procs[0].signals == [signal.SIGTERM] and st_of(mp)["signalled"]
    assert mp.escalate() is False  # one signal only: a second would exit without recording the run
    assert popen.procs[0].signals == [signal.SIGTERM]
    popen.procs[0].rc = 0
    assert st_of(mp)["running"] is False and st_of(mp)["returncode"] == 0 and mp.escalate() is False
    mp.start("panel-y", repo / "y.toml", log, record=True)  # the first one ended: a new start is fine
    assert len(popen.calls) == 2 and popen.calls[1][1]["env"]["BG3AI_BENCH_RECORD"] == "1"


def test_matrix_process_needs_uv(repo):
    mp = launch.MatrixProcess(repo, popen=FakePopen(), which=lambda n: None)
    with pytest.raises(panel.PanelError, match="uv is not on PATH"):
        mp.start("panel-x", repo / "x.toml", repo / "x.log", record=True)


def no_proc(pid):
    return None


def test_start_writes_a_new_matrix_file_and_starts_it(repo):
    popen = FakePopen()
    mp = launch.MatrixProcess(repo, popen=popen, which=lambda n: n)
    rt = repo / "runtime"
    r = launch.start(
        mp, plan(repo), runtime=rt, budget=rt / "budget.json", allow_unverified=False, now=0.0,
        submit_when_done=True, proc=no_proc,
    )  # fmt: skip
    f = rt / "panel" / "panel-19700101-000000.toml"
    assert r["matrix_file"] == str(f) and f.is_file() and r["started"]["name"] == "panel-19700101-000000"
    assert launch.read_choices(rt, "panel-19700101-000000") == {
        "name": "panel-19700101-000000", "record": True, "submit_when_done": True, "auto": {}
    }  # fmt: skip
    assert popen.calls[0][1]["env"]["BG3AI_BENCH_RECORD"] == "1"
    assert matrix_cli.load(f, repo)["name"] == "panel-19700101-000000"
    with pytest.raises(panel.PanelError, match="is running"):
        launch.start(mp, plan(repo), runtime=rt, budget=rt / "budget.json", allow_unverified=False, now=1.0)
    popen.procs[0].rc = 1
    with pytest.raises(panel.PanelError, match="this second already"):
        launch.start(mp, plan(repo), runtime=rt, budget=rt / "budget.json", allow_unverified=False, now=0.0)
    assert len(popen.calls) == 1
    with pytest.raises(panel.PanelError, match="can't start: the reserve"):
        launch.start(
            mp, plan(repo, max_cost_usd=50.0), runtime=rt, budget=rt / "budget.json", allow_unverified=False, now=2.0
        )
    assert len(popen.calls) == 1


def st_of(mp: launch.MatrixProcess) -> dict:
    st = mp.state()
    assert st is not None
    return st


def test_submit_when_done_needs_record(repo):
    popen = FakePopen()
    rt = repo / "runtime"
    with pytest.raises(panel.PanelError, match="needs Record"):
        launch.start(
            launch.MatrixProcess(repo, popen=popen, which=lambda n: n), plan(repo), runtime=rt,
            budget=rt / "budget.json", allow_unverified=False, now=0.0, record=False, submit_when_done=True,
            proc=no_proc,
        )  # fmt: skip
    assert popen.calls == [] and not (rt / "panel").exists()


def test_flag():
    assert launch.flag({}, "record", True) is True and launch.flag({"record": False}, "record", True) is False
    with pytest.raises(panel.PanelError, match="record must be true or false"):
        launch.flag({"record": "no"}, "record", True)


@pytest.mark.parametrize(
    ("argv", "want"),
    [
        (
            ["/r/.venv/bin/python3", "runner/play.py", "bench", "run", "--matrix", "m.toml", "--runner", "session"],
            "m.toml",
        ),
        (["python", "/r/runner/play.py", "bench", "resume", "--matrix=x/m.toml"], "x/m.toml"),
        (["python", "runner/play.py", "bench", "status", "--matrix", "m.toml"], None),
        (["python", "runner/play.py", "run", "--until", "M1"], None),
        (["python", "runner/play.py", "bench", "run", "--matrix"], None),
        (["bash", "-c", "sleep 1"], None),
        ([], None),
    ],
)
def test_bench_matrix_arg(argv, want):
    assert launch.bench_matrix_arg(argv) == want


def test_found_reads_the_runner_lock_and_the_process(tmp_path):
    rt = tmp_path / "runtime"
    rt.mkdir()
    repo = tmp_path
    procs: dict[int, tuple[list[str], Path]] = {}
    assert launch.found(rt, procs.get) is None  # no runner.lock
    (rt / "runner.lock").write_text("")
    assert launch.found(rt, procs.get) is None  # a lock with no pid
    (rt / "runner.lock").write_text("77")
    assert launch.found(rt, procs.get) is None  # a stale pid: no such process
    procs[77] = (["python", "runner/play.py", "run", "--until", "M1"], repo)
    assert launch.found(rt, procs.get) is None  # a playthrough holds the lock: no matrix
    procs[77] = (["python", "runner/play.py", "bench", "run", "--matrix", "runtime/panel/m.toml"], repo)
    (rt / "panel").mkdir()
    (rt / "panel" / "m.toml").write_text('name = "night2"\n')
    launch.write_choices(rt, {"name": "night2", "record": True, "submit_when_done": False, "auto": {}})
    got = launch.found(rt, procs.get)
    assert got == {
        "name": "night2", "pid": 77, "matrix_file": str(rt / "panel" / "m.toml"), "running": True, "stopping": False,
        "choices": {"name": "night2", "record": True, "submit_when_done": False, "auto": {}},
    }  # fmt: skip
    launch.request_stop(rt, 5.0)
    assert (rt / "STOP").read_text() == "5.0" and found_of(rt, procs.get)["stopping"] is True
    (rt / "panel" / "m.toml").write_text("not toml [")
    assert found_of(rt, procs.get)["name"] == "m"  # the file's stem when its name can't be read


def found_of(rt: Path, proc) -> dict:
    got = launch.found(rt, proc)
    assert got is not None
    return got


def test_start_refuses_while_a_found_matrix_plays(repo):
    popen = FakePopen()
    rt = repo / "runtime"
    (rt / "runner.lock").write_text("9")
    (rt / "other.toml").write_text('name = "other"\n')
    proc = {9: (["py", "runner/play.py", "bench", "run", "--matrix", "runtime/other.toml"], repo)}.get
    with pytest.raises(panel.PanelError, match="matrix other is running \\(pid 9\\)"):
        launch.start(
            launch.MatrixProcess(repo, popen=popen, which=lambda n: n), plan(repo), runtime=rt,
            budget=rt / "budget.json", allow_unverified=False, now=0.0, proc=proc,
        )  # fmt: skip
    assert popen.calls == []


def test_read_proc_of_this_process_and_of_none():
    import os
    import sys

    assert launch.read_proc(os.getpid()) == (sys.orig_argv, Path.cwd())
    assert launch.read_proc(2**22 + 1) is None  # above Linux's pid_max


def test_matrix_process_ended_after_stop_is_not_signalled(repo):
    popen, clock = FakePopen(), [0.0]
    mp = launch.MatrixProcess(repo, popen=popen, which=lambda n: n, now=lambda: clock[0], runtime=repo / "elsewhere")
    (repo / "elsewhere").mkdir()
    mp.start("panel-x", repo / "x.toml", repo / "x.log", record=True)
    mp.stop()
    assert (repo / "elsewhere" / "STOP").is_file()  # the runtime it was given
    popen.procs[0].rc = 0  # STOP ended it in time
    clock[0] = launch.STOP_GRACE_S * 2
    assert mp.escalate() is False and popen.procs[0].signals == []
