# pyright: basic
"""A side's unanswered reaction prompts (arena/unanswered.py; owner D346, orchestrator D350): the 45 s bound, the charge
on its next turns, its budget, what it is told, and the facts."""

from __future__ import annotations

import types
from pathlib import Path
from unittest import mock

import pytest
from bg3harness.arena import match as am
from bg3harness.arena import unanswered as un
from test_arena_lost_control import goaded
from test_arena_match import ARENA, SIDES, arena_events, grade, my_turn, setup, staller
from test_arena_reactions import open_at_round_2, patient

RULES = Path(am.__file__).resolve().parents[3] / "prompts" / "rules" / "arena.md"
UP = {
    "screen": "reaction",
    "reaction": {"prompt": "Sentinel?", "owner": "B", "options": [{"i": 1, "name": "Sentinel", "who": "Red Ash"}]},
}


def check(reads, ledger=None):
    """The real _reaction_check on (seconds, ui.state) reads: [(op, args)] it sent, the facts, the hold."""
    calls, facts = [], []

    def rpc(op, args=None, context=None):
        calls.append((op, args))
        return {"status": "running"} if op == "ui.react" else game.ui

    game = types.SimpleNamespace(ui={}, rpc=rpc)
    hold = am._Hold(ledger=ledger or un.Unanswered())
    ref = types.SimpleNamespace(round=3, event=lambda ev, **k: facts.append((ev, k)))
    spec = types.SimpleNamespace(name="t", turn_cap_s=90)
    for t, ui in reads:
        game.ui = ui
        am._reaction_check(spec, game, ref, lambda m: None, hold, 1000.0 + t)  # type: ignore[arg-type]
    return [c for c in calls if c[0] != "ui.state"], facts, hold


def test_the_bound_is_45_s_whatever_the_turn_cap():
    assert un.REACTION_ANSWER_S == 45.0
    assert check([(t, UP) for t in range(0, 44, 2)])[0] == []
    sent = check([(t, UP) for t in range(0, 48, 2)])[0]
    assert sent[0] == ("ui.react", {"use": False})


def test_a_decline_records_the_wait_and_tells_only_the_owner():
    sent, facts, hold = check([(t, UP) for t in range(0, 48, 2)])
    assert facts == [
        (
            "arena_reaction_declined",
            {"round": 3, "side": "B", "detail": "Sentinel?", "waited_s": 46.0, "over_budget": False},
        )
    ]
    assert sent[1] == (
        "arena.notice",
        {
            "side": "B",
            "text": "reaction: Sentinel (Red Ash) unanswered for 46 s: declined; 46 s comes off your next turns",
        },
    )
    assert hold.ledger.debt == {"B": 46.0}


def test_a_notice_that_fails_doesnt_end_the_match():
    logged = []

    def rpc(op, args):
        raise RuntimeError("no such op")

    un.notice(types.SimpleNamespace(rpc=rpc), logged.append, "B", "x")
    assert "telling side B failed" in logged[0]


def test_the_budget_ends_once_then_prompts_decline_at_their_first_read():
    ledger = un.Unanswered(budget_s=40.0)
    _sent, facts, _hold = check([(t, UP) for t in range(0, 48, 2)], ledger)
    assert [f[0] for f in facts] == ["arena_reaction_declined", "arena_reaction_budget_spent"]
    assert facts[1][1] == {"side": "B", "round": 3, "waited_s": 46.0, "budget_s": 40.0}
    assert ledger.spent == {"B"}
    sent, facts, _hold = check([(0, UP)], ledger)  # a new prompt, its first read
    assert sent[0] == ("ui.react", {"use": False})
    assert facts == [
        (
            "arena_reaction_declined",
            {"round": 3, "side": "B", "detail": "Sentinel?", "waited_s": 0.0, "over_budget": True},
        )
    ]
    assert "declined at once: your side's 0.666667 min" in sent[1][1]["text"]
    assert ledger.bound("A") == un.REACTION_ANSWER_S  # the other side's budget is its own


def test_a_spent_budget_isnt_reported_again():
    ledger = un.Unanswered(budget_s=10.0)
    assert ledger.declined("B", 20.0) is True
    assert ledger.declined("B", 20.0) is False


def test_a_prompt_nobody_owns_costs_nobody():
    ui = {"screen": "reaction", "reaction": {"prompt": "?", "unowned": True, "options": []}}
    sent, facts, hold = check([(0, ui)])
    assert sent == [("ui.react", {"use": False})]
    assert [f[0] for f in facts] == ["arena_reaction_unowned"]
    assert hold.ledger.debt == {} and hold.ledger.waited == {}


@pytest.mark.parametrize(
    ("debt", "cap", "charged", "left"),
    [(10.0, 90.0, 10.0, 0.0), (45.0, 90.0, 45.0, 0.0), (100.0, 90.0, 45.0, 55.0), (0.0, 90.0, 0.0, 0.0)],
)
def test_the_charge_is_at_most_half_a_cap_and_the_rest_carries(debt, cap, charged, left):
    ledger = un.Unanswered(debt={"A": debt})
    assert ledger.charge("A", cap) == charged
    assert ledger.debt["A"] == left


