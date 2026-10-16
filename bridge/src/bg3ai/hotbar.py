"""The player's own N-target cast: hotbar cell click -> one click per target -> confirm (audit ACT-8, se half).

Osiris UseSpell takes 2 targets, so Bless on 3 allies, Magic Missile darts and Scorching Ray / Eldritch Blast beams
need the route a player takes. Everything here is a player's input (cursor moves, left clicks, ESCAPE, a right
click, the HUD's tick button) checked by what the HUD shows (client ops in Client/Hotbar.lua):

    ui.hotbar_slot {spell, char}   the caster's hotbar cell: deck, index, grid, and the cell under the cursor now
    ui.cast_state {char}           spell in hand?, the cursor's target counter, tick enabled, cast entity phase
    ui.cast_confirm                the multi-target tick button (finish with fewer targets than the max)
    ui.cast_cancel                 the HUD's cancel, last resort after ESCAPE and a right click

    from bg3ai.hotbar import hotbar_cast
    res = hotbar_cast(Game(), InputClient(), caster, "Target_Bless", [a, b, c])
    res.ok, res.reason, res.stage, res.as_dict()

Wired into use_ability (server._hotbar_use, act 2026-10-16): >2 targets come here when HOTBAR_CAST_ENABLED is True
(off until acceptance se "use_ability Bless on 3 via the hotbar route (ACT-8 wiring)" passes); the mod's act.cast
{ui=true} job makes the fairness checks of an Osiris cast first (our turn in combat, the spell known and prepared,
each target seen and in range) and watches the cast; this module adds the UI's own proof.

bg3le has no Visual.PointToScreen, so the cell's screen spot is found with the cursor: the cell under it (its
IsMouseOver) tells where we are, and the deck grid (column-first, HotBar.xaml LSGrid Orientation=Vertical) tells
how far to move. Found spots are cached per window size, so the first cast after a launch pays the search
(LOCATE_BUDGET_S) and later ones one probe.

GUESSES (unverified live; G1-G6 are in Client/Hotbar.lua; acceptance se "hotbar widget tree (ACT-8, read-only)" and
"hotbar cast: Bless on 3 allies (ACT-8)" check them): G7 the HUD scales with window height / 2160 (only the first
step's pitch; then measured); G8 decks sit left to right in KeyboardHotBars order; G9 row 0 is the top row (flipped
on the first miss); G10 the deck tabs sit in the band just above the decks (TAB_FY); the cast "started" once no
spell is in hand or the cast entity's phase left None; ESCAPE / a right click put a spell in hand back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import clicker
from .game import Game, GameError
from .input import InputClient

# act's switch for routing an N-target use_ability here (off until the live acceptance item passes).
HOTBAR_CAST_ENABLED = False

BUDGET_BASE_S = 1.0          # the spec's budget: 1 s + 0.5 s per target (reported; over_budget when exceeded)
BUDGET_PER_TARGET_S = 0.5
HARD_CAP_FACTOR = 3.0        # past this many budgets the cast is cancelled (a stuck step, not a slow one)
ARM_WAIT_S = 0.6             # cell click -> the spell is in hand
PICK_WAIT_S = 0.6            # target click -> the counter moves
START_WAIT_S = 1.0           # last click / tick -> the cast started
TARGET_HOVER_S = 1.5         # hover search per target (the camera is already on the fight)
LOCATE_BUDGET_S = 4.0        # finding the cell when no spot is cached
CANCEL_WAIT_FRAMES = 6

# Where the hotbar sits (fractions of the window): bottom centre. Scan rows first, then outward from the centre.
SCAN_FY = (0.95, 0.92, 0.89, 0.97, 0.86, 0.83, 0.80)
SCAN_FX_STEP = 0.02
SCAN_FX_SPAN = 0.40
TAB_FY = (0.80, 0.83, 0.86, 0.77, 0.89, 0.74)   # GUESS G10: the deck tabs (BottomLinks) sit just above the decks
TAB_FX_STEP = 0.03
WALK_STEPS = 12
# A cell's pitch before any is measured: HotbarSlotSize 88 (HotBar.xaml) in a 2160-high design. GUESS G7: the HUD is
# scaled by window height / 2160; the walk measures the real pitch from the first two cells it sees.
DESIGN_H = 2160.0
SLOT_DESIGN = 88.0

_geom: dict[str, Any] = {"size": None, "cells": {}}   # (deck, index) -> (x, y), valid for one window size


@dataclass
class HotbarCastResult:
    ok: bool
    reason: str | None = None        # why not (a short code); detail["why"] says it in words
    stage: str | None = None         # slot | locate | arm | target | confirm | start | timeout
    picked: int = 0                  # target clicks the game took
    elapsed_s: float = 0.0
    budget_s: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def over_budget(self) -> bool:
        return self.elapsed_s > self.budget_s

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok, "picked": self.picked, "elapsed_s": round(self.elapsed_s, 2),
                             "budget_s": round(self.budget_s, 2)}
        if self.over_budget:
            d["over_budget"] = True
        for k in ("reason", "stage"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        d.update(self.detail)
        return d


def _rpc(game: Game, op: str, args: dict | None = None) -> dict:
    r = game.rpc(op, args or {}, context="client")
    return r if isinstance(r, dict) else {}


MAP_TTL_S = 20.0   # the server's hotbar map (op hotbar.map) is reused this long per character
_maps: dict[str | None, tuple[float, dict]] = {}


def server_map(game: Game, char: str | None) -> dict | None:
    """The caster's hotbar slot -> spell id map from the server's HotbarContainer (Shared/HotbarMap.lua), cached
    MAP_TTL_S. Live window #1: the HUD's slot VMs expose no spell id and the client entity may lack the component."""
    hit = _maps.get(char)
    if hit and time.monotonic() - hit[0] < MAP_TTL_S:
        return hit[1]
    if not char:
        return None
    try:
        r = game.rpc("hotbar.map", {"char": char}, context="server")
    except GameError:
        return None
    m = r.get("map") if isinstance(r, dict) else None
    if isinstance(m, dict) and m:
        _maps[char] = (time.monotonic(), m)
        return m
    return None


