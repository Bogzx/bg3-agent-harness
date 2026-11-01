"""Two real bridges (bg3ai-mcp subprocesses, BG3AI_SIDE=A and B) against one fake mod playing a two-team match: every
RPC carries its bridge's side, each side sees its own team as the party and the other as hostile, a side acting out of
its turn or with the other team's character gets the mod's refusal word for word, and a side's my_turn wait ends on
its own team's turn. Both bridges serve the arena tool set (capabilities.ARENA_TOOLS_OFF)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3ai.capabilities import ARENA_TOOLS_OFF
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.integration

TIMEOUT_S = 180
WAIT_POLLS = 3000  # x 10 ms: side B's wait_for reads the fight well within this (a failure, not a race, past it)
TEAM_A = ["Tav", "Shadowheart"]
TEAM_B = ["Lae'zel", "Karlach"]


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def env(side: str) -> dict[str, str]:
    return {"BG3AI_SIDE": side, "BG3AI_TOOLS_OFF": ARENA_TOOLS_OFF}


def waiting(fake: FakeMod, since: int) -> bool:
    """Side B read the fight after call `since` (its wait_for has started)."""
    return ("B", "state.combat") in fake.sided_ops(since)


def test_two_sides_take_turns(tmp_path: Path) -> None:
    model = GameModel(clock=FakeClock())
    model.start_arena(TEAM_A, TEAM_B)  # order: Tav (A), Lae'zel (B), Shadowheart (A), Karlach (B)
    out: dict[str, str] = {}

    async def match_with_wait() -> None:
        with FakeMod(model) as fake:
            async with (
                bridge_session(fake.port, tmp_path / "a", env("A")) as a,
                bridge_session(fake.port, tmp_path / "b", env("B")) as b,
            ):
                out["a_party"] = await call(a, "get_party")
                out["a_combat"] = await call(a, "get_combat_state")
                out["b_combat"] = await call(b, "get_combat_state")

                # Tav's turn (side A): A acts; B's calls are refused at once, nothing done for B
                out["a_move"] = await call(a, "move_to", {"x": 3.0, "y": 0.0, "z": 0.0})
                out["b_move_early"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 0.0})
                out["b_end_early"] = await call(b, "end_turn", {"wait": False})

                # B waits for its turn: once its wait has read the fight (still Tav's turn), A ends Tav's turn
                n = len(fake.calls)
                b_wait = asyncio.create_task(call(b, "wait_for", {"target": "my_turn", "timeout_s": 60}))
                for _ in range(WAIT_POLLS):
                    if waiting(fake, n):
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError(f"side B's wait_for never read the fight: {fake.sided_ops(n)}")
                out["a_end"] = await call(a, "end_turn", {"wait": False})
                out["b_wait"] = await b_wait

                # Lae'zel's turn (side B): B acts; A is refused; B can't act with A's character
                out["b_move"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 0.0})
                out["a_move_late"] = await call(a, "move_to", {"x": 4.0, "y": 0.0, "z": 0.0})
                out["b_with_a"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 1.0, "who": "Shadowheart"})
                out["b_end"] = await call(b, "end_turn", {"wait": False})

                # Shadowheart's turn (side A), hidden from B: B's refusal doesn't name her
                model.hide("Shadowheart")
                out["b_combat_hidden"] = await call(b, "get_combat_state")
                out["b_move_hidden"] = await call(b, "move_to", {"x": 16.0, "y": 0.0, "z": 0.0})

            sides = fake.sides()
            assert set(sides) == {"A", "B"} and sides["A"] > 0 and sides["B"] > 0, sides

    run(match_with_wait())

    # each side's view: own team = party, the other team = hostile
    assert "Tav" in out["a_party"] and "Shadowheart" in out["a_party"]
    assert "Lae'zel" not in out["a_party"] and "Karlach" not in out["a_party"]
    for text, foe in ((out["a_combat"], "Lae'zel"), (out["b_combat"], "Tav")):
        assert foe in text, text

    assert "FAILED" not in out["a_move"] and "not your turn" not in out["a_move"], out["a_move"]
    for key in ("b_move_early", "b_end_early"):
        assert "not your turn: Tav (side A) is acting" in out[key], out[key]
    assert "Lae'zel" in out["b_wait"], out["b_wait"]
    assert "not your turn" not in out["a_end"] and "FAILED" not in out["a_end"], out["a_end"]
    assert "FAILED" not in out["b_move"], out["b_move"]
    assert "not your turn: Lae'zel (side B) is acting" in out["a_move_late"], out["a_move_late"]
    assert "Shadowheart is not on your side" in out["b_with_a"], out["b_with_a"]
    assert "Shadowheart" not in out["b_combat_hidden"], out["b_combat_hidden"]
    assert "not your turn: the other side is acting" in out["b_move_hidden"], out["b_move_hidden"]
    assert "Shadowheart" not in out["b_move_hidden"], out["b_move_hidden"]

    # what the game did: A's move for Tav, B's for Lae'zel, nothing else
    a, b = (model.arena.teams[s] for s in ("A", "B")) if model.arena else ([], [])
    assert [(s, u) for s, u, _ in model.moves] == [("A", a[0].uuid), ("B", b[0].uuid)], model.moves
