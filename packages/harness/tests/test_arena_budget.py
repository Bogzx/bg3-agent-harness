# pyright: basic
"""run_arena's money cap offline: max_cost (USD, both sides together) against each side's running cost (its
processes' cost_usd, as a pi client reports it after each model response); on test_arena_match's ArenaModel and
agents."""

from __future__ import annotations

import jsonschema
from bg3harness.arena import match as am
from test_arena_match import (
    BENCH_FACTS_SCHEMA,
    SIDES,
    Game,
    Run,
    arena_events,
    attacker,
    grade,
    my_turn,
    passer,
    setup,
)


def spender(game: Game, side: str, run: Run) -> None:
    """On its turn: one model response a second, $0.05 each (a little model time), and no end: it thinks on."""
    if my_turn(game, side):
        run.model += 0.5
        run.cost = round(run.cost + 0.05, 6)


def end_of(out) -> dict:
    [end] = [e for e in arena_events(out) if e["ev"] == "arena_end"]
    jsonschema.validate(end, BENCH_FACTS_SCHEMA)
    return end


def test_a_match_that_reaches_its_money_cap_mid_phase_ends_over_budget_at_once(tmp_path):
    """A's client spends inside Karlach's phase: at A + B >= max_cost the runner ends the match then (no end_turn,
    no winner, invalid), both agents stopped, the costs so far and the cap in arena_end."""
    model, game, clock, agents, spec = setup({"A": spender, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, max_cost=0.5)
    assert out.end is not None and (out.end["reason"], out.end["winner"]) == ("over_budget", None)
    assert out.invalid == "over_budget: A $0.5100 + B $0.0100 reached max_cost $0.5"
    assert model.ends == [{"reason": "over_budget"}]
    assert all(r.stopped for r in agents.runs.values())
    assert out.cost("A") == 0.51 and out.cost("B") == 0.01
    end = end_of(tmp_path)
    assert (end["reason"], end["rounds"], end["cost"], end["max_cost"]) == (
        "over_budget",
        1,
        {"A": 0.51, "B": 0.01},
        0.5,
    )
    turns = [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn"]
    assert turns == ["Karlach"] and not [c for c in game.calls if c[0] == "act.end_turn"]  # mid-phase
    assert grade(tmp_path)["invalid_reason"] == "over_budget"


def test_an_ended_process_s_cost_counts_toward_the_cap(tmp_path):
    """A's first process exits mid-fight having spent $0.32; its resume spends on: the cap counts both."""

    def exits_then_spends(game: Game, side: str, run: Run) -> None:
        if run.conversation == "conv-A" and run.prompt != am.RESUME:
            run.cost = 0.32
            run.end()
            return
        spender(game, side, run)

    _model, game, clock, agents, spec = setup({"A": exits_then_spends, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, max_cost=0.5)
    assert out.end is not None and out.end["reason"] == "over_budget"
    assert end_of(tmp_path)["cost"] == {"A": 0.53, "B": 0.01}  # 0.32 + the resume's 0.01 + 4 x 0.05
    assert [len(out.exits[s]) for s in ("A", "B")] == [2, 1]


def test_a_match_under_its_cap_ends_as_it_would_and_records_its_costs(tmp_path):
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, max_cost=1.0)
    assert out.invalid is None and out.end is not None and out.end["reason"] == "wipe"
    end = end_of(tmp_path)  # the mod's own end, from the ring
    assert (end["cost"], end["max_cost"]) == ({"A": 0.01, "B": 0.01}, 1.0)
    assert grade(tmp_path)["valid"] is True


def test_without_a_cap_nothing_ends_on_cost_and_arena_end_says_so(tmp_path):
    _model, game, clock, agents, spec = setup({"A": spender, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] != "over_budget"
    assert end_of(tmp_path)["max_cost"] is None


def test_a_side_reaching_its_own_cap_ends_the_match_over_budget(tmp_path):
    """A matrix caps each paid side at its reserve: A's spending reaches its $0.30 while the total is far from $2."""
    model, game, clock, agents, spec = setup({"A": spender, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, max_cost=2.0, side_caps={"A": 0.3, "B": 1.0})
    assert out.end is not None and out.end["reason"] == "over_budget" and model.ends == [{"reason": "over_budget"}]
    assert out.invalid == "over_budget: A $0.3100 + B $0.0100 reached side A's cap $0.3"
    assert end_of(tmp_path)["cost"] == {"A": 0.31, "B": 0.01}


def test_arena_end_s_cost_counts_what_the_agents_spend_after_the_end(tmp_path, monkeypatch):
    """Window 6: arena_end's cost was read at the end moment and missed ~$0.0006 a match, the responses charged
    between the end and the agents' stop; it is written after the stop, equal to the costs charged."""
    stop = Run.stop

    def last_response(self: Run) -> am.AgentExit:
        if not self.stopped:
            self.cost = round(self.cost + 0.0006, 6)
        return stop(self)

    monkeypatch.setattr(Run, "stop", last_response)
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, max_cost=1.0)
    assert out.end is not None and out.end["reason"] == "wipe"
    assert end_of(tmp_path)["cost"] == {"A": out.cost("A"), "B": out.cost("B")} == {"A": 0.0106, "B": 0.0106}
    evs = [e["ev"] for e in arena_events(tmp_path)]
    assert evs.count("arena_end") == 1 and evs[-1] == "arena_end"
