"""Hover-checked mouse click on a named UI button, for buttons whose command does nothing when we run it.

live2 §1 (2026-10-21): the pickpocket window's Steal button "pressed" through its Command started no steal (14 tries,
no event); every steal that happened came from a hand click on Steal. Same idea as boxclick.py: move the cursor, ask
the mod whether it is over the button (a probe op reading the button's IsMouseOver), click only when it is. A miss
never clicks, so a wrong spot can't walk the character or click the world.

    from bg3ai.hoverclick import hover_click
    res = hover_click(lambda: game.rpc("ui.steal_probe", {}, context="client"), inp, "steal", STEAL_SEED)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

Probe = Callable[[], Any]  # -> {"open": bool, "over": bool, "enabled": bool, ...}

STEP = 0.012  # grid step around the seed (window fractions; ~23 px at 1920 wide)
RINGS = 4  # rings around the seed: 9x9 points at most
BUDGET_S = 6.0
PROBE_GAP_S = 0.1  # between probes, as boxclick (a probe every 30 ms crashed the game on a closing box)
_hits: dict[str, tuple[float, float]] = {}  # button key -> the last point over it (this process)


def candidates(
    seed: tuple[float, float], key: str = "", step: float = STEP, rings: int = RINGS
) -> list[tuple[float, float]]:
    """The last hit for `key` first, then the seed, then rings of points around it, nearest first."""
    pts = [
        (round(seed[0] + i * step, 4), round(seed[1] + j * step, 4))
        for i in range(-rings, rings + 1)
        for j in range(-rings, rings + 1)
    ]
    pts.sort(key=lambda p: (abs(p[0] - seed[0]) + abs(p[1] - seed[1]), p))
    hit = _hits.get(key)
    return ([hit] if hit else []) + [p for p in pts if p != hit and 0 < p[0] < 1 and 0 < p[1] < 1]


def hover_click(
    probe: Probe,
    inp: Any,
    key: str,
    seed: tuple[float, float],
    budget_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """-> {clicked: True, at: (fx, fy), tries} | {gone: True, tries} (the window closed) | {error, tries}.
    The probe says `open` (the page with the button shows), `over` (the cursor is over the button) and `enabled`."""
    first = probe()
    if not isinstance(first, dict) or not first.get("open"):
        return {"gone": True, "tries": 0}
    if first.get("button") is False:
        return {"error": "the window has no such button", "tries": 0}
    if first.get("enabled") is False:
        return {"error": "the button is greyed out", "tries": 0}
    w, h = inp.window_size()
    end = clock() + (BUDGET_S if budget_s is None else budget_s)
    tries = 0
    for fx, fy in candidates(seed, key):
        if clock() >= end:
            break
        tries += 1
        inp.move(int(fx * w), int(fy * h))
        inp.wait_frames(2)
        sleep(PROBE_GAP_S)
        v = probe()
        if not isinstance(v, dict) or not v.get("open"):
            return {"gone": True, "tries": tries}
        if not v.get("over"):
            continue
        inp.click(int(fx * w), int(fy * h))
        _hits[key] = (fx, fy)
        inp.move(w // 2, int(h * 0.2))  # off the button: no hover left on it
        return {"clicked": True, "at": (fx, fy), "tries": tries}
    return {"error": "no cursor position over the button found", "tries": tries}
