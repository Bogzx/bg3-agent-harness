"""Offline tests for bg3ai.metrics: recorder, stream timing, session summary, live view, and the hooks in
game.py/server.py. Fake stream-json transcript and fake bridge metrics; no game needed.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

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

    def test_enemy_turns_are_not_idle(self):
        # Combat: the party ends its turn at 1 s, enemies act 1-21 s (pturn false), the agent's next action
        # comes at 25 s: 24 s raw, 4 s of it the agent's own turn.
        tools = [tool("end_turn", 0.0, 1.0), tool("use_ability", 25.0, 26.0)]
        samples = [sample(t, combat=True) | {"pturn": not (2 <= t <= 21)} for t in range(0, 27)]
        (g,) = M.gaps(M.action_intervals(tools), samples)
        self.assertEqual((g["kind"], g["gap_s"], g["turn_s"], g["s"], g["own_s"]), ("combat", 24.0, 20.0, 4.0, 4.0))
        s = M.summarize(tools, samples, [], start=T0, end=T0 + 27)
        self.assertEqual(s["long_gaps"]["total"], 0)
        self.assertEqual(s["idle_s"]["combat"]["median"], 4.0)            # party-turn idle
        self.assertEqual(s["idle_s"]["combat_incl_enemy"]["median"], 24.0)  # the raw view stays reported
        self.assertEqual(s["enemy_turn_s"], 20.0)
        # busy during an enemy turn (party member pushed) is not subtracted twice
        samples2 = [x | {"moving": 5 <= x["t"] - T0 <= 8} for x in samples]
        (g2,) = M.gaps(M.action_intervals(tools), samples2)
        self.assertEqual((g2["busy_s"], g2["turn_s"], g2["s"]), (4.0, 16.0, 4.0))
        # the agent's own long think on its turn still counts
        samples3 = [sample(t, combat=True) | {"pturn": True} for t in range(0, 27)]
        (g3,) = M.gaps(M.action_intervals(tools), samples3)
        self.assertEqual((g3["s"], g3.get("turn_s")), (24.0, None))

    def test_enemy_turns_fallback_from_wait_for(self):
        # Older samples without pturn: an in-combat wait_for that blocked (3-20 s) marks the gap up to its end
        # as enemy turns; a wait_for that returned at once (and gaps out of combat) excludes nothing.
        tools = [tool("end_turn", 0.0, 1.0), tool("wait_for", 3.0, 20.0), tool("use_ability", 24.0, 25.0),
                 tool("wait_for", 26.0, 26.2), tool("use_ability", 40.0, 41.0)]
        samples = [sample(t, combat=True) for t in range(0, 42)]
        g1, g2 = M.gaps(M.action_intervals(tools), samples, tools)
        self.assertEqual((g1["gap_s"], g1["turn_s"], g1["s"]), (23.0, 19.0, 4.0))
        self.assertEqual((g2["gap_s"], g2["s"], "turn_s" in g2), (15.0, 15.0, False))
        (e,) = M.gaps(M.action_intervals(tools[:3]), [sample(t) for t in range(0, 26)], tools)
        self.assertEqual((e["kind"], e["s"]), ("explore", 23.0))
        # without the tool records (old callers): unchanged
        g1, _ = M.gaps(M.action_intervals(tools), samples)
        self.assertEqual(g1["s"], 23.0)

    def test_loading_screens_are_not_idle(self):
        # S015: a party wipe, then load_game took 23.7 s (and the loading screen ran on after it): that is
        # not the agent idling. load_game's own time and the sampler's unanswered polls are left out.
        tools = [tool("move_to", 0.0, 1.0), tool("load_game", 3.0, 20.0), tool("move_to", 30.0, 31.0)]
        samples = [sample(t) for t in range(0, 3)] + [{"type": "sample", "t": T0 + t, "load": True}
                                                       for t in (24.0, 28.0)] + [sample(29.0), sample(30.0)]
        (g,) = M.gaps(M.action_intervals(tools), samples, tools)
        # 29 s raw: load_game 3-20, load samples 20-24 (span 5 s, capped by the call) and 24-28
        self.assertEqual((g["gap_s"], g["load_s"], g["s"]), (29.0, 25.0, 4.0))
        s = M.summarize(tools, samples, [], start=T0, end=T0 + 31)
        self.assertEqual(s["loading_s"], 25.0)
        self.assertEqual(s["long_gaps"]["total"], 0)
        self.assertEqual(s["samples"], 5)  # load samples are not activity samples
        # the sampler writes them when nav.activity fails
        rec = M.Recorder(None)
        self.assertEqual(rec.load_sample(T0), {"type": "sample", "t": round(T0, 2), "load": True})

    def test_sample_pturn(self):
        rec = M.Recorder(None)
        self.assertNotIn("pturn", rec.sample(T0, False, 0, False, False, party_turn=False))  # out of combat
        self.assertIs(rec.sample(T0, False, 0, False, True, party_turn=False)["pturn"], False)
        self.assertNotIn("pturn", rec.sample(T0, False, 0, False, True))  # older mod: unknown

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

    def test_failed_nested_call_keeps_its_ops(self):
        # S038: jump_to inside do_actions hung in nav.landing; the failed nested record named no op.
        with tempfile.TemporaryDirectory() as d:
            rec = M.Recorder(Path(d) / "m.jsonl")
            rec.rpc_done("nav.landing", T0 + 0.1, T0 + 10.1)
            r = rec.tool_done("jump_to", 12000, nested=True, ok=False, t1=T0 + 12)
            self.assertEqual(r["ops"][0][0], "nav.landing")
            rec.rpc_done("state.status", T0 + 20.1, T0 + 20.2)
            r2 = rec.tool_done("get_status", 200, nested=True, t1=T0 + 20.3)
            self.assertNotIn("ops", r2)  # a nested call that worked stays lean

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
                return {"pos": {"p": [G.n * 2.0, 0, 0]}, "jobs": 0, "in_dialogue": False, "in_combat": G.n == 2,
                        **({"party_turn": False} if G.n == 2 else {})}

        stop = threading.Event()
        rec = M.Recorder(None)
        seen = []
        rec.sample = lambda *a: seen.append(a)
        M.sampler_loop(rec, G(), stop, interval=0.001)
        self.assertEqual([(s[1], s[4], s[5]) for s in seen], [(False, False, None), (True, True, False),
                                                              (True, False, None)])


    def test_sampler_loop_client_loading_screen(self):
        """S023 06:29:38: the server answered nav.activity through a ~17 s loading screen; the client's game
        state says it is loading, so those samples are load samples (not idle)."""
        import threading

        class G:
            n = 0
            states = ["Running", "SwapLevel", "LoadLevel", "Running"]

            def eval(self, code, context="client", timeout=12.0):
                assert context == "client"
                return G.states[min(G.n, len(G.states) - 1)]

            def rpc(self, op, args=None, context="server", timeout=3.0):
                return {"pos": {}, "jobs": 0, "in_dialogue": False, "in_combat": False}

        stop = threading.Event()
        rec = M.Recorder(None)
        kinds = []

        def tick(kind):
            kinds.append(kind)
            G.n += 1
            if G.n >= 4:
                stop.set()
        rec.sample = lambda *a: tick("sample")
        rec.load_sample = lambda t: tick("load")
        M.sampler_loop(rec, G(), stop, interval=0.001)
        self.assertEqual(kinds, ["sample", "load", "load", "sample"])

    def test_mem_sampler(self):
        """S038 20:48 OOM (RSS 5 -> 13 GB in ~30 s): a mem record a minute, and at once on a 1 GB RSS jump, with
        both Lua heaps (sys.mem) so the next spike reads as our Lua or the engine."""
        clock = [1000.0]
        rss = [5000]

        class G:
            calls = []

            def rpc(self, op, args=None, context="server", timeout=3.0):
                G.calls.append((op, context))
                if context == "client":
                    raise TimeoutError("client frozen")
                return {"context": "server", "lua_mb": 41.5, "high": True}

        rec = M.Recorder(None)
        out = []
        rec.write = out.append
        ms = M.MemSampler(rec, G(), rss=lambda: rss[0], now=lambda: clock[0])
        self.assertIsNone(ms.tick())  # a fresh bridge: not before a minute
        clock[0] += 30
        self.assertIsNone(ms.tick())
        clock[0] += 31
        r = ms.tick()
        self.assertEqual(r, {"type": "mem", "t": 1061.0, "rss_mb": 5000, "lua_server_mb": 41.5, "high": True})
        self.assertEqual(G.calls, [("sys.mem", "server"), ("sys.mem", "client")])
        clock[0] += 5
        rss[0] = 6100
        r = ms.tick()
        self.assertEqual((r["why"], r["rss_mb"]), ("rss_jump", 6100))
        clock[0] += 5
        rss[0] = 6500
        self.assertIsNone(ms.tick())
        self.assertEqual(len(out), 2)

    def test_rss_from_proc(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proc = Path(d)
            (proc / "4242").mkdir()
            (proc / "4242" / "comm").write_text("bg3\n")
            (proc / "4242" / "status").write_text("Name:\tbg3\nVmRSS:\t 5242880 kB\n")
            (proc / "self").mkdir()
            self.assertEqual(M.game_pid(proc), 4242)
            self.assertEqual(M.rss_mb(4242, proc), 5120)
            self.assertIsNone(M.rss_mb(99, proc))
            self.assertIsNone(M.rss_mb(None, proc))

    def test_client_state_unknown(self):
        class Boom:
            def eval(self, *a, **k):
                raise TimeoutError("no answer")
        self.assertIsNone(M.client_state(object()))
        self.assertIsNone(M.client_state(Boom()))


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


class ApiSpeed(unittest.TestCase):
    """ms per output token and API seconds per turn next to think time (impl-act, think time S003 -> S013)."""

    def test_stream_estimate_and_exact_result(self):
        evs = [(0.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}}),
               (1.0, {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 100}),
               (2.0, {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "x" * 350}]}}),
               (3.0, {"type": "assistant", "message": {"id": "m1", "content": [
                   {"type": "tool_use", "id": "b", "name": "look", "input": {}}]}}),
               (3.5, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b"}]}}),
               (4.5, {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "y" * 700}]}})]
        turns = [r for t, ev in evs for r in M.stream_records(ev, t)]
        a = M.api_speed(turns)
        self.assertEqual(a["source"], "stream")
        self.assertEqual(a["turns"], 2)
        self.assertEqual(a["api_s_per_turn"], 2.0)  # (3.0 + 1.0) / 2
        turns += M.stream_records({"type": "result", "duration_api_ms": 3000, "num_turns": 2,
                                   "usage": {"output_tokens": 200}}, 5.0)
        a = M.api_speed(turns)
        self.assertEqual((a["source"], a["ms_per_out_tok"], a["api_s_per_turn"]), ("result", 15.0, 1.5))
        s = M.summarize([], [], turns)
        self.assertEqual(s["api"]["ms_per_out_tok"], 15.0)

    def test_wrapup_phase_is_not_play(self):
        # Play until the first WRAP UP NOW clock line at 30 s, then save + notes with long gaps.
        clock = "ok\n⏱ 21:40 · 3m left: WRAP UP NOW. Reach a safe spot, save_game, then follow ..."
        evs = [(29.0, {"type": "user", "message": {"content": [
                   {"type": "tool_result", "tool_use_id": "q", "content": "playbook: when the ⏱ line says WRAP UP NOW"}]}}),
               (30.0, {"type": "user", "message": {"content": [
                   {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": clock}]}]}}),
               (45.0, {"type": "user", "message": {"content": [
                   {"type": "tool_result", "tool_use_id": "b", "content": "⏱ SESSION OVER: only save_game"}]}})]
        st = M.StreamTiming(None)
        for t, ev in evs:
            st.on_event(ev, T0 + t)
        self.assertEqual([r["t"] - T0 for r in st.records if r["type"] == "wrapup"], [30.0])  # first only
        tools = [tool("move_to", 0.0, 1.0), tool("interact", 2.0, 3.0),
                 tool("loot", 25.0, 26.0),         # 22 s gap before it: play, counted
                 tool("move_to", 40.0, 41.0),      # 14 s gap, cut at 30 s: 4 s of play
                 tool("wait", 60.0, 61.0)]         # 19 s gap in wrap-up: not counted
        samples = [sample(t) for t in range(0, 62)]
        s = M.summarize(tools, samples, st.records, start=T0, end=T0 + 70)
        self.assertEqual(s["wrapup_s"], 40.0)
        self.assertEqual(s["idle_s"]["wrapup"]["n"], 1)
        self.assertEqual(s["idle_s"]["wrapup"]["max"], 19.0)
        self.assertEqual(s["idle_s"]["all"]["n"], 3)
        self.assertEqual(s["idle_s"]["explore"]["max"], 22.0)
        self.assertEqual((s["long_gaps"]["total"], s["long_gaps"]["wrapup"]), (1, 1))
        self.assertEqual(s["targets"]["long_gaps_non_dialogue"]["value"], 1)
        g = M.gaps(M.action_intervals(tools), samples, tools, T0 + 30)
        self.assertEqual([(x["kind"], x["s"]) for x in g],
                         [("explore", 1.0), ("explore", 22.0), ("explore", 4.0), ("wrapup", 19.0)])
        # no clock line: everything counts, as before
        s = M.summarize(tools, samples, [], start=T0, end=T0 + 70)
        self.assertIsNone(s["wrapup_s"])
        self.assertEqual(s["long_gaps"]["total"], 3)

    def test_wrapup_backfill_from_stream(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            stream = Path(d) / "S001.jsonl"
            stream.write_text(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "x ⏱ 21:40 · 2m left: WRAP UP NOW."}]}}) + "\n")
            turns = [{"type": "result", "t": T0 + 5, "id": "a"}]
            self.assertEqual(M.wrapup_from_stream(stream, turns), [{"type": "wrapup", "t": T0 + 5}])
            (Path(d) / "S001.turns.jsonl").write_text("\n".join(json.dumps(r) for r in turns) + "\n")
            s = M.summarize_files(None, Path(d) / "S001.turns.jsonl", T0, T0 + 20)
            self.assertEqual(s["wrapup_s"], 15.0)


class CallRates(unittest.TestCase):
    def test_rates_from_stream_uses_wrapup_excluded(self):
        from bg3ai import metrics as M
        turns = [{"type": "use", "t": 10.0, "name": "travel_to"}, {"type": "use", "t": 20.0, "name": "wait_for"},
                 {"type": "use", "t": 30.0, "name": "get_frontier"}, {"type": "use", "t": 40.0, "name": "Read"},
                 {"type": "use", "t": 50.0, "name": "combat_turn"}, {"type": "use", "t": 70.0, "name": "look"}]
        r = M.call_rates(turns, [], 0.0, 60.0)   # wrap-up from 60 s: the look is left out
        self.assertEqual((r["calls"], r["reads"], r["action_calls"]), (5, 1, 2))
        self.assertEqual((r["calls_pm"], r["reads_pm"], r["actions_pm"]), (5.0, 1.0, 2.0))
        self.assertEqual(r["reads_by"], {"get_frontier": 1})

    def test_bridge_fallback(self):
        from bg3ai import metrics as M
        tools = [{"tool": "look", "t0": 5.0}, {"tool": "move_to", "t0": 6.0}, {"tool": "look", "t0": 7.0, "nested": True}]
        r = M.call_rates([], tools, 0.0, 60.0)
        self.assertEqual((r["source"], r["calls"], r["reads"], r["action_calls"]), ("bridge", 2, 1, 1))
