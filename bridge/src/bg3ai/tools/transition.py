"""The wait after a click on the rest/travel Yes/No box, while the game changes screens (A23), moved out of
world_tools.py unchanged. world_tools._box_act runs it as boxclick's after-click step for TRANSITION_TOOLS."""

from __future__ import annotations

import math
import time
from typing import Any

from ..game import GameError, hold_client, release_client
from . import core

# A23 (S086 05:17:45 hang; impl-setup S17/S21): our click on the rest/travel Yes/No box starts the game's transition
# (End the Day, a camp scene, a region load). S086: after the click, ui.box_open polls and then the follow-up
# ui.long_rest job read the client UI while the game tore the box down: 954 frame-lock timeouts in two client Ticks,
# the story thread stopped answering, FAULT hang. From the click on, no client call at all (game.hold_client) until
# the server says the transition is over: a rest-flow answer (end_day_ok, rest_started, a refusal), a conversation
# starting (the rest check's Dialog flag), the server's game state back to Running after a load, or the leader moved
# (a trip within the level); then TRANSITION_SETTLE_S with the server up. No word in TRANSITION_MAX_S: go on anyway.
TRANSITION_TOOLS = ("long_rest", "short_rest", "fast_travel")
TRANSITION_MAX_S = 20.0
TRANSITION_SETTLE_S = 1.0
TRANSITION_POLL_S = 0.25
_TRANSITION_DONE = ("end_day_ok", "end_day_failed", "rest_started", "rest_start_failed", "rest_cancelled",
                    "gather_failed", "ready_failed", "rest_denied")
_TRANSITION_LAST: dict = {}


def _transition_wait(tool: str) -> dict:
    """Server reads only (sys.info, camp.rest_flow, state.status) until the transition after the box click is over.
    -> {transition: what ended it, transition_s}."""
    from . import inventory, travel  # loaded after this module (tools/__init__.py order)

    t0 = time.monotonic()

    def srv(op: str, a: dict | None = None) -> Any:
        try:
            return core.game.rpc(op, a or {}, context="server", timeout=3.0)
        except GameError:
            return None  # a load resets the server Lua: no answer = still loading

    pos0 = travel._lead_pos(srv("state.status")) if tool == "fast_travel" else None
    left, why = False, "no word from the game"
    while time.monotonic() - t0 < TRANSITION_MAX_S:
        info = srv("sys.info")
        gs = info.get("game_state") if isinstance(info, dict) else None
        if gs not in ("Running", "Paused"):
            left = True
        elif left:
            why = "the game loaded"
            break
        elif tool == "short_rest" and time.monotonic() - t0 >= TRANSITION_SETTLE_S:
            why = "settled"
            break
        elif tool == "long_rest":
            flow = srv("camp.rest_flow", {"since_ms": int((time.monotonic() - t0) * 1000) + 300})
            steps = [str(x) for x in (flow.get("steps") or [])] if isinstance(flow, dict) else []
            done = [x for x in steps if inventory._rest_step(x) in _TRANSITION_DONE]
            can = flow.get("can_rest") if isinstance(flow, dict) else None
            if done:
                why = f"the game answered ({inventory._rest_step(done[-1])})"
                break
            if any("Dialog" in (r.get("flags") or []) for r in (can or {}).get("blocked") or [] if isinstance(r, dict)):
                why = "a conversation started"
                break
        elif tool == "fast_travel" and pos0:
            p = travel._lead_pos(srv("state.status"))
            if p and math.dist(p[:3], pos0[:3]) > 3.0:
                why = "the party moved"
                break
        time.sleep(TRANSITION_POLL_S)
    if why not in ("settled", "no word from the game"):
        time.sleep(TRANSITION_SETTLE_S)
    out = {"transition": why, "transition_s": round(time.monotonic() - t0, 1)}
    _TRANSITION_LAST.clear()
    _TRANSITION_LAST.update(out, tool=tool)
    return out


def _transition_after(tool: str):
    """boxclick's after-click step for a transition box: hold every client call, wait on the server, release."""
    def after(res: dict) -> dict:
        hold_client(TRANSITION_MAX_S + TRANSITION_SETTLE_S + 5.0, f"{tool}: the game is changing screens after the Yes")
        try:
            res.update(_transition_wait(tool))
        finally:
            release_client()
        res["closed"] = True
        return res
    return after
