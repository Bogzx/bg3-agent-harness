"""Macros (macros.py): loot_area, dialogue_advance and their helpers.
"""

from __future__ import annotations

import time
from typing import Any

from .. import actq, delta, macros, render
from ..game import GameError
from . import core, overview, plumbing, queueing, reporting, turn_plan

# ---- macros ----

_looted: set[str] = set()  # containers/corpses loot_area already emptied this bridge session


_IN_CONVO = "in a conversation"  # Guard's refusal: "character is in a conversation (InDialog)"


def _convo_refused(err: Any) -> bool:
    """A refusal for a conversation: the Guard's 'character is in a conversation (...)' or camp_travel's 'cannot do
    that during a conversation' ('in a conversation' is no substring of that one)."""
    e = str(err or "")
    return _IN_CONVO in e or "during a conversation" in e

CONVO_WAIT_S = 6.0  # a step refused for a conversation with none to answer: retry this long (banter, a stale flag,
# a trader's goodbye line; 4.0 until 2026-10-16: a voiced closing line runs 2-5 s)


def _dialogue_needs_answer(ui: Any = None) -> bool:
    """A conversation the agent must answer is on screen: a dialogue screen showing answers (or one we can't read).
    A dialogue screen with no answers (a trader's goodbye after the trade closed, a line still being voiced, a
    closing line) ends by itself: callers wait it out instead of failing (live window 2026-10-16, trade → camp)."""
    if ui is None:
        ui = core.game.try_rpc("ui.state", context="client")
    if not isinstance(ui, dict) or ui.get("screen") != "dialogue":
        return False
    d = ui.get("dialogue")
    return not isinstance(d, dict) or bool(d.get("options"))


def _retry_after_dialogue(watcher: actq.Watcher, who: str | None, step: actq.Step, rep: actq.Report,
                          text: str) -> tuple[actq.Report, str, str | None]:
    """A macro step refused "in a conversation". Live S021: loot_area right after a fight hit Karlach's
    post-combat conversation and failed all 4 corpses the same way. A dialogue on screen is the agent's
    to answer: stop the macro at once (why). None on screen (ambient banter, a flag a moment stale after a
    dialogue ended): retry the step until the game lets it, up to CONVO_WAIT_S."""
    end = time.monotonic() + CONVO_WAIT_S
    while True:
        if _dialogue_needs_answer():
            return rep, text, "a conversation is open: answer it (choose_dialogue), then run this again"
        if time.monotonic() >= end:
            return rep, text, (f"still in a conversation after {CONVO_WAIT_S:g}s with no dialogue on screen "
                               f"({text.split(': ', 1)[-1]})")
        time.sleep(0.4)
        rep, text = turn_plan._run_one(watcher, who, step)
        if rep.done or rep.stop_kind not in (None, "failed") or _IN_CONVO not in text:
            return rep, text, None


_LOOT_HARD_STOPS = {"combat", "dialogue", "prompt"}  # _macro_groups: these stop before an open loot window's take


def _macro_groups(who: str | None, groups: list, ignore: set | None = None,
                  done: list | None = None) -> tuple[list[str], str | None, list[str]]:
    """Run groups of steps [(label, [Step])] in order. A failed step skips the rest of its group (that
    target); an interrupt stops everything. Returns (lines, why it stopped or None, labels not reached);
    `done` collects the indices of groups whose steps all succeeded."""
    watcher = actq.Watcher(queueing._watch_conn(), ignore=ignore or set())
    watcher.baseline()
    lines: list[str] = []
    for gi, (label, steps) in enumerate(groups):
        parts = []
        ok = True
        for step in steps:
            rep, text = turn_plan._run_one(watcher, who, step)
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
                    quiet = actq.Watcher(queueing._watch_conn(), ignore=set(actq.INTERRUPT_KINDS))
                    quiet.baseline()
                    rep2, text2 = turn_plan._run_one(quiet, who, rest[0])
                    parts.append(text2)
                    if rep2.done and done is not None and len(rest) == 1:
                        done.append(gi)
                lines.append(f"{label}: " + "; ".join(parts))
                return lines, queueing._label(rep.stopped or rep.stop_kind or ""), [g[0] for g in groups[gi + 1:]]
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
        reporting._batch["last"] = "interrupted"
    changes = delta.changes_text(core.game, core.H)
    if changes:
        lines.append(changes)
    return reporting._finish(tool, args, "\n".join(lines), t0)


def _no_combat(tool: str) -> dict:
    w = queueing._watch_conn().try_rpc("act.watch") or {}
    if w.get("combat"):
        raise GameError(f"{tool} is for outside combat")
    return w


