"""Read latency against a fake bg3le that keeps its tick semantics (fake_bg3le.py), with and without a
running queue job polling the game from another thread (S009: reads median 155 ms while a queue ran,
59 ms without; the Lua itself ~16 ms per eval).

Old shape: one connection behind one lock (pool_max=1), every op its own eval, events drained after
(server then client). New shape: Game.read() = server ops + client ops in parallel evals on pooled
connections, each with its context's events folded in, so the tool's EVENTS section costs no extra tick.

  uv run --project bridge python -m unittest bridge/tests/test_read_latency.py -v
  BG3AI_LATENCY_REPORT=1 ... prints the numbers.
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fake_bg3le import FakeBg3le  # noqa: E402

from bg3ai import metrics as M  # noqa: E402
from bg3ai.game import Game, GameError  # noqa: E402

TICK_S = 0.030    # server (story thread) tick
CTICK_S = 0.016   # client tick
P1 = "11111111-2222-3333-4444-555555555555"
REPORT = bool(os.environ.get("BG3AI_LATENCY_REPORT"))


def _fake(**kw) -> FakeBg3le:
    f = FakeBg3le(server_tick_s=TICK_S, client_tick_s=CTICK_S, **kw)
    f.op("state.status", lambda a, c: {"party": [{"uuid": P1, "name": "Claude", "hp": 10, "max_hp": 10,
                                                   "pos": [0, 0, 0]}], "active": P1}, cost_s=0.004)
    f.op("nav.markers", lambda a, c: {"markers": []}, cost_s=0.002)
    f.op("ui.state", lambda a, c: {"screen": "game"}, cost_s=0.003)
    f.op("state.nearby", lambda a, c: {"characters": [], "items": []}, cost_s=0.004)
    f.op("job.poll", lambda a, c: {"status": "running", "elapsed_ms": 100})
    f.op("act.watch", lambda a, c: {"party": []}, cost_s=0.001)

    def entity(a, c):
        if a.get("uuid") != P1:
            raise RuntimeError("not visible")
        return {"name": "Claude"}
    f.op("state.entity", entity)
    return f.start()


def _old_get_status(g: Game) -> None:
    """What get_status did in S009: ui.state, status+markers, then events server, events client."""
    g.try_rpc("ui.state", context="client")
    g.multi([("state.status", {}), ("nav.markers", {})])
    g.drain_events("server")
    g.drain_events("client")


def _new_get_status(g: Game) -> None:
    g.begin_tool()
    g.read([("state.status", {}), ("nav.markers", {})], [("ui.state", {})])
    g.parallel(lambda: g.drain_events("server"), lambda: g.drain_events("client"))  # _events_text


class _Queue:
    """A running action queue: job.poll every 0.1 s from its own thread, plus the activity sampler's
    act.watch once a second, both on the shared Game (like actq + _running_jobs)."""

    def __init__(self, g: Game):
        self.g, self.stop = g, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        n = 0
        while not self.stop.is_set():
            self.g.rpc("job.poll", {"id": 1})
            n += 1
            if n % 10 == 0:
                self.g.rpc("act.watch")
            time.sleep(0.1)

    def __enter__(self):
        self.t.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def _settle(g: Game) -> None:
    """Wait for g's background event drains (read() without client ops drains the client context on a
    helper thread) so eval counts taken after this don't race a late eval."""
    for t in list(g._bg_drain.values()):
        if t is not None:
            t.join(5)


def _measure(fn, g: Game, n: int = 30) -> dict:
    ms = []
    for i in range(n):
        time.sleep(0.013 * (i % 7))  # arrive at different phases of the tick
        t0 = time.perf_counter()
        fn(g)
        ms.append((time.perf_counter() - t0) * 1000)
    return {"median": statistics.median(ms), "p90": M.percentile(ms, 90), "max": max(ms)}


