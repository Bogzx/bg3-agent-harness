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

The detection budget, the Steam guard and the run loop are in test_crash_loop.py; the splash/menu loads and the
restart drift in test_crash_menu.py.
The fake game is a real process started by the real Runner.launch_game() (LAUNCH_SH points at a stub script),
so the launch → crash → kill → relaunch path runs for real; only the game's answers (debug port, game state,
saves) are simulated, from whether that process is alive. A crash is a SIGKILL of the stub's process group.
The fake claude is a stub on the real claude command line (so --resume and the session id are checked).

Safety: everything that could reach the real game is patched. pids_of_game()/all_bg3_pids() never return a real bg3 pid,
port_open()/lua_eval()/rpc() never touch port 9998, and RUNTIME/SAVE data live in a temp dir.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import game_side as rgame
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
import legacy_series as lser
from bg3harness.game import io as G  # noqa: E402  (the game's I/O, moved out of play.py)
from bg3harness.game import controller as C  # noqa: E402, N812  (the GameController and its timings)
from bg3harness.game import oom as O  # noqa: E402, N812  (the OOM check's journal and ramwatch reads)

G_FUNCS = {n for n, v in vars(G).items() if callable(v) and getattr(v, "__module__", None) == G.__name__
           and not isinstance(v, type)}


CTL_NAMES = ("KILL_GRACE", "KILL_CONFIRM", "LAUNCH_BACKOFF", "MENU_POLL", "SPLASH_LOADMENU_S")
MOVED = dict.fromkeys(CTL_NAMES, C) | dict.fromkeys(("user_journal", "read_spikes"), O)


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io, the GameController's
    timings to bg3harness.game.controller and the OOM check's reads to bg3harness.game.oom."""
    return MOVED.get(name) or (G if name in G_FUNCS else testenv.home(name))

FAKE_LAUNCH = """#!/bin/sh
# stand-in for runner/launch_game.sh: just a process that lives until killed
echo "fake game $@"
exec sleep 3600
"""

FAKE_CLAUDE = r"""
import json, os, sys, time
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as f:
    env = {k: os.environ.get(k) for k in ("BG3AI_SAVE_OWNER", "BG3AI_SAVE_PREFIX", "BG3AI_SAVE_GATES", "BG3AI_SESSION")}
    f.write(json.dumps({"argv": sys.argv[1:], "t": time.time(), "env": env}) + "\n")
resume = "--resume" in sys.argv
if os.environ.get("FAKE_CLAUDE_HANG") and not resume:
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # a wedged claude: only SIGKILL stops it
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "x",
                      "mcp_servers": [{"name": "bg3", "status": "connected"}]}), flush=True)
    time.sleep(3600)
# RUN-8: FAKE_CLAUDE_MCP=failed → the bg3 server failed at init (the agent then plays on with no game tools)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "x",
                  "mcp_servers": [{"name": "bg3", "status": os.environ.get("FAKE_CLAUDE_MCP", "connected")}]}),
      flush=True)
# RUN-7: FAKE_CLAUDE_LIMIT=N → the first N processes hit the usage limit (claude's shapes) and exit 1 at once
n_calls = sum(1 for _ in open(log))
if n_calls <= int(os.environ.get("FAKE_CLAUDE_LIMIT", "0")):
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                      "resetsAt": float(os.environ.get("FAKE_CLAUDE_RESET", "0")) or None}}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text",
                      "text": "Claude AI usage limit reached. Your limit will reset at 3pm."}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                      "result": "Claude AI usage limit reached. Your limit will reset at 3pm.", "total_cost_usd": 0}),
          flush=True)
    sys.exit(1)
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "resumed" if resume else "playing"}]}}), flush=True)
# FAKE_CLAUDE_HOLD_S=N → the bg3 MCP server's fight hold (a fight on at WRAP UP): the session ends N s from now at most
if os.environ.get("FAKE_CLAUDE_HOLD_S") and os.environ.get("BG3AI_FIGHT_HOLD_PATH") and not resume:
    with open(os.environ["BG3AI_FIGHT_HOLD_PATH"], "w") as f:
        json.dump({"deadline": time.time() + float(os.environ["FAKE_CLAUDE_HOLD_S"]), "held": True}, f)
try:
    time.sleep(float(os.environ["FAKE_CLAUDE_RESUME_S" if resume else "FAKE_CLAUDE_FIRST_S"]))
except KeyboardInterrupt:
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "num_turns": 7, "total_cost_usd": 0.25,
                      "usage": {"input_tokens": 100, "output_tokens": 50}}), flush=True)
    sys.exit(130)
print(json.dumps({"type": "result", "subtype": "success", "result": "done", "num_turns": 3, "total_cost_usd": 0.5,
                  "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
"""


