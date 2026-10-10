#!/usr/bin/env python3
"""Offline tests for runner/ramwatch.py (fake /proc rows; never signals real processes)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ramwatch as R  # noqa: E402

REAL_ANON_SWAP_MB = R.anon_swap_mb

PROCS = [
    (100, "bg3", "./bin/bg3 -continueGame"),
    (200, "python3", "python3 -m unittest discover -s tests -p test_*.py"),
    (201, "bash", "/bin/bash tools/dev/test_act_lua.sh"),
    (202, "docker", "docker run --rm nickblah/lua lua tools/dev/test_act_lua.lua"),
    (300, "claude", "claude -p --model opus test_x.py"),
    (301, "python3", "python3 runner/play.py run --playthrough claude-run-2"),
    (302, "python3", "uv run --project bridge python -m bg3ai.server"),
]


class RamWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-test-ram"))
        self.logs = []
        self.w = R.RamWatch(self.logs.append, logfile=self.tmp / "ram.log")
        patcher = mock.patch.object(R, "anon_swap_mb", return_value=0)   # never read the real game's /proc
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selects_only_test_runs(self):
        self.assertEqual(R.game_pids(PROCS), [100])
        self.assertEqual(R.test_pids(PROCS), [200, 201, 202])

    def test_meminfo_parse(self):
        p = self.tmp / "meminfo"
        p.write_text("MemTotal:       16000000 kB\nMemAvailable:    2048000 kB\nSwapFree: 1024 kB\n")
        self.assertEqual(R.meminfo(p)["MemAvailable"], 2000)

    @mock.patch.object(R, "rss_mb", return_value=5000)
    @mock.patch.object(R, "get_adj", return_value=200)
    def test_ok_ram_leaves_game_adj_alone_and_logs(self, _adj, _rss):
        # The game is the OOM victim of choice now (its own capped scope); lowering its adj made agents the victims.
        with mock.patch.object(R, "set_adj", return_value="sudo") as sa, mock.patch.object(R.os, "kill") as k:
            out = self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
            self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
        sa.assert_not_called()
        k.assert_not_called()
        self.assertEqual(out["action"], "")
        self.assertIn("avail=6000MB", (self.tmp / "ram.log").read_text())

    @mock.patch.object(R, "rss_mb", return_value=5000)
    @mock.patch.object(R, "get_adj", return_value=-500)
    def test_low_ram_marks_tests_then_kills_them(self, _adj, _rss):
        with mock.patch.object(R, "set_adj", return_value="ok") as sa, mock.patch.object(R.os, "kill") as k:
            out = self.w.tick({"MemAvailable": 1200}, PROCS)
            self.assertEqual(sorted(c.args[0] for c in sa.call_args_list), [200, 201, 202])
            k.assert_not_called()
            self.assertIn("marked", out["action"])
            self.assertTrue(any("WARNING" in m for m in self.logs))
            out = self.w.tick({"MemAvailable": 500}, PROCS)
        self.assertEqual(sorted(c.args[0] for c in k.call_args_list), [200, 201, 202])
        self.assertIn("killed 3", out["action"])

    def test_rss_spike_logged_with_last_tools(self):
        # S038 20:48: 5.0 → 13.3 GB in < 40 s after the Shadowfell load, around jump_to/move_to calls.
        import json
        self.w.actions, self.w.spikes = self.tmp / "actions.jsonl", self.tmp / "spikes.jsonl"
        t = 1_000_000.0
        self.w.actions.write_text("\n".join(json.dumps(r) for r in [
            {"t": t - 500, "tool": "get_status", "args": {}, "ms": 50, "result": "old"},
            {"t": t + 8, "tool": "do_actions", "args": {"n": 3}, "ms": 195, "result": "1. jump_to: FAILED\nSTOPPED"},
            {"t": t + 30, "tool": "jump_to", "args": {"x": -408.5}, "ms": 12118, "result": "NOT READY"}]) + "\n")
        self.assertIsNone(self.w.sample(t, 5000, 6000, [100]))
        self.assertIsNone(self.w.sample(t + 20, 6500, 6000, [100]))  # +1.5 GB: not yet
        self.assertTrue(self.w.fast)                                  # but growing fast: 1 s samples
        rec = self.w.sample(t + 35, 9000, 2000, [100])
        self.assertEqual((rec["kind"], rec["from_mb"], rec["to_mb"], rec["seconds"]), ("spike", 5000, 9000, 35))
        self.assertEqual(len(rec["tools"]), 2)
        self.assertIn("jump_to", rec["tools"][-1])
        self.assertIn("do_actions", rec["tools"][0])
        self.assertTrue(any("RSS SPIKE" in m and "jump_to" in m for m in self.logs))
        self.assertIn("RSS SPIKE", (self.tmp / "ram.log").read_text())
        self.assertEqual(json.loads(self.w.spikes.read_text())["to_mb"], 9000)
        self.assertIsNone(self.w.sample(t + 40, 9100, 2000, [100]))  # one report per window

    def _kill_setup(self):
        import json
        self.w.actions, self.w.spikes, self.w.kills = (self.tmp / "a.jsonl", self.tmp / "s.jsonl",
                                                        self.tmp / "k.jsonl")
        self.w.logs = self.tmp / "logs"
        self.w.logs.mkdir()
        (self.w.logs / "bg3le-20261010-181659.log.1813133").write_bytes(
            b"server LoadLevel\n\x00\nNavigation.lua:189 Tick took 229 ms\n")
        self.w.actions.write_text(json.dumps({"t": 99.0, "tool": "jump_to", "args": {"x": -408.5}, "ms": 1,
                                              "result": "ok"}) + "\n")

    def test_kills_game_above_rss_limit_with_crumbs(self):
        self._kill_setup()
        with mock.patch.object(R.os, "kill") as k:
            self.assertIsNone(self.w.sample(90.0, 5000, 6000, [100, 101]))
            rec = self.w.sample(100.0, R.KILL_GAME_MB + 1, 3000, [100, 101])
        self.assertEqual(rec["kind"], "kill")
        self.assertEqual([c.args for c in k.call_args_list], [(100, R.signal.SIGKILL), (101, R.signal.SIGKILL)])
        self.assertIn("Tick took 229 ms", rec["bg3le"][-1])
        self.assertIn("jump_to", rec["tools"][0])
        self.assertTrue(any("KILLED bg3" in m and "Tick took" in m for m in self.logs))
        self.assertTrue(self.w.kills.exists())

    def test_kills_game_on_anon_plus_swap_when_rss_stays_low(self):
        # S039 21:53: RSS 4.8 → 7.9 GB, the scope swapped the rest out and the kernel OOM-killed bg3 at the 10G cap
        # before the RSS limit fired. Anon + swap catches it.
        self._kill_setup()
        with mock.patch.object(R.os, "kill") as k:
            self.assertIsNone(self.w.sample(90.0, 5800, 6000, [100], anon_swap=5500))   # normal play
            self.assertIsNone(self.w.sample(91.0, 7000, 4000, [100], anon_swap=7400))
            self.assertTrue(self.w.fast)
            rec = self.w.sample(92.0, 7900, 3000, [100], anon_swap=R.KILL_ANON_SWAP_MB + 100)
        self.assertEqual(rec["kind"], "kill")
        self.assertEqual(k.call_args_list[-1].args, (100, R.signal.SIGKILL))
        self.assertIn("anon+swap", rec["why"])
        self.assertEqual(rec["anon_swap_mb"], R.KILL_ANON_SWAP_MB + 100)

    def test_anon_swap_from_proc_status(self):
        st = self.tmp / "status"
        st.write_text("VmRSS:\t 5894116 kB\nRssAnon:\t 5400000 kB\nRssFile:\t 490000 kB\nVmSwap:\t 135824 kB\n")
        with mock.patch.object(R, "Path", return_value=st):
            self.assertEqual(REAL_ANON_SWAP_MB(1), (5400000 + 135824) // 1024)
        self.assertEqual(REAL_ANON_SWAP_MB(-1), 0)                     # no such process

    def test_low_avail_kills_tests_first_then_game(self):
        self._kill_setup()
        with mock.patch.object(R, "test_pids", return_value=[200]), mock.patch.object(R.os, "kill") as k:
            self.assertIsNone(self.w.sample(100.0, 6000, 600, [100]))     # tests go first
            self.assertEqual(k.call_args_list[-1].args, (200, R.signal.SIGTERM))
            self.assertTrue(self.w.fast)
            rec = self.w.sample(105.0, 6100, 600, [100])                  # still short: the game
        self.assertEqual(k.call_args_list[-1].args, (100, R.signal.SIGKILL))
        self.assertIn("RAM available", rec["why"])

    def test_rss_slow_growth_and_new_game_are_not_spikes(self):
        t = 2_000_000.0
        for i in range(30):                                         # +3 GB over 5 min: a slow climb
            self.assertIsNone(self.w.sample(t + i * 10, 5000 + i * 100, 6000))
        self.assertIsNone(self.w.sample(t + 400, 0, 6000))          # the game died: baseline reset
        self.assertIsNone(self.w.sample(t + 405, 6000, 6000))       # a new game's first sample (0 → 6 GB)

    def test_lock_single_instance(self):
        a = R.take_lock(self.tmp / "l")
        self.assertIsNotNone(a)
        self.assertIsNone(R.take_lock(self.tmp / "l"))
        a.close()


if __name__ == "__main__":
    unittest.main()
