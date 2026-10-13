#!/usr/bin/env python3
"""Tests for tools/bg3le_buildinfo.py and runner/launch_game.sh's stale-build refusal (UI-1), on fake dists.

Run: python3 -m unittest tools/test_bg3le_buildinfo.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bg3le_buildinfo as B  # noqa: E402

LAUNCH = B.REPO / "runner" / "launch_game.sh"


def buildinfo(patches: Path, skip: str | None = None, change: str | None = None) -> str:
    """BUILDINFO as tools/build_bg3le.sh writes it for `patches`, without `skip` and with `change`'s hash wrong."""
    lines = ["bg3le commit: 3728a68ef4a98fe0d25e3dac9b7d1f89d96f40ec"]
    for p in sorted(patches.glob("*.patch")):
        if p.name == skip:
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        lines.append(f"patch: {p.name} {'0' * 16 if p.name == change else h}")
    lines += ["built: 2026-10-13T10:00:00+03:00", "sha256: " + "ab" * 32]
    return "\n".join(lines) + "\n"


class ProblemsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.patches = self.tmp / "patches"
        self.patches.mkdir()
        for n in ("0001-a.patch", "0002-b.patch", "0003-c.patch"):
            (self.patches / n).write_text(f"diff for {n}\n")
        self.dist = self.tmp / "dist"
        self.dist.mkdir()

    def write(self, text: str) -> None:
        (self.dist / "BUILDINFO").write_text(text)

    def test_current(self):
        self.write(buildinfo(self.patches))
        self.assertEqual(B.problems(self.dist, self.patches), [])

    def test_missing_one(self):
        self.write(buildinfo(self.patches, skip="0003-c.patch"))
        bad = B.problems(self.dist, self.patches)
        self.assertIn("missing 0003-c.patch", bad)
        self.assertIn("dist built 2026-10-13T10:00:00+03:00", bad)

    def test_changed_hash(self):
        self.write(buildinfo(self.patches, change="0002-b.patch"))
        self.assertTrue(any(b.startswith("changed 0002-b.patch") for b in B.problems(self.dist, self.patches)))

    def test_no_patch_lines(self):
        # this laptop's dist (2026-10-09): built before any patch existed
        self.write("bg3le commit: x\nbuilt: 2026-10-09T18:31:45+03:00\nsha256: ab\n")
        self.assertEqual(len([b for b in B.problems(self.dist, self.patches) if b.startswith("missing")]), 3)

    def test_patch_removed_from_repo(self):
        self.write(buildinfo(self.patches) + "patch: 0004-gone.patch 0123456789abcdef\n")
        self.assertTrue(any("0004-gone.patch" in b for b in B.problems(self.dist, self.patches)))

    def test_no_buildinfo(self):
        self.assertTrue(B.problems(self.dist, self.patches)[0].startswith("no "))

    def test_cli_exit(self):
        self.write(buildinfo(self.patches, skip="0001-a.patch"))
        r = subprocess.run([sys.executable, str(B.REPO / "tools" / "bg3le_buildinfo.py"), "--dist", str(self.dist),
                            "--patches", str(self.patches)], capture_output=True, text=True)
        self.assertEqual(r.returncode, B.STALE_EXIT)
        self.assertIn("missing 0001-a.patch", r.stdout)
        self.assertIn(B.REBUILD, r.stdout)


class LaunchDryRunTest(unittest.TestCase):
    """launch_game.sh --dry-run against a fake game, sniper and dist (the real patches/bg3le): nothing launches."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        game, sniper = self.tmp / "game", self.tmp / "sniper"
        (game / "bin").mkdir(parents=True)
        sniper.mkdir()
        for exe in (game / "bin" / "bg3", sniper / "run"):
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        self.dist = self.tmp / "dist"
        self.dist.mkdir()
        (self.dist / "libbg3le.so").write_bytes(b"")
        self.env = {**os.environ, "BG3_DIR": str(game), "BG3AI_SNIPER_DIR": str(sniper),
                    "BG3AI_BG3LE_LIB": str(self.dist / "libbg3le.so"), "BG3AI_RUNTIME": str(self.tmp / "runtime"),
                    "BG3AI_GAME_SCOPE": "0", "BG3LE_DEBUG_PORT": "59998"}
        self.env.pop("BG3AI_ALLOW_STALE_BG3LE", None)

    def launch(self, **env) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(LAUNCH), "--dry-run", "--no-input-shim"], capture_output=True, text=True,
                              env={**self.env, **env}, timeout=60)

    def test_missing_patch_refused(self):
        last = sorted(B.PATCHES.glob("*.patch"))[-1].name
        (self.dist / "BUILDINFO").write_text(buildinfo(B.PATCHES, skip=last))
        r = self.launch()
        self.assertEqual(r.returncode, B.STALE_EXIT, r.stderr)
        self.assertIn("REFUSING TO LAUNCH", r.stderr)
        self.assertIn(f"missing {last}", r.stderr)
        self.assertIn(B.REBUILD, r.stderr)
        self.assertNotIn("bin/bg3", r.stdout)

    def test_allow_stale_warns(self):
        (self.dist / "BUILDINFO").write_text(buildinfo(B.PATCHES, skip=sorted(B.PATCHES.glob("*.patch"))[0].name))
        r = self.launch(BG3AI_ALLOW_STALE_BG3LE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STALE bg3le", r.stderr)
        self.assertIn("./bin/bg3", r.stdout)

    def test_current_launches(self):
        (self.dist / "BUILDINFO").write_text(buildinfo(B.PATCHES))
        r = self.launch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("STALE", r.stderr)
        self.assertIn("./bin/bg3", r.stdout)


if __name__ == "__main__":
    unittest.main()
