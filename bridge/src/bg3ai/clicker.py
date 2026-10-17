"""Click-on-entity: the generic fallback for flows the game only starts from a click.

Looting a corpse, lockpicking a door or chest, disarming a discovered trap, pickpocketing, and anything else
without an engine call. Server ops (act.loot_corpse / act.lockpick / act.disarm / act.pickpocket) do the
fairness checks, walk the actor next to the target and select it, then finish with
``{"needs_click": uuid, "expect": "container" | "roll" | "pickpocket"}``. This module does the click:

    project (client op ui.screen_pos) -> move the virtual cursor (input shim) -> wait 2 frames
    -> verify the hover (client op ui.hover_check, i.e. the engine's picking helper)
    -> click -> verify the expected screen (client op ui.state)

The hover check makes the click verifiable: we only click when the engine itself says the cursor is on
the intended entity, so a miss never interacts with the wrong thing. If the hover misses, we retry
with small pixel offsets and different aim heights.

    from bg3ai.game import Game
    from bg3ai.input import InputClient
    from bg3ai.clicker import run_click_action
    res = run_click_action(Game(), InputClient(), "act.loot_corpse", {"char": me, "corpse": body})
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .game import Game, GameError, JobResult
from .input import InputClient

# ui.state "screen" values that count as success for each expectation.
EXPECT_SCREENS: dict[str, set[str]] = {
    "container": {"container"},
    "roll": {"roll"},
    "pickpocket": {"pickpocket", "container", "trade"},
    "any": set(),  # any change of screen
}

# Pixel nudges tried around the projected point, then aim points: (aim height, metres along the entity's own
# X axis). A door's origin is its hinge, so the leaf is ~0.5 m to one side of it (live S005: 9 hover misses).
OFFSETS = [(0, 0), (0, -14), (0, 14), (-12, 0), (12, 0), (0, -30), (0, 28), (-24, -10), (24, -10)]
# S032 17:42 (Vault Door e679, 90 tries, nothing ever under the cursor) and ~10 doors/chests S017-S026: a door's
# origin is its hinge, so a wide (double/vault) door's leaf is 1-2 m along its X axis: aims at ±1.4 m too.
# S070 21:08:39 (Dragon Head Torch, 89 aims on the wall behind it): a wall-mounted item hangs above the item aims'
# 1.1 m: 1.6 / 2.1 m over the origin too (Pick.aim_point; a character's are its chest).
# Last in the list: the first view's probe takes every aim's centre anyway.
AIMS: list[tuple[str, float]] = [("auto", 0.0), ("chest", 0.0), ("low", 0.0), ("feet", 0.0),
                                 ("mid", 0.5), ("mid", -0.5), ("mid", 0.9), ("mid", -0.9),
                                 ("mid", 1.4), ("mid", -1.4), ("high", 0.0), ("top", 0.0)]

# Camera moves a player would make to look at the target (closed loop on ui.screen_pos): zoom out while the
# target sits near/over an edge, rotate (middle-drag) toward it when it is far off to a side or behind.
EDGE = 0.85          # |ndc| beyond this counts as "at the edge" (hover may land on the HUD)
CAMERA_STEPS = 6
ROTATE_FRAC = 0.18   # middle-drag length as a fraction of the window width
# Wall-clock cap on the whole hover search (both passes). 144 tries took 15-33 s in claude-run-2 S017-S024
# and never found a door that the first ~20 tries missed; a player gives up on a fiddly door in seconds.
HOVER_BUDGET_S = 8.0
OCCLUDED_PASS_S = 2.5   # each look after turning the camera past an object that takes the cursor
# A right-click opens the world context menu (WorldContextMenu.xaml) at the cursor; its entries are made by the
# engine for the hovered entity. The menu takes a few frames to appear.
MENU_WAIT_S = 2.0
# The game's CameraCenter key: the camera jumps back onto the selected character (the server op selected the
# actor next to the target). Live 2026-10-10: camera panned away (Claude at ndc y 25) -> Home -> (0.00, 0.08)
# in ~1 s. Without it the camera may still follow a companion (lockpick 12:3x: off_screen, tries=0).
CENTER_KEY = "Home"
CENTER_WAIT_S = 1.5

# The faster search (naut-r2 S001 16:54-16:55: click_on off_screen x2 at 7 s each, then 83 tries in 12.6 s). Revert
# with BG3AI_HOVER_V2=0 (or clicker.HOVER_V2 = False):
#  - each try settles on the picking helper's own cursor (hover_check `cursor` == the point moved to) instead of a
#    fixed 2-frame wait: ~2 frames a try instead of ~4 (the 83 tries were ~135 ms each, RPCs only 1.1 s of 12.6 s);
#  - the first aim's 9 points (61/77 hits in the log are within them), then every other aim's centre (5 more hits
#    are try 10 = the 2nd aim's centre), then the remaining offsets; when nearly all of the probe (the first aim's
#    ring + every aim's centre) hovered something else, the camera turns at once instead of after the 8 s budget
#    (e126 the nerve: an 8 s pass on an occluder, then the 2nd rotation found it);
#  - a rotation that carried a point behind the camera across to the other side (or from the front to behind) was
#    the wrong way: the drag direction flips and is remembered (the off_screen calls: left,left,right,left,right,left
#    with the target behind, where the |x| check never fires).
HOVER_V2 = os.environ.get("BG3AI_HOVER_V2", "1") != "0"
PROBE_OCCLUDED_FRAC = 0.75   # share of the probe's tries that hovered something else for "occluded: turn now"
PROBE_MIN_TRIES = 6
DEDUPE_PX = 3                # a point this close to one already tried is skipped (aims that project together)
_drag = {"flip": False}      # the learned middle-drag direction (v2)


# Screens that cover the world: a click can't reach an entity behind them, and camera moves don't help.
COVERING_SCREENS = {"book", "dialogue", "trade", "donate", "level_up", "learn_spells", "tadpole_tree", "game_menu",
                    "options", "save_load", "map", "rest", "reward", "character_creation", "main_menu", "loading"}


@dataclass
class ClickResult:
    ok: bool
    reason: str | None = None          # off_screen | hover_miss | no_effect | error text
    screen: str | None = None          # ui.state screen after the click
    pos: tuple[int, int] | None = None  # window coords that were clicked
    tries: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "tries": self.tries}
        for k in ("reason", "screen", "pos"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.detail:
            d.update(self.detail)
        return d


def _screen(game: Game) -> dict:
    try:
        return game.rpc("ui.state", {}, context="client") or {}
    except GameError:
        return {}


def _screen_pos(game: Game, uuid: str | None, aim: str = "auto", ox: float = 0.0, pos=None) -> dict:
    """ui.screen_pos of an entity's aim point, or of a world point `pos` (x, y, z) when given."""
    if pos is not None:
        return game.rpc("ui.screen_pos", {"pos": [float(v) for v in pos]}, context="client") or {}
    args: dict[str, Any] = {"uuid": uuid, "aim": aim}
    if ox:
        args["ox"] = ox
    return game.rpc("ui.screen_pos", args, context="client") or {}


