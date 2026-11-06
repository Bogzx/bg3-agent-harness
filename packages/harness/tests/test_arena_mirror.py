# pyright: basic
"""run_arena on a mirror arena offline (bg3harness.arena.match, step 1): the twins' uuids come from arena.setup's
result, never the arena file, and a result without them starts no match; on test_arena_match's ArenaModel."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from bg3harness.arena import match as am
from test_arena_match import ARENA, SIDES, Game, arena_events, attacker, grade, passer, setup


def mirror_pair(cls: str, a: str, b: str) -> dict[str, Any]:
    names = {"A": a, "B": b}
    return {"class": cls, "template": "t-" + cls, "race": "r-" + cls, "stats": "S_" + cls, "names": names, "kit": {}}


MIRROR = dict(
    ARENA,
    teams={"A": [{"name": "Ash"}, {"name": "Cedar"}], "B": [{"name": "Birch"}, {"name": "Dune"}]},
    mirror={"pairs": [mirror_pair("Fighter", "Ash", "Birch"), mirror_pair("Rogue", "Cedar", "Dune")]},
)


def setup_result(game: Game, change: Callable[[dict[str, Any]], None]) -> None:
    """arena.setup's job result, changed by `change` (the job settles in the call)."""
    rpc = game.rpc

    def rpc_changed(op: str, args: dict[str, Any], side: str | None = None, context: str = "server") -> Any:
        r = rpc(op, args, side, context)
        if op == "arena.setup" and isinstance(r, dict) and isinstance(r.get("result"), dict):
            change(r["result"])
        return r

    game.rpc = rpc_changed  # type: ignore[method-assign]


def test_a_mirror_arenas_uuids_come_from_the_setup_result_never_the_file(tmp_path):
    """A mirror arena's file names the twins only: the match keys on the uuids arena.setup's result gives (pair i at
    index i), and arena_start carries them."""
    model, game, clock, agents, spec = setup({"A": attacker, "B": passer}, MIRROR)
    assert spec.mirror and spec.uuids == {"A": (None, None), "B": (None, None)}
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and out.end is not None and out.end["reason"] == "wipe"
    made = model.team_uuids()
    [start] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_start"]
    assert start["uuids"] == made and start["teams"] == {"A": ["Ash", "Cedar"], "B": ["Birch", "Dune"]}
    assert model.setup_args["arena"]["mirror"] == MIRROR["mirror"]  # the file goes to arena.setup as it is
    assert grade(tmp_path, MIRROR)["result"] == "A"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda r: r.pop("teams"), "arena.setup's result gives team A of the mirror arena None, not 2 uuids"),
        (lambda r: r["teams"]["B"].pop(), "arena.setup's result gives team B of the mirror arena ["),
        (lambda r: r["teams"]["A"].reverse(), "arena.status team A is ["),
    ],
    ids=["no teams", "short", "disagrees with status"],
)
def test_a_mirror_setup_result_without_its_twins_starts_no_match(tmp_path, change, why):
    """Fail closed: a mirror arena's setup result without its teams, short of a twin, or not the teams arena.status
    reports is a setup error: no agent starts, no arena record is written."""
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer}, MIRROR)
    setup_result(game, change)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is None and out.invalid is not None and out.invalid.startswith("setup: ArenaSetupError: " + why)
    assert agents.started == [] and not arena_events(tmp_path)
