"""The runner's pure session logic (moved from runner/play.py, Phase D.3): session ids and deadlines, the
relaunch and save rules, progress and drift, the kickoff and resume texts, the profile, the UI-strict and gates
choices. Reads files at most (the run's events, the memory's progress files); no game I/O.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

from bg3ai import metrics as latency
from bg3ai import milestones

import bg3harness
from bg3harness import clients, ramwatch, runs
from bg3harness.game import io as game
from bg3harness.game.io import Save
from bg3harness.game.saves import SAVE_PREFIX, is_ai_save, is_autosave
from bg3harness.runner import env as renv
from bg3harness.session import FIGHT_HOLD_S, GAME_FAULTS, fight_hold_path, held_deadline  # noqa: F401  (re-exported)


def session_id(n: int) -> str:
    return f"S{n:03d}"


def predict_session_number(existing: list[int], current: int | None) -> int:
    """The number session.py's `start` will give the next session (max of logs and state, plus one)."""
    return max(existing + [current or 0]) + 1


def wrapup_minutes(session_minutes: float) -> float:
    """Length of the wrap-up phase: 3 min, but at most a third of the session."""
    return min(renv.WRAPUP_MINUTES, session_minutes / 3)


def wrap_minutes(session_minutes: float) -> float:
    """Minutes into the session when WRAP UP starts (the clock line and the memory hooks)."""
    return session_minutes - wrapup_minutes(session_minutes)


def early_exit_minutes(reason: str, started: float, ended: float, session_minutes: float,
                       threshold: float = renv.EARLY_EXIT_MINUTES) -> float | None:
    """Minutes left when claude exited on its own (not a crash, hang, hard stop or abort) more than
    `threshold` minutes before the deadline; else None. The runner then starts the next session at once
    (claude-run-2: S001 used 14 of 30 min, S002 12.6 of 20)."""
    if reason != "exited":
        return None
    left = (started + session_minutes * 60 - ended) / 60
    return round(left, 1) if left > threshold else None


# Early exits are kept (decided 2026-10-11 from S040-S046): a session ending 6-7 min early at a milestone costs only
# the boundary (previous claude end → next claude start: 29-86 s, median ~45 s, mostly the memory keeper's 25-51 s),
# which a full-length session pays too: play stays ~97 % of the wall clock either way, while bouncing a finished
# wrap-up turned into idle filler (S036). boundary_s is kept per session to watch that cost.
BOUNDARY_MAX_S = 15 * 60   # a longer gap is a runner stop, not a boundary


def boundary_s(history: list[dict], started: float) -> float | None:
    """Seconds from the previous play session's end to this one's claude start (None: first, or a runner stop)."""
    prev = next((h for h in reversed(history) if h.get("kind", "play") == "play" and h.get("ended")), None)
    if prev is None:
        return None
    gap = started - prev["ended"]
    return round(gap, 1) if 0 <= gap <= BOUNDARY_MAX_S else None


def detect_budget_s(kind: str) -> float:
    """Worst case from a fault to claude stopped and recovery starting, with the current constants.
    crash/port lost: next health check; game hang: HEALTH_FAILURES slow evals; agent hang: the silence limit.
    Then claude gets SIGINT, SIGTERM, SIGKILL (the main loop ticks every 2 s)."""
    probe = renv.HEALTH_EVAL_TIMEOUT + 5  # eval timeout + LuaDebugClient connect timeout
    stop = renv.SIGINT_GRACE + renv.SIGTERM_GRACE + 2 * 3
    if kind == "crash":
        return renv.HEALTH_INTERVAL + 1 + stop
    if kind == "hang":
        return renv.HEALTH_INTERVAL + probe + (renv.HEALTH_FAILURES - 1) * (renv.HEALTH_RETRY_INTERVAL + probe) + stop
    if kind == "agent_hang":
        return renv.AGENT_SILENT_MINUTES * 60 + 2 + stop
    raise ValueError(kind)


def deadlines(start: float, session_minutes: float, grace: float = renv.HARD_STOP_GRACE) -> tuple[float, float]:
    """(soft deadline shown to the agent, hard stop when the runner interrupts Claude)."""
    deadline = start + session_minutes * 60
    return deadline, deadline + grace


def session_budget(now: float, run_end: float | None, session_minutes: float,
                   min_minutes: float = renv.MIN_SESSION_MINUTES) -> float | None:
    """Minutes for the next session given the --hours budget; None when there's no room for one."""
    if run_end is None:
        return session_minutes
    left = (run_end - now) / 60
    if left < min_minutes:
        return None
    return min(session_minutes, left)


