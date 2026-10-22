"""What every tool result carries: fight debriefs, skill-check tags on dialogue answers, the result finisher (events,
clock, lessons, hints), auto dialogue/combat reads and the _guard wrapper.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .. import debrief, delta, lessons, lore, memtools, offlimits, render, rules
from ..game import GameError, NotReady
from . import core

# ---- fight debriefs (debrief.py, research/fight-debrief.md): learn from a fight that went badly ----
_FIGHTS: dict[str, Any] = {"tr": None}
_retry: dict[str, Any] = {"rec": None, "status": False, "fight": False}


def _fights_path() -> Path:
    """runtime/sessions/<playthrough>/<Sxxx>.fights.jsonl beside the session's metrics (docs/ARCHITECTURE.md
    "Learning from fights"); memory/fights.jsonl when the runner set no metrics path (tests, hand runs)."""
    m = os.environ.get("BG3AI_METRICS_PATH", "")
    if m.endswith(".metrics.jsonl"):
        return Path(m[: -len(".metrics.jsonl")] + ".fights.jsonl")
    return core.MEMORY / "fights.jsonl"


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
        warns, ended = _fights().feed(evs, turn_plan._party_cached())
        out = list(warns)
        if ended:
            out.append(_close_fight())
        return "\n".join(x for x in out if x)
    except Exception as e:
        core._log("debrief", {}, f"feed failed: {e}", 0)
        return ""


_UNUSED_RX = re.compile(r"(?i)\b(potion|elixir|scroll|grenade|flask|bomb)\b")


def _close_fight(reloaded: bool = False) -> str:
    """Close the open fight: the debrief block, a [fight] line in the session log, a fights.jsonl record."""
    st = core.game.try_rpc("state.status") or {}
    rows = [r for r in st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    m = os.environ.get("BG3AI_METRICS_PATH", "")
    sess = os.environ.get("BG3AI_SESSION") or (Path(m).name.split(".")[0] if m.endswith(".metrics.jsonl") else None)
    meta = {"session": sess, "region": st.get("region")}   # the runner sets only the metrics path (S035: null)
    rec = _fights().close(rows, [], meta, reloaded=reloaded)
    if rec is None:
        return ""
    if rec["grade"] != "clean":
        # What the party carried and didn't use (its own inventory: fair). One read, bad fights only.
        inv = core.game.try_rpc("state.inventory", {}) or {}
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
        memory._append_note(debrief.fight_note(rec), "fight")
    except Exception:
        pass
    return debrief.render(rec)


OWN_LESSONS_MAX = 3
_OWN_LESSON_RE = re.compile(r"^- (\d{1,2}):(\d{2})(?:-\d{1,2}:\d{2})? \[lesson\]\s*(.+)$", re.I)


def _own_lessons(rec: dict | None) -> list[str]:
    """The agent's own [lesson] lines written since `rec`'s fight began, from that fight's session log
    (BACKLOG ~1107: S033's "kill Umbral Tremors early" lesson after attempt 1 never reached attempt 2;
    lessons.md only gets it from the keeper at the session's end). Newest last, at most OWN_LESSONS_MAX."""
    if not isinstance(rec, dict):
        return []
    sess = rec.get("session")
    path = (core.MEMORY / "sessions" / f"{sess}.md") if sess else None
    if path is None or not path.is_file():
        path = session._session_log()
    try:
        text = path.read_text(errors="replace") if path is not None else ""
    except OSError:
        return []
    # a retry chain (attempt 1 → reload → attempt 2 …) counts from its first attempt
    m = re.search(r"T(\d{2}):(\d{2})", str(_fights().chain_start(rec) or ""))
    start = int(m.group(1)) * 60 + int(m.group(2)) if m else None
    out: list[str] = []
    for line in text.splitlines():
        lm = _OWN_LESSON_RE.match(line.strip())
        if not lm:
            continue
        at = int(lm.group(1)) * 60 + int(lm.group(2))
        if start is not None and not (0 <= (at - start) % 1440 <= 12 * 60):   # before the fight (midnight-safe)
            continue
        if lm.group(3).strip() not in out:
            out.append(lm.group(3).strip())
    return out[-OWN_LESSONS_MAX:]


def _own_lessons_text(rec: dict | None) -> str:
    own = _own_lessons(rec)
    return ("YOUR LESSON from that fight (session log): " + " | ".join(own)) if own else ""


def _last_attempt_text(rec: dict) -> str:
    """LAST ATTEMPT (debrief) + the agent's own lessons from that fight."""
    return "\n".join(x for x in (debrief.last_attempt(rec, _fights().history), _own_lessons_text(rec)) if x)


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
                f.chain_t = rec.get("chain_t") or rec.get("t")
                facts = (rec.get("key_facts") or [])[:debrief.FACTS_SHOWN]
                own = _own_lessons_text(rec)
                head = f"RETRY of the fight that went {rec.get('grade_before') or rec.get('grade')}: "
                lost = tr.informative(names, before=rec) if debrief._graded(rec) == "clean" else None
                if lost is not None and debrief._graded(lost) != "clean":
                    # reloaded while it went fine (S061 09:53/09:56 lure practice): the facts that matter are the
                    # last lost attempt's
                    head += ("; ".join(facts) + " · " if facts else "") + (
                        f"the last lost attempt vs them ({debrief._outcome(lost)}, {lost.get('session') or 'earlier'}"
                        f"{debrief._hhmm(lost)}): ")
                    facts = (lost.get("key_facts") or [])[:debrief.FACTS_SHOWN]
                return (head + ("; ".join(facts) if facts else rec.get("summary", "")) + (f"\n{own}" if own else ""))
        warns = tr.first_turn_warnings(cs)
        past = tr.last_time_rec(cs) if warns else None
        own = _own_lessons_text(past) if past else ""
        return "\n".join(warns + ([own] if own else []))
    except Exception as e:
        core._log("debrief", {}, f"hint failed: {e}", 0)
        return ""


# ---- skill checks in dialogue: each tagged option shows the check and the speaker's own modifier ----------
# Fair: the player sees the tag on the answer and the bonuses on the character sheet; the DC is only shown on
# the roll screen after picking, so it is never added here.
_dlg = {"speaker": None}  # party member in the current conversation (from the last dialogue_started event)
OFFLIMITS = offlimits.OffLimits()  # areas the game warned the party off (trespass talks), walks keep out (vis V26)
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
    c = core.game.try_rpc("state.character", {"uuid": uuid, "sections": ["abilities"]}) or {}
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
        party = sorted(turn_plan._party_set() - {None})
        speaker = _dlg["speaker"] if _dlg["speaker"] in party else (core.game.try_rpc("state.status") or {}).get("active")
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
            txt = f"{name} check: {core.H.get(speaker) if speaker else '?'} {_signed(mine) if mine is not None else '?'}"
            best = max(((v, u) for u, v in mods.items() if v is not None), default=None)
            if best and best[1] != speaker and (mine is None or best[0] > mine):
                txt += f" (party best {core.H.get(best[1])} {_signed(best[0])}: they can start the next talk)"
            notes.append(txt)
        o["check"] = "; ".join(notes)
    return ui


# Answers read by scrolling, per dialogue node: (speaker, line, the visible rows' texts) -> {n: option}.
_answer_pages: dict = {}
# The answer list the agent was last shown: {"node": (speaker, line), "texts": {n: text}} (choose_dialogue's numbers).
_listed: dict = {}


def _node(d: dict | None) -> tuple:
    return ((d or {}).get("speaker"), (d or {}).get("text"))


def _complete_answers(ui: dict) -> dict:
    """A long answer list shows only a window of its rows (live: 6 of Karlach's 10). Wheel it like a player and
    list every answer, numbered 1..N in display order (texts of rows outside the window show only when scrolled).
    Cached per node. S081: the mod's count said nothing was below (a two-line row), so `scrollable` (the list's
    ScrollViewer can scroll) triggers the read too, and the read goes to the list's bottom, not to a count."""
    d = ui.get("dialogue") if isinstance(ui, dict) else None
    if not d or not d.get("options") or not (d.get("more_below") or d.get("more_above") or d.get("scrollable")):
        return ui
    from .. import clicker
    key = _node(d) + (tuple(clicker.answer_key(o.get("text")) for o in d["options"]),)
    hit = _answer_pages.get(key)
    if hit is None:
        inp = world_tools._inp()
        if inp is None:
            return ui

        def read() -> list:
            u = core.game.try_rpc("ui.state", context="client") or {}
            dd = u.get("dialogue") if isinstance(u, dict) else None
            return list((dd or {}).get("options") or [])

        hit = clicker.read_answer_list(core.game, inp, read)
        if len(_answer_pages) > 50:
            _answer_pages.clear()
        _answer_pages[key] = hit
    got, why = hit
    if len(got) < len(d["options"]):
        return ui   # the read saw less than the window (the list changed meanwhile): the window as it is
    d = dict(d, options=[got[n] for n in sorted(got)])
    d.pop("more_above", None)
    d.pop("more_below", None)
    if why:
        d["scroll_note"] = why
    return dict(ui, dialogue=d)


def _note_listed(ui: dict) -> None:
    """Remember the numbered answers just rendered for the agent: choose_dialogue(n) means the answer shown as n."""
    d = ui.get("dialogue") if isinstance(ui, dict) else None
    opts = [o for o in (d or {}).get("options") or [] if isinstance(o, dict) and o.get("text")]
    _listed.clear()
    if opts:
        _listed.update(node=_node(d), texts={int(o["i"]): str(o["text"]) for o in opts if isinstance(o.get("i"), int)})


def _render_ui(ui: dict, brief: bool = False) -> list[str]:
    _keep_read(ui)
    try:
        ui = _complete_answers(ui)
    except Exception:  # best effort: the visible window still renders
        pass
    _note_listed(ui)
    try:
        ui = _annotate_checks(ui)
    except Exception:  # best effort: a missing sheet never hides the dialogue itself
        pass
    return render.render_ui(ui, core.H, brief=brief)


def _keep_read(ui: Any) -> None:
    """A book/note/letter panel on screen: keep its text for recall() (what the party has read)."""
    pop = ui.get("popup") if isinstance(ui, dict) and ui.get("screen") == "book" else None
    if isinstance(pop, dict) and pop.get("text"):
        try:
            text = core.H.scrub(str(pop["text"]))
            title = str(pop.get("title") or "") or (text[:60].rsplit(" ", 1)[0] + "…" if len(text) > 60 else text)
            lore.remember(core.MEMORY, core.H.scrub(title), text)
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
               "get_combat_state", "get_dialogue", "ability_preview", "get_level_up_options", "recall",
               "memory_list", "memory_read", "memory_search", "session_start"}


