# pyright: basic
"""A match on the live game keeps its nav files out of the shared Script Extender folder (live B1 smoke 2026-11-05: a
match replaced the avatar's nav mirror and left a guard file and scenario.json), on every way out of arena.live.play."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bg3harness.arena import live
from bg3harness.arena.match import ArenaOutcome, ArenaSetupError
from bg3harness.arena.spec import ArenaSpec
from bg3harness.runner.run_isolation import isolate_nav, nav_marker

ARENA = {
    "name": "test_field",
    "save": {"name": "Tav-1__SCN-arena_test", "sha1": "0" * 40},
    "teams": {"A": [{"name": "Karlach"}], "B": [{"name": "Wyll"}]},
    "seed": 7,
    "karmic": False,
    "round_cap": 20,
    "wall_cap_min": 30,
    "turn_cap_s": 60,
    "condition": "rules",
    "tools_off": "arena-v1",
}
NAV, GUARD, SCENARIO = "nav_host.json", "nav_guard_host.json", "scenario.json"


class Runner:
    abort = False

    def __init__(self, run_dir: Path, end_fails: bool = False) -> None:
        self.state = SimpleNamespace(dir=run_dir)
        self.end_fails = end_fails

    def facts_writer(self) -> None:
        return None

    def scenario_undo(self) -> None:
        pass

    def end_run_game(self, when: str, after: str) -> None:
        if self.end_fails:
            raise OSError("the game would not stop")


@pytest.fixture
def match(tmp_path, monkeypatch):
    """play() with the game, the agents and the match stood in; returns (run, se, seen). `start` is what start_game
    does (default: note what the game finds, then write the match's own files)."""
    se = tmp_path / "se"
    se.mkdir()
    (se / NAV).write_text("the player's")
    (se / GUARD).write_text("the player's guard")
    monkeypatch.setattr(live.renv, "SE_BG3AI_DIR", se)
    monkeypatch.setattr(live.renv, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(live.run_host, "bench_recording", lambda env: None)
    monkeypatch.setattr(live.run_host, "machine_pins", lambda env: {})
    monkeypatch.setattr(live, "budget_refusal", lambda sides, cost: None)
    monkeypatch.setattr(live, "side_client", lambda *a: (object(), {}))
    monkeypatch.setattr(live.aagents, "LiveAgents", lambda *a: object())
    monkeypatch.setattr(live.aagents, "summary", lambda exits: {})
    seen: dict[str, Any] = {}

    def start(r, spec):
        seen["at_start"] = sorted(p.name for p in se.iterdir())
        (se / NAV).write_text("the match's")
        (se / SCENARIO).write_text('{"renames":[]}')

    def run(arena=lambda *a, **k: ArenaOutcome(end={}), start_game=start, end_fails=False, keep_game=False):
        monkeypatch.setattr(live, "run_arena", arena)
        monkeypatch.setattr(live, "start_game", start_game)
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        sides: dict[str, Any] = {s: SimpleNamespace(player=s, profile=None) for s in "AB"}
        r = Runner(run_dir, end_fails)
        spec = ArenaSpec.from_dict(ARENA)
        return live.play(spec, sides, lambda opt: r, "run", {}, log=lambda s: None, keep_game=keep_game), run_dir

    return run, se, seen


def assert_put_back(se: Path) -> None:
    assert sorted(p.name for p in se.iterdir()) == [GUARD, NAV]
    assert (se / NAV).read_text() == "the player's"
    assert (se / GUARD).read_text() == "the player's guard"
    assert not nav_marker().exists()


def test_a_match_starts_with_no_nav_files_and_puts_the_players_back(match):
    run, se, seen = match
    _, run_dir = run()
    assert seen["at_start"] == []
    assert_put_back(se)
    assert (run_dir / "reset" / "run" / NAV).read_text() == "the match's"  # the match's own stay with its run
    assert (run_dir / "reset" / "run" / SCENARIO).is_file()


def test_a_match_that_raises_puts_them_back(match):
    run, se, _ = match

    def crash(*a, **k):
        raise RuntimeError("the match broke")

    with pytest.raises(RuntimeError, match="the match broke"):
        run(arena=crash)
    assert_put_back(se)


def test_a_failed_start_puts_them_back(match):
    run, se, _ = match

    def refused(r, spec):  # the game came up and wrote its files before the save check refused
        (se / NAV).write_text("the match's")
        raise ArenaSetupError("the arena's save didn't load")

    with pytest.raises(ArenaSetupError):
        run(start_game=refused)
    assert_put_back(se)


def test_a_game_that_will_not_stop_still_puts_them_back(match):
    run, se, _ = match
    with pytest.raises(OSError, match="would not stop"):
        run(end_fails=True)
    assert_put_back(se)


def test_a_match_restores_what_a_dead_match_left_moved(match, tmp_path):
    run, se, seen = match
    isolate_nav(se, tmp_path / "dead" / "reset", nav_marker(), True)  # the dead match's marker, its files still moved
    assert json.loads(nav_marker().read_text())["files"] == [GUARD, NAV]
    run()
    assert seen["at_start"] == []
    assert_put_back(se)


def test_a_game_kept_up_leaves_the_nav_files_moved_for_the_next_start(match):
    run, se, seen = match
    run(keep_game=True)
    assert (se / NAV).read_text() == "the match's"  # the game is up and still writes it: nothing goes back under it
    assert nav_marker().exists()
    run()
    assert seen["at_start"] == []
    assert_put_back(se)