def relaunch_reason(started: float | None, now: float, last_log: str | None,
                    max_age_h: float = renv.GAME_MAX_AGE_H, session_s: float = 0.0, ram_mb: int | None = None,
                    ram_limit_mb: int = renv.RELAUNCH_RAM_MB, mod_path: str | None = None,
                    mod_live: str | None = None) -> str | None:
    """Why the game process should be relaunched before the next session, or None: the last session's log shows a
    party ready check failing (ready_failed / end_day_failed), the game loads the mod from mod_path instead of the
    runner's snapshot mod_live, the game already holds ram_limit_mb or more RAM+swap, or the process would be
    max_age_h or more old by the end of a session that may run session_s (its length plus the fight hold: S082
    started at 1.9 h and ran to 2.3 h)."""
    m = renv.READY_FAIL_RE.search(last_log or "")
    if m:
        return f"the last session logged {m.group(0)} (ready checks break in a long-lived game process)"
    if mod_path is not None and mod_live and mod_path != mod_live:
        return (f"the game loads the mod from {mod_path}, not the runner's snapshot {mod_live} (a merge into main "
                "mid-session would reach its next load_game: S090)")
    if ram_mb is not None and ram_limit_mb > 0 and ram_mb >= ram_limit_mb:
        return (f"the game holds {ram_mb} MB RAM+swap (relaunched at {ram_limit_mb} MB: it grows ~0.9 GB/h and "
                f"ramwatch kills it at {ramwatch.KILL_ANON_SWAP_MB} MB)")
    if started and max_age_h > 0 and now - started + session_s >= max_age_h * 3600:
        age = f"{(now - started) / 3600:.1f} h old"
        if session_s:
            age += f" and the session may run {session_s / 60:.0f} min (to {(now - started + session_s) / 3600:.1f} h)"
        return f"the game process is {age} (relaunched at {max_age_h:g} h: ready checks break in a long-lived process)"
    return None


def game_ram_mb() -> int | None:
    """The running game's RAM+swap (RssAnon + VmSwap of all bg3 processes, ramwatch's kill measure), None if none."""
    pids = ramwatch.game_pids()
    return sum(ramwatch.anon_swap_mb(p) for p in pids) if pids else None


AGENT_SAVES_KEPT = 50


def agent_quicksaves(actions: list[dict], owner: str | None) -> list[str]:
    """Quicksave folders the MCP server's saver wrote (actions.jsonl "save_game.bg" records with ok and the folder
    found on disk, bridge saver.py), in order; only `owner`'s (None: none, an unknown owner proves nothing)."""
    out = []
    for a in actions:
        if a.get("tool") != "save_game.bg" or not a.get("ok") or not a.get("folder"):
            continue
        s = game.parse_save_folder(str(a["folder"]))
        if s and owner and s.owner.casefold() == owner.casefold() and is_autosave(s.label) and s.folder not in out:
            out.append(s.folder)
    return out


# MCP tools that never change the game: observations, notes/memory, saves, and waits on jobs that other (logged)
# calls started. Batch wrappers (latency.BATCH_TOOLS) don't either: each of their steps is a record of its own.
STATE_KEEPING_TOOLS = {
    "get_status", "look", "get_party", "get_character", "get_actions", "get_nearby", "look_at", "get_inventory",
    "get_journal", "get_map", "get_frontier", "get_layout", "route", "get_explored", "path_preview",
    "get_combat_state", "get_dialogue", "get_events", "screenshot", "zoom", "end_combat_report", "note",
    "memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append", "session_start",
    "end_session", "get_level_up_options", "ability_preview", "ui_dump", "character_creation_options",
    "list_saves", "save_game", "save_game.bg", "wait_for", "cancel_queue", "recall", "wiki_search", "wiki_page",
    "session_clock",
}
REFUSED_PREFIXES = ("FAILED", "NOT READY", "REFUSED")   # the bridge's not-ok results (server._log)


def ended_on_save(actions: list[dict], save: Save) -> bool:
    """True when the session's tool log (actions.jsonl records since its start, oldest first) ends with a load_game
    of `save` after which nothing changed the game: every later call keeps the state (STATE_KEEPING_TOOLS, a batch
    wrapper) or was refused. The game then stands exactly at `save`, so a relaunch onto it loses nothing although
    the save predates the session. S091 + S092 both ended on load_game S090-end, and the S24 mod-snapshot relaunch
    was skipped at 09:05 ("older than S092's start"). An unknown tool counts as a change."""
    loads = [i for i, a in enumerate(actions) if a.get("tool") == "load_game"]
    if not loads:
        return False
    last = actions[loads[-1]]
    m = re.match(r"load_game: done — loaded=(\S+)", str(last.get("result") or ""))
    arg = str((last.get("args") or {}).get("save") or "")
    if not m or m.group(1) != save.label or ("__" in arg and arg != save.folder):
        return False
    return all(keeps_state(a) for a in actions[loads[-1] + 1:])


