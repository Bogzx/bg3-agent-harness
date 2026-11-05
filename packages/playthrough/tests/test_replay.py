import json
import time
from pathlib import Path

import pytest
from bg3play import replay
from bg3play.store import RunStore

from .conftest import PT

S096_START = 1792311134.6876369  # 2026-10-18 11:12:14.69 on the recording machine (EEST, UTC+3)
NOW = S096_START + 3600


@pytest.fixture
def bucharest(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Bucharest")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_clock_to_epoch(bucharest):
    assert replay.clock_to_epoch("11:12:16", S096_START) == 1792311136.0
    assert replay.clock_to_epoch("11:12:14", S096_START) == 1792311134.0
    assert replay.clock_to_epoch("11:12:13", S096_START) == 1792311133.0  # just before the start, not a day later
    assert replay.clock_to_epoch("00:00:05", S096_START) == 1792311134.0 + (24 * 3600 - (11 * 3600 + 12 * 60 + 14)) + 5
    assert replay.clock_to_epoch("bad", S096_START) is None


def test_window_actions(tmp_path: Path):
    f = tmp_path / "actions.jsonl"
    rows = [{"t": t, "tool": "look", "args": {}, "ms": 1, "result": f"r{t}"} for t in (5, 10, 15, 20)]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n[1]\n")
    got = replay.window_actions(f, 10, 15)
    assert [e["t"] for e in got] == [10, 15] and got[0]["kind"] == "action" and got[0]["result"] == "r10"
    assert len(replay.window_actions(f, 0, 100, limit=3)) == 3
    assert replay.window_actions(tmp_path / "missing", 0, 1) == []


def test_replay_on_the_fixture(store: RunStore, git_memory: Path, tmp_path: Path, bucharest):
    video = tmp_path / "video"
    video.mkdir()
    for name in ("rec-1-0000.mp4", "rec-1-0001.mp4", "rec-2-0000.mp4"):
        (video / name).write_bytes(b"x")
    (video / "index.json").write_text(
        json.dumps(
            {
                "v": 1,
                "segments": [
                    {
                        "file": "rec-1-0000.mp4",
                        "run": "rec-1",
                        "start": S096_START - 700,
                        "end": S096_START - 100,
                        "bytes": 1,
                    },
                    {
                        "file": "rec-1-0001.mp4",
                        "run": "rec-1",
                        "start": S096_START - 100,
                        "end": S096_START + 500,
                        "bytes": 1,
                    },
                    {"file": "rec-2-0000.mp4", "run": "rec-2", "start": S096_START + 600, "end": None, "bytes": 1},
                ],
            }
        )
    )
    r = replay.replay(store, PT, "S096", video, NOW)
    assert r["started"] == S096_START and r["ended"] == 1792312705.6318264
    kinds = {e["kind"] for e in r["events"]}
    assert kinds == {"action", "say", "memory"}
    ts = [e["t"] for e in r["events"]]
    assert ts == sorted(ts) and r["started"] - 1 <= ts[0] and ts[-1] <= r["ended"] + 900
    says = [e for e in r["events"] if e["kind"] == "say"]
    assert says and all(r["started"] - 1 <= e["t"] <= r["ended"] + 60 for e in says)
    assert [e["subject"] for e in r["events"] if e["kind"] == "memory"] == ["S096 memory-keeper: HANDOFF.md"]
    assert [s["file"] for s in r["segments"]] == ["rec-1-0001.mp4", "rec-2-0000.mp4"]
    assert replay.replay(store, PT, "S999", video, NOW) is None


def test_replay_of_running_session(store: RunStore, tmp_path: Path):
    # S097 runs now: no runner.json history row, no [end] line; its start is in runner.status.json
    st = json.loads(store.status_path.read_text())
    start = 1792312800.0
    st.update(session="S097", started=start, session_ended=None, log="/elsewhere/S097.log")
    store.status_path.write_text(json.dumps(st))
    (store.session_dir(PT) / "S097.log").write_text("")
    with store.actions_path.open("a") as f:
        f.write(json.dumps({"t": start + 30, "tool": "look", "args": {}, "ms": 5, "result": "WHERE beach"}) + "\n")
    row = store.sessions(PT)[-1]
    assert (row["session"], row["started"], row["running"]) == ("S097", start, True)
    r = replay.replay(store, PT, "S097", tmp_path / "video", start + 90)
    assert r is not None and (r["started"], r["ended"]) == (start, start + 90)
    assert [(e["kind"], e["t"]) for e in r["events"]] == [("action", start + 30)]
    msg = {"id": "m1", "t": start + 20, "text": "rest first"}
    (store.session_dir(PT) / "operator.jsonl").write_text(json.dumps(msg) + "\n")
    r = replay.replay(store, PT, "S097", tmp_path / "video", start + 90)
    assert r is not None
    assert [(e["kind"], e["t"]) for e in r["events"]] == [("operator", start + 20), ("action", start + 30)]


def test_operator_messages_on_the_replay_axis(tmp_path: Path):
    rows = [{"id": "m1", "t": 90.0, "text": "before"}, {"id": "m2", "t": 110.0, "text": "rest first"}]
    (tmp_path / "operator.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    done = [
        {"id": "m1", "t": 101.0, "session": "S004", "tool": "look"},  # sent before the window, delivered in it
        {"id": "m2", "t": 115.0, "session": "S004", "tool": "travel_to"},
        {"id": "m2", "t": 120.0, "redeliver": True},  # no event: the screen's status comes from the snapshot
        {"id": "m2", "t": 300.0, "session": "S005", "tool": "look"},  # after the window
    ]
    (tmp_path / "operator.delivered.jsonl").write_text("".join(json.dumps(r) + "\n" for r in done))
    assert replay.operator_events(tmp_path, 100.0, 200.0) == [
        {"t": 110.0, "kind": "operator", "id": "m2", "text": "rest first", "status": "queued"},
        {"t": 101.0, "kind": "operator", "id": "m1", "text": "before", "status": "delivered",
         "delivered": {"t": 101.0, "session": "S004", "tool": "look"}},
        {"t": 115.0, "kind": "operator", "id": "m2", "text": "rest first", "status": "delivered",
         "delivered": {"t": 115.0, "session": "S004", "tool": "travel_to"}},
    ]  # fmt: skip
    assert replay.operator_events(tmp_path / "none", 0.0, 1e12) == []
