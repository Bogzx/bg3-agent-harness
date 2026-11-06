"""Turn helpers end_turn and its wait use: a downed or passing member's turn, the DISABLED lines.
"""

from __future__ import annotations

import time

from .. import turn
from . import core, queueing


def _downed_text(u: str) -> str:
    return (f"{queueing._label(u)} is downed: nothing to play, its turn passes by itself (the game rolls its death "
            "saving throw). Healing Word or Help (adjacent) from an ally gets it up.")


# S087: a stunned member's open turn the game itself passes; if it is still open this long with nothing else moving,
# the wait ends it as a player would (End Turn for a character that can't act) and says so.
PASS_END_S = 5.0


def _end_passing(cs: dict, since: float, done: set, notes: list | None) -> None:
    """End the open turns of passing (stunned) party members when nothing moved for PASS_END_S (once each)."""
    if time.monotonic() - since < PASS_END_S or turn.acting(cs):
        return
    rows = {o.get("uuid"): o for o in cs.get("order") or [] if isinstance(o, dict)}
    for u in turn.passing(cs):
        if u in done:
            continue
        done.add(u)
        r = core.game.run_job("act.end_turn", {"char": u}, wait_s=combat._end_wait_s(5))
        if notes is not None:
            why = rows.get(u, {}).get("passes") or "can't act"
            notes.append(f"ended {queueing._label(u)}'s turn ({why}: it can't act)" if r.status == "done"
                         else f"{queueing._label(u)}'s turn ({why}) did not end: {r.error or r.status}")


# How often a wait for a party turn (end_turn's wait, wait_for my_turn) reads state.combat (~10-20 ms): the turn's
# start reaches the agent this much late at most (0.3 / 0.5 s until 2026-10-16: ~0.2 s mean per turn, 8-15 a run).
TURN_POLL_S = 0.15


def _disabled_lines(only: str | None = None) -> list[str]:
    """The DISABLED lines (turn.disabled_lines) of a fresh combat read; [] out of combat."""
    cs = core.game.try_rpc("state.combat") or {}
    return turn.disabled_lines(cs, queueing._label, only) if cs.get("active") else []


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    combat, macro_tools,
)