def _slot(game: Game, spell: str, char: str | None, tabs: bool = False) -> dict:
    args: dict[str, Any] = {"spell": spell}
    if char:
        args["char"] = char
    if tabs:
        args["tabs"] = True
    hit = _maps.get(char)
    if hit and time.monotonic() - hit[0] < MAP_TTL_S:
        args["map"] = hit[1]
    r = _rpc(game, "ui.hotbar_slot", args)
    if "map" not in args and r.get("ids") == "none" and not r.get("found"):
        # neither the slot VMs nor the client entity named the spells: the server's map, then the same read again
        m = server_map(game, char)
        if m:
            args["map"] = m
            r = _rpc(game, "ui.hotbar_slot", args)
    return r


def cast_state(game: Game, char: str | None) -> dict:
    try:
        return _rpc(game, "ui.cast_state", {"char": char} if char else {})
    except GameError:
        return {}


def cell_rc(index: int, rows: int | None) -> tuple[int, int]:
    """(column, row) of a 1-based deck index in a column-first grid of `rows` rows."""
    r = max(1, int(rows or 1))
    i = max(0, int(index) - 1)
    return i // r, i % r


def default_pitch(win_h: int, cell: dict | None = None) -> float:
    w = (cell or {}).get("w") or SLOT_DESIGN
    return float(w) * win_h / DESIGN_H


