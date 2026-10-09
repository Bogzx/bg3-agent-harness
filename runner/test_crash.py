#!/usr/bin/env python3
"""Offline crash-recovery test for runner/play.py: a fake game and a fake claude, no BG3, no Steam.

    python3 runner/test_crash.py          (~30 s)

The fake game is a real process started by the real Runner.launch_game() (LAUNCH_SH points at a stub script),
so the launch → crash → kill → relaunch path runs for real; only the game's answers (debug port, game state,
saves) are simulated, from whether that process is alive. A crash is a SIGKILL of the stub's process group.
The fake claude is a stub on the real claude command line (so --resume and the session id are checked).

Safety: everything that could reach the real game is patched. pids_of_game() never returns a real bg3 pid,
port_open()/lua_eval()/rpc() never touch port 9998, and RUNTIME/SAVE data live in a temp dir.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import play as P  # noqa: E402

FAKE_LAUNCH = """#!/bin/sh
# stand-in for runner/launch_game.sh: just a process that lives until killed
echo "fake game $@"
exec sleep 3600
"""

FAKE_CLAUDE = r"""
import json, os, sys, time
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "t": time.time()}) + "\n")
resume = "--resume" in sys.argv
print(json.dumps({"type": "system", "subtype": "init", "session_id": "x"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "resumed" if resume else "playing"}]}}), flush=True)
try:
    time.sleep(float(os.environ["FAKE_CLAUDE_RESUME_S" if resume else "FAKE_CLAUDE_FIRST_S"]))
except KeyboardInterrupt:
    sys.exit(130)
