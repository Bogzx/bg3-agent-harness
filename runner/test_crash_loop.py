#!/usr/bin/env python3
"""runner/play.py's run loop without a game (moved from test_crash.py): the worst-case detection budget, the
Steam-offline guard before any launch, and Runner.run's setup failures, restarts and session lengths.

    python3 -m unittest runner/test_crash_loop.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # first: the game's profile folders are a temp dir before play

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import legacy_playthrough as lpt
from bg3harness.game import controller as C  # noqa: N812  (the GameController and its timings)
from bg3harness.game import io as G  # noqa: N812
from bg3harness.game import oom as O  # noqa: N812  (the OOM check's journal and ramwatch reads)
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import game_side as rgame
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate

G_FUNCS = {
    n
    for n, v in vars(G).items()
    if callable(v) and getattr(v, "__module__", None) == G.__name__ and not isinstance(v, type)
}


CTL_NAMES = ("KILL_GRACE", "KILL_CONFIRM", "LAUNCH_BACKOFF", "MENU_POLL", "SPLASH_LOADMENU_S")
MOVED: dict[str, ModuleType] = {**dict.fromkeys(CTL_NAMES, C), **dict.fromkeys(("user_journal", "read_spikes"), O)}


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io, the GameController's
    timings to bg3harness.game.controller and the OOM check's reads to bg3harness.game.oom."""
    return MOVED.get(name) or (G if name in G_FUNCS else testenv.home(name))


class DetectionBudget(unittest.TestCase):
    """With the real constants, every fault is detected and claude stopped within 3 min (the agent hang after its
    silence limit). Then kill_game (≤ KILL_GRACE + KILL_CONFIRM) and BG3's own relaunch + load follow."""

    def test_real_constants(self):
        for kind in ("crash", "hang"):
            self.assertLessEqual(rpolicy.detect_budget_s(kind), 180, kind)
        self.assertLessEqual(rpolicy.detect_budget_s("agent_hang") - renv.AGENT_SILENT_MINUTES * 60, 180)
        self.assertLessEqual(renv.AGENT_SILENT_MINUTES, 5)
        self.assertLessEqual(C.KILL_GRACE + C.KILL_CONFIRM, 60)