def keeps_state(a: dict) -> bool:
    """An actions.jsonl record that didn't change the game: a STATE_KEEPING_TOOLS call, a batch wrapper or a refused
    call. An unknown tool counts as a change."""
    return (a.get("tool") in STATE_KEEPING_TOOLS or a.get("tool") in latency.BATCH_TOOLS
            or str(a.get("result") or "").startswith(REFUSED_PREFIXES))


def lose_play_mark(last: dict, actions: list[dict]) -> tuple[float, str]:
    """From when a relaunch onto an older save loses play: the last game-changing call of the last session
    (`actions`: actions.jsonl since its start, keeps_state), else its start. A session that saved partway and played
    on without saving would otherwise relaunch onto that save and lose the rest (2026-10-18: losing play is worse
    than a skipped relaunch). -> (time, what it is, for the log)."""
    sid = last.get("session") or "the last session"
    changed = [a for a in actions if not keeps_state(a)]
    if not changed:
        return float(last.get("started", 0)), f"{sid}'s start"
    a = max(changed, key=lambda x: x.get("t", 0))
    return float(a.get("t", 0)), (f"{sid}'s last game change ({a.get('tool')} at "
                                  f"{datetime.fromtimestamp(a.get('t', 0)):%H:%M:%S})")


def boundary_save(saves: list[Save], runner_saves: set[str], combat: dict[str, bool | None], last: dict,
                  actions: list[dict]) -> tuple[Save | None, str]:
    """The runner's own quicksave at the end of the last play session (ensure_save, when the agent didn't save), if
    a relaunch can load it instead of an older save without losing play: written after the session started, out of
    combat (`combat`: runner.json runner_save_combat, the party's state when the runner asked for the save), and no
    call changed the game after it (`actions`: actions.jsonl since the session's start). S093 09:50: HANDOFF named
    S090-end, S093 played on without a named save, and the snapshot relaunch was skipped although the runner's
    QuickSave_24 held where S093 ended. A mid-fight one doesn't: the enemies' turns go on in the running game, and a
    reload would roll them again. -> (save, "") or (None, why not)."""
    sid = last.get("session") or "the last session"
    mine = [s for s in saves if s.folder in runner_saves and s.mtime >= last.get("started", 0)]
    if not mine:
        return None, f"no runner quicksave since {sid}'s start"
    s = max(mine, key=lambda x: x.mtime)
    was = combat.get(s.folder)
    if was is None:
        return None, f"the runner's quicksave {s.label}: unknown whether the party was in a fight"
    if was:
        return None, (f"the runner's quicksave {s.label} was made mid-fight (the enemies' turns go on in the running "
                      "game; a reload would roll them again)")
    changed = [a for a in actions if a.get("t", 0) > s.mtime and not keeps_state(a)]
    if changed:
        return None, f"the runner's quicksave {s.label}: {changed[-1].get('tool')} changed the game after it"
    return s, ""


def foreign_use_since(save: Save, saves: list[Save], actions: list[dict], game_started: float | None,
                      runner_saves: set[str], prefix: str = SAVE_PREFIX) -> str | None:
    """Why the running game may no longer be at `save` (this playthrough's newest save), or None.

    --take-over plays on from the running game; that's only right if nobody else used it since. Workers' live tests
    load and save their own saves (LIVETEST-*) or older playthrough saves, and a relaunch by tools/dev/first_launch.sh
    --continue loads the newest save of any kind. Any of these after `save` was written → load `save` instead."""
    for s in saves:
        if (s.mtime > save.mtime and s.owner == save.owner and s.folder not in runner_saves
                and not is_autosave(s.label) and not is_ai_save(s, prefix)):
            return f"save '{s.label}' was written after {save.label}"
    for a in actions:
        if a.get("t", 0) <= save.mtime or a.get("tool") != "load_game":
            continue
        args = a.get("args") or {}
        m = re.search(r"loaded=(\S+)", str(a.get("result") or ""))
        got = m.group(1) if m else str(args.get("save") or args.get("label") or "")
        if got not in (save.label, save.folder):
            return f"load_game '{got}' ran after {save.label} was written"
    if game_started and game_started > save.mtime:
        return f"the game was relaunched after {save.label} was written (it loads its newest save, not ours)"
    return None


