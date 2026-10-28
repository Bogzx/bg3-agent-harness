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
    mp = launch.MatrixProcess(repo, popen=popen, which=lambda n: f"/usr/bin/{n}", now=lambda: 100.0)
    assert mp.state() is None
    with pytest.raises(panel.PanelError, match="no matrix"):
        mp.stop()
    log = repo / "runtime" / "x.log"
    mp.start("panel-x", repo / "runtime" / "x.toml", log)
    ((argv, kw),) = popen.calls
    assert argv == ["/usr/bin/uv", "run", "python", "runner/play.py", "bench", "run", "--matrix",
                    str(repo / "runtime" / "x.toml"), "--runner", "session"]  # fmt: skip
    assert kw["cwd"] == repo and kw["start_new_session"] and kw["stderr"] == subprocess.STDOUT and log.is_file()
    st = mp.state()
    assert st == {"name": "panel-x", "pid": 4242, "started": 100.0, "log": str(log), "running": True,
                  "returncode": None, "stopping": False}  # fmt: skip
    with pytest.raises(panel.PanelError, match="is running"):
        mp.start("panel-y", repo / "y.toml", log)
    mp.stop()
    assert popen.procs[0].signals == [signal.SIGTERM] and st_of(mp)["stopping"]
    with pytest.raises(panel.PanelError, match="stopping already"):
        mp.stop()  # a second SIGTERM would exit without recording the run
    assert popen.procs[0].signals == [signal.SIGTERM]
    popen.procs[0].rc = 0
    assert st_of(mp)["running"] is False and st_of(mp)["returncode"] == 0
    mp.start("panel-y", repo / "y.toml", log)  # the first one ended: a new start is fine
    assert len(popen.calls) == 2


def test_matrix_process_needs_uv(repo):
    mp = launch.MatrixProcess(repo, popen=FakePopen(), which=lambda n: None)
    with pytest.raises(panel.PanelError, match="uv is not on PATH"):
        mp.start("panel-x", repo / "x.toml", repo / "x.log")


def test_start_writes_a_new_matrix_file_and_starts_it(repo):
    popen = FakePopen()
    mp = launch.MatrixProcess(repo, popen=popen, which=lambda n: n)
    rt = repo / "runtime"
    r = launch.start(mp, plan(repo), runtime=rt, budget=rt / "budget.json", allow_unverified=False, now=0.0)
    f = rt / "panel" / "panel-19700101-000000.toml"
    assert r["matrix_file"] == str(f) and f.is_file() and r["started"]["name"] == "panel-19700101-000000"
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