class _Locator:
    """Finds the screen spot of one hotbar cell with the cursor (closed loop on the cell's IsMouseOver)."""

    def __init__(self, game: Game, inp: InputClient, spell: str, char: str | None, want: dict):
        self.game, self.inp, self.spell, self.char = game, inp, spell, char
        self.want = want                 # the ui.hotbar_slot result for the target cell
        self.size = inp.window_size()
        if _geom["size"] != self.size:
            _geom["size"], _geom["cells"] = self.size, {}
        self.cells: dict = _geom["cells"]
        self.probes = 0
        self.last: dict = {}
        self.py_sign = 1

    def probe(self, x: float, y: float) -> dict:
        w, h = self.size
        x, y = int(round(min(max(x, 0), w - 1))), int(round(min(max(y, 0), h - 1)))
        self.inp.move(x, y)
        self.inp.wait_frames(2)
        self.probes += 1
        try:
            r = _slot(self.game, self.spell, self.char)
        except GameError as e:
            r = {"error": str(e)}
        r["_pos"] = (x, y)
        at = r.get("at")
        if isinstance(at, dict) and at.get("deck") is not None and at.get("index") is not None:
            self.cells[(at["deck"], at["index"])] = (x, y, at.get("rows"), at.get("cols"), at.get("order"))
        self.last = r
        return r

    def pitch(self, deck: str) -> tuple[float, float]:
        """(px per column, px per row) for `deck`, measured from the cells seen in it, else the default."""
        seen = [(k[1], v) for k, v in self.cells.items() if k[0] == deck]
        px = py = None
        for i, (ia, va) in enumerate(seen):
            for ib, vb in seen[i + 1:]:
                ca, ra = cell_rc(ia, va[2])
                cb, rb = cell_rc(ib, vb[2])
                if px is None and ca != cb:
                    px = (vb[0] - va[0]) / (cb - ca)
                if py is None and ra != rb:
                    py = (vb[1] - va[1]) / (rb - ra)
        d = default_pitch(self.size[1], self.want.get("cell"))
        # GUESS G9: row 0 is the top bar (rows grow downward); the first measured pair replaces the sign
        return (px if px else d), (py if py else d * self.py_sign)

    def predict(self) -> tuple[float, float] | None:
        """Where the wanted cell should be, from a cached spot of it or of another cell in its deck."""
        deck, idx, rows = self.want["deck"], self.want["index"], self.want.get("rows")
        hit = self.cells.get((deck, idx))
        if hit:
            return hit[0], hit[1]
        same = [(k[1], v) for k, v in self.cells.items() if k[0] == deck]
        if not same:
            return None
        px, py = self.pitch(deck)
        i0, v0 = same[0]
        c0, r0 = cell_rc(i0, v0[2] or rows)
        ct, rt = cell_rc(idx, rows)
        return v0[0] + (ct - c0) * px, v0[1] + (rt - r0) * py

    def scan(self, deadline: float) -> dict | None:
        """Sweep the bottom band until the cursor is on any hotbar cell."""
        w, h = self.size
        n = int(SCAN_FX_SPAN / SCAN_FX_STEP)
        fxs = [0.5] + [0.5 + s * k * SCAN_FX_STEP for k in range(1, n + 1) for s in (-1, 1)]
        for fy in SCAN_FY:
            for fx in fxs:
                if time.monotonic() >= deadline:
                    return None
                r = self.probe(fx * w, fy * h)
                if r.get("over") or r.get("at"):
                    return r
        return None

    def walk(self, r: dict, deadline: float) -> dict | None:
        """From the cell under the cursor, step toward the wanted one. Returns the result with over=True or None.
        A move that falls off the bar goes back to the last cell seen; after a row move it also flips the row sign
        (GUESS G9 wrong)."""
        deck, idx, rows = self.want["deck"], self.want["index"], self.want.get("rows")
        misses, prev, last_dr = 0, r, 0
        for _ in range(WALK_STEPS):
            if r.get("over"):
                return r
            if time.monotonic() >= deadline:
                return None
            at = r.get("at")
            if not at:
                misses += 1
                if misses > 3 or not prev.get("at"):
                    return None
                if last_dr:
                    self.py_sign, last_dr = -self.py_sign, 0
                r = prev          # re-plan from the last cell seen
                continue
            prev = r
            x, y = r["_pos"]
            if at.get("deck") == deck:
                px, py = self.pitch(deck)
                ca, ra = cell_rc(at["index"], at.get("rows") or rows)
                ct, rt = cell_rc(idx, rows)
                last_dr = rt - ra
                r = self.probe(x + (ct - ca) * px, y + (rt - ra) * py)
            else:
                # another deck: decks sit left to right in KeyboardHotBars order (GUESS G8); jump past its edge
                px, _ = self.pitch(at["deck"])
                ca, _ = cell_rc(at["index"], at.get("rows"))
                cols = at.get("cols") or (ca + 1)
                right = (self.want.get("order") or 0) > (at.get("order") or 0)
                dx = (cols - ca) * px if right else -(ca + 1) * px
                last_dr = 0
                r = self.probe(x + dx, y)
        return r if r.get("over") else None

    def locate(self) -> tuple[tuple[int, int] | None, dict]:
        deadline = time.monotonic() + LOCATE_BUDGET_S
        p = self.predict()
        r = self.probe(*p) if p else None
        if r and r.get("over"):
            return r["_pos"], r
        if r is None or not r.get("at"):
            r = self.scan(deadline)
            if r is None:
                return None, self.last
        r2 = self.walk(r, deadline)
        if r2 is None:
            return None, self.last
        return r2["_pos"], r2


DECK_SETTLE_S = 0.45   # HotBar.xaml DeckResizeDuration-ish: the concertina animates the shown deck open
_tabs: dict[str, Any] = {"size": None, "at": {}}   # deck tab name -> (x, y), per window size


