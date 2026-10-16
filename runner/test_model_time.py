#!/usr/bin/env python3
"""Unit tests for runner/model_time.py: the span arithmetic, model messages (trigger, TTFT from the thinking timer),
the harness / model / overlap / other split, token scaling to the result event's exact totals, the speed and the
API-adjusted model time, old turns files without message ids, resumed sessions' cumulative API time, the idle gaps
(in step with bg3ai.metrics.gaps), the run window and the CLI. Fixture sessions only; never launches the game.

    python3 -m unittest runner/test_model_time.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_time as MT  # noqa: E402
from model_time import M  # noqa: E402

T0 = 1_000_000.0
REAL = Path(__file__).resolve().parents[1] / "runtime" / "sessions" / "naut"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def _asst(mid: str, t: float, content: list, think_ms: float | None = None) -> dict:
    ev = {"type": "assistant", "timestamp": _iso(T0 + t),
          "message": {"id": mid, "content": content, "usage": {"output_tokens": 1}}}
    if think_ms:
        ev["thinking_duration_ms"] = think_ms
    return ev


def _tu(i: str, name: str, inp: dict | None = None) -> dict:
    return {"type": "tool_use", "id": i, "name": f"mcp__bg3__{name}", "input": inp or {}}


def _tr(t: float, i: str) -> dict:
    return {"type": "user", "timestamp": _iso(T0 + t),
            "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"}]}}


def _think_tok(n: int) -> dict:
    return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": n}


def _result(api_ms: float, out: int, think: int, wall_ms: float = 60_000) -> dict:
    return {"type": "result", "duration_api_ms": api_ms, "duration_ms": wall_ms, "num_turns": 4,
            "usage": {"output_tokens": out, "output_tokens_details": {"thinking_tokens": think}}}


THINK = {"type": "thinking", "thinking": ""}   # redacted, as in the real streams


def scenario(result: dict | None = None) -> list[dict]:
    """m1 (first: no trigger) look [0,1]; m2 thinks 1.5 s (first block at 3.0: TTFT 0.5) then move_to [3.5,6];
    m3 get_nearby [7,7.2] while it still streams look (7.4) [7.4,8]; m4 text at 9. model: 2.5 + 1.4 + 1.0 = 4.9 s;
    harness: 1 + 2.5 + 0.2 + 0.6 = 4.3 s; overlap 0.2 s (get_nearby inside m3's [6, 7.4])."""
    evs = [_asst("m1", 0.0, [_tu("u1", "look")]), _tr(1.0, "u1"),
           _think_tok(50), _think_tok(100),
           _asst("m2", 3.0, [THINK], think_ms=1500), _asst("m2", 3.5, [_tu("u2", "move_to", {"x": 1})]),
           _tr(6.0, "u2"),
           _asst("m3", 7.0, [_tu("u3", "get_nearby")]), _tr(7.2, "u3"), _asst("m3", 7.4, [_tu("u4", "look")]),
           _tr(8.0, "u4"),
           _asst("m4", 9.0, [{"type": "text", "text": "x" * 70}])]
    return evs + ([result] if result else [])


def write_session(d: Path, name: str, events: list[dict], turns: bool = True, metrics: list[dict] | None = None):
    """The stream, and (turns=True) the runner's turns file from the same events at their timestamps."""
    (d / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    if turns:
        recs = MT.stream_info(d / f"{name}.jsonl")["records"]
        (d / f"{name}.turns.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    if metrics is not None:
        (d / f"{name}.metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in metrics))


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bg3ai-mt-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Spans(unittest.TestCase):
    def test_union_intersect_subtract(self):
        self.assertEqual(MT._union([(3, 4), (0, 1), (0.5, 2), (2, 2)]), [(0, 2), (3, 4)])
        self.assertEqual(MT._intersect([(0, 2), (3, 5)], [(1, 4)]), [(1, 2), (3, 4)])
        self.assertEqual(MT._subtract([(0, 10)], [(2, 3), (2.5, 4), (9, 12)]), [(0, 2), (4, 9)])
        self.assertAlmostEqual(MT._length([(0, 2), (1, 3)]), 3)

    def test_split_four_ways(self):
        s = MT.split([(0, 4)], [(3, 6)], 0, 10)
        self.assertEqual(s, {"harness_only_s": 2, "model_only_s": 3, "overlap_s": 1, "other_s": 4})
        clipped = MT.split([(0, 4)], [(3, 6)], 3.5, 5)
        self.assertEqual(clipped, {"harness_only_s": 1, "model_only_s": 0, "overlap_s": 0.5, "other_s": 0})


class Messages(unittest.TestCase):
    def test_trigger_ttft_and_grouping(self):
        with tempfile.TemporaryDirectory() as d:
            write_session(Path(d), "S001", scenario(), turns=False)
            info = MT.stream_info(Path(d) / "S001.jsonl")
            msgs = MT.model_turns(info["records"], info["think_ms"])
        self.assertEqual([m["mid"] for m in msgs], ["m1", "m2", "m3", "m4"])
        m1, m2, m3, m4 = msgs
        self.assertIsNone(m1["trigger"])                          # its prompt time is not recorded
        self.assertAlmostEqual(m2["trigger"] - T0, 1.0, places=3)
        self.assertAlmostEqual(m2["t_last"] - T0, 3.5, places=3)
        self.assertAlmostEqual(m2["think_s"], 1.5)
        self.assertAlmostEqual(m2["ttft_s"], 0.5, places=3)       # 3.0 - 1.0 - 1.5
        self.assertEqual(m2["think_est"], 100)                    # the CLI's running estimate, max before m2
        self.assertAlmostEqual(m3["trigger"] - T0, 6.0, places=3)
        self.assertAlmostEqual(m4["trigger"] - T0, 8.0, places=3)  # after m3's last result, not its first
        self.assertNotIn("ttft_s", m3)

    def test_trigger_never_before_previous_message_end(self):
        turns = [{"type": "assistant", "t": 0, "mid": "a"}, {"type": "use", "t": 0, "id": "x", "name": "look"},
                 {"type": "result", "t": 0.5, "id": "x"}, {"type": "assistant", "t": 2, "mid": "a", "est": 5},
                 {"type": "assistant", "t": 3, "mid": "b", "est": 1}]
        b = MT.model_turns(turns)[1]
        self.assertEqual(b["trigger"], 2)   # the result came back while "a" still streamed


class Session(TmpDir):
    def test_split_tokens_speed_adjustment(self):
        write_session(self.dir, "S001", scenario(_result(api_ms=4000, out=400, think=200)))
        s = MT.analyze_session("S001", self.dir / "S001.jsonl")
        self.assertAlmostEqual(s["model_s"], 4.9, places=2)
        self.assertAlmostEqual(s["harness_s"], 4.3, places=2)
        sp = s["split"]
        self.assertAlmostEqual(sp["overlap_s"], 0.2, places=2)
        self.assertAlmostEqual(sp["harness_only_s"], 4.1, places=2)
        self.assertAlmostEqual(sp["model_only_s"], 4.7, places=2)
        self.assertAlmostEqual(sp["other_s"], 0.0, places=2)
        # tokens: every message sums to the exact 400, thinking (m2's 100 estimated) to the exact 200
        self.assertEqual(s["tokens_source"], "exact_scaled")
        self.assertAlmostEqual(sum(m["tok"] for m in s["msgs"]), 400, places=6)
        m2 = s["msgs"][1]
        self.assertAlmostEqual(m2["think_est"] * 2.0, 200)
        # speed: 400 tok / (4 s API - 4 messages x 0.5 s TTFT) = 200 tok/s
        self.assertEqual(s["speed_source"], "api")
        self.assertAlmostEqual(s["tps"], 200.0, places=3)
        r = MT.report([s])
        tok = s["tok"]
        gen = tok / 200.0
        self.assertAlmostEqual(r["gen_s"], round(gen, 2), places=2)
        self.assertAlmostEqual(r["adjusted_model_s"], round(4.9 - gen + tok / MT.REF_OUT_TOK_S, 2), places=2)
        self.assertAlmostEqual(r["adjusted_model_s_ref_ttft"],
                               round(4.9 - gen + tok / MT.REF_OUT_TOK_S - 3 * 0.5 + 3 * MT.REF_TTFT_S, 2), places=2)
        self.assertAlmostEqual(r["residual_s"], round(4.9 - gen - 1.5, 2), places=2)
        self.assertEqual(r["model_turns"], 3)
        self.assertEqual(r["ttft_s"]["median"], 0.5)
        self.assertEqual(r["harness_by_tool"]["look"]["n"], 2)
        self.assertNotIn("per_session", r)

    def test_no_result_event_estimates_from_the_timeline(self):
        write_session(self.dir, "S001", scenario())
        s = MT.analyze_session("S001", self.dir / "S001.jsonl")
        self.assertEqual(s["tokens_source"], "estimate")
        self.assertEqual(s["speed_source"], "timeline")
        est = sum(m["est"] + m["think_est"] for m in s["counted"])
        self.assertAlmostEqual(s["tps"], est / (4.9 - 3 * 0.5), places=2)

    def test_old_turns_without_message_ids_use_the_stream(self):
        write_session(self.dir, "S001", scenario(_result(4000, 400, 200)), turns=False)
        old = [{k: v for k, v in r.items() if k not in ("mid", "est")}
               for r in MT.stream_info(self.dir / "S001.jsonl")["records"] if r["type"] != "think_tok"]
        (self.dir / "S001.turns.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
        s = MT.analyze_session("S001", self.dir / "S001.jsonl")
        self.assertEqual(len(s["msgs"]), 4)
        self.assertEqual(s["ttfts"], [s["ttfts"][0]])
        self.assertAlmostEqual(s["model_s"], 4.9, places=2)

    def test_no_thinking_timer_speed_includes_ttft(self):
        evs = [e for e in scenario(_result(4000, 400, 200))]
        for e in evs:
            e.pop("thinking_duration_ms", None)
        write_session(self.dir, "S001", evs)
        s = MT.analyze_session("S001", self.dir / "S001.jsonl")
        self.assertEqual(s["speed_source"], "api_incl_ttft")
        self.assertAlmostEqual(s["tps"], 100.0, places=3)          # 400 / 4 s
        r = MT.report([s])
        self.assertIsNone(r["adjusted_model_s"])                   # TTFT not separable: only the full normalisation
        gen = s["tok"] / 100.0
        self.assertAlmostEqual(r["adjusted_model_s_ref_ttft"],
                               round(4.9 - gen + s["tok"] / MT.REF_OUT_TOK_S + 3 * MT.REF_TTFT_S, 2), places=2)

    def test_resume_api_time_minus_its_parent(self):
        write_session(self.dir, "S001", scenario(_result(10_000, 900, 0, wall_ms=50_000)))
        write_session(self.dir, "S001-r1", scenario(_result(14_000, 400, 200, wall_ms=9_000)))
        res = MT.stream_info(self.dir / "S001-r1.jsonl")["result"]
        self.assertEqual(MT.own_api_ms(self.dir / "S001-r1.jsonl", res), 4000)
        self.assertEqual(MT.resume_parent(self.dir / "S001-r2.jsonl").name, "S001-r1.jsonl")
        self.assertIsNone(MT.resume_parent(self.dir / "S001.jsonl"))
        s = MT.analyze_session("S001-r1", self.dir / "S001-r1.jsonl")
        self.assertAlmostEqual(s["tps"], 200.0, places=3)
        (self.dir / "S001.jsonl").unlink()                          # no parent: the timeline speed instead
        s = MT.analyze_session("S001-r1", self.dir / "S001-r1.jsonl")
        self.assertEqual(s["speed_source"], "timeline")


class Idle(unittest.TestCase):
    def fixture(self):
        tools = [{"type": "tool", "tool": "move_to", "t0": 0, "t1": 1, "ms": 1000},
                 {"type": "tool", "tool": "move_to", "t0": 10, "t1": 11, "ms": 1000},     # explore gap 1-10
                 {"type": "tool", "tool": "wait_for", "t0": 12, "t1": 15, "ms": 3000},
                 {"type": "tool", "tool": "use_ability", "t0": 20, "t1": 21, "ms": 1000},  # combat gap 11-20
                 {"type": "tool", "tool": "load_game", "t0": 22, "t1": 25, "ms": 3000},
                 {"type": "tool", "tool": "choose_dialogue", "t0": 30, "t1": 31, "ms": 1000}]  # dialogue 21-30
        samples = [{"type": "sample", "t": t, "moving": t <= 4} for t in range(1, 11)]
        samples += [{"type": "sample", "t": t, "combat": True, "pturn": not 13 <= t <= 16} for t in range(11, 21)]
        samples += [{"type": "sample", "t": t, "dlg": t >= 27} for t in range(21, 31)]
        return tools, samples

    def test_idle_matches_metrics_gaps(self):
        tools, samples = self.fixture()
        acts = M.action_intervals(tools)
        ours = MT.idle_spans(acts, samples, tools, None)
        theirs = M.gaps(acts, samples, tools, None)
        self.assertEqual([k for k, _ in ours], [g["kind"] for g in theirs])
        for (_, spans), g in zip(ours, theirs):
            self.assertAlmostEqual(MT._length(spans), g["s"], places=2)
        self.assertEqual([k for k, _ in ours], ["explore", "combat", "dialogue"])

    def test_wrapup_cut(self):
        tools, samples = self.fixture()
        acts = M.action_intervals(tools)
        ours = MT.idle_spans(acts, samples, tools, 15.0)
        theirs = M.gaps(acts, samples, tools, 15.0)
        self.assertEqual([k for k, _ in ours], [g["kind"] for g in theirs])
        for (_, spans), g in zip(ours, theirs):
            self.assertAlmostEqual(MT._length(spans), g["s"], places=2)

    @unittest.skipUnless((REAL / "S001.latency.json").is_file(), "no real naut session here")
    def test_real_session_matches_latency_json(self):
        lat = json.loads((REAL / "S001.latency.json").read_text())
        s = MT.analyze_session("S001", REAL / "S001.jsonl")
        r = MT.report([s])
        for kind in ("all", "explore", "combat", "dialogue"):
            self.assertEqual(r["idle"][kind]["n"], lat["idle_s"][kind]["n"])
        self.assertAlmostEqual(r["idle"]["all"]["idle_s"], lat["idle_s"]["all"]["mean"] * lat["idle_s"]["all"]["n"],
                               delta=1.0)


class RunLevel(TmpDir):
    def test_window_and_sum(self):
        write_session(self.dir, "S001", scenario(_result(4000, 400, 200)))
        later = [dict(e, timestamp=_iso(datetime.fromisoformat(e["timestamp"][:-1] + "+00:00").timestamp() + 1000))
                 if e.get("timestamp") else e for e in scenario(_result(4000, 400, 200))]
        write_session(self.dir, "S002", later)
        write_session(self.dir, "S002-fix", scenario())          # not a play session
        both = MT.model_time(self.dir)
        self.assertEqual(both["sessions"], 2)
        self.assertAlmostEqual(both["model_s"], 9.8, places=1)
        self.assertEqual([p["session"] for p in both["per_session"]], ["S001", "S002"])
        one = MT.model_time(self.dir, until=T0 + 100)
        self.assertAlmostEqual(both["adjusted_model_s"], 2 * one["adjusted_model_s"], places=1)
        second = MT.model_time(self.dir, since=T0 + 500)
        self.assertEqual(second["sessions"], 1)
        part = MT.model_time(self.dir, since=T0 + 2, until=T0 + 100)   # clipped: no exact totals
        self.assertEqual(part["tokens_source"], "estimate")

    def test_session_without_tokens_or_speed_borrows_the_pooled_speed(self):
        a = {"session": "A", "lo": 0, "hi": 10, "msgs": [], "counted": [{"trigger": 0, "t_last": 2, "tok": 200}],
             "ttfts": [0.5], "ttft_med": 0.5, "model_s": 2.0, "harness_s": 1, "harness_call_s": 1, "by_tool": {},
             "bridge_tool_s": 1, "split": {b: 0 for b in MT.BUCKETS}, "tok": 200, "tokens_source": "exact_scaled",
             "thinking_tok": 0, "tps": 200.0, "speed_source": "api", "api_s": 2, "idle": []}
        b = dict(a, session="B", tps=None, speed_source="timeline", api_s=None)
        r = MT.report([a, b])
        self.assertAlmostEqual(r["gen_s"], 2.0)                    # B at A's 200 tok/s
        self.assertEqual(r["speed_source"], "api+timeline")
        self.assertIsNone(r["api_s"])

    def test_never_raises(self):
        self.assertEqual(MT.model_time(self.dir / "missing"), {"sessions": 0})
        (self.dir / "S001.jsonl").write_text("not json\n")
        self.assertEqual(MT.model_time(self.dir), {"sessions": 0})

    def test_cli_run_k(self):
        series, run2 = self.dir / "naut", self.dir / "naut-r2"
        series.mkdir()
        run2.mkdir()
        write_session(run2, "S001", scenario(_result(4000, 400, 200)))
        (series / "runs.jsonl").write_text(json.dumps({"run": 1, "playthrough": "naut", "started": 0, "t": 1}) + "\n"
                                           + json.dumps({"run": 2, "playthrough": "naut-r2", "started": T0 - 1,
                                                         "t": T0 + 9}) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(MT.main([str(series), "--run", "2"]), 0)
        d = json.loads(out.getvalue())
        self.assertEqual(d["sessions"], 1)
        self.assertAlmostEqual(d["model_s"], 4.9, places=2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(MT.main([str(series), "--run", "7"]), 2)


if __name__ == "__main__":
    unittest.main()
