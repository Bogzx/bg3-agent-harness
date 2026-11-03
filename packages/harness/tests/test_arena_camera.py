# pyright: basic
"""The arena's camera (Client/ArenaCamera.lua): the runner puts the game camera on the fight each tick."""

from __future__ import annotations

import pytest
from bg3harness.arena import match as am
from test_arena_match import SIDES, attacker, passer, setup


@pytest.mark.parametrize("error", [None, "no game camera (GameCameraBehavior)"])
def test_each_tick_puts_the_camera_on_the_fight_and_its_errors_never_end_the_match(tmp_path, error):
    """Every tick of a match still on calls arena.camera in the client context with no side (the harness's op);
    a failing camera is logged once and the match plays to its end as without it."""
    model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    model.camera_error = error
    logs: list[str] = []
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, log=logs.append)
    assert out.invalid is None
    assert out.end == {"winner": "A", "reason": "wipe", "rounds": 1, "hp_left_A": 1.0, "hp_left_B": 0.0}
    loop = [op for op, side in game.calls if op in ("arena.status", "arena.camera") and side is None]
    loop = loop[loop.index("arena.camera") - 1 :]
    assert loop == ["arena.status", "arena.camera"] * model.cameras + ["arena.status"]  # the last saw the end
    assert model.cameras >= 1
    assert sum("arena.camera" in m for m in logs) == (1 if error else 0)
