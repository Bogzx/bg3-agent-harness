# pyright: basic
"""bg3bench.submission.start_states: a probe over a fake live host whose facts are a real windmill run's start, its
fingerprint passing X01 on that real run, and apply keeping the shipped release what release.build makes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from test_submission_release import fake_dump, fixture_files

from bg3bench import matrix_cli, saves, suite
from bg3bench.submission import consistency, pack, release, start_states, validate

TIMELINES = Path(__file__).parent / "fixtures" / "timelines"
WINDMILL = "Tav-26221264947__SCN-fight_act1_windmill"
WINDMILL_SHA1 = "ca8dae135db0df99b21c9e59197e0953144b63be"
TAG = suite.suite_version()


def windmill_rows() -> list[dict]:
    return start_states.read_facts(TIMELINES / "windmill_opus_goblins.facts.jsonl")


@dataclass
class FakeRunner:
    facts: Path
    snapshot: dict | None

    def facts_now(self) -> None:
        if self.snapshot is not None:
            with self.facts.open("a") as f:
                f.write(json.dumps(self.snapshot) + "\n")


@dataclass
class FakeHost:
    """LiveRunHost's start writes the run_start, load and settings marks; facts_now the first snapshot; record the
    run_end. Records what was called."""

    rows: list[dict]
    fail_start: bool = False
    runs: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def start(self, job, sc, run_dir):
        self.calls.append(("start", job["client"], job["model"], job["condition"], sc["name"], run_dir.name))
        if self.fail_start:
            raise RuntimeError("the start save didn't load")
        marks = [r for r in self.rows if r["kind"] == "mark" and r["mark"] in ("run_start", "load", "settings")]
        (run_dir / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in marks))
        snap = next((r for r in self.rows if r["kind"] == "snapshot"), None)
        self.runs[run_dir] = (FakeRunner(run_dir / "facts.jsonl", snap), 0.0, [])

    def record(self, job, sc, run_dir, outcome):
        self.calls.append(("record", outcome.result.reason))
        self.runs.pop(run_dir)
        with (run_dir / "facts.jsonl").open("a") as f:
            f.write(json.dumps({"v": 1, "kind": "mark", "t": 1.0, "game_t": 1.0, "mark": "run_end"}) + "\n")
        return {}

    def abort(self, when="operator exit"):
        self.calls.append(("abort", when))


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 100.0
        return self.t


def run_capture(tmp_path, host, names=(WINDMILL,), sha1: str | None = WINDMILL_SHA1, sleep=None):
    waits: list[float] = []
    doc = start_states.capture(
        host,
        saves.load()[TAG],
        pack.load_scenarios(),
        list(names),
        tmp_path / "start-states.json",
        tmp_path / "sessions",
        lambda _name: sha1,
        tag=TAG,
        sleep=sleep or waits.append,
        clock=Clock(),
        log=lambda _m: None,
    )
    return doc, waits


def x01(rows, fp, sc_name="fight_act1_windmill"):
    sc = pack.load_scenarios()[sc_name]
    b = validate.Loaded(Path("."), {}, {}, {}, {}, rows, None, {}, sc, {"fingerprint": fp}, {})
    return consistency.x01_start_state(b)


def test_a_probe_records_the_first_snapshot_of_a_run_without_an_agent_and_x01_accepts_real_runs(tmp_path):
    host = FakeHost(windmill_rows())
    doc, waits = run_capture(tmp_path, host)
    got = doc["saves"][WINDMILL]
    assert got["save_sha1"] == WINDMILL_SHA1 and got["scenario"] == "fight_act1_windmill" and not doc["errors"]
    assert got["fingerprint"] == {
        "party": [["Gale", 3], ["Shadowheart", 3], ["Tav", 3], ["Wyll", 3]],
        "gold": 156,
        "area": "Blighted Village",
        "difficulty": "Balanced",
        "name": "Tav",
    }
    assert got["fingerprint"] == saves.load()[TAG][WINDMILL].fingerprint  # what the oracle runs recorded
    assert waits == [start_states.FIRST_POLL_S]  # as long as a run's first poll comes after its kickoff
    start, record = host.calls
    assert start[:5] == ("start", "claude", "no-model", "rules", "fight_act1_windmill") and record[0] == "record"
    assert (tmp_path / "sessions" / got["run_dir"] / "facts.jsonl").is_file()  # the evidence stays
    assert json.loads((tmp_path / "start-states.json").read_text()) == doc
    assert x01(windmill_rows(), got["fingerprint"]) == (True, "")  # a real run from that save
    haiku = start_states.read_facts(TIMELINES / "windmill_haiku_bugbear.facts.jsonl")
    assert x01(haiku, got["fingerprint"]) == (True, "")


def test_a_probe_that_shows_no_level_or_another_save_records_an_error(tmp_path):
    rows = windmill_rows()
    snap = next(r for r in rows if r["kind"] == "snapshot")
    loading = [{**r, "party": []} if r is snap else r for r in rows]
    doc, _ = run_capture(tmp_path, FakeHost(loading))
    assert "no party" in doc["errors"][WINDMILL] and WINDMILL not in doc["saves"]
    other = [{**r, "detail": "Tav-1__SCN-other"} if r.get("mark") == "load" else r for r in rows]
    doc, _ = run_capture(tmp_path / "b", FakeHost(other))
    assert "not " + WINDMILL in doc["errors"][WINDMILL]
    doc, _ = run_capture(tmp_path / "c", FakeHost([r for r in rows if r["kind"] != "snapshot"]))
    assert doc["errors"][WINDMILL] == "no facts snapshot"


def test_a_wrong_story_copy_is_not_probed_and_a_failed_start_moves_on(tmp_path):
    host = FakeHost(windmill_rows())
    doc, _ = run_capture(tmp_path, host, sha1="0" * 40)
    assert "install it first" in doc["errors"][WINDMILL] and host.calls == []
    host = FakeHost(windmill_rows(), fail_start=True)
    grove = "Tav-1522126493__SCN-fight_act1_grove_gate"
    doc, _ = run_capture(tmp_path / "b", host, names=(WINDMILL, grove), sha1=None)
    assert set(doc["errors"]) == {WINDMILL, grove}  # sha1 None: neither copy is there
    host = FakeHost(windmill_rows(), fail_start=True)
    doc, _ = run_capture(tmp_path / "c", host)
    assert doc["errors"][WINDMILL] == "RuntimeError: the start save didn't load"
    assert [c[0] for c in host.calls] == ["start"]  # the host cleaned up its own failed start


def test_an_interrupted_probe_aborts_the_open_run(tmp_path):
    host = FakeHost(windmill_rows())

    def interrupted(_s):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_capture(tmp_path, host, sleep=interrupted)
    assert host.calls[-1] == ("abort", "start-state probe failed")


def test_a_later_capture_adds_to_the_file(tmp_path):
    run_capture(tmp_path, FakeHost(windmill_rows()), sha1="0" * 40)
    doc, _ = run_capture(tmp_path, FakeHost(windmill_rows()))
    assert WINDMILL in doc["saves"] and not doc["errors"]  # the old error is gone


def capture_doc(name: str, fp: dict, sha1: str) -> dict:
    return {
        "schema": 1,
        "kind": start_states.KIND,
        "suite": TAG,
        "errors": {},
        "saves": {name: {"save_sha1": sha1, "scenario": "x", "run_dir": "d", "fingerprint": fp}},
    }


GROVE = "Tav-1522126493__SCN-fight_act1_grove_gate"
GROVE_FP = {"party": [["Tav", 4]], "gold": 10, "area": "Emerald Grove", "difficulty": "Balanced", "name": "Tav"}


def docs_without_grove() -> tuple[dict, dict]:
    """saves.json's and the shipped release's documents with grove_gate's save not fingerprinted yet."""
    saves_doc = json.loads(saves.SAVES_JSON.read_text())
    release_doc = json.loads(release.default_path().read_text())
    for s in saves_doc["suites"][TAG]["saves"]:
        if s["name"] == GROVE:
            s["fingerprint"] = None
    for sc in release_doc["scenarios"].values():
        if sc["save"] == GROVE:
            sc["fingerprint"] = None
    return saves_doc, release_doc


