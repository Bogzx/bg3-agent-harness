#!/usr/bin/env python3
"""Offline tests for bg3harness.ramwatch (fake /proc rows; never signals real processes)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bg3harness import ramwatch as R

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

UNIT = "bg3ai-game-20261104-143000.scope"
TREE = ('     0x1a00003 "Baldur\'s Gate 3 (1920x1080) - (Vulkan) - v4.1.1": ("bg3" "bg3")  1408x792+0+0  +0+0\n'
        '     0x1a00001 "bg3": ("bg3" "bg3")  10x10+0+0  +0+0\n')
XWININFO_ID = ('xwininfo: Window id: 0x1a00003 "Baldur\'s Gate 3 (1920x1080) - (Vulkan) - v4.1.1"\n\n'
               "  Width: 1408\n  Height: 792\n  Depth: 24\n  Map State: IsUnMapped\n  Override Redirect State: no\n")


class RamWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-test-ram"))
        self.logs = []
        with mock.patch.dict(R.os.environ, clear=False):
            R.os.environ.pop(R.GAME_CAP_ENV, None)  # the 10G numbers, whatever cap this shell's game runs at
            self.w = R.RamWatch(self.logs.append, logfile=self.tmp / "ram.log")
        patcher = mock.patch.object(R, "anon_swap_mb", return_value=0)   # never read the real game's /proc
        patcher.start()
        self.addCleanup(patcher.stop)
        self.journal, self.windows = "", ""   # fake journalctl / xwininfo output (never the real ones)
        self.w.run = self.fake_run

    def fake_run(self, args, **_kw):
        if args[0] == "journalctl":
            return SimpleNamespace(stdout=self.journal)
        return SimpleNamespace(stdout=self.windows if "-tree" in args else XWININFO_ID)

    def scope_tree(self, unit: str = UNIT, oom_kill: int = 0, memory_max: str | None = None) -> Path:
        """A fake /proc/100/cgroup in the game's scope and that scope's memory files."""
        self.w.proc, self.w.cgroup_root = self.tmp / "proc", self.tmp / "cg"
        (self.w.proc / "100").mkdir(parents=True, exist_ok=True)
        rel = f"user.slice/user-1000.slice/app.slice/{unit}"
        (self.w.proc / "100" / "cgroup").write_text(f"0::/{rel}\n")
        cg = self.w.cgroup_root / rel
        cg.mkdir(parents=True, exist_ok=True)
        (cg / "memory.events").write_text(f"low 0\nhigh 0\nmax 12\noom 1\noom_kill {oom_kill}\noom_group_kill 0\n")
        (cg / "memory.stat").write_text(f"anon {5000 * 2**20}\nfile {3000 * 2**20}\nkernel {200 * 2**20}\n"
                                        "shmem 0\nfile_mapped 1048576\npgfault 99\n")
        if memory_max is not None:
            (cg / "memory.max").write_text(memory_max + "\n")
        return cg

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

    def test_kill_limits_follow_a_cap_in_g(self):
        # REVIEW-16 A4 (D880): min(the 10G number, cap - 500 MiB); the pilot's game ran at MemoryMax=8G
        self.assertEqual((R.cap_mb("8G"), R.cap_mb("7.5g")), (8192, 7680))
        self.assertEqual(R.kill_limits(R.cap_mb("8G")), (7692, 7692))
        self.assertEqual(R.kill_limits(R.cap_mb("9G")), (8716, 8716))
        self.assertEqual(R.kill_limits(R.cap_mb("10G")), (R.KILL_GAME_MB, R.KILL_ANON_SWAP_MB))  # 9500/9000 stay
        self.assertEqual(R.kill_limits(R.cap_mb("12G")), (R.KILL_GAME_MB, R.KILL_ANON_SWAP_MB))

    def test_kill_limits_follow_a_cap_in_m(self):
        self.assertEqual((R.cap_mb("8192M"), R.cap_mb("6000m")), (8192, 6000))
        self.assertEqual(R.kill_limits(R.cap_mb("6000m")), (5500, 5500))

    def test_kill_limits_keep_the_10g_numbers_without_a_numeric_cap(self):
        for cap in (None, "", "infinity", "80%", "8X", "0"):
            self.assertIsNone(R.cap_mb(cap), cap)
        self.assertEqual(R.kill_limits(None), (R.KILL_GAME_MB, R.KILL_ANON_SWAP_MB))

    def test_cap_mb_reads_bytes_and_k_t_like_systemd(self):
        # REVIEW-18d NOTE 2: launch_game.sh passes BG3AI_GAME_MEMORY_MAX to MemoryMax as given
        self.assertEqual(R.cap_mb("8589934592"), 8192)
        self.assertEqual(R.cap_mb("8388608K"), 8192)
        self.assertEqual(R.cap_mb("8388608k"), 8192)
        self.assertEqual(R.cap_mb("1T"), 1024 * 1024)
        self.assertEqual(R.kill_limits(R.cap_mb("8589934592")), (7692, 7692))

    def _watch(self, env: dict) -> R.RamWatch:
        with mock.patch.dict(R.os.environ, env):
            if not env:
                R.os.environ.pop(R.GAME_CAP_ENV, None)
            w = R.RamWatch(self.logs.append, logfile=self.tmp / "ram.log")
        w.actions, w.spikes, w.kills, w.logs, w.run = self.w.actions, self.w.spikes, self.w.kills, self.w.logs, self.w.run
        w.proc, w.cgroup_root = self.tmp / "no-proc", self.tmp / "no-cg"   # no scope: the env's cap
        return w

    def test_an_8g_game_is_killed_under_its_cap(self):
        self._kill_setup()
        w = self._watch({R.GAME_CAP_ENV: "8G"})
        with mock.patch.object(R.os, "kill") as k:
            self.assertIsNone(w.sample(90.0, 5800, 6000, [100], anon_swap=7102))   # 2026-10-28's healthy max
            self.assertIsNone(w.sample(91.0, 7000, 4000, [100], anon_swap=7691))
            rec = w.sample(92.0, 7000, 4000, [100], anon_swap=7692)
        self.assertEqual(rec["kind"], "kill")
        self.assertEqual(k.call_args_list, [mock.call(100, R.signal.SIGKILL)])
        self.assertIn("anon+swap 7692 MB >= 7692 MB", rec["why"])
        with mock.patch.object(R.os, "kill"):
            self.assertIsNone(w.sample(200.0, 7352, 4000, [100], anon_swap=0))  # the healthy RSS max
            rec = w.sample(300.0, 7692, 4000, [100], anon_swap=0)
        self.assertIn("game RSS 7692 MB >= 7692 MB", rec["why"])

    def test_the_scope_memory_max_sets_the_cap_without_the_env(self):
        # REVIEW-18 §4: a ramwatch started without $BG3AI_GAME_MEMORY_MAX follows the scope's real cap
        self._kill_setup()
        w = self._watch({})
        self.assertEqual((w.kill_game_mb, w.kill_anon_swap_mb), (R.KILL_GAME_MB, R.KILL_ANON_SWAP_MB))
        self.scope_tree(memory_max=str(8 * 2**30))
        w.proc, w.cgroup_root = self.w.proc, self.w.cgroup_root
        with mock.patch.object(R.os, "kill") as k:
            rec = w.sample(100.0, 6000, 6000, [100], anon_swap=7692)
        self.assertEqual((w.kill_game_mb, w.kill_anon_swap_mb), (7692, 7692))
        self.assertEqual((rec["kind"], k.call_args_list), ("kill", [mock.call(100, R.signal.SIGKILL)]))

    def test_the_cap_is_the_smallest_memory_max_of_the_scope_and_its_ancestors(self):
        # REVIEW-18d NOTE 1: the scope's processes run under every ancestor's limit too (app.slice, user@.service, ...)
        cg = self.scope_tree(memory_max="max")
        app, user = cg.parent, cg.parent.parent
        (app / "memory.max").write_text(f"{8 * 2**30}\n")
        (user / "memory.max").write_text("max\n")
        (self.w.cgroup_root / "memory.max").write_text("1048576\n")   # the root itself is not a limit read here
        self.assertEqual(R.scope_cap_mb(cg, self.w.cgroup_root), 8192)
        (cg / "memory.max").write_text(f"{10 * 2**30}\n")                # a higher scope cap under the 8G slice
        self.assertEqual(R.scope_cap_mb(cg, self.w.cgroup_root), 8192)
        (cg / "memory.max").write_text(f"{6 * 2**30}\n")
        self.assertEqual(R.scope_cap_mb(cg, self.w.cgroup_root), 6144)
        (app / "memory.max").write_text("max\n")
        (cg / "memory.max").write_text("max\n")
        self.assertIsNone(R.scope_cap_mb(cg, self.w.cgroup_root))
        w = self._watch({})
        (app / "memory.max").write_text(f"{8 * 2**30}\n")
        w.proc, w.cgroup_root = self.w.proc, self.w.cgroup_root
        w.sample(100.0, 5000, 6000, [100], anon_swap=5000)
        self.assertEqual((w.kill_game_mb, w.kill_anon_swap_mb), (7692, 7692))

    def test_the_scope_memory_max_wins_over_the_env_and_max_falls_back_to_it(self):
        w = self._watch({R.GAME_CAP_ENV: "6G"})
        self.assertEqual(w.kill_anon_swap_mb, 5644)
        cg = self.scope_tree(memory_max=str(8 * 2**30))
        w.proc, w.cgroup_root = self.w.proc, self.w.cgroup_root
        w.sample(100.0, 5000, 6000, [100], anon_swap=5000)
        self.assertEqual((w.kill_game_mb, w.kill_anon_swap_mb), (7692, 7692))
        (cg / "memory.max").write_text("max\n")                          # no cap on the scope: the env's
        w.sample(110.0, 5000, 6000, [100], anon_swap=5000)
        self.assertEqual((w.kill_game_mb, w.kill_anon_swap_mb), (5644, 5644))

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
            self.assertIsNone(self.w.sample(t + i * 10, 5000 + i * 100, 6000, [100]))
        self.assertIsNone(self.w.sample(t + 400, 0, 6000, [100]))          # the game died: baseline reset
        self.assertIsNone(self.w.sample(t + 405, 6000, 6000, [100]))       # a new game's first sample (0 → 6 GB)

    def test_last_tick_skips_spike_lines(self):
        # live window 20261016-183253: setup ran 10 s after a save-load spike, the log's last line was the SPIKE
        log = self.tmp / "ram.log"
        self.assertIsNone(R.last_tick(log))                          # no log yet
        log.write_text(
            "2026-10-16 18:33:57 avail=7344MB swap_free=2764MB game_rss=2564MB anon+swap=1780MB game_adj=[0, 0]\n"
            "2026-10-16 18:34:33 RSS SPIKE: game 2527 → 4688 MB in 56 s (RAM available 5110 MB); last tool calls: "
            "none in the last 2 min\n")
        t, ln = R.last_tick(log)
        self.assertIn("anon+swap=1780MB", ln)
        self.assertEqual(t, R.datetime(2026, 10, 16, 18, 33, 57).timestamp())
        log.write_text("2026-10-16 18:33:57 avail=7344MB game_rss=2564MB game_adj=[0, 0]\n")   # pre-anon+swap code
        self.assertIsNone(R.last_tick(log))
        self.w._write("x")
        self.w.tick({"MemAvailable": 9000, "SwapFree": 100}, procs=[])   # the watchdog's own tick line parses
        self.assertLess(abs(R.last_tick(log)[0] - R.time.time()), 5)

    @mock.patch.object(R, "rss_mb", return_value=5000)
    @mock.patch.object(R, "get_adj", return_value=200)
    def test_tick_logs_window_and_scope_memory(self, _adj, _rss):
        # r13 a1 (2026-11-04 14:37): the game died with no word of its scope's memory or its window in ram.log
        self.scope_tree(oom_kill=1)
        self.windows = TREE
        self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
        line = (self.tmp / "ram.log").read_text().splitlines()[-1]
        self.assertIn(" win=unmapped 1408x792 ", line)
        self.assertIn(f"scope={UNIT} events[low=0 high=0 max=12 oom=1 oom_kill=1 oom_group_kill=0] "
                      "stat[anon=5000MB file=3000MB kernel=200MB shmem=0MB file_mapped=1MB]", line)
        self.assertIsNotNone(R.last_tick(self.tmp / "ram.log"))
        self.windows = ""                                            # no game window on the display
        self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
        self.assertIn(" win=none ", (self.tmp / "ram.log").read_text().splitlines()[-1])
        self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, [])      # no game: no window, no scope
        self.assertNotIn("win=", (self.tmp / "ram.log").read_text().splitlines()[-1])

    def test_game_window_states(self):
        run = lambda a, **_k: SimpleNamespace(stdout=TREE if "-tree" in a else XWININFO_ID.replace("UnMapped", "Viewable"))  # noqa: E731
        self.assertEqual(R.game_window(":1", run), "viewable 1408x792")
        self.assertEqual(R.game_window(":1", lambda a, **_k: SimpleNamespace(stdout="")), "none")

        def no_xwininfo(a, **_k):
            raise FileNotFoundError("xwininfo")
        self.assertEqual(R.game_window(":1", no_xwininfo), "none")

    def test_scope_end_logs_result_and_last_memory(self):
        import shutil
        cg = self.scope_tree(oom_kill=1)
        self.w.sample(1000.0, 5000, 6000, [100])
        self.assertEqual(self.w.scope["unit"], UNIT)
        shutil.rmtree(cg)                                            # --collect: the scope's cgroup is gone
        self.w.sample(1005.0, 0, 6000, [])                           # no end line in the journal yet: wait
        self.assertFalse(any("ended" in m for m in self.logs))
        self.journal = (f"Started {UNIT} - [systemd-run] /usr/bin/env ...\n"
                        f"{UNIT}: A process of this unit has been killed by the OOM killer.\n"
                        f"{UNIT}: Failed with result 'oom-kill'.\n"
                        f"{UNIT}: Consumed 20min 40.069s CPU time, 9.9G memory peak.\n")
        self.w.sample(1010.0, 0, 6000, [])
        msg = [m for m in self.logs if "ended" in m]
        self.assertEqual(len(msg), 1)
        self.assertIn(f"game scope {UNIT} ended: Result=oom-kill; last memory 5 s before it was gone: events[", msg[0])
        self.assertIn("oom_kill=1", msg[0])
        self.assertIn("journal: A process of this unit has been killed", msg[0])
        self.assertIn("Result=oom-kill", (self.tmp / "ram.log").read_text())
        self.w.sample(1015.0, 0, 6000, [])                           # once
        self.assertEqual(len([m for m in self.logs if "ended" in m]), 1)

    def test_scope_end_without_journal_lines_and_a_new_scope(self):
        import shutil
        cg = self.scope_tree()
        self.w.sample(1000.0, 5000, 6000, [100])
        shutil.rmtree(cg)
        self.w.sample(1005.0, 0, 6000, [])
        self.w.sample(1005.0 + R.SCOPE_END_WAIT_S, 0, 6000, [])     # the journal never had it: logged anyway
        self.assertIn("Result=unknown (no end line in the user journal)", self.logs[-1])
        self.scope_tree()
        self.w.sample(2000.0, 5000, 6000, [100])
        shutil.rmtree(self.w.cgroup_root)
        new = "bg3ai-game-20261104-150000.scope"
        self.scope_tree(unit=new)                                    # the next game's scope, no gap seen
        self.journal = f"{UNIT}: Killed unit cgroup with SIGKILL on client request.\n"
        self.w.sample(2005.0, 5000, 6000, [100])
        self.assertIn(f"game scope {UNIT} ended: Result=unknown", self.logs[-1])
        self.assertIn("journal: Killed unit cgroup", self.logs[-1])
        self.assertEqual(self.w.scope["unit"], new)

    def test_scope_result(self):
        self.assertEqual(R.scope_result(["Consumed 1s CPU time."]), "success")
        self.assertEqual(R.scope_result(["Failed with result 'signal'.", "Consumed 1s CPU time."]), "signal")
        self.assertIsNone(R.scope_result(["Killed unit cgroup with SIGKILL on client request."]))
        no_scope = self.tmp / "p"
        (no_scope / "7").mkdir(parents=True)
        (no_scope / "7" / "cgroup").write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
        self.assertIsNone(R.game_scope_cgroup(7, no_scope, self.tmp))

    def test_lock_single_instance(self):
        a = R.take_lock(self.tmp / "l")
        self.assertIsNotNone(a)
        self.assertIsNone(R.take_lock(self.tmp / "l"))
        a.close()


# A real line of the operator's thermal_log.sh (runtime/thermal.log, 2026-10-17, after the second freeze)
SH_LINE = ("2026-10-17 16:25:39 cpu=51 acpi=51 amdgpu=49 nvme=36 nv=[47,8.09,210,0,0x0000000000000001] "
           "profile=balanced load=0.21 bg3=down")


def hwmon_tree(root: Path, sensors: dict[str, int | None]) -> Path:
    """Fake /sys/class/hwmon: hwmon<i>/name (+ temp1_input in millidegrees unless the value is None)."""
    for i, (name, milli) in enumerate(sensors.items()):
        d = root / f"hwmon{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        if milli is not None:
            (d / "temp1_input").write_text(f"{milli}\n")
    return root


def samples(t0: float, temps: list[tuple[int | None, int | None]], step: float = 5) -> list:
    return [(t0 + i * step, c, g) for i, (c, g) in enumerate(temps)]


class ThermalTest(unittest.TestCase):
    """Temperature lines in thermal_log.sh's format, the heat-pause state, the heat kill (fake hwmon/nvidia-smi)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-test-heat"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.logs = []
        self.w = R.RamWatch(self.logs.append, logfile=self.tmp / "ram.log")
        self.w.thermal_log, self.w.thermal_lock_path = self.tmp / "thermal.log", self.tmp / "thermal_log.lock"
        self.w.heat_kills, self.w.actions, self.w.logs = self.tmp / "heat_kills.jsonl", self.tmp / "a.jsonl", self.tmp
        self.w.limits = R.heat_limits({})
        self.addCleanup(lambda: self.w.thermal_lock and self.w.thermal_lock.close())

    def test_line_has_thermal_log_sh_format(self):
        root = hwmon_tree(self.tmp / "hw", {"nvme": 36850, "k10temp": 51125, "acpitz": 51000, "amdgpu": 49000,
                                             "BAT0": None})
        (self.tmp / "profile").write_text("balanced\n")
        (self.tmp / "loadavg").write_text("0.21 0.30 0.25 1/900 1234\n")
        s = R.thermal_sample(root, self.tmp / "profile", "47,8.09,210,0,0x0000000000000001", self.tmp / "loadavg",
                             game_up=False)
        self.assertEqual(R.thermal_line(s, R.datetime(2026, 10, 17, 16, 25, 39)), SH_LINE)
        self.assertEqual(R.sample_gpu(s), 47)                            # NVIDIA's, not the iGPU's 49

    def test_missing_sensors_and_no_nvidia(self):
        root = hwmon_tree(self.tmp / "hw", {"k10temp": 80000, "amdgpu": None})
        s = R.thermal_sample(root, self.tmp / "none", "", self.tmp / "none", game_up=True)
        self.assertEqual(R.thermal_line(s, R.datetime(2026, 10, 17, 18, 0, 0)),
                         "2026-10-17 18:00:00 cpu=80 acpi=- amdgpu=- nvme=- nv=[] profile= load= bg3=up")
        self.assertIsNone(R.sample_gpu(s))
        self.assertIsNone(R.hwmon_temp("k10temp", self.tmp / "no-such-dir"))
        s = R.thermal_sample(hwmon_tree(self.tmp / "hw2", {"amdgpu": 61000}), nv="", game_up=False)
        self.assertEqual(R.sample_gpu(s), 61)                            # no nvidia-smi: the amdgpu hwmon

    def test_nvidia_query(self):
        ok = mock.Mock(return_value=mock.Mock(returncode=0, stdout="68, 35.02, 1500, 99, 0x0000000000000004\n"))
        self.assertEqual(R.nvidia_query(ok), "68,35.02,1500,99,0x0000000000000004")
        self.assertIn("--format=csv,noheader,nounits", ok.call_args.args[0])
        self.assertEqual(R.nvidia_query(mock.Mock(side_effect=FileNotFoundError("nvidia-smi"))), "")
        self.assertEqual(R.nvidia_query(mock.Mock(side_effect=R.subprocess.TimeoutExpired("nvidia-smi", 5))), "")
        self.assertEqual(R.nvidia_query(mock.Mock(return_value=mock.Mock(returncode=9, stdout="NVIDIA-SMI has failed"))),
                         "")
        s = R.thermal_sample(hwmon_tree(self.tmp / "hw", {"k10temp": 50000}), nv="[N/A],[N/A],0,0,0x0",
                             game_up=False)
        self.assertIsNone(s["nv_temp"])

    def test_parse_both_writers(self):
        t, cpu, gpu = R.parse_thermal(SH_LINE)
        self.assertEqual((t, cpu, gpu), (R.datetime(2026, 10, 17, 16, 25, 39).timestamp(), 51, 47))
        self.assertEqual(R.parse_thermal("2026-10-17 18:00:00 cpu=- acpi=- amdgpu=70 nvme=- nv=[] profile= "
                                         "load= bg3=up")[1:], (None, 70))
        self.assertIsNone(R.parse_thermal("2026-10-17 18:00:00 KILLED bg3 [1] (SIGKILL): too hot"))
        log = self.tmp / "thermal.log"
        self.assertIsNone(R.last_thermal(log))
        log.write_text(SH_LINE + "\ngarbage\n")
        self.assertEqual(R.last_thermal(log)[1], SH_LINE)

    def test_limits_from_env(self):
        self.assertEqual(R.heat_limits({}), {"pause_cpu_c": 92, "pause_gpu_c": 85, "pause_s": 60,
                                             "kill_cpu_c": 97, "kill_gpu_c": 92, "kill_s": 20})
        lim = R.heat_limits({"BG3AI_HEAT_PAUSE_CPU_C": "88", "BG3AI_HEAT_KILL_S": "x"})
        self.assertEqual((lim["pause_cpu_c"], lim["kill_s"]), (88, 20))   # a bad value = the default

    def test_heat_state_needs_the_whole_window(self):
        lim, t0 = R.heat_limits({}), 1_000_000.0
        hot = samples(t0, [(60, 50)] + [(93, 60)] * 13)                  # 93 C from t0+5 to t0+65: 60 s
        h = R.heat_state(hot, t0 + 66, lim)
        self.assertTrue(h["pause"])
        self.assertEqual((h["hot_s"], h["cpu"], h["gpu"], h["cool"]), (60, 93, 60, False))
        self.assertFalse(R.heat_state(hot[:-1], t0 + 61, lim)["pause"])   # 55 s
        gpu = samples(t0, [(70, 86)] * 13)
        self.assertTrue(R.heat_state(gpu, t0 + 61, lim)["pause"])         # the GPU alone
        gap = hot[:6] + [(t + 20, c, g) for t, c, g in hot[6:]]           # a 25 s hole in the log
        self.assertFalse(R.heat_state(gap, gap[-1][0], lim)["pause"])
        self.assertFalse(R.heat_state(hot, t0 + 200, lim)["pause"])       # stale: no pause
        self.assertTrue(R.heat_state([], t0, lim)["cool"])
        cooling = hot + samples(t0 + 70, [(80, 60)] * 12)                 # under the limit for 55 s
        self.assertFalse(R.heat_state(cooling, t0 + 126, lim)["cool"])
        self.assertTrue(R.heat_state(cooling + [(t0 + 130, 79, 60)], t0 + 131, lim)["cool"])

    def test_watchdog_writes_synced_lines_and_yields_to_thermal_log_sh(self):
        s = {"k10temp": 70, "acpitz": 69, "amdgpu": 60, "nvme": 40, "nv": "65,30.1,1500,98,0x0", "nv_temp": 65,
             "profile": "balanced", "load": "6.1", "bg3": True}
        with mock.patch.object(R.os, "fsync") as fs:
            self.assertIsNone(self.w.thermal(1_000_000.0, s, pids=[]))
        fs.assert_called_once()
        self.assertEqual(R.parse_thermal(self.w.thermal_log.read_text().splitlines()[-1])[1:], (70, 65))
        self.w.thermal_lock.close()                                      # thermal_log.sh holds the lock:
        self.w.thermal_lock = None
        other = R.take_lock(self.w.thermal_lock_path)
        self.addCleanup(other.close)
        self.w.thermal(1_000_005.0, s, pids=[])
        self.assertEqual(len(self.w.thermal_log.read_text().splitlines()), 1)   # one writer only
        self.assertEqual(len(self.w.heat), 2)                            # … but the guard still samples

    def test_heat_kill_after_20_s(self):
        hot = {"k10temp": 98, "nv": "70,35,1500,99,0x0", "nv_temp": 70, "bg3": True}
        (self.tmp / "a.jsonl").write_text(R.json.dumps({"t": 1_000_010.0, "tool": "combat_turn", "args": {},
                                                        "ms": 900, "result": "ok"}) + "\n")
        with mock.patch.object(R.os, "kill") as k, mock.patch.object(R.os, "fsync"):
            for i in range(4):                                           # 98 C for 15 s
                self.assertIsNone(self.w.thermal(1_000_000.0 + 5 * i, hot, pids=[100]))
            k.assert_not_called()
            rec = self.w.thermal(1_000_020.0, hot, pids=[100])           # … 20 s
            k.assert_called_once_with(100, R.signal.SIGKILL)
        self.assertEqual((rec["kind"], rec["cpu_c"], rec["seconds"]), ("heat_kill", 98, 20))
        self.assertIn("combat_turn", rec["tools"][0])
        self.assertIn("CPU 98 C / GPU 70 C for 20 s", rec["why"])
        self.assertEqual(R.json.loads((self.tmp / "heat_kills.jsonl").read_text())["kind"], "heat_kill")
        self.assertTrue(any("KILLED bg3 [100]" in m and "too hot" in m for m in self.logs))

    def test_no_heat_kill_without_a_game_or_under_the_limits(self):
        with mock.patch.object(R.os, "kill") as k, mock.patch.object(R.os, "fsync"):
            for i in range(10):                                          # 45 s at 99 C, no game
                self.w.thermal(1_000_000.0 + 5 * i, {"k10temp": 99, "nv_temp": 93}, pids=[])
            for i in range(10):                                          # 45 s just under the kill limits
                self.w.thermal(2_000_000.0 + 5 * i, {"k10temp": 96, "nv_temp": 91}, pids=[100])
        k.assert_not_called()


if __name__ == "__main__":
    unittest.main()
