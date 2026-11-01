"""Two real bridges (bg3ai-mcp subprocesses, BG3AI_SIDE=A and B) against one fake mod playing a two-team match: every
RPC carries its bridge's side, each side sees its own team as the party and the other as hostile, a side acting out of
its turn or with the other team's character gets the mod's refusal word for word, and a side's my_turn wait ends on
its own team's turn. Both bridges serve the arena tool list (BG3AI_TOOLS_OFF=arena-v1)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
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
    return {"BG3AI_SIDE": side, "BG3AI_TOOLS_OFF": "arena-v1"}


def waiting(fake: FakeMod, since: int) -> bool:
    """Side B read the fight after call `since` (its wait_for has started)."""
    return ("B", "state.combat") in fake.sided_ops(since)


# What only side A may know: its hidden character and her cast, its spell ids, its reaction prompt's options.
A_ONLY = ("Shadowheart", "Bless", "Shield", "Projectile_FireBolt", "Target_Bless")
A_SHOWN = ("Shadowheart", "Bless", "Shield")  # what A's own results show of them (casts render by name)


def test_two_sides_take_turns(tmp_path: Path) -> None:
    model = GameModel(clock=FakeClock())
    model.start_arena(TEAM_A, TEAM_B)  # order: Tav (A), Lae'zel (B), Shadowheart (A), Karlach (B)
    model.hide("Shadowheart")  # side B never sees her
    a_out: dict[str, str] = {}
    b_out: dict[str, str] = {}

    async def match() -> None:
        with FakeMod(model) as fake:
            async with (
                bridge_session(fake.port, tmp_path / "a", env("A")) as a,
                bridge_session(fake.port, tmp_path / "b", env("B")) as b,
            ):
                tools = {t.name for t in (await a.list_tools()).tools}
                assert {"end_turn", "combat_turn", "use_ability", "wait_for", "move_to", "react"} <= tools
                assert not {"travel_to", "long_rest", "load_game", "memory_read", "screenshot", "get_dialogue"} & tools

                a_out["party"] = await call(a, "get_party")
                a_out["combat"] = await call(a, "get_combat_state")
                b_out["combat"] = await call(b, "get_combat_state")

                # Tav's turn (side A): A acts; B's calls are refused at once, nothing done for B
                a_out["move"] = await call(a, "move_to", {"x": 3.0, "y": 0.0, "z": 0.0})
                model.arena_cast("Tav", "Projectile_FireBolt", "Fire Bolt")
                model.arena_damage("Lae'zel", "Tav", 5)
                b_out["move_early"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 0.0})
                b_out["end_early"] = await call(b, "end_turn", {"wait": False})

                # B waits for its turn: once its wait has read the fight (still Tav's turn), A ends Tav's turn
                n = len(fake.calls)
                b_wait = asyncio.create_task(call(b, "wait_for", {"target": "my_turn", "timeout_s": 60}))
                for _ in range(WAIT_POLLS):
                    if waiting(fake, n):
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError(f"side B's wait_for never read the fight: {fake.sided_ops(n)}")
                a_out["end"] = await call(a, "end_turn", {"wait": False})
                b_out["wait"] = await b_wait

                # Lae'zel's turn (side B): B acts; Tav gets a reaction prompt (A's to answer, waiting for B)
                b_out["move"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 0.0})
                model.open_reaction("Tav")
                b_out["status_prompt"] = await call(b, "get_status", {"full": True})
                b_out["react"] = await call(b, "react", {"use": True})
                a_out["status_prompt"] = await call(a, "get_status", {"full": True})
                a_out["react"] = await call(a, "react", {"use": False})
                a_out["move_late"] = await call(a, "move_to", {"x": 4.0, "y": 0.0, "z": 0.0})
                b_out["with_a"] = await call(b, "move_to", {"x": 17.0, "y": 0.0, "z": 1.0, "who": "Tav"})
                b_out["end"] = await call(b, "end_turn", {"wait": False})

                # Shadowheart's turn (side A), unseen by B
                model.arena_cast("Shadowheart", "Target_Bless", "Bless")
                model.arena_damage("Lae'zel", "Shadowheart", 4)
                b_out["combat_hidden"] = await call(b, "get_combat_state")
                b_out["events_hidden"] = await call(b, "get_events")
                b_out["move_hidden"] = await call(b, "move_to", {"x": 16.0, "y": 0.0, "z": 0.0})
                a_out["events"] = await call(a, "get_events")

            sides = fake.sides()
            assert set(sides) == {"A", "B"} and sides["A"] > 0 and sides["B"] > 0, sides

    run(match())

    # each side's view: own team = party, the other team = hostile
    assert "Tav" in a_out["party"] and "Shadowheart" in a_out["party"]
    assert "Lae'zel" not in a_out["party"] and "Karlach" not in a_out["party"]
    assert "Lae'zel" in a_out["combat"] and "Tav" in b_out["combat"]

    assert "FAILED" not in a_out["move"], a_out["move"]
    for key in ("move_early", "end_early"):
        assert "not your turn (side A is acting)" in b_out[key], b_out[key]
    assert "Lae'zel" in b_out["wait"], b_out["wait"]
    assert "FAILED" not in a_out["end"], a_out["end"]
    assert "FAILED" not in b_out["move"], b_out["move"]

    # the reaction prompt: A's to answer; B is told the game waits on the other side, and is refused if it answers
    assert "REACTION" in a_out["status_prompt"] and "Shield" in a_out["status_prompt"], a_out["status_prompt"]
    assert "REACTION" not in b_out["status_prompt"], b_out["status_prompt"]
    assert "trade" not in b_out["status_prompt"], b_out["status_prompt"]  # ALERTS name no tool arena-v1 removes
    assert "WAITING a reaction prompt of the other side is open" in b_out["status_prompt"], b_out["status_prompt"]
    assert "not your reaction" in b_out["react"], b_out["react"]
    assert "FAILED" not in a_out["react"] and model.reacted == [("Tav", False)], a_out["react"]

    assert "not your turn (side B is acting)" in a_out["move_late"], a_out["move_late"]
    assert "Tav is not on your side" in b_out["with_a"], b_out["with_a"]
    assert "not your turn (side A is acting)" in b_out["move_hidden"], b_out["move_hidden"]
    assert "Fire Bolt" in b_out["move_early"], b_out["move_early"]  # Tav's cast, seen: by name, no spell id
    assert "Someone" in b_out["combat_hidden"], b_out["combat_hidden"]  # the unseen Shadowheart's damage

    # side B never reads an A-only fact; side A has them all
    for key, text in b_out.items():
        leaked = [f for f in A_ONLY if f in text]
        assert not leaked, f"side B's {key} shows {leaked}:\n{text}"
    a_text = "\n".join(a_out.values())
    assert all(f in a_text for f in A_SHOWN), a_text

    # what the game did: A's move for Tav, B's for Lae'zel, nothing else
    assert model.arena is not None
    tav, lae = model.arena.teams["A"][0].uuid, model.arena.teams["B"][0].uuid
    assert [(s, u) for s, u, _ in model.moves] == [("A", tav), ("B", lae)], model.moves