@core.mcp.tool()
@reporting._guard
def loot_area(radius: float = macros.LOOT_RADIUS, who: str | None = None, corpses: bool = True,
              containers: bool = True, opened: bool = False) -> str:
    """Loot every VISIBLE corpse and unlocked container within radius (m), nearest-first walking order: walk, open, take all. Skips locked ones, owned ones (stealing), discovered armed traps, already opened ones (opened=true includes them) and what this session already looted. Stops at the first interrupt (combat, conversation, prompt, new hostile, HP drop)."""
    t0 = time.monotonic()
    w = _no_combat("loot_area")
    char = plumbing._who(who)
    me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
    near = core.game.rpc("state.nearby", {"radius": max(2.0, min(float(radius), 30.0))})
    plumbing._remember_names(near)
    targets, skipped = macros.loot_targets(near, me.get("pos"), float(radius), _looted, corpses, containers,
                                           opened=opened)
    if not targets:
        body = f"nothing to loot within {render.num(radius)}m" + (f" (skipped: {'; '.join(skipped)})" if skipped else "")
        return reporting._finish("loot_area", {"radius": radius}, body, t0)
    groups = []
    for t in targets:
        h = core.H.get(t["uuid"], t.get("name"))
        first = actq.Step("loot_corpse", {"corpse": h}) if t["kind"] == "corpse" else actq.Step("interact", {"target": h})
        groups.append((f"{h} {t.get('name') or ''}".strip(), [first, actq.Step("loot", {"take": "all"})]))
    ok: list[int] = []
    lines, stop, left = _macro_groups(char, groups, done=ok)
    for i in ok:
        _looted.add(targets[i]["uuid"])
    if len(ok) < len(lines):
        # A failed take (an empty body) can leave the loot window open: close it so the world is usable.
        core.game.try_rpc("ui.loot", {"close": True}, context="client")
    head = f"loot_area: {len(targets)} target(s) within {render.num(radius)}m"
    if skipped:
        head += f"; skipped {'; '.join(skipped)}"
    return _macro_finish("loot_area", {"radius": radius}, [head] + lines, stop, left, t0)


# A voiced or cinematic line still playing: wait this long for the next prompt before handing back (every return
# here costs a model turn; S003's Helm cinematic took 6 empty advance/wait turns at the old 8 s).
DIALOGUE_LINE_WAIT_S = 15.0
# dialogue_advance with no conversation on screen: when the game says no party member is in one (nav.activity), wait
# only this long for one to open instead of open_s (2.5 s). naut series: 2-3 calls a run after a conversation had
# already ended ("0 filler line(s), STOPPED: conversation ended" at 2.7 s each). 2.5 reverts it.
DIALOGUE_IDLE_OPEN_S = 0.5
# naut series (helm): "*The helm's alien transponder…*" played in the party's combat turn, AutoSkip (then) skipped
# nothing there, and 2 dialogue_advance + 1 get_combat_state sat the whole 15 s on it, ~30-45 s a run. A line AutoSkip
# says it can't skip (autoskip.status line.blocked: "our turn" with its switch off, or its tries used up; or AutoSkip
# off) hands back once it has played this long, saying so. 0 = off (wait the full DIALOGUE_LINE_WAIT_S).
DIALOGUE_STUCK_EXIT_S = 2.0
# The bridge's own autopilot after a read tool (get_combat_state, look, …: a dialogue_started event in its result)
# waits at most this long on a playing line: a read is not a wait (naut-r2: get_combat_state 15.4 s).
AUTO_DIALOGUE_READ_LINE_S = 3.0
# Autopilot (fast reflexes): the bridge clicks through filler itself after choose_dialogue and when a
# conversation opens, so the agent sees the next real choice at once. AUTO_DIALOGUE = False turns it off.
AUTO_DIALOGUE = True


def _line_stuck(waited_s: float) -> str | None:
    """A playing line AutoSkip can't skip (autoskip.status): why, for the stop reason; None while it is skipping
    it, it is too new, or the status can't be read (an old mod: the plain wait)."""
    st = core.game.try_rpc("autoskip.status", context="client")
    if not isinstance(st, dict):
        return None
    if st.get("on") is False:
        return (f"AutoSkip is off, {waited_s:.0f}s so far" if waited_s >= DIALOGUE_STUCK_EXIT_S else None)
    ln = st.get("line") if isinstance(st.get("line"), dict) else {}
    age = float(ln.get("age") or 0) / 1000
    why = {"our turn": "AutoSkip doesn't skip lines in your combat turn",
           "tries": f"still up after {ln.get('tries')} skips"}.get(ln.get("blocked"))
    if not why or age < DIALOGUE_STUCK_EXIT_S:
        return None
    return f"{'a cinematic shot' if ln.get('notext') else 'a line'} playing {age:.0f}s, {why}"


