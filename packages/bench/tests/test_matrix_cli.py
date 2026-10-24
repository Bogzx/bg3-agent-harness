# pyright: basic
import io
import json
import re
from pathlib import Path

import pytest

from bg3bench import conditions, matrix, matrix_cli, scenario
from bg3bench.matrix import RunResult

REPO = Path(__file__).resolve().parents[3]
PI = (REPO / "profiles" / "opus-5.5-pi.toml").read_text().replace('status = "offline"', 'status = "verified"')
CLAUDE = 'client = "claude"\nmodel = "claude-opus-5-5"\neffort = "high"\nstatus = "verified"\n'


NAUTILOID = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "nautiloid.json").read_text())


def fake_repo(tmp_path: Path, spent: float = 0.0) -> Path:
    repo = tmp_path / "repo"
    (repo / "packages" / "bench" / "scenarios").mkdir(parents=True)
    (repo / "profiles").mkdir()
    (repo / "runtime").mkdir()
    for sc in (NAUTILOID, {**NAUTILOID, "name": "grove", "until": "grove"}):
        (repo / "packages" / "bench" / "scenarios" / f"{sc['name']}.json").write_text(json.dumps(sc))
    (repo / "profiles" / "pi.toml").write_text(PI)
    (repo / "profiles" / "claude.toml").write_text(CLAUDE)
    (repo / "runtime" / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": spent}))
    return repo


def matrix_file(tmp_path: Path, body: str | None = None) -> Path:
    path = tmp_path / "night1.toml"
    path.write_text(
        body
        or """
name = "night1"
models = ["profiles/pi.toml", "profiles/claude.toml"]
scenarios = ["nautiloid", "grove"]
conditions = ["rules"]
repeats = 2
allow_gaps = true

[budget]
max_cost = { nautiloid = 2.5, grove = 4 }
"""
    )
    return path


class Clock:
    t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def cli(repo, *argv, run_one=None, host=None):
    out, clock = io.StringIO(), Clock()
    code = matrix_cli.main(list(argv), repo=repo, run_one=run_one, host=host, now=clock.now, sleep=clock.sleep, out=out)
    return code, out.getvalue()


@pytest.mark.parametrize(
    ("status", "allow", "refused"),
    [
        ("", False, "profile status 'offline' (allow_unverified = true runs it)"),
        ('status = "offline"', False, "profile status 'offline' (allow_unverified = true runs it)"),
        ('status = "offline"', True, None),
        ('status = "unsupported"', True, "profile status 'unsupported'"),
    ],
)
def test_a_profile_never_played_live_needs_allow_unverified(tmp_path, status, allow, refused):
    repo = fake_repo(tmp_path)
    (repo / "profiles" / "claude.toml").write_text(f'client = "claude"\nmodel = "claude-opus-5-5"\n{status}\n')
    body = matrix_file(tmp_path).read_text()
    path = matrix_file(
        tmp_path, body.replace("allow_gaps = true", "allow_gaps = true\nallow_unverified = true") if allow else body
    )
    if refused is None:
        assert len(matrix_cli.load(path, repo)["jobs"]) == 8
    else:
        with pytest.raises(matrix_cli.MatrixFileError, match=re.escape(f"claude.toml: {refused}")):
            matrix_cli.load(path, repo)


def test_a_profile_is_checked_as_the_run_loads_it(tmp_path):
    repo = fake_repo(tmp_path)
    (repo / "profiles" / "pi.toml").write_text('client = "pi"\nmodel = "m"\nstatus = "verified"\n')
    with pytest.raises(matrix_cli.MatrixFileError, match=r"pi\.toml: pi needs an \[endpoint\] table"):
        matrix_cli.load(matrix_file(tmp_path), repo)


def test_plan_is_a_dry_run_table(tmp_path):
    repo = fake_repo(tmp_path, spent=1.5)
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path)))
    assert code == 0
    lines = out.splitlines()
    head = ["job", "client", "effort", "profile", "scenario", "condition", "max_cost", "reserve"]
    assert lines[0].split() == head
    pi = ["anthropic_claude-opus-5.5~nautiloid~rules~r1", "pi", "medium", "pi.toml", "nautiloid", "rules"]
    assert lines[1].split() == [*pi, "$2.50", "$2.50"]
    claude = next(ln.split() for ln in lines if ln.startswith("claude-opus-5-5~grove~rules~r1"))
    # never charged nor capped by the matrix (fixlive: its $0.25 stopped a Claude Code run)
    assert claude[1:] == ["claude", "high", "claude.toml", "grove", "rules", "-", "-"]
    assert "8 jobs (4 paid), reserve $13.00" in out
    assert "spent: $1.50 of $20.00" not in out and "$1.50 of $20.00 spent" in out
    assert "condition rules: a pilot (allow_gaps); play.py lacks a rules-only prompt" in out
    assert not (repo / "runtime" / "matrix").exists()  # nothing written


