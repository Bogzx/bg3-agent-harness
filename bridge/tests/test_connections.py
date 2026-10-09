"""The game's LuaDebug connection count stays bounded over a long run.

Live, se saw bg3le log "debug: client attached (371 connected)" over one session. Cause: the runner's
port_open() health probe (every few seconds) was a bare connect-and-close; bg3le replays its status lines
to each new client first, that send fails once the probe is gone, and unpatched bg3le then returns without
closing the fd or dropping the client (patches/bg3le/0004 fixes the server side). The fake models both.

  uv run --project bridge python -m unittest bridge/tests/test_connections.py -v
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fake_bg3le import FakeBg3le  # noqa: E402

from bg3ai.game import Game, GameError  # noqa: E402
from bg3ai.luadebug import port_answers  # noqa: E402

P1 = "11111111-2222-3333-4444-555555555555"


def _fake(**kw) -> FakeBg3le:
    f = FakeBg3le(server_tick_s=0.005, client_tick_s=0.005, status_lines=128, leak_failed_replay=True, **kw)
    f.op("state.status", lambda a, c: {"party": [], "active": P1})
    f.op("ui.state", lambda a, c: {"screen": "game"})
    f.op("job.poll", lambda a, c: {"status": "running"})
    f.op("act.watch", lambda a, c: {"party": []})

    def boom(a, c):
        raise RuntimeError("not visible")
    f.op("state.entity", boom)
    return f.start()


def _wait_conns(fake: FakeBg3le, n: int, timeout: float = 3.0) -> int:
    end = time.monotonic() + timeout
    while fake.conns != n and time.monotonic() < end:
        time.sleep(0.01)
    return fake.conns


class Probe(unittest.TestCase):
    def test_bare_connect_close_leaks_on_unpatched_bg3le(self):
        """The fake reproduces the live leak (so the next test means something)."""
        fake = _fake()
        try:
            for _ in range(20):
                with socket.create_connection(("127.0.0.1", fake.port), timeout=5):
                    pass
                time.sleep(0.01)  # live probes are seconds apart; don't overflow the fake's accept backlog
            time.sleep(0.3)
            self.assertGreater(fake.leaked, 10)
        finally:
            fake.stop()

    def test_port_answers_leaves_nothing_behind(self):
        fake = _fake()
        try:
            for _ in range(100):
                self.assertTrue(port_answers("127.0.0.1", fake.port, 1.0))
            self.assertEqual(_wait_conns(fake, 0), 0)
            self.assertEqual(fake.leaked, 0)
        finally:
            fake.stop()

    def test_port_answers_false_when_nothing_listens(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(port_answers("127.0.0.1", port, 0.5))


class LongRun(unittest.TestCase):
    def test_long_run_keeps_connections_bounded(self):
        """An MCP server's worth of Games (agent, interrupt watch, sampler) hammered from many threads,
        with failing ops, background drains, a closed-and-reused pool and runner-style port probes:
        the game never sees more than the pools' total, and nothing is left once they're closed."""
        fake = _fake()
        games = [Game(port=fake.port, pool_max=4) for _ in range(3)]
        cap = sum(g.pool_max for g in games)
        stop = threading.Event()
        errors: list[BaseException] = []

        def worker(g: Game, i: int):
            try:
                k = 0
                while not stop.is_set():
                    k += 1
                    g.begin_tool()
                    if k % 3 == 0:
                        g.read([("state.status", {})], [("ui.state", {})])
                    elif k % 3 == 1:
                        g.read1("state.status")  # client events drained on a background thread
                        g.parallel(lambda: g.drain_events("server"), lambda: g.drain_events("client"))
                    else:
                        try:
                            g.rpc("state.entity", {"uuid": "x"})
                        except GameError:
                            pass
                    if i == 0 and k % 25 == 0:
                        g.close()  # session end / dashboard error path: the next RPC reopens the pool
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def prober():
            while not stop.is_set():
                port_answers("127.0.0.1", fake.port, 1.0)
                time.sleep(0.01)

        ts = [threading.Thread(target=worker, args=(g, i), daemon=True) for g in games for i in range(4)]
        ts.append(threading.Thread(target=prober, daemon=True))
        try:
            for t in ts:
                t.start()
            time.sleep(3.0)
            stop.set()
            for t in ts:
                t.join(10)
            self.assertEqual(errors, [])
            # + the probe's short-lived connection, + one pool's worth while a close()d pool's sockets are
            # still being torn down on the game side as the next RPC reopens it. Not growing with run time.
            self.assertLessEqual(fake.peak_conns, cap + 1 + games[0].pool_max)
            for g in games:
                self.assertLessEqual(g.open_connections, g.pool_max)
            for g in games:
                for t in list(g._bg_drain.values()):
                    if t is not None:
                        t.join(5)
                g.close()
                self.assertEqual(g.open_connections, 0)
            self.assertEqual(_wait_conns(fake, 0), 0)
            self.assertEqual(fake.leaked, 0)
        finally:
            stop.set()
            for g in games:
                g.close()
            fake.stop()

    def test_release_after_close_closes_the_connection(self):
        """A connection in flight when close() runs must not go back into the closed pool's idle list."""
        fake = FakeBg3le(server_tick_s=0.2, client_tick_s=0.2).start()
        fake.op("state.status", lambda a, c: {})
        g = Game(port=fake.port, pool_max=2)
        try:
            t = threading.Thread(target=lambda: g.rpc("state.status"))
            t.start()
            time.sleep(0.05)  # the eval is waiting for the tick
            g.close()
            t.join(5)
            self.assertEqual(g.open_connections, 0)
            self.assertEqual(_wait_conns(fake, 0), 0)
        finally:
            g.close()
            fake.stop()


if __name__ == "__main__":
    unittest.main()