def _finish(tool: str, args: dict, body: str, t0: float, events: bool = True, issued: bool = False,
            extra: dict | None = None) -> str:
    if _batch["active"]:
        # Inside do_actions: no per-step event drain or clock; the batch reports once at the end.
        text = core.H.scrub(body.rstrip())
        core._log(tool, args, text, (time.monotonic() - t0) * 1000, extra)
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
        ev = core._events_text(body, job if re.fullmatch(r"j\d+", job) else None,
                          max_wait=core.DIALOGUE_DRAIN_MAX_S if tool in core._DIALOGUE_TOOLS else None)
        if ev:
            out.append(ev)
        auto = _auto_dialogue(tool, body, ev)
        if auto:
            out.append(auto)
            ev = core._events_text()
            if ev:
                out.append(ev)
        core._fight_seen(tool, ev)
        fight = _auto_combat(tool, body, ev)
        if fight:
            out.append(fight)
        les = _lessons_text(tool, body, ev)
        if les:
            out.append(les)
    queueing._running_memo.update(on=True, keys=None, out=None, watch=None)
    try:
        if "RUNNING " not in body:  # what still runs, so the agent appends (queue_actions) instead of waiting
            try:
                out.append(queueing._running_text())
            except GameError:
                pass
        try:
            out.append(batch._notes_window())
        except GameError:
            pass
    finally:
        queueing._running_memo["on"] = False
    if tool == "wait_for" and _pace["calls_since"] == 0 and _pace["issued_at"] \
            and t0 - _pace["issued_at"] < PACE_GAP_S and rules.coaching():
        out.append("PACE: you waited right after issuing. Next time first do independent work (queue the arrival "
                   "steps, look, plan, notes): the character acts while you think.")
        _pace["nudge"] = True
    if tool not in ("get_status", "look", "level_up"):
        if overview._LEVEL_NUDGE.stale():  # XP arrives mid-play: one state.status read a minute finds the level up
            overview._LEVEL_NUDGE.seen_at = time.time()  # also when the read fails (loading): no retry on every call
            overview._LEVEL_NUDGE.observe(core.game.try_rpc("state.status"))
        lv = overview._LEVEL_NUDGE.text()
        if lv:
            out.append(lv)
    out.append(saving.SAVER.take_notes())  # SAVED / SAVE FAILED from a background save
    c = core._clock_line() or core.LIFE.clock()  # LIFE.clock: manual sessions' elapsed-time reminder (no runner deadline)
    if c:
        out.append(c)
    text = core.H.scrub("\n".join(x for x in out if x))
    core._log(tool, args, text, (time.monotonic() - t0) * 1000, extra)
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
    if not macro_tools.AUTO_DIALOGUE or tool in _NO_AUTO_DIALOGUE or "DIALOGUE " in body:
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
        n, lines, stop = macro_tools._dialogue_autopilot(open_s=open_s,
                                             line_s=macro_tools.AUTO_DIALOGUE_READ_LINE_S if tool in _READ_TOOLS else None)
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
        return "COMBAT STARTED (no get_combat_state needed):\n" + observe._combat_state_text()
    except GameError:
        return ""


