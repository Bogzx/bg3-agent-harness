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
from typing import Any, Callable

from mcp.server.mcpserver import Image, MCPServer

from . import actq, delta, economy, leveling, macros, metrics, party, render, saver, saves, turn
from . import companions, debrief, lessons, longnav, lore, navhints
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

def _log(tool: str, args: dict, result: str, ms: float, extra: dict | None = None) -> None:
    # Every tool result passes here (via _finish): one latency record per call (metrics.py).
    METRICS.tool_done(tool, ms, nested=_batch["active"], ok=not result.startswith(("FAILED", "NOT READY", "REFUSED")),
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
    for evs in _par(lambda: drain("server"), lambda: drain("client")):
        evs = _drop_events(evs)
        if evs:
            seen += evs
            _note_dialogue_speaker(evs)
            parts.append(render.render_events(evs, H, body=body, job=job, flap=render.FLAP, party=_party_cached()))
    fight = _fight_feed(seen)
    if fight:
        parts.append(fight)
    return "\n".join(p for p in parts if p)


# ---- fight debriefs (debrief.py, research/fight-debrief.md): learn from a fight that went badly ----
_FIGHTS: dict[str, Any] = {"tr": None}
_retry: dict[str, Any] = {"rec": None, "status": False, "fight": False}


def _fights_path() -> Path:
    """runtime/sessions/<playthrough>/<Sxxx>.fights.jsonl beside the session's metrics (docs/ARCHITECTURE.md
    "Learning from fights"); memory/fights.jsonl when the runner set no metrics path (tests, hand runs)."""
    m = os.environ.get("BG3AI_METRICS_PATH", "")
    if m.endswith(".metrics.jsonl"):
        return Path(m[: -len(".metrics.jsonl")] + ".fights.jsonl")
    return MEMORY / "fights.jsonl"


def _fights() -> debrief.Tracker:
    p = _fights_path()
    if _FIGHTS["tr"] is None or _FIGHTS.get("path") != p:   # a new memory dir (tests): a fresh history
        hist: list = []
        for f in sorted(p.parent.glob("*fights.jsonl")) if p.parent.is_dir() else []:
            hist += debrief.load_history(f)   # every session's fights of this playthrough, oldest first
        _FIGHTS.update(tr=debrief.Tracker(hist), path=p)
        _retry.update(rec=None, status=False, fight=False)
    return _FIGHTS["tr"]


def _fight_feed(evs: list) -> str:
    """Every drained event batch: WARNING lines for repeated patterns, and the FIGHT DEBRIEF when the fight ended."""
    if not evs:
        return ""
    try:
        warns, ended = _fights().feed(evs, _party_cached())
        out = list(warns)
        if ended:
            out.append(_close_fight())
        return "\n".join(x for x in out if x)
    except Exception as e:  # noqa: BLE001 - a debrief must never break a tool result
        _log("debrief", {}, f"feed failed: {e}", 0)
        return ""


_UNUSED_RX = re.compile(r"(?i)\b(potion|elixir|scroll|grenade|flask|bomb)\b")


def _close_fight(reloaded: bool = False) -> str:
    """Close the open fight: the debrief block, a [fight] line in the session log, a fights.jsonl record."""
    st = game.try_rpc("state.status") or {}
    rows = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    m = os.environ.get("BG3AI_METRICS_PATH", "")
    sess = os.environ.get("BG3AI_SESSION") or (Path(m).name.split(".")[0] if m.endswith(".metrics.jsonl") else None)
    meta = {"session": sess, "region": st.get("region")}   # the runner sets only the metrics path (S035: null)
    rec = _fights().close(rows, [], meta, reloaded=reloaded)
    if rec is None:
        return ""
    if rec["grade"] != "clean":
        # What the party carried and didn't use (its own inventory: fair). One read, bad fights only.
        inv = game.try_rpc("state.inventory", {}) or {}
        used = set((rec.get("consumables") or {}).get("used") or [])
        names = []
        for it in inv.get("items") or [] if isinstance(inv, dict) else []:
            n = str(it.get("name") or "") if isinstance(it, dict) else ""
            if n and _UNUSED_RX.search(n) and n not in used and n not in names and not it.get("equipped_slot"):
                names.append(n)
        rec["consumables"]["unused"] = rec["unused"] = names[:6]
        rec["key_facts"] = debrief.key_facts(rec)
        rec["summary"] = debrief.summary(rec)
    debrief.append(_fights_path(), rec)
    try:
        _append_note(debrief.fight_note(rec), "fight")
    except Exception:  # noqa: BLE001
        pass
    return debrief.render(rec)


def _fight_hint(cs: dict, party: set | None) -> str:
    """At the first combat-state read of a fight: the retried fight's key facts (after a load_game), else what
    past debriefs vs these enemies said. Once per fight."""
    try:
        tr = _fights()
        if not isinstance(cs, dict) or not cs.get("active"):
            return ""
        if tr.cur is None:
            tr.feed([{"kind": "round_started", "data": {"round": cs.get("round") or 1}}], party)
        tr.observe(cs, party)
        f = tr.cur
        if f is None or getattr(f, "hinted", False):
            return ""
        f.hinted = True
        rec = _retry["rec"] if _retry["fight"] else None
        if rec:
            names = {o.get("name") for o in cs.get("order") or [] if isinstance(o, dict) and o.get("relation") == "hostile"}
            if not names or names & {e.get("name") for e in rec.get("enemies") or []}:
                _retry["fight"] = False
                facts = (rec.get("key_facts") or [])[:3]
                return (f"RETRY of the fight that went {rec.get('grade_before') or rec.get('grade')}: "
                        + ("; ".join(facts) if facts else rec.get("summary", "")))
        return "\n".join(tr.first_turn_warnings(cs))
    except Exception as e:  # noqa: BLE001
        _log("debrief", {}, f"hint failed: {e}", 0)
        return ""


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


# Answers read by scrolling, per dialogue node: (speaker, line, total) -> {i: option}.
_answer_pages: dict = {}


def _complete_answers(ui: dict) -> dict:
    """A long answer list shows only a window of its rows (live: 6 of Karlach's 10). Wheel it like a player and
    list every answer (texts of rows outside the window show only when scrolled). Cached per node."""
    d = ui.get("dialogue") if isinstance(ui, dict) else None
    if not d or not d.get("options") or not (d.get("more_below") or d.get("more_above")):
        return ui
    total = len(d["options"]) + int(d.get("more_below") or 0) + int(d.get("more_above") or 0)
    key = (d.get("speaker"), d.get("text"), total)
    got = _answer_pages.get(key)
    why = None
    if got is None or len(got) < total:
        inp = _inp()
        if inp is None:
            return ui

        def read() -> list:
            u = game.try_rpc("ui.state", context="client") or {}
            dd = u.get("dialogue") if isinstance(u, dict) else None
            return list((dd or {}).get("options") or [])

        from . import clicker
        got, why = clicker.read_answer_list(game, inp, read)
        if len(_answer_pages) > 50:
            _answer_pages.clear()
        _answer_pages[key] = got
    for o in d["options"]:
        got.setdefault(o["i"], o)
    d = dict(d, options=[got[i] for i in sorted(got)])
    left = total - len(got)
    d.pop("more_above", None)
    d["more_below"] = left if left > 0 else None
    if why:
        d["scroll_note"] = why
    return dict(ui, dialogue=d)


def _render_ui(ui: dict, brief: bool = False) -> list[str]:
    _keep_read(ui)
    try:
        ui = _complete_answers(ui)
    except Exception:  # best effort: the visible window still renders
        pass
    try:
        ui = _annotate_checks(ui)
    except Exception:  # best effort: a missing sheet never hides the dialogue itself
        pass
    return render.render_ui(ui, H, brief=brief)


def _keep_read(ui: Any) -> None:
    """A book/note/letter panel on screen: keep its text for recall() (what the party has read)."""
    pop = ui.get("popup") if isinstance(ui, dict) and ui.get("screen") == "book" else None
    if isinstance(pop, dict) and pop.get("text"):
        try:
            text = H.scrub(str(pop["text"]))
            title = str(pop.get("title") or "") or (text[:60].rsplit(" ", 1)[0] + "…" if len(text) > 60 else text)
            lore.remember(MEMORY, H.scrub(title), text)
        except Exception:  # best effort: never breaks a read
            pass


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


# Consecutive wait_for(<event kinds>) answers "nothing is running", with only these reads in between.
_noop_waits = {"n": 0}
_READ_TOOLS = {"look", "look_at", "get_status", "get_nearby", "get_frontier", "get_layout", "get_map", "session_clock", "wiki_page",
               "wiki_search", "get_party", "list_saves", "get_inventory", "screenshot", "zoom", "route", "get_journal",
               "get_combat_state", "get_dialogue", "ability_preview", "get_level_up_options", "recall"}


def _finish(tool: str, args: dict, body: str, t0: float, events: bool = True, issued: bool = False,
            extra: dict | None = None) -> str:
    if _batch["active"]:
        # Inside do_actions: no per-step event drain or clock; the batch reports once at the end.
        text = H.scrub(body.rstrip())
        _log(tool, args, text, (time.monotonic() - t0) * 1000, extra)
        if text.startswith(("FAILED", "NOT READY", "REFUSED")):
            _batch["last"] = "failed"
        return text
    if tool == "wait_for":
        _noop_waits["n"] = _noop_waits["n"] + 1 if body.startswith("nothing is running that would cause") else 0
    elif tool not in _READ_TOOLS:
        _noop_waits["n"] = 0
    out = [body.rstrip()]
    if events:
        job = str(args.get("target") or "").strip().lower() if tool == "wait_for" else ""
        ev = _events_text(body, job if re.fullmatch(r"j\d+", job) else None,
                          max_wait=DIALOGUE_DRAIN_MAX_S if tool in _DIALOGUE_TOOLS else None)
        if ev:
            out.append(ev)
        auto = _auto_dialogue(tool, body, ev)
        if auto:
            out.append(auto)
            ev = _events_text()
            if ev:
                out.append(ev)
        fight = _auto_combat(tool, body, ev)
        if fight:
            out.append(fight)
        les = _lessons_text(tool, body, ev)
        if les:
            out.append(les)
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
        if _LEVEL_NUDGE.stale():  # XP arrives mid-play: one state.status read a minute finds the level up
            _LEVEL_NUDGE.seen_at = time.time()  # also when the read fails (loading): no retry on every call
            _LEVEL_NUDGE.observe(game.try_rpc("state.status"))
        lv = _LEVEL_NUDGE.text()
        if lv:
            out.append(lv)
    out.append(SAVER.take_notes())  # SAVED / SAVE FAILED from a background save
    c = _clock_line()
    if c:
        out.append(c)
    text = H.scrub("\n".join(x for x in out if x))
    _log(tool, args, text, (time.monotonic() - t0) * 1000, extra)
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
        if tool == "talk_to" and head.startswith("talk_to: done") and "dialogue_line" not in ev:
            # S028 15:50 (Wulbren, Lia at the dock): "talk_to: done — dialogue" and nothing else; the agent read
            # get_dialogue (screen=game) to find out. Say it: the game started a talk that never showed a window.
            return ("(no conversation window opened: they only had a passing remark, nothing to answer; "
                    "talk to them again later or follow the journal)")
        return ""  # no dialogue on screen (ambient banter, a plain door): nothing to add
    return "\n".join(([f"auto-advanced {n} opening filler line(s):"] if n else []) + lines
                     + ([f"STOPPED: {stop}"] if stop else []))


# Tools whose own result already shows the fight (no extra combat read after them).
_NO_AUTO_COMBAT = {"get_combat_state", "combat_turn", "end_turn", "screenshot", "zoom", "session_clock",
                   "end_combat_report", "note"}


def _auto_combat(tool: str, body: str, ev: str) -> str:
    """When this call's events say a fight started, show what get_combat_state would (turn order, shared turn,
    the acting member's actions) right here: S023-S026 the agent read get_combat_state after the action or
    wait_for that started a fight (20 reads). One state.combat + state.actions read (~50 ms), once per fight."""
    if tool in _NO_AUTO_COMBAT or "combat_started" not in (ev or "") or "YOUR ACTIONS" in body:
        return ""
    try:
        return "COMBAT STARTED (no get_combat_state needed):\n" + _combat_state_text()
    except GameError:
        return ""


LESSONS = lessons.Store(MEMORY / "lessons.md")
_WHERE_RE = re.compile(r"^WHERE ([^|\n]+)", re.M)
# A move/action result's CHANGES block: "  WHERE <old> -> <new>" (S035 entered the Gauntlet of Shar and its trials
# through move_to/walk results only; its Umbral Tremor lesson never showed).
_WHERE_MOVED_RE = re.compile(r"^\s+WHERE .*? -> ([^\n]+)$", re.M)


def _lessons_text(tool: str, body: str, ev: str) -> str:
    """memory/lessons.md lines matching a fight that just started (enemy names/races, the place) or a place just
    entered (docs/ARCHITECTURE.md "Learning from fights"). Once per fight / place; one combat + one status read
    per fight start (+ one nearby read for the enemies' statuses), none otherwise."""
    ev = ev or ""
    if "combat_ended" in ev or tool == "load_game":
        LESSONS.fight_over()
    m = _WHERE_RE.search(body) if tool in ("get_status", "look") else None
    m = m or _WHERE_MOVED_RE.search(body)
    if m and not LESSONS.in_fight and "combat_started" not in ev:
        return LESSONS.region_lines([x.strip() for x in m.group(1).split("·")])
    started = "combat_started" in ev or (tool == "get_combat_state" and body.startswith("COMBAT"))
    if LESSONS.in_fight or not started or not LESSONS.lessons():
        return ""
    cs = game.try_rpc("state.combat") or {}
    st = game.try_rpc("state.status") or {}
    enemies = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("relation") == "hostile"
               and not o.get("item")]
    return LESSONS.fight_lines(_with_statuses(enemies), [st.get("subregion") or "", st.get("region") or ""])


def _with_statuses(rows: list[dict]) -> list[dict]:
    """Character rows + the status names the examine panel shows ([Mirrored]): the combat rows don't carry them,
    the nearby read does (one read)."""
    nb = game.try_rpc("state.nearby", {"kinds": ["characters"]}) or {}
    st_of = {c.get("uuid"): [x.get("name") for x in c.get("statuses") or [] if isinstance(x, dict) and x.get("name")]
             for c in nb.get("characters") or [] if isinstance(c, dict) and c.get("uuid")}
    return [{**o, "statuses": st_of.get(o.get("uuid")) or []} for o in rows]


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


def _objective_ref(ref: str) -> str:
    """o1/o2 (an objective marker) as a target: the name it is labelled with (S041 22:24 talk_to(npc="o1") for the
    Balthazar marker failed "unknown target 'o1'"). Not an objective handle: ref unchanged."""
    m = re.fullmatch(r"o(\d+)", str(ref or "").strip().lower())
    if not m:
        return ref
    ms = _objective_markers(max_age=600)
    i = int(m.group(1))
    if not 1 <= i <= len(ms) or not isinstance(ms[i - 1], dict):
        raise ValueError(f"unknown target '{ref}': no objective {ref} now (get_map lists them)")
    label = render.objective_label(ms[i - 1])
    try:
        return _uuid(label)
    except ValueError:
        raise ValueError(f"{ref} is the objective marker '{label}', not something in view by that name: "
                         f"route(objective='{ref}', go=true) to get there, then use the eN from get_nearby") from None


TELEPORT_SCROLLS = {"misty step": 18.0, "dimension door": 18.0}  # BG3 ranges (m, 3D)


def _teleport_hint(res: Any) -> str:
    """route: LOWER GROUND beyond a jump and a carried teleport scroll that covers it (S039: Misty Step and Dimension
    Door scrolls sat in Claude's bags while the agent looked for a jump off the arrival island). One inventory read,
    only in that case."""
    if not isinstance(res, dict):
        return ""
    far = [lo for lo in res.get("lower") or [] if isinstance(lo, dict) and lo.get("from") and lo.get("to")
           and isinstance(lo.get("gap_m"), (int, float)) and isinstance(lo.get("reach_m"), (int, float))
           and lo["gap_m"] > lo["reach_m"]]
    if not far:
        return ""
    inv = game.try_rpc("state.inventory", {}) or {}
    scrolls = [it for it in (inv.get("items") or []) if isinstance(it, dict)
               and any(k in str(it.get("name") or "").lower() for k in TELEPORT_SCROLLS)]
    if not scrolls:
        return ""
    _remember_names(inv)

    def rng(it: dict) -> float:
        return next(r for k, r in TELEPORT_SCROLLS.items() if k in str(it.get("name")).lower())

    best = max(scrolls, key=rng)
    lines = []
    for lo in far[:2]:
        d3 = sum((a - b) ** 2 for a, b in zip(lo["from"], lo["to"])) ** 0.5
        if d3 <= rng(best):
            lines.append(f"TELEPORT: {H.get(best['uuid'], best.get('name'))} {best.get('name')} ({render.num(rng(best))}m)"
                         f" reaches the floor {lo.get('dir') or ''} from the edge {render.pos(lo['from'])}"
                         f" ({render.num(round(d3, 1))}m): move_to the edge, then use the scroll at {render.pos(lo['to'])}")
        else:
            lines.append(f"TELEPORT: the carried {best.get('name')} reaches {render.num(rng(best))}m; the floor "
                         f"{lo.get('dir') or ''} is {render.num(round(d3, 1))}m from the edge: too far")
    return ("\n" + "\n".join(lines)) if lines else ""


def _uuid(ref: str) -> str:
    if re.fullmatch(r"o\d+", str(ref or "").strip().lower()):
        return _objective_ref(ref)
    try:
        return H.resolve(ref)
    except ValueError as e:
        # Live 12:3x: pickpocket(who="Shadowheart") / level_up(who="Wyll") as a fresh bridge's first calls failed
        # "unknown target": names are learnt from reads. A party/camp name is worth one status read.
        if "unknown target" not in str(e) or re.fullmatch(r"e\d+", str(ref or "").strip().lower()):
            raise
        _remember_names(game.try_rpc("state.status"))
        try:
            return H.resolve(ref)
        except ValueError as e2:
            # S029 16:04: talk_to(npc="Wulbren") as the session's first call: he stood in view, unnamed to a fresh
            # bridge. A character in sight is worth one nearby read (sight-checked by the mod, so fair).
            # S037 20:15: insert_item(into="Riddle of the Night") right after a travel: the pedestal stood in view,
            # unnamed until a get_nearby. Items in sight are learnt by the same read.
            if "unknown target" not in str(e2):
                raise
            _remember_names(game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters", "items"]}))
            return H.resolve(ref)


def _active() -> str:
    st = game.rpc("state.status")
    _remember_names(st)
    a = st.get("active")
    if not a:
        raise GameError("no active character")
    return a


def _who_uuid(ref: str) -> str:
    """A `who` reference: always one of ours. S035 19:32/19:38: a hostile copy named 'Karlach'/'Shadowheart' (Shadow-
    Cursed Lands) made who="Karlach" ambiguous; the party member is the only valid `who`, so take it."""
    try:
        return _uuid(ref)
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        party = _party_set()
        mine = [H.by_handle[h] for h in H.matches(ref) if H.by_handle.get(h) in party]
        if len(mine) == 1:
            return mine[0]
        # S034 19:17:15: two summons named Spiritual Weapon (e328 from an earlier cast, e612 now) stopped a chain
        # "is ambiguous". In a fight, the one in the turn order, alive, and acting now if several are, is meant.
        cs = game.try_rpc("state.combat") or {}
        cands = set(mine) or {H.by_handle.get(h) for h in H.matches(ref)}
        rows = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("uuid") in cands
                and not o.get("dead") and o.get("relation") != "hostile"]
        now = set(turn.acting(cs)) if cs.get("active") else set()
        pick = [o["uuid"] for o in rows if o["uuid"] in now] or [o["uuid"] for o in rows]
        if len(pick) == 1 or (pick and len({o["uuid"] for o in rows if o["uuid"] in now}) == 1):
            return pick[0]
        raise


def _who(ref: str | None) -> str:
    return _who_uuid(ref) if ref else _active()


def _world_uuid(ref: str) -> str:
    """A target out in the world (click_on/pick_up/interact) given by name: never an item the party carries.
    S035 19:37: click_on("Umbral Gem") for the trial's gem on the floor → "ambiguous: e55, e257", both gems in
    party bags. Carried namesakes are skipped; if only carried ones are known, one nearby read looks for one in
    sight, else the refusal says who holds it."""
    r = str(ref or "").strip()
    try:
        u = _uuid(r)
        err = None
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        u, err = None, e
    if re.fullmatch(r"e\d+", r.lower()):
        return u
    inv = game.try_rpc("state.inventory", {"expand": True}) or {}
    _remember_names(inv)
    carried = {it["uuid"]: it.get("owner") for it in inv.get("items") or []
               if isinstance(it, dict) and isinstance(it.get("uuid"), str)}
    if u is not None and u not in carried:
        return u

    def world() -> list[str]:
        return [H.by_handle[h] for h in H.matches(r) if H.by_handle.get(h) not in carried]

    w = world()
    if not w:
        _remember_names(game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters", "items"]}))
        w = world()
    if len(w) == 1:
        return w[0]
    if not w:
        held = ", ".join(f"{H.get(c)} by {_name_of(carried[c])}" if carried[c] else H.get(c)
                         for c in (H.by_handle[h] for h in H.matches(r)) if c in carried)
        raise ValueError(f"no '{r}' in sight; the party carries it ({held}): it is in a bag, not in the world "
                         "(get_inventory; get_nearby/get_frontier list what lies around)")
    opts = ", ".join(f"{H.get(x)} ({_name_of(x)})" for x in w[:8])
    raise ValueError(f"'{r}' is ambiguous: {opts} (party-carried ones skipped)") from err


def _interact_uuid(ref: str) -> str:
    """interact's target: a carried item can be used too (a book), so only an ambiguous name skips the carried."""
    try:
        return _uuid(ref)
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        return _world_uuid(ref)


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


# Outside combat, movement/interaction results carry no portrait block at all unless HP changed since the
# last result for that character (then one short hp=34->7/55); the position becomes the plain @(..).
_QUIET_AFTER = {"move_to", "walk_toward", "interact", "pick_up", "loot", "loot_corpse", "talk_to", "jump_to"}
_last_hp: dict[str, Any] = {}


def _trim_after(res: Any, tool: str = "", char: str | None = None) -> Any:
    """The mod's after={...} portrait summary, cut to what the agent can act on: no position when the result
    already has one (walks print it as dist=.. @(..)), and outside combat no action economy or slot table
    (always full there), only hp when hurt. Slots render flat (slots=1:3) so the {..} stays one level deep."""
    if not isinstance(res, dict) or not isinstance(res.get("after"), dict):
        return res
    a = res["after"]
    prev = _last_hp.get(char or "?")
    if a.get("hp") is not None:
        _last_hp[char or "?"] = a["hp"]
    if tool in _QUIET_AFTER and not a.get("in_combat"):
        out = {k: v for k, v in res.items() if k != "after"}
        if a.get("pos") is not None and res.get("pos") is None and res.get("position") is None:
            out["pos"] = a["pos"]
        hp, mx = a.get("hp"), a.get("max_hp")
        if hp is not None and mx is not None and prev is not None and hp != prev:
            out["hp"] = f"{render.num(prev)}->{render.num(hp)}/{render.num(mx)}"
        return out
    if a.get("in_combat"):
        keep = {k: a[k] for k in ("hp", "max_hp", "action", "bonus", "reaction", "movement", "my_turn") if k in a}
        for k in ("slots", "pact_slots"):
            if isinstance(a.get(k), dict) and a[k]:
                keep[k] = ",".join(f"{lv}:{n}" for lv, n in sorted(a[k].items()))
    else:
        keep = {}
        if a.get("hp") is not None and a.get("max_hp") is not None and a["hp"] < a["max_hp"]:
            keep["hp"] = f"{render.num(a['hp'])}/{render.num(a['max_hp'])}"
    if a.get("sneaking"):
        keep["sneaking"] = True
    if a.get("pos") is not None and res.get("pos") is None and res.get("position") is None:
        keep["pos"] = a["pos"]
    out = {k: v for k, v in res.items() if k != "after"}
    if keep:
        out["after"] = keep
    return out


def _job_text(name: str, r: JobResult, char: str | None = None) -> str:
    res = r.result
    if isinstance(res, dict) and "phases" in res:  # ui.roll's state timeline: metrics only (_roll_step)
        res = {k: v for k, v in res.items() if k != "phases"}
    r = JobResult(r.status, _trim_after(res, name, char), r.error, r.events, r.elapsed_ms)
    if name == "trade" and isinstance(r.result, dict) and ("wares" in r.result or "offer" in r.result):
        head = f"trade: done" if r.status == "done" else f"trade: {r.status.upper()}" + (f" — {r.error}" if r.error else "")
        return head + "\n" + economy.trade_text(r.result, _item_handle, **_trade_view)
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


# Turn-based mode outside combat (S021 burning inn, S024-S026 Shadow-Cursed Lands: the game forces it; every walk
# then failed "no path / 0 movement" and the agent hand-clicked the HUD's Exit TB button 2-10x per session).
# Ops that walk: before them, leave turn-based mode the way a player does (the HUD button, for the whole party).
_WALK_OPS = {"act.move_to", "act.interact", "act.pickup", "act.talk", "act.loot_corpse", "act.pickpocket"}
TB_REFUSAL = "turn-based mode outside combat"  # Actions.lua A.TB_REFUSAL: the walk op's refusal
TB_EXIT_WAIT_S = 2.0
_tb_wanted = {"on": False}  # the agent turned it on itself (set_party turn_based_on): walks keep it


def _tb_mode() -> dict:
    m = game.try_rpc("act.mode")
    return m if isinstance(m, dict) and "ftb" in m else {}


def _set_turn_based(on: bool) -> tuple[bool, str]:
    """Enter/leave turn-based mode outside combat for the whole party: press the HUD's Enter/Exit TB button
    (HotBar EnterFTBButton / ExitFTBButton, what the player clicks), else Osiris ForceTurnBasedMode per member."""
    btn = "EnterFTBButton" if on else "ExitFTBButton"
    how = []
    pressed = game.try_rpc("ui.press", {"widget": "HotBar", "button": btn}, context="client")
    if isinstance(pressed, dict) and pressed.get("done"):
        how.append(f"pressed the HUD's {'Enter' if on else 'Exit'} TB button")
        end = time.monotonic() + TB_EXIT_WAIT_S
        while time.monotonic() < end:
            if bool(_tb_mode().get("ftb")) == on:
                return True, how[0]
            time.sleep(0.15)
        how[0] += f" (no effect in {TB_EXIT_WAIT_S:g}s)"
    r = game.run_job("act.turn_based", {"on": on}, wait_s=6)
    if r.status == "done":
        how.append("ForceTurnBasedMode for the party")
        return True, ", then ".join(how)
    if r.status == "failed" and ("not in turn-based" in str(r.error) or "already in turn-based" in str(r.error)):
        return True, ", then ".join(how) if how else "nothing to do"
    left = (r.result or {}).get("still_turn_based") if isinstance(r.result, dict) else None
    how.append(f"ForceTurnBasedMode {r.status}: {r.error or ''}".rstrip(": ")
               + (f" (still turn-based: {', '.join(H.get(u) for u in left)})" if left else ""))
    return False, ", then ".join(how)


def _tb_refused(status: str | None, error: Any) -> bool:
    return status == "failed" and TB_REFUSAL in str(error or "")


def _leave_turn_based_for_walk() -> str:
    """The reflex after a walk op was refused for turn-based mode outside combat: leave it (the HUD button) and
    return the note; the caller retries the walk once."""
    ok, how = _set_turn_based(False)
    if ok:
        return (f"(the game had put the party in turn-based mode outside combat; left it first: {how}. In the "
                "Shadow-Cursed Lands it comes back while anyone is cursed: keep everyone in the light)")
    return (f"STILL IN TURN-BASED MODE outside combat ({how}): walks get only the turn's movement; "
            "click the Exit TB button (bottom right of the hotbar) or end_turn to get movement back")


def _log_stage_ms(op: str, r: JobResult) -> None:
    """The mod's staged UI jobs (level up, character creation) report time per stage in _stage_ms: logged to
    runtime/stage_ms.jsonl, never shown (S028: Wyll's camp level-up took 60 s and nothing said where)."""
    if not isinstance(r.result, dict) or "_stage_ms" not in r.result:
        return
    ms = r.result.pop("_stage_ms")
    try:
        with open(RUNTIME / "stage_ms.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "op": op, "status": r.status,
                                "elapsed_ms": r.elapsed_ms, "stages": ms}) + "\n")
    except OSError:
        pass


def _act(tool: str, op: str, args: dict, wait_s: float = 25.0, context: str = "server",
         wait: bool = True, note: str = "", post=None, on_start=None) -> str:
    t0 = time.monotonic()
    if _session_over():
        return _finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    if op in _WALK_OPS and context == "server" and _tb_wanted["on"]:
        args = dict(args, tb_ok=True)
    if not _batch["active"]:
        held = _hold_for_early_walks(args.get("char"))
        note = "\n".join(x for x in (held, note) if x)
    if not wait and not _batch["active"] and context == "server":
        return _start_async(tool, op, args, t0, note)
    ex = _batch["exec"]
    if _batch["active"]:
        wait_s = max(wait_s, 120.0)  # a batch/queue step runs to completion (interrupts cancel it)
    starts = [f for f in (ex.track if ex is not None else None, on_start) if f is not None]
    kw = {"on_start": (lambda ctx, jid: [f(ctx, jid) for f in starts])} if starts else {}
    r = game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if op in _WALK_OPS and _tb_refused(r.status, r.error):
        tb = _leave_turn_based_for_walk()
        note = "\n".join(x for x in (tb, note) if x)
        r = game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if context == "server" and r.status == "failed" and _IN_CONVO in str(r.error or "") and _close_left_trade():
        # S037 20:25:57: walk_toward right after trade(confirm) → "character is in a conversation"; the trade window
        # was still up (get_dialogue + ESCAPE, 2 more calls). Walking off closes a trade for a player: close, rerun.
        note = "\n".join(x for x in ("(closed the trade window first)", note) if x)
        r = game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    _log_stage_ms(op, r)
    _remember_names(r.result)
    _batch["last"] = r.status
    _batch["result"] = r.result
    if _batch["active"] and isinstance(r.result, dict) and "after" in r.result:
        # In a batch the per-step portrait summary is noise; the run ends with one NOW line + CHANGES.
        r = JobResult(r.status, {k: v for k, v in r.result.items() if k != "after"}, r.error, r.events, r.elapsed_ms)
    if post is not None:  # a line from after the job (use_ability: did the opener start a fight?)
        note = "\n".join(x for x in (post(r), note) if x)
    return _finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)


def _close_left_trade(wait_s: float = 2.0) -> bool:
    """A trade screen left open blocks every action as "in a conversation": close it with ESCAPE (the trade
    screen's own close) and wait for it to go. True = it was open and is closed now."""
    if _ui_screen()[0] != "trade":
        return False
    game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        time.sleep(0.2)
        if _ui_screen()[0] not in ("trade", "dialogue"):
            return True
    return False


# Async jobs started with wait=false: handle (j12) -> what the agent needs to follow them up.
_async: dict[str, dict] = {}


def _start_async(tool: str, op: str, args: dict, t0: float, note: str = "") -> str:
    """Start a mod job and return at once: "move_to: started j12, ETA 3.4s (14m)". The mod announces the
    end with a job_finished event (notify), so any later tool result shows it; wait_for('j12') blocks."""
    started = game.rpc(op, dict(args, notify=True))
    if op in _WALK_OPS and isinstance(started, dict) and _tb_refused(started.get("status"), started.get("error")):
        tb = _leave_turn_based_for_walk()
        note = "\n".join(x for x in (tb, note) if x)
        started = game.rpc(op, dict(args, notify=True))
    if not isinstance(started, dict) or "job" not in started:
        return _finish(tool, args, _job_text(tool, JobResult("done", result=started)), t0)
    if started.get("status") not in (None, "running"):
        r = JobResult(started["status"], started.get("result"), started.get("error"))
        _remember_names(r.result)
        _batch["last"] = r.status
        return _finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)
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
        return _finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)
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


