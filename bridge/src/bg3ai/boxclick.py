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

import json
import time
from pathlib import Path
from typing import Any, Callable

Rpc = Callable[[str, dict], Any]

# Where the answers sat in S013/S014 (window fractions of a 1920x1080 window; both agent clicks worked).
SEEDS = {"yes": (0.44, 0.54), "no": (0.56, 0.54)}
_hits: dict[str, tuple[float, float]] = {}  # answer (and answer|title) -> last point over its button (this process)
# Hits that worked, kept across processes ("answer|title" -> window fractions). Each play session has a new MCP
# server, so a box seen once per run (character creation's "Reset Tutorials") was searched for from the seed every
# run (naut2: the popup step took ~1.2 s of naming). The server sets it (runtime/boxclick_hits.json); None = off.
HITS_PATH: Path | None = None

GRID_X = [round(0.30 + 0.035 * i, 3) for i in range(12)]   # 0.30 .. 0.685
GRID_Y = [round(0.40 + 0.02 * i, 3) for i in range(15)]    # 0.40 .. 0.68
BUDGET_S = 8.0
AFTER_CLICK_FRAMES = 4   # was 8: the close is polled anyway, every CLOSE_POLL_S
# Back to 0.1 s (0.03 from 2026-10-17 14:53): all three game crashes (S072/S077/S078, impl-setup S17) came while
# confirm_probe was polled every 30 ms on a box the rest / travel transition was tearing down. PROBE_GAP_S spaces
# the probes before the click the same way; after the click only ui.box_open is read (_watch).
CLOSE_POLL_S = 0.1
PROBE_GAP_S = 0.1
CLOSE_WATCH_S = 1.5


def _key(answer: str) -> str:
    a = str(answer or "yes").strip().lower()
    return "no" if a in ("no", "cancel", "back", "close") else ("yes" if a in ("yes", "ok", "confirm", "accept") else a)


def _stored() -> dict:
    if HITS_PATH is None:
        return {}
    try:
        d = json.loads(HITS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _remember(k: str, title: str | None, p: tuple[float, float]) -> None:
    _hits[k] = p
    if not title:
        return
    _hits[f"{k}|{title}"] = p
    if HITS_PATH is None:
        return
    d = _stored()
    if d.get(f"{k}|{title}") == list(p):
        return
    d[f"{k}|{title}"] = list(p)
    try:
        HITS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HITS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(HITS_PATH)
    except OSError:
        pass


def _remembered(k: str, title: str | None) -> tuple[float, float] | None:
    """This box's own last hit (this process, then the stored one), else this answer's last hit."""
    if title:
        if f"{k}|{title}" in _hits:
            return _hits[f"{k}|{title}"]
        p = _stored().get(f"{k}|{title}")
        if isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p):
            return float(p[0]), float(p[1])
    return _hits.get(k)


def candidates(answer: str, title: str | None = None) -> list[tuple[float, float]]:
    """Points to try, nearest the last hit (or the seed) first."""
    k = _key(answer)
    seed = _remembered(k, title) or SEEDS.get(k) or SEEDS["yes"]
    pts = [seed] + [(x, y) for y in GRID_Y for x in GRID_X]
    out, seen = [], set()
    for p in sorted(pts, key=lambda p: (p != seed, abs(p[0] - seed[0]) + 2 * abs(p[1] - seed[1]))):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _watch(rpc: Rpc, res: dict) -> dict:
    """After the click or key: did the box close? Reads only ui.box_open (game state + whether a box widget is
    visible), never confirm_probe: no walk of the box's buttons and no IsMouseOver on a box the game may be
    tearing down. A transition (game state not Running/Paused) counts as closed and ends the watch. A box still
    showing after CLOSE_WATCH_S is the same question or the next one (the rest flow asks twice): `shown`."""
    t1 = time.monotonic() + CLOSE_WATCH_S
    while True:
        v = rpc("ui.box_open", {})
        if not isinstance(v, dict) or not v.get("box"):
            res["closed"] = True
            return res
        if time.monotonic() >= t1:
            res["shown"] = True
            return res
        time.sleep(CLOSE_POLL_S)