def mark_inflight_stuck(guard_file: Path) -> str | None:
    """After the runner killed a hung game: marks the mod's in-flight background search stuck, so the next start
    counts it as a strike on that level. The mod marks it from its own Tick, which runs on the server thread that
    froze (2026-10-13: a ways warm froze SCL_Main_A for 30 min; the marker stayed fresh, no strike, and the next load
    froze again). guard_file: the file, or the mod's dir (the newest nav_guard*.json: NAV-3 keeps one per
    playthrough, nav_guard_<host uuid>.json). The marker is "inflights"["ways warm"] (NAV-7, one per op) or an
    older file's "inflight". Returns the level, or None when there was nothing in flight."""
    try:
        if guard_file.is_dir():
            files = sorted(guard_file.glob("nav_guard*.json"), key=lambda p: p.stat().st_mtime)
            if not files:
                return None
            guard_file = files[-1]
        d = json.loads(guard_file.read_text())
        f = d.get("inflight") if isinstance(d, dict) else None
        many = d.get("inflights") if isinstance(d, dict) else None
        if isinstance(many, dict) and isinstance(many.get("ways warm"), dict):
            f = many["ways warm"]
        if not isinstance(f, dict) or not f.get("level") or f.get("stuck"):
            return None
        f["stuck"] = True
        guard_file.write_text(json.dumps(d))
    except (OSError, ValueError) as e:
        renv.log(f"nav guard: couldn't mark the in-flight search stuck: {e}")
        return None
    renv.log(f"nav guard: the game hung during {f.get('op')} on {f['level']}: marked stuck (a strike at the next start)")
    return f["level"]


def read_actions_since(t: float, path: Path | None = None, tail_bytes: int = 4_000_000) -> list[dict]:
    """runtime/actions.jsonl records (the MCP server's tool log) with t > `t`; reads only the file's tail."""
    path = path or renv.RUNTIME / "actions.jsonl"
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            data = f.read()
    except OSError:
        return out
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("t", 0) > t:
            out.append(rec)
    return out


def crash_tools_note(why: str, actions: list[dict], until: float, n: int = 3) -> str:
    """The kickoff warning after a game crash: the last n tool calls (runtime/actions.jsonl) up to `until`, so the
    next session doesn't walk straight back into it (S038: jump_to toward the Shadowfell ledge → OOM)."""
    last = [a for a in actions if a.get("t", 0) <= until + 1][-n:]
    if not last:
        return (f"RUNNER NOTE: the last session ended in a game crash ({why}). No tool calls were logged just "
                "before it; reconcile your notes with get_status.")
    calls = "; ".join(
        f"{datetime.fromtimestamp(a['t']):%H:%M:%S} {a.get('tool')} "
        f"{json.dumps(a.get('args') or {}, separators=(',', ':'))[:140]} → "
        f"{str(a.get('result') or '').splitlines()[0][:90] if a.get('result') else '?'}" for a in last)
    return (f"RUNNER NOTE: the last session ended in a game crash ({why}). The last {len(last)} tool calls before it: "
            f"{calls}. Treat the last one as the likely trigger: don't repeat it at the same spot (a jump: walk "
            "there via route/WAYS instead; a spell/item: another one), and reconcile your notes with get_status.")


def game_started_at() -> float | None:
    """When the running game was launched (runtime/game.pid is written at each launch), or None."""
    try:
        return (renv.RUNTIME / "game.pid").stat().st_mtime
    except OSError:
        return None


POS_KEYS = ("pos", "pos_name", "party_pos")   # where the party stands: never a save-state change by itself


def made_progress(start_sig: dict | None, end_sig: dict | None) -> bool | None:
    """True/False, or None when it can't be told (a signature is missing). The position is left out: any step
    would count (session_progress counts a far move)."""
    if start_sig is None or end_sig is None:
        return None
    strip = lambda d: {k: v for k, v in d.items() if k not in POS_KEYS}
    return strip(start_sig) != strip(end_sig)


# A session also made progress when it scored this many story points (seen-sets only grow: a reload never scores
# twice) or ended this far from where it started on the same level. S040 descended the Shadowfell islands, met
# Nightsong and fought Balthazar (32 pts), wiped and reloaded: same save signature → "no progress" → CHANGE APPROACH.
PROGRESS_MIN_POINTS = 10
PROGRESS_MOVE_M = 60.0


def session_progress(start_sig: dict | None, end_sig: dict | None, points: int | None) -> tuple[bool | None, str]:
    """(progress, why): the save signature changed, or >= PROGRESS_MIN_POINTS story points, or a move of
    >= PROGRESS_MOVE_M; False only when it can be told; None when nothing is known (a crash, no reads)."""
    sig = made_progress(start_sig, end_sig)
    if sig:
        changed = [k for k in end_sig if k not in POS_KEYS and start_sig.get(k) != end_sig.get(k)]
        return True, "changed " + ", ".join(changed)
    if isinstance(points, (int, float)) and points >= PROGRESS_MIN_POINTS:
        return True, f"{points:g} story points"
    a, b = (start_sig or {}).get("pos"), (end_sig or {}).get("pos")
    if sig is False and a and b and math.dist(a, b) >= PROGRESS_MOVE_M:
        return True, f"moved {math.dist(a, b):.0f} m"
    if sig is None:
        return None, "unknown"
    return False, f"same save state, {points if points is not None else '?'} story points"


