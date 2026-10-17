"""acceptance.py's strict section (S17: strict frame lock across 2 loads + a long rest), offline with a fake game.

    uv run --project bridge python -m unittest tools/dev/test_acceptance_strict.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
import acceptance as A  # noqa: E402


class FakeLock:
    """Ext._Internal.UiStrict/UiSerialize as the client eval sees them; each load and the rest add timeouts."""

    def __init__(self, missing=False):
        self.on, self.refused, self.lua, self.missing = False, 0, 100, missing
        self.evals = []

    def eval(self, code, context="client"):
        self.evals.append(code)
        if self.missing:
            return "missing"
        m = re.search(r"f\((true|false)?\)", code)
        if m and m.group(1):
            self.on = m.group(1) == "true"
        return f"{str(self.on).lower()} {self.refused} {self.lua} 0"

    def step(self):
        self.lua += 2
        if self.on:
            self.refused += 1


class StrictSection(unittest.TestCase):
    def _run(self, lock, loads_ok=True, rest="long_rest: done — rested (full)", alive=True, crash=""):
        got, calls = [], []

        def load(label, record=False):
            calls.append(("load_game", label, lock.on))
            lock.step()
            return loads_ok

        def took(name, **kw):
            calls.append((name, kw, lock.on))
            if name == "long_rest":
                lock.step()
                return rest, 9.0
            return "camp: done", 0.4

        fake_s = mock.Mock()
        fake_s.game.eval = lock.eval
        with mock.patch.object(A, "S", fake_s), mock.patch.object(A, "load_verified", load), \
                mock.patch.object(A, "took", took), mock.patch.object(A, "alive", lambda: alive), \
                mock.patch.object(A, "bg3le_log", lambda: None), mock.patch.object(A, "grep_crash", lambda lg: crash), \
                mock.patch.object(A.time, "sleep", lambda s: None), mock.patch.object(A, "log", lambda m: None), \
                mock.patch.object(A, "item", lambda *a: got.append(a)):
            A.sec_strict()
        return got, calls

    def test_on_across_two_loads_and_a_rest_then_restored(self):
        lock = FakeLock()
        got, calls = self._run(lock)
        self.assertEqual([c[0] for c in calls], ["load_game", "load_game", "camp", "long_rest"])
        self.assertTrue(all(c[2] for c in calls))                     # strict was on for every step
        self.assertEqual([c[1] for c in calls[:2]], [A.BASE, A.HOTBAR_SAVE])
        self.assertFalse(lock.on)                                     # restored to what it was
        name, verdict, ev = got[0]
        self.assertEqual((name, verdict), (A.STRICT_ITEM, "PASS"))
        self.assertIn(f"load {A.BASE}: ok (refused +1 lua_timeouts +2 ui_timeouts +0)", ev)
        self.assertIn("total refused +3 lua_timeouts +6 ui_timeouts +0", ev)

    def test_restored_after_a_failed_load_and_the_item_fails(self):
        lock = FakeLock()
        got, calls = self._run(lock, loads_ok=False)
        self.assertEqual([c[0] for c in calls], ["load_game"])
        self.assertFalse(lock.on)
        self.assertEqual(got[0][1], "FAIL")

    def test_rest_failure_and_crash_fail(self):
        self.assertEqual(self._run(FakeLock(), rest="long_rest: FAILED — no")[0][0][1], "FAIL")
        got, _ = self._run(FakeLock(), crash="crash: signal 11 at (nil)")
        self.assertEqual(got[0][1], "FAIL")
        self.assertIn("crash: signal 11", got[0][2])

    def test_partial_rest_retried(self):
        lock = FakeLock()
        _, calls = self._run(lock, rest="long_rest: FAILED — not enough supplies; partial=true rests anyway")
        self.assertEqual([c for c in calls if c[0] == "long_rest"][-1][1], {"partial": True})

    def test_skipped_without_patch_0010(self):
        got, calls = self._run(FakeLock(missing=True))
        self.assertEqual(calls, [])
        self.assertEqual(got[0][1], "SKIP")

    def test_registered_last(self):
        self.assertEqual(A.SECTIONS[-1], "strict")
        self.assertIs(A.FUNCS["strict"], A.sec_strict)
        self.assertEqual(A.SECTION_OWNERS["strict"], "setup")


if __name__ == "__main__":
    unittest.main()
