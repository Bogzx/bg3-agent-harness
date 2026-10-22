"""adapters/agent_process.py: one agent process pumped into the session's files, stopped at its cap, on a game
fault or at its cost cap. A fake process whose stream ends when it is killed; no sleeps."""

import json
import queue
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from bg3harness import clients
from bg3play.adapters.agent_process import AgentCall, ClientAgent


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeClient:
    name = "fake"
    supports_resume = True
    assigns_session = True
    no_tools = ""

    def __init__(self):
        self.written: list[clients.Request] = []

    def write_config(self, req: clients.Request) -> list[Path]:
        self.written.append(req)
        return []

    def env(self, req: clients.Request) -> dict[str, str]:
        return {"FAKE": "1"}

    def command(self, req: clients.Request) -> list[str]:
        return ["fake", "--resume" if req.resume else "--session-id", str(req.session), req.prompt]

    def parse(self, line: str) -> list[clients.Event]:
        d = json.loads(line)
        if d["type"] == "say":
            return [clients.Event("text", text=d["text"])]
        if d["type"] == "usage":
            return [clients.Event("usage", cost_usd=d["cost"])]
        return [clients.Event("result", subtype="success", cost_usd=d.get("cost"))]


class FakeProc:
    """Its stdout yields the lines put on `lines`; None (or kill) ends it."""

    def __init__(self, lines: list[str], hold: bool):
        self.q: queue.Queue[str | None] = queue.Queue()
        for ln in lines:
            self.q.put(ln)
        if not hold:
            self.q.put(None)
        self.killed = False

    @property
    def stdout(self) -> Iterator[str]:
        while (ln := self.q.get()) is not None:
            yield ln

    def wait(self) -> int:
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True
        self.q.put(None)


def call(tmp_path: Path, **kw: Any) -> AgentCall:
    base: dict[str, Any] = {"sdir": tmp_path, "sid": "S004", "prompt": "Play.", "cap_s": 600.0, "conversation": "c-1",
            "resume": False, "env": {"BG3AI_SESSION": "S004"}}  # fmt: skip
    return AgentCall(**(base | kw))


def agent(
    lines: list[str],
    hold: bool = False,
    clock: Clock | None = None,
    fault: Callable[[], str | None] = lambda: None,
    watch_s: float = 30.0,
) -> tuple[ClientAgent, list[Any]]:
    spawned: list[Any] = []  # (cmd, cwd, env) then the FakeProc, per spawn

    def spawn(cmd: list[str], cwd: Path, env: dict[str, str]) -> FakeProc:
        spawned.append((cmd, cwd, env))
        spawned.append(FakeProc(lines, hold))
        return spawned[-1]

    a = ClientAgent(FakeClient(), "m1", "high", Path("/play"), clock or Clock(), fault, spawn, watch_s)
    return a, spawned


SAY = json.dumps({"type": "say", "text": "Off the first deck."}) + "\n"


def test_an_exited_process_writes_the_logs_the_store_reads(tmp_path: Path):
    a, spawned = agent([SAY, json.dumps({"type": "result", "cost": 0.25}) + "\n"])
    done = threading.Event()
    res = a.run(call(tmp_path), done)
    assert (res.reason, res.exit_code, res.claude_session, res.cost_usd) == ("exited", 0, "c-1", 0.25)
    cmd, cwd, env = spawned[0]
    assert cmd == ["fake", "--session-id", "c-1", "Play."] and cwd == Path("/play")
    assert env["BG3AI_SESSION"] == "S004" and env["FAKE"] == "1"
    assert "Off the first deck." in (tmp_path / "S004.log").read_text()
    assert (tmp_path / "S004.jsonl").read_text().startswith(SAY)
    kinds = [json.loads(ln)["kind"] for ln in (tmp_path / "S004.events.jsonl").read_text().splitlines()]
    assert kinds == ["session_start", "text", "result", "session_end"]


def test_a_resume_appends_to_the_same_files(tmp_path: Path):
    a, _ = agent([SAY])
    a.run(call(tmp_path))
    a2, spawned = agent([SAY])
    a2.run(call(tmp_path, resume=True))
    assert spawned[0][0][1] == "--resume"
    log = (tmp_path / "S004.log").read_text()
    assert log.count("=====") == 2 and "fake resume c-1" in log
    seqs = [json.loads(ln)["seq"] for ln in (tmp_path / "S004.events.jsonl").read_text().splitlines()]
    assert seqs == list(range(6))


def test_the_time_cap_stops_the_process(tmp_path: Path):
    a, spawned = agent([SAY], hold=True)
    res = a.run(call(tmp_path, cap_s=0))
    assert (res.reason, res.fault_at) == ("cap", 1000.0) and spawned[1].killed


def test_a_game_fault_stops_the_process(tmp_path: Path):
    a, spawned = agent([SAY], hold=True, fault=lambda: "hang", watch_s=0)
    res = a.run(call(tmp_path))
    assert (res.reason, res.fault_at, res.exit_code) == ("hang", 1000.0, -9) and spawned[1].killed


def test_the_cost_cap_stops_the_process(tmp_path: Path):
    usage = [json.dumps({"type": "usage", "cost": c}) + "\n" for c in (0.1, 0.3, 0.6)]
    a, spawned = agent(usage, hold=True)
    res = a.run(call(tmp_path, max_cost_usd=0.5))
    assert (res.reason, res.cost_usd) == ("cost_cap", 0.6) and spawned[1].killed
