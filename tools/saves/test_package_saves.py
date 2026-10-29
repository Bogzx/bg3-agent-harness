# pyright: basic
"""Offline tests for tools/saves/package_saves.py on tiny synthetic saves in a temporary Story folder (never the
game's): the zips, their read-back, the refusals and the saves.json fill.

Run: uv run pytest tools/saves/test_package_saves.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from bg3bench import saves

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pkg = _load("saves_package", "package_saves.py")
ti = _load("saves_test_inspect", "test_inspect.py")  # its synthetic saves
inspect_mod = ti.inspect_mod

TAG = "bench-v0.8"
CLEAN = ti.lspk.build([("meta.lsf", ti.META, "none")])
DIRTY = ti.synthetic()  # a Steam ID, an e-mail and a user path inside


def save_entry(name: str, lsv: bytes) -> dict:
    return {
        "name": name,
        "scenarios": [name.split("__SCN-")[1]],
        "save_sha1": hashlib.sha1(lsv).hexdigest(),
        "zip": f"{name}.zip",
        "key": f"saves/{TAG}/{name}.zip",
        "bytes": None,
        "sha256": None,
        "fingerprint": None,
    }


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.story, self.out, self.json = root / "Story", root / "out", root / "saves.json"
        self.story.mkdir()
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name: str, lsv: bytes, extra: dict[str, bytes] | None = None) -> dict:
        d = self.story / name
        d.mkdir()
        (d / f"{name.split('__')[1]}.lsv").write_bytes(lsv)
        (d / f"{name.split('__')[1]}.WebP").write_bytes(b"RIFF\0\0\0\0WEBP")
        for n, data in (extra or {}).items():
            (d / n).write_bytes(data)
        return save_entry(name, lsv)

    def write_json(self, entries: list[dict]) -> None:
        self.json.write_text(
            json.dumps({"schema": 1, "about": "x", "suites": {TAG: {"saves": entries}}}, indent=1) + "\n"
        )

    def run_it(self) -> int:
        return pkg.run(TAG, self.story, self.out, self.json, inspect_mod.inspect_save, self.lines.append)

    def story_state(self) -> dict:
        return {
            str(p.relative_to(self.story)): (p.stat().st_mtime_ns, p.read_bytes())
            for p in self.story.rglob("*")
            if p.is_file()
        }

    def test_packages_clean_saves_and_fills_saves_json(self):
        a = self.add("Tav-1__SCN-alpha", CLEAN)
        b = self.add("Tav-2__SCN-beta", CLEAN + b"")
        self.write_json([a, b])
        before = self.story_state()
        self.assertEqual(self.run_it(), 0, self.lines)
        self.assertEqual(self.story_state(), before)  # the Story folder is only read
        listed = saves.load(self.json)[TAG]
        for name in ("Tav-1__SCN-alpha", "Tav-2__SCN-beta"):
            data = (self.out / f"{name}.zip").read_bytes()
            self.assertEqual(listed[name].bytes, len(data))
            self.assertEqual(listed[name].sha256, hashlib.sha256(data).hexdigest())
            files = saves.unpack(data, name)  # what the panel's install reads
            self.assertEqual(sorted(files), [f"{name.split('__')[1]}.WebP", f"{name.split('__')[1]}.lsv"])
            with zipfile.ZipFile(self.out / f"{name}.zip") as zf:
                self.assertTrue(all(i.date_time == pkg.ZIP_TIME for i in zf.infolist()))
        sums = (self.out / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(sums), 2)
        index = json.loads((self.out / "index.json").read_text())
        self.assertEqual(index["saves"]["Tav-1__SCN-alpha"]["inspect"]["findings"], 0)

    def test_zip_is_deterministic(self):
        files = {"x.lsv": CLEAN, "x.WebP": b"w"}
        self.assertEqual(
            pkg.zip_bytes("Tav-1__SCN-x", files), pkg.zip_bytes("Tav-1__SCN-x", dict(reversed(files.items())))
        )

    def test_personal_data_is_refused_and_saves_json_untouched(self):
        a = self.add("Tav-1__SCN-alpha", CLEAN)
        dirty = self.add("Tav-2__SCN-dirty", DIRTY)
        self.write_json([a, dirty])
        before = self.json.read_bytes()
        self.assertEqual(self.run_it(), 1)
        self.assertEqual(self.json.read_bytes(), before)
        self.assertFalse((self.out / "Tav-2__SCN-dirty.zip").exists())
        self.assertTrue(any("REFUSED Tav-2__SCN-dirty: personal data" in m and "steam_id" in m for m in self.lines))
        self.assertNotIn(ti.STEAM, "\n".join(self.lines))

    def test_wrong_sha1_missing_folder_and_stray_files_are_refused(self):
        wrong = self.add("Tav-1__SCN-alpha", CLEAN) | {"save_sha1": "0" * 40}
        missing = save_entry("Tav-2__SCN-gone", CLEAN)
        stray = self.add("Tav-3__SCN-stray", CLEAN, {"notes.txt": b"hi"})
        two = self.add("Tav-4__SCN-two", CLEAN, {"other.lsv": CLEAN})
        self.write_json([wrong, missing, stray, two])
        self.assertEqual(self.run_it(), 1)
        text = "\n".join(self.lines)
        self.assertIn("Tav-1__SCN-alpha: its .lsv sha1", text)
        self.assertIn("Tav-2__SCN-gone: not a folder", text)
        self.assertIn("unexpected file notes.txt", text)
        self.assertIn("Tav-4__SCN-two: not exactly one .lsv", text)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["SHA256SUMS", "index.json"])

    def test_a_linked_save_folder_is_refused(self):
        real = Path(self.tmp.name) / "elsewhere"
        real.mkdir()
        os.symlink(real, self.story / "Tav-1__SCN-alpha")
        self.write_json([save_entry("Tav-1__SCN-alpha", CLEAN)])
        self.assertEqual(self.run_it(), 1)

    def test_saves_json_disagreeing_with_the_scenarios_is_refused(self):
        a = self.add("Tav-1__SCN-alpha", CLEAN)
        self.write_json([a])
        scenarios = {"alpha": {"start": {"save": "Tav-1__SCN-alpha", "save_sha1": "0" * 40}}}
        with self.assertRaisesRegex(pkg.PackageError, "disagree"):
            pkg.run(TAG, self.story, self.out, self.json, inspect_mod.inspect_save, self.lines.append, scenarios)
        self.assertFalse(self.out.exists())

    def test_out_inside_the_story_folder_is_refused(self):
        self.write_json([])
        argv = [
            "--suite",
            TAG,
            "--story",
            str(self.story),
            "--out",
            str(self.story / "x"),
            "--saves-json",
            str(self.json),
        ]
        self.assertEqual(pkg.main(argv), 1)
        self.assertFalse((self.story / "x").exists())


if __name__ == "__main__":
    unittest.main()
