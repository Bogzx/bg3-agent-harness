"""Offline tests for tools/dev/check_session.py on a synthetic session directory.

Run: python3 -m unittest tools/dev/test_check_session.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_session as C  # noqa: E402

T0 = 1791588670.0


def use(i, name, inp, result, err=False):
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err,
                                                       "content": [{"type": "text", "text": json.dumps({"result": result})}]}]}}]


def write_jsonl(p: Path, recs):
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))


class SessionTest(unittest.TestCase):
    def make(self, good=True):
        d = Path(tempfile.mkdtemp())
        run = d / "sessions" / "claude-run-9"
        run.mkdir(parents=True)
        (d / "logs").mkdir()
        lat = {"start": T0, "end": T0 + 1200, "wrapup_s": 170.0, "enemy_turn_s": 30,
               "api": {"source": "result", "api_s_per_turn": 2.0, "ms_per_out_tok": 12},
               "tool_rt_ms": {"read": {"n": 50, "median": 45.0, "p90": 90.0}},
               "pipelining": {"pct": 20, "dialogue": 3, "enemy_turn": 2, "strict_pct": 15}, "think_overlap_pct": 30,
               "wait_for_noop": {"n": 3, "of": 5, "by": {"already": 3}}}
        if not good:
            for k in ("wrapup_s", "wait_for_noop", "think_overlap_pct"):
                lat.pop(k)
        (run / "S001.latency.json").write_text(json.dumps(lat))
        tx = use(1, "mcp__bg3__get_status", {}, "WHERE Grove" if good else "FAILED: game not ready")
        tx += use(2, "mcp__bg3__walk_toward", {}, "arrived\n(+5 more: props 5; get_nearby / get_frontier)"
                  if good else "arrived — after={hp=7/55 @(1,2,3)}\n  + Candle e9")
        tx += use(3, "ToolSearch", {"query": "x"}, "ok")
        if not good:
            tx += use(4, "mcp__bg3__session_clock", {}, "⏱ 10m left")
            tx += use(5, "ToolSearch", {}, "ok") + use(6, "ToolSearch", {}, "ok")
        write_jsonl(run / "S001.jsonl", tx)
        write_jsonl(run / "S001.turns.jsonl", [{"type": "use", "t": T0 + 5, "name": "get_status"},
                                               {"type": "use", "t": T0 + (12 if good else 90), "name": "walk_toward"}]
                    + ([{"type": "wrapup", "t": T0 + 1030}] if good else []))
        write_jsonl(run / "S001.metrics.jsonl", [
            {"type": "sample", "t": T0 + 1, "combat": True, **({"pturn": True} if good else {})},
            {"type": "tool", "tool": "wait_for", "ms": 120 if good else 4000, "noop": "cannot_happen"},
            {"type": "tool", "tool": "look", "ms": 40, "ops": [["nav.hazards", 5.0 if good else 25.0]]}])
        write_jsonl(d / "actions.jsonl", [{"t": T0 + 600, "tool": "save_game.bg", "save": "x", "ok": good,
                                           "menu_s": 0.8 if good else 4.5, "disk_s": 2.0, "detail": "menus"}])
        (d / "runner.log").write_text(
            "2026-10-10 02:31:09 runner: S001: claude starting (opus)\n"
            + ("" if good else "2026-10-10 02:40:00 runner: S001: agent_hang: silent 4 min\n")
            + "2026-10-10 02:48:40 runner: check_all: 9 pass, 0 warn, 0 fail (19 s)\n"
            "2026-10-10 02:48:42 runner: S002: claude starting (opus)\n"
            "2026-10-10 02:49:00 runner: agent_hang in S002, not ours\n")
        log = d / "logs" / "bg3le-20000101-000000.log.1"
        n = 12 if good else 371
        log.write_bytes(("[ 1.0] noesis: 18 of 18 View input entry points hooked\n"
                         "[ 2.0] noesis: UI generation 2 (StartLoading); 0 queued releases dropped, 0 leaked in all\n"
                         f"[ 3.0] debug: client attached ({n} connected)\n"
                         "[ 4.0] debug: client detached (3 remaining)\n"
                         + ("[ 5.0] noesis: UI generation 3 (Init); 0 queued, 40 parked, 1200 released late, "
                            "20 leaked in all; leaked: Grid 12, TextBlock 8\n" if good else
                            "[ 5.0] noesis: UI generation 3 (Init); 0 queued releases dropped, 900 leaked in all\n")
                         + ("" if good else "[ 9.0] crash: signal 11 at 0x30\n")).encode())
        return d, run

    def verdicts(self, good):
        d, run = self.make(good)
        rows = C.run_checks(C.load(run, "S001"), d)
        return {n: (v, detail) for v, n, detail in rows}

    def test_good_session_passes(self):
        got = self.verdicts(True)
        bad = {n: vd for n, vd in got.items() if vd[0] not in ("PASS", "INFO")}
        self.assertEqual(bad, {})
        self.assertIn("check_all", got["runner"][1])
        self.assertIn("max 12, now 3", got["connections"][1])
        self.assertIn("parked 40, released late 1200; leaked: Grid 12, TextBlock 8", got["ui_leak"][1])

    def test_bad_session_flags_each_item(self):
        got = self.verdicts(False)
        for name in ("first_call", "first_action", "session_clock", "toolsearch", "wait_for_noop", "pipelining",
                     "wrapup", "ops", "pturn", "arrivals", "saves", "runner", "ui_leak"):
            self.assertEqual(got[name][0], "WARN", (name, got[name]))
        self.assertEqual(got["connections"][0], "FAIL")
        self.assertEqual(got["bg3le_crash"][0], "FAIL")
        self.assertIn("1 walk/interact results with the after=", got["arrivals"][1])

    def test_main_latest_and_exit(self):
        d, run = self.make(True)
        out = io.StringIO()
        with mock.patch.object(C, "RUNTIME", d), mock.patch("sys.stdout", out):
            self.assertEqual(C.main(["--run", str(run), "--session", "S001"]), 0)
            self.assertEqual(C.latest(d), (run, "S001"))
        self.assertRegex(out.getvalue(), r"(?m)^PASS wrapup: ")

    def test_no_session(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(C, "RUNTIME", Path(d)), \
                mock.patch("sys.stdout", io.StringIO()):
            (Path(d) / "sessions").mkdir()
            self.assertIsNone(C.latest(Path(d)))


if __name__ == "__main__":
    unittest.main()
