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
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import actq, delta, leveling, macros, metrics, party, render, saver, saves, turn
from . import longnav
from . import route as route_text
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
Pipeline like a human clicking ahead: issue the next steps, then observe and plan while they run.
queue_actions runs several steps back to back (walk, open, loot, walk on) and returns at once; call it
again to append more. Movement (move_to, walk_toward, travel_to) also returns at once with a job id or a
queue entry: keep planning while the character walks; wait_for only when the next step depends on the
arrival. Other action tools block until they settle; pass wait=false to return at once.
In combat, combat_turn plays a whole turn (with fallbacks) in one call and says who acts next. Chores are
one call each: loot_area, dialogue_advance, travel_to, regroup, read_all, rest_if_needed.
Use screenshot only when the text tools can't tell you something (spatial layout, puzzles, unknown popups)."""

mcp = MCPServer("bg3", instructions=INSTRUCTIONS)
game = Game(port=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
H = Handles()
DELTA = delta.TRACKER.bind(H)  # the agent's last look; get_status/look/get_map diff against it
METRICS = metrics.Recorder.from_env(RUNTIME)  # $BG3AI_METRICS_PATH (set by the runner) or runtime/metrics.jsonl
game.metrics = METRICS
_screen = None
_last_full_frame = None  # PIL image of the latest screenshot (for zoom)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def _log(tool: str, args: dict, result: str, ms: float) -> None:
    # Every tool result passes here (via _finish): one latency record per call (metrics.py).
    METRICS.tool_done(tool, ms, nested=_batch["active"], ok=not result.startswith(("FAILED", "NOT READY", "REFUSED")))
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME / "actions.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "tool": tool, "args": args, "ms": round(ms),
                                "result": result[:4000]}) + "\n")
    except OSError:
        pass


_clock_shown = {"at": 0.0, "stuck": None}
WRAPUP_HINT = "follow knowledge/playbooks/session-wrapup.md"
PROGRESS_PATH = os.environ.get("BG3AI_PROGRESS_PATH") or (
    re.sub(r"\.metrics\.jsonl$", ".progress.jsonl", os.environ["BG3AI_METRICS_PATH"])
    if os.environ.get("BG3AI_METRICS_PATH", "").endswith(".metrics.jsonl") else "")
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
        from . import progress
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


def _clock_line() -> str:
    """The clock for tool results: always in the last 10 minutes, otherwise at most every 10 minutes,
    and at once when a stuck episode starts (then with the clock, while it lasts)."""
    now = time.time()
    st = _stuck()
    new_stuck = st is not None and st.get("t") != _clock_shown["stuck"]
    if not DEADLINE and not st:
        return ""
    if not new_stuck and (not DEADLINE or DEADLINE - now > WRAPUP_S) and now - _clock_shown["at"] < 600:
        # Between full lines, a terse one: S009 guessed "time is nearly up" 4 min after the last clock with
        # 15 min left, saved, wrote its handoff (a 42 s gap), then walked back; and session_clock costs a turn.
        return f"⏱ {time.strftime('%H:%M')} · {int((DEADLINE - now) // 60)}m left" if DEADLINE else ""
    _clock_shown["at"] = now
    if st is not None:
        _clock_shown["stuck"] = st.get("t")
    return _clock()


