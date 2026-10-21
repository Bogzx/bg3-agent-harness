# pyright: basic
import io
import json
from pathlib import Path

import pytest

from bg3bench import matrix, matrix_cli, scenario
from bg3bench.matrix import RunResult

PI = 'client = "pi"\nmodel = "anthropic/claude-opus-5.5"\neffort = "high"\n'
CLAUDE = 'client = "claude"\nmodel = "claude-opus-5-5"\neffort = "high"\n'


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


def cli(repo, *argv, run_one=None):
    out, clock = io.StringIO(), Clock()
    code = matrix_cli.main(list(argv), repo=repo, run_one=run_one, now=clock.now, sleep=clock.sleep, out=out)
    return code, out.getvalue()


def test_plan_is_a_dry_run_table(tmp_path):
    repo = fake_repo(tmp_path, spent=1.5)
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path)))
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["job", "client", "effort", "max_cost"]
    assert lines[1].split() == ["anthropic_claude-opus-5.5~nautiloid~rules~r1", "pi", "high", "$2.50"]
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
    code, out = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 2 and "play.py can't play 'rules' yet" in capsys.readouterr().err
    code, out = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 0 and "condition rules: refused by run" in out


def test_run_without_run_one_builds_the_legacy_adapter(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path)
    seen = []
    kwdefaults = matrix_cli.legacy_cli.LegacyCli.__init__.__kwdefaults__
    monkeypatch.setitem(kwdefaults, "execute", lambda cmd, env: seen.append(cmd) or 2)
    path = matrix_file(tmp_path)
    code, out = cli(repo, "run", "--matrix", str(path))
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
    code, _ = cli(repo, "plan", "--matrix", str(matrix_file(tmp_path, body)))
    assert code == 2 and "gold: goal" in capsys.readouterr().err


def test_a_changed_matrix_file_is_refused(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    path = matrix_file(tmp_path)
    cli(repo, "run", "--matrix", str(path), run_one=lambda job: RunResult("r", True, True, 0.0, None, True))
    changed = path.read_text().replace("repeats = 2", "repeats = 3")
    code, _ = cli(repo, "run", "--matrix", str(matrix_file(tmp_path, changed)))
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
        ("name = 'm'\nscenarios = ['grove']\nmodels = ['profiles/bare.toml']", "needs client and model"),
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
    assert [j["client"] for j in m["jobs"]].count("claude") == 1
