"""Act tools in the world: moving, interacting, picking up, scrolls, revives, clicks and looting.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from .. import actq, economy, hotbar, hoverclick, macros, render, twostep
from ..game import GameError, JobResult, hold_client, release_client
from . import core, overview, plumbing, reporting

# ---------------------------------------------------------------------------
# act: world
# ---------------------------------------------------------------------------

@core.mcp.tool()
@reporting._guard
def move_to(target: str = "", x: float | None = None, y: float | None = None, z: float | None = None,
            who: str | None = None, run: bool = True, wait: bool = False, within: float | None = None,
            trespass: bool = False) -> str:
    """Walk to a visible entity (target=id) or a position (x,y,z). Returns within ~1 s: the real result if it already ended (blocked, refused, arrived), else a job id + ETA while the walk goes on; keep planning meanwhile. wait=true blocks only in combat. Stops early if combat/dialogue starts. who defaults to the active character (chained companions follow). Out of combat a destination past ~58 m goes via travel_to (legs) by itself. within=1.5 (with target): stop inside that melee reach. A floor plate as target is stood on (its centre; the companions stay put); walks go round the floor plates the party has seen. Walks keep out of an off-limits area the game warned you about (round it, else refused); trespass=true walks in anyway."""
    args: dict[str, Any] = {"char": plumbing._who(who), "run": run}
    if trespass:
        args["trespass"] = True
    if target:
        args["target"] = plumbing._uuid(target)
        if within is not None:
            args["within"] = float(within)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id> or x,y,z")
    # combat_turn's moves (in_turn, BATCH_CHECKS): in combat _far_move is always None, no act.watch for it
    far = None if reporting._batch.get("in_travel") or reporting._batch.get("in_turn") else \
        _far_move(args["char"], args.get("target"), args.get("pos"))
    if far:  # S005: 'destination is too far away' ×4 (one move is capped at MOVE_CAP_M): walk it in legs
        dest, d = far
        out = travel_macro.travel_to(x=dest[0], y=dest[1], z=dest[2], who=who, trespass=trespass)
        return f"move_to: {render.num(round(d))}m is past one walk ({render.num(MOVE_CAP_M)}m): went via travel_to\n" + out
    held = _plate_hold(args["char"]) if target and core.H.names.get(core.H.get(args["target"])) in PLATE_NAMES else []
    wait, note = plumbing._walk_wait(wait)
    out = plumbing._act("move_to", "act.move_to", args, wait=wait, note=note)
    if held:
        out += "\nstaying off the plates: " + ", ".join(queueing._label(u) for u in held) + " (regroup brings them back)"
    return out


PLATE_NAMES = ("Pressure Plate", "Marble Plate")  # the mod's V.PLATE_NAMES (Visibility.lua)


def _plate_hold(char: str) -> list[str]:
    """move_to onto a floor plate: the companions chained to the walker stay where they are (as the stay tool does),
    so following it they don't step on other plates. S084 04:16-04:21 (Counting House vault code plates): the code
    reset while companions shuffled onto plates. The ones held, in party order."""
    st = core.game.try_rpc("state.status") or {}
    held = []
    for r in st.get("party") or []:
        u = r.get("uuid")
        if u and u != char and r.get("chained") is True and u not in overview._stay:
            overview._stay.add(u)
            core.game.try_rpc("act.unchain", {"char": u})
            held.append(u)
    return held


MOVE_CAP_M = 58.0  # just under Guard.lua's G.MAX_MOVE_DIST (60 m, from the character): beyond it act.move_to refuses


def _ent_pos(uuid: str) -> list | None:
    """A party member's position from act.watch (cheap), else the visible entity's from state.entity."""
    p = travel_macro._pos_now(uuid)
    if p:
        return p
    ent = core.game.try_rpc("state.entity", {"uuid": uuid}) or {}
    p = ent.get("pos") if isinstance(ent, dict) else None
    return p if isinstance(p, (list, tuple)) and len(p) >= 3 else None


def _last_seen(uuid: str) -> tuple[list, int | None] | None:
    """(position, seconds ago) where the party last saw a character that is out of sight now (the mod's memory of
    this level: what get_nearby lists as REMEMBERED), else None (visible, never seen, or an item).
    S026: talk_to Mattis/e511/e720 refused "not visible" 7x; each cost a get_nearby + a walk + a retry."""
    ent = core.game.try_rpc("state.entity", {"uuid": uuid}) or {}
    if not isinstance(ent, dict) or ent.get("visible") is not False:
        return None
    p = ent.get("last_pos")
    if not (isinstance(p, (list, tuple)) and len(p) >= 3):
        return None
    ago = ent.get("ago_s")
    return [float(v) for v in p[:3]], (int(ago) if isinstance(ago, (int, float)) else None)


def _seen_line(target: str, seen: tuple[list, int | None]) -> str:
    ago = f" {render.num(seen[1])}s ago" if seen[1] is not None else ""
    return f"{target} is out of sight: going to where you last saw them{ago} {render.pos(seen[0])}"


def _far_move(char: str, target: str | None, pos: list | None) -> tuple[list, float] | None:
    """(destination, metres) when a move is past MOVE_CAP_M out of combat (go via travel_to's legs), else None.
    In combat it stays a plain move (the guard's refusal is the honest answer there)."""
    w = queueing._watch_conn().try_rpc("act.watch") or {}
    if w.get("combat"):
        return None
    here = next((r.get("pos") for r in w.get("party") or [] if r.get("uuid") == char), None)
    dest = _ent_pos(target) if target else pos
    if not here or not dest:
        return None
    d = macros.dist(here, dest)
    return (list(dest[:3]), d) if d > MOVE_CAP_M else None


@core.mcp.tool()
@reporting._guard
def interact(target: str, who: str | None = None, wait: bool = True) -> str:
    """Use/open/activate a visible object: door, lever, chest, corpse (opens loot), book, ladder, waypoint. Walks there first (past 40 m: a travel_to, then the use on arrival). A locked door/chest opens when anyone in the party carries its key (that member walks up and opens it). wait=false returns at once with a job id."""
    char = plumbing._who(who)
    _EMPTY_CLOSED[0] = -1e9
    out = plumbing._act("interact", "act.interact", {"char": char, "target": plumbing._interact_uuid(target)}, wait=wait)
    out = travel._walk_first("interact", out, char, who, target, actq.Step("interact", {"target": target})) or out
    return _container_left_open(out) if wait else out


# S073 22:47-22:58: 3 hand ESCAPEs after interact opened a cabinet/wardrobe ("CONTAINER text=Take All", "STOPPED:
# container on screen"): two showed no slots, one only things the agent left. An empty one is closed here; a full
# one stays open (the take is the agent's) and the result says how to close it. The grid may fill a frame late, so
# an empty read is read again before closing.
CONTAINER_EMPTY_READS, CONTAINER_EMPTY_S = 3, 0.3
CONTAINER_OPEN_NOTE = ('(the container window stays open: loot() takes everything and closes it, loot(take="ids") '
                       'takes some, loot(take="none") closes it without taking)')


def _container_left_open(out: str) -> str:
    if "\nCONTAINER " not in "\n" + out:
        return out
    for i in range(CONTAINER_EMPTY_READS):
        c = (core.game.try_rpc("ui.state", context="client") or {}).get("container")
        if not isinstance(c, dict):
            return out  # closed meanwhile
        if c.get("slots"):
            return out + "\n" + CONTAINER_OPEN_NOTE
        if i + 1 < CONTAINER_EMPTY_READS:
            time.sleep(CONTAINER_EMPTY_S)
    r = core.game.run_job("ui.loot", {"close": True}, context="client", wait_s=5)
    if r.status != "done":
        return out + f"\n(the container is empty; closing its window failed: {r.error or r.status})"
    _EMPTY_CLOSED[0] = time.monotonic()
    keep = [ln for ln in out.split("\n") if not ln.startswith("CONTAINER ") and ln != "STOPPED: container on screen"]
    return "\n".join(keep + ["(the container is empty: closed its window)"])


_EMPTY_CLOSED = [-1e9]   # monotonic time interact closed an empty container (loot_area's loot step follows it)


@core.mcp.tool()
@reporting._guard
def pick_up(item: str, who: str | None = None, wait: bool = True) -> str:
    """Pick up a visible item from the ground (walks there first). wait=false returns at once with a job id."""
    return plumbing._act("pick_up", "act.pickup", {"char": plumbing._who(who), "item": plumbing._world_uuid(item)}, wait=wait)


_inp_client = {"c": None}


def _inp():
    from ..input import InputClient
    if _inp_client["c"] is None:
        try:
            c = InputClient()
            c.agent(True)
            _inp_client["c"] = c
        except Exception:
            return None
    return _inp_client["c"]


# A23 (S086 05:17:45 hang; impl-setup S17/S21): our click on the rest/travel Yes/No box starts the game's transition
# (End the Day, a camp scene, a region load). S086: after the click, ui.box_open polls and then the follow-up
# ui.long_rest job read the client UI while the game tore the box down: 954 frame-lock timeouts in two client Ticks,
# the story thread stopped answering, FAULT hang. From the click on, no client call at all (game.hold_client) until
# the server says the transition is over: a rest-flow answer (end_day_ok, rest_started, a refusal), a conversation
# starting (the rest check's Dialog flag), the server's game state back to Running after a load, or the leader moved
# (a trip within the level); then TRANSITION_SETTLE_S with the server up. No word in TRANSITION_MAX_S: go on anyway.
TRANSITION_TOOLS = ("long_rest", "short_rest", "fast_travel")
TRANSITION_MAX_S = 20.0
TRANSITION_SETTLE_S = 1.0
TRANSITION_POLL_S = 0.25
_TRANSITION_DONE = ("end_day_ok", "end_day_failed", "rest_started", "rest_start_failed", "rest_cancelled",
                    "gather_failed", "ready_failed", "rest_denied")
_TRANSITION_LAST: dict = {}


def _transition_wait(tool: str) -> dict:
    """Server reads only (sys.info, camp.rest_flow, state.status) until the transition after the box click is over.
    -> {transition: what ended it, transition_s}."""
    t0 = time.monotonic()

    def srv(op: str, a: dict | None = None) -> Any:
        try:
            return core.game.rpc(op, a or {}, context="server", timeout=3.0)
        except GameError:
            return None  # a load resets the server Lua: no answer = still loading

    pos0 = travel._lead_pos(srv("state.status")) if tool == "fast_travel" else None
    left, why = False, "no word from the game"
    while time.monotonic() - t0 < TRANSITION_MAX_S:
        info = srv("sys.info")
        gs = info.get("game_state") if isinstance(info, dict) else None
        if gs not in ("Running", "Paused"):
            left = True
        elif left:
            why = "the game loaded"
            break
        elif tool == "short_rest" and time.monotonic() - t0 >= TRANSITION_SETTLE_S:
            why = "settled"
            break
        elif tool == "long_rest":
            flow = srv("camp.rest_flow", {"since_ms": int((time.monotonic() - t0) * 1000) + 300})
            steps = [str(x) for x in (flow.get("steps") or [])] if isinstance(flow, dict) else []
            done = [x for x in steps if inventory._rest_step(x) in _TRANSITION_DONE]
            can = flow.get("can_rest") if isinstance(flow, dict) else None
            if done:
                why = f"the game answered ({inventory._rest_step(done[-1])})"
                break
            if any("Dialog" in (r.get("flags") or []) for r in (can or {}).get("blocked") or [] if isinstance(r, dict)):
                why = "a conversation started"
                break
        elif tool == "fast_travel" and pos0:
            p = travel._lead_pos(srv("state.status"))
            if p and math.dist(p[:3], pos0[:3]) > 3.0:
                why = "the party moved"
                break
        time.sleep(TRANSITION_POLL_S)
    if why not in ("settled", "no word from the game"):
        time.sleep(TRANSITION_SETTLE_S)
    out = {"transition": why, "transition_s": round(time.monotonic() - t0, 1)}
    _TRANSITION_LAST.clear()
    _TRANSITION_LAST.update(out, tool=tool)
    return out


def _transition_after(tool: str):
    """boxclick's after-click step for a transition box: hold every client call, wait on the server, release."""
    def after(res: dict) -> dict:
        hold_client(TRANSITION_MAX_S + TRANSITION_SETTLE_S + 5.0, f"{tool}: the game is changing screens after the Yes")
        try:
            res.update(_transition_wait(tool))
        finally:
            release_client()
        res["closed"] = True
        return res
    return after


def _box_act(tool: str, op: str, args: dict, wait_s: float, rounds: int = 3, on_start=None) -> str:
    """_act for the client jobs that answer the game's Yes/No box (rests, fast travel, confirm). When the mod
    says its button press didn't land (needs_click; live S013/S014 "[ForceUpdate] Yes/No" stayed open), click
    the button with a hover-checked mouse click (boxclick) and run the op again from where it stopped."""
    from .. import boxclick

    out = plumbing._act(tool, op, args, context="client", wait_s=wait_s, on_start=on_start)
    notes: list[str] = []
    for _ in range(rounds):
        res = reporting._batch.get("result")
        ans = res.get("needs_click") if isinstance(res, dict) else None
        inp = _inp() if ans else None
        if inp is None:
            break
        c = boxclick.click_box(lambda o, a: core.game.rpc(o, a, context="client"), inp, str(ans),
                               after=_transition_after(tool) if tool in TRANSITION_TOOLS else None)
        q = res.get("question")
        notes.append(boxclick.describe(c) + (f" (the question: {q})" if q and "[ForceUpdate]" not in str(q) else ""))
        if c.get("transition") == "a conversation started":
            # S086: the Yes started a camp scene; the agent answers it (no client read of the rest screen meanwhile)
            out = (f"{tool}: STOPPED — {notes.pop()}; a conversation started (a camp scene): finish it (get_dialogue, "
                   f"dialogue_advance / choose_dialogue), then {tool} again")
            break
        if tool in ("confirm", "fast_travel") or not (c.get("closed") or c.get("shown") or c.get("gone")):
            if tool != "long_rest" and tool != "short_rest" and not c.get("error"):
                out = f"{tool}: done — {notes.pop()}" + ("; travelling" if tool == "fast_travel" else "")
            break
        time.sleep(0.5)  # the next screen (rest supplies, travel) opens
        out = plumbing._act(tool, op, args, context="client", wait_s=wait_s)
    if notes and ": done" not in out.split("\n", 1)[0]:
        # Only on a failure: S033 18:36 a full rest read "(the button press didn't land …)" first, as if it failed.
        out = "(the button press didn't land: " + "; ".join(notes) + ")\n" + out
    return out


def _click_act(tool: str, op: str, args: dict, wait_s: float = 40.0) -> str:
    """Server op that walks up and then needs a verified click on the target (engine has no direct call)."""
    from ..clicker import run_click_action

    t0 = time.monotonic()
    if core._session_over():
        return reporting._finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    r = run_click_action(core.game, _inp(), op, args, wait_s)
    plumbing._remember_names(r.result)
    reporting._batch["last"] = r.status
    return reporting._finish(tool, args, plumbing._job_text(tool, r, args.get("char")), t0)


@core.mcp.tool()
@reporting._guard
def loot_corpse(corpse: str, who: str | None = None) -> str:
    """Walk to a visible dead body and open its loot window (then use loot)."""
    _EMPTY_CLOSED[0] = -1e9
    return _click_act("loot_corpse", "act.loot_corpse", {"char": (me := plumbing._who(who)), "target": plumbing._nearest_uuid(corpse, me)})


@core.mcp.tool()
@reporting._guard
def lockpick(target: str, who: str | None = None) -> str:
    """Pick the lock of a visible locked door/chest (needs Thieves' Tools); opens the roll — then roll_check."""
    return _act_click_roll("lockpick", "act.lockpick", {"char": (me := plumbing._who(who)), "target": plumbing._nearest_uuid(target, me)})


@core.mcp.tool()
@reporting._guard
def disarm(trap: str, who: str | None = None) -> str:
    """Disarm a DISCOVERED trap (needs a Trap Disarm Toolkit); opens the roll — then roll_check."""
    return _act_click_roll("disarm", "act.disarm", {"char": (me := plumbing._who(who)), "target": plumbing._nearest_uuid(trap, me)})


@core.mcp.tool()
@reporting._guard
def pickpocket(npc: str, who: str | None = None, take: str = "", min_chance: int = 0, leave: bool = False) -> str:
    """Sneak up to a visible NPC and open the pickpocket window; the result lists their pockets with the game's own odds per item (chance %, DC). Those odds assume nobody sees the thief: a steal in the owner's sight fails whatever they say, so steal while hidden (sneaking unseen, invisible). take: steal that item (name or id) in the same call (the window is opened first if needed; Steal is clicked); min_chance: take nothing below this %. A failed steal is a crime if they notice. leave=true closes the window."""
    t0 = time.monotonic()
    target = plumbing._uuid(npc)
    args: dict[str, Any] = {"npc": target}
    if leave:
        _pp_open["npc"] = None
        r = _pockets(dict(args, leave=True))
        gone = r.status != "done" and "no pickpocket window" in str(r.error or "")   # it had closed itself (live 12:4x)
        return reporting._finish("pickpocket", {"npc": npc, "leave": True},
                       "pickpocket: left (window closed)" if r.status == "done" else
                       "pickpocket: left (no window was open)" if gone else plumbing._job_text("pickpocket", r), t0)
    out = sel = ""
    listing = _pockets(args)
    if listing.status == "done" and _pp_open["npc"] != target:
        # live2 §1: a window left open for another NPC (or opened by hand) was listed as this NPC's pockets (a stale
        # empty Cerys window read "nothing to steal" for Alfira and Tumeril 4 times). Close it and open this NPC's.
        # live4 §2: the close lands frames later, so an open sent at once was undone with it ("opened", then no window
        # 2/2 when another process had left this NPC's window open): wait for the window to be gone first.
        _pockets(dict(args, leave=True))
        listing = _pockets(args)
        deadline = time.monotonic() + PP_CLOSE_S
        while listing.status == "done" and time.monotonic() < deadline:
            time.sleep(PP_POLL_S)
            listing = _pockets(args)
        if listing.status == "done":
            return reporting._finish("pickpocket", args, "pickpocket: FAILED — a pickpocket window this call didn't "
                                     f"open stayed open {PP_CLOSE_S:g} s after closing it; nothing taken", t0)
    if listing.status != "done" and "no pickpocket window" in str(listing.error or ""):
        char = plumbing._who(who)
        if who and not uitools._hud_has(char):
            # Live 12:3x: who=Shadowheart while Claude was controlled: the engine opened the window, but the client
            # shows it only for the controlled character ("no pickpocket window" after 3 s). Take control, as a player
            # does before stealing with someone else.
            r = core.game.run_job("act.select", {"char": char}, wait_s=5)
            sel = f"(took control of {who}: the window shows for the controlled character)" if r.status == "done" else ""
        out = _click_act("pickpocket", "act.pickpocket", {"char": char, "target": target})
        if reporting._batch.get("last") != "done":
            return out
        _pp_open["npc"] = target
        # The engine says opened before the client widget shows (live 12:5x: Dammon's window up ~1 s later).
        deadline = time.monotonic() + 3.0
        listing = _pockets(args)
        def early(r: JobResult) -> bool:   # widget not up yet, or up with its slots not filled yet (live 12:5x)
            if r.status == "done":   # or its odds not filled yet (live5 S3#0: Gold x15 'odds ?', live4 read 95%)
                items = (r.result or {}).get("items") or []
                return not items or any(economy.odds_unread(it) for it in items if isinstance(it, dict))
            return "no pickpocket window" in str(r.error or "")
        while early(listing) and time.monotonic() < deadline:
            time.sleep(PP_POLL_S)
            listing = _pockets(args)
    if listing.status != "done":
        return reporting._finish("pickpocket", args, (out + "\n" if out else "") + plumbing._job_text("pickpocket", listing), t0)
    plumbing._remember_names(listing.result)
    text = economy.pockets_text(listing.result, _pocket_handle)
    if take:
        tk = plumbing._uuid(take) if re.fullmatch(r"e\d+", take.strip()) else take   # a handle from the listing, else a name
        stole = _steal(args, tk, min_chance)
        if stole.startswith("stole "):   # live4 §5: the listing read before the steal still showed the stolen item
            after = _pockets(args)
            text = economy.pockets_text(after.result, _pocket_handle) if after.status == "done" else ""
        text = stole + ("\n" + text if text else "")
    head = out.split("\n", 1)[0] + "\n" if out else ""
    return reporting._finish("pickpocket", {"npc": npc, "take": take}, head + text + ("\n" + sel if sel else ""), t0)


_pp_open: dict[str, str | None] = {"npc": None}   # the NPC whose pickpocket window this server opened last
PP_CLOSE_S = 2.0   # how long a closed pickpocket window may take to go
PP_POLL_S = 0.25
SELECT_ONLY = 101   # ui.pickpocket's min_chance above any odds: it selects the item, reads its odds, presses nothing
STEAL_SEED = (0.5604, 0.6519)   # live2 §1: the Steal button at (538, 352) of a 960x540 window
STEAL_ROLL_S = 5.0


def _steal(args: dict, item: str, min_chance: int) -> str:
    """Select the item (the mod reads its odds), then click Steal as a player does: ui.pickpocket's own press of
    StealBtn never started a steal (live2 §1: 0 events in 14 tries; every steal came from a hand click)."""
    r = _pockets(dict(args, take=item, min_chance=SELECT_ONLY))
    plumbing._remember_names(r.result)
    res = r.result if isinstance(r.result, dict) else {}
    if r.status == "done" and not res.get("pending"):
        return economy.steal_text(res, _pocket_handle)   # odds unread: the mod pressed and the roll came anyway
    it = res.get("item")
    if not isinstance(it, dict):
        return plumbing._job_text("pickpocket", r)
    chance = it.get("chance")
    if min_chance and isinstance(chance, (int, float)) and chance < min_chance:
        return f"FAILED: {it.get('name')}: {chance}% chance (DC {it.get('dc')}) is below min_chance {min_chance}; nothing taken"
    inp = _inp()
    if inp is None:
        return "FAILED: no mouse input to click Steal (the input helper is not running)"
    probe = lambda: core.game.rpc("ui.steal_probe", {}, context="client")  # noqa: E731
    with plumbing._InputHeld():
        click = hoverclick.hover_click(probe, inp, "steal", STEAL_SEED)
    if not click.get("clicked"):
        why = "the window closed" if click.get("gone") else click.get("error")
        return f"FAILED: couldn't click Steal ({why}; {click.get('tries', 0)} tries); nothing taken"
    return economy.steal_text(_steal_outcome(probe, it), _pocket_handle)


def _steal_outcome(probe, it: dict) -> dict:
    """After the click: the window's roll until it finished, or the window closing (a caught thief's crime scene)."""
    end = time.monotonic() + STEAL_ROLL_S
    while True:
        v = probe()
        if not isinstance(v, dict) or not v.get("open"):
            return {"item": it, "closed": True, "chance": it.get("chance")}
        roll = v.get("roll") or {}
        if roll.get("finished"):
            return {"item": it, "success": roll.get("success"), "rolled": roll.get("rolled"), "dc": roll.get("dc"),
                    "chance": it.get("chance")}
        if time.monotonic() >= end:
            return {"item": it, "pending": True}
        time.sleep(0.2)


def _pockets(args: dict) -> JobResult:
    """ui.pickpocket as a JobResult; its refusals (no window open) come back as failed, not raised."""
    try:
        return core.game.run_job("ui.pickpocket", args, context="client", wait_s=15)
    except Exception as e:
        return JobResult("failed", error=str(e))


def _pocket_handle(it: dict) -> str:
    u = it.get("uuid")
    return core.H.get(u, it.get("name")) if isinstance(u, str) and u else ""


def _carried_scroll(char: str, ability: str) -> dict | None:
    """A scroll (any carried item) named like `ability` ('Scroll of Invisibility' or just 'Invisibility'): the
    character's own first, else a party member's. S030 16:34: use_ability(Shadowheart, "Scroll of Invisibility")
    → "no action called", then manage_inventory use left the game waiting for a target (screenshot + hand click)."""
    a = re.sub(r"\s+", " ", str(ability or "").strip().lower())
    if not a:
        return None
    want = {a, a if a.startswith("scroll of ") else f"scroll of {a}"}
    items = [it for it in ((core.game.try_rpc("state.inventory", {}) or {}).get("items") or [])
             if isinstance(it, dict) and str(it.get("name") or "").strip().lower() in want and it.get("uuid")]
    if not items:
        return None
    return next((it for it in items if it.get("owner") == char), items[0])


SCROLL_MAX_PICKS = 6     # Scorching Ray's beams and the like: one click per pick
SCROLL_SETTLE_S = 2.5    # a combat scroll: the scroll is used up this long after the last click at most


def _scroll_more_picks(inp, char: str, tgts: list[str]) -> str:
    """A scroll whose spell takes several picks (Scorching Ray's 3 beams): click on while the cursor counter wants
    more, cycling `tgts` (one target: all on it), then the tick when the game waits for it. '' when one pick did."""
    from ..clicker import click_entity
    def state() -> dict:
        return core.game.try_rpc("ui.cast_state", {"char": char}, context="client") or {}

    n = 1
    st = state()
    while st.get("armed") and (st.get("remaining") or 0) > 0 and n < SCROLL_MAX_PICKS:
        cr = click_entity(core.game, inp, tgts[n % len(tgts)], "L")
        if not cr.ok:
            return (f"; pick {n + 1} missed ({cr.reason}): the game still waits for it (click_on <id>, or press_key "
                    f"ESCAPE to put it away)")
        n += 1
        st = state()
    if st.get("armed") and st.get("can_force"):
        try:
            core.game.rpc("ui.cast_confirm", {}, context="client")
        except GameError as e:
            return f"; {n} picks, but the tick failed ({e})"
    return f" ({n} picks)" if n > 1 else ""


def _scroll_settle(char: str) -> str:
    """A scroll read in combat (act.interact's combat_scroll): once it is used up, the mod charges what reading it
    cost (an action) and the engine did not. What to tell the agent about it."""
    end = time.monotonic() + SCROLL_SETTLE_S
    while True:
        last = time.monotonic() >= end
        r = core.game.try_rpc("act.scroll_settle", {"char": char, "final": last}) or {}
        if r.get("read") or r.get("none"):
            break
        if last:
            return "; the scroll was not used up (no target taken?): no action spent"
        time.sleep(0.2)
    if not r.get("read"):
        return ""
    paid = ", ".join(str(c.get("resource")) for c in r.get("charged") or [] if isinstance(c, dict))
    return "; its action is spent" + (f" (charged by the guard: {paid})" if paid else "")


def _use_scroll(char: str, it: dict, target: str, targets: list[str] | None = None,
                pos: list[float] | None = None) -> str:
    """Read a scroll as a player does: use it from the bags (the game then waits for a target), then click the
    target (default: the reader), once per pick for a multi-pick spell (`targets` in click order), or with `pos` and
    no target the ground there (a point/cone scroll). In combat (A7) it is read on the reader's turn for an action,
    like casting; the mod refuses it off-turn or with no action left.
    A scroll on another member's bags is named, not taken."""
    name = str(it.get("name"))
    if it.get("owner") and it.get("owner") != char:
        raise GameError(f"{queueing._label(char)} has no action called that; {queueing._label(it['owner'])} carries {name} "
                        f"({core.H.get(it['uuid'])}): use_ability(who=\"{overview._name_of(it['owner'])}\", ability=\"{name}\", "
                        f"target=...) or give it over first")
    tgt0 = plumbing._uuid(target) if target else char
    if _two_step_scroll(name) and tgt0 != char:
        far = _body_far(char, tgt0)
        if far:
            raise GameError(far)
    if pos is not None and not target and _TELEPORT_SCROLL.search(name):
        # S093 09:47: a teleport to a point with no floor under it (Misty Step into the psyche's void: Gale died);
        # the mod's landing check (a floor at that height, no Chasm) before the scroll is read
        lc = core.game.try_rpc("act.landing_check", {"char": char, "pos": [float(v) for v in pos[:3]]}) or {}
        if lc.get("ok") is False:
            raise GameError(f"{name} not read: {lc.get('why')}")
    _select_reader(char)
    out = plumbing._act("use_ability", "act.interact", {"char": char, "target": it["uuid"]})
    if reporting._batch.get("last") != "done" or "drunk" in out.partition("\n")[0]:
        return out  # a potion (in combat: for its bonus action) waits for no target
    inp = _inp()
    if inp is None:
        return out + "\nthe game waits for the scroll's target: no input shim to click it (click_on <id>)"
    from ..clicker import click_entity
    tgt = plumbing._uuid(target) if target else char
    # the targeting cursor comes up a few frames after the use; a click before it is a plain click on the world
    armed, _ = hotbar._wait(core.game, char, lambda s: bool(s.get("armed")), twostep.ARM_WAIT_S)
    scr = (core.game.try_rpc("ui.state", context="client") or {}).get("screen")
    if scr == "book":
        # S031 17:12: use_ability(book, target=pedestal) read the book (its page opened) and the click then turned
        # the camera 6x behind the page: a book is read, not cast on anything.
        head, _, rest = out.partition("\n")
        head += (f" ({name} opened for reading: it has no target; press_key ESCAPE closes the page. To put it on a "
                 f"pedestal or in a slot: insert_item(item, into))")
        return head + ("\n" + rest if rest else "")
    head, _, rest = out.partition("\n")
    in_combat = "combat_scroll" in head
    head = head.replace(" — combat_scroll", " — read in combat").replace("combat_scroll", "read in combat")
    if pos is not None and not target:
        head += _scroll_ground(inp, name, char, pos)
        if in_combat:
            head += _scroll_settle(char)
        return head + ("\n" + rest if rest else "")
    if not armed:
        # live9 S002 17:34 x2: the body click went in with nothing in hand, a plain click on the world
        head += (f" ({name} read, but no spell came into hand, so {queueing._label(tgt)} was not clicked: it cast at "
                 "once, or the game did not take the read)")
        if in_combat:
            head += _scroll_settle(char)
        return head + ("\n" + rest if rest else "")
    tgts = [tgt] + [plumbing._uuid(t) for t in targets or []]
    cr = click_entity(core.game, inp, tgt, "L")
    if cr.ok and _two_step_scroll(name) and tgt != char:
        head += _revive_spot(inp, name, char, tgt)
    elif cr.ok:
        head += f" ({name} read, cast on {queueing._label(tgt)} by a click)" + _scroll_more_picks(inp, char, tgts)
    else:
        head += (f"; the game waits for {name}'s target but the click on {queueing._label(tgt)} failed ({cr.reason}): "
                 f"click_on <id>, or press_key ESCAPE to put it away")
    if in_combat and cr.ok:
        head += _scroll_settle(char)
    return head + ("\n" + rest if rest else "")


def _select_reader(char: str) -> None:
    """live9 S002 17:34 (x2): Lae'zel read a Scroll of Revivify while the HUD had another member selected; the spell
    never showed in hand (ui.cast_state reads the HUD's), so the body click did nothing. After set_party select
    Lae'zel the same read put it in hand. A player selects the reader before using a scroll from their bags."""
    hud = (core.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
    if isinstance(hud, str) and hud[-36:] != char[-36:]:
        core.game.run_job("act.select", {"char": char}, wait_s=5)


_TELEPORT_SCROLL = re.compile(r"misty step|thunder step|far step", re.IGNORECASE)
TELEPORT_WAIT_S = 2.0   # a teleport scroll: the reader lands this long after the ground click at most


def _scroll_ground(inp, name: str, char: str, pos: list[float]) -> str:
    """S066 #66/#130/#143: Scroll of Cone of Cold and Scroll of Misty Step came back "cast on <the reader> by a
    click" (the x,y,z dropped; Karlach never moved, a 400 g scroll and the action for nothing). The spell in hand
    is aimed at the ground at `pos` as a player clicks it (twostep.ground_click); a teleport says where the reader
    is after it."""
    def at() -> list[float] | None:
        st = core.game.try_rpc("state.status") or {}
        p = next((r.get("pos") for r in st.get("party") or [] if isinstance(r, dict) and r.get("uuid") == char), None)
        return list(p[:3]) if isinstance(p, (list, tuple)) and len(p) >= 3 else None

    tele = bool(_TELEPORT_SCROLL.search(name))
    p0 = at() if tele else None
    res = twostep.ground_click(core.game, inp, char, name, pos, should_stop=combat._click_stop())
    where = render.pos(pos)
    looks = twostep.looks_brief(res.detail.get("looks") or [])
    if not res.ok:
        why = res.detail.get("why") or res.reason
        back = ("the spell left the hand without a cast" if res.reason == "dropped"
                else "the spell was put back" if res.detail.get("cancelled") or res.stage == "arm"
                else "the spell may still be in hand (press_key ESCAPE)")
        out = f"; {name}: the ground at {where} was not taken ({queueing._label(why)}); {back}, nothing cast"
        if looks:
            out += f" [{queueing._label(looks)}]"
        # L8 23:58: this said "nothing cast" while Karlach stood 5.1 m on, 2 m from the landing a second later
        p1 = at() if tele and p0 is not None else None
        if p1 is not None and sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5 > 1.0:
            d = sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5
            out += (f"; yet {overview._name_of(char)} moved {d:.0f} m (now {render.pos([round(v, 1) for v in p1])}): check "
                    "whether the scroll is still in the bags")
        return out
    src = res.detail.get("ground_src")
    out = f" ({name} read, aimed at the ground at {where} by a click" + (f"; ground point from {src}" if src else "")
    if res.detail.get("clear_of"):
        at_ = render.pos([round(v, 1) for v in res.detail.get("ground_at") or pos])
        out += (f"; landed the click at {at_}, {res.detail.get('shifted_m')} m from the asked point: "
                f"{', '.join(queueing._label(u) for u in res.detail['clear_of'])} stood under the cursor there")
    out += ")"
    if not tele or p0 is None:
        return out
    end, p1 = time.monotonic() + TELEPORT_WAIT_S, p0
    while time.monotonic() < end:
        p1 = at() or p0
        if sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5 > 1.0:
            break
        time.sleep(0.2)
    d = sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5
    return out + (f"; {overview._name_of(char)} moved {d:.0f} m, now {render.pos([round(v, 1) for v in p1])}" if d > 1.0
                  else f"; {overview._name_of(char)} did NOT move (still {render.pos([round(v, 1) for v in p0])})")


REVIVE_WAIT_S = 4.0   # Revivify: the body stands up this long after the second click at most


def _two_step_scroll(name: str) -> bool:
    """A scroll whose spell takes two clicks: the body, then the spot it stands up on (Teleportation_Revivify_Scroll,
    Resurrection: SpellType Teleportation with OriginTargetConditions in the game data)."""
    return bool(re.search(r"revivify|resurrection", str(name or ""), re.I))


def _is_dead(uuid: str) -> bool | None:
    st = core.game.try_rpc("state.status") or {}
    for r in st.get("party") or []:
        if isinstance(r, dict) and r.get("uuid") == uuid:
            return bool(r.get("dead"))
    return None


REVIVE_REACH_M = 9.0  # Revivify's range (Teleportation_Resurrection TargetRadius)


def _body_far(char: str, tgt: str) -> str | None:
    """A body out of the scroll's reach, before the scroll is read (S058 07:52: read at camp, the bodies were back
    in the Audience Hall: the click failed off_screen). None when near or unknown."""
    st = core.game.try_rpc("state.status") or {}
    pos = {r.get("uuid"): r.get("pos") for r in st.get("party") or [] if isinstance(r, dict)}
    a, b = pos.get(char), pos.get(tgt)
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) >= 3 and len(b) >= 3):
        return None
    d = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5
    if d <= REVIVE_REACH_M:
        return None
    return (f"{overview._name_of(tgt)}'s body is {d:.0f} m away (it stays where they died; camp doesn't have it): walk within "
            f"{REVIVE_REACH_M:.0f} m first (move_to target={queueing._label(tgt)} within=3), then read the scroll. Not read")


def _revive_spot(inp, name: str, char: str, tgt: str) -> str:
    """S057/S058: 7 Scrolls of Revivify 'cast on Shadowheart by a click' and nobody stood up: the game then waits for
    the spot the body stands up on. Click the ground beside the body (twostep.revive_ground: S089 07:08 x3 the click
    went on a creature, the spell stayed in hand and this said only STILL DEAD), then say whether they came back."""
    time.sleep(0.3)   # the second step's cursor comes up a few frames after the first click
    res = twostep.revive_ground(core.game, inp, char, name, tgt, should_stop=combat._click_stop())
    combat._log_hotbar(name, res)
    if not res.ok and res.reason != "not_armed":
        why = res.detail.get("why") or res.reason
        looks = twostep.looks_brief(res.detail.get("looks") or [])
        back = ("the spell left the hand without a cast" if res.reason == "dropped"
                else "the spell was put back" if res.detail.get("cancelled")
                else "the spell may still be in hand (press_key ESCAPE)")
        return (f"; {name}: the body {queueing._label(tgt)} was clicked, but no ground beside it was taken to stand them up "
                f"on ({queueing._label(why)})" + (f" [{queueing._label(looks)}]" if looks else "")
                + f"; {back}, nothing cast (the scroll is still in the bags). The spot must be floor right beside the "
                "body with nobody on it (a burning patch there is fine: S090): move the party off the ground around "
                "the body, or read it from another side")
    end = time.monotonic() + REVIVE_WAIT_S
    dead = _is_dead(tgt)
    while dead and time.monotonic() < end:
        time.sleep(0.4)
        dead = _is_dead(tgt)
    how = (f"clicked the body {queueing._label(tgt)} and the ground beside it" if res.ok
           else f"clicked the body {queueing._label(tgt)}; no spell in hand after it")
    if res.detail.get("spot_in"):
        how += (f"; they stand up in {res.detail['spot_in']} (the spot beside the body the game took): it can put them "
                "down again at once (S090), so heal them now (Lay on Hands, a potion: Help gives 1 HP) and get them out")
    if dead is None:
        return f" ({name} read: {how}; not a party member, so not checked)"
    if dead:
        return f" ({name} read: {how}, but {overview._name_of(tgt)} is STILL DEAD after {REVIVE_WAIT_S:.0f} s)"
    return f" ({name} read: {overview._name_of(tgt)} REVIVED ({how}))"


@core.mcp.tool()
@reporting._guard
@plumbing._input_exclusive
def click_on(target: str, button: str = "left") -> str:
    """Last-resort: point the mouse at a visible entity (verified hover) and click it, for interactions no other tool covers. Logged."""
    from .. import clicker
    from ..clicker import click_entity

    t0 = time.monotonic()
    inp = _inp()
    if inp is None:
        raise GameError("input shim not available")
    u, btn = plumbing._world_uuid(target), "R" if button.lower().startswith("r") else "L"
    head = ""
    if clicker.HOVER_V2 and _click_off_view(u):
        # select the member beside it BEFORE the first look (naut-r2: the off_screen pass cost 7 s before the retry)
        try:
            near = _click_selects(u)
        except Exception:
            near = None
        if near and core.game.run_job("act.select", {"char": near}, wait_s=4).status == "done":
            head = f"selected {queueing._label(near)} first (next to it; the camera follows the selected character)\n"
    cr = click_entity(core.game, inp, u, btn)
    if not cr.ok and cr.reason == "off_screen" and not head:
        near = _click_selects(u)
        if near and core.game.run_job("act.select", {"char": near}, wait_s=4).status == "done":
            head = f"selected {queueing._label(near)} first (next to it; the camera follows the selected character)\n"
            cr = click_entity(core.game, inp, u, btn)
    tail = f"\nthe {cr.screen} screen covers the world: press_key ESCAPE first" if cr.reason == "covered" else ""
    return reporting._finish("click_on", {"target": target}, head + ("clicked" if cr.ok else f"FAILED: {cr.reason}") + " — " + render.compact(cr.as_dict(), core.H) + tail, t0)


CLICK_SELECT_M = 12.0   # click_on off_screen: a party member at most this near the target is selected, then retried


def _click_off_view(uuid: str) -> bool:
    """click_on: the target is not well inside the window now (one ui.screen_pos; False when it can't be read)."""
    from ..clicker import _centered
    try:
        sp = core.game.rpc("ui.screen_pos", {"uuid": uuid}, context="client")
    except Exception:
        return False
    return isinstance(sp, dict) and "on_screen" in sp and not _centered(sp)


def _click_selects(uuid: str) -> str | None:
    """click_on off_screen: the game's center key brings the camera back to the SELECTED character, who may stand far
    from the target while another member stands next to it (naut-r2 S001 16:54: a shared combat turn with the camera
    on Lae'zel, Corran 1.6 m from the nerve; click_on FAILED off_screen x2, then set_party select Corran + click_on
    worked). -> the living member nearest the target when that is not the selected one, else None."""
    tpos = _ent_pos(uuid)
    if not tpos:
        return None
    active = (core.game.try_rpc("state.status") or {}).get("active")
    rows = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
    best, bd, ad = None, None, None
    for r in rows:
        p = r.get("pos") if isinstance(r, dict) else None
        if not (isinstance(p, (list, tuple)) and len(p) >= 3) or r.get("dead") or r.get("downed"):
            continue
        d = sum((float(a) - float(b)) ** 2 for a, b in zip(p[:3], tpos[:3])) ** 0.5
        if r.get("uuid") == active:
            ad = d
        if bd is None or d < bd:
            best, bd = r.get("uuid"), d
    if not best or best == active or bd > CLICK_SELECT_M or (ad is not None and ad - bd < 3.0):
        return None
    return best


def _act_click_roll(tool: str, op: str, args: dict) -> str:
    out = _click_act(tool, op, args)
    if reporting._batch["last"] == "done" and not reporting._batch["active"]:
        ui = core.game.try_rpc("ui.state", context="client") or {}
        if ui.get("roll"):
            out += "\n" + "\n".join(reporting._render_ui(ui))
    return out


@core.mcp.tool()
@reporting._guard
def loot(take: str = "all", who: str | None = None) -> str:
    """Take items from the open container/corpse window, then close it. take: "all", comma-separated item ids (from get_inventory, which lists the open container's contents), or "none" (close it without taking)."""
    if take.strip().lower() in ("none", "leave"):
        return plumbing._act("loot", "ui.loot", {"close": True}, context="client")
    if take.strip().lower() == "all":
        if time.monotonic() - _EMPTY_CLOSED[0] < 10 and not (core.game.try_rpc("ui.state", context="client") or {}).get(
                "container"):
            _EMPTY_CLOSED[0] = -1e9
            return "loot: done — nothing to take (the container was empty; interact closed its window)"
        # Right after interact/loot_corpse the container panel may still be opening (live S002: "no container
        # open" then the Backpack counted as already looted): give it up to ~2 s.
        for _ in range(8):
            try:
                return plumbing._act("loot", "ui.loot", {"all": True}, context="client")
            except GameError as e:
                if "no container open" not in str(e):
                    raise
                time.sleep(0.25)
        return plumbing._act("loot", "ui.loot", {"all": True}, context="client")
    char = plumbing._who(who)
    results = [plumbing._act("loot", "act.transfer", {"item": plumbing._uuid(t.strip()), "to": char})
               for t in take.split(",") if t.strip()]
    if (core.game.try_rpc("ui.state", context="client") or {}).get("container"):
        # S073: the take is done; the window would stay up for a hand ESCAPE
        r = core.game.run_job("ui.loot", {"close": True}, context="client", wait_s=5)
        results.append("(closed the container window)" if r.status == "done"
                       else f"(closing the container window failed: {r.error or r.status})")
    return "\n".join(results)


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    travel, combat, inventory, uitools, queueing, travel_macro,
)