def _centered(sp: dict) -> bool:
    nd = sp.get("ndc")
    return bool(sp.get("on_screen")) and nd is not None and abs(nd[0]) <= EDGE and abs(nd[1]) <= EDGE


def camera_move(sp: dict) -> str | None:
    """The next camera move that brings a projected point into view: "zoom_out", "rotate_left",
    "rotate_right", or None when it is already well inside the window."""
    if _centered(sp):
        return None
    nd = sp.get("ndc")
    if sp.get("behind") or nd is None or abs(nd[0]) > 1.4:
        return "rotate_left" if sp.get("side") == "left" else "rotate_right"
    return "zoom_out"


def bring_into_view(game: Game, inp: InputClient, uuid: str | None = None, steps: int = CAMERA_STEPS,
                    pos=None, out: dict | None = None) -> list[str]:
    """Move the camera like a player would until `uuid` (or the world point `pos` = (x, y, z): a ground spot) is
    well inside the window. Returns the moves made; `out` receives the last ui.screen_pos read ({"error"} if one
    failed)."""
    moves: list[str] = []
    v2 = HOVER_V2
    flip = _drag["flip"] if v2 else False   # set when a rotation made things worse: the drag direction is the other way
    prev_x: float | None = None
    prev_edge: float | None = None
    prev_behind, prev_side = False, None
    for _ in range(steps):
        try:
            sp = _screen_pos(game, uuid, pos=pos)
        except GameError as e:
            if out is not None:
                out.clear()
                out["error"] = str(e)
            break
        if out is not None:
            out.clear()
            out.update(sp)
        mv = camera_move(sp)
        nd = sp.get("ndc")
        behind, side = bool(sp.get("behind")), sp.get("side")
        rotated = bool(moves) and moves[-1].startswith("rotate")
        if rotated and nd is not None and prev_x is not None and abs(nd[0]) > abs(prev_x) + 0.05:
            flip = not flip
            if v2:
                _drag["flip"] = flip
        elif v2 and rotated and behind and prev_side is not None and \
                ((prev_behind and side != prev_side) or (not prev_behind and side == prev_side)):
            # behind: crossed the back to the other side, or turned from the front to behind on the same side
            flip = not flip
            _drag["flip"] = flip
        prev_behind, prev_side = behind, side
        edge = max(abs(nd[0]), abs(nd[1])) if nd is not None else None
        if moves and moves[-1] == "zoom_out" and mv == "zoom_out" and edge is not None and prev_edge is not None \
                and edge > prev_edge - 0.02:
            break  # zoomed out as far as it goes
        prev_x = nd[0] if nd is not None else None
        prev_edge = edge
        if mv is None:
            break
        w, h = inp.window_size()
        cx, cy = w // 2, h // 2
        if mv == "zoom_out":
            inp.wheel(-2, cx, cy)
        else:
            right = (mv == "rotate_right") != flip
            dx = int(w * ROTATE_FRAC) * (-1 if right else 1)
            inp.drag(cx, cy, cx + dx, cy, button="M", steps=10)
            inp.move(cx, cy)
        moves.append(mv)
        settle_camera(game, inp, uuid, pos)   # the camera eases into place
    else:
        if out is not None and moves:
            try:   # the steps ran out: the read after the last move
                out.clear()
                out.update(_screen_pos(game, uuid, pos=pos))
            except GameError as e:
                out["error"] = str(e)
    return moves


def view_point(game: Game, inp: InputClient, pos, steps: int = CAMERA_STEPS) -> tuple[dict, list[str]]:
    """bring_into_view for a world point: (the last ui.screen_pos of it, the moves made)."""
    out: dict = {}
    moves = bring_into_view(game, inp, None, steps=steps, pos=pos, out=out)
    return out, moves


SETTLE_MIN_FRAMES = 3     # v2: a camera move is settled when the target's projection stops changing
SETTLE_POLL_FRAMES = 2
SETTLE_EPS = 0.004        # ndc between two polls
SETTLE_MAX_S = 0.9        # never longer than the old fixed wait (12 frames + 0.15 s) by much


def _ndc_key(sp: dict) -> tuple | None:
    nd = sp.get("ndc")
    if nd is not None:
        return ("in", float(nd[0]), float(nd[1]))
    return ("behind", sp.get("side")) if sp.get("behind") else None


