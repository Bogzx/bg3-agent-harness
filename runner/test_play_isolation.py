"""test_play.py's fakes must not leak into the play module: tearDown restores what the first _runner call saved.

pytest runs test_play.py's classes in file order (unittest sorts them), so a leaked fake from Relaunch broke
ModSnapshot.test_game_mod_path_from_the_game_environ under pytest only.
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: I001  (first: the game's profile folders are a temp dir before play)
import test_play_saves as TP  # noqa: N812

import play as P  # noqa: N812


class RelaunchRestores(unittest.TestCase):
    def test_runner_twice_still_restores_the_real_functions(self) -> None:
        real = (P.game_mod_path, P.list_saves, P.port_open)
        case = TP.Relaunch("test_reason")
        try:
            with tempfile.TemporaryDirectory() as td:
                for _ in range(2):
                    case._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0)
        finally:
            case.tearDown()
        self.assertEqual((P.game_mod_path, P.list_saves, P.port_open), real)


class RealDirs(unittest.TestCase):
    """live5 §0.4: test_runs reached Runner.reset_for_run with play's real SE_BG3AI_DIR and moved the live game's
    Script Extender BG3AI/nav_*.json away. Every runner test module that imports play imports testenv first (one
    that imports only another test module gets it from that one), so play's profile folders are a temp dir
    whichever module the process imports first."""

    HERE = Path(__file__).resolve().parent
    FIRST = re.compile(r"^(import play\b|from play import)", re.M)

    def test_play_reads_the_temp_profile_not_the_real_one(self) -> None:
        import steam_paths

        real = {steam_paths.saves_dir({}), steam_paths.se_dir({}) / "BG3AI"}
        for d in (P.SAVE_DIR, P.SE_BG3AI_DIR):
            self.assertNotIn(d, real)
            self.assertTrue(d.is_relative_to(testenv.ROOT), d)

    def test_every_runner_test_module_imports_testenv_first(self) -> None:
        late = []
        for f in sorted(self.HERE.glob("test_*.py")):
            text = f.read_text()
            first = self.FIRST.search(text)
            if first is None:
                continue
            env = re.search(r"^import testenv\b", text, re.M)
            if env is None or env.start() > first.start():
                late.append(f.name)
        self.assertEqual(late, [])


if __name__ == "__main__":
    unittest.main()