def test_run_status_resume(tmp_path):
    repo = fake_repo(tmp_path)
    path = matrix_file(tmp_path)
    calls = []

    def run_one(job):
        calls.append(job)
        if len(calls) == 3:
            return RunResult(None, False, False, None, "runner_error", True)
        return RunResult(f"{job['id']}#{len(calls)}", True, len(calls) % 2 == 1, 1.0, None, True)

    code, out = cli(repo, "status", "--matrix", str(path))
    assert code == 2  # no queue yet
    code, out = cli(repo, "run", "--matrix", str(path), run_one=run_one)
    assert code == 0 and out.rstrip().endswith("stopped: finished") and len(calls) == 9
    assert calls[0]["profile"] == str(repo / "profiles" / "pi.toml") and calls[0]["max_cost"] == 2.5
    code, out = cli(repo, "status", "--matrix", str(path))
    rows = {line.split()[0]: line for line in out.splitlines()[1:]}
    first = "anthropic_claude-opus-5.5~nautiloid~rules~r1"
    assert code == 0 and rows[first].split()[1:4] == ["done", "1", "pass"]
    assert "pending" not in out
    progress = json.loads((repo / "runtime" / "matrix" / "night1" / "progress.json").read_text())
    assert (progress["done"], progress["invalid_runs"], progress["spent_usd"]) == (8, 1, 0.0)  # charged by play.py
    code, out = cli(repo, "resume", "--matrix", str(path), run_one=run_one)
    assert code == 0 and len(calls) == 9


def test_status_of_an_invalid_and_an_interrupted_attempt(tmp_path):
    repo = fake_repo(tmp_path)
    path = matrix_file(tmp_path, 'name = "m"\nmodels = ["profiles/claude.toml"]\nscenarios = ["grove"]\n'
                       'conditions = ["rules"]\nrepeats = 2\n')  # fmt: skip
    answers = iter([RunResult(None, False, False, 0.0, "runner_error", True)] * 3 + [KeyboardInterrupt()])

    def run_one(job):
        out = next(answers)
        if isinstance(out, BaseException):
            raise out
        return out

    with pytest.raises(KeyboardInterrupt):
        cli(repo, "run", "--matrix", str(path), run_one=run_one)
    code, out = cli(repo, "status", "--matrix", str(path))
    assert code == 0 and "invalid: runner_error" in out and "running or interrupted" in out and "gave_up" in out


def test_the_budget_stop_is_exit_1(tmp_path):
    repo = fake_repo(tmp_path, spent=19.0)
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path)), run_one=lambda job: None)
    assert code == 1 and out.rstrip().endswith(f"stopped: {matrix.BUDGET}")


def test_run_refuses_a_condition_play_py_lacks(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    body = matrix_file(tmp_path).read_text().replace("allow_gaps = true", "")
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)), "--runner", "legacy")
    assert code == 2 and "play.py can't play 'rules' yet" in capsys.readouterr().err
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 0 and "condition rules: refused by run --runner legacy" in out
    assert "condition rules through claude: refused by run; a session run lacks claude reads" in out
    assert "through pi" not in out  # pi plays rules exactly


