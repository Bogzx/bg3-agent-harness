"""adapters/session_files.py and its wiring: an app session writes the analysis files play.py writes (live9 #16:
app sessions had no Sxxx.metrics/progress/latency/triage/review files, and no facts.jsonl polls or choices)."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from bg3harness import clients, events
from bg3harness.runner import env as renv
from bg3play.adapters import harness_runner as hr
from bg3play.adapters.session_files import FactsFeed, ProgressPoller

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

    def read(timeout: float | None = None) -> dict[str, Any] | None:
        n.append(1)
        if len(n) == 3:
            stop.set()
        return poll("TUT_Avernus_C")

    ProgressPoller(tmp_path, "S002", 1000.0, read, lambda m: None, interval=0.0).loop(stop)
    assert len(n) == 4  # 3, then the last read at the stop


def test_the_poller_reads_once_more_at_the_stop_progress_only(tmp_path: Path):
    # v010 pilot choice_act2_nightsong sonnet: the story changed after the last poll and the session ended (0 points).
    # REVIEW-18r: that last read is progress only (the end's facts are FactsFeed's), with evals that end within the join
    stop = threading.Event()
    timeouts: list[float | None] = []
    fed: list[Any] = []
    region = ["TUT_Avernus_C"]

    def read(timeout: float | None = None) -> dict[str, Any] | None:
        timeouts.append(timeout)
        got = poll(region[0])
        if len(timeouts) == 1:  # after this poll: the scene resolves and the session ends before the next
            region[0] = "WLD_Main_A"
            stop.set()
        return got

    ProgressPoller(tmp_path, "S001", 1000.0, read, lambda m: None, interval=0.0, facts=fed.append).loop(stop)
    assert timeouts == [None, renv.LAST_EVAL_TIMEOUT] and 2 * renv.LAST_EVAL_TIMEOUT < renv.POLL_JOIN_S
    assert len(fed) == 1  # the poll before the stop only
    recs = [json.loads(ln) for ln in (tmp_path / "S001.progress.jsonl").read_text().splitlines()]
    assert ("progress", "region", "WLD_Main_A") in [(r["type"], r.get("kind"), r.get("what")) for r in recs]


class FakeRings:
    """The mod's referee and roll rings (ref.since / roll.since) as RingFeed reads them, and the inventory eval."""

    def __init__(self) -> None:
        self.ref: list[dict[str, Any]] = []
        self.evals: list[str] = []

    def rpc(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        recs = self.ref if op == "ref.since" else []
        new = [r for r in recs if r["seq"] > int(args["seq"])]
        return {"boot": "b1", "latest": len(recs), "first": 1, "now": 0, "events": new[: int(args["limit"])]}

    def eval(self, code: str, ctx: str) -> str:
        self.evals.append(ctx)
        return json.dumps([{"ok": True, "result": {"items": [{"name": "Potion of Healing", "count": 2}]}}])


def game_read(region: str) -> dict[str, Any]:
    return {"status": {"region": region, "gold": 5, "party": [{"name": "Ilse", "level": 1}]},
            "ui": {"screen": "game", "region": "Beach"}, "journal": {"quests": []}}  # fmt: skip


def facts_of(sdir: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in (sdir / "facts.jsonl").read_text().splitlines()]


def test_the_facts_feed_writes_what_play_py_s_facts_poll_writes(tmp_path: Path):
    rings = FakeRings()
    feed = FactsFeed(tmp_path, rings.rpc, rings.eval, {"playthrough": PT, "client": "claude", "model": "opus"})
    p = ProgressPoller(tmp_path, "S001", 1000.0, lambda: game_read("TUT_Avernus_C"), lambda m: None, facts=feed.poll)
    p.poll(1015.0)
    rings.ref.append({"seq": 1, "t": 0, "ev": "trade", "gold": -40})
    p.poll(1030.0)
    recs = facts_of(tmp_path)
    assert [(r["kind"], r.get("mark") or r.get("ev")) for r in recs] == [
        ("mark", "run_start"),
        ("snapshot", None),  # the first read: the full state with the party inventory
        ("event", "trade"),  # the referee ring's record (the ring started at its end: nothing from before)
    ]
    assert recs[0]["detail"] == {"playthrough": PT, "client": "claude", "model": "opus"}
    assert recs[1]["inv"] == [{"name": "Potion of Healing", "count": 2}] and rings.evals == ["server", "server"]
    # a later server's feed keeps the file's time zero (one run_start per file, as play.py's)
    FactsFeed(tmp_path, rings.rpc, rings.eval, {"playthrough": PT})
    assert [r.get("mark") for r in facts_of(tmp_path)].count("run_start") == 1


def test_the_facts_feed_runs_one_poll_at_a_time(tmp_path: Path):
    # REVIEW-18s: an old session's pass still out on a hung game overlapped the next session's poller on the same feed
    rings = FakeRings()
    a_in, b_in, release = threading.Event(), threading.Event(), threading.Event()
    inside, most = [0], [0]

    def rpc(op: str, args: dict[str, Any]) -> dict[str, Any]:
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        try:
            if threading.current_thread().name == "A":
                a_in.set()
                release.wait(10)  # the hung game's rpc, until the test lets it answer
            else:
                b_in.set()
            return rings.rpc(op, args)
        finally:
            inside[0] -= 1

    feed = FactsFeed(tmp_path, rpc, rings.eval, {"playthrough": PT})
    a = threading.Thread(target=feed.poll, args=(game_read("TUT_Avernus_C"),), name="A")
    b = threading.Thread(target=feed.poll, args=(game_read("TUT_Avernus_C"),), name="B")
    a.start()
    assert a_in.wait(10)
    b.start()
    entered = b_in.wait(0.5)  # B must wait for A's poll: it never gets in while A is inside
    release.set()
    a.join(10)
    b.join(10)
    assert not entered and most[0] == 1 and b_in.is_set()


def test_an_app_session_writes_its_choices_into_facts(tmp_path: Path):
    sup, *_, runtime = make(tmp_path, hours=0.5)
    sdir = runtime / "sessions" / PT
    sdir.mkdir(parents=True)
    log = events.EventLog(sdir / "S001.events.jsonl")  # what ClientAgent writes while the agent plays
    log.write(clients.Event("tool_result", text="DIALOGUE Mattis: Buy?\n  1. Yes.\n  2. No.", t=1001.0))
    log.write(clients.Event("text", text="Too pricey.", t=1002.0))
    log.write(clients.Event("tool_call", tool="choose_dialogue", args={"option": 2}, t=1003.0))
    log.close()
    rings = FakeRings()
    runner = sup.runner
    assert isinstance(runner, hr.HarnessRunner)
    feeds: list[Path] = []

    def facts_for(d: Path, run_start: dict[str, Any]) -> FactsFeed:
        feeds.append(d)
        return FactsFeed(d, rings.rpc, rings.eval, run_start)

    runner.facts_for = facts_for
    sup.start(PT, wait=True)
    recs = facts_of(sdir)
    assert recs[0]["mark"] == "run_start" and recs[0]["detail"]["playthrough"] == PT
    assert [(r.get("ev"), r.get("picked_text"), r.get("reason")) for r in recs if r["kind"] == "event"] == [
        ("choice", "No.", "Too pricey.")
    ]
    assert feeds == [sdir]


def test_the_attached_game_feeds_facts_over_the_bridge(tmp_path: Path):
    from bg3harness.game.io import EVAL_TIMEOUT

    rings = FakeRings()
    calls: list[tuple[str, float]] = []

    class Bridge:  # bg3ai.game.Game's rpc and eval
        def rpc(self, op: str, args: dict[str, Any], timeout: float) -> Any:
            calls.append((op, timeout))
            return rings.rpc(op, args)

        def eval(self, code: str, ctx: str, timeout: float) -> str:
            calls.append(("eval", timeout))
            return rings.eval(code, ctx)

    hr.AttachedGame(Bridge()).facts(tmp_path, {"playthrough": PT}).poll(game_read("TUT_Avernus_C"))
    assert [r["kind"] for r in facts_of(tmp_path)] == ["mark", "snapshot"]
    assert {op for op, _ in calls} == {"ref.since", "roll.since", "eval"}
    assert {t for _, t in calls} == {EVAL_TIMEOUT}