def settle_camera(game: Game, inp: InputClient, uuid: str | None, pos=None) -> None:
    """After a camera move: the old fixed ease wait (12 frames + 0.15 s), or with HOVER_V2 until the target's
    projection is still between two polls (a point behind the camera: the same side twice)."""
    if not HOVER_V2:
        inp.wait_frames(12)
        time.sleep(0.15)
        return
    end = time.monotonic() + SETTLE_MAX_S
    inp.wait_frames(SETTLE_MIN_FRAMES)
    prev = None
    while time.monotonic() < end:
        try:
            k = _ndc_key(_screen_pos(game, uuid, pos=pos))
        except GameError:
            return
        if prev is not None and k is not None and k[0] == prev[0] and \
                (k[0] == "behind" and k[1] == prev[1] or
                 k[0] == "in" and abs(k[1] - prev[1]) + abs(k[2] - prev[2]) < SETTLE_EPS):
            return
        prev = k
        inp.wait_frames(SETTLE_POLL_FRAMES)


def center_on_actor(game: Game, inp: InputClient, uuid: str) -> bool:
    """When `uuid` is not well inside the window, press the game's center-camera key (the camera returns to the
    selected character, who stands next to the target) and wait for it to settle. True if the key was pressed."""
    try:
        if _centered(_screen_pos(game, uuid)):
            return False
    except GameError:
        return False
    inp.key(CENTER_KEY)
    end = time.monotonic() + CENTER_WAIT_S
    prev = None
    prev_k = None
    while time.monotonic() < end:
        inp.wait_frames(6)
        try:
            sp = _screen_pos(game, uuid)
        except GameError:
            break
        nd = sp.get("ndc")
        if nd is not None and prev is not None and abs(nd[0] - prev[0]) + abs(nd[1] - prev[1]) < 0.01:
            break   # the camera stopped easing
        k = _ndc_key(sp)
        if HOVER_V2 and k is not None and k[0] == "behind" and k == prev_k:
            break   # still behind the camera, same side: bring_into_view turns it (v1 waited the whole 1.5 s)
        prev, prev_k = nd, k
    return True


def hover_point(game: Game, inp: InputClient, uuid: str, frames: int = 2, deadline: float | None = None,
                seen: dict[str, int] | None = None) -> tuple[tuple[int, int] | None, int, dict]:
    """Move the virtual cursor until the engine's picking helper reports `uuid` under it.

    Stops at `deadline` (time.monotonic()). `seen` counts the other entities the helper reported instead.
    Returns ((x, y) or None, tries, last hover_check)."""
    w, h = inp.window_size()
    tries = 0
    last: dict = {}
    done_aims: set = set()
    # why a no-id object under the cursor wasn't the target, from whichever aim saw one: the final aim usually hovers
    # nothing, so S063 10:57:47 (door, 51 aims, hovered_instead=[(an object with no id)]) reported no no_id_objects
    nouuid: dict = {}

    def sticky(d: dict) -> dict:
        return {**nouuid, **d} if nouuid and not d.get("nouuid") else d

    for aim, ox in AIMS:
        if deadline is not None and time.monotonic() >= deadline:
            last = {**last, "timed_out": True}
            break
        try:
            sp = _screen_pos(game, uuid, aim, ox)
        except GameError as e:
            last = {"error": str(e)}
            continue
        key = (sp.get("aim") or aim, ox)   # an item's "auto" is "low": the same 9 points twice cost ~1 s
        if key in done_aims:
            continue
        done_aims.add(key)
        if not sp.get("on_screen"):
            last = {"off_screen": True, **sp}
            continue
        nx, ny = sp["ndc"]
        bx, by = (nx * 0.5 + 0.5) * w, (ny * 0.5 + 0.5) * h
        for dx, dy in OFFSETS:
            x, y = int(round(bx + dx)), int(round(by + dy))
            if not (0 <= x < w and 0 <= y < h):
                continue
            tries += 1
            inp.move(x, y)              # blocks until the event reached the game
            inp.wait_frames(frames)     # let the picking helper update
            try:
                last = game.rpc("ui.hover_check", {"uuid": uuid}, context="client")
            except GameError as e:
                last = {"error": str(e)}
                continue
            if last.get("hovered"):
                return (x, y), tries, last
            for k in ("nouuid", "nouuid_why"):
                if last.get(k):
                    nouuid[k] = last[k]
            if seen is not None and last.get("hover"):
                seen[last["hover"]] = seen.get(last["hover"], 0) + 1
            elif seen is not None and last.get("hover_nouuid"):
                seen["(an object with no id)"] = seen.get("(an object with no id)", 0) + 1
            if deadline is not None and time.monotonic() >= deadline:
                return None, tries, sticky({**last, "timed_out": True})
    return None, tries, sticky(last)


def _cursor_scale(inp: InputClient) -> tuple[float, float] | None:
    """drawable / window size: the picking helper's cursor may be in either space."""
    try:
        st = inp.status()
        (ww, wh), (dw, dh) = st["size"], st["drawable"]
        return (dw / ww, dh / wh) if ww and wh and dw and dh else None
    except Exception:  # noqa: BLE001 - a fake or an older shim: window coords only
        return None


def _at(hc: dict, x: int, y: int, scale: tuple[float, float] | None) -> bool | None:
    """Is the picking helper's cursor at (x, y)? None when it reports no cursor."""
    c = hc.get("cursor") if isinstance(hc, dict) else None
    if not (isinstance(c, (list, tuple)) and len(c) >= 2):
        return None
    try:
        cx, cy = float(c[0]), float(c[1])
    except (TypeError, ValueError):
        return None
    if abs(cx - x) <= 1.5 and abs(cy - y) <= 1.5:
        return True
    return bool(scale) and abs(cx - x * scale[0]) <= 2 and abs(cy - y * scale[1]) <= 2