def test_apply_writes_saves_json_and_the_release_as_the_release_tool_builds_them(tmp_path):
    sha1 = saves.load()[TAG][GROVE].save_sha1
    saves_doc, release_doc = docs_without_grove()
    assert "fight_act1_grove_gate" in release.unfingerprinted(release_doc)
    before = json.loads(json.dumps(release_doc))
    assert start_states.apply(capture_doc(GROVE, GROVE_FP, sha1), saves_doc, release_doc) == [GROVE]
    assert release_doc["scenarios"]["fight_act1_grove_gate"]["fingerprint"] == GROVE_FP
    assert "fight_act1_grove_gate" not in release.unfingerprinted(release_doc)
    assert {k: v for k, v in release_doc["scenarios"].items() if k != "fight_act1_grove_gate"} == {
        k: v for k, v in before["scenarios"].items() if k != "fight_act1_grove_gate"
    }
    # the release tool builds the same scenarios from the new saves.json (the shipped file's sync test stays green)
    path = tmp_path / "saves.json"
    path.write_text(json.dumps(saves_doc))
    served = release.serve("rules", ["pi", "claude"], Path("/nonexistent"), environ={}, dump=fake_dump(fixture_files()))
    built = release.build(
        TAG, before["versions"], before["pins"], pack.load_scenarios(), saves.load(path)[TAG], {"rules": served}
    )
    assert built["scenarios"] == release_doc["scenarios"] and built["saves"] == release_doc["saves"]
    assert start_states.apply(capture_doc(GROVE, GROVE_FP, sha1), saves_doc, release_doc) == []  # again: no change


