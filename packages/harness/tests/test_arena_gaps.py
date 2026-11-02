# pyright: basic
"""Phase numbers with gaps (arena-mod beb7fc2d): a phase whose members' turns all pass takes its number and gets no
arena_turn, only arena_turn_passed records, so facts read B p1, B p3 (A's p2 passed). Nothing in the runner (turns,
caps, forfeits, the stall clock) or the bench's grader counts on contiguous numbers or alternating sides."""

from __future__ import annotations

from typing import Any

import jsonschema
from bg3harness.arena import match as am
from bg3harness.arena.spec import ArenaSpec
from bg3harness.testing.scripted import FakeClock, GameModel, Member
from test_arena_match import (
    ARENA,
    BENCH_FACTS_SCHEMA,
    SIDES,
    Agents,
    ArenaModel,
    Clock,
    Game,
    arena_events,
    attacker,
    grade,
    passer,
    staller,
)


class PassingModel(ArenaModel):
    """ArenaModel whose `passes` members can't act: when one's turn starts, the mod passes it (arena_turn_passed), and
    a phase of only such members takes its phase number with no arena_turn (Arena.lua passCantAct)."""

    def __init__(self, clock: FakeClock, order: list[str], passes: set[str]) -> None:
        super().__init__(clock, True, order)
        self.passes = passes

    def _push_turn(self, kind: str, who: Member | None = None, rnd: int | None = None) -> None:
        a = self.arena
        if kind != "turn_started" or a is None or self.ended is not None or a.acting.name not in self.passes:
            super()._push_turn(kind, who, rnd)
            return
        GameModel._push_turn(self, kind, who, rnd)  # the game's own TurnStarted
        now = a.acting
        side = a.side_of(now.uuid)
        self.mod_round = max(self.mod_round, 1)
        r, last, n = self.phase
        if (r, last) != (self.mod_round, side):
            self.phase = (self.mod_round, side, n + 1 if r == self.mod_round else 1)
        self.ref_record("arena_turn_passed", side=side, character=now.uuid, name=now.name, why="downed",
                        round=self.mod_round)  # fmt: skip
        self.arena_end_turn()


def setup_passing(plays: dict[str, Any], passes: set[str]):
    mc = FakeClock()
    model = PassingModel(mc, ["Wyll", "Karlach", "Halsin", "Gale"], passes)
    game = Game(model)
    clock = Clock(mc)
    return model, game, clock, Agents(game, clock, plays), ArenaSpec.from_dict(ARENA)


def phases(out) -> list[tuple[str, int, int]]:
    return [(e["side"], e["round"], e["phase"]) for e in arena_events(out) if e["ev"] == "arena_turn"]


def test_a_passed_phase_leaves_a_gap_and_the_match_grades_as_any(tmp_path):
    """Round 1 is B [Wyll] p1 | A [Karlach] passed p2 | B [Halsin] p3 | A [Gale] p4: two B phases in a row in facts,
    each its own turn, the passed member's record kept, and the wipe graded valid."""
    _model, game, clock, agents, spec = setup_passing({"A": attacker, "B": passer}, {"Karlach"})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and out.end is not None and (out.end["reason"], out.end["winner"]) == ("wipe", "A")
    assert phases(tmp_path)[:3] == [("B", 1, 1), ("B", 1, 3), ("A", 1, 4)]
    passed = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_passed"]
    assert passed and {(e["side"], e["character"], e["why"]) for e in passed} == {("A", "Karlach", "downed")}
    for e in arena_events(tmp_path):
        jsonschema.validate(e, BENCH_FACTS_SCHEMA)
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"]) == (True, "A", "wipe")
    assert g["turns"]["A"] == len([p for p in phases(tmp_path) if p[0] == "A"])
    assert g["turns"]["B"] == len([p for p in phases(tmp_path) if p[0] == "B"])


def test_forced_turns_count_across_a_passed_phase_and_its_gap(tmp_path):
    """A's only acting member stalls every round with Karlach's phase passed in between: three of A's turns forced
    in a row forfeit, in the runner and in the grader alike; B's two phases in a row are two turns."""
    _model, game, clock, agents, spec = setup_passing({"A": staller, "B": passer}, {"Karlach"})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["forfeit"], out.end["cause"]) == (
        "forfeit",
        "A",
        "forced_turns",
    )
    forced = [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced == ["Gale", "Gale", "Gale"]
    assert [p for p in phases(tmp_path) if p[0] == "A"] == [("A", 1, 4), ("A", 2, 4), ("A", 3, 4)]
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "B", {"side": "A", "cause": "forced_turns"})