LESSONS = lessons.Store(core.MEMORY / "lessons.md")
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
    cs = core.game.try_rpc("state.combat") or {}
    st = core.game.try_rpc("state.status") or {}
    enemies = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("relation") == "hostile"
               and not o.get("item")]
    return LESSONS.fight_lines(_with_statuses(enemies), [st.get("subregion") or "", st.get("region") or ""])


def _with_statuses(rows: list[dict]) -> list[dict]:
    """Character rows + the status names the examine panel shows ([Mirrored]): the combat rows don't carry them,
    the nearby read does (one read)."""
    nb = core.game.try_rpc("state.nearby", {"kinds": ["characters"]}) or {}
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
    p = getattr(core.game, "parallel", None)
    return p(*thunks) if p else [t() for t in thunks]


def _read(server_ops: list, client_ops: list = ()) -> tuple[list, list]:
    """game.read: server + client ops (and both contexts' events) in one game tick; failed ops are None."""
    if hasattr(core.game, "read"):
        return core.game.read(server_ops, client_ops)
    return (delta.multi_rpc(core.game, list(server_ops)) if server_ops else [],
            [core.game.try_rpc(op, a, context="client") for op, a in client_ops])


def _read1(op: str, args: dict | None = None, context: str = "server") -> Any:
    """game.read1: rpc(op) with both contexts' events folded into the same tick; raises like rpc()."""
    r1 = getattr(core.game, "read1", None)
    return r1(op, args, context) if r1 else core.game.rpc(op, args, context)