def hover_search(game: Game, inp: InputClient, uuid: str, frames: int = 2, deadline: float | None = None,
                 seen: dict[str, int] | None = None, early: bool = True,
                 info: dict | None = None, centres: bool = True) -> tuple[tuple[int, int] | None, int, dict]:
    """hover_point's faster order and settle (HOVER_V2): the first aim's 9 points, every other aim's centre, then
    the remaining offsets; each try reads hover_check as soon as the helper's cursor is at the point (at most
    `frames` frames). With `early`, stops after the first aim's points and every aim's centre when nearly all of
    them hovered something else (last "occluded": True: the caller turns the camera). `info` collects {"settle": "cursor"|"frames"} across passes."""
    w, h = inp.window_size()
    info = info if info is not None else {}
    scale = info.get("scale", _cursor_scale(inp))
    info["scale"] = scale
    tries, other = 0, 0
    last: dict = {}
    nouuid: dict = {}
    tried: list[tuple[int, int]] = []
    proj: list[tuple[float, float] | None] = []   # per AIMS entry: base point, None = off screen/duplicate aim
    keys: set = set()

    def sticky(d: dict) -> dict:
        return {**nouuid, **d} if nouuid and not d.get("nouuid") else d

    def project(i: int) -> tuple[float, float] | None:
        nonlocal last
        while len(proj) <= i:
            aim, ox = AIMS[len(proj)]
            try:
                sp = _screen_pos(game, uuid, aim, ox)
            except GameError as e:
                last = {"error": str(e)}
                proj.append(None)
                continue
            key = (sp.get("aim") or aim, ox)
            if key in keys or not sp.get("on_screen"):
                if not sp.get("on_screen") and key not in keys:
                    last = {"off_screen": True, **sp}
                keys.add(key)
                proj.append(None)
                continue
            keys.add(key)
            nx, ny = sp["ndc"]
            proj.append(((nx * 0.5 + 0.5) * w, (ny * 0.5 + 0.5) * h))
        return proj[i]

    def order():
        rest = [(i, o) for i in range(1, len(AIMS)) for o in OFFSETS[1:]]
        yield from ((0, o) for o in OFFSETS)
        # live window #1 (10-16, S050 Foundry door e7): a door's "auto" aim is its hinge, so all 9 first points
        # hovered the frame and the camera turned twice (19 tries 1.6 s) before trying the leaf. The first view's
        # probe (`centres`) also takes every other aim's centre (+<=9 tries) before it may call the target
        # occluded; a turned view's quick probe stays the first aim's ring (the centres missed already).
        if centres:
            yield from ((i, OFFSETS[0]) for i in range(1, len(AIMS)))
        yield "probe_done"
        if not centres:
            yield from ((i, OFFSETS[0]) for i in range(1, len(AIMS)))
        yield from rest

    def check(x: int, y: int) -> dict:
        mode = info.get("settle")
        if mode != "cursor":
            inp.wait_frames(frames)
            hc = game.rpc("ui.hover_check", {"uuid": uuid}, context="client") or {}
            if mode is None:   # first try: the full wait, then learn whether the helper reports the cursor here
                info["settle"] = "cursor" if _at(hc, x, y, scale) else "frames"
            return hc
        hc = game.rpc("ui.hover_check", {"uuid": uuid}, context="client") or {}
        waited = 0
        while not _at(hc, x, y, scale) and waited < frames:
            inp.wait_frames(1)
            waited += 1
            hc = game.rpc("ui.hover_check", {"uuid": uuid}, context="client") or {}
        if hc.get("hovered") and waited < frames:
            # the hit decides the click: one more frame and a second look (a pick one frame behind the cursor)
            inp.wait_frames(1)
            hc = game.rpc("ui.hover_check", {"uuid": uuid}, context="client") or {}
        return hc

    for step in order():
        if step == "probe_done":
            if early and tries >= PROBE_MIN_TRIES and other >= PROBE_OCCLUDED_FRAC * tries:
                return None, tries, sticky({**last, "occluded": True})
            continue
        if deadline is not None and time.monotonic() >= deadline:
            return None, tries, sticky({**last, "timed_out": True})
        i, (dx, dy) = step
        base = project(i)
        if base is None:
            continue
        x, y = int(round(base[0] + dx)), int(round(base[1] + dy))
        if not (0 <= x < w and 0 <= y < h) or any(abs(x - a) <= DEDUPE_PX and abs(y - b) <= DEDUPE_PX
                                                    for a, b in tried):
            continue
        tried.append((x, y))
        tries += 1
        inp.move(x, y)
        try:
            last = check(x, y)
        except GameError as e:
            last = {"error": str(e)}
            continue
        if last.get("hovered"):
            return (x, y), tries, last
        for k in ("nouuid", "nouuid_why"):
            if last.get(k):
                nouuid[k] = last[k]
        if last.get("hover") or last.get("hover_nouuid"):
            other += 1
        if seen is not None and last.get("hover"):
            seen[last["hover"]] = seen.get(last["hover"], 0) + 1
        elif seen is not None and last.get("hover_nouuid"):
            seen["(an object with no id)"] = seen.get("(an object with no id)", 0) + 1
    return None, tries, sticky(last)


MENU_SCAN_PX = (6, 320, 6)   # cursor offsets below the right-click point that are tried, in window px
MENU_SCAN_X = (60, 140)      # and to its right (the menu opens at the cursor, rows run down from it)


def _menu(game: Game, item: str) -> dict:
    return game.rpc("ui.context_menu", {"item": item}, context="client") or {}


