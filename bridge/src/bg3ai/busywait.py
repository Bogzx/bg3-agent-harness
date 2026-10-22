"""Waiting out bg3le's busy UI (Client/UI.lua UI.busy) instead of failing the call at once.

A client UI read that waited out bg3le's frame lock (a load, a rest, a screen being built: dialogue -> trade)
makes the mod refuse every UI read for UI.BUSY_COOLDOWN_MS (200 ms), and an op that met it answers
"bg3le: UI busy ... retry in a moment" (Shared/RPC.lua drops a job it started). live5 (20261022-034005): trade()
0.2 s after the dialogue opened the trade screen, set_reactions and ui.hotbar_slot 2.5 s after a load got that answer
and failed. The op is asked again after the cooldown, within a bounded budget; the time spent is returned so the tool
result can say so.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

BUSY_MARK = "bg3le: UI busy"
BUDGET_S = 5.0  # a load's frame-lock timeouts come in bursts of a few cooldowns; past this the busy answer stands
STEP_S = 0.25  # one cooldown (0.2 s) and a frame

# The client ops asked again: reads, and ops that move the game toward a stated target (ui.reactions sets modes,
# ui.trade sets Barter mode only when it isn't and moves items in its job, which RPC.lua drops on a busy answer).
# Not the others: a busy answer can come after the op's own press ran (the press was the slow call), and asking again
# would press twice (ui.dialogue_choose would pick an answer of the next line; tools/dev/check_all.py busy_retry
# keeps presses out for the same reason). Their callers see the busy answer.
RETRY_OPS = frozenset({"ui.state", "ui.selected", "ui.trade", "ui.reactions", "ui.hotbar_slot", "ui.cast_state"})


class Clock(Protocol):
    def __call__(self) -> float: ...


class Sleep(Protocol):
    def __call__(self, seconds: float, /) -> None: ...


def is_busy(message: str) -> bool:
    return BUSY_MARK in message


def call[T](
    fn: Callable[[], T],
    clock: Clock,
    sleep: Sleep,
    error: type[Exception],
    budget_s: float = BUDGET_S,
    step_s: float = STEP_S,
) -> tuple[T, float]:
    """fn() and the seconds spent waiting for a busy UI before it answered. An `error` whose message is the busy
    answer is retried every step_s until budget_s has passed; then it is raised with the wait appended. Any other
    error is raised at once."""
    t0 = asked = clock()  # the wait is the time before the last ask, not the ask itself (a slow answer isn't one)
    while True:
        try:
            return fn(), asked - t0
        except error as e:
            if not is_busy(str(e)):
                raise
            waited = clock() - t0
            if waited + step_s > budget_s:
                raise error(f"{e} (still busy after waiting {waited:.1f} s)") from e
        sleep(step_s)
        asked = clock()


def note(waited_s: float) -> str:
    """The line a tool result gets when its calls waited for a busy UI ('' when they didn't)."""
    if waited_s <= 0:
        return ""
    return f"(waited {waited_s:.1f} s for the game's UI: busy after a load or a screen change)"
