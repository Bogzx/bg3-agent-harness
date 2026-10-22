# pyright: basic
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""A new-game bench run plays its scenario's difficulty and Karmic Dice, and the run is invalid when the game's differ.

live7 (research/ovh/naut-old-vs-new.md): nautiloid.json says Balanced, the agent's new_game picked Explorer, nothing
enforced it and the settings check could only say `settings_unverified` (the readout had no difficulty reader).
Now LiveRunHost gives the bridge the scenario's difficulty (BG3AI_SCENARIO_DIFFICULTY), the bridge's new_game starts
that one whatever the agent asks, the runner sets the scenario's Karmic Dice (scn.karmic) and reads both back after the
first load (scn.difficulty, roll.snapshot), and a contradiction or a setting it can't confirm is a harness failure.

The bridge runs for real (bg3ai-mcp over stdio) against the fake mod (GameModel); the readout talks to the same model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bg3harness import game_settings, runs, scenario_file
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel
from test_run_host import JOB, NEW_SC, SAVE_SC, Rig, rig, run_dir  # noqa: F401  (rig: the fixture)

pytestmark = pytest.mark.integration


def rpc(model: GameModel):
    """game.rpc against the fake mod's model (what the runner's readout calls)."""

    def call_op(op: str, args: dict[str, Any]) -> Any:
        reply = model.reply("server", op, args)
        assert reply is not None, op
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]

    return call_op


def agent_new_game(env: dict[str, str], home: Path, asked: str) -> tuple[GameModel, str]:
    """The agent calls new_game(asked) through the real bridge with the run's env; the fake game's difficulty after."""
    model = GameModel(clock=FakeClock())
    model.difficulty = "Explorer"  # the main menu's last pick

    async def go() -> str:
        with FakeMod(model) as fake:
            extra = {k: v for k, v in env.items() if k.startswith("BG3AI_")} | {"BG3AI_PLAYTHROUGH": "bench"}
            async with bridge_session(fake.port, home, extra) as s:
                return await call(s, "new_game", {"difficulty": asked})

    return model, asyncio.run(asyncio.wait_for(go(), 120))


def test_a_new_game_run_starts_the_scenarios_difficulty_whatever_the_agent_picks(rig: Rig, tmp_path: Path):
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))  # NEW_SC: Balanced, karmic dice off
    assert rig.h.env[H.SCENARIO_DIFFICULTY] == "Balanced"
    model, text = agent_new_game(rig.h.env, tmp_path / "home", "Explorer")
    assert model.difficulty == "Balanced"
    assert "this run's scenario fixes the difficulty: Balanced (not Explorer)" in text
    model, text = agent_new_game(rig.h.env, tmp_path / "home2", "balanced")  # the same pick: no note
    assert model.difficulty == "Balanced" and "fixes the difficulty" not in text


def test_a_save_start_or_a_scenario_without_a_difficulty_leaves_the_pick_alone(rig: Rig, tmp_path: Path):
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))  # the next job: a save start (its save fixes the difficulty)
    assert H.SCENARIO_DIFFICULTY not in rig.h.env
    model, _ = agent_new_game(rig.h.env, tmp_path / "home", "Tactician")
    assert model.difficulty == "Tactician"


def readback(model: GameModel, want: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The runner's first-load step (game_setup): the scenario's karmic dice set, the readout, the settings check."""
    call_op = rpc(model)
    if want.get("karmic_dice") is not None:
        game_settings.apply_karmic(call_op, want["karmic_dice"])
    got = game_settings.readout({}, call_op)
    return scenario_file.mismatches(want, got)


def test_the_settings_check_reads_the_difficulty_and_sets_the_karmic_dice():
    want = {"difficulty": "Balanced", "karmic_dice": False}
    model = GameModel(clock=FakeClock())
    model.difficulty, model.roll_mode = "DIFFICULTY_MEDIUM", "WeightedRandom"  # the mod's ruleset name; karmic on
    assert readback(model, want) == ([], [])  # Balanced; karmic now off
    assert model.roll_mode == "Random"
    model.difficulty = "DIFFICULTY_EASY"  # live7: the agent's Explorer
    assert readback(model, want) == (["difficulty is 'Explorer', the scenario needs 'Balanced'"], [])
    model.scenario_setup = False  # no scn.* switch: neither can be read back
    assert readback(model, {"difficulty": "Balanced"}) == ([], ["difficulty"])


def test_a_settings_mismatch_or_an_unverified_setting_makes_the_run_invalid():
    for kind in ("settings_mismatch", "settings_unverified"):
        fails, _ = runs.event_failures([{"t": 5.0, "kind": kind, "problems": ["difficulty is 'Explorer'"]}])
        assert [(f["kind"], f["class"]) for f in fails] == [(kind, runs.HARNESS)]


def test_the_game_gets_the_scn_switch_when_the_scenario_fixes_a_setting():
    # without it scn.difficulty / scn.karmic are refused (live7's unseeded run: unverified)
    from types import SimpleNamespace

    from bg3harness.runner.checks_side import ChecksSide

    def env(seed: int | None, settings: dict[str, Any] | None) -> dict[str, str]:
        return ChecksSide.setup_env(SimpleNamespace(opt=SimpleNamespace(seed=seed, scenario_settings=settings)))  # pyright: ignore[reportArgumentType]

    on = {game_settings.SETUP_ENV: "1"}
    assert env(None, {"difficulty": "Balanced", "karmic_dice": None, "timing": "realtime"}) == on
    assert env(None, {"difficulty": None, "karmic_dice": False}) == on
    assert env(7, None) == on
    assert env(None, {"difficulty": None, "karmic_dice": None, "timing": "turn_based"}) == {}