# A runner restart keeps a running game only if it still shows where the last session ended: between runs the
# orchestrator's acceptance tests load their own saves (S059 08:45 and S063 10:51 started on an Act-2 fixture /
# another save and the agent had to reload by itself). Party levels/XP are left out (summons and level-ups jitter).
DRIFT_KEYS = ("level", "journal", "journal_steps")
DRIFT_MOVE_M = 15.0


def state_drift(end_sig: dict | None, now_sig: dict | None) -> str | None:
    """Why the running game is not where the last session left it (end_sig), or None (same place, or unknown)."""
    if not end_sig or not now_sig:
        return None
    for k in DRIFT_KEYS:
        if k in end_sig and k in now_sig and end_sig[k] != now_sig[k]:
            return f"{k} {now_sig[k]} (the last session ended at {end_sig[k]})"
    a, b, who = end_sig.get("pos"), now_sig.get("pos"), "the party"
    name = end_sig.get("pos_name")
    if name and name in (now_sig.get("party_pos") or {}):   # the same member, whatever the party's order now
        b, who = now_sig["party_pos"][name], name
    if a and b and math.dist(a, b) > DRIFT_MOVE_M:
        return f"{who} stands {math.dist(a, b):.0f} m from where the last session ended"
    return None


def run_events(state_dir: Path, since: float) -> list[dict]:
    """runner_events.jsonl records at or after `since`."""
    out = []
    try:
        lines = (state_dir / "runner_events.jsonl").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("t", 0) >= since:
            out.append(e)
    return out


def crash_loop(events: list[dict], now: float, run_started: float, n: int = renv.CRASH_LOOP_FAULTS,
               window: float = renv.CRASH_LOOP_WINDOW_S) -> str | None:
    """Why the run should stop as a crash loop (n game crashes/hangs or setup failures within `window` of this run;
    a runner (re)start by the operator counts from its start), or None."""
    since = max(now - window, run_started - 1)
    bad = [e for e in events if e.get("t", 0) >= since and (
        (e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS) or e.get("kind") == "setup_failed")]
    if len(bad) < n:
        return None
    return f"crash loop: {len(bad)} game crashes/hangs/setup failures in {(now - min(e['t'] for e in bad)) / 60:.0f} min"


def day_cost(state_dir: Path, now: float) -> float:
    """The play agent's spend in the trailing COST_DAY_S (finished processes; run --max-cost-per-day)."""
    return runs.claude_totals(state_dir, now - renv.COST_DAY_S)["cost_usd"]


def status_line(now: float, playthrough: str, state: str, run_started: float, sessions: int, seen: dict | None,
                spent_day: float, cost_cap: float | None, faults: int) -> str:
    """runtime/runner.status.txt: what the runner does, for the orchestrator, in one line."""
    ms = milestones.status(seen)
    story = (f"milestones {ms['reached']}/{ms['total']} (Act {ms['act']})"
             + (": THE GAME IS FINISHED" if ms["ended"] else f", next {ms['next'] or '-'}"))
    return (f"{datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S} {playthrough}: {state} | up {(now - run_started) / 3600:.1f} h, "
            f"{sessions} session(s) | {story} | ${spent_day:.2f} in 24 h"
            + (f" (cap ${cost_cap:g})" if cost_cap else "")
            + f" | {faults} game fault(s) in {renv.CRASH_LOOP_WINDOW_S / 60:.0f} min (stop at {renv.CRASH_LOOP_FAULTS})")


def story_points(summary_path: Path) -> int | None:
    """Story progress points of a finished session (Sxxx.progress.json), or None."""
    try:
        return json.loads(summary_path.read_text()).get("points")
    except (OSError, ValueError, AttributeError):
        return None


def until_reached(seen: dict | None, milestone: str) -> dict | None:
    """{id, label} when milestones.status(seen) has `milestone` reached (the progress seen-state), else None."""
    item = next((i for i in milestones.status(seen or {})["items"] if i["id"] == milestone), None)
    return {"id": item["id"], "label": item["label"]} if item and item["reached"] else None


def read_seen(state_dir: Path) -> dict | None:
    """The playthrough's persistent progress seen-state (bg3ai.progress Timeline), or None."""
    try:
        return json.loads((state_dir / "progress_state.json").read_text())
    except (OSError, ValueError):
        return None


def keeper_model(option: str | None, play_model: str) -> str:
    """The memory keeper's model: --keeper-model, where "same" (the default) is the session's play model. A
    benchmark run then measures one model end to end, its own note-keeping included (BENCHMARK-BIAS.md)."""
    return play_model if not option or option == "same" else option


def is_stuck(history: list[dict], n: int = renv.STUCK_SESSIONS) -> bool:
    """The last n finished sessions each ended with no progress (unknowns don't count as stuck)."""
    recent = [h for h in history if h.get("kind", "play") == "play"][-n:]
    return len(recent) == n and all(h.get("progress") is False for h in recent)