def test_run_without_run_one_plays_run_session_on_the_host(tmp_path, capsys):
    from test_session_run import Host

    repo = fake_repo(tmp_path)
    body = "name = 'm'\nscenarios = ['nautiloid']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']"
    code, _ = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 2 and "no run_session host" in capsys.readouterr().err  # the CLI alone has no host yet
    code, _ = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)), host=Host([]))
    assert code == 2 and "can't play rules exactly" in capsys.readouterr().err  # claude's prompt is coached
    body += "\nallow_gaps = true"
    host = Host([[("exited", 5)]], cost=None)
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)), host=host)
    assert code == 0 and out.rstrip().endswith(f"stopped: {matrix.FINISHED}")
    ((_job, sc, run_dir),) = host.started
    assert sc == scenario.load(repo / "packages" / "bench" / "scenarios" / "nautiloid.json")  # v2, not resolved
    assert run_dir.parent == repo / "runtime" / "sessions" and (run_dir / "result.json").is_file()
    assert json.loads((run_dir / "result.json").read_text())["pilot"] == [conditions.GAP_CLAUDE_PROMPT]
    assert not (repo / "runtime" / "matrix" / "m" / "scenarios").exists()  # resolved files are the legacy path's


def test_run_stops_when_the_host_says_stopped(tmp_path):
    from test_session_run import Host

    repo = fake_repo(tmp_path)
    body = "name = 'm'\nscenarios = ['nautiloid']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']"
    body += "\nallow_gaps = true"
    host = Host([[("exited", 5)]], cost=None)
    host.stopped = True  # LiveRunHost.stopped: a signal or runtime/STOP
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)), host=host)
    assert code == 1 and out.rstrip().endswith(f"stopped: {matrix.STOPPED}") and host.started == []


def test_run_without_run_one_builds_the_legacy_adapter(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path)
    seen = []
    kwdefaults = matrix_cli.legacy_cli.LegacyCli.__init__.__kwdefaults__
    monkeypatch.setitem(kwdefaults, "execute", lambda cmd, env: seen.append(cmd) or 2)
    path = matrix_file(tmp_path)
    code, out = cli(repo, "run", "--matrix", str(path), "--runner", "legacy")
    # the adapter ran play.py in the fake repo; no run record: an invalid run with an unknown cost stops the matrix
    assert code == 1 and out.rstrip().endswith(f"stopped: {matrix.COST_UNKNOWN}")
    assert seen[0][1] == str(repo / "runner" / "play.py") and "--profile" in seen[0] and "--max-cost" in seen[0]
    # the job's scenario goes to play.py as its resolved file, written into the matrix folder
    resolved = repo / "runtime" / "matrix" / "night1" / "scenarios" / "nautiloid.json"
    assert seen[0][3:5] == ["--scenario-file", str(resolved)]
    want = scenario.resolve(scenario.load(repo / "packages" / "bench" / "scenarios" / "nautiloid.json"))
    assert json.loads(resolved.read_text()) == want
    assert json.loads((resolved.parent / "grove.json").read_text())["until"] == "grove"