class FakeGame:
    """Simulated answers of the game launched by the runner (alive = the stub process is alive)."""

    def __init__(self, runner: lpt.Runner, saves: list[rpolicy.Save]):
        self.r = runner
        self.saves = list(saves)
        self.hung_pid: int | None = None   # a hang belongs to one game process; a relaunch answers again
        self.port_lost_pid: int | None = None   # process alive, port 9998 closed
        self.crash_loads: dict[str, int] = {}   # save folder → how many launches with it crash during the load
        self.load_crashed: set[int] = set()     # launches (stub pids) that crashed: bg3le crash log, port closed
        self.mod_gone_pid: int | None = None    # RUN-8: this launch answers evals but Mods.BG3AI is nil

    def load_crashes(self) -> bool:
        """Like S014's relaunch: bg3le logs `crash: signal 11` mid-load, the port closes, the launcher lives on."""
        folder, pid = self.loaded(), self.r.game_proc.pid
        if pid in self.load_crashed:
            return True
        if folder is None or self.crash_loads.get(folder, 0) <= 0:
            return False
        self.crash_loads[folder] -= 1
        self.load_crashed.add(pid)
        logs = renv.RUNTIME / "logs"
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

    def game_state(self, timeout: float = rgame.EVAL_TIMEOUT):
        try:
            self.lua_eval("return tostring(Ext.Utils.GetGameState())", "client", timeout)
        except Exception:  # noqa: BLE001 - like play.game_state
            return None
        return "Running" if self.loaded() else "Menu"

    def lua_eval(self, code: str, context: str = "client", timeout: float = rgame.EVAL_TIMEOUT) -> str:
        if not self.port_open():
            raise ConnectionError("fake game: connection refused")
        if self.hung:
            time.sleep(timeout)            # the port accepts, the eval never answers
            raise TimeoutError("fake game: eval timed out")
        if code == rgame.MOD_READY_LUA:
            return "false" if self.r.game_proc is not None and self.r.game_proc.pid == self.mod_gone_pid else "true"
        if "F5" in code:  # the runner's quicksave fallback
            t = time.time()
            self.saves.append(rpolicy.Save(f"Claude-{900 + len(self.saves)}__QuickSave_1", "Claude", "QuickSave_1", t, t))
        return "{}"

    def rpc(self, op: str, args=None, context: str = "server", timeout: float = 0):
        if not self.alive() or self.hung:
            raise rgame.RpcError(f"{op}: fake game down")
        if context == "server" and not self.loaded():
            raise rgame.RpcError(f"{op}: no server context at the menu")
        return {}

    def crash(self) -> None:
        os.killpg(self.r.game_proc.pid, signal.SIGKILL)


