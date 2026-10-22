"""wait, markers, wiki, session_clock, the movie skipper, alwaysLoad marks, plain compacted results and capability
removal.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from mcp.server.mcpserver import MCPServer

from .. import capabilities, compact, render, rules
from ..game import Game
from . import batch, core, plumbing, queueing, reporting, world_tools


@core.mcp.tool()
@reporting._guard
def wait(seconds: float = 3.0) -> str:
    """Let game time pass (cutscenes, NPC turns) and report what happened. Returns early on the first notable event (combat/turn/dialogue start or end, a job or queue finishing). To follow an action, prefer wait_for."""
    t0 = time.monotonic()
    end = t0 + max(0.2, min(seconds, 30.0))
    hit = None
    while time.monotonic() < end and hit is None:
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
        hit = queueing._peek_events(_WAIT_KINDS)
    waited = render.num(round(time.monotonic() - t0, 1))
    body = f"{hit.get('kind')} after {waited}s" if hit else f"waited {waited}s"
    return reporting._finish("wait", {"seconds": seconds}, body, t0)


_WAIT_KINDS = {"combat_started", "combat_ended", "turn_started", "dialogue_started", "dialogue_ended",
               "job_finished", "queue_done", "queue_interrupted"}


def _load_markers() -> list[dict]:
    p = core.MEMORY / "markers.json"
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else data.get("markers", [])
    except (OSError, json.JSONDecodeError):
        return []


@core.mcp.tool()
@reporting._guard
def place_marker(label: str, target: str = "", x: float | None = None, y: float | None = None,
                 z: float | None = None) -> str:
    """Remember a place on your own map (persists across sessions): at a visible entity's position or x,y,z."""
    t0 = time.monotonic()
    st = core.game.rpc("state.status")
    if target:
        ent = core.game.rpc("state.entity", {"uuid": plumbing._uuid(target)})
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
    core.MEMORY.mkdir(parents=True, exist_ok=True)
    (core.MEMORY / "markers.json").write_text(json.dumps(markers, indent=1))
    return reporting._finish("place_marker", {"label": label}, f"marker {mid} '{label}' @{render.pos(p)}", t0)


@core.mcp.tool()
@reporting._guard
def delete_marker(marker_id: str) -> str:
    """Delete one of your map markers."""
    t0 = time.monotonic()
    markers = [m for m in _load_markers() if m.get("id") != marker_id]
    (core.MEMORY / "markers.json").write_text(json.dumps(markers, indent=1))
    return reporting._finish("delete_marker", {"id": marker_id}, f"deleted {marker_id}", t0, events=False)


@core.mcp.tool()
def wiki_search(query: str, limit: int = 5) -> str:
    """Search bg3.wiki (general game knowledge, like a player looking things up). Tag what you learn [wiki]."""
    from ..wiki import format_search
    from ..wiki import wiki_search as ws

    try:
        return format_search(ws(query, limit))
    except Exception as e:  # network errors etc.
        return f"wiki unavailable: {e}"


@core.mcp.tool()
def wiki_page(title: str, section: str = "", max_chars: int = 2500) -> str:
    """Read a bg3.wiki page (or one section) as plain text. A whole page is cut at max_chars and lists its sections: read the one you need with section=. For a puzzle, recall(query) and get_nearby(kinds="props") first."""
    from ..wiki import format_page
    from ..wiki import wiki_page as wp

    try:
        return format_page(wp(title, section or None, max_chars))
    except Exception as e:
        return f"wiki unavailable: {e}"


@core.mcp.tool()
def session_clock() -> str:
    """The session clock: the same ⏱ line every tool result ends with. It says WRAP UP NOW by itself when it is time;
    until then there is no time to watch."""
    line = core._clock()  # includes the stuck signal, if any
    if not line:
        return "No session deadline set."
    if core.DEADLINE and core.FIGHT_LINE not in line and core._times()[0] > time.time():
        # S073 polled session_clock 10 times in its last 2.5 min of play, waiting for WRAP UP instead of playing
        line += ("\nNo need to call session_clock again: the ⏱ line on every result switches to WRAP UP NOW by "
                 "itself. Until then, play.")
    return line


def _session_log() -> Path | None:
    sess = os.environ.get("BG3AI_SESSION")
    d = core.MEMORY / "sessions"
    if sess:
        return d / f"{sess}.md"
    logs = sorted(d.glob("S*.md")) if d.is_dir() else []
    return logs[-1] if logs else None


def _movie_skipper(stop: "threading.Event") -> None:
    """Cutscenes are always skipped (project requirement). The mod's AutoSkip handles dialogue lines in-game
    and asks for a shim ESCAPE when a Bink movie ignores the engine's; skipper.py polls that request
    (autoskip.status, ~150 ms) on its own connection, with the old ui.state poll as a fallback. It idles
    while another process (runner/play.py) holds runtime/skipper.lock: one skipper per game."""
    from ..skipper import MovieSkipper

    MovieSkipper(Game(port=core.game.port), world_tools._inp, lambda msg: core._log("autoskip", {}, msg, 0),
                 lock_path=core.RUNTIME / "skipper.lock").run(stop)


