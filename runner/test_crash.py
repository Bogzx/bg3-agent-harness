#!/usr/bin/env python3
"""Offline crash-recovery test for runner/play.py: a fake game and a fake claude, no BG3, no Steam.

    python3 runner/test_crash.py          (~60 s)

Faults covered, each through the real Runner.play_session (detect → stop claude → recover → resume):
  crash        the game process dies (SIGKILL of the stub's process group)
  port lost    the process lives on but the debug port stops answering
  game hang    the port stays open but every eval times out (the fake blocks for the eval's timeout)
  agent hang   claude prints nothing for AGENT_SILENT_MINUTES and ignores SIGINT/SIGTERM (needs SIGKILL)
plus a crash during the relaunch's load (bg3le `crash:` log → retry, then the previous good save, then give up
with an alert), LaunchWatch's crash signs, the memory note after a crash, the worst-case detection budget with the real constants (≤ 3 min), the Steam-offline guard before any
launch, and session-config.json "minutes" switching the session length between sessions.

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
if os.environ.get("FAKE_CLAUDE_HANG") and not resume:
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # a wedged claude: only SIGKILL stops it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "x"}), flush=True)
    time.sleep(3600)
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
        self.port_lost_pid: int | None = None   # process alive, port 9998 closed
        self.crash_loads: dict[str, int] = {}   # save folder → how many launches with it crash during the load
        self.load_crashed: set[int] = set()     # launches (stub pids) that crashed: bg3le crash log, port closed

    def load_crashes(self) -> bool:
        """Like S014's relaunch: bg3le logs `crash: signal 11` mid-load, the port closes, the launcher lives on."""
        folder, pid = self.loaded(), self.r.game_proc.pid
        if pid in self.load_crashed:
            return True
        if folder is None or self.crash_loads.get(folder, 0) <= 0:
            return False
        self.crash_loads[folder] -= 1
        self.load_crashed.add(pid)
        logs = P.RUNTIME / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"bg3le-{self.r.state['game']['stamp']}.log.{pid}").write_text(
            "[ 1.0] bg3le loaded\n[ 9.1] crash: signal 11 at (nil) on tid 7\n[ 9.1] crash:   pc bg3+0x55cf2e6\n")
        return True

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
        return self.alive() and self.r.game_proc.pid != self.port_lost_pid and not self.load_crashes()

    def game_state(self, timeout: float = P.EVAL_TIMEOUT):
        try:
            self.lua_eval("return tostring(Ext.Utils.GetGameState())", "client", timeout)
        except Exception:  # noqa: BLE001 - like play.game_state
            return None
        return "Running" if self.loaded() else "Menu"

    def lua_eval(self, code: str, context: str = "client", timeout: float = P.EVAL_TIMEOUT) -> str:
        if not self.port_open():
            raise ConnectionError("fake game: connection refused")
        if self.hung:
            time.sleep(timeout)            # the port accepts, the eval never answers
            raise TimeoutError("fake game: eval timed out")
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

        names = ("RUNTIME", "LAUNCH_SH", "HEALTH_INTERVAL", "HEALTH_RETRY_INTERVAL", "HEALTH_EVAL_TIMEOUT",
                 "AGENT_SILENT_MINUTES", "SIGINT_GRACE", "SIGTERM_GRACE", "KILL_GRACE",
                 "port_open", "game_state", "lua_eval", "rpc",
                 "server_ready", "available_ops", "host_character_name", "ui_screen", "read_progress",
                 "pids_of_game", "list_saves", "RESUME_MIN_MINUTES", "steam_ready", "crash_reporter_pids",
                 "LAUNCH_BACKOFF")
        self.saved = {n: getattr(P, n) for n in names}
        os.environ.pop("FAKE_CLAUDE_HANG", None)
        P.RUNTIME = self.tmp / "runtime"
        P.LAUNCH_SH = launch
        P.HEALTH_INTERVAL = 1
        P.HEALTH_RETRY_INTERVAL = 0.5
        P.HEALTH_EVAL_TIMEOUT = 0.5
        P.AGENT_SILENT_MINUTES = 2               # the fake claude idles silently; test_agent_hang sets 3 s
        P.SIGINT_GRACE, P.SIGTERM_GRACE, P.KILL_GRACE = 2, 2, 2
        P.steam_ready = lambda: (True, "test: offline")
        P.crash_reporter_pids = lambda: []   # never a real reporter (kill_game would kill it)
        P.LAUNCH_BACKOFF = (0.2, 0.2)

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
        P.server_ready = lambda: g.port_open() and not g.hung and g.loaded() is not None
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

    def crash_after(self, s: float, hang: bool = False, port_lost: bool = False, load_crashes=None) -> None:
        def go():
            time.sleep(s)
            if load_crashes:
                self.g.crash_loads = dict(load_crashes)   # the relaunches crash, not the session's first launch
            if hang:
                self.g.hung_pid = self.r.game_proc.pid
            elif port_lost:
                self.g.port_lost_pid = self.r.game_proc.pid
            else:
                self.g.crash()
        threading.Thread(target=go, daemon=True).start()

    def events(self) -> list[dict]:
        f = self.r.state.dir / "runner_events.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []

    def assert_recovered(self, fault: str, within_s: float) -> dict:
        ev = self.events()
        faults = [e for e in ev if e["kind"] == "fault"]
        rec = [e for e in ev if e["kind"] == "recovered"]
        self.assertEqual([e["fault"] for e in faults], [fault], ev)
        self.assertEqual([e["fault"] for e in rec], [fault], ev)
        self.assertLessEqual(rec[0]["seconds"], within_s, rec)
        self.assertLessEqual(rec[0]["t"] - faults[0]["t"], within_s)
        return rec[0]

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
        self.assertIn("FAULT crash: game process exited", log)
        self.assertIn("relaunched", self.assert_recovered("crash", 20)["how"])

    def test_crash_during_the_relaunch_is_seen_in_seconds_and_retried(self):
        # S014: the relaunch crashed mid-load (bg3le `crash:` lines), the launcher lived on, and the runner waited
        # 10 min for "game state Running". Now the crash log ends the wait and the same save is tried again.
        self.crash_after(3, load_crashes={"Claude-2__S001-before-imps": 1})
        t0 = time.time()
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "exited")
        self.assertEqual(self.g.loaded(), "Claude-2__S001-before-imps")
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("game died while waiting for", log)
        self.assertIn("bg3le logged crash: signal 11", log)
        self.assertIn("launch 1/3 with Claude-2__S001-before-imps failed", log)
        ev = self.events()
        self.assertEqual([e["attempt"] for e in ev if e["kind"] == "launch_failed"], [1])
        self.assert_recovered("crash", 20)
        self.assertLess(time.time() - t0, 40)
        self.assertNotIn("crashed the game on load", " ".join(self.claude_runs()[1]))   # same save: no note

    def test_second_load_crash_falls_back_to_the_previous_good_save(self):
        self.crash_after(3, load_crashes={"Claude-2__S001-before-imps": 2})
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "exited")
        self.assertEqual(self.g.loaded(), "Claude-1__S001-start")
        self.assertEqual(self.r.state["loaded_save"], "Claude-1__S001-start")
        prompt = self.claude_runs()[1][self.claude_runs()[1].index("-p") + 1]
        self.assertIn('"S001-before-imps" crashed the game on load 2 times', prompt)
        self.assertIn('save "S001-start"', prompt)                                  # recover()'s note: the real one
        self.assertIn("retrying in 0.2 s with Claude-1__S001-start", (P.RUNTIME / "runner.log").read_text())

    def test_three_load_crashes_stop_the_runner_loudly(self):
        self.crash_after(3, load_crashes={"Claude-2__S001-before-imps": 9, "Claude-1__S001-start": 9})
        with self.assertRaises(P.LaunchGaveUp) as cm:
            self.r.play_session(20, first=True)
        self.assertIn("3 launches in a row died", str(cm.exception))
        self.assertEqual([e["save"] for e in self.events() if e["kind"] == "launch_failed"],
                         ["Claude-2__S001-before-imps", "Claude-2__S001-before-imps", "Claude-1__S001-start"])
        self.assertEqual(self.r.state["history"][-1]["reason"], "crash")       # the session is still recorded
        self.assertFalse(self.g.alive())                                       # the last dead launch was killed
        self.assertEqual(len(self.claude_runs()), 1)                           # no resume into a dead game

    def test_port_lost_with_process_alive_is_recovered(self):
        self.crash_after(3, port_lost=True)
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "exited")
        self.assertEqual(len(self.claude_runs()), 2)
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("FAULT crash: debug port 9998 stopped answering (game process still alive)", log)
        self.assertNotEqual(self.r.game_proc.pid, self.g.port_lost_pid)       # the deaf game was killed
        self.assertTrue(self.g.port_open())
        self.assert_recovered("crash", 20)

    def test_hang_is_recovered_too(self):
        self.crash_after(3, hang=True)
        self.r.play_session(20, first=True)
        hung_pid = self.g.hung_pid
        self.assertEqual(len(self.claude_runs()), 2)
        self.assertIn("recovering: stopped responding", (P.RUNTIME / "runner.log").read_text())
        self.assertNotEqual(self.r.game_proc.pid, hung_pid)                   # the hung game was replaced
        self.assertFalse(self.g.hung)
        self.assertIn("liveness evals in a row failed or timed out", (P.RUNTIME / "runner.log").read_text())
        self.assert_recovered("hang", 25)

    def test_agent_hang_restarts_claude_only(self):
        os.environ["FAKE_CLAUDE_HANG"] = "1"
        P.AGENT_SILENT_MINUTES = 3 / 60
        res = self.r.play_session(20, first=True)
        game_pid = self.r.game_proc.pid
        runs = self.claude_runs()
        self.assertEqual(len(runs), 2)
        prompt = runs[1][runs[1].index("-p") + 1]
        self.assertIn("produced no output", prompt)                           # agent_resume_prompt, not a crash note
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], runs[0][runs[0].index("--session-id") + 1])
        self.assertEqual(res.reason, "exited")
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("FAULT agent_hang: no claude output", log)
        self.assertNotIn("recovering:", log)                                  # the game was left alone
        self.assertEqual(self.r.game_proc.pid, game_pid)
        self.assertNotIn("killing the game", log)
        rec = self.assert_recovered("agent_hang", 15)                         # SIGINT, SIGTERM ignored → SIGKILL
        self.assertIn("game untouched", rec["how"])
        self.assertEqual(self.r.state["history"][-1]["resumes"], 1)

    def test_too_little_time_left_ends_the_session_and_keeps_the_note(self):
        P.RESUME_MIN_MINUTES = 30                     # a 20-min session never has 30 left
        self.crash_after(3)
        self.r.play_session(20, first=True)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(self.r.state["history"][-1]["resumes"], 0)
        self.assertEqual(self.g.loaded(), "Claude-2__S001-before-imps")       # recovered for the next session
        self.assertTrue(any("RUNNER NOTE: the game crashed" in n for n in self.r.pending_notes))