# A concrete pre-flight for pace, right in the kickoff (the most salient spot): in claude-run-2 S001 the
# agent read the CLAUDE.md pipelining rules, then blocked on every walk anyway (31 wait=true walks, 0% of
# actions pipelined, idle median 2.27 s) and wrapped up 16 min early when a ladder bugged out. Coaching: a
# rules-only condition's kickoff has none (client_setup.kickoff_pace).
PACE_PREFLIGHT = bg3harness.prompts().pace_preflight("claude")


def build_kickoff(sid: str, playthrough: str, loaded_save: str | None, notes: list[str], briefing: str = "",
                  save_why: str = "", pace: str = PACE_PREFLIGHT) -> str:
    """The first message of a session. `briefing` is session.py start's memory injection (the runner opened the
    session): it leads the message, where the SessionStart hook used to put it. Without it (the runner couldn't open
    the session) the agent calls session_start. It names no deadline: every time shown before WRAP UP NOW was read
    as a clock to beat (S066-S082 wrapped up 7-13 min early)."""
    lines = [
        f"This is session {sid} of the BG3 playthrough '{playthrough}', started by the autonomous runner.",
        ("Your memory briefing (PLAYTHROUGH.md and HANDOFF.md) is above. " if briefing else
         "First call session_start: it shows your memory briefing (PLAYTHROUGH.md and HANDOFF.md). ")
        + "Run the session-start playbook (memory_read knowledge/playbooks/session-start.md), then "
        "play following CLAUDE.md. Your notes are files under memory/: use the memory_* tools and note.",
        "Keep playing until the ⏱ line in tool results says WRAP UP NOW: you'll be told when to wrap up. Then run "
        "the session-wrapup playbook; its last step is end_session, and you stop (no more tool calls) when that "
        "reports the commit. Nobody will answer questions: decide yourself and write down why.",
        *([pace] if pace else []),
    ]
    if briefing:
        lines.insert(0, briefing)
    if loaded_save:
        why = f" ({save_why})" if save_why else ""
        lines.append(f"The runner has already loaded save \"{loaded_save}\"{why}. Don't reload it; verify state "
                     "with get_status.")
    for note in notes:
        lines.append(note)
    return "\n\n".join(lines)


def resume_prompt(why: str, notes: list[str]) -> str:
    """The message that resumes a session's claude conversation after the runner recovered a crashed game."""
    lines = [f"RUNNER: the game {why}. The runner relaunched it and loaded the latest save; the same session "
             f"continues (you'll be told when to wrap up). Call get_status first: anything after that save is lost, so redo it "
             "and note the crash in the session log in one line. Then keep playing following CLAUDE.md."]
    return "\n\n".join(lines + notes)


def agent_resume_prompt(notes: list[str]) -> str:
    """The message that resumes a session whose claude process went silent and was stopped by the runner."""
    lines = [f"RUNNER: your previous turn produced no output for {renv.AGENT_SILENT_MINUTES:g} min, so the runner stopped "
             f"and resumed you; the game kept running. Same session (you'll be told when to wrap up). Call "
             "get_status, then keep playing following CLAUDE.md; keep each step short (no tool call or think longer "
             "than a minute)."]
    return "\n\n".join(lines + notes)


def stop_resume_prompt(reason: str) -> str:
    """The message that resumes a session whose claude ended its turn while the stop check (session.py stop, the old
    Stop hook) still blocks: keep playing, finish the wrap-up, or fix lint errors."""
    return f"RUNNER: you stopped, but this session can't end yet. {reason}"


def new_game_note(owner: str) -> str:
    return (f"RUNNER NOTE: this is a new game. Name your character exactly \"{owner}\" (create_character plan "
            f"\"name\": \"{owner}\"; create_character types it and verifies it before Venture forth). Saves are "
            "matched to this playthrough by that name.")


ROLEPLAY_MODES = ("strict", "light")
DIFFICULTIES = ("Explorer", "Balanced", "Tactician")   # new_game's
ROLEPLAY_TEXT = {
    "strict": "Roleplay: strict. Play as this character for the whole game: dialogue, companions, moral choices and "
              "combat style follow CHARACTER.md; odds, safety and approval only break ties between in-character "
              "options.",
    "light": "Roleplay: light. Keep the persona in dialogue and big choices; odds, safety and approval weigh as much "
             "as the character does.",
}