class ReadLatency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = _fake()
        cls.results = {}

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        if REPORT:
            for k, v in cls.results.items():
                print(f"{k:34s} median {v['median']:6.1f} ms  p90 {v['p90']:6.1f}  max {v['max']:6.1f}")

    def _run(self, name, fn, pool, queue):
        g = Game(port=self.fake.port, pool_max=pool)
        try:
            if queue:
                with _Queue(g):
                    r = _measure(fn, g)
            else:
                r = _measure(fn, g)
        finally:
            g.close()
        self.results[name] = r
        return r

    def test_get_status_shape(self):
        old_idle = self._run("old get_status, idle", _old_get_status, 1, False)
        old_q = self._run("old get_status, queue running", _old_get_status, 1, True)
        new_idle = self._run("new get_status, idle", _new_get_status, 4, False)
        new_q = self._run("new get_status, queue running", _new_get_status, 4, True)
        # One server tick (+ the ops' Lua time) instead of three or four, and the queue doesn't add a tick.
        self.assertLess(new_idle["median"], 2 * TICK_S * 1000)
        self.assertLess(new_q["median"], 2 * TICK_S * 1000)
        self.assertLess(new_q["p90"], 3 * TICK_S * 1000)
        self.assertLess(new_idle["median"], old_idle["median"] / 1.6)
        self.assertLess(new_q["median"], old_q["median"] / 1.8)

    def test_single_op_read_one_tick(self):
        def old(g):
            g.rpc("state.nearby", {"radius": 25})
            g.drain_events("server")
            g.drain_events("client")

        def new(g):
            g.begin_tool()
            g.read1("state.nearby", {"radius": 25})
            g.parallel(lambda: g.drain_events("server"), lambda: g.drain_events("client"))

        o = self._run("old get_nearby, queue running", old, 1, True)
        n = self._run("new get_nearby, queue running", new, 4, True)
        self.assertLess(n["median"], 2 * TICK_S * 1000)
        self.assertLess(n["median"], o["median"] / 2)

    def test_one_eval_per_context(self):
        fake = _fake()  # its own: the class fake can still get late evals from the timing tests' Games
        g = Game(port=fake.port)
        try:
            g.begin_tool()
            g.read1("state.nearby")  # warm the pool
            _settle(g)  # the warm-up's background client drain must not land in the count
            before = len(fake.evals)
            g.begin_tool()
            g.read([("state.status", {}), ("nav.markers", {})], [("ui.state", {})])
            g.drain_events("server")
            g.drain_events("client")
            _settle(g)
            self.assertEqual(len(fake.evals) - before, 2)  # one server eval, one client eval, no drains
        finally:
            g.close()
            fake.stop()

    def test_strict_read_keeps_the_mods_error(self):
        g = Game(port=self.fake.port)
        try:
            self.assertEqual(g.read1("state.entity", {"uuid": P1}), {"name": "Claude"})
            with self.assertRaisesRegex(GameError, "not visible"):
                g.read1("state.entity", {"uuid": "nope"})
            s, c = g.read([("state.entity", {"uuid": "nope"}), ("state.status", {})], [])
            self.assertIsNone(s[0])
            self.assertEqual(s[1]["active"], P1)
        finally:
            g.close()


LIVE_TICK_S, LIVE_CTICK_S = 0.025, 0.046  # 2nd machine, 2026-10-10: server ~40 Hz, client = render frame ~21 fps


def _tool(g: Game, read) -> list:
    """One read tool as server.py runs it: begin_tool, the read, then _events_text's parallel drains."""
    g.begin_tool()
    read(g)
    return g.parallel(lambda: g.drain_events("server"), lambda: g.drain_events("client"))


class LiveTicks(unittest.TestCase):
    """Live (check_reads --queue): every read ~100 ms, ~3 sequential evals. The ticks were the floor (an eval
    waits for its context's next pump: TCP_NODELAY is set on both ends), but _events_text drained the two
    contexts on parallel() helper threads, which didn't see the read's thread-local "already drained" mark,
    so each read paid a second tick; and server-only reads also waited for the slow client tick."""

    @staticmethod
    def _live_fake() -> FakeBg3le:
        fake = FakeBg3le(server_tick_s=LIVE_TICK_S, client_tick_s=LIVE_CTICK_S)
        base = _fake()  # same ops
        base.stop()
        fake.ops, fake.cost_s = base.ops, base.cost_s
        return fake.start()

    @classmethod
    def setUpClass(cls):
        cls.fake = cls._live_fake()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_events_section_after_read_costs_no_eval(self):
        fake = self._live_fake()  # its own, so no other test's late eval lands in the count
        g = Game(port=fake.port)
        try:
            _tool(g, lambda g: g.read1("state.nearby"))  # warm the pool
            _settle(g)
            before = len(fake.evals)
            _tool(g, lambda g: g.read([("state.status", {}), ("nav.markers", {})], [("ui.state", {})]))
            _settle(g)
            self.assertEqual(len(fake.evals) - before, 2)  # one server + one client eval, no drains
        finally:
            g.close()
            fake.stop()

    def test_server_only_read_does_not_wait_for_the_client_tick(self):
        g = Game(port=self.fake.port)
        try:
            _tool(g, lambda g: g.read1("state.nearby"))
            ms = []
            for i in range(30):
                time.sleep(0.011 * (i % 7))
                t0 = time.perf_counter()
                _tool(g, lambda g: g.read1("state.nearby"))
                ms.append((time.perf_counter() - t0) * 1000)
            med = statistics.median(ms)
            if REPORT:
                print(f"\nlive ticks: server-only read median {med:.1f} ms  p90 {M.percentile(ms, 90):.1f}")
            self.assertLess(med, 30)
            self.assertLess(M.percentile(ms, 90), LIVE_TICK_S * 1000 + 15)
        finally:
            g.close()

    def test_get_status_shape_at_live_ticks(self):
        g = Game(port=self.fake.port)
        try:
            r = _measure(lambda g: _tool(g, lambda g: g.read([("state.status", {}), ("nav.markers", {})],
                                                            [("ui.state", {})])), g)
            if REPORT:
                print(f"\nlive ticks: get_status median {r['median']:.1f} ms  p90 {r['p90']:.1f}")
            self.assertLess(r["median"], LIVE_CTICK_S * 1000)  # one client tick at most, not two
        finally:
            g.close()

    def test_background_client_events_arrive_once_on_the_next_tool(self):
        fake = self._live_fake()
        g = Game(port=fake.port)
        try:
            fake.emit("client", "dialogue_started")
            fake.emit("server", "quest_updated")
            s1, c1 = _tool(g, lambda g: g.read1("state.nearby"))
            self.assertEqual([e["kind"] for e in s1], ["quest_updated"])
            seen = [e["kind"] for e in c1]
            time.sleep(LIVE_CTICK_S * 2.5)
            fake.emit("client", "line")
            for _ in range(3):
                seen += [e["kind"] for e in _tool(g, lambda g: g.read1("state.nearby"))[1]]
                time.sleep(LIVE_CTICK_S * 2.5)
            seen += [e["kind"] for e in g.drain_events("client")]
            self.assertEqual(seen, ["dialogue_started", "line"])
        finally:
            g.close()
            fake.stop()


