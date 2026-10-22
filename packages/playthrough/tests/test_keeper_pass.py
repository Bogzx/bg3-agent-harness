# pyright: basic
import json
import threading
from pathlib import Path
from typing import Any

from bg3play import config
from bg3play.keeper_pass import MemoryKeeper
from bg3play.memory.keeper import KeeperResult
from bg3play.supervisor import SessionRequest, SessionResult, Supervisor, SupervisorError


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


def profile_gone(cfg):
    raise SupervisorError("no client profile pi-or")


def test_a_pass_that_cant_start_is_an_error_row_not_an_exception(tmp_path: Path):
    # the client profile vanished between sessions: client_for raised out of run_keeper and the Supervisor ended
    # the whole run on it
    k, run = make(tmp_path, profile_gone)
    k.run_keeper("ilse", SessionResult("S002", "ok"))
    (row,) = [
        json.loads(ln) for ln in (tmp_path / "runtime/sessions/ilse/keeper_history.jsonl").read_text().splitlines()
    ]
    assert run.calls == [] and row["sid"] == "S002" and row["status"] == "error"
    assert row["detail"] == "SupervisorError: no client profile pi-or" and row["t"] == 1234.5


def test_the_run_plays_on_after_a_keeper_that_cant_start(tmp_path: Path):
    k, _ = make(tmp_path, profile_gone)
    played: list[int] = []

    class Runner:
        def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
            played.append(req.session)
            if req.session == 2:
                stop.set()
            return SessionResult(f"S{req.session:03d}", "ok")

    sup = Supervisor(Runner(), k, tmp_path / "runtime", tmp_path / "playthroughs", lambda: 0.0)
    sup.start("ilse", wait=True)
    assert played == [1, 2] and sup.detail == "stopped"


def test_the_keepers_pi_spend_needs_no_runner_on_sys_path(tmp_path: Path):
    # the keeper's pi client and its cost meter came from runner/'s pi_client shim (bg3harness.clients/runs imported
    # the top-level `pi_client`): without runner/ on sys.path a paid keeper pass died with ModuleNotFoundError
    import subprocess
    import sys

    code = """
import sys
from bg3harness import clients, profiles, runs
from bg3play.memory.keeper_spend import KeeperSpend
assert not any(p.rstrip("/").endswith("runner") for p in sys.path), sys.path
p = profiles.parse({"client": "pi", "model": "anthropic/stub-model", "effort": "low",
    "endpoint": {"provider": "openrouter", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "BG3_STUB_KEY"},
    "model_info": {"input": ["text"], "reasoning": True, "context_window": 200000, "max_tokens": 16000,
                   "cost": {"input": 1.0, "output": 4.0, "cache_read": 0.1}}})
assert clients.make("pi", profile=p, system_file="x.md").name == "pi"
assert runs.stream_events(['{"type": "session"}'], name="pi") == []
k = KeeperSpend("pi", 0.05, None)
assert k.paid and k.meter is not None
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