def click_box(rpc: Rpc, inp: Any, answer: str = "yes", budget_s: float = BUDGET_S,
              after: Callable[[dict], dict] | None = None) -> dict:
    """-> {clicked: label, at: (x, y), closed: bool, shown?: True, tries} | {error, tries} | {gone: True}.
    after: what follows the click instead of _watch (ui.box_open polls): the rest/travel boxes start a transition,
    and A23 (S086 hang) reads nothing of the client UI from the click until the server says it is over."""
    watch = after or (lambda res: _watch(rpc, res))
    k = _key(answer)
    first = rpc("ui.confirm_probe", {"answer": answer})
    last = time.monotonic()
    if not isinstance(first, dict) or not first.get("box"):
        return {"gone": True, "tries": 0}
    if first.get("error"):
        return {"error": first["error"], "tries": 0}
    w, h = inp.window_size()
    end = time.monotonic() + budget_s
    title = first.get("title") if isinstance(first.get("title"), str) else None
    pts = candidates(answer, title)
    tries = 0
    row = None
    while pts and time.monotonic() < end:
        fx, fy = pts.pop(0)
        tries += 1
        inp.move(int(fx * w), int(fy * h))
        inp.wait_frames(2)
        time.sleep(max(0.0, last + PROBE_GAP_S - time.monotonic()))
        v = rpc("ui.confirm_probe", {"answer": answer})
        last = time.monotonic()
        if not isinstance(v, dict) or not v.get("box"):
            return {"gone": True, "tries": tries}
        if v.get("over_other") and row is None:
            # on the button row, just another button: search along this row first
            row = fy
            pts.sort(key=lambda p: (abs(p[1] - row) > 0.021, abs(p[0] - fx)))
        if not v.get("over"):
            continue
        inp.click(int(fx * w), int(fy * h))
        if after is not None:
            inp.move(w // 2, int(h * 0.2))  # off the box at once (no hover left on its buttons)
        inp.wait_frames(AFTER_CLICK_FRAMES)
        res = watch({"clicked": v.get("label"), "at": (fx, fy), "tries": tries, "closed": False})
        if res["closed"] or res.get("shown"):
            _remember(k, title, (fx, fy))
        inp.move(w // 2, int(h * 0.2))  # off the box (no hover left on its buttons)
        return res
    if k == "yes" and (first.get("buttons") or [None])[0] == first.get("label"):
        return _press_default(rpc, inp, answer, str(first.get("label")), tries, (w, h), watch)
    return {"error": f"no cursor position over the '{answer}' button found", "tries": tries}


def _press_default(rpc: Rpc, inp: Any, answer: str, label: str, tries: int, wh: tuple[int, int],
                   watch: Callable[[dict], dict] | None = None) -> dict:
    """RETURN answers a box's default button, the affirmative one (Yes/Continue, drawn highlighted; live
    2026-10-10 on the rest flow's "Camp without supplies?" box). The fallback when no cursor position
    reads as over the button (S026 09:03: 96 tries, no hover). Same key a player would press."""
    inp.move(wh[0] // 2, int(wh[1] * 0.2))  # nothing hovered while the key goes in
    inp.wait_frames(2)
    inp.key("RETURN")
    inp.wait_frames(8)
    res = (watch or (lambda r: _watch(rpc, r)))({"clicked": f"{label} (RETURN key)", "tries": tries, "closed": False,
                                                "via": "key"})
    if res["closed"]:
        return res
    return {"error": f"no cursor position over the '{answer}' button found, and RETURN didn't answer it either",
            "tries": tries}


def describe(res: dict) -> str:
    """One line for the tool result."""
    if res.get("gone"):
        return "the question box closed"
    if res.get("error"):
        return f"couldn't click the box: {res['error']} ({res.get('tries', 0)} tries)"
    s = f"clicked {res.get('clicked')} on the question box"
    if res.get("closed"):
        s += f" (closed, {res.get('tries', 0)} tr{'y' if res.get('tries') == 1 else 'ies'})"
    else:
        s += "; a question box still shows (the same one, or the next question)"
    return s
