"""bg3bench.pins: buildinfo, the mod version, the tree digest, the version pins and their drift (moved from
runner/test_runs.py)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bg3bench import pins as R


def _git(answers: dict):
    """A fake pins._git: {first arg: stdout} (None: git failed)."""
    return lambda cwd, *args: answers.get(args[0])


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3bench-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class Pins(TmpDir):
    def test_buildinfo_mod_version_and_digest(self):
        info = R.buildinfo(
            "bg3le commit: 3728a68\npatch: 0001-a.patch 126c\npatch: 0002-b.patch 51bc\n"
            "built: 2026-10-13\nsha256: 7d4c\n"
        )
        self.assertEqual(
            info, {"commit": "3728a68", "sha256": "7d4c", "patches": ["0001-a.patch 126c", "0002-b.patch 51bc"]}
        )
        self.assertEqual(R.mod_version('<attribute id="Version64" type="int64" value="36028797018963968"/>'), "1.0.0.0")
        v = (1 << 55) | (2 << 47) | (3 << 31) | 4
        self.assertEqual(R.mod_version(f'id="Version64" type="int64" value="{v}"'), "1.2.3.4")
        self.assertIsNone(R.mod_version("<save/>"))
        self.assertEqual(R.tree_digest({"a": "1", "b": "2"}), R.tree_digest({"b": "2", "a": "1"}))
        self.assertNotEqual(R.tree_digest({"a": "1"}), R.tree_digest({"a": "2"}))

    def test_version_pins(self):
        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "BUILDINFO").write_text("bg3le commit: abc\npatch: 0001-x.patch 1\nsha256: feed\n")
        meta = self.tmp / "meta.lsx"
        meta.write_text('<attribute id="Version64" type="int64" value="36028797018963968"/>')
        git = _git(
            {
                "rev-parse": "92634f4\n",
                "status": " M runner/runs.py\n?? runner/new.py\n",
                "diff": "diff --git a/runner/runs.py\n",
                "describe": "v0.3.6-dirty\n",
            }
        )
        pins = R.version_pins(self.tmp, "25605617", dist, meta, {"Mods/x.lua": "aa"}, git)
        self.assertEqual(pins["game_build"], "25605617")
        self.assertEqual(
            pins["bg3le"],
            {"source": "v0.3.6-dirty", "local_changes": True, "commit": "abc", "sha256": "feed", "patches": 1},
        )
        self.assertEqual(pins["mod"]["version"], "1.0.0.0")
        self.assertEqual((pins["repo"]["commit"], pins["repo"]["dirty"]), ("92634f4", True))
        self.assertEqual(len(pins["repo"]["diff"]), 16)
        # another edit → another diff hash; a clean tree has none
        other = R.repo_pin(
            self.tmp, _git({"rev-parse": "92634f4", "status": " M runner/runs.py\n", "diff": "something else"})
        )
        self.assertNotEqual(other["diff"], pins["repo"]["diff"])
        clean = R.repo_pin(self.tmp, _git({"rev-parse": "92634f4", "status": "", "diff": ""}))
        self.assertEqual(clean, {"commit": "92634f4", "dirty": False, "diff": None})
        # no BUILDINFO: the binary's own sha256; no git, no manifest: None, never an exception
        (dist / "BUILDINFO").unlink()
        (dist / "libbg3le.so").write_bytes(b"elf")
        bare = R.version_pins(self.tmp, "", dist, self.tmp / "missing.lsx", {}, _git({}))
        self.assertEqual(len(bare["bg3le"]["sha256"]), 64)
        self.assertEqual(
            (bare["game_build"], bare["mod"], bare["repo"]["commit"], bare["bg3le"]["local_changes"]),
            (None, {"version": None, "sha1": None}, None, None),
        )

    def test_pin_drift(self):
        first = {
            "game_build": "1",
            "bg3le": {"sha256": "a", "source": "v0.3.6-dirty"},
            "mod": {"sha1": "m"},
            "repo": {"commit": "c1", "dirty": False, "diff": None},
        }
        self.assertEqual(R.pin_drift(first, json.loads(json.dumps(first))), [])
        self.assertEqual(R.pin_drift(None, first), [])
        now = json.loads(json.dumps(first))
        now["game_build"] = "2"
        now["repo"].update(commit="c2", dirty=True, diff="d")
        self.assertEqual(
            R.pin_drift(first, now),
            ["game_build: 1 → 2", "repo.commit: c1 → c2", "repo.dirty: False → True", "repo.diff: None → d"],
        )