# Claude Code defers MCP tools behind ToolSearch: the agent sees only names until it loads a schema, and
# in S001/S002 it loaded single-step tools one by one and never queue_actions/do_actions/wait_for. These
# core play tools carry _meta "anthropic/alwaysLoad", so their schemas are in context from the first
# turn; the rest (setup, saves, wiki, raw input, rare services) stay deferred.
ALWAYS_LOAD = (
    # pipelining
    "queue_actions", "do_actions", "wait_for", "cancel_queue", "combat_turn",
    # movement and interaction
    "travel_to", "move_to", "walk_toward", "interact", "pick_up", "talk_to", "jump_to", "loot", "loot_area",
    "loot_corpse", "use_ability", "end_turn", "react", "roll_check",
    # dialogue
    "get_dialogue", "choose_dialogue", "dialogue_advance",
    # observation
    "look", "get_status", "get_nearby", "get_frontier", "route", "get_combat_state", "look_at", "session_clock",
    "save_game",
    # S001 spent 7 ToolSearch turns loading these mid-play
    "wiki_page", "wiki_search", "recall", "load_game", "list_saves",
    # memory notes without a Read/Edit round trip (S023-S026: 20-30 s/session of idle memory I/O)
    "note",
    # S002-S007: 26 ToolSearch calls (10 in S005) loaded these mid-play; each tool here was used in 2+
    # sessions or 3+ times (rests/camp/level-up: rare but on every long day). Still deferred: ui_dump,
    # ui_action, path_preview, fast_travel and the setup/menu tools (1 use or none).
    "screenshot", "click", "click_on", "press_key", "dismiss_popups", "lockpick", "regroup", "get_map",
    "get_party", "get_actions", "get_inventory", "level_up", "get_level_up_options", "short_rest", "long_rest",
    "camp", "confirm",
    # S018-S026: 80 ToolSearch calls loaded these (manage_inventory 18, trade 12, fast_travel 12, read_all 9,
    # get_journal 9, stay 6, set_party 6), each a ~2 s turn mid-play
    "manage_inventory", "trade", "fast_travel", "read_all", "get_journal", "stay", "set_party", "equip_best",
    # S027-S039: still ~1-2 ToolSearch turns a session (get_layout 4, path_preview 4, swap_party 3, zoom 2,
    # insert_item, get_explored, disarm); S039 opened with one at the Shadowfell ledge. ui_dump/ui_action
    # stay deferred: hand UI work should cost a step.
    "get_layout", "path_preview", "swap_party", "zoom", "insert_item", "get_explored", "disarm", "pickpocket",
    # memory and session (2026-10-13: were the client's Read/Edit/Write and hooks; now the server's)
    "memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append",
    "session_start", "end_session",
)
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def _mark_always_load(names=ALWAYS_LOAD) -> list[str]:
    """Set the alwaysLoad hint on the core tools; returns the names it could not find."""
    missing = []
    for name in names:
        tool = core.mcp._tool_manager.get_tool(name)
        if tool is None:
            missing.append(name)
            continue
        tool.meta = {**(tool.meta or {}), **ALWAYS_LOAD_META}
    return missing


if os.environ.get("BG3AI_ALWAYS_LOAD", "1") != "0":  # 0: everything deferred (A/B checks)
    _mark_always_load()


# Every result the client gets passes compact.py (each thing said once, short: the model re-reads every result on
# every later turn). Wrapped here, at the MCP layer, so do_actions and the wiki tools (no _guard) get it too and the
# steps do_actions runs inside itself (direct calls) do not. The session briefing on the first result stays as is.
BRIEFING_SEP = "\n\n=== the result of your call ===\n"
# str tools return plain text, not structured {"result": "..."}: Claude Code showed the model the JSON form
# (escaped newlines and quotes on every line). BG3AI_PLAIN_RESULTS=0 restores the SDK default.
PLAIN_RESULTS = os.environ.get("BG3AI_PLAIN_RESULTS", "1") != "0"


def _compacted(name: str, fn: Callable) -> Callable:
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        out = fn(*a, **kw)
        if isinstance(out, str) and compact.enabled():
            head, sep, body = out.rpartition(BRIEFING_SEP)
            out = head + sep + compact.apply(name, body)
        return out
    wrapper.__compacted__ = True
    return wrapper


def _mark_results(server: MCPServer = core.mcp) -> None:
    for tool in server._tool_manager.list_tools():
        if not getattr(tool.fn, "__compacted__", False):
            tool.fn = _compacted(tool.name, tool.fn)
        schema = tool.fn_metadata.output_schema or {}
        if PLAIN_RESULTS and schema.get("properties", {}).get("result", {}).get("type") == "string":
            tool.fn_metadata.output_schema = None
            tool.__dict__.pop("output_schema", None)  # Tool.output_schema is a cached_property


_mark_results()


# Capabilities (capabilities.py): tools a session's condition switches off are removed here, after the alwaysLoad
# marks (_mark_always_load reports removed names as missing), so the client never sees them; the batch tools lose
# them as steps, and note= on an action is refused when the note tool is off (it writes the memory log).
TOOLS_OFF = capabilities.disabled([t.name for t in core.mcp._tool_manager.list_tools()], os.environ)
for _name in sorted(TOOLS_OFF):
    core.mcp.remove_tool(_name)
batch._BATCHABLE -= TOOLS_OFF
INSTRUCTIONS = core.instructions(TOOLS_OFF)
if TOOLS_OFF:  # the rules of the served groups only
    core.RULES.rules = rules.load(off=TOOLS_OFF)
core.mcp._lowlevel_server.instructions = rules.instructions(INSTRUCTIONS, core.RULES.rules)


def _note_refused(text: str) -> str:
    return capabilities.NOTE_REFUSED if text and text.strip() and "note" in TOOLS_OFF else ""