# How long an action call waits on a just-started job before returning "started": refusals come back from the start
# RPC itself, so this only catches jobs that end at once. 1.0 s blocked the agent ~45-60 s/session (logs review
# 2026-10-10); a later failure still arrives as a job_finished event on the next result.
SETTLE_S = float(os.environ.get("BG3AI_SETTLE_S", "0.3") or 0)
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
        # state.inventory rides in the same tick for the ALERTS line (supplies, gold, encumbrance, gear);
        # test fakes without game.read don't serve it.
        real = hasattr(game, "read")
        ops = [("state.status", {}), ("nav.markers", {})] + ([("state.inventory", delta.INV.args())] if real else [])
        (st, mk, *inv), (ui, *ri) = _read(ops, [("ui.state", {})] + ([("ui.rest_info", {})] if real else []))
        inv = delta.INV.merge(inv[0]) if inv else None
        if ri and isinstance(ri[0], dict) and isinstance(ri[0].get("short_rests_left"), int):
            ALERTS.short_rests = ri[0]["short_rests_left"]
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
    apart = _party_check(st, ui=ui)
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    tk = _talk_line(st, force=full or first or _too_big(lines))
    if tk:
        body += "\n" + tk
    al = _alerts_line(st, inv, force=full or first or _too_big(lines))
    if al:
        body += "\n" + al
    ph = _party_hint_line(st, markers, force=full or first or _too_big(lines))
    if ph:
        body += "\n" + ph
    if _retry["status"] and _retry["rec"]:
        _retry["status"] = False   # the first look after a reload repeats it once
        body += "\n" + debrief.last_attempt(_retry["rec"])
    return _finish("get_status", {"full": full}, body, t0)


ALERTS = economy.AlertTracker()


def _name_of(u: str) -> str:
    h = H.get(u)
    return H.names.get(h) or h


def _alerts_line(st: Any, inv: Any, force: bool = False) -> str:
    """'ALERTS low HP Claude 34/88 | camp supplies 27/40 … | gold 93 < 200 …' (economy.alerts); shown with a
    full status and whenever the alerts change. Never raises: an observation must not fail on it."""
    try:
        if isinstance(inv, dict):
            _remember_names(inv)
        return ALERTS.line(economy.alerts(st, inv, _name_of, ALERTS.partial_rest, ALERTS.short_rests), force=force)
    except Exception:  # noqa: BLE001
        return ""


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
    apart = _party_check(st, ui=got.get("ui"))
    if apart:
        body += "\n" + apart
    lv = _level_up_line(st)
    if lv:
        body += "\n" + lv
    tk = _talk_line(st, force=full or first or _too_big(lines))
    if tk:
        body += "\n" + tk
    al = _alerts_line(st, got.get("inventory"), force=full or first or _too_big(lines))
    if al:
        body += "\n" + al
    ph = _party_hint_line(st, got.get("markers"), force=full or first or _too_big(lines))
    if ph:
        body += "\n" + ph
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


_TALK = companions.TalkTracker()


def _talk_line(st: Any, force: bool = False) -> str:
    """get_status/look: 'TALK: e5 Wyll wants to talk …' (the "!" over a companion) when it changes. Never raises."""
    try:
        return _TALK.line(st, _label, force=force)
    except Exception:  # noqa: BLE001
        return ""


PARTY_HINT = companions.HintTracker()
_no_join: dict[str, str] = {}  # camp companion uuid → region where its camp menu offered no join answer (swap_party)
COMPANIONS_MD = PLAY_DIR / "knowledge" / "companions.md"


def _party_hint_line(st: Any, markers: Any, force: bool = False) -> str:
    """'PARTY: Halsin (camp, Druid 5) is tied to Shadow-Cursed Lands … → swap_party(…) | …' (companions.hint):
    out of combat/dialogue, printed when it changes or with a full view. The skill read (state.camp) runs
    only when the party or camp roster changed. Never raises: an observation must not fail on it."""
    try:
        if not isinstance(st, dict) or st.get("in_combat") or st.get("in_dialogue"):
            return ""
        party_rows = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
        camp = [c for c in st.get("camp") or [] if isinstance(c, dict)
                and _no_join.get(c.get("uuid")) != str(st.get("region") or "")]
        if PARTY_HINT.need_skills(party_rows, camp):
            got = game.try_rpc("state.camp") if camp else None
            if isinstance(got, dict):
                PARTY_HINT.skills = {r.get("uuid"): r.get("skills") or {} for r in (got.get("party") or []) + (got.get("camp") or [])}
                PARTY_HINT.avatars = {r.get("uuid") for r in got.get("party") or [] if r.get("avatar")}
            else:
                PARTY_HINT.skills, PARTY_HINT.avatars = None, set()
        av = getattr(PARTY_HINT, "avatars", set())
        party_rows = [dict(r, avatar=True) if r.get("uuid") in av else r for r in party_rows]
        quests = sorted({str(m.get("quest")) for m in markers or [] if isinstance(m, dict) and m.get("quest")})
        bits = companions.hint(party_rows, camp, str(st.get("region") or ""), quests,
                               companions.ties(COMPANIONS_MD), _label, PARTY_HINT.skills)
        return PARTY_HINT.line(bits, force=force)
    except Exception:  # noqa: BLE001
        return ""


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


def _at_camp(ui: dict | None) -> bool:
    """The minimap says Campsite: companions idle at their tents there (S014: PARTY APART 16-23 m x3 while
    long resting, a false alarm). The camp is left with the camp tool, which brings everyone along."""
    return isinstance(ui, dict) and str(ui.get("region") or "").strip().lower() == "campsite"


