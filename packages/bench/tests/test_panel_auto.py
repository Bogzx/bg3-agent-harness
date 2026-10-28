# pyright: basic
import json
from pathlib import Path

from bg3bench.serve import auto, launch
from bg3bench.serve.panel import PanelError


def end(run_id, valid=True, **kw):
    return {"type": "end", "job": "j", "attempt": 1, "t": 1, "result": {"run_id": run_id, "valid": valid, **kw}}


def matrix(rt: Path, name: str, records: list, *, submit=True) -> None:
    (rt / "panel").mkdir(parents=True, exist_ok=True)
    launch.write_choices(rt, {"name": name, "record": True, "submit_when_done": submit, "auto": {}})
    d = rt / "matrix" / name
    d.mkdir(parents=True)
    lines = [json.dumps({"type": "plan", "name": name, "jobs": []})] + [json.dumps(r) for r in records]
    (d / "queue.jsonl").write_text("\n".join(lines) + '\n{"type": "end", "res')  # the last line half-written


def choices(rt: Path, name: str) -> dict:
    doc = launch.read_choices(rt, name)
    assert doc is not None
    return doc


def test_valid_run_ids(tmp_path):
    assert auto.valid_run_ids(tmp_path) == []
    matrix(tmp_path, "m", [end("a"), end("b", valid=False), {"type": "start", "job": "j"}, end(None), end("a"),
                           end("c")])  # fmt: skip
    assert auto.valid_run_ids(tmp_path / "matrix" / "m") == ["a", "c"]


def test_tick_prepares_each_valid_finished_run_once(tmp_path):
    rt = tmp_path
    matrix(rt, "m1", [end("r1"), end("r2"), end("r3"), end("r4"), end("r5")])
    matrix(rt, "m2", [end("x1")], submit=False)  # Submit when done unticked: never touched
    rows = [{"run_id": f"r{i}", "dir": f"d{i}"} for i in (1, 2, 3, 5)] + [{"run_id": "x1", "dir": "dx"}]
    calls: list[str] = []

    def prepare(run):
        calls.append(run)
        if run == "d3":
            raise PanelError("pack refused it")
        return {"bundle_sha256": "ab" * 32, "verdict": {"accepted": run == "d1"}}

    done = auto.tick(rt, rows, prepared=lambda run: run == "d5", prepare=prepare, now=7.0)
    # r4 has no run dir yet, d5 is prepared already (the user's Validate)
    assert calls == ["d1", "d2", "d3"]
    assert done == [
        {"matrix": "m1", "run": "d1", "t": 7.0, "bundle_sha256": "ab" * 32, "accepted": True},
        {"matrix": "m1", "run": "d2", "t": 7.0, "bundle_sha256": "ab" * 32, "accepted": False},
        {"matrix": "m1", "run": "d3", "t": 7.0, "error": "pack refused it"},
    ]
    assert set(choices(rt, "m1")["auto"]) == {"d1", "d2", "d3"}
    was_prepared = lambda run: run == "d5"
    assert auto.tick(rt, rows, prepared=was_prepared, prepare=prepare, now=8.0) == []  # once: a failure waits
    rows.append({"run_id": "r4", "dir": "d4"})
    assert [d["run"] for d in auto.tick(rt, rows, prepared=was_prepared, prepare=prepare, now=9.0)] == ["d4"]
    assert choices(rt, "m2")["auto"] == {}


def test_tick_without_panel_matrices(tmp_path):
    assert auto.tick(tmp_path, [], prepared=lambda r: False, prepare=lambda r: {}, now=0.0) == []
    (tmp_path / "panel").mkdir()
    (tmp_path / "panel" / "broken.json").write_text("{")
    assert auto.tick(tmp_path, [], prepared=lambda r: False, prepare=lambda r: {}, now=0.0) == []