class FakeRunBase(unittest.TestCase):
    """The real Runner on a fake game (a stub process) and a fake claude; see the module docstring."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-crash-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
                 "pids_of_game", "all_bg3_pids", "list_saves", "RESUME_MIN_MINUTES", "steam_ready", "crash_reporter_pids",
                 "LAUNCH_BACKOFF", "PLAYTHROUGHS", "press_menu_key", "MENU_POLL", "bg3le_problems", "MOD_GONE_S",
                 "user_journal", "SE_BG3AI_DIR")
        self.saved = {n: getattr(home(n), n) for n in names}
        for k in ("FAKE_CLAUDE_HANG", "FAKE_CLAUDE_MCP", "FAKE_CLAUDE_LIMIT", "FAKE_CLAUDE_RESET", "FAKE_CLAUDE_HOLD_S"):
            os.environ.pop(k, None)
            self.addCleanup(os.environ.pop, k, None)
        renv.RUNTIME = self.tmp / "runtime"
        renv.LAUNCH_SH = launch
        renv.HEALTH_INTERVAL = 1
        renv.HEALTH_RETRY_INTERVAL = 0.5
        renv.HEALTH_EVAL_TIMEOUT = 0.5
        renv.AGENT_SILENT_MINUTES = 2               # the fake claude idles silently; test_agent_hang sets 3 s
        renv.SIGINT_GRACE, renv.SIGTERM_GRACE, C.KILL_GRACE = 2, 2, 2
        rcode.steam_ready = lambda: (True, "test: offline")
        G.crash_reporter_pids = lambda: []   # never a real reporter (kill_game would kill it)
        C.LAUNCH_BACKOFF = (0.2, 0.2)
        renv.PLAYTHROUGHS = self.tmp / "playthroughs"     # profile.json
        G.press_menu_key = lambda log=None: "fake key"         # never the real input shim
        rcode.bg3le_problems = lambda: []                  # the real dist may be stale on a dev machine (UI-1)
        C.MENU_POLL = 0.05
        O.user_journal = lambda since: ""              # recover's OOM check: never the real journalctl (up to 20 s)
        renv.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"     # a hang's recover marks nav_guard*.json: never the live game's
        for name, fake in (("fill", lambda env: {}), ("problem", lambda env: None)):  # CI has no X server: the
            patch = mock.patch.object(lcli.display_env, name, fake)                       # stub game needs no display
            patch.start()
            self.addCleanup(patch.stop)

        self.claude = claude
        now = time.time()
        self.wire(rstate.Options("crash-test", None, 20, "opus", save_owner="Claude"),
                  [rpolicy.Save("Claude-1__S001-start", "Claude", "S001-start", now - 300, now - 300),
                   rpolicy.Save("Claude-2__S001-before-imps", "Claude", "S001-before-imps", now - 100, now - 100),
                   rpolicy.Save("Tav-3__my own game", "Tav", "my own game", now - 10, now - 10)])   # newer, not ours

    def wire(self, opt: rstate.Options, saves: list[rpolicy.Save]) -> None:
        r = lpt.Runner(opt)
        r.next_sid = lambda: "S001"
        r.close_memory_session = lambda res: None
        r.between_sessions_check = lambda: None   # tools/dev/check_all.py would probe the real port 9998
        r.check_plan_lines = lambda *a, **k: []   # so would tools/dev/check_plans.py (a uv run, up to 90 s, at
        #                                           session prep and end: the fake game passes its port/state checks)
        r.code_update_check = lambda: None        # other workers edit the tree meanwhile: no self-restart here
        r.mem = self.tmp / "memory"
        real_cmd = r.agent_cmd
        r.agent_cmd = lambda req: [sys.executable, str(self.claude)] + real_cmd(req)[1:]
        g = FakeGame(r, saves)
        G.port_open, G.game_state, G.lua_eval, G.rpc = g.port_open, g.game_state, g.lua_eval, g.rpc
        G.server_ready = lambda: g.port_open() and not g.hung and g.loaded() is not None
        G.available_ops = lambda ctx: set()
        G.host_character_name = lambda: "Claude"
        G.ui_screen = lambda: "game"
        G.read_progress = lambda log=None: None
        G.pids_of_game = lambda: []          # never a real bg3 pid; the stub dies with its process group
        G.all_bg3_pids = lambda: []          # nor a user's own BG3 (RUN-4: one would block every launch)
        rcode.list_saves = lambda: list(g.saves)
        self.r, self.g = r, g

    def tearDown(self):
        if self.g.alive():
            with contextlib.suppress(ProcessLookupError):   # it can exit between poll() and the kill
                os.killpg(self.r.game_proc.pid, signal.SIGKILL)
            self.r.game_proc.wait()
        for n, v in self.saved.items():
            setattr(home(n), n, v)

    def claude_runs(self) -> list[list[str]]:
        return [json.loads(ln)["argv"] for ln in self.claude_log.read_text().splitlines()]

    def claude_envs(self) -> list[dict]:
        return [json.loads(ln)["env"] for ln in self.claude_log.read_text().splitlines()]

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



class AgentFailures(FakeRunBase):
    """RUN-7 / RUN-8 through a real (fake) claude process."""

    def test_mcp_failure_stops_claude_at_once(self):
        # RUN-8: init says bg3 failed; the agent would play toolless to its deadline (the stop check keeps it going)
        os.environ["FAKE_CLAUDE_MCP"] = "failed"
        os.environ["FAKE_CLAUDE_FIRST_S"] = "60"
        t0 = time.time()
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "mcp_failed")
        self.assertIn("bg3 MCP server is failed", res.detail)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(len(self.claude_runs()), 1)                       # no resume, no stop-check bounce
        self.assertIn("mcp_failed", [e["kind"] for e in self.events()])

    def test_usage_limit_is_waited_out(self):
        # RUN-7: claude exits at once on a usage limit: api_limit with the reset time, not a quick failure
        os.environ["FAKE_CLAUDE_LIMIT"] = "1"
        os.environ["FAKE_CLAUDE_RESET"] = str(time.time() + 7200)
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "api_limit")
        self.assertIn("usage limit", res.detail)
        self.assertAlmostEqual(res.resets_at, float(os.environ["FAKE_CLAUDE_RESET"]), delta=1)
        self.assertEqual(len(self.claude_runs()), 1)                       # no stop-check resume into the limit

    def test_mod_gone_is_a_hang(self):
        # RUN-8: the game answers but Mods.BG3AI is nil: recover (relaunch + load) and resume, not a toolless session
        renv.MOD_GONE_S = 2
        renv.HEALTH_INTERVAL = 1

        def go():
            time.sleep(3)
            self.g.mod_gone_pid = self.r.game_proc.pid
        threading.Thread(target=go, daemon=True).start()
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "exited")
        faults = [e for e in self.events() if e["kind"] == "fault"]
        self.assertEqual([e["fault"] for e in faults], ["hang"])
        self.assertIn("BG3AI mod is gone", faults[0]["detail"])
        self.assertNotEqual(self.r.game_proc.pid, self.g.mod_gone_pid)    # a relaunched game


class CrashRecovery(FakeRunBase):
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
        log = (renv.RUNTIME / "runner.log").read_text()
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
        log = (renv.RUNTIME / "runner.log").read_text()
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
        self.assertIn("retrying in 0.2 s with Claude-1__S001-start", (renv.RUNTIME / "runner.log").read_text())

    def test_three_load_crashes_stop_the_runner_loudly(self):
        self.crash_after(3, load_crashes={"Claude-2__S001-before-imps": 9, "Claude-1__S001-start": 9})
        with self.assertRaises(rstate.LaunchGaveUp) as cm:
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
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("FAULT crash: debug port 9998 stopped answering (game process still alive)", log)
        self.assertNotEqual(self.r.game_proc.pid, self.g.port_lost_pid)       # the deaf game was killed
        self.assertTrue(self.g.port_open())
        self.assert_recovered("crash", 20)

    def test_hang_is_recovered_too(self):
        self.crash_after(3, hang=True)
        self.r.play_session(20, first=True)
        hung_pid = self.g.hung_pid
        self.assertEqual(len(self.claude_runs()), 2)
        self.assertIn("recovering: stopped responding", (renv.RUNTIME / "runner.log").read_text())
        self.assertNotEqual(self.r.game_proc.pid, hung_pid)                   # the hung game was replaced
        self.assertFalse(self.g.hung)
        self.assertIn("liveness evals in a row failed or timed out", (renv.RUNTIME / "runner.log").read_text())
        self.assert_recovered("hang", 25)

    def test_agent_hang_restarts_claude_only(self):
        os.environ["FAKE_CLAUDE_HANG"] = "1"
        renv.AGENT_SILENT_MINUTES = 3 / 60
        res = self.r.play_session(20, first=True)
        game_pid = self.r.game_proc.pid
        runs = self.claude_runs()
        self.assertEqual(len(runs), 2)
        prompt = runs[1][runs[1].index("-p") + 1]
        self.assertIn("produced no output", prompt)                           # agent_resume_prompt, not a crash note
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], runs[0][runs[0].index("--session-id") + 1])
        self.assertEqual(res.reason, "exited")
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("FAULT agent_hang: no claude output", log)
        self.assertNotIn("recovering:", log)                                  # the game was left alone
        self.assertEqual(self.r.game_proc.pid, game_pid)
        self.assertNotIn("killing the game", log)
        rec = self.assert_recovered("agent_hang", 15)                         # SIGINT, SIGTERM ignored → SIGKILL
        self.assertIn("game untouched", rec["how"])
        self.assertEqual(self.r.state["history"][-1]["resumes"], 1)

    def test_too_little_time_left_ends_the_session_and_keeps_the_note(self):
        renv.RESUME_MIN_MINUTES = 30                     # a 20-min session never has 30 left
        self.crash_after(3)
        self.r.play_session(20, first=True)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(self.r.state["history"][-1]["resumes"], 0)
        self.assertEqual(self.g.loaded(), "Claude-2__S001-before-imps")       # recovered for the next session
        self.assertTrue(any("RUNNER NOTE: the game crashed" in n for n in self.r.pending_notes))


class StopCheck(FakeRunBase):
    """claude ends its turn while the session can't end yet: the stop check (session.py stop, which was the Stop hook)
    resumes the same conversation with the reason, a bounded number of times."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_CLAUDE_FIRST_S"] = "1"
        os.environ["FAKE_CLAUDE_RESUME_S"] = "0.5"
        saved = renv.MAX_STOP_RESUMES
        self.addCleanup(setattr, testenv.home("MAX_STOP_RESUMES"), "MAX_STOP_RESUMES", saved)

    def test_keep_playing_resumes_the_same_conversation(self):
        reasons = ["Keep playing: 18 minutes left in this session.", None]
        calls = []

        def check(sid, deadline, minutes):
            calls.append((sid, minutes, deadline > time.time()))
            return reasons.pop(0) if reasons else None

        self.r.stop_check = check
        res = self.r.play_session(20, first=True)
        runs = self.claude_runs()
        self.assertEqual(len(runs), 2, runs)
        self.assertEqual(runs[1][runs[1].index("--resume") + 1], runs[0][runs[0].index("--session-id") + 1])
        self.assertIn("this session can't end yet. Keep playing: 18 minutes",
                      runs[1][runs[1].index("-p") + 1])
        self.assertEqual(calls, [("S001", 20, True)] * 2)   # the session's own deadline, not the resume's
        self.assertEqual(res.reason, "exited")
        h = self.r.state["history"][-1]
        self.assertEqual((h["stop_resumes"], h["keeper_model"]), (1, "opus"))
        self.assertTrue((self.r.state.dir / "S001-c1.log").exists())
        self.assertEqual([e["BG3AI_SESSION"] for e in self.claude_envs()], ["S001", "S001"])

    def test_a_fight_at_the_deadline_holds_the_hard_stop(self):
        # S078: a session never ends mid-fight. The bg3 MCP server writes the fight hold (Sxxx.hold.json); the runner
        # moves its hard stop and the session's end with it. A 3 s session with a 1 s hard-stop grace would SIGINT
        # the 10 s fake claude at ~4-6 s; held to +20 s, claude ends on its own and the stop check gets the held end.
        os.environ.update(FAKE_CLAUDE_FIRST_S="10", FAKE_CLAUDE_HOLD_S="20")
        calls = []
        self.r.stop_check = lambda sid, deadline, minutes: calls.append(deadline) and None
        with mock.patch.object(rpolicy, "deadlines", lambda start, m: (start + m * 60, start + m * 60 + 1)):
            t0 = time.time()
            res = self.r.play_session(0.05, first=True)
        self.assertEqual(res.reason, "exited")   # not hard_stop
        self.assertEqual(len(self.claude_runs()), 1)
        held = json.loads((self.r.state.dir / "S001.hold.json").read_text())["deadline"]
        self.assertEqual(calls, [held])
        self.assertGreater(held, t0 + 15)
        self.assertIn("[runner] fight hold (a fight at WRAP UP): session end", (self.r.state.dir / "S001.log").read_text())

    def test_the_fight_hold_is_bounded(self):
        hold = self.tmp / "S001.hold.json"
        self.assertEqual(rpolicy.held_deadline(hold, 1000.0), 1000.0)   # no file
        hold.write_text(json.dumps({"deadline": 1300, "held": True}))
        self.assertEqual(rpolicy.held_deadline(hold, 1000.0), 1300.0)
        hold.write_text(json.dumps({"deadline": 1000 + rpolicy.FIGHT_HOLD_S + 600}))
        self.assertEqual(rpolicy.held_deadline(hold, 1000.0), 1000.0 + rpolicy.FIGHT_HOLD_S)   # never past the bound
        for bad in ("", "[1]", "{\"deadline\": \"x\"}", json.dumps({"deadline": 900})):
            hold.write_text(bad)
            self.assertEqual(rpolicy.held_deadline(hold, 1000.0), 1000.0)   # never earlier

    def test_capped(self):
        renv.MAX_STOP_RESUMES = 2
        self.r.stop_check = lambda *a: "Keep playing: 10 minutes left in this session."
        self.r.play_session(20, first=True)
        self.assertEqual(len(self.claude_runs()), 3)
        self.assertEqual(self.r.state["history"][-1]["stop_resumes"], 2)


