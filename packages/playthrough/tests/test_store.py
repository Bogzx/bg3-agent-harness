from pathlib import Path

import pytest

from bg3play import api
from bg3play.store import RunStore, StoreError

from .conftest import PT


def test_playthroughs_and_current(store: RunStore):
    assert store.playthroughs() == [PT]
    assert store.current() == PT


def test_sessions_join_history_end_line_and_latency(store: RunStore):
    rows = store.sessions(PT)
    assert [r["session"] for r in rows] == ["S094", "S095", "S096"]
    assert [(r["outcome"], r["cost_usd"], r["turns"]) for r in rows] == [
        ("success", 5.33, 179),
        ("success", 8.58, 225),
        ("error_during_execution", 8.12, 305),
    ]
    for r in rows:
        assert r["started"] < r["ended"] and r["model"] == "opus"
        assert r["latency"]["apm"] is not None and r["points"] is not None


def test_agent_log_by_file_name_not_absolute_path(store: RunStore):
    # runner.status.json "log" is an absolute path on the recording machine; the store uses only its name
    assert store.agent_log(PT) == store.session_dir(PT) / "S096.log"


def test_agent_log_falls_back_to_last_session(store: RunStore):
    store.status_path.write_text('{"playthrough": "other"}')
    assert store.agent_log(PT).name == "S096.log"
    assert store.current() == "other"


def test_memory_tree_and_file(store: RunStore):
    paths = [f["path"] for f in store.memory_tree(PT)]
    assert "HANDOFF.md" in paths and "snapshots/status.md" in paths and "sessions/S096.md" in paths
    assert store.memory_file(PT, "objectives.md").startswith("# Objectives")
    assert store.memory_file(PT, "missing.md") is None


@pytest.mark.parametrize(
    "rel",
    ["../../../runtime/runner.status.json", "/etc/passwd", ".git/config", "snapshots/../../HANDOFF.sh", "HANDOFF"],
)
def test_memory_path_refused(store: RunStore, rel: str):
    with pytest.raises(StoreError):
        store.memory_file(PT, rel)


def test_bad_playthrough_name_refused(store: RunStore):
    with pytest.raises(StoreError):
        store.session_dir("../etc")


def test_memory_log_without_and_with_git(store: RunStore, git_memory: Path):
    log = store.memory_log(PT)
    assert [(c["subject"], c["t"], c["session"]) for c in log] == [
        ("S096 memory-keeper: HANDOFF.md", 1792312786, "S096"),
        ("S095", 1792311056, "S095"),
    ]
    assert [c["session"] for c in store.memory_log(PT, rel="objectives.md")] == ["S095"]


def test_memory_diff_per_file(store: RunStore, git_memory: Path):
    keeper = store.memory_log(PT, rel="HANDOFF.md")[0]["sha"]
    patch = store.memory_diff(PT, "HANDOFF.md", keeper)
    assert "+- keeper line" in patch.splitlines()
    assert store.memory_diff(PT, "objectives.md", keeper) == ""   # that commit didn't touch it
    first = store.memory_log(PT, rel="objectives.md")[0]["sha"]
    assert "new file" in store.memory_diff(PT, "objectives.md", first)
    assert store.memory_diff(PT, "HANDOFF.md", "0" * 40) is None
    with pytest.raises(StoreError):
        store.memory_diff(PT, "HANDOFF.md", "HEAD; rm")
    with pytest.raises(StoreError):
        store.memory_diff(PT, "../../runtime/runner.status.json", keeper)


def test_memory_diff_without_repo(store: RunStore):
    assert store.memory_diff(PT, "HANDOFF.md", "abcdef1") is None


def test_memory_log_empty_without_repo(store: RunStore):
    assert store.memory_log(PT) == []


def test_latest_screenshot(store: RunStore):
    assert store.latest_screenshot().name == "shot_000001.jpg"
    assert RunStore(Path("/nonexistent"), Path("/nonexistent")).latest_screenshot() is None


def test_watch_payload(store: RunStore):
    w = api.watch(store, PT)
    assert w["v"] == 1 and w["session"] == "S096" and w["runner"]["session"] == "S096"
    assert w["goal"] and "epilogue" in w["goal"]
    assert w["party"]["party"] and all(m["max_hp"] > 0 for m in w["party"]["party"])
    assert {e["kind"] for e in w["log"]} >= {"say", "tool", "result", "end"}
    assert w["actions"] and w["cost"]["sessions"] == 3 and w["cost"]["total_usd"] > w["cost"]["last_session_usd"]
    assert w["latency"]["apm"] is not None and w["screenshot"]["name"] == "shot_000001.jpg"


def test_watch_of_other_playthrough_filters_shared_actions(store: RunStore):
    store.status_path.write_text('{"playthrough": "someone-else"}')
    w = api.watch(store, PT)
    ss = store.sessions(PT)
    assert w["runner"] is None
    assert w["actions"] and all(ss[0]["started"] <= a["t"] <= ss[-1]["ended"] for a in w["actions"])


def test_timeline_payload(store: RunStore):
    t = api.timeline(store, PT)
    assert [s["session"] for s in t["sessions"]] == ["S094", "S095", "S096"]
    assert t["decisions"] and t["decisions"][0]["id"] == "D-01"
    late = [d for d in t["decisions"] if d["session_id"] in ("S094", "S095", "S096")]
    assert all(d["t"] for d in late)
    assert t["fights"] and {f["session"] for f in t["fights"]} == {"S094", "S095", "S096"}


def test_playthroughs_payload(store: RunStore):
    (row,) = api.playthroughs(store)["playthroughs"]
    assert row["name"] == PT and row["current"] and row["sessions"] == 3 and row["last_session"] == "S096"