class DetectionBudget(unittest.TestCase):
    """With the real constants, every fault is detected and claude stopped within 3 min (the agent hang after its
    silence limit). Then kill_game (≤ KILL_GRACE + KILL_CONFIRM) and BG3's own relaunch + load follow."""

    def test_real_constants(self):
        for kind in ("crash", "hang"):
            self.assertLessEqual(P.detect_budget_s(kind), 180, kind)
        self.assertLessEqual(P.detect_budget_s("agent_hang") - P.AGENT_SILENT_MINUTES * 60, 180)
        self.assertLessEqual(P.AGENT_SILENT_MINUTES, 5)
        self.assertLessEqual(P.KILL_GRACE + P.KILL_CONFIRM, 60)


class SteamGuard(unittest.TestCase):
    """No game launch unless Steam's last login was offline (and Steam runs)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-steam-"))
        self.log = self.tmp / "steamui_login.txt"
        os.environ["BG3AI_STEAM_LOGIN_LOG"] = str(self.log)
        os.environ.pop("BG3AI_ALLOW_STEAM_ONLINE", None)
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "port_open", "LAUNCH_SH")}
        P.RUNTIME = self.tmp / "runtime"
        P.port_open = lambda *a, **k: False
        P.LAUNCH_SH = self.tmp / "must-not-run.sh"

    def tearDown(self):
        os.environ.pop("BG3AI_STEAM_LOGIN_LOG", None)
        for n, v in self.saved.items():
            setattr(P, n, v)

    def write(self, *lines: str) -> None:
        self.log.write_text("\n".join(f"[2026-10-09 21:00:0{i}] [ None ] {ln}" for i, ln in enumerate(lines)) + "\n")

    def test_parser(self):
        sp = P.steam_paths
        self.write("Starting login", "SetLoginState: Success - OK", "Start offline - 1", "SetLoginState: Success - OK")
        self.assertTrue(sp.steam_offline()[0])
        self.write("Start offline - 1", "Starting login", "Received logon success response")   # went online
        ok, why = sp.steam_offline()
        self.assertFalse(ok)
        self.assertIn("online", why)
        self.write("Start offline - 0")
        self.assertFalse(sp.steam_offline()[0])
        self.log.unlink()
        self.assertFalse(sp.steam_offline()[0])

    def test_launch_refused_loudly_when_online(self):
        self.write("Starting login", "Received logon success response")
        r = P.Runner(P.Options("steam-test", None, 20, "opus", save_owner="Claude"))
        with self.assertRaises(P.SteamNotOffline):
            r.launch_game("Claude-1__x")
        self.assertIsNone(r.game_proc)                                    # nothing launched
        self.assertIn("REFUSING TO LAUNCH BG3", (P.RUNTIME / "runner.log").read_text())
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(ev[-1]["kind"], "steam_refused")
        self.assertFalse(json.loads((P.RUNTIME / "runner.status.json").read_text())["steam_offline"])

    def test_steam_not_running_is_refused_too(self):
        self.write("Start offline - 1")
        real = P.subprocess.run
        P.subprocess.run = lambda *a, **k: P.subprocess.CompletedProcess(a, 1, "", "")   # pgrep: no steam
        try:
            ok, why = P.steam_ready()
        finally:
            P.subprocess.run = real
        self.assertFalse(ok)
        self.assertIn("isn't running", why)

    def test_allow_online_override(self):
        self.write("Starting login")
        os.environ["BG3AI_ALLOW_STEAM_ONLINE"] = "1"
        try:
            real = P.subprocess.run
            P.subprocess.run = lambda *a, **k: P.subprocess.CompletedProcess(a, 0, "123\n", "")
            try:
                self.assertTrue(P.steam_ready()[0])
            finally:
                P.subprocess.run = real
        finally:
            os.environ.pop("BG3AI_ALLOW_STEAM_ONLINE")


class RunLoop(unittest.TestCase):
    """Runner.run(): a Steam refusal never counts toward giving up; start/stop land in runner_events.jsonl."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-loop-"))
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "STEAM_RETRY_S", "MAX_SETUP_FAILURES")}
        P.RUNTIME = self.tmp / "runtime"
        P.STEAM_RETRY_S = 0
        P.MAX_SETUP_FAILURES = 2

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(P, n, v)

    def test_steam_refusals_retry_forever_then_play(self):
        r = P.Runner(P.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        calls = []

        def play(minutes, first):
            calls.append(minutes)
            if len(calls) <= 5:                     # more than MAX_SETUP_FAILURES refusals
                raise P.SteamNotOffline("last Steam login was online")
            r.stop_requested = True
            return P.SessionResult("S001", "c", 0, "exited", time.time() - 600, time.time())
        r.play_session = play
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 6)
        kinds = [json.loads(ln)["kind"] for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual((kinds[0], kinds[-1]), ("runner_start", "runner_stop"))
        self.assertNotIn("setup_failed", kinds)
        self.assertIn("Steam not offline", (P.RUNTIME / "runner.log").read_text())


    def test_launch_gave_up_stops_the_runner_with_an_alert(self):
        r = P.Runner(P.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None

        def play(minutes, first):
            raise P.LaunchGaveUp("3 launches in a row died before the save played: x")
        r.play_session = play
        self.assertEqual(r.run(), 1)
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([e["kind"] for e in evs][-2:], ["launch_gave_up", "runner_stop"])
        self.assertEqual(evs[-1]["why"], "gave up: the game crashes on every launch")
        alert = json.loads((P.RUNTIME / "runner.status.json").read_text())["alert"]
        self.assertEqual(alert["kind"], "launch_gave_up")
        self.assertIn("GAME CRASH LOOP", (P.RUNTIME / "runner.log").read_text())
        r2 = P.Runner(P.Options("loop-test", None, 20, "opus"))                # the next run clears the alert
        r2.check_memory = lambda: None
        r2.stop_requested = True
        r2.run()
        self.assertIsNone(json.loads((P.RUNTIME / "runner.status.json").read_text())["alert"])


class FakeMenuGame:
    """A game at the main menu's splash ("Press any key"): the client state flips LoadMenu/Menu (or stays LoadMenu),
    ui.state shows SplashScreen as a panel, ui.menu fails until a key gets past it; a load takes 3 polls."""

    def __init__(self, splash: bool = True, flip: bool = True, load_starts: bool = True):
        self.splash, self.flip, self.load_starts = splash, flip, load_starts
        self.polls, self.presses, self.loads, self.loading, self.running = 0, 0, [], 0, False

    def game_state(self, timeout: float = 0):
        self.polls += 1
        if self.running:
            return "Running"
        if self.loading:
            self.loading -= 1
            self.running = self.loading == 0
            return "Running" if self.running else "LoadSession"
        if self.splash:
            return ("LoadMenu", "Menu")[self.polls % 2] if self.flip else "LoadMenu"
        return "Menu"

    def rpc(self, op: str, args=None, context: str = "server", timeout: float = 0):
        if op == "ui.state":
            if self.running:
                return {"screen": "game"}
            return {"screen": "game", "panels_open": ["SplashScreen"]} if self.splash else {"screen": "main_menu"}
        if op == "ui.menu":
            if self.splash:
                raise P.RpcError("ui.menu: not at the main menu")
            self.loads.append(args)
            self.loading = 3 if self.load_starts else 0
            return {}
        return {}

    def press(self) -> str:
        self.presses += 1
        self.splash = False
        return "fake Enter"


class SplashMenu(unittest.TestCase):
    """Live bug: a fresh game at the splash (state LoadMenu/Menu) made ensure_game wait 5+ min for Running.
    Now LoadMenu counts as the menu, the splash gets a key, and Running is awaited only after a load started."""

    NAMES = ("RUNTIME", "port_open", "game_state", "rpc", "available_ops", "server_ready", "list_saves",
             "press_menu_key", "host_character_name", "MENU_POLL", "SPLASH_LOADMENU_S", "LOAD_START_TIMEOUT")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-splash-"))
        self.saved = {n: getattr(P, n) for n in self.NAMES}
        P.RUNTIME = self.tmp / "runtime"
        P.MENU_POLL, P.SPLASH_LOADMENU_S, P.LOAD_START_TIMEOUT = 0.01, 0.2, 1

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(P, n, v)

    def run_ensure(self, g: FakeMenuGame):
        P.port_open = lambda *a, **k: True
        P.game_state, P.rpc, P.press_menu_key = g.game_state, g.rpc, g.press
        P.available_ops = lambda ctx: {"ui.state", "ui.menu"}
        P.server_ready = lambda: g.running
        P.host_character_name = lambda: "Claude"
        P.list_saves = lambda: [P.Save("Claude-1__S014-start", "Claude", "S014-start", time.time() - 60)]
        r = P.Runner(P.Options("splash-test", None, 20, "opus", save_owner="Claude"))
        r.mem = self.tmp / "memory"
        r.owns_game = lambda: True
        t0 = time.time()
        label = r.ensure_game(first=True)
        return r, label, time.time() - t0, (P.RUNTIME / "runner.log").read_text()

    def test_splash_flipping_loadmenu_menu_gets_a_key_then_the_save_loads(self):
        g = FakeMenuGame()
        r, label, secs, log = self.run_ensure(g)
        self.assertEqual(label, "S014-start")
        self.assertEqual(g.presses, 1)
        self.assertEqual(len(g.loads), 1)
        self.assertTrue(g.running)
        self.assertEqual(r.state["loaded_save"], "Claude-1__S014-start")
        self.assertLess(secs, 5)
        self.assertNotIn("game is busy", log)
        self.assertIn("splash screen (state Menu): pressed fake Enter", log)

    def test_splash_stuck_in_loadmenu_gets_a_key_after_a_while(self):
        g = FakeMenuGame(flip=False)
        _r, label, _secs, log = self.run_ensure(g)
        self.assertEqual((label, g.presses), ("S014-start", 1))
        self.assertIn("splash screen (state LoadMenu)", log)

    def test_main_menu_gets_no_key(self):
        # a key on the main menu would hit Continue
        g = FakeMenuGame(splash=False)
        _r, label, _secs, _log = self.run_ensure(g)
        self.assertEqual((label, g.presses, len(g.loads)), ("S014-start", 0, 1))

    def test_a_load_that_never_starts_fails_fast_instead_of_waiting_for_running(self):
        g = FakeMenuGame(load_starts=False)
        t0 = time.time()
        with self.assertRaisesRegex(RuntimeError, "never started"):
            self.run_ensure(g)
        self.assertLess(time.time() - t0, 5)
        self.assertNotIn("waiting for game state Running", (P.RUNTIME / "runner.log").read_text())


class Watch(unittest.TestCase):
    """LaunchWatch: each crash sign, read from fakes (no real processes or ports)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-watch-"))
        self.saved = {n: getattr(P, n) for n in ("pids_of_game", "crash_reporter_pids", "port_open")}
        self.pids, self.reporters, self.port = [], [], False
        P.pids_of_game = lambda: list(self.pids)
        P.crash_reporter_pids = lambda: list(self.reporters)
        P.port_open = lambda *a, **k: self.port
        self.w = P.LaunchWatch("20261010-020143", self.tmp, started=100.0)

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(P, n, v)

    def test_crash_line_in_this_launchs_bg3le_log(self):
        main = self.tmp / "bg3le-20261010-020143.log.379707"
        (self.tmp / "bg3le-20261009-222233.log.124451").write_text("[ 1.0] crash: signal 11 (an older launch)\n")
        main.write_text("[ 1.0] bg3le loaded\n[ 2.0] Lua debugger listening\n[ 3.0] cra")   # partial last line
        self.assertIsNone(self.w.check(now=101))
        with open(main, "a") as f:
            f.write("sh: signal 11 at (nil) on tid 379801\n[ 3.0] crash:   pc bg3+0x55cf2e6\n")
        self.assertEqual(self.w.check(now=102), "bg3le logged crash: signal 11 at (nil) on tid 379801")

    def test_crash_reporter_log_and_process(self):
        (self.tmp / "bg3le-20261010-020143.log.383698").write_text(
            "[ 1.0] bg3le loaded\n[ 1.0] host is LinuxCrashReporter, not bg3 -- bg3le stays inactive\n")
        self.assertIn("LinuxCrashReporter started", self.w.check(now=101))
        w = P.LaunchWatch("other", self.tmp, started=100.0)
        self.reporters = [4242]
        self.assertEqual(w.check(now=101), "LinuxCrashReporter is running (pid 4242)")

    def test_bg3_gone_after_it_was_up(self):
        self.assertIsNone(self.w.check(now=101))          # not up yet: still launching
        self.pids, self.port = [7], True
        self.assertIsNone(self.w.check(now=105))
        self.pids = []
        self.assertEqual(self.w.check(now=110), "the bg3 process is gone")

    def test_port_refused_too_long_with_bg3_up(self):
        self.pids = [7]
        self.assertIsNone(self.w.check(now=110))
        self.assertIsNone(self.w.check(now=169))
        self.assertIn("refused for 61 s", self.w.check(now=171))
        w = P.LaunchWatch("x", self.tmp, started=100.0)  # open, then closed: counted from the last open
        self.port = True
        self.assertIsNone(w.check(now=110))
        self.port = False
        self.assertIsNone(w.check(now=169))
        self.assertIn("refused for 61 s", w.check(now=171))


class MemoryAfterCrash(unittest.TestCase):
    """close_memory_session after a game fault: the handoff error is not passed on as a lint task (S014)."""

    S014 = ("memory lint: m — 2 error(s), 0 warning(s)\n"
            "ERROR  handoff     HANDOFF.md was written by S013, not by the current session S014. Rewrite it.\n"
            "ERROR  stale       objectives.md unchanged for 3 sessions\nFix the errors above, then re-run.\n")

    def runner(self, report: str):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-mem-"))
        P.RUNTIME = self.tmp / "runtime"
        r = P.Runner(P.Options("mem-test", None, 20, "opus"))
        r.memory_state = lambda: {"open": True, "current": 14}
        r.session_py = lambda *a: P.subprocess.CompletedProcess(a, 1, report, "")
        self.fix_runs = []
        r.run_claude = lambda *a, **k: self.fix_runs.append(a)
        return r

    def setUp(self):
        self.saved_rt = P.RUNTIME

    def tearDown(self):
        P.RUNTIME = self.saved_rt

    def res(self, reason):
        return P.SessionResult("S014", "c", 0, reason, time.time() - 400, time.time())

    def test_lint_error_lines(self):
        self.assertEqual(P.memory_lint_errors(self.S014, skip=("handoff",)),
                         ["ERROR  stale       objectives.md unchanged for 3 sessions"])
        self.assertEqual(len(P.memory_lint_errors(self.S014)), 2)

    def test_crash_with_only_the_handoff_error_leaves_no_note(self):
        r = self.runner("ERROR  handoff     HANDOFF.md was written by S013, not by the current session S014.\n")
        r.close_memory_session(self.res("crash"))
        self.assertEqual((r.pending_notes, self.fix_runs), ([], []))
        self.assertIn("memory left open for the next session", (P.RUNTIME / "runner.log").read_text())
        self.assertNotIn("fix-notes session", (P.RUNTIME / "runner.log").read_text())  # none runs after a crash

    def test_crash_passes_on_other_errors_only(self):
        r = self.runner(self.S014)
        r.close_memory_session(self.res("hang"))
        self.assertEqual(len(r.pending_notes), 1)
        self.assertIn("objectives.md unchanged", r.pending_notes[0])
        self.assertNotIn("written by S013", r.pending_notes[0])

    def test_normal_end_still_runs_the_fix_pass_and_passes_everything_on(self):
        r = self.runner(self.S014)
        r.close_memory_session(self.res("exited"))
        self.assertEqual(len(self.fix_runs), 1)
        self.assertIn("written by S013", r.pending_notes[0])


class LiveSessionLength(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-len-"))
        self.saved_rt = P.RUNTIME
        P.RUNTIME = self.tmp / "runtime"

    def tearDown(self):
        P.RUNTIME = self.saved_rt

    def test_minutes_from_session_config_each_session(self):
        r = P.Runner(P.Options("len-test", None, 20, "opus"))
        cfg = r.state.dir / "session-config.json"
        self.assertEqual(r.session_minutes(), 20)                          # no file: --session-minutes
        cfg.write_text(json.dumps({"minutes": 60, "effort": "low"}))
        self.assertEqual(r.session_minutes(), 60)
        self.assertIn("session length now 60 min", (P.RUNTIME / "runner.log").read_text())
        cfg.write_text(json.dumps({"minutes": 5}))                          # too short without --allow-short-sessions
        self.assertEqual(r.session_minutes(), 20)
        cfg.write_text(json.dumps({"minutes": "60"}))                       # bad type: ignored
        self.assertEqual(r.session_minutes(), 20)
        cfg.write_text("{broken")
        self.assertEqual(r.session_minutes(), 20)
        r.opt.allow_short = True
        cfg.write_text(json.dumps({"minutes": 5}))
        self.assertEqual(r.session_minutes(), 5)
        self.assertEqual(json.loads((P.RUNTIME / "runner.status.json").read_text())["session_minutes"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