def load_profile(path: Path) -> dict:
    """playthroughs/<pt>/profile.json: {"character": {"brief", "name"?, "constraints"?, "roleplay"}, "difficulty"?}.
    A missing or broken file, or a character without a brief, means today's flow (no character block)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    c = raw.get("character")
    if isinstance(c, dict) and isinstance(c.get("brief"), str) and c["brief"].strip():
        out["character"] = dict(c, roleplay=c.get("roleplay") if c.get("roleplay") in ROLEPLAY_MODES else "strict")
    else:
        out.pop("character", None)
    if out.get("difficulty") not in DIFFICULTIES:
        out.pop("difficulty", None)
    return out


def update_profile(profile: dict, brief: str | None = None, roleplay: str | None = None,
                   difficulty: str | None = None) -> dict:
    """The profile with run --character-brief/--roleplay/--difficulty applied; other keys are kept."""
    out = json.loads(json.dumps(profile))
    c = dict(out.get("character") or {})
    if brief and brief.strip():
        c["brief"] = brief.strip()
    if roleplay:
        c["roleplay"] = roleplay
    if c.get("brief"):
        c.setdefault("roleplay", "strict")
        out["character"] = c
    if difficulty:
        out["difficulty"] = difficulty
    return out


def character_creation_note(profile: dict, name: str | None) -> str:
    """Phase 0 (CHAR-1): the first session of a new game with a character brief designs and creates the character."""
    c = profile.get("character") or {}
    lines = [
        "RUNNER NOTE: PHASE 0, CHARACTER CREATION. This is a new game and the playthrough has a character brief. "
        "Before any play, memory_read knowledge/playbooks/character-creation.md and follow it in order: new_game at the "
        "difficulty below, design the build and the persona from the brief, write memory/CHARACTER.md and the build "
        "plan in party-build.md (during new_game's load), then create_character with the name. Its result is the "
        "first look (name/race/class as the game spells them, objectives, what's nearby) and queues the first save "
        "itself: the runner learns the character's name from it (if the result says it wasn't started: "
        "save_game(label=\"S001-start\")). Then make the first move at once, with no more reads, and write "
        "PLAYTHROUGH.md and the log while it runs. Then keep playing as that character following CLAUDE.md.",
        "Character brief (the operator's words):\n> " + c.get("brief", "").strip().replace("\n", "\n> "),
    ]
    if c.get("constraints"):
        lines.append(f"Constraints: {c['constraints']}")
    lines.append(ROLEPLAY_TEXT[c.get("roleplay") if c.get("roleplay") in ROLEPLAY_TEXT else "strict"])
    d = profile.get("difficulty")
    lines.append(f"Difficulty: new_game(\"{d or 'Explorer'}\")" + ("." if d else " (the default; the profile sets none)."))
    if name:
        lines.append(f"Name: exactly \"{name}\" (create_character plan \"name\"); saves are matched to this "
                     "playthrough by it.")
    else:
        lines.append("Name: yours to choose, to fit the brief (create_character plan \"name\"). Make it distinctive: "
                     "it becomes this playthrough's save owner.")
    return "\n".join(lines)


def scenario_start_note(scenario: str | None, profile: dict, loaded: str | None) -> str:
    """The first session of a scenario run that starts from a save: no new game, the character is the save's."""
    c = profile.get("character") or {}
    lines = [
        f"RUNNER NOTE: SCENARIO START ({scenario}). This run starts mid-game from a fixed save, not a new game: the "
        f"runner loaded \"{loaded or 'the start save'}\". The character"
        + (f" ({c['name']})" if c.get("name") else "") + " is the save's: name, race, class, level, party, gear and "
        "journal are what the game shows. Your memory is new; HANDOFF.md holds the harness's short start note. "
        "Before playing on: get_status and get_party, then write memory/CHARACTER.md (the format in "
        "knowledge/playbooks/character-creation.md; header lines from the game) with the persona from the brief "
        "below, the party in party-build.md and the Character line in PLAYTHROUGH.md. Never start a new game.",
    ]
    if c.get("brief"):
        lines.append("Character brief (the operator's words):\n> " + c["brief"].strip().replace("\n", "\n> "))
    if c.get("constraints"):
        lines.append(f"Constraints: {c['constraints']}")
    lines.append(ROLEPLAY_TEXT[c.get("roleplay") if c.get("roleplay") in ROLEPLAY_TEXT else "strict"])
    return "\n".join(lines)


def fix_notes_prompt(lint_report: str) -> str:
    return ("The session is over, but `session.py end` refused to commit your memory because lint "
            "failed. Do NOT play or touch the game. Only fix the memory files so lint passes (memory_read, "
            "memory_edit, memory_write: each result shows the file's remaining lint errors), then call end_session "
            f"and stop (no more tool calls). You have {renv.FIX_SESSION_MINUTES} minutes.\n\nLint report:\n{lint_report[-6000:]}")


