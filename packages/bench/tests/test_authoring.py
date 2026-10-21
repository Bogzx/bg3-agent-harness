# pyright: basic
import hashlib
import json

import pytest

from bg3bench import authoring, matrix, scenario
from bg3bench.matrix import RunResult

NAME = "act1_gold_1000"
FOLDER = f"Tav-4242__SCN-{NAME}"


def make_save(tmp_path, folder=FOLDER, label=f"SCN-{NAME}"):
    d = tmp_path / "game_saves" / folder
    d.mkdir(parents=True)
    (d / f"{label}.lsv").write_bytes(b"LSPK fake save bytes")
    (d / f"{label}.WebP").write_bytes(b"thumb")
    return d


def register(tmp_path, **kw):
    return authoring.register_save(make_save(tmp_path), tmp_path / "store", NAME, now=lambda: 1792300000.0, **kw)


def test_register_copies_and_hashes_the_save(tmp_path):
    reg = register(tmp_path, inspector=lambda p: [])
    src = tmp_path / "game_saves" / FOLDER
    copy = tmp_path / "store" / NAME / FOLDER
    assert (copy / f"SCN-{NAME}.lsv").read_bytes() == (src / f"SCN-{NAME}.lsv").read_bytes()
    assert sorted(p.name for p in src.iterdir()) == [f"SCN-{NAME}.WebP", f"SCN-{NAME}.lsv"]  # original untouched
    assert reg["lsv_sha1"] == hashlib.sha1(b"LSPK fake save bytes").hexdigest()
    assert reg["owner"] == "Tav" and reg["folder"] == FOLDER and reg["inspector"] == []
    assert [f["path"] for f in reg["files"]] == [f"SCN-{NAME}.WebP", f"SCN-{NAME}.lsv"]
    assert reg["files"][1]["sha256"] == hashlib.sha256(b"LSPK fake save bytes").hexdigest()
    assert json.loads((tmp_path / "store" / NAME / "registration.json").read_text()) == reg
    with pytest.raises(authoring.AuthoringError, match="registered once"):
        authoring.register_save(src, tmp_path / "store", NAME, now=lambda: 0.0)


@pytest.mark.parametrize(
    "folder,label,msg",
    [
        (f"Claude-1__SCN-{NAME}", f"SCN-{NAME}", "names 'claude'"),
        (f"tav-1__SCN-{NAME}", f"SCN-{NAME}", "one plain capitalised word"),
        (f"Mr Tav-1__SCN-{NAME}", f"SCN-{NAME}", "one plain capitalised word"),
        ("Tav-1__S012-camp", "S012-camp", "re-save it as SCN-act1_gold_1000"),
    ],
)
def test_register_refuses_non_neutral_saves(tmp_path, folder, label, msg):
    src = make_save(tmp_path, folder, label)
    with pytest.raises(authoring.AuthoringError, match=msg):
        authoring.register_save(src, tmp_path / "store", NAME, now=lambda: 0.0)
    assert not (tmp_path / "store").exists()


def test_register_refuses_incomplete_saves_and_inspector_findings(tmp_path):
    src = make_save(tmp_path)
    (src / f"SCN-{NAME}.lsv").unlink()
    with pytest.raises(authoring.AuthoringError, match="missing"):
        authoring.register_save(src, tmp_path / "store", NAME, now=lambda: 0.0)
    with pytest.raises(authoring.AuthoringError, match="not a save folder"):
        authoring.register_save(tmp_path / "nowhere", tmp_path / "store", NAME, now=lambda: 0.0)
    other = tmp_path / "b"
    other.mkdir()
    with pytest.raises(authoring.AuthoringError, match="Steam ID"):
        register(other, inspector=lambda p: ["meta.lsf holds a Steam ID"])
    # the flagged copy is kept for a person to look at, with the findings recorded
    assert json.loads((other / "store" / NAME / "registration.json").read_text())["inspector"] == [
        "meta.lsf holds a Steam ID"
    ]


def draft(reg, **kw):
    args = {
        "title": "Act 1 → 1000 gold",
        "family": "sandbox",
        "where": "Emerald Grove, out of combat, no dialogue open.",
        "objective": "Get the party 1000 gold.",
        "brief": "A neutral fighter.",
        "run_hours": 0.75,
        "goal": {"gold": {"gte": 1000}},
        "max_cost": 30.0,
        "party": ["Tav (Fighter)", "Shadowheart (Cleric)"],
    }
    return authoring.draft_scenario(reg, **{**args, **kw})