def _party_check(st: dict | None, rechain: bool = True, ui: dict | None = None) -> str:
    """get_status/look: re-chain companions that drifted off the leader's group (unless told to stay, busy,
    or in combat/dialogue), then the PARTY APART line ('' when everyone is together or at camp)."""
    if not isinstance(st, dict) or _at_camp(ui):
        return ""
    try:
        rows = party.apart(st, stay=_stay)
        if rechain and rows and not st.get("in_combat") and not st.get("in_dialogue") and st.get("active"):
            done = set(_rechain(party.rechain_candidates(rows, _member_busy), st["active"]))
            for r in rows:
                if r["uuid"] in done:
                    r["unchained"] = False
            rows = [r for r in rows if r["unchained"] or r["other_level"] or r.get("dy")
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
    u = _who_uuid(who)
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
def get_party(camp: bool = False) -> str:
    """Each party member's key numbers: HP, AC, level, conditions, resources (spell slots, actions), concentration.
    camp=true: instead, the companions waiting at camp (class, level, pending level up, best skills, weapons and armour) — who swap_party could bring in."""
    t0 = time.monotonic()
    if camp:
        got = game.rpc("state.camp")
        _remember_names(got)
        body = "\n".join(companions.camp_lines(got.get("camp") or []))
        return _finish("get_party", {"camp": True}, body, t0)

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
    args = {"uuid": _who_uuid(who)} if who else {}  # no uuid: the mod uses the selected character
    if sections:
        args["sections"] = [s.strip() for s in sections.split(",") if s.strip()]
    res = _read1("state.character", args)
    _remember_names(res)
    return _finish("get_character", {"who": who, "sections": sections}, render.compact(res, H), t0)


# Action lists show names only (render.ACTION_IDS False; ~0.8 KB per character per combat): use_ability and
# combat_turn take the shown name and map it back to the id from the character's own list. who -> name -> ids.
_ability_ids: dict[str, dict[str, list[str]]] = {}
_ABILITY_ID = re.compile(r"[A-Za-z]+_\w+")


def _learn_actions(who: str | None, acts: Any) -> None:
    rows = acts.get("actions") if isinstance(acts, dict) else acts
    if not who or not isinstance(rows, list):
        return
    names: dict[str, list[str]] = {}
    for a in rows:
        if isinstance(a, dict) and a.get("name") and a.get("spell"):
            ids = names.setdefault(str(a["name"]).strip().lower(), [])
            if a["spell"] not in ids:
                ids.append(a["spell"])
    _ability_ids[who] = names


def _ability_id(who: str, ability: str) -> str:
    """An ability id from what the agent wrote: an id as is, 'Fire Bolt (Projectile_FireBolt)' → the id, or a
    shown name ('Fire Bolt', any case) → the id from who's action list (read once on a miss)."""
    a = str(ability or "").strip()
    m = re.fullmatch(r".*\((" + _ABILITY_ID.pattern + r")\)\s*", a)
    if m:
        return m.group(1)
    if _ABILITY_ID.fullmatch(a) or not a:
        return a
    ids = (_ability_ids.get(who) or {}).get(a.lower())
    if ids is None:
        _learn_actions(who, game.try_rpc("state.actions", {"uuid": who}))
        ids = (_ability_ids.get(who) or {}).get(a.lower())
    if not ids:
        ids = _ability_by_words(who, a)
    if not ids:
        raise GameError(f"{_label(who)} has no action called '{a}' (get_actions lists them)")
    if len(ids) > 1:
        raise GameError(f"{_label(who)} has {len(ids)} actions called '{a}': give the id ({', '.join(ids)})")
    return ids[0]


def _unknown_ability_line(line: str, slot: str) -> bool:
    """A combat_turn slot line whose alternatives all failed on a wrong ability id/name (refused before anything
    was spent), e.g. 'action: Shout_Dodge: FAILED — unknown action Shout_Dodge (not an ability id; …)'."""
    if not line.startswith(f"{slot}:") or ": done" in line:
        return False
    return "not an ability id" in line or "has no action called" in line


def _id_words(i: str) -> list[str]:
    """Shout_Rage_Frenzy → ['rage', 'frenzy'] (the type prefix dropped, CamelCase split)."""
    parts = i.split("_")[1:] if "_" in i else [i]
    return [w.lower() for p in parts for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", p)]


def _ability_by_words(who: str, name: str) -> list[str]:
    """A name that is no shown name but starts exactly one of the character's ids. S029: Karlach's plan said
    "Rage"; a Berserker's rage is shown as 'Frenzy' (Shout_Rage_Frenzy), so the bonus slot was lost."""
    want = [w.lower() for w in re.findall(r"[A-Za-z]+|\d+", name)]
    if not want:
        return []
    ids = sorted({i for v in (_ability_ids.get(who) or {}).values() for i in v})
    hits = [i for i in ids if _id_words(i)[:len(want)] == want]
    return hits if len(hits) == 1 else []


_ATTACKISH = re.compile(r"(?i)mainhandattack|_attack$|^attack$|^weapon attack$")


def _own_attack(who: str, ability: str) -> tuple[str, str]:
    """A guessed weapon attack the character doesn't have → its own weapon attack, with a line saying so.
    S028: the Spiritual Weapon summon lost rounds 5 and 6 to 'Target_MainHandAttack: the character does not
    have that action' and 'unknown action Target_SpiritualWeapon_Attack'; its attack is 'Greataxe Slash'
    (Target_MainHandAttack_SpiritualWeapon_Greataxe, a bonus action). Unchanged (and no note) when the
    character has that id, it isn't an attack, or its own attack isn't one clear id."""
    if not ability or not _ATTACKISH.search(ability) and "SpiritualWeapon" not in ability:
        return ability, ""
    if who not in _ability_ids:
        _learn_actions(who, game.try_rpc("state.actions", {"uuid": who}))
    shown = {i: n for n, ids in (_ability_ids.get(who) or {}).items() for i in ids}
    if not shown or ability in shown:
        return ability, ""
    if any(ability.startswith(i + "_") for i in shown):
        # S033 18:37: Shadowheart's Spiritual Weapon variant Target_SpiritualWeapon_Greataxe (the container
        # Target_SpiritualWeapon is what she has) became her Main Hand Attack: a variant of a spell it has is a cast.
        return ability, ""
    pre = "Projectile_MainHandAttack" if ability.startswith("Projectile_") else "Target_MainHandAttack"
    own = sorted(i for i in shown if i.startswith(pre))
    if len(own) != 1:
        return ability, ""
    return own[0], (f"{ability}: {_label(who)} has no such action → its own weapon attack "
                    f"'{shown[own[0]]}' ({own[0]})")


@mcp.tool()
@_guard
def get_actions(who: str | None = None) -> str:
    """The actions/spells/items a character can use right now (as on the hotbar), with costs and availability."""
    t0 = time.monotonic()
    res = _read1("state.actions", {"uuid": _who_uuid(who)} if who else {})
    _learn_actions(_who_uuid(who) if who else (res.get("uuid") if isinstance(res, dict) else None), res)
    body = render.render_actions(res, H)
    return _finish("get_actions", {"who": who}, body, t0)


_NEARBY_KINDS = {"characters", "items", "door", "container", "item", "ladder", "usable", "trap", "object"}


def _nearby_kinds(kinds: str) -> tuple[list[str], list[str]]:
    """Split get_nearby's kinds into mod kinds and name words. S026: kinds="button" and kinds="props" (the
    delta's own "+37 more: props 37" word) both answered "Nothing visible in range" while Buttons stood 5 m away."""
    mod, words = [], []
    for k in (k.strip().lower() for k in kinds.split(",") if k.strip()):
        k1 = {"chars": "characters", "npcs": "characters", "doors": "door", "containers": "container",
              "ladders": "ladder", "traps": "trap", "objects": "object"}.get(k, k)
        (mod if k1 in _NEARBY_KINDS else words).append(k1)
    if words:
        mod = sorted(set(mod) | {"characters", "items", "object"})
    return mod, words


def _nearby_match(res: dict, words: list[str]) -> dict:
    """Keep what a word names: "props" = decoration (candles, vases...), anything else = a name substring
    (singular or plural)."""
    def hit(o: dict) -> bool:
        name = str(o.get("name") or "").lower()
        for w in words:
            if w == "props":
                if delta.is_clutter(o):
                    return True
            elif w in name or (w.endswith("s") and w[:-1] in name):
                return True
        return False
    out = dict(res)
    out["characters"] = [c for c in res.get("characters") or [] if hit(c)]
    out["items"] = [it for it in res.get("items") or [] if hit(it)]
    out["remembered"] = [r for r in res.get("remembered") or [] if hit(r)]
    out.pop("more_items", None)
    return out


def _nearby_miss_text(full: dict, words: list[str], radius: float) -> str:
    """A name search that matched nothing: what was asked, then the characters in view (names + ids)."""
    names = ", ".join(repr(w) for w in words)
    chars = [c for c in full.get("characters") or [] if isinstance(c, dict) and c.get("uuid")]
    out = f"No {names} in view within {render.num(radius)}m (a name match, part of a name is enough)."
    if chars:
        shown = [f"{H.get(c['uuid'], c.get('name'))} {c.get('name') or '?'}" for c in chars[:8]]
        out += " Characters in view: " + ", ".join(shown) + (f" (+{len(chars) - 8} more)" if len(chars) > 8 else "")
    else:
        out += " No characters in view."
    return out + " Someone out of sight: get_map / the journal for where they went, or route to their last place."


@mcp.tool()
@_guard
def get_nearby(radius: float = 25.0, kinds: str = "") -> str:
    """Visible characters and interactable objects near the party (ids, distance, relation, HP, locked/opened, [lit]/[glowing] = an object's visible puzzle state, e.g. a painting lit by its button), plus remembered last-seen positions. kinds: optional filter: characters, items, door, container, item, ladder, usable, trap, props (decoration), or words to match names (e.g. "button,lever"). Lookalikes show as one line "(+N more <name>)"; kinds=<that name> lists each with its id."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"radius": radius}
    words: list[str] = []
    if kinds:
        args["kinds"], words = _nearby_kinds(kinds)
        if words:
            args["item_limit"] = 400
    res = _read1("state.nearby", args)
    if words and isinstance(res, dict):
        full, res = res, _nearby_match(res, words)
        if not any(res.get(k) for k in ("characters", "items", "remembered")):
            # S042 22:51 kinds="Jaheira,characters" → "Nothing visible in range." while 9 corpses and the party stood
            # there: say the name search found nothing, and who IS in view
            _remember_names(full)
            return _finish("get_nearby", args, _nearby_miss_text(full, words, radius), t0)
    elif kinds and "characters" not in args["kinds"] and isinstance(res, dict):
        # Remembered = characters out of sight: noise in a search for doors/items (S018-S035: 65 of 123 such calls
        # carried ~4 REMEMBERED lines each, e.g. the Self-Same mirrors in every gem search)
        res = {k: v for k, v in res.items() if k != "remembered"}
    _remember_names(res)
    if radius == 25.0 and not kinds and isinstance(res, dict):
        DELTA.observe(delta.snapshot(H, nearby=res))  # the agent has now seen these; look() won't repeat them
    return _finish("get_nearby", args, render.render_nearby(res, H, fold=not [w for w in words if w != "props"]), t0)


@mcp.tool()
@_guard
def look_at(target: str) -> str:
    """What the Examine panel shows for a visible character or object (stats, resistances, conditions, description)."""
    t0 = time.monotonic()
    res = _read1("state.entity", {"uuid": _uuid(target)})
    _remember_names(res)
    return _finish("look_at", {"target": target}, render.compact(_shown_statuses(res), H), t0)


def _shown_statuses(res: Any) -> Any:
    """Examine shows a condition's name, never the engine's status id (SCL_MOONSHIELD, MAG_..._BUFF: ids carry
    hints the name doesn't; fairness audit S023-S027). The id stays only where a status has no name."""
    if not isinstance(res, dict) or not isinstance(res.get("statuses"), list):
        return res
    sts = [{k: v for k, v in s.items() if k != "id"} if isinstance(s, dict) and s.get("name") else s
           for s in res["statuses"]]
    return {**res, "statuses": sts}


@mcp.tool()
@_guard
def get_inventory(who: str | None = None, filter: str = "") -> str:
    """Party inventory (or one member's): equipment, notable items (story, magic, consumables, tools, bags) and a junk summary; plus the open container, camp supplies carried and gear upgrades. filter: "equipped" | "consumables" | "food" (camp supplies: supply=N per item) | "junk" (what trade sell=junk sells) | "gear" (better gear in bags) | "all" | words to match names (e.g. "potion")."""
    t0 = time.monotonic()
    args: dict[str, Any] = {}
    if who:
        args["uuid"] = _who_uuid(who)
    if filter and filter.strip().lower() not in ("equipped", "notable"):
        args["expand"] = True  # food sits in the Camp Supply Sacks, keys on the keychain (S026: filter=food found nothing)
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
        body = render.render_map(res, H)
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
def get_layout(radius: float = 12.0, who: str | None = None, to: str = "") -> str:
    """The walls, openings and floor around you as a text map (what the overhead camera shows), N up, you = @: mazes, rooms, ledges, chasms. Marks the party (1-3), characters (E hostile, n other), doors (D), ladders (H), levers/buttons (L), containers (C). In a trial room with a floor mosaic it also prints the mosaic's picture and, standing at its start, the MOSAIC PATH along its lines to the far end (to="x,z" or an id: to the line nearest that instead) as one do_actions of straight move_to legs. radius 4-20 m."""
    t0 = time.monotonic()
    char = _who_uuid(who) if who else _active()
    lay = _read1("nav.layout", {"char": char, "radius": radius})
    st = game.try_rpc("state.status") or {}
    near = game.try_rpc("state.nearby", {"radius": min(max(radius, 4.0), 20.0) * 1.5}) or {}
    _remember_names(near)
    goal = _layout_goal(to, near) if to else None
    text = render.render_layout(lay if isinstance(lay, dict) else {}, H, near, st.get("party") or [], char, goal)
    return _finish("get_layout", {"radius": radius}, text, t0)


def _layout_goal(to: str, near: dict) -> tuple[float, float]:
    """get_layout to=: "x,z" (or "x,y,z"), or the id/name of something in view -> (x, z)."""
    nums = re.findall(r"-?\d+(?:\.\d+)?", to)
    if "," in to and len(nums) in (2, 3):
        return float(nums[0]), float(nums[-1])
    u = _uuid(to)
    for o in (near.get("characters") or []) + (near.get("items") or []):
        if isinstance(o, dict) and o.get("uuid") and H.get(o["uuid"]) == H.get(u) and isinstance(o.get("pos"), list) and len(o["pos"]) >= 3:
            return float(o["pos"][0]), float(o["pos"][2])
    raise ValueError(f"to={to!r}: give x,z or the id of something in view")


@mcp.tool()
@_guard
def route(objective: str = "", target: str = "", x: float | None = None, y: float | None = None,
          z: float | None = None, who: str | None = None, connectors: bool = False, go: bool = False) -> str:
    """The way to a goal, read from the engine instead of a screenshot: objective (o1, or a map marker m1), a visible target id, or x,y,z.
    Says how far and how high it is (floors above/below you and at the goal), the walk in legs (8m NW, CLIMB 9m up, 6m E) with
    fire on the way, and when no path exists: the closest walkable spot and the ladders/doors in view ranked by how much they
    help (which way a ladder climbs), ending with one NEXT step. connectors=true lists ladders/doors even when a path exists.
    go=true: check and go in one call: starts travel_to the goal when a path exists, else to the closest walkable spot."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": _who_uuid(who)} if who else {}  # no char: nav.route uses the active one
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
    seen_txt = ""
    try:
        res = _read1("nav.route", args)
    except GameError as e:
        seen = _last_seen(args["target"]) if "target" in args and "not visible" in str(e) else None
        if not seen:
            raise
        # out of sight now: the way to where the party last saw them (S025 route e992/e6 "not visible")
        seen_txt = _seen_line(target, seen) + "\n"
        args["pos"] = seen[0]
        del args["target"]
        res = _read1("nav.route", args)
    _remember_names(res)
    ends = DEAD.for_goal(args.get("pos") or (res.get("goal") if isinstance(res, dict) else None))
    tele = _teleport_hint(res)
    text = _finish("route", {"objective": objective, "target": target},
                   seen_txt + route_text.render_route(res, H, what, ends) + tele, t0)
    if go and isinstance(res, dict):
        # S023-S026: route then travel_to the same goal (17x) or to its closest walkable spot (12x): 2 model turns.
        c = res.get("closest") if isinstance(res.get("closest"), dict) else {}
        if res.get("reachable"):
            dest = {"target": target} if target and "pos" not in args else {"x": args["pos"][0], "y": args["pos"][1],
                                                                             "z": args["pos"][2]}
        elif isinstance(c.get("pos"), (list, tuple)) and len(c["pos"]) >= 3:
            dest = {"x": c["pos"][0], "y": c["pos"][1], "z": c["pos"][2]}
        else:
            return text + "\ngo: not started (no path and no closest walkable spot)"
        return text + "\nGO " + travel_to(who=who, **dest)
    return text


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
    # loose: a winding walk counts when nothing direct does; alts: a dead direction names the ones that walk
    res = game.rpc("nav.reach_toward", {**args, "loose": True, "alts": "dir" in args})
    if not isinstance(res, dict) or not res.get("reachable") or not res.get("pos"):
        n = res.get("samples") if isinstance(res, dict) else None
        hz = macros.hazard_text(res.get("hazard")) if isinstance(res, dict) else ""
        if isinstance(res, dict) and res.get("hidden_floor"):
            return _finish("walk_toward", args, f"FAILED: the way toward {what} is the chasm, with no floor you can see"
                           f" ({_hidden_text(res['hidden_floor'])}). On an invisible-floor trial walk in short steps"
                           " (move_to ≤ 3m) along the mosaic's lines (get_layout).", t0)
        if hz:
            return _finish("walk_toward", args, f"FAILED: the way toward {what} crosses {hz}. Go around it"
                           " (another direction, get_frontier) or "
                           f"{macros.hazard_way_out(res.get('hazard'))}.", t0)
        body = (f"FAILED: no walkable ground toward {what} within {render.num(max_m)}m"
                + (f" ({n} spots checked)" if n is not None else ""))
        hint = _route_hint(args)  # toward an objective/target only (a direction has no goal)
        others = navhints.other_dirs_text(res.get("others") if isinstance(res, dict) else None)
        return _finish("walk_toward", args, body + ("\n" + hint if hint else others + "." if others else
                                                    ". Try another direction, get_frontier, or a door."), t0)
    head = f"heading {res.get('dir') or ''} toward {what}: {render.num(res.get('straight_m', 0))}m"
    if res.get("path_m") is not None:
        head += f" (path {render.num(res['path_m'])}m{', winding' if res.get('winding') else ''})"
    if res.get("needs_jump"):
        head += ", needs a jump"
    if res.get("hazard_beyond"):
        head += f", stopping short of {macros.hazard_text(res['hazard_beyond'])}"
    if res.get("hidden_beyond"):
        head += f", stopping at the chasm edge (no floor you can see beyond, {_hidden_text(res['hidden_beyond'])})"
    if res.get("standing_in"):
        head += f" (you are standing in {res['standing_in']})"
    wait, note = _walk_wait(wait)
    body = _act("walk_toward", "act.move_to", {"char": args["char"], "pos": res["pos"], "run": run}, wait=wait,
                note=note)
    return head + "\n" + body


def _hidden_text(h: dict) -> str:
    """Where a walk would leave visible floor for a trial's invisible one (nav hidden_floor {pos, at_m})."""
    p = h.get("pos") if isinstance(h, dict) else None
    at = f"{render.num(h.get('at_m', 0))}m along, " if isinstance(h, dict) and h.get("at_m") is not None else ""
    return at + ("at " + render.pos(p) if isinstance(p, list) and len(p) == 3 else "ahead")


@mcp.tool()
@_guard
def get_combat_state() -> str:
    """Turn order (visible combatants), whose turn it is, round, and the active character's remaining action economy."""
    t0 = time.monotonic()
    return _finish("get_combat_state", {}, _combat_state_text(), t0)


def _combat_state_text() -> str:
    res = game.rpc("state.combat")
    _remember_names(res)
    if isinstance(res, dict) and not res.get("active", True) and len(res) <= 1:
        _acts_shown.clear()
        _light_noted.clear()
        return "Not in combat."
    body = render.render_combat(res, H) if isinstance(res, dict) else render.compact(res, H)
    # One read should be enough to plan the whole turn: add the acting party member's actions.
    st = game.try_rpc("state.status") or {}
    party = {c.get("uuid") for c in st.get("party") or []}
    hint = _fight_hint(res, party)
    if hint:
        body += "\n" + hint
    mine = turn.party_acting(res, party) if isinstance(res, dict) else []
    cur = st.get("active") if st.get("active") in mine else (mine[0] if mine else None)
    if len(mine) > 1:
        body += ("\nSHARED TURN: " + ", ".join(_label(u) for u in mine)
                 + " can all act now, in any order (combat_turn who=..., end each one's turn)")
    if cur in party:
        acts = game.try_rpc("state.actions", {"uuid": cur})
        _learn_actions(cur, acts)
        if acts:
            mv = (acts.get("resources") or {}).get("Movement")  # [amount, max] (State.lua resourcesView)
            rl = turn.reach_line(res, cur, mv[0] if isinstance(mv, (list, tuple)) and mv else None, _label)
            if rl:
                body += "\n" + rl
            body += "\nYOUR ACTIONS (" + H.get(cur) + ")\n" + render.render_actions(acts, H)
            _acts_shown[cur] = set(render.render_actions(acts, H).splitlines()[1:])
    body += _light_note(res)
    return body


_light_noted: set[str] = set()  # members told this combat that they fight with the lantern


def _light_note(cs: Any) -> str:
    """'\nLIGHT: Claude fights with the Moonlantern …' once per member per fight (S025-S027: Claude, a paladin,
    swung the Moonlantern with the Sword of Justice in a bag; dropping it mid-fight in the curse loses Moonshield
    for everyone near, so the fix is after the fight: equip_best hands the light to the caster)."""
    if not isinstance(cs, dict) or not cs.get("active"):
        _light_noted.clear()
        return ""
    out = []
    for o in cs.get("order") or []:
        u, light = o.get("uuid"), o.get("light_in_hand")
        if light and u and u not in _light_noted:
            _light_noted.add(u)
            out.append(f"LIGHT: {_label(u)} fights with the {light} in the main hand (torch damage). Keep it held "
                       "in the Shadow Curse (its aura is the party's Moonshield); after the fight equip_best gives "
                       "it to a caster and puts a real weapon back in this hand.")
    return "".join("\n" + x for x in out)


# A names-only action line (render.ACTION_IDS False): the name ends where the first detail starts.
_ACT_NAME = re.compile(r"(.*?)(?= \[| [\d.]+m\b| self\b| point\b| L\d\b| uses | aoe | —|$)")

# Action lines last listed per character this combat: combat_turn's next-up listing repeated the same ~40 lines
# (~2 KB) every turn in S011 (Jump, Dip, Shove, all spells). get_combat_state always lists everything.
_acts_shown: dict[str, set[str]] = {}


def _actions_delta(uuid: str, acts: Any) -> str:
    """'\n<full list>' the first time this combat, then only the NOW line and what changed since."""
    text = render.render_actions(acts, H)
    lines = text.splitlines()
    head = lines[:1] if lines and lines[0].startswith("NOW ") else []
    body = lines[len(head):]
    prev = _acts_shown.get(uuid)
    _acts_shown[uuid] = set(body)
    if prev is None:
        return "\n" + text
    new = [x for x in body if x not in prev]

    def key(x: str) -> str:  # "Name (spell_id)" or "Name [cost]": a changed cost/uses line is a '+', not a '-'
        m = re.match(r"(.*?\(\S+\))", x) or re.match(_ACT_NAME, x)
        return m.group(1) if m else x
    renewed = {key(x) for x in new}
    unav = next((x for x in body if x.startswith("UNAVAILABLE")), "")

    def now_unavailable(x: str) -> bool:  # S026: 13 L3 spells were in the '+ UNAVAILABLE' line and listed again as '-'
        m = None if " (" in x else re.match(_ACT_NAME, x)
        name = m.group(1) if m else x.split(" (", 1)[0]
        return bool(unav) and re.search(r"(?<![\w'])" + re.escape(name) + r"(?=[,;( ]|$)", unav) is not None
    gone = sorted(x for x in prev if x not in body and key(x) not in renewed and not x.startswith("UNAVAILABLE")
                  and not now_unavailable(x))
    if len(gone) + len(new) > len(body) // 2:
        return "\n" + text
    out = head + [f"(other actions as listed before for {H.get(uuid)}; get_combat_state lists them all)"]
    out += ["+ " + x for x in new] + ["- " + key(x) for x in gone]
    return "\n" + "\n".join(out)


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
            who: str | None = None, run: bool = True, wait: bool = False, within: float | None = None) -> str:
    """Walk to a visible entity (target=id) or a position (x,y,z). Returns within ~1 s: the real result if it already ended (blocked, refused, arrived), else a job id + ETA while the walk goes on; keep planning meanwhile. wait=true blocks only in combat. Stops early if combat/dialogue starts. who defaults to the active character (chained companions follow). Out of combat a destination past ~58 m goes via travel_to (legs) by itself. within=1.5 (with target): stop inside that melee reach."""
    args: dict[str, Any] = {"char": _who(who), "run": run}
    if target:
        args["target"] = _uuid(target)
        if within is not None:
            args["within"] = float(within)
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


def _last_seen(uuid: str) -> tuple[list, int | None] | None:
    """(position, seconds ago) where the party last saw a character that is out of sight now (the mod's memory of
    this level: what get_nearby lists as REMEMBERED), else None (visible, never seen, or an item).
    S026: talk_to Mattis/e511/e720 refused "not visible" 7x; each cost a get_nearby + a walk + a retry."""
    ent = game.try_rpc("state.entity", {"uuid": uuid}) or {}
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
    """Use/open/activate a visible object: door, lever, chest, corpse (opens loot), book, ladder, waypoint. Walks there first (past 40 m: a travel_to, then the use on arrival). A locked door/chest opens when anyone in the party carries its key (that member walks up and opens it). wait=false returns at once with a job id."""
    char = _who(who)
    out = _act("interact", "act.interact", {"char": char, "target": _interact_uuid(target)}, wait=wait)
    return _walk_first("interact", out, char, who, target, actq.Step("interact", {"target": target})) or out


@mcp.tool()
@_guard
def pick_up(item: str, who: str | None = None, wait: bool = True) -> str:
    """Pick up a visible item from the ground (walks there first). wait=false returns at once with a job id."""
    return _act("pick_up", "act.pickup", {"char": _who(who), "item": _world_uuid(item)}, wait=wait)


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


def _box_act(tool: str, op: str, args: dict, wait_s: float, rounds: int = 3, on_start=None) -> str:
    """_act for the client jobs that answer the game's Yes/No box (rests, fast travel, confirm). When the mod
    says its button press didn't land (needs_click; live S013/S014 "[ForceUpdate] Yes/No" stayed open), click
    the button with a hover-checked mouse click (boxclick) and run the op again from where it stopped."""
    from . import boxclick

    out = _act(tool, op, args, context="client", wait_s=wait_s, on_start=on_start)
    notes: list[str] = []
    for _ in range(rounds):
        res = _batch.get("result")
        ans = res.get("needs_click") if isinstance(res, dict) else None
        inp = _inp() if ans else None
        if inp is None:
            break
        c = boxclick.click_box(lambda o, a: game.rpc(o, a, context="client"), inp, str(ans))
        q = res.get("question")
        notes.append(boxclick.describe(c) + (f" (the question: {q})" if q and "[ForceUpdate]" not in str(q) else ""))
        if tool in ("confirm", "fast_travel") or not (c.get("closed") or c.get("next") or c.get("gone")):
            if tool != "long_rest" and tool != "short_rest" and not c.get("error"):
                out = f"{tool}: done — {notes.pop()}" + ("; travelling" if tool == "fast_travel" else "")
            break
        time.sleep(0.5)  # the next screen (rest supplies, travel) opens
        out = _act(tool, op, args, context="client", wait_s=wait_s)
    if notes and ": done" not in out.split("\n", 1)[0]:
        # Only on a failure: S033 18:36 a full rest read "(the button press didn't land …)" first, as if it failed.
        out = "(the button press didn't land: " + "; ".join(notes) + ")\n" + out
    return out


def _click_act(tool: str, op: str, args: dict, wait_s: float = 40.0) -> str:
    """Server op that walks up and then needs a verified click on the target (engine has no direct call)."""
    from .clicker import run_click_action

    t0 = time.monotonic()
    if _session_over():
        return _finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    r = run_click_action(game, _inp(), op, args, wait_s)
    _remember_names(r.result)
    _batch["last"] = r.status
    return _finish(tool, args, _job_text(tool, r, args.get("char")), t0)


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
def pickpocket(npc: str, who: str | None = None, take: str = "", min_chance: int = 0, leave: bool = False) -> str:
    """Sneak up to a visible NPC and open the pickpocket window (best while sneaking and unseen); the result lists their pockets with the game's own odds per item (chance %, DC). take: steal that item (name or id) in the same call (the window is opened first if needed); min_chance: take nothing below this %. A failed steal is a crime if they notice. leave=true closes the window."""
    t0 = time.monotonic()
    target = _uuid(npc)
    args: dict[str, Any] = {"npc": target}
    if leave:
        r = _pockets(dict(args, leave=True))
        gone = r.status != "done" and "no pickpocket window" in str(r.error or "")   # it had closed itself (live 12:4x)
        return _finish("pickpocket", {"npc": npc, "leave": True},
                       "pickpocket: left (window closed)" if r.status == "done" else
                       "pickpocket: left (no window was open)" if gone else _job_text("pickpocket", r), t0)
    out = sel = ""
    listing = _pockets(args)
    if listing.status != "done" and "no pickpocket window" in str(listing.error or ""):
        char = _who(who)
        if who and char != (game.try_rpc("state.status") or {}).get("active"):
            # Live 12:3x: who=Shadowheart while Claude was controlled: the engine opened the window, but the client
            # shows it only for the controlled character ("no pickpocket window" after 3 s). Take control, as a player
            # does before stealing with someone else.
            r = game.run_job("act.select", {"char": char}, wait_s=5)
            sel = f"(took control of {who}: the window shows for the controlled character)" if r.status == "done" else ""
        out = _click_act("pickpocket", "act.pickpocket", {"char": char, "target": target})
        if _batch.get("last") != "done":
            return out
        # The engine says opened before the client widget shows (live 12:5x: Dammon's window up ~1 s later).
        deadline = time.monotonic() + 3.0
        listing = _pockets(args)
        def early(r: JobResult) -> bool:   # widget not up yet, or up with its slots not filled yet (live 12:5x)
            if r.status == "done":
                return not (r.result or {}).get("items")
            return "no pickpocket window" in str(r.error or "")
        while early(listing) and time.monotonic() < deadline:
            time.sleep(0.25)
            listing = _pockets(args)
    if listing.status != "done":
        return _finish("pickpocket", args, (out + "\n" if out else "") + _job_text("pickpocket", listing), t0)
    _remember_names(listing.result)
    text = economy.pockets_text(listing.result, _pocket_handle)
    if take:
        tk = _uuid(take) if re.fullmatch(r"e\d+", take.strip()) else take   # a handle from the listing, else a name
        r = _pockets(dict(args, take=tk, min_chance=min_chance or None))
        _remember_names(r.result)
        text = (economy.steal_text(r.result, _pocket_handle) if r.status == "done" else _job_text("pickpocket", r)) + "\n" + text
    head = out.split("\n", 1)[0] + "\n" if out else ""
    return _finish("pickpocket", {"npc": npc, "take": take}, head + text + ("\n" + sel if sel else ""), t0)


def _pockets(args: dict) -> JobResult:
    """ui.pickpocket as a JobResult; its refusals (no window open) come back as failed, not raised."""
    try:
        return game.run_job("ui.pickpocket", args, context="client", wait_s=15)
    except Exception as e:  # noqa: BLE001
        return JobResult("failed", error=str(e))


def _pocket_handle(it: dict) -> str:
    u = it.get("uuid")
    return H.get(u, it.get("name")) if isinstance(u, str) and u else ""


def _carried_scroll(char: str, ability: str) -> dict | None:
    """A scroll (any carried item) named like `ability` ('Scroll of Invisibility' or just 'Invisibility'): the
    character's own first, else a party member's. S030 16:34: use_ability(Shadowheart, "Scroll of Invisibility")
    → "no action called", then manage_inventory use left the game waiting for a target (screenshot + hand click)."""
    a = re.sub(r"\s+", " ", str(ability or "").strip().lower())
    if not a:
        return None
    want = {a, a if a.startswith("scroll of ") else f"scroll of {a}"}
    items = [it for it in ((game.try_rpc("state.inventory", {}) or {}).get("items") or [])
             if isinstance(it, dict) and str(it.get("name") or "").strip().lower() in want and it.get("uuid")]
    if not items:
        return None
    return next((it for it in items if it.get("owner") == char), items[0])


def _use_scroll(char: str, it: dict, target: str) -> str:
    """Read a scroll as a player does: use it from the bags (the game then waits for a target), then click the
    target (default: the reader). A scroll on another member's bags is named, not taken."""
    name = str(it.get("name"))
    if it.get("owner") and it.get("owner") != char:
        raise GameError(f"{_label(char)} has no action called that; {_label(it['owner'])} carries {name} "
                        f"({H.get(it['uuid'])}): use_ability(who=\"{_name_of(it['owner'])}\", ability=\"{name}\", "
                        f"target=...) or give it over first")
    out = _act("use_ability", "act.interact", {"char": char, "target": it["uuid"]})
    if _batch.get("last") != "done" or "drunk" in out.partition("\n")[0]:
        return out  # a potion (in combat: for its bonus action) waits for no target
    inp = _inp()
    if inp is None:
        return out + "\nthe game waits for the scroll's target: no input shim to click it (click_on <id>)"
    from .clicker import click_entity
    tgt = _uuid(target) if target else char
    time.sleep(0.3)  # the targeting cursor comes up a few frames after the use
    scr = (game.try_rpc("ui.state", context="client") or {}).get("screen")
    if scr == "book":
        # S031 17:12: use_ability(book, target=pedestal) read the book (its page opened) and the click then turned
        # the camera 6x behind the page: a book is read, not cast on anything.
        head, _, rest = out.partition("\n")
        head += (f" ({name} opened for reading: it has no target; press_key ESCAPE closes the page. To put it on a "
                 f"pedestal or in a slot: insert_item(item, into))")
        return head + ("\n" + rest if rest else "")
    cr = click_entity(game, inp, tgt, "L")
    head, _, rest = out.partition("\n")
    if cr.ok:
        head += f" ({name} read, cast on {_label(tgt)} by a click)"
    else:
        head += (f"; the game waits for {name}'s target but the click on {_label(tgt)} failed ({cr.reason}): "
                 f"click_on <id>, or press_key ESCAPE to put it away")
    return head + ("\n" + rest if rest else "")


@mcp.tool()
@_guard
def click_on(target: str, button: str = "left") -> str:
    """Last-resort: point the mouse at a visible entity (verified hover) and click it, for interactions no other tool covers. Logged."""
    from .clicker import click_entity

    t0 = time.monotonic()
    inp = _inp()
    if inp is None:
        raise GameError("input shim not available")
    cr = click_entity(game, inp, _world_uuid(target), "R" if button.lower().startswith("r") else "L")
    tail = f"\nthe {cr.screen} screen covers the world: press_key ESCAPE first" if cr.reason == "covered" else ""
    return _finish("click_on", {"target": target}, ("clicked" if cr.ok else f"FAILED: {cr.reason}") + " — " + render.compact(cr.as_dict(), H) + tail, t0)


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
    """Walk up to a visible character (follows them if they move) and start a conversation. The result already shows the dialogue at its first real choice (opening filler clicked through); answer with choose_dialogue. Someone out of sight whom the party saw on this level, or past 40 m: walks there first and talks on arrival (queued)."""
    char = _who(who)
    out = _act("talk_to", "act.talk", {"char": char, "npc": _uuid(npc)}, wait_s=30, wait=wait)
    # S026: 7 refusals out of sight (Mattis, e511, e720); S028: 2 "too far away (47-50m, max 40m)" at camp.
    return _walk_first("talk_to", out, char, who, npc, actq.Step("talk_to", {"npc": npc})) or out


_TOO_FAR_RX = re.compile(r"is too far away \((\d+(?:\.\d+)?)m from this character, max \d+m\)")


def _walk_first(tool: str, out: str, char: str, who: str | None, target: str, step) -> str | None:
    """A refusal that a walk fixes, as a player clicks it and the character walks there: the target is past the
    act range (S023 interact 75 m, S028 talk_to 47/50 m and interact 40 m at camp) or out of sight but seen on this
    level (S026 talk_to 7x). Starts travel_to there and queues `step` for the arrival; None when it doesn't apply
    (inside do_actions, in combat, another refusal, never seen)."""
    if _batch["active"]:
        return None
    far = _TOO_FAR_RX.search(out)
    if not far and "target is not visible" not in out:
        return None
    if (_watch_conn().try_rpc("act.watch") or {}).get("combat"):
        return None
    if far:
        dest: dict[str, Any] = {"target": target}
        line = f"{target} is {far.group(1)}m away: walking there first"
    else:
        seen = _last_seen(_uuid(target))
        if not seen:
            return None
        dest = {"x": seen[0][0], "y": seen[0][1], "z": seen[0][2]}
        line = _seen_line(target, seen)
    trip = travel_to(who=who, **dest)
    if "FAILED" in trip or "REFUSED" in trip:
        return out + "\n" + trip
    queued = _enqueue(char, [step], ignore=None)
    return f"{line}; then {tool} {target} on arrival ({queued})\n{trip}"


@mcp.tool()
@_guard
def jump_to(x: float, y: float, z: float, who: str | None = None, wait: bool = True, force: bool = False) -> str:
    """Jump to a position (costs movement/bonus action in combat; distance limited by Strength). A landing in a harmful surface (fire, acid, ...) or on a wall, or a long jump that may fall short into one, is moved to the nearest clear spot near it within reliable reach (~4.5 m) and says so; with no clear spot near it the jump is refused. force=true jumps exactly there."""
    char = _who(who)
    pos, note = [x, y, z], ""
    if not force:
        try:
            lnd = game.rpc("nav.landing", {"char": char, "pos": pos})
        except NotReady:
            raise  # S038: the check hung the server; an unchecked jump on top of it helps nothing
        except GameError:
            lnd = None
        refusal = navhints.jump_refusal(lnd, pos)  # with a working plan (walk / walk-then-jump) when one exists
        if refusal:
            if _batch["active"]:
                _batch["last"] = "failed"
            return refusal
        if isinstance(lnd, dict) and lnd.get("safe") is False:
            bad = navhints.landing_bad_text(lnd)
            pos = list(lnd["suggest"])
            if lnd.get("floor_y") is not None:  # the same column's floor (a guessed y over the chasm around it)
                note = (f"landing {render.pos([x, y, z])} {bad}: the floor there is at y {render.num(lnd['floor_y'])}, "
                        f"jumping to {render.pos(pos)} ({render.num(lnd.get('suggest_dist_m', '?'))}m jump, clear)")
            else:
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
        t0 = time.monotonic()
        on = a == "turn_based_on"
        if _tb_mode().get("combat"):
            return _finish("set_party", {"action": a}, "set_party: FAILED — in combat (turn-based mode ends with the fight)", t0)
        ok, how = _set_turn_based(on)
        _tb_wanted["on"] = on and ok
        text = f"set_party: {'done' if ok else 'FAILED'} — turn-based mode {'on' if on else 'off'} ({how})"
        _batch["last"] = "done" if ok else "failed"
        return _finish("set_party", {"action": a}, text, t0)
    raise ValueError("unknown action")


def _wait_running(max_s: float = 60.0) -> bool:
    """After a teleport/load: the client back to Running and the server answering."""
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        try:
            if game.game_state() == "Running" and game.server_ready():
                return True
        except (GameError, NotReady, OSError):
            pass
        time.sleep(0.4)
    return False


def _settle_hud(max_s: float) -> bool:
    """After a rest/camp transition: wait until the HUD is up and nothing blocks travel on two polls in a row
    (live 11:4x: M pressed right after a camp leave was ignored for 3.6 s)."""
    deadline, good = time.monotonic() + max_s, 0
    while time.monotonic() < deadline:
        b = game.try_rpc("ui.travel_block", context="client")
        good = good + 1 if isinstance(b, dict) and b.get("hud") and not b.get("error") else 0
        if good >= 2:
            return True
        time.sleep(0.5)
    return False


def _end_night_first() -> str:
    """Live 2026-10-10: IsAtNightCamp=true + TravelError=Unavailable for hours: a long rest whose night never
    ended (29/31 fast_travel and 7 camp-leave failures 10:00-10:52). Travelling first needs the night to end, as a
    player would by finishing the rest: ui.long_rest(sleep_only) presses the camp's rest button at night, which
    (live 11:2x) opens the rest screen again; it rests from there, partially if supplies are short. '' when it
    isn't night at camp, else the line to show ('FAILED …')."""
    b = game.try_rpc("ui.travel_block", context="client")
    if not (isinstance(b, dict) and b.get("night_camp")):
        return ""
    out = _box_act("long_rest", "ui.long_rest", {"sleep_only": True}, wait_s=60)
    res = _batch.get("result")
    if isinstance(res, dict) and res.get("night_ended"):
        _wait_running(30)
        # Live 11:2x-3x: the party sleeps (TravelError 'Condition'), then the morning moves it out of camp with the
        # HUD hidden; M pressed meanwhile never opened the map. Wait for the HUD and no travel error.
        _settle_hud(NIGHT_SLEEP_WAIT_S)
        how = res.get("rest") or "slept"
        used = res.get("supplies_used")
        return ("ended the night at camp first (a long rest's night was still running, which blocks travel): rested ("
                + str(how) + (f", {used} supplies" if used is not None else "") + ")")
    first = out.splitlines()[0] if out else "?"
    return ("FAILED: the night at camp hasn't ended (a long rest's night: travel is blocked until you sleep) — "
            + first[:300])


def _wake_first() -> str:
    """Acceptance 12:16: long_rest returned 3.6 s in with the party still Sleeping (a camp scene, TravelError
    'Condition', no night_camp flag) and the next fast_travel failed in 0.1 s 'can't fast travel now (Condition)'.
    A player waits for the morning: wait for the HUD and no travel error. '' when nothing to wait for."""
    b = game.try_rpc("ui.travel_block", context="client")
    if not (isinstance(b, dict) and b.get("error") == "Condition" and not b.get("dialogue")):
        return ""
    t = time.monotonic()
    ok = _settle_hud(NIGHT_SLEEP_WAIT_S)
    return f"waited {time.monotonic() - t:.0f}s for the party to wake up after the rest" + ("" if ok else " (still blocked)")


def _leave_camp_first() -> str | None:
    """Live 2026-10-10: a fast travel started at camp moved only the controlled character (companions and the
    camera stayed at the Campsite, PARTY APART 1800 m). ui.fast_travel refuses at camp, so leave it first, as the
    camp tool does (everyone comes along). Returns the line to show, '' when not at camp, or 'FAILED …'."""
    night = _end_night_first()
    if night.startswith("FAILED"):
        return night
    night = "\n".join(x for x in (night, _wake_first()) if x)
    try:
        game.rpc("ui.fast_travel", {"waypoint": "-", "dry": True}, context="client")
        return night
    except GameError as e:
        if "at camp" not in str(e):
            return night  # any other refusal: the real call reports it
    _travel_block(TRAVEL_BLOCK_WAIT_S)
    r = game.run_job("act.leave_camp", {}, wait_s=60)
    if r.status != "done":
        return "FAILED to leave camp first — " + _job_text("camp", r) + _travel_block_text(_travel_block())
    if not _wait_running(60):
        return "FAILED: left camp, but the game didn't come back to Running within 60 s"
    _settle_hud(15)
    return "\n".join(x for x in (night, "left camp first (a fast travel from camp leaves your companions behind)") if x)


@mcp.tool()
@_guard
def fast_travel(waypoint: str) -> str:
    """Travel to an unlocked waypoint (name or id from get_map). Not possible in combat. From camp it leaves camp
    first (everyone comes along), then travels."""
    left = _leave_camp_first()
    if left.startswith("FAILED"):
        _batch["last"] = "failed"
        return _finish("fast_travel", {"waypoint": waypoint}, "fast_travel: " + left, time.monotonic())
    # S035 19:27:44, 19:39:13: "can't fast travel now (Location)" twice inside the Gauntlet of Shar, no reason or
    # way out. The greyed map button says it all along: an area block answers at once, like camp().
    if not left and _travel_block().get("error") == "Location":
        _batch["last"] = "failed"
        return _finish("fast_travel", {"waypoint": waypoint}, "fast_travel: FAILED — can't fast travel now "
                       "(Location): this area does not allow travel (a dungeon or story area; camp() is blocked "
                       "too). Walk out the way you came (travel_to the spot where camp/fast_travel last worked), "
                       "then fast_travel.", time.monotonic())
    watch = _ReadyCheckWatch()
    with watch:
        out = _box_act("fast_travel", "ui.fast_travel", {"waypoint": waypoint}, wait_s=70, on_start=watch.on_start)
    if watch.failed is not None:
        if _ui_screen()[0] == "map":
            game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
        _batch["last"] = "failed"
        return _finish("fast_travel", {"waypoint": waypoint}, (left + "\n" if left else "") + (
            f"fast_travel: FAILED — the game's party ready check for the trip failed ({watch.failed}); stopped "
            f"waiting at once (S032: 66 s each). A broken ready check comes with a long-lived game process (the "
            f"runner relaunches the game between sessions); until then walk (travel_to) or try a waypoint in this "
            f"region"), time.monotonic())
    if out.rstrip().endswith("travelling"):
        # The box needed a mouse click (region change): the mod's job had stopped there, so nothing watched the
        # trip. Live 11:4x: "travelling" came back mid-load and the next reads saw the loading screen.
        if _load_started(10.0):
            arrived = _wait_running(120) and _settle_hud(20)
            out += "; arrived" if arrived else "; still not back in the game 2 min later (wait_for idle)"
    return (left + "\n" + out) if left else out


class _ReadyCheckWatch:
    """While a fast travel runs, watch the server's rest_flow events (Events.lua REST_FLOW) for the party ready
    check failing and cancel the client job then. S032 17:23:43 / 17:24:57: `ready_failed:WaypointTravel_RegionSwap`
    came at once, but ui.fast_travel (a client job: it can't see Osiris) waited out its 66 s each time."""

    POLL_S = 0.5

    def __init__(self) -> None:
        self.job: tuple[str, int] | None = None
        self.failed: str | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def on_start(self, context: str, job_id: int) -> None:
        self.job = (context, job_id)

    def __enter__(self) -> "_ReadyCheckWatch":
        self._seq = int((getattr(game, "event_seq", None) or {}).get("server", 0) or 0)
        self._t = threading.Thread(target=self._run, daemon=True, name="ready-check-watch")
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(2.0)

    def check(self, res: Any) -> bool:
        """One events.since answer; True when the ready check failed (sets self.failed)."""
        for e in (res or {}).get("events") or [] if isinstance(res, dict) else []:
            self._seq = max(self._seq, int(e.get("seq") or 0))
            step = str((e.get("data") or {}).get("step") or "") if isinstance(e.get("data"), dict) else ""
            if e.get("kind") == "rest_flow" and step.startswith("ready_failed"):
                self.failed = step.split(":", 1)[1] if ":" in step else "ready check"
                return True
        return False

    def _run(self) -> None:
        wg = _watch_conn()
        while not self._stop.wait(self.POLL_S):
            try:
                res = wg.rpc("events.since", {"seq": self._seq, "limit": 200}, context="server", timeout=3.0)
            except Exception:  # noqa: BLE001 - a load or a busy server: try again next beat
                continue
            if self.check(res):
                if self.job is not None:
                    try:
                        wg.rpc("job.cancel", {"id": self.job[1]}, context=self.job[0], timeout=3.0)
                    except Exception:  # noqa: BLE001 - the job may have just ended
                        pass
                return


NIGHT_SLEEP_WAIT_S = 30.0  # after a night's rest: TravelError 'Condition' while the party has Sleeping
TRAVEL_BLOCK_WAIT_S = 8.0  # right after a load the game blocks travel ("Unavailable") for a few seconds


def _travel_block(wait_s: float = 0.0) -> dict:
    """ui.travel_block, polled up to wait_s while the game says travel is Unavailable (live 2026-10-10: transient
    after a load). {} when the client can't say."""
    deadline = time.monotonic() + wait_s
    while True:
        b = game.try_rpc("ui.travel_block", context="client") or {}
        if not b.get("error") or b.get("dialogue") or time.monotonic() >= deadline:
            return b
        time.sleep(0.4)


def _travel_block_text(b: dict) -> str:
    """Why the game won't travel, from what a player sees (greyed map/camp button, an open conversation)."""
    if not b:
        return ""
    if b.get("dialogue"):
        return " A conversation is open: finish it first (choose_dialogue)."
    if b.get("night_camp"):
        return " It is night at camp: a long rest's night hasn't ended (long_rest goes to sleep and ends it)."
    if b.get("error"):
        return (f" The game blocks travel here ({b['error']}): the camp and map travel buttons are greyed out."
                f" On screen: {b.get('screen') or '?'}. Talk to whoever waits nearby, finish the camp scene, or walk"
                f" (travel_to) and try again.")
    return ""


@mcp.tool()
@_guard
def camp(action: str = "go") -> str:
    """action: go (travel to camp, like the camp button) | leave (return to where you were)."""
    if action == "leave":
        night = _end_night_first()
        if night.startswith("FAILED"):
            return _finish("camp", {"action": action}, "camp: " + night, time.monotonic())
        night = "\n".join(x for x in (night, _wake_first()) if x)
        _travel_block(TRAVEL_BLOCK_WAIT_S)
        out = _act("camp", "act.leave_camp", {}, wait_s=60)
        if _batch.get("last") == "done":
            _settle_hud(15)  # the leave's transition: the next tool (fast_travel's M) works only after it
        out = (night + "\n" + out) if night else out
        if "cannot travel right now" in out:
            out += _travel_block_text(_travel_block())
        return out
    # S035 19:27:30, 19:38:58: camp() pressed the button inside the Gauntlet of Shar and the job failed only after
    # its 12 s wait, with the reason (Location) the greyed button showed all along. A player sees the grey button:
    # an area block reads first and answers at once (transient errors still get the press).
    b = _travel_block()
    if b.get("error") == "Location":
        _batch["last"] = "failed"
        return _finish("camp", {"action": action}, "camp: FAILED — cannot travel right now —" + _camp_block_text(b),
                       time.monotonic())
    out = _act("camp", "act.go_to_camp", {}, wait_s=60)
    if isinstance(out, str) and "cannot travel right now" in out:
        out += _camp_block_text(_travel_block())
    return out


_CAMP_BLOCKS = {  # the HUD's TravelError (camp button greyed) → why, and the way out
    "Combat": "a fight is on: finish it first (combat_turn), then camp()",
    "Dialog": "a conversation is open: finish it first (choose_dialogue)",
    "Condition": "a party member's condition blocks it (asleep after a rest, downed, ...): wait a few seconds, "
                 "heal/raise the downed member, then camp() again",
    "DangerZone": "this is a danger zone (enemies or a hostile area around): walk away from it, then camp() again",
    "InDanger": "the party is in danger (hostiles close or aware of you): walk away, then camp() again",
}


def _camp_block_text(b: dict) -> str:
    """camp() go failed: the game's own reason (what the greyed camp button says) and the way out. S030 16:46:
    'cannot travel right now' inside the Gauntlet of Shar with no reason, and the agent gave up on resting."""
    if b.get("dialogue"):
        return " " + _CAMP_BLOCKS["Dialog"] + "."
    err = b.get("error")
    if err in _CAMP_BLOCKS:
        return f" The game blocks camp travel ({err}): {_CAMP_BLOCKS[err]}."
    if b.get("night_camp"):
        return " It is night at camp: a long rest's night hasn't ended (long_rest goes to sleep and ends it)."
    head = f" The game blocks camp travel here ({err})" if err else \
        " The game gave no reason (the camp button isn't greyed)"
    return (head + ": this area does not let the party go to camp (a dungeon or story area). Ways out: walk back "
            "out the way you came (to where camp() last worked) or fast_travel to a waypoint, then camp(); here "
            "short_rest, potions and healing spells are what's left.")


SWAP_DIALOGUE_S = 25.0  # swap_party: the longest it drives the companion's dialogue


WALK_UP_SHORT_M = 2.0  # _walk_up_to stops this far short of the character (act.talk's approach covers the rest)


def _walk_up_to(char: str, uuid: str, name: str, pos: list | None = None) -> str:
    """Walks `char` next to a character too far to talk to (one walk, or travel_to's legs past MOVE_CAP_M).
    pos: where it stands when it isn't in view (state.camp rows carry it for the camp companions)."""
    far = _far_move(char, None if pos else uuid, pos)
    if far:
        dest, d = far
        here = _ent_pos(char)
        if here and d > WALK_UP_SHORT_M:
            # Acceptance 12:2x (swap to Wyll, 59 m): the walk ended on his spot and travel_to added a 3.4 m second leg
            # (1.6 s). Stop short of him on the way in: the talk job walks the last metres itself.
            k = (d - WALK_UP_SHORT_M) / d
            dest = [here[0] + (dest[0] - here[0]) * k, dest[1], here[2] + (dest[2] - here[2]) * k]
        out = travel_to(x=dest[0], y=dest[1], z=dest[2], who=H.get(char))   # who: a handle; runs async
        if "started" in out:
            wait_for(target="idle", timeout_s=60, lead_s=0)
        first = out.splitlines()[0] if out else ""
        return f"walked to {name} ({render.num(round(d))}m)" + (f": {first[:160]}" if "FAILED" in first else "")
    dest = {"pos": list(pos[:3])} if pos else {"target": uuid}
    r = game.run_job("act.move_to", {"char": char, "run": True, **dest}, wait_s=45)
    return f"walked to {name}" + ("" if r.status == "done" else f" ({_job_text('move_to', r)[:120]})")


@mcp.tool()
@_guard
def swap_party(into: str, out: str = "", leave_camp: bool = True) -> str:
    """Swap a companion waiting at camp into the active party in one call, out of combat, from anywhere: goes to camp, talks to `into`, picks its join answer (and, with a full party, the answer that sends `out` to camp), leaves camp and re-chains everyone to the leader. out: who goes to camp (required with 4 members; never your own character). get_party(camp=true) lists who waits at camp; get_status's PARTY line says when a swap pays off. A camp scene or an answer it can't recognise stops it and shows the dialogue: answer with choose_dialogue, then call it again. A companion whose menu has no join answer (the game's story state) is reported and left."""
    t0 = time.monotonic()
    args = {"into": into, "out": out}

    def done(body: str) -> str:
        return _finish("swap_party", args, body, t0)

    if _session_over():
        return done("REFUSED: the session is over. Save and write your handoff notes.")
    st = game.rpc("state.status")
    _remember_names(st)
    if st.get("in_combat") or st.get("in_dialogue"):
        return done("swap_party: REFUSED — not during " + ("combat" if st.get("in_combat") else "a conversation"))
    info = game.try_rpc("state.camp") or {}
    _remember_names(info)
    camp_rows = [c for c in info.get("camp") or st.get("camp") or [] if isinstance(c, dict)]
    party_rows = [r for r in info.get("party") or st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    inc = companions.find_row(camp_rows, into, _uuid)
    if inc is None:
        if companions.find_row(party_rows, into, _uuid):
            return done(f"swap_party: {into} is already in the party")
        return done(f"swap_party: FAILED — {into} is not waiting at camp. At camp: "
                    + (", ".join(f"{c.get('name')} ({companions.class_text(c)})" for c in camp_rows) or "nobody"))
    outr = companions.find_row(party_rows, out, _uuid) if out else None
    if out and outr is None:
        return done(f"swap_party: FAILED — {out} is not in the party ({', '.join(str(r.get('name')) for r in party_rows)})")
    if outr is not None and outr.get("avatar"):
        return done(f"swap_party: REFUSED — {outr.get('name')} is your own character; name a companion as out")
    full = len(party_rows) >= 4
    if full and outr is None:
        live = [r for r in party_rows if not r.get("dead")]
        sug = companions.swap_out(live, [r for r in live if not r.get("avatar")], inc)
        return done("swap_party: the party is full (4): name who goes to camp, e.g. swap_party(into=\""
                    + str(inc.get("name")) + "\", out=\"" + str((sug or {}).get("name", "?")) + "\")")
    steps = []
    ui = game.try_rpc("ui.state", context="client") or {}
    went = False
    if not _at_camp(ui):
        # S039 21:48:34 (Nightsong's Prison): "FAILED at go to camp — camp: FAILED — cannot travel right now" with no
        # reason. Same area pre-check as camp(): a greyed camp button (Location) answers at once, with the way out.
        b = _travel_block()
        if b.get("error") == "Location":
            return done("swap_party: FAILED at go to camp — cannot travel right now —" + _camp_block_text(b)
                        + " Swap once camp() works again.")
        r = game.run_job("act.go_to_camp", {}, wait_s=60)
        if r.status != "done" and "already at camp" not in str(r.error or ""):
            # (live 11:14: with the level-up page up the UI didn't say "camp" while the story's DB did: not a failure)
            text = _job_text("camp", r)
            if "cannot travel right now" in text:
                text += _camp_block_text(_travel_block())
            return done("swap_party: FAILED at go to camp — " + text)
        went = r.status == "done"
        steps.append("went to camp" if went else "at camp")
        time.sleep(1.0)
        ui = game.try_rpc("ui.state", context="client") or {}
        if ui.get("screen") == "dialogue":
            return done("\n".join(steps + ["STOPPED: a camp scene started on arrival: answer it (choose_dialogue), "
                                            f"then swap_party(into=\"{inc.get('name')}\", out=\"{out}\") again"]
                                   + _render_ui(ui)))
    leader = st.get("active") or _active()
    # The join menu is the player's own character's (S032 17:27: Shadowheart was in control, Wyll: "I'm all chatted
    # out ... I may have a word with the chief", no window, swap FAILED; the agent then walked Claude over by hand,
    # ~55 s). Talk as the avatar; the leader is the fallback when no avatar row is known.
    talker = next((r["uuid"] for r in party_rows if r.get("avatar") and r.get("uuid") and not r.get("dead")), leader)
    r = game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
    if r.status != "done" and any(w in _job_text("talk_to", r) for w in ("too far away", "not visible")):
        # Live 2026-10-10: Halsin stood 66 m from the camp arrival point, out of view (talk is capped at 40 m and
        # needs sight): walk up to where state.camp says it stands first.
        steps.append(_walk_up_to(talker, inc["uuid"], str(inc.get("name")), inc.get("pos")))
        r = game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
    if r.status != "done":
        return done("\n".join(steps + ["swap_party: FAILED to talk to " + str(inc.get("name")) + " — " + _job_text("talk_to", r)]))
    picked, stop = [], None
    deadline = time.monotonic() + SWAP_DIALOGUE_S
    opened = asked = False
    while time.monotonic() < deadline:
        ui = game.try_rpc("ui.state", context="client") or {}
        if ui.get("screen") != "dialogue":
            if not opened and time.monotonic() < deadline - SWAP_DIALOGUE_S + 3.0:
                time.sleep(0.25)
                continue
            break
        opened = True
        d = ui.get("dialogue") or {}
        opts = [o for o in d.get("options") or [] if not o.get("off")]
        if d.get("changing") or d.get("listening") or (not opts and not d.get("continue")):
            time.sleep(0.3)
            continue
        i, what = None, ""
        if not opts:
            i, what = 0, "continue"
        else:
            if outr is not None and asked:
                # only after the join answer: a companion's top menu may name others ("About Gale...")
                i = companions.pick_swap(opts, str(outr.get("name")))
                what = f"send {outr.get('name')} to camp"
            if i is None and not asked:
                i, what = companions.pick_join(opts), "join"
                asked = i is not None
            if i is None:
                ok, fi, _ = macros.dialogue_filler(d, strict=True)
                i, what = (fi, "continue") if ok else (None, "")
        if i is None and not asked:   # no join answer in the companion's menu (pick_join found none)
            lv = next((int(o.get("i") or 0) for o in opts if re.match(r"\s*leave\b", str(o.get("text") or ""), re.I)), 0)
            if lv:
                # Live 2026-10-10 (SCL camp): Halsin's menu had no join answer at all (Wyll's had "I'd like you to
                # join me."): the game decides when a companion may join. Leave and stop suggesting it here.
                name = str(inc.get("name"))
                game.run_job("ui.dialogue_choose", {"i": lv}, context="client", wait_s=10)
                _no_join[inc["uuid"]] = str(st.get("region") or "")
                if went and leave_camp:
                    game.run_job("act.leave_camp", {}, wait_s=60)
                    steps.append("left camp")
                menu = "; ".join(str(o.get("text"))[:50] for o in opts)
                return done("\n".join(steps + [
                    f"swap_party: {name} doesn't offer to join right now: the camp menu has no join answer ({menu}). "
                    f"The game decides when (story/quest state, e.g. after a camp scene or a quest step). Left the "
                    f"conversation; the PARTY hint skips {name} in this region."]))
        if i is None:
            stop = "no join/swap answer recognised: pick it with choose_dialogue (then camp(action='leave'), regroup)"
            break
        if what != "continue":
            txt = next((str(o.get("text")) for o in opts if int(o.get("i") or 0) == i), "")
            picked.append(f"{what}: '{txt[:70]}'")
        rj = game.run_job("ui.dialogue_choose", {"i": i}, context="client", wait_s=20)
        if rj.status != "done":
            stop = f"could not pick option {i}: {rj.error or rj.status}"
            break
        time.sleep(0.3)
    else:
        stop = "the dialogue is still open after " + str(int(SWAP_DIALOGUE_S)) + " s"
    steps += picked
    if stop:
        ui = game.try_rpc("ui.state", context="client") or {}
        return done("\n".join(steps + [f"STOPPED: {stop}"] + _render_ui(ui)))
    st2 = game.rpc("state.status")
    _remember_names(st2)
    now = {r.get("uuid") for r in st2.get("party") or []}
    joined = inc["uuid"] in now
    left = outr is None or outr["uuid"] not in now
    if not joined:
        return done("\n".join(steps + [f"swap_party: FAILED — {inc.get('name')} did not join (party: "
                                        + ", ".join(_label(u) for u in now) + "). Talk to it: talk_to(\"" + str(inc.get("name")) + "\")"]))
    steps.append(f"{_label(inc['uuid'])} joined" + (f", {outr.get('name')} waits at camp" if outr is not None and left else ""))
    if outr is not None and not left:
        steps.append(f"WARNING: {outr.get('name')} is still in the party")
    if went and leave_camp:
        r = game.run_job("act.leave_camp", {}, wait_s=60)
        steps.append("left camp" if r.status == "done" else "leave camp: " + _job_text("camp", r))
        st2 = game.try_rpc("state.status") or st2
    lead = st2.get("active") or leader
    members = [r.get("uuid") for r in st2.get("party") or [] if r.get("uuid") != lead and not r.get("summon")
               and r.get("uuid") not in _stay]
    took = _rechain(members, lead) if members else []
    if took:
        steps.append("chained to " + _label(lead) + ": " + ", ".join(_label(u) for u in took))
    lv = _level_up_line(st2)
    if lv:
        steps.append(lv)
    PARTY_HINT.skill_sig = None  # the roster changed: the next status re-reads the skills
    return done("swap_party: done — " + "; ".join(steps))


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


def _roll_line(roll: dict, label: str = "roll") -> str:
    """One plain line for a roll result: 'roll: Persuasion (Charisma Check) DC 16: d20 4 + 7 = 11 → FAILED'.
    The raw dict hid the outcome (success was only a missing key) and FinalResult is the natural d20, not the
    total (S024: result=13 bonus=6 vs DC 18 = success)."""
    what = " ".join(x for x in (str(roll.get("skill") or ""), f"({roll['check']})" if roll.get("check") else "") if x)
    head = f"{label}: {what}" if what else label
    if roll.get("dc") is not None:
        head += f" DC {roll['dc']}"
    die, bonus = roll.get("result"), roll.get("bonus")
    if isinstance(die, (int, float)):
        if isinstance(bonus, (int, float)):
            sign = "+" if bonus >= 0 else "-"
            nums = f"d20 {die:g} {sign} {abs(bonus):g} = {die + bonus:g}"
        elif isinstance(bonus, list) and len(bonus) == 2:
            nums = f"d20 {die:g} + {bonus[0]}..{bonus[1]}"
        else:
            nums = f"d20 {die:g}"
        head += f": {nums}"
    if roll.get("advantage"):
        head += f" ({roll['advantage']})"
    head += " → SUCCESS" if roll.get("success") else " → FAILED"
    if roll.get("inspiration") is not None:
        head += f" (inspiration {roll['inspiration']})"
    return head


def _roll_step(op_args: dict, label: str, wait_s: float) -> tuple[str, dict | None]:
    """One ui.roll step; a result that carries a roll is shown as _roll_line instead of the raw dict."""
    text = _act("roll_check", "ui.roll", op_args, context="client", wait_s=wait_s)
    res = _batch.get("result") if isinstance(_batch.get("result"), dict) else {}
    roll = res.get("roll") if isinstance(res.get("roll"), dict) else None
    if res.get("phases"):
        METRICS.write({"type": "roll_phases", "t": round(time.time(), 3), "phases": res["phases"]})
    if _batch.get("last") == "done" and roll and roll.get("phase") == "result":
        line = _roll_line(roll, label)
        if res.get("retriggered"):
            line += f" (start re-sent {res['retriggered']}x)"
        text = re.sub(r"\Aroll_check: done — [^\n]*", lambda _m: line, text, count=1)
    return text, roll


@mcp.tool()
@_guard
def roll_check(boosts: str = "", reroll_on_fail: bool = False) -> str:
    """Resolve the skill-check roll on screen: optionally add boosts shown on the roll screen (comma list, e.g. "Guidance"), roll, and continue."""
    args: dict[str, Any] = {"reroll": reroll_on_fail}
    if boosts:
        args["boosts"] = [b.strip() for b in boosts.split(",") if b.strip()]
    try:
        first, roll = _roll_step(args, "roll", 20)
    except GameError as e:
        # S035 19:24:20: roll_check(boosts="Guidance") on a roll without Guidance → FAILED, and the agent rolled bare
        # in a second call. A boost the roll doesn't offer can't be added by hand either: roll without it, say so.
        if not args.get("boosts") or "no boost '" not in str(e):
            raise
        args.pop("boosts")
        first, roll = _roll_step(args, "roll", 20)
        first = f"(rolled without {boosts}: {str(e).split(' on this roll ', 1)[-1].strip('()')})\n" + first
    # ui.roll stops at the result screen (so a reroll can be chosen); "roll, and continue" means one call:
    # press Continue (or reroll a failure first) here, not in a second roll_check round trip (live S002).
    outs = [first]
    if _batch.get("last") != "done" or not isinstance(roll, dict) or roll.get("phase") != "result":
        return first
    if reroll_on_fail and not roll.get("success"):
        if roll.get("inspiration") == 0:
            outs.append("(no reroll: no Inspiration points left)")
        else:
            again, _ = _roll_step({"reroll": True}, "reroll (1 Inspiration)", 25)
            res = _batch.get("result") if isinstance(_batch.get("result"), dict) else {}
            if res.get("reroll_ignored"):
                again = "(no reroll: the game kept the first result; the Inspiration reroll was not offered)"
            outs.append(again)
            if _batch.get("last") != "done":
                return "\n".join(outs)
    outs.append(_act("roll_check", "ui.roll", {}, context="client", wait_s=30))
    outs = [re.sub(r"\n⏱[^\n]*$", "", o) for o in outs[:-1]] + outs[-1:]
    return "\n".join(outs)


@mcp.tool()
@_guard
def react(use: bool, which: str = "") -> str:
    """Answer a reaction prompt (Shield, Counterspell, Opportunity Attack...). use=true to react."""
    args: dict[str, Any] = {"use": use}
    if which:
        args["which"] = which
        if use and not str(which).strip().isdigit():
            ui = game.try_rpc("ui.state", context="client") or {}
            i = _reaction_index(ui.get("reaction") if isinstance(ui, dict) else None, str(which))
            if isinstance(i, str):
                return _finish("react", args, "react: FAILED — " + i, time.monotonic())
            args["which"] = i
    return _act("react", "ui.react", args, context="client", wait_s=15)


_ROMAN = re.compile(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX)$")


def _reaction_index(r: dict | None, name: str) -> int | str:
    """react(which="Shield"): the option's number. S034 19:03:14: "no reaction option Shield (3 shown)" — ui.react
    takes a number and the options carried no names (decision=Undecided i=1..3); the prompt text named them
    ("… | 76/81 | Shield | II | Shield | II | III | Shield | III | Do Not React"). Named options first, else the
    reaction names in the prompt text in display order (slot-level numerals and HP left out). str = why not."""
    if not isinstance(r, dict):
        return "no reaction prompt on screen"
    opts = [o for o in r.get("options") or [] if isinstance(o, dict)]
    want = name.strip().lower()
    for o in opts:
        if want and want in str(o.get("name") or "").lower():
            return int(o.get("i") or 1)
    toks = [t.strip() for t in str(r.get("prompt") or "").split("|")]
    if any("choose how to react" in t.lower() for t in toks):
        toks = toks[next(k for k, t in enumerate(toks) if "choose how to react" in t.lower()) + 1:]
    names = [t for t in toks if t and not _ROMAN.match(t) and not re.fullmatch(r"[\d/ ]+", t)
             and t.lower() != "do not react"]
    hits = [k for k, t in enumerate(names) if want and want in t.lower()]
    if hits and (len(names) == len(opts) or len(set(n.lower() for n in names)) == 1):
        return hits[0] + 1
    shown = ", ".join(f"{k + 1}={t}" for k, t in enumerate(names)) or "no names shown"
    return (f"no reaction option {name!r} on this prompt ({len(opts)} options: {shown}); "
            "answer with its number, react(use=true, which=\"1\"), or react(use=false)")


# ---------------------------------------------------------------------------
# act: combat
# ---------------------------------------------------------------------------

@mcp.tool()
@_guard
def use_ability(ability: str, target: str = "", x: float | None = None, y: float | None = None,
                z: float | None = None, who: str | None = None, upcast: int = 0, targets: str = "",
                wait: bool = True, force: bool = False) -> str:
    """Use an action/spell/attack/item from get_actions: at a target id, at a position (x,y,z), or on self. upcast: spell slot level to cast at. targets: comma list for multi-target spells. Costs and rules are the game's. Out of combat at a creature, the result says whether it started a fight (COMBAT: ...) or not (combat: NOT started — a dormant/unaware target). A carried scroll works by name ("Scroll of Invisibility", or the spell's name when it isn't an action): out of combat it is read and its target clicked (default: who). A Devotion paladin's blow that could kill a Temporarily Hostile or neutral character is refused (it would break the oath); force=true strikes anyway."""
    char = _who(who)
    try:
        aid = _ability_id(char, ability)
    except GameError as e:
        if "has no action called" not in str(e):
            raise
        word = str(ability).strip().lower()
        if word in ("disarm", "disarm trap") and target:
            # S031 17:10: use_ability("Disarm") after a disarm the agent doubted → "no action called"; it is the
            # disarm tool (the right-click menu entry), not a hotbar action.
            return disarm(target, who)
        if word in ("lockpick", "pick lock", "lock pick") and target:
            return lockpick(target, who)
        scroll = _carried_scroll(char, ability)
        if scroll is None:
            raise
        return _use_scroll(char, scroll, target)
    spell, note = _own_attack(char, aid)
    tail = ""
    args: dict[str, Any] = {"char": char, "spell": spell}
    if target:
        args["target"] = _uuid(target)
    if targets:
        args["targets"] = [_uuid(t) for t in targets.split(",") if t.strip()]
        keep = 2 - (1 if "target" in args else 0)
        if len(args["targets"]) > keep:
            # S018/S025/S029: Bless on 3 was refused ("more than two targets"), losing the whole action (the
            # game's cast call takes 2 targets). Cast it on the first two and say who was left out.
            left = args["targets"][keep:]
            args["targets"] = args["targets"][:keep]
            tail = (f"targets: cast on the first 2 only (a cast takes at most 2 here); left out: "
                    f"{', '.join(_label(u) for u in left)}")
    if None not in (x, y, z):
        args["pos"] = [x, y, z]
    if upcast:
        args["upcast"] = upcast
    if force:
        args["force"] = True
    opener = (wait and not _batch["active"] and "target" in args and args["target"] not in (_party_cached() or ())
              and not (game.try_rpc("state.combat") or {}).get("active"))
    post = (lambda r: _opener_combat(args["target"], r.result) if r.status == "done" else "") if opener else None
    out = _act("use_ability", "act.cast", args, wait_s=30, wait=wait, post=post)
    if tail and isinstance(out, str):
        out = re.sub(r"(\n⏱[^\n]*)?$", lambda m: f"\n{tail}" + (m.group(0) or ""), out, count=1)
    return f"{note}\n{out}" if note and isinstance(out, str) else out


OPENER_WAIT_S = 1.0  # combat starts a few frames after the hit: how long an opener's result waits for it


def _opener_combat(tu: str, res: Any) -> str:
    """use_ability out of combat at a character: did it start a fight? Acceptance 13:3x: the first Ranged
    Attack on a dormant Shadow did damage but no combat began, and nothing said so. '' for items."""
    ts = game.try_rpc("act.target_state", {"uuid": tu}) or {}
    if not isinstance(ts, dict) or "locked" in ts or "opened" in ts or ts.get("held"):
        return ""
    end = time.monotonic() + OPENER_WAIT_S
    while True:
        cs = game.try_rpc("state.combat") or {}
        if cs.get("active"):
            return ("COMBAT: the attack started a fight" + (f" (round {cs['round']})" if cs.get("round") else "")
                    + ": play the turns with combat_turn (get_combat_state shows the order)")
        if time.monotonic() >= end:
            break
        time.sleep(0.15)
    if ts.get("dead") or (game.try_rpc("act.target_state", {"uuid": tu}) or {}).get("dead"):
        return f"combat: none started ({_label(tu)} died)"
    dmg = res.get("total_damage") if isinstance(res, dict) else None
    if isinstance(dmg, (int, float)) and dmg > 0:
        return (f"combat: NOT started — {_label(tu)} took {render.num(dmg)} damage but no fight began "
                "(dormant or unaware; it acts only once combat starts): attack again or move on")
    return f"combat: not started (no hit on {_label(tu)} seen)"


@mcp.tool()
@_guard
def end_turn(who: str | None = None, wait: bool = True) -> str:
    """End the current character's turn; blocks through enemy turns until a party member can act or combat ends (wait=false: return right after ending it; then wait_for('my_turn'))."""
    t0 = time.monotonic()
    if who:
        char = _who_uuid(who)
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
    cs = game.try_rpc("state.combat") or {}
    if char in turn.downed(cs):
        # S018: end_turn on downed Claude/Shadowheart -> "FAILED: character is downed" (x2).
        body = f"end_turn {_label(char)}: skipped — {_downed_text(char)}"
        if wait and not _batch["active"]:
            body += "\n" + _wait_for_party_turn(80.0)
        return _finish("end_turn", args, body, t0)
    r = game.run_job("act.end_turn", args, wait_s=15)
    _remember_names(r.result)
    _batch["last"] = r.status
    nothing = _nothing_to_end(r)
    if nothing:
        # S023-S025: 24 end_turn calls on a turn combat_turn had already ended (shared turn groups) or that
        # wasn't the character's: the goal (that turn over) already holds, so it isn't a failure. Say whose
        # turn it is instead, and wait like a real end_turn would.
        _batch["last"] = "done"
        body = f"end_turn {_label(char)}: nothing to end — {nothing}"
        if wait:
            body += ("\n" if not _batch["active"] else " — ") + _wait_for_party_turn(80.0)
        return _finish("end_turn", args, body, t0)
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


def _nothing_to_end(r) -> str:
    """Why an end_turn the mod refused needs nothing done ('' = a real failure): the turn already ended, it
    is someone else's turn, or combat is over. Names who acts now."""
    err = str(r.error or "") if r.status != "done" else ""
    if "already ended its turn" in err:
        why = "its turn already ended (combat_turn ends it; the rest of its turn group is still acting)"
    elif "not this character's turn" in err:
        why = "it is not its turn"
    elif err.startswith("not in combat"):
        return "not in combat"
    else:
        return ""
    cs = game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        return why + "; combat is over"
    now = turn.acting(cs)
    party = _party_set()
    if now:
        why += "; now " + ", ".join(_label(u) + (" (yours)" if u in party else "") for u in now)
    return why


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


_NOTE_TIME = re.compile(r"^(\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)\s+")
_NOTE_TAG = re.compile(r"^\[[A-Za-z][\w -]*\]")


def _note_lines(text: str, tag: str = "note") -> list[str]:
    """Session-log lines '- HH:MM [tag] text' from note text: one per non-empty line; a leading '- ', an
    'HH:MM ' (or 'HH:MM-HH:MM ') and a '[tag]' the agent wrote are kept, the rest filled in."""
    tag = re.sub(r"[^\w -]", "", tag or "note").strip() or "note"
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        m = _NOTE_TIME.match(line)
        hhmm = m.group(1) if m else time.strftime("%H:%M")
        if m:
            line = line[m.end():]
        if not _NOTE_TAG.match(line):
            line = f"[{tag}] {line}"
        out.append(f"- {hhmm} {line}")
    return out


def _append_note(text: str, tag: str = "note") -> tuple[int, Path | None]:
    """Append note lines to the session log (append-only, as the memory rules want). -> (lines, path)."""
    lines = _note_lines(text, tag)
    path = _session_log()
    if not lines or path is None or not path.parent.is_dir():
        return 0, path
    with open(path, "a+") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        f.write("\n".join(lines) + "\n")
    return len(lines), path


def _note_quiet(text: str) -> None:
    """note= on an action tool: written before the action starts; a failure never blocks the action."""
    if text and text.strip():
        try:
            _append_note(text)
        except OSError as e:
            _log("note", {}, f"note not saved: {e}", 0)


@mcp.tool()
@_guard
def note(text: str, tag: str = "note") -> str:
    """Append line(s) to your session log (memory/sessions/Sxxx.md) at once: no file Read/Edit round trip, so a note never holds up play. One log line per line of text, '- HH:MM [tag] text'; start a line with its own [tag] (quest, decision, npc, combat, loot, puzzle, lesson, stuck, ...) to override tag. Curated files (objectives, npcs, quests, ...) are updated from the log between sessions; Edit them yourself only for plan changes you need this session. travel_to, queue_actions and do_actions take note= too (written with the action, no extra turn)."""
    t0 = time.monotonic()
    n, path = _append_note(text, tag)
    if not n:
        body = "Nothing noted (empty text)." if path is not None else "NOT SAVED: no session log."
    else:
        body = f"Noted {n} line{'s' if n != 1 else ''} in {path.name}."
    return _finish("note", {}, body, t0)


# ---------------------------------------------------------------------------
# act: items, rest, progression
# ---------------------------------------------------------------------------


def _item_uuid(item: str, who: str | None = None, worn: bool = False) -> str:
    """_uuid, except that a name shared by identical party items (same name, rarity, slot, stats) picks one
    instead of failing 'ambiguous' (S027: equip "Simple Robe" → ambiguous e14,e148,e165,e169, batch stopped).
    Prefers the item in `who`'s bag, unworn (worn=True for unequip: a worn one)."""
    try:
        return _uuid(item)
    except ValueError as e:
        if "ambiguous" not in str(e):
            raise
        inv = game.try_rpc("state.inventory", {}) or {}
        _remember_names(inv)
        rows = [it for it in inv.get("items") or [] if isinstance(it, dict)
                and str(it.get("name") or "").strip().lower() == str(item).strip().lower()]
        key = lambda it: (it.get("rarity"), it.get("slot_type"), it.get("ac"), str(it.get("dmg")))  # noqa: E731
        if len(rows) < 2 or len({key(it) for it in rows}) != 1:
            raise
        owner = _who(who)
        rows.sort(key=lambda it: (bool(it.get("equipped_slot")) != worn, it.get("owner") != owner))
        return rows[0]["uuid"]


@mcp.tool()
@_guard
def feed_gale(item: str) -> str:
    """Give Gale a magic item to absorb, in the donation window his dialogue opens ('Give Gale a magic artefact'): selects the item and presses Donate. item: name or handle from get_inventory (one in a party bag)."""
    return _act("feed_gale", "ui.donate", {"item": _uuid(H.get(_item_uuid(item)))}, context="client", wait_s=15)


def _insert_job(args: dict, wait_s: float = 15) -> JobResult:
    """ui.insert as a JobResult; its refusals (no panel open) come back as failed, not raised."""
    try:
        return game.run_job("ui.insert", args, context="client", wait_s=wait_s)
    except Exception as e:  # noqa: BLE001
        return JobResult("failed", error=str(e))


def _insert_listing(res: dict) -> str:
    slots = ", ".join(x if x else "empty" for x in res.get("slots") or []) or "?"
    rows = []
    for it in res.get("items") or []:
        u = it.get("uuid")
        h = H.get(u, it.get("name")) if isinstance(u, str) and u else "?"
        rows.append(f"{h} {it.get('name') or '?'}" + (" (in the slot)" if it.get("selected") else ""))
    return (f"INSERT panel {res.get('panel') or '?'}: slot(s) {slots}; state {res.get('state') or '?'}\n"
            + ("items it can take: " + "; ".join(rows[:20]) + (f" (+{len(rows) - 20} more)" if len(rows) > 20 else "")
               if rows else "no items listed (the panel shows the selected character's bag)"))


def _opened_dialogue(out: str) -> bool:
    return "\nDIALOGUE " in "\n" + (out or "") or "dialogue_started" in (out or "")


def _insert_poll(out: str, wait_s: float = 4.0) -> JobResult:
    """The panel shows a moment after the interact; a conversation instead of it ends the wait at once."""
    deadline = time.monotonic() + wait_s
    while True:
        r = _insert_job({})
        if (r.status != "failed" or "no Insert Item panel" not in str(r.error) or _opened_dialogue(out)
                or time.monotonic() > deadline):
            return r
        time.sleep(0.25)


def _colocated_usables(target: str, within_m: float = 1.5) -> list[tuple[str, str]]:
    """Other usable objects in sight standing where the target stands (uuid, name), nearest first."""
    near = game.try_rpc("state.nearby", {"radius": 20, "kinds": ["usable", "container"]}) or {}
    _remember_names(near)
    rows = [it for it in near.get("items") or [] if isinstance(it, dict) and isinstance(it.get("pos"), list)]
    me = next((it for it in rows if it.get("uuid") == target), None)
    if not me:
        return []
    def d(it: dict) -> float:
        return sum((float(a) - float(b)) ** 2 for a, b in zip(it["pos"][:3], me["pos"][:3])) ** 0.5
    return [(it["uuid"], str(it.get("name") or "?")) for it in sorted(rows, key=d)
            if it is not me and d(it) <= within_m]


@mcp.tool()
@_guard
def insert_item(item: str | None = None, into: str | None = None, who: str | None = None) -> str:
    """Put an item into an "Insert Item" panel (pedestal, plaque, socket: what a player drags into the slot) and press Insert. into: the object (opens its panel with interact when none is open; walks there). item: name or handle; without item it opens the panel and lists the slot and the items it can take. who: who opens/inserts (default: the item's carrier, else the active character). Some story pedestals punish a wrong item: choose by the riddle/notes."""
    t0 = time.monotonic()
    args = {"item": item, "into": into, "who": who}
    u_item = _uuid(H.get(_item_uuid(item))) if item else None
    char = _who_uuid(who) if who else None
    if char is None and u_item:
        inv = game.try_rpc("state.inventory", {}) or {}
        _remember_names(inv)
        row = next((it for it in inv.get("items") or [] if isinstance(it, dict) and it.get("uuid") == u_item), None)
        char = row.get("owner") if row else None
    char = char or _active()
    note = ""
    r = _insert_job({})
    if r.status == "failed" and "no Insert Item panel" in str(r.error):
        if not into:
            return _finish("insert_item", args, "insert_item: FAILED — no Insert Item panel is open: pass into=<the pedestal/socket> (interact opens it)", t0)
        if char != (game.try_rpc("state.status") or {}).get("active"):
            # S037 20:15-20:17: who=Shadowheart while Claude was controlled: 3 interacts "used … success", no panel;
            # selecting her and clicking opened it. The client shows the panel only to the controlled character
            # (as pickpocket's window), so take control first, as a player does.
            if game.run_job("act.select", {"char": char}, wait_s=5).status == "done":
                note = f"took control of {_name_of(char)} (the panel shows for the controlled character)"
        out = _act("interact", "act.interact", {"char": char, "target": _uuid(into)}, wait_s=40)
        walked = _walk_first("interact", out, char, who, into, actq.Step("interact", {"target": into}))
        if walked:  # far / out of sight: the walk runs, the interact is queued for the arrival
            return _finish("insert_item", args, walked + "\ncall insert_item again when the panel is open", t0)
        r = _insert_poll(out)
        others = _colocated_usables(_uuid(into)) if r.status == "failed" and "no Insert Item panel" in str(r.error) else []
        if others and not _opened_dialogue(out):
            # S037 20:18-20:20: the Pedestal of Reckoning only talks; the gem slot is the Ancient Altar at the same
            # spot (a hand click on it opened the panel). Try what stands there, as a player clicks the other part.
            h, nm = others[0]
            out2 = _act("interact", "act.interact", {"char": char, "target": h}, wait_s=40)
            r = _insert_poll(out2)
            if r.status == "done":
                into = H.get(h)
                note = "; ".join(x for x in (note, f"the slot is on {H.get(h)} {nm} (at the same spot)") if x)
            else:
                out = out2 if _opened_dialogue(out2) else out
        if r.status == "failed" and "no Insert Item panel" not in str(r.error):
            return _finish("insert_item", args, f"insert_item: FAILED — {r.error}", t0)
        if r.status == "failed":
            alt = "; ".join(f"{H.get(h)} {nm}" for h, nm in others)
            if _opened_dialogue(out):
                body = (f"insert_item: FAILED — no Insert Item panel: using it opened a conversation instead\n{out}"
                        + (f"\nthe slot may be on what stands at the same spot ({alt}): end the conversation, then "
                           f"insert_item(into={H.get(others[0][0])})" if others else ""))
            else:
                body = (f"insert_item: FAILED — no Insert Item panel opened ({out.splitlines()[0] if out else 'interact'})"
                        + (f"; tried {H.get(others[0][0])} {others[0][1]} at the same spot too" if others else "")
                        + "; not every pedestal takes items: look at it (get_nearby / screenshot)")
            return _finish("insert_item", args, body, t0)
        note = "; ".join(x for x in (note, f"opened the panel by {_name_of(char)}") if x)
    elif r.status == "failed":
        return _finish("insert_item", args, f"insert_item: FAILED — {r.error}", t0)
    listing = dict(r.result) if isinstance(r.result, dict) else {}
    if not listing.get("panel") and into:  # the panel's own VM carries no name (live 17:5x)
        listing["panel"] = _name_of(_uuid(into))
    if not u_item:
        return _finish("insert_item", args, "insert_item: done — " + (note + "\n" if note else "") + _insert_listing(listing)
                       + "\ninsert_item(item=<handle>) puts one in and presses Insert", t0)
    r = _insert_job({"item": u_item})
    if r.status != "done":
        body = f"insert_item: FAILED — {r.error or r.status}\n" + _insert_listing(listing)
        return _finish("insert_item", args, body, t0)
    res = r.result or {}
    name = res.get("item") or res.get("inserted") or res.get("placed") or _name_of(u_item)
    if res.get("placed") and not res.get("inserted"):
        # S037 20:23: the Verge altar's 3 gem slots; the first two gems read FAILED though each sat in its slot.
        n, k = res.get("slots_total") or 0, res.get("filled") or 0
        body = (f"insert_item: done — put {name} in the slot ({k} of {n} filled; state {res.get('state')}): the panel "
                f"waits for {n - k} more item(s): insert_item(item=<next>) fills the next slot, Insert presses itself")
    elif res.get("state") == "Fail":
        why = res.get("fail_reason") or "no reason given"
        body = f"insert_item: FAILED — the panel refused {name} ({why}); it is still open"
        if why == "Interrupted":
            # Live 2026-10-10 17:5x (S031-pre-riddle): the game answers Interrupted to a real mouse drag + Insert click
            # too, the same game-side block as long_rest's missing ready check. Retrying or hand clicks won't help.
            body += ("\nthe game interrupted the insert (a real drag and click get the same answer now): don't retry "
                     "by hand; note it and play on elsewhere")
    else:
        # S037 20:17/20:20: "the panel is still open: ESCAPE closes it" after a story insert that closed it (the
        # pedestal vanished) → the ESCAPE was REFUSED. Look once before saying so.
        closed = res.get("closed")
        if not closed:
            time.sleep(0.3)
            again = _insert_job({}, wait_s=3)
            closed = again.status == "failed" and "no Insert Item panel" in str(again.error)
        body = f"insert_item: done — inserted {name} into {listing.get('panel') or 'the panel'}" + (
            " (panel closed)" if closed else f" (state {res.get('state')}; the panel is still open: ESCAPE closes it)")
    return _finish("insert_item", args, body + ("\n" + note if note else ""), t0)


@mcp.tool()
@_guard
def manage_inventory(op: str, item: str, who: str | None = None, to: str | None = None) -> str:
    """op: equip | unequip | use (drink/read/eat) | give (to another party member: to=name) | camp (send to camp chest; item="junk" sends all junk at once: what trade sell=junk would sell)."""
    o = op.lower()
    if o in ("equip", "unequip", "use", "give") or (o == "camp" and item.strip().lower() != "junk"):
        item = H.get(_item_uuid(item, who if o != "give" else None, worn=o == "unequip"))
    if o == "equip":
        return _act("manage_inventory", "act.equip", {"char": _who(who), "item": _uuid(item)})
    if o == "unequip":
        return _act("manage_inventory", "act.unequip", {"char": _who(who), "item": _uuid(item)})
    if o == "use":
        # Osiris Use on a carried item. In combat only a potion, on the drinker's turn: the mod checks and
        # charges the item's own cost (a bonus action), as the hotbar would.
        return _act("manage_inventory", "act.interact", {"char": _who(who), "target": _uuid(item)})
    if o == "give":
        targs = {"item": _uuid(item), "to": _uuid(to) if to else None}
        out = _act("manage_inventory", "act.transfer", targs)
        if "unequip it first" in out.split("\n", 1)[0]:
            # S028 15:51 (Spellcrux Amulet, Karlach → Gale): a player drags a worn item onto another portrait;
            # here the refusal cost a 3-step batch (unequip, give, equip). Take it off its wearer, then give.
            row = next((it for it in (game.try_rpc("state.inventory", {}) or {}).get("items") or []
                        if isinstance(it, dict) and it.get("uuid") == targs["item"] and it.get("equipped_slot")), None)
            if row and row.get("owner"):
                r = game.run_job("act.unequip", {"char": row["owner"], "item": targs["item"]}, wait_s=10)
                if r.status != "done":
                    return out + f"\n(unequipping it from {_name_of(row['owner'])} failed: {_job_text('unequip', r)})"
                out = _act("manage_inventory", "act.transfer", targs)
                head, _, rest = out.partition("\n")
                out = head + f" (unequipped from {_name_of(row['owner'])} first)" + ("\n" + rest if rest else "")
        return out
    if o == "camp":
        if item.strip().lower() == "junk":
            return _stash_junk(who)
        return _act("manage_inventory", "act.transfer", {"item": _uuid(item), "to": "camp"})
    raise ValueError("op must be equip|unequip|use|give|camp")


def _keep_set(keep: str) -> set[str]:
    return {_uuid(k.strip()) for k in (keep or "").split(",") if k.strip()}


def _stash_junk(who: str | None, keep: str = "") -> str:
    """manage_inventory(op="camp", item="junk"): send every junk item (economy.is_junk) to the camp chest, one
    engine transfer each (S023 did this by hand: inventory screen, right-click, 6 screenshots)."""
    t0 = time.monotonic()
    inv = game.rpc("state.inventory", {"uuid": _who_uuid(who)} if who else {})
    _remember_names(inv)
    items = economy.junk(inv, _keep_set(keep))
    sent, failed = [], []
    for it in items:
        r = game.run_job("act.transfer", {"item": it["uuid"], "to": "camp"}, wait_s=10)
        (sent if r.status == "done" else failed).append(f"{H.get(it['uuid'])} {it.get('name')}"
                                                        + ("" if r.status == "done" else f" ({r.error})"))
        if r.status != "done" and len(failed) >= 3:
            break  # the same refusal (combat, camp unavailable) would repeat for every item
    kg = sum((i.get("weight") or 0) * (i.get("count") or 1) for i in items if f"{H.get(i['uuid'])} {i.get('name')}" in sent)
    body = f"manage_inventory: {'done' if sent and not failed else 'FAILED' if not sent else 'partly done'} — sent {len(sent)} junk items to camp ({kg:.1f} kg)"
    if not items:
        body = "manage_inventory: done — no junk to send (equipped, story, consumables, food, keys and magic gear are kept)"
    if sent:
        body += ": " + ", ".join(sent[:20]) + (" …" if len(sent) > 20 else "")
    if failed:
        body += "\nFAILED: " + "; ".join(failed)
    return _finish("manage_inventory", {"op": "camp", "item": "junk"}, body, t0)


@mcp.tool()
@_guard
def equip_best(who: str | None = None) -> str:
    """Equip the better gear sitting in party bags (out of combat): for each member (or just who), every item that beats what they wear in that slot (armour/shield AC, weapon damage/enchantment, else rarity) and that they are proficient with; moved from another member's bag first. get_inventory(filter="gear") lists the same without equipping."""
    t0 = time.monotonic()
    if (game.try_rpc("state.status") or {}).get("in_combat"):
        # S034 19:15: called as a Displacer Beast ambush began; every give failed "… (use the UI)", which invites a
        # hand click. In combat a swap costs an action and gives are refused: say so, do nothing.
        return _finish("equip_best", {"who": who}, "equip_best: NOT NOW — in combat (gear swaps cost actions and "
                       "items can't change hands mid-fight). Call it again once the fight ends; nothing was moved.", t0)
    inv = game.rpc("state.inventory", {})
    _remember_names(inv)
    done, failed = [], []
    lp = economy.lantern_plan(inv)
    if lp and (not who or _who_uuid(who) in (lp["frm"], lp["to"])):
        # The lantern's aura works from any holder: the caster carries the light, the fighter gets a weapon
        # (S025-S027 Claude fought with it). Unequip → give → equip: the gap is ~1 s.
        tag = f"{_name_of(lp['to'])}: {H.get(lp['lantern'])} {lp['name']} (light for the party; was {_name_of(lp['frm'])}'s main hand)"
        for op, args in (("act.unequip", {"char": lp["frm"], "item": lp["lantern"]}),
                         ("act.transfer", {"item": lp["lantern"], "to": lp["to"]}),
                         ("act.equip", {"char": lp["to"], "item": lp["lantern"]})):
            r = game.run_job(op, args, wait_s=10)
            if r.status != "done":
                failed.append(f"{tag}: {op.split('.')[1]} failed: {r.error}")
                if op == "act.equip":  # never leave it in a bag: the aura needs it held
                    game.run_job("act.transfer", {"item": lp["lantern"], "to": lp["frm"]}, wait_s=10)
                if op != "act.unequip":
                    game.run_job("act.equip", {"char": lp["frm"], "item": lp["lantern"]}, wait_s=10)
                break
        else:
            done.append(tag)
            inv = game.rpc("state.inventory", {})
            _remember_names(inv)
    ups = economy.upgrades(inv)
    if who:
        ups = [u for u in ups if u["who"] == _who_uuid(who)]
    if not ups and not done and not failed:
        hands = economy.main_hands(inv, _name_of)
        return _finish("equip_best", {"who": who}, "equip_best: done — nothing better in the bags"
                       + (f"\n{hands}" if hands else ""), t0)
    for u in ups:
        tag = f"{_name_of(u['who'])}: {H.get(u['uuid'])} {u['name']} ({u['why']})"
        if u["holder"] != u["who"]:
            r = game.run_job("act.transfer", {"item": u["uuid"], "to": u["who"]}, wait_s=10)
            if r.status != "done":
                failed.append(f"{tag}: give failed: {r.error}")
                continue
        r = game.run_job("act.equip", {"char": u["who"], "item": u["uuid"]}, wait_s=10)
        (done if r.status == "done" else failed).append(tag if r.status == "done" else f"{tag}: {r.error}")
    body = f"equip_best: {'done' if done and not failed else 'FAILED' if not done else 'partly done'}"
    if done:
        body += " — equipped " + "; ".join(done)
    if failed:
        body += "\nFAILED: " + "; ".join(failed)
    hands = economy.main_hands(game.try_rpc("state.inventory", {}) or inv, _name_of)   # after the swaps
    if hands:
        body += "\n" + hands
    return _finish("equip_best", {"who": who}, body, t0)


@mcp.tool()
@_guard
def short_rest() -> str:
    """Short rest (2 per long rest): restores some HP and short-rest resources. The result shows HP before→after per member and the short rests left."""
    before = game.try_rpc("state.status")
    out = _box_act("short_rest", "ui.short_rest", {}, wait_s=20)
    res = _batch.get("result")
    if isinstance(res, dict) and isinstance(res.get("short_rests_left"), int):
        ALERTS.short_rests = res["short_rests_left"]
    if out.startswith("short_rest: done") or "short_rest: done" in out:
        out += "\n" + economy.short_rest_text(before, game.try_rpc("state.status"), res)
    return out


@mcp.tool()
@_guard
def long_rest(confirm: bool = True, partial: bool = False) -> str:
    """Long rest at camp (go there with camp first): consumes 40 camp supplies, restores everything, may trigger camp scenes (handle them as dialogues). The game's questions on the way ("End the day?", "someone still wants to talk, rest anyway?") are answered Yes and listed in the result; confirm=false stops at the first one instead (then confirm(...)). With fewer than 40 supplies it stops before resting (a partial rest restores only half HP and slots): get food first, or partial=true to rest anyway."""
    args: dict[str, Any] = {} if confirm else {"confirm": False}
    if partial:
        args["partial"] = True
    refused = _rest_blocked()
    if refused:
        return refused
    # No blind re-press after 4 s (S030: requested → gathered → requested, then the engine said no ~10 s after the
    # first press: the re-press fell inside its answer window). The server's rest flow says whether the press
    # reached the game; only a press that didn't is repeated.
    args["repress"] = False
    t0 = time.monotonic()

    def since_ms() -> int:
        return int((time.monotonic() - t0) * 1000) + 1500

    out = _box_act("long_rest", "ui.long_rest", args, wait_s=90)
    ALERTS.rested(_batch.get("result"))
    if "rest screen didn't open" in out:
        flow = _rest_flow_answer(since=since_ms)
        if isinstance(flow, dict) and not flow.get("steps"):
            # the press never reached the story (S018: a press swallowed by a closing screen): press once more
            out = _box_act("long_rest", "ui.long_rest", args, wait_s=90)
            ALERTS.rested(_batch.get("result"))
            if "rest screen didn't open" not in out:
                return out
            flow = _rest_flow_answer(since=since_ms)
        if "end_day_ok" in (flow or {}).get("steps", []):
            # the game said yes after the job's 4 s window: the rest screen is coming, carry on in this call
            # without pressing again (a press at night is the story's "leave night mode")
            out = _box_act("long_rest", "ui.long_rest", {**args, "no_press": True}, wait_s=90)
            ALERTS.rested(_batch.get("result"))
            if "rest screen didn't open" not in out:
                return out
            flow = game.try_rpc("camp.rest_flow", {"since_ms": since_ms()})
        out += "\n" + _rest_flow_hint(flow)
    return out


def _rest_flow_answer(wait_s: float = 8.0, since: Callable[[], int] = lambda: 20000) -> dict | None:
    """S030: the engine answered the End-the-Day request ~10 s after the press, after ui.long_rest gave up.
    When the party gathered and no answer came yet, wait for it (instead of a second long_rest call)."""
    t_end = time.monotonic() + wait_s
    while True:
        flow = game.try_rpc("camp.rest_flow", {"since_ms": since()})
        steps = (flow.get("steps") or []) if isinstance(flow, dict) else []
        last_gather = max((i for i, x in enumerate(steps) if x == "gathered"), default=-1)
        answered = last_gather < 0 or any(_rest_step(x) not in _REST_FLOW_PENDING for x in steps[last_gather:])
        if answered or time.monotonic() >= t_end:
            return flow if isinstance(flow, dict) else None
        time.sleep(0.5)


# The engine's long-rest validation per party member (CanDoRest.LongRestErrorFlags, Events.lua camp.can_rest):
# the HUD greys the Long Rest button and its tooltip says why, so a player sees these reasons. S028-S030: 10
# "rest screen didn't open" failures, 8 s each, with screenshots, HUD clicks and a save reload to work around.
_REST_FLAG_TEXT = {
    "Combat": "in combat",
    "Dialog": "in a conversation (finish it first)",
    "FTB": "turn-based mode is on (set_party(action=\"turn_based_off\"))",
    "Script": "a story event blocks resting right now",
    "GlobalDisabled": "resting is disabled here",
    "DropInCCActive": "a character editor is open",
    "DownedOrDead": "downed or dead",
    "NotEnoughResources": "not enough camp supplies",
    "Location": "not at a place where you can rest",
}
# Refused before pressing anything (sure blockers); the others only explain a failure.
_REST_HARD_FLAGS = ("Combat", "Dialog", "FTB", "Script", "GlobalDisabled", "DropInCCActive")


def _rest_reasons(can: Any, flags: tuple[str, ...] | None = None) -> str:
    """'Karlach: turn-based mode is on (...); Gale: in combat' from camp.can_rest, limited to `flags`."""
    if not isinstance(can, dict):
        return ""
    parts = []
    for row in can.get("blocked") or []:
        fl = [str(f) for f in row.get("flags") or [] if flags is None or str(f) in flags]
        if fl:
            parts.append(f"{row.get('name') or '?'}: " + ", ".join(_REST_FLAG_TEXT.get(f, f) for f in fl))
    if (can.get("global_disabled") or 0) > 0 and (flags is None or "GlobalDisabled" in flags):
        parts.append("resting is disabled for now (story)")
    return "; ".join(parts)


def _rest_blocked() -> str:
    can = game.try_rpc("camp.can_rest")
    METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can})
    if not _rest_reasons(can, _REST_HARD_FLAGS):
        return ""
    time.sleep(0.5)  # a flag set for a moment (a teleport into camp, a closing conversation) must not refuse a rest
    can = game.try_rpc("camp.can_rest")
    why = _rest_reasons(can, _REST_HARD_FLAGS)
    if not why:
        return ""
    METRICS.write({"type": "can_rest", "t": round(time.time(), 3), "can": can, "refused": True})
    return f"long_rest: FAILED — the game doesn't allow a long rest now (the Long Rest button is greyed out): {why}"


# S028 15:52-15:59 x5 + S029 16:04 x2: "rest screen didn't open" with nothing on screen, no box; screenshots and HUD
# clicks after. The camp's End-the-Day handshake (GLO_Camp.txt) runs on the server: the mod pushes each step
# (Events.lua rest_flow) and keeps the last ones; say where the request died.
_REST_FLOW_TEXT = {
    "": "the game never registered the rest request (the button press did not reach it)",
    "requested": "the game took the rest request but never answered it (a stuck camp state: a conversation, a fight "
                 "or loading a save clears it; Larian bug GUS-150045)",
    "gather_failed": "the game refused to gather the party at camp (someone can't come: busy, in a conversation or "
                     "far away)",
    "gathered": "the party gathered but the game didn't go on to end the day (S030: it answered no ~10 s later)",
    "end_day_failed": "the game refused to end the day after gathering the party (its own rest check; reloading a "
                      "save didn't help in S030)",
    "end_day_ok": "the game started the night: the rest screen should follow (call long_rest again)",
    "rest_start_failed": "the game could not start the rest",
    "rest_cancelled": "the rest was cancelled",
    "rest_denied": "the game denied the rest",
    "ready_failed": "the game's ready check (the \"End the day?\" question) failed",
}
# Steps that are not the game's final answer to the request (a ready check runs between gathered and the answer).
_REST_FLOW_PENDING = ("requested", "gathered", "ready_ok", "rest_waiting")


def _rest_step(step: str) -> str:
    return step.split(":", 1)[0]


def _rest_flow_hint(flow: Any = None) -> str:
    if flow is None:
        flow = game.try_rpc("camp.rest_flow", {"since_ms": 20000})
    if not isinstance(flow, dict):
        return ""
    steps = [str(x) for x in flow.get("steps") or []]
    METRICS.write({"type": "rest_flow", "t": round(time.time(), 3), "steps": steps, "step_ms": flow.get("step_ms"),
                   "story": flow.get("story"), "can_rest": flow.get("can_rest")})
    # The furthest step says most: a re-press after "gathered" is swallowed by the story (DB_GoingToCamp), so the
    # last step alone misled S030 into a save reload ("requested: a stuck camp state ... loading a save clears it").
    answers = [x for x in steps if _rest_step(x) not in _REST_FLOW_PENDING]
    last = answers[-1] if answers else "gathered" if "gathered" in steps else steps[-1] if steps else ""
    key = _rest_step(last)
    text = _REST_FLOW_TEXT.get(key, key) + (f" ({last.split(':', 1)[1]})" if ":" in last else "")
    if key == "gathered" and not any(_rest_step(x) in ("ready_ok", "ready_failed") for x in steps):
        text += "; no ready-check result either (the \"End the day?\" question never showed or was never answered)"
    out = f"REST FLOW {' → '.join(steps) or 'nothing'}: {text}"
    why = _rest_reasons(flow.get("can_rest"))
    return out + (f"\nthe game's rest check: {why}" if why else "")


@mcp.tool()
@_guard
def get_level_up_options(who: str | None = None) -> str:
    """What the level-up screen offers for a character (class/subclass, feats or ability points, spells, skills)."""
    t0 = time.monotonic()
    res = game.rpc("ui.level_up_options", {"uuid": _who(who)}, context="client")
    return _finish("get_level_up_options", {"who": who}, render.compact(res, H), t0)


@mcp.tool()
@_guard
def level_up(who: str | None = None, plan: str = "", out: str = "") -> str:
    """Level a character up in one call (out of combat). With no plan it uses your stored line for that character and level in memory/party-build.md ("- Gale L2: {...}"). plan: JSON like {"subclass":"Evocation","feat":"Ability Improvement","abilities":{"Strength":2},"spells":["Magic Missile","Shield"],"cantrips":[],"skills":[],"passives":["Agonizing Blast"]}; omitted fields take the game's defaults, except choices with no default: a subclass, spells/cantrips to learn, and passives (invocations, fighting styles). Then it fails and lists what can be picked; add them to the plan and call again.
    A companion waiting at camp is levelled too (every level it has waiting): it swaps in for `out` (default: the member it replaces best), levels, and swaps back."""
    st = game.try_rpc("state.status") or {}
    _remember_names(st)  # camp members' names (live 12:3x: the first call of a fresh bridge, level_up(who="Wyll"),
    uuid = _who(who)     # failed "unknown target 'Wyll'": only party names were known before a status read)
    camp = next((c for c in st.get("camp") or [] if isinstance(c, dict) and c.get("uuid") == uuid), None)
    if camp is not None:
        # S026: Wyll (camp, L4 pending since S021) → "character is not in the party portraits"; the advice to
        # swap by hand was never followed (S026-S027): do the swap, level, swap back in this call.
        return _level_camp_member(camp, plan, out, st)
    with _LEVEL_LOCK:  # one level-up page at a time: parallel calls (S010 x3, S024 x2) shared one UI
        return _level_up(uuid, plan)


def _level_camp_member(row: dict, plan: str, out: str, st: dict) -> str:
    name = str(row.get("name") or "?")
    if not row.get("level_up"):
        return f"level_up: nothing to do — {name} (camp) has no level up waiting"
    if st.get("in_combat") or st.get("in_dialogue"):
        return f"level_up: REFUSED — {name} is in camp: swapping them in needs no combat/conversation"
    party = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    if out:
        outr = companions.find_row(party, out, _uuid)
    elif len(party) >= 4:
        live = [r for r in party if not r.get("dead")]
        outr = companions.swap_out(live, [r for r in live if not r.get("avatar")], row)
    else:
        outr = None
    outname = str((outr or {}).get("name") or "")
    if len(party) >= 4 and not outname:
        return f"level_up: FAILED — {name} is in camp and nobody can make room: level_up(who=\"{name}\", out=<companion>)"
    was_at_camp = _at_camp(game.try_rpc("ui.state", context="client"))
    steps = []
    sw = swap_party(into=name, out=outname, leave_camp=False)
    if "swap_party: done" not in sw:
        return f"level_up: FAILED — {name} is in camp and the swap-in did not finish:\n{sw}"
    steps.append(f"swapped in {name}" + (f" for {outname}" if outname else ""))
    results, first = [], True
    for _ in range(6):  # every level waiting (a camp member may be 2 behind)
        row2 = next((r for r in leveling.pending(game.try_rpc("state.status")) if r["uuid"] == row.get("uuid")), None)
        if row2 is None:
            break
        with _LEVEL_LOCK:
            r = _level_up(str(row["uuid"]), plan if first else "")
        first = False
        results.append(f"L{row2['to']}: {r}")
        if not r.startswith("level_up: done"):
            break
    ok = bool(results) and all(": level_up: done" in r for r in results)
    if not ok and results and "plan." in results[-1]:
        # Live 11:14: Wyll L4 needs a cantrip + a spell the stored plan lacked. Swapping back now would make the
        # retry swap in again (~30 s): keep him in with the page up; the retry is the normal one-member call.
        keep = (f"{name} stays in the party" + (f" ({outname} at camp)" if outname else "") +
                f" with the level-up page open: call level_up(who=\"{name}\", plan=...) with the picks below"
                + (f", then swap_party(into=\"{outname}\", out=\"{name}\") to bring {outname} back" if outname else ""))
        ndone = sum(1 for r in results if ": level_up: done" in r)   # live 12:2x: L4 done, L5 lacked picks → said FAILED
        head = f"level_up: partly done ({ndone} of {len(results)} levels)" if ndone else "level_up: FAILED"
        return f"{head} — {name} (camp member): " + "; ".join(steps + [keep]) + "\n" + "\n".join(results)
    back = swap_party(into=outname, out=name, leave_camp=False) if outname else ""
    if outname:
        steps.append(f"swapped back ({outname} in, {name} at camp)" if "swap_party: done" in back
                     else f"swap back FAILED: {back}")
    if not was_at_camp:
        rj = game.run_job("act.leave_camp", {}, wait_s=60)
        steps.append("left camp" if rj.status == "done" else "leave camp: " + _job_text("camp", rj))
    # Live 12:4x: Wyll L4 done (spells clicked, leveled_up), then L5 failed on the closing L4 page: the headline
    # said FAILED as if nothing had happened. A level that went through is said first.
    ndone = sum(1 for r in results if ": level_up: done" in r)
    head = "level_up: done" if ok else (f"level_up: partly done ({ndone} of {len(results)} levels)" if ndone else "level_up: FAILED")
    return f"{head} — {name} (camp member): " + "; ".join(steps) + "\n" + "\n".join(results or ["no level up waiting after the swap"])


_LEVEL_LOCK = threading.Lock()


def _level_up(uuid: str, plan: str) -> str:
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
    if out.startswith("level_up: FAILED") and "portrait apart" in out:
        # Two members with the same HP, max HP and level (S042 23:11 Shadowheart ×2): the client finds the selected
        # member's portrait exactly, so select it (as the error asks), retry, then give the control back.
        prev = None
        try:
            prev = _active()
            game.run_job("act.select", {"char": uuid}, wait_s=5)
            time.sleep(0.2)  # the client's ClientControl follows the selection a frame or two later
        except GameError:
            pass
        out = _act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
        if prev and prev != uuid:
            try:
                game.run_job("act.select", {"char": prev}, wait_s=5)
            except GameError:
                pass
    if out.startswith("level_up: FAILED") and "choices: #1" in out:
        # The page had just opened and its list labels were not rendered yet (S009: "subclass: no 'Evocation';
        # choices: #1, #2, ..." twice, the same plan right after went through): once more with the page open.
        out = _act("level_up", "ui.level_up", args, context="client", wait_s=60, note=note)
    res = _batch.get("result")
    if out.startswith("level_up: FAILED") and isinstance(res, dict) and res.get("needs_clicks") and _inp() is not None:
        # Spell picks are mouse clicks (leveling.click_spells); then the same plan again finishes the page.
        clicks = leveling.click_spells(lambda op, a: game.rpc(op, a, context="client"), _inp(), res["needs_clicks"])
        note = (note + " " if note else "") + f"(clicked spells: {', '.join(clicks['picked']) or 'none'}" + (
            f"; failed: {clicks['failed']})" if clicks["failed"] else ")")
        # keep_page: the picks live on this page; S032 the rerun closed the (frozen, Started) page and lost them.
        out = _act("level_up", "ui.level_up", dict(args, keep_page=bool(clicks["picked"])), context="client",
                   wait_s=60, note=note)
    if out.startswith("level_up: done"):
        _LEVEL_NUDGE.done(uuid)
    return out


@mcp.tool()
@_guard
def trade(npc: str = "", offer: str = "", request: str = "", confirm: bool = False, sell: str = "",
          keep: str = "", full: bool = False, donate: bool = False, count: int = 0) -> str:
    """Barter with a trader. Open with npc=<id> (talks to them and picks the conversation's trade answer, e.g. 'Let me see your wares.'; if none is offered yet it stops at the conversation), then list items via get_inventory; offer/request: comma lists of item ids to move; confirm=true to finish the deal. price is for the whole stack (price=156 count=3: 52 each; a stack moves whole). Wares marked supply=N are food (camp supplies per item; a long rest needs 40). A stack's "how many?" box is answered for you: the whole stack, or count=N.
    sell="junk": sell everything a player sells at every trader in one call (not equipped, story, consumables, food, keys/tools, magic gear, or keep=<comma ids>), up to the trader's gold; get_inventory(filter="junk") previews it.
    Opening or trade() with no args lists the wares grouped (food with supply, potions/scrolls, gear by rarity) and your best-priced items, capped per group; full=true lists everything. offer/request/confirm calls show the table and the balance only.
    donate=true gives the offered items for nothing (answers the game's "Donate these items?"); automatic on a quest hand-over screen (a dialogue answer like "take them" opens a trade screen whose other side has no wares and no gold): trade(offer=<id>, confirm=true) gives it and closes the screen."""
    if sell:
        if sell.strip().lower() != "junk":
            raise ValueError('sell must be "junk" (or use offer=<ids> for specific items)')
        if npc:  # S028 15:51: trade(npc, sell="junk") failed "no trade screen is open": open it first
            opened = _open_trade(npc)
            if opened.startswith("trade: FAILED") or opened.startswith("FAILED") or "\nSTOPPED" in opened:
                return opened
            out = _sell_junk(keep)
            return (opened + "\n" + out) if opened else out
        scr = (game.try_rpc("ui.state", context="client") or {}).get("screen")
        if scr == "dialogue":  # a trader's conversation is up: pick its trade answer, as trade(npc) does
            opened = _open_trade("")
            if opened.startswith("trade: FAILED") or "\nSTOPPED" in opened:
                return opened
            out = _sell_junk(keep)
            return (opened + "\n" + out) if opened else out
        if scr not in (None, "trade"):
            return ('trade: REFUSED — sell="junk" needs a trader: trade(npc=<trader id>, sell="junk") talks to them, '
                    "opens the trade and sells in one call (no trade screen is open now)")
        return _sell_junk(keep)
    args: dict[str, Any] = {"confirm": confirm}
    if count:
        args["count"] = int(count)
    head = ""
    if npc:
        args["npc"] = _uuid(npc)
        opened = _open_trade(npc)
        if opened.startswith("trade: FAILED") or opened.startswith("FAILED") or "\nSTOPPED" in opened:
            return opened
        head = opened
    if offer:
        args["offer"] = [_uuid(i) for i in offer.split(",") if i.strip()]
    if request:
        args["request"] = [_uuid(i) for i in request.split(",") if i.strip()]
    handover = False
    if confirm and offer and not request:
        handover = economy.handover(game.try_rpc("ui.trade", {}, context="client"))
        donate = donate or handover
    if donate:
        args["donate"] = True
    _trade_view.update(listing=not (offer or request or confirm), full=full)
    if full:
        args["max"] = 500
    out = _act("trade", "ui.trade", args, context="client", wait_s=20)
    res = _batch.get("result")
    if isinstance(res, dict) and res.get("donate_box"):
        out = _answer_donate(res, handover)
    elif offer and "couldn't be moved" in out:
        notes = economy.equipped_offers(game.try_rpc("state.inventory", {}), args["offer"], _name_of)
        if notes:
            out += "\n" + "\n".join(notes)
    return (head + "\n" + out) if head else out


def _answer_donate(res: dict, handover: bool) -> str:
    """trade(donate=true) pressed Barter on a one-sided deal: answer the game's "Donate these items?" box with Yes
    (S028 15:48: the agent screenshotted, clicked Barter and confirmed by hand). On a hand-over screen, close it
    after (nothing else can happen there) so the conversation goes on."""
    gave = ", ".join(f"{_item_handle(m)} {m.get('name') or '?'}" for m in res.get("moved") or []
                     if isinstance(m, dict) and m.get("side") == "you") or "the offered items"
    box = None
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        box = game.try_rpc("ui.confirm", {"answer": "Yes", "dry": True}, context="client")
        if isinstance(box, dict) and box.get("buttons"):
            break
        box = None
        time.sleep(0.2)
    if box is None:
        return (f"trade: FAILED — gave {gave} with Barter, but no 'Donate these items?' box came up\n"
                + "\n".join(_render_ui(game.try_rpc("ui.state", context="client") or {})))
    q = f"{box.get('title') or ''} {box.get('text') or ''}".strip()
    if not re.search(r"donat|give", q, re.I):
        return f"trade: STOPPED — the game asks: {q} {box.get('buttons')}; answer with confirm"
    c = _box_act("confirm", "ui.confirm", {"answer": "Yes"}, wait_s=8)
    if "confirm: done" not in c:
        return f"trade: FAILED — gave {gave} with Barter; the donate box: {c}"
    out = f"trade: done — gave {gave} (donated: answered the game's '{q}' with Yes)"
    if handover:
        time.sleep(0.3)
        if _ui_screen()[0] == "trade":
            esc = _press_escape()
            out += "; closed the hand-over screen" if "FAILED" not in esc and "REFUSED" not in esc else f"; close: {esc}"
        u = game.try_rpc("ui.state", context="client") or {}
        if u.get("screen") not in (None, "game", "trade"):
            out += "\n" + "\n".join(_render_ui(u))
    return out


def _open_trade(npc: str) -> str:
    """trade(npc) with no trade screen up: talk to them and pick the conversation's trade answer, as a player does
    ('Let me see your wares.'). '' = it was already open; a short note when this call opened it; else the reason
    (FAILED / the conversation, for the agent to choose). The dialogue's Trade button (btnTrade → StartTrade) runs but
    opens nothing from bg3le (live 12:4x, Dammon): its entity parameter doesn't get through."""
    def ui() -> dict:
        return game.try_rpc("ui.state", context="client") or {}

    def wait(pred, s: float) -> dict:
        end, u = time.monotonic() + s, ui()
        while not pred(u) and time.monotonic() < end:
            time.sleep(0.2)
            u = ui()
        return u

    u = ui()
    if u.get("screen") == "trade":
        return ""
    talked = ""
    if u.get("screen") != "dialogue":
        if not npc:
            return "trade: FAILED — no conversation or trade screen is open: trade(npc=<trader id>) talks to them first"
        talked = talk_to(npc)
        if "FAILED" in talked.split("\n", 1)[0]:
            return f"trade: FAILED — could not start the conversation: {talked}"
        u = wait(lambda x: bool((x.get("dialogue") or {}).get("options")) or x.get("screen") not in ("dialogue", "game"), 3.0)
    d = u.get("dialogue") or {}
    pick = economy.trade_answer(d.get("options") or [])
    if pick is None:
        opts = "; ".join(f"{o.get('i')}. {o.get('text')}" for o in d.get("options") or [] if isinstance(o, dict))
        return (f"trade: no trade answer in the conversation with {npc} yet ({opts or 'no answers shown'})\n"
                + (talked + "\n" if talked else "")
                + "STOPPED: pick an answer with choose_dialogue (the trade one may come later), then trade()")
    r = game.run_job("ui.dialogue_choose", {"i": pick["i"]}, context="client", wait_s=20)
    if r.status != "done":
        return f"trade: FAILED — picking '{pick.get('text')}': " + _job_text("choose_dialogue", r)
    u = wait(lambda x: x.get("screen") == "trade", 4.0)
    if u.get("screen") != "trade":
        return (f"trade: FAILED — picked '{pick.get('text')}' but no trade screen opened (screen={u.get('screen')})\n"
                + "\n".join(_render_ui(u)))
    return f"(opened: {'talked to ' + npc + ', ' if npc else ''}picked '{pick.get('text')}')"


_trade_view: dict[str, bool] = {"listing": True, "full": False}  # how the next trade result is rendered


def _item_handle(it: dict) -> str:
    u = it.get("uuid")
    return H.get(u, it.get("name")) if isinstance(u, str) and u else "(no id)"


def _sell_junk(keep: str = "") -> str:
    t0 = time.monotonic()
    inv = game.rpc("state.inventory", {})
    _remember_names(inv)
    junk = {it["uuid"]: it for it in economy.junk(inv, _keep_set(keep))}
    st = game.rpc("ui.trade", {"max": 500}, context="client")  # every slot, not the default 60
    _remember_names(st)
    on_screen = [it for it in (st.get("your_items") or []) if isinstance(it, dict) and it.get("uuid") in junk]
    budget = st.get("trader_gold")
    picked, total = [], 0
    # price is the whole stack's (the game's TradePrice; live: Potion of Healing x5 = 40), never x count
    for it in sorted(on_screen, key=lambda x: -(x.get("price") or 0)):
        p = it.get("price") or 0
        if isinstance(budget, (int, float)) and total + p > budget:
            continue
        picked.append(it["uuid"])
        total += p
    if not picked:
        why = ("no junk in the bags" if not junk else
               ("the trader has no gold left: sell at another trader, or swap junk for wares of equal value "
                "(offer=<ids>, request=<ids>)" if not budget else
                f"the trader has only {budget} gold, less than any one junk item") if on_screen
               else "none of the junk is on this trade screen")
        return _finish("trade", {"sell": "junk"}, f"trade: done — nothing sold ({why})", t0)
    _EV_DROP.update(items={H._bare(u) for u in picked}, gold=True, until=0.0)
    try:
        out = _act("trade", "ui.trade", {"offer": picked, "confirm": True}, context="client", wait_s=30)
    finally:
        _EV_DROP["until"] = time.monotonic() + EV_DROP_TAIL_S  # late AddedTo events of this sale
    left = len(on_screen) - len(picked)
    note = f"\n(sold {len(picked)} junk items, ~{total} g at the shown prices" + (
        f"; {left} more left: the trader ran out of gold, stash them with manage_inventory op=camp item=junk)" if left else ")")
    return out + note


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
def confirm(answer: str = "yes", amount: int = 0) -> str:
    """Answer the open Yes/No (OK/Cancel, Confirm...) question box: answer = "yes" | "no" | a button label | its number. Shows the next question if the game asks another. A stack-split box ("how many?", POPUP ... split): amount=N sets how many before "yes"."""
    args: dict[str, Any] = {"answer": answer}
    if amount:
        args["amount"] = int(amount)
    if str(answer).strip().lower() not in ("no", "cancel", "decline", "2") and \
            (game.try_rpc("ui.state", context="client") or {}).get("screen") == "reward":
        # S042 22:50:08: confirm("Accept") on Selûne's Reward panel (screen=reward) failed "no confirmation box is
        # open"; a player clicks its Accept button, which is what dismiss_popups' GetRewardsAndClose does.
        t0 = time.monotonic()
        res = game.rpc("ui.dismiss", {}, context="client")
        return _finish("confirm", {"answer": answer}, "confirm: done — the Reward panel: " + render.compact(res, H), t0)
    return _box_act("confirm", "ui.confirm", args, wait_s=8)


def _reactions_rpc(char: str, args: dict) -> Any:
    """ui.reactions works on the HUD's selected character (the client's ClientControl). The server's `active` falls
    back to the host when no party member holds ClientControl, so "already selected" can be wrong (S025; S041
    22:41:22 combat_turn "reactions: FAILED — char is not the selected character"): select first when the server
    disagrees, and on the client's refusal select and retry once."""
    try:
        if _active() != char:
            game.run_job("act.select", {"char": char}, wait_s=5)
    except GameError:
        pass
    try:
        return game.rpc("ui.reactions", args, context="client")
    except GameError as e:
        if "not the selected character" not in str(e):
            raise
    game.run_job("act.select", {"char": char}, wait_s=5)
    for _ in range(5):  # the client's ClientControl follows the selection a frame or two later
        time.sleep(0.1)
        try:
            return game.rpc("ui.reactions", args, context="client")
        except GameError as e:
            if "not the selected character" not in str(e):
                raise
            last = e
    raise GameError(f"{last} (selected {_label(char)} and retried)")


@mcp.tool()
@_guard
def set_reactions(who: str | None = None, settings: str = "") -> str:
    """See or change a character's reaction prompts (e.g. Shield, Divine Smite, Opportunity Attack). settings: JSON {"<reaction name>": "never"|"always"|"ask"}; empty = list current."""
    args: dict[str, Any] = {"char": _who(who)}
    if settings:
        args["set"] = json.loads(settings)
    t0 = time.monotonic()
    res = _reactions_rpc(args["char"], args)
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
    """Press a keyboard key in the game (SDL scancode name, e.g. ESCAPE, SPACE, RETURN, F5, 1). Prefer dedicated tools. ESCAPE with nothing open is refused (it would open the game menu)."""
    key = key.upper()
    if key in ("ESCAPE", "ESC"):
        return _press_escape()
    return _act("press_key", "ui.key", {"key": key}, context="client", wait_s=5)


def _ui_screen() -> tuple[str, list]:
    """(screen, panels_open) from ui.state; ('?', []) when the client can't say."""
    ui = game.try_rpc("ui.state", context="client")
    if not isinstance(ui, dict) or ui.get("error"):
        return "?", []
    return ui.get("screen") or "game", list(ui.get("panels_open") or [])


def _press_escape() -> str:
    """S021 05:38 / S026 09:42 (sale lost): ESCAPE used as 'close' on the plain game view opened the game menu.
    Refused there; and when a press still lands on the game menu (the panel had closed itself), it is closed again."""
    t0 = time.monotonic()
    before, panels = _ui_screen()
    if before == "game" and not panels:
        return _finish("press_key", {"key": "ESCAPE"}, "REFUSED: nothing is open to close: ESCAPE here "
                       "would open the game menu. Panels our tools open close themselves.", t0)
    out = _act("press_key", "ui.key", {"key": "ESCAPE"}, context="client", wait_s=5)
    if before not in ("game_menu", "options", "save_load"):
        time.sleep(0.3)
        if _ui_screen()[0] == "game_menu":
            # The menu's own Resume button (live 12:1x: a second key press left the menu open 3/3 while the
            # result said "closed it again"); then check, and say so if it is still up.
            game.try_rpc("ui.press", {"button": "Resume", "widget": "GameMenu"}, context="client")
            closed = False
            for _ in range(6):
                time.sleep(0.15)
                if _ui_screen()[0] != "game_menu":
                    closed = True
                    break
            if not closed:
                game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
                time.sleep(0.3)
                closed = _ui_screen()[0] != "game_menu"
            out += ("\nthe ESCAPE opened the game menu (what it was meant to close had already closed): "
                    + ("closed it again" if closed else "it is STILL OPEN (press_key ESCAPE once more closes it)"))
            if before == "dialogue":
                out += "\n(ESCAPE does not leave a conversation: pick its Leave/exit answer with choose_dialogue)"
    return out


@mcp.tool()
@_guard
def click(x: float, y: float, button: str = "left") -> str:
    """Last-resort mouse click at a screen position given as fractions 0-1 of the latest screenshot.
    button: "left", "right", or "none" (= hover: only moves the cursor there, nothing is clicked; for a tooltip or
    the cursor text of a targeting mode, then take a screenshot)."""
    t0 = time.monotonic()
    b = (button or "left").strip().lower()
    kind = {"left": "L", "l": "L", "right": "R", "r": "R", "none": "", "hover": "", "move": ""}.get(b)
    if kind is None:
        # S039 21:47:42: an unknown button used to commit a LEFT click (with the Jump targeting on: a 5 m jump)
        raise GameError(f"unknown button {button!r}: use left, right, or none (= hover, no click)")
    inp = _inp()
    if inp is None:
        raise GameError("input shim not available")
    w, h = inp.window_size()
    px, py = int(x * w), int(y * h)
    if not kind:
        inp.move(px, py)
        time.sleep(0.3)
        return _finish("click", {"x": x, "y": y, "button": "none"}, f"hovered at {px},{py} (nothing clicked)", t0)
    inp.click(px, py, kind)
    time.sleep(0.3)
    return _finish("click", {"x": x, "y": y}, f"clicked {'right' if kind == 'R' else 'left'} at {px},{py}", t0)



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
def do_actions(actions: str, wait: bool = True, ignore: str = "", note: str = "") -> str:
    """Run several actions in order in ONE call: a whole combat turn, a looting sweep, a dialogue chain. Plan from one observation, then execute.
    actions: JSON list of steps, e.g.
    [{"tool":"move_to","target":"e12"},{"tool":"use_ability","ability":"Target_MainHandAttack","target":"e12","unless":"hp_below:30"},{"tool":"use_ability","ability":"Shout_SecondWind","if":"hp_below:50"},{"tool":"end_turn"}]
    Allowed: move_to, walk_toward, interact, pick_up, loot, loot_corpse, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
    Optional per step: "if"/"unless" with in_combat, in_dialogue, my_turn, hp_below:<pct>, item_present:<id>, target_visible:<id> (a failed condition skips the step).
    Stops and drops the rest on: combat or a conversation starting, a reaction/roll/popup, a new hostile in view, HP falling below 35%, any damage taken outside combat, stepping into a harmful surface (fire, acid, ...), a trap spotted on the way, a blocked teleport, or a failed step. ignore="hostile,hp" disables some (combat, dialogue, prompt, hostile, hp, damage, surface, teleport, trap).
    Returns each step's outcome, why it stopped (and what was left), a NOW line and CHANGES (position, HP, items, new things in view, mode). wait=false queues the steps instead (see queue_actions).
    note: memory log line(s) appended to your session log at once, as note() does (no extra turn)."""
    t0 = time.monotonic()
    if not wait:
        return queue_actions(actions, ignore=ignore, note=note)
    _note_quiet(note)
    try:
        steps = actq.parse_steps(actions, _BATCHABLE)
    except actq.StepError as e:
        return f"FAILED: {e}"
    _hold_for_early_walks(None)  # a batch issued in a walk's last seconds starts after the arrival
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
def queue_actions(actions: str, who: str | None = None, replace: bool = False, ignore: str = "",
                  note: str = "") -> str:
    """Append steps to a character's action queue and return at once; they run back to back while you observe and plan the next move. Same step format, conditions and interrupts as do_actions. On the end you get a queue_done or queue_interrupted event (why, what was left) in any later result; wait_for('queue') blocks until it is done. replace=true drops what was still queued first. Each character has its own queue, so two characters can act at once. Your own direct action for that character pre-empts the queue. note: memory log line(s) for your session log, written at once (as note())."""
    t0 = time.monotonic()
    _note_quiet(note)
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
        floor = _other_floor(char, _pos_now(char) if ft else me.get("pos"), dest)
        if floor and not floor.startswith("REFUSED"):
            pre.append(floor)
        elif floor:
            return _finish("travel_to", {"to": what}, "".join(p + "\n" for p in pre) + f"travel_to {what}: {floor}", t0)
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
    left = _leave_camp_first()
    if left.startswith("FAILED"):
        return (f"fast travel to {wp['name']}: {left}: walking instead", None)
    r = game.run_job("ui.fast_travel", {"waypoint": wp["name"]}, context="client", wait_s=70)
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
    plan = _LEVEL_NUDGE.plan_hint(MEMORY)
    return (f"NOTES WINDOW: ~{render.num(round(lr[1]))}s of walking left: note() what happened / plan now "
            "(not after it ends, when the character would stand idle); queue_actions the arrival first"
            + ("\n" + plan if plan else ""))


@mcp.tool()
@_guard
def cancel_queue(who: str | None = None) -> str:
    """Stop a character's queue (who) or every queue (who empty): the running step is cancelled and the rest dropped."""
    t0 = time.monotonic()
    out = _queues().cancel(_who_uuid(who) if who else None)
    if not out:
        return _finish("cancel_queue", {}, "no queue was running", t0)
    lines = [f"cancelled {_label(w)}: " + (f"stopped {run}, " if run else "") + f"dropped {n}" for w, n, run in out]
    return _finish("cancel_queue", {}, "\n".join(lines), t0)


_PROMPT_SCREENS_WAIT = ("reaction", "dialogue", "roll", "popup", "game_over")
# Screens that wait on the player like a prompt: wait_for stops when one opens, and a wait for events that starts
# on one answers at once (S026 09:04: the rest screen opened during a 25 s wait for dialogue_started, then a
# second 20 s wait began on it: ~45 s of a screen waiting for the agent).
_INPUT_SCREENS_WAIT = {
    "rest": "long_rest finishes it", "level_up": "level_up", "learn_spells": "level_up",
    "trade": "trade, or press_key ESCAPE to close it", "tutorial": "dismiss_popups", "reward": "dismiss_popups",
    "tadpole_tree": "press_key ESCAPE to close it", "donate": "feed_gale(item)",
    "insert": "insert_item(item) (insert_item() lists what it takes), or press_key ESCAPE to close it",
}


def _input_screen_text(scr: str) -> str:
    return f"the {scr.replace('_', ' ')} screen is open and waits on you: {_INPUT_SCREENS_WAIT[scr]}"
# Event kinds the world can raise with none of our jobs or queues running (an ambush, a hazard). Waiting on
# anything else with nothing running, on the plain game screen and not during enemy turns, cannot end (S013:
# 3x 4 s on 'roll'/'dialogue_started' after a failed lockpick), so wait_for answers at once.
WORLD_EVENT_KINDS = {"combat_started", "spotted", "damage", "died", "status_applied", "status_removed"}
EVENT_GRACE_S = 0.5  # after a job/queue we saw running stops, wait this long for the event it may cause


def _wait_combat(view: dict) -> dict:
    """state.combat, read at most once per wait_for tick; `view` also keeps the party set for the whole wait."""
    if "combat" not in view:
        view["combat"] = game.try_rpc("state.combat") or {}
    return view["combat"]


def _wait_acting(view: dict) -> list[str]:
    """Party members whose turn it is ([] out of combat)."""
    cs = _wait_combat(view)
    if not cs.get("active"):
        return []
    if "party" not in view:
        view["party"] = _party_set()
    return turn.party_acting(cs, view["party"])


def _wait_state(low: str, tgt: str, kinds: set, ui: dict, view: dict) -> str | None:
    """What the current state already answers for a wait target (None: nothing yet). `view`: see _wait_combat."""
    scr = ui.get("screen") if isinstance(ui, dict) else None
    if low == "idle" or "idle" in kinds:
        if not _queues().busy() and not _running_jobs():
            return "idle: no action running"
    if low == "queue" or low.startswith("queue:") or "queue" in kinds:
        who = _uuid(tgt.split(":", 1)[1]) if low.startswith("queue:") else None
        if not _queues().busy(who):
            return _last_queue_text(who) or "queue: empty"
    if low == "my_turn" or "my_turn" in kinds:
        if not _wait_combat(view).get("active"):
            return "not in combat"
        if _wait_acting(view):
            return _now_turn_text(_wait_acting(view))
    if not kinds:
        return None
    for k in kinds & set(_PROMPT_SCREENS_WAIT):
        if scr == k:  # the prompt is already up: its screen change is never coming
            return f"{k} on screen\n" + "\n".join(_render_ui(ui))
    d = (ui.get("dialogue") or {}) if isinstance(ui, dict) else {}
    if scr == "dialogue" and kinds & {"dialogue_started", "dialogue_options"} and (
            "dialogue_started" in kinds or d.get("options")):
        # Already open (its event went out in an earlier result): live S002 waited the full 5 s here.
        return "dialogue open\n" + "\n".join(_render_ui(ui))
    if "dialogue_ended" in kinds and scr == "game":
        return "no dialogue open"
    if "combat_ended" in kinds and not _wait_combat(view).get("active"):
        return "not in combat"
    if "combat_started" in kinds and _wait_combat(view).get("active"):
        return "already in combat"
    return None


def _quiet(scr: str | None, view: dict) -> bool:
    """Nothing of ours runs and the game waits on us: plain game screen, no job, no queue, not an enemy turn."""
    if scr not in (None, "game") or _queues().busy() or _running_jobs():
        return False
    return not _wait_combat(view).get("active") or bool(_wait_acting(view))


# Early wake (impl-act "pipelining stuck"): in exploration the agent waits for arrival, then thinks ~2 s while
# the character stands. wait_for on a walk returns when about one think time (lead_s) of it is left, so the
# agent plans the next step during the last seconds; what it then issues for that character runs after the
# walk arrives (_hold_for_early_walks), never cancelling it. Not in combat (the turn needs the real arrival).
LEAD_S = 2.0          # default lead: the agent's typical think time
EARLY_CHECK_S = 0.25  # how often wait_for looks at the remaining ETA (one act.watch per trip)
EARLY_HOLD_S = 8.0    # an action issued after an early wake waits at most this long for the walk to end
_early: dict[str, dict] = {}  # key -> {"char", "job"|"queue"} of walks whose early wake fired (hold their followers)
_early_seen: set = set()      # keys that already woke a wait once (the next wait_for blocks to the end)


def _walk_left(low: str) -> list[tuple[str, str | None, float, str, dict]]:
    """For a wait target, the walks it waits on: (key, char, seconds left, label, gate entry). [] when it also
    waits on something that is not a walk with a known ETA (no early wake then)."""
    now = time.monotonic()
    out = []
    jobs: list[str] = []
    if re.fullmatch(r"j\d+", low):
        jobs = [low]
    elif low == "idle":
        jobs = _running_jobs()
    for h in jobs:
        info = _async.get(h)
        if not info or info.get("tool") not in _WALKS or info.get("eta") is None:
            return []
        out.append((f"{h}@{info['t0']}", info.get("char"), float(info["eta"]) - (now - info["t0"]),
                    f"{h} {info['tool']}", {"char": info.get("char"), "job": h}))
    if low in ("idle", "queue") or low.startswith("queue:"):
        who = _uuid(low.split(":", 1)[1]) if low.startswith("queue:") else None
        for q in _queues().status():
            if who is not None and q["who"] != who:
                continue
            trip = _trips.get(q["who"])
            if not (q["running"] or "").startswith("travel_to") or q["waiting"] or not trip:
                return []  # arrival steps still to run, or not a trip: the queue is not nearly done
            tl = _trip_left(q["who"])
            if tl is None or tl[1] is None:
                return []
            out.append((f"queue:{q['who']}@{trip['t0']}", q["who"], tl[1], f"queue {_label(q['who'])} travel_to",
                        {"char": q["who"], "queue": True}))
    return out


def _early_wake(low: str, lead_s: float, view: dict) -> str | None:
    """The wait_for answer when every walk it waits on has at most lead_s left (once per walk), else None."""
    if lead_s <= 0:
        return None
    walks = _walk_left(low)
    if not walks or max(w[2] for w in walks) > lead_s or any(w[0] in _early_seen for w in walks):
        return None
    if _wait_combat(view).get("active"):
        return None
    for key, _, _, _, gate in walks:
        _early_seen.add(key)
        _early[key] = gate
    left = max(0.0, max(w[2] for w in walks))
    who = sorted({_label(w[1]) for w in walks if w[1]})
    return (f"{', '.join(w[3] for w in walks)}: arriving, ETA ~{render.num(round(left, 1))}s (early wake, "
            f"lead_s={render.num(lead_s)}): plan the next step now. What you issue"
            + (f" for {', '.join(who)}" if who else "") + " runs after the walk arrives (it does not cancel it);"
            " wait_for again blocks to the end.")


def _hold_for_early_walks(char: str | None) -> str:
    """Before a direct action: if an early-woken walk of that character (any, if char is None) is still
    going, wait for it to end, so the action queues after the walk instead of cancelling it. A note or ''."""
    if not _early:
        return ""
    keys = [k for k, g in _early.items() if char is None or g.get("char") in (None, char)]
    if not keys:
        return ""
    t0 = time.monotonic()

    def going() -> bool:
        for k in keys:
            g = _early[k]
            if g.get("job") and g["job"] in _running_jobs():
                return True
            if g.get("queue") and _queues().busy(g["char"]):
                return True
        return False

    held = False
    while going() and time.monotonic() - t0 < EARLY_HOLD_S:
        held = True
        time.sleep(0.1)
    for k in keys:
        _early.pop(k, None)
    if not held:
        return ""
    return f"(waited {render.num(round(time.monotonic() - t0, 1))}s for the walk to arrive first)"



@mcp.tool()
@_guard
def wait_for(target: str = "idle", timeout_s: float = 15.0, lead_s: float = LEAD_S) -> str:
    """Block only until something happens, then report it plus events and CHANGES. target: a job id (j12) from an async action; 'queue' (all queues done) or 'queue:<who>'; 'idle' (no async job or queue running); 'my_turn' (a party member can act, or combat ended); or event kinds, comma-separated (e.g. 'combat_started,dialogue_started,job_finished'). Also returns early when a reaction/roll/dialogue/popup or a rest/level-up/trade screen appears. Returns at once when the state already answers it or nothing running could cause it. Outside combat, waiting on a walk/travel_to (job id, 'queue', 'idle') returns once when ~lead_s seconds of it are left (ETA line), so you plan while it finishes: what you issue then runs after the arrival, never cancelling it; lead_s=0 waits for the end."""
    t0 = time.monotonic()
    lead_s = max(0.0, min(float(lead_s), 10.0))
    early_at = 0.0
    timeout_s = max(0.1, min(float(timeout_s), 60.0))
    deadline = t0 + timeout_s
    tgt = (target or "idle").strip()
    low = tgt.lower()
    special = re.fullmatch(r"j\d+", low) or low in ("idle", "queue", "my_turn") or low.startswith("queue:")
    kinds = set() if special else {k.strip() for k in low.split(",") if k.strip()}
    ui_at = 0.0
    screen0 = None
    view: dict = {}
    ui: dict = {}
    body = None
    noop = None  # why the wait never blocked (metrics 'wait_for_noop'), or None
    first = True
    busy_seen = 0.0  # last time a job/queue was seen running during this wait
    while body is None:
        now = time.monotonic()
        if now - ui_at >= 0.5:
            ui_at = now
            ui = game.try_rpc("ui.state", context="client") or {}
            ui = ui if isinstance(ui, dict) else {}
            scr = ui.get("screen")
            if screen0 is None:
                screen0 = scr
                if kinds and scr in _INPUT_SCREENS_WAIT:
                    body = f"not waiting: {_input_screen_text(scr)}"
                    noop = "screen"
                    break
            elif scr in _PROMPT_SCREENS_WAIT and scr != screen0:
                body = f"stopped waiting: {scr} on screen\n" + "\n".join(_render_ui(ui))
                break
            elif scr in _INPUT_SCREENS_WAIT and scr != screen0:
                body = f"stopped waiting: {_input_screen_text(scr)}"
                break
        view.pop("combat", None)  # fresh each tick (the party set is kept)
        scr = ui.get("screen")
        if re.fullmatch(r"j\d+", low):
            if _batch["active"] and _batch["exec"] is not None and low in _async:
                _batch["exec"].track(_async[low]["ctx"], _async[low]["id"])  # cancel_queue cancels it too
            jv = game.rpc("job.poll", {"id": int(low[1:])})
            if jv.get("status") != "running":
                info = _async.pop(low, {})
                METRICS.bg_end(low)
                r = JobResult(jv["status"], jv.get("result"), jv.get("error"),
                              elapsed_ms=jv.get("elapsed_ms", 0))
                _remember_names(r.result)
                body = _job_text(info.get("tool") or low, r, info.get("char"))
                if info.get("tool") in ARRIVAL_LEAD_TOOLS and r.status == "done" and not _batch["active"] and \
                        not ((r.result or {}).get("after") or {}).get("in_combat"):
                    body += _arrival_leads()
                noop = "job_done" if first else None
                break
        else:
            body = _wait_state(low, tgt, kinds, ui, view)
            if body is not None:
                noop = "already" if first else None
                break
        if not kinds and lead_s and not _batch["active"] and now - early_at >= EARLY_CHECK_S:
            early_at = now
            body = _early_wake(low, lead_s, view)
            if body is not None:
                noop = "early" if first else None
                break
        if kinds:
            hit = _peek_events(kinds)
            if hit:
                body = f"{hit.get('kind')} happened"
                noop = "already" if first else None
                break
            quiet = _quiet(scr, view)
            if not quiet:
                busy_seen = now
            elif not kinds & WORLD_EVENT_KINDS and now - busy_seen >= EVENT_GRACE_S:
                body = (f"nothing is running that would cause {tgt} (no job, no queue, no prompt): "
                        "act instead (talk_to, move_to, queue_actions)")
                noop = "cannot_happen" if busy_seen == 0.0 else None
                break
            elif quiet and busy_seen == 0.0 and _noop_waits["n"] >= 1:
                # S023 06:47-06:53: 20 waits for combat_started in a row with only reads (look_at, session_clock)
                # between them, 4 s + a model turn each. The second one in a row answers at once.
                body = (f"nothing is running that would cause {tgt} — again: only reads ran since the last wait said "
                        "so, and waiting never starts a fight or a conversation. Act: move_to/walk_toward, talk_to, "
                        "interact, or queue_actions")
                noop = "repeat"
                break
            elif quiet and now - max(t0, busy_seen) >= IDLE_EVENT_WAIT_S:
                # S009: 'dialogue_started' after a finished 3 m walk waited its full 10 s, and the last minutes
                # went to 45+30+40 s waits with nothing running. Nothing of ours can cause it: say so now.
                body = (f"nothing is running that would cause {tgt} (no job, no queue, no prompt) after "
                        f"{render.num(round(now - t0, 1))}s: act instead (talk_to, move_to, queue_actions)")
                break
        first = False
        if now > deadline:
            body = f"still waiting for {tgt} after {render.num(round(timeout_s, 1))}s" + (
                "\n" + _running_text() if _running_text() else "")
            break
        time.sleep(0.1)
    if (low in ("queue", "idle") or low.startswith("queue:")) and not _batch["active"] and "LEADS" not in body:
        steps = re.findall(r"(?m)^\d+\. (\w+)", body)
        if steps and steps[-1] in ARRIVAL_LEAD_TOOLS and re.search(r": done \(\d+ steps?\)", body) \
                and not (view.get("combat") or {}).get("active"):
            body += _arrival_leads()
    changes = delta.changes_text(game, H)
    if changes:
        body += "\n" + changes
    return _finish("wait_for", {"target": tgt}, body, t0, extra={"noop": noop} if noop else None)


# S023-S027: after a walk's wait_for the next call was get_frontier (22x), get_nearby (18x) or route (18x), each a
# ~2 s model turn to learn where to go on from here. The arrival result now carries the best unvisited leads + WAYS.
ARRIVAL_LEAD_TOOLS = {"move_to", "walk_toward", "travel_to", "jump_to"}
ARRIVAL_LEADS = 5


def _arrival_leads() -> str:
    """'\nLEADS (unvisited; get_frontier lists all): e529 Damaged Vase <usable> 8.3m E | …' + the WAYS line."""
    res = game.try_rpc("nav.frontier", {})
    if not isinstance(res, dict) or not isinstance(res.get("leads"), list):
        return ""
    _remember_names(res)
    new = [l for l in res["leads"] if isinstance(l, dict) and l.get("unvisited")]
    out = []
    if new:
        shown = render.render_frontier({"leads": new[:ARRIVAL_LEADS]}, H).splitlines()
        more = len(new) - ARRIVAL_LEADS + (res.get("more") or 0)
        out.append("LEADS (unvisited; get_frontier lists all): " + " | ".join(x.replace(" unvisited", "") for x in shown)
                   + (f" | +{more} more" if more > 0 else ""))
    ways = render.render_ways(res.get("ways"))
    if ways:
        out.append(ways)
    return ("\n" + "\n".join(out)) if out else ""


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
        if row and row.get("chained") is False and not (row.get("dead") or row.get("downed") or row.get("summon")):
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
    _turn_cs.clear()  # a step changes the turn: the next check reads it again
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


_party_memo: dict = {"t": -1e9, "uuids": None}


def _party_cached(ttl_s: float = 30.0) -> set | None:
    """The party's uuids, re-read at most every ttl_s (the EVENTS crowd mute needs them; one RPC per 30 s)."""
    now = time.monotonic()
    if now - _party_memo["t"] > ttl_s:
        st = game.try_rpc("state.status")
        if isinstance(st, dict) and st.get("party"):
            _party_memo.update(t=now, uuids={c.get("uuid") for c in st["party"] if isinstance(c, dict)})
    return _party_memo["uuids"]


def _party_set() -> set:
    st = game.try_rpc("state.status") or {}
    return {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}


# combat_turn's own state.combat read, reused by the first step check (no step ran in between): one round
# trip instead of two at the start of every turn.
_turn_cs: dict = {}


def _turn_check(who: str, step: actq.Step | None) -> tuple[bool, str]:
    """Before each turn step, from what the player sees: (fatal, why). fatal = the turn is over for us;
    a non-fatal why (target dead / out of view) skips just this alternative."""
    cs = _turn_cs.pop("cs", None) or game.try_rpc("state.combat") or {}
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
        row = next((o for o in cs.get("order") or [] if o.get("uuid") == u), None)
        # A combatant on the read turn order is visible (state.combat lists only seen ones) and says if it
        # died: no act.target_state round trip (S018-S026: ~22 ms each, 2-4 per turn).
        ts = ({"visible": True, "dead": row.get("dead")} if row is not None
              else game.try_rpc("act.target_state", {"uuid": u}))
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


# Why a melee move stopped short that a step-in during the action can't fix (an 'early' stop, bumped or
# blocked with movement left, keeps the old path: the attack runs and steps into range itself).
MELEE_OUT_SHORT = ("movement", "hazard", "route_end", "no_path")
# Abilities that are melee even when get_actions has no range for them.
MELEE_IDS = ("Target_MainHandAttack", "Target_OffhandAttack", "Target_UnarmedAttack")


# A melee route that ends this close to the target ends at its body, not at a ledge (S024: "no walkable route
# gets closer than 2.5m", then a step put Shadowheart in reach): try the spot beside it.
MELEE_ROUTE_END_M = 4.0


def _turn_move(mv: turn.Move, who: str, watcher: actq.Watcher, lines: list, after: dict,
               info: dict | None = None) -> str:
    """The move slot: 'ok', 'none' (couldn't move; the turn goes on, fallbacks may still work) or 'stop'.
    A move toward a target that ends out of `within` says how far it got and why; info["short"] gets that
    reason ("movement" / "hazard" / "route_end" / "no_path" / "early") for combat_turn's fallbacks."""
    info = info if info is not None else {}
    info.pop("short", None)
    info.pop("melee_out", None)
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

    # A melee move (no within, or a weapon-sized one) walks to a path-checked spot beside the target inside
    # the guard's edge-to-edge reach (act.move_to within=). S023-S025: the route walk (nav.approach) ended
    # "no walkable route gets closer than 2.5m" (the engine route stops at the target's body) and 22 melee
    # attacks were refused out of range.
    melee = tu is not None and (mv.within is None or mv.within <= turn.MELEE_RANGE_M)

    def out_of_melee(d2: float | None, in_reach: Any = None) -> str:
        """Acceptance 13:3x: 'out of movement (12m left, the route needs 12m)' ended 3.1 m away (9/9.3 m →
        3.4 m: the real walk loses ~1.5 m to the route), and the melee attack then failed out of range.
        A melee move that ends clearly out of reach (the mod's in_reach=false, or ≥ weapon reach + pad
        centre to centre after a short walk) marks info["melee_out"]: the action slot skips melee
        alternatives on that target with that reason and falls back (ranged / Dash) instead of a miss."""
        if not melee:
            return ""
        want = mv.within if mv.within is not None else turn.MELEE_M
        if in_reach is False or (info.get("short") in MELEE_OUT_SHORT and d2 is not None
                                 and d2 > want + turn.MELEE_REACH_PAD):
            info["melee_out"] = {"target": tu, "dist": d2}
            return " — melee is out of reach this turn"
        return ""
    if tu and mv.within is not None:
        d = dist_now()
        if d is not None and d <= mv.within:
            lines.append(f"move: already within {render.num(mv.within)}m of {what} ({render.num(d)}m)")
            return "ok"
        ap = _approach(who, tu, mv, mv.within)
        if ap is not None and melee and ap.get("reachable") is not False and (
                ap.get("limit") == "in_range"
                or (ap.get("limit") == "route_end" and float(ap.get("end_m") or 99) <= MELEE_ROUTE_END_M)):
            pass  # no hazard, within movement: the spot beside the target (below), not the route point
        elif ap is not None:
            if ap.get("reachable") is False or not ap.get("pos") or float(ap.get("walk_m") or 0) < 0.5:
                info["short"] = "no_path" if ap.get("reachable") is False else (ap.get("limit") or "no_path")
                lines.append(f"move {what}: stays put, {render.num(d) if d is not None else '?'}m away — "
                             + (_short_why(ap) or "no way closer") + out_of_melee(d))
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
    if step is None and melee:
        step = actq.Step("move_to", {"target": mv.target,
                                     "within": mv.within if mv.within is not None else turn.MELEE_M})
    if step is None:
        step = actq.Step("move_to", {"target": mv.target} if mv.target else dict(zip("xyz", mv.pos)))
    rep, text = _run_one(watcher, who, step)
    _after(rep, after)
    if rep.done:
        res = text.split(': ', 1)[-1]
        lr = rep.last_result if isinstance(rep.last_result, dict) else {}
        if melee and lr.get("in_reach") is False:
            info["short"] = (ap or {}).get("limit") if (ap or {}).get("limit") == "route_end" else "early"
            res = (res.rstrip(" —") + f" — stopped short of melee reach of {what}: "
                   + (_short_why(ap) if info["short"] == "route_end" else "the walk ended early (blocked or bumped?)"))
        if tu and "target" not in step.args:
            # A walk to a point near the target: its dist= is to that point, not to the target.
            d2 = dist_now()
            res = re.sub(r"\s*dist=[\d.]+", "", res).rstrip(" —")
            if d2 is not None:
                res += f", now {render.num(d2)}m from {what}" + short_note(d2, ap)
            res += out_of_melee(d2, lr.get("in_reach") if "target" in step.args else None)
        elif melee:
            res += out_of_melee(lr.get("dist"), lr.get("in_reach"))
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
                    if rep2.done:
                        info["short"] = info.get("short") or "movement"
                        res2 += out_of_melee(d2)
                lines.append(f"move {what}: {text} → partial {render.num(budget)}m toward it: {res2}")
                if rep2.done:
                    if tu is None:
                        info["short"] = "movement"
                    return "ok"
                if rep2.stop_kind not in (None, "failed"):
                    lines.append(f"STOPPED: {_label(rep2.stopped or '')}")
                    return "stop"
                return "none"
    if melee and "could not get closer" in text and "target" in step.args:
        # S032 17:44: after a Dash (9 m left) Claude's walk to the spot beside a Soulhunter 5.7 m away ended where
        # it started ("could not get closer", movement unspent) while Karlach reached the same target along the
        # route. The spot beside the target is one guess; walk the real route (nav.approach) once before giving up.
        ap2 = _approach(who, tu, mv, mv.within if mv.within is not None else turn.MELEE_M)
        if ap2 is not None and ap2.get("reachable") is not False and ap2.get("pos") \
                and float(ap2.get("walk_m") or 0) >= 0.5:
            x, y, z = ap2["pos"]
            rep2, text2 = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
            _after(rep2, after)
            if rep2.done:
                d2 = dist_now()
                res2 = re.sub(r"\s*dist=[\d.]+", "", text2.split(': ', 1)[-1]).rstrip(" —")
                if d2 is not None:
                    res2 += f", now {render.num(d2)}m from {what}" + short_note(d2, ap2)
                res2 += out_of_melee(d2)
                lines.append(f"move {what}: {text} → along the route: {res2}")
                return "ok"
            if rep2.stop_kind not in (None, "failed"):
                lines.append(f"move {what}: {text}")
                lines.append(f"STOPPED: {_label(rep2.stopped or '')}")
                return "stop"
    if turn.move_too_long(text):
        info["short"] = "movement"
    elif melee and "could not get closer" in text:
        # live 15:1x: Shadowheart's walk to the spot beside a Wraith failed 3.5 m away; the melee was still sent
        # (range miss) and the Dash walked on to 3 m. A blocked walk is a route that ends short.
        m = re.search(r"dist=([\d.]+)", text)
        info["short"] = "route_end"
        text += out_of_melee(float(m.group(1)) if m else dist_now())
    lines.append(f"move {what}: {text}")
    return "none"


def _ability_range(who: str, ability: Any, info: dict) -> float | None:
    """An ability's range from get_actions (read once per turn into info["ranges"]); None when unknown."""
    if "ranges" not in info:
        acts = game.try_rpc("state.actions", {"uuid": who})
        acts = acts.get("actions") if isinstance(acts, dict) else acts
        info["ranges"] = {a.get("spell"): a.get("range") for a in acts or [] if isinstance(a, dict)}
    rng = info["ranges"].get(ability)
    return float(rng) if isinstance(rng, (int, float)) else None


def _slot_skip(alt: turn.Alt, who: str, info: dict | None) -> str:
    """Why combat_turn skips this alternative without asking the game ('' = try it): a melee attack on the
    move's target after the move ended out of reach (said as 'out of range', so the auto fallback runs),
    or a Dash that would walk on toward a move target that is gone."""
    if not info:
        return ""
    a = alt.step.args
    sp = a.get("ability")
    mo = info.get("melee_out")
    if alt.then_move and alt.step.tool == "use_ability" and "Dash" in str(sp or ""):
        if info.get("move_lost"):
            return f"the move's target {info['move_lost']}: dashing would walk nowhere"
        if mo and info.get("short") not in (None, "movement", "early"):
            # live 14:5x: Shadowheart's route ended 2.9 m from a Wraith (no walkable route closer); the Dash
            # walked on to the same spot and the action was lost
            return "more movement gets no closer (the route ends short, not the movement)"
    if not mo or alt.step.tool != "use_ability" or not a.get("target"):
        return ""
    try:
        if _uuid(str(a["target"])) != mo["target"]:
            return ""
    except ValueError:
        return ""
    rng = _ability_range(who, sp, info)
    if (rng is None and sp not in MELEE_IDS) or (rng is not None and rng > turn.MELEE_RANGE_M):
        return ""
    d = mo.get("dist")
    return (f"{_label(mo['target'])} is out of range for melee after the move"
            + (f" ({render.num(d)}m)" if d is not None else ""))


def _turn_slot(name: str, alts: list, who: str, watcher: actq.Watcher, lines: list,
               after: dict, info: dict | None = None) -> tuple[str, bool]:
    """Try a slot's alternatives in order. ('ok'|'none'|'stop', then_move of the one that worked)."""
    tried: list[str] = []
    for alt in alts:
        fatal, why = _turn_check(who, alt.step)
        if fatal:
            lines.append(f"{name}: " + " → ".join(tried + [why]))
            return "stop", False
        why = why or _slot_skip(alt, who, info)
        if why:
            tried.append(f"{alt.step.short()}: skipped ({why})")
            continue
        rep, text = _run_one(watcher, who, alt.step)
        text = _outcome(alt.step, text)
        _after(rep, after)
        if not rep.done and rep.stop_kind in (None, "failed"):
            asked = alt.step.args.get("ability")
            _step_note.clear()
            variant = _pick_variant(alt.step, text)
            step_in = (variant
                       or _step_into_range(who, alt.step, text, watcher, after)
                       or _step_into_sight(who, alt.step, text, watcher, after))
            if step_in:
                # S040-S041: "FAILED: choose a variant: … → took …" read (and triaged) as a failure; the variant pick
                # is the player's quick click on the container's entry, not a failure: name it plainly
                tried.append(f"{asked}: {step_in}" if variant else f"{text} → {step_in}")
                rep, text = _run_one(watcher, who, alt.step)
                text = _outcome(alt.step, text)
                _after(rep, after)
            elif _step_note.get("why"):
                text += f" (no step closer: {_step_note['why']})"
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


_VARIANTS = re.compile(r"choose a variant: ([\w, ]+)")


def _pick_variant(step: actq.Step, text: str) -> str:
    """combat_turn: a container spell refused 'choose a variant: A, B, ...' (S023-S025 x5: Spiritual Weapon,
    Spirit Guardians, Lay on Hands) is cast as its first variant, like a quick click on the first entry; the
    line names it. Changes step.args in place; '' when it isn't that failure."""
    m = _VARIANTS.search(text) if step.tool == "use_ability" else None
    if not m:
        return ""
    names = [v.strip() for v in m.group(1).split(",") if v.strip()]
    # S033 18:47: Lay on Hands on Claude at 13/104 HP took SmallHeal (the first); the agent re-cast BigHeal the next
    # round. A heal container takes its biggest heal (the engine refuses it when the charges don't cover it).
    big = next((v for v in names if re.search(r"(?i)bigheal|_big\b|greater", v)), None)
    first = big or (names[0] if names else "")
    if not first or first == step.args.get("ability"):
        return ""
    step.args["ability"] = first
    return f"took the {'biggest heal' if big else 'first'} variant {first}"


_step_note: dict = {}  # _step_into_range: why the melee step closer failed (shown on the slot's line)


def _step_into_range(who: str, step: actq.Step, text: str, watcher: actq.Watcher, after: dict) -> str:
    """combat_turn: a cast just out of range (S002: Sacred Flame at 18.6 m, range 18 m) walks into range
    along the real route when the movement left covers it; the caller then casts again. Returns what it
    did ('stepped 0.9m into range'), or '' (not a range failure, too far, no route, not enough movement)."""
    gap = macros.range_gap(text) if step.tool == "use_ability" else None
    if not gap or gap[0] - gap[1] > (macros.STEP_INTO_RANGE_RANGED_M if gap[1] > turn.MELEE_RANGE_M
                                     else macros.STEP_INTO_RANGE_M):
        return ""
    a = step.args
    if gap[1] <= turn.MELEE_RANGE_M and a.get("target"):
        # Melee: a spot beside the target inside the edge-to-edge reach (the route walk stops at its body).
        rep, _text = _run_one(watcher, who, actq.Step("move_to", {"target": a["target"], "within": gap[1]}))
        _after(rep, after)
        lr = rep.last_result if isinstance(rep.last_result, dict) else {}
        if not rep.done or lr.get("in_reach") is False:
            # S042 r4: Karlach's melee at 2.7/4.0/3.2 m failed ×3 with 12 m of movement left and no word on why the
            # step closer didn't happen: name it on the failed line
            _step_note["why"] = (re.sub(r"^\S+ ", "", _text.split("\n")[0])[:140] if not rep.done
                                 else f"the step ended {render.num(lr.get('dist', '?'))}m away, still out of reach")
            return ""
        return "stepped next to it" if not lr.get("already_there") else ""
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
    if args.get("target"):
        # S032 17:45: "out of range (19.6 m, range 18.0 m) → stepped 2.2m into range → FAILED — no line of
        # sight": the route point in range was behind cover. A spot in range AND in sight (nav.los_spot) first.
        sp = game.try_rpc("nav.los_spot", {"char": who, "target": args["target"], "range": args["within"],
                                           "avoid": True})
        if isinstance(sp, dict) and sp.get("found") and sp.get("pos"):
            x, y, z = sp["pos"]
            rep, _text = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
            _after(rep, after)
            return f"stepped {render.num(sp.get('walk_m', '?'))}m into range and sight" if rep.done else ""
    ap = game.try_rpc("nav.approach", args)  # budget: the movement left (the op's default in combat)
    if not (isinstance(ap, dict) and ap.get("reachable") and ap.get("limit") == "in_range" and ap.get("pos")):
        return ""
    x, y, z = ap["pos"]
    rep, _text = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
    _after(rep, after)
    return f"stepped {render.num(ap.get('walk_m', '?'))}m into range" if rep.done else ""


def _step_into_sight(who: str, step: actq.Step, text: str, watcher: actq.Watcher, after: dict) -> str:
    """combat_turn: a cast refused 'no line of sight' (S018: 14 casts, pillars in the Shattered Sanctum, a
    downed ally behind vines) walks to the nearest spot, within the movement left and the ability's range,
    from which a sight ray reaches the target (nav.los_spot); the caller then casts again, and the engine's
    own line-of-sight check decides. Returns what it did ('stepped 2m to see it'), or '' (another failure,
    a melee ability, no such spot, the walk failed)."""
    if step.tool != "use_ability" or "line of sight" not in text.lower() or not step.args.get("target"):
        return ""
    try:
        tu = _uuid(str(step.args["target"]))
    except ValueError:
        return ""
    acts = game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    rng = next((a.get("range") for a in acts or [] if isinstance(a, dict)
                and a.get("spell") == step.args.get("ability")), None)
    if not isinstance(rng, (int, float)):
        return ""
    if rng <= turn.MELEE_RANGE_M:
        # S033 18:49: Cure Wounds and Help on downed Shadowheart 1.5 m away: "no line of sight" twice, the turn
        # lost, Claude downed next. A touch cast already in reach looks for a spot beside it that sees it;
        # out of reach, walking next to it is the move's job (out of range → step in).
        d = turn.straight_dist(game.try_rpc("state.combat") or {}, who, tu)
        if d is None or d > rng + turn.MELEE_REACH_PAD:
            return ""
    sp = game.try_rpc("nav.los_spot", {"char": who, "target": tu, "range": max(1.0, float(rng) - 0.5),
                                       "avoid": True})
    if not (isinstance(sp, dict) and sp.get("found") and sp.get("pos")):
        return ""
    x, y, z = sp["pos"]
    rep, _text = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
    _after(rep, after)
    return f"stepped {render.num(sp.get('walk_m', '?'))}m to see it" if rep.done else ""


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
    res, then_move = _turn_slot(f"{slot} (auto fallback)", alts, who, watcher, lines, after, info)
    if res == "ok" and then_move and p.move:
        res = _turn_move(p.move, who, watcher, lines, after, info)
    return res


def _retarget_lost(p: turn.TurnPlan, who: str, lines: list, info: dict) -> bool:
    """combat_turn: the move's target is dead or out of sight (acceptance 13:3x: a Shadow vanished into
    Shadow Blend during Claude's walk; both attacks were skipped 'not visible', then the Dash walked
    nowhere and the turn ended with 9 m unused). Re-aim the move and every alternative aimed at it at the
    nearest visible living hostile and say so; none in sight: say so and mark info["move_lost"] (a Dash
    that would walk on is skipped). True when the plan was re-aimed. One state.combat read (left for the
    next step check)."""
    try:
        lost = _uuid(str(p.move.target))
    except ValueError:
        return False
    cs = _turn_cs.get("cs") or game.try_rpc("state.combat") or {}
    _turn_cs["cs"] = cs
    if not cs.get("active"):
        return False
    row = next((o for o in cs.get("order") or [] if o.get("uuid") == lost), None)
    if row is not None:
        if not row.get("dead"):
            return False
        why = "is dead"
    else:
        ts = game.try_rpc("act.target_state", {"uuid": lost}) or {}
        if not isinstance(ts, dict) or (ts.get("visible") and not ts.get("dead")):
            return False
        why = "is dead" if ts.get("dead") else "is out of sight"
    near = turn.hostiles_in_reach(cs, who, 1e6, {lost})
    if not near:
        said = info.get("move_lost") == f"{_label(lost)} {why}"
        info["move_lost"] = f"{_label(lost)} {why}"
        if not said:  # S028: the same line came 3 times in one turn (move, action, bonus)
            lines.append(f"target: {_label(lost)} {why} and no other hostile is in sight: "
                         "its move and attacks are skipped")
        return False
    new = near[0]
    new_h = H.get(new)
    p.move.target = new_h
    for alts in [p.slots.get("action") or [], p.slots.get("bonus") or []] + list(p.slots.get("extra") or []):
        for a in alts:
            t = a.step.args.get("target")
            try:
                if t and _uuid(str(t)) == lost:
                    a.step.args["target"] = new_h
            except ValueError:
                pass
    d = turn.straight_dist(cs, who, new)
    info.pop("move_lost", None)
    if (p.move.within is None or p.move.within <= turn.MELEE_RANGE_M) and d is not None \
            and d > (p.move.within or turn.MELEE_M) + turn.MELEE_REACH_PAD:
        info["melee_out"] = {"target": new, "dist": d}  # a walk on (movement left) re-checks it
    lines.append(f"target: {_label(lost)} {why} → the plan now aims at the nearest visible hostile "
                 f"{_label(new)}" + (f" ({render.num(d)}m)" if d is not None else "") + " (retarget:false keeps it)")
    return True


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


def _downed_text(u: str) -> str:
    return (f"{_label(u)} is downed: nothing to play, its turn passes by itself (the game rolls its death "
            "saving throw). Get it up from an ally's turn: Healing Word, Help (adjacent) or a potion.")


def _wait_turn(ready, max_s: float, blocked=None, on_prompt=None, extend_s: float = 0.0) -> tuple[dict, str]:
    """combat_turn sent ahead (planned while enemies act): poll until ready(combat) or combat ends, a prompt
    needs an answer, blocked(combat) names why the turn can't come (another party member must act first),
    or max_s passes. on_prompt(ui) answers a prompt itself (returns True: keep waiting). Past max_s it waits up
    to extend_s more while the enemies' turns still move on (a turn changed in the last TURN_PROGRESS_S).
    (last state.combat, why it stopped waiting or '')."""
    t0 = time.monotonic()
    deadline = t0 + max_s
    n = 0
    seen, moved = None, float("-inf")  # moved: when the acting combatant last changed during the wait
    while True:
        cs = game.try_rpc("state.combat") or {}
        if not cs.get("active") or ready(cs):
            return cs, ""
        key = (cs.get("round"), tuple(turn.acting(cs)))
        if key != seen:
            if seen is not None:
                moved = time.monotonic()
            seen = key
        why = blocked(cs) if blocked else ""
        if why:
            return cs, why
        n += 1
        if n % 3 == 0:
            ui = game.try_rpc("ui.state", context="client") or {}
            if ui.get("screen") in _PROMPT_SCREENS:
                if on_prompt and on_prompt(ui):
                    continue
                return cs, _prompt_why(ui)
        t = time.monotonic()
        if t > deadline:
            # S031 17:04: a 30 s wait timed out mid enemy phase (8 ghosts), the plan came back "not played" and
            # was sent again (another 5 s of thinking + 34 s of waiting). Enemies still taking turns: wait on.
            if not (t - moved < TURN_PROGRESS_S and t < t0 + max_s + extend_s):
                return cs, "timed out"
        time.sleep(0.3)


def _prompt_why(ui: dict) -> str:
    """Why the wait for the turn stopped on a prompt. S029: 5 sent-ahead turns stopped on 'reaction on screen'
    (Shield Blow on an enemy's attack); each cost get_dialogue + react + the same combat_turn again."""
    why = f"{ui.get('screen')} on screen"
    rx = ui.get("reaction") if ui.get("screen") == "reaction" else None
    if isinstance(rx, dict):
        prompt = str(rx.get("prompt") or "").replace(" | Choose how to react.", "")
        why += (f": {H.scrub(prompt)}" if prompt else "") + \
            ' — answer it with react(use=true|false), then send the plan again; or put "on_reaction":"use"|"skip"' \
            " in the plan to answer such prompts while it waits"
    return why


def _answer_reaction(use: bool) -> str:
    r = _watch_conn().run_job("ui.react", {"use": use}, context="client", wait_s=10)
    return f"reaction prompt: {'used' if use else 'declined'} ({r.status})"


def _turn_next(who: str, party: set) -> str:
    """After the turn: who acts next, and the next party member's actions if it is already their turn."""
    cs = game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        _acts_shown.clear()
        _light_noted.clear()
        return "combat is over: loot, heal, then end_combat_report"
    others = [u for u in turn.party_acting(cs, party) if u != who]
    cur = others[0] if others else None
    if cur:
        out = f"NOW {_label(cur)}'s turn: plan it"
        if len(others) > 1:
            out += f"; also {', '.join(_label(u) for u in others[1:])}"
        acts = game.try_rpc("state.actions", {"uuid": cur})
        _learn_actions(cur, acts)
        if acts:
            out += "\nYOUR ACTIONS (" + H.get(cur) + ")" + _actions_delta(cur, acts)
        return out
    text, mine = turn.next_up(cs, party, _label, ended=who)
    if mine:
        text += ". Plan it while they act and send its combat_turn now (it waits for the turn)"
    return text


@mcp.tool()
@_guard
def combat_turn(plan: str, who: str | None = None, end: bool = True, wait_next: bool = False,
                ignore: str = "hostile", wait_turn: float = 30.0) -> str:
    """A WHOLE combat turn in one call — or the turns of SEVERAL party members acting now: plan = JSON list
    [{"who":"Karlach","plan":{...}},{"who":"Gale","plan":{...}}] plays them in order (each as below, each turn ended);
    a later member's plan waits for its turn like a sent-ahead plan (the chain waits ≤80 s in all); a member that already ended its turn is skipped, and one the game wants first is played first; it stops and
    hands back as soon as a turn is interrupted, a member's turn doesn't come in time, or the fight ends, and lists
    who was NOT played. Use it when the result
    says `NOW <a>'s turn: plan it; also <b>, <c>` (S023-S026: 175 back-to-back combat_turn calls, one per member).
    Single turn:  move, action, bonus action, extra attacks, end turn. Each slot lists alternatives tried in order until one works (out of range after the move → the ranged attack; no ranged → Dash; else Disengage). If every action alternative is out of range, it tries the ranged weapon attack itself, else Dash + walk on when the move ran out of movement. A move that ends out of reach says how far it got and why (out of movement / fire on the route / no route closer).
    plan: JSON, e.g. {"move":{"target":"e12","within":1.5},"action":[{"ability":"Target_MainHandAttack","target":"e12"},{"ability":"Projectile_MainHandAttack","target":"e12"},{"ability":"Shout_Dash","then_move":true},"Shout_Disengage"],"bonus":{"ability":"Shout_SecondWind","if":"hp_below:50"},"extra":[[{"ability":"Target_MainHandAttack","target":"e12"}]]}
    move: "e12" (walk next to it), {"target","within":<m>} (stop once that close, e.g. ranged), [x,y,z]. Walks the real route (stairs, detours). If the walk doesn't fit the movement left it walks as far as it does (partial:false turns that off); it keeps out of fire on the way (avoid:false walks through).
    Abilities: the shown name or the id; a weapon attack the character doesn't have (a summon's) becomes its own weapon attack. Extra Attack: after the first weapon attack, an "extra" slot attack costs no action (the game's rule).
    Alternatives: an ability id, or {"ability","target"|"x,y,z","upcast","targets","force","if","unless","then_move"}, or {"tool":<batchable tool>,...}. then_move: after it, walk the move again (a Dash does this by default; then_move:false turns it off).
    order: "move,action,bonus,extra" (default; e.g. "bonus,move,action" for Misty Step first). reactions: {"Divine Smite":"ask"} sets prompts first; on_reaction: "use"|"skip" answers reaction prompts mid-turn and while a sent-ahead plan waits (default: stop).
    No line of sight to a ranged target: it first steps (within the movement left and the ability's range) to the nearest spot that sees it, then casts again. A downed character has no turn to play (it passes by itself); another of your characters acting first makes a sent-ahead plan return at once; your summon (Spiritual Weapon …) acting first and not in the plan: its turn is played for it (its weapon attack on the plan's first target, else the nearest hostile) and then the plan.
    Before each step: still this character's turn? target still visible and alive? (else the next alternative). Stops on the usual interrupts (combat/dialogue/prompt/hp/teleport; ignore="" adds new hostiles in view).
    A slot whose every alternative missed (target dead, out of range, no line of sight) tries the plan's own targeted abilities on the nearest other visible hostile in reach (retarget:false turns it off). A melee move that ends out of reach says "melee is out of reach this turn" and skips the melee attacks on that target (ranged / Dash next). The move's target dead or out of sight (Shadow Blend, killed first): the plan is re-aimed at the nearest visible hostile, and walks on if movement is left (retarget:false keeps it). No bonus planned and an ally downed: Healing Word on them if this character has it (auto_bonus:false turns it off).
    An empty plan {} just ends that character's turn (e.g. it already acted through single tools).
    end=true ends the turn and returns at once with who acts next, so you can plan the next turn while enemies act (wait_next=true blocks until a party member's turn).
    Not this character's turn yet (enemies still acting): send the plan anyway; it waits up to wait_turn s for the turn (stops on a prompt), up to 30 s longer while enemies are still taking turns, then plays it."""
    chain = _turn_chain(plan)
    if chain is None:
        end = _plan_end(plan, end)
        try:
            return _combat_turn_one(plan, who, end, wait_next, ignore, wait_turn, extend_s=TURN_EXTEND_S)
        except (GameError, ValueError) as e:
            auto = _summon_first(str(e), plan, ignore)
            if auto is None:
                raise
        return auto + "\n\n" + _combat_turn_one(plan, who, end, wait_next, ignore, wait_turn, extend_s=TURN_EXTEND_S)
    if not end:
        raise ValueError("a list of member plans ends each turn: send end=false plans one at a time")
    outs: list[str] = []
    played = 0
    stop = ""
    skipped: list[str] = []
    swaps = 0
    c0 = time.monotonic()
    i = 0
    while i < len(chain):
        w, pl = chain[i]
        # each member waits for its turn like a sent-ahead plan does, all within CHAIN_BUDGET_S (MCP timeout 110 s)
        wt = wait_turn if i == 0 else max(0.0, min(wait_turn, CHAIN_BUDGET_S - (time.monotonic() - c0)))
        try:
            txt = _combat_turn_one(pl, w, True, wait_next and i == len(chain) - 1, ignore, wt,
                                   extend_s=TURN_EXTEND_S if i == 0 else 0.0)
        except (GameError, ValueError) as e:
            why = re.sub(r'\[string "[^"]*"\]:\d+: ', "", str(e))
            if "already ended its turn" in why:
                # S028: [Karlach, Claude, Gale, Shadowheart] with Karlach already done stopped the whole chain
                # (0 of 4 played, a re-plan round trip). Her turn is over: skip her, play the rest.
                skipped.append(f"{w}: skipped — {why.split(' (now ')[0]}")
                chain.pop(i)
                continue
            first = _chain_first(why, chain[i + 1:])
            if first is not None and swaps < len(chain):
                # a later member of this very list must act first (the game's turn order): play it first
                swaps += 1
                chain.insert(i, chain.pop(i + 1 + first))
                continue
            auto = _summon_first(why, pl, ignore) if swaps < len(chain) else None
            if auto is not None:
                swaps += 1
                outs.append(auto)
                continue
            stop = f"{w}: not played — {why}"
            break
        outs.append(txt)
        played += 1
        i += 1
        if _batch.get("last") == "interrupted":
            stop = f"{w}'s turn was interrupted (above)"
            break
        if txt.startswith("combat_turn: nothing to play") or "combat is over" in txt:
            stop = "the fight is over"
            break
    # one clock line, at the end
    outs = [re.sub(r"\n⏱[^\n]*$", "", o) for o in outs[:-1]] + outs[-1:]
    if skipped:
        outs.insert(0, "\n".join(skipped))
    if played < len(chain):
        rest = [w for w, _ in chain[played:]]
        outs.append(f"CHAIN STOPPED after {played} of {len(chain)}: {stop or 'stopped'}. NOT PLAYED: {', '.join(rest)}"
                    " — plan them from this state (combat_turn again).")
    return "\n\n".join(outs)


def _plan_end(plan: str, end: bool) -> bool:
    """S041 22:36:18: {"end": false} written inside a single plan (it is combat_turn's argument) failed the whole call
    'unknown plan key(s) end'. Inside a single plan it means the same as the argument (turn.parse_plan drops it;
    a chain's member plans always end their turns)."""
    try:
        v = json.loads(plan).get("end") if isinstance(plan, str) else None
    except (ValueError, AttributeError):
        return end
    return v if isinstance(v, bool) else end


CHAIN_BUDGET_S = 80.0  # a list of member plans: total time the later members may wait for their turns
TURN_EXTEND_S = 30.0  # the first (or only) plan: extra wait past wait_turn while enemy turns still progress
TURN_PROGRESS_S = 10.0  # ... "progress" = the acting combatant changed this recently


def _summon_first(why: str, plan: str, ignore: str) -> str | None:
    """A turn refused because our own summon (Spiritual Weapon …) must act first and the plan has no turn for it
    (S035: 3 chains stopped 'now e510 Spiritual Weapon', up to 21.7 s waited, a re-send each): play the summon's
    turn as a player would — its weapon attack on the plan's first target (the nearest visible hostile when that
    one is gone or out of sight), else end it — then the caller retries. None: not a summon of ours."""
    m = re.search(r"\(combat_turn who=([^)\s]+)\)", why)
    if not m:
        return None
    try:
        su = _uuid(m.group(1))
    except ValueError:
        return None
    cs = game.try_rpc("state.combat") or {}
    row = next((o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("uuid") == su), None)
    if not row or not row.get("summon"):
        return None
    hostile = {o.get("uuid") for o in cs.get("order") or [] if isinstance(o, dict)
               and o.get("relation") == "hostile" and not o.get("dead")}
    tgt = None
    for t in re.findall(r'"target"\s*:\s*"([^"]+)"', plan or ""):
        try:
            u = _uuid(t)
        except ValueError:
            continue
        if u in hostile:   # never a heal's target (an ally)
            tgt = u
            break
    if tgt is None:
        here = row.get("pos")
        foes = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("relation") == "hostile"
                and not o.get("dead") and o.get("pos") and here]
        if foes:
            tgt = min(foes, key=lambda o: sum((a - b) ** 2 for a, b in zip(o["pos"], here)))["uuid"]
    sp = ({"move": {"target": H.get(tgt), "within": 1.5},
           "action": [{"ability": "Target_MainHandAttack", "target": H.get(tgt)}], "on_reaction": "skip"}
          if tgt else {})
    txt = _combat_turn_one(json.dumps(sp), H.get(su), True, False, ignore, 5.0)
    return f"{_label(su)} (your summon) had to act first and wasn't in the plan: played it (weapon attack on " \
           f"the plan's target, else ended)\n" + txt