STUCK_DIRECTIVE = (
    "RUNNER DIRECTIVE, CHANGE APPROACH: the last {n} sessions ended with no measurable progress "
    f"(same place, party levels/XP and journal, under {PROGRESS_MIN_POINTS} story points, no far move: "
    "{what}). What you've been doing isn't working. "
    "Before anything else: run the stuck playbook, write three approaches in hypotheses.md that are "
    "genuinely different from the ones in your recent session logs, and pick one you haven't tried. "
    "Consider parking the blocked objective and pursuing a different quest, region or main-story lead.")


def stuck_directive(history: list[dict], n: int = renv.STUCK_SESSIONS) -> str:
    sig = next((h.get("end_sig") for h in reversed(history) if h.get("end_sig")), None) or {}
    # region/subregion as the map names them; the level id stays internal (FAIRNESS.md, audit C24)
    what = ", ".join(f"{k}={v}" for k, v in sig.items() if k in ("region", "subregion")) or "n/a"
    return STUCK_DIRECTIVE.format(n=n, what=what)


def _short(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def timing_event(ev: clients.Event) -> dict | None:
    """A non-claude client's Event in the stream-json shape bg3ai.metrics.StreamTiming reads (assistant text and
    tool calls, tool results, the process's result), so its turns.jsonl and latency report work the same; claude's
    raw events go to StreamTiming as they are. None: nothing StreamTiming times."""
    if ev.kind == "text":
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": ev.text}]}}
    if ev.kind == "tool_call":
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ev.call_id,
                                                              "name": ev.tool, "input": ev.args or {}}]}}
    if ev.kind == "tool_result":
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ev.call_id,
                                                         "content": ev.text, "is_error": ev.is_error}]}}
    if ev.kind == "result":
        return {"type": "result", "num_turns": ev.turns, "duration_api_ms": None,
                "usage": {"output_tokens": (ev.tokens or {}).get("output")}}
    return None


def gates_decision(char_name: str | None, owner: str | None, new_game: bool) -> bool | None:
    """Whether the save/load gates may be opened for the game that is up.

    True: a brand-new game, or the controlled character is the playthrough's save owner.
    False: a character is loaded and it's someone else (e.g. the user's own playthrough).
    None: can't tell (no name, or no owner known yet), so leave the gates alone.
    """
    if new_game:
        return True
    if not char_name or not owner:
        return None
    return char_name.strip().casefold() == owner.strip().casefold()


def save_identity(owner: str | None, env_prefix: str = SAVE_PREFIX) -> tuple[str, str]:
    """(BG3AI_SAVE_OWNER, BG3AI_SAVE_PREFIX) for the MCP server. The prefix is the runner's own $BG3AI_SAVE_PREFIX,
    else the owner plus the dash the game adds (`Claude-` for `Claude-<n>__<label>`). The <n> is per save, not per
    playthrough, so the folder name can't tell two playthroughs of one name apart. No owner yet (a new game before
    character creation): both empty, and the server adopts the name create_character typed."""
    owner = (owner or "").strip()
    return owner, env_prefix or (f"{owner}-" if owner else "")


def gates_env(allow: bool | None) -> dict:
    """BG3AI_SAVE_GATES for the MCP server: "1"/"0" = gates_decision opened/closed the save/load gates for this game
    (the server's _enable_save_gates follows it); absent when the runner couldn't tell (the server's own default)."""
    return {} if allow is None else {"BG3AI_SAVE_GATES": "1" if allow else "0"}


PLAY_SETTINGS = [
    # (op, args) applied before every session; cutscene autoskip is a project requirement.
    ("ui.config", {"autoskip": True}),
    ("ui.settings", {"show_tutorials": False}),
    ("ui.dismiss", {}),
]


def ui_strict_wanted(env) -> bool | None:
    """BG3AI_UI_STRICT: "1" on, "0" off, unset/anything else None (leave the game's mode alone; default off)."""
    return {"1": True, "0": False}.get(str(env.get("BG3AI_UI_STRICT", "")).strip())


def ui_strict_lua(on: bool) -> str:
    """Client Lua: set the strict mode, -> "<on> <refused> <lua timeouts> <ui timeouts>" or "missing"."""
    return ("local f = Ext._Internal and Ext._Internal.UiStrict if type(f) ~= 'function' then return 'missing' end "
            f"f({'true' if on else 'false'}) local s, n = f() local _, lt, ut = Ext._Internal.UiSerialize() "
            "return tostring(s) .. ' ' .. tostring(n) .. ' ' .. tostring(lt) .. ' ' .. tostring(ut)")


def ui_strict_text(reply: str) -> str:
    """ui_strict_lua's reply as one log line."""
    parts = str(reply).split()
    if len(parts) != 4 or parts[0] not in ("true", "false"):
        return f"not set ({reply!r}: the running bg3le lacks patch 0010?)"
    return (f"{'ON' if parts[0] == 'true' else 'off'} (refused {parts[1]}, lock timeouts lua {parts[2]} "
            f"ui {parts[3]})")