def test_draft_and_write(tmp_path):
    reg = register(tmp_path)
    sc = draft(reg)
    assert scenario.validate(sc, NAME) == []
    assert sc["start"]["save"] == FOLDER and sc["start"]["save_sha1"] == reg["lsv_sha1"]
    assert sc["profile"]["character"]["name"] == "Tav" and sc["game_settings"]["karmic_dice"] is False
    assert (sc["timing"], sc["game_settings"]["seed"], "think_pause" in sc) == ("realtime", None, False)
    assert draft(reg, family="fight", seed=7)["timing"] == "turn_based"
    assert draft(reg, family="fight", seed=7)["game_settings"]["seed"] == 7
    path = authoring.write_draft(sc, tmp_path / "scenarios")
    assert scenario.load(path)["goal"] == {"gold": {"gte": 1000}}
    with pytest.raises(authoring.AuthoringError, match="never overwritten"):
        authoring.write_draft(sc, tmp_path / "scenarios")
    assert draft(reg, goal=None, until="grove", journal=["Find a Cure"])["start"]["note"]["journal"] == ["Find a Cure"]
    with pytest.raises(authoring.AuthoringError, match="D8"):
        draft(reg, goal={"no_combat": True})


def test_attach_oracle(tmp_path):
    sc = draft(register(tmp_path))
    traj = tmp_path / "trajectory.jsonl"
    traj.write_text('{"tool": "trade"}\n')
    ref = authoring.attach_oracle(
        tmp_path / "oracle", sc, traj, kind="model_run", run_id="gold-r001", passed=True, t_pass_s=1500.0
    )
    assert ref == json.loads((tmp_path / "oracle" / f"{NAME}.json").read_text())
    assert ref["sha256"] == hashlib.sha256(b'{"tool": "trade"}\n').hexdigest()
    assert ref["scenario_digest"] == scenario.digest(sc) and ref["trajectory"] == "trajectory.jsonl"
    for kw, msg in [
        ({"kind": "guess"}, "oracle kind"),
        ({"passed": False}, "must reach the goal"),
    ]:
        with pytest.raises(authoring.AuthoringError, match=msg):
            authoring.attach_oracle(
                tmp_path / "oracle",
                sc,
                traj,
                **{"kind": "scripted", "run_id": None, "passed": True, "t_pass_s": 1.0, **kw},
            )
    with pytest.raises(authoring.AuthoringError, match="no trajectory"):
        authoring.attach_oracle(
            tmp_path / "o", sc, tmp_path / "none", kind="scripted", run_id=None, passed=True, t_pass_s=None
        )


class Clock:
    t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_baseline_jobs():
    jobs = authoring.baseline_jobs(NAME)
    assert [j["model"] for j in jobs] == ["baseline:donothing"] * 5 + [f"baseline:random:{i}" for i in range(1, 6)]
    assert {j["client"] for j in jobs} == {"baseline"} and all(j["max_cost"] is None for j in jobs)


def test_run_baselines_through_the_matrix(tmp_path):
    calls = []

    def run_one(job):
        calls.append(job["model"])
        return RunResult(job["id"], True, False, 0.0)

    out = authoring.run_baselines(tmp_path / "bl", NAME, run_one, now=Clock().now, sleep=Clock().sleep)
    assert out == {"stop": matrix.FINISHED, "donothing": {"n": 5, "k": 0}, "random": {"n": 5, "k": 0}, "ok": True}
    assert len(calls) == 10
    assert json.loads((tmp_path / "bl" / "budget.json").read_text()) == {"cap": 0.0, "spent": 0.0}
    assert json.loads((tmp_path / "bl" / "matrix" / "progress.json").read_text())["done"] == 10


def test_a_baseline_that_passes_fails_the_rule(tmp_path):
    def run_one(job):
        return RunResult(job["id"], True, job["model"] == "baseline:random:3", 0.0)

    out = authoring.run_baselines(tmp_path / "bl", NAME, run_one, now=Clock().now, sleep=Clock().sleep)
    assert out["random"] == {"n": 5, "k": 1} and out["ok"] is False


def test_a_paid_baseline_run_stops_at_the_zero_budget(tmp_path):
    def run_one(job):
        return RunResult(job["id"], True, False, 0.5)  # a baseline must never cost money

    out = authoring.run_baselines(tmp_path / "bl", NAME, run_one, now=Clock().now, sleep=Clock().sleep)
    assert out["stop"] == matrix.BUDGET and out["donothing"]["n"] == 1 and out["ok"] is False


def test_register_cli(tmp_path, capsys):
    src = make_save(tmp_path)
    store = tmp_path / "store"
    assert authoring.main(["register", str(src), str(store), NAME], inspector=lambda p: []) == 0
    assert f"registered {NAME}: {store / NAME / FOLDER}" in capsys.readouterr().out
    assert authoring.main(["register", str(src), str(store), NAME], inspector=lambda p: []) == 2
    assert "registered once" in capsys.readouterr().err


def test_the_save_inspector_is_the_harness_tool(tmp_path):
    inspect = authoring.save_inspector()
    assert inspect(tmp_path) == []  # no .lsv, no finding; the module loaded
    import inspect as stdlib_inspect

    assert hasattr(stdlib_inspect, "signature")  # tools/saves/inspect.py did not shadow it