def _guard(fn):
    """Wrap a tool so game errors become readable text instead of exceptions."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        t0 = time.monotonic()
        try:
            core.LIFE.ensure()  # manual mode: the first call opens the memory session (was the SessionStart hook)
        except Exception as e:
            core._log("session_start", {}, f"session not opened: {type(e).__name__}: {e}", 0)
        end = core._times()[1] if core.DEADLINE and time.time() > core.DEADLINE else core.DEADLINE  # a fight hold moves it
        refused = rules.stop_refusal(fn.__name__, end, _READ_TOOLS,  # deadline + grace: for any client
                                     off={t for t in ("end_session", "save_game") if core.mcp._tool_manager.get_tool(t) is None})
        if refused:
            return _finish(fn.__name__, kw, refused, t0, events=False)
        waited = newgame._await_new_game() if newgame._new_game["thread"] is not None and fn.__name__ not in newgame._NEW_GAME_NO_WAIT \
            else ""
        getattr(core.game, "begin_tool", lambda: None)()
        gated = fn.__name__ not in _SAVE_UNGATED
        if gated:
            saving.SAVER.enter()  # a background save has the menus up for ~1 s: don't interleave with it
        who_prev = getattr(plumbing._who_tl, "note", "")
        plumbing._who_tl.note = ""
        try:
            out = fn(*a, **kw)
            if plumbing._who_tl.note and isinstance(out, str):  # _who picked the member acting now (ACT-P2); last, so
                out = out + "\n" + plumbing._who_tl.note           # a result's own first word (FAILED, done) stays first
            if waited and isinstance(out, str):
                out = waited + "\n" + out
            if core.LIFE.pending and isinstance(out, str):  # the briefing rides on the first text result
                out = core.RULES.wrap(core.LIFE.take_pending(), memory._session_key()) + "\n\n=== the result of your call ===\n" + out
            return out
        except memtools.Refused as e:
            return _finish(fn.__name__, kw, str(e), t0, events=False)
        except NotReady as e:
            return _finish(fn.__name__, kw, f"NOT READY: {e}", t0, events=False)
        except (GameError, ValueError) as e:
            post = "\n" + plumbing._who_tl.note if plumbing._who_tl.note else ""
            return _finish(fn.__name__, kw, "FAILED: " + re.sub(r'\[string "[^"]*"\]:\d+: ', "", str(e)) + post, t0)
        finally:
            plumbing._who_tl.note = who_prev
            if gated:
                saving.SAVER.leave()
    return wrapper


# Tools that don't touch the game screen, so they run while a background save has the menus up.
_SAVE_UNGATED = {"list_saves", "session_clock", "wiki_page", "wiki_search", "memory_list", "memory_read",
                 "memory_search", "memory_write", "memory_edit", "memory_append", "session_start", "end_session"}


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    plumbing, overview, observe, world_tools, memory, newgame, batch, queueing, turn_plan, macro_tools, saving,
    session,
)