class NautiloidBase(FakeRunBase):
    """A progress feed: the Nautiloid, then its quest done once self.escaped is set."""

    def setUp(self):
        super().setUp()
        self.saved_story = (rchecks.story.read_game, rchecks.story.POLL_S)
        self.addCleanup(lambda: (setattr(rchecks.story, "read_game", self.saved_story[0]),
                                 setattr(rchecks.story, "POLL_S", self.saved_story[1])))
        rchecks.story.POLL_S = 0.3
        self.escaped = False

        def feed(_lua):   # the progress poller's journal/status read: the Nautiloid, then its quest done
            if not self.g.loaded():
                return None
            return {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                    "journal": {"quests": [{"title": "Escape the Nautiloid", "done": self.escaped,
                                            "steps": ["Get off the nautiloid"]}]}}
        rchecks.story.read_game = feed

    def quiet(self, r: lpt.Runner) -> None:
        r.check_memory = lambda: None
        r.code_update_check = lambda: None          # other workers edit the tree meanwhile: no self-restart here
        r.between_sessions_check = lambda: None     # no tools/dev/check_all.py, check_plans.py
        r.check_plan_lines = lambda *a, **k: []


class NewRuns(NautiloidBase):
    """RUN-2 (run --until stops at a milestone with a run record) and CHAR-1 (phase 0 from a character brief)."""

    def test_until_milestone_stops_the_run_with_a_record(self):
        r = self.r
        r.opt.until = "nautiloid_escaped"
        self.quiet(r)

        def escape():
            time.sleep(4)
            self.escaped = True
        threading.Thread(target=escape, daemon=True).start()
        t0 = time.time()
        self.assertEqual(r.run(), 0)
        self.assertLess(time.time() - t0, 40)                      # not the fake claude's 60 s
        self.assertEqual(len(self.claude_runs()), 1)                # no second session
        h = r.state["history"][-1]
        self.assertEqual((h["session"], h["reason"]), ("S001", "milestone"))
        recs = [json.loads(ln) for ln in (r.state.dir / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec["milestone"], rec["label"], rec["reached_in"]),
                         ("nautiloid_escaped", "Nautiloid escaped", "S001"))
        self.assertEqual((rec["sessions"], rec["crashes"], rec["recoveries"]), (1, 0, 0))
        self.assertEqual((rec["cost_usd"], rec["turns"], rec["tokens"]["output"]), (0.25, 7, 50))   # the [end] event
        self.assertGreater(rec["wall_min"], 0)
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("milestone nautiloid_escaped (Nautiloid escaped) reached: stopping (--until)", log)
        self.assertIn("runner stopped (--until nautiloid_escaped: reached in S001)", log)
        self.assertIn("[runner] milestone nautiloid_escaped reached; stopping claude",
                      (r.state.dir / "S001.log").read_text())
        # a second run of the same playthrough doesn't play past it
        r2 = lpt.Runner(rstate.Options("crash-test", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped"))
        r2.check_memory = lambda: None
        self.assertEqual(r2.run(), 0)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn("already reached in this playthrough", (renv.RUNTIME / "runner.log").read_text())

    def test_game_end_stops_a_playthrough_run_with_a_record(self):
        # playthrough mode (no --until): the end credits after the finale end the run; no relaunch, no next session
        r = self.r
        self.quiet(r)
        credits = {"on": False}

        def feed(_lua):   # the finale (END_Main = "Upper City"), then the end credits roll with no game session
            if not self.g.loaded():
                return None
            if credits["on"]:
                return {"status": None, "ui": {"screen": "credits", "page": "Credits"}}
            return {"status": {"region": "Upper City", "party": [{"name": "Claude", "level": 12}]},
                    "ui": {"screen": "game", "region": "High Hall"}}
        rchecks.story.read_game = feed

        def roll():
            time.sleep(4)
            credits["on"] = True
        threading.Thread(target=roll, daemon=True).start()
        t0 = time.time()
        self.assertEqual(r.run(), 0)
        self.assertLess(time.time() - t0, 40)                      # not the fake claude's 60 s
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertEqual(r.state["history"][-1]["reason"], "milestone")
        rec = json.loads((r.state.dir / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual((rec["outcome"], rec["milestone"], rec["reached_in"]), ("reached", "game_end", "S001"))
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("milestone game_end (The end (credits / epilogue)) reached: stopping (the game is finished)", log)
        self.assertIn("runner stopped (the game is finished: The end (credits / epilogue) in S001)", log)
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertIn({"milestone": "game_end", "terminal": True},
                      [{k: e[k] for k in ("milestone", "terminal") if k in e} for e in evs if e["kind"] == "milestone"])
        seen = json.loads((r.state.dir / "progress_state.json").read_text())
        self.assertEqual(seen["game_end"]["how"], "the end credits after the finale")
        # the next runner start has nothing to play
        r2 = lpt.Runner(rstate.Options("crash-test", None, 20, "opus", save_owner="Claude"))
        r2.check_memory = lambda: None
        self.assertEqual(r2.run(), 0)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn("the game is finished: already reached in this playthrough", (renv.RUNTIME / "runner.log").read_text())

    def test_crash_loop_stops_a_long_run(self):
        # playthrough mode, many hours unattended: a game that dies every session ends the run instead of relaunching
        r = self.r
        self.quiet(r)
        played = []

        def session(minutes, first):
            now = time.time()
            played.append(now)
            for _ in range(2):   # two crashes recovered inside each session
                r.event("fault", fault="crash", session="S%03d" % len(played), detail="test")
            return rstate.SessionResult("S%03d" % len(played), "c", 0, "exited", now - 300, now)
        r.play_session = session
        self.assertEqual(r.run(), 1)
        self.assertEqual(len(played), 3)   # 6 faults after the 3rd session: no 4th
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("crash loop: 6 game crashes/hangs/setup failures in", log)
        self.assertIn("runner stopped (gave up: crash loop", log)
        line = (renv.RUNTIME / renv.STATUS_LINE).read_text()
        self.assertIn("crash-test: STOPPED (gave up: crash loop", line)
        self.assertIn("6 game fault(s) in 60 min (stop at 6)", line)
        self.assertEqual(json.loads((renv.RUNTIME / "runner.status.json").read_text())["alert"]["kind"], "crash_loop")

    def test_new_playthrough_with_a_brief_runs_phase_0_and_adopts_the_created_name(self):
        brief = "a pious, short-tempered dwarf cleric who distrusts mind flayers"
        os.environ["FAKE_CLAUDE_FIRST_S"] = "4"
        self.wire(rstate.Options("brief-test", None, 20, "opus", character_brief=brief, roleplay="light",
                            difficulty="Balanced"), [])
        r = self.r
        self.quiet(r)   # a real check_all before the session took ~5 s: the "mid-session" save came before it
        prof = json.loads((renv.PLAYTHROUGHS / "brief-test" / "profile.json").read_text())
        self.assertEqual(prof, {"character": {"brief": brief, "roleplay": "light"}, "difficulty": "Balanced"})

        def first_save():   # the agent's create_character + first save, mid-session
            time.sleep(2)
            t = time.time()
            self.g.saves.append(rpolicy.Save("Mira-4810126690__S001-start", "Mira", "S001-start", t, t))
        threading.Thread(target=first_save, daemon=True).start()
        self.r.play_session(20, first=True)

        prompt = self.claude_runs()[0][self.claude_runs()[0].index("-p") + 1]
        self.assertIn("PHASE 0, CHARACTER CREATION", prompt)
        self.assertIn("knowledge/playbooks/character-creation.md", prompt)
        self.assertIn("> " + brief, prompt)
        self.assertIn("Roleplay: light", prompt)
        self.assertIn('new_game("Balanced")', prompt)
        self.assertIn("Name: yours to choose", prompt)
        self.assertNotIn("Claude", prompt.split("PHASE 0", 1)[1])                  # no forced name
        self.assertNotIn("Name your character exactly", prompt)
        self.assertEqual(self.claude_envs()[0], {"BG3AI_SAVE_OWNER": "", "BG3AI_SAVE_PREFIX": "",
                                                 "BG3AI_SAVE_GATES": "1", "BG3AI_SESSION": "S001"})   # new game: gates open
        # I3: the created name is the save owner from now on, persisted, and the prefix follows
        self.assertEqual(r.state["save_owner"], "Mira")
        self.assertEqual(rstate.RunnerState("brief-test")["save_owner"], "Mira")
        self.assertEqual((r.save_env()["BG3AI_SAVE_OWNER"], r.save_env()["BG3AI_SAVE_PREFIX"]), ("Mira", "Mira-"))
        prof = json.loads((renv.PLAYTHROUGHS / "brief-test" / "profile.json").read_text())
        self.assertEqual(prof["character"]["name"], "Mira")
        self.assertEqual(r.choose_save().folder, "Mira-4810126690__S001-start")

    def test_new_game_without_a_brief_keeps_todays_forced_name(self):
        os.environ["FAKE_CLAUDE_FIRST_S"] = "2"
        self.wire(rstate.Options("nobrief-test", None, 20, "opus", save_owner="Claude"), [])
        self.r.play_session(20, first=True)
        prompt = self.claude_runs()[0][self.claude_runs()[0].index("-p") + 1]
        self.assertIn('Name your character exactly "Claude"', prompt)
        self.assertNotIn("PHASE 0", prompt)
        self.assertEqual(self.claude_envs()[0]["BG3AI_SAVE_PREFIX"], "Claude-")      # RUN-5: from --save-owner alone


class Series(NautiloidBase):
    """run --until nautiloid_escaped --runs 2 end to end: two playthroughs, two new games, two records."""

    def setUp(self):
        super().setUp()
        for n in ("PLAY_DIR", "KNOWLEDGE_DIR", "SE_BG3AI_DIR", "current_pins"):   # setdefault: the base saved
            self.saved.setdefault(n, getattr(home(n), n))                     # SE_BG3AI_DIR before it set a tmp one
        pins = lser.current_pins()   # frozen: other workers editing the shared tree mid-test read as version drift
        lser.current_pins = lambda: json.loads(json.dumps(pins))
        renv.PLAY_DIR = self.tmp / "play"                  # check_memory links play/memory here, not in the repo
        renv.KNOWLEDGE_DIR = renv.PLAY_DIR / "knowledge"
        renv.KNOWLEDGE_DIR.mkdir(parents=True)
        (renv.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        renv.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        renv.SE_BG3AI_DIR.mkdir(parents=True)
        (renv.SE_BG3AI_DIR / "nav_guard.json").write_text('{"slow":{"TUT_Avernus_C":{}}}')
        os.environ["FAKE_CLAUDE_FIRST_S"] = "60"

        def feed(_lua):   # every game escapes the Nautiloid 3 s after it was launched (a new game included)
            if not self.g.alive():
                return None
            done = time.time() - (self.r.state["game"] or {}).get("launched", time.time()) > 3
            return {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                    "journal": {"quests": [{"title": "Escape the Nautiloid", "done": done,
                                            "steps": ["Get off the nautiloid"]}]}}
        rchecks.story.read_game = feed
        test, orig = self, lpt.Runner.__init__

        def init(r, opt):   # every Runner the series makes plays on the fake game with the fake claude
            orig(r, opt)
            r.next_sid = lambda: "S001"
            r.close_memory_session = lambda res: None
            real_cmd = r.agent_cmd
            r.agent_cmd = lambda req: [sys.executable, str(test.claude)] + real_cmd(req)[1:]
            r.code_update_check = lambda: None
            r.between_sessions_check = lambda: None
            r.check_plan_lines = lambda *a, **k: []
            test.g.r = test.r = r
        lpt.Runner.__init__ = init
        self.addCleanup(setattr, lpt.Runner, "__init__", orig)

    def test_two_runs_in_a_row(self):
        opt = rstate.Options("naut", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped", keeper=False,
                        character_brief="a dwarf cleric", series="naut")
        self.r.game_proc = None
        t0 = time.time()
        self.assertEqual(lser.run_series(opt, 2, {"runner": None, "stop": False}), 0)
        self.assertLess(time.time() - t0, 90)
        self.assertEqual(len(self.claude_runs()), 2)
        recs = [json.loads(ln) for ln in (renv.RUNTIME / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual([(r["run"], r["playthrough"], r["outcome"]) for r in recs],
                         [(1, "naut", "reached"), (2, "naut-r2", "reached")])
        self.assertTrue(all(r["clean"] for r in recs), [r["failures"] for r in recs])
        self.assertEqual(recs[0]["reset"]["nav_files"], ["nav_guard.json"])     # the kill list moved aside, once
        self.assertEqual(recs[1]["reset"]["nav_files"], [])
        for pt in ("naut", "naut-r2"):
            self.assertTrue((renv.PLAYTHROUGHS / pt / "memory" / "HANDOFF.md").is_file())
            prof = json.loads((renv.PLAYTHROUGHS / pt / "profile.json").read_text())
            self.assertEqual(prof["character"]["brief"], "a dwarf cleric")
        # run 2 started a new game: no --load of run 1's saves (same character name), and its own MCP save window
        launches = [e for e in self.claude_envs()]
        self.assertEqual(len(launches), 2)
        prompt2 = self.claude_runs()[1][self.claude_runs()[1].index("-p") + 1]
        self.assertIn("PHASE 0, CHARACTER CREATION", prompt2)
        self.assertNotIn("--load", self.g.r.game_proc.args if self.g.r.game_proc else [])
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("between runs: game stopped", log)
        self.assertIn("series naut: 2/2 run(s) recorded, 2 clean, 2 clean in a row", log)
        # re-running the finished series plays nothing
        self.assertEqual(lser.run_series(opt, 2, {"runner": None, "stop": False}), 0)
        self.assertEqual(len(self.claude_runs()), 2)


class StaleBg3le(FakeRunBase):
    """UI-1: a bg3le build without every patches/bg3le patch is never launched, and the runner stops (no retry)."""

    def test_stale_build_stops_the_runner_before_any_launch(self):
        rcode.bg3le_problems = lambda: ["missing 0009-noesis-uaf.patch"]
        r = self.r
        r.check_memory = lambda: None
        r.code_update_check = lambda: None
        self.assertEqual(r.run(), 1)
        self.assertIsNone(r.game_proc)                                         # nothing launched
        self.assertFalse(self.claude_log.exists())
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("REFUSING TO LAUNCH BG3: stale bg3le build (missing 0009-noesis-uaf.patch)", log)
        self.assertIn("tools/build_bg3le.sh", log)
        self.assertIn("runner stopped (stale bg3le build", log)
        self.assertNotIn("setup failed", log)
        st = json.loads((renv.RUNTIME / "runner.status.json").read_text())
        self.assertEqual((st["bg3le_stale"], st["alert"]["kind"]), (["missing 0009-noesis-uaf.patch"], "bg3le_stale"))
        self.assertIn("bg3le_stale", [e["kind"] for e in self.events()])

    def test_launch_scripts_exit_4_is_the_same_stop(self):
        stale = self.tmp / "launch_stale.sh"
        stale.write_text("#!/bin/sh\necho 'launch_game: REFUSING TO LAUNCH: STALE bg3le build'\nexit 4\n")
        stale.chmod(0o755)
        renv.LAUNCH_SH = stale
        r = self.r
        r.check_memory = lambda: None
        r.code_update_check = lambda: None
        self.assertEqual(r.run(), 1)
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("launch_game.sh exited 4", log)
        self.assertNotIn("launch 1/3", log)                                    # not retried as a crash
        self.assertEqual([e["kind"] for e in self.events()].count("bg3le_stale"), 1)


class Watch(unittest.TestCase):
    """LaunchWatch: each crash sign, read from fakes (no real processes or ports)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-watch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {n: getattr(home(n), n) for n in ("pids_of_game", "crash_reporter_pids", "port_open")}
        self.pids, self.reporters, self.port = [], [], False
        G.pids_of_game = lambda: list(self.pids)
        G.crash_reporter_pids = lambda: list(self.reporters)
        G.port_open = lambda *a, **k: self.port
        self.w = rgame.LaunchWatch("20261010-020143", self.tmp, started=100.0)

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(home(n), n, v)

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
        w = rgame.LaunchWatch("other", self.tmp, started=100.0)
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
        w = rgame.LaunchWatch("x", self.tmp, started=100.0)  # open, then closed: counted from the last open
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
        renv.RUNTIME = self.tmp / "runtime"
        r = lpt.Runner(rstate.Options("mem-test", None, 20, "opus"))
        r.memory_state = lambda: {"open": True, "current": 14}
        r.session_py = lambda *a: rcode.subprocess.CompletedProcess(a, 1, report, "")
        self.fix_runs = []
        r.run_claude = lambda *a, **k: self.fix_runs.append(a)
        return r

    def setUp(self):
        self.saved_rt = renv.RUNTIME

    def tearDown(self):
        renv.RUNTIME = self.saved_rt

    def res(self, reason):
        return rstate.SessionResult("S014", "c", 0, reason, time.time() - 400, time.time())

    def test_lint_error_lines(self):
        self.assertEqual(rcode.memory_lint_errors(self.S014, skip=("handoff",)),
                         ["ERROR  stale       objectives.md unchanged for 3 sessions"])
        self.assertEqual(len(rcode.memory_lint_errors(self.S014)), 2)

    def test_crash_with_only_the_handoff_error_leaves_no_note(self):
        r = self.runner("ERROR  handoff     HANDOFF.md was written by S013, not by the current session S014.\n")
        r.close_memory_session(self.res("crash"))
        self.assertEqual((r.pending_notes, self.fix_runs), ([], []))
        self.assertIn("memory left open for the next session", (renv.RUNTIME / "runner.log").read_text())
        self.assertNotIn("fix-notes session", (renv.RUNTIME / "runner.log").read_text())  # none runs after a crash

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


    def test_normal_end_fix_pass_runs_without_the_game(self):
        r = self.runner(self.S014)
        kw = []
        r.run_claude = lambda *a, **k: kw.append(k)
        r.close_memory_session(self.res("exited"))
        self.assertEqual(kw[0].get("needs_game"), False)   # S027-fix was killed by the game's OOM death

    def test_killed_session_is_treated_like_a_crash(self):
        # S027: an outside SIGTERM (exit 143) 3 min in was taken for a clean exit → fix-notes pass for the handoff
        self.assertEqual(renv.exit_reason("exited", 143), "killed")
        self.assertEqual(renv.exit_reason("exited", -15), "killed")
        self.assertEqual(renv.exit_reason("exited", 0), "exited")
        self.assertEqual(renv.exit_reason("hard_stop", 143), "hard_stop")   # the runner's own signals
        r = self.runner("ERROR  handoff     HANDOFF.md was written by S013, not by the current session S014.\n")
        r.close_memory_session(self.res("killed"))
        self.assertEqual((r.pending_notes, self.fix_runs), ([], []))
        self.assertIn("ended by claude killed", (renv.RUNTIME / "runner.log").read_text())

    def test_no_fix_pass_once_a_stop_was_requested(self):
        r = self.runner(self.S014)
        r.stop_requested = True
        r.close_memory_session(self.res("exited"))
        self.assertEqual(self.fix_runs, [])
        self.assertIn("written by S013", r.pending_notes[0])


class LiveSessionLength(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-len-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved_rt = renv.RUNTIME
        renv.RUNTIME = self.tmp / "runtime"

    def tearDown(self):
        renv.RUNTIME = self.saved_rt

    def test_minutes_from_session_config_each_session(self):
        r = lpt.Runner(rstate.Options("len-test", None, 20, "opus"))
        cfg = r.state.dir / "session-config.json"
        self.assertEqual(r.session_minutes(), 20)                          # no file: --session-minutes
        cfg.write_text(json.dumps({"minutes": 60, "effort": "low"}))
        self.assertEqual(r.session_minutes(), 60)
        self.assertIn("session length now 60 min", (renv.RUNTIME / "runner.log").read_text())
        cfg.write_text(json.dumps({"minutes": 5}))                          # too short without --allow-short-sessions
        self.assertEqual(r.session_minutes(), 20)
        cfg.write_text(json.dumps({"minutes": "60"}))                       # bad type: ignored
        self.assertEqual(r.session_minutes(), 20)
        cfg.write_text("{broken")
        self.assertEqual(r.session_minutes(), 20)
        r.opt.allow_short = True
        cfg.write_text(json.dumps({"minutes": 5}))
        self.assertEqual(r.session_minutes(), 5)
        self.assertEqual(json.loads((renv.RUNTIME / "runner.status.json").read_text())["session_minutes"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
