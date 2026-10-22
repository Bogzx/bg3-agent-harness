"""tools/dev/live_window.py offline: a fake repo (fake first_launch.sh, acceptance.py, steam_paths.py, uv) and fake
game processes. No game, no Steam, nothing launched for real.

    python3 -m unittest discover -s tools/dev -p 'test_live_window.py'
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_window as LW  # noqa: E402

ACC = types.SimpleNamespace(SECTIONS=["reads", "act", "se"], LOCK_HELD_ENV="BG3AI_GAME_LOCK_HELD",
                            SECTION_OWNERS={"reads": "vis", "act": "act", "se": "se"})

FAKE_ACCEPTANCE = r'''
import fcntl, json, os, sys
from pathlib import Path
repo = Path(__file__).resolve().parents[2]
out = repo / "runtime" / "acceptance"
out.mkdir(parents=True, exist_ok=True)
probe = {"lock_env": os.environ.get("BG3AI_GAME_LOCK_HELD"), "argv": sys.argv[1:]}
with open(repo / "runtime" / "game.lock", "a") as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probe["game_lock"] = "free"
    except BlockingIOError:
        probe["game_lock"] = "held"
(repo / "runtime" / "acceptance-probe.json").write_text(json.dumps(probe))
if "--check" in sys.argv:
    print("11 checks, 0 FAIL" if os.environ.get("FAKE_CHECK_RC", "0") == "0" else "1 FAIL: tools")
    sys.exit(int(os.environ.get("FAKE_CHECK_RC", "0")))
rows = json.loads(os.environ.get("FAKE_ROWS", "[]"))
for s in {r["section"] for r in rows}:
    (out / f"{s}.log").write_text(f"log of {s}\n")
(out / "results.json").write_text(json.dumps(rows))
(out / "table.md").write_text("| item | verdict | evidence |\n")
if os.environ.get("FAKE_MEMORY"):
    (out / "memory.json").write_text(os.environ["FAKE_MEMORY"])
print("plan: ...")
sys.exit(sum(r["verdict"] == "FAIL" for r in rows))
'''

FAKE_LAUNCH = r'''#!/bin/bash
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ls -l /proc/$$/fd > "$REPO/runtime/launch-fds.txt"
echo "$*" > "$REPO/runtime/launch-args.txt"
echo "DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY" > "$REPO/runtime/launch-env.txt"
echo "== 3 launch"
if [ "${FAKE_LAUNCH_RC:-0}" != 0 ]; then echo "  FAIL: the game exited before bg3le's debug server came up" >&2; fi
exit "${FAKE_LAUNCH_RC:-0}"
'''

FAKE_UV = r'''#!/bin/bash
while [ $# -gt 0 ] && [ "$1" != python ]; do shift; done
shift
exec python3 "$@"
'''


class FakeProcs:
    """bg3 processes: `game` harness pids that ignore SIGTERM and die on SIGKILL; `foreign` a user's own BG3."""

    def __init__(self, game=(), foreign=(), dies_on=signal.SIGKILL, steam=True, saves: Path | None = None):
        self.g, self.f, self.dies_on, self.steam, self.saves = list(game), list(foreign), dies_on, steam, saves
        self.sent: list[tuple[int, int]] = []

    def steam_running(self):
        return self.steam

    def save_dir(self, save):
        return self.saves / save

    def steam_offline(self):
        return True, True, "steamui_login.txt: Start offline - 1"

    def bg3le_problems(self):
        return []

    def game(self):
        return list(self.g)

    def foreign(self):
        return list(self.f)

    def reporters(self):
        return []

    def kill(self, pid, sig):
        assert pid not in self.f, f"signalled the user's bg3 {pid}"
        self.sent.append((pid, sig))
        if sig == self.dies_on and pid in self.g:
            self.g.remove(pid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.rt = self.repo / "runtime"
        (self.repo / "tools" / "dev").mkdir(parents=True)
        (self.repo / "runner").mkdir()
        (self.repo / "packages" / "harness" / "src" / "bg3harness").mkdir(parents=True)
        (self.repo / "mod").mkdir()
        (self.repo / "bin").mkdir()
        self.rt.mkdir()
        self.saves = self.repo / "saves"
        (self.saves / LW.DEFAULT_SAVE).mkdir(parents=True)
        self.write("tools/steam_paths.py", 'import os, sys\nprint("offline: steamui_login" if os.environ.get('
                   '"FAKE_STEAM_RC", "0") == "0" else "NOT offline: online")\nsys.exit(int(os.environ.get('
                   '"FAKE_STEAM_RC", "0")))\n')
        self.write("tools/bg3le_buildinfo.py", 'print("OK: dist has all 10 patches")\n')
        self.write("tools/dev/acceptance.py", FAKE_ACCEPTANCE)
        self.write("tools/dev/first_launch.sh", FAKE_LAUNCH, exe=True)
        self.write("packages/harness/src/bg3harness/__init__.py", "")
        self.write("packages/harness/src/bg3harness/ramwatch.py", "import time\ntime.sleep(3)\n")
        self.write("bin/uv", FAKE_UV, exe=True)
        self.xauth = self.repo / "xauth_test"   # a real file: display_env.fill replaces a missing one (stale)
        self.xauth.write_text("")
        self.env = dict(os.environ, PATH=f"{self.repo / 'bin'}:{os.environ['PATH']}", DISPLAY=":1",
                        XAUTHORITY=str(self.xauth))
        for k in ("FAKE_STEAM_RC", "FAKE_CHECK_RC", "FAKE_LAUNCH_RC", "FAKE_ROWS"):
            self.env.pop(k, None)
        self.out: list[str] = []
        self.ram_lock = None

    def tearDown(self):
        if self.ram_lock:
            self.ram_lock.close()
        self.tmp.cleanup()

    def write(self, rel, text, exe=False):
        p = self.repo / rel
        p.write_text(text)
        if exe:
            p.chmod(0o755)

    def hold_ramwatch_lock(self):
        import fcntl
        self.ram_lock = open(self.rt / "ramwatch.lock", "a")
        fcntl.flock(self.ram_lock, fcntl.LOCK_EX)

    def window(self, *args, procs=None, **env):
        self.env.update(env)
        a = LW.parse(list(args))
        procs = procs or FakeProcs(game=[4242], saves=None)
        if getattr(procs, "saves", 0) is None:
            procs.saves = self.saves
        w = LW.Window(a, repo=self.repo, runtime=self.rt, procs=procs, env=self.env,
                      out=self.out.append, sleep=lambda s: None, acceptance=ACC)
        return w

    def launched(self) -> bool:
        return (self.rt / "launch-args.txt").exists()

    def verdict(self, w, step):
        return [r["verdict"] for r in w.rows if r["step"] == step]


class TestDryRun(Base):
    def test_prints_every_step_runs_nothing(self):
        boom = mock.Mock(side_effect=AssertionError("ran something"))
        real_run = subprocess.run

        def run(argv, *a, **k):   # the dry run's probes may only look (pgrep)
            assert argv[0] == "pgrep", argv
            return real_run(argv, *a, **k)
        with mock.patch.object(subprocess, "Popen", boom), mock.patch.object(subprocess, "run", run), \
                mock.patch.object(LW.fcntl, "flock", boom):
            w = self.window("--dry-run", "--only", "act,se", procs=FakeProcs(game=[], saves=None))
            self.assertEqual(w.run(), 0)
        text = "\n".join(self.out)
        for step in ("lock", "display", "steam", "bg3le", "check", "ramwatch", "launch", "reload", "acceptance",
                     "collect", "stop", "summary"):
            self.assertRegex(text, rf"\d+\. {step}\b")
        self.assertIn("acceptance.py --only act,se", text)
        self.assertIn("BG3AI_GAME_LOCK_HELD=1", text)
        self.assertIn("--load Claude-4810126690__S027-start", text)
        self.assertFalse((self.rt / "live").exists())
        self.assertFalse(self.launched())

    def test_probes_say_what_would_refuse(self):
        self.window("--dry-run", procs=FakeProcs(foreign=[777], steam=False, saves=None)).run()
        text = "\n".join(self.out)
        self.assertIn("WOULD REFUSE: the user's own bg3 runs (pid(s) [777])", text)
        self.assertIn("WOULD REFUSE: no Steam client process", text)
        self.assertRegex(text, r"would refuse at: .*launch")

    def test_probe_missing_save_and_held_lock(self):
        holder = subprocess.Popen(["flock", str(self.rt / "game.lock"), "sleep", "30"])
        try:
            for _ in range(50):
                if LW.lock_holders(self.rt / "game.lock"):
                    break
                time.sleep(0.1)
            self.window("--dry-run", "--save", "Claude-1__nope", procs=FakeProcs(saves=None)).run()
        finally:
            holder.kill()
            holder.wait()
        text = "\n".join(self.out)
        self.assertIn("WOULD REFUSE: no save folder", text)
        self.assertIn("game.lock open in pid(s)", text)

    def test_keep_game_and_skip_check_show(self):
        self.window("--dry-run", "--keep-game", "--skip-check").run()
        text = "\n".join(self.out)
        self.assertIn("SKIP (--keep-game)", text)
        self.assertIn("SKIP (--skip-check)", text)


class TestRefusals(Base):
    def test_game_lock_held_by_a_worker(self):
        holder = subprocess.Popen(["flock", str(self.rt / "game.lock"), "sleep", "30"])
        try:
            for _ in range(50):
                if LW.held_by_other(self.rt / "game.lock"):
                    break
                time.sleep(0.1)
            w = self.window()
            rc = w.run()
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(self.verdict(w, "lock"), ["FAIL"])
        self.assertGreaterEqual(rc, 1)
        self.assertFalse(self.launched())
        ev = w.rows[0]["evidence"]
        self.assertIn("game.lock", ev)
        self.assertIn("--lock-wait", ev)

    def test_runner_lock_held(self):
        holder = subprocess.Popen(["flock", str(self.rt / "runner.lock"), "sleep", "30"])
        try:
            for _ in range(50):
                if LW.held_by_other(self.rt / "runner.lock"):
                    break
                time.sleep(0.1)
            w = self.window()
            w.run()
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(self.verdict(w, "lock"), ["FAIL"])
        self.assertIn("runner", w.rows[0]["evidence"])
        self.assertIn(str(holder.pid), w.rows[0]["evidence"])
        self.assertFalse(self.launched())

    def test_steam_not_offline_stops_before_launch(self):
        w = self.window(FAKE_STEAM_RC="3")
        self.assertGreaterEqual(w.run(), 1)
        self.assertEqual(self.verdict(w, "steam"), ["FAIL"])
        self.assertIn("exit 3", [r for r in w.rows if r["step"] == "steam"][0]["evidence"])
        self.assertEqual(self.verdict(w, "check"), [])
        self.assertFalse(self.launched())
        self.assertTrue((w.dir / "summary.md").exists())

    def test_steam_not_running_stops_before_launch(self):
        w = self.window(procs=FakeProcs(game=[], steam=False, saves=None))
        w.run()
        self.assertEqual(self.verdict(w, "steam"), ["PASS", "FAIL"])
        self.assertFalse(self.launched())

    def test_missing_save_refused_before_launch(self):
        self.hold_ramwatch_lock()
        procs = FakeProcs(game=[], saves=None)
        w = self.window("--save", "Claude-1__nope", procs=procs)
        w.run()
        self.assertEqual(self.verdict(w, "launch"), ["FAIL"])
        self.assertIn("no save folder", [r for r in w.rows if r["step"] == "launch"][0]["evidence"])
        self.assertFalse(self.launched())
        self.assertEqual(procs.sent, [])

    def test_check_fail_stops_before_launch(self):
        w = self.window(FAKE_CHECK_RC="1")
        w.run()
        self.assertEqual(self.verdict(w, "check"), ["FAIL"])
        self.assertFalse(self.launched())
        self.assertIn("### orchestrator", w.summary())

    def test_users_own_bg3_is_never_touched(self):
        procs = FakeProcs(game=[], foreign=[777], saves=None)
        self.hold_ramwatch_lock()
        w = self.window(procs=procs)
        w.run()
        self.assertEqual(self.verdict(w, "launch"), ["FAIL"])
        self.assertEqual(self.verdict(w, "stop"), ["SKIP"])
        self.assertEqual(procs.sent, [])
        self.assertFalse(self.launched())

    def test_harness_game_already_up_needs_attach_and_is_not_stopped(self):
        procs = FakeProcs(game=[4242], saves=None)
        self.hold_ramwatch_lock()
        w = self.window(procs=procs)
        w.run()
        self.assertEqual(self.verdict(w, "launch"), ["FAIL"])
        self.assertIn("--attach", [r for r in w.rows if r["step"] == "launch"][0]["evidence"])
        self.assertEqual(procs.sent, [])
        self.assertFalse(self.launched())

    def test_unknown_section_refused_before_anything(self):
        with self.assertRaises(SystemExit):
            self.window("--only", "nope").run()
        self.assertFalse((self.rt / "live").exists())


ROWS = [
    {"section": "reads", "item": "status read", "verdict": "PASS", "evidence": "12 ms"},
    {"section": "act", "item": "move_to", "verdict": "FAIL", "evidence": "teleport_blocked"},
    {"section": "act", "item": "jump", "verdict": "SKIP", "evidence": "no jump"},
    {"section": "se", "item": "hotbar Bless", "verdict": "FAIL", "evidence": "no slot", "owner": "act"},
    {"section": "se", "item": "UI-5 strict", "verdict": "PASS", "evidence": "0 refusals"},
]


class TestWindow(Base):
    def launch_then(self, procs, *args, rows=ROWS, **env):
        """The fake launch 'starts' the game: procs gets harness pid 4242 once first_launch.sh ran."""
        orig = LW.Window.cmd

        def cmd(w, argv, *a, **k):
            r = orig(w, argv, *a, **k)
            if argv[0].endswith("first_launch.sh") and r[0] == 0 and "--attach" not in argv:
                procs.g.append(4242)
            return r
        self.hold_ramwatch_lock()
        with mock.patch.object(LW.Window, "cmd", cmd):
            w = self.window(*args, procs=procs, FAKE_ROWS=json.dumps(rows), **env)
            rc = w.run()
        return w, rc

    def test_happy_path_table_owners_and_stop(self):
        procs = FakeProcs(game=[], saves=None)
        w, rc = self.launch_then(procs)
        self.assertEqual(rc, 3)   # acceptance step FAIL (exit 2) + 2 item FAILs
        for step in ("lock", "display", "steam", "bg3le", "check", "ramwatch", "launch", "stop"):
            self.assertEqual(self.verdict(w, step), ["PASS"], step)
        self.assertEqual(self.verdict(w, "reload"), ["SKIP"])
        self.assertEqual(self.verdict(w, "acceptance"), ["FAIL"])
        self.assertEqual(self.verdict(w, "collect"), [])
        # the launch: the save, --skip-shim, the display env; never the lock fd
        self.assertEqual((self.rt / "launch-args.txt").read_text().split(),
                         ["--skip-shim", "--", "--load", LW.DEFAULT_SAVE])
        self.assertIn(f"XAUTHORITY={self.xauth}", (self.rt / "launch-env.txt").read_text())
        self.assertNotIn("game.lock", (self.rt / "launch-fds.txt").read_text())
        # the acceptance ran while the window held game.lock, told not to flock it itself
        probe = json.loads((self.rt / "acceptance-probe.json").read_text())
        self.assertEqual(probe, {"lock_env": "1", "argv": [], "game_lock": "held"})
        # SIGTERM first (bg3 ignores it), then SIGKILL; only the harness pid
        self.assertEqual(procs.sent, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        s = (w.dir / "summary.md").read_text()
        self.assertIn("| act | act | 0 | 1 | 1 |", s)
        self.assertIn("| se | se | 1 | 1 | 0 |", s)
        self.assertIn("### act (2)", s)   # act's own FAIL + se's item that names act as its owner
        self.assertIn("- [act] move_to: teleport_blocked", s)
        self.assertIn("- [se] hotbar Bless: no slot", s)
        self.assertNotIn("### se", s)
        self.assertNotIn("Game memory", s)   # no memory.json from the acceptance: no table
        # tee: outputs and copies in the run dir; the lock is released at the end
        self.assertIn("plan: ...", (w.dir / "acceptance.log").read_text())
        self.assertIn("first_launch.sh", (w.dir / "window.log").read_text())
        self.assertTrue((w.dir / "acceptance" / "act.log").exists())
        self.assertTrue((w.dir / "acceptance" / "results.json").exists())
        self.assertFalse(LW.held_by_other(self.rt / "game.lock"))

    def test_memory_per_section_in_the_summary(self):
        # L8: the window game hit its 10G scope cap (+990M swap); the summary shows the memory after each section
        mem = [{"after": "start", "rss": 2497, "swap": 0, "cg_current": 3100, "cg_peak": 3200, "cg_swap_peak": 0,
                "anon": 1700, "file": 1400},
               {"after": "act", "rss": 6250, "swap": 10, "cg_current": 9900, "cg_peak": 10240, "cg_swap_peak": 990,
                "anon": 5400, "file": 4500},
               {"after": "se"}]   # the game died
        w, _ = self.launch_then(FakeProcs(game=[], saves=None), FAKE_MEMORY=json.dumps(mem))
        s = (w.dir / "summary.md").read_text()
        self.assertIn("| after | RSS | swap | scope | scope peak | scope swap peak | anon | file |", s)
        self.assertIn("| start | 2497 | 0 | 3100 | 3200 | 0 | 1700 | 1400 |", s)
        self.assertIn("| act | 6250 | 10 | 9900 | 10240 | 990 | 5400 | 4500 |", s)
        self.assertIn("| se | no bg3 | | | | | | |", s)
        self.assertTrue((w.dir / "acceptance" / "memory.json").exists())

    def test_only_and_keep_game(self):
        procs = FakeProcs(game=[], saves=None)
        w, rc = self.launch_then(procs, "--only", "se", "--keep-game", rows=ROWS[3:])
        self.assertEqual(json.loads((self.rt / "acceptance-probe.json").read_text())["argv"], ["--only", "se"])
        self.assertEqual(self.verdict(w, "stop"), ["SKIP"])
        self.assertEqual(procs.sent, [])
        self.assertNotIn("| act |", w.summary())

    def test_all_pass_exit_zero(self):
        rows = [dict(r, verdict="PASS") for r in ROWS]
        w, rc = self.launch_then(FakeProcs(game=[], saves=None), rows=rows)
        self.assertEqual(rc, 0)
        self.assertIn("## Failures by owner\n\nnone", w.summary())

    def test_game_death_collects_logs(self):
        logs = self.rt / "logs"
        logs.mkdir()
        (logs / "bg3le-20261016-1.log.4242").write_text("boot\n" * 500 + "crash: signal 11 at bg3+0x218f104\n")
        (logs / "game-20261016-1.log").write_text("game line\n")
        (self.rt / "ram.log").write_text("rss 9.6 GB\n")
        rows = ROWS[:2] + [{"section": "act", "item": "game alive after section act", "verdict": "FAIL",
                            "evidence": "bg3 is gone"},
                           {"section": "se", "item": "section se", "verdict": "SKIP", "evidence": "not run"}]
        procs = FakeProcs(game=[], saves=None)
        orig = LW.Window.cmd

        def cmd(w, argv, *a, **k):   # the game dies during the acceptance
            r = orig(w, argv, *a, **k)
            if argv[0].endswith("first_launch.sh"):
                procs.g.append(4242)
            elif "acceptance.py" in " ".join(argv) and "--check" not in argv:
                procs.g.clear()
            return r
        self.hold_ramwatch_lock()
        with mock.patch.object(LW.Window, "cmd", cmd), \
                mock.patch.object(LW.subprocess, "run", wraps=subprocess.run) as run:
            w = self.window(procs=procs, FAKE_ROWS=json.dumps(rows))
            w.run()
        self.assertEqual(w.died, "act")
        self.assertEqual(self.verdict(w, "collect"), ["PASS"])
        d = w.dir / "death"
        self.assertIn("crash: signal 11", (d / "bg3le-crash-lines.txt").read_text())
        self.assertEqual(len((d / "bg3le-tail.log").read_text().splitlines()), 300)
        self.assertIn("rss 9.6 GB", (d / "ram.log").read_text())
        self.assertTrue((d / "game-20261016-1.log").exists())
        self.assertTrue(any(c.args[0][:2] == ["journalctl", "--user"] for c in run.call_args_list))
        self.assertEqual(procs.sent, [])   # nothing left to stop
        self.assertEqual(self.verdict(w, "stop"), ["PASS"])

    def test_launch_failure_with_no_game_collects(self):
        procs = FakeProcs(game=[], saves=None)
        self.hold_ramwatch_lock()
        w = self.window(procs=procs, FAKE_LAUNCH_RC="1")
        w.run()
        self.assertEqual(self.verdict(w, "launch"), ["FAIL"])
        self.assertIn("exited before bg3le", [r for r in w.rows if r["step"] == "launch"][0]["evidence"])
        self.assertEqual(w.died, "launch")
        self.assertEqual(self.verdict(w, "collect"), ["PASS"])
        self.assertEqual(self.verdict(w, "acceptance"), [])
        self.assertTrue((w.dir / "launch.log").exists())

    def test_attach_reuses_the_game_and_reloads_when_mod_is_newer(self):
        procs = FakeProcs(game=[4242], saves=None)
        (self.rt / "game.pid").write_text("4242")
        old = time.time() - 100
        os.utime(self.rt / "game.pid", (old, old))
        (self.repo / "mod" / "X.lua").write_text("-- new")
        reloads = []
        orig = LW.Window.cmd

        def cmd(w, argv, *a, **k):
            if "-c" in argv or "bg3ai.luadebug" in argv:
                reloads.append(argv)
                return 0, "true"
            return orig(w, argv, *a, **k)
        self.hold_ramwatch_lock()
        with mock.patch.object(LW.Window, "cmd", cmd):
            w = self.window("--attach", procs=procs, FAKE_ROWS=json.dumps(ROWS[:1]))
            w.run()
        self.assertEqual((self.rt / "launch-args.txt").read_text().split(), ["--attach", "--skip-shim"])
        self.assertEqual(self.verdict(w, "reload"), ["PASS"])
        self.assertEqual(len(reloads), 3)   # Ext.Debug.Reset() + 2 module checks
        self.assertEqual(procs.sent[-1], (4242, signal.SIGKILL))

    def test_attach_no_reload_when_mod_older(self):
        (self.repo / "mod" / "X.lua").write_text("-- old")
        old = time.time() - 100
        os.utime(self.repo / "mod" / "X.lua", (old, old))
        (self.rt / "game.pid").write_text("4242")
        self.hold_ramwatch_lock()
        w = self.window("--attach", "--keep-game", procs=FakeProcs(game=[4242], saves=None), FAKE_ROWS=json.dumps(ROWS[:1]))
        w.run()
        self.assertEqual(self.verdict(w, "reload"), ["SKIP"])

    def test_ramwatch_started_when_not_running(self):
        w = self.window(procs=FakeProcs(game=[], saves=None))
        w.dir.mkdir(parents=True)
        w.ramwatch()
        self.assertEqual(self.verdict(w, "ramwatch"), ["PASS"])
        self.assertIn("started", w.rows[-1]["evidence"])


class TestHelpers(Base):
    def test_xwayland_auth(self):
        ps = "1873 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_ABcd -rootless -listenfd 4"
        self.assertEqual(LW.xwayland_auth(ps), "/run/user/1000/xauth_ABcd")
        self.assertIsNone(LW.xwayland_auth(""))

    def test_display_filled_from_xwayland_when_unset(self):
        self.env.pop("DISPLAY")
        self.env.pop("XAUTHORITY")
        w = self.window()
        fake = subprocess.CompletedProcess([], 0, stdout="9 /usr/bin/Xwayland :1 -auth /run/user/1000/xa_1 -x\n")
        with mock.patch.object(LW.subprocess, "run", return_value=fake):
            self.assertTrue(w.display())
        self.assertEqual((w.env["DISPLAY"], w.env["XAUTHORITY"]), (":1", "/run/user/1000/xa_1"))

    def test_display_fails_without_any_auth(self):
        self.env.pop("XAUTHORITY")
        w = self.window()
        with mock.patch.object(LW.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            self.assertFalse(w.display())

    def test_find_uv(self):
        self.assertEqual(LW.find_uv({"PATH": str(self.repo / "bin")}), str(self.repo / "bin" / "uv"))
        home = self.repo / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        self.assertEqual(LW.find_uv({"PATH": "/nonexistent", "HOME": str(home)}), "uv")
        (home / ".local" / "bin" / "uv").write_text("")
        self.assertEqual(LW.find_uv({"PATH": "/nonexistent", "HOME": str(home)}), str(home / ".local/bin/uv"))

    def test_cmd_timeout(self):
        w = self.window()
        w.dir.mkdir(parents=True)
        t = time.monotonic()
        rc, _ = w.cmd(["sleep", "20"], timeout=0.5)
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - t, 5)

    def test_held_by_other(self):
        p = self.rt / "x.lock"
        self.assertFalse(LW.held_by_other(p))
        p.touch()
        self.assertFalse(LW.held_by_other(p))

    def test_real_acceptance_owners_cover_every_section(self):
        acc = LW.load_acceptance()
        self.assertEqual(set(acc.SECTION_OWNERS), set(acc.SECTIONS))
        self.assertTrue(set(acc.SECTION_OWNERS.values()) <= {"act", "vis", "se", "setup"})
        self.assertEqual(acc.LOCK_HELD_ENV, "BG3AI_GAME_LOCK_HELD")

    def test_real_play_harness_logic_is_used(self):
        p = LW.Procs()
        gio = LW.load_game_io()
        with mock.patch.object(gio, "all_bg3_pids", return_value=[11, 22]), \
                mock.patch.object(gio, "is_harness_pid", side_effect=lambda pid: pid == 11):
            self.assertEqual((p.game(), p.foreign()), ([11], [22]))

    def test_every_process_name_procs_uses_resolves(self):
        # live5 §0.1: Procs called play.<name> after the names moved to bg3harness.game.io, so every live window
        # died at launch with an AttributeError the fakes never saw. Every self.p.<name> must exist on the module.
        import re
        names = set(re.findall(r"self\.p\.(\w+)", Path(LW.__file__).read_text()))
        self.assertEqual(names, {"pids_of_game", "foreign_bg3_pids", "crash_reporter_pids"})
        gio = LW.Procs().p
        for name in names:
            self.assertTrue(callable(getattr(gio, name, None)), name)
        with mock.patch.object(gio, "all_bg3_pids", return_value=[]):
            p = LW.Procs()
            self.assertEqual((p.game(), p.foreign()), ([], []))
        with mock.patch.object(gio.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(LW.Procs().reporters(), [])


if __name__ == "__main__":
    unittest.main()