def _clock() -> str:
    st = _stuck()
    stuck = (" · " if DEADLINE else "⏱ ") + _stuck_text(st) if st else ""
    if not DEADLINE:
        return stuck
    left = DEADLINE - time.time()
    if left <= 0:
        return f"⏱ SESSION OVER: only save_game and note-taking now; {WRAPUP_HINT}."
    # Wrap-up starts at BG3AI_SESSION_WRAPUP (the runner: 10 min before the end, a third of a short
    # session); before it, say how much is play time so "14m left" doesn't read as "wrap up now".
    try:
        wrap_at = float(os.environ.get("BG3AI_SESSION_WRAPUP") or 0)
    except ValueError:
        wrap_at = 0.0
    wrap_left = DEADLINE - wrap_at if 0 < wrap_at < DEADLINE else WRAPUP_S
    m = int(left // 60)
    hhmm = time.strftime("%H:%M")  # wall clock: S001 invented log times without it
    if left <= wrap_left:
        return f"⏱ {hhmm} · {m}m left: WRAP UP NOW. Reach a safe spot, save_game, then {WRAPUP_HINT}."
    play = int((left - wrap_left) // 60)
    return (f"⏱ {hhmm} · {m}m left · {play}m of play: keep playing until WRAP UP at {round(wrap_left / 60)}m left"
            + stuck)


def _session_over() -> bool:
    return bool(DEADLINE) and time.time() > DEADLINE


def _events_text() -> str:
    def drain(ctx):
        try:
            return game.drain_events(ctx)
        except GameError:
            return []

    parts = []
    for evs in _par(lambda: drain("server"), lambda: drain("client")):
        if evs:
            _note_dialogue_speaker(evs)
            parts.append(render.render_events(evs, H))
    return "\n".join(p for p in parts if p)


# ---- skill checks in dialogue: each tagged option shows the check and the speaker's own modifier ----------
# Fair: the player sees the tag on the answer and the bonuses on the character sheet; the DC is only shown on
# the roll screen after picking, so it is never added here.
_dlg = {"speaker": None}  # party member in the current conversation (from the last dialogue_started event)
_sheet_cache: dict[str, tuple[float, dict]] = {}
SHEET_TTL_S = 120.0
_ABILITY_TAGS = {"strength": "str", "dexterity": "dex", "constitution": "con", "intelligence": "int",
                 "wisdom": "wis", "charisma": "cha"}
_TAG_NOTES = {"illithid": "uses the tadpole's power (story consequences)"}


def _note_dialogue_speaker(evs: list) -> None:
    for e in evs:
        if not isinstance(e, dict) or e.get("kind") != "dialogue_started":
            continue
        d = e.get("data") or {}
        if d.get("automated"):
            continue
        party = [p.get("uuid") if isinstance(p, dict) else p for p in d.get("party") or []]
        if party and party[0]:
            _dlg["speaker"] = party[0]


def _sheet(uuid: str) -> dict:
    hit = _sheet_cache.get(uuid)
    if hit and time.monotonic() - hit[0] < SHEET_TTL_S:
        return hit[1]
    c = game.try_rpc("state.character", {"uuid": uuid, "sections": ["abilities"]}) or {}
    _sheet_cache[uuid] = (time.monotonic(), c if isinstance(c, dict) else {})
    return _sheet_cache[uuid][1]


def _check_mod(sheet: dict, tag: str) -> int | None:
    key = re.sub(r"[^a-z]", "", tag.lower())
    for name, v in (sheet.get("skills") or {}).items():
        if re.sub(r"[^a-z]", "", str(name).lower()) == key and isinstance(v, (int, float)):
            return int(v)
    ab = _ABILITY_TAGS.get(key)
    score = (sheet.get("abilities") or {}).get(ab) if ab else None
    return (int(score) - 10) // 2 if isinstance(score, (int, float)) else None


def _signed(v: int) -> str:
    return f"+{v}" if v >= 0 else str(v)


def _annotate_checks(ui: dict) -> dict:
    """Add 'check' to each tagged dialogue option: 'Persuasion: Claude +5 (party best: Shadowheart +6)'."""
    d = ui.get("dialogue") if isinstance(ui, dict) else None
    opts = [o for o in (d or {}).get("options") or [] if isinstance(o, dict) and o.get("tags")]
    if not opts:
        return ui
    try:
        party = sorted(_party_set() - {None})
        speaker = _dlg["speaker"] if _dlg["speaker"] in party else (game.try_rpc("state.status") or {}).get("active")
    except GameError:
        return ui
    for o in opts:
        notes = []
        for t in o["tags"]:
            key = re.sub(r"[^a-z]", "", str(t).lower())
            if key in _TAG_NOTES:
                notes.append(_TAG_NOTES[key])
                continue
            mods = {u: _check_mod(_sheet(u), str(t)) for u in party}
            mine = mods.get(speaker)
            if mine is None and all(v is None for v in mods.values()):
                notes.append("class/race/background option")
                continue
            name = str(t).title()
            txt = f"{name} check: {H.get(speaker) if speaker else '?'} {_signed(mine) if mine is not None else '?'}"
            best = max(((v, u) for u, v in mods.items() if v is not None), default=None)
            if best and best[1] != speaker and (mine is None or best[0] > mine):
                txt += f" (party best {H.get(best[1])} {_signed(best[0])}: they can start the next talk)"
            notes.append(txt)
        o["check"] = "; ".join(notes)
    return ui


def _render_ui(ui: dict, brief: bool = False) -> list[str]:
    try:
        ui = _annotate_checks(ui)
    except Exception:  # best effort: a missing sheet never hides the dialogue itself
        pass
    return render.render_ui(ui, H, brief=brief)


class _BatchState(threading.local):
    """Per-thread batch flags (same [] interface as the old dict). A queue worker thread runs steps
    batch-quiet while the agent's own tool calls on the MCP thread report normally.
    active: inside do_actions / a queue (no per-step event drain); last: the last step's status;
    exec: the actq.Executor running the steps (gets the job id to cancel); result: last job result."""

    def __init__(self) -> None:
        self.d = {"active": False, "last": "done", "exec": None, "result": None}

    def __getitem__(self, k: str) -> Any:
        return self.d[k]

    def __setitem__(self, k: str, v: Any) -> None:
        self.d[k] = v

    def get(self, k: str, default: Any = None) -> Any:
        return self.d.get(k, default)  # dict parity: roll_check used .get and raised (live S005)


_batch = _BatchState()


# Pace bookkeeping for the agent's own calls (MCP thread): when it last issued an async action, how many calls
# came after it, and how often the NEXT hint was shown. Teaches pipelining in context (impl-setup part 5).
_pace = {"issued_at": 0.0, "calls_since": 0, "next_shown": 0, "nudge": False}
PACE_GAP_S = 1.5
NEXT_WALK = ("NEXT: queue_actions what to do on arrival, then look/plan/notes while it runs; "
             "wait_for only when you are out of work")


def _finish(tool: str, args: dict, body: str, t0: float, events: bool = True, issued: bool = False) -> str:
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
        auto = _auto_dialogue(tool, body, ev)
        if auto:
            out.append(auto)
            ev = _events_text()
            if ev:
                out.append(ev)
    _running_memo.update(on=True, keys=None, out=None, watch=None)
    try:
        if "RUNNING " not in body:  # what still runs, so the agent appends (queue_actions) instead of waiting
            try:
                out.append(_running_text())
            except GameError:
                pass
        try:
            out.append(_notes_window())
        except GameError:
            pass
    finally:
        _running_memo["on"] = False
    if tool == "wait_for" and _pace["calls_since"] == 0 and _pace["issued_at"] \
            and t0 - _pace["issued_at"] < PACE_GAP_S:
        out.append("PACE: you waited right after issuing. Next time first do independent work (queue the arrival "
                   "steps, look, plan, notes): the character acts while you think.")
        _pace["nudge"] = True
    if tool not in ("get_status", "look", "level_up"):
        lv = _LEVEL_NUDGE.text()
        if lv:
            out.append(lv)
    out.append(SAVER.take_notes())  # SAVED / SAVE FAILED from a background save
    c = _clock_line()
    if c:
        out.append(c)
    text = H.scrub("\n".join(x for x in out if x))
    _log(tool, args, text, (time.monotonic() - t0) * 1000)
    if issued:
        _pace["issued_at"], _pace["calls_since"] = time.monotonic(), 0
    else:
        _pace["calls_since"] += 1
    return text


# Tools after which the bridge does not open-and-advance a conversation by itself (they handle dialogue).
_NO_AUTO_DIALOGUE = {"dialogue_advance", "choose_dialogue", "get_dialogue", "roll_check", "react", "screenshot",
                     "zoom", "session_clock", "end_combat_report"}


def _auto_dialogue(tool: str, body: str, ev: str) -> str:
    """When this call opened a conversation (talk_to done, an interact that starts one, or a dialogue_started
    event), click through its opening filler and show the first real choice now: saves the get_dialogue +
    dialogue_advance turns the agent spent on every conversation (S002/S003). Filler only, never an answer."""
    if not AUTO_DIALOGUE or tool in _NO_AUTO_DIALOGUE or "DIALOGUE " in body:
        return ""
    head = body.lstrip().split("\n", 1)[0]
    if tool == "talk_to" and head.startswith("talk_to: done"):
        open_s = 2.5  # the conversation is expected; it may take a moment to open
    elif "dialogue_started" in ev:
        open_s = 0.5
    elif tool == "interact" and head.startswith("interact: done"):
        open_s = 0.6  # a console/book/pod may open a dialogue right after the use
    else:
        return ""
    try:
        n, lines, stop = _dialogue_autopilot(open_s=open_s)
    except GameError:
        return ""
    if n == 0 and stop == "conversation ended":
        return ""  # no dialogue on screen (ambient banter, a plain door): nothing to add
    return "\n".join(([f"auto-advanced {n} opening filler line(s):"] if n else []) + lines
                     + ([f"STOPPED: {stop}"] if stop else []))


def _next_walk_hint() -> str:
    """The NEXT line after a walk that is still running: the first 3 times, then only after a PACE nudge."""
    if _pace["next_shown"] >= 3 and not _pace["nudge"]:
        return ""
    _pace["next_shown"] += 1
    _pace["nudge"] = False
    return NEXT_WALK


def _par(*thunks) -> list:
    """game.parallel when the game has it (one tick for independent RPCs), else one after another."""
    p = getattr(game, "parallel", None)
    return p(*thunks) if p else [t() for t in thunks]


def _read(server_ops: list, client_ops: list = ()) -> tuple[list, list]:
    """game.read: server + client ops (and both contexts' events) in one game tick; failed ops are None."""
    if hasattr(game, "read"):
        return game.read(server_ops, client_ops)
    return (delta.multi_rpc(game, list(server_ops)) if server_ops else [],
            [game.try_rpc(op, a, context="client") for op, a in client_ops])


def _read1(op: str, args: dict | None = None, context: str = "server") -> Any:
    """game.read1: rpc(op) with both contexts' events folded into the same tick; raises like rpc()."""
    r1 = getattr(game, "read1", None)
    return r1(op, args, context) if r1 else game.rpc(op, args, context)


def _guard(fn):
    """Wrap a tool so game errors become readable text instead of exceptions."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        t0 = time.monotonic()
        getattr(game, "begin_tool", lambda: None)()
        gated = fn.__name__ not in _SAVE_UNGATED
        if gated:
            SAVER.enter()  # a background save has the menus up for ~1 s: don't interleave with it
        try:
            return fn(*a, **kw)
        except NotReady as e:
            return _finish(fn.__name__, kw, f"NOT READY: {e}", t0, events=False)
        except (GameError, ValueError) as e:
            return _finish(fn.__name__, kw, "FAILED: " + re.sub(r'\[string "[^"]*"\]:\d+: ', "", str(e)), t0)
        finally:
            if gated:
                SAVER.leave()
    return wrapper


# Tools that don't touch the game screen, so they run while a background save has the menus up.
_SAVE_UNGATED = {"list_saves", "session_clock", "wiki_page", "wiki_search"}


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


def _act(tool: str, op: str, args: dict, wait_s: float = 25.0, context: str = "server",
         wait: bool = True, note: str = "") -> str:
    t0 = time.monotonic()
    if _session_over():
        return _finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    if not wait and not _batch["active"] and context == "server":
        return _start_async(tool, op, args, t0, note)
    ex = _batch["exec"]
    if _batch["active"]:
        wait_s = max(wait_s, 120.0)  # a batch/queue step runs to completion (interrupts cancel it)
    kw = {"on_start": ex.track} if ex is not None else {}
    r = game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    _remember_names(r.result)
    _batch["last"] = r.status
    _batch["result"] = r.result
    if _batch["active"] and isinstance(r.result, dict) and "after" in r.result:
        # In a batch the per-step portrait summary is noise; the run ends with one NOW line + CHANGES.
        r = JobResult(r.status, {k: v for k, v in r.result.items() if k != "after"}, r.error, r.events, r.elapsed_ms)
    return _finish(tool, args, _job_text(tool, r) + ("\n" + note if note else ""), t0)


# Async jobs started with wait=false: handle (j12) -> what the agent needs to follow them up.
_async: dict[str, dict] = {}


def _start_async(tool: str, op: str, args: dict, t0: float, note: str = "") -> str:
    """Start a mod job and return at once: "move_to: started j12, ETA 3.4s (14m)". The mod announces the
    end with a job_finished event (notify), so any later tool result shows it; wait_for('j12') blocks."""
    started = game.rpc(op, dict(args, notify=True))
    if not isinstance(started, dict) or "job" not in started:
        return _finish(tool, args, _job_text(tool, JobResult("done", result=started)), t0)
    if started.get("status") not in (None, "running"):
        r = JobResult(started["status"], started.get("result"), started.get("error"))
        _remember_names(r.result)
        _batch["last"] = r.status
        return _finish(tool, args, _job_text(tool, r), t0)
    h = f"j{started['job']}"
    _async[h] = {"ctx": "server", "id": started["job"], "tool": tool, "t0": time.monotonic(),
                 "eta": started.get("eta_s"), "char": args.get("char")}
    METRICS.bg_start(h, started.get("eta_s"))
    # Settle window: a refusal, a blocked path or a short action that ends within SETTLE_S comes back as
    # its real result now (no wait_for round trip); anything longer keeps running while the agent plans.
    view = _settle(lambda: game.try_rpc("job.poll", {"id": started["job"]}))
    if view is not None:
        _async.pop(h, None)
        METRICS.bg_end(h)
        r = JobResult(view["status"], view.get("result"), view.get("error"), elapsed_ms=view.get("elapsed_ms", 0))
        _remember_names(r.result)
        _batch["last"] = r.status
        return _finish(tool, args, _job_text(tool, r) + ("\n" + note if note else ""), t0)
    body = f"{tool}: started {h}"
    if started.get("eta_s") is not None:
        body += f", ETA {render.num(float(started['eta_s']))}s"
    if started.get("route_m") is not None:
        body += f" ({render.num(float(started['route_m']))}m)"
    if note:
        body += "\n" + note
    if tool in _WALKS:
        hint = _next_walk_hint()
        if hint:
            body += "\n" + hint
    _batch["last"] = "done"
    return _finish(tool, args, body, t0, issued=True)


SETTLE_S = float(os.environ.get("BG3AI_SETTLE_S", "1.0") or 0)  # ≤1.5 s: the goal's live-feel budget
_WALKS = ("move_to", "walk_toward", "travel_to")


def _settle(poll, limit: float | None = None) -> dict | None:
    """Poll a just-started job for up to SETTLE_S; its final view if it ended, else None (still running)."""
    end = time.monotonic() + (SETTLE_S if limit is None else limit)
    while True:
        view = poll()
        if isinstance(view, dict) and view.get("status") not in (None, "running"):
            return view
        if time.monotonic() >= end:
            return None
        time.sleep(0.1)


def _walk_wait(wait: bool) -> tuple[bool, str]:
    """wait=true on a walk blocks only in combat (moving before an attack). Outside it the walk runs while the
    agent plans: S001 learned wait=true from its own results and pipelined 0% (impl-setup part 5)."""
    if not wait or _batch["active"]:
        return wait, ""
    w = game.try_rpc("act.watch")
    if not isinstance(w, dict) or w.get("combat"):
        return True, ""  # in combat, or unknown: keep the old blocking behaviour
    return False, "(wait=true ignored outside combat: the walk runs while you plan)"


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------

_last_subregion = {"name": None}
_objectives: dict[str, Any] = {"last": [], "at": 0.0}  # markers as last shown (o1, o2, ... index into this)
MARKERS_TTL_S = 5.0  # route/travel_to reuse markers fetched this recently (a marker's position doesn't move)


def _set_objectives(ms: list) -> None:
    _objectives["last"], _objectives["at"] = ms, time.monotonic()


def _objective_markers(max_age: float = 0.0) -> list:
    """Active quest objective markers (nav.markers), remembered so o1/o2 refer to what was shown.
    max_age > 0: the markers fetched within that many seconds (by get_status/look/get_map) are fine."""
    if max_age and _objectives["last"] and time.monotonic() - _objectives["at"] < max_age:
        return _objectives["last"]
    res = game.try_rpc("nav.markers", {})
    ms = res.get("markers") if isinstance(res, dict) else None
    if not isinstance(ms, list):
        return []
    _set_objectives(ms)
    return ms


def _delta_body(lines: list[str], ui: dict | None = None) -> str:
    """Delta view text. A prompt still on screen (unchanged, so not in the diff) gets a one-line reminder."""
    body = "\n".join(lines) if lines else "No change since your last look (full=true shows everything)."
    open_ = [k for k in ("dialogue", "roll", "reaction", "popup", "container") if (ui or {}).get(k)]
    if open_ and not set(_render_ui(ui, brief=True)) & set(lines):
        body += f"\n({'/'.join(open_).upper()} still open, unchanged; full=true repeats it)"
    return body


def _too_big(lines: list[str]) -> bool:
    return len(lines) > delta.BIG_CHANGE or any(x.startswith("WHERE") for x in lines)


@mcp.tool()
@_guard
def get_status(full: bool = False) -> str:
    """Where you are, party HP/conditions, mode (exploring/combat/dialogue), what's on screen, objectives, and recent events. Call this first.
    After the first call it returns only what CHANGED since your last look (moves, HP, conditions, mode, objectives, new prompts); full=true shows everything again."""
    t0 = time.monotonic()
    try:
        # One game tick: status+markers (server) and ui.state (client) in parallel, events folded in.
        (st, mk), (ui,) = _read([("state.status", {}), ("nav.markers", {})], [("ui.state", {})])
        if st is None:
            st = game.rpc("state.status")  # surfaces the real error
    except NotReady:
        gs = game.game_state()
        ui = game.try_rpc("ui.state", context="client")
        body = f"No game session loaded (game state: {gs})."
        if ui:
            body += "\n" + "\n".join(_render_ui(ui))
        return _finish("get_status", {}, body, t0, events=False)
    _remember_names(st)
    sub = (ui or {}).get("region")
    if sub and sub != _last_subregion["name"]:
        # The minimap's subregion name only exists client-side; hand it to the server's breadcrumbs, off the
        # read's critical path (it was a whole extra tick on every get_status after a subregion change).
        _last_subregion["name"] = sub

        def tell(sub=sub):
            if game.try_rpc("nav.set_subregion", {"name": sub}) is None and _last_subregion["name"] == sub:
                _last_subregion["name"] = None  # retry on the next get_status
        threading.Thread(target=tell, daemon=True).start()
    markers = mk.get("markers") if isinstance(mk, dict) and isinstance(mk.get("markers"), list) else None
    if markers is not None:
        _set_objectives(markers)
    first = not DELTA.has("party")
    lines = DELTA.observe(delta.snapshot(H, status=st, ui=ui, markers=markers))
    if full or first or _too_big(lines):
        body = render.render_status(st, ui, H)
        obj = render.render_objectives(markers or [], here_only=True, limit=3)
        if obj:
            body += "\n" + "\n".join(obj)
    else:
        body = _delta_body(lines, ui)
    apart = _party_check(st)
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    return _finish("get_status", {"full": full}, body, t0)


@mcp.tool()
@_guard
def look(full: bool = False) -> str:
    """The cheap way to keep your bearings: what CHANGED since your last look, in a few lines. Party moves (12m NW), HP, conditions gained/lost, characters and objects that appeared, left view or changed (hostile, dead, opened, unlocked), items gained/lost, gold, mode, new objectives and quest entries, and any new prompt (dialogue, roll, popup).
    full=true (or the first call, or a new area): the full status plus everything visible nearby."""
    t0 = time.monotonic()
    got = delta.fetch(game, delta.LOOK_PARTS, events=True)  # one game tick, events included
    st = got.get("status")
    if st is None:
        return get_status(full=True)
    for v in got.values():
        _remember_names(v)
    if got.get("markers") is not None:
        _set_objectives(got["markers"])
    first = not DELTA.has("party", "ents")
    lines = DELTA.observe(delta.snapshot_of(H, got))
    if full or first or _too_big(lines):
        body = render.render_status(st, got.get("ui"), H)
        obj = render.render_objectives(got.get("markers") or [], here_only=True, limit=3)
        if obj:
            body += "\n" + "\n".join(obj)
        if got.get("nearby") is not None:
            body += "\n" + render.render_nearby(got["nearby"], H)
        hz = _hazard_lines(got.get("hazards"), always=True)
    else:
        body = _delta_body(lines, got.get("ui"))
        hz = _hazard_lines(got.get("hazards"))
    if hz:
        body += "\n" + "\n".join(hz)
    apart = _party_check(st)
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    return _finish("look", {"full": full}, body, t0)


_LEVEL_NUDGE = leveling.Nudge()


def _level_up_line(st: dict | None) -> str:
    """get_status/look: 'LEVEL UP: e3 Gale (2) → level_up(who) …' outside combat ('' when nobody can level)."""
    if not isinstance(st, dict):
        return ""
    _LEVEL_NUDGE.observe(st)
    text = leveling.line(st, _label, MEMORY)
    if text:
        _LEVEL_NUDGE.text(shown=True)
    return text


_stay: set[str] = set()  # companions told to stay (stay tool): no re-chain, no PARTY APART nag, nobody waits


def _member_busy(u: str) -> bool:
    """A companion in the middle of its own queue or walk (re-chaining it now would cut that short)."""
    return _queues().busy(u) or any(info.get("char") == u for info in list(_async.values()))


def _rechain(members: list[str], leader: str) -> list[str]:
    """Attach members back to the leader's group (act.chain); the ones that took."""
    ok = []
    for u in members:
        r = game.run_job("act.chain", {"char": u, "leader": leader}, wait_s=3)
        if r.status == "done" or "already chained" in str(r.error or ""):
            ok.append(u)
    return ok


def _party_check(st: dict | None, rechain: bool = True) -> str:
    """get_status/look: re-chain companions that drifted off the leader's group (unless told to stay, busy,
    or in combat/dialogue), then the PARTY APART line ('' when everyone is together)."""
    if not isinstance(st, dict):
        return ""
    try:
        rows = party.apart(st, stay=_stay)
        if rechain and rows and not st.get("in_combat") and not st.get("in_dialogue") and st.get("active"):
            done = set(_rechain(party.rechain_candidates(rows, _member_busy), st["active"]))
            for r in rows:
                if r["uuid"] in done:
                    r["unchained"] = False
            rows = [r for r in rows if r["unchained"] or r["other_level"]
                    or (r["dist"] is not None and r["dist"] > party.APART_M)]
            if done:
                return (party.apart_line(rows, _label) + "\n" if rows else "") + \
                    "re-chained to " + _label(st["active"]) + ": " + ", ".join(_label(u) for u in done)
        return party.apart_line(rows, _label)
    except (GameError, ValueError, TypeError):
        return ""


@mcp.tool()
@_guard
def stay(who: str, on: bool = True) -> str:
    """Keep a companion where it is on purpose (guarding a door, standing on a plate): it is not re-chained to the leader, PARTY APART only notes it as staying, and the leader's travel_to doesn't wait for it. on=false (or regroup) brings it back into the group. Every other companion is chained to the leader by default and re-chained after it moved on its own."""
    t0 = time.monotonic()
    u = _uuid(who)
    if on:
        _stay.add(u)
        game.try_rpc("act.unchain", {"char": u})  # a chained member would follow the leader's next walk
        return _finish("stay", {"who": who}, f"{_label(u)} stays put (stay(who, on=false) or regroup brings it back)", t0)
    _stay.discard(u)
    st = game.try_rpc("state.status") or {}
    lead = st.get("active")
    took = _rechain([u], lead) if lead and lead != u else []
    return _finish("stay", {"who": who}, f"{_label(u)} " + (f"re-chained to {_label(lead)}" if took else "no longer staying"), t0)


_hazards_seen: dict = {"sig": None}


def _hazard_lines(hazards: Any, always: bool = False) -> list[str]:
    """HAZARD lines for look: always in the full view; in a delta only when the patches in view changed
    (one appeared, went out or moved >2 m), so a walk past the same fire doesn't repeat it."""
    if not isinstance(hazards, list):
        return []
    sig = tuple(sorted((str(h.get("surface")), round((h.get("center") or [0])[0] / 2), round((h.get("center") or [0, 0, 0])[2] / 2))
                       for h in hazards if isinstance(h, dict)))
    prev = _hazards_seen["sig"]
    _hazards_seen["sig"] = sig
    if not (always or sig != prev):
        return []
    if not sig:
        return ["HAZARD none in view now"] if prev and not always else []
    return render.render_hazards(hazards)


_party_seen: list[str] = []  # get_party's last party (uuids), so its sheets ride in the status read


@mcp.tool()
@_guard
def get_party() -> str:
    """Each party member's key numbers: HP, AC, level, conditions, resources (spell slots, actions), concentration."""
    t0 = time.monotonic()

    def sheet_ops(uuids):
        return [("state.character", {"uuid": u, "sections": ["brief", "resources"]}) for u in uuids]

    # Status and every sheet (of the party seen last time) + events in one tick; a changed party re-reads.
    guess = list(_party_seen)
    got, _ = _read([("state.status", {})] + sheet_ops(guess))
    st, sheets = got[0], got[1:]
    if st is None:
        st = game.rpc("state.status")  # surfaces the real error
    _remember_names(st)
    lines = []
    party = st.get("party") or []
    uuids = [c["uuid"] for c in party]
    _party_seen[:] = uuids
    if uuids != guess:
        sheets, _ = _read(sheet_ops(uuids))
    for c, sheet in zip(party, sheets):
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
    args = {"uuid": _uuid(who)} if who else {}  # no uuid: the mod uses the selected character
    if sections:
        args["sections"] = [s.strip() for s in sections.split(",") if s.strip()]
    res = _read1("state.character", args)
    _remember_names(res)
    return _finish("get_character", {"who": who, "sections": sections}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def get_actions(who: str | None = None) -> str:
    """The actions/spells/items a character can use right now (as on the hotbar), with costs and availability."""
    t0 = time.monotonic()
    res = _read1("state.actions", {"uuid": _uuid(who)} if who else {})
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
    res = _read1("state.nearby", args)
    _remember_names(res)
    if radius == 25.0 and not kinds and isinstance(res, dict):
        DELTA.observe(delta.snapshot(H, nearby=res))  # the agent has now seen these; look() won't repeat them
    return _finish("get_nearby", args, render.render_nearby(res, H), t0)


@mcp.tool()
@_guard
def look_at(target: str) -> str:
    """What the Examine panel shows for a visible character or object (stats, resistances, conditions, description)."""
    t0 = time.monotonic()
    res = _read1("state.entity", {"uuid": _uuid(target)})
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
    (st, res), _ = _read([("state.status", {}), ("state.inventory", args)])
    _remember_names(st or {})  # party first: short handles + owner names
    if res is None:
        res = game.rpc("state.inventory", args)  # surfaces the mod's error
    _remember_names(res)
    if not who and isinstance(res, dict):
        DELTA.observe({"inv": delta.snapshot(H, inventory=res)["inv"]})  # seen now; look() reports later changes
    return _finish("get_inventory", {"who": who, "filter": filter}, render.render_inventory(res, H, filter), t0)


@mcp.tool()
@_guard
def get_journal(quest: str = "") -> str:
    """Active quests with their latest journal entry, plus finished quest titles. quest: words from a title to see all of that quest's entries."""
    t0 = time.monotonic()
    res = _read1("state.journal")
    if isinstance(res, list):
        DELTA.observe(delta.snapshot(H, journal=res))
    return _finish("get_journal", {"quest": quest}, render.render_journal(res, H, quest), t0)


@mcp.tool()
@_guard
def get_map(full: bool = False) -> str:
    """Current region/subregion, unlocked waypoints, active quest objectives (distance, direction), and your own map markers.
    After the first call it shows only what changed (new waypoints, objectives added/done, region); full=true for everything."""
    t0 = time.monotonic()
    (res, mk), _ = _read([("state.map", {}), ("nav.markers", {})])
    if res is None:
        res = game.rpc("state.map")
    ms = mk.get("markers") if isinstance(mk, dict) and isinstance(mk.get("markers"), list) else None
    if ms is not None:
        _set_objectives(ms)
    first = not DELTA.has("waypoints")
    snap = delta.snapshot(H, map_=res, markers=ms)
    region = " · ".join(str(x) for x in (res.get("subregion"), res.get("region")) if x) if isinstance(res, dict) else ""
    snap["map_region"] = region
    moved_area = DELTA.last.get("map_region") not in (None, region)
    lines = DELTA.observe(snap)
    markers = _load_markers()
    if full or first or moved_area:
        body = render.compact(res, H)
        obj = render.render_objectives(ms or [])
        if obj:
            body += "\nOBJECTIVES\n" + "\n".join(obj)
        if markers:
            body += "\nMARKERS\n" + "\n".join(f"  {m['id']} {m['label']} @{render.pos(m.get('pos'))} ({m.get('region', '')})" for m in markers)
    else:
        body = "\n".join(lines) if lines else f"No map change ({region or '?'}; {len(ms or [])} objectives, {len(markers)} markers). full=true lists them."
    return _finish("get_map", {"full": full}, body, t0)


@mcp.tool()
@_guard
def get_frontier() -> str:
    """Leads worth exploring: visible doors, ladders, stairs, exits and unvisited things near you, with distance and direction. Use it to decide where to go next."""
    t0 = time.monotonic()
    res = _read1("nav.frontier", {})
    _remember_names(res)
    return _finish("get_frontier", {}, render.render_frontier(res, H), t0)


@mcp.tool()
@_guard
def route(objective: str = "", target: str = "", x: float | None = None, y: float | None = None,
          z: float | None = None, who: str | None = None, connectors: bool = False) -> str:
    """The way to a goal, read from the engine instead of a screenshot: objective (o1, or a map marker m1), a visible target id, or x,y,z.
    Says how far and how high it is (floors above/below you and at the goal), the walk in legs (8m NW, CLIMB 9m up, 6m E) with
    fire on the way, and when no path exists: the closest walkable spot and the ladders/doors in view ranked by how much they
    help (which way a ladder climbs), ending with one NEXT step. connectors=true lists ladders/doors even when a path exists."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": _uuid(who)} if who else {}  # no char: nav.route uses the active one
    t = (target or objective).strip()
    if re.fullmatch(r"[om]\d+", t) or (objective and not target):  # objective o1 / words, map marker m1
        marker = t if re.fullmatch(r"m\d+", t) else ""
        args["pos"], what = _travel_dest(marker, "" if marker else t, None, None, None)
    elif target:
        args["target"] = _uuid(target)
        what = target
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
        what = render.pos([x, y, z])
    else:
        raise ValueError("give objective=o1 (or m1), target=<id>, or x,y,z")
    if connectors:
        args["connectors"] = True
    res = _read1("nav.route", args)
    _remember_names(res)
    ends = DEAD.for_goal(args.get("pos") or (res.get("goal") if isinstance(res, dict) else None))
    return _finish("route", {"objective": objective, "target": target},
                   route_text.render_route(res, H, what, ends), t0)


@mcp.tool()
@_guard
def get_explored() -> str:
    """Where the party has been in this area (your own breadcrumbs): size of explored region and recent named places."""
    t0 = time.monotonic()
    res = _read1("nav.explored", {})
    return _finish("get_explored", {}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def path_preview(target: str = "", x: float | None = None, y: float | None = None, z: float | None = None,
                 who: str | None = None) -> str:
    """Walking distance to a target id or x,y,z: reachable?, path length, needs a jump?, fits remaining movement? Use before committing combat movement."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": _who(who)}
    if target and re.fullmatch(r"[om]\d+", target.strip()):  # objective o1 / map marker m1, as travel_to takes
        t = target.strip()
        args["pos"] = _travel_dest(t if t[0] == "m" else "", t if t[0] == "o" else "", None, None, None)[0]
    elif target:
        args["target"] = _uuid(target)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id or o1/m1> or x,y,z")
    res = game.rpc("nav.path", args)
    return _finish("path_preview", {"target": target}, render.render_path(res, H), t0)


def _route_hint(args: dict) -> str:
    """nav.route for a failed walk toward a pos/target: the height gap, the closest spot, the best ladder/door,
    and one NEXT step (route.hint_text). '' if it can't be had."""
    q = {k: args[k] for k in ("char", "pos", "target") if k in args}
    res = game.try_rpc("nav.route", q) if ("pos" in q or "target" in q) else None
    if not isinstance(res, dict):
        return ""
    _remember_names(res)
    return route_text.hint_text(res, H)


@mcp.tool()
@_guard
def walk_toward(direction: str = "", objective: str = "", target: str = "", max_m: float = 30.0,
                who: str | None = None, run: bool = True, wait: bool = False) -> str:
    """Walk as far as the ground allows (up to max_m) toward a compass direction (N, NE, E, SE, S, SW, W, NW), a quest objective (id like o1 from get_status/get_map, or words from its name), or a visible target id. Picks the farthest point with a direct walkable path, then walks there like move_to (returns within ~1 s with a job id + ETA, or the result if it already ended; wait=true blocks only in combat). Good for "head west" or "go toward the objective marker" when no door or item is in view."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": _who(who), "max_m": max_m}
    if objective:
        try:  # o2 means the o2 the agent last saw; the order changes as the party moves
            want = render.find_objective(_objectives["last"], objective)
        except ValueError:
            want = None
        fresh = _objective_markers()
        m = None
        if want is not None:
            m = next((x for x in fresh if isinstance(x, dict) and x.get("label") == want.get("label")
                      and x.get("quest") == want.get("quest")), None)
        if m is None:
            m = render.find_objective(fresh, objective)
        if not (m.get("here") and m.get("pos")):
            raise ValueError(f"objective '{render.objective_label(m)}' is not in this area")
        args["pos"] = m["pos"]
        what = f"objective {render.objective_label(m)}"
    elif target:
        args["target"] = _uuid(target)
        what = target
    elif direction:
        args["dir"] = direction.strip().upper()
        what = args["dir"]
    else:
        raise ValueError("give direction=N|NE|E|SE|S|SW|W|NW, objective=o1, or target=<id>")
    res = game.rpc("nav.reach_toward", args)
    if not isinstance(res, dict) or not res.get("reachable") or not res.get("pos"):
        n = res.get("samples") if isinstance(res, dict) else None
        hz = macros.hazard_text(res.get("hazard")) if isinstance(res, dict) else ""
        if hz:
            return _finish("walk_toward", args, f"FAILED: the way toward {what} crosses {hz}. Go around it"
                           " (another direction, get_frontier) or "
                           f"{macros.hazard_way_out(res.get('hazard'))}.", t0)
        body = (f"FAILED: no walkable ground toward {what} within {render.num(max_m)}m"
                + (f" ({n} spots checked)" if n is not None else ""))
        hint = _route_hint(args)  # toward an objective/target only (a direction has no goal)
        return _finish("walk_toward", args, body + ("\n" + hint if hint else
                                                    ". Try another direction, get_frontier, or a door."), t0)
    head = f"heading {res.get('dir') or ''} toward {what}: {render.num(res.get('straight_m', 0))}m"
    if res.get("path_m") is not None:
        head += f" (path {render.num(res['path_m'])}m)"
    if res.get("needs_jump"):
        head += ", needs a jump"
    if res.get("hazard_beyond"):
        head += f", stopping short of {macros.hazard_text(res['hazard_beyond'])}"
    if res.get("standing_in"):
        head += f" (you are standing in {res['standing_in']})"
    wait, note = _walk_wait(wait)
    body = _act("walk_toward", "act.move_to", {"char": args["char"], "pos": res["pos"], "run": run}, wait=wait,
                note=note)
    return head + "\n" + body


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
    st = game.try_rpc("state.status") or {}
    party = {c.get("uuid") for c in st.get("party") or []}
    mine = turn.party_acting(res, party) if isinstance(res, dict) else []
    cur = st.get("active") if st.get("active") in mine else (mine[0] if mine else None)
    if len(mine) > 1:
        body += ("\nSHARED TURN: " + ", ".join(_label(u) for u in mine)
                 + " can all act now, in any order (combat_turn who=..., end each one's turn)")
    if cur in party:
        acts = game.try_rpc("state.actions", {"uuid": cur})
        if acts:
            body += "\nYOUR ACTIONS (" + H.get(cur) + ")\n" + render.render_actions(acts, H)
    return _finish("get_combat_state", {}, body, t0)


@mcp.tool()
@_guard
def get_dialogue() -> str:
    """The current dialogue line and numbered answer options. A tagged option also shows its check and your speaker's bonus (plus the party's best), never the DC. Or the roll/reaction/popup on screen."""
    t0 = time.monotonic()
    ui = _read1("ui.state", context="client")
    lines = _render_ui(ui)
    return _finish("get_dialogue", {}, "\n".join(lines) or f"No dialogue on screen (screen={ui.get('screen')}).", t0)


@mcp.tool()
@_guard
def get_events() -> str:
    """Never blocks: what happened since your last tool call (combat log, quest updates, items, rolls, job_finished, queue_*), plus jobs and queues still running."""
    t0 = time.monotonic()
    body = _running_text()
    return _finish("get_events", {}, body, t0) or "No new events."


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
            who: str | None = None, run: bool = True, wait: bool = False) -> str:
    """Walk to a visible entity (target=id) or a position (x,y,z). Returns within ~1 s: the real result if it already ended (blocked, refused, arrived), else a job id + ETA while the walk goes on; keep planning meanwhile. wait=true blocks only in combat. Stops early if combat/dialogue starts. who defaults to the active character (chained companions follow). Out of combat a destination past ~58 m goes via travel_to (legs) by itself."""
    args: dict[str, Any] = {"char": _who(who), "run": run}
    if target:
        args["target"] = _uuid(target)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id> or x,y,z")
    far = None if _batch.get("in_travel") else _far_move(args["char"], args.get("target"), args.get("pos"))
    if far:  # S005: 'destination is too far away' ×4 (one move is capped at MOVE_CAP_M): walk it in legs
        dest, d = far
        out = travel_to(x=dest[0], y=dest[1], z=dest[2], who=who)
        return f"move_to: {render.num(round(d))}m is past one walk ({render.num(MOVE_CAP_M)}m): went via travel_to\n" + out
    wait, note = _walk_wait(wait)
    return _act("move_to", "act.move_to", args, wait=wait, note=note)


MOVE_CAP_M = 58.0  # just under Guard.lua's G.MAX_MOVE_DIST (60 m, from the character): beyond it act.move_to refuses


def _ent_pos(uuid: str) -> list | None:
    """A party member's position from act.watch (cheap), else the visible entity's from state.entity."""
    p = _pos_now(uuid)
    if p:
        return p
    ent = game.try_rpc("state.entity", {"uuid": uuid}) or {}
    p = ent.get("pos") if isinstance(ent, dict) else None
    return p if isinstance(p, (list, tuple)) and len(p) >= 3 else None


def _far_move(char: str, target: str | None, pos: list | None) -> tuple[list, float] | None:
    """(destination, metres) when a move is past MOVE_CAP_M out of combat (go via travel_to's legs), else None.
    In combat it stays a plain move (the guard's refusal is the honest answer there)."""
    w = _watch_conn().try_rpc("act.watch") or {}
    if w.get("combat"):
        return None
    here = next((r.get("pos") for r in w.get("party") or [] if r.get("uuid") == char), None)
    dest = _ent_pos(target) if target else pos
    if not here or not dest:
        return None
    d = macros.dist(here, dest)
    return (list(dest[:3]), d) if d > MOVE_CAP_M else None


@mcp.tool()
@_guard
def interact(target: str, who: str | None = None, wait: bool = True) -> str:
    """Use/open/activate a visible object: door, lever, chest, corpse (opens loot), book, ladder, waypoint. Walks there first. wait=false returns at once with a job id."""
    return _act("interact", "act.interact", {"char": _who(who), "target": _uuid(target)}, wait=wait)


@mcp.tool()
@_guard
def pick_up(item: str, who: str | None = None, wait: bool = True) -> str:
    """Pick up a visible item from the ground (walks there first). wait=false returns at once with a job id."""
    return _act("pick_up", "act.pickup", {"char": _who(who), "item": _uuid(item)}, wait=wait)


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
            out += "\n" + "\n".join(_render_ui(ui))
    return out


@mcp.tool()
@_guard
def loot(take: str = "all", who: str | None = None) -> str:
    """Take items from the open container/corpse window. take: "all", or comma-separated item ids (from get_inventory, which lists the open container's contents)."""
    if take.strip().lower() == "all":
        # Right after interact/loot_corpse the container panel may still be opening (live S002: "no container
        # open" then the Backpack counted as already looted): give it up to ~2 s.
        for _ in range(8):
            try:
                return _act("loot", "ui.loot", {"all": True}, context="client")
            except GameError as e:
                if "no container open" not in str(e):
                    raise
                time.sleep(0.25)
        return _act("loot", "ui.loot", {"all": True}, context="client")
    char = _who(who)
    results = [_act("loot", "act.transfer", {"item": _uuid(t.strip()), "to": char})
               for t in take.split(",") if t.strip()]
    return "\n".join(results)


@mcp.tool()
@_guard
def talk_to(npc: str, who: str | None = None, wait: bool = True) -> str:
    """Walk up to a visible character (follows them if they move) and start a conversation. The result already shows the dialogue at its first real choice (opening filler clicked through); answer with choose_dialogue."""
    return _act("talk_to", "act.talk", {"char": _who(who), "npc": _uuid(npc)}, wait_s=30, wait=wait)


@mcp.tool()
@_guard
def jump_to(x: float, y: float, z: float, who: str | None = None, wait: bool = True, force: bool = False) -> str:
    """Jump to a position (costs movement/bonus action in combat; distance limited by Strength). A landing in a harmful surface (fire, acid, ...) or on a wall, or a long jump that may fall short into one, is moved to the nearest clear spot near it within reliable reach (~4.5 m) and says so; with no clear spot near it the jump is refused. force=true jumps exactly there."""
    char = _who(who)
    pos, note = [x, y, z], ""
    if not force:
        lnd = game.try_rpc("nav.landing", {"char": char, "pos": pos})
        if isinstance(lnd, dict) and lnd.get("safe") is False:
            bad = f"in {lnd['surface']}" if lnd.get("surface") else "on a blocked tile (wall or hole)"
            if lnd.get("short_pos") and not lnd.get("blocked"):
                bad = (f"{render.num(lnd.get('dist_m', '?'))}m away, past reliable reach "
                       f"({render.num(lnd.get('reliable_m', '?'))}m): it may land short {bad}")
            else:
                bad = f"is {bad}"
            if not lnd.get("suggest"):
                if _batch["active"]:
                    _batch["last"] = "failed"
                alt = (f" The nearest clear landing in reach is {render.pos(lnd['nearest_clear'])}, "
                       f"{render.num(lnd.get('nearest_clear_off_m', '?'))}m from it." if lnd.get("nearest_clear") else "")
                return (f"jump_to: FAILED — the landing {render.pos(pos)} {bad} and no clear spot within 3m of it is "
                        f"in reach ({render.num(lnd.get('reliable_m', '?'))}m).{alt} Pick another landing "
                        "(get_frontier lists HAZARD patches) or force=true.")
            pos = list(lnd["suggest"])
            note = (f"landing {render.pos([x, y, z])} {bad}: jumping to {render.pos(pos)} instead "
                    f"({render.num(lnd.get('suggest_off_m', '?'))}m from it, {render.num(lnd.get('suggest_dist_m', '?'))}m jump, clear)")
    body = _act("jump_to", "act.cast", {"char": char, "spell": "Projectile_Jump", "pos": pos}, wait=wait)
    return f"{note}\n{body}" if note else body


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
    """Pick dialogue answer number `option` (as numbered in the last DIALOGUE shown). Then clicks through any filler that follows (Continue, a single non-committing line) and returns the next real choice, roll, or the end. Never picks a real answer for you."""
    t0 = time.monotonic()
    r = game.run_job("ui.dialogue_choose", {"i": option}, context="client", wait_s=20)
    if AUTO_DIALOGUE and r.status == "done" and not _batch["active"]:
        # Fast reflexes: click through the filler that follows the answer here and show the next real
        # choice, instead of a dialogue_advance / get_dialogue round trip per line (S002/S003).
        n, lines, stop = _dialogue_autopilot(open_s=0.3)
        body = "\n".join([_job_text("choose_dialogue", r)]
                         + ([f"then auto-advanced {n} filler line(s):"] if n else []) + lines
                         + ([f"STOPPED: {stop}"] if stop else []))
        return _finish("choose_dialogue", {"option": option}, body, t0)
    time.sleep(0.3)
    ui = game.try_rpc("ui.state", context="client") or {}
    body = _job_text("choose_dialogue", r) + "\n" + "\n".join(_render_ui(ui))
    return _finish("choose_dialogue", {"option": option}, body, t0)


@mcp.tool()
@_guard
def roll_check(boosts: str = "", reroll_on_fail: bool = False) -> str:
    """Resolve the skill-check roll on screen: optionally add boosts shown on the roll screen (comma list, e.g. "Guidance"), roll, and continue."""
    args: dict[str, Any] = {"reroll": reroll_on_fail}
    if boosts:
        args["boosts"] = [b.strip() for b in boosts.split(",") if b.strip()]
    first = _act("roll_check", "ui.roll", args, context="client", wait_s=20)
    # ui.roll stops at the result screen (so a reroll can be chosen); "roll, and continue" means one call:
    # press Continue (or reroll a failure first) here, not in a second roll_check round trip (live S002).
    roll = (_batch.get("result") or {}).get("roll") if isinstance(_batch.get("result"), dict) else None
    if _batch.get("last") != "done" or not isinstance(roll, dict) or roll.get("phase") != "result":
        return first
    if reroll_on_fail and not roll.get("success"):
        first += "\n" + _act("roll_check", "ui.roll", {"reroll": True}, context="client", wait_s=20)
        if _batch.get("last") != "done":
            return first
    return first + "\n" + _act("roll_check", "ui.roll", {}, context="client", wait_s=30)


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
                z: float | None = None, who: str | None = None, upcast: int = 0, targets: str = "",
                wait: bool = True) -> str:
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
    return _act("use_ability", "act.cast", args, wait_s=30, wait=wait)


@mcp.tool()
@_guard
def end_turn(who: str | None = None, wait: bool = True) -> str:
    """End the current character's turn; blocks through enemy turns until a party member can act or combat ends (wait=false: return right after ending it; then wait_for('my_turn'))."""
    t0 = time.monotonic()
    if who:
        char = _uuid(who)
    else:
        # The selected character, unless it can't act now and exactly one other party member can (the
        # game moves the selection within a shared turn group; ending that one is what the agent means).
        char = _active()
        cs = game.try_rpc("state.combat") or {}
        mine = turn.party_acting(cs, _party_set()) if cs.get("active") else []
        if char not in mine and len(mine) == 1:
            char = mine[0]
    args = {"char": char}
    if _session_over():
        return _finish("end_turn", args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    r = game.run_job("act.end_turn", args, wait_s=15)
    _remember_names(r.result)
    _batch["last"] = r.status
    body = _job_text("end_turn", r)
    if not _batch["active"]:
        # Name whose turn it was: in a shared turn group the game moves the selection, and the agent
        # read Lae'zel's after={...} as Claude's (claude-run-2 S001).
        body = body.replace("end_turn:", f"end_turn {_label(char)}:", 1)
    if r.status == "done" and wait and not _batch["active"]:
        body += "\n" + _wait_for_party_turn(80.0)
    elif r.status == "done" and wait:
        body += " — " + _wait_for_party_turn(80.0)
    return _finish("end_turn", args, body, t0)


# Screens that need an answer from the agent while others act.
_PROMPT_SCREENS = ("reaction", "dialogue", "roll", "popup", "game_over")


def _now_turn_text(mine: list[str]) -> str:
    """'now e1's turn', or for a shared party turn group 'now e46's and e1's turn (shared: any order)'."""
    if len(mine) == 1:
        return f"now {H.get(mine[0])}'s turn"
    return "now " + " and ".join(f"{H.get(u)}'s" for u in mine) + " turn (shared turn group: any order, end each)"


def _wait_for_party_turn(max_s: float) -> str:
    """After ending a turn: poll until a party member is the active combatant, combat ends,
    or a prompt (reaction, roll, dialogue, popup) needs an answer."""
    st = game.try_rpc("state.status") or {}
    party = {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}
    deadline = time.monotonic() + max_s
    while True:
        cs = game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            return "combat is over"
        mine = turn.party_acting(cs, party)
        if mine:
            return _now_turn_text(mine)
        ui = game.try_rpc("ui.state", context="client") or {}
        if ui.get("screen") in _PROMPT_SCREENS:
            _batch["last"] = "interrupted"
            return f"waiting stopped: {ui.get('screen')} on screen\n" + "\n".join(_render_ui(ui))
        if time.monotonic() > deadline:
            _batch["last"] = "interrupted"
            return "other combatants are still acting (use wait or get_combat_state)"
        time.sleep(0.5)


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
        # Osiris Use on a carried item; out of combat only (it would skip the
        # bonus-action cost: in combat use the item's action from get_actions).
        return _act("manage_inventory", "act.interact", {"char": _who(who), "target": _uuid(item)})
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
def long_rest(confirm: bool = True) -> str:
    """Long rest at camp (go there with camp first): consumes camp supplies, restores everything, may trigger camp scenes (handle them as dialogues). The game's questions on the way ("End the day?", "someone still wants to talk, rest anyway?") are answered Yes and listed in the result; confirm=false stops at the first one instead (then confirm(...))."""
    args: dict[str, Any] = {} if confirm else {"confirm": False}
    return _act("long_rest", "ui.long_rest", args, context="client", wait_s=90)


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
    """Level a character up in one call (out of combat). With no plan it uses your stored line for that character and level in memory/party-build.md ("- Gale L2: {...}"). plan: JSON like {"subclass":"Evocation","feat":"Ability Improvement","abilities":{"Strength":2},"spells":["Magic Missile","Shield"],"cantrips":[],"skills":[],"passives":["Agonizing Blast"]}; omitted fields take the game's defaults, except choices with no default: a subclass, spells/cantrips to learn, and passives (invocations, fighting styles). Then it fails and lists what can be picked; add them to the plan and call again."""
    uuid = _who(who)
    args: dict[str, Any] = {"uuid": uuid}
    note = ""
    if plan:
        args["plan"] = json.loads(plan)
    else:
        row = next((r for r in leveling.pending(game.try_rpc("state.status")) if r["uuid"] == uuid), None)
        stored = leveling.plan_for(MEMORY, row["name"], row["to"]) if row else None
        if stored is not None:
            args["plan"] = stored
            note = f"(plan: {row['name']} L{row['to']} from memory/party-build.md)"
        elif row:
            note = f"(no stored plan for {row['name']} L{row['to']}: game defaults)"
    out = _act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    res = _batch.get("result")
    if out.startswith("level_up: FAILED") and isinstance(res, dict) and res.get("needs_clicks") and _inp() is not None:
        # Spell picks are mouse clicks (leveling.click_spells); then the same plan again finishes the page.
        clicks = leveling.click_spells(lambda op, a: game.rpc(op, a, context="client"), _inp(), res["needs_clicks"])
        note = (note + " " if note else "") + f"(clicked spells: {', '.join(clicks['picked']) or 'none'}" + (
            f"; failed: {clicks['failed']})" if clicks["failed"] else ")")
        out = _act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    if out.startswith("level_up: done"):
        _LEVEL_NUDGE.done(uuid)
    return out


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
    """Close informational popups (tutorials, info boxes, reward/book panels). Decision popups are never closed; they're listed so you can answer with confirm."""
    t0 = time.monotonic()
    res = game.rpc("ui.dismiss", {}, context="client")
    return _finish("dismiss_popups", {}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def confirm(answer: str = "yes") -> str:
    """Answer the open Yes/No (OK/Cancel, Confirm...) question box: answer = "yes" | "no" | a button label | its number. Shows the next question if the game asks another."""
    return _act("confirm", "ui.confirm", {"answer": answer}, context="client", wait_s=8)


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
    "place_marker", "camp", "short_rest", "walk_toward", "travel_to",
}


@mcp.tool()
def do_actions(actions: str, wait: bool = True, ignore: str = "") -> str:
    """Run several actions in order in ONE call: a whole combat turn, a looting sweep, a dialogue chain. Plan from one observation, then execute.
    actions: JSON list of steps, e.g.
    [{"tool":"move_to","target":"e12"},{"tool":"use_ability","ability":"Target_MainHandAttack","target":"e12","unless":"hp_below:30"},{"tool":"use_ability","ability":"Shout_SecondWind","if":"hp_below:50"},{"tool":"end_turn"}]
    Allowed: move_to, walk_toward, interact, pick_up, loot, loot_corpse, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
    Optional per step: "if"/"unless" with in_combat, in_dialogue, my_turn, hp_below:<pct>, item_present:<id>, target_visible:<id> (a failed condition skips the step).
    Stops and drops the rest on: combat or a conversation starting, a reaction/roll/popup, a new hostile in view, HP falling below 35%, any damage taken outside combat, stepping into a harmful surface (fire, acid, ...), a blocked teleport, or a failed step. ignore="hostile,hp" disables some (combat, dialogue, prompt, hostile, hp, damage, surface, teleport).
    Returns each step's outcome, why it stopped (and what was left), a NOW line and CHANGES (position, HP, items, new things in view, mode). wait=false queues the steps instead (see queue_actions)."""
    t0 = time.monotonic()
    if not wait:
        return queue_actions(actions, ignore=ignore)
    try:
        steps = actq.parse_steps(actions, _BATCHABLE)
    except actq.StepError as e:
        return f"FAILED: {e}"
    ex = _make_executor(None, ignore)
    ex.watcher.baseline()
    it = iter(steps)
    pending: list = []

    def nxt():
        return pending.pop(0) if pending else next(it, None)

    def peek():
        if not pending:
            s_ = next(it, None)
            if s_ is not None:
                pending.append(s_)
        return pending[0] if pending else None

    rep = ex.run(nxt, peek)
    body = _report_text(rep, len(steps))
    if rep.stopped:
        _batch["last"] = "interrupted"
    changes = delta.changes_text(game, H)
    if changes:
        body += "\n" + changes
    return _finish("do_actions", {"n": len(steps)}, body, t0)


@mcp.tool()
@_guard
def queue_actions(actions: str, who: str | None = None, replace: bool = False, ignore: str = "") -> str:
    """Append steps to a character's action queue and return at once; they run back to back while you observe and plan the next move. Same step format, conditions and interrupts as do_actions. On the end you get a queue_done or queue_interrupted event (why, what was left) in any later result; wait_for('queue') blocks until it is done. replace=true drops what was still queued first. Each character has its own queue, so two characters can act at once. Your own direct action for that character pre-empts the queue."""
    t0 = time.monotonic()
    if _session_over():
        return _finish("queue_actions", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    try:
        steps = actq.parse_steps(actions, _BATCHABLE)
    except actq.StepError as e:
        return _finish("queue_actions", {}, f"FAILED: {e}", t0)
    body = _enqueue(_who(who), steps, replace, ignore)
    return _finish("queue_actions", {"n": len(steps)}, body, t0)


def _enqueue(char: str, steps: list, replace: bool = False, ignore: str | None = "") -> str:
    """Add steps to char's queue (ignore=None keeps the queue's current interrupt settings)."""
    if ignore is not None or char not in _queue_ignore:
        _queue_ignore[char] = _parse_ignore(ignore or "")
    n, mine = len(steps), []
    if not _queues().busy(char):
        # A walk started with move_to/walk_toward is still going: the queue's first action would supersede
        # it (one action per character), so the queue first waits for it to arrive.
        mine = [h for h in _running_jobs() if _async.get(h, {}).get("char") == char]
        steps = [actq.Step("wait_for", {"target": h, "timeout_s": 60}) for h in mine] + list(steps)
    METRICS.bg_start(f"queue:{char}")
    q = _queues().add(char, steps, replace=replace)
    with _queues().lock:
        waiting = len(q.steps)
    after = f", after {'/'.join(mine)} arrives" if mine else ""
    return f"queued {n} step(s) for {_label(char)} ({waiting} waiting{after})"


def _travel_async(char: str, w: dict, marker: str, objective: str, target: str, x, y, z, t0: float,
                  avoid: str = "auto", waited: bool = False, fast: bool = True) -> str:
    """travel_to without waiting: check the destination now (bad ones fail at once), then run the trip
    as a queue entry for that character (the queue worker calls travel_to again, batch-active)."""
    args: dict[str, Any] = {k: v for k, v in (("marker", marker), ("objective", objective), ("target", target),
                                              ("x", x), ("y", y), ("z", z)) if v not in ("", None)}
    if avoid != "auto":
        args["avoid_hazards"] = avoid
    dest = None
    if target:
        what, dist_m = target, None
        _uuid(target)  # an unknown id fails now
    else:
        dest, what = _travel_dest(marker, objective, x, y, z)
        me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
        dist_m = macros.dist(me.get("pos"), dest) if me.get("pos") else None
    if _session_over():
        return _finish("travel_to", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    pre = []
    if dest is not None:
        ft = _fast_travel_first(char, dest, dist_m) if fast else None
        if ft:
            pre.append(ft[0])
            dist_m = ft[1] if ft[1] is not None else dist_m
        warn = longnav.start_warning(DEAD.for_goal(dest))
        if warn:
            pre.append(warn)
    queued = _enqueue(char, [actq.Step("travel_to", args)], ignore=None)
    if dest is not None and dist_m is not None:
        _trips[char] = {"dest": dest, "d0": dist_m, "t0": time.monotonic()}
    body = "".join(p + "\n" for p in pre)
    body += f"travel_to {what}: started" + (f" ({render.num(round(dist_m, 1))}m)" if dist_m is not None else "")
    # Settle window: a trip that stops at once (hazard, no ground, an interrupt) or a short one comes back now.
    if _settle(lambda: None if _queues().busy(char) else {"status": "ended"}) is not None:
        _trips.pop(char, None)
        return _finish("travel_to", {"to": what}, body + "\n" + (_last_queue_text(char) or "queue: ended"), t0)
    prog = _trip_progress(char)
    body += (f"; {prog}" if prog else "") + f"; {queued}."
    if waited:
        body += " (wait=true ignored: the trip runs while you plan)"
    hint = _next_walk_hint()
    return _finish("travel_to", {"to": what}, body + ("\n" + hint if hint else ""), t0, issued=True)


def _fast_travel_first(char: str, dest: list, dist_m: float | None) -> tuple[str, float | None] | None:
    """A long trip with an unlocked waypoint much nearer the goal (longnav.pick_waypoint): fast travel there
    first, as a player would (S011: travel_to Zevlor 463 m on foot, ETA 6 min, with Emerald Grove Environs
    next to him). Returns (line, metres left after it) or None (no shortcut, or the fast travel failed: walk)."""
    if dist_m is None or dist_m < longnav.FT_MIN_M:
        return None
    wps = (game.try_rpc("nav.waypoints", {"pos": dest}) or {}).get("waypoints")
    here = _pos_now(char)
    wp = longnav.pick_waypoint(here, dest, wps) if here else None
    if not wp:
        return None
    r = game.run_job("ui.fast_travel", {"waypoint": wp["name"]}, context="client", wait_s=60)
    if r.status != "done":
        return (f"fast travel to {wp['name']} failed ({r.error or r.status}): walking instead", None)
    now = None
    for _ in range(20):  # the party reappears at the waypoint after the load
        p = (_watch_conn().try_rpc("act.watch") or {}).get("party") or []
        now = next((q.get("pos") for q in p if q.get("uuid") == char), None)
        if now and macros.dist(now, wp["pos"]) < 40:
            break
        time.sleep(0.5)
    left = macros.dist(now, dest) if now else None
    return (f"fast travel to {wp['name']} first ({render.num(wp['to_goal_m'])}m from it; you were "
            f"{render.num(round(dist_m))}m away): done", left)


_trips: dict[str, dict] = {}  # char -> {dest, d0, t0} of the travel_to its queue is running (live progress)
DEAD = longnav.DeadEnds()  # where trips were blocked this session (S010: the same boulder wall 5 times)


def _trip_left(char: str) -> tuple[float, float | None] | None:
    """(metres left, ETA s or None) of a running travel_to: one act.watch read, ETA from the speed so far."""
    trip = _trips.get(char)
    if not trip:
        return None
    pos = _pos_now(char)
    if not pos:
        return None
    left = macros.dist(pos, trip["dest"])
    dt = time.monotonic() - trip["t0"]
    speed = (trip["d0"] - left) / dt if dt > 0.3 else 0.0
    return left, (left / speed if speed > 0.5 else None)


def _trip_progress(char: str) -> str:
    """'31m left, ETA ~7s' for a running travel_to."""
    tl = _trip_left(char)
    if tl is None:
        return ""
    left, eta = tl
    s = f"{render.num(round(left))}m left"
    if eta is not None:
        s += f", ETA ~{render.num(round(eta))}s"
    return s


# S005: 11 gaps >10 s, most with Edit/Write of notes right after a walk ENDED (the character stood still while
# the agent wrote). A result that sees a long walk running says once that this is the window for notes.
NOTES_MIN_S = 8.0     # a job/trip with at least this much left gets the hint
TRIP_MPS = 2.0        # travel_to ETA before its speed is known (S005: 88 m in ~50 s, legs included)
_notes_shown: set = set()


def _long_running() -> tuple[str, float] | None:
    """(key, seconds left) of the longest-running async job or travel_to trip, if any."""
    best = None
    for h in _running_jobs():
        info = _async[h]
        if info.get("eta") is not None:
            left = float(info["eta"]) - (time.monotonic() - info["t0"])
            if best is None or left > best[1]:
                best = (f"{h}@{info['t0']}", left)
    for c, trip in list(_trips.items()):
        if not _queues().busy(c):
            continue
        tl = _trip_left(c)
        if tl is None:
            continue
        left = tl[1] if tl[1] is not None else tl[0] / TRIP_MPS
        if best is None or left > best[1]:
            best = (f"{c}@{trip['t0']}", left)
    return best


def _notes_window() -> str:
    """'NOTES WINDOW: ~20s of walking left ...' once per long job/trip ('' otherwise)."""
    lr = _long_running()
    if lr is None or lr[1] < NOTES_MIN_S or lr[0] in _notes_shown:
        return ""
    _notes_shown.add(lr[0])
    return (f"NOTES WINDOW: ~{render.num(round(lr[1]))}s of walking left: write memory notes / plan now "
            "(not after it ends, when the character would stand idle); queue_actions the arrival first")


@mcp.tool()
@_guard
def cancel_queue(who: str | None = None) -> str:
    """Stop a character's queue (who) or every queue (who empty): the running step is cancelled and the rest dropped."""
    t0 = time.monotonic()
    out = _queues().cancel(_uuid(who) if who else None)
    if not out:
        return _finish("cancel_queue", {}, "no queue was running", t0)
    lines = [f"cancelled {_label(w)}: " + (f"stopped {run}, " if run else "") + f"dropped {n}" for w, n, run in out]
    return _finish("cancel_queue", {}, "\n".join(lines), t0)


_PROMPT_SCREENS_WAIT = ("reaction", "dialogue", "roll", "popup", "game_over")


@mcp.tool()
@_guard
def wait_for(target: str = "idle", timeout_s: float = 15.0) -> str:
    """Block only until something happens, then report it plus events and CHANGES. target: a job id (j12) from an async action; 'queue' (all queues done) or 'queue:<who>'; 'idle' (no async job or queue running); 'my_turn' (a party member can act, or combat ended); or event kinds, comma-separated (e.g. 'combat_started,dialogue_started,job_finished'). Also returns early when a reaction/roll/dialogue/popup appears."""
    t0 = time.monotonic()
    timeout_s = max(0.1, min(float(timeout_s), 60.0))
    deadline = t0 + timeout_s
    tgt = (target or "idle").strip()
    low = tgt.lower()
    ui_at = 0.0
    screen0 = None
    party = None
    body = None
    while body is None:
        now = time.monotonic()
        if re.fullmatch(r"j\d+", low):
            if _batch["active"] and _batch["exec"] is not None and low in _async:
                _batch["exec"].track(_async[low]["ctx"], _async[low]["id"])  # cancel_queue cancels it too
            view = game.rpc("job.poll", {"id": int(low[1:])})
            if view.get("status") != "running":
                info = _async.pop(low, {})
                METRICS.bg_end(low)
                r = JobResult(view["status"], view.get("result"), view.get("error"),
                              elapsed_ms=view.get("elapsed_ms", 0))
                _remember_names(r.result)
                body = _job_text(info.get("tool") or low, r)
        elif low == "idle":
            if not _queues().busy() and not _running_jobs():
                body = "idle: no action running"
        elif low == "queue" or low.startswith("queue:"):
            who = _uuid(tgt.split(":", 1)[1]) if ":" in tgt else None
            if not _queues().busy(who):
                body = _last_queue_text(who) or "queue: empty"
        elif low == "my_turn":
            if party is None:
                st = game.try_rpc("state.status") or {}
                party = {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}
            cs = game.try_rpc("state.combat") or {}
            if not cs.get("active"):
                body = "not in combat"
            elif turn.party_acting(cs, party):
                body = _now_turn_text(turn.party_acting(cs, party))
        else:
            kinds = {k.strip() for k in tgt.split(",") if k.strip()}
            hit = _peek_events(kinds)
            if hit:
                body = f"{hit.get('kind')} happened"
            elif now - t0 >= IDLE_EVENT_WAIT_S and screen0 in (None, "game") \
                    and not _queues().busy() and not _running_jobs():
                # S009: 'dialogue_started' after a finished 3 m walk waited its full 10 s, and the last minutes
                # went to 45+30+40 s waits with nothing running. Nothing of ours can cause it: say so now.
                body = (f"nothing is running that would cause {tgt} (no job, no queue, no prompt) after "
                        f"{render.num(round(now - t0, 1))}s: act instead (talk_to, move_to, queue_actions)")
        if body is not None:
            break
        if now - ui_at >= 0.5:
            ui_at = now
            ui = game.try_rpc("ui.state", context="client") or {}
            scr = ui.get("screen") if isinstance(ui, dict) else None
            if scr == "dialogue" and "dialogue_started" in low:
                # Already open (its event went out in an earlier result): live S002 waited the full 5 s here.
                body = "dialogue open\n" + "\n".join(_render_ui(ui))
                break
            if screen0 is None:
                screen0 = scr
            elif scr in _PROMPT_SCREENS_WAIT and scr != screen0:
                body = f"stopped waiting: {scr} on screen\n" + "\n".join(_render_ui(ui))
                break
        if now > deadline:
            body = f"still waiting for {tgt} after {render.num(round(timeout_s, 1))}s" + (
                "\n" + _running_text() if _running_text() else "")
            break
        time.sleep(0.1)
    changes = delta.changes_text(game, H)
    if changes:
        body += "\n" + changes
    return _finish("wait_for", {"target": tgt}, body, t0)


# ---- queue plumbing ----

_watch_game: Game | None = None  # own LuaDebug connection for interrupt checks (never blocks the agent's calls)
_queue_mgr: actq.QueueManager | None = None
_queue_ignore: dict[str, set] = {}


def _watch_conn() -> Game:
    global _watch_game
    if _watch_game is None:
        _watch_game = Game(port=game.port)
    return _watch_game


def _parse_ignore(ignore: str) -> set:
    out = {x.strip().lower() for x in (ignore or "").split(",") if x.strip()}
    bad = out - set(actq.INTERRUPT_KINDS)
    if bad:
        raise ValueError(f"unknown interrupt kind(s) {', '.join(sorted(bad))}; use {', '.join(actq.INTERRUPT_KINDS)}")
    return out


def _accepts(fn, name: str) -> bool:
    import inspect
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _call_step(ex: actq.Executor, step: actq.Step, who: str | None) -> tuple[str, str, Any]:
    """Run one step's tool in this thread, batch-quiet, with ex receiving the job id (for cancelling)."""
    fn = globals()[step.tool]
    args = dict(step.args)
    if who and not args.get("who") and _accepts(fn, "who"):
        args["who"] = H.get(who)
    saved = dict(_batch.d)
    _batch["active"], _batch["exec"], _batch["last"], _batch["result"] = True, ex, "done", None
    try:
        text = fn(**args)
        return text, _batch["last"], _batch["result"]
    finally:
        _batch.d.update(saved)


def _predicate(name: str, arg: str, who: str | None, _w: dict) -> bool:
    w = _watch_conn().try_rpc("act.watch") or {}
    if name == "in_combat":
        return bool(w.get("combat"))
    if name == "in_dialogue":
        return bool(w.get("dialogue"))
    if name == "hp_below":
        rows = [r for r in w.get("party") or [] if who is None or r.get("uuid") == who] or (w.get("party") or [])
        lim = float(arg)
        return any(actq._pct(r) is not None and actq._pct(r) < lim for r in rows)
    if name == "my_turn":
        cs = _watch_conn().try_rpc("state.combat") or {}
        return bool(cs.get("active")) and (who or _active()) in turn.acting(cs)
    if name in ("item_present", "target_visible"):
        try:
            u = _uuid(arg)
        except ValueError:
            return False
        ts = _watch_conn().try_rpc("act.target_state", {"uuid": u}) or {}
        if name == "item_present":
            return bool(ts.get("visible") or ts.get("held"))
        return bool(ts.get("visible")) and not ts.get("held")
    return False


def _make_executor(who: str | None, ignore: str | set = "") -> actq.Executor:
    ign = ignore if isinstance(ignore, set) else _parse_ignore(ignore)
    return _new_executor(actq.Watcher(_watch_conn(), ignore=ign), _watch_conn(), who)


def _new_executor(watcher: actq.Watcher, wg: Game, who: str | None) -> actq.Executor:
    holder: dict = {}
    ex = actq.Executor(lambda step, w: _call_step(holder["ex"], step, w), watcher,
                       lambda ctx, jid: wg.rpc("job.cancel", {"id": jid}, ctx), who=who, predicate=_predicate)
    holder["ex"] = ex
    return ex


def _label(text: str) -> str:
    """uuids in a reason -> "e14 Goblin" (handles plus the last known name)."""
    def sub(m: re.Match) -> str:
        h = H.get(m.group(0))
        n = H.names.get(h)
        return f"{h} {n}" if n else h
    return render.UUID_RE.sub(sub, text)


def _report_text(rep: actq.Report, n: int) -> str:
    lines = list(rep.lines)
    if rep.stopped:
        tail = f"STOPPED: {_label(rep.stopped)}"
        if rep.left:
            tail += f"; not run: {', '.join(rep.left)}"
        lines.append(tail)
    after = rep.last_result.get("after") if isinstance(rep.last_result, dict) else None
    if isinstance(after, dict):
        keep = {k: after[k] for k in ("hp", "max_hp", "action", "bonus", "movement", "my_turn") if k in after}
        if keep:
            lines.append("NOW " + render.compact(keep, H))
    return "\n".join(lines)


def _on_queue_report(who: str, rep: actq.Report) -> None:
    METRICS.bg_end(f"queue:{who}")
    data: dict[str, Any] = {"who": who, "done": rep.done}
    if rep.skipped:
        data["skipped"] = rep.skipped
    if rep.stopped:
        data["why"] = _label(rep.stopped)
        if rep.left:
            data["left"] = rep.left
        if rep.lines:
            data["last"] = H.scrub(rep.lines[-1])[:160]
    kind = "queue_interrupted" if rep.stopped else "queue_done"
    try:
        _watch_conn().rpc("act.note", {"kind": kind, "data": data})
    except GameError:
        pass
    if who not in _stay:
        _rechain_after(who)


def _rechain_after(who: str) -> None:
    """A companion's own queue ended (walked, interacted, came back from regroup): chain it back to the
    leader so the next walk takes it along (S005: an unchained Shadowheart stayed 77 m behind)."""
    try:
        st = game.try_rpc("state.status") or {}
        lead = st.get("active")
        if not lead or lead == who or st.get("in_combat") or st.get("in_dialogue"):
            return
        row = next((r for r in st.get("party") or [] if r.get("uuid") == who), None)
        if row and row.get("chained") is False and not row.get("dead") and not row.get("downed"):
            _rechain([who], lead)
    except (GameError, ValueError, TypeError):
        pass


def _queues() -> actq.QueueManager:
    global _queue_mgr
    if _queue_mgr is None:
        _queue_mgr = actq.QueueManager(lambda who: _new_executor(
            actq.Watcher(_watch_conn(), ignore=_queue_ignore.get(who, set())), _watch_conn(), who), _on_queue_report)
    return _queue_mgr


def _last_queue_text(who: str | None) -> str:
    qs = [q for w, q in _queues().queues.items() if (who is None or w == who) and q.last_report is not None]
    if not qs:
        return ""
    q = max(qs, key=lambda x: x.finished_at)
    rep = q.last_report
    head = f"queue {_label(q.who)}: " + (f"STOPPED: {_label(rep.stopped)}" if rep.stopped else f"done ({rep.done} steps)")
    if rep.stopped and rep.left:
        head += f"; not run: {', '.join(rep.left)}"
    return head + ("\n" + "\n".join(rep.lines[-4:]) if rep.lines else "")


# One tool result asks twice (RUNNING line, notes window; S009: two job.poll RPCs per job and two act.watch
# reads per trip on every result). Inside _finish's tail ("on") the first answer is reused.
_running_memo: dict[str, Any] = {"on": False, "keys": None, "out": None}


def _running_jobs() -> list[str]:
    """Async jobs still running (forgets settled ones). All jobs of a context are polled in one eval."""
    keys = tuple(sorted(_async))
    if _running_memo["on"] and _running_memo["keys"] == keys and _running_memo["out"] is not None:
        return list(_running_memo["out"])
    items = list(_async.items())
    views: dict[str, Any] = {}
    for ctx in {info["ctx"] for _, info in items}:
        mine = [(h, info) for h, info in items if info["ctx"] == ctx]
        try:
            res = delta.multi_rpc(game, [("job.poll", {"id": info["id"]}) for _, info in mine], ctx)
        except GameError:
            res = [None] * len(mine)
        views.update({h: v for (h, _), v in zip(mine, res)})
    out = []
    for h, info in items:
        view = views.get(h)
        if not isinstance(view, dict) or view.get("status") != "running":
            _async.pop(h, None)
            METRICS.bg_end(h)
            continue
        out.append(h)
    if _running_memo["on"]:
        _running_memo.update(keys=tuple(sorted(_async)), out=list(out))
    return out


def _running_text() -> str:
    parts = []
    for h in _running_jobs():
        info = _async[h]
        s_ = f"{h} {info['tool']}"
        if info.get("eta") is not None:
            left = float(info["eta"]) - (time.monotonic() - info["t0"])
            s_ += f" ~{render.num(round(max(0.0, left), 1))}s left"
        parts.append(s_)
    qs = _queues().status()
    for q in qs:
        s_ = f"queue {_label(q['who'])}: " + (q["running"] or "starting")
        if (q["running"] or "").startswith("travel_to"):
            prog = _trip_progress(q["who"])
            if prog:
                s_ += f" ({prog})"
        if q["waiting"]:
            s_ += f" then {', '.join(q['waiting'][:4])}" + (f" +{len(q['waiting']) - 4}" if len(q["waiting"]) > 4 else "")
        parts.append(s_)
    running = {q["who"] for q in qs}
    for c in [c for c in _trips if c not in running]:
        _trips.pop(c, None)
    if not parts:
        return ""
    return "RUNNING " + " | ".join(parts) + (" (queue_actions appends after it)" if qs else "")


IDLE_EVENT_WAIT_S = 4.0  # wait_for <event kinds> with no job, queue or prompt running gives up after this


def _peek_events(kinds: set) -> dict | None:
    """First event of one of `kinds` since the agent's last tool call, without consuming it
    (the result's EVENTS section still lists everything)."""
    def since(ctx):
        return game.try_rpc("events.since", {"seq": game.event_seq[ctx], "limit": 300}, context=ctx)

    got = _par(lambda: since("server"), lambda: since("client"))  # both contexts in one tick
    for ctx, res in zip(("server", "client"), got):
        stashed = game.stashed_events(ctx) if hasattr(game, "stashed_events") else []  # read() took them
        for ev in stashed + ((res.get("events") or []) if isinstance(res, dict) else []):
            if ev.get("kind") in kinds:
                return ev
    return None


# ---------------------------------------------------------------------------
# a whole combat turn in one call (turn.py), and macros (macros.py)
# ---------------------------------------------------------------------------

def _run_one(watcher: actq.Watcher, who: str | None, step: actq.Step,
             outer: actq.Executor | None = None) -> tuple[actq.Report, str]:
    """One step through the executor (guarded tool, interrupts watched, job cancelled on an interrupt).
    outer: the queue/batch executor running this macro, if any; it also tracks the step's job, so
    cancel_queue or an interrupt it sees cancels the step. Returns (report, the step's line without
    its number)."""
    ex = _new_executor(watcher, _watch_conn(), who)
    if outer is not None:
        own = ex.track

        def track(ctx: str, jid: int) -> None:
            own(ctx, jid)
            outer.track(ctx, jid)
        ex.track = track
    it = iter([step])
    rep = ex.run(lambda: next(it, None), lambda: None)
    text = re.sub(r"^\d+\. ", "", rep.lines[0]) if rep.lines else step.short()
    return rep, text


def _outcome(step: actq.Step, text: str) -> str:
    """'use_ability: done — ...' -> 'Target_MainHandAttack e12: done — ...' (what was tried, then how it went)."""
    body = text.split(": ", 1)[1] if text.startswith(step.tool + ":") or text.startswith(step.short() + ":") else text
    a = step.args
    what = " ".join(str(x) for x in (a.get("ability"), a.get("target")) if x) if step.tool == "use_ability" else step.short()
    return f"{what}: {body}"


def _after(rep: actq.Report, keep: dict) -> None:
    a = rep.last_result.get("after") if isinstance(rep.last_result, dict) else None
    if isinstance(a, dict):
        keep.update(a)


def _now_line(after: dict) -> str:
    keep = {k: after[k] for k in ("hp", "max_hp", "action", "bonus", "movement", "reaction") if k in after}
    return "NOW " + render.compact(keep, H) if keep else ""


def _party_set() -> set:
    st = game.try_rpc("state.status") or {}
    return {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}


def _turn_check(who: str, step: actq.Step | None) -> tuple[bool, str]:
    """Before each turn step, from what the player sees: (fatal, why). fatal = the turn is over for us;
    a non-fatal why (target dead / out of view) skips just this alternative."""
    cs = game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        return True, "combat is over"
    now = turn.acting(cs)
    if now and who not in now:
        return True, f"it is no longer {_label(who)}'s turn (now {', '.join(_label(u) for u in now)})"
    tgt = step.args.get("target") if step is not None else None
    if tgt:
        try:
            u = _uuid(str(tgt))
        except ValueError:
            return False, f"unknown target {tgt}"
        ts = game.try_rpc("act.target_state", {"uuid": u})
        if isinstance(ts, dict):
            if ts.get("dead"):
                return False, f"{_label(u)} is already dead"
            if not ts.get("visible"):
                return False, f"{_label(u)} is not visible"
    return False, ""


def _approach(who: str, tu: str | None, mv: turn.Move, within: float, budget: float | None = None) -> dict | None:
    """nav.approach: where to walk along the real route to get within `within` m (None: the op is missing)."""
    args: dict = {"char": who, "within": within, "avoid": mv.avoid}
    args.update({"target": tu} if tu else {"pos": mv.pos})
    if budget is not None:
        args["budget_m"] = budget
    r = game.try_rpc("nav.approach", args)
    return r if isinstance(r, dict) and ("pos" in r or "reachable" in r) else None


def _short_why(ap: dict | None) -> str:
    """Why an approach stops where it does, as the agent should hear it ('' = it reaches range)."""
    if not ap:
        return ""
    if ap.get("reachable") is False:
        return str(ap.get("reason") or "no path to it")
    lim = ap.get("limit")
    if lim == "movement":
        need = ap.get("need_m") or ap.get("route_m")
        left = ap.get("movement_left")
        bits = ([f"{render.num(left)}m left"] if left is not None else []) + \
            ([f"the route needs {render.num(need)}m"] if need is not None else [])
        return "out of movement" + (f" ({', '.join(bits)})" if bits else "")
    if lim == "hazard":
        h = ap.get("hazard") or {}
        return (f"kept out of {h.get('surface', 'a harmful surface')} on the route {render.num(h.get('at_m', 0))}m ahead"
                " (move avoid:false walks through)")
    if lim == "route_end":
        return f"no walkable route gets closer than {render.num(ap.get('end_m', 0))}m (raised/blocked: ranged, jump or another way)"
    return ""


def _turn_move(mv: turn.Move, who: str, watcher: actq.Watcher, lines: list, after: dict,
               info: dict | None = None) -> str:
    """The move slot: 'ok', 'none' (couldn't move; the turn goes on, fallbacks may still work) or 'stop'.
    A move toward a target that ends out of `within` says how far it got and why; info["short"] gets that
    reason ("movement" / "hazard" / "route_end" / "no_path" / "early") for combat_turn's fallbacks."""
    info = info if info is not None else {}
    info.pop("short", None)
    fatal, why = _turn_check(who, None)
    if fatal:
        lines.append(f"move: {why}")
        return "stop"
    what = mv.target or render.pos(mv.pos)
    tu = _uuid(mv.target) if mv.target else None
    step = None
    ap = None

    def dist_now() -> float | None:
        return turn.straight_dist(game.try_rpc("state.combat") or {}, who, tu) if tu else None

    def short_note(d2: float | None, ap2: dict | None) -> str:
        want = mv.within if mv.within is not None else turn.MELEE_M
        if d2 is None or d2 <= want + 0.3:
            return ""
        w = _short_why(ap2)
        info["short"] = (ap2 or {}).get("limit") or "early"
        return f" — stopped short of ≤{render.num(want)}m: " + (w or "the walk ended early (blocked or bumped?)")

    if tu and mv.within is not None:
        d = dist_now()
        if d is not None and d <= mv.within:
            lines.append(f"move: already within {render.num(mv.within)}m of {what} ({render.num(d)}m)")
            return "ok"
        ap = _approach(who, tu, mv, mv.within)
        if ap is not None:
            if ap.get("reachable") is False or not ap.get("pos") or float(ap.get("walk_m") or 0) < 0.5:
                info["short"] = "no_path" if ap.get("reachable") is False else (ap.get("limit") or "no_path")
                lines.append(f"move {what}: stays put, {render.num(d) if d is not None else '?'}m away — "
                             + (_short_why(ap) or "no way closer"))
                return "none"
            x, y, z = ap["pos"]
            step = actq.Step("move_to", {"x": x, "y": y, "z": z})
        elif d is not None:  # a mod without nav.approach: straight-line samples
            r = game.try_rpc("nav.reach_toward", {"char": who, "target": tu, "max_m": max(0.5, d - mv.within + 0.3)})
            if not (isinstance(r, dict) and r.get("reachable") and r.get("pos")):
                # Don't walk all the way up to a target we only wanted to be in range of.
                lines.append(f"move {what}: no walkable ground toward it ({render.num(d)}m, want ≤{render.num(mv.within)}m)")
                info["short"] = "no_path"
                return "none"
            x, y, z = r["pos"]
            step = actq.Step("move_to", {"x": x, "y": y, "z": z})
    if step is None:
        step = actq.Step("move_to", {"target": mv.target} if mv.target else dict(zip("xyz", mv.pos)))
    rep, text = _run_one(watcher, who, step)
    _after(rep, after)
    if rep.done:
        res = text.split(': ', 1)[-1]
        if tu and "target" not in step.args:
            # A walk to a point near the target: its dist= is to that point, not to the target.
            d2 = dist_now()
            res = re.sub(r"\s*dist=[\d.]+", "", res).rstrip(" —")
            if d2 is not None:
                res += f", now {render.num(d2)}m from {what}" + short_note(d2, ap)
        lines.append(f"move {what}: {res}")
        return "ok"
    text = text.split(": ", 1)[-1] if text.startswith("move_to:") else text
    if rep.stop_kind not in (None, "failed"):
        lines.append(f"move {what}: {text}")
        lines.append(f"STOPPED: {_label(rep.stopped or '')}")
        return "stop"
    if turn.is_fatal(text):
        lines.append(f"move {what}: {text}")
        return "stop"
    left = turn.movement_left(text)
    if mv.partial and turn.move_too_long(text) and (left is None or left >= 1.0):
        if left is None:
            p = game.try_rpc("nav.path", {"char": who, **({"target": tu} if tu else {"pos": mv.pos})}) or {}
            left = p.get("movement_left") if isinstance(p, dict) else None
        if isinstance(left, (int, float)) and left >= 1.0:
            budget = float(left) - 0.3
            ap = _approach(who, tu, mv, mv.within if mv.within is not None else turn.MELEE_M, budget)
            if ap is not None:
                r = ap if ap.get("reachable") and ap.get("pos") and float(ap.get("walk_m") or 0) >= 0.5 else None
                if r is None:
                    info["short"] = ap.get("limit") or "no_path"
                    lines.append(f"move {what}: {text} → can't get closer: {_short_why(ap) or 'no way closer'}")
                    return "none"
            else:
                r = game.try_rpc("nav.reach_toward", {"char": who, **({"target": tu} if tu else {"pos": mv.pos}),
                                                      "max_m": budget})
            if isinstance(r, dict) and r.get("reachable") and r.get("pos"):
                x, y, z = r["pos"]
                rep2, text2 = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
                _after(rep2, after)
                res2 = text2.split(': ', 1)[-1]
                if rep2.done and tu:
                    d2 = dist_now()
                    res2 = re.sub(r"\s*dist=[\d.]+", "", res2).rstrip(" —")
                    if d2 is not None:
                        res2 += f", now {render.num(d2)}m from {what}" + short_note(d2, ap)
                lines.append(f"move {what}: {text} → partial {render.num(budget)}m toward it: {res2}")
                if rep2.done:
                    if tu is None:
                        info["short"] = "movement"
                    return "ok"
                if rep2.stop_kind not in (None, "failed"):
                    lines.append(f"STOPPED: {_label(rep2.stopped or '')}")
                    return "stop"
                return "none"
    if turn.move_too_long(text):
        info["short"] = "movement"
    lines.append(f"move {what}: {text}")
    return "none"


def _turn_slot(name: str, alts: list, who: str, watcher: actq.Watcher, lines: list,
               after: dict) -> tuple[str, bool]:
    """Try a slot's alternatives in order. ('ok'|'none'|'stop', then_move of the one that worked)."""
    tried: list[str] = []
    for alt in alts:
        fatal, why = _turn_check(who, alt.step)
        if fatal:
            lines.append(f"{name}: " + " → ".join(tried + [why]))
            return "stop", False
        if why:
            tried.append(f"{alt.step.short()}: skipped ({why})")
            continue
        rep, text = _run_one(watcher, who, alt.step)
        text = _outcome(alt.step, text)
        _after(rep, after)
        if not rep.done and rep.stop_kind in (None, "failed"):
            step_in = _step_into_range(who, alt.step, text, watcher, after)
            if step_in:
                tried.append(f"{text} → {step_in}")
                rep, text = _run_one(watcher, who, alt.step)
                text = _outcome(alt.step, text)
                _after(rep, after)
        if rep.done:
            lines.append(f"{name}: " + " → ".join(tried + [text]))
            return "ok", alt.then_move
        tried.append(text)
        if rep.skipped:
            continue
        if rep.stop_kind not in (None, "failed") or turn.is_fatal(text):
            lines.append(f"{name}: " + " → ".join(tried))
            if rep.stop_kind not in (None, "failed"):
                lines.append(f"STOPPED: {_label(rep.stopped or '')}")
            return "stop", False
    if alts:
        lines.append(f"{name}: " + " → ".join(tried) + (" (no alternative worked)" if len(alts) > 1 else ""))
    return "none", False


def _step_into_range(who: str, step: actq.Step, text: str, watcher: actq.Watcher, after: dict) -> str:
    """combat_turn: a cast just out of range (S002: Sacred Flame at 18.6 m, range 18 m) walks into range
    along the real route when the movement left covers it; the caller then casts again. Returns what it
    did ('stepped 0.9m into range'), or '' (not a range failure, too far, no route, not enough movement)."""
    gap = macros.range_gap(text) if step.tool == "use_ability" else None
    if not gap or gap[0] - gap[1] > macros.STEP_INTO_RANGE_M:
        return ""
    a = step.args
    args: dict = {"char": who, "within": max(0.5, gap[1] - 0.3), "avoid": True}
    if a.get("target"):
        try:
            args["target"] = _uuid(str(a["target"]))
        except ValueError:
            return ""
    elif all(a.get(k) is not None for k in "xyz"):
        args["pos"] = [float(a[k]) for k in "xyz"]
    else:
        return ""
    ap = game.try_rpc("nav.approach", args)  # budget: the movement left (the op's default in combat)
    if not (isinstance(ap, dict) and ap.get("reachable") and ap.get("limit") == "in_range" and ap.get("pos")):
        return ""
    x, y, z = ap["pos"]
    rep, _text = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
    _after(rep, after)
    return f"stepped {render.num(ap.get('walk_m', '?'))}m into range" if rep.done else ""


AUTO_RANGED = ("Projectile_MainHandAttack", "Projectile_OffhandAttack")


def _turn_auto_fallback(p: turn.TurnPlan, slot: str, planned: list, who: str, watcher: actq.Watcher,
                        lines: list, after: dict, info: dict) -> str:
    """Every alternative of a slot missed its target (claude-run-2: S002 the move toward Zhalk stopped short,
    S002 the planned imp died first, S007 two casts with no line of sight wasted the action):
      1. out of range: the ranged weapon attack on the same target if it is within its range;
      2. retarget: the plan's own targeted abilities on the nearest other hostile in sight and in reach;
      3. action slot, the move ran out of movement: Dash and walk on.
    Only abilities the character has available; retarget=false in the plan turns 2 off."""
    miss = lines[-1] if lines else ""
    tgt = (p.move.target if p.move else None) or next(
        (a.step.args.get("target") for a in planned if a.step.args.get("target")), None)
    tu = None
    if tgt:
        try:
            tu = _uuid(str(tgt))
        except ValueError:
            tu = None
    acts = game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    avail = {a.get("spell"): a for a in acts or [] if isinstance(a, dict) and a.get("available") is not False}
    tried = {a.step.args.get("ability") for a in planned}
    cs = game.try_rpc("state.combat") or {}
    alts: list[turn.Alt] = []
    if tu and "out of range" in miss:
        d = turn.straight_dist(cs, who, tu)
        for sp in AUTO_RANGED:
            rng = (avail.get(sp) or {}).get("range")
            if sp in avail and sp not in tried and (d is None or not isinstance(rng, (int, float)) or d <= rng):
                alts.append(turn.Alt(actq.Step("use_ability", {"ability": sp, "target": str(tgt)})))
                break
    if p.retarget:
        planned_targets = set()
        for a in planned:
            t = a.step.args.get("target")
            if t:
                try:
                    planned_targets.add(_uuid(str(t)))
                except ValueError:
                    pass
        used: set = set()
        for a in planned:
            sp, t = a.step.args.get("ability"), a.step.args.get("target")
            if a.step.tool != "use_ability" or not sp or not t or sp in used or sp not in avail:
                continue
            used.add(sp)
            rng = avail[sp].get("range")
            for u in turn.hostiles_in_reach(cs, who, rng if isinstance(rng, (int, float)) else None,
                                            planned_targets)[:2]:
                args = {k: v for k, v in a.step.args.items() if k not in ("target", "targets", "x", "y", "z")}
                alts.append(turn.Alt(actq.Step("use_ability", {**args, "target": H.get(u)})))
        alts = alts[:1 + turn.MAX_RETARGET]
    if (slot == "action" and p.move and info.get("short") == "movement" and "Shout_Dash" in avail
            and "Shout_Dash" not in tried):
        alts.append(turn.Alt(actq.Step("use_ability", {"ability": "Shout_Dash"}), then_move=True))
    if not alts:
        return "none"
    res, then_move = _turn_slot(f"{slot} (auto fallback)", alts, who, watcher, lines, after)
    if res == "ok" and then_move and p.move:
        res = _turn_move(p.move, who, watcher, lines, after, info)
    return res


def _turn_auto_bonus(who: str, watcher: actq.Watcher, lines: list, after: dict) -> str:
    """No bonus action was planned: if an ally is downed and this character can Healing Word them, do it
    (a downed ally loses its turns and dies on 3 failed saves; the bonus action was unused in ~all
    claude-run-2 turns). 'ok' | 'none' | 'stop'."""
    if after.get("bonus") == 0:
        return "none"
    cs = game.try_rpc("state.combat") or {}
    if not any(o.get("relation") == "party" and o.get("hp_pct") == 0 and not o.get("dead")
               for o in cs.get("order") or []):
        return "none"
    acts = game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    avail = {a.get("spell"): a for a in acts or [] if isinstance(a, dict) and a.get("available") is not False}
    alts = []
    for sp in turn.BONUS_HEALS:
        if sp in avail:
            rng = avail[sp].get("range")
            for u in turn.downed_allies(cs, who, float(rng) if isinstance(rng, (int, float)) else 18.0)[:2]:
                alts.append(turn.Alt(actq.Step("use_ability", {"ability": sp, "target": H.get(u)})))
    if not alts:
        return "none"
    return _turn_slot("bonus (auto: ally downed)", alts, who, watcher, lines, after)[0]


def _wait_turn(ready, max_s: float) -> tuple[dict, str]:
    """combat_turn sent ahead (planned while enemies act): poll until ready(combat) or combat ends, a prompt
    needs an answer, or max_s passes. (last state.combat, why it stopped waiting or '')."""
    deadline = time.monotonic() + max_s
    n = 0
    while True:
        cs = game.try_rpc("state.combat") or {}
        if not cs.get("active") or ready(cs):
            return cs, ""
        n += 1
        if n % 3 == 0:
            ui = game.try_rpc("ui.state", context="client") or {}
            if ui.get("screen") in _PROMPT_SCREENS:
                return cs, f"{ui.get('screen')} on screen"
        if time.monotonic() > deadline:
            return cs, "timed out"
        time.sleep(0.3)


def _answer_reaction(use: bool) -> str:
    r = _watch_conn().run_job("ui.react", {"use": use}, context="client", wait_s=10)
    return f"reaction prompt: {'used' if use else 'declined'} ({r.status})"


def _turn_next(who: str, party: set) -> str:
    """After the turn: who acts next, and the next party member's actions if it is already their turn."""
    cs = game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        return "combat is over: loot, heal, then end_combat_report"
    others = [u for u in turn.party_acting(cs, party) if u != who]
    cur = others[0] if others else None
    if cur:
        out = f"NOW {_label(cur)}'s turn: plan it"
        if len(others) > 1:
            out += f"; also {', '.join(_label(u) for u in others[1:])}"
        acts = game.try_rpc("state.actions", {"uuid": cur})
        if acts:
            out += "\nYOUR ACTIONS (" + H.get(cur) + ")\n" + render.render_actions(acts, H)
        return out
    text, mine = turn.next_up(cs, party, _label, ended=who)
    if mine:
        text += ". Plan it while they act and send its combat_turn now (it waits for the turn)"
    return text


@mcp.tool()
@_guard
def combat_turn(plan: str, who: str | None = None, end: bool = True, wait_next: bool = False,
                ignore: str = "hostile", wait_turn: float = 30.0) -> str:
    """A WHOLE combat turn in one call: move, action, bonus action, extra attacks, end turn. Each slot lists alternatives tried in order until one works (out of range after the move → the ranged attack; no ranged → Dash; else Disengage). If every action alternative is out of range, it tries the ranged weapon attack itself, else Dash + walk on when the move ran out of movement. A move that ends out of reach says how far it got and why (out of movement / fire on the route / no route closer).
    plan: JSON, e.g. {"move":{"target":"e12","within":1.5},"action":[{"ability":"Target_MainHandAttack","target":"e12"},{"ability":"Projectile_MainHandAttack","target":"e12"},{"ability":"Shout_Dash","then_move":true},"Shout_Disengage"],"bonus":{"ability":"Shout_SecondWind","if":"hp_below:50"},"extra":[[{"ability":"Target_MainHandAttack","target":"e12"}]]}
    move: "e12" (walk next to it), {"target","within":<m>} (stop once that close, e.g. ranged), [x,y,z]. Walks the real route (stairs, detours). If the walk doesn't fit the movement left it walks as far as it does (partial:false turns that off); it keeps out of fire on the way (avoid:false walks through).
    Alternatives: an ability id, or {"ability","target"|"x,y,z","upcast","targets","if","unless","then_move"}, or {"tool":<batchable tool>,...}. then_move: after it (Dash), walk the move again.
    order: "move,action,bonus,extra" (default; e.g. "bonus,move,action" for Misty Step first). reactions: {"Divine Smite":"ask"} sets prompts first; on_reaction: "use"|"skip" answers reaction prompts mid-turn (default: stop).
    Before each step: still this character's turn? target still visible and alive? (else the next alternative). Stops on the usual interrupts (combat/dialogue/prompt/hp/teleport; ignore="" adds new hostiles in view).
    A slot whose every alternative missed (target dead, out of range, no line of sight) tries the plan's own targeted abilities on the nearest other visible hostile in reach (retarget:false turns it off). No bonus planned and an ally downed: Healing Word on them if this character has it (auto_bonus:false turns it off).
    end=true ends the turn and returns at once with who acts next, so you can plan the next turn while enemies act (wait_next=true blocks until a party member's turn).
    Not this character's turn yet (enemies still acting): send the plan anyway; it waits up to wait_turn s for the turn (stops on a prompt), then plays it."""
    t0 = time.monotonic()
    if _session_over():
        return _finish("combat_turn", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    p = turn.parse_plan(plan, _BATCHABLE)
    cs = game.rpc("state.combat")
    _remember_names(cs)
    party = _party_set()
    if not cs.get("active"):
        raise GameError("not in combat: use do_actions or queue_actions")
    now = turn.acting(cs)
    waited = ""
    want = _uuid(who) if who else None
    ready = (lambda c: want in turn.acting(c)) if want else (lambda c: bool(turn.party_acting(c, party)))
    if not ready(cs) and wait_turn > 0:
        w0 = time.monotonic()
        cs, why = _wait_turn(ready, wait_turn)
        _remember_names(cs)
        if not cs.get("active"):
            raise GameError("combat is over: loot, heal, then end_combat_report")
        now = turn.acting(cs)
        waited = f"waited {time.monotonic() - w0:.1f}s for the turn" + (f" ({why})" if why else "")
    mine = [u for u in now if u in party]
    if who:
        char = want
    else:
        sel = _active()
        char = sel if sel in mine else (mine[0] if mine else sel)
    if char not in now:
        ended = any(o.get("uuid") == char and o.get("ended") for o in cs.get("order") or [])
        why = f"{_label(char)} already ended its turn" if ended else f"it is not {_label(char)}'s turn"
        raise GameError(f"{why} (now {', '.join(_label(u) for u in now) or '?'})" + (f"; {waited}" if waited else ""))
    lines = [f"TURN {_label(char)}" + (f" (round {cs['round']})" if cs.get("round") is not None else "")
             + (f" — {waited}" if waited else "")]
    if p.reactions:
        try:
            game.rpc("ui.reactions", {"char": char, "set": p.reactions}, context="client")
            lines.append("reactions set: " + ", ".join(f"{k}={v}" for k, v in p.reactions.items()))
        except GameError as e:
            lines.append(f"reactions: FAILED — {e}")
    watcher = turn.TurnWatcher(_watch_conn(), p.on_reaction, _answer_reaction, ignore=_parse_ignore(ignore))
    watcher.baseline()
    after: dict = {}
    info: dict = {}
    stopped = False
    for slot in p.order:
        if slot == "move":
            res = _turn_move(p.move, char, watcher, lines, after, info) if p.move else "none"
        elif slot == "extra":
            res = "none"
            for i, alts in enumerate(p.slots["extra"], 1):
                res, _ = _turn_slot(f"extra {i}", alts, char, watcher, lines, after)
                if (res == "none" and alts and lines and lines[-1].startswith(f"extra {i}:")
                        and turn.missed_target(lines[-1])):
                    res = _turn_auto_fallback(p, f"extra {i}", alts, char, watcher, lines, after, info)
                if res == "stop":
                    break
        else:
            res, then_move = _turn_slot(slot, p.slots[slot], char, watcher, lines, after)
            if res == "ok" and then_move and p.move:
                res = _turn_move(p.move, char, watcher, lines, after, info)
            elif (res == "none" and slot in ("action", "bonus") and p.slots[slot] and lines
                  and lines[-1].startswith(f"{slot}:") and turn.missed_target(lines[-1])):
                res = _turn_auto_fallback(p, slot, p.slots[slot], char, watcher, lines, after, info)
        if res == "stop":
            stopped = True
            break
    if not stopped and p.auto_bonus and not p.slots["bonus"]:
        if _turn_auto_bonus(char, watcher, lines, after) == "stop":
            stopped = True
    lines.extend(watcher.answered)
    if end and not stopped:
        fatal, why = _turn_check(char, None)
        if fatal:
            lines.append(f"end_turn: {why}")
        else:
            ex = _new_executor(watcher, _watch_conn(), char)
            text, _status, res = ex.call_tool(actq.Step("end_turn", {"wait": wait_next}), char)
            lines.append(re.sub(r" — after=\{[^}]*\}| after=\{[^}]*\}", "", text))
            if isinstance(res, dict) and isinstance(res.get("after"), dict):
                after.update(res["after"])
    elif stopped:
        lines.append("turn NOT ended: decide, then continue (combat_turn again, or single tools)")
    now = _now_line(after)
    if now:
        lines.append(now.replace("NOW ", f"NOW {H.get(char)} ", 1))
    changes = delta.changes_text(game, H)
    if changes:
        lines.append(changes)
    if end and not stopped:
        nxt = _turn_next(char, party)
        if nxt:
            lines.append(nxt)
    if stopped:
        _batch["last"] = "interrupted"
    return _finish("combat_turn", {"who": who}, "\n".join(lines), t0)


# ---- macros ----

_looted: set[str] = set()  # containers/corpses loot_area already emptied this bridge session


def _macro_groups(who: str | None, groups: list, ignore: set | None = None,
                  done: list | None = None) -> tuple[list[str], str | None, list[str]]:
    """Run groups of steps [(label, [Step])] in order. A failed step skips the rest of its group (that
    target); an interrupt stops everything. Returns (lines, why it stopped or None, labels not reached);
    `done` collects the indices of groups whose steps all succeeded."""
    watcher = actq.Watcher(_watch_conn(), ignore=ignore or set())
    watcher.baseline()
    lines: list[str] = []
    for gi, (label, steps) in enumerate(groups):
        parts = []
        ok = True
        for step in steps:
            rep, text = _run_one(watcher, who, step)
            parts.append(text)
            if rep.stop_kind not in (None, "failed"):
                lines.append(f"{label}: " + "; ".join(parts))
                return lines, _label(rep.stopped or rep.stop_kind or ""), [g[0] for g in groups[gi + 1:]]
            if not rep.done:
                ok = False
                break
        if ok and done is not None:
            done.append(gi)
        lines.append(f"{label}: " + "; ".join(parts))
    return lines, None, []


def _macro_finish(tool: str, args: dict, lines: list[str], stop: str | None, left: list[str], t0: float) -> str:
    if stop:
        lines.append(f"STOPPED: {stop}" + (f"; not reached: {', '.join(left)}" if left else ""))
        _batch["last"] = "interrupted"
    changes = delta.changes_text(game, H)
    if changes:
        lines.append(changes)
    return _finish(tool, args, "\n".join(lines), t0)


def _no_combat(tool: str) -> dict:
    w = _watch_conn().try_rpc("act.watch") or {}
    if w.get("combat"):
        raise GameError(f"{tool} is for outside combat")
    return w


@mcp.tool()
@_guard
def loot_area(radius: float = macros.LOOT_RADIUS, who: str | None = None, corpses: bool = True,
              containers: bool = True, opened: bool = False) -> str:
    """Loot every VISIBLE corpse and unlocked container within radius (m), nearest-first walking order: walk, open, take all. Skips locked ones, owned ones (stealing), discovered armed traps, already opened ones (opened=true includes them) and what this session already looted. Stops at the first interrupt (combat, conversation, prompt, new hostile, HP drop)."""
    t0 = time.monotonic()
    w = _no_combat("loot_area")
    char = _who(who)
    me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
    near = game.rpc("state.nearby", {"radius": max(2.0, min(float(radius), 30.0))})
    _remember_names(near)
    targets, skipped = macros.loot_targets(near, me.get("pos"), float(radius), _looted, corpses, containers,
                                           opened=opened)
    if not targets:
        body = f"nothing to loot within {render.num(radius)}m" + (f" (skipped: {'; '.join(skipped)})" if skipped else "")
        return _finish("loot_area", {"radius": radius}, body, t0)
    groups = []
    for t in targets:
        h = H.get(t["uuid"], t.get("name"))
        first = actq.Step("loot_corpse", {"corpse": h}) if t["kind"] == "corpse" else actq.Step("interact", {"target": h})
        groups.append((f"{h} {t.get('name') or ''}".strip(), [first, actq.Step("loot", {"take": "all"})]))
    ok: list[int] = []
    lines, stop, left = _macro_groups(char, groups, done=ok)
    for i in ok:
        _looted.add(targets[i]["uuid"])
    if len(ok) < len(lines):
        # A failed take (an empty body) can leave the loot window open: close it so the world is usable.
        game.try_rpc("ui.loot", {"close": True}, context="client")
    head = f"loot_area: {len(targets)} target(s) within {render.num(radius)}m"
    if skipped:
        head += f"; skipped {'; '.join(skipped)}"
    return _macro_finish("loot_area", {"radius": radius}, [head] + lines, stop, left, t0)


# A voiced or cinematic line still playing: wait this long for the next prompt before handing back (every return
# here costs a model turn; S003's Helm cinematic took 6 empty advance/wait turns at the old 8 s).
DIALOGUE_LINE_WAIT_S = float(os.environ.get("BG3AI_DIALOGUE_LINE_WAIT_S", "15"))
# Autopilot (fast reflexes): the bridge clicks through filler itself after choose_dialogue and when a
# conversation opens, so the agent sees the next real choice at once. BG3AI_AUTO_DIALOGUE=0 turns it off.
AUTO_DIALOGUE = os.environ.get("BG3AI_AUTO_DIALOGUE", "1") != "0"


def _dialogue_autopilot(max_lines: int = macros.DIALOGUE_MAX, open_s: float = 2.5,
                        line_s: float | None = None, strict: bool = True) -> tuple[int, list[str], str | None]:
    """Click through FILLER only (macros.dialogue_filler: a Continue prompt or one untagged, non-committing
    option) until a real choice, a roll/reaction/popup, or the end. Returns (lines advanced, report lines,
    stop reason). open_s: how long a dialogue that is still opening may take to appear. strict (the bridge's
    own autopilot): only a Continue prompt or a bare "..."/Continue/End option, never a spoken answer, even
    the only one; dialogue_advance (the agent asked for it) also takes a single plain non-committing answer."""
    line_s = DIALOGUE_LINE_WAIT_S if line_s is None else line_s
    lines: list[str] = []
    stop = None
    n = 0
    wait_until = time.monotonic() + line_s
    open_until = time.monotonic() + open_s
    while n < max(1, min(int(max_lines), 30)):
        ui = game.try_rpc("ui.state", context="client") or {}
        scr = ui.get("screen")
        if scr in (None, "game") and n == 0 and time.monotonic() < open_until:
            time.sleep(0.25)
            continue
        if scr != "dialogue":
            stop = "conversation ended" if scr in (None, "game") else f"{scr} on screen"
            if scr not in (None, "game"):
                lines.extend(_render_ui(ui, brief=True))
            break
        ok, i, why = macros.dialogue_filler(ui.get("dialogue"), strict=strict)
        if not ok:
            if why in ("line still playing", "no options yet") and time.monotonic() < wait_until:
                time.sleep(0.3)
                continue
            stop = why
            lines.extend(_render_ui(ui, brief=True))
            break
        said = (ui.get("dialogue") or {}).get("text")
        r = game.run_job("ui.dialogue_choose", {"i": i}, context="client", wait_s=20)
        n += 1
        lines.append(f"{n}. " + (f"'{H.scrub(str(said))[:90]}' → " if said else "") +
                     ("continue" if i == 0 else f"option {i}") + ("" if r.status == "done" else f" {r.status.upper()}"
                                                                  + (f" — {r.error}" if r.error else "")))
        if r.status != "done":
            stop = "could not advance"
            break
        wait_until = time.monotonic() + line_s
        time.sleep(0.2)
    else:
        stop = f"advanced {n} lines (max_lines)"
    return n, lines, stop


@mcp.tool()
@_guard
def dialogue_advance(max_lines: int = macros.DIALOGUE_MAX) -> str:
    """Click through FILLER dialogue lines only (a Continue prompt, or a single untagged option that commits to nothing). Stops and shows the dialogue at the first real choice (2+ options, a skill/class tag, a committing option), a roll, a reaction, a popup, or when the conversation ends. Never picks an answer for you. Rarely needed: choose_dialogue and talk_to already do this."""
    t0 = time.monotonic()
    n, lines, stop = _dialogue_autopilot(max_lines, strict=False)
    head = f"dialogue_advance: {n} filler line(s)"
    body = [head] + lines + ([f"STOPPED: {stop}"] if stop else [])
    return _finish("dialogue_advance", {"max_lines": max_lines}, "\n".join(body), t0)


def _travel_dest(marker: str, objective: str, x, y, z) -> tuple[list, str]:
    if marker:
        st = game.try_rpc("state.status") or {}
        ms = _load_markers()
        m = next((m for m in ms if m.get("id") == marker), None) or next(
            (m for m in ms if marker.lower() in str(m.get("label", "")).lower()), None)
        if m is None or not m.get("pos"):
            raise ValueError(f"no marker '{marker}' (get_map lists your markers)")
        if m.get("level") and st.get("level") and m["level"] != st["level"]:
            raise ValueError(f"marker {m['id']} is in {m.get('region') or m['level']}: fast_travel there first")
        return list(m["pos"]), f"marker {m['id']} {m.get('label', '')}"
    if objective:
        m = render.find_objective(_objective_markers(MARKERS_TTL_S), objective)
        if not (m.get("here") and m.get("pos")):
            raise ValueError(f"objective '{render.objective_label(m)}' is not in this area")
        return list(m["pos"]), f"objective {render.objective_label(m)}"
    if None not in (x, y, z):
        return [x, y, z], render.pos([x, y, z])
    raise ValueError("give marker=m1, objective=o1, target=<id>, waypoint=<name> or x,y,z")


def _escape_surface(char: str, toward: list | None = None) -> str | None:
    """Standing in a harmful surface (fire, acid, ...)? Step to the nearest clear spot (nav.safe_spot), as a
    player would before anything else; with toward (travel_to's destination) a spot ahead wins over one as
    near behind. Returns a line saying what happened, or None if not in one."""
    r = game.try_rpc("nav.safe_spot", {"char": char, **({"pos": toward} if toward else {})})
    if not isinstance(r, dict) or r.get("safe") is not False:
        return None
    what = r.get("standing_in") or "a harmful surface"
    if not (r.get("found") and r.get("pos")):
        return f"standing in {what} and no clear ground within 8m: get out another way (jump_to, an exit)"
    px, py, pz = r["pos"]
    watcher = actq.Watcher(_watch_conn(), ignore={"surface", "damage", "hp", "hostile"})
    watcher.baseline()
    rep, text = _run_one(watcher, char, actq.Step("move_to", {"x": px, "y": py, "z": pz}))
    return f"left {what} {r.get('dir') or ''} {render.num(r.get('path_m', 0))}m: {text}"


@mcp.tool()
@_guard
def travel_to(marker: str = "", objective: str = "", waypoint: str = "", target: str = "",
              x: float | None = None, y: float | None = None, z: float | None = None, who: str | None = None,
              wait: bool = False, avoid_hazards: str = "auto", fast: str = "auto") -> str:
    """Get somewhere far in one call: your map marker (m1 or words of its label), a quest objective on this level (o1), a visible target, x,y,z, or an unlocked waypoint (fast travel). Walks the game's own route in one go when the whole way is plain walking (long trips in ≤55 m legs), else chains walks of up to 30 m along walkable ground toward it, around harmful surfaces you can see (fire, acid, ...); a leg that fails on the ground (no path, blocked) is retried another way (along the route, or shorter; twice per trip); stops on arrival (within 3 m), when no walkable ground leads further, after 2 legs without progress, or at the first interrupt (combat, conversation, prompt, new hostile, any damage). Stepping into a surface: it steps out (forward if it can) and goes on.
    avoid_hazards: auto (default) = when no clear way leads on, it takes the shortest crossing (says e.g. 'crossing 1.5m of Fire (~1-4 dmg + Burning)') unless HP is too low for the worst case; strict = never crosses; cross = crosses even at low HP (never lava/chasms, never >6 m).
    Returns within ~1 s with live progress (m left, ETA), or the outcome if the trip already ended: the trip runs as the first entry of the character's action queue, so queue_actions appends what to do on arrival while you observe and plan. Every later result shows it RUNNING, then queue_done or queue_interrupted (why). wait is ignored (the trip never blocks).
    fast: auto (default) = a trip of 120 m+ fast travels first to an unlocked waypoint of this level that is much nearer the goal (says so); never = always walk.
    A trip that stops twice at the same blocked spot says "tried 2x: blocked" and names another way (a waypoint, another side); route() shows the dead ends, unlocked waypoints and the closest spot you have been."""
    t0 = time.monotonic()
    if waypoint:
        return fast_travel(waypoint)
    policy = (avoid_hazards or "auto").strip().lower()
    if policy not in macros.AVOID_POLICIES:
        raise ValueError("avoid_hazards must be auto, strict or cross")
    w = _no_combat("travel_to")
    char = _who(who)
    if target:  # a far target is walked in legs to where it stands (one move_to to it is capped at MOVE_CAP_M)
        here = next((r.get("pos") for r in w.get("party") or [] if r.get("uuid") == char), None)
        tp = _ent_pos(_uuid(target))
        if here and tp and macros.dist(here, tp) > macros.TRAVEL_LEG_M:
            x, y, z, target = tp[0], tp[1], tp[2], ""
    if not _batch["active"]:  # wait=true too: the agent plans while the character walks
        return _travel_async(char, w, marker, objective, target, x, y, z, t0, policy, waited=wait,
                             fast=(fast or "auto").strip().lower() != "never")
    outer = _batch["exec"] if _batch["active"] else None  # the queue running this trip: its cancel stops it
    if target:
        lines, stop, left = _macro_groups(char, [(f"walk to {target}", [actq.Step("move_to", {"target": target})])])
        return _macro_finish("travel_to", {"target": target}, lines, stop, left, t0)
    dest, what = _travel_dest(marker, objective, x, y, z)
    me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
    here = me.get("pos")
    hp = [me.get("hp"), me.get("max_hp")]
    start_d = macros.dist(here, dest)
    lines = [f"travel_to {what}: {render.num(round(start_d, 1))}m"]
    crew = _trip_crew(char, lines)  # the leader's trip takes the party along (None: a companion's own trip)
    # travel_to deals with surfaces and damage itself (steps out, crosses on purpose, its own watcher stops on
    # other damage): the queue running it must not stop the trip on them.
    outer_w = outer.watcher if outer is not None else None
    added: set = set()

    def quiet(kinds: set) -> None:
        if outer_w is not None:
            new = kinds - outer_w.ignore
            outer_w.ignore |= new
            added.update(new)
    if policy != "strict":
        quiet({"surface", "damage"})
    in_travel, _batch["in_travel"] = _batch.get("in_travel"), True  # its legs are plain moves (never re-routed)
    try:
        esc = _escape_surface(char, dest) if me.get("surface") else None
        if esc:
            lines.append(esc)
            here = _pos_now(char) or here
        watcher = actq.Watcher(_watch_conn())
        watcher.baseline()
        prog = macros.TravelProgress(here, dest)
        stop = None
        retries, failed_end = 0, None  # a failed leg gets an alternative leg (S002: one failed leg ended the trip)
        for leg in range(1, macros.TRAVEL_MAX_LEGS + 1):
            d = macros.dist(here, dest)
            if outer is not None and (outer.interrupt or outer.stop.is_set()):
                stop = (outer.interrupt or ("cancel", "cancelled"))[1]
                break
            if d <= macros.TRAVEL_ARRIVE_M:
                lines.append(f"arrived ({render.num(round(d, 1))}m from it)")
                break
            # direct: the engine's own route in one long move_to when the whole way is plain walking (S005: 30 m
            # legs of sampled points ended on beach rocks and slopes, "path blocked stopped_short")
            args = {"char": char, "pos": dest, "max_m": min(macros.TRAVEL_LEG_M, d), "direct": True}
            if policy != "strict":
                args["avoid"] = "least"
            if failed_end is not None:
                args.update(alt=True, exclude=failed_end)
            r = game.try_rpc("nav.reach_toward", args)
            if isinstance(r, dict) and not r.get("reachable") and r.get("hazard"):
                stop = (f"the way on crosses {macros.hazard_text(r['hazard'])} ({render.num(round(d, 1))}m left)"
                        + (": no way around or across it found" if policy != "strict" else "")
                        + ": go around it (walk_toward another direction, get_frontier) or "
                        + macros.hazard_way_out(r["hazard"]))
                break
            if not (isinstance(r, dict) and r.get("reachable") and r.get("pos")) or macros.dist(r["pos"], dest) > d - 1.0:
                stop = (f"no walkable ground leads further toward it ({render.num(round(d, 1))}m left"
                        + (", no other leg either" if failed_end is not None else "")
                        + "): try get_frontier, a door, or a jump")
                if isinstance(r, dict) and r.get("hazard_beyond"):
                    stop += f"; ahead: {macros.hazard_text(r['hazard_beyond'])}"
                stop += _climb_hint(here, dest)
                break
            ex = r.get("exposure")
            leg_w, note = watcher, ""
            if isinstance(ex, dict) and ex.get("surface"):
                ok, why = macros.crossing_ok(ex, hp[0], hp[1], policy)
                if not ok:
                    stop = (f"the way on crosses {macros.exposure_text(ex)} @{render.pos(ex.get('pos'))} "
                            f"({render.num(round(d, 1))}m left): {why}")
                    break
                note = f"crossing {macros.exposure_text(ex)} @{render.pos(ex.get('pos'))}, the shortest way on: "
                leg_w = actq.Watcher(_watch_conn(), ignore={"surface", "damage"})
                leg_w.baseline()
                watcher.ignore.add("damage")  # Burning keeps ticking on the next legs
            elif r.get("detour"):
                hz = (r.get("straight") or {}).get("hazard")
                note = f"around {hz['surface']} (heading {r.get('dir') or ''}): " if isinstance(hz, dict) and hz.get("surface") else ""
            if r.get("direct"):
                note = f"along the route ({render.num(r.get('route_m') or r.get('path_m') or 0)}m): " + note
            if r.get("alt"):
                note = f"another way ({'along the route' if r['alt'] == 'route' else 'a shorter leg'}): " + note
            failed_end = None
            px, py, pz = r["pos"]
            rep, text = _run_one(leg_w, char, actq.Step("move_to", {"x": px, "y": py, "z": pz}), outer)
            if r.get("hazard_beyond") and not ex:
                text += f" (kept clear of {macros.hazard_text(r['hazard_beyond'])})"
            lines.append(f"leg {leg}: {note}{text}")
            res = rep.last_result if isinstance(rep.last_result, dict) else {}
            aft = res.get("after") or {}
            if aft.get("hp") is not None:
                hp = [aft.get("hp"), aft.get("max_hp", hp[1])]
            if rep.stop_kind == "surface":
                esc = _escape_surface(char, dest)
                if policy == "strict":
                    stop = _label(rep.stopped or "") + (f"; {esc}" if esc else "")
                    break
                if esc:
                    lines.append(esc)
                here = _pos_now(char) or here
                why = prog.leg(here, True)
                if why:
                    stop = why + ": try get_frontier, a door, or a jump"
                    break
                continue
            if not rep.done:
                if (retries < macros.TRAVEL_LEG_RETRIES and macros.leg_retryable(rep.stop_kind, rep.stopped, text)
                        and not (outer is not None and (outer.interrupt or outer.stop.is_set()))):
                    retries += 1
                    failed_end = [px, py, pz]
                    here = _pos_now(char) or here
                    continue
                stop = _label(rep.stopped or "") if rep.stop_kind not in (None, "failed") else f"leg {leg} failed"
                break
            here = (aft.get("pos") or res.get("pos") or r["pos"])
            why = prog.leg(here, macros.leg_stopped_short(res))
            if why:
                stop = why + ": try get_frontier, a door, or a jump"
                break
            if crew:
                note, behind = _wait_for_stragglers(char, here, crew, outer)
                if note:
                    lines.append(note)
                if behind:
                    stop = behind
                    break
        else:
            stop = f"still {render.num(round(macros.dist(here, dest), 1))}m away after {macros.TRAVEL_MAX_LEGS} legs"
    finally:
        _batch["in_travel"] = in_travel
        if outer_w is not None:
            outer_w.ignore -= added
    if longnav.ground_stop(stop, lines):
        stop += _dead_end_note(char, here, dest, what)
    return _macro_finish("travel_to", {"to": what}, lines, stop, [], t0)


def _dead_end_note(char: str, here: Any, dest: list, what: str) -> str:
    """travel_to stopped on blocked ground: remember the spot; from the second stop there, say so and name
    another way (longnav.stop_note)."""
    at = _pos_now(char) or here
    e = DEAD.record(dest, at, macros.dist(at, dest))
    if e is None or e.count < longnav.DEAD_REPEAT:
        return ""
    wps = (game.try_rpc("nav.waypoints", {"pos": dest}) or {}).get("waypoints")
    wp = longnav.pick_waypoint(at, dest, wps, min_m=0, save_m=longnav.ROUTE_WP_SAVE_M, ratio=1.0)
    return longnav.stop_note(e, wp, what.replace("objective ", ""))


def _trip_crew(char: str, lines: list) -> set | None:
    """Start of the leader's travel_to: chain drifted companions so they follow, and return the members to
    wait for when they fall behind: chained to the leader (or just re-chained), on this level, not staying,
    dead, downed or off stage. None when char is not the leader (a companion's own trip)."""
    st = game.try_rpc("state.status") or {}
    if st.get("active") != char:
        return None
    rows = party.apart(st, stay=_stay)
    took = _rechain(party.rechain_candidates(rows, _member_busy), char)
    if took:
        lines.append("chained to follow: " + ", ".join(_label(u) for u in took))
    other = {r["uuid"] for r in rows if r["other_level"]}
    return {r.get("uuid") for r in st.get("party") or []
            if r.get("uuid") != char and (r.get("chained") is True or r.get("uuid") in took)
            and r.get("uuid") not in _stay and r.get("uuid") not in other
            and not (r.get("dead") or r.get("off_stage") or r.get("downed"))}


def _wait_for_stragglers(char: str, here: Any, crew: set, outer: actq.Executor | None) -> tuple[str, str | None]:
    """After a leader's leg: a companion more than party.STRAGGLER_M behind (stuck on a ladder, a gap, a
    door) is waited for a moment, then walked back in legs. Returns (note for the leg lines, the stop
    reason if one still can't keep up)."""
    def lag() -> list[tuple[str, float]]:
        rows = (_watch_conn().try_rpc("act.watch") or {}).get("party") or []
        rows = [r for r in rows if r.get("uuid") in crew and not _member_busy(r.get("uuid"))]
        return party.stragglers(rows, here)

    def stopped() -> bool:
        return outer is not None and (outer.interrupt is not None or outer.stop.is_set())
    behind = lag()
    if not behind:
        return "", None
    first = dict(behind)
    end = time.monotonic() + party.CATCH_UP_S
    while behind and time.monotonic() < end and not stopped():
        time.sleep(0.25)
        behind = lag()
    names = ", ".join(f"{_label(u)} {render.num(round(d))}m" for u, d in first.items())
    if not behind:
        return f"waited for {names} behind: caught up", None
    if stopped():
        return "", None
    x, y, z = here
    for u, _d in behind:
        _queue_ignore[u] = set()
        _queues().add(u, [actq.Step("travel_to", {"x": x, "y": y, "z": z})], replace=True)
    end = time.monotonic() + 30.0
    while any(_queues().busy(u) for u, _ in behind) and time.monotonic() < end and not stopped():
        time.sleep(0.2)
    rows = {r.get("uuid"): r.get("pos") for r in (_watch_conn().try_rpc("act.watch") or {}).get("party") or []}
    still = [(u, macros.dist(rows.get(u), here)) for u, _d in behind if rows.get(u)]
    still = [(u, d) for u, d in still if d > party.STRAGGLER_M]
    if not still:
        return f"{names} fell behind: walked back to {_label(char)}", None
    left = ", ".join(f"{_label(u)} {render.num(round(d))}m behind" for u, d in still)
    return (f"{names} fell behind; the walk back failed",
            f"{left} can't keep up (stuck on a ladder, gap or door?): route(who=...) for their way, regroup(), "
            "or stay(who) to leave them on purpose")


def _climb_hint(here: list | None, dest: list | None) -> str:
    """travel_to stopped short of a destination on another deck: name the nearest ladders/stairs (the walker
    never climbs on its own; interact with the ladder does)."""
    if not (here and dest) or abs(dest[1] - here[1]) < 2.5:
        return ""
    res = game.try_rpc("nav.frontier", {})
    leads = res.get("leads") if isinstance(res, dict) else None
    ups = sorted((l for l in leads or [] if l.get("uuid") and l.get("kind") in ("ladder", "stairs")),
                 key=lambda l: l.get("dist") or 1e9)[:2]
    side = "above" if dest[1] > here[1] else "below"
    hint = f"; it is {render.num(round(abs(dest[1] - here[1]), 1))}m {side} you"
    if not ups:
        return hint + ": find a ladder or stairs (get_frontier) or jump_to"
    _remember_names(res)
    names = ", ".join(f"{H.get(l['uuid'], l.get('name'))} {l.get('name', '?')} {render.num(l.get('dist', '?'))}m"
                      for l in ups)
    return hint + f": climb with interact on a ladder ({names}), then travel_to again"


def _pos_now(char: str) -> list | None:
    """A party member's position from act.watch (read once per tool result tail, see _running_memo)."""
    rows = _running_memo.get("watch") if _running_memo["on"] else None
    if rows is None:
        rows = (_watch_conn().try_rpc("act.watch") or {}).get("party") or []
        if _running_memo["on"]:
            _running_memo["watch"] = rows
    return next((r.get("pos") for r in rows if r.get("uuid") == char), None)


@mcp.tool()
@_guard
def regroup(leader: str | None = None, near_m: float = macros.REGROUP_NEAR_M, chain: bool = True) -> str:
    """Bring party members who are farther than near_m from the leader (default: the active character) back to them, all walking at once, each in legs of verified walkable ground like travel_to (any distance; jump/climb links on the route are fine out of combat), then chain everyone to the leader (chain=false skips that). Downed members are left (help them up first); a member on another level is named (walk the leader there or fast travel). Members told to stay come back too. Each walk stops on the usual interrupts."""
    t0 = time.monotonic()
    w = _no_combat("regroup")
    lead = _who(leader)
    st = game.try_rpc("state.status") or {}
    info = {r.get("uuid"): r for r in st.get("party") or []}
    rows = {r.get("uuid"): r for r in w.get("party") or []}
    lp = (rows.get(lead) or {}).get("pos")
    if not lp:
        raise GameError(f"no position for {_label(lead)}")
    lreg = (info.get(lead) or {}).get("region")
    far, lines, skip = [], [], set()
    for u, r in rows.items():
        if u == lead:
            continue
        _stay.discard(u)
        reg = (info.get(u) or {}).get("region")
        if reg and lreg and reg != lreg:
            lines.append(f"{_label(u)}: on another level ({reg}): walk {_label(lead)} there or fast_travel")
            skip.add(u)
            continue
        d = macros.dist(r.get("pos"), lp)
        if r.get("downed"):
            lines.append(f"{_label(u)}: downed, {render.num(round(d, 1))}m away (help them up)")
            skip.add(u)
        elif d > near_m:
            far.append((u, d))
    unchained = [u for u in rows if u != lead and u not in skip and (info.get(u) or {}).get("chained") is False]
    if not far and not (chain and unchained):
        return _finish("regroup", {}, "\n".join(lines + [f"everyone is within {render.num(near_m)}m of {_label(lead)}"]), t0)
    x, y, z = lp
    for u, d in far:
        _queue_ignore[u] = set()
        _queues().add(u, [actq.Step("travel_to", {"x": x, "y": y, "z": z})], replace=True)
    end = time.monotonic() + party.REGROUP_WAIT_S
    while any(_queues().busy(u) for u, _ in far) and time.monotonic() < end:
        time.sleep(0.1)
    now = {r.get("uuid"): r.get("pos") for r in (_watch_conn().try_rpc("act.watch") or {}).get("party") or []}
    lp2 = now.get(lead) or lp
    stop = None
    for u, d in far:
        q = _queues().queues.get(u)
        rep = q.last_report if q else None
        d2 = macros.dist(now.get(u), lp2) if now.get(u) else None
        at = f" → {render.num(round(d2, 1))}m" if d2 is not None else ""
        last = (rep.lines[-1] if rep and rep.lines else "").splitlines()
        why = next((ln for ln in reversed(last) if ln.startswith("STOPPED")), last[-1] if last else "")
        if rep is None or _queues().busy(u):
            lines.append(f"{_label(u)} ({render.num(round(d, 1))}m{at}): still walking")
        elif d2 is not None and d2 <= max(near_m, macros.TRAVEL_ARRIVE_M + 1.0):
            lines.append(f"{_label(u)} ({render.num(round(d, 1))}m{at}): back")
        else:
            lines.append(f"{_label(u)} ({render.num(round(d, 1))}m{at}): {why or 'did not get back'}".rstrip())
            stop = stop or (f"{_label(u)} could not get back to {_label(lead)}"
                            + (f": {_label(rep.stopped)}" if rep and rep.stopped else ""))
    if chain:
        for u in rows:
            if u != lead and u not in skip and ((info.get(u) or {}).get("chained") is not True or any(u == f for f, _ in far)):
                r = game.run_job("act.chain", {"char": u, "leader": lead}, wait_s=10)
                ok = r.status == "done" or "already chained" in str(r.error or "")
                lines.append(f"chain {_label(u)} → {_label(lead)}: " + ("done" if ok else f"{r.status} — {r.error}"))
    return _macro_finish("regroup", {}, [f"regroup at {_label(lead)}: {len(far)} walking"] + lines, stop, [], t0)


_READ_FILE = "read_items.json"


def _read_set() -> set:
    try:
        return set(json.loads((MEMORY / _READ_FILE).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


@mcp.tool()
@_guard
def read_all(who: str | None = None, max_items: int = 6) -> str:
    """Read every book, note and letter in the party's bags you haven't read yet (by name), one after another, and return their text. Remembers what was read across sessions. Out of combat only. Stops at a conversation, roll, reaction or decision popup."""
    t0 = time.monotonic()
    _no_combat("read_all")
    inv = game.rpc("state.inventory", {"uuid": _uuid(who)} if who else {})
    _remember_names(inv)
    seen = _read_set()
    items = [it for it in (inv.get("items") or []) if macros.readable(it) and it.get("uuid") not in seen]
    if not items:
        return _finish("read_all", {}, "nothing new to read", t0)
    watcher = actq.Watcher(_watch_conn(), ignore={"prompt"})
    watcher.baseline()
    lines, stop, budget = [], None, 6000
    for it in items[:max(1, min(int(max_items), 12))]:
        h = H.get(it["uuid"], it.get("name"))
        rep, text = _run_one(watcher, it.get("owner"), actq.Step("manage_inventory", {"op": "use", "item": h}))
        if not rep.done:
            lines.append(f"{h} {it.get('name')}: {text}")
            if rep.stop_kind not in (None, "failed"):
                stop = _label(rep.stopped or "")
                break
            continue
        page = None
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            ui = game.try_rpc("ui.state", context="client") or {}
            scr = ui.get("screen")
            if scr in ("dialogue", "roll", "reaction", "game_over") or (scr == "popup" and (ui.get("popup") or {}).get("decision")):
                lines.extend(_render_ui(ui, brief=True))
                stop = f"{scr} on screen"
                break
            if scr in ("book", "popup", "reward") and ui.get("popup"):
                page = ui["popup"]
                break
            time.sleep(0.2)
        if stop:
            break
        seen.add(it["uuid"])
        txt = ""
        if page:
            txt = " — ".join(str(page[k]) for k in ("title", "text") if page.get(k))
            game.try_rpc("ui.dismiss", {}, context="client")
            end = time.monotonic() + 1.5  # let the panel close, so the next book's text isn't this one's
            while time.monotonic() < end and (game.try_rpc("ui.state", context="client") or {}).get("screen") == "book":
                time.sleep(0.1)
        txt = H.scrub(txt)[:max(200, budget)] if txt else "(read; no text panel seen)"
        budget -= len(txt)
        lines.append(f"{h} {it.get('name')}: {txt}")
    try:
        MEMORY.mkdir(parents=True, exist_ok=True)
        (MEMORY / _READ_FILE).write_text(json.dumps(sorted(seen)))
    except OSError:
        pass
    left = [f"{H.get(it['uuid'])} {it.get('name')}" for it in items if it["uuid"] not in seen]
    head = f"read_all: {len(items)} unread"
    return _macro_finish("read_all", {}, [head] + lines, stop, left if stop else [], t0)


@mcp.tool()
@_guard
def rest_if_needed(threshold: float = 50.0, allow_long: bool = False) -> str:
    """Rest only when it's worth it: if any party member is downed or at/below threshold % HP, take a short rest; if no short rest is left and allow_long=true, go to camp and long rest (camp scenes then come up as dialogues). Out of combat only. HP only: check spell slots yourself."""
    t0 = time.monotonic()
    w = _no_combat("rest_if_needed")
    low = macros.hp_low(w.get("party") or [], threshold)
    if not low:
        pcts = [round(100 * r["hp"] / r["max_hp"]) for r in w.get("party") or []
                if isinstance(r.get("hp"), (int, float)) and r.get("max_hp")]
        return _finish("rest_if_needed", {}, f"no rest needed (lowest HP {min(pcts) if pcts else '?'}%)", t0)
    who_low = ", ".join(f"{_label(u)} {hp}/{mx}" for u, hp, mx in low)
    lines, stop, left = _macro_groups(None, [("rest", [actq.Step("short_rest", {})])])
    rested = bool(lines) and "short_rest: done" in lines[-1]
    if not rested and not stop and allow_long:
        more, stop, left = _macro_groups(None, [("long rest", [actq.Step("camp", {"action": "go"}),
                                                               actq.Step("long_rest", {})])])
        lines += more
    elif not rested and not stop:
        lines.append("no short rest worked: long rest at camp (camp, long_rest) or heal with potions/spells")
    return _macro_finish("rest_if_needed", {}, [f"low: {who_low}"] + lines, stop, left, t0)


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


def _save_menus(label: str) -> tuple[bool, str]:
    """Drive the game's save menus for one save (blocking, ~1 s; runs in the saver thread). (ok, detail)"""
    if not label:
        r = game.run_job("ui.menu", {"action": "quicksave"}, context="client", wait_s=30)
        return r.status == "done", _job_text("quicksave", r)
    r = game.run_job("ui.menu", {"action": "save", "name": label}, context="client", wait_s=30)
    res = r.result if isinstance(r.result, dict) else {}
    if (r.status == "done" and res.get("needs_text")) or (r.status == "failed" and res.get("needs_key")):
        # An in-game key/text didn't land: send it through the input shim, then let the mod continue.
        inp = _inp()
        if inp is None:
            return False, "the save menus need the input shim (" + (r.error or "needs_text") + "); try save_game() (quicksave)"
        if res.get("needs_text"):
            inp.text(label)
        inp.key(res.get("needs_key") or "RETURN")
        r = game.run_job("ui.menu", {"action": "save_confirm", "name": label}, context="client", wait_s=40)
        res = r.result if isinstance(r.result, dict) else {}
    ms = res.get("ms")
    detail = "menus " + " ".join(f"{k}={v}" for k, v in ms.items()) if isinstance(ms, dict) else _job_text("save", r)
    return r.status == "done", detail


def _can_save() -> tuple[bool, str]:
    """Whether the game takes a save now: nothing modal on screen and the Save command enabled."""
    ui = game.try_rpc("ui.state", context="client") or {}
    scr = (ui.get("screen") or "game") if isinstance(ui, dict) else "game"
    if scr not in ("game", "game_menu", "save_load"):
        return False, f"{scr} on screen"
    dry = game.try_rpc("ui.menu", {"action": "save", "dry": True}, context="client")
    if not (isinstance(dry, dict) and dry.get("can")):
        return False, "the game has saving disabled here"
    return True, ""


def _save_log(rec: dict) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME / "actions.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "tool": "save_game.bg", **rec}) + "\n")
    except OSError:
        pass


SAVER = saver.Saver(_save_menus, _can_save, lambda: SAVE_DIR, _save_log)


@mcp.tool()
@_guard
def save_game(label: str = "") -> str:
    """Save the game (returns at once: the save runs in the background; SAVED shows on a later result). label: short name for a named save (e.g. "Sxxx-pre-kagha"); empty = quicksave. If the game can't save right now (conversation, roll) it is queued for the next moment it can."""
    t0 = time.monotonic()
    _enable_save_gates()
    label = re.sub(r"[^A-Za-z0-9 _-]", "", label)[:40] if label else ""  # the mod's name filter (no dots)
    body = SAVER.save(label, sync=_session_over())  # session over: nothing comes after, so wait for the disk
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
    dropped = SAVER.settle()  # a background save finishes writing first; a queued one is dropped
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
    # The mod selects the save on the Load page (by playthrough, position and date) and presses Load; the
    # load then resets the mod's Lua, so a poll may fail mid-way: wait for the game to be Running again.
    try:
        r = game.run_job("ui.menu", saves.load_args(SAVE_DIR, save), context="client", wait_s=40)
        if r.status == "failed" and isinstance(r.result, dict) and r.result.get("needs_key") and _inp() is not None:
            _inp().key(r.result["needs_key"])
            r = JobResult("done", {"loading": True, "via": "shim"})
    except (GameError, NotReady) as e:
        r = JobResult("done", {"loading": True, "note": str(e)[:80]})
    if r.status == "done":
        deadline, ok = time.monotonic() + 120, False
        time.sleep(1.0)
        while time.monotonic() < deadline and not ok:
            try:
                ok = game.game_state() == "Running" and game.server_ready()
            except (GameError, NotReady, OSError):
                ok = False
            if not ok:
                time.sleep(0.5)
        r = JobResult("done", {"loaded": save.split("__", 1)[-1]}) if ok else \
            JobResult("failed", None, "the game did not come back to Running within 120 s")
    game.event_seq = {"server": 0, "client": 0}
    DELTA.reset()  # a loaded save is a new world: the next look is a full one
    return _finish("load_game", {"save": save}, _job_text("load_game", r) + (f"\n{dropped}" if dropped else ""),
                   t0, events=False)


@mcp.tool()
@_guard
def wait(seconds: float = 3.0) -> str:
    """Let game time pass (cutscenes, NPC turns) and report what happened. Returns early on the first notable event (combat/turn/dialogue start or end, a job or queue finishing). To follow an action, prefer wait_for."""
    t0 = time.monotonic()
    end = t0 + max(0.2, min(seconds, 30.0))
    hit = None
    while time.monotonic() < end and hit is None:
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
        hit = _peek_events(_WAIT_KINDS)
    waited = render.num(round(time.monotonic() - t0, 1))
    body = f"{hit.get('kind')} after {waited}s" if hit else f"waited {waited}s"
    return _finish("wait", {"seconds": seconds}, body, t0)


_WAIT_KINDS = {"combat_started", "combat_ended", "turn_started", "dialogue_started", "dialogue_ended",
               "job_finished", "queue_done", "queue_interrupted"}


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
    """Time left in this play session. Rarely needed: every tool result already ends with the ⏱ line."""
    return _clock() or "No session deadline set."  # includes the stuck signal, if any


def _session_log() -> Path | None:
    sess = os.environ.get("BG3AI_SESSION")
    d = MEMORY / "sessions"
    if sess:
        return d / f"{sess}.md"
    logs = sorted(d.glob("S*.md")) if d.is_dir() else []
    return logs[-1] if logs else None


def _movie_skipper(stop: "threading.Event") -> None:
    """Cutscenes are always skipped (user requirement). The mod's AutoSkip handles dialogue lines in-game
    and asks for a shim ESCAPE when a Bink movie ignores the engine's; skipper.py polls that request
    (autoskip.status, ~150 ms) on its own connection, with the old ui.state poll as a fallback. It idles
    while another process (runner/play.py) holds runtime/skipper.lock: one skipper per game."""
    from .skipper import MovieSkipper

    MovieSkipper(Game(port=game.port), _inp, lambda msg: _log("autoskip", {}, msg, 0),
                 lock_path=RUNTIME / "skipper.lock").run(stop)


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
    "wiki_page", "wiki_search", "load_game", "list_saves",
    # S002-S007: 26 ToolSearch calls (10 in S005) loaded these mid-play; each tool here was used in 2+
    # sessions or 3+ times (rests/camp/level-up: rare but on every long day). Still deferred: ui_dump,
    # ui_action, path_preview, fast_travel and the setup/menu tools (1 use or none).
    "screenshot", "click", "click_on", "press_key", "dismiss_popups", "lockpick", "regroup", "get_map",
    "get_party", "get_actions", "get_inventory", "level_up", "get_level_up_options", "short_rest", "long_rest",
    "camp", "confirm",
)
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def _mark_always_load(names=ALWAYS_LOAD) -> list[str]:
    """Set the alwaysLoad hint on the core tools; returns the names it could not find."""
    missing = []
    for name in names:
        tool = mcp._tool_manager.get_tool(name)
        if tool is None:
            missing.append(name)
            continue
        tool.meta = {**(tool.meta or {}), **ALWAYS_LOAD_META}
    return missing


if os.environ.get("BG3AI_ALWAYS_LOAD", "1") != "0":  # 0: everything deferred (A/B checks)
    _mark_always_load()


def main() -> None:
    import threading

    stop = threading.Event()
    if os.environ.get("BG3AI_MOVIE_SKIP", "1") != "0":
        threading.Thread(target=_movie_skipper, args=(stop,), name="movie-skipper", daemon=True).start()
    sample_s = float(os.environ.get("BG3AI_METRICS_SAMPLE_S", "1.0") or 0)
    if sample_s > 0:  # busy/idle samples for the latency metrics, on their own connection
        threading.Thread(target=metrics.sampler_loop, args=(METRICS, Game(port=game.port), stop, sample_s),
                         name="activity-sampler", daemon=True).start()
    try:
        mcp.run("stdio")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
