# pyright: basic
"""The arena's camera (Client/ArenaCamera.lua): the runner puts the game camera on the fight each tick."""

from __future__ import annotations

import pytest
from bg3harness.arena import match as am
from test_arena_match import SIDES, passer, setup


@pytest.mark.parametrize(
    ("error", "skips", "logged"),
    [
        (None, 0, ["on the fighters"]),
        (None, 2, ["skipped: side A's input", "on the fighters"]),
        ("no game camera (GameCameraBehavior)", 0, ["failed: no game camera (GameCameraBehavior)"]),
    ],
)
def test_each_tick_puts_the_camera_on_the_fight_and_its_answers_are_logged_when_they_change(
    tmp_path, error, skips, logged
):
    """Every tick of a match still on calls arena.camera in the client context with no side (the harness's op). Its
    answer is logged when it changes (live window 6: a camera that skipped every tick left no line in the runner's
    log), and a failing camera never ends the match."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    model.camera_error, model.camera_skips = error, skips
    logs: list[str] = []
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, log=logs.append)
    assert out.invalid is None and out.end is not None and out.end["reason"] == "round_cap"
    loop = [op for op, side in game.calls if op in ("arena.status", "arena.camera") and side is None]
    loop = loop[loop.index("arena.camera") - 1 :]
    assert loop == ["arena.status", "arena.camera"] * model.cameras + ["arena.status"]  # the last saw the end
    assert model.cameras > skips
    assert [m.split("arena.camera ", 1)[1] for m in logs if "arena.camera" in m] == logged
