"""Offline tests for tools/dev/probe_revive_replay.py (no game needed): the calls it reads, the job id map, the forced
two-click revive and the control line.

Run: uv run pytest -q tools/dev/test_probe_revive_replay.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_revive_replay as replay
import probe_revive_stuck as probe


def events(rows: list[dict]) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "S001.events.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


class ReplayTest(unittest.TestCase):
    def test_load_calls_window_results_and_skips(self):
        t = replay.clock("16:47:38")
        p = events(
            [
                {"kind": "tool_call", "t": t - 100, "tool": "get_status", "args": {}, "call_id": "a"},
                {
                    "kind": "tool_call",
                    "t": t,
                    "tool": "use_ability",
                    "args": {"ability": "Cure Wounds"},
                    "call_id": "b",
                },
                {"kind": "tool_call", "t": t + 0.022, "tool": "short_rest", "call_id": "c"},
                {"kind": "tool_call", "t": t + 1, "tool": "memory_write", "args": {"x": 1}, "call_id": "d"},
                {"kind": "tool_result", "t": t + 3.6, "text": "short_rest: done", "call_id": "c"},
                {"kind": "tool_result", "t": t + 3.7, "text": "use_ability: done", "call_id": "b"},
                {"kind": "tool_call", "t": t + 999, "tool": "get_status", "args": {}, "call_id": "e"},
            ]
        )
        calls = replay.load_calls(p, t - 1, t + 10)
        self.assertEqual([c["tool"] for c in calls], ["use_ability", "short_rest"])  # in window, no memory_*
        self.assertEqual(calls[1]["args"], {})
        self.assertEqual(calls[0]["job"], "use_ability: done")
        self.assertEqual(len(replay.load_calls(p, None, t + 10)), 3)  # 'session': from the start

    def test_job_map_learns_and_offsets(self):
        jobs = replay.JobMap()
        self.assertEqual(jobs.map("j63"), "j63")  # nothing known yet
        jobs.learn("walk_toward: started j62, ETA 4.3s", "move_to: started j5, ETA 3s")
        self.assertEqual(jobs.map("j62"), "j5")
        self.assertEqual(jobs.map("j63"), "j6")  # the reader's select, never printed: by its offset
        self.assertEqual(jobs.map("queue"), "queue")
        self.assertEqual(
            replay.mapped_args({"tool": "wait_for", "args": {"target": "j63", "timeout_s": 20}}, jobs),
            {"target": "j6", "timeout_s": 20},
        )

    def test_job_map_waits_for_an_id_a_running_call_prints(self):
        # 16:46:31: wait_for("j55") went out 0.16 s after the walk_toward that started j55, before it returned
        jobs = replay.JobMap([{"job": "walk_toward: started j55, ETA 1.3s"}], wait_s=2.0)
        threading.Timer(0.1, jobs.learn, ("walk_toward: started j55", "walk_toward: started j9")).start()
        t0 = time.monotonic()
        self.assertEqual(jobs.map("j55"), "j9")
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_two_click_revive_clicks_by_the_reader_first(self):
        class Run:
            def spots(self):
                return [[1.0, 0.0, 1.0]]

        twostep = SimpleNamespace(_Run=Run, ground_ok=lambda gc: (False, "a character is under the cursor"))
        replay.force_two_click_revive(twostep, lambda game, op, args: {"world": [264.0, 0.2, 205.1]})
        run = SimpleNamespace(landing=None, game=None, caster="sh")
        self.assertEqual(twostep._Run.spots(run), [[264.2, 0.43, 205.57], [1.0, 0.0, 1.0]])  # the job's click 1
        self.assertEqual(twostep.ground_ok({}), (True, None))  # clicked whatever the cursor reads
        self.assertEqual(twostep.ground_ok({})[0], False)  # only that once
        jump = SimpleNamespace(landing=[1, 2, 3], game=None, caster="sh")
        self.assertEqual(twostep._Run.spots(jump), [[1.0, 0.0, 1.0]])  # a landing spell: untouched

    def test_control_line(self):
        rows = [
            {"uuid": "tav-uuid", "name": "Tav", "resurrect": True, "resurrected": True, "osi": 1},
            {"uuid": "sh-uuid", "name": "Shadowheart", "client_control": True, "character_control": True, "osi": 0},
        ]
        self.assertEqual(
            probe.control_line(rows, "sh-uuid"),
            "hud=Shadowheart | Tav: resurrect,resurrected osi=1 | Shadowheart: client_control,character_control osi=0",
        )


if __name__ == "__main__":
    unittest.main()
