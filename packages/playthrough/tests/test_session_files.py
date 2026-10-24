"""adapters/session_files.py and its wiring: an app session writes the analysis files play.py writes (live9 #16:
app sessions had no Sxxx.metrics/progress/latency/triage/review files)."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from bg3play.adapters import harness_runner as hr
from bg3play.adapters.session_files import ProgressPoller

from .test_harness_runner import PT, make


def test_an_app_session_writes_the_files_play_py_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reviews: list[tuple[Path, str]] = []
    monkeypatch.setattr(hr, "write_review", lambda sdir, sid, runtime, log: reviews.append((sdir, sid)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    sup, agent, *_, keeper, runtime = make(tmp_path, hours=0.5)
    sdir = runtime / "sessions" / PT
    order: list[str] = []
    keeper.run_keeper = lambda pt, res: order.append("keeper")  # type: ignore[method-assign]
    runner = sup.runner
    assert isinstance(runner, hr.HarnessRunner)
    sup.after_session = lambda pt, res: (order.append("review"), runner.review(pt, res))
    sup.start(PT, wait=True)
    # the bg3 server writes its tool records (and fights, and reads progress) at the session's own path
    assert agent.calls[0].env["BG3AI_METRICS_PATH"] == str(sdir / "S001.metrics.jsonl")
    for name in ("S001.latency.json", "S001.progress.json", "S001.triage.md"):
        assert (sdir / name).is_file(), name
    assert json.loads((sdir / "S001.progress.json").read_text())["minutes"] == 30.0
    rows = [json.loads(ln) for ln in (sdir / "latency_history.jsonl").read_text().splitlines()]
    assert [r["session"] for r in rows] == ["S001"]
    assert [json.loads(ln)["session"] for ln in (sdir / "progress_history.jsonl").read_text().splitlines()] == ["S001"]
    # the review once the keeper pass is done (play.py's order)
    assert order == ["keeper", "review"] and reviews == [(sdir, "S001")]


def test_a_session_that_never_started_gets_no_review(tmp_path: Path):
    sup, *_ = make(tmp_path, hours=0.5)
    done: list[str] = []

    def refuse(cfg: Any, sdir: Path) -> hr.Agent:
        raise hr.SupervisorError("no client")

    runner = sup.runner
    assert isinstance(runner, hr.HarnessRunner)
    runner.agent_for = refuse
    sup.after_session = lambda pt, res: done.append(res.session)
    sup.start(PT, wait=True)
    assert [h["session"] for h in sup.history(PT)] == ["?"] and done == []


def poll(region: str) -> dict[str, Any]:
    return {"status": {"region": region}}


def test_the_poller_writes_story_progress_and_survives_a_dead_game(tmp_path: Path):
    reads: list[dict[str, Any] | None] = [poll("TUT_Avernus_C"), None, poll("WLD_Main_A")]

    def read() -> dict[str, Any] | None:
        got = reads.pop(0)
        if got is None:
            raise ConnectionError("the game doesn't answer")
        return got

    p = ProgressPoller(tmp_path, "S002", 1000.0, read, lambda m: None, interval=15.0)
    for t in (1015.0, 1030.0, 1045.0):
        p.poll(t)
    recs = [json.loads(ln) for ln in (tmp_path / "S002.progress.jsonl").read_text().splitlines()]
    assert [(r["type"], r.get("kind"), r.get("what")) for r in recs] == [
        ("poll", None, None),  # the first poll: a silent baseline
        ("poll", None, None),  # the dead game: a poll without an answer
        ("progress", "region", "WLD_Main_A"),
    ]
    assert "WLD_Main_A" in json.loads((tmp_path / "progress_state.json").read_text())["regions"]


def test_the_poller_loop_polls_until_stopped(tmp_path: Path):
    stop = threading.Event()
    n: list[int] = []

    def read() -> dict[str, Any] | None:
        n.append(1)
        if len(n) == 3:
            stop.set()
        return poll("TUT_Avernus_C")

    ProgressPoller(tmp_path, "S002", 1000.0, read, lambda m: None, interval=0.0).loop(stop)
    assert len(n) == 3