def _press_menu(game: Game, inp: InputClient, item: str, at: tuple[int, int]) -> str | None:
    """Pick `item` in the context menu a right-click at `at` opened: move the cursor down the menu until that row
    is under it (its IsMouseOver), then click it, as a player does (the row's own command does nothing, live).
    None when clicked, else why not; a menu left open is closed again (ESCAPE)."""
    deadline = time.monotonic() + MENU_WAIT_S
    why = "the context menu did not open"
    while True:
        try:
            _menu(game, item)
            why = None
            break
        except GameError as e:
            if "not open" not in str(e):
                why = str(e)
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if why is None:
        w, h = inp.window_size()
        why = f"could not point at '{item}' in the context menu"
        for ox in MENU_SCAN_X:
            for oy in range(*MENU_SCAN_PX):
                x, y = at[0] + ox, at[1] + oy
                if not (0 <= x < w and 0 <= y < h):
                    continue
                inp.move(x, y)
                inp.wait_frames(2)
                try:
                    m = _menu(game, item)
                except GameError as e:
                    why = str(e)
                    break
                if m.get("over"):
                    inp.click(x, y, button="L")
                    return None
            else:
                continue
            break
    try:
        if (game.rpc("ui.context_menu", {}, context="client") or {}).get("open"):
            inp.key("ESCAPE")
    except GameError:
        pass
    return why


ROLL_WALK_EXTRA_S = 6.0   # extra wait for a lockpick/disarm roll while the game still walks the actor up
WALK_EPS_M = 0.15


class _WalkWatch:
    """Party positions (nav.activity) between polls: is anyone still moving after the click, and how far they went."""

    def __init__(self, game: Game) -> None:
        self.game, self.walked_m = game, 0.0
        self.last = self._pos()

    def _pos(self) -> dict | None:
        try:
            a = self.game.rpc("nav.activity", {})
        except Exception:  # noqa: BLE001 - an older mod, or a fake: no walk info
            return None
        return a.get("pos") if isinstance(a, dict) and isinstance(a.get("pos"), dict) else None

    def moving(self) -> bool:
        time.sleep(0.25)
        now = self._pos()
        if now is None or self.last is None:
            return False
        step = max((sum((a - b) ** 2 for a, b in zip(now[u], self.last[u])) ** 0.5
                    for u in now if u in self.last), default=0.0)
        self.last = now
        self.walked_m += step
        return step > WALK_EPS_M


def click_entity(game: Game, inp: InputClient, uuid: str, button: str = "L", expect: str | None = None,
                 verify_timeout: float = 4.0, menu: str | None = None) -> ClickResult:
    """Hover-verify then click `uuid`; with `menu`, right-click and pick that context-menu entry (a player's
    right-click -> Lockpick). If `expect` is given, wait until ui.state shows that screen."""
    before = _screen(game).get("screen")
    if before in COVERING_SCREENS:
        # S031 17:13: click_on under an open book page turned the camera 6x and said off_screen.
        return ClickResult(False, "covered", before, None, 0, {})
    centered = center_on_actor(game, inp, uuid)
    moves = bring_into_view(game, inp, uuid)
    seen: dict[str, int] = {}
    passes_diag: dict = {}   # no-id details from an earlier pass (a later pass may never hover the object)

    def keep_diag(d: dict) -> None:
        passes_diag.update({k: d[k] for k in ("nouuid", "nouuid_why") if d.get(k)})
    if HOVER_V2:
        pos, tries, last = _search_v2(game, inp, uuid, moves, seen, keep_diag)
    else:
        pos, tries, last = _search_v1(game, inp, uuid, moves, seen, keep_diag)
    return _finish_click(game, inp, uuid, button, expect, verify_timeout, menu, before, centered, moves, seen,
                         passes_diag, pos, tries, last)


def _turn(game: Game, inp: InputClient, uuid: str, mult: int) -> None:
    """One occluded-pass camera turn: a middle-drag of `mult` x ROTATE_FRAC of the width, then let it ease."""
    w, h = inp.window_size()
    cx, cy = w // 2, h // 2
    inp.drag(cx, cy, cx - int(w * ROTATE_FRAC) * mult, cy, button="M", steps=10)
    settle_camera(game, inp, uuid)