def test_a_scenario_the_legacy_runner_cant_stop_is_refused(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    sc = {**NAUTILOID, "name": "gold", "goal": {"gold": {"gte": 1000}}}
    del sc["until"]
    (repo / "packages" / "bench" / "scenarios" / "gold.json").write_text(json.dumps(sc))
    body = "name = 'm'\nscenarios = ['gold']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']"
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 0 and "scenario gold: refused by run --runner legacy" in out  # run_session plays any goal
    gaps = matrix_file(tmp_path, body + "\nallow_gaps = true")
    code, _ = cli(repo, "run", "--matrix", str(gaps), "--runner", "legacy")
    assert code == 2 and "gold: goal" in capsys.readouterr().err


def test_a_scenario_that_is_not_ready_is_skipped_visibly(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    note = {"where": "w", "objective": "o"}
    start = {"kind": "save", "save": None, "save_sha1": None, "needs_save": "Act 1, the Grove", "note": note}
    char = {"name": "Tav", "brief": "b"}
    for name, st in (("unmade", start), ("refused", {**start, "save": "Tav-1__SCN-refused", "not_ready": "refused"})):
        st = {k: v for k, v in st.items() if name == "unmade" or k != "needs_save"}
        sc = {**NAUTILOID, "name": name, "start": st, "profile": {"character": char}}
        (repo / "packages" / "bench" / "scenarios" / f"{name}.json").write_text(json.dumps(sc))
    body = "name = 'm'\nscenarios = ['nautiloid', 'unmade', 'refused']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']"
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 0 and "1 jobs (0 paid)" in out
    assert "scenario refused: skipped, not ready: refused" in out
    assert "scenario unmade: skipped, not ready: no start save yet (start.needs_save: where to make it)" in out
    calls = []
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)), run_one=lambda job: calls.append(job)
                    or RunResult("r", True, True, 0.0, None, True))  # fmt: skip
    assert code == 0 and [j["scenario"] for j in calls] == ["nautiloid"] and "scenario unmade: skipped" in out
    only = body.replace("'nautiloid', ", "").replace("name = 'm'", "name = 'm2'")
    code, _ = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, only)), run_one=lambda job: None)
    assert code == 2 and "no scenario of it is ready" in capsys.readouterr().err


def test_a_changed_matrix_file_is_refused(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    path = matrix_file(tmp_path)
    cli(repo, "run", "--matrix", str(path), run_one=lambda job: RunResult("r", True, True, 0.0, None, True))
    changed = path.read_text().replace("repeats = 2", "repeats = 3")
    code, _ = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, changed)), "--runner", "legacy")
    assert code == 2 and "another plan" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("name = 'a b'", "slug"),
        ("name = 'm'\nrepeats = 0", "repeats"),
        ("name = 'm'\nscenarios = []", "scenarios must be"),
        ("name = 'm'\nscenarios = ['nope']", "no scenario 'nope'"),
        ("name = 'm'\nscenarios = ['grove']\n[budget]\nmax_cost = { grove = -1 }", "USD > 0"),
        ("name = 'm'\nscenarios = ['grove']\n[budget]\nmax_cost = { nautiloid = 1 }", "not in the matrix"),
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['profiles/none.toml']", "none.toml"),
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['packages/bench/scenarios/grove.json']", "grove.json"),
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['profiles/bare.toml']", "bare.toml: model must be a non-empty string"),
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['profiles/pi.toml']\nconditions = ['rules']", "spends money"),
        ("name = 'm'\nscenarios = ['grove', 'grove']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']",
         "duplicate"),
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['profiles/claude.toml']\nconditions = ['nope']", "nope"),
        ("not toml =", "night1.toml"),
        ("name = 'm'\nscenarios = ['grove']\n[[group]]\nscenarios = ['grove']", "either [[group]]"),
        ("name = 'm'\n[[group]]\nscenarios = ['nope']", "no scenario 'nope'"),
        ("name = 'm'\n" + "[[group]]\nscenarios = ['grove']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']\n" * 2,
         "two groups share"),
    ],
)  # fmt: skip
def test_bad_matrix_files_are_refused(tmp_path, capsys, body, why):
    repo = fake_repo(tmp_path)
    (repo / "profiles" / "bare.toml").write_text('client = "pi"\n')
    code, _ = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 2 and why in capsys.readouterr().err


def test_plan_with_an_unreadable_budget(tmp_path):
    repo = fake_repo(tmp_path)
    (repo / "runtime" / "budget.json").unlink()
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path)))
    assert code == 0 and "unreadable" in out and "stops before its first job" in out


GROUPS = """
name = "pilot"
allow_gaps = true

[[group]]
models = ["profiles/pi.toml"]
scenarios = ["nautiloid"]
conditions = ["rules", "rules+coaching@1a2b3c4d"]
repeats = 2

[[group]]
models = ["profiles/claude.toml"]
scenarios = ["nautiloid"]
conditions = ["rules+coaching@1a2b3c4d"]

[budget]
max_cost = { nautiloid = 0.25 }
"""


