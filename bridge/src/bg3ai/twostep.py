"""The player's own two-step cast: hotbar cell click -> the first pick -> a ground click (audit ACT-9, act).

Revivify / Resurrection (the spell) and Dimension Door take two picks, which Osiris UseSpell can't give without the
TargetPos2 engine write (off: an unproven struct mapping, HANDOFF §7). A player gives them with two clicks:

    Revivify        the body (a dead ally, its corpse or tombstone), then the ground beside it (not a character)
    Dimension Door  who goes (the caster to go alone, or the ally beside them), then the landing on the ground

Everything is a player's input (cursor moves, left clicks, ESCAPE / a right click to put the spell back) checked by
what the player sees: the hotbar cell and the spell in hand (hotbar.arm), the engine's picking helper on the first
pick (clicker.hover_point), and for the ground click the cursor's world point, the character under it and the
cursor shape (client op ui.ground_check: "Cast" vs "CastWarning"), then the spell state after the click (the pick
taken, the cast started).

    from bg3ai.twostep import twostep_cast
    res = twostep_cast(Game(), InputClient(), caster, "Teleportation_DimensionDoor", caster, landing=[x, y, z])
    res = twostep_cast(Game(), InputClient(), caster, "Teleportation_Revivify", body)

Wired into use_ability (server._twostep_use) behind hotbar.HOTBAR_CAST_ENABLED (off): the mod's act.cast {ui=true}
job makes the fairness checks first (our turn, known/prepared, the body / ally seen and in range, the landing in
range and on a floor) and watches the cast; when off, the mod's ACT-9 refusal stands.

GUESSES (unverified live; acceptance act "Dimension Door 10 m via the hotbar (ACT-9)" and SESSION-WATCH "Revivify
via the hotbar" check them): T1 the cursor reads "CastWarning" on a spot the spell won't take (Pick.lua); T2 the
game asks who goes first, then the landing (game data OriginTargetConditions; if the first click is not taken, the
landing is tried first once, DD_ORDER); T3 a taken first pick shows in ui.cast_state (the counter, the cast entity's
picked/index) or the cursor leaving the first step; T4 a ground click on a spot the game refuses leaves the spell
in hand (we then try the next spot).
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from . import clicker, hotbar
from .game import Game, GameError
from .hotbar import HotbarCastResult
from .input import InputClient

BUDGET_S = 3.0                 # cell + 2 clicks, a player's pace (reported; over_budget when exceeded)
HARD_CAP_S = 12.0              # past this the cast is cancelled
PICK_WAIT_S = 0.8              # first pick click -> the state moves
LAND_WAIT_S = 1.2              # ground click -> the cast started
GROUND_TOL_M = 0.8             # the cursor's world point this close (flat) to the spot we aim at
# Revivify's second pick ("not Character()"): the ground this far from the body's feet, toward the caster first,
# then turned by SPOT_TURNS_DEG (S089 07:08 x3: the spots 1.2-2.4 m toward the reader all lay under a character, the
# reader and the Emperor it had just helped up; the game said "Target can't be a creature. Choose destination").
# A27 (S090 07:12-07:13, Wyll's body in fire on the High Hall stairs): the game answered "Target is blocked!" on the
# clear stairs 1.5-3 m below the body toward the reader and took the burning floor right beside it (the agent's hand
# click, ~1 m off): the ring closest to the body first, burning spots in their ring order (not last).
SPOT_DISTS = (1.0, 1.5)
SPOT_TURNS_DEG = (0, 60, -60, 120, -120, 180)
# A ground click the game refuses ("Target is blocked!") keeps the spell in hand and costs nothing: a revive tries
# up to a whole ring of them.
REVIVE_MAX_CLICKS = len(SPOT_TURNS_DEG)
# S089/S090: the spot taken inside a harmful patch the party sees (nav.hazards: centre + w/d box) is named in the
# result (they stand up downed and burning); this much beyond the box counts as in it.
HAZARD_MARGIN_M = 0.5
HAZARD_SCAN_M = 12
LAND_NUDGES_M = ((0.0, 0.0), (0.4, 0.0), (-0.4, 0.0), (0.0, 0.4), (0.0, -0.4))   # around a landing the game refused
MAX_GROUND_CLICKS = 3
# L8 23:58 (act, Misty Step scroll 6 m): all 5 spots (the landing, then +-0.4 m) read "a character is under the
# cursor" (Shadowheart): a nudge smaller than a body never leaves it. A landing whose cursor ray hits a character
# is tried again this far (flat) from that character's feet: straight away from it through the landing (along the
# cast direction when the landing is on it), then turned by CLEAR_TURNS_DEG (sideways clears a body standing
# between the camera and the spot). At most CLEAR_BODIES characters; spots past CLEAR_MAX_SHIFT_M from the asked
# landing are not tried.
BODY_CLEAR_M = 1.2
CLEAR_TURNS_DEG = (0, 60, -60, 120, -120)
CLEAR_BODIES = 2
CLEAR_MAX_SHIFT_M = 2.5
VIEW_STEPS = 4                 # camera moves to bring a ground point into view
DD_ORDER = ("who", "landing")  # GUESS T2

_TWO_STEP = re.compile(r"revivify|resurrection|dimensiondoor|dimension_door", re.I)


def is_two_step(spell: str) -> bool:
    """A spell the game picks in two clicks (the mod's A.two_step_spell reads the stats; this is its name form for
    routing before any call)."""
    return bool(_TWO_STEP.search(str(spell or "")))


def is_revive(spell: str) -> bool:
    return bool(re.search(r"revivify|resurrection", str(spell or ""), re.I))


def _rpc(game: Game, op: str, args: dict) -> dict:
    r = game.rpc(op, args, context="client")
    return r if isinstance(r, dict) else {}


def ground_check(game: Game, pos: list[float] | None, avoid: list[str] | None = None,
                 tol: float = GROUND_TOL_M) -> dict:
    args: dict[str, Any] = {"tol": tol, "no_chars": True}
    if pos is not None:
        args["pos"] = list(pos)
    if avoid:
        args["avoid"] = list(avoid)
    try:
        return _rpc(game, "ui.ground_check", args)
    except GameError as e:
        return {"error": str(e)}


def ground_ok(gc: dict) -> tuple[bool, str | None]:
    """May a ground click go in here? The cursor on our spot, no avoided character under it, no warning cursor.
    An unreadable cursor shape (no T1 read) does not block: the post-click state proves it then."""
    if gc.get("error"):
        return False, f"ground_check: {gc['error']}"
    if gc.get("on_avoid"):
        return False, f"a character is under the cursor ({gc.get('hover')})"
    if gc.get("near") is False:
        return False, f"the cursor's ground point is {gc.get('d_m')} m off"
    if gc.get("warning"):
        return False, f"the game shows a {gc.get('cursor') or gc.get('overlay')} cursor there"
    return True, None


def clear_spots(want: list[float], body: list[float], origin: list[float] | None = None) -> list[list[float]]:
    """Ground points BODY_CLEAR_M (flat) from `body` (the feet of the character the cursor ray hit when aimed at
    `want`), at want's height: away from the body through `want` first, then turned by CLEAR_TURNS_DEG; `want` on
    the body itself: along the cast direction (from `origin`, the caster), else +x. Spots farther than
    CLEAR_MAX_SHIFT_M from `want` are left out."""
    dx, dz = want[0] - body[0], want[2] - body[2]
    d = math.hypot(dx, dz)
    if d < 0.2 and origin is not None:
        dx, dz = want[0] - origin[0], want[2] - origin[2]
        d = math.hypot(dx, dz)
    if d < 1e-6:
        dx, dz, d = 1.0, 0.0, 1.0
    ux, uz = dx / d, dz / d
    out = []
    for deg in CLEAR_TURNS_DEG:
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        p = [round(body[0] + BODY_CLEAR_M * (ux * c - uz * s), 2), want[1],
             round(body[2] + BODY_CLEAR_M * (ux * s + uz * c), 2)]
        if math.hypot(p[0] - want[0], p[2] - want[2]) <= CLEAR_MAX_SHIFT_M:
            out.append(p)
    return out


def revive_spots(body: list[float], caster: list[float] | None) -> list[list[float]]:
    """Ground points around `body` (feet) at its height: each of SPOT_DISTS, toward `caster` first (+x without one),
    then turned by SPOT_TURNS_DEG."""
    dx, dz = (caster[0] - body[0], caster[2] - body[2]) if caster else (1.0, 0.0)
    d = math.hypot(dx, dz)
    if d < 1e-6:
        dx, dz, d = 1.0, 0.0, 1.0
    ux, uz = dx / d, dz / d
    out = []
    for r in SPOT_DISTS:
        for deg in SPOT_TURNS_DEG:
            c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
            out.append([round(body[0] + r * (ux * c - uz * s), 2), body[1], round(body[2] + r * (ux * s + uz * c), 2)])
    return out


def in_hazard(p: list[float], patches: list[dict] | None) -> str | None:
    """The surface of the nav.hazards patch whose box (+HAZARD_MARGIN_M) holds `p` at about its height, else None."""
    for h in patches or []:
        c = h.get("center") if isinstance(h, dict) else None
        if not (isinstance(c, list) and len(c) >= 3):
            continue
        hw = float(h.get("w_m") or 0) / 2 + HAZARD_MARGIN_M
        hd = float(h.get("d_m") or 0) / 2 + HAZARD_MARGIN_M
        if abs(p[0] - c[0]) <= hw and abs(p[2] - c[2]) <= hd and abs(p[1] - c[1]) <= 2.0:
            return str(h.get("surface") or "a harmful surface")
    return None


def looks_brief(looks: list[dict]) -> str:
    """The cursor looks of a ground click in one line: where the ground point came from and what was under the
    cursor, counted ("ground point from ray_plane x5; under the cursor: <uuid> x5")."""
    if not looks:
        return ""
    def counted(key: str) -> str:
        n: dict[str, int] = {}
        for lk in looks:
            if lk.get(key):
                n[str(lk[key])] = n.get(str(lk[key]), 0) + 1
        return ", ".join(f"{k} x{v}" for k, v in n.items())
    src, hov = counted("world_src"), counted("hover")
    return "; ".join(x for x in (f"{len(looks)} cursor looks", f"ground point from {src or 'nothing read'}",
                                 f"under the cursor: {hov}" if hov else "") if x)


def _took(before: dict, now: dict) -> bool:
    """The first pick was taken (GUESS T3): the counter / the cast entity moved, or the cast already started."""
    if hotbar._progress(before, now) or hotbar._started(now):
        return True
    bi, ni = (before.get("cast") or {}).get("index"), (now.get("cast") or {}).get("index")
    if isinstance(bi, int) and isinstance(ni, int) and ni != bi:
        return True
    bt, nt = before.get("targeting"), now.get("targeting")
    return bool(bt and nt and bt != nt)


class _Run:
    def __init__(self, game: Game, inp: InputClient, caster: str, spell: str, first: str,
                 landing: list[float] | None, should_stop=None):
        self.stop = should_stop or (lambda: False)
        self.game, self.inp, self.caster, self.spell = game, inp, caster, spell
        self.first, self.landing = first, landing
        self.t0 = time.monotonic()
        self.hard = self.t0 + HARD_CAP_S
        self.res = HotbarCastResult(False, budget_s=BUDGET_S,
                                    detail={"spell": spell, "targets": 2, "two_step": "revive" if landing is None
                                            else "teleport"})
        self.st: dict = {}
        self.hazards: list[dict] = []

    def done(self, ok: bool, stage: str | None = None, reason: str | None = None, why: str | None = None,
             cancel_it: bool = False, **extra) -> HotbarCastResult:
        if cancel_it:
            how = hotbar.cancel(self.game, self.inp, self.caster)
            if how:
                extra["cancelled"] = how
        self.res.ok, self.res.stage, self.res.reason = ok, stage, reason
        if why:
            extra["why"] = why
        self.res.detail.update(extra)
        self.res.elapsed_s = time.monotonic() - self.t0
        return self.res

    # --- the first pick: a character, hover-verified
    def pick_first(self) -> tuple[bool, dict]:
        clicker.bring_into_view(self.game, self.inp, self.first, steps=3)
        deadline = min(self.hard, time.monotonic() + hotbar.TARGET_HOVER_S)
        seen: dict[str, int] = {}
        tpos, tries, hc = clicker.hover_point(self.game, self.inp, self.first, deadline=deadline, seen=seen)
        if tpos is None:
            reason = "off_screen" if tries == 0 and hc.get("off_screen") else "hover_miss"
            # L7 22:09: the first pick's miss said only "could not point at"; what the helper named instead
            info = {"reason": reason, "why": f"could not point at {self.first}", "hover_tries": tries,
                    "hovered_instead": seen,
                    "hover_last": {k: hc.get(k) for k in ("hover", "cursor", "world", "timed_out")
                                   if hc.get(k) is not None}}
            return False, {k: v for k, v in info.items() if v or k == "hover_tries"}
        if hc.get("via_field"):
            self.res.detail["first_via"] = hc["via_field"]
        if self.stop():
            return False, {"reason": "cancelled", "why": "the step was cancelled or interrupted"}
        before = self.st
        self.inp.click(tpos[0], tpos[1], button="L")
        took, st = hotbar._wait(self.game, self.caster, lambda s: _took(before, s), PICK_WAIT_S)
        self.st = st
        if not took:
            return False, {"reason": "not_picked", "state": st,
                           "why": "the game did not take the first pick (not a valid target, out of range or sight)"}
        self.res.picked += 1
        self.res.detail["first_s"] = round(time.monotonic() - self.t0, 2)
        return True, {}

    # --- the ground click: candidate world points, each cursor-checked, then the state after the click
    def spots(self) -> list[list[float]]:
        if self.landing is not None:
            return [[self.landing[0] + dx, self.landing[1], self.landing[2] + dz] for dx, dz in LAND_NUDGES_M]
        try:
            feet = _rpc(self.game, "ui.screen_pos", {"uuid": self.first, "aim": "feet"}).get("world")
            org = _rpc(self.game, "ui.screen_pos", {"uuid": self.caster, "aim": "feet"}).get("world")
        except GameError:
            return []
        if not (isinstance(feet, list) and len(feet) >= 3):
            return []
        out = revive_spots(feet, org if isinstance(org, list) and len(org) >= 3 else None)
        try:
            hz = (self.game.rpc("nav.hazards", {"char": self.caster, "radius": HAZARD_SCAN_M}) or {}).get("hazards")
        except GameError:
            hz = None
        self.hazards = hz if isinstance(hz, list) else []
        return out

    def _clear_of(self, body: str) -> list[list[float]]:
        """Landing spots clear of the character `body` the cursor ray hit (its feet from ui.screen_pos)."""
        try:
            feet = _rpc(self.game, "ui.screen_pos", {"uuid": body, "aim": "feet"}).get("world")
            org = _rpc(self.game, "ui.screen_pos", {"uuid": self.caster, "aim": "feet"}).get("world")
        except GameError:
            return []
        if not (isinstance(feet, list) and len(feet) >= 3 and self.landing is not None):
            return []
        return clear_spots(self.landing, feet, org if isinstance(org, list) and len(org) >= 3 else None)

    def click_ground(self, done_when) -> tuple[bool, dict]:
        """Click candidate spots until `done_when(state)` holds. Never clicks on a warning cursor or a character."""
        if self.landing is not None:
            sp0, moves = clicker.view_point(self.game, self.inp, self.landing, VIEW_STEPS)
            if moves:
                self.res.detail["camera"] = moves
            if sp0.get("error"):
                return False, {"reason": "error", "why": sp0["error"]}
        avoid = [self.first] if self.landing is None else [self.first, self.caster]
        w, h = self.inp.window_size()
        clicks, looks = 0, []
        max_clicks = MAX_GROUND_CLICKS if self.landing is not None else REVIVE_MAX_CLICKS
        last: dict = {}
        todo = self.spots()
        cleared: list[str] = []
        while todo:
            spot = todo.pop(0)
            if time.monotonic() >= self.hard or clicks >= max_clicks:
                break
            try:
                sp = _rpc(self.game, "ui.screen_pos", {"pos": spot})
            except GameError as e:
                last = {"reason": "error", "why": str(e)}
                continue
            if not sp.get("on_screen") or sp.get("x") is None:
                last = {"reason": "off_screen", "why": "the ground spot is off screen"}
                continue
            x, y = int(sp["x"]), int(sp["y"])
            if not (0 <= x < w and 0 <= y < h):
                continue
            want = sp.get("world") or spot
            self.inp.move(x, y)
            self.inp.wait_frames(2)
            gc = ground_check(self.game, want, avoid)
            ok, why = ground_ok(gc)
            looks.append({k: gc.get(k) for k in ("d_m", "world_src", "cursor", "warning", "on_avoid", "hover")
                          if gc.get(k) is not None})
            if not ok:
                last = {"reason": "ground_refused", "why": why, "ground": gc}
                body = gc.get("hover") if gc.get("on_avoid") else None
                if body and body not in cleared and len(cleared) < CLEAR_BODIES:
                    cleared.append(body)
                    todo[:0] = self._clear_of(body)
                continue
            if self.stop():
                return False, {"reason": "cancelled", "why": "the step was cancelled or interrupted"}
            before = self.st
            self.inp.click(x, y, button="L")
            clicks += 1
            took, st = hotbar._wait(self.game, self.caster, lambda s: done_when(before, s), LAND_WAIT_S)
            self.st = st
            if took:
                self.res.picked += 1
                self.res.detail.update(ground_clicks=clicks, ground_at=want, looks=looks,
                                       ground_src=gc.get("world_src"))
                hz = in_hazard(want, self.hazards) if self.landing is None and want else None
                if hz:
                    self.res.detail["spot_in"] = hz
                if cleared and self.landing is not None:
                    self.res.detail.update(clear_of=cleared, shifted_m=round(math.hypot(
                        want[0] - self.landing[0], want[2] - self.landing[2]), 1))
                return True, {}
            if not st.get("armed"):
                # the spell left the hand without a cast (the game dropped it): nothing more to click
                return False, {"reason": "dropped", "why": "the spell left the hand after the ground click",
                               "state": st, "looks": looks}
            last = {"reason": "not_taken", "why": "the game did not take the ground click (T4)", "state": st}
        self.res.detail["looks"] = looks
        return False, last or {"reason": "no_spot", "why": "no ground spot to click"}


def twostep_cast(game: Game, inp: InputClient | None, caster: str, spell: str, first: str,
                 landing: list[float] | None = None, select: bool = True, should_stop=None) -> HotbarCastResult:
    """Cast a two-step `spell` from `caster`'s hotbar: `first` is the body (Revivify) or who goes (Dimension Door:
    the caster to go alone); `landing` [x, y, z] the teleport's landing (None = Revivify: the ground beside the
    body, toward the caster). Every failure after the cell click cancels, so no spell stays in hand."""
    if inp is None:
        return HotbarCastResult(False, reason="no_input", stage="slot", budget_s=BUDGET_S,
                                detail={"why": "input shim not available for the clicks"})
    run = _Run(game, inp, caster, spell, first, landing, should_stop)
    fail, st = hotbar.arm(game, inp, caster, spell, run.res, select, should_stop)
    if fail:
        return run.done(False, **fail)
    run.st = st
    if landing is not None and DD_ORDER[0] == "landing":
        return _landing_first(run)
    ok, info = run.pick_first()
    if not ok and landing is not None and info["reason"] == "not_picked" and run.st.get("armed"):
        # GUESS T2 wrong? the game may want the landing first: that order, once
        run.res.detail["order_flipped"] = True
        return _landing_first(run)
    if not ok:
        return _fail(run, "first", info)
    if hotbar._started(run.st):
        # review L5: the first pick already cast it (a one-step spell the name regex routed here): no ground click,
        # which with no spell in hand would be a plain move order (movement spent in combat)
        return run.done(True, phase=(run.st.get("cast") or {}).get("phase"), one_step=True)
    ok, info = run.click_ground(lambda before, s: hotbar._started(s))
    if not ok:
        return _fail(run, "ground", info)
    return run.done(True, phase=(run.st.get("cast") or {}).get("phase"))


ARM_WAIT_S = 1.0               # a scroll's Use -> its spell in hand


def ground_click(game: Game, inp: InputClient, caster: str, spell: str, pos: list[float],
                 should_stop=None) -> HotbarCastResult:
    """Aim the spell already in hand (a scroll read: its Use put the spell there) at the ground at `pos`, as a player
    clicks it: Cone of Cold's direction, Misty Step's landing, a sphere's centre (S066 #66/#143: those scrolls were
    clicked on the reader, the x,y,z dropped). The same cursor-checked click as Dimension Door's landing. ok = the
    cast started; every failure puts the spell back, so nothing is cast and no click lands as a move order."""
    run = _Run(game, inp, caster, spell, caster, [float(v) for v in pos[:3]], should_stop)
    run.res.detail.update(targets=1, two_step=None, ground=True)
    armed, run.st = hotbar._wait(game, caster, lambda s: bool(s.get("armed")), ARM_WAIT_S)
    if not armed:
        return run.done(False, "arm", "not_armed", "no spell in hand after the scroll's use")
    ok, info = run.click_ground(lambda before, s: hotbar._started(s))
    if not ok:
        return _fail(run, "ground", info)
    return run.done(True, phase=(run.st.get("cast") or {}).get("phase"))


def revive_ground(game: Game, inp: InputClient, reader: str, spell: str, body: str,
                  should_stop=None) -> HotbarCastResult:
    """The second pick of a revive scroll whose body was already clicked: the ground around the body, with the same
    cursor checks as the spell's hotbar route (no character under the cursor, no warning cursor), proven by the
    spell leaving the hand. S089 07:08 x3: a click on a creature left the spell waiting for its destination and the
    scroll unread. ok = the cast started; every failure puts the spell back (nothing cast, the scroll kept)."""
    run = _Run(game, inp, reader, spell, body, None, should_stop)
    run.res.detail.update(targets=1)
    armed, run.st = hotbar._wait(game, reader, lambda s: bool(s.get("armed")), ARM_WAIT_S)
    if not armed:
        return run.done(False, "arm", "not_armed", "no spell in hand after the body's click")
    ok, info = run.click_ground(lambda before, s: hotbar._started(s))
    if not ok:
        return _fail(run, "ground", info)
    return run.done(True, phase=(run.st.get("cast") or {}).get("phase"))


def _fail(run: _Run, stage: str, info: dict) -> HotbarCastResult:
    reason, why = info.pop("reason", "failed"), info.pop("why", None)
    # "dropped": the spell already left the hand; an ESCAPE now would open the game menu
    return run.done(False, stage, reason, why, cancel_it=reason != "dropped", **info)


def _landing_first(run: _Run) -> HotbarCastResult:
    ok, info = run.click_ground(_took)
    if not ok:
        return _fail(run, "ground", info)
    if not hotbar._started(run.st):
        ok, info = run.pick_first()
        if not ok:
            return _fail(run, "first", info)
        if not hotbar._started(run.st):
            ok, run.st = hotbar._wait(run.game, run.caster, hotbar._started, LAND_WAIT_S)
            if not ok:
                return _fail(run, "start", {"reason": "not_started", "why": "the cast did not start after both picks",
                                            "state": run.st})
    return run.done(True, phase=(run.st.get("cast") or {}).get("phase"))
