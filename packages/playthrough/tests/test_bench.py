import copy
import json
import shutil
from pathlib import Path

import pytest
from bg3play import api, bench
from bg3play.store import RunStore
from jsonschema import Draft202012Validator

from .conftest import PT

SCHEMAS = Path(__file__).resolve().parents[3] / "packages" / "bench" / "schema"
FIXTURES = Path(__file__).parent / "fixtures" / "bench"
RUNS = ("e2e-naut-claude-r001", "e2e-naut-pi-r001")


def load(run: str, name: str) -> dict:
    return json.loads((FIXTURES / run / name).read_text())


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


def with_choices_and_luck(res: dict) -> dict:
    """A choice scenario's result: the claude run's with picks, a persona fit and a luck summary added."""
    res = copy.deepcopy(res)
    res["luck"] = {"n": 40, "z": -1.25, "party": 31, "other": 9, "skipped": 2, "nat20": 3, "nat1": 1}
    res["choices"] = {
        "picks": [
            {"t": 1792620000.0, "scene": "Shadowheart pod", "options": ["Free her", "Leave"], "picked": 0,
             "pick": "Free her", "reason": "an ally against the mind flayers"},
            {"t": None, "scene": "Us", "options": ["Adopt", "Kill"], "picked": None, "pick": "Adopt", "reason": None},
        ],
        "fit": {"status": "ok", "score": 7.5, "judge_model": "judge-x", "summary": "in character"},
    }  # fmt: skip
    return res


def write(d: Path, man: dict, res: dict) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / bench.MANIFEST).write_text(json.dumps(man))
    (d / bench.RESULT).write_text(json.dumps(res))
    return d


@pytest.mark.parametrize("run", RUNS)
def test_fixtures_are_valid_bench_files(run: str):
    """The fixtures are the real e2e run's bench output (runtime/probes/live3/bench); they follow the bench schemas."""
    validator("manifest.v1.schema.json").validate(load(run, "manifest.json"))
    validator("result.v1.schema.json").validate(load(run, "result.json"))


def test_the_synthetic_choice_result_is_valid():
    validator("result.v1.schema.json").validate(with_choices_and_luck(load(RUNS[0], "result.json")))


def test_grade_reads_both_files_as_written(tmp_path: Path):
    man, res = load(RUNS[0], "manifest.json"), with_choices_and_luck(load(RUNS[0], "result.json"))
    g = bench.grade(write(tmp_path / "r", man, res))
    assert g is not None and g["result"] == res
    assert g["manifest"] == {k: man[k] for k in bench.MANIFEST_KEYS}
    assert g["manifest"]["model"]["slug"] == "claude-opus-5-5" and g["result"]["t_pass_s"] == 1042.7


def test_grade_none_without_files(tmp_path: Path):
    assert bench.grade(tmp_path) is None
    assert bench.grade(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda m, r: m.update(schema=2), "manifest.json has schema 2; this app reads schema 1"),
        (lambda m, r: r.update(schema=2), "result.json has schema 2"),
        (lambda m, r: r.pop("tokens"), "result.json lacks tokens"),
        (lambda m, r: m.pop("model"), "manifest.json lacks model"),
        (lambda m, r: r.update(run_id="other"), "but result.json is run 'other'"),
    ],
)
def test_grade_refuses_a_file_it_cannot_read(tmp_path: Path, change, error: str):
    man, res = load(RUNS[1], "manifest.json"), load(RUNS[1], "result.json")
    change(man, res)
    g = bench.grade(write(tmp_path / "r", man, res))
    assert g is not None and error in g["error"]


def test_grade_with_one_file_or_bad_json(tmp_path: Path):
    d = write(tmp_path / "r", load(RUNS[0], "manifest.json"), {})
    (d / bench.RESULT).unlink()
    assert bench.grade(d) == {"error": "result.json is missing or not a JSON object"}
    (d / bench.RESULT).write_text("{not json")
    assert bench.grade(d) == {"error": "result.json is missing or not a JSON object"}


def test_api_reads_the_playthrough_session_dir(store: RunStore):
    assert api.bench_grade(store, PT) == {"v": 1, "playthrough": PT, "bench": None}
    for name in (bench.MANIFEST, bench.RESULT):
        shutil.copy(FIXTURES / RUNS[0] / name, store.session_dir(PT) / name)
    payload = api.bench_grade(store, PT)
    assert payload["bench"]["result"]["outcome"] == "reached"
    assert payload["bench"]["manifest"]["run_id"] == RUNS[0]