def test_apply_refuses_another_sha1_suite_or_fingerprint():
    sha1 = saves.load()[TAG][GROVE].save_sha1
    saves_doc, release_doc = docs_without_grove()
    keep = json.dumps([saves_doc, release_doc])
    with pytest.raises(ValueError, match="not listed"):
        start_states.apply(capture_doc(GROVE, GROVE_FP, "1" * 40), saves_doc, release_doc)
    with pytest.raises(ValueError, match="capture is for"):
        start_states.apply({**capture_doc(GROVE, GROVE_FP, sha1), "suite": "bench-v0.1"}, saves_doc, release_doc)
    other = {**(saves.load()[TAG][WINDMILL].fingerprint or {}), "gold": 1}
    with pytest.raises(ValueError, match="another fingerprint"):
        start_states.apply(capture_doc(WINDMILL, other, WINDMILL_SHA1), saves_doc, release_doc)
    with pytest.raises(ValueError, match="release.v1"):
        start_states.apply(capture_doc(GROVE, {**GROVE_FP, "gold": "x"}, sha1), saves_doc, release_doc)
    assert json.dumps([saves_doc, release_doc]) == keep  # nothing changed
    assert start_states.apply(capture_doc(WINDMILL, other, WINDMILL_SHA1), saves_doc, release_doc, replace=True)


def test_the_cli_apply_and_capture_through_the_matrix_cli(tmp_path, capsys, monkeypatch):
    sha1 = saves.load()[TAG][GROVE].save_sha1
    saves_path, rel = tmp_path / "saves.json", tmp_path / "release.json"
    saves_doc, release_doc = docs_without_grove()
    saves_path.write_text(json.dumps(saves_doc))
    rel.write_text(json.dumps(release_doc))
    cap = tmp_path / "cap.json"
    cap.write_text(json.dumps(capture_doc(GROVE, GROVE_FP, sha1)))
    args = ["apply", str(cap), "--saves-json", str(saves_path), "--release", str(rel)]
    assert start_states.main(args) == 0
    assert "fingerprints written for 1 saves" in capsys.readouterr().out
    assert saves.load(saves_path)[TAG][GROVE].fingerprint == GROVE_FP
    assert json.loads(rel.read_text())["scenarios"]["fight_act1_grove_gate"]["fingerprint"] == GROVE_FP
    assert not release.errors(json.loads(rel.read_text()))
    assert matrix_cli.main(["start-states"], repo=tmp_path) == 2  # no host: no game
    assert "needs the game" in capsys.readouterr().err
    host = FakeHost(windmill_rows())
    monkeypatch.setattr(saves.DirStory, "lsv_sha1", lambda _self, name: WINDMILL_SHA1 if name == WINDMILL else None)
    rc = matrix_cli.main(
        ["start-states", "--save", WINDMILL],
        repo=tmp_path,
        host=cast(Any, host),
        sleep=lambda _s: None,
    )
    assert rc == 0 and host.calls[0][0] == "start"
    doc = json.loads((tmp_path / "runtime" / "start-states" / f"{TAG}.json").read_text())
    assert doc["saves"][WINDMILL]["fingerprint"] == saves.load()[TAG][WINDMILL].fingerprint
    assert matrix_cli.main(["start-states", "--save", "Tav-1__SCN-nope"], repo=tmp_path, host=cast(Any, host)) == 2


def test_the_harness_live_host_is_what_capture_drives():
    """play.py bench passes its LiveRunHost: pyright checks it against LiveHost here, the test that it has open runs."""
    from bg3harness.runner.run_host import LiveRunHost

    host: start_states.LiveHost = LiveRunHost(cast(Any, lambda _opt: None), run_files=lambda *_a: {}, env={})
    assert host.runs == {}
