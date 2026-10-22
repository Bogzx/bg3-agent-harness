# pyright: basic
"""Ctrl-C on bg3play-serve with a playthrough running (B3), with real processes: serve's main with the Supervisor over
HarnessRunner, a real agent process (ClientAgent spawning a python that plays forever) and the real memory session
(SessionPy: session.py start / session-end subprocesses). Only the game (always up) and the HTTP server (whose
serve_forever starts the playthrough through the app, then gets the Ctrl-C) are fakes.

PASS: the agent process is gone, the session has its history row ("stopped"), runner.status.json session_ended,
memory session-end ran, no keeper.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from bg3harness import clients
from bg3play import config
from bg3play import server as srv_mod
from bg3play import supervisor as sup
from bg3play.adapters.agent_process import ClientAgent
from bg3play.adapters.harness_runner import HarnessRunner, SessionPy
from bg3play.memory.new_playthrough import create_memory

pytestmark = pytest.mark.e2e

PT = "shut"
WAIT_S = 60


class ForeverClient:
    """A client whose process writes its pid, says one line and plays until it is killed."""

    name = "forever"
    supports_resume = True
    assigns_session = True
    no_tools = ""

    def __init__(self, pidfile: Path):
        self.pidfile = pidfile

    def write_config(self, req: clients.Request) -> list[Path]:
        return []

    def env(self, req: clients.Request) -> dict[str, str]:
        return {}

    def command(self, req: clients.Request) -> list[str]:
        code = (
            "import os, sys, time\n"
            f"open({str(self.pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "print('playing', flush=True)\n"
            "time.sleep(600)\n"
        )
        return [sys.executable, "-c", code]

    def parse(self, line: str) -> list[clients.Event]:
        return [clients.Event("text", text=line.strip())]


class UpGame:
    def begin(self, sdir: Path, mem: Path) -> None:
        return None

    def ok(self) -> bool:
        return True

    def progress(self) -> dict[str, Any] | None:
        return {"level": "TUT"}

    def oom(self) -> str | None:
        return None

    def recover(self, why: str) -> str | None:
        raise RuntimeError("no game")

    def new_game(self) -> None:
        raise RuntimeError("no game")

    def ensure_save(self, since: float) -> str | None:
        return None

    def is_new(self) -> bool:
        return False

    def notes(self) -> list[str]:
        return []


def wait_until(cond, what: str):
    end = time.monotonic() + WAIT_S
    while not (got := cond()):
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.05)
    return got


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_ctrl_c_ends_the_running_session_and_its_agent(tmp_path: Path):
    runtime, pts, cwd = tmp_path / "runtime", tmp_path / "playthroughs", tmp_path / "play"
    runtime.mkdir()
    cwd.mkdir()
    mem = pts / PT / "memory"
    create_memory(PT, mem)
    config.save(pts / PT, config.validate({"v": 1, "name": PT, "keeper": {"enabled": False}}))
    pidfile = tmp_path / "agent.pid"
    agents: list[ClientAgent] = []

    def agent_for(cfg: config.PlaythroughConfig, sdir: Path) -> ClientAgent:
        agents.append(ClientAgent(ForeverClient(pidfile), "m", None, cwd, time.time))
        return agents[-1]

    runner = HarnessRunner(runtime, pts, agent_for, UpGame(), SessionPy, time.time)

    class CtrlCServer:
        server_address = ("127.0.0.1", 0)

        def __init__(self, app: srv_mod.App):
            self.app = app

        def serve_forever(self) -> None:
            assert self.app.control(PT, "start", {})[0] == 200
            wait_until(lambda: pidfile.exists() and pidfile.read_text(), "the agent process")
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    served: list[CtrlCServer] = []

    def fake_serve(app: srv_mod.App, host: str, port: int, verbose: bool = False) -> CtrlCServer:
        served.append(CtrlCServer(app))
        return served[-1]

    with (
        mock.patch.object(srv_mod, "serve", fake_serve),
        mock.patch.object(srv_mod, "session_runner", lambda opt: (runner, None)),
    ):
        rc = srv_mod.main(["--runtime", str(runtime), "--playthroughs", str(pts), "--port", "0", "--supervise"])
    assert rc == 0
    supervisor = served[0].app.supervisor
    assert supervisor is not None and supervisor.state == "idle"
    assert not alive(int(pidfile.read_text()))
    hist = sup.read_history(sup.history_path(runtime, PT))
    assert [(h["session"], h["outcome"]) for h in hist] == [("S001", "stopped")]
    status = json.loads((runtime / "runner.status.json").read_text())
    assert status["session"] == "S001" and status["session_ended"] is not None
    play = json.loads((runtime / "sessions" / PT / "runner.json").read_text())["history"]
    assert [p["reason"] for p in play] == ["aborted"]
    assert "ended before wrap-up" in (mem / "sessions" / "S001.md").read_text()  # session.py session-end ran
    evs = [json.loads(ln) for ln in (runtime / "sessions" / PT / "runner_events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e.get("why")) for e in evs] == [("runner_start", None), ("runner_stop", "stopped")]
    assert status["runner_pid"] is None


def test_ctrl_c_during_the_keeper_pass_ends_its_client(tmp_path: Path):
    # B6: the keeper's client runs in its own process group; on Ctrl-C the app waited SHUTDOWN_S for it and left it
    # running (a paid pass kept spending)
    from bg3play.memory import keeper

    runtime, pts = tmp_path / "runtime", tmp_path / "playthroughs"
    runtime.mkdir()
    create_memory(PT, pts / PT / "memory")
    config.save(pts / PT, config.validate({"v": 1, "name": PT}))
    pidfile = tmp_path / "keeper.pid"

    class OneSession:
        def run_session(self, req: sup.SessionRequest, stop) -> sup.SessionResult:
            stop.set()  # one session, then the keeper
            return sup.SessionResult(f"S{req.session:03d}", "ok")

    def keeper_cmd(*a: Any, **kw: Any) -> list[str]:
        code = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(600)\n"
        return [sys.executable, "-c", code]

    class CtrlCServer:
        server_address = ("127.0.0.1", 0)

        def __init__(self, app: srv_mod.App):
            self.app = app

        def serve_forever(self) -> None:
            assert self.app.control(PT, "start", {})[0] == 200
            wait_until(lambda: pidfile.exists() and pidfile.read_text(), "the keeper's client")
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    served: list[CtrlCServer] = []

    def fake_serve(app: srv_mod.App, host: str, port: int, verbose: bool = False) -> CtrlCServer:
        served.append(CtrlCServer(app))
        return served[-1]

    t0 = time.monotonic()
    with (
        mock.patch.object(srv_mod, "serve", fake_serve),
        mock.patch.object(srv_mod, "SHUTDOWN_S", 20),
        mock.patch.object(srv_mod, "session_runner", lambda opt: (OneSession(), lambda cfg: ("claude", None))),
        mock.patch.object(keeper, "keeper_cmd", keeper_cmd),
    ):
        rc = srv_mod.main(["--runtime", str(runtime), "--playthroughs", str(pts), "--port", "0", "--supervise"])
    assert rc == 0 and time.monotonic() - t0 < 20
    assert served[0].app.supervisor is not None and served[0].app.supervisor.state == "idle"
    assert not alive(int(pidfile.read_text()))
    hist = (runtime / "sessions" / PT / "keeper_history.jsonl").read_text().splitlines()
    (row,) = [json.loads(ln) for ln in hist]
    assert row["sid"] == "S001" and row["timed_out"] and "the app shut down" in row["detail"]
