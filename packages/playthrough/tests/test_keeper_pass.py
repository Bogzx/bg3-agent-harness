import json
from pathlib import Path
from typing import Any

from bg3play import config
from bg3play.keeper_pass import MemoryKeeper
from bg3play.memory.keeper import KeeperResult
from bg3play.supervisor import SessionResult


class FakeRun:
    def __init__(self):
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *a: Any, **kw: Any) -> KeeperResult:
        self.calls.append((a, kw))
        return KeeperResult(f"S{a[1]:03d}", status="ok", files=["quests.md"], commit="abc1234")


def make(tmp_path: Path, client_for, **cfg) -> tuple[MemoryKeeper, FakeRun]:
    pts = tmp_path / "playthroughs"
    (pts / "ilse").mkdir(parents=True)
    config.save(pts / "ilse", config.validate({"v": 1, "name": "ilse", **cfg}))
    run = FakeRun()
    return MemoryKeeper(tmp_path / "runtime", pts, client_for, lambda: 1234.5, run), run


def test_runs_the_pass_with_the_runners_client_and_logs_it(tmp_path: Path):
    profile = object()
    k, run = make(
        tmp_path,
        lambda cfg: ("pi", profile),
        agent={"model": "opus"},
        keeper={"effort": "medium", "minutes": 3.0},
    )
    k.run_keeper("ilse", SessionResult("S004-r1", "ok"))
    ((a, kw),) = run.calls
    d = tmp_path / "runtime" / "sessions" / "ilse"
    assert a == (tmp_path / "playthroughs" / "ilse" / "memory", 4, d / "S004-keeper", "opus", "medium", 3.0)
    budget = tmp_path / "runtime" / "budget.json"
    assert kw == {"client": "pi", "profile": profile, "max_cost": 0.05, "budget": budget}
    (row,) = [json.loads(ln) for ln in (d / "keeper_history.jsonl").read_text().splitlines()]
    assert row["t"] == 1234.5 and row["client"] == "pi" and row["status"] == "ok" and row["commit"] == "abc1234"


def test_keeper_model_overrides_the_play_model(tmp_path: Path):
    k, run = make(
        tmp_path, lambda cfg: ("codex", None), agent={"model": "gpt"}, keeper={"model": "gpt-mini", "max_cost_usd": 0.2}
    )
    k.run_keeper("ilse", SessionResult("S001", "ok"))
    assert run.calls[0][0][3] == "gpt-mini" and run.calls[0][1]["max_cost"] == 0.2


def test_nothing_when_the_runner_keeps_the_memory_or_no_session(tmp_path: Path):
    k, run = make(tmp_path, lambda cfg: None)
    k.run_keeper("ilse", SessionResult("S001", "ok"))
    k2, run2 = make(tmp_path / "b", lambda cfg: ("pi", None))
    k2.run_keeper("ilse", SessionResult("?", "error"))
    assert run.calls == [] and run2.calls == []
    assert not (tmp_path / "runtime" / "sessions" / "ilse" / "keeper_history.jsonl").exists()
