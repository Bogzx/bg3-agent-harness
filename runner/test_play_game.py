#!/usr/bin/env python3
"""runner/play.py killing its game and telling its own game processes from a user's (moved from test_play.py). Never
signals a real process: pids, /proc reads and kill calls are fakes.

    python3 -m unittest runner/test_play_game.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")
import play as P  # noqa: N812, I001  (play puts packages/harness/src on sys.path)
from bg3harness.game import io as G  # noqa: N812
from bg3harness.game import controller as C  # noqa: N812  (the GameController and its timings)

os_getpgid = P.os.getpgid


class KillGame(unittest.TestCase):
    """kill_game with every OS call mocked: nothing is signalled for real."""

    def setUp(self):
        self.saved = (
            G.all_bg3_pids,
            G.port_open,
            P.os.kill,
            P.os.killpg,
            P.time.sleep,
            C.KILL_GRACE,
            C.KILL_CONFIRM,
            P.RUNTIME,
            G.crash_reporter_pids,
            G.is_harness_pid,
            G.proc_start,
            G.proc_cmdline,
        )
        self.tmp = tempfile.TemporaryDirectory()
        P.RUNTIME = Path(self.tmp.name)  # RUN-9: never the real runtime/runner.log
        G.crash_reporter_pids = lambda: []  # nor the machine's real crash reporters
        self.sent = []
        self.alive = {4242}  # our harness game
        self.foreign = set()  # a user's own Steam-launched BG3
        self.ignores_term = True
        # the launcher's group 777: launch_game.sh started at tick 555 (runner.json records it as pg_start)
        self.leader = {"start": 555, "cmd": b"/bin/bash\0/repo/runner/launch_game.sh\0"}

        def kill(pid, sig):
            self.sent.append((pid, sig))
            if sig == P.signal.SIGKILL or not self.ignores_term:
                self.alive.discard(pid)

        G.all_bg3_pids = lambda: sorted(self.alive | self.foreign)
        G.is_harness_pid = lambda pid: pid not in self.foreign
        G.proc_start = lambda pid: self.leader["start"] if pid == 777 else None
        G.proc_cmdline = lambda pid: self.leader["cmd"] if pid == 777 else b""
        G.port_open = lambda *a, **k: bool(self.alive)
        P.os.kill = kill
        P.os.killpg = lambda pg, sig: self.sent.append(("pg", pg, sig))
        P.time.sleep = lambda s: None
        C.KILL_GRACE = 0.01
        C.KILL_CONFIRM = 0.01
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc = None
        self.r.state = type(  # pyright: ignore[reportAttributeAccessIssue]  (a fake RunnerState)
            "S",
            (),
            {
                "data": {"game": {"pgid": 777, "pg_start": 555}},
                "__getitem__": lambda s, k: s.data.get(k),
                "__setitem__": lambda s, k, v: s.data.__setitem__(k, v),
            },
        )()

    def tearDown(self):
        (
            G.all_bg3_pids,
            G.port_open,
            P.os.kill,
            P.os.killpg,
            P.time.sleep,
            C.KILL_GRACE,
            C.KILL_CONFIRM,
            P.RUNTIME,
            G.crash_reporter_pids,
            G.is_harness_pid,
            G.proc_start,
            G.proc_cmdline,
        ) = self.saved
        self.tmp.cleanup()

    def test_logs_to_the_test_runtime(self):
        # RUN-9: the kill bursts these tests log go to a temp runtime, never REPO/runtime/runner.log
        self.r.kill_game()
        self.assertIn("killing the game", (P.RUNTIME / "runner.log").read_text())
        self.assertNotEqual(self.saved[7], P.REPO / "runtime")  # the module-level guard: BG3AI_RUNTIME is a tmp dir

    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        self.r.kill_game()
        sigs = [x[-1] for x in self.sent]
        self.assertIn(P.signal.SIGTERM, sigs)
        self.assertIn(P.signal.SIGKILL, sigs)
        self.assertIn(("pg", 777, P.signal.SIGTERM), self.sent)  # the launcher's group too
        self.assertIn((4242, P.signal.SIGKILL), self.sent)  # and the bg3 process itself
        self.assertIsNone(self.r.state["game"])

    def test_no_sigkill_when_sigterm_works(self):
        self.ignores_term = False
        self.r.kill_game()
        self.assertNotIn(P.signal.SIGKILL, [x[-1] for x in self.sent])

    def test_survivor_raises(self):
        P.os.kill = lambda pid, sig: self.sent.append((pid, sig))  # nothing ever dies
        with self.assertRaises(RuntimeError):
            self.r.kill_game()

    def test_users_own_bg3_never_signalled(self):
        # RUN-4: a user's Steam-launched BG3 beside ours: ours dies, theirs gets no signal at all
        self.foreign = {5151}
        self.r.kill_game()
        self.assertIn((4242, P.signal.SIGKILL), self.sent)
        self.assertNotIn(5151, [x[0] for x in self.sent])
        self.assertIn("leaving bg3 pid(s) 5151 alone", (P.RUNTIME / "runner.log").read_text())

    def test_stale_group_not_signalled(self):
        # RUN-4: runner.json's pgid after a reboot (or a reused pid): another start time or another program
        for leader in ({"start": 999, "cmd": self.leader["cmd"]}, {"start": 555, "cmd": b"/usr/bin/firefox\0"}):
            with self.subTest(leader=leader):
                self.sent, self.alive = [], {4242}
                self.r.state["game"] = {"pgid": 777, "pg_start": 555}
                self.leader = leader
                self.assertIsNone(self.r.owned_group())
                self.r.kill_game()
                self.assertNotIn("pg", [x[0] for x in self.sent])
                self.assertIn((4242, P.signal.SIGKILL), self.sent)  # our game itself still goes

    def test_owned_group(self):
        self.assertEqual(self.r.owned_group(), 777)
        self.r.state["game"] = {"pgid": 777}  # a record from before pg_start: the command line decides
        self.assertEqual(self.r.owned_group(), 777)
        self.leader = {"start": None, "cmd": b""}  # leader gone: ours only while it holds our game
        P.os.getpgid = lambda pid: 777 if pid == 4242 else 1
        try:
            self.assertEqual(self.r.owned_group(), 777)
            self.alive = set()
            self.assertIsNone(self.r.owned_group())
        finally:
            P.os.getpgid = os_getpgid

    def test_launch_refused_beside_a_foreign_bg3(self):
        self.foreign, self.alive = {5151}, set()
        self.r.event = lambda kind, **f: self.sent.append(("event", kind))
        saved = (P.steam_ready, P.bg3le_problems, P.subprocess.Popen)
        P.steam_ready, P.bg3le_problems = (lambda: (True, "ok")), (lambda: [])
        P.subprocess.Popen = None  # launching would fail loudly
        try:
            with self.assertRaises(P.ForeignGameRunning):
                self.r.launch_game(None)
        finally:
            P.steam_ready, P.bg3le_problems, P.subprocess.Popen = saved
        self.assertIn(("event", "foreign_game"), self.sent)
        self.assertTrue(issubclass(P.ForeignGameRunning, P.LaunchBlocked))  # the run loop retries, never gives up

    def test_launch_refreshes_a_stale_xauthority(self):
        # naut3: Xwayland restarted (16:01 freeze) with a new -auth file; a game launched with the old one can't
        # open a window. Each launch re-checks it.
        from unittest import mock

        self.alive = set()
        envs: list[dict] = []
        args: list[list] = []

        class Proc:
            pid = 4343

        def popen(a, **kw):
            args.append(a)
            envs.append(kw["env"])
            return Proc()

        self.r.state = {}  # pyright: ignore[reportAttributeAccessIssue]
        self.r.refresh_mod_snapshot = lambda: []
        with (
            mock.patch.object(P, "steam_ready", lambda: (True, "ok")),
            mock.patch.object(P, "bg3le_problems", lambda: []),
            mock.patch.object(P.subprocess, "Popen", popen),
            mock.patch.object(P, "mod_tree_hashes", lambda: {}),
            mock.patch.object(P, "write_status", lambda **kw: None),
            mock.patch.object(
                P.display_env,
                "pgrep_xwayland",
                lambda: "9 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_new -rootless\n",
            ),
            mock.patch.object(P.display_env, "problem", lambda env: None),
            mock.patch.dict(P.os.environ, {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_old"}),
        ):
            self.r.launch_game(None)
        self.assertEqual(envs[0]["XAUTHORITY"], "/run/user/1000/xauth_new")
        self.assertEqual(args[0][1:3], ["--mod-path", str(P.MOD_LIVE)])  # S24: the snapshot, never <repo>/mod
        self.assertIn("display env refreshed", (P.RUNTIME / "runner.log").read_text())

    def test_launch_refused_without_a_display(self):
        # L8: a launch with no usable display fell back to the AMD iGPU and dumped cores; refuse, the run loop retries
        from unittest import mock

        self.alive = set()
        self.r.event = lambda kind, **f: self.sent.append(("event", kind, f))
        with (
            mock.patch.object(P, "steam_ready", lambda: (True, "ok")),
            mock.patch.object(P, "bg3le_problems", lambda: []),
            mock.patch.object(P.subprocess, "Popen", None),
            mock.patch.object(P, "write_status", lambda **kw: None),
            mock.patch.object(P.display_env, "pgrep_xwayland", lambda: ""),
            mock.patch.dict(P.os.environ, {"DISPLAY": ":97", "XAUTHORITY": "/nonexistent/xauth"}),
            self.assertRaises(P.NoDisplay) as cm,
        ):
            self.r.launch_game(None)
        self.assertIn("DISPLAY=:97 but no X server socket", str(cm.exception))
        self.assertTrue(issubclass(P.NoDisplay, P.LaunchBlocked))
        self.assertEqual(self.sent[-1][:2], ("event", "display_refused"))
        self.assertIn("REFUSING TO LAUNCH BG3: no usable display", (P.RUNTIME / "runner.log").read_text())


class HarnessPid(unittest.TestCase):
    """RUN-4: is_harness_pid/proc_start on real processes (sleep children; no game)."""

    def spawn(self, **env):
        import subprocess

        p = subprocess.Popen(["sleep", "30"], env={"PATH": "/usr/bin:/bin", **env})
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.1)
        return p.pid

    def test_markers(self):
        self.assertFalse(G.is_harness_pid(self.spawn()))  # a user's own process
        self.assertFalse(G.is_harness_pid(self.spawn(SteamAppId="1086940")))  # a Steam-launched BG3
        self.assertTrue(G.is_harness_pid(self.spawn(BG3AI_GAME_STAMP="20261016-120000")))  # launch_game.sh's
        self.assertTrue(
            G.is_harness_pid(
                self.spawn(  # a launch from before
                    VK_LAYER_MESA_SCREENSHOT_CONFIG="comms;control=bg3ai-shot;output_dir=/x"
                )
            )
        )
        self.assertFalse(G.is_harness_pid(2**22 + 7))  # no such process

    def test_proc_start_identity(self):
        a = self.spawn()
        self.assertIsInstance(G.proc_start(a), int)
        self.assertEqual(G.proc_start(a), G.proc_start(a))
        self.assertIsNone(G.proc_start(2**22 + 7))
        self.assertIn(b"sleep", G.proc_cmdline(a))

    def test_launcher_marks_the_game(self):
        sh = (P.REPO / "runner" / "launch_game.sh").read_text()
        self.assertIn('"BG3AI_GAME_STAMP=$stamp"', sh)
        self.assertIn('SCOPE_UNIT="bg3ai-game-$stamp.scope"', sh)


if __name__ == "__main__":
    unittest.main()
