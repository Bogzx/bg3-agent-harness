"""Offline tests: background saves (saver.py). save_game returns at once, the menus run in a thread while
the next tool call waits for them, the write is confirmed on disk in the background (SAVED / SAVE FAILED on a
later result), and a save the game can't take now is queued for the next moment between tool calls.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import saver, server  # noqa: E402

# Captured at import (test discovery, before any test runs). Other modules patch the global time.sleep to a
# no-op for speed; if one ever leaks that patch, these timing tests would see menus that take no time at all.
REAL_SLEEP = time.sleep


def real_time(case: unittest.TestCase) -> None:
    if time.sleep is not REAL_SLEEP:
        leaked = time.sleep
        time.sleep = REAL_SLEEP
        case.addCleanup(setattr, time, "sleep", leaked)


def write_save(root: Path, label: str, n: int = 1) -> Path:
    d = root / f"Claude-{n}__{label}"
    d.mkdir(exist_ok=True)
    (d / f"{label}.lsv").write_bytes(b"x" * 100)  # the game's layout: the .lsv is named after the label
    (d / f"{label}.WebP").write_bytes(b"x")
    return d


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class Rig:
    """A fake game for the saver: menus take menu_s, then the save folder appears write_s later."""

    def __init__(self, menu_s=0.2, write_s=0.1, menu_ok=True, writes=True):
        self.dir = Path(tempfile.mkdtemp(prefix="bg3ai-saves-"))
        atexit.register(shutil.rmtree, self.dir, True)
        self.menu_s, self.write_s, self.menu_ok, self.writes = menu_s, write_s, menu_ok, writes
        self.can = True
        self.menus: list[str] = []
        self.n = 0

    def run_menu(self, label):
        self.menus.append(label)
        time.sleep(self.menu_s)
        if self.menu_ok and self.writes:
            self.n += 1
            n = self.n
            threading.Timer(self.write_s, lambda: write_save(self.dir, label or "QuickSave_1", n)).start()
        return self.menu_ok, "menus new=10 accept=300 done=600" if self.menu_ok else "save page did not open"

    def can_save(self):
        return (True, "") if self.can else (False, "dialogue on screen")

    def saver(self):
        return saver.Saver(self.run_menu, self.can_save, lambda: self.dir)


class SaverTest(unittest.TestCase):
    def setUp(self):
        real_time(self)
        saved = (saver.DISK_WAIT_S, saver.DEFER_POLL_S, saver.STABLE_S)
        saver.DISK_WAIT_S, saver.DEFER_POLL_S, saver.STABLE_S = 2.0, 0.05, 0.05
        self.addCleanup(lambda: setattr(saver, "DISK_WAIT_S", saved[0]) or setattr(saver, "DEFER_POLL_S", saved[1])
                        or setattr(saver, "STABLE_S", saved[2]))

    def test_returns_at_once_and_the_next_call_waits_for_the_menus(self):
        rig = Rig(menu_s=0.3)
        s = rig.saver()
        s.enter()  # inside the save_game tool
        t0 = time.monotonic()
        out = s.save("S010-pre-kagha")
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertIn("saving 'S010-pre-kagha' in the background", out)
        s.leave()
        waited = s.enter()  # the agent's next tool
        s.leave()
        self.assertGreater(waited, 0.15)  # it waited for the menus
        self.assertLess(waited, 0.6)      # but not for the write
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertIn("SAVED 'S010-pre-kagha'", s.take_notes())
        self.assertEqual(s.take_notes(), "")  # shown once

    def test_tools_after_the_menus_dont_wait_for_the_write(self):
        rig = Rig(menu_s=0.05, write_s=0.5)
        s = rig.saver()
        s.save("x")
        time.sleep(0.1)
        self.assertLess(s.enter(), 0.01)
        s.leave()
        self.assertIn("writing", s.status())
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertIn("SAVED 'x'", s.take_notes())

    def test_queued_until_the_game_can_save_and_no_tool_runs(self):
        rig = Rig(menu_s=0.05)
        rig.can = False
        s = rig.saver()
        out = s.save("S010-end")
        self.assertIn("queued 'S010-end'", out)
        self.assertIn("dialogue on screen", out)
        self.assertLess(s.enter(), 0.01)  # nothing on screen: tools run freely
        rig.can = True
        time.sleep(0.2)
        self.assertEqual(rig.menus, [])   # a tool is running: the save waits for the gap
        s.leave()
        self.assertTrue(wait_until(lambda: rig.menus == ["S010-end"]))
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertIn("SAVED 'S010-end'", s.take_notes())

    def test_menu_failure_is_reported_and_frees_the_screen(self):
        rig = Rig(menu_ok=False)
        s = rig.saver()
        s.save("x")
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertFalse(s.ui_busy)
        self.assertIn("SAVE FAILED 'x': save page did not open; call save_game again", s.take_notes())

    def test_a_conversation_during_the_menus_queues_it_once_more(self):
        # S067 #77: Orin died, the Emperor's conversation took the screen at the menus' accept step: SAVE FAILED
        rig = Rig(menu_s=0.05)
        real = rig.run_menu
        tries = []

        def run_menu(label):
            tries.append(label)
            if len(tries) == 1:
                rig.can = False                  # the conversation opens mid-menu
                return False, "in-game RETURN did not advance 'accept'"
            return real(label)
        s = saver.Saver(run_menu, rig.can_save, lambda: rig.dir)
        s.save("S067-orin-dead")
        self.assertTrue(wait_until(lambda: s.pending is not None and s.pending["phase"] == "queued"))
        self.assertIn("the save menus were interrupted (dialogue on screen); queued", s.take_notes())
        self.assertEqual(tries, ["S067-orin-dead"])
        rig.can = True                           # the conversation ended
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertEqual(tries, ["S067-orin-dead"] * 2)
        self.assertIn("SAVED 'S067-orin-dead'", s.take_notes())

    def test_a_second_menu_failure_is_reported(self):
        rig = Rig(menu_ok=False)

        def run_menu(label):
            rig.can = False
            return False, "save page did not open"
        s = saver.Saver(run_menu, rig.can_save, lambda: rig.dir)
        s.save("x")
        self.assertTrue(wait_until(lambda: s.pending is not None and s.pending["phase"] == "queued"))
        rig.can = True
        self.assertTrue(wait_until(lambda: s.pending is None))
        self.assertIn("SAVE FAILED 'x': save page did not open; call save_game again", s.take_notes())

    def test_no_folder_on_disk_is_a_failure(self):
        saver.DISK_WAIT_S = 0.3
        rig = Rig(writes=False, menu_s=0.01)
        s = rig.saver()
        s.save("x")
        self.assertTrue(wait_until(lambda: s.pending is None))
        note = s.take_notes()
        self.assertIn("SAVE FAILED 'x': no save folder appeared", note)
        self.assertIn("On disk: no folder *__x in", note)

    def test_s017_folder_lsv_layout_counts_and_a_miss_says_what_is_there(self):
        # S017: 'Claude-25101260926__S017-pre-dror/S017-pre-dror.lsv' was on disk but the check looked for
        # '<folder>.lsv' and reported 4/4 saves lost
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        t = time.time()
        d = root / "Claude-25101260926__S017-pre-dror"
        d.mkdir()
        (d / "S017-pre-dror.lsv").write_bytes(b"x" * 100)
        (d / "S017-pre-dror.WebP").write_bytes(b"x")
        self.assertEqual(saver.written_save(root, "S017-pre-dror", t), d)
        seen = saver.on_disk(root, "S017-pre-dror", t + 50)
        self.assertIn("Claude-25101260926__S017-pre-dror: [S017-pre-dror.WebP 1B -50s, S017-pre-dror.lsv 100B -50s]", seen)
        self.assertIn("save dir missing", saver.on_disk(root / "nope", "x", t))

    def test_stray_save_under_the_default_name(self):
        # S052 05:21: 'S052-lab-fight2' was written as '…__Steel Watch Foundry - Lab Level - 16h 14m'
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        t = time.time()
        old = root / "Claude-1__S052-lab-fight"
        old.mkdir()
        (old / "S052-lab-fight.lsv").write_bytes(b"x")
        os.utime(old / "S052-lab-fight.lsv", (t - 100, t - 100))
        q = root / "Claude-2__QuickSave_1"
        q.mkdir()
        (q / "QuickSave_1.lsv").write_bytes(b"x")
        self.assertIsNone(saver.stray_save(root, "S052-lab-fight2", t))   # older saves and quicksaves don't count
        d = root / "Claude-21111262918__Steel Watch Foundry - Lab Level - 16h 14m"
        d.mkdir()
        (d / "Steel Watch Foundry - Lab Level - 16h 14m.lsv").write_bytes(b"x" * 10)
        self.assertEqual(saver.stray_save(root, "S052-lab-fight2", t), d)
        self.assertIsNone(saver.stray_save(root, "", t))

    def test_overwrite_of_an_existing_save_is_named(self):
        # audit UI-6: with a save row selected the game wrote over that save; this was reported as a good save
        # "under another name"
        saver.DISK_WAIT_S = 0.4
        rig = Rig(writes=False, menu_s=0.01)
        old = rig.dir / "Claude-7__S010-pre-kagha"
        old.mkdir()
        (old / "S010-pre-kagha.lsv").write_bytes(b"x")
        os.utime(old / "S010-pre-kagha.lsv", (time.time() - 500, time.time() - 500))

        def overwrite(label):
            rig.menus.append(label)
            (old / "S010-pre-kagha.lsv").write_bytes(b"y" * 50)  # the game rewrites the selected save's folder
            return True, "menus new=10 accept=300 done=600"
        rig.run_menu = overwrite
        logged = []
        s = saver.Saver(rig.run_menu, rig.can_save, lambda: rig.dir, logged.append)
        s.save("S100-new")
        self.assertTrue(wait_until(lambda: s.pending is None, 5))
        note = s.take_notes()
        self.assertIn("OVERWROTE AN EXISTING SAVE", note)
        self.assertIn("'S010-pre-kagha'", note)
        self.assertNotIn("good save", note)
        self.assertTrue(any(r.get("overwrote") == old.name for r in logged), logged)

    def test_one_save_at_a_time(self):
        rig = Rig(menu_s=0.3)
        s = rig.saver()
        s.save("a")
        self.assertIn("already being saved", s.save("a"))
        self.assertIn("REFUSED", s.save("b"))
        self.assertTrue(wait_until(lambda: s.pending is None))

    def test_settle_drops_a_queued_save(self):
        rig = Rig()
        rig.can = False
        s = rig.saver()
        s.save("x")
        s.enter()  # load_game runs inside a tool
        t0 = time.monotonic()
        note = s.settle(timeout=3)
        s.leave()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("dropped", note)
        self.assertIsNone(s.pending)
        self.assertEqual(rig.menus, [])

    def test_sync_save_waits_for_the_disk(self):
        rig = Rig(menu_s=0.05, write_s=0.1)
        s = rig.saver()
        out = s.save("S010-end", sync=True)
        self.assertIn("SAVED 'S010-end'", out)
        self.assertIsNone(s.pending)

    def test_written_save_matches_label_quicksave_and_time(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        old = write_save(root, "S009-end", 1)
        os.utime(old / "S009-end.lsv", (time.time() - 100, time.time() - 100))
        t = time.time()
        self.assertIsNone(saver.written_save(root, "S009-end", t))  # the old one doesn't count
        new = write_save(root, "S009-end", 2)
        self.assertEqual(saver.written_save(root, "S009-end", t), new)
        self.assertIsNone(saver.written_save(root, "end", t))
        q = write_save(root, "QuickSave_3", 3)
        self.assertEqual(saver.written_save(root, "", t), q)


class ServerSave(Base):
    def setUp(self):
        super().setUp()
        real_time(self)
        self.rig = Rig(menu_s=0.2)
        saved = (server.SAVER, server._enable_save_gates, saver.STABLE_S)
        server.SAVER = self.rig.saver()
        server._enable_save_gates = lambda: None
        saver.STABLE_S = 0.05
        self.addCleanup(self._undo, saved)

    def _undo(self, saved):
        server.SAVER.settle(3)
        server.SAVER, server._enable_save_gates, saver.STABLE_S = saved

    def test_save_game_returns_at_once_and_a_later_result_says_saved(self):
        t0 = time.monotonic()
        out = server.save_game("S010-pre.kagha!")
        self.assertLess(time.monotonic() - t0, 0.15)
        self.assertIn("saving 'S010-prekagha' in the background", out)  # the mod's name filter, no dots
        t1 = time.monotonic()
        server.session_clock()  # ungated: doesn't wait for the menus
        self.assertLess(time.monotonic() - t1, 0.1)
        self.assertTrue(wait_until(lambda: server.SAVER.pending is None))
        later = server.wait_for("idle", timeout_s=0.1)
        self.assertIn("SAVED 'S010-prekagha'", later)

    def test_a_gated_tool_waits_for_the_menus(self):
        server.save_game("x")
        t0 = time.monotonic()
        server.wait_for("idle", timeout_s=0.1)
        self.assertGreater(time.monotonic() - t0, 0.1)  # waited for the ~0.2 s of menus
        self.assertEqual(self.rig.menus, ["x"])


if __name__ == "__main__":
    unittest.main()
