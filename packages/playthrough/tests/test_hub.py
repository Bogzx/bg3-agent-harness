import json
from pathlib import Path

import pytest

from bg3play.hub import Hub
from bg3play.store import RunStore

PT = "pt1"


def append(path: Path, *lines: str) -> None:
    with path.open("a") as f:
        f.write("".join(ln + "\n" for ln in lines))


def action(i: int) -> str:
    return json.dumps({"t": 1000.0 + i, "tool": "look", "args": {}, "ms": i, "result": f"r{i}"})


@pytest.fixture
def rt(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "sessions" / PT).mkdir(parents=True)
    (tmp_path / "playthroughs" / PT / "memory").mkdir(parents=True)
    (runtime / "runner.status.json").write_text(json.dumps({"playthrough": PT, "session": "S001", "log": "/x/S001.log"}))
    (runtime / "actions.jsonl").write_text("")
    (runtime / "sessions" / PT / "S001.log").write_text("===== S001 2026-10-21 10:00:00 claude session x\n")
    return runtime


def make_hub(rt: Path, git=None, buffer: int = 2000) -> Hub:
    store = RunStore(rt, rt.parent / "playthroughs", **({"git": git} if git else {}))
    return Hub(store, generation="g1", buffer=buffer)


def types(events) -> list[str]:
    return [e.type for e in events]


def test_first_poll_then_appended_lines_in_order(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    first, reset, last = hub.read(None)
    assert not reset and types(first) == ["status", "session", "log"]
    assert first[1].data == {"playthrough": PT, "session": "S001"}
    assert last == "g1-3"

    append(rt / "actions.jsonl", action(1), action(2))
    append(rt / "sessions" / PT / "S001.log", "10:00:01 [say] going north", "10:00:02 [tool] look {}")
    assert hub.poll() == 4
    events, reset, last = hub.read("g1-3")
    assert not reset and last == "g1-7"
    assert [(e.type, e.data.get("text") or e.data.get("result")) for e in events] == [
        ("log", "going north"), ("log", "look {}"), ("action", "r1"), ("action", "r2")]
    assert [e.seq for e in events] == [4, 5, 6, 7]
    assert hub.poll() == 0  # nothing new: no events


def test_partial_line_waits_for_its_newline(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    with (rt / "actions.jsonl").open("a") as f:
        f.write(action(1)[:20])
    assert hub.poll() == 0
    with (rt / "actions.jsonl").open("a") as f:
        f.write(action(1)[20:] + "\n")
    assert hub.poll() == 1


def test_reconnect_gets_exactly_the_gap(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    _, _, seen = hub.read(None)
    append(rt / "actions.jsonl", *(action(i) for i in range(5)))
    hub.poll()
    # the viewer read two of the five, then dropped; it reconnects with the last id it got
    got, _, _ = hub.read(seen)
    mid = hub.event_id(got[1].seq)
    append(rt / "actions.jsonl", action(5))
    hub.poll()
    gap, reset, _ = hub.read(mid)
    assert not reset and [e.data["result"] for e in gap] == ["r2", "r3", "r4", "r5"]


def test_unknown_generation_future_id_or_evicted_gap_resets(rt: Path):
    hub = make_hub(rt, buffer=4)
    hub.poll()
    append(rt / "actions.jsonl", *(action(i) for i in range(6)))
    hub.poll()
    buffered = [e.seq for e in hub.events]
    assert len(buffered) == 4
    for last_id in ("old-3", "g1-999", "g1-2", "g1-x"):
        events, reset, _ = hub.read(last_id)
        assert reset and [e.seq for e in events] == buffered, last_id
    events, reset, _ = hub.read(f"g1-{buffered[0] - 1}")  # the oldest id still adjacent to the buffer
    assert not reset and [e.seq for e in events] == buffered


def test_truncated_actions_file_emits_reset(rt: Path):
    hub = make_hub(rt)
    append(rt / "actions.jsonl", action(1), action(2))
    hub.poll()
    (rt / "actions.jsonl").write_text(action(9) + "\n")
    hub.poll()
    events, _, _ = hub.read(None)
    assert types(events)[-2:] == ["actions_reset", "action"] and events[-1].data["result"] == "r9"


def test_status_change_ignores_heartbeat(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    st = json.loads((rt / "runner.status.json").read_text())
    (rt / "runner.status.json").write_text(json.dumps(dict(st, heartbeat=5, updated=6)))
    assert hub.poll() == 0
    (rt / "runner.status.json").write_text(json.dumps(dict(st, deadline=1234.0)))
    hub.poll()
    events, _, _ = hub.read("g1-3")
    assert types(events) == ["status"] and events[0].data["deadline"] == 1234.0


def test_new_session_log_switches_and_announces(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    st = json.loads((rt / "runner.status.json").read_text())
    (rt / "sessions" / PT / "S002.log").write_text("10:30:00 [say] new session\n")
    (rt / "runner.status.json").write_text(json.dumps(dict(st, session="S002", log="/x/S002.log")))
    hub.poll()
    events, _, _ = hub.read("g1-3")
    assert types(events) == ["status", "session", "log"]
    assert events[1].data["session"] == "S002" and events[2].data["session"] == "S002"


def test_memory_commits_after_head_changes(rt: Path):
    calls = []
    log = ["b\t200\tS001 memory-keeper: x\na\t100\tS001\n"]

    def fake_git(repo: Path, args: list[str]) -> str:
        calls.append(args)
        return log[0]

    mem = rt.parent / "playthroughs" / PT / "memory"
    head = mem / ".git" / "logs" / "HEAD"
    head.parent.mkdir(parents=True)
    head.write_text("1\n")
    hub = make_hub(rt, git=fake_git)
    hub.poll()
    assert "memory" not in types(hub.read(None)[0])  # history before the hub started is not news
    n = len(calls)
    hub.poll()
    assert len(calls) == n  # HEAD unchanged: git not run
    log[0] = "c\t300\tS002\n" + log[0]
    head.write_text("1\n2\n")
    hub.poll()
    events, _, _ = hub.read("g1-3")
    assert [(e.type, e.data["sha"], e.data["session"]) for e in events] == [("memory", "c", "S002")]


def test_wait_returns_on_publish_and_close(rt: Path):
    hub = make_hub(rt)
    hub.poll()
    _, _, last = hub.read(None)
    hub.publish("x", {})
    events, _, _ = hub.read(last, timeout=5)  # already there: no wait
    assert types(events) == ["x"]
    hub.close()
    events, reset, _ = hub.read(hub.event_id(hub.seq), timeout=5)  # closed: returns at once
    assert events == [] and not reset


def test_generation_without_dash_required(rt: Path):
    with pytest.raises(ValueError):
        Hub(RunStore(rt, rt), generation="a-b")
