"""Character creation's name: make sure the character is called what the plan says before "Venture forth".

Saves are matched to a playthrough by the character's name (runner --save-owner, BG3AI_SAVE_PREFIX), so a
character finished as the default "Tav" breaks save ownership. bg3le cannot write CharacterName (an
ls.LocaString), and nothing typed it in the first live run. The flow, with the client ops in
mod/.../Client/Progression.lua ("Character name"):

    1. ui.cc_name {name}                  already called that?             -> done
    2. ui.cc_name {name, set}             write the name box's Text; accepted only if the character's
                                          CharacterName reads back as the name
    3. ui.cc_name {page}                  go to the naming page (flowPage item 3, what Proceed does)
    4. hover-probe the box                move the shim cursor down the screen's centre line until the
                                          box reports IsMouseOver, then centre on it
    5. click, check `focused`, LCTRL+A, BACKSPACE, text, RETURN (the route used live)
    6. ui.cc_name {name}                  the box shows the name and the character's name is it (or is
                                          unreadable) -> done; anything else raises NamingError
    7. ui.cc_finish {name}                re-checks the name, then FinishCreating

`rpc(op, args)` is any callable that runs a client-context op and returns its result (raising on error):
Game.rpc with context="client", or tools/dev/test_new_game.py's Driver.rpc.
"""
from __future__ import annotations

import time
from typing import Any, Callable

Rpc = Callable[[str, dict], Any]

MAX_LEN = 20            # LSTextBox MaxLength on the naming page
DESIGN_H = 2160         # the CC pages are laid out for 3840x2160 and scaled to the window
DESIGN_W = 3840
# NamingContent: VerticalAlignment=Bottom, Margin bottom 280, the box frame is 196 high: its centre sits
# about 378 design px above the bottom edge.
BOX_FROM_BOTTOM = 378


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


def ensure_name(rpc: Rpc, inp, name: str, log: Callable[[str], None] = lambda m: None,
                settle: float = 0.3) -> dict:
    """Make the character creation page's character name `name`, or raise NamingError.

    Returns {"via": "already"|"text"|"typed", "view": <last ui.cc_name view>}. `inp` is a bg3ai.input
    InputClient (None: only the no-typing routes are tried)."""
    check_name(name)
    v = _view(rpc, name)
    if v.get("can_edit") is False:
        raise NamingError("the selected origin's name cannot be changed (pick a custom character)")
    if v.get("confirmed"):
        return {"via": "already", "view": v}

    v = _view(rpc, name, set=True)
    time.sleep(settle)
    v = _view(rpc, name)
    if v.get("confirmed"):
        log(f"name '{name}' set by writing the name box")
        return {"via": "text", "view": v}
    log(f"writing the name box didn't take (box {v.get('text')!r}, character {v.get('name')!r}); typing it")

    if inp is None:
        raise NamingError(f"name '{name}' needs typing and the input shim is not available")
    v = _view(rpc, page=True)
    if not v.get("box"):
        raise NamingError("no name box on the naming page")
    time.sleep(0.8)  # page transition
    x, y = locate_box(rpc, inp, log)

    for attempt in range(2):
        inp.click(x, y)
        time.sleep(0.2)
        v = _view(rpc, name)
        if v.get("focused"):
            break
    else:
        raise NamingError(f"clicking the name box at {x},{y} didn't focus it")
    inp.key("LCTRL+A")
    inp.key("BACKSPACE")
    inp.text(name)
    inp.key("RETURN")
    time.sleep(settle)
    v = _view(rpc, name)
    if not v.get("ok"):
        raise NamingError(f"typed '{name}', but the box shows {v.get('text')!r} and the character is "
                          f"{v.get('name')!r}")
    log(f"name '{name}' typed (box {v.get('text')!r}, character {v.get('name')!r})")
    return {"via": "typed", "view": v, "at": [x, y]}


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