def test_groups_merge_repeat_major(tmp_path):
    m = matrix_cli.load(matrix_file(tmp_path, GROUPS), fake_repo(tmp_path))
    assert [(j["model"], j["condition"], j["repeat"]) for j in m["jobs"]] == [
        ("anthropic/claude-opus-5.5", "rules", 1),
        ("anthropic/claude-opus-5.5", "rules+coaching@1a2b3c4d", 1),
        ("claude-opus-5-5", "rules+coaching@1a2b3c4d", 1),
        ("anthropic/claude-opus-5.5", "rules", 2),
        ("anthropic/claude-opus-5.5", "rules+coaching@1a2b3c4d", 2),
    ]
    assert m["conditions"] == ["rules", "rules+coaching@1a2b3c4d"]
    code, out = cli(fake_repo(tmp_path / "b"), "plan", "--matrix", str(matrix_file(tmp_path, GROUPS)))
    assert code == 0 and "5 jobs (4 paid), reserve $1.00" in out


def test_the_pilot_matrix_loads_in_this_repo():
    repo = Path(__file__).resolve().parents[3]
    m = matrix_cli.load(repo / "packages" / "bench" / "matrices" / "pilot-2026-10-22.toml", repo)
    paid = [j for j in m["jobs"] if j["client"] in matrix.PAID_CLIENTS]
    assert len(paid) == 6 and sum(j["max_cost"] for j in paid) <= 1.5  # the DeepSeek budget
    assert [j["client"] for j in m["jobs"]].count("claude") == 2  # both conditions, like DeepSeek (pi r14)


def test_the_variance_pilot_plan(tmp_path):
    """BENCHMARK-PLAN.md phase 2: 2 models x 10 scenarios x 6 runs; the paid half is bounded at $15 (play.py bench
    plan's output through this repo's profiles and scenarios, against a budget file of its own). Two of the ten have
    no start save yet (research/ovh/live6.md §2): they are skipped, so 8 scenarios play (96 jobs, $12.00)."""
    real = Path(__file__).resolve().parents[3]
    repo = tmp_path / "repo"
    (repo / "runtime").mkdir(parents=True)
    for d in ("packages", "profiles"):
        (repo / d).symlink_to(real / d)
    (repo / "runtime" / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": 2.0}))
    code, out = cli(repo, "plan", "--matrix", str(real / "packages" / "bench" / "matrices" / "variance-pilot.toml"))
    assert code == 0
    rows = [ln.split() for ln in out.splitlines()[1:97]]
    assert len(rows) == 96 and [r[0].rsplit("~", 1)[1] for r in rows] == [
        f"r{i}" for i in range(1, 7) for _ in range(16)
    ]
    cells = {(r[1], r[4]) for r in rows}
    assert {c for c, _ in cells} == {"pi", "claude"} and len(cells) == 16  # every model plays every ready scenario
    assert {r[2] for r in rows} == {"medium"} and {r[5] for r in rows} == {"rules"}
    assert all(r[6:] == (["$0.25", "$0.25"] if r[1] == "pi" else ["-", "-"]) for r in rows)
    assert "96 jobs (48 paid), reserve $12.00" in out
    assert "scenario fight_act1_windmill_tactician: skipped, not ready: no start save yet" in out
    assert (
        "scenario fight_act2_house_of_healing: skipped, not ready: not made" in out
        and "budget " in out
        and "$2.00 of $20.00 spent" in out
    )
    assert "condition rules: a pilot (allow_gaps); play.py lacks a rules-only prompt" in out
    m = matrix_cli.load(real / "packages" / "bench" / "matrices" / "variance-pilot.toml", real)
    fams = sorted(sc["family"] for sc in m["scenarios"].values())
    assert fams == ["choice"] * 2 + ["exploration"] * 2 + ["fight"] * 4 + ["sandbox"] * 2
    assert not any(sc["holdout"] for sc in m["scenarios"].values())
    assert {"fight_act1_windmill", "fight_act1_windmill_tactician"} <= set(m["scenarios"])
