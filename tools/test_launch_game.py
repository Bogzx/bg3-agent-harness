#!/usr/bin/env python3
"""runner/launch_game.sh's game-build refusal and its log pointers when the game exits (issue #101), against a fake
Steam library (game, sniper, appmanifest) and a fake bg3le dist: nothing real starts.

Run: python3 -m unittest tools/test_launch_game.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import bg3le_buildinfo as B  # noqa: N812
from bg3harness.preflight import pinned_build
from test_bg3le_buildinfo import buildinfo

LAUNCH = B.REPO / "runner" / "launch_game.sh"
PIN = pinned_build(B.REPO)  # WANT_BUILD in tools/dev/first_launch.sh, which tools/ci/game_build.py reads
OTHER = "26000000"
ACF = '"AppState"\n{{\n\t"appid"\t\t"1086940"\n\t"StateFlags"\t\t"4"\n\t"buildid"\t\t"{build}"\n}}\n'
# What the fake game writes to bg3le's log ($BG3LE_LOG.<pid>, as bg3le does) before it exits with $FAKE_RC
CRASH = ["[ 1.000] WARNING: resolve: ecs::Foo not found in this build; what uses it is off"] + [
    f"[ 2.000] crash:   line {i}" for i in range(12)
]


class LaunchGameTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        apps = self.tmp / "lib" / "steamapps"
        self.game = apps / "common" / "Baldurs Gate 3"
        self.manifest = apps / "appmanifest_1086940.acf"
        sniper = self.tmp / "sniper"
        (self.game / "bin").mkdir(parents=True)
        sniper.mkdir()
        # the sniper runs what follows its "--" (env ... ./bin/bg3 ...) in the game dir, as the real one does
        (sniper / "run").write_text('#!/bin/sh\nshift\nexec "$@"\n')
        (self.game / "bin" / "bg3").write_text(
            '#!/bin/sh\n[ -z "$FAKE_LOG" ] || cp "$FAKE_LOG" "$BG3LE_LOG.$$"\nexit "${FAKE_RC:-0}"\n'
        )
        for exe in (sniper / "run", self.game / "bin" / "bg3"):
            exe.chmod(0o755)
        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "libbg3le.so").write_bytes(b"")
        (dist / "BUILDINFO").write_text(buildinfo(B.PATCHES))
        # Steam "runs": a pgrep that finds steam, the real one for anything else (as test_display_env.py)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        real = shutil.which("pgrep")
        (bin_ / "pgrep").write_text(f'#!/bin/sh\ncase "$*" in *steam*) echo 1; exit 0;; esac\nexec {real} "$@"\n')
        (bin_ / "pgrep").chmod(0o755)
        xauth = self.tmp / "xauth"
        xauth.write_text("")
        self.runtime = self.tmp / "runtime"
        self.env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "BG3_DIR": str(self.game),
                    "BG3AI_SNIPER_DIR": str(sniper), "BG3AI_BG3LE_LIB": str(dist / "libbg3le.so"),
                    "BG3AI_RUNTIME": str(self.runtime), "BG3AI_PROFILE_DIR": str(self.tmp / "profile"),
                    "BG3AI_CONFIG_DIR": str(self.tmp / "config"), "BG3AI_REQUIRE_STEAM_OFFLINE": "0",
                    "BG3AI_GAME_SCOPE": "0", "BG3LE_DEBUG_PORT": "59996", "BG3AI_LAUNCH_STAMP": "t1",
                    "DISPLAY": "localhost:96", "XAUTHORITY": str(xauth)}  # fmt: skip
        for k in ("BG3AI_ALLOW_STALE_BG3LE", "BG3AI_ALLOW_OTHER_BUILD"):
            self.env.pop(k, None)

    def installed(self, build: str) -> None:
        self.manifest.write_text(ACF.format(build=build))

    def launch(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(LAUNCH), "--no-input-shim", *args], capture_output=True, text=True,
                              env={**self.env, **env}, timeout=60)  # fmt: skip

    # the game build

    def test_another_build_is_refused(self):
        self.installed(OTHER)
        for args in ((), ("--dry-run",)):
            r = self.launch(*args)
            self.assertEqual(r.returncode, 6, r.stderr)
            self.assertIn(f"REFUSING TO LAUNCH: installed buildid {OTHER} is not the pinned {PIN}", r.stderr)
            self.assertIn("docs/GAME-UPDATES.md", r.stderr)
            self.assertIn("BG3AI_ALLOW_OTHER_BUILD=1", r.stderr)
            self.assertNotIn("bin/bg3", r.stdout)
        self.assertFalse((self.runtime / "logs").exists())  # nothing launched

    def test_allow_other_build_warns(self):
        self.installed(OTHER)
        r = self.launch("--dry-run", BG3AI_ALLOW_OTHER_BUILD="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"WARNING !!! launching ANOTHER GAME BUILD (BG3AI_ALLOW_OTHER_BUILD=1): installed buildid "
                      f"{OTHER} is not the pinned {PIN}", r.stderr)  # fmt: skip
        self.assertIn("./bin/bg3", r.stdout)

    def test_the_pinned_build_launches(self):
        self.installed(PIN)
        r = self.launch("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("game build", r.stderr.lower())
        self.assertIn("./bin/bg3", r.stdout)

    def test_no_bg3le_skips_the_check(self):
        self.installed(OTHER)
        r = self.launch("--dry-run", "--no-bg3le")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("buildid", r.stderr)

    def test_cant_tell_only_warns(self):
        r = self.launch("--dry-run")  # no appmanifest, and the fake game's files are no pinned build's
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("launch_game: warning: game build: can't read the manifest", r.stderr)
        self.assertIn("./bin/bg3", r.stdout)

    # the logs on exit

    def run_game(self, rc: int, lines: list[str]) -> subprocess.CompletedProcess[str]:
        self.installed(PIN)
        log = self.tmp / "bg3le.txt"
        log.write_text("".join(f"{x}\n" for x in ["[ 0.100] bg3le: loaded", *lines]))
        return self.launch(FAKE_RC=str(rc), FAKE_LOG=str(log))

    def bg3le_logs(self) -> list[Path]:
        return sorted((self.runtime / "logs").glob("bg3le-t1.log.*"))

    def test_a_crash_points_at_the_logs(self):
        r = self.run_game(7, CRASH)
        self.assertEqual(r.returncode, 7, r.stderr)  # the game's own status
        (log,) = self.bg3le_logs()
        self.assertIn(f"launch_game: game log {self.runtime / 'logs' / 'game-t1.log'}", r.stderr)
        self.assertIn(f"launch_game: bg3le logs: {log}", r.stderr)
        self.assertIn(f"launch_game: {log}: 13 crash:/WARNING: resolve lines, up to 8 shown:", r.stderr)
        self.assertIn(f"launch_game:   {CRASH[0]}", r.stderr)
        self.assertIn(f"launch_game:   {CRASH[7]}", r.stderr)
        self.assertNotIn(CRASH[8], r.stderr)  # bounded
        self.assertNotIn("bg3le: loaded", r.stderr)

    def test_a_clean_exit_names_the_logs_only(self):
        r = self.run_game(0, CRASH[:1])  # a resolve warning, no crash
        self.assertEqual(r.returncode, 0, r.stderr)
        (log,) = self.bg3le_logs()
        self.assertIn(f"launch_game: bg3le logs: {log}", r.stderr)
        self.assertNotIn("WARNING: resolve", r.stderr)

    def test_a_logged_crash_shows_its_lines_at_status_0(self):
        r = self.run_game(0, CRASH[1:3])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"launch_game:   {CRASH[2]}", r.stderr)

    def test_no_bg3le_log(self):
        self.installed(PIN)
        r = self.launch(FAKE_RC="1")  # the fake game writes no bg3le log
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"launch_game: no bg3le log ({self.runtime / 'logs' / 'bg3le-t1.log'}.<pid>)", r.stderr)


if __name__ == "__main__":
    unittest.main()
