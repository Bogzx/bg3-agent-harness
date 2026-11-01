"""Batch tools: do_actions, queue_actions, cancel_queue and wait_for.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .. import actq, delta, longnav, macros, render, rules, turn
from ..game import GameError, JobResult, NotReady
from . import combat, core, memory, overview, plumbing, reporting, travel

# ---------------------------------------------------------------------------
# batch: several actions per decision
# ---------------------------------------------------------------------------

_BATCHABLE = {
    "move_to", "interact", "pick_up", "loot", "loot_corpse", "talk_to", "jump_to", "set_party", "use_ability",
    "end_turn", "choose_dialogue", "roll_check", "react", "manage_inventory", "wait", "press_key",
    "place_marker", "camp", "short_rest", "walk_toward", "travel_to",
}


def _as_json(arg: Any) -> str:
    """A batch argument as the JSON text the step parsers take: clients send a real JSON array or object, or the same
    as a string (D15: one interface for every model)."""
    return arg if isinstance(arg, str) else json.dumps(arg)


@core.mcp.tool()
def do_actions(actions: list[dict[str, Any]] | dict[str, Any] | str, wait: bool = True, ignore: str = "",
               note: str = "") -> str:
    """Run several actions in order in ONE call: a whole combat turn, a looting sweep, a dialogue chain. Plan from one observation, then execute.
    actions: a list of steps (or the same list as a JSON string), e.g.
    [{"tool":"move_to","target":"e12"},{"tool":"use_ability","ability":"Target_MainHandAttack","target":"e12","unless":"hp_below:30"},{"tool":"use_ability","ability":"Shout_SecondWind","if":"hp_below:50"},{"tool":"end_turn"}]
    Allowed: move_to, walk_toward, travel_to (a whole trip as one step, e.g. after a climb), interact, pick_up, loot, loot_corpse, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
    Optional per step: "if"/"unless" with in_combat, in_dialogue, my_turn, hp_below:<pct>, item_present:<id>, target_visible:<id> (a failed condition skips the step).
    Stops and drops the rest on: combat or a conversation starting, a reaction/roll/popup, a new hostile in view, HP falling below 35%, any damage taken outside combat, stepping into a harmful surface (fire, acid, ...), a trap spotted on the way, a blocked teleport, or a failed step; and before a step once the call has run 90 s (time budget, a wait cut to fit). ignore="hostile,hp" disables some (combat, dialogue, prompt, hostile, hp, damage, surface, teleport, trap).
    Returns each step's outcome, why it stopped (and what was left), a NOW line and CHANGES (position, HP, items, new things in view, mode). wait=false queues the steps instead (see queue_actions).
    note: memory log line(s) appended to your session log at once, as note() does (no extra turn)."""
    t0 = time.monotonic()
    actions = _as_json(actions)
    if refused := session._note_refused(note):
        return reporting._finish("do_actions", {}, refused, t0)
    if not wait:
        return queue_actions(actions, ignore=ignore, note=note)
    memory._note_quiet(note)
    try:
        steps = actq.parse_steps(actions, _BATCHABLE)
    except actq.StepError as e:
        return f"FAILED: {e}"
    _hold_for_early_walks(None)  # a batch issued in a walk's last seconds starts after the arrival
    ex = queueing._make_executor(None, ignore)
    ex.budget_s = actq.BATCH_BUDGET_S  # the client waits for this call; a queue runs while the agent plays on
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
    body = queueing._report_text(rep, len(steps))
    if rep.stopped:
        reporting._batch["last"] = "interrupted"
    changes = delta.changes_text(core.game, core.H)
    if changes:
        body += "\n" + changes
    return reporting._finish("do_actions", {"n": len(steps)}, body, t0)


@core.mcp.tool()
@reporting._guard
def queue_actions(actions: list[dict[str, Any]] | dict[str, Any] | str, who: str | None = None,
                  replace: bool = False, ignore: str = "", note: str = "") -> str:
    """Append steps to a character's action queue and return at once; they run back to back while you observe and plan the next move. Same step format, conditions and interrupts as do_actions. On the end you get a queue_done or queue_interrupted event (why, what was left) in any later result; wait_for('queue') blocks until it is done. replace=true drops what was still queued first. Each character has its own queue, so two characters can act at once. Your own direct action for that character pre-empts the queue. note: memory log line(s) for your session log, written at once (as note())."""
    t0 = time.monotonic()
    actions = _as_json(actions)
    if refused := session._note_refused(note):
        return reporting._finish("queue_actions", {}, refused, t0)
    memory._note_quiet(note)
    if core._session_over():
        return reporting._finish("queue_actions", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    try:
        steps = actq.parse_steps(actions, _BATCHABLE)
    except actq.StepError as e:
        return reporting._finish("queue_actions", {}, f"FAILED: {e}", t0)
    body = _enqueue(plumbing._who(who), steps, replace, ignore)
    return reporting._finish("queue_actions", {"n": len(steps)}, body, t0)


def _enqueue(char: str, steps: list, replace: bool = False, ignore: str | None = "") -> str:
    """Add steps to char's queue (ignore=None keeps the queue's current interrupt settings)."""
    # review M4/M5: a background end still running for char: wait for it, so these steps get their own executor (the
    # end's ignores every interrupt) and a replace doesn't cancel the end
    held = turn_tool._hold_for_bg_end(char) if char in turn_tool._bg_ends else ""
    if ignore is not None or char not in queueing._queue_ignore:
        queueing._queue_ignore[char] = queueing._parse_ignore(ignore or "")
    n, mine = len(steps), []
    if not queueing._queues().busy(char):
        # A walk started with move_to/walk_toward is still going: the queue's first action would supersede
        # it (one action per character), so the queue first waits for it to arrive.
        mine = [h for h in queueing._running_jobs() if plumbing._async.get(h, {}).get("char") == char]
        steps = [actq.Step("wait_for", {"target": h, "timeout_s": 60}) for h in mine] + list(steps)
    core.METRICS.bg_start(f"queue:{char}")
    q = queueing._queues().add(char, steps, replace=replace)
    with queueing._queues().lock:
        waiting = len(q.steps)
    after = f", after {'/'.join(mine)} arrives" if mine else ""
    return (held + "\n" if held else "") + f"queued {n} step(s) for {queueing._label(char)} ({waiting} waiting{after})"


def _travel_async(char: str, w: dict, marker: str, objective: str, target: str, x, y, z, t0: float,
                  avoid: str = "auto", waited: bool = False, fast: bool = True, trespass: bool = False) -> str:
    """travel_to without waiting: check the destination now (bad ones fail at once), then run the trip
    as a queue entry for that character (the queue worker calls travel_to again, batch-active)."""
    args: dict[str, Any] = {k: v for k, v in (("marker", marker), ("objective", objective), ("target", target),
                                              ("x", x), ("y", y), ("z", z)) if v not in ("", None)}
    if avoid != "auto":
        args["avoid_hazards"] = avoid
    if trespass:
        args["trespass"] = True
    dest = None
    if target:
        what, dist_m = target, None
        plumbing._uuid(target)  # an unknown id fails now
    else:
        dest, what = macro_tools._travel_dest(marker, objective, x, y, z)
        me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
        dist_m = macros.dist(me.get("pos"), dest) if me.get("pos") else None
    if core._session_over():
        return reporting._finish("travel_to", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    pre = []
    if dest is not None:
        ft = _fast_travel_first(char, dest, dist_m) if fast else None
        if ft:
            pre.append(ft[0])
            dist_m = ft[1] if ft[1] is not None else dist_m
        floor = travel_macro._other_floor(char, travel_macro._pos_now(char) if ft else me.get("pos"), dest)
        if floor and not floor.startswith("REFUSED"):
            pre.append(floor)
        elif floor:
            return reporting._finish("travel_to", {"to": what}, "".join(p + "\n" for p in pre) + f"travel_to {what}: {floor}", t0)
        warn = longnav.start_warning(DEAD.for_goal(dest))
        if warn:
            pre.append(warn)
    queued = _enqueue(char, [actq.Step("travel_to", args)], ignore=None)
    if dest is not None and dist_m is not None:
        _trips[char] = {"dest": dest, "d0": dist_m, "t0": time.monotonic()}
    body = "".join(p + "\n" for p in pre)
    body += f"travel_to {what}: started" + (f" ({render.num(round(dist_m, 1))}m)" if dist_m is not None else "")
    # Settle window: a trip that stops at once (hazard, no ground, an interrupt) or a short one comes back now.
    if plumbing._settle(lambda: None if queueing._queues().busy(char) else {"status": "ended"}) is not None:
        _trips.pop(char, None)
        return reporting._finish("travel_to", {"to": what}, body + "\n" + (queueing._last_queue_text(char) or "queue: ended"), t0)
    prog = _trip_progress(char)
    body += (f"; {prog}" if prog else "") + f"; {queued}."
    if waited:
        body += " (wait=true ignored: the trip runs while you plan)"
    hint = reporting._next_walk_hint()
    return reporting._finish("travel_to", {"to": what}, body + ("\n" + hint if hint else ""), t0, issued=True)


def _fast_travel_first(char: str, dest: list, dist_m: float | None) -> tuple[str, float | None] | None:
    """A long trip with an unlocked waypoint much nearer the goal (longnav.pick_waypoint): fast travel there
    first, as a player would (S011: travel_to Zevlor 463 m on foot, ETA 6 min, with Emerald Grove Environs
    next to him). Returns (line, metres left after it) or None (no shortcut, or the fast travel failed: walk)."""
    if dist_m is None or dist_m < longnav.FT_MIN_M:
        return None
    if travel_macro._door_link(char, dest):
        return None  # B4 (S037): the way is through a door the party used (the trip goes through it), not a waypoint's
    wps = (core.game.try_rpc("nav.waypoints", {"pos": dest}) or {}).get("waypoints")
    here = travel_macro._pos_now(char)
    wp = longnav.pick_waypoint(here, dest, wps) if here else None
    if not wp:
        return None
    # S074 23:00:54: travel_to(x,y,z) 1.9 km from inside Szarr Palace ended at once as a bare `FAILED: can't fast
    # travel now (Location)` (the Lua refusal raised through here), with no way out and no walk. A Location block
    # gets fast_travel's text and the walk goes on; a block remembered for this level skips the try.
    if travel.AREA.block is not None:
        st = core.game.try_rpc("state.status") or {}
        if travel.AREA.current(st.get("level")) is not None:
            return (f"fast travel to {wp['name']} skipped: the game blocks travel here (story area, Location)."
                    + travel._area_refusal("travel_to", st) + " Walking instead.", None)
    left = travel._leave_camp_first()
    if left.startswith("FAILED"):
        return (f"fast travel to {wp['name']}: {left}: walking instead", None)
    sub = overview.minimap_sub()
    try:
        r = core.game.run_job("ui.fast_travel", {"waypoint": wp["name"]}, context="client", wait_s=70)
    except NotReady:
        raise
    except GameError as e:
        r = JobResult("failed", None, str(e))
    if r.status != "done" and "(Location)" in str(r.error):
        return (f"fast travel to {wp['name']}: can't fast travel now (Location): this area does not allow travel (a "
                "dungeon or story area; camp() is blocked too)." + travel._area_refusal("travel_to") + " Walking instead.",
                None)
    if r.status != "done":
        return (f"fast travel to {wp['name']} failed ({r.error or r.status}): walking instead", None)
    travel._trip_arrived()
    overview.trip_arrived(sub, f"the trip to {wp['name']}")
    now = None
    for _ in range(20):  # the party reappears at the waypoint after the load
        p = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
        now = next((q.get("pos") for q in p if q.get("uuid") == char), None)
        if now and macros.dist(now, wp["pos"]) < 40:
            break
        time.sleep(0.5)
    left = macros.dist(now, dest) if now else None
    return (f"fast travel to {wp['name']} first ({render.num(wp['to_goal_m'])}m from it; you were "
            f"{render.num(round(dist_m))}m away): done", left)


_trips: dict[str, dict] = {}  # char -> {dest, d0, t0} of the travel_to its queue is running (live progress)


def _map_level() -> str | None:
    """The level the party is on (state.status), for the dead ends: they apply only on the level they were hit on,
    and only until a wall/door near the party changes (geo_gen, V13: a dead end before a wall opened is no longer one)."""
    st = core.game.try_rpc("state.status")
    if not isinstance(st, dict) or not st.get("level"):
        return None
    return f"{st['level']}#{st['geo_gen']}" if st.get("geo_gen") is not None else st["level"]


# where trips were blocked this session (S010: the same boulder wall 5 times); per level, cleared by load_game
DEAD = longnav.DeadEnds(level=_map_level)


def _trip_left(char: str) -> tuple[float, float | None] | None:
    """(metres left, ETA s or None) of a running travel_to: one act.watch read, ETA from the speed so far."""
    trip = _trips.get(char)
    if not trip:
        return None
    pos = travel_macro._pos_now(char)
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
    for h in queueing._running_jobs():
        info = plumbing._async[h]
        if info.get("eta") is not None:
            left = float(info["eta"]) - (time.monotonic() - info["t0"])
            if best is None or left > best[1]:
                best = (f"{h}@{info['t0']}", left)
    for c, trip in list(_trips.items()):
        if not queueing._queues().busy(c):
            continue
        tl = _trip_left(c)
        if tl is None:
            continue
        left = tl[1] if tl[1] is not None else tl[0] / TRIP_MPS
        if best is None or left > best[1]:
            best = (f"{c}@{trip['t0']}", left)
    return best


def _notes_window() -> str:
    """'NOTES WINDOW: ~20s of walking left ...' once per long job/trip ('' otherwise, and in a condition without
    coaching: rules.coaching)."""
    if not rules.coaching():
        return ""
    lr = _long_running()
    if lr is None or lr[1] < NOTES_MIN_S or lr[0] in _notes_shown:
        return ""
    _notes_shown.add(lr[0])
    plan = overview._LEVEL_NUDGE.plan_hint(core.MEMORY)
    return (f"NOTES WINDOW: ~{render.num(round(lr[1]))}s of walking left: note() what happened / plan now "
            "(not after it ends, when the character would stand idle); queue_actions the arrival first"
            + ("\n" + plan if plan else ""))


@core.mcp.tool()
@reporting._guard
def cancel_queue(who: str | None = None) -> str:
    """Stop a character's queue (who) or every queue (who empty): the running step is cancelled and the rest dropped."""
    t0 = time.monotonic()
    wu = plumbing._who_uuid(who) if who else None
    held = turn_tool._hold_for_bg_end(wu)  # review M5: a background turn end is never cancelled half-way
    out = queueing._queues().cancel(wu)
    if not out:
        return reporting._finish("cancel_queue", {}, (held + "\n" if held else "") + "no queue was running", t0)
    lines = ([held] if held else []) + [f"cancelled {queueing._label(w)}: " + (f"stopped {run}, " if run else "")
                                        + f"dropped {n}" for w, n, run in out]
    return reporting._finish("cancel_queue", {}, "\n".join(lines), t0)


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
        view["combat"] = core.game.try_rpc("state.combat") or {}
    return view["combat"]


def _wait_acting(view: dict) -> list[str]:
    """Party members whose turn it is ([] out of combat)."""
    cs = _wait_combat(view)
    if not cs.get("active"):
        return []
    if "party" not in view:
        view["party"] = turn_plan._party_set()
    return turn.party_acting(cs, view["party"])


def _wait_state(low: str, tgt: str, kinds: set, ui: dict, view: dict) -> str | None:
    """What the current state already answers for a wait target (None: nothing yet). `view`: see _wait_combat."""
    scr = ui.get("screen") if isinstance(ui, dict) else None
    if low == "idle" or "idle" in kinds:
        if not queueing._queues().busy() and not queueing._running_jobs():
            return "idle: no action running"
    if low == "queue" or low.startswith("queue:") or "queue" in kinds:
        who = plumbing._uuid(tgt.split(":", 1)[1]) if low.startswith("queue:") else None
        if not queueing._queues().busy(who):
            return queueing._last_queue_text(who) or "queue: empty"
    if low == "my_turn" or "my_turn" in kinds:
        if not _wait_combat(view).get("active"):
            return "not in combat"
        if _wait_acting(view):
            return "\n".join([combat._now_turn_text(_wait_acting(view))] + turn.disabled_lines(_wait_combat(view), queueing._label))
    if not kinds:
        return None
    for k in kinds & set(_PROMPT_SCREENS_WAIT):
        if scr == k:  # the prompt is already up: its screen change is never coming
            return f"{k} on screen\n" + "\n".join(reporting._render_ui(ui))
    d = (ui.get("dialogue") or {}) if isinstance(ui, dict) else {}
    if scr == "dialogue" and kinds & {"dialogue_started", "dialogue_options"} and (
            "dialogue_started" in kinds or d.get("options")):
        # Already open (its event went out in an earlier result): live S002 waited the full 5 s here.
        return "dialogue open\n" + "\n".join(reporting._render_ui(ui))
    if "dialogue_ended" in kinds and scr == "game":
        return "no dialogue open"
    if "combat_ended" in kinds and not _wait_combat(view).get("active"):
        return "not in combat"
    if "combat_started" in kinds and _wait_combat(view).get("active"):
        return "already in combat"
    return None


def _quiet(scr: str | None, view: dict) -> bool:
    """Nothing of ours runs and the game waits on us: plain game screen, no job, no queue, not an enemy turn."""
    if scr not in (None, "game") or queueing._queues().busy() or queueing._running_jobs():
        return False
    return not _wait_combat(view).get("active") or bool(_wait_acting(view))


# Early wake (impl-act "pipelining stuck"): in exploration the agent waits for arrival, then thinks ~2 s while
# the character stands. wait_for on a walk returns when about one think time (lead_s) of it is left, so the
# agent plans the next step during the last seconds; what it then issues for that character runs after the
# walk arrives (_hold_for_early_walks), never cancelling it. Not in combat (the turn needs the real arrival).
# ACT-P4 (naut2 runs 1-2): of 5 early wakes, 1 was used as meant (loot steps queued for the arrival) and 4 were
# followed at once by wait_for(lead_s=0): one more model turn each (~10 s lost). Off by default now; lead_s=2 opts
# in per call. LEAD_S = 2.0 brings the old default back.
LEAD_S = 0.0  # default lead (0 = wait for the arrival)
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
        jobs = queueing._running_jobs()
    for h in jobs:
        info = plumbing._async.get(h)
        if not info or info.get("tool") not in plumbing._WALKS or info.get("eta") is None:
            return []
        out.append((f"{h}@{info['t0']}", info.get("char"), float(info["eta"]) - (now - info["t0"]),
                    f"{h} {info['tool']}", {"char": info.get("char"), "job": h}))
    if low in ("idle", "queue") or low.startswith("queue:"):
        who = plumbing._uuid(low.split(":", 1)[1]) if low.startswith("queue:") else None
        for q in queueing._queues().status():
            if who is not None and q["who"] != who:
                continue
            trip = _trips.get(q["who"])
            if not (q["running"] or "").startswith("travel_to") or q["waiting"] or not trip:
                return []  # arrival steps still to run, or not a trip: the queue is not nearly done
            tl = _trip_left(q["who"])
            if tl is None or tl[1] is None:
                return []
            out.append((f"queue:{q['who']}@{trip['t0']}", q["who"], tl[1], f"queue {queueing._label(q['who'])} travel_to",
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
    who = sorted({queueing._label(w[1]) for w in walks if w[1]})
    return (f"{', '.join(w[3] for w in walks)}: arriving, ETA ~{render.num(round(left, 1))}s (early wake, "
            f"lead_s={render.num(lead_s)}): plan the next step now. What you issue"
            + (f" for {', '.join(who)}" if who else "") + " runs after the walk arrives (it does not cancel it).")


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
            if g.get("job") and g["job"] in queueing._running_jobs():
                return True
            if g.get("queue") and queueing._queues().busy(g["char"]):
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



@core.mcp.tool()
@reporting._guard
def wait_for(target: str = "idle", timeout_s: float = 15.0, lead_s: float = LEAD_S) -> str:
    """Block only until something happens, then report it plus events and CHANGES. target: a job id (j12) from an async action; 'queue' (all queues done) or 'queue:<who>'; 'idle' (no async job or queue running); 'my_turn' (a party member can act, or combat ended); or event kinds, comma-separated (e.g. 'combat_started,dialogue_started,job_finished'). Also returns early when a reaction/roll/dialogue/popup or a rest/level-up/trade screen appears. Returns at once when the state already answers it or nothing running could cause it. A walk/travel_to (job id, 'queue', 'idle') is waited to its arrival; lead_s=2 (outside combat) returns once ~2 s of it are left instead, only for when you will queue the arrival step without needing its result: what you issue then runs after the arrival, never cancelling it."""
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
            ui = core.game.try_rpc("ui.state", context="client") or {}
            ui = ui if isinstance(ui, dict) else {}
            scr = ui.get("screen")
            if screen0 is None:
                screen0 = scr
                if kinds and scr in _INPUT_SCREENS_WAIT:
                    body = f"not waiting: {_input_screen_text(scr)}"
                    noop = "screen"
                    break
            elif scr in _PROMPT_SCREENS_WAIT and scr != screen0:
                body = f"stopped waiting: {scr} on screen\n" + "\n".join(reporting._render_ui(ui))
                break
            elif scr in _INPUT_SCREENS_WAIT and scr != screen0:
                body = f"stopped waiting: {_input_screen_text(scr)}"
                break
        view.pop("combat", None)  # fresh each tick (the party set is kept)
        scr = ui.get("screen")
        if re.fullmatch(r"j\d+", low):
            if reporting._batch["active"] and reporting._batch["exec"] is not None and low in plumbing._async:
                reporting._batch["exec"].track(plumbing._async[low]["ctx"], plumbing._async[low]["id"])  # cancel_queue cancels it too
            jv = core.game.rpc("job.poll", {"id": int(low[1:])})
            if jv.get("status") != "running":
                info = plumbing._async.pop(low, {})
                core.METRICS.bg_end(low)
                r = JobResult(jv["status"], jv.get("result"), jv.get("error"),
                              elapsed_ms=jv.get("elapsed_ms", 0))
                plumbing._remember_names(r.result)
                body = plumbing._job_text(info.get("tool") or low, r, info.get("char"))
                if info.get("tool") in ARRIVAL_LEAD_TOOLS and r.status == "done" and not reporting._batch["active"] and \
                        not ((r.result or {}).get("after") or {}).get("in_combat"):
                    body += _arrival_leads()
                noop = "job_done" if first else None
                break
        else:
            body = _wait_state(low, tgt, kinds, ui, view)
            if body is not None:
                noop = "already" if first else None
                break
        if not kinds and lead_s and not reporting._batch["active"] and now - early_at >= EARLY_CHECK_S:
            early_at = now
            body = _early_wake(low, lead_s, view)
            if body is not None:
                noop = "early" if first else None
                break
        if kinds:
            hit = queueing._peek_events(kinds)
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
            elif quiet and busy_seen == 0.0 and reporting._noop_waits["n"] >= 1:
                # S023 06:47-06:53: 20 waits for combat_started in a row with only reads (look_at, session_clock)
                # between them, 4 s + a model turn each. The second one in a row answers at once.
                body = (f"nothing is running that would cause {tgt} — again: only reads ran since the last wait said "
                        "so, and waiting never starts a fight or a conversation. Act: move_to/walk_toward, talk_to, "
                        "interact, or queue_actions")
                noop = "repeat"
                break
            elif quiet and now - max(t0, busy_seen) >= queueing.IDLE_EVENT_WAIT_S:
                # S009: 'dialogue_started' after a finished 3 m walk waited its full 10 s, and the last minutes
                # went to 45+30+40 s waits with nothing running. Nothing of ours can cause it: say so now.
                body = (f"nothing is running that would cause {tgt} (no job, no queue, no prompt) after "
                        f"{render.num(round(now - t0, 1))}s: act instead (talk_to, move_to, queue_actions)")
                break
        first = False
        if now > deadline:
            body = f"still waiting for {tgt} after {render.num(round(timeout_s, 1))}s" + (
                "\n" + queueing._running_text() if queueing._running_text() else "")
            break
        time.sleep(0.1)
    if (low in ("queue", "idle") or low.startswith("queue:")) and not reporting._batch["active"] and "LEADS" not in body:
        steps = re.findall(r"(?m)^\d+\. (\w+)", body)
        if steps and steps[-1] in ARRIVAL_LEAD_TOOLS and re.search(r": done \(\d+ steps?\)", body) \
                and not (view.get("combat") or {}).get("active"):
            body += _arrival_leads()
    changes = delta.changes_text(core.game, core.H)
    if changes:
        body += "\n" + changes
    return reporting._finish("wait_for", {"target": tgt}, body, t0, extra={"noop": noop} if noop else None)


# S023-S027: after a walk's wait_for the next call was get_frontier (22x), get_nearby (18x) or route (18x), each a
# ~2 s model turn to learn where to go on from here. The arrival result now carries the best unvisited leads + WAYS.
ARRIVAL_LEAD_TOOLS = {"move_to", "walk_toward", "travel_to", "jump_to"}
ARRIVAL_LEADS = 5


def _arrival_leads() -> str:
    """'\nLEADS (unvisited; get_frontier lists all): e529 Damaged Vase <usable> 8.3m E | …' + the WAYS line."""
    # NAV-6: at arrival the warm-up hasn't run yet (300 ms of stillness); ways only from its answers, no cold searches
    res = core.game.try_rpc("nav.frontier", {"ways_cached": True})
    if not isinstance(res, dict) or not isinstance(res.get("leads"), list):
        return ""
    plumbing._remember_names(res)
    new = [l for l in res["leads"] if isinstance(l, dict) and l.get("unvisited")]
    out = []
    if new:
        shown = render.render_frontier({"leads": new[:ARRIVAL_LEADS]}, core.H).splitlines()
        more = len(new) - ARRIVAL_LEADS + (res.get("more") or 0)
        out.append("LEADS (unvisited; get_frontier lists all): " + " | ".join(x.replace(" unvisited", "") for x in shown)
                   + (f" | +{more} more" if more > 0 else ""))
    ways = render.render_ways(res.get("ways"))
    if ways:
        out.append(ways)
    return ("\n" + "\n".join(out)) if out else ""


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    queueing, turn_plan, turn_tool, macro_tools, travel_macro, session,
)
