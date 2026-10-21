"""acceptance.py game_memory: the game's memory per section for live_window's summary (S16, L8's 10G scope peak).

    python3 -m unittest tools/dev/test_acceptance_memory.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import core  # noqa: E402

SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/bg3ai-game-20261017-223018.scope"


class GameMemory(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.proc, self.cg = Path(d.name, "proc"), Path(d.name, "cgroup")

    def pid(self, pid: int, comm: str, rss_kb: int, swap_kb: int, cg: str = SCOPE) -> None:
        p = self.proc / str(pid)
        p.mkdir(parents=True)
        (p / "comm").write_text(comm + "\n")
        (p / "status").write_text(f"Name:\t{comm}\nVmRSS:\t  {rss_kb} kB\nVmSwap:\t  {swap_kb} kB\n")
        (p / "cgroup").write_text(f"0::{cg}\n")

    def test_no_game(self):
        self.proc.mkdir()
        (self.proc / "self").mkdir()
        self.pid(10, "steam", 100, 0)
        self.assertEqual(core.game_memory(self.proc, self.cg), {})

    def test_rss_and_scope(self):
        # the L8 window game at its stop: 10G scope peak (MemoryMax), 990 MB swap, while the RSS was ~5.4 GB
        self.pid(4242, "bg3", 5_400 * 1024, 900 * 1024)
        self.pid(4243, "bg3", 100 * 1024, 90 * 1024)   # a second bg3 process in the same scope: summed
        self.pid(77, "bash", 9_999 * 1024, 0)
        scope = self.cg / SCOPE.lstrip("/")
        scope.mkdir(parents=True)
        mb = 2**20
        for f, v in (("memory.current", 9_800), ("memory.peak", 10_240), ("memory.swap.current", 990),
                     ("memory.swap.peak", 991)):
            (scope / f).write_text(f"{v * mb}\n")
        (scope / "memory.stat").write_text(f"anon {5_300 * mb}\nfile {4_400 * mb}\nkernel {50 * mb}\n")
        self.assertEqual(core.game_memory(self.proc, self.cg),
                         {"rss": 5_500, "swap": 990, "scope": "bg3ai-game-20261017-223018.scope",
                          "cg_current": 9_800, "cg_peak": 10_240, "cg_swap": 990, "cg_swap_peak": 991,
                          "anon": 5_300, "file": 4_400})

    def test_not_in_a_game_scope(self):
        # BG3AI_GAME_SCOPE=0: the caller's cgroup is not the game's; RSS only
        self.pid(4242, "bg3", 2048, 0, cg="/user.slice/user-1000.slice/session-2.scope")
        self.assertEqual(core.game_memory(self.proc, self.cg), {"rss": 2, "swap": 0})


if __name__ == "__main__":
    unittest.main()