def _dialogue_autopilot(max_lines: int = macros.DIALOGUE_MAX, open_s: float = 2.5,
                        line_s: float | None = None, strict: bool = True,
                        idle_open_s: float | None = None) -> tuple[int, list[str], str | None]:
    """Click through FILLER only (macros.dialogue_filler: a Continue prompt or one untagged, non-committing
    option) until a real choice, a roll/reaction/popup, or the end. Returns (lines advanced, report lines,
    stop reason). open_s: how long a dialogue that is still opening may take to appear. strict (the bridge's
    own autopilot): only a Continue prompt or a bare "..."/Continue/End option, never a spoken answer, even
    the only one; dialogue_advance (the agent asked for it) also takes a single plain non-committing answer.
    idle_open_s: no dialogue on screen and the game says no party member is in a conversation → wait only this long."""
    line_s = DIALOGUE_LINE_WAIT_S if line_s is None else line_s
    lines: list[str] = []
    stop = None
    n = 0
    t_start = time.monotonic()
    wait_until = t_start + line_s
    open_until = t_start + open_s
    idle_checked = idle_open_s is None or idle_open_s >= open_s
    stuck_check = t_start + min(DIALOGUE_STUCK_EXIT_S, 1.0)
    while n < max(1, min(int(max_lines), 30)):
        ui = core.game.try_rpc("ui.state", context="client") or {}
        scr = ui.get("screen")
        if scr in (None, "game") and n == 0 and time.monotonic() < open_until:
            if not idle_checked:
                idle_checked = True
                act = core.game.try_rpc("nav.activity")
                if isinstance(act, dict) and act.get("in_dialogue") is False:
                    open_until = min(open_until, t_start + idle_open_s)
                    if time.monotonic() >= open_until:
                        continue
            time.sleep(0.25)
            continue
        if scr != "dialogue":
            stop = "conversation ended" if scr in (None, "game") else f"{scr} on screen"
            if scr not in (None, "game"):
                lines.extend(reporting._render_ui(ui, brief=True))
            break
        ok, i, why = macros.dialogue_filler(ui.get("dialogue"), strict=strict)
        if not ok:
            now = time.monotonic()
            if why in ("line still playing", "no options yet") and now < wait_until:
                if why == "line still playing" and DIALOGUE_STUCK_EXIT_S > 0 and now >= stuck_check:
                    stuck_check = now + 1.0
                    stuck = _line_stuck(now - t_start)
                    if stuck:
                        stop = (f"{why} ({stuck}; it wants no input: wait_for dialogue_ended or level_changed, "
                                f"or act on what else you can)")
                        lines.extend(reporting._render_ui(ui, brief=True))
                        break
                time.sleep(0.3)
                continue
            stop = why
            lines.extend(reporting._render_ui(ui, brief=True))
            if why.startswith("a real choice (") and reporting._listed.get("node") == reporting._node(ui.get("dialogue")):
                stop = f"a real choice ({len(reporting._listed['texts'])} options)"  # S081: "(6 options)" under 7 listed
            break
        said = (ui.get("dialogue") or {}).get("text")
        r = core.game.run_job("ui.dialogue_choose", {"i": i}, context="client", wait_s=20)
        n += 1
        lines.append(f"{n}. " + (f"'{core.H.scrub(str(said))[:90]}' → " if said else "") +
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


@core.mcp.tool()
@reporting._guard
def dialogue_advance(max_lines: int = macros.DIALOGUE_MAX) -> str:
    """Click through FILLER dialogue lines only (a Continue prompt, or a single untagged option that commits to nothing). Stops and shows the dialogue at the first real choice (2+ options, a skill/class tag, a committing option), a roll, a reaction, a popup, or when the conversation ends. Never picks an answer for you. Rarely needed: choose_dialogue and talk_to already do this."""
    t0 = time.monotonic()
    n, lines, stop = _dialogue_autopilot(max_lines, strict=False, idle_open_s=DIALOGUE_IDLE_OPEN_S)
    head = f"dialogue_advance: {n} filler line(s)"
    body = [head] + lines + ([f"STOPPED: {stop}"] if stop else [])
    return reporting._finish("dialogue_advance", {"max_lines": max_lines}, "\n".join(body), t0)


def _travel_dest(marker: str, objective: str, x, y, z) -> tuple[list, str]:
    if marker:
        st = core.game.try_rpc("state.status") or {}
        ms = session._load_markers()
        m = next((m for m in ms if m.get("id") == marker), None) or next(
            (m for m in ms if marker.lower() in str(m.get("label", "")).lower()), None)
        if m is None or not m.get("pos"):
            raise ValueError(f"no marker '{marker}' (get_map lists your markers)")
        if m.get("level") and st.get("level") and m["level"] != st["level"]:
            raise ValueError(f"marker {m['id']} is in {m.get('region') or m['level']}: fast_travel there first")
        return list(m["pos"]), f"marker {m['id']} {m.get('label', '')}"
    if objective:
        m = render.find_objective(overview._objective_markers(overview.MARKERS_TTL_S), objective)
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
    r = core.game.try_rpc("nav.safe_spot", {"char": char, **({"pos": toward} if toward else {})})
    if not isinstance(r, dict) or r.get("safe") is not False:
        return None
    what = r.get("standing_in") or "a harmful surface"
    if not (r.get("found") and r.get("pos")):
        return f"standing in {what} and no clear ground within 8m: get out another way (jump_to, an exit)"
    px, py, pz = r["pos"]
    watcher = actq.Watcher(queueing._watch_conn(), ignore={"surface", "damage", "hp", "hostile"})
    watcher.baseline()
    rep, text = turn_plan._run_one(watcher, char, actq.Step("move_to", {"x": px, "y": py, "z": pz}))
    return f"left {what} {r.get('dir') or ''} {render.num(r.get('path_m', 0))}m: {text}"


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    travel_macro, session,
)
