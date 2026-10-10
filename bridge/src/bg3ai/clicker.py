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
AIMS: list[tuple[str, float]] = [("auto", 0.0), ("chest", 0.0), ("low", 0.0), ("feet", 0.0),
                                 ("mid", 0.5), ("mid", -0.5), ("mid", 0.9), ("mid", -0.9)]

# Camera moves a player would make to look at the target (closed loop on ui.screen_pos): zoom out while the
# target sits near/over an edge, rotate (middle-drag) toward it when it is far off to a side or behind.
EDGE = 0.85          # |ndc| beyond this counts as "at the edge" (hover may land on the HUD)
CAMERA_STEPS = 6
ROTATE_FRAC = 0.18   # middle-drag length as a fraction of the window width
# Wall-clock cap on the whole hover search (both passes). 144 tries took 15-33 s in claude-run-2 S017-S024
# and never found a door that the first ~20 tries missed; a player gives up on a fiddly door in seconds.
HOVER_BUDGET_S = 8.0


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


def _screen_pos(game: Game, uuid: str, aim: str = "auto", ox: float = 0.0) -> dict:
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


def bring_into_view(game: Game, inp: InputClient, uuid: str, steps: int = CAMERA_STEPS) -> list[str]:
    """Move the camera like a player would until `uuid` is well inside the window. Returns the moves made."""
    moves: list[str] = []
    flip = False          # set when a rotation made things worse: the drag direction is the other way
    prev_x: float | None = None
    prev_edge: float | None = None
    for _ in range(steps):
        try:
            sp = _screen_pos(game, uuid)
        except GameError:
            break
        mv = camera_move(sp)
        nd = sp.get("ndc")
        if moves and moves[-1].startswith("rotate") and nd is not None and prev_x is not None \
                and abs(nd[0]) > abs(prev_x) + 0.05:
            flip = not flip
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
        inp.wait_frames(12)   # the camera eases into place
        time.sleep(0.15)
    return moves


def hover_point(game: Game, inp: InputClient, uuid: str, frames: int = 2, deadline: float | None = None,
                seen: dict[str, int] | None = None) -> tuple[tuple[int, int] | None, int, dict]:
    """Move the virtual cursor until the engine's picking helper reports `uuid` under it.

    Stops at `deadline` (time.monotonic()). `seen` counts the other entities the helper reported instead.
    Returns ((x, y) or None, tries, last hover_check)."""
    w, h = inp.window_size()
    tries = 0
    last: dict = {}
    for aim, ox in AIMS:
        if deadline is not None and time.monotonic() >= deadline:
            last = {**last, "timed_out": True}
            break
        try:
            sp = _screen_pos(game, uuid, aim, ox)
        except GameError as e:
            last = {"error": str(e)}
            continue
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
            if seen is not None and last.get("hover"):
                seen[last["hover"]] = seen.get(last["hover"], 0) + 1
            if deadline is not None and time.monotonic() >= deadline:
                return None, tries, {**last, "timed_out": True}
    return None, tries, last


def click_entity(game: Game, inp: InputClient, uuid: str, button: str = "L", expect: str | None = None,
                 verify_timeout: float = 4.0) -> ClickResult:
    """Hover-verify then click `uuid`; if `expect` is given, wait until ui.state shows that screen."""
    before = _screen(game).get("screen")
    moves = bring_into_view(game, inp, uuid)
    deadline = time.monotonic() + HOVER_BUDGET_S
    seen: dict[str, int] = {}
    pos, tries, last = hover_point(game, inp, uuid, deadline=deadline, seen=seen)
    if pos is None and not moves and not last.get("timed_out"):
        # Every aim missed although the target looked in view: one more look from a wider angle.
        w, h = inp.window_size()
        inp.wheel(-2, w // 2, h // 2)
        inp.wait_frames(12)
        time.sleep(0.15)
        moves = ["zoom_out"]
        pos, t2, last = hover_point(game, inp, uuid, deadline=deadline, seen=seen)
        tries += t2
    extra: dict[str, Any] = {"camera": moves} if moves else {}
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
        return ClickResult(False, reason, before, None, tries, extra)
    inp.click(pos[0], pos[1], button=button)
    if not expect:
        return ClickResult(True, None, None, pos, tries, extra)
    wanted = EXPECT_SCREENS.get(expect, {expect})
    deadline = time.monotonic() + verify_timeout
    st: dict = {}
    while time.monotonic() < deadline:
        st = _screen(game)
        scr = st.get("screen")
        if (wanted and scr in wanted) or (not wanted and scr != before):
            return ClickResult(True, None, scr, pos, tries, extra)
        time.sleep(0.05)
    if expect == "roll":
        # A key in the party opens the lock without a roll; a trap kit may
        # resolve instantly. Accept the visible state change instead.
        try:
            ts = game.rpc("act.target_state", {"uuid": uuid})
        except GameError:
            ts = {}
        if ts.get("locked") is False or ts.get("trap_armed") is False:
            return ClickResult(True, None, "unlocked" if ts.get("locked") is False else "disarmed", pos, tries, extra)
    if st.get("notifications"):
        extra["notifications"] = st["notifications"]
    return ClickResult(False, "no_effect", st.get("screen"), pos, tries, extra)


def finish_needs_click(game: Game, inp: InputClient, result: dict | None) -> dict | None:
    """If a server job result asks for a click, perform it and merge the outcome into the result."""
    if not isinstance(result, dict) or not result.get("needs_click"):
        return result
    cr = click_entity(game, inp, result["needs_click"], result.get("button", "L"), result.get("expect"))
    out = {k: v for k, v in result.items() if k not in ("needs_click", "button")}
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
        jr.error = {"off_screen": "the target is not on screen",
                    "hover_miss": "could not point at the target",
                    "no_effect": "nothing happened"}.get(reason, reason)
    return jr
