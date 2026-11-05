#!/usr/bin/env python3
"""play.py bench and the recording's encoder (bg3harness recorder.bench_encoder): the commands that drive the game
need one set, plan and status none. Never launches the game.

    python3 runner/test_bench_encoder.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))  # never the real runtime
import play as P  # noqa: N812  (play puts packages/harness/src on sys.path)


class BenchEncoder(unittest.TestCase):
    def test_status_needs_no_encoder_set_and_run_is_refused_without_one(self):
        from bg3bench import matrix_cli  # play puts packages/bench/src on sys.path

        # this machine's encoder setting out of reach (BG3AI_BENCH_RECORD=0: test_session_host's)
        none_set = {"BG3AI_CONFIG_DIR": tempfile.mkdtemp(prefix="bg3ai-test-config-")}
        with mock.patch.dict(os.environ, none_set), mock.patch.object(matrix_cli, "main", return_value=0):
            for k in ("BG3AI_BENCH_RECORD_ENCODER", "BG3AI_BENCH_RECORD"):
                os.environ.pop(k, None)
            self.assertEqual(P.lcli.cmd_bench(["status", "--matrix", "m.toml"]), 0)
            self.assertEqual(P.lcli.cmd_bench(["plan", "--matrix", "m.toml"]), 0)
            with self.assertRaisesRegex(ValueError, "no bench recording encoder set"):
                P.lcli.cmd_bench(["run", "--matrix", "m.toml"])


if __name__ == "__main__":
    unittest.main()
