"""Hover-checked mouse click on one row of a reaction prompt with several rows (one per spell level).

Live S091/S092: Gale's Shield prompt (one row per slot level, I-VI) never answered to the mod's commands (react
TIMEOUT 4 of 5, by name and by number: the row's press + InterruptAcceptPlayerCommand), while every hand click on a
Shield row cast it at once (S092 08:45:05, 08:47:50, 08:51:28, 08:53:30, 08:55:36). The mod now answers
``{needs_click: <row>, rows}`` for such a prompt and this module clicks the row the way the agent did: move the
cursor, ask the mod whether it is over that row (``ui.react_probe``, Noesis IsMouseOver), click only when it is.

    from bg3ai.reactclick import click_row
    res = click_row(lambda op, a: game.rpc(op, a, context="client"), inp, 2)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

Rpc = Callable[[str, dict], Any]

# S092 (1920x1080): row 1 of 6 / 5 / 4 rows at y 0.457 / 0.507 / 0.557 (the panel grows upward from a fixed
# bottom), one row every 0.05; the rows span x 0.43-0.61.
ROW_DY = 0.05
TOP_OF_6 = 0.457
SEED_X = 0.5
GRID_X = [0.5, 0.46, 0.55]
GRID_Y = [round(0.30 + 0.025 * i, 3) for i in range(19)]   # 0.30 .. 0.75
BUDGET_S = 6.0
PROBE_GAP_S = 0.1     # as boxclick: no tight IsMouseOver polling on a popup the game may be tearing down
CLOSE_POLL_S = 0.1
CLOSE_WATCH_S = 1.5


def seed(which: int, rows: int) -> tuple[float, float]:
    """Where row `which` of `rows` sat in S092."""
    return SEED_X, round(TOP_OF_6 + ROW_DY * (6 - rows + which - 1), 3)


def candidates(which: int, rows: int) -> list[tuple[float, float]]:
    s = seed(which, rows)
    pts = {s} | {(x, y) for x in GRID_X for y in GRID_Y}
    return sorted(pts, key=lambda p: (p != s, 2 * abs(p[1] - s[1]) + abs(p[0] - s[0])))


def _watch(rpc: Rpc, res: dict, before: int) -> dict:
    """After the click: the prompt gone, or one event fewer, = answered. Reads only whether the prompt shows and
    its event count (watch=true: no row walk, no IsMouseOver while the game takes the popup down). Still showing
    after CLOSE_WATCH_S: `shown` (the click did not answer it, or the next prompt came)."""
    t1 = time.monotonic() + CLOSE_WATCH_S
    while True:
        v = rpc("ui.react_probe", {"watch": True})
        n = int(v.get("events") or 0) if isinstance(v, dict) else 0
        if not isinstance(v, dict) or not v.get("open") or n < before:
            res["closed"] = True
            if isinstance(v, dict) and v.get("open"):
                res["pending"] = n
            return res
        if time.monotonic() >= t1:
            res["shown"] = True
            return res
        time.sleep(CLOSE_POLL_S)


def click_row(rpc: Rpc, inp: Any, which: int, budget_s: float = BUDGET_S) -> dict:
    """-> {clicked: which, at: (x, y), tries, closed: bool, shown?, pending?} | {error, tries} | {gone: True}."""
    first = rpc("ui.react_probe", {"which": which})
    if not isinstance(first, dict) or not first.get("open"):
        return {"gone": True, "tries": 0}
    rows = int(first.get("rows") or which)
    if which < 1 or which > rows:
        return {"error": f"no reaction row {which} ({rows} shown)", "tries": 0}
    before = int(first.get("events") or 1)
    w, h = inp.window_size()
    end = time.monotonic() + budget_s
    pts = candidates(which, rows)
    tried: set[tuple[float, float]] = set()
    tries, last = 0, time.monotonic()
    while pts and time.monotonic() < end:
        fx, fy = pts.pop(0)
        if (fx, fy) in tried:
            continue
        tried.add((fx, fy))
        tries += 1
        inp.move(int(fx * w), int(fy * h))
        inp.wait_frames(2)
        time.sleep(max(0.0, last + PROBE_GAP_S - time.monotonic()))
        v = rpc("ui.react_probe", {"which": which})
        last = time.monotonic()
        if not isinstance(v, dict) or not v.get("open"):
            return {"gone": True, "tries": tries}
        if v.get("over"):
            inp.click(int(fx * w), int(fy * h))
            inp.wait_frames(4)
            inp.move(w // 2, int(h * 0.2))   # off the popup (no hover left on its rows)
            return _watch(rpc, {"clicked": which, "at": (fx, fy), "tries": tries, "closed": False}, before)
        k = v.get("over_row")
        if isinstance(k, int) and k != which:
            # on another row: the wanted one is (which - k) rows below it
            pts.insert(0, (fx, round(fy + ROW_DY * (which - k), 3)))
    return {"error": f"no cursor position over reaction row {which} found", "tries": tries}


def describe(res: dict) -> str:
    if res.get("gone"):
        return "the prompt was gone before the click"
    if res.get("error"):
        return f"{res['error']} ({res.get('tries', 0)} cursor tries)"
    at = res.get("at") or (0, 0)
    head = f"clicked reaction row {res.get('clicked')} at ({at[0]:.3f},{at[1]:.3f})"
    if res.get("closed"):
        return head + (f"; {res['pending']} more prompt(s) waiting" if res.get("pending") else "")
    return head + "; the prompt still shows (the same or the next one)"
