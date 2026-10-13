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
import shutil
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
    env = {k: os.environ.get(k) for k in ("BG3AI_SAVE_OWNER", "BG3AI_SAVE_PREFIX", "BG3AI_SAVE_GATES", "BG3AI_SESSION")}
    f.write(json.dumps({"argv": sys.argv[1:], "t": time.time(), "env": env}) + "\n")
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
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "num_turns": 7, "total_cost_usd": 0.25,
                      "usage": {"input_tokens": 100, "output_tokens": 50}}), flush=True)
    sys.exit(130)
print(json.dumps({"type": "result", "subtype": "success", "result": "done", "num_turns": 3, "total_cost_usd": 0.5,
                  "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
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
                 "pids_of_game", "list_saves", "RESUME_MIN_MINUTES", "steam_ready", "crash_reporter_pids",
                 "LAUNCH_BACKOFF", "PLAYTHROUGHS", "press_menu_key", "MENU_POLL", "bg3le_problems")
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
        P.PLAYTHROUGHS = self.tmp / "playthroughs"     # profile.json
        P.press_menu_key = lambda: "fake key"          # never the real input shim
        P.bg3le_problems = lambda: []                  # the real dist may be stale on a dev machine (UI-1)
        P.MENU_POLL = 0.05

        self.claude = claude
        now = time.time()
        self.wire(P.Options("crash-test", None, 20, "opus", save_owner="Claude"),
                  [P.Save("Claude-1__S001-start", "Claude", "S001-start", now - 300),
                   P.Save("Claude-2__S001-before-imps", "Claude", "S001-before-imps", now - 100),
                   P.Save("Tav-3__my own game", "Tav", "my own game", now - 10)])   # newer, not ours

    def wire(self, opt: P.Options, saves: list[P.Save]) -> None:
        r = P.Runner(opt)
        r.next_sid = lambda: "S001"
        r.close_memory_session = lambda res: None
        r.between_sessions_check = lambda: None   # tools/dev/check_all.py would probe the real port 9998
        r.mem = self.tmp / "memory"
        real_cmd = r.claude_cmd
        r.claude_cmd = lambda *a, **k: [sys.executable, str(self.claude)] + real_cmd(*a, **k)[1:]
        g = FakeGame(r, saves)
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


class StopCheck(FakeRunBase):
    """claude ends its turn while the session can't end yet: the stop check (session.py stop, which was the Stop hook)
    resumes the same conversation with the reason, a bounded number of times."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_CLAUDE_FIRST_S"] = "1"
        os.environ["FAKE_CLAUDE_RESUME_S"] = "0.5"
        saved = P.MAX_STOP_RESUMES
        self.addCleanup(setattr, P, "MAX_STOP_RESUMES", saved)

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

    def test_capped(self):
        P.MAX_STOP_RESUMES = 2
        self.r.stop_check = lambda *a: "Keep playing: 10 minutes left in this session."
        self.r.play_session(20, first=True)
        self.assertEqual(len(self.claude_runs()), 3)
        self.assertEqual(self.r.state["history"][-1]["stop_resumes"], 2)


class NautiloidBase(FakeRunBase):
    """A progress feed: the Nautiloid, then its quest done once self.escaped is set."""

    def setUp(self):
        super().setUp()
        self.saved_story = (P.story.read_game, P.story.POLL_S)
        self.addCleanup(lambda: (setattr(P.story, "read_game", self.saved_story[0]),
                                 setattr(P.story, "POLL_S", self.saved_story[1])))
        P.story.POLL_S = 0.3
        self.escaped = False

        def feed(_lua):   # the progress poller's journal/status read: the Nautiloid, then its quest done
            if not self.g.loaded():
                return None
            return {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                    "journal": {"quests": [{"title": "Escape the Nautiloid", "done": self.escaped,
                                            "steps": ["Get off the nautiloid"]}]}}
        P.story.read_game = feed

    def quiet(self, r: P.Runner) -> None:
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
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("milestone nautiloid_escaped (Nautiloid escaped) reached: stopping (--until)", log)
        self.assertIn("runner stopped (--until nautiloid_escaped: reached in S001)", log)
        self.assertIn("[runner] milestone nautiloid_escaped reached; stopping claude",
                      (r.state.dir / "S001.log").read_text())
        # a second run of the same playthrough doesn't play past it
        r2 = P.Runner(P.Options("crash-test", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped"))
        r2.check_memory = lambda: None
        self.assertEqual(r2.run(), 0)
        self.assertEqual(len(self.claude_runs()), 1)
        self.assertIn("already reached in this playthrough", (P.RUNTIME / "runner.log").read_text())

    def test_new_playthrough_with_a_brief_runs_phase_0_and_adopts_the_created_name(self):
        brief = "a pious, short-tempered dwarf cleric who distrusts mind flayers"
        os.environ["FAKE_CLAUDE_FIRST_S"] = "4"
        self.wire(P.Options("brief-test", None, 20, "opus", character_brief=brief, roleplay="light",
                            difficulty="Balanced"), [])
        r = self.r
        self.quiet(r)   # a real check_all before the session took ~5 s: the "mid-session" save came before it
        prof = json.loads((P.PLAYTHROUGHS / "brief-test" / "profile.json").read_text())
        self.assertEqual(prof, {"character": {"brief": brief, "roleplay": "light"}, "difficulty": "Balanced"})

        def first_save():   # the agent's create_character + first save, mid-session
            time.sleep(2)
            self.g.saves.append(P.Save("Mira-4810126690__S001-start", "Mira", "S001-start", time.time()))
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
        self.assertEqual(P.RunnerState("brief-test")["save_owner"], "Mira")
        self.assertEqual((r.save_env()["BG3AI_SAVE_OWNER"], r.save_env()["BG3AI_SAVE_PREFIX"]), ("Mira", "Mira-"))
        prof = json.loads((P.PLAYTHROUGHS / "brief-test" / "profile.json").read_text())
        self.assertEqual(prof["character"]["name"], "Mira")
        self.assertEqual(r.choose_save().folder, "Mira-4810126690__S001-start")

    def test_new_game_without_a_brief_keeps_todays_forced_name(self):
        os.environ["FAKE_CLAUDE_FIRST_S"] = "2"
        self.wire(P.Options("nobrief-test", None, 20, "opus", save_owner="Claude"), [])
        self.r.play_session(20, first=True)
        prompt = self.claude_runs()[0][self.claude_runs()[0].index("-p") + 1]
        self.assertIn('Name your character exactly "Claude"', prompt)
        self.assertNotIn("PHASE 0", prompt)
        self.assertEqual(self.claude_envs()[0]["BG3AI_SAVE_PREFIX"], "Claude-")      # RUN-5: from --save-owner alone


class Series(NautiloidBase):
    """run --until nautiloid_escaped --runs 2 end to end: two playthroughs, two new games, two records."""

    def setUp(self):
        super().setUp()
        for n in ("PLAY_DIR", "KNOWLEDGE_DIR", "SE_BG3AI_DIR"):
            self.saved[n] = getattr(P, n)
        P.PLAY_DIR = self.tmp / "play"                  # check_memory links play/memory here, not in the repo
        P.KNOWLEDGE_DIR = P.PLAY_DIR / "knowledge"
        P.KNOWLEDGE_DIR.mkdir(parents=True)
        (P.KNOWLEDGE_DIR / "builds.md").write_text("builds\n")
        P.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        P.SE_BG3AI_DIR.mkdir(parents=True)
        (P.SE_BG3AI_DIR / "nav_guard.json").write_text('{"slow":{"TUT_Avernus_C":{}}}')
        os.environ["FAKE_CLAUDE_FIRST_S"] = "60"

        def feed(_lua):   # every game escapes the Nautiloid 3 s after it was launched (a new game included)
            if not self.g.alive():
                return None
            done = time.time() - (self.r.state["game"] or {}).get("launched", time.time()) > 3
            return {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                    "journal": {"quests": [{"title": "Escape the Nautiloid", "done": done,
                                            "steps": ["Get off the nautiloid"]}]}}
        P.story.read_game = feed
        test, orig = self, P.Runner.__init__

        def init(r, opt):   # every Runner the series makes plays on the fake game with the fake claude
            orig(r, opt)
            r.next_sid = lambda: "S001"
            r.close_memory_session = lambda res: None
            real_cmd = r.claude_cmd
            r.claude_cmd = lambda *a, **k: [sys.executable, str(test.claude)] + real_cmd(*a, **k)[1:]
            r.code_update_check = lambda: None
            r.between_sessions_check = lambda: None
            r.check_plan_lines = lambda *a, **k: []
            test.g.r = test.r = r
        P.Runner.__init__ = init
        self.addCleanup(setattr, P.Runner, "__init__", orig)

    def test_two_runs_in_a_row(self):
        opt = P.Options("naut", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped", keeper=False,
                        character_brief="a dwarf cleric", series="naut")
        self.r.game_proc = None
        t0 = time.time()
        self.assertEqual(P.run_series(opt, 2, {"runner": None, "stop": False}), 0)
        self.assertLess(time.time() - t0, 90)
        self.assertEqual(len(self.claude_runs()), 2)
        recs = [json.loads(ln) for ln in (P.RUNTIME / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual([(r["run"], r["playthrough"], r["outcome"]) for r in recs],
                         [(1, "naut", "reached"), (2, "naut-r2", "reached")])
        self.assertTrue(all(r["clean"] for r in recs), [r["failures"] for r in recs])
        self.assertEqual(recs[0]["reset"]["nav_files"], ["nav_guard.json"])     # the kill list moved aside, once
        self.assertEqual(recs[1]["reset"]["nav_files"], [])
        for pt in ("naut", "naut-r2"):
            self.assertTrue((P.PLAYTHROUGHS / pt / "memory" / "HANDOFF.md").is_file())
            prof = json.loads((P.PLAYTHROUGHS / pt / "profile.json").read_text())
            self.assertEqual(prof["character"]["brief"], "a dwarf cleric")
        # run 2 started a new game: no --load of run 1's saves (same character name), and its own MCP save window
        launches = [e for e in self.claude_envs()]
        self.assertEqual(len(launches), 2)
        prompt2 = self.claude_runs()[1][self.claude_runs()[1].index("-p") + 1]
        self.assertIn("PHASE 0, CHARACTER CREATION", prompt2)
        self.assertNotIn("--load", self.g.r.game_proc.args if self.g.r.game_proc else [])
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("between runs: game stopped", log)
        self.assertIn("series naut: 2/2 run(s) recorded, 2 clean, 2 clean in a row", log)
        # re-running the finished series plays nothing
        self.assertEqual(P.run_series(opt, 2, {"runner": None, "stop": False}), 0)
        self.assertEqual(len(self.claude_runs()), 2)


class StaleBg3le(FakeRunBase):
    """UI-1: a bg3le build without every patches/bg3le patch is never launched, and the runner stops (no retry)."""

    def test_stale_build_stops_the_runner_before_any_launch(self):
        P.bg3le_problems = lambda: ["missing 0009-noesis-uaf.patch"]
        r = self.r
        r.check_memory = lambda: None
        r.code_update_check = lambda: None
        self.assertEqual(r.run(), 1)
        self.assertIsNone(r.game_proc)                                         # nothing launched
        self.assertFalse(self.claude_log.exists())
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("REFUSING TO LAUNCH BG3: stale bg3le build (missing 0009-noesis-uaf.patch)", log)
        self.assertIn("tools/build_bg3le.sh", log)
        self.assertIn("runner stopped (stale bg3le build", log)
        self.assertNotIn("setup failed", log)
        st = json.loads((P.RUNTIME / "runner.status.json").read_text())
        self.assertEqual((st["bg3le_stale"], st["alert"]["kind"]), (["missing 0009-noesis-uaf.patch"], "bg3le_stale"))
        self.assertIn("bg3le_stale", [e["kind"] for e in self.events()])

    def test_launch_scripts_exit_4_is_the_same_stop(self):
        stale = self.tmp / "launch_stale.sh"
        stale.write_text("#!/bin/sh\necho 'launch_game: REFUSING TO LAUNCH: STALE bg3le build'\nexit 4\n")
        stale.chmod(0o755)
        P.LAUNCH_SH = stale
        r = self.r
        r.check_memory = lambda: None
        r.code_update_check = lambda: None
        self.assertEqual(r.run(), 1)
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("launch_game.sh exited 4", log)
        self.assertNotIn("launch 1/3", log)                                    # not retried as a crash
        self.assertEqual([e["kind"] for e in self.events()].count("bg3le_stale"), 1)


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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
        self.assertIn("runner stopped (gave up: the game crashes on every launch)",
                      (P.RUNTIME / "runner.log").read_text())

    def test_mod_refusals_are_setup_failures_not_the_runners_death(self):
        # RUN-3: an RpcError / LuaDebugError from the session setup (a refused ui.menu load, a failed eval) used to
        # miss the run loop's except and kill the unattended runner. Now: setup failure → recover → next session.
        P.MAX_SETUP_FAILURES = 3
        r = P.Runner(P.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        recovered, calls = [], []
        r.recover = lambda why: recovered.append(why)

        def play(minutes, first):
            calls.append(first)
            if len(calls) == 1:
                raise P.RpcError("ui.menu: loading is disabled (bg3ai/config.json)")
            if len(calls) == 2:
                raise P.LuaDebugError("EVAL_ENGINE_NOT_READY")
            r.stop_requested = True
            return P.SessionResult("S001", "c", 0, "exited", time.time() - 600, time.time())
        r.play_session = play
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(recovered), 2)
        self.assertIn("loading is disabled", recovered[0])
        kinds = [json.loads(ln)["kind"] for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(kinds.count("setup_failed"), 2)
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("session setup failed: RpcError: ui.menu: loading is disabled", log)
        self.assertIn("runner stopped (stopped)", log)

    def test_any_other_death_is_logged_with_its_traceback(self):
        r = P.Runner(P.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None

        def play(minutes, first):
            raise KeyError("boom")
        r.play_session = play
        with self.assertRaises(KeyError):
            r.run()
        log = (P.RUNTIME / "runner.log").read_text()
        self.assertIn("runner died:\nTraceback", log)
        self.assertIn("runner stopped (died: KeyError: 'boom')", log)


class FakeMenuGame:
    """A game at the main menu's splash ("Press any key"): the client state flips LoadMenu/Menu (or stays LoadMenu),
    ui.state shows SplashScreen as a panel, ui.menu fails until a key gets past it; a load takes 3 polls."""

    def __init__(self, splash: bool = True, flip: bool = True, load_starts: bool = True):
        self.splash, self.flip, self.load_starts = splash, flip, load_starts
        self.polls, self.presses, self.loads, self.loading, self.running = 0, 0, [], 0, False
        self.load_gate = True   # the mod's persisted load gate (ui.config load_enabled)
        self.refuse = None      # ui.menu load raises this RpcError text

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
        if op == "ui.config" and "load_enabled" in (args or {}):
            self.load_gate = args["load_enabled"]
        if op == "ui.menu":
            if self.splash:
                raise P.RpcError("ui.menu: not at the main menu")
            if self.refuse:
                raise P.RpcError(self.refuse)
            if not self.load_gate:
                raise P.RpcError("ui.menu: loading is disabled")
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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

    def test_load_gates_open_before_the_menu_load(self):
        # RUN-3: the mod refuses ui.menu load while its persisted load gate is off (a fresh profile's default)
        g = FakeMenuGame(splash=False)
        g.load_gate = False
        _r, label, _secs, _log = self.run_ensure(g)
        self.assertEqual((label, len(g.loads)), ("S014-start", 1))
        self.assertTrue(g.load_gate)

    def test_a_refused_menu_load_relaunches_the_game_with_the_save(self):
        # RUN-3: any refusal (gate, playthrough not on the page, eval failure) → kill + launch_and_load
        # (-loadSaveGame needs no gate), not a dead runner.
        g = FakeMenuGame(splash=False)
        g.refuse = "ui.menu: loading is disabled (bg3ai/config.json load_enabled=false)"
        relaunched = []
        real_runner = P.Runner

        class R(real_runner):
            def kill_game(self):
                relaunched.append("kill")

            def launch_and_load(self, save):
                relaunched.append(save.folder)
                return save
        P.Runner = R
        try:
            r, label, _secs, log = self.run_ensure(g)
        finally:
            P.Runner = real_runner
        self.assertEqual(label, "S014-start")
        self.assertEqual(relaunched, ["kill", "Claude-1__S014-start"])
        self.assertEqual(r.state["loaded_save"], "Claude-1__S014-start")
        self.assertIn("refused (ui.menu: loading is disabled", log)
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(ev[-1]["kind"], "load_refused")

    def test_a_load_that_never_starts_fails_fast_instead_of_waiting_for_running(self):
        g = FakeMenuGame(load_starts=False)
        t0 = time.time()
        with self.assertRaisesRegex(RuntimeError, "never started"):
            self.run_ensure(g)
        self.assertLess(time.time() - t0, 5)
        self.assertNotIn("waiting for game state Running", (P.RUNTIME / "runner.log").read_text())


class RestartDrift(unittest.TestCase):
    """S059 08:45 / S063 10:51: a runner restart (test cycle) kept the running game, but acceptance had loaded its own
    saves in between; claude started on an Act-2 fixture. Now a restart compares the game with the last session's
    end_sig and loads the newest save when it moved; an unchanged game is kept (no reload)."""

    END = {"level": "BGO_Main_A", "region": "R", "party_levels": ["9"], "pos": [15.3, 46.4, 224.3],
           "journal": "921b6e07b9fa", "journal_steps": 413}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-drift-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        names = SplashMenu.NAMES + ("read_progress",)
        self.saved = {n: getattr(P, n) for n in names}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME = self.tmp / "runtime"
        P.MENU_POLL, P.SPLASH_LOADMENU_S, P.LOAD_START_TIMEOUT = 0.01, 0.2, 1

    def run_restart(self, now_sig: dict | None, take_over: bool = True, owned: bool = True):
        g = FakeMenuGame(splash=False)
        g.running = True
        orig_rpc = g.rpc

        def rpc(op, args=None, context="server", timeout=0):
            if op == "ui.menu":
                g.running = False   # an in-game load: Running → loading → Running
            return orig_rpc(op, args, context, timeout)
        P.port_open = lambda *a, **k: True
        P.game_state, P.rpc, P.press_menu_key = g.game_state, rpc, g.press
        P.available_ops = lambda ctx: {"ui.state", "ui.menu"}
        P.server_ready = lambda: g.running
        P.host_character_name = lambda: "Claude"
        P.read_progress = lambda: now_sig
        P.list_saves = lambda: [P.Save("Claude-1__S062-end", "Claude", "S062-end", time.time() - 60)]
        r = P.Runner(P.Options("drift-test", None, 20, "opus", save_owner="Claude", take_over=take_over))
        r.mem = self.tmp / "memory"
        r.owns_game = lambda: owned
        r.state["loaded_save"] = "Claude-1__S062-end" if owned else None
        r.state["history"] = [{"kind": "play", "session": "S062", "ended": time.time() - 600, "end_sig": self.END}]
        label = r.ensure_game(first=True)
        logf = P.RUNTIME / "runner.log"
        return g, label, logf.read_text() if logf.exists() else ""

    def test_fixture_loaded_in_between_reloads_the_newest_save(self):
        act2 = dict(self.END, level="SCL_Main_A", journal="79e1e111a061", journal_steps=219, pos=[635.3, 18.8, -1499.8])
        g, label, log = self.run_restart(act2)
        self.assertEqual((label, len(g.loads)), ("S062-end", 1))
        self.assertIn("the running game moved since the last session (level SCL_Main_A", log)

    def test_take_over_of_a_foreign_game_also_reloads(self):
        g, label, _log = self.run_restart(dict(self.END, pos=[115.3, 46.4, 224.3]), owned=False)
        self.assertEqual((label, len(g.loads)), ("S062-end", 1))

    def test_same_place_is_kept(self):
        g, label, log = self.run_restart(dict(self.END, pos=[16.0, 46.4, 226.0], party_levels=["1", "9"]))
        self.assertEqual((label, len(g.loads)), (None, 0))
        self.assertNotIn("moved since", log)

    def test_unknown_state_is_kept(self):
        g, label, _log = self.run_restart(None)
        self.assertEqual((label, len(g.loads)), (None, 0))

    def test_state_drift(self):
        self.assertIsNone(P.state_drift(None, self.END))
        self.assertIn("journal_steps 414", P.state_drift(self.END, dict(self.END, journal_steps=414)))
        self.assertIn("40 m from", P.state_drift(self.END, dict(self.END, pos=[55.3, 46.4, 224.3])))


class Watch(unittest.TestCase):
    """LaunchWatch: each crash sign, read from fakes (no real processes or ports)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-watch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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


    def test_normal_end_fix_pass_runs_without_the_game(self):
        r = self.runner(self.S014)
        kw = []
        r.run_claude = lambda *a, **k: kw.append(k)
        r.close_memory_session(self.res("exited"))
        self.assertEqual(kw[0].get("needs_game"), False)   # S027-fix was killed by the game's OOM death

    def test_killed_session_is_treated_like_a_crash(self):
        # S027: an outside SIGTERM (exit 143) 3 min in was taken for a clean exit → fix-notes pass for the handoff
        self.assertEqual(P.exit_reason("exited", 143), "killed")
        self.assertEqual(P.exit_reason("exited", -15), "killed")
        self.assertEqual(P.exit_reason("exited", 0), "exited")
        self.assertEqual(P.exit_reason("hard_stop", 143), "hard_stop")   # the runner's own signals
        r = self.runner("ERROR  handoff     HANDOFF.md was written by S013, not by the current session S014.\n")
        r.close_memory_session(self.res("killed"))
        self.assertEqual((r.pending_notes, self.fix_runs), ([], []))
        self.assertIn("ended by claude killed", (P.RUNTIME / "runner.log").read_text())

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
