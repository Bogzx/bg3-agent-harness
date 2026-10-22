"""Server state shared by every tool module: paths and settings, the MCP server, the game connection, the session clock
and fight hold, event text.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import boxclick, delta, memtools, metrics, render, rules, settings
from ..game import Game, GameError
from ..render import Handles

SETTINGS = settings.from_env(os.environ)  # the only env read for these (settings.py)
REPO = SETTINGS.repo
RUNTIME = SETTINGS.runtime
boxclick.HITS_PATH = RUNTIME / "boxclick_hits.json"   # a box's button position, kept across sessions
PLAY_DIR = SETTINGS.play_dir
MEMORY = SETTINGS.memory
KNOWLEDGE = SETTINGS.knowledge
SAVE_DIR = SETTINGS.save_dir
DEADLINE = SETTINGS.deadline
WRAPUP_AT = SETTINGS.wrapup_at  # the runner's wrap-up start (BG3AI_SESSION_WRAPUP), 0 = WRAPUP_S before DEADLINE
WRAPUP_S = 10 * 60  # wrap-up length without the runner's BG3AI_SESSION_WRAPUP (the runner: 3 min, a third of a short one)

# The server instructions, one part per line group; a part that names a tool group (capabilities.py) is served only
# with that tool on, so no client is told about tools it doesn't have. Every client and condition gets them, so they
# say what the tools do, not how to play well (that is coaching: packages/harness/prompts/coaching/).
_INSTRUCTION_PARTS: tuple[tuple[str, str], ...] = (
    ("", """Tools to play Baldur's Gate 3 as the player. All information is what the player can see.
