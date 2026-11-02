#!/usr/bin/env python3
"""play.py arena's refusals before any game work: a draft arena, a profile that isn't there; and each side's player
name. Never launches the game.

    python3 -m unittest runner/test_arena_cli.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
import arena_cli
import play

REPO = Path(__file__).resolve().parents[1]
PI = str(REPO / "profiles" / "deepseek-v4.1-flash-pi.toml")


class ArenaCliTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = play.main(["arena", *argv])
        return code, err.getvalue()

    def test_a_draft_arena_is_refused(self):
        code, err = self.run_cli(["--arena", "draft_open_field", "--a", PI, "--b", PI, "--allow-unverified"])
        self.assertEqual(code, 2)
        self.assertIn("a draft arena can't be played", err)

    def test_an_unverified_or_missing_profile_is_refused(self):
        a = json.loads((REPO / "packages" / "bench" / "arenas" / "draft_open_field.json").read_text())
        a.pop("draft")
        a.update(name="open_field", save={"name": "Tav-1__SCN-arena", "sha1": "0" * 40})
        a["location"]["spawns"] = {"A": [0.0, 0.0, 0.0], "B": [0.0, 0.0, 20.0]}  # fair (check_geometry)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "open_field.json"
            path.write_text(json.dumps(a))
            offline = self.run_cli(["--arena", str(path), "--a", PI, "--b", PI])
            code, err = self.run_cli(
                ["--arena", str(path), "--a", PI, "--b", "profiles/none.toml", "--allow-unverified"]
            )
        self.assertEqual(offline[0], 2)
        self.assertIn("profile status 'offline'", offline[1])
        self.assertEqual(code, 2)
        self.assertIn("profiles/none.toml", err)

    def test_sides_are_named_as_the_bench_rates_them(self):
        s = arena_cli.sides(PI, PI, allow_unverified=True)
        self.assertEqual((s["A"].side, s["B"].side), ("A", "B"))
        self.assertTrue(s["A"].player.startswith("pi/"))
        self.assertEqual(s["A"].profile, PI)

    def test_a_scripted_side_plays_no_profile_and_is_recorded_as_scripted(self):
        s = arena_cli.sides("scripted", PI, allow_unverified=True)
        self.assertEqual((s["A"].player, s["A"].profile, s["A"].client), ("scripted/none", "scripted", "scripted"))
        self.assertTrue(s["B"].player.startswith("pi/"))

    def test_an_arena_name_resolves_to_the_bench_s_file(self):
        self.assertEqual(arena_cli.arena_path("open_field").name, "open_field.json")
        self.assertEqual(arena_cli.arena_path("x/y.json"), Path("x/y.json"))


if __name__ == "__main__":
    unittest.main()
