"""Offline tests for tools/dev/probe_standup.py (no game needed): the wrapped _end_standup's order of reads and walks.

Run: uv run pytest -q tools/dev/test_probe_standup.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_standup as su


class FakeLua:
    """Answers the probe's Lua by which snippet it is; `moved` = how far end_standup's own walk took the member."""

    def __init__(self, moved: float):
        self.moved, self.sent = moved, []

    def __call__(self, code: str) -> str:
        if code == su.SAMPLER % su.probe.lua_str("body"):
            self.sent.append("sampler")
            return "sampling body"
        if code == su.NOW:
            self.sent.append("now")
            return json.dumps({"t": 9100, "first": [0, 0, 0], "last": [self.moved, 0, 0]})
        if code == su.STOP:
            self.sent.append("stop")
            return json.dumps({"samples": [{"t": 0}], "calls": [{"name": "CharacterMoveToPosition"}]})
        if "bg3ai_revive_probe_mv" in code:
            self.sent.append("raw_move")
            return json.dumps({"uuid": "body", "start": [0, 0, 0], "ok": True, "tasks": 1})
        self.sent.append("after")
        return json.dumps({"pos": [2.98, 0, 0], "tasks": 0, "compact": "osi=0"})


class WrapTest(unittest.TestCase):
    def run_probe(self, moved: float):
        lua, naps = FakeLua(moved), []
        out = Path(tempfile.mkdtemp()) / "su.jsonl"
        f = su.wrap(lambda r, b: f"; {b} stood up", lua, out, raw_secs=6.0, sleep=naps.append)
        reply = f("reader", "body")
        return reply, lua.sent, naps, json.loads(out.read_text().splitlines()[0])

    def test_no_step_runs_the_raw_walk_after_end_standup(self):
        reply, sent, naps, rec = self.run_probe(0.0)
        self.assertEqual(reply, "; body stood up")  # the agent's reply is end_standup's own
        self.assertEqual(sent, ["sampler", "now", "raw_move", "after", "stop"])
        self.assertEqual(naps, [6.0])
        self.assertEqual(rec["end_standup"]["moved_m"], 0.0)
        self.assertEqual(rec["raw_move"]["moved_m"], 2.98)
        self.assertEqual(rec["raw_move"]["at_ms"], 9100)
        self.assertEqual(rec["calls"], [{"name": "CharacterMoveToPosition"}])

    def test_the_sampler_is_stopped_when_end_standup_or_a_read_raises(self):
        """App review of #37: the tick sampler and the G.osi wrapper must not outlive a failed probe."""
        out = Path(tempfile.mkdtemp()) / "su.jsonl"
        lua = FakeLua(0.0)

        def boom(r, b):
            raise RuntimeError("bridge down")

        with self.assertRaises(RuntimeError):
            su.wrap(boom, lua, out, sleep=lambda s: None)("reader", "body")
        self.assertEqual(lua.sent, ["sampler", "stop"])
        lua2 = FakeLua(0.0)
        real = lua2.__call__

        def bad_now(code):
            if code == su.NOW:
                raise RuntimeError("debugger timeout")
            return real(code)

        reply = su.wrap(lambda r, b: "; ok", bad_now, out, sleep=lambda s: None)("reader", "body")
        self.assertEqual((reply, lua2.sent), ("; ok", ["sampler", "stop"]))
        rec = json.loads(out.read_text().splitlines()[-1])
        self.assertEqual(rec["probe_error"], "debugger timeout")

    def test_arm_b_casts_on_the_member_during_end_standup_and_records_it(self):
        """night2: arm B sends the spell CAST_AFTER_S into end_standup on its own thread; the record says when (the
        sampler's clock), what it replied and how long it took, and the sampler stops only after it ended."""
        lua, naps, cast_done = FakeLua(0.0), [], threading.Event()
        out = Path(tempfile.mkdtemp()) / "su.jsonl"

        def cure() -> str:
            cast_done.set()
            return "use_ability: done — cast=Target_CureWounds target={e2}"

        def end_standup(r, b):  # the stand-up job runs while the spell is cast (N12: ~5 s into it)
            self.assertTrue(cast_done.wait(5), "the cast ran during end_standup")
            return "; body took no step"

        f = su.wrap(end_standup, lua, out, raw_secs=6.0, sleep=naps.append, during=cure, cast_after=2.5)
        self.assertEqual(f("reader", "body"), "; body took no step")
        rec = json.loads(out.read_text().splitlines()[0])
        self.assertEqual(rec["arm"], "B")
        self.assertEqual(rec["cast"]["after_s"], 2.5)
        self.assertEqual(rec["cast"]["at_ms"], 9100)
        self.assertEqual(rec["cast"]["reply"], "use_ability: done — cast=Target_CureWounds target={e2}")
        self.assertIn("took_s", rec["cast"])
        self.assertEqual(naps, [2.5, 6.0])  # the cast's delay, then the raw walk's
        self.assertEqual(lua.sent, ["sampler", "now", "now", "raw_move", "after", "stop"])

    def test_arm_a_casts_nothing(self):
        _, _, naps, rec = self.run_probe(0.0)
        self.assertEqual(rec["arm"], "A")
        self.assertNotIn("cast", rec)
        self.assertEqual(naps, [6.0])

    def test_a_step_needs_no_raw_walk(self):
        _, sent, naps, rec = self.run_probe(2.5)
        self.assertEqual(sent, ["sampler", "now", "stop"])
        self.assertEqual(naps, [])
        self.assertNotIn("raw_move", rec)


if __name__ == "__main__":
    unittest.main()