class Events(unittest.TestCase):
    def test_every_event_exactly_once_under_concurrency(self):
        fake = _fake()
        g = Game(port=fake.port)
        stop = threading.Event()
        got = {"server": [], "client": []}
        lock = threading.Lock()

        def emitter():
            while not stop.is_set():
                fake.emit("server", "tick")
                fake.emit("client", "line")
                time.sleep(0.003)

        def reader(i):
            for k in range(25):
                g.begin_tool()
                if k % 2:
                    g.read([("state.status", {})], [("ui.state", {})])
                else:
                    g.read1("state.nearby")
                evs = g.parallel(lambda: g.drain_events("server"), lambda: g.drain_events("client"))
                with lock:
                    got["server"].extend(e["seq"] for e in evs[0])
                    got["client"].extend(e["seq"] for e in evs[1])

        try:
            em = threading.Thread(target=emitter, daemon=True)
            em.start()
            rs = [threading.Thread(target=reader, args=(i,)) for i in range(3)]
            for r in rs:
                r.start()
            for r in rs:
                r.join()
            stop.set()
            em.join()
            for ctx in ("server", "client"):
                tail = g.drain_events(ctx)
                seqs = got[ctx] + [e["seq"] for e in tail]
                self.assertEqual(len(seqs), len(set(seqs)), f"{ctx}: an event was delivered twice")
                self.assertEqual(sorted(seqs), list(range(1, fake.seq[ctx] + 1)), f"{ctx}: an event was lost")
        finally:
            g.close()
            fake.stop()

    def test_stashed_events_reach_the_next_drain(self):
        fake = _fake()
        g = Game(port=fake.port)
        try:
            fake.emit("server", "quest_updated")
            g.begin_tool()
            g.read1("state.nearby")
            self.assertEqual([e["kind"] for e in g.stashed_events("server")], ["quest_updated"])
            g.begin_tool()  # a different tool: its drain must ask the game again, and still get the stash
            fake.emit("server", "item_added")
            self.assertEqual([e["kind"] for e in g.drain_events("server")], ["quest_updated", "item_added"])
            self.assertEqual(g.drain_events("server"), [])
        finally:
            g.close()
            fake.stop()


class Pool(unittest.TestCase):
    def test_single_connection_debugger_still_works(self):
        fake = _fake(max_conns=1)  # bg3se-style: a second connection is dropped
        g = Game(port=fake.port, pool_max=4)
        try:
            for _ in range(3):
                g.begin_tool()
                s, c = g.read([("state.status", {})], [("ui.state", {})])
                self.assertEqual(s[0]["active"], P1)
            self.assertLessEqual(fake.peak_conns, 1)
        finally:
            g.close()
            fake.stop()

    def test_pool_caps_connections(self):
        fake = _fake()
        g = Game(port=fake.port, pool_max=3)
        try:
            ts = [threading.Thread(target=lambda: [g.rpc("state.status") for _ in range(5)]) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertLessEqual(fake.peak_conns, 3)
            self.assertGreaterEqual(fake.peak_conns, 2)
        finally:
            g.close()
            fake.stop()

    def test_metrics_count_only_the_tools_own_rpcs(self):
        fake = _fake()
        g = Game(port=fake.port)
        g.metrics = rec = M.Recorder(None)
        try:
            with _Queue(g):
                time.sleep(0.25)
                t0 = time.time()
                g.begin_tool()
                g.read([("state.status", {})], [("ui.state", {})])
                r = rec.tool_done("get_status", (time.time() - t0) * 1000)
            self.assertEqual(r["rpc_n"], 2)  # the queue's job.poll calls in the window aren't this tool's
            self.assertEqual(sorted(o[0] for o in r["ops"]), ["state.status+events.since", "ui.state+events.since"])
        finally:
            g.close()
            fake.stop()


if __name__ == "__main__":
    unittest.main()