def _chain_first(why: str, later: list[tuple[str, str]]) -> int | None:
    """A member's turn refused because '<eX> (yours) must act first: play that turn (combat_turn who=eX)':
    the index in later of the member that is eX, or None."""
    m = re.search(r"\(combat_turn who=([^)\s]+)\)", why)
    if not m:
        return None
    try:
        first = _uuid(m.group(1))
    except ValueError:
        return None
    for j, (w, _pl) in enumerate(later):
        try:
            if _who_uuid(w) == first:
                return j
        except ValueError:
            continue
    return None


def _turn_chain(plan: str) -> list[tuple[str, str]] | None:
    """combat_turn's list form → [(who, plan JSON)]; None for a single plan."""
    if not isinstance(plan, str) or not plan.lstrip().startswith("["):
        return None
    try:
        items = json.loads(plan)
    except ValueError:
        return None
    if not isinstance(items, list) or not items or not all(isinstance(x, dict) and x.get("who") and "plan" in x
                                                            for x in items):
        raise ValueError('a list plan is [{"who": "<member>", "plan": {...}}, ...] (one entry per member acting now)')
    return [(str(x["who"]), x["plan"] if isinstance(x["plan"], str) else json.dumps(x["plan"])) for x in items]


def _combat_turn_one(plan: str, who: str | None, end: bool, wait_next: bool, ignore: str, wait_turn: float,
                     extend_s: float = 0.0) -> str:
    t0 = time.monotonic()
    if _session_over():
        return _finish("combat_turn", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    p = turn.parse_plan(plan, _BATCHABLE)
    cs = game.rpc("state.combat")
    _remember_names(cs)
    st0 = game.try_rpc("state.status") or {}  # one read for the party and the selected character
    party = {c.get("uuid") for c in st0.get("party") or [] if isinstance(c, dict)}
    if not cs.get("active"):
        # S025: sent as the last enemy died; nothing to play is not a failure. Opening a fight is a plain attack.
        return _finish("combat_turn", {"who": who}, "combat_turn: nothing to play — not in combat (combat is over, "
                       "or start a fight with use_ability / do_actions)", t0)
    now = turn.acting(cs)
    waited = ""
    want = _who_uuid(who) if who else None
    if want and want in turn.downed(cs):
        return _finish("combat_turn", {"who": who}, _downed_text(want), t0)
    ready = (lambda c: want in turn.acting(c)) if want else (lambda c: bool(turn.party_acting(c, party)))

    def blocked(c: dict) -> str:
        if want and want in turn.downed(c):
            return f"{_label(want)} is downed"
        first = turn.blocking(c, party, want) if want else []
        if not first:
            return ""
        return (", ".join(_label(u) for u in first) + " (yours) must act first: play that turn "
                f"(combat_turn who={H.get(first[0])}) or end it (end_turn who={H.get(first[0])})")

    answered: list[str] = []

    def on_prompt(ui: dict) -> bool:
        # the plan's on_reaction also answers the prompts that come while it waits (an enemy's attack → Shield
        # Blow / Hellish Rebuke), like the game's "always"/"never" setting; anything else still stops the wait
        if not p.on_reaction or ui.get("screen") != "reaction" or len(answered) >= 6:
            return False
        try:
            answered.append(_answer_reaction(p.on_reaction == "use"))
        except Exception as e:  # noqa: BLE001 - an unanswerable prompt stops the wait after all
            answered.append(f"reaction: could not answer ({e})")
            return False
        return True

    if not ready(cs) and wait_turn > 0:
        w0 = time.monotonic()
        cs, why = _wait_turn(ready, wait_turn, blocked, on_prompt, extend_s)
        _remember_names(cs)
        if not cs.get("active"):
            raise GameError("combat is over: loot, heal, then end_combat_report")
        now = turn.acting(cs)
        waited = f"waited {time.monotonic() - w0:.1f}s for the turn" + (f" ({why})" if why else "")
        if answered:
            waited += " — " + "; ".join(answered)
        if want and want in turn.downed(cs):
            return _finish("combat_turn", {"who": who}, _downed_text(want), t0)
    mine = turn.party_acting(cs, party)
    if who:
        char = want
    else:
        sel = st0.get("active") if not waited else None
        if not sel:
            sel = _active()
        char = sel if sel in mine else (mine[0] if mine else sel)
    if char in turn.downed(cs):
        return _finish("combat_turn", {"who": who}, _downed_text(char), t0)
    if char not in now:
        ended = any(o.get("uuid") == char and o.get("ended") for o in cs.get("order") or [])
        why = f"{_label(char)} already ended its turn" if ended else f"it is not {_label(char)}'s turn"
        raise GameError(f"{why} (now {', '.join(_label(u) for u in now) or '?'})" + (f"; {waited}" if waited else ""))
    lines = [f"TURN {_label(char)}" + (f" (round {cs['round']})" if cs.get("round") is not None else "")
             + (f" — {waited}" if waited else "") + _light_note(cs)]
    fight_hint = _fight_hint(cs, party)   # the first turn of a fight: a retry's key facts / past debriefs' warnings
    if fight_hint:
        lines.append(fight_hint)
    notes: list[str] = []
    for alt in [a for k in ("action", "bonus") for a in p.slots.get(k) or []] + \
            [a for slot in p.slots.get("extra") or [] for a in slot]:
        if alt.step.tool == "use_ability" and alt.step.args.get("ability"):
            try:  # shown names → ids first: the fallbacks below compare ids (retarget, variants, tried)
                actor = alt.step.args.get("who") and _who_uuid(alt.step.args["who"]) or char
                sp, note = _own_attack(actor, _ability_id(actor, alt.step.args["ability"]))
                alt.step.args["ability"] = sp
                if note and note not in notes:
                    notes.append(note)
            except (GameError, ValueError):
                pass  # use_ability says why when that alternative runs
    lines += notes
    if p.reactions:
        try:
            _reactions_rpc(char, {"char": char, "set": p.reactions})
            lines.append("reactions set: " + ", ".join(f"{k}={v}" for k, v in p.reactions.items()))
        except GameError as e:
            lines.append(f"reactions: FAILED — {e}")
    watcher = turn.TurnWatcher(_watch_conn(), p.on_reaction, _answer_reaction, ignore=_parse_ignore(ignore))
    watcher.baseline()
    after: dict = {}
    info: dict = {}
    stopped = False
    _turn_cs["cs"] = cs  # nothing ran since this read: the first step check reuses it
    moved = False
    for slot in p.order:
        if p.move and p.move.target and p.retarget and (slot == "move" or (moved and slot in ("action", "bonus"))):
            # The move's target died or went out of sight (sent ahead / during the walk): aim the plan at the
            # nearest visible hostile, and walk again if the move already ran and movement is left.
            if _retarget_lost(p, char, lines, info) and slot != "move" and moved \
                    and not (isinstance(after.get("movement"), (int, float)) and after["movement"] < 1.0):
                if _turn_move(p.move, char, watcher, lines, after, info) == "stop":
                    stopped = True
                    break
        if slot == "move":
            # a lost target (said above): no walk (S028: "move e742: FAILED — target is not visible" after it)
            res = _turn_move(p.move, char, watcher, lines, after, info) if p.move and not info.get("move_lost") \
                else "none"
            moved = True
        elif slot == "extra":
            res = "none"
            # S032 17:44: Karlach's action went to Dash, then "extra 1: Target_MainHandAttack: FAILED — not enough
            # action": Extra Attack only follows the Attack action. Say so instead of a failure.
            act_line = next((ln for ln in reversed(lines) if ln.startswith("action")), "")
            spent = turn.action_without_attack(act_line) if p.slots.get("action") else None
            if spent and p.slots["extra"]:
                lines.append(f"extra: skipped (the action went to {spent}; Extra Attack only follows an attack)")
                continue
            # S042 round 4: the bonus slot's auto fallback fired a weapon attack that was the free Extra Attack
            # ("bonus (auto fallback): Projectile_MainHandAttack e381: done … extra_attack"), then "extra 1: … FAILED —
            # not enough action" ×3. An Extra Attack already taken this turn uses up the first extra slot.
            used = [ln.split(":")[0] for ln in lines[1:] if not ln.startswith("extra")
                    and re.search(r": done\b[^\n]*\bextra_attack\b", ln)]
            extras = list(enumerate(p.slots["extra"], 1))
            if used and extras:
                lines.append(f"extra 1: skipped (the Extra Attack already went to the {used[0]} attack)")
                extras = extras[len(used):]
            for i, alts in extras:
                res, _ = _turn_slot(f"extra {i}", alts, char, watcher, lines, after, info)
                if (res == "none" and alts and lines and lines[-1].startswith(f"extra {i}:")
                        and turn.missed_target(lines[-1])):
                    res = _turn_auto_fallback(p, f"extra {i}", alts, char, watcher, lines, after, info)
                if res == "stop":
                    break
        else:
            res, then_move = _turn_slot(slot, p.slots[slot], char, watcher, lines, after, info)
            if res == "ok" and then_move and p.move:
                res = _turn_move(p.move, char, watcher, lines, after, info)
            elif (res == "none" and slot in ("action", "bonus") and p.slots[slot] and lines
                  and lines[-1].startswith(f"{slot}:") and turn.missed_target(lines[-1])):
                res = _turn_auto_fallback(p, slot, p.slots[slot], char, watcher, lines, after, info)
            if res == "none" and slot in ("action", "bonus") and lines and _unknown_ability_line(lines[-1], slot):
                # S035 19:33: action ["Shout_Dodge"] → "unknown action" (refused, nothing spent), then end_turn threw
                # Wyll's action away. A wrong id/name is a plan slip: keep the turn open for the real choice.
                if "Dodge" in lines[-1]:
                    lines.append("BG3 has no Dodge action (5e only): Shout_Disengage, Shout_Dash, Shout_Hide, or an attack")
                res = "stop"
        _turn_cs.clear()
        if res == "stop":
            stopped = True
            break
    _turn_cs.clear()
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
    elif stopped and char in turn.downed(game.try_rpc("state.combat") or {}):
        lines.append(f"{_label(char)} went down: its turn passes by itself (death saving throws); "
                     "get it up with Healing Word / Help from an ally")
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


_IN_CONVO = "in a conversation"  # Guard's refusal: "character is in a conversation (InDialog)"
CONVO_WAIT_S = 4.0  # a step refused for a conversation with none on screen: retry this long (banter, a stale flag)


def _retry_after_dialogue(watcher: actq.Watcher, who: str | None, step: actq.Step, rep: actq.Report,
                          text: str) -> tuple[actq.Report, str, str | None]:
    """A macro step refused "in a conversation". Live S021: loot_area right after a fight hit Karlach's
    post-combat conversation and failed all 4 corpses the same way. A dialogue on screen is the agent's
    to answer: stop the macro at once (why). None on screen (ambient banter, a flag a moment stale after a
    dialogue ended): retry the step until the game lets it, up to CONVO_WAIT_S."""
    end = time.monotonic() + CONVO_WAIT_S
    while True:
        ui = game.try_rpc("ui.state", context="client") or {}
        if isinstance(ui, dict) and ui.get("screen") == "dialogue":
            return rep, text, "a conversation is open: answer it (choose_dialogue), then run this again"
        if time.monotonic() >= end:
            return rep, text, (f"still in a conversation after {CONVO_WAIT_S:g}s with no dialogue on screen "
                               f"({text.split(': ', 1)[-1]})")
        time.sleep(0.4)
        rep, text = _run_one(watcher, who, step)
        if rep.done or rep.stop_kind not in (None, "failed") or _IN_CONVO not in text:
            return rep, text, None


_LOOT_HARD_STOPS = {"combat", "dialogue", "prompt"}  # _macro_groups: these stop before an open loot window's take


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
            if not rep.done and rep.stop_kind in (None, "failed") and _IN_CONVO in text:
                rep, text, why = _retry_after_dialogue(watcher, who, step, rep, text)
                if why:
                    lines.extend([f"{label}: " + "; ".join(parts)] if parts else [])
                    return lines, why, [g[0] for g in groups[gi:]]
            parts.append(text)
            if rep.stop_kind not in (None, "failed"):
                rest = steps[steps.index(step) + 1:]
                if rep.done and rest and rest[0].tool == "loot" and rep.stop_kind not in _LOOT_HARD_STOPS:
                    # S042 22:51:18: the corpse's loot window was OPEN when a 2 HP tick stopped loot_area; it
                    # closed itself unlooted. A player takes what's in the open window first (one click), then reacts.
                    quiet = actq.Watcher(_watch_conn(), ignore=set(actq.INTERRUPT_KINDS))
                    quiet.baseline()
                    rep2, text2 = _run_one(quiet, who, rest[0])
                    parts.append(text2)
                    if rep2.done and done is not None and len(rest) == 1:
                        done.append(gi)
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
              wait: bool = False, avoid_hazards: str = "auto", fast: str = "auto", note: str = "") -> str:
    """Get somewhere far in one call: your map marker (m1 or words of its label), a quest objective on this level (o1), a visible target, x,y,z, or an unlocked waypoint (fast travel). Walks the game's own route in one go when the whole way is plain walking (long trips in ≤55 m legs), else chains walks of up to 30 m along walkable ground toward it, around harmful surfaces you can see (fire, acid, ...); a leg that fails on the ground (no path, blocked) is retried another way (along the route, or shorter; twice per trip); stops on arrival (within 3 m), when no walkable ground leads further, after 2 legs without progress, or at the first interrupt (combat, conversation, prompt, new hostile, any damage). Stepping into a surface: it steps out (forward if it can) and goes on.
    avoid_hazards: auto (default) = when no clear way leads on, it takes the shortest crossing (says e.g. 'crossing 1.5m of Fire (~1-4 dmg + Burning)') unless HP is too low for the worst case; strict = never crosses; cross = crosses even at low HP (never lava/chasms, never >6 m).
    Returns within ~1 s with live progress (m left, ETA), or the outcome if the trip already ended: the trip runs as the first entry of the character's action queue, so queue_actions appends what to do on arrival while you observe and plan. Every later result shows it RUNNING, then queue_done or queue_interrupted (why). wait is ignored (the trip never blocks).
    fast: auto (default) = a trip of 120 m+ fast travels first to an unlocked waypoint of this level that is much nearer the goal (says so); never = always walk.
    A trip that stops twice at the same blocked spot says "tried 2x: blocked" and names another way (a waypoint, another side); route() shows the dead ends, unlocked waypoints and the closest spot you have been.
    note: memory log line(s) appended to your session log at once, as note() does: write notes with the walk, no extra turn."""
    t0 = time.monotonic()
    _note_quiet(note)
    if waypoint:
        return fast_travel(waypoint)
    policy = (avoid_hazards or "auto").strip().lower()
    if policy not in macros.AVOID_POLICIES:
        raise ValueError("avoid_hazards must be auto, strict or cross")
    w = _no_combat("travel_to")
    char = _who(who)
    seen_txt = ""
    if target:  # a far target is walked in legs to where it stands (one move_to to it is capped at MOVE_CAP_M)
        here = next((r.get("pos") for r in w.get("party") or [] if r.get("uuid") == char), None)
        tp = _ent_pos(_uuid(target))
        seen = None if tp else _last_seen(_uuid(target))
        if seen:  # out of sight now: walk to where the party last saw them (S025/S026 "target is not visible")
            seen_txt = _seen_line(target, seen) + "\n"
            (x, y, z), target = seen[0], ""
        elif here and tp and macros.dist(here, tp) > macros.TRAVEL_LEG_M:
            x, y, z, target = tp[0], tp[1], tp[2], ""
    if not _batch["active"]:  # wait=true too: the agent plans while the character walks
        return seen_txt + _travel_async(char, w, marker, objective, target, x, y, z, t0, policy, waited=wait,
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
        blocks = 0  # legs re-run after someone in the way moved (or was stepped aside)
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
            # A leg along the engine's route may head away from the goal first (S029: a 325 m route, 2.4x the
            # straight line, began SE of a NW goal): it gains route metres, not straight-line ones.
            along = bool(isinstance(r, dict) and r.get("direct") and r.get("route_left_m") is not None
                         and prog.route_gain(r["route_left_m"]))
            if not (isinstance(r, dict) and r.get("reachable") and r.get("pos")) or (
                    macros.dist(r["pos"], dest) > d - 1.0 and not along):
                stop = (f"no walkable ground leads further toward it ({render.num(round(d, 1))}m left"
                        + (", no other leg either" if failed_end is not None else "")
                        + "): try get_frontier, a door, or a jump")
                if isinstance(r, dict) and r.get("hazard_beyond"):
                    stop += f"; ahead: {macros.hazard_text(r['hazard_beyond'])}"
                hid = r.get("hidden_floor") or r.get("hidden_beyond") if isinstance(r, dict) else None
                if hid:  # S035: a walk onto the Faith-Leap chasm's invisible floor killed Claude
                    stop = (f"the way on is the chasm, with no floor you can see ({_hidden_text(hid)};"
                            f" {render.num(round(d, 1))}m left): on an invisible-floor trial walk in short steps"
                            " (move_to ≤ 3m) along the mosaic's lines (get_layout)")
                break
            # A plain sampled leg that gains only a few metres on a long trip (acceptance: 30 m, 2 m, 29 m legs; the
            # sampler stopped at an obstacle edge): one verified alternative leg first, kept when it gets clearly further.
            if (failed_end is None and not r.get("direct") and not r.get("exposure") and not r.get("detour")
                    and d > macros.TRAVEL_MIN_GAIN_M + macros.TRAVEL_ARRIVE_M
                    and d - macros.dist(r["pos"], dest) < macros.TRAVEL_MIN_GAIN_M):
                a2 = game.try_rpc("nav.reach_toward", {**args, "alt": True, "exclude": r["pos"]})
                if (isinstance(a2, dict) and a2.get("reachable") and a2.get("pos") and not a2.get("exposure")
                        and macros.dist(a2["pos"], dest) < macros.dist(r["pos"], dest) - 2.0):
                    r = a2
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
            if (macros.leg_stopped_short(res) and blocks < BLOCKER_TRIES and "blocked" in text
                    and macros.dist(here, r["pos"]) > macros.TRAVEL_ARRIVE_M):
                note, cleared = _clear_blocker(char, here, r["pos"], outer)
                if note:
                    lines.append(note)
                    blocks = blocks + 1 if cleared else BLOCKER_TRIES  # a blocker that stayed put stays put: once
                if cleared:
                    continue  # the same leg again, not a stall
            short = macros.leg_stopped_short(res)
            why = prog.leg(here, short, route_left=r.get("route_left_m") if r.get("direct") and not short else None)
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
        # Every ground stop (no ground ahead, stalled legs, a blocked leg) names the way up/down: the stall stops
        # never did, so the ladder was named only by some stops (se 12:3x).
        stop += _climb_hint(_pos_now(char) or here, dest)
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
    if _at_camp(game.try_rpc("ui.state", context="client")):
        # Logs pass 4 #5: at the Campsite companions idle at their tents and never follow: every camp walk (59 m)
        # stopped "e4 Karlach 37m … can't keep up" after a 15 s walk back. A camp walk is the leader's own.
        return set()
    rows = party.apart(st, stay=_stay)
    took = _rechain(party.rechain_candidates(rows, _member_busy), char)
    if took:
        lines.append("chained to follow: " + ", ".join(_label(u) for u in took))
    other = {r["uuid"] for r in rows if r["other_level"]}
    return {r.get("uuid") for r in st.get("party") or []
            if r.get("uuid") != char and (r.get("chained") is True or r.get("uuid") in took)
            and r.get("uuid") not in _stay and r.get("uuid") not in other
            and not (r.get("dead") or r.get("off_stage") or r.get("downed") or r.get("summon"))}


BLOCKER_TRIES = 2      # travel_to: legs re-run per trip after someone in the way moved
BLOCKER_AHEAD_M = 4.0  # who counts as in the way: near the first metres of the leg ...
BLOCKER_NEAR_M = 1.6   # ... within this of that line (a doorway is ~1.5-2 m wide)
BLOCKER_WAIT_S = 3.0   # an NPC in the way gets this long to move on
BLOCKER_STEP_M = 3.0   # a companion in the way is stepped back this far behind the walker


def _in_the_way(here: list, toward: list, rows: list[dict], skip: str) -> list[dict]:
    """Characters standing on the first BLOCKER_AHEAD_M of the walk from here toward `toward` (flat distance to
    that line under BLOCKER_NEAR_M, same floor), nearest first."""
    dx, dz = toward[0] - here[0], toward[2] - here[2]
    n = (dx * dx + dz * dz) ** 0.5
    if n < 0.1:
        return []
    ux, uz = dx / n, dz / n
    out = []
    for r in rows:
        p = r.get("pos")
        if r.get("uuid") == skip or not (isinstance(p, (list, tuple)) and len(p) >= 3) or r.get("dead"):
            continue
        if abs(p[1] - here[1]) > 2.0:
            continue
        t = (p[0] - here[0]) * ux + (p[2] - here[2]) * uz
        if not 0.0 <= t <= BLOCKER_AHEAD_M:
            continue
        off = abs((p[0] - here[0]) * uz - (p[2] - here[2]) * ux)
        if off <= BLOCKER_NEAR_M:
            out.append(dict(r, _t=t))
    return sorted(out, key=lambda r: r["_t"])


def _clear_blocker(char: str, here: list, toward: list, outer: actq.Executor | None) -> tuple[str, bool]:
    """A leg stopped short "path blocked" right where someone stands (live 11:12, Last Light Inn: four legs
    stopped at an opened Oak Door, the party/NPCs in the doorway). A companion in the way is stepped back behind
    the walker; an NPC gets BLOCKER_WAIT_S to move on. Returns (line naming who blocked, whether to re-run the
    leg). ('', False): nobody in the way, the usual stall handling goes on."""
    watch = (_watch_conn().try_rpc("act.watch") or {}).get("party") or []
    near = (game.try_rpc("state.nearby", {"radius": BLOCKER_AHEAD_M + 2, "kinds": ["characters"]}) or {}).get("characters") or []
    party_ids = {r.get("uuid") for r in watch}
    rows = [dict(r, party=True) for r in watch] + [r for r in near if r.get("uuid") not in party_ids]
    who = _in_the_way(here, toward, rows, char)
    if not who:
        return "", False
    names = ", ".join(f"{_label(r['uuid'])}" if r.get("party") else f"{H.get(r['uuid'], r.get('name'))} {r.get('name', '?')}"
                      for r in who)
    mates = [r for r in who if r.get("party") and not _member_busy(r["uuid"])]
    if mates:
        dx, dz = toward[0] - here[0], toward[2] - here[2]
        n = max((dx * dx + dz * dz) ** 0.5, 0.1)
        back = [here[0] - dx / n * BLOCKER_STEP_M, here[1], here[2] - dz / n * BLOCKER_STEP_M]
        for r in mates:
            game.run_job("act.move_to", {"char": r["uuid"], "pos": back}, wait_s=6)
        return f"in the way: {names}: stepped back behind {_label(char)}, walking on", True
    if outer is not None and (outer.interrupt or outer.stop.is_set()):
        return "", False
    first = {r["uuid"]: r.get("pos") for r in who}
    end = time.monotonic() + BLOCKER_WAIT_S
    while time.monotonic() < end:
        time.sleep(0.3)
        near = (game.try_rpc("state.nearby", {"radius": BLOCKER_AHEAD_M + 2, "kinds": ["characters"]}) or {}).get("characters") or []
        if not _in_the_way(here, toward, [r for r in near if r.get("uuid") in first], char):
            return f"in the way: {names}: waited {render.num(round(BLOCKER_WAIT_S - (end - time.monotonic()), 1))}s for them to move, walking on", True
    return (f"in the way: {names} (stands in the path, didn't move in {render.num(BLOCKER_WAIT_S)}s): "
            "talk_to them, go around (walk_toward another direction) or wait"), False


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
    # Followers trail a running leader by more after a long leg: wait while they still close in (the gap shrinks),
    # up to CATCH_UP_MAX_S; only a member who stopped gaining is walked back.
    t_start = time.monotonic()
    end = t_start + party.CATCH_UP_S
    gap = max(d for _, d in behind)
    while behind and time.monotonic() < end and not stopped():
        time.sleep(0.25)
        behind = lag()
        now_gap = max((d for _, d in behind), default=0.0)
        if now_gap < gap - party.CATCH_UP_GAIN_M:
            gap = now_gap
            end = min(t_start + party.CATCH_UP_MAX_S, max(end, time.monotonic() + party.CATCH_UP_S / 2))
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


def _floor_ways(within: float = 1e9) -> tuple[dict | None, list[dict]]:
    """(frontier, the 2 nearest ladders/stairs/exits in view within `within` m): the ways to another floor or an
    interior. Leads only: the open-direction searches cost ~100 ms."""
    res = game.try_rpc("nav.frontier", {"ways": False})
    leads = res.get("leads") if isinstance(res, dict) else None
    ups = sorted((l for l in leads or [] if l.get("uuid") and l.get("kind") in ("ladder", "stairs", "exit")
                  and (l.get("dist") or 0) <= within), key=lambda l: l.get("dist") or 1e9)[:2]
    return res, ups


def _ways_text(res: dict | None, ups: list[dict]) -> str:
    _remember_names(res)
    return ", ".join(f"{H.get(l['uuid'], l.get('name'))} {l.get('name', '?')} <{l.get('kind')}> "
                     f"{render.num(l.get('dist', '?'))}m{(' ' + l['dir']) if l.get('dir') else ''}" for l in ups)


def _climb_hint(here: list | None, dest: list | None) -> str:
    """travel_to stopped short of a destination on another deck: name the nearest ladders/stairs/exits (the walker
    never climbs on its own; interact with the ladder does)."""
    if not (here and dest) or abs(dest[1] - here[1]) < 2.5:
        return ""
    res, ups = _floor_ways()
    side = "above" if dest[1] > here[1] else "below"
    hint = f"; it is {render.num(round(abs(dest[1] - here[1]), 1))}m {side} you"
    if not ups:
        return hint + ": find a ladder or stairs (get_frontier) or jump_to"
    return hint + f": climb with interact on a ladder/stairs/hatch ({_ways_text(res, ups)}), then travel_to again"


FLOOR_DY_M = 6.0      # travel_to: a goal this far above/below gets one route probe before the trip starts
FLOOR_WAY_M = 40.0    # ... and an entrance/ladder/stairs this near is named as the way there
FLOOR_FAR_DY_M = 20.0  # a goal too far to preview this far up/down + an entrance here = an interior (refused)
FLOOR_DETOUR_X = 3.0   # a route this many times the straight distance (and FLOOR_DETOUR_ADD_M longer) to a goal on
FLOOR_DETOUR_ADD_M = 40.0  # another floor, with a ladder/stairs in view: the trip walks, the start names the climb


def _other_floor(char: str, here: list | None, dest: list | None) -> str | None:
    """A goal on another floor or inside an interior that no walk reaches (the engine's route says so) while a way
    in/up/down is in view: the refusal naming it (None: walk as usual). Live 10:45: travel_to toward the
    Mausoleum puzzle room, 62 m below, walked 120 m the wrong way to stop at "no walkable ground", with the
    Mausoleum Entrance 17 m away. Costs one nav.route (+ a leads-only frontier) only for goals >= 6 m up/down."""
    if not (here and dest) or abs(dest[1] - here[1]) < FLOOR_DY_M:
        return None
    r = game.try_rpc("nav.route", {"char": char, "pos": list(dest), "ways": False})
    dy = abs(dest[1] - here[1])
    if not isinstance(r, dict):
        return None
    if r.get("reachable") is True:
        return _floor_detour_note(r, here, dest, dy)
    if r.get("reachable") is not False:
        # Too far for the engine to preview: an interior (the Grand Mausoleum's rooms sit ~950 m off and 69 m
        # below its gate in level coordinates; live 11:2x travel_to walked off toward them) when far below/above
        # and an entrance is right here. Else walk as before.
        if r.get("reachable") is True or not (r.get("estimate") or r.get("too_far")) or dy < FLOOR_FAR_DY_M:
            return None
    res, ups = _floor_ways(FLOOR_WAY_M)
    if r.get("reachable") is not False:
        ups = [u for u in ups if u.get("kind") == "exit"]  # an interior: only a named entrance/exit leads in
    if not ups:
        return None
    side = "above" if dest[1] > here[1] else "below"
    first = H.get(ups[0]["uuid"], ups[0].get("name"))
    why = ("no walk on this floor reaches it (inside/under/over something)" if r.get("reachable") is False
           else f"{render.num(round(macros.dist(here, dest)))}m off and too far to preview: likely inside an interior")
    return (f"REFUSED: the goal is {render.num(round(dy))}m {side} you and {why}. The way there is likely: {_ways_text(res, ups)}. "
            f"interact {first} (or queue_actions [interact {first}, travel_to ...]), then travel_to again")


def _floor_detour_note(r: dict, here: list, dest: list, dy: float) -> str | None:
    """A walkable route to a goal on another floor that winds far around (se 12:3x, Roadside Cliffs: locks 9-16 m
    below a ledge, the ladder 13 m N named only by some stops) while a ladder/stairs is in view: a note naming the
    climb (the walk still starts; never a refusal: the long way may be the only one the agent knows)."""
    length, straight = r.get("length_m"), macros.dist(here, dest)
    # The engine's route climbs/drops on its own (live 13:08: the 60 m route to a goal 10 m up went up the 11 m ladder
    # 2.6 m away, "used Ladder"): nothing to add.
    if any(isinstance(lk, dict) and lk.get("kind") in ("climb", "drop") for lk in r.get("links") or []):
        return None
    if not isinstance(length, (int, float)) or length < max(FLOOR_DETOUR_X * straight, straight + FLOOR_DETOUR_ADD_M):
        return None
    res, ups = _floor_ways(FLOOR_WAY_M)
    ups = [u for u in ups if u.get("kind") in ("ladder", "stairs")]
    if not ups:
        return None
    side = "above" if dest[1] > here[1] else "below"
    return (f"note: the goal is {render.num(round(dy))}m {side} you and the walk there winds {render.num(round(length))}m "
            f"for {render.num(round(straight))}m straight; shorter: {_ways_text(res, ups)} "
            f"(cancel, interact {H.get(ups[0]['uuid'], ups[0].get('name'))}, then travel_to again)")


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
        if u == lead or (info.get(u) or {}).get("summon"):
            skip.add(u)  # summons: not companions, they come and go with their spell
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
    # S035 19:28: Shadowheart's own move_to + jump_to queue (her way up) was replaced by regroup's travel_to, which
    # found no walkable way ("no walkable ground leads further"). A member already on its own queue keeps it.
    own = [(u, d) for u, d in far if _queues().busy(u)]
    for u, d in own:
        lines.append(f"{_label(u)} ({render.num(round(d, 1))}m): left on its own queue (it is still running); "
                     "regroup again once it ends")
    far = [(u, d) for u, d in far if (u, d) not in own]
    skip.update(u for u, _ in own)   # no chain mid-queue either (a chained follower walks after the leader)
    if not far and not (chain and unchained):
        return _finish("regroup", {}, "\n".join(lines), t0)
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
    inv = game.rpc("state.inventory", {"uuid": _who_uuid(who)} if who else {})
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
            if page.get("text"):
                lore.remember(MEMORY, H.scrub(str(page.get("title") or it.get("name") or "")), H.scrub(str(page["text"])))
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
def recall(query: str = "", max_chars: int = 3000) -> str:
    """Search what the party has READ (books, notes, letters, inscriptions shown on screen, read_all) and its journal steps, by words: the puzzle hints a player remembers (e.g. "mausoleum order", "painting", "password"). Empty query: the titles read so far. Only texts the game showed you; read nearby books/notes first (interact) when nothing matches."""
    t0 = time.monotonic()
    journal = game.try_rpc("state.journal") if query.strip() else None
    return _finish("recall", {"query": query}, lore.search(MEMORY, query, journal, max(500, min(int(max_chars), 8000))), t0)


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


GAME_ALIVE_MIN_MB = 256  # the Steam runtime's `bg3` parent is ~60 MB; the game itself is GBs once at the menu


def _game_process_alive(proc: Path = Path("/proc")) -> bool | None:
    """Whether the game runs on this machine (None when /proc can't be read). The Steam runtime starts two "bg3"
    processes, a ~60 MB parent and the game as its child: only a bg3 of GAME_ALIVE_MIN_MB+ counts, so a dead game
    child with a lingering parent takes the crash fast path instead of waiting out the load."""
    try:
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    if (d / "comm").read_text().strip() == "bg3" and \
                            (metrics.rss_mb(int(d.name), proc) or 0) >= GAME_ALIVE_MIN_MB:
                        return True
                except OSError:
                    continue
        return False
    except OSError:
        return None


LOAD_START_S = 8.0  # a pressed Load leaves the Running state within this


def _server_boot() -> str | None:
    """The server Lua state's id (sys.ping boot): a new one after every load. None when it can't be read."""
    p = game.try_rpc("sys.ping", context="server")
    return p.get("boot") if isinstance(p, dict) else None


def _load_not_done(boot0: str | None) -> str:
    """'' when the load evidently happened, else why not (the game is Running again at this point)."""
    for _ in range(10):  # the Load page can take a moment to go after the state flips
        ui = game.try_rpc("ui.state", context="client") or {}
        scr = ui.get("screen") if isinstance(ui, dict) else None
        if scr not in ("save_load", "main_menu", "loading"):
            break
        time.sleep(0.5)
    else:
        return f"the game is Running but the {scr.replace('_', ' ')} screen is still up: the save did not load"
    boot = _server_boot()
    if boot0 and boot and boot == boot0:
        return "the game is Running but nothing was loaded (the same session as before the call)"
    return ""


def _load_started(max_s: float) -> bool:
    """True once the game leaves Running (the load began), or when it can't be read (a load resets the mod)."""
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        try:
            if game.game_state() != "Running":
                return True
        except (GameError, NotReady, OSError):
            return True
        time.sleep(0.25)
    return False


LOAD_RETRIES = 1  # extra Load presses when a press didn't start a load


def _load_once(save: str, boot0: str | None, alive_at_start: bool | None) -> tuple[JobResult, bool]:
    """One select+press of the save and the wait for it. (result, True) when the press didn't start a load
    (worth pressing again); a crash, a timeout or a real load is final."""
    # The mod selects the save on the Load page (by playthrough, position and date) and presses Load; the
    # load then resets the mod's Lua, so a poll may fail mid-way: wait for the game to be Running again.
    try:
        r = game.run_job("ui.menu", saves.load_args(SAVE_DIR, save), context="client", wait_s=40)
        if r.status == "failed" and isinstance(r.result, dict) and r.result.get("needs_key") and _inp() is not None:
            _inp().key(r.result["needs_key"])
            r = JobResult("done", {"loading": True, "via": "shim"})
    except (GameError, NotReady) as e:
        r = JobResult("done", {"loading": True, "note": str(e)[:80]})
    if r.status == "done" and not (isinstance(r.result, dict) and r.result.get("loading")):
        # Live 11:2x: during the post-rest sleep the Load press did nothing, the page stayed open and the old check
        # (Running + server answering) said "done" after 1.7 s. A real load leaves Running within a few seconds.
        if not _load_started(LOAD_START_S):
            return JobResult("failed", None, "the Load press didn't start a load (no loading screen within "
                             f"{LOAD_START_S:.0f} s, pressed {LOAD_RETRIES + 1} times; the game may be mid-transition, "
                             "e.g. the long rest's sleep): nothing was loaded"), True
    if r.status != "done":
        return r, False
    deadline, ok, dead = time.monotonic() + 120, False, False
    time.sleep(1.0)
    # Pass 4 10:40: the game SIGSEGV'd 1 s into a load and this waited the full 120 s: a gone process ends it.
    while time.monotonic() < deadline and not ok:
        if alive_at_start and _game_process_alive() is False:
            dead = True
            break
        try:
            ok = game.game_state() == "Running" and game.server_ready()
        except (GameError, NotReady, OSError):
            ok = False
        if not ok:
            time.sleep(0.5)
    # vis/se live: a "done" with nothing loaded broke every later step. Running again is not enough: the
    # server Lua state must be a new one (a load rebuilds it) and the Load page must be gone.
    why = _load_not_done(boot0) if ok else ""
    if why:
        return JobResult("failed", None, why), True
    if ok:
        return JobResult("done", {"loaded": save.split("__", 1)[-1]}), False
    if dead:
        return JobResult("failed", None, "the game CRASHED during the load (its process is gone; the bg3le log in "
                         "runtime/logs has the crash): it has to be restarted"), False
    return JobResult("failed", None, "the game did not come back to Running within 120 s"), False


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
    retried_fight, fight_block = _fight_before_load()
    alive_at_start = _game_process_alive()
    boot0 = _server_boot()
    retried = 0
    while True:
        r, retry = _load_once(save, boot0, alive_at_start)
        if not retry or retried >= LOAD_RETRIES or _game_process_alive() is False:
            break
        # Acceptance 12:13: once the Load press did nothing (the page stayed up, "try again" after 7.7 s) and the
        # next call loaded fine. Press again here: the job reuses an open Load page, reselects and presses.
        retried += 1
        time.sleep(1.0)
    if retry:  # still not loading: close the Load page and the game menu under it, so the game view is back
        ui = game.try_rpc("ui.state", context="client") or {}
        if isinstance(ui, dict) and ui.get("screen") in ("save_load", "game_menu"):
            for _ in range(2):
                game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
                time.sleep(0.4)
    elif retried and r.status == "done" and isinstance(r.result, dict):
        r.result["pressed"] = f"{retried + 1} times (the first press didn't start a load)"
    game.event_seq = {"server": 0, "client": 0}
    DELTA.reset()  # a loaded save is a new world: the next look is a full one
    delta.INV.reset()  # and its inventory: the mod's rev restarts with its Lua state (stale gear/gold after a load)
    body = _job_text("load_game", r) + (f"\n{dropped}" if dropped else "")
    if fight_block:
        body += "\n" + fight_block
    if retried_fight is not None and r.status == "done":
        _retry.update(rec=retried_fight, status=True, fight=True)
        body += "\n" + debrief.last_attempt(retried_fight)
    return _finish("load_game", {"save": save}, body, t0, events=False)


def _fight_before_load() -> tuple[dict | None, str]:
    """load_game: the events not drained yet still belong to the fight (the load resets the event cursor); a fight
    still open is closed as reloaded. Returns (the fight this load retries or None, its debrief block or '')."""
    try:
        tr = _fights()
        evs = []
        for ctx in ("server", "client"):
            try:
                evs += game.drain_events(ctx)
            except GameError:
                pass
        _warns, ended = tr.feed(evs, _party_cached())
        if tr.cur is not None:
            block = _close_fight(reloaded=not ended)   # it ended in these events: its own grade, else reloaded
            return tr.retry_of() if ended else tr.last, block
        return tr.retry_of(), ""
    except Exception as e:  # noqa: BLE001 - never block a load
        _log("debrief", {}, f"before load failed: {e}", 0)
        return None, ""


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
def wiki_page(title: str, section: str = "", max_chars: int = 2500) -> str:
    """Read a bg3.wiki page (or one section) as plain text. A whole page is cut at max_chars and lists its sections: read the one you need with section= (S018-S027 pages ran ~2.5 KB median, most of it unread). For a puzzle, recall(query) and get_nearby(kinds="props") first."""
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
