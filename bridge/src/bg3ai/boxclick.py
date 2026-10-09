"""Hover-checked mouse click on a Yes/No question box whose button ignores its Command.

Live S013/S014: the rest flow's "[ForceUpdate] Yes/No" box (Dialog_box, ls.DCMessageBox) stayed open after
every command press (long_rest "too many confirmations", confirm "pressed=Yes via=command still_open"),
while the agent's own screenshot + click on Yes worked. Those failures cost ~80 s of >10 s gaps in S013+S014.
The mod now reports ``needs_click`` instead of pressing again, and this module clicks the button the way a
player would: move the cursor, ask the mod whether the cursor is over that button (``ui.confirm_probe``,
Noesis IsMouseOver, the same check leveling.py uses), and click only when it is. A miss never clicks.

    from bg3ai.boxclick import click_box
    res = click_box(lambda op, a: game.rpc(op, a, context="client"), inp, "yes")
"""

from __future__ import annotations

import time
from typing import Any, Callable

Rpc = Callable[[str, dict], Any]

# Where the answers sat in S013/S014 (window fractions of a 1920x1080 window; both agent clicks worked).
SEEDS = {"yes": (0.44, 0.54), "no": (0.56, 0.54)}
_hits: dict[str, tuple[float, float]] = {}  # answer -> last point that was over its button (this process)

GRID_X = [round(0.30 + 0.035 * i, 3) for i in range(12)]   # 0.30 .. 0.685
GRID_Y = [round(0.40 + 0.02 * i, 3) for i in range(15)]    # 0.40 .. 0.68
BUDGET_S = 8.0


def _key(answer: str) -> str:
    a = str(answer or "yes").strip().lower()
    return "no" if a in ("no", "cancel", "back", "close") else ("yes" if a in ("yes", "ok", "confirm", "accept") else a)


def candidates(answer: str) -> list[tuple[float, float]]:
    """Points to try, nearest the last hit (or the seed) first."""
    k = _key(answer)
    seed = _hits.get(k) or SEEDS.get(k) or SEEDS["yes"]
    pts = [seed] + [(x, y) for y in GRID_Y for x in GRID_X]
    out, seen = [], set()
    for p in sorted(pts, key=lambda p: (p != seed, abs(p[0] - seed[0]) + 2 * abs(p[1] - seed[1]))):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _sig(v: dict) -> str:
    return f"{v.get('title')}#{v.get('text')}#{'|'.join(map(str, v.get('buttons') or []))}"


def click_box(rpc: Rpc, inp: Any, answer: str = "yes", budget_s: float = BUDGET_S) -> dict:
    """-> {clicked: label, at: (x, y), closed: bool, next?: view, tries} | {error, tries} | {gone: True}."""
    k = _key(answer)
    first = rpc("ui.confirm_probe", {"answer": answer})
    if not isinstance(first, dict) or not first.get("box"):
        return {"gone": True, "tries": 0}
    if first.get("error"):
        return {"error": first["error"], "tries": 0}
    sig0 = _sig(first)
    w, h = inp.window_size()
    end = time.monotonic() + budget_s
    pts = candidates(answer)
    tries = 0
    row = None
    while pts and time.monotonic() < end:
        fx, fy = pts.pop(0)
        tries += 1
        inp.move(int(fx * w), int(fy * h))
        inp.wait_frames(2)
        v = rpc("ui.confirm_probe", {"answer": answer})
        if not isinstance(v, dict) or not v.get("box"):
            return {"gone": True, "tries": tries}
        if v.get("over_other") and row is None:
            # on the button row, just another button: search along this row first
            row = fy
            pts.sort(key=lambda p: (abs(p[1] - row) > 0.021, abs(p[0] - fx)))
        if not v.get("over"):
            continue
        inp.click(int(fx * w), int(fy * h))
        inp.wait_frames(8)
        res = {"clicked": v.get("label"), "at": (fx, fy), "tries": tries, "closed": False}
        t1 = time.monotonic() + 1.5
        while time.monotonic() < t1:
            after = rpc("ui.confirm_probe", {"answer": answer})
            if not isinstance(after, dict) or not after.get("box"):
                res["closed"] = True
                break
            if _sig(after) != sig0:
                res["next"] = {kk: after.get(kk) for kk in ("title", "text", "buttons") if after.get(kk)}
                break
            time.sleep(0.1)
        if res["closed"] or res.get("next"):
            _hits[k] = (fx, fy)
        inp.move(w // 2, int(h * 0.2))  # off the box (no hover left on its buttons)
        return res
    return {"error": f"no cursor position over the '{answer}' button found", "tries": tries}


def describe(res: dict) -> str:
    """One line for the tool result."""
    if res.get("gone"):
        return "the question box closed"
    if res.get("error"):
        return f"couldn't click the box: {res['error']} ({res.get('tries', 0)} tries)"
    s = f"clicked {res.get('clicked')} on the question box"
    if res.get("closed"):
        s += " (closed)"
    elif res.get("next"):
        n = res["next"]
        s += f"; next question: {n.get('text') or n.get('title')} [{'/'.join(map(str, n.get('buttons') or []))}]"
    else:
        s += " but it is still open"
    return s