def show_deck(game: Game, inp: InputClient, spell: str, char: str | None, want: dict) -> tuple[bool, dict]:
    """Paging: click the wanted deck's tab (HotbarDeckButton_*) so the concertina opens that deck in full, as a
    player clicks "Class" above the hotbar. The tab is found with the cursor (deck_button_over); its spot is cached."""
    tab = want.get("deck_button")
    if not tab:
        return False, {"why": f"no deck tab known for {want.get('deck')}"}
    size = inp.window_size()
    if _tabs["size"] != size:
        _tabs["size"], _tabs["at"] = size, {}
    w, h = size
    deadline = time.monotonic() + LOCATE_BUDGET_S
    pts: list[tuple[int, int]] = []
    if tab in _tabs["at"]:
        pts.append(_tabs["at"][tab])
    n = int(SCAN_FX_SPAN / TAB_FX_STEP)
    fxs = [0.5] + [0.5 + sgn * k * TAB_FX_STEP for k in range(1, n + 1) for sgn in (-1, 1)]
    pts += [(int(fx * w), int(fy * h)) for fy in TAB_FY for fx in fxs]
    probes = 0
    for x, y in pts:
        if time.monotonic() >= deadline:
            break
        inp.move(x, y)
        inp.wait_frames(2)
        probes += 1
        try:
            r = _slot(game, spell, char, tabs=True)
        except GameError as e:
            return False, {"why": str(e), "probes": probes}
        over = r.get("deck_button_over")
        if over:
            _tabs["at"][over] = (x, y)
        if over == tab:
            inp.click(x, y, button="L")
            time.sleep(DECK_SETTLE_S)
            _geom["cells"] = {k: v for k, v in _geom["cells"].items() if k[0] != want.get("deck")}
            return True, {"tab": tab, "probes": probes}
    return False, {"why": f"could not point at the {tab} tab", "probes": probes}


def locate_cell(game: Game, inp: InputClient, spell: str, char: str | None, want: dict) -> tuple[tuple[int, int] | None, dict]:
    """The window spot of the wanted cell with the cursor on it (IsMouseOver proof), or (None, last read)."""
    loc = _Locator(game, inp, spell, char, want)
    pos, last = loc.locate()
    last = dict(last, probes=loc.probes)
    if pos is None:
        _geom["cells"].pop((want.get("deck"), want.get("index")), None)
    return pos, last


def _armed(game: Game, char: str | None) -> dict:
    return cast_state(game, char)


