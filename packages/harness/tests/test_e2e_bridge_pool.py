"""E2E plan 2 (research/ovh/pkg-harness-analysis.md H): the bridge's RPC pool (bg3ai.game.Game) against the fake
mod when the game refuses one connection.

B2: one refused connect while another connection was open set pool_max to the open count for the server's life, so
every later parallel() read ran serially (two round trips per get_status). The shrink now lasts POOL_RETRY_S on the
pool's clock, then the pool grows again; a game that still takes only one (bg3se) refuses again and it shrinks again.
No sleeps: the test waits on the pool's own condition and drives a fake clock.
"""

from __future__ import annotations

import threading

import pytest
from bg3ai import game as game_mod  # pyright: ignore[reportMissingTypeStubs] - the bridge ships no py.typed
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def refuse_one_while_one_is_open(g: game_mod.Game, fake: FakeMod) -> None:
    """A second connection is refused while the first is in flight; the waiting caller then gets the first."""
    first = g._acquire()  # pyright: ignore[reportPrivateUsage]
    fake.refuse_connects = 1
    got: list[object] = []
    t = threading.Thread(target=lambda: got.append(g._acquire()))  # pyright: ignore[reportPrivateUsage]
    t.start()
    with g._cond:  # pyright: ignore[reportPrivateUsage] - notified in the same hold that shrinks the pool
        assert g._cond.wait_for(lambda: g.pool_max < g.pool_cap, timeout=30)  # pyright: ignore[reportPrivateUsage]
    g._release(first)  # pyright: ignore[reportPrivateUsage]
    t.join(30)
    assert got == [first]
    g._release(first)  # pyright: ignore[reportPrivateUsage]


def test_pool_grows_again_after_a_refused_connect() -> None:
    clock = Clock()
    with FakeMod(GameModel(clock=FakeClock())) as fake:
        g = game_mod.Game(port=fake.port, pool_max=4)
        g.now = clock
        try:
            assert g.rpc("state.status")["party"]
            refuse_one_while_one_is_open(g, fake)
            assert (g.pool_max, g.open_connections) == (1, 1)

            # inside the window: reads stay on the one connection that works, no connect attempts
            clock.t += game_mod.POOL_RETRY_S - 1
            before = fake.connects
            g.parallel(lambda: g.rpc("state.status"), lambda: g.rpc("state.status"))
            assert (g.pool_max, fake.connects) == (1, before)

            # after it: the pool is back to 4, and a second concurrent caller opens a second connection
            clock.t += 1
            assert g.rpc("state.status")["party"]
            assert g.pool_max == 4
            held = g._acquire()  # pyright: ignore[reportPrivateUsage]
            second = g._acquire()  # pyright: ignore[reportPrivateUsage]
            assert held is not second and g.open_connections == 2
            assert fake.connects == before + 1
            g._release(held)  # pyright: ignore[reportPrivateUsage]
            g._release(second)  # pyright: ignore[reportPrivateUsage]
        finally:
            g.close()


def test_a_game_that_takes_one_connection_shrinks_the_pool_again() -> None:
    clock = Clock()
    with FakeMod(GameModel(clock=FakeClock())) as fake:
        g = game_mod.Game(port=fake.port, pool_max=4)
        g.now = clock
        try:
            g.rpc("state.status")
            refuse_one_while_one_is_open(g, fake)
            clock.t += game_mod.POOL_RETRY_S
            g.rpc("state.status")
            assert g.pool_max == 4
            refuse_one_while_one_is_open(g, fake)  # bg3se: still one connection
            assert g.pool_max == 1
            clock.t += game_mod.POOL_RETRY_S - 1
            g.rpc("state.status")
            assert g.pool_max == 1
        finally:
            g.close()
