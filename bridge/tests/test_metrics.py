"""Offline tests for bg3ai.metrics: recorder, stream timing, session summary, live view, and the hooks in
game.py/server.py. Fake stream-json transcript and fake bridge metrics; no game needed.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-"))

from bg3ai import metrics as M  # noqa: E402
from bg3ai.game import Game  # noqa: E402

T0 = 1_760_000_000.0


def tool(name, t0, t1, nested=False, job_ms=0.0, rpc_ms=20.0, rpc_n=1):
    r = {"type": "tool", "tool": name, "t0": T0 + t0, "t1": T0 + t1, "ms": (t1 - t0) * 1000, "job_ms": job_ms,
         "rpc_ms": rpc_ms, "rpc_n": rpc_n, "busy": job_ms > 0}
    if nested:
        r["nested"] = True
    return r


def sample(t, moving=False, job=0, dlg=False, combat=False):
    return {"type": "sample", "t": T0 + t, "moving": moving, "job": job, "dlg": dlg, "combat": combat}


# A fake claude stream-json transcript with arrival times (seconds from T0).
STREAM = [
    (0.0, {"type": "system", "subtype": "init", "session_id": "x"}),
    (2.0, {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Looking around."},
        {"type": "tool_use", "id": "u1", "name": "mcp__bg3__get_status", "input": {}}]}}),
    (2.1, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "WHERE ..."}]}}),
    (5.1, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u2", "name": "mcp__bg3__move_to", "input": {"target": "e3"}}]}}),
    (9.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u2", "content": "done"}]}}),
    (10.0, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u3", "name": "mcp__bg3__do_actions",
         "input": {"actions": json.dumps([{"tool": "interact", "target": "e4"}, {"tool": "loot"}])}}]}}),
    (14.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u3", "content": "1. ok\n2. ok"}]}}),
    (30.0, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u4", "name": "mcp__bg3__choose_dialogue", "input": {"option": 2}}]}}),
    (30.5, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u4", "content": "ok"}]}}),
    (31.0, {"type": "result", "subtype": "success"}),
]


def timed_turns():
    st = M.StreamTiming(None)
    for t, ev in STREAM:
        st.on_event(ev, T0 + t)
    return st.records


class Stats(unittest.TestCase):
    def test_percentile(self):
        self.assertIsNone(M.percentile([], 50))
        self.assertEqual(M.percentile([5], 90), 5)
        self.assertEqual(M.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(M.percentile(range(1, 11), 90), 9.1)

    def test_stats(self):
        self.assertEqual(M.stats([]), {"n": 0})
        s = M.stats([1, 3])
        self.assertEqual((s["n"], s["median"], s["max"]), (2, 2.0, 3))

    def test_moved(self):
        a = {"p": [0, 0, 0]}
        self.assertFalse(M.moved(None, a))
        self.assertFalse(M.moved(a, {"p": [0.1, 0, 0]}))
        self.assertTrue(M.moved(a, {"p": [1, 0, 0]}))
        self.assertFalse(M.moved(a, []))  # empty Lua table encodes as []


class Stream(unittest.TestCase):
    def test_records(self):
        recs = timed_turns()
        uses = [r for r in recs if r["type"] == "use"]
        self.assertEqual([u["name"] for u in uses], ["get_status", "move_to", "do_actions", "choose_dialogue"])
        self.assertEqual(uses[2]["steps"], 2)
        self.assertEqual(len([r for r in recs if r["type"] == "result"]), 4)

    def test_think_times(self):
        self.assertEqual([round(x, 2) for x in M.think_times(timed_turns())], [3.0, 1.0, 16.0])

    def test_stream_actions_fallback(self):
        acts = M.stream_actions(timed_turns())
        self.assertEqual([a["tool"] for a in acts], ["move_to", "do_actions", "choose_dialogue"])

    def test_turns_file(self):
        with tempfile.TemporaryDirectory() as d:
            st = M.StreamTiming(Path(d) / "S001.turns.jsonl")
            for t, ev in STREAM:
                st.on_event(ev, T0 + t)
            self.assertEqual(len(M.read_jsonl(Path(d) / "S001.turns.jsonl")), len(st.records))

    def test_summary_from_stream_only(self):
        s = M.summarize([], [], timed_turns())
        self.assertEqual(s["source"], "stream")
        # gaps: move_to end 9.0 → do_actions 10.0 (1 s, explore); do_actions end 14.0 → choose_dialogue 30.0 (16 s, dialogue)
        self.assertEqual(s["long_gaps"]["dialogue"], 1)
        self.assertEqual(s["long_gaps"]["explore"], 0)
        self.assertTrue(s["targets"]["long_gaps_non_dialogue"]["pass"])
        self.assertEqual(s["actions"], 1 + 2 + 1)
        self.assertEqual(s["turns"], 3)


class Summary(unittest.TestCase):
    def setUp(self):
        # 0-60 s: get_status, move_to (job), a do_actions batch with 2 steps, a 12 s pause, end_turn in combat,
        # a 20 s pause before a dialogue answer.
        self.tools = [
            tool("get_status", 0.0, 0.05),
            tool("move_to", 1.0, 5.0, job_ms=3800),
            tool("interact", 6.0, 7.0, nested=True, job_ms=900),
            tool("loot", 7.0, 7.5, nested=True),
            tool("do_actions", 6.0, 7.6, job_ms=900),
            tool("get_nearby", 8.0, 8.2, rpc_ms=150),
            tool("use_ability", 19.5, 21.0, job_ms=1200),   # 12 s after loot: in combat (samples)
            tool("end_turn", 22.0, 22.5),
            tool("choose_dialogue", 42.5, 43.0),            # 20 s after end_turn: dialogue
            tool("get_status", 50.0, 50.03),
        ]
        self.samples = [sample(t, moving=1 <= t <= 5) for t in range(0, 8)] \
            + [sample(t, combat=True) for t in range(8, 23)] + [sample(t, dlg=True) for t in range(23, 44)] \
            + [sample(t) for t in range(44, 60)]

    def test_summary(self):
        s = M.summarize(self.tools, self.samples, timed_turns(), start=T0, end=T0 + 60)
        self.assertEqual(s["source"], "bridge")
        self.assertEqual(s["minutes"], 1.0)
        kinds = {g["next"]: g["kind"] for g in M.gaps(M.action_intervals(self.tools), self.samples)}
        self.assertEqual(kinds, {"interact": "explore", "use_ability": "combat", "end_turn": "combat",
                                 "choose_dialogue": "dialogue"})
        self.assertEqual(s["long_gaps"]["total"], 2)
        self.assertEqual((s["long_gaps"]["combat"], s["long_gaps"]["dialogue"]), (1, 1))
        t = s["targets"]
        self.assertTrue(t["median_idle_explore_s"]["pass"])           # 1.0 s
        self.assertFalse(t["long_gaps_non_dialogue"]["pass"])          # the 12 s combat gap
        self.assertEqual(t["long_gaps_non_dialogue"]["value"], 1)
        self.assertTrue(t["bridge_rt_median_ms"]["pass"])
        self.assertEqual(s["actions"], 6)  # move_to, interact, loot, use_ability, end_turn, choose_dialogue
        self.assertEqual(s["tool_calls"], 8)  # nested steps are not top-level calls
        self.assertAlmostEqual(s["busy_pct"], round(100 * 5 / 60, 1))
        self.assertAlmostEqual(s["dialogue_pct"], round(100 * 21 / 60, 1))

    def test_async_walk_is_not_idle(self):
        # An async move_to returns at once; the party walks 1-9 s, the next action comes at 12 s.
        tools = [tool("move_to", 0.0, 0.5), tool("talk_to", 12.0, 13.0)]
        samples = [sample(t, moving=1 <= t <= 9) for t in range(0, 14)]
        (g,) = M.gaps(M.action_intervals(tools), samples)
        self.assertEqual(g["gap_s"], 11.5)
        self.assertEqual(g["busy_s"], 8.5)   # 0.5-9 s: each moving sample covers the second before it
        self.assertEqual(g["s"], 3.0)        # idle: the 3 s after arrival
        s = M.summarize(tools, samples, [], start=T0, end=T0 + 14)
        self.assertEqual(s["long_gaps"]["total"], 0)
        # no busy samples: the whole gap is idle, as before
        (g,) = M.gaps(M.action_intervals(tools), [sample(t) for t in range(0, 14)])
        self.assertEqual((g["s"], g["busy_s"]), (11.5, 0.0))

    def test_rt_excludes_job_time(self):
        s = M.summarize(self.tools, self.samples, [], start=T0, end=T0 + 60)
        self.assertLess(s["tool_rt_ms"]["all"]["max"], 1000)

    def test_write_summary_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            mp = d / "S001.metrics.jsonl"
            mp.write_text("\n".join(json.dumps(r) for r in self.tools + self.samples) + "\nnot json\n")
            s = M.summarize_files(mp, None, T0, T0 + 60)
            M.write_summary(s, d / "S001.latency.json", d / "latency_history.jsonl", "S001")
            M.write_summary(s, d / "S002.latency.json", d / "latency_history.jsonl", "S002")
            self.assertEqual(json.loads((d / "S001.latency.json").read_text())["session"], "S001")
            rows = M.read_jsonl(d / "latency_history.jsonl")
            self.assertEqual([r["session"] for r in rows], ["S001", "S002"])
            self.assertEqual(rows[0]["pass"]["long_gaps_non_dialogue"], False)

    def test_empty(self):
        s = M.summarize([], [], [])
        self.assertIsNone(s["targets"]["median_idle_explore_s"]["pass"])
        self.assertIsNone(s["busy_pct"])

    def test_live_view(self):
        v = M.live_view(self.tools, self.samples, [], now=T0 + 60)
        self.assertEqual(v["current_idle_s"], 17.0)  # since choose_dialogue ended at 43
        self.assertFalse(v["busy_now"])
        self.assertEqual(len(v["timeline"]["buckets"]), 60)
        self.assertIn("act", v["timeline"]["buckets"])
        self.assertIn("dlg", v["timeline"]["buckets"])
        busy = M.live_view(self.tools, self.samples + [sample(60, moving=True)], [], now=T0 + 60.5)
        self.assertTrue(busy["busy_now"])
        self.assertIsNone(busy["current_idle_s"])


class Recording(unittest.TestCase):
    def test_tool_done_accounts_rpc_and_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            rec = M.Recorder(Path(d) / "m.jsonl")
            rec.rpc_done("state.status", T0 + 0.1, T0 + 0.13)
            rec.rpc_done("act.move_to", T0 + 0.2, T0 + 0.22)
            rec.job_done("act.move_to", T0 + 0.2, T0 + 2.2, "done")
            rec.rpc_done("old", T0 - 5, T0 - 4.9)
            r = rec.tool_done("move_to", 2500, t1=T0 + 2.5)
            self.assertEqual(r["rpc_n"], 2)
            self.assertAlmostEqual(r["rpc_ms"], 50, delta=1)
            self.assertAlmostEqual(r["job_ms"], 2000, delta=1)
            self.assertTrue(r["busy"])
            rec.sample(T0 + 3, moving=False, job=0, dlg=False, combat=False)
            r2 = rec.tool_done("get_status", 30, nested=True, ok=False, t1=T0 + 10)
            self.assertFalse(r2["busy"])
            self.assertTrue(r2["nested"])
            self.assertIs(r2["ok"], False)
            lines = M.read_jsonl(Path(d) / "m.jsonl")
            self.assertEqual([x["type"] for x in lines], ["tool", "sample", "tool"])
            self.assertIsNone(rec.tool_done("autoskip", 0))

    def test_game_rpc_hook(self):
        g = Game()
        rec = M.Recorder(None)
        g.metrics = rec
        g.eval = lambda code, context="client", timeout=12.0: '{"ok":true,"result":{"x":1}}'
        self.assertEqual(g.rpc("state.status"), {"x": 1})
        self.assertEqual([r[2] for r in rec.rpcs], ["state.status"])

        calls = iter(['{"ok":true,"result":{"job":7}}', '{"ok":true,"result":{"status":"done","elapsed_ms":900}}'])
        g.eval = lambda code, context="client", timeout=12.0: next(calls)
        r = g.run_job("act.move_to", {})
        self.assertEqual(r.status, "done")
        self.assertEqual([j[2] for j in rec.jobs], ["act.move_to"])
        g.eval = lambda code, context="client", timeout=12.0: '{"ok":true,"result":{"answered":1}}'
        g.run_job("ui.answer", {})  # instant op: not a job
        self.assertEqual(len(rec.jobs), 1)

    def test_sampler_loop(self):
        import threading

        class G:
            n = 0

            def rpc(self, op, args=None, context="server", timeout=3.0):
                G.n += 1
                if G.n >= 3:
                    stop.set()
                return {"pos": {"p": [G.n * 2.0, 0, 0]}, "jobs": 0, "in_dialogue": False, "in_combat": G.n == 2}

        stop = threading.Event()
        rec = M.Recorder(None)
        seen = []
        rec.sample = lambda *a: seen.append(a)
        M.sampler_loop(rec, G(), stop, interval=0.001)
        self.assertEqual([(s[1], s[4]) for s in seen], [(False, False), (True, True), (True, False)])


class ServerHook(unittest.TestCase):
    def test_every_tool_call_is_recorded(self):
        from bg3ai import server
        from test_objectives import FakeGame

        with tempfile.TemporaryDirectory() as d:
            orig_game, orig_m = server.game, server.METRICS
            server.METRICS = M.Recorder(Path(d) / "m.jsonl")
            server.game = FakeGame()
            try:
                server.get_status()
                server.walk_toward(direction="W", wait=True)
            finally:
                server.game, server.METRICS = orig_game, orig_m
            recs = M.read_jsonl(Path(d) / "m.jsonl")
            self.assertEqual([r["tool"] for r in recs], ["get_status", "walk_toward"])
            self.assertTrue(all(r["t1"] >= r["t0"] for r in recs))


if __name__ == "__main__":
    unittest.main()