def cancel(game: Game, inp: InputClient, char: str | None) -> str | None:
    """Put the spell back as a player does: ESCAPE, then a right click, then the HUD's cancel. None when nothing
    was in hand; else how it was cancelled ("escape" | "right_click" | "command" | "failed")."""
    if not _armed(game, char).get("armed"):
        return None
    inp.key("ESCAPE")
    inp.wait_frames(CANCEL_WAIT_FRAMES)
    if not _armed(game, char).get("armed"):
        how = "escape"
    else:
        w, h = inp.window_size()
        inp.click(w // 2, int(h * 0.4), button="R")
        inp.wait_frames(CANCEL_WAIT_FRAMES)
        if not _armed(game, char).get("armed"):
            how = "right_click"
        else:
            try:
                _rpc(game, "ui.cast_cancel", {})
            except GameError:
                pass
            inp.wait_frames(CANCEL_WAIT_FRAMES)
            how = "command" if not _armed(game, char).get("armed") else "failed"
    # an ESCAPE that landed after the spell was already gone opens the game menu: close it again
    if clicker._screen(game).get("screen") == "game_menu":
        inp.key("ESCAPE")
    return how


def _progress(before: dict, now: dict) -> bool:
    """A target click was taken: the cursor counter went down, or the cast entity holds more targets."""
    rb, rn = before.get("remaining"), now.get("remaining")
    if isinstance(rb, (int, float)) and isinstance(rn, (int, float)) and rn < rb:
        return True
    pb = (before.get("cast") or {}).get("picked")
    pn = (now.get("cast") or {}).get("picked")
    return isinstance(pb, int) and isinstance(pn, int) and pn > pb


def _started(now: dict) -> bool:
    """The cast left the targeting phase: no spell in hand any more, or the cast entity moved past None."""
    if not now:
        return False
    if not now.get("armed"):
        return True
    phase = (now.get("cast") or {}).get("phase")
    return phase not in (None, "", "None", "0")


def _wait(game: Game, char: str | None, pred, timeout: float) -> tuple[bool, dict]:
    end = time.monotonic() + timeout
    st: dict = {}
    while True:
        st = cast_state(game, char)
        if pred(st):
            return True, st
        if time.monotonic() >= end:
            return False, st
        time.sleep(0.03)


def arm(game: Game, inp: InputClient, caster: str, spell: str, res: HotbarCastResult,
        select: bool = True, should_stop=None) -> tuple[dict | None, dict]:
    """The hotbar half every UI cast shares: find the caster's cell (select the caster, page the deck), click it and
    prove the spell is in hand. Returns (None, cast_state) once armed, else (failure, {}) where failure holds
    done()'s stage/reason/why/cancel_it (+ extra). Fills res.detail: cell, probes, paging, cell_pos, arm_s."""

    def fail(stage: str, reason: str, why: str | None, cancel_it: bool = False, **extra) -> tuple[dict, dict]:
        return dict(stage=stage, reason=reason, why=why, cancel_it=cancel_it, **extra), {}

    scr = clicker._screen(game).get("screen")
    if scr in clicker.COVERING_SCREENS:
        return fail("slot", "covered", f"the {scr} screen covers the hotbar (press_key ESCAPE first)")
    try:
        want = _slot(game, spell, caster)
    except GameError as e:
        return fail("slot", "no_hotbar", str(e))
    if want.get("selected") is False and select:
        # the hotbar shows the selected character: select the caster first, as a player clicks its portrait
        game.run_job("act.select", {"char": caster}, wait_s=5)
        try:
            want = _slot(game, spell, caster)
        except GameError as e:
            return fail("slot", "no_hotbar", str(e))
    if want.get("selected") is False:
        return fail("slot", "not_selected", "the caster is not the selected character")
    if not want.get("found"):
        return fail("slot", "not_on_hotbar", want.get("why") or "not on the hotbar")
    for k in ("deck", "index", "rows", "cols"):
        if want.get(k) is not None:
            res.detail.setdefault("cell", {})[k] = want[k]
    if want.get("upcast"):
        return fail("slot", "upcast", "an upcast through the hotbar's level picker is not built yet")
    if want.get("container"):
        return fail("slot", "container", "the spell sits in a container slot (its variants' sub-bar is not built yet)")
    if want.get("can_use") is False:
        return fail("slot", "cannot_use", "the hotbar shows the spell greyed out (no slot/resource, or not now)")

    pre = cast_state(game, caster)
    if pre.get("armed"):
        res.detail["cleared_before"] = cancel(game, inp, caster)

    pos, last = (None, {}) if want.get("rendered") is False else locate_cell(game, inp, spell, caster, want)
    res.detail["probes"] = last.get("probes")
    if pos is None and want.get("shown_deck") != want.get("deck"):
        # paging: the deck is collapsed (no button for the cell) or squeezed in the concertina (no hover)
        paged, pinfo = show_deck(game, inp, spell, caster, want)
        res.detail["paging"] = pinfo
        if paged:
            want = _slot(game, spell, caster)
            pos, last = locate_cell(game, inp, spell, caster, want)
            res.detail["probes"] = (res.detail.get("probes") or 0) + (last.get("probes") or 0)
    if pos is None:
        if want.get("rendered") is False:
            return fail("locate", "not_rendered",
                        f"the {want.get('deck')} deck does not show the cell (deck tab {want.get('deck_button')})")
        return fail("locate", "cell_not_found", "could not point at the hotbar cell", at=last.get("at"))
    if should_stop is not None and should_stop():
        return fail("arm", "cancelled", "the step was cancelled or interrupted before the cell click")
    t_click = time.monotonic()
    inp.click(pos[0], pos[1], button="L")
    sp_lower = spell.lower()

    def in_hand(st: dict) -> bool:
        s = (st.get("spell") or (st.get("cast") or {}).get("spell") or "")
        return bool(st.get("armed")) and (not s or s.lower() == sp_lower)

    armed_ok, st = _wait(game, caster, in_hand, ARM_WAIT_S)
    res.detail["cell_pos"] = pos
    if not armed_ok:
        if st.get("armed"):
            # another spell in hand: the click landed on the wrong cell (a stale cached spot); forget the spot
            _geom["cells"].pop((want.get("deck"), want.get("index")), None)
            return fail("arm", "wrong_spell", f"the cell click put {st.get('spell')} in hand",
                        cancel_it=True, state=st)
        return fail("arm", "not_armed", "the cell click did not put the spell in hand", state=st)
    res.detail["arm_s"] = round(time.monotonic() - t_click, 2)
    return None, st


def hotbar_cast(game: Game, inp: InputClient | None, caster: str, spell: str, targets: list[str],
                select: bool = True, should_stop=None) -> HotbarCastResult:
    """Cast `spell` from `caster`'s hotbar on `targets` (uuids, in click order; the same uuid twice = 2 darts or
    beams). Steps: the cell (ui.hotbar_slot; the deck's tab first when it is collapsed) -> click it, prove the spell
    is in hand -> per target a hover-verified click and a counter read -> the tick when fewer than the max -> proof
    the cast started. Any failure after the cell click cancels, so no spell stays in hand. should_stop() -> True
    (the step was cancelled / interrupted, audit review M1): no more clicks, the spell in hand is cancelled."""
    t0 = time.monotonic()
    budget = BUDGET_BASE_S + BUDGET_PER_TARGET_S * len(targets)
    res = HotbarCastResult(False, budget_s=budget, detail={"spell": spell, "targets": len(targets)})

    def done(ok: bool, stage: str | None = None, reason: str | None = None, why: str | None = None,
             cancel_it: bool = False, **extra) -> HotbarCastResult:
        if cancel_it and inp is not None:
            how = cancel(game, inp, caster)
            if how:
                extra["cancelled"] = how
        res.ok, res.stage, res.reason = ok, stage, reason
        if why:
            extra["why"] = why
        res.detail.update(extra)
        res.elapsed_s = time.monotonic() - t0
        return res

    if inp is None:
        return done(False, "slot", "no_input", "input shim not available for the clicks")
    if not targets:
        return done(False, "slot", "no_targets", "no targets")
    fail, st = arm(game, inp, caster, spell, res, select, should_stop)
    if fail:
        return done(False, **fail)
    stop = should_stop or (lambda: False)
    hard = t0 + budget * HARD_CAP_FACTOR + LOCATE_BUDGET_S
    for i, uuid in enumerate(targets):
        last_one = i == len(targets) - 1
        if time.monotonic() >= hard:
            return done(False, "timeout", "timeout", f"over {round(hard - t0, 1)} s", cancel_it=True)
        before = st
        clicker.bring_into_view(game, inp, uuid, steps=3)
        hover_deadline = min(hard, time.monotonic() + TARGET_HOVER_S)
        tpos, tries, hc = clicker.hover_point(game, inp, uuid, deadline=hover_deadline)
        if tpos is None:
            reason = "off_screen" if tries == 0 and hc.get("off_screen") else "hover_miss"
            return done(False, "target", reason, f"could not point at target {i + 1} ({uuid})",
                        cancel_it=True, target=uuid)
        if stop():
            return done(False, "target", "cancelled", "the step was cancelled or interrupted", cancel_it=True)
        inp.click(tpos[0], tpos[1], button="L")
        res.picked = i + 1   # provisional; reset below when the game did not take it
        took, st = _wait(game, caster, lambda s: _progress(before, s) or _started(s), PICK_WAIT_S)
        if not took:
            res.picked = i
            return done(False, "target", "not_picked", f"the game did not take target {i + 1} ({uuid}): out of "
                        "range, not a valid target, or not in sight", cancel_it=True, target=uuid, state=st)
        if _started(st) and not last_one:
            # the spell's max was reached before our list ended: the game cast on what it had
            return done(False, "target", "fired_early",
                        f"the cast went off after {i + 1} of {len(targets)} targets (the spell's max)", state=st)
    if not _started(st):
        if stop():
            return done(False, "confirm", "cancelled", "the step was cancelled or interrupted", cancel_it=True)
        if st.get("armed") and st.get("can_force"):
            try:
                _rpc(game, "ui.cast_confirm", {})
                res.detail["confirmed"] = True
            except GameError as e:
                return done(False, "confirm", "confirm_failed", str(e), cancel_it=True)
        started, st = _wait(game, caster, _started, START_WAIT_S)
        if not started:
            # review M3: a late start (past START_WAIT_S) is still a cast: one more read before calling it a failure
            st = cast_state(game, caster) or st
            started = _started(st)
        if not started:
            return done(False, "start", "not_started", "the cast did not start after the last target",
                        cancel_it=True, state=st)
    return done(True, phase=(st.get("cast") or {}).get("phase"))