Start with get_status. Refer to things by their short id (e12) or a party member's name.
queue_actions runs several steps back to back (walk, open, loot, walk on) and returns at once; call it
again to append more. Movement (move_to, walk_toward, travel_to) also returns at once with a job id or a
queue entry; wait_for blocks until a job, the queue or a turn is done. Other action tools block until they
settle; pass wait=false to return at once.
In combat, combat_turn plays a whole turn (with fallbacks) in one call and says who acts next. Chores are
one call each: loot_area, dialogue_advance, travel_to, regroup, read_all, rest_if_needed."""),
    ("screenshot", """screenshot shows the game screen as a picture; zoom crops the last one."""),
    ("memory_read", """Memory: your notes live in memory/ (read and write with memory_list, memory_read, memory_search, memory_write,
memory_edit, memory_append; note appends to the session log). knowledge/ holds read-only reference notes and the
playbooks. Session: if your first message doesn't show your memory briefing, call session_start first; at the end
of the wrap-up call end_session and stop (no more tool calls) only when it reports the commit."""),
)


def instructions(off: set[str] | frozenset[str] = frozenset()) -> str:
    """The server instructions for the served tool set: parts that need a tool in `off` are left out."""
    return "\n".join(text for tool, text in _INSTRUCTION_PARTS if tool not in off)


INSTRUCTIONS = instructions()  # every tool on; session.INSTRUCTIONS is the served set's

# The assembled rules for clients other than Claude Code (rules.py): in `instructions` and on the first briefing.
RULES = rules.Once(rules.load(required=rules.serve_enabled()))
mcp = MCPServer("bg3", instructions=rules.instructions(INSTRUCTIONS, RULES.rules))
SANDBOX = memtools.Sandbox(lambda: MEMORY, lambda: KNOWLEDGE)  # memory/ + read-only knowledge/ (memtools.py)
LIFE = memtools.Lifecycle(lambda: MEMORY)  # opens/ends the memory session without client hooks
game = Game(port=SETTINGS.port, pool_max=SETTINGS.rpc_conns, job_push=SETTINGS.job_push)
H = Handles()
DELTA = delta.TRACKER.bind(H)  # the agent's last look; get_status/look/get_map diff against it
METRICS = metrics.Recorder.from_env(RUNTIME)  # $BG3AI_METRICS_PATH (set by the runner) or runtime/metrics.jsonl
game.metrics = METRICS
_screen = None
_last_full_frame = None  # PIL image of the latest screenshot (for zoom)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def _log(tool: str, args: dict, result: str, ms: float, extra: dict | None = None) -> None:
    # Every tool result passes here (via _finish): one latency record per call (metrics.py).
    METRICS.tool_done(tool, ms, nested=reporting._batch["active"], ok=not result.startswith(("FAILED", "NOT READY", "REFUSED")),
                      extra=extra)
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME / "actions.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "tool": tool, "args": args, "ms": round(ms),
                                "result": result[:4000]}) + "\n")
    except OSError:
        pass


_clock_shown = {"at": 0.0, "stuck": None}
WRAPUP_HINT = "follow knowledge/playbooks/session-wrapup.md"
PROGRESS_PATH = SETTINGS.progress_path
_stuck_cache: dict = {"mtime": None, "open": None}


def _stuck() -> dict | None:
    """The runner's open stuck episode (bg3ai.progress: no story progress for STUCK_MIN minutes while
    active), from Sxxx.progress.jsonl; None when progressing or when no runner writes the file."""
    if not PROGRESS_PATH:
        return None
    try:
        mt = os.stat(PROGRESS_PATH).st_mtime
    except OSError:
        return None
    if mt != _stuck_cache["mtime"]:
        from .. import progress
        open_ = None
        for r in progress.load(Path(PROGRESS_PATH)):
            if r.get("type") == "stuck":
                open_ = r
            elif r.get("type") in ("unstuck", "progress"):
                open_ = None
        _stuck_cache.update(mtime=mt, open=open_)
    return _stuck_cache["open"]


def _stuck_text(st: dict) -> str:
    mins = int((time.time() - float(st.get("since") or st.get("t") or time.time())) // 60)
    return f"no story progress for {mins} min: change approach (knowledge/playbooks/stuck.md)"


def _wrap_left() -> float:
    """Seconds before DEADLINE when WRAP UP starts: WRAPUP_AT (the runner's wrap-up time), else WRAPUP_S."""
    return DEADLINE - WRAPUP_AT if 0 < WRAPUP_AT < DEADLINE else WRAPUP_S


def _clock_line() -> str:
    """The clock for tool results: always from WRAP UP on and in a fight, otherwise in full at most every 10 minutes
    and at once when a stuck episode starts (then with the clock, while it lasts)."""
    now = time.time()
    st = _stuck()
    new_stuck = st is not None and st.get("t") != _clock_shown["stuck"]
    if not DEADLINE and not st:
        return ""
    if DEADLINE and _fighting():
        if st is not None:
            _clock_shown["stuck"] = st.get("t")
        return _clock()  # no time in it: not counted as a full line (the next one comes on time after the fight)
    full = new_stuck or (DEADLINE and now >= _times()[0])
    if not full and now - _clock_shown["at"] < 600:
        # Between full lines, a terse one: S009 guessed "time is nearly up" 4 min after the last clock with
        # 15 min left, saved, wrote its handoff (a 42 s gap), then walked back; and session_clock costs a turn.
        if not DEADLINE:
            return ""
        return f"⏱ {time.strftime('%H:%M')} · {KEEP_PLAYING}"
    _clock_shown["at"] = now
    if st is not None:
        _clock_shown["stuck"] = st.get("t")
    return _clock()


# Before WRAP UP NOW the ⏱ line shows no time left at all. Every countdown was read as a clock to beat: S069 wrapped up
# at a bare "13m left", S070 at "6m of play", S077/S078 at ~6-8 min of play, and S066-S082 (six sessions in a row, after
# the S10/S14/S18 texts) at 7-13 min ("I'm out of time to work out the door", S082 at 7 min of play: two S082-end saves).
KEEP_PLAYING = "keep playing: you'll be told when to wrap up"

# A session never ends mid-fight. S078 01:42:25, 2 min of play before WRAP UP: "Time is nearly up, so I'm reloading"
# a Viconia fight at round 1 with Silence on Gydd holding; three rounds of clock text (S10/S12/S14/S15) hadn't taken
# the time pressure away. So in a fight the ⏱ line shows no time, and a fight still on at WRAP UP holds the clock:
# WRAP UP comes when the fight ends (the end its wrap-up length later), at most FIGHT_HOLD_S after the planned WRAP UP,
# then the normal end. The hold goes to $BG3AI_FIGHT_HOLD_PATH (set by the runner), which the runner's hard stop and
# session.py's end check read; a restarted server (crash resume) keeps the session's cap from it.
FIGHT_HOLD_S = 15 * 60
FIGHT_READ_S = 15.0  # a state.combat read is kept this long; combat_started/combat_ended events and load_game drop it
FIGHT_LINE = "in a fight: finish it, taking each turn the time it needs; the session waits for the fight"
HOLD_PATH = SETTINGS.hold_path
_hold: dict = {"of": None, "cap": 0.0, "wrap": None, "held": False, "fight": None, "read_at": 0.0,
         "written": None}


def _fight_now() -> bool:
    """In combat (a state.combat read, kept FIGHT_READ_S; no game: no)."""
    now = time.monotonic()
    if _hold["fight"] is None or now - _hold["read_at"] > FIGHT_READ_S:
        _hold.update(fight=bool((game.try_rpc("state.combat") or {}).get("active")), read_at=now)
    return _hold["fight"]


def _fight_seen(tool: str, ev: str) -> None:
    """A fight started or ended (or a load): the next clock reads state.combat again."""
    if tool == "load_game" or "combat_started" in (ev or "") or "combat_ended" in (ev or ""):
        _hold["fight"] = None


def _times() -> tuple[float, float, bool]:
    """(WRAP UP, end, held) of this runner session (DEADLINE set), with the fight hold applied. While held, WRAP UP
    is the cap (what the runner and session.py plan for); when the fight ends (or the cap comes), it is that moment."""
    wrap_len = _wrap_left()
    base = DEADLINE - wrap_len
    if _hold["of"] != (DEADLINE, base):  # once per server process (tests change the session)
        _hold.update(of=(DEADLINE, base), cap=base + FIGHT_HOLD_S, wrap=None, held=False, written=None)
        saved = _read_hold()
        if saved:
            _hold["cap"] = min(_hold["cap"], float(saved.get("cap") or _hold["cap"]))
            _hold["held"] = bool(saved.get("held"))
            _hold["wrap"] = None if _hold["held"] else saved.get("wrap")
    now, cap = time.time(), _hold["cap"]
    wrap = max(base, float(_hold["wrap"] or 0))
    held = wrap <= now < cap and _fight_now()
    if _hold["held"] and not held:  # the fight ended (or the cap came): WRAP UP now
        _hold["wrap"] = wrap = max(wrap, min(now, cap))
    _hold["held"] = held
    if held:
        wrap = cap
    _write_hold(wrap, wrap + wrap_len, held)
    return wrap, wrap + wrap_len, held


def _fighting() -> bool:
    """The ⏱ line is the fight line: held, or a fight before WRAP UP (after the cap: the normal end)."""
    wrap, _, held = _times()
    return held or (time.time() < wrap and _fight_now())


def _read_hold() -> dict:
    if not HOLD_PATH:
        return {}
    try:
        d = json.loads(Path(HOLD_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_hold(wrap: float, end: float, held: bool) -> None:
    row = {"wrap": int(wrap), "deadline": int(end), "held": held, "cap": int(_hold["cap"])}
    if not HOLD_PATH or row == _hold["written"]:
        return
    if _hold["written"] is None and not held and int(wrap) == int(DEADLINE - _wrap_left()):
        return  # nothing held yet: no file
    try:
        tmp = Path(HOLD_PATH + ".tmp")
        tmp.write_text(json.dumps({**row, "t": int(time.time())}))
        os.replace(tmp, HOLD_PATH)
        _hold["written"] = row
    except OSError:
        pass


def _clock() -> str:
    st = _stuck()
    stuck = (" · " if DEADLINE else "⏱ ") + _stuck_text(st) if st else ""
    if not DEADLINE:
        return stuck
    hhmm = time.strftime("%H:%M")  # wall clock: S001 invented log times without it
    if _fighting():
        return f"⏱ {hhmm} · {FIGHT_LINE}" + stuck
    wrap, end, _ = _times()
    now = time.time()
    left = end - now
    # Rules-only (the benchmark's ranked condition) names no playbook and no tool its caps may have switched off.
    coach = rules.coaching()
    if left <= 0:
        if not coach:
            return "⏱ SESSION OVER: game actions close in a few minutes."
        return f"⏱ SESSION OVER: only save_game and note-taking now; {WRAPUP_HINT}."
    # Wrap-up starts at BG3AI_SESSION_WRAPUP (the runner: 3 min before the end, a third of a short session); before
    # it, no minutes and no WRAP UP time (KEEP_PLAYING).
    if now >= wrap:
        how = f" Reach a safe spot, save_game, then {WRAPUP_HINT}." if coach else ""
        return f"⏱ {hhmm} · {int(left // 60)}m left: WRAP UP NOW.{how}"
    return f"⏱ {hhmm} · {KEEP_PLAYING}" + stuck


def _session_over() -> bool:
    return bool(DEADLINE) and time.time() > _times()[1]


# Dialogue tools don't wait for a stalled tick to read events (see Game.drain_events_bounded).
DIALOGUE_DRAIN_MAX_S = 0.25
_DIALOGUE_TOOLS = frozenset({"dialogue_advance", "choose_dialogue", "talk_to"})


# Events a result already reports in its own words (set by the tool for its one call): item uuids whose item_added
# is noise, and whether the gold stack's item_added is (sell=junk: the game moves each member's sold items to the
# trading character first — acceptance 12:21: 5 "item_added … owner=Claude" lines + "Gold count=799").
# The window stays open ~10 s after the call ("until"): the game's AddedTo for a big sale (54 items) trickles in after
# the trade job ends and would otherwise land in the next tool's result.
_EV_DROP: dict[str, Any] = {"items": set(), "gold": False, "until": 0.0}
EV_DROP_TAIL_S = 10.0


def _drop_events(evs: list | None) -> list:
    if _EV_DROP["until"] and time.monotonic() > _EV_DROP["until"]:
        _EV_DROP.update(items=set(), gold=False, until=0.0)
    items, gold = _EV_DROP["items"], _EV_DROP["gold"]
    if not evs or not (items or gold):
        return evs or []
    def noise(e) -> bool:
        if not isinstance(e, dict) or e.get("kind") != "item_added":
            return False
        it = (e.get("data") or {}).get("item") or {}
        u = it.get("uuid") if isinstance(it, dict) else it
        return (isinstance(u, str) and H._bare(u) in items) or (gold and isinstance(it, dict) and it.get("name") == "Gold")
    return [e for e in evs if not noise(e)]


def _events_text(body: str = "", job: str | None = None, max_wait: float | None = None) -> str:
    """Drained events, minus those the result's own text (body) already reports."""
    def drain(ctx):
        try:
            bounded = getattr(game, "drain_events_bounded", None)
            if max_wait is not None and bounded is not None:
                return bounded(ctx, max_wait)
            return game.drain_events(ctx)
        except GameError:
            return []

    parts = []
    seen: list = []
    for evs in reporting._par(lambda: drain("server"), lambda: drain("client")):
        evs = _drop_events(evs)
        if evs:
            seen += evs
            reporting._note_dialogue_speaker(evs)
            reporting.OFFLIMITS.feed(evs)
            parts.append(render.render_events(evs, H, body=body, job=job, flap=render.FLAP, party=turn_plan._party_cached(),
                                              echo=render.ECHO))
    fight = reporting._fight_feed(seen)
    if fight:
        parts.append(fight)
    return "\n".join(p for p in parts if p)


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    reporting, turn_plan,
)