print(json.dumps({"type": "result", "subtype": "success", "result": "done"}), flush=True)
"""


class FakeGame:
    """Simulated answers of the game launched by the runner (alive = the stub process is alive)."""

    def __init__(self, runner: P.Runner, saves: list[P.Save]):
        self.r = runner
        self.saves = list(saves)
        self.hung_pid: int | None = None   # a hang belongs to one game process; a relaunch answers again

    @property
    def hung(self) -> bool:
        return self.alive() and self.r.game_proc.pid == self.hung_pid

    def alive(self) -> bool:
        p = self.r.game_proc
        return p is not None and p.poll() is None

    def loaded(self) -> str | None:
        p = self.r.game_proc
        if not self.alive():
            return None
        a = list(p.args)
        return a[a.index("--load") + 1] if "--load" in a else None

    def port_open(self, *a, **k) -> bool:
        return self.alive()

    def game_state(self):
        if not self.alive() or self.hung:
            return None
        return "Running" if self.loaded() else "Menu"

    def lua_eval(self, code: str, context: str = "client", timeout: float = 0) -> str:
        if not self.alive() or self.hung:
            raise ConnectionError("fake game: no answer")
        if "F5" in code:  # the runner's quicksave fallback
            self.saves.append(P.Save(f"Claude-{900 + len(self.saves)}__QuickSave_1", "Claude", "QuickSave_1", time.time()))
        return "{}"

    def rpc(self, op: str, args=None, context: str = "server", timeout: float = 0):
        if not self.alive() or self.hung:
            raise P.RpcError(f"{op}: fake game down")
        if context == "server" and not self.loaded():
            raise P.RpcError(f"{op}: no server context at the menu")
        return {}

    def crash(self) -> None:
        os.killpg(self.r.game_proc.pid, signal.SIGKILL)


class CrashRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-crash-"))
        launch = self.tmp / "launch_game.sh"
        launch.write_text(FAKE_LAUNCH)
        launch.chmod(0o755)
        claude = self.tmp / "fake_claude.py"
        claude.write_text(FAKE_CLAUDE)
        self.claude_log = self.tmp / "claude.jsonl"
        os.environ.update(FAKE_CLAUDE_LOG=str(self.claude_log), FAKE_CLAUDE_FIRST_S="60", FAKE_CLAUDE_RESUME_S="2")

        names = ("RUNTIME", "LAUNCH_SH", "HEALTH_INTERVAL", "port_open", "game_state", "lua_eval", "rpc",
                 "server_ready", "available_ops", "host_character_name", "ui_screen", "read_progress",
                 "pids_of_game", "list_saves", "RESUME_MIN_MINUTES")
        self.saved = {n: getattr(P, n) for n in names}
        P.RUNTIME = self.tmp / "runtime"
        P.LAUNCH_SH = launch
        P.HEALTH_INTERVAL = 1

        r = P.Runner(P.Options("crash-test", None, 20, "opus", save_owner="Claude"))
        r.next_sid = lambda: "S001"
        r.close_memory_session = lambda res: None
        r.mem = self.tmp / "memory"
        real_cmd = r.claude_cmd
        r.claude_cmd = lambda *a, **k: [sys.executable, str(claude)] + real_cmd(*a, **k)[1:]
        now = time.time()
        g = FakeGame(r, [P.Save("Claude-1__S001-start", "Claude", "S001-start", now - 300),
                         P.Save("Claude-2__S001-before-imps", "Claude", "S001-before-imps", now - 100),
                         P.Save("Tav-3__my own game", "Tav", "my own game", now - 10)])   # newer, not ours
        P.port_open, P.game_state, P.lua_eval, P.rpc = g.port_open, g.game_state, g.lua_eval, g.rpc
        P.server_ready = lambda: g.alive() and not g.hung and g.loaded() is not None
        P.available_ops = lambda ctx: set()
        P.host_character_name = lambda: "Claude"
        P.ui_screen = lambda: "game"
        P.read_progress = lambda: None
        P.pids_of_game = lambda: []          # never a real bg3 pid; the stub dies with its process group
        P.list_saves = lambda: list(g.saves)
        self.r, self.g = r, g

    def tearDown(self):
        if self.g.alive():
            os.killpg(self.r.game_proc.pid, signal.SIGKILL)
            self.r.game_proc.wait()
        for n, v in self.saved.items():
            setattr(P, n, v)

    def claude_runs(self) -> list[list[str]]:
        return [json.loads(ln)["argv"] for ln in self.claude_log.read_text().splitlines()]

    def crash_after(self, s: float, hang: bool = False) -> None:
        def go():
            time.sleep(s)
            if hang:
                self.g.hung_pid = self.r.game_proc.pid
            else:
                self.g.crash()
        threading.Thread(target=go, daemon=True).start()

    def test_crash_relaunches_loads_latest_own_save_and_resumes_claude(self):
        self.crash_after(4)
        res = self.r.play_session(20, first=True)

        relaunch = self.r.game_proc
        runs = self.claude_runs()
        self.assertEqual(len(runs), 2, runs)
        sid_first = runs[0][runs[0].index("--session-id") + 1]
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], sid_first)   # same conversation
        prompt = runs[1][runs[1].index("-p") + 1]
        self.assertIn("the game crashed", prompt)
        self.assertIn("S001-before-imps", prompt)                              # recover()'s RUNNER NOTE
        self.assertIsNotNone(relaunch)
        self.assertEqual(self.g.loaded(), "Claude-2__S001-before-imps")         # newest Claude save, not Tav's
        self.assertEqual(res.reason, "exited")
        h = self.r.state["history"][-1]
        self.assertEqual((h["session"], h["reason"], h["resumes"]), ("S001", "exited", 1))
        self.assertGreater(h["minutes"], 0.05)                                  # whole session, both legs
        self.assertEqual(self.r.pending_notes, [])                              # the note went to the resume
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("recovering: crashed", log)
        self.assertIn("resuming claude after the crash", log)
        self.assertTrue((self.r.state.dir / "S001-r1.log").exists())

    def test_hang_is_recovered_too(self):
        self.crash_after(3, hang=True)
        hung_pid = None
        self.r.play_session(20, first=True)
        hung_pid = self.g.hung_pid
        self.assertEqual(len(self.claude_runs()), 2)
        self.assertIn("recovering: stopped responding", (P.RUNTIME / "runner.log").read_text())
        self.assertNotEqual(self.r.game_proc.pid, hung_pid)                   # the hung game was replaced
        self.assertFalse(self.g.hung)

    def test_too_little_time_left_ends_the_session_and_keeps_the_note(self):
        P.RESUME_MIN_MINUTES = 30                     # a 20-min session never has 30 left
        self.crash_after(3)
        self.r.play_session(20, first=True)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(self.r.state["history"][-1]["resumes"], 0)
        self.assertEqual(self.g.loaded(), "Claude-2__S001-before-imps")       # recovered for the next session
        self.assertTrue(any("RUNNER NOTE: the game crashed" in n for n in self.r.pending_notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