def test_the_rules_state_the_bound_the_runner_uses():
    text = " ".join(RULES.read_text(encoding="utf-8").split())
    assert f"A prompt unanswered in {un.REACTION_ANSWER_S:g} s is declined" in text
    assert "up to half each" in text and un.CHARGE_SHARE == 0.5
    assert "Past 1/6 of the time limit in waits" in text and un.BUDGET_SHARE == 1 / 6


def notices_of(game):
    """Records arena.notice as a fake mod that takes it."""
    seen, real = [], game.rpc

    def rpc(op, args, side=None, context="server"):
        if op == "arena.notice":
            assert side is None  # the runner's op; the mod scopes it to args.side
            seen.append((args["side"], args["text"]))
            return {"ok": True}
        return real(op, args, side, context=context)

    game.rpc = rpc  # type: ignore[method-assign]
    return seen


def _prompt_then_think(out_dir):
    """Wyll's side leaves a prompt open at round 2, then thinks on. -> B's model time from its first thinking tick to
    the runner's first cap end, the arena's turn cap, what side B was told."""
    first: list[float] = []
    ends: list[float] = []

    def b_play(game, side, run):
        if not model.reacted:
            return patient(game, side, run)
        if my_turn(game, side) and not first:
            first.append(run.model)
        staller(game, side, run)

    model, game, clock, agents, spec = setup({"A": patient, "B": b_play})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))
    seen = notices_of(game)
    real = game.rpc

    def watch(op, args, side=None, context="server"):
        if op == "act.end_turn" and args.get("forced") and side == "B" and not ends:
            ends.append(agents.runs["B"].model)
        return real(op, args, side, context=context)

    game.rpc = watch  # type: ignore[method-assign]
    am.run_arena(spec, SIDES, game, agents, out_dir, clock)
    return ends[0] - first[0], spec.turn_cap_s, seen


def test_the_owners_next_phase_is_capped_early_by_the_wait(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    with mock.patch.object(un.Unanswered, "charge", lambda self, side, cap: 0.0):
        uncharged, _cap, _seen = _prompt_then_think(base)
    charged_dir = tmp_path / "charged"
    charged_dir.mkdir()
    took, cap, seen = _prompt_then_think(charged_dir)
    assert took < uncharged
    rows = [e for e in arena_events(charged_dir) if e["ev"] == "arena_turn_charged"]
    assert rows[0]["side"] == "B" and rows[0]["charged_s"] == cap / 2  # waited 46 s of a 60 s cap: half a cap, no more
    assert rows[0]["debt_left_s"] > 0 and rows[1]["debt_left_s"] == 0  # the rest came off its next phase
    assert rows[0]["character"] != rows[1]["character"]  # once a phase, not every read of it
    assert ("B", f"this turn has {cap / 2:.0f} s of model time: {cap / 2:.0f} s went to unanswered prompts") in seen
    assert all(s == "B" for s, _ in seen)
    g = grade(charged_dir)["reactions_unanswered"]
    assert g["B"]["declined"] == 1 and g["B"]["waited_s"] == 46.0 and g["B"]["charged_s"] == 46.0
    assert g["A"]["declined"] == 0


def test_a_phase_the_game_runs_keeps_the_debt_for_the_next(tmp_path):
    """Wyll's next phase is the game's own (no cap to take it from): the charge falls on Halsin's, B's next."""
    lost: set[str] = set()
    ticks = [0]

    def b_play(game, side, run):
        if not (char := my_turn(game, side)):
            return
        if model.match().acting.name in lost:
            ticks[0] += 1
            if ticks[0] >= 3:  # the runner has read the phase; the game's AI ends it, no call through the runner
                lost.clear()
                game.model.reply_side("server", "act.end_turn", {"char": char}, side)
            return
        if model.reacted:
            staller(game, side, run)  # a phase that lasts: the runner's read of it charges it
        else:
            patient(game, side, run)

    model, game, clock, agents, spec = setup({"A": patient, "B": b_play})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))

    def goad_once() -> None:
        if model.reacted and not ticks[0]:
            lost.add("Wyll")

    clock.tick_hooks.insert(0, goad_once)
    notices_of(game)
    goaded(game, lost)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    evs = arena_events(tmp_path)
    assert [e["character"] for e in evs if e["ev"] == "arena_control_lost"] == ["Wyll"]
    assert next(e["character"] for e in evs if e["ev"] == "arena_turn_charged") == "Halsin"


def test_the_budget_is_a_sixth_of_the_wall_cap(tmp_path):
    """At 4.5 min the budget is 45 s: the first prompt left open uses it up."""
    model, game, clock, agents, spec = setup({"A": patient, "B": patient}, {**ARENA, "wall_cap_min": 4.5})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))
    notices_of(game)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    rows = [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_budget_spent"]
    assert [(e["side"], e["budget_s"]) for e in rows] == [("B", 45.0)]
    assert grade(tmp_path, {**ARENA, "wall_cap_min": 4.5})["reactions_unanswered"]["B"]["budget_spent"] is True
