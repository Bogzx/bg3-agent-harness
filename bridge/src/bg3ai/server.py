"""bg3 MCP server: the agent's eyes and hands in Baldur's Gate 3.

Design rules (docs/ARCHITECTURE.md):
* Engine first. State is compact text from the mod's visibility-filtered ops, and actions are engine
  calls or UI commands. Screenshots are an explicit opt-in (`screenshot`, `zoom`) for ambiguity only.
* Fair. Everything comes through the mod's player-visible projections. GUIDs never reach the agent
  (short handles like e12 instead).
* Every result ends with what happened since the last call (events) and the session clock.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import render
from .game import Game, GameError, JobResult, NotReady
from .render import Handles

REPO = Path(__file__).resolve().parents[3]
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
PLAY_DIR = Path(os.environ.get("BG3AI_PLAY_DIR", REPO / "play"))
MEMORY = Path(os.environ.get("BG3AI_MEMORY_DIR", PLAY_DIR / "memory"))
SAVE_DIR = Path.home() / ".local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"
DEADLINE = float(os.environ.get("BG3AI_SESSION_DEADLINE", "0") or 0)
WRAPUP_S = 10 * 60

INSTRUCTIONS = """Tools to play Baldur's Gate 3 as the player. All information is what the player can see.
Start with get_status. Refer to things by their short id (e12) or a party member's name.
Action tools block until the action settles (arrived / interrupted / failed) and report events.
Use screenshot only when the text tools can't tell you something (spatial layout, puzzles, unknown popups)."""

mcp = MCPServer("bg3", instructions=INSTRUCTIONS)
game = Game(port=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
H = Handles()
_screen = None
_last_full_frame = None  # PIL image of the latest screenshot (for zoom)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def _log(tool: str, args: dict, result: str, ms: float) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME / "actions.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "tool": tool, "args": args, "ms": round(ms),
                                "result": result[:4000]}) + "\n")
    except OSError:
        pass


_clock_shown = {"at": 0.0}


def _clock_line() -> str:
    """The clock for tool results: always in the last 10 minutes, otherwise at most every 10 minutes."""
    if not DEADLINE:
        return ""
    now = time.time()
    if DEADLINE - now > WRAPUP_S and now - _clock_shown["at"] < 600:
        return ""
    _clock_shown["at"] = now
    return _clock()