class SteamGuard(unittest.TestCase):
    """No game launch unless Steam's last login was offline (and Steam runs)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-steam-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "steamui_login.txt"
        os.environ["BG3AI_STEAM_LOGIN_LOG"] = str(self.log)
        self.env_saved = {
            k: os.environ.pop(k, None)
            for k in ("BG3AI_ALLOW_STEAM_ONLINE", "BG3AI_REQUIRE_STEAM_OFFLINE", "BG3AI_CONFIG_DIR")
        }
        os.environ["BG3AI_CONFIG_DIR"] = str(self.tmp / "config")  # never this machine's real marker
        self.marker = self.tmp / "config" / "require-steam-offline"
        self.saved = {n: getattr(home(n), n) for n in ("RUNTIME", "port_open", "LAUNCH_SH")}
        renv.RUNTIME = self.tmp / "runtime"
        G.port_open = lambda *a, **k: False
        renv.LAUNCH_SH = self.tmp / "must-not-run.sh"

    def tearDown(self):
        os.environ.pop("BG3AI_STEAM_LOGIN_LOG", None)
        for k, v in self.env_saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        for n, v in self.saved.items():
            setattr(home(n), n, v)

    def require_offline(self, why: str = "this machine shares the Steam account") -> None:
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(why + "\n")

    def steam_ready(self, steam_running: bool = True) -> tuple[bool, str]:
        real = rcode.subprocess.run
        rcode.subprocess.run = lambda *a, **k: rcode.subprocess.CompletedProcess(
            a, 0 if steam_running else 1, "123\n" if steam_running else "", ""
        )
        try:
            return rcode.steam_ready()
        finally:
            rcode.subprocess.run = real

    def write(self, *lines: str) -> None:
        self.log.write_text("\n".join(f"[2026-10-09 21:00:0{i}] [ None ] {ln}" for i, ln in enumerate(lines)) + "\n")

    def test_parser(self):
        sp = renv.steam_paths
        self.write("Starting login", "SetLoginState: Success - OK", "Start offline - 1", "SetLoginState: Success - OK")
        self.assertTrue(sp.steam_offline()[0])
        self.write("Start offline - 1", "Starting login", "Received logon success response")  # went online
        ok, why = sp.steam_offline()
        self.assertFalse(ok)
        self.assertIn("online", why)
        self.write("Start offline - 0")
        self.assertFalse(sp.steam_offline()[0])
        self.log.unlink()
        self.assertFalse(sp.steam_offline()[0])

    def test_launch_refused_loudly_when_online(self):
        self.require_offline()
        self.write("Starting login", "Received logon success response")
        r = lpt.Runner(rstate.Options("steam-test", None, 20, "opus", save_owner="Claude"))
        with self.assertRaises(lpt.SteamNotOffline):
            r.launch_game("Claude-1__x")
        self.assertIsNone(r.game_proc)  # nothing launched
        self.assertIn("REFUSING TO LAUNCH BG3", (renv.RUNTIME / "runner.log").read_text())
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(ev[-1]["kind"], "steam_refused")
        self.assertFalse(json.loads((renv.RUNTIME / "runner.status.json").read_text())["steam_offline"])

    def test_steam_not_running_is_refused_too(self):
        self.write("Start offline - 1")
        for required in (False, True):
            if required:
                self.require_offline()
            ok, why = self.steam_ready(steam_running=False)
            self.assertFalse(ok)
            self.assertIn("isn't running", why)

    def test_online_is_fine_by_default(self):
        # RUN-11: a public user's machine has no marker and no env: an online Steam launches (it used to retry forever)
        self.write("Starting login", "Received logon success response")
        ok, why = self.steam_ready()
        self.assertTrue(ok, why)
        self.assertIn("not required", why)

    def test_marker_requires_offline(self):
        # ~/.config/bg3ai/require-steam-offline keeps the requirement on, its text in the reason
        self.require_offline("box-b: shares the account with box-a")
        self.write("Starting login")
        ok, why = self.steam_ready()
        self.assertFalse(ok)
        self.assertIn("box-b: shares the account", why)
        self.write("Start offline - 1")
        self.assertTrue(self.steam_ready()[0])

    def test_env_overrides(self):
        self.write("Starting login")
        os.environ["BG3AI_REQUIRE_STEAM_OFFLINE"] = "1"  # no marker: the env turns it on
        self.assertFalse(self.steam_ready()[0])
        self.require_offline()
        os.environ["BG3AI_REQUIRE_STEAM_OFFLINE"] = "0"  # and off, over the marker
        self.assertTrue(self.steam_ready()[0])
        os.environ.pop("BG3AI_REQUIRE_STEAM_OFFLINE")
        self.assertFalse(self.steam_ready()[0])
        os.environ["BG3AI_ALLOW_STEAM_ONLINE"] = "1"  # the older override still turns it off
        self.assertTrue(self.steam_ready()[0])

    def test_launcher_agrees(self):
        # launch_game.sh asks `steam_paths.py launch-ok` (exit 0 launch / 3 refuse): the same policy as steam_ready
        sp = Path(renv.REPO) / "tools" / "steam_paths.py"

        def launch_ok() -> int:
            return rcode.subprocess.run(
                [sys.executable, str(sp), "launch-ok"], capture_output=True, text=True, env=dict(os.environ)
            ).returncode

        self.write("Starting login")
        self.assertEqual(launch_ok(), 0)
        self.require_offline()
        self.assertEqual(launch_ok(), 3)
        self.write("Start offline - 1")
        self.assertEqual(launch_ok(), 0)
        self.assertIn('steam_paths.py" launch-ok', (Path(renv.REPO) / "runner" / "launch_game.sh").read_text())


class RunLoop(unittest.TestCase):
    """Runner.run(): a Steam refusal never counts toward giving up; start/stop land in runner_events.jsonl."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-loop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {n: getattr(home(n), n) for n in ("RUNTIME", "STEAM_RETRY_S", "MAX_SETUP_FAILURES", "HANDOVER")}
        renv.RUNTIME = self.tmp / "runtime"
        # bound at import: a restart's handover stays in this test
        rcode.HANDOVER = renv.RUNTIME / "runner-handover.json"
        renv.STEAM_RETRY_S = 0
        renv.MAX_SETUP_FAILURES = 2
        # other workers edit the tree meanwhile: a changed module would raise RestartRunner out of run()
        patcher = mock.patch.object(lpt.Runner, "code_update_check", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(home(n), n, v)

    def test_steam_refusals_retry_forever_then_play(self):
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        calls = []

        def play(minutes, first):
            calls.append(minutes)
            if len(calls) <= 5:  # more than MAX_SETUP_FAILURES refusals
                raise lpt.SteamNotOffline("last Steam login was online")
            r.stop_requested = True
            return rstate.SessionResult("S001", "c", 0, "exited", time.time() - 600, time.time())

        r.play_session = play
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 6)
        kinds = [json.loads(ln)["kind"] for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual((kinds[0], kinds[-1]), ("runner_start", "runner_stop"))
        self.assertNotIn("setup_failed", kinds)
        self.assertIn("Steam not offline", (renv.RUNTIME / "runner.log").read_text())

    def test_launch_gave_up_stops_the_runner_with_an_alert(self):
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None

        def play(minutes, first):
            raise rstate.LaunchGaveUp("3 launches in a row died before the save played: x")

        r.play_session = play
        self.assertEqual(r.run(), 1)
        evs = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([e["kind"] for e in evs][-2:], ["launch_gave_up", "runner_stop"])
        self.assertEqual(evs[-1]["why"], "gave up: the game crashes on every launch")
        alert = json.loads((renv.RUNTIME / "runner.status.json").read_text())["alert"]
        self.assertEqual(alert["kind"], "launch_gave_up")
        self.assertIn("GAME CRASH LOOP", (renv.RUNTIME / "runner.log").read_text())
        r2 = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))  # the next run clears the alert
        r2.check_memory = lambda: None
        r2.stop_requested = True
        r2.run()
        self.assertIsNone(json.loads((renv.RUNTIME / "runner.status.json").read_text())["alert"])
        self.assertIn(
            "runner stopped (gave up: the game crashes on every launch)", (renv.RUNTIME / "runner.log").read_text()
        )

    def test_mod_refusals_are_setup_failures_not_the_runners_death(self):
        # RUN-3: an RpcError / LuaDebugError from the session setup (a refused ui.menu load, a failed eval) used to
        # miss the run loop's except and kill the unattended runner. Now: setup failure → recover → next session.
        renv.MAX_SETUP_FAILURES = 3
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        recovered, calls = [], []
        r.recover = lambda why: recovered.append(why)

        def play(minutes, first):
            calls.append(first)
            if len(calls) == 1:
                raise rgame.RpcError("ui.menu: loading is disabled (bg3ai/config.json)")
            if len(calls) == 2:
                raise rcode.LuaDebugError("EVAL_ENGINE_NOT_READY")
            r.stop_requested = True
            return rstate.SessionResult("S001", "c", 0, "exited", time.time() - 600, time.time())

        r.play_session = play
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(recovered), 2)
        self.assertIn("loading is disabled", recovered[0])
        kinds = [json.loads(ln)["kind"] for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(kinds.count("setup_failed"), 2)
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("session setup failed: RpcError: ui.menu: loading is disabled", log)
        self.assertIn("runner stopped (stopped)", log)

    def test_api_limit_never_gives_up(self):
        # RUN-7: 5 limits in a row (was: 3 quick exits → "gave up" in ~90 s): each waits, then the run goes on
        renv.API_BACKOFF_MIN_S, renv.API_BACKOFF_MAX_S = 0.01, 0.05
        home = testenv.home("API_BACKOFF_MIN_S")
        self.addCleanup(lambda: setattr(home, "API_BACKOFF_MIN_S", 60) or setattr(home, "API_BACKOFF_MAX_S", 3600))
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        calls = []

        def play(minutes, first):
            calls.append(first)
            now = time.time()
            if len(calls) <= 5:
                return rstate.SessionResult("S001", "c", 1, "api_limit", now - 2, now, detail="usage limit reached")
            r.stop_requested = True
            return rstate.SessionResult("S001", "c", 0, "exited", now - 600, now)

        r.play_session = play
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 6)
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual([e["repeat"] for e in ev if e["kind"] == "api_limit"], [1, 2, 3, 4, 5])
        self.assertNotIn("gave up", (renv.RUNTIME / "runner.log").read_text())

    def test_auth_errors_still_stop(self):
        # RUN-7: a bad key / unknown model exits at once as before ("exited" < 60 s): 3 strikes stop the runner
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        real_sleep = rcode.time.sleep
        rcode.time.sleep = lambda s: None
        self.addCleanup(setattr, rcode.time, "sleep", real_sleep)
        r.play_session = lambda minutes, first: rstate.SessionResult(
            "S001", "c", 1, "exited", time.time() - 2, time.time()
        )
        self.assertEqual(r.run(), 1)
        self.assertIn("runner stopped (gave up: claude exits at once)", (renv.RUNTIME / "runner.log").read_text())

    def test_an_overflow_at_the_kickoff_is_a_quick_failure(self):
        # the kickoff alone overflows the context (no tool call): every new session would, so 3 strikes stop
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        real_sleep = rcode.time.sleep
        rcode.time.sleep = lambda s: None
        self.addCleanup(setattr, rcode.time, "sleep", real_sleep)
        calls = []

        def play(minutes, first):
            calls.append(1)
            r.stop_requested = len(calls) >= 5  # without the rule: sessions go on until stopped
            return rstate.SessionResult("S001", "c", 0, "context_overflow", time.time() - 300, time.time())

        r.play_session = play
        self.assertEqual(r.run(), 1)
        self.assertEqual(len(calls), renv.MAX_QUICK_FAILURES)
        self.assertIn("runner stopped (gave up: claude exits at once)", (renv.RUNTIME / "runner.log").read_text())

    def test_an_overflow_after_play_is_no_quick_failure(self):
        res = rstate.SessionResult("S001", "c", 0, "context_overflow", 0.0, 30.0, tool_calls=40)
        self.assertFalse(renv.quick_failure(res))
        self.assertTrue(renv.quick_failure(rstate.SessionResult("S001", "c", 0, "exited", 0.0, 30.0, tool_calls=40)))

    def test_mcp_failures_are_setup_failures(self):
        # RUN-8: the bg3 MCP server never comes up: a setup failure per session, then a loud give-up
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None
        real_sleep, rcode.time.sleep = rcode.time.sleep, (lambda s: None)
        self.addCleanup(setattr, rcode.time, "sleep", real_sleep)
        n = []

        def play(minutes, first):
            n.append(1)
            return rstate.SessionResult(
                "S001", "c", 0, "mcp_failed", time.time() - 5, time.time(), detail="the bg3 MCP server is failed"
            )

        r.play_session = play
        self.assertEqual(r.run(), 1)
        self.assertEqual(len(n), renv.MAX_SETUP_FAILURES)
        self.assertIn(
            "runner stopped (gave up: the bg3 MCP server fails to start)", (renv.RUNTIME / "runner.log").read_text()
        )

    def test_any_other_death_is_logged_with_its_traceback(self):
        r = lpt.Runner(rstate.Options("loop-test", None, 20, "opus"))
        r.check_memory = lambda: None

        def play(minutes, first):
            raise KeyError("boom")

        r.play_session = play
        with self.assertRaises(KeyError):
            r.run()
        log = (renv.RUNTIME / "runner.log").read_text()
        self.assertIn("runner died:\nTraceback", log)
        self.assertIn("runner stopped (died: KeyError: 'boom')", log)


if __name__ == "__main__":
    unittest.main()
