# pyright: basic
"""The JSON Schemas of the files the app shares with play.py (schemas/): supervisor.json, runner.json and
runner.status.json as play.py wrote them (the recorded fixture run) and as the app writes them (HarnessRunner under
the Supervisor), so a writer that drifts from what the readers use fails here."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from bg3play import supervisor as sup
from bg3play.supervisor import SessionResult

from .conftest import FIXTURE
from .conftest import PT as FIXTURE_PT
from .test_harness_runner import PT, make

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def validator(name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def errors(name: str, doc: Any) -> list[str]:
    return [f"{list(e.absolute_path)}: {e.message}" for e in validator(name).iter_errors(doc)]


def load(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def test_play_pys_files_in_the_fixture_run():
    assert errors("runner.schema.json", load(FIXTURE / "runtime" / "sessions" / FIXTURE_PT / "runner.json")) == []
    assert errors("runner-status.schema.json", load(FIXTURE / "runtime" / "runner.status.json")) == []


def test_the_apps_files_after_two_sessions(tmp_path: Path):
    s, _, _, _, _, runtime = make(tmp_path, sigs=[{"level": "TUT"}, {"level": "WLD"}] * 2)
    s.start(PT, wait=True)
    supervisor_json = load(sup.history_path(runtime, PT))
    assert len(supervisor_json["history"]) == 2 and errors("supervisor.v1.schema.json", supervisor_json) == []
    runner_json = load(runtime / "sessions" / PT / "runner.json")
    assert len(runner_json["history"]) == 2 and errors("runner.schema.json", runner_json) == []
    assert errors("runner-status.schema.json", load(runtime / "runner.status.json")) == []


def test_a_session_that_never_started_and_one_with_game_deaths(tmp_path: Path):
    path = sup.history_path(tmp_path, PT)
    rows = [SessionResult("?", "error", detail="no client"), SessionResult("S001-r1", "crash", None, 1.0, 2.0, 0.3,
                                                                               game_deaths=("oom", ""))]  # fmt: skip
    sup.write_history(path, [asdict(r) for r in rows])
    assert errors("supervisor.v1.schema.json", load(path)) == []


@pytest.mark.parametrize(
    ("name", "doc"),
    [
        ("supervisor.v1.schema.json", {"history": []}),  # no v
        ("supervisor.v1.schema.json", {"v": 1, "history": [{"session": "S001", "outcome": "won"}]}),
        ("runner.schema.json", {"history": [{"kind": "play", "started": 1.0, "ended": 2.0}]}),  # no session
        ("runner-status.schema.json", {"session_ended": "yes"}),
    ],
)
def test_the_schemas_refuse_what_the_readers_cant_use(name: str, doc: Any):
    assert errors(name, doc)