def _search_v2(game: Game, inp: InputClient, uuid: str, moves: list[str], seen: dict[str, int],
               keep_diag) -> tuple[tuple[int, int] | None, int, dict]:
    """HOVER_V2: probe the view; occluded -> turn right, then left, each probed; the last view (or an unoccluded
    one) gets the full search. Whole search within HOVER_BUDGET_S."""
    deadline = time.monotonic() + HOVER_BUDGET_S
    info: dict = {}
    pos, tries, last = hover_search(game, inp, uuid, deadline=deadline, seen=seen, info=info)
    keep_diag(last)
    if pos is None and not moves and not last.get("timed_out") and not last.get("occluded"):
        w, h = inp.window_size()   # every aim missed with nothing in front: one more look from a wider angle
        inp.wheel(-2, w // 2, h // 2)
        settle_camera(game, inp, uuid)
        moves.append("zoom_out")
        pos, t2, last = hover_search(game, inp, uuid, deadline=deadline, seen=seen, info=info)
        tries += t2
        keep_diag(last)
    if pos is None and seen:
        turns = ((1, "rotate_occluded_right"), (-2, "rotate_occluded_left"))
        for n, (mult, name) in enumerate(turns):
            if time.monotonic() >= deadline:
                break
            _turn(game, inp, uuid, mult)
            moves.append(name)
            final = n == len(turns) - 1
            pos, t2, last = hover_search(game, inp, uuid, deadline=deadline if final else
                                         min(deadline, time.monotonic() + OCCLUDED_PASS_S), seen=seen,
                                         early=not final, info=info, centres=final)
            tries += t2
            keep_diag(last)
            if pos is not None:
                break
    if info.get("settle"):
        last = {**last, "settle": info["settle"]}
    return pos, tries, last


def _search_v1(game: Game, inp: InputClient, uuid: str, moves: list[str], seen: dict[str, int],
               keep_diag) -> tuple[tuple[int, int] | None, int, dict]:
    deadline = time.monotonic() + HOVER_BUDGET_S
    pos, tries, last = hover_point(game, inp, uuid, deadline=deadline, seen=seen)
    keep_diag(last)
    if pos is None and not moves and not last.get("timed_out"):
        # Every aim missed although the target looked in view: one more look from a wider angle.
        w, h = inp.window_size()
        inp.wheel(-2, w // 2, h // 2)
        inp.wait_frames(12)
        time.sleep(0.15)
        moves.append("zoom_out")
        pos, t2, last = hover_point(game, inp, uuid, deadline=deadline, seen=seen)
        tries += t2
        keep_diag(last)
    if pos is None and seen:
        # Something in front keeps taking the cursor (S053 05:45:31 Metal Crate: 31 aims all on an object 3.2 m from
        # it, no_id_objects m=3.2): turn the camera as a player does, one step each way, and look again.
        w, h = inp.window_size()
        cx, cy = w // 2, h // 2
        for mult, name in ((1, "rotate_occluded_right"), (-2, "rotate_occluded_left")):
            inp.drag(cx, cy, cx - int(w * ROTATE_FRAC) * mult, cy, button="M", steps=10)
            inp.wait_frames(12)
            time.sleep(0.15)
            moves.append(name)
            pos, t2, last = hover_point(game, inp, uuid, deadline=time.monotonic() + OCCLUDED_PASS_S, seen=seen)
            tries += t2
            keep_diag(last)
            if pos is not None:
                break
    return pos, tries, last


def _finish_click(game: Game, inp: InputClient, uuid: str, button: str, expect: str | None, verify_timeout: float,
                  menu: str | None, before: str | None, centered: bool, moves: list[str], seen: dict[str, int],
                  passes_diag: dict, pos: tuple[int, int] | None, tries: int, last: dict) -> ClickResult:
    moves = (["center"] if centered else []) + moves
    extra: dict[str, Any] = {"camera": moves} if moves else {}
    if tries > 1 and last.get("settle"):
        extra["settle"] = last["settle"]   # live check: "cursor" = the per-try settle on the helper's cursor works
    if pos is None:
        # off_screen only when no aim point was ever in the window (tries counts the in-window points)
        reason = "off_screen" if tries == 0 and last.get("off_screen") else "hover_miss"
        if seen:
            # what the cursor kept landing on (an object in front of the target, or a bigger hit box)
            extra["hovered_instead"] = sorted(seen, key=lambda u: -seen[u])[:3]
        elif last.get("hover"):
            extra["hover"] = last["hover"]
        if last.get("timed_out"):
            extra["gave_up_after_s"] = HOVER_BUDGET_S
        diag = {**passes_diag, **{k: last[k] for k in ("nouuid", "nouuid_why") if last.get(k)}}
        if diag.get("nouuid"):
            # why the no-id object under the cursor wasn't taken for the target (Pick.same_entity: m = origin distance)
            extra["no_id_objects"] = diag["nouuid"]
        if diag.get("nouuid_why"):
            extra["no_id_why"] = diag["nouuid_why"]
        return ClickResult(False, reason, before, None, tries, extra)
    if last.get("via") in ("handle", "origin"):
        extra["hover_via"] = last["via"]   # a no-id object matched the target (S050/S051 doors)
    inp.click(pos[0], pos[1], button="R" if menu else button)
    if menu:
        why = _press_menu(game, inp, menu, pos)
        if why:
            return ClickResult(False, "no_menu_entry", before, pos, tries, dict(extra, menu=why))
        extra["menu"] = menu
    if not expect:
        return ClickResult(True, None, None, pos, tries, extra)
    wanted = EXPECT_SCREENS.get(expect, {expect})
    t_click = time.monotonic()
    deadline = t_click + verify_timeout
    st: dict = {}
    walk = _WalkWatch(game) if expect == "roll" else None
    while True:
        st = _screen(game)
        scr = st.get("screen")
        if (wanted and scr in wanted) or (not wanted and scr != before):
            if walk and walk.walked_m > 0.3:
                extra["walked_m"] = round(walk.walked_m, 1)
            return ClickResult(True, None, scr, pos, tries, extra)
        if wanted and scr == "dialogue" and before != "dialogue":
            # Live window #2 (10-16 19:51, S050 Foundry door): the Lockpick row was pressed and a Steel Watcher
            # answered with the crime scene (then combat) in place of the roll; this read "nothing happened" after
            # 6.4 s. The click took: say what the game did instead, at once.
            if walk and walk.walked_m > 0.3:
                extra["walked_m"] = round(walk.walked_m, 1)
            extra["instead"] = "dialogue"
            return ClickResult(False, "interrupted", scr, pos, tries, extra)
        now = time.monotonic()
        if now >= deadline:
            # S047 02:57:54: the Lockpick row made the game walk the actor ~1 m to its own lockpick spot first; the
            # fixed 4 s ran out ("nothing happened"). While the party still moves, the roll may yet come.
            if walk is None or not walk.moving() or now - t_click >= verify_timeout + ROLL_WALK_EXTRA_S:
                break
        time.sleep(0.05)
    if walk and walk.walked_m > 0.3:
        extra["walked_m"] = round(walk.walked_m, 1)
    if expect == "roll":
        # A key in the party opens the lock without a roll; a trap kit may
        # resolve instantly. Accept the visible state change instead: the one this click was for. S031 17:10:
        # a trapped bookcase (never locked) read "unlocked" = done 3x while its trap stayed armed.
        try:
            ts = game.rpc("act.target_state", {"uuid": uuid})
        except GameError:
            ts = {}
        trap = (menu or "").lower().startswith("disarm") or (menu is None and "trap_armed" in ts)
        if trap and ts.get("trap_armed") is False:
            return ClickResult(True, None, "disarmed", pos, tries, extra)
        if not trap and ts.get("locked") is False:
            return ClickResult(True, None, "unlocked", pos, tries, extra)
        if trap and ts.get("trap_armed"):
            extra["trap"] = "still armed"
    if st.get("notifications"):
        extra["notifications"] = st["notifications"]
    return ClickResult(False, "no_effect", st.get("screen"), pos, tries, extra)


# Revivify's second click: the ground this far from the body toward the reader (the body is a character, and the
# spot's TargetConditions are "not Character()"); a cursor still on the body tries farther out.
SPOT_DISTS = (1.2, 1.8, 2.4)


def click_spot(game: Game, inp: InputClient, uuid: str, toward: str, button: str = "L") -> ClickResult:
    """Click the ground beside `uuid`, toward `toward` (a two-step cast's second target). Never clicks while the
    engine's picking helper has `uuid` itself under the cursor."""
    tries = 0
    for d in SPOT_DISTS:
        tries += 1
        try:
            sp = game.rpc("ui.screen_pos", {"uuid": uuid, "aim": "feet", "toward": toward, "dist": d},
                          context="client") or {}
        except GameError as e:
            return ClickResult(False, reason=str(e), tries=tries)
        if not sp.get("on_screen") or sp.get("x") is None:
            return ClickResult(False, reason="off_screen", tries=tries)
        x, y = int(sp["x"]), int(sp["y"])
        inp.move(x, y)
        inp.wait_frames(2)
        try:
            hc = game.rpc("ui.hover_check", {"uuid": uuid}, context="client") or {}
        except GameError:
            hc = {}
        if hc.get("hovered"):
            continue
        inp.click(x, y, button=button)
        return ClickResult(True, pos=(x, y), tries=tries, detail={"dist_m": d})
    return ClickResult(False, reason="hover_miss", tries=tries)


def finish_needs_click(game: Game, inp: InputClient, result: dict | None) -> dict | None:
    """If a server job result asks for a click, perform it and merge the outcome into the result."""
    if not isinstance(result, dict) or not result.get("needs_click"):
        return result
    cr = click_entity(game, inp, result["needs_click"], result.get("button", "L"), result.get("expect"),
                      menu=result.get("menu"))
    out = {k: v for k, v in result.items() if k not in ("needs_click", "button", "menu")}
    out["click"] = cr.as_dict()
    if cr.ok:
        out["opened"] = cr.screen
    return out


# Click flows that only work with a tool in the actor's own inventory. Without it the game's click just "uses"
# the door/chest (tries to open it) and nothing else happens: live S011/S013 spent ~16 lockpick attempts,
# right-click context menus and screenshots on locked doors while nobody in the party carried Thieves' Tools.
TOOL_NEEDED: dict[str, tuple[str, str, str]] = {
    # op: (item name, name fragment to match, what to do instead)
    "act.lockpick": ("Thieves' Tools", "thieves", "use its key, force it (attack the lock or door; "
                     "a Sturdy one shrugs off weak hits), cast Knock, find another way in, or buy Thieves' "
                     "Tools from a trader"),
    "act.disarm": ("Trap Disarm Toolkit", "trap disarm", "avoid the trap, trigger it from range, "
                   "or buy a Trap Disarm Toolkit from a trader"),
}


def tool_owners(game: Game, op: str) -> list[str] | None:
    """Party members carrying the tool op needs (None: no tool needed, or the inventory can't be read)."""
    need = TOOL_NEEDED.get(op)
    if not need:
        return None
    try:
        inv = game.rpc("state.inventory", {"expand": True})
    except Exception:
        return None
    if not isinstance(inv, dict):
        return None
    owners: list[str] = []
    for it in inv.get("items") or []:
        if need[1] in str(it.get("name") or "").lower() and it.get("owner") and it["owner"] not in owners:
            owners.append(it["owner"])
    return owners


def tool_check(game: Game, op: str, char: str | None) -> str | None:
    """None when the actor carries the tool op needs (or the op needs none, or the check can't be made);
    otherwise why not, naming the party members who do carry one. Party inventory is on screen anyway."""
    need = TOOL_NEEDED.get(op)
    if not need or not char:
        return None
    name, frag, instead = need
    owners = tool_owners(game, op)
    if owners is None or char in owners:
        return None
    names = {}
    try:
        st = game.rpc("state.status", {})
        names = {m.get("uuid"): m.get("name") for m in (st or {}).get("party") or []}
    except Exception:
        pass
    me = names.get(char) or "this character"
    if owners:
        who = [str(names.get(u) or u) for u in owners]
        return f"{me} carries no {name}; {', '.join(who)} does: run it again with who={who[0]}"
    return f"nobody in the party carries {name}: {instead}"


def _with_holder(jr: JobResult, holder: str | None) -> JobResult:
    """Name the tool holder who stood in for the asked character (result tool_holder=...)."""
    if holder:
        jr.result = dict(jr.result, tool_holder=holder) if isinstance(jr.result, dict) else {"tool_holder": holder}
    return jr


def run_click_action(game: Game, inp: InputClient | None, op: str, args: dict, wait_s: float = 40.0) -> JobResult:
    """Run a click-on-entity server op end to end (approach, click, verify).

    The job's status is downgraded to "failed" if the click could not be completed."""
    why = tool_check(game, op, args.get("char"))
    holder = None
    if why:
        # S024/S026: "Karlach carries no Thieves' Tools; Claude does: run it again with who=Claude" cost a turn
        # each time. A player just has the one with the tools do it (like a key holder opening a door).
        holder = next(iter(tool_owners(game, op) or []), None)
        if not holder:
            return JobResult("failed", error=why)
        args = dict(args, char=holder)
    jr = _with_holder(game.run_job(op, args, wait_s=wait_s), holder)
    if jr.status != "done" or not isinstance(jr.result, dict) or not jr.result.get("needs_click"):
        return jr
    if inp is None:
        jr.status, jr.error = "failed", "input shim not available for the click"
        return jr
    merged = finish_needs_click(game, inp, jr.result)
    jr.result = merged
    if not merged.get("click", {}).get("ok"):
        jr.status = "failed"
        reason = merged.get("click", {}).get("reason", "click failed")
        jr.error = {"covered": f"the {merged.get('click', {}).get('screen')} screen covers the world (press_key ESCAPE first)",
                    "off_screen": "the target is not on screen",
                    "hover_miss": "could not point at the target",
                    "no_effect": "nothing happened",
                    "interrupted": ("a conversation started instead (someone saw it: lockpicking, disarming "
                                    "or picking a pocket in sight of its owner or a guard is a crime): answer it"),
                    "no_menu_entry": "the right-click menu had no such entry"}.get(reason, reason)
        if merged.get("click", {}).get("trap") == "still armed":
            jr.error = ("the trap is still armed after Disarm (no roll and no change within 4 s; opening or using it now sets it "
                        "off): try disarm again with whoever has the best Sleight of Hand, or leave it")
    return jr


# ---------------------------------------------------------------------------
# Long dialogue answer lists: the rows below (or above) the visible window
# ---------------------------------------------------------------------------

# Where the answer list sits: tried until ui.answer_list says the cursor is over it (live 13:4x at 1920x1080:
# (0.5, 0.72) on the first try). The last hit is tried first.
LIST_FX = (0.5, 0.42, 0.58)
LIST_FY = tuple(0.62 + 0.025 * i for i in range(14))
WHEEL_STEP = 3          # notches per scroll step (live: 1 notch = ~48 px, a row is 84 px)
SCROLL_STEPS = 8
_list_hit: dict = {"at": None}


def _answer_list(game: Game) -> dict:
    try:
        r = game.rpc("ui.answer_list", {}, context="client")
    except GameError:
        return {}
    return r if isinstance(r, dict) else {}


def _over_list(game: Game, inp: InputClient) -> tuple[int, int] | None:
    w, h = inp.window_size()
    pts = [(int(w * fx), int(h * fy)) for fy in LIST_FY for fx in LIST_FX]
    if _list_hit["at"]:
        pts.insert(0, _list_hit["at"])
    for x, y in pts:
        inp.move(x, y)
        inp.wait_frames(2)
        if _answer_list(game).get("over"):
            _list_hit["at"] = (x, y)
            return x, y
    return None


def read_answer_list(game: Game, inp: InputClient, read_options) -> tuple[dict, str | None]:
    """Scroll a dialogue's answer list with the mouse wheel, as a player does, and collect every row's text.
    read_options() -> the dialogue's options now ([{i, text, ...}], i = answer number). Returns ({i: option},
    None) or (what was read, why it stopped short). The list is wheeled back to the top afterwards."""
    got: dict = {o["i"]: o for o in read_options() if isinstance(o.get("i"), int)}
    info = _answer_list(game)
    total = info.get("total")
    if not total:
        return got, "the answer list's size could not be read"
    at = _over_list(game, inp)
    if at is None:
        return got, "could not point at the answer list"
    why = None
    for _ in range(SCROLL_STEPS):
        if len(got) >= total:
            break
        inp.wheel(-WHEEL_STEP, at[0], at[1])
        inp.wait_frames(6)
        time.sleep(0.1)
        for o in read_options():
            if isinstance(o.get("i"), int):
                got.setdefault(o["i"], o)
        info = _answer_list(game)
        if info.get("scrollable") is not None and info.get("offset", 0) >= info["scrollable"] - 0.5 \
                and len(got) < total:
            why = f"read {len(got)} of {total} answers at the bottom of the list"
            break
    else:
        if len(got) < total:
            why = f"read {len(got)} of {total} answers"
    inp.wheel(WHEEL_STEP * SCROLL_STEPS, at[0], at[1])  # back to the top, where the game opened it
    inp.wait_frames(2)
    return got, why


def answer_key(text) -> str:
    """An answer's text for comparisons: a leading [TAG], case, punctuation and spacing ignored."""
    body = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(text or ""))
    return re.sub(r"[\W_]+", " ", body.lower()).strip()


def scroll_to_answer(game: Game, inp: InputClient, read_options, text: str, up: bool = False) -> tuple[int | None, str | None]:
    """Wheel a dialogue's answer list until the row reading `text` is rendered (audit UI-4: a row below the fold
    is pressed by its own button, never by guessing its answer VM). Returns (its number now, None) or
    (None, why). The list is left where the row shows: choosing it moves the dialogue on."""
    want = answer_key(text)

    def find() -> int | None:
        for o in read_options():
            if isinstance(o.get("i"), int) and answer_key(o.get("text")) == want:
                return o["i"]
        return None

    i = find()
    if i is not None:
        return i, None
    at = _over_list(game, inp)
    if at is None:
        return None, "could not point at the answer list"
    step = WHEEL_STEP if up else -WHEEL_STEP
    for _ in range(SCROLL_STEPS):
        inp.wheel(step, at[0], at[1])
        inp.wait_frames(6)
        time.sleep(0.1)
        i = find()
        if i is not None:
            return i, None
    return None, f"no row reads '{text}' after {SCROLL_STEPS} wheel steps"
