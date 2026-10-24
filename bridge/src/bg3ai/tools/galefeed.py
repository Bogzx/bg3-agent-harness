"""feed_gale's flow: give the item in the donation window, close the window, check Gale took it.

Live D10 (research/fix/live.md "Window D10", 2026-10-24): the mod's DonateItemsCommand(Tag) moves the item out of the
window, but Gale absorbs it only when the window closes. Its close button is CustomEvent "CloseWidget" (a string bg3le
can't pass) and ESCAPE doesn't close it; a mouse click on the X did, and then '[Arcane Hunger]' was gone. So the X is
clicked hover-checked (hoverclick.py, the mod's ui.donate_probe), and done means the item left the party and the
hunger left Gale. Before, feed_gale said "done — donated=e132 window_open" with the gloves still worn.
"""

from __future__ import annotations

import time

from .. import hoverclick
from ..game import JobResult
from . import core, plumbing, reporting, world_tools

CLOSE_SEED = (0.5727, 0.2875)  # D10: the X at (733, 207) of a 1280x720 shot; ~10 px wide there
CLOSE_STEP = 0.005  # finer than Steal's grid: the X is ~0.008 of the width
HUNGER = {"ORI_GALE_BOMB1"}  # Gale's "Arcane Hunger" (D10 state.status)
SETTLE_S = 5.0  # window close -> item consumed and the status removed
POLL_S = 0.25


def _hungry(st: dict) -> bool:
    return any(
        isinstance(s, dict) and s.get("id") in HUNGER
        for m in st.get("party") or []
        if isinstance(m, dict)
        for s in m.get("statuses") or []
    )


def _in_party(uuid: str) -> bool:
    inv = core.game.try_rpc("state.inventory", {"expand": True}) or {}
    return any(isinstance(it, dict) and it.get("uuid") == uuid for it in inv.get("items") or [])


def _donate(uuid: str) -> JobResult:
    try:
        return core.game.run_job("ui.donate", {"item": uuid}, context="client", wait_s=15)
    except Exception as e:
        return JobResult("failed", error=str(e))


def feed(uuid: str) -> str:
    """The item's uuid -> feed_gale's reply."""
    t0, args = time.monotonic(), {"item": uuid}
    h = core.H.get(uuid)
    h = f"{h} {core.H.names[h]}" if core.H.names.get(h) else h
    with plumbing._InputHeld():
        r = _donate(uuid)
        if r.status != "done":
            return reporting._finish("feed_gale", args, plumbing._job_text("feed_gale", r), t0)
        res = r.result if isinstance(r.result, dict) else {}
        if res.get("window_open"):
            inp = world_tools._inp()
            if inp is None:
                why = "no mouse input to close it (the input helper is not running)"
            else:
                probe = lambda: core.game.rpc("ui.donate_probe", {}, context="client")  # noqa: E731
                click = hoverclick.hover_click(probe, inp, "donate_close", CLOSE_SEED, step=CLOSE_STEP)
                why = (
                    ""
                    if click.get("clicked") or click.get("gone")
                    else (f"couldn't click its close button ({click.get('error')}; {click.get('tries', 0)} tries)")
                )
            if why:
                return reporting._finish(
                    "feed_gale",
                    args,
                    f"feed_gale: FAILED — {h} is in Gale's side of the "
                    f"donation window, but {why}; Gale takes it when the window closes",
                    t0,
                )
    end = time.monotonic() + SETTLE_S
    while True:
        st = core.game.try_rpc("state.status") or {}
        hungry = _hungry(st) if isinstance(st, dict) else True
        if not hungry and not (core.game.try_rpc("ui.donate_probe", {}, context="client") or {}).get("open"):
            break
        if time.monotonic() >= end:
            break
        time.sleep(POLL_S)
    kept = _in_party(uuid)
    if kept or hungry:
        why = "it is still in the party's inventory" if kept else "Gale still has Arcane Hunger"
        return reporting._finish(
            "feed_gale", args, f"feed_gale: FAILED — gave {h} and closed the window, but {why}", t0
        )
    return reporting._finish(
        "feed_gale", args, f"feed_gale: done — Gale absorbed {h} (no Arcane Hunger on him now)", t0
    )