def _clock() -> str:
    if not DEADLINE:
        return ""
    left = DEADLINE - time.time()
    if left <= 0:
        return "⏱ SESSION OVER: only save_game and note-taking now. Run the session-wrapup skill."
    m = int(left // 60)
    if left <= WRAPUP_S:
        return f"⏱ {m}m left: WRAP UP NOW. Reach a safe spot, save_game, then run the session-wrapup skill."
    return f"⏱ {m}m left"


def _session_over() -> bool:
    return bool(DEADLINE) and time.time() > DEADLINE


def _events_text() -> str:
    parts = []
    for ctx in ("server", "client"):
        try:
            evs = game.drain_events(ctx)
        except GameError:
            continue
        if evs:
            parts.append(render.render_events(evs, H))
    return "\n".join(p for p in parts if p)


_batch = {"active": False, "last": "done"}


def _finish(tool: str, args: dict, body: str, t0: float, events: bool = True) -> str:
    if _batch["active"]:
        # Inside do_actions: no per-step event drain or clock; the batch reports once at the end.
        text = H.scrub(body.rstrip())
        _log(tool, args, text, (time.monotonic() - t0) * 1000)
        if text.startswith(("FAILED", "NOT READY", "REFUSED")):
            _batch["last"] = "failed"
        return text
    out = [body.rstrip()]
    if events:
        ev = _events_text()
        if ev:
            out.append(ev)
    c = _clock_line()
    if c:
        out.append(c)
    text = H.scrub("\n".join(x for x in out if x))
    _log(tool, args, text, (time.monotonic() - t0) * 1000)
    return text


def _guard(fn):
    """Wrap a tool so game errors become readable text instead of exceptions."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        t0 = time.monotonic()
        try:
            return fn(*a, **kw)
        except NotReady as e:
            return _finish(fn.__name__, kw, f"NOT READY: {e}", t0, events=False)
        except (GameError, ValueError) as e:
            return _finish(fn.__name__, kw, f"FAILED: {e}", t0)
    return wrapper


def _uuid(ref: str) -> str:
    return H.resolve(ref)


def _active() -> str:
    st = game.rpc("state.status")
    a = st.get("active")
    if not a:
        raise GameError("no active character")
    return a


def _who(ref: str | None) -> str:
    return _uuid(ref) if ref else _active()


def _remember_names(obj: Any) -> None:
    """Register every uuid/name pair in a payload so later references by name work."""
    if isinstance(obj, dict):
        u = obj.get("uuid")
        if isinstance(u, str) and render.UUID_RE.search(u):
            H.get(u, obj.get("name"))
        for v in obj.values():
            _remember_names(v)
    elif isinstance(obj, list):
        for v in obj:
            _remember_names(v)


def _job_text(name: str, r: JobResult) -> str:
    if r.status == "done":
        s = f"{name}: done"
        if r.result not in (None, {}, []):
            s += " — " + render.compact(r.result, H)
    elif r.status == "still_running":
        s = f"{name}: still running (use wait to keep watching)"
    else:
        s = f"{name}: {r.status.upper()}"
        if r.error:
            s += f" — {r.error}"
        if r.result not in (None, {}, []):
            s += " — " + render.compact(r.result, H)
    return s


def _act(tool: str, op: str, args: dict, wait_s: float = 25.0, context: str = "server") -> str:
    t0 = time.monotonic()
    if _session_over():
        return _finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    r = game.run_job(op, args, context=context, wait_s=wait_s)
    _remember_names(r.result)
    _batch["last"] = r.status
    return _finish(tool, args, _job_text(tool, r), t0)


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------

_last_subregion = {"name": None}


@mcp.tool()
@_guard
def get_status() -> str:
    """Where you are, party HP/conditions, mode (exploring/combat/dialogue), what's on screen, and recent events. Call this first."""
    t0 = time.monotonic()
    ui = game.try_rpc("ui.state", context="client")
    try:
        st = game.rpc("state.status")
    except NotReady:
        gs = game.game_state()
        body = f"No game session loaded (game state: {gs})."
        if ui:
            body += "\n" + "\n".join(render.render_ui(ui, H))
        return _finish("get_status", {}, body, t0, events=False)
    _remember_names(st)
    sub = (ui or {}).get("region")
    if sub and sub != _last_subregion["name"]:
        # The minimap's subregion name only exists client-side; hand it to the server's breadcrumbs.
        if game.try_rpc("nav.set_subregion", {"name": sub}) is not None:
            _last_subregion["name"] = sub
    return _finish("get_status", {}, render.render_status(st, ui, H), t0)


@mcp.tool()
@_guard
def get_party() -> str:
    """Each party member's key numbers: HP, AC, level, conditions, resources (spell slots, actions), concentration."""
    t0 = time.monotonic()
    st = game.rpc("state.status")
    _remember_names(st)
    lines = []
    for c in st.get("party") or []:
        sheet = game.try_rpc("state.character", {"uuid": c["uuid"], "sections": ["brief", "resources"]})
        lines.append(render.character_line(c, H, st.get("active")))
        if sheet:
            skip = {"uuid", "name", "classes", "hp", "max_hp", "temp_hp", "level", "statuses", "pos"}
            lines.append("   " + render.compact({k: v for k, v in sheet.items() if k not in skip}, H))
    return _finish("get_party", {}, "\n".join(lines) or "No party?", t0)


@mcp.tool()
@_guard
def get_character(who: str | None = None, sections: str = "") -> str:
    """Character sheet. Full for party members (abilities, skills, resources, spell slots, equipment, passives); what Examine shows for others. who: id or party name (default: active character). sections: optional comma list to limit output, e.g. "resources,equipment"."""
    t0 = time.monotonic()
    args = {"uuid": _who(who)}
    if sections:
        args["sections"] = [s.strip() for s in sections.split(",") if s.strip()]
    res = game.rpc("state.character", args)
    _remember_names(res)
    return _finish("get_character", {"who": who, "sections": sections}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def get_actions(who: str | None = None) -> str:
    """The actions/spells/items a character can use right now (as on the hotbar), with costs and availability."""
    t0 = time.monotonic()
    res = game.rpc("state.actions", {"uuid": _who(who)})
    body = render.render_actions(res, H)
    return _finish("get_actions", {"who": who}, body, t0)


@mcp.tool()
@_guard
def get_nearby(radius: float = 25.0, kinds: str = "") -> str:
    """Visible characters and interactable objects near the party (ids, distance, relation, HP, locked/opened...), plus remembered last-seen positions. kinds: optional filter like "characters" or "items"."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"radius": radius}
    if kinds:
        args["kinds"] = [k.strip() for k in kinds.split(",") if k.strip()]
    res = game.rpc("state.nearby", args)
    _remember_names(res)
    return _finish("get_nearby", args, render.render_nearby(res, H), t0)


@mcp.tool()
@_guard
def look_at(target: str) -> str:
    """What the Examine panel shows for a visible character or object (stats, resistances, conditions, description)."""
    t0 = time.monotonic()
    res = game.rpc("state.entity", {"uuid": _uuid(target)})
    _remember_names(res)
    return _finish("look_at", {"target": target}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def get_inventory(who: str | None = None, filter: str = "") -> str:
    """Party inventory (or one member's): equipment, notable items (story, magic, consumables, tools, bags) and a junk summary; plus the open container. filter: "equipped" | "consumables" | "all" | words to match names (e.g. "potion")."""
    t0 = time.monotonic()
    args: dict[str, Any] = {}
    if who:
        args["uuid"] = _uuid(who)
    if filter:
        args["filter"] = filter
    args.pop("filter", None)  # filtering happens in the renderer
    _remember_names(game.try_rpc("state.status") or {})  # party first: short handles + owner names
    res = game.rpc("state.inventory", args)
    _remember_names(res)
    return _finish("get_inventory", {"who": who, "filter": filter}, render.render_inventory(res, H, filter), t0)


@mcp.tool()
@_guard
def get_journal(quest: str = "") -> str:
    """Active quests with their latest journal entry, plus finished quest titles. quest: words from a title to see all of that quest's entries."""
    t0 = time.monotonic()
    res = game.rpc("state.journal")
    return _finish("get_journal", {"quest": quest}, render.render_journal(res, H, quest), t0)


@mcp.tool()
@_guard
def get_map() -> str:
    """Current region/subregion, unlocked waypoints, and your own map markers."""
    t0 = time.monotonic()
    res = game.rpc("state.map")
    body = render.compact(res, H)
    markers = _load_markers()
    if markers:
        body += "\nMARKERS\n" + "\n".join(f"  {m['id']} {m['label']} @{render.pos(m.get('pos'))} ({m.get('region', '')})" for m in markers)
    return _finish("get_map", {}, body, t0)


@mcp.tool()
@_guard
def get_frontier() -> str:
    """Leads worth exploring: visible doors, ladders, stairs, exits and unvisited things near you, with distance and direction. Use it to decide where to go next."""
    t0 = time.monotonic()
    res = game.rpc("nav.frontier", {})
    _remember_names(res)
    return _finish("get_frontier", {}, render.render_frontier(res, H), t0)


@mcp.tool()
@_guard
def get_explored() -> str:
    """Where the party has been in this area (your own breadcrumbs): size of explored region and recent named places."""
    t0 = time.monotonic()
    res = game.rpc("nav.explored", {})
    return _finish("get_explored", {}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def path_preview(target: str = "", x: float | None = None, y: float | None = None, z: float | None = None,
                 who: str | None = None) -> str:
    """Walking distance to a target id or x,y,z: reachable?, path length, needs a jump?, fits remaining movement? Use before committing combat movement."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": _who(who)}
    if target:
        args["target"] = _uuid(target)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id> or x,y,z")
    res = game.rpc("nav.path", args)
    return _finish("path_preview", {"target": target}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def get_combat_state() -> str:
    """Turn order (visible combatants), whose turn it is, round, and the active character's remaining action economy."""
    t0 = time.monotonic()
    res = game.rpc("state.combat")
    _remember_names(res)
    if isinstance(res, dict) and not res.get("active", True) and len(res) <= 1:
        return _finish("get_combat_state", {}, "Not in combat.", t0)
    body = render.compact(res, H)
    # One read should be enough to plan the whole turn: add the acting party member's actions.
    cur = res.get("current") if isinstance(res, dict) else None
    st = game.try_rpc("state.status") or {}
    party = {c.get("uuid") for c in st.get("party") or []}
    if cur in party:
        acts = game.try_rpc("state.actions", {"uuid": cur})
        if acts:
            body += "\nYOUR ACTIONS (" + H.get(cur) + ")\n" + render.render_actions(acts, H)
    return _finish("get_combat_state", {}, body, t0)


@mcp.tool()
@_guard
def get_dialogue() -> str:
    """The current dialogue line and numbered answer options (with skill-check tags), or the roll/reaction/popup on screen."""
    t0 = time.monotonic()
    ui = game.rpc("ui.state", context="client")
    lines = render.render_ui(ui, H)
    return _finish("get_dialogue", {}, "\n".join(lines) or f"No dialogue on screen (screen={ui.get('screen')}).", t0)


@mcp.tool()
@_guard
def get_events() -> str:
    """Everything that happened since your last tool call (combat log, quest updates, items, rolls)."""
    t0 = time.monotonic()
    return _finish("get_events", {}, "", t0) or "No new events."


@mcp.tool()
@_guard
def screenshot(scale: float = 0.5) -> Image | str:
    """Picture of the game screen exactly as the player sees it. Costly: use only when text tools can't answer (layout, puzzles, unknown popups). scale: 0.25-1.0 of 2560px width."""
    global _screen, _last_full_frame
    from .screen import ScreenCapture

    if _screen is None:
        _screen = ScreenCapture(RUNTIME / "shots")
    width = int(2560 * max(0.25, min(scale, 1.0)))
    jpg, full = _screen.capture(max_width=width)
    _last_full_frame = full
    _log("screenshot", {"scale": scale}, f"{full.size}", 0)
    return Image(data=jpg, format="jpeg")


@mcp.tool()
@_guard
def zoom(x: float, y: float, w: float, h: float) -> Image | str:
    """Crop of the LAST screenshot at full resolution. x,y,w,h are fractions 0-1 of the screen (e.g. 0.3,0.7,0.4,0.3 = bottom-middle dialogue box)."""
    import io

    if _last_full_frame is None:
        return "Take a screenshot first."
    W, Hh = _last_full_frame.size
    box = (int(x * W), int(y * Hh), int((x + w) * W), int((y + h) * Hh))
    crop = _last_full_frame.crop(box)
    if crop.width > 1600:
        crop = crop.resize((1600, round(crop.height * 1600 / crop.width)))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


# ---------------------------------------------------------------------------
# act: world
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def move_to(target: str = "", x: float | None = None, y: float | None = None, z: float | None = None,
            who: str | None = None, run: bool = True) -> str:
    """Walk to a visible entity (target=id) or a position (x,y,z). Stops early if combat/dialogue starts. who defaults to the active character (chained companions follow)."""
    args: dict[str, Any] = {"char": _who(who), "run": run}
    if target:
        args["target"] = _uuid(target)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id> or x,y,z")
    return _act("move_to", "act.move_to", args)


@mcp.tool()
@_guard
def interact(target: str, who: str | None = None) -> str:
    """Use/open/activate a visible object: door, lever, chest, corpse (opens loot), book, ladder, waypoint. Walks there first."""
    return _act("interact", "act.interact", {"char": _who(who), "target": _uuid(target)})


@mcp.tool()
@_guard
def pick_up(item: str, who: str | None = None) -> str:
    """Pick up a visible item from the ground."""
    return _act("pick_up", "act.pickup", {"char": _who(who), "item": _uuid(item)})


_inp_client = {"c": None}


def _inp():
    from .input import InputClient
    if _inp_client["c"] is None:
        try:
            c = InputClient()
            c.agent(True)
            _inp_client["c"] = c
        except Exception:
            return None
    return _inp_client["c"]


def _click_act(tool: str, op: str, args: dict, wait_s: float = 40.0) -> str:
    """Server op that walks up and then needs a verified click on the target (engine has no direct call)."""
    from .clicker import run_click_action

    t0 = time.monotonic()
    if _session_over():
        return _finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    r = run_click_action(game, _inp(), op, args, wait_s)
    _remember_names(r.result)
    _batch["last"] = r.status
    return _finish(tool, args, _job_text(tool, r), t0)


@mcp.tool()
@_guard
def loot_corpse(corpse: str, who: str | None = None) -> str:
    """Walk to a visible dead body and open its loot window (then use loot)."""
    return _click_act("loot_corpse", "act.loot_corpse", {"char": _who(who), "target": _uuid(corpse)})


@mcp.tool()
@_guard
def lockpick(target: str, who: str | None = None) -> str:
    """Pick the lock of a visible locked door/chest (needs Thieves' Tools); opens the roll — then roll_check."""
    return _act_click_roll("lockpick", "act.lockpick", {"char": _who(who), "target": _uuid(target)})


@mcp.tool()
@_guard
def disarm(trap: str, who: str | None = None) -> str:
    """Disarm a DISCOVERED trap (needs a Trap Disarm Toolkit); opens the roll — then roll_check."""
    return _act_click_roll("disarm", "act.disarm", {"char": _who(who), "target": _uuid(trap)})


@mcp.tool()
@_guard
def pickpocket(npc: str, who: str | None = None) -> str:
    """Sneak up to a visible NPC and open the pickpocket window (best while sneaking and unseen). Crime if caught."""
    return _click_act("pickpocket", "act.pickpocket", {"char": _who(who), "target": _uuid(npc)})


@mcp.tool()
@_guard
def click_on(target: str, button: str = "left") -> str:
    """Last-resort: point the mouse at a visible entity (verified hover) and click it, for interactions no other tool covers. Logged."""
    from .clicker import click_entity

    t0 = time.monotonic()
    inp = _inp()
    if inp is None:
        raise GameError("input shim not available")
    cr = click_entity(game, inp, _uuid(target), "R" if button.lower().startswith("r") else "L")
    return _finish("click_on", {"target": target}, ("clicked" if cr.ok else f"FAILED: {cr.reason}") + " — " + render.compact(cr.as_dict(), H), t0)


def _act_click_roll(tool: str, op: str, args: dict) -> str:
    out = _click_act(tool, op, args)
    if _batch["last"] == "done" and not _batch["active"]:
        ui = game.try_rpc("ui.state", context="client") or {}
        if ui.get("roll"):
            out += "\n" + "\n".join(render.render_ui(ui, H))
    return out


@mcp.tool()
@_guard
def loot(take: str = "all", who: str | None = None) -> str:
    """Take items from the open container/corpse window. take: "all", or comma-separated item ids (from get_inventory, which lists the open container's contents)."""
    if take.strip().lower() == "all":
        return _act("loot", "ui.loot", {"all": True}, context="client")
    char = _who(who)
    results = [_act("loot", "act.transfer", {"item": _uuid(t.strip()), "to": char})
               for t in take.split(",") if t.strip()]
    return "\n".join(results)


@mcp.tool()
@_guard
def talk_to(npc: str, who: str | None = None) -> str:
    """Walk up to a visible character and start a conversation. Then use get_dialogue / choose_dialogue."""
    return _act("talk_to", "act.talk", {"char": _who(who), "npc": _uuid(npc)}, wait_s=30)


@mcp.tool()
@_guard
def jump_to(x: float, y: float, z: float, who: str | None = None) -> str:
    """Jump to a position (costs movement/bonus action in combat)."""
    return _act("jump_to", "act.cast", {"char": _who(who), "spell": "Projectile_Jump", "pos": [x, y, z]})


@mcp.tool()
@_guard
def set_party(action: str, who: str | None = None, leader: str | None = None) -> str:
    """Party control. action: select (control 'who'), chain (link 'who' to 'leader'), unchain (detach 'who'), sneak_on, sneak_off, turn_based_on, turn_based_off."""
    a = action.lower()
    if a == "select":
        return _act("set_party", "act.select", {"char": _who(who)})
    if a == "chain":
        return _act("set_party", "act.chain", {"char": _who(who), "leader": _who(leader) if leader else _active()})
    if a == "unchain":
        return _act("set_party", "act.unchain", {"char": _who(who)})
    if a in ("sneak_on", "sneak_off"):
        return _act("set_party", "act.sneak", {"char": _who(who), "on": a == "sneak_on"})
    if a in ("turn_based_on", "turn_based_off"):
        return _act("set_party", "act.turn_based", {"on": a == "turn_based_on"})
    raise ValueError("unknown action")


@mcp.tool()
@_guard
def fast_travel(waypoint: str) -> str:
    """Travel to an unlocked waypoint (name or id from get_map). Not possible in combat."""
    return _act("fast_travel", "ui.fast_travel", {"waypoint": waypoint}, context="client", wait_s=60)


@mcp.tool()
@_guard
def camp(action: str = "go") -> str:
    """action: go (travel to camp, like the camp button) | leave (return to where you were)."""
    if action == "leave":
        return _act("camp", "act.leave_camp", {}, wait_s=60)
    return _act("camp", "act.go_to_camp", {}, wait_s=60)


# ---------------------------------------------------------------------------
# act: dialogue and rolls
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def choose_dialogue(option: int) -> str:
    """Pick dialogue answer number `option` (as numbered by get_dialogue). Returns the next line/options."""
    t0 = time.monotonic()
    r = game.run_job("ui.dialogue_choose", {"i": option}, context="client", wait_s=20)
    time.sleep(0.3)
    ui = game.try_rpc("ui.state", context="client") or {}
    body = _job_text("choose_dialogue", r) + "\n" + "\n".join(render.render_ui(ui, H))
    return _finish("choose_dialogue", {"option": option}, body, t0)


@mcp.tool()
@_guard
def roll_check(boosts: str = "", reroll_on_fail: bool = False) -> str:
    """Resolve the skill-check roll on screen: optionally add boosts shown on the roll screen (comma list, e.g. "Guidance"), roll, and continue."""
    args: dict[str, Any] = {"reroll": reroll_on_fail}
    if boosts:
        args["boosts"] = [b.strip() for b in boosts.split(",") if b.strip()]
    return _act("roll_check", "ui.roll", args, context="client", wait_s=20)


@mcp.tool()
@_guard
def react(use: bool, which: str = "") -> str:
    """Answer a reaction prompt (Shield, Counterspell, Opportunity Attack...). use=true to react."""
    args: dict[str, Any] = {"use": use}
    if which:
        args["which"] = which
    return _act("react", "ui.react", args, context="client", wait_s=15)


# ---------------------------------------------------------------------------
# act: combat
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def use_ability(ability: str, target: str = "", x: float | None = None, y: float | None = None,
                z: float | None = None, who: str | None = None, upcast: int = 0, targets: str = "") -> str:
    """Use an action/spell/attack/item from get_actions: at a target id, at a position (x,y,z), or on self. upcast: spell slot level to cast at. targets: comma list for multi-target spells. Costs and rules are the game's."""
    args: dict[str, Any] = {"char": _who(who), "spell": ability}
    if target:
        args["target"] = _uuid(target)
    if targets:
        args["targets"] = [_uuid(t) for t in targets.split(",") if t.strip()]
    if None not in (x, y, z):
        args["pos"] = [x, y, z]
    if upcast:
        args["upcast"] = upcast
    return _act("use_ability", "act.cast", args, wait_s=30)


@mcp.tool()
@_guard
def end_turn(who: str | None = None) -> str:
    """End the current character's turn; blocks through enemy turns until a party member can act or combat ends."""
    return _act("end_turn", "act.end_turn", {"char": _who(who)}, wait_s=90)


@mcp.tool()
@_guard
def end_combat_report(what_worked: str, what_to_change: str, next_goal: str) -> str:
    """Required after each fight: a short reflection that is appended to your session log."""
    t0 = time.monotonic()
    path = _session_log()
    if path:
        with open(path, "a") as f:
            f.write(f"- {time.strftime('%H:%M')} COMBAT REPORT worked: {what_worked} | change: {what_to_change} | next: {next_goal}\n")
    return _finish("end_combat_report", {}, f"Logged to {path.name if path else '(no session log)'}. Now loot and heal up.", t0)


# ---------------------------------------------------------------------------
# act: items, rest, progression
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def manage_inventory(op: str, item: str, who: str | None = None, to: str | None = None) -> str:
    """op: equip | unequip | use (drink/read/eat) | give (to another party member: to=name) | camp (send to camp chest)."""
    o = op.lower()
    if o == "equip":
        return _act("manage_inventory", "act.equip", {"char": _who(who), "item": _uuid(item)})
    if o == "unequip":
        return _act("manage_inventory", "act.unequip", {"char": _who(who), "item": _uuid(item)})
    if o == "use":
        return _act("manage_inventory", "act.cast", {"char": _who(who), "item": _uuid(item)})
    if o == "give":
        return _act("manage_inventory", "act.transfer", {"item": _uuid(item), "to": _uuid(to) if to else None})
    if o == "camp":
        return _act("manage_inventory", "act.transfer", {"item": _uuid(item), "to": "camp"})
    raise ValueError("op must be equip|unequip|use|give|camp")


@mcp.tool()
@_guard
def short_rest() -> str:
    """Short rest (2 per long rest): restores some HP and short-rest resources."""
    return _act("short_rest", "ui.short_rest", {}, context="client", wait_s=20)


@mcp.tool()
@_guard
def long_rest() -> str:
    """Long rest at camp (go there with camp first): consumes camp supplies, restores everything, may trigger camp scenes (handle them as dialogues)."""
    return _act("long_rest", "ui.long_rest", {}, context="client", wait_s=90)


@mcp.tool()
@_guard
def get_level_up_options(who: str | None = None) -> str:
    """What the level-up screen offers for a character (class/subclass, feats or ability points, spells, skills)."""
    t0 = time.monotonic()
    res = game.rpc("ui.level_up_options", {"uuid": _who(who)}, context="client")
    return _finish("get_level_up_options", {"who": who}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def level_up(who: str | None = None, plan: str = "") -> str:
    """Level a character up. plan: JSON like {"class":"Paladin","subclass":"Devotion","feat":"Ability Improvement","abilities":{"Strength":2},"spells":["Bless"],"skills":[]}; omit fields to take the game's defaults/recommendations."""
    args: dict[str, Any] = {"uuid": _who(who)}
    if plan:
        args["plan"] = json.loads(plan)
    return _act("level_up", "ui.level_up", args, context="client", wait_s=60)


@mcp.tool()
@_guard
def trade(npc: str = "", offer: str = "", request: str = "", confirm: bool = False) -> str:
    """Barter with a trader. Open with npc=<id> (starts the conversation's trade), then list items via get_inventory; offer/request: comma lists of item ids to move; confirm=true to finish the deal."""
    args: dict[str, Any] = {"confirm": confirm}
    if npc:
        args["npc"] = _uuid(npc)
    if offer:
        args["offer"] = [_uuid(i) for i in offer.split(",") if i.strip()]
    if request:
        args["request"] = [_uuid(i) for i in request.split(",") if i.strip()]
    return _act("trade", "ui.trade", args, context="client", wait_s=20)


@mcp.tool()
@_guard
def ability_preview(ability: str, target: str, who: str | None = None) -> str:
    """What the game shows when targeting: hit chance / save DC, damage preview, range and line of sight."""
    t0 = time.monotonic()
    res = game.rpc("state.preview", {"char": _who(who), "spell": ability, "target": _uuid(target)})
    return _finish("ability_preview", {"ability": ability, "target": target}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def ui_action(command: str, widget: str = "", param: str = "") -> str:
    """Escape hatch for screens without a dedicated tool (level up, trade, character creation, popups): run a named UI command on the current screen, as get_dialogue/ui dump lists them."""
    args: dict[str, Any] = {"command": command}
    if widget:
        args["widget"] = widget
    if param:
        args["param"] = param
    return _act("ui_action", "ui.command", args, context="client", wait_s=15)


@mcp.tool()
@_guard
def dismiss_popups() -> str:
    """Close informational popups (tutorials, info boxes, reward/book panels). Decision popups are never closed; they're listed so you can choose with ui_action."""
    t0 = time.monotonic()
    res = game.rpc("ui.dismiss", {}, context="client")
    return _finish("dismiss_popups", {}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def set_reactions(who: str | None = None, settings: str = "") -> str:
    """See or change a character's reaction prompts (e.g. Shield, Divine Smite, Opportunity Attack). settings: JSON {"<reaction name>": "never"|"always"|"ask"}; empty = list current."""
    args: dict[str, Any] = {"char": _who(who)}
    if settings:
        args["set"] = json.loads(settings)
    t0 = time.monotonic()
    res = game.rpc("ui.reactions", args, context="client")
    _remember_names(res)
    return _finish("set_reactions", {"who": who}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def ui_dump(text_only: bool = True) -> str:
    """Text dump of what's on screen in the UI (visible text, buttons and their commands). Cheaper than a screenshot."""
    t0 = time.monotonic()
    res = game.rpc("ui.dump", {"text_only": text_only}, context="client")
    body = res if isinstance(res, str) else render.compact(res, H)
    return _finish("ui_dump", {}, body[:6000], t0)


@mcp.tool()
@_guard
def press_key(key: str) -> str:
    """Press a keyboard key in the game (SDL scancode name, e.g. ESCAPE, SPACE, RETURN, F5, 1). Prefer dedicated tools."""
    return _act("press_key", "ui.key", {"key": key.upper()}, context="client", wait_s=5)


@mcp.tool()
@_guard
def click(x: float, y: float, button: str = "left") -> str:
    """Last-resort mouse click at a screen position given as fractions 0-1 of the latest screenshot."""
    t0 = time.monotonic()
    inp = _inp()
    if inp is None:
        raise GameError("input shim not available")
    w, h = inp.window_size()
    inp.click(int(x * w), int(y * h), "R" if button.lower().startswith("r") else "L")
    time.sleep(0.3)
    return _finish("click", {"x": x, "y": y}, f"clicked {button} at {int(x * w)},{int(y * h)}", t0)



# ---------------------------------------------------------------------------
# starting a playthrough
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def new_game(difficulty: str = "Explorer") -> str:
    """From the main menu: start a new game at the given difficulty (Explorer | Balanced | Tactician) and open character creation."""
    if not PLAYTHROUGH:
        raise GameError("new_game is only available in an AI playthrough session")
    return _act("new_game", "ui.new_game", {"difficulty": difficulty}, context="client", wait_s=90)


@mcp.tool()
@_guard
def character_creation_options() -> str:
    """What the character creation screen offers right now: origins, races/subraces, classes/subclasses, backgrounds, ability points, skill and spell picks, current step."""
    t0 = time.monotonic()
    res = game.rpc("ui.cc_options", {}, context="client")
    return _finish("character_creation_options", {}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def create_character(plan: str) -> str:
    """Fill in character creation and finish it. plan: JSON, e.g. {"race":"Half-Orc","class":"Paladin","subclass":"Oath of Devotion","background":"Soldier","abilities":"recommended","name":"Claude"}; or {"origin":"Lae'zel"}. Omitted fields keep the game's defaults/recommendations."""
    return _act("create_character", "ui.cc_apply", {"plan": json.loads(plan)}, context="client", wait_s=110)


# ---------------------------------------------------------------------------
# batch: several actions per decision
# ---------------------------------------------------------------------------

_BATCHABLE = {
    "move_to", "interact", "pick_up", "loot", "loot_corpse", "talk_to", "jump_to", "set_party", "use_ability",
    "end_turn", "choose_dialogue", "roll_check", "react", "manage_inventory", "wait", "press_key",
    "place_marker", "camp", "short_rest",
}


@mcp.tool()
def do_actions(actions: str) -> str:
    """Run several actions in order in ONE call: a whole combat turn, a looting sweep, a dialogue chain. Plan from one observation, then execute.
    actions: JSON list of steps, each {"tool": <name>, ...that tool's arguments}, e.g.
    [{"tool":"move_to","target":"e12"},{"tool":"use_ability","ability":"Target_MainHandAttack","target":"e12"},{"tool":"use_ability","ability":"Shout_SecondWind"},{"tool":"end_turn"}]
    Allowed: move_to, interact, pick_up, loot, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
    Stops at the first step that fails or is interrupted (combat/dialogue started, reaction prompt, target gone) and reports what ran, plus all events."""
    t0 = time.monotonic()
    try:
        steps = json.loads(actions)
        if isinstance(steps, dict):
            steps = steps.get("actions", [steps])
        assert isinstance(steps, list) and steps
    except Exception:
        return "FAILED: actions must be a JSON list of {\"tool\": ..., args...}"
    if len(steps) > 20:
        return "FAILED: at most 20 steps per batch"
    lines = []
    _batch["active"] = True
    try:
        for i, step in enumerate(steps, 1):
            step = dict(step)
            name = step.pop("tool", None)
            if name not in _BATCHABLE:
                lines.append(f"{i}. {name}: not allowed in a batch; stopped")
                break
            fn = globals()[name]
            _batch["last"] = "done"
            try:
                text = fn(**step)
            except TypeError as e:
                lines.append(f"{i}. {name}: bad arguments ({e}); stopped")
                break
            lines.append(f"{i}. {text}")
            if _batch["last"] != "done":
                if i < len(steps):
                    lines.append(f"stopped after step {i} ({_batch['last']}); {len(steps) - i} step(s) not run")
                break
    finally:
        _batch["active"] = False
    return _finish("do_actions", {"n": len(steps)}, "\n".join(lines), t0)


# ---------------------------------------------------------------------------
# meta: saves, waiting, markers, wiki, clock
# ---------------------------------------------------------------------------

SAVE_PREFIX = os.environ.get("BG3AI_SAVE_PREFIX", "")
PLAYTHROUGH = os.environ.get("BG3AI_PLAYTHROUGH", "")


def _enable_save_gates() -> None:
    """Saving/loading is gated off in the mod by default (protects a human's own playthrough). The runner
    sets BG3AI_PLAYTHROUGH only for the AI's own game, so only then does the agent get save/load."""
    if not PLAYTHROUGH:
        raise GameError("saving/loading is disabled outside an AI playthrough session (BG3AI_PLAYTHROUGH unset)")
    game.rpc("ui.config", {"save_enabled": True, "load_enabled": True}, context="client")


@mcp.tool()
@_guard
def save_game(label: str = "") -> str:
    """Save the game. label: short name for a named save (e.g. "before-gauntlet"); empty = quicksave."""
    t0 = time.monotonic()
    _enable_save_gates()
    if not label:
        r = game.run_job("ui.menu", {"action": "quicksave"}, context="client", wait_s=30)
        return _finish("save_game", {"label": label}, _job_text("save_game", r), t0)
    label = re.sub(r"[^A-Za-z0-9 _.-]", "", label)[:40] or "save"
    before = {d.name for d in SAVE_DIR.iterdir()} if SAVE_DIR.is_dir() else set()
    r = game.run_job("ui.menu", {"action": "save", "name": label}, context="client", wait_s=30)
    if r.status == "done" and isinstance(r.result, dict) and r.result.get("needs_text"):
        # The name box only takes real keyboard text: type it through the input shim, then confirm.
        inp = _inp()
        if inp is None:
            raise GameError("named saves need the input shim; use save_game() for a quicksave")
        inp.text(label)
        inp.key("RETURN")
        r = game.run_job("ui.menu", {"action": "save_confirm", "name": label}, context="client", wait_s=40)
    # Confirm on disk: a new save folder ending in __<label> (the load menu shows it the same way).
    deadline = time.monotonic() + 15
    found = None
    while time.monotonic() < deadline and not found:
        if SAVE_DIR.is_dir():
            found = next((d.name for d in SAVE_DIR.iterdir() if d.name not in before and d.name.endswith("__" + label)), None)
        if not found:
            time.sleep(0.3)
    body = _job_text("save_game", r) + (f"\nsaved as '{label}'" if found else "\nWARNING: no save folder appeared; try save_game() (quicksave)")
    return _finish("save_game", {"label": label}, body, t0)


@mcp.tool()
@_guard
def list_saves(limit: int = 10) -> str:
    """Your most recent saves (as the load menu shows them)."""
    t0 = time.monotonic()
    rows = []
    if SAVE_DIR.is_dir():
        for d in SAVE_DIR.iterdir():
            if d.is_dir() and (d.name.startswith(SAVE_PREFIX) if SAVE_PREFIX else not PLAYTHROUGH):
                rows.append((d.stat().st_mtime, d.name))
    rows.sort(reverse=True)
    body = "\n".join(f"{time.strftime('%m-%d %H:%M', time.localtime(t))}  {n.split('__', 1)[-1]}  [{n}]" for t, n in rows[:limit])
    return _finish("list_saves", {}, body or "No saves.", t0, events=False)


@mcp.tool()
@_guard
def load_game(save: str = "latest") -> str:
    """Load one of YOUR saves (folder name from list_saves, or "latest"). Use after a party wipe or a clear mistake."""
    t0 = time.monotonic()
    _enable_save_gates()
    mine = sorted((d for d in SAVE_DIR.iterdir() if d.is_dir() and SAVE_PREFIX and d.name.startswith(SAVE_PREFIX)),
                  key=lambda d: d.stat().st_mtime, reverse=True) if SAVE_DIR.is_dir() else []
    if save == "latest":
        if not mine:
            raise GameError("no saves of this playthrough found (BG3AI_SAVE_PREFIX unset or no saves yet)")
        save = mine[0].name
    elif not any(d.name == save or d.name.endswith("__" + save) for d in mine):
        raise GameError(f"'{save}' is not one of this playthrough's saves; see list_saves")
    else:
        save = next(d.name for d in mine if d.name == save or d.name.endswith("__" + save))
    r = game.run_job("ui.menu", {"action": "load", "name": save}, context="client", wait_s=120)
    game.event_seq = {"server": 0, "client": 0}
    return _finish("load_game", {"save": save}, _job_text("load_game", r), t0, events=False)


@mcp.tool()
@_guard
def wait(seconds: float = 3.0) -> str:
    """Let game time pass (cutscenes, NPC turns, a still-running action) and report what happened."""
    t0 = time.monotonic()
    time.sleep(max(0.2, min(seconds, 30.0)))
    return _finish("wait", {"seconds": seconds}, f"waited {seconds}s", t0)


def _load_markers() -> list[dict]:
    p = MEMORY / "markers.json"
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else data.get("markers", [])
    except (OSError, json.JSONDecodeError):
        return []


@mcp.tool()
@_guard
def place_marker(label: str, target: str = "", x: float | None = None, y: float | None = None,
                 z: float | None = None) -> str:
    """Remember a place on your own map (persists across sessions): at a visible entity's position or x,y,z."""
    t0 = time.monotonic()
    st = game.rpc("state.status")
    if target:
        ent = game.rpc("state.entity", {"uuid": _uuid(target)})
        p = ent.get("pos")
    elif None not in (x, y, z):
        p = [x, y, z]
    else:
        me = next((c for c in st.get("party", []) if c.get("uuid") == st.get("active")), {})
        p = me.get("pos")
    markers = _load_markers()
    mid = f"m{max([int(m['id'][1:]) for m in markers if re.fullmatch(r'm\d+', str(m.get('id')))] + [0]) + 1}"
    markers.append({"id": mid, "label": label, "pos": p, "region": st.get("region") or st.get("level"),
                    "level": st.get("level"), "t": time.strftime("%Y-%m-%d %H:%M")})
    MEMORY.mkdir(parents=True, exist_ok=True)
    (MEMORY / "markers.json").write_text(json.dumps(markers, indent=1))
    return _finish("place_marker", {"label": label}, f"marker {mid} '{label}' @{render.pos(p)}", t0)


@mcp.tool()
@_guard
def delete_marker(marker_id: str) -> str:
    """Delete one of your map markers."""
    t0 = time.monotonic()
    markers = [m for m in _load_markers() if m.get("id") != marker_id]
    (MEMORY / "markers.json").write_text(json.dumps(markers, indent=1))
    return _finish("delete_marker", {"id": marker_id}, f"deleted {marker_id}", t0, events=False)


@mcp.tool()
def wiki_search(query: str, limit: int = 5) -> str:
    """Search bg3.wiki (general game knowledge, like a player looking things up). Tag what you learn [wiki]."""
    from .wiki import format_search, wiki_search as ws

    try:
        return format_search(ws(query, limit))
    except Exception as e:  # network errors etc.
        return f"wiki unavailable: {e}"


@mcp.tool()
def wiki_page(title: str, section: str = "", max_chars: int = 5000) -> str:
    """Read a bg3.wiki page (or one section) as plain text."""
    from .wiki import format_page, wiki_page as wp

    try:
        return format_page(wp(title, section or None, max_chars))
    except Exception as e:
        return f"wiki unavailable: {e}"


@mcp.tool()
def session_clock() -> str:
    """Time left in this play session."""
    return _clock() or "No session deadline set."


def _session_log() -> Path | None:
    sess = os.environ.get("BG3AI_SESSION")
    d = MEMORY / "sessions"
    if sess:
        return d / f"{sess}.md"
    logs = sorted(d.glob("S*.md")) if d.is_dir() else []
    return logs[-1] if logs else None


def _movie_skipper(stop: "threading.Event") -> None:
    """Cutscenes are always skipped (user requirement). The mod's AutoSkip handles dialogue lines in-game;
    Bink movies only react to real input, so this watcher sends ESCAPE through the input shim when the
    UI reports a movie for two consecutive polls. Uses its own connection; never touches other screens."""
    watcher = Game(port=game.port)
    seen = 0
    while not stop.wait(2.0):
        try:
            st = watcher.rpc("ui.state", context="client", timeout=3.0)
        except Exception:
            seen = 0
            continue
        if st.get("screen") == "movie":
            seen += 1
            if seen >= 2:
                inp = _inp()
                if inp is not None:
                    try:
                        inp.key("ESCAPE")
                        _log("autoskip", {}, "movie skipped (ESCAPE via shim)", 0)
                    except Exception:
                        pass
                seen = 0
                stop.wait(1.0)
        else:
            seen = 0


def main() -> None:
    import threading

    stop = threading.Event()
    if os.environ.get("BG3AI_MOVIE_SKIP", "1") != "0":
        threading.Thread(target=_movie_skipper, args=(stop,), name="movie-skipper", daemon=True).start()
    try:
        mcp.run("stdio")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
