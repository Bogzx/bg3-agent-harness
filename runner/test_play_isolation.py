"""test_play.py's fakes must not leak into the play module: tearDown restores what the first _runner call saved.

pytest runs test_play.py's classes in file order (unittest sorts them), so a leaked fake from Relaunch broke
ModSnapshot.test_game_mod_path_from_the_game_environ under pytest only.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_play as TP  # noqa: N812

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


if __name__ == "__main__":
    unittest.main()
