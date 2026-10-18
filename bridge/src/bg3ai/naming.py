"""Character creation's name: make sure the character is called what the plan says before "Venture forth".

Saves are matched to a playthrough by the character's name (runner --save-owner, BG3AI_SAVE_PREFIX), so a
character finished as the default "Tav" breaks save ownership. What bg3le can and cannot see on the naming
page (verified live, Patch 8; details in mod/.../Client/Progression.lua "Character name"):
CharacterName reads as a localization handle ("ResStr_...") whatever the box shows, so the character's name
is unreadable; the box's text is readable (through its selection); focus and hover are readable; the box is
only created once the naming page is first shown; a click focuses it and selects its text; LCTRL+A does
nothing. Writing the box's Text from Lua could not be verified to reach the character, so it is not used.

    1. ui.cc_name {name}                  the box already shows the name               -> done
    2. ui.cc_name {page}                  go to the naming page (flowPage item 3, what Proceed does), then
                                          poll until the box exists
    3. hover-probe the box                close a message box over the page first (Reset Tutorials, see
                                          dismiss_popup); move the shim cursor down the screen's centre line until the
                                          box reports IsMouseOver, then centre on it
    4. click, check `focused`, END + BACKSPACE x (MAX_LEN + 2), text, RETURN
    5. ui.cc_name {name}                  the box shows the name (and the character's name is it, or is
                                          unreadable) -> done; anything else raises NamingError
    6. ui.cc_finish {name}                re-checks the box, then FinishCreating; the caller confirms the
                                          name in game afterwards (the only real read-back)

`rpc(op, args)` is any callable that runs a client-context op and returns its result (raising on error):
Game.rpc with context="client", or tools/dev/test_new_game.py's Driver.rpc.

Pacing (naut series, 2026-10-13: naming took 3.6-5.2 s, mostly hover probes and fixed sleeps). By default each
step waits for the state it needs, the old fixed delay kept as its cap: the box is tried first where it was last
found (this process) or where the layout puts it (live: exactly there, 960,891 at 1920x1080), two hovers in a row
there within the old 0.8 s page-transition sleep, no centring walk; the scan + centring is the fallback, and a
message box is re-checked when a probe or a click comes up empty (it may open after the page). The click waits for
`focused`, the typing for the box to show the name, the popup click for the box to close. BG3AI_CC_PACED=1 brings
back the fixed sleeps and the full scan + centring (`paced()`).

Typing (naut2 series, 2026-10-17: naming 3.2-3.3 s against the 2.5 s target, `type` ~1.55 s of it): the shim spends
~3 frames per key, so END + BACKSPACE x 22 + RETURN were ~72 frames. The click focuses the box and selects its text,
so the name is typed over the selection and only RETURN follows (`type_over_selection`). When the box then doesn't
show the name (no selection: the text was appended), only what the box holds is cleared (its length + 2
backspaces) and the name typed again. Paced mode keeps the full clear.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

Rpc = Callable[[str, dict], Any]

MAX_LEN = 20            # LSTextBox MaxLength on the naming page
DESIGN_H = 2160         # the CC pages are laid out for 3840x2160 and scaled to the window
DESIGN_W = 3840
# NamingContent: VerticalAlignment=Bottom, Margin bottom 280, the box frame is 196 high: its centre sits
# about 378 design px above the bottom edge.
BOX_FROM_BOTTOM = 378


PAGE_SETTLE_S = 0.8     # the old fixed page-transition sleep: now the cap of the quick hover
POPUP_SETTLE_S = 0.3    # the old sleep after closing a message box: now the cap of the wait for it to close
FOCUS_SETTLE_S = 0.2    # the old sleep after the click: now the cap of the wait for `focused`
POLL_S = 0.03

_last_at: dict[tuple[int, int], tuple[int, int]] = {}   # window size -> where the box was found (this process)


def paced() -> bool:
    """BG3AI_CC_PACED=1: the fixed sleeps and the full hover scan of before (read per call)."""
    return os.environ.get("BG3AI_CC_PACED", "").strip() not in ("", "0")


class NamingError(RuntimeError):
    """The name could not be set or verified. Never finish character creation after this."""


def check_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise NamingError("the character needs a non-empty name")
    if name != name.strip() or "\n" in name or "\r" in name:
        raise NamingError(f"bad character name {name!r}: one line, no leading/trailing spaces")
    if len(name) > MAX_LEN:
        raise NamingError(f"character name {name!r} is longer than the page allows ({MAX_LEN})")
    return name


def probe_points(w: int, h: int) -> list[tuple[int, int]]:
    """Cursor positions to try, nearest the expected box centre first (x = centre of the screen)."""
    s = min(w / DESIGN_W, h / DESIGN_H)
    x = w // 2
    y0 = h - BOX_FROM_BOTTOM * s
    step = max(4.0, 40 * s)
    out: list[tuple[int, int]] = []
    for k in range(0, 13):
        for y in ((y0,) if k == 0 else (y0 - k * step, y0 + k * step)):
            if 0 <= y < h:
                out.append((x, int(round(y))))
    return out


def _view(rpc: Rpc, name: str | None = None, **flags) -> dict:
    args = dict(flags)
    if name is not None:
        args["name"] = name
    v = rpc("ui.cc_name", args)
    if not isinstance(v, dict):
        raise NamingError(f"ui.cc_name returned {v!r}")
    return v


def _hover(inp, rpc: Rpc, x: int, y: int) -> bool:
    inp.move(x, y)
    inp.wait_frames(2)
    return bool(_view(rpc).get("hover_box"))


def quick_box(rpc: Rpc, inp, cap: float) -> tuple[tuple[int, int], str] | None:
    """The box where it was last found or where the layout puts it, if the cursor there reports it twice in a row
    (a page still sliding in can't), retried until `cap` s. -> ((x, y), "cached"|"predicted") or None."""
    w, h = inp.window_size()
    pts: list[tuple[tuple[int, int], str]] = []
    if (w, h) in _last_at:
        pts.append((_last_at[(w, h)], "cached"))
    p = probe_points(w, h)[0]
    if all(q != p for q, _ in pts):
        pts.append((p, "predicted"))
    end = time.monotonic() + cap
    while True:
        for (x, y), how in pts:
            if _hover(inp, rpc, x, y) and _hover(inp, rpc, x, y):
                return (x, y), how
        if time.monotonic() >= end:
            return None
        time.sleep(POLL_S)


def _wait_view(rpc: Rpc, name: str | None, ok: Callable[[dict], bool], cap: float) -> dict:
    """ui.cc_name until ok(view) or `cap` s (paced: sleep the cap, then one read)."""
    if paced():
        time.sleep(cap)
        return _view(rpc, name)
    end = time.monotonic() + cap
    while True:
        v = _view(rpc, name)
        if ok(v) or time.monotonic() >= end:
            return v
        time.sleep(POLL_S)


def locate_box(rpc: Rpc, inp, log: Callable[[str], None] = lambda m: None) -> tuple[int, int]:
    """Window coordinates of the name box's centre, found by hover probing. Raises NamingError."""
    w, h = inp.window_size()
    hit = None
    for x, y in probe_points(w, h):
        if _hover(inp, rpc, x, y):
            hit = (x, y)
            break
    if hit is None:
        raise NamingError(f"the name box never reported the cursor over it (window {w}x{h}, "
                          f"{len(probe_points(w, h))} probes down the centre line)")
    # Centre vertically on the box: walk out from the hit in small steps until the cursor leaves it.
    x, y = hit
    fine = max(2, round(8 * min(w / DESIGN_W, h / DESIGN_H)))
    top = bottom = y
    for _ in range(20):
        if top - fine < 0 or not _hover(inp, rpc, x, top - fine):
            break
        top -= fine
    for _ in range(20):
        if bottom + fine >= h or not _hover(inp, rpc, x, bottom + fine):
            break
        bottom += fine
    at = (x, (top + bottom) // 2)
    log(f"name box at {at} (hover {top}..{bottom}, window {w}x{h})")
    return at


def dismiss_popup(rpc: Rpc, inp, log: Callable[[str], None] = lambda m: None) -> dict | None:
    """A message box over the naming page blocks every hover probe: on a profile that has played before, a new
    game shows "Reset Tutorials" (buttons Reset Tutorials / Don't Reset) right there (naut S001 and naut-r2 S001,
    2026-10-13: create_character FAILED twice each, then the agent clicked "Don't Reset" by hand). Its button
    ignores its command, so it gets the hover-checked shim click; the negative answer (last button) changes
    nothing. -> the click result, or None when no box is open."""
    from bg3ai.boxclick import click_box, describe
    try:
        probe = rpc("ui.confirm_probe", {"answer": "no"})
    except Exception:
        return None
    if not isinstance(probe, dict) or not probe.get("box"):
        return None
    res = click_box(rpc, inp, "no")
    log(f"naming page: {probe.get('title') or 'a message box'} was open ({describe(res)})")
    if res.get("closed") and not paced():   # click_box already saw it close
        return res
    if paced():
        time.sleep(POPUP_SETTLE_S)
        return res
    end = time.monotonic() + POPUP_SETTLE_S
    while time.monotonic() < end:
        try:
            gone = not (rpc("ui.confirm_probe", {"answer": "no"}) or {}).get("box")
        except Exception:
            gone = True
        if gone:
            break
        time.sleep(POLL_S)
    return res


def _clear_and_type(inp, name: str, n: int = MAX_LEN + 2) -> None:
    inp.key("END")
    for _ in range(min(n, MAX_LEN + 2)):
        inp.key("BACKSPACE", sync=False)
    inp.sync()
    inp.text(name)
    inp.key("RETURN")


def type_over_selection(rpc: Rpc, inp, name: str, settle: float) -> tuple[dict, str]:
    """After the focusing click (it selects the box's text): type the name over it, RETURN once the box shows it.
    Otherwise clear what the box shows now and type again. -> (last view, "over_selection"|"cleared")."""
    inp.text(name)
    v = _wait_view(rpc, name, lambda vv: bool(vv.get("ok")), settle)
    if v.get("ok"):
        inp.key("RETURN")   # no re-read: ui.cc_finish re-checks the box before it finishes
        return v, "over_selection"
    text = v.get("text")
    _clear_and_type(inp, name, len(text) + 2 if isinstance(text, str) else MAX_LEN + 2)
    return _wait_view(rpc, name, lambda vv: bool(vv.get("ok")), settle), "cleared"


def ensure_name(rpc: Rpc, inp, name: str, log: Callable[[str], None] = lambda m: None,
                settle: float = 0.3, box_timeout: float = 5.0) -> dict:
    """Make the naming page's box show `name` by typing it, or raise NamingError.

    Returns {"via": "already"|"typed", "view": <last ui.cc_name view>, "at"?: [x, y]}. `inp` is a
    bg3ai.input InputClient (None: only the "already" route)."""
    check_name(name)
    v = _view(rpc, name)
    if v.get("can_edit") is False:
        raise NamingError("the selected origin's name cannot be changed (pick a custom character)")
    if v.get("ok"):
        return {"via": "already", "view": v}
    if inp is None:
        raise NamingError(f"name '{name}' needs typing and the input shim is not available")

    slow = paced()
    t0 = time.monotonic()
    ms: dict[str, int] = {}

    def lap(k: str) -> None:
        ms[k] = round((time.monotonic() - t0) * 1000) - sum(ms.values())

    v = _view(rpc, page=True)
    moved = bool(v.get("moved"))
    deadline = time.monotonic() + box_timeout
    while not v.get("box"):  # created when the page is first shown
        if time.monotonic() > deadline:
            raise NamingError(f"no name box on the naming page after {box_timeout:.0f} s")
        time.sleep(0.2 if slow else 0.05)
        v = _view(rpc)
        moved = moved or bool(v.get("moved"))
    lap("page")
    popups = 0
    if slow:
        if moved:
            time.sleep(PAGE_SETTLE_S)  # page transition
        popups += dismiss_popup(rpc, inp, log) is not None
        lap("popup")
        x, y = locate_box(rpc, inp, log)
        how = "scan"
    else:
        popups += dismiss_popup(rpc, inp, log) is not None
        lap("popup")
        hit = quick_box(rpc, inp, PAGE_SETTLE_S if moved else 0.1)
        if hit is None and dismiss_popup(rpc, inp, log) is not None:  # opened after the page did
            popups += 1
            hit = quick_box(rpc, inp, 0.1)
        if hit is not None:
            (x, y), how = hit
            log(f"name box at {(x, y)} ({how})")
        else:
            how = "scan"
            try:
                x, y = locate_box(rpc, inp, log)
            except NamingError:
                if dismiss_popup(rpc, inp, log) is None:
                    raise
                popups += 1
                x, y = locate_box(rpc, inp, log)
    lap("locate")

    for attempt in range(2):
        inp.click(x, y)
        v = _wait_view(rpc, name, lambda vv: bool(vv.get("focused")), FOCUS_SETTLE_S)
        if v.get("focused"):
            break
        if not slow and dismiss_popup(rpc, inp, log) is not None:  # a box opened over the page meanwhile
            popups += 1
    else:
        raise NamingError(f"clicking the name box at {x},{y} didn't focus it")
    lap("focus")
    if slow:
        _clear_and_type(inp, name)
        v, typed = _wait_view(rpc, name, lambda vv: bool(vv.get("ok")), settle), "full_clear"
    else:
        v, typed = type_over_selection(rpc, inp, name, settle)
    lap("type")
    if not v.get("ok"):
        raise NamingError(f"typed '{name}', but the box shows {v.get('text')!r} and the character is "
                          f"{v.get('name') or v.get('name_handle')!r}")
    _last_at[inp.window_size()] = (x, y)
    log(f"name '{name}' typed (box {v.get('text')!r}, character {v.get('name') or 'unreadable'})")
    return {"via": "typed", "view": v, "at": [x, y], "how": how, "popups": popups, "ms": ms,
            "pacing": "fixed" if slow else "state", "typed": typed}


def finish(rpc: Rpc, name: str | None) -> dict:
    """FinishCreating ("Venture forth"), refused by the mod unless the name checks out."""
    return rpc("ui.cc_finish", {"name": name} if name is not None else {})


def name_and_finish(rpc: Rpc, inp, name: str | None, log: Callable[[str], None] = lambda m: None,
                    do_finish: bool = True) -> dict:
    """After ui.cc_apply {finish=false} (or one that stopped with needs_name): name, then finish."""
    out: dict = {}
    if name is not None:
        out["name"] = ensure_name(rpc, inp, name, log)
    if do_finish:
        out["finish"] = finish(rpc, name)
    return out
