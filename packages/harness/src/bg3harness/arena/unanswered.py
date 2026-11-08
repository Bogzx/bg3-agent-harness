"""What a side's unanswered reaction prompts cost it (owner D346, orchestrator D350; ops/logs/wall-cap-design.md).

A prompt left open holds the game for both sides, and nothing else charges the side that left it. REACTION_ANSWER_S
from its first read on screen it is declined, as a player who lets it pass. The time waited is that side's debt, taken
off its next phases' model-time caps, at most CHARGE_SHARE of a cap each, so unanswered prompts alone never end a
turn, and a phase capped only because of the charge counts toward no forfeit (match.py Turn.charged_cap). The waits of
one side add up to at most its budget (BUDGET_SHARE of the wall cap); past it, that side's prompts are declined at
their first read, which is what keeps unanswered prompts from using up a match.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Two model steps at the slowest model's p99 (deepseek-flash, 25.5 s a step, wall-cap-design.md): the one in progress
# when the prompt opens and the answer. The budget is the guarantee, so a slow model shouldn't lose a prompt to this.
REACTION_ANSWER_S = 45.0
CHARGE_SHARE = 0.5
BUDGET_SHARE = 1 / 6


@dataclass
class Unanswered:
    """Per side: seconds waited on its unanswered prompts, the part not yet charged, and whether its budget is gone."""

    budget_s: float = math.inf
    waited: dict[str, float] = field(default_factory=dict[str, float])
    debt: dict[str, float] = field(default_factory=dict[str, float])
    spent: set[str] = field(default_factory=set[str])

    def bound(self, side: str | None) -> float:
        """How long `side`'s prompt stays open from its first read."""
        return 0.0 if side in self.spent else REACTION_ANSWER_S

    def declined(self, side: str, waited_s: float) -> bool:
        """A prompt of `side`'s declined after waited_s. True when this one used up the side's budget."""
        self.waited[side] = self.waited.get(side, 0.0) + waited_s
        self.debt[side] = self.debt.get(side, 0.0) + waited_s
        if side not in self.spent and self.waited[side] >= self.budget_s:
            self.spent.add(side)
            return True
        return False

    def charge(self, side: str, cap: float) -> float:
        """The seconds of a phase's cap `side` starts without, taken off its debt."""
        charged = min(self.debt.get(side, 0.0), cap * CHARGE_SHARE)
        if charged > 0:
            self.debt[side] -= charged
        return charged


def cap_words(cap: float, charged: float) -> str:
    """The per-turn cap as its side is told when it ends the turn: on a charged phase, the time the side had."""
    if charged <= 0:
        return f"the per-turn time cap ({cap:g} s of model time)"
    return f"the per-turn time cap ({cap - charged:.0f} s of model time: {charged:.0f} s went to unanswered prompts)"


def notice(game: Any, log: Callable[[str], None], side: str, text: str) -> None:
    """The fact for one side's next results (Arena.lua arena.notice); failing never ends the match."""
    try:
        game.rpc("arena.notice", {"side": side, "text": text})
    except Exception as e:
        log(f"arena: telling side {side} failed: {e}")


def note_declined(
    game: Any, ref: Any, log: Callable[[str], None], ledger: Unanswered, owner: str, rx: dict[str, Any], waited: float
) -> None:
    """The ledger, facts and the owner's notice for a prompt of `owner`'s the runner just declined."""
    over = owner in ledger.spent
    used_up = ledger.declined(owner, waited)
    ref.event(
        "arena_reaction_declined",
        round=ref.round,
        side=owner,
        detail=str(rx.get("prompt") or ""),
        waited_s=round(waited, 1),
        over_budget=over,
    )
    if used_up:
        ref.event(
            "arena_reaction_budget_spent",
            side=owner,
            round=ref.round,
            waited_s=round(ledger.waited[owner], 1),
            budget_s=round(ledger.budget_s, 1),
        )
    options: list[dict[str, Any]] = rx.get("options") or []
    shown = [f"{o.get('name')} ({o.get('who')})" for o in options if o.get("name")]
    names = ", ".join(shown) or "a prompt"
    if over:
        text = f"{names} declined at once: your side's {ledger.budget_s / 60:g} min of unanswered prompts is used up"
    else:
        text = f"{names} unanswered for {waited:.0f} s: declined; {waited:.0f} s comes off your next turns"
    notice(game, log, owner, "reaction: " + text)


def charge_phase(game: Any, ref: Any, log: Callable[[str], None], ledger: Unanswered, turn: Any, cap: float) -> float:
    """At a phase's first read: what its side's unanswered prompts take off its cap; the fact and the side's notice."""
    charged = ledger.charge(turn.side, cap)
    if charged > 0:
        ref.event(
            "arena_turn_charged",
            side=turn.side,
            character=turn.name,
            round=turn.round,
            charged_s=round(charged, 1),
            debt_left_s=round(ledger.debt[turn.side], 1),
        )
        left, took = f"{cap - charged:.0f}", f"{charged:.0f}"
        notice(game, log, turn.side, f"this turn has {left} s of model time: {took} s went to unanswered prompts")
    return charged
