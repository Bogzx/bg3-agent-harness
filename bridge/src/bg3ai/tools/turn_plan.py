"""combat_turn's building blocks: turn checks, moves and target state.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from .. import actq, render, turn
from . import core, overview, plumbing, queueing, reporting

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
    ex = queueing._new_executor(watcher, queueing._watch_conn(), who)
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
    return "NOW " + render.compact(keep, core.H) if keep else ""


_party_memo: dict = {"t": -1e9, "uuids": None}


def _party_cached(ttl_s: float = 30.0) -> set | None:
    """The party's uuids, re-read at most every ttl_s (the EVENTS crowd mute needs them; one RPC per 30 s)."""
    if time.monotonic() - _party_memo["t"] > ttl_s:
        _remember_party(core.game.try_rpc("state.status"))
    return _party_memo["uuids"]


def _remember_party(st: Any) -> None:
    """Keep the party of a state.status read for _party_cached (a tool that just read it saves its result a read)."""
    if isinstance(st, dict) and st.get("party"):
        _party_memo.update(t=time.monotonic(), uuids={c.get("uuid") for c in st["party"] if isinstance(c, dict)})


def _party_set() -> set:
    st = core.game.try_rpc("state.status") or {}
    return {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}


# combat_turn's own state.combat read, reused by the first step check (no step ran in between): one round
# trip instead of two at the start of every turn.
_turn_cs: dict = {}
# actq.BATCH_CHECKS (env BG3AI_BATCH_CHECKS=0 reverts): the read is also taken from the watcher's check after each
# finished step (the same round trip as its events + act.watch) and kept until the next step starts (_run_one
# clears it) instead of one read per check: the turn can't change while no step of ours runs. A read older than
# this is read again anyway.
TURN_CS_MAX_S = 1.5


def _turn_batch() -> bool:
    """Inside a combat_turn (this thread) with BATCH_CHECKS on: keep/reuse its state.combat reads."""
    return actq.BATCH_CHECKS and bool(reporting._batch.get("in_turn"))


def _cs_keep(cs: Any) -> dict:
    """Remember a state.combat read for the next step check (BATCH_CHECKS); returns it ({} if none)."""
    cs = cs if isinstance(cs, dict) else {}
    if cs:
        _turn_cs.update(cs=cs, t=time.monotonic())
    return cs


def _cs_cached() -> dict | None:
    """The kept state.combat read, if still valid (no step ran since, not older than TURN_CS_MAX_S)."""
    kept = dict(_turn_cs)  # review L3: one snapshot (the monitor thread's reaction answer may clear it meanwhile)
    cs = kept.get("cs")
    if cs is None:
        return None
    if actq.BATCH_CHECKS and time.monotonic() - kept.get("t", 0.0) > TURN_CS_MAX_S:  # no time: expired
        _turn_cs.clear()
        return None
    return cs
# uuid -> relation from combat_turn's turn order reads: the side of a planned target that is out of sight now.
_turn_sides: dict = {}


def _seen_sides(cs: dict) -> None:
    if len(_turn_sides) > 2000:
        _turn_sides.clear()
    _turn_sides.update(turn.sides(cs))


def _side(cs: dict, u: str | None, party: set | None) -> str | None:
    """turn.side with what earlier reads saw (ACT-1: a fallback keeps to the planned target's side)."""
    return turn.side(cs, u, party if party is not None else (_party_cached() or set()), _turn_sides)


GONE_WHY = {"left_fight": "it is no longer in the turn order"}


def _gone(ts: dict) -> str:
    """'is gone (…)' for an act.target_state reply whose combatant left the turn order (Actions.lua left_fight);
    '' for one only out of sight. Never why (dead, despawned): a player sees only the portrait leave (audit C9)."""
    g = ts.get("gone") if isinstance(ts, dict) else None
    return f"is gone ({GONE_WHY.get(g, g)})" if g else ""


def _target_state(u: str) -> Any:
    """act.target_state; on_bar = the target was on a turn order read this session (a combatant the party saw), the
    only case where it may say the target left the turn order."""
    return core.game.try_rpc("act.target_state", {"uuid": u, "on_bar": u in _turn_sides})


def _turn_check(who: str, step: actq.Step | None) -> tuple[bool, str]:
    """Before each turn step, from what the player sees: (fatal, why). fatal = the turn is over for us;
    a non-fatal why (target dead / out of view) skips just this alternative."""
    if _turn_batch():
        cs = _cs_cached() or _cs_keep(core.game.try_rpc("state.combat"))
    else:
        cs = _turn_cs.pop("cs", None) or core.game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        return True, "combat is over"
    now = turn.acting(cs)
    if now and who not in now:
        return True, f"it is no longer {queueing._label(who)}'s turn (now {', '.join(queueing._label(u) for u in now)})"
    tgt = step.args.get("target") if step is not None else None
    if tgt:
        try:
            u = plumbing._uuid(str(tgt))
        except ValueError:
            return False, f"unknown target {tgt}"
        row = next((o for o in cs.get("order") or [] if o.get("uuid") == u), None)
        # A combatant on the read turn order is visible (state.combat lists only seen ones) and says if it
        # died: no act.target_state round trip (S018-S026: ~22 ms each, 2-4 per turn).
        ts = ({"visible": True, "dead": row.get("dead")} if row is not None
              else _target_state(u))
        if isinstance(ts, dict):
            # a revive's target is a body (S063: Revivify on dead Shadowheart was skipped, then re-aimed at a foe)
            if ts.get("dead") and not turn.REVIVE.search(str(step.args.get("ability") or "")):
                return False, f"{queueing._label(u)} is already dead"
            if _gone(ts) and not turn.REVIVE.search(str(step.args.get("ability") or "")):
                return False, f"{queueing._label(u)} {_gone(ts)}"
            if not ts.get("visible"):
                return False, f"{queueing._label(u)} is not visible"
    return False, ""


def _approach(who: str, tu: str | None, mv: turn.Move, within: float, budget: float | None = None) -> dict | None:
    """nav.approach: where to walk along the real route to get within `within` m (None: the op is missing)."""
    args: dict = {"char": who, "within": within, "avoid": mv.avoid}
    args.update({"target": tu} if tu else {"pos": mv.pos})
    if budget is not None:
        args["budget_m"] = budget
    r = core.game.try_rpc("nav.approach", args)
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


TURN_CONVO_WAIT_S = 20.0  # combat_turn: a step refused "in a conversation" waits this long for mid-fight lines


def _turn_run(watcher: actq.Watcher, who: str, step: actq.Step) -> tuple[actq.Report, str]:
    """A combat_turn step. S045 (Ketheric): 6 steps "character is in a conversation (InDialog)" while his mid-fight
    lines played ("line still playing" ×18), each a failed alternative and a re-plan. Lines with nothing to answer:
    wait for them (re-trying the step), up to TURN_CONVO_WAIT_S; an answer on screen is the agent's: say so."""
    rep, text = _run_one(watcher, who, step)
    if rep.done or rep.stop_kind not in (None, "failed") or macro_tools._IN_CONVO not in text:
        return rep, text
    t0 = time.monotonic()
    while time.monotonic() - t0 < TURN_CONVO_WAIT_S:
        ui = core.game.try_rpc("ui.state", context="client") or {}
        d = (ui.get("dialogue") or {}) if isinstance(ui, dict) and ui.get("screen") == "dialogue" else {}
        if [o for o in d.get("options") or [] if isinstance(o, dict) and not o.get("off")]:
            return rep, text + " — a mid-fight conversation needs your answer: choose_dialogue, then combat_turn again"
        time.sleep(0.5)
        rep, text = _run_one(watcher, who, step)
        if rep.done or rep.stop_kind not in (None, "failed") or macro_tools._IN_CONVO not in text:
            return rep, text + f" (after waiting {time.monotonic() - t0:.0f}s for the mid-fight lines)"
    return rep, text + f" (still in a conversation after {TURN_CONVO_WAIT_S:g}s of lines)"


# ACT-P1 (naut2 run 1 19:59:07): combat_turn {"move":"o3"} (the Transponder, 35 m off) was REFUSED "o3 is the objective
# marker …", then 27 calls / 56 s of route + Dash + walk_toward + wait_for + end_turn per member per round followed. A
# move to an objective whose name is nothing in view walks toward its marker like a position move, with the turn's
# partial rule (as far as the movement left goes; a Dash with then_move walks on). BG3AI_TURN_MOVE_OBJECTIVE=0 reverts.
TURN_MOVE_OBJECTIVE = os.environ.get("BG3AI_TURN_MOVE_OBJECTIVE", "1") != "0"
TURN_OBJECTIVE_MAX_M = 40.0  # the walkable spot toward the marker is picked this far out at most (then partial)


def _turn_move_objective(p: turn.TurnPlan) -> None:
    """combat_turn: the move's oN → what it names in view (unchanged rule), else the marker's position (mv.label
    says which). An objective in another area or unknown: ValueError, as before."""
    mv = p.move
    ref = str(mv.target or "").strip().lower() if mv else ""
    if not (TURN_MOVE_OBJECTIVE and re.fullmatch(r"o\d+", ref)):
        return
    try:
        mv.target = core.H.get(plumbing._objective_ref(ref)) or mv.target  # its name is something in view: walk to that
        return
    except ValueError as e:
        if "is the objective marker" not in str(e):
            raise
    m = render.find_objective(overview._objective_markers(max_age=600), ref)
    if not (m.get("here") and isinstance(m.get("pos"), list) and len(m["pos"]) == 3):
        raise ValueError(f"objective {ref} '{render.objective_label(m)}' is not in this area: travel_to(objective='{ref}')")
    mv.target, mv.within = None, None
    mv.pos = [float(x) for x in m["pos"]]
    mv.label = f"{ref} (objective {render.objective_label(m)})"


# A partial walk whose sampled spot still needs more than the movement left (naut2 run 1: "beyond movement (path
# 9.7 m, 9.0 m left) → partial 8.7m: FAILED — beyond movement (path 9.7 m …)": the 8.7 m budget is straight, the
# route bends) tries once more, shorter in proportion to how much the walk bends (budget × budget / path: naut3-r3
# #56, 1.8 m that needed 3.1 m, the old "budget − overshoot" left 0.5 m and no retry). False reverts.
PARTIAL_SHRINK_RETRY = True


def _path_over(text: str) -> float | None:
    """'beyond movement (path 9.7 m, 9.0 m left)' → 0.7 (how much the route needs over the movement left)."""
    m = re.search(r"path ([\d.]+) m, ([\d.]+) m left", text or "")
    return float(m.group(1)) - float(m.group(2)) if m else None


def _path_need(text: str) -> float | None:
    """'beyond movement (path 9.7 m, 9.0 m left)' → 9.7 (the walk the refused spot needs)."""
    m = re.search(r"path ([\d.]+) m, [\d.]+ m left", text or "")
    return float(m.group(1)) if m else None


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
    what = mv.label or mv.target or render.pos(mv.pos)
    tu = plumbing._uuid(mv.target) if mv.target else None
    step = None
    ap = None

    def dist_now() -> float | None:
        # Live window 2026-10-16 ("batched step checks" FAIL, single state.combat 4 > 3 and 6 > 5): this read ran
        # after every move with a target; the watcher's read after that move step (post_ops) already holds it.
        if not tu:
            return None
        cs = ((_cs_cached() or _cs_keep(core.game.try_rpc("state.combat"))) if _turn_batch()
              else core.game.try_rpc("state.combat") or {})
        return turn.straight_dist(cs, who, tu)

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
    cs = _cs_cached()
    blk = turn.move_blocked(cs, who) if cs else None
    if blk:
        # S091 08:30:24: Lethargic Claude's plan walked off the Orb of Negation's platform; a member that can't walk
        # stays put at once and the plan goes on from there (Actions.lua combat_budget refuses the walk too)
        info["short"] = "no_path"
        lines.append(f"move {what}: stays put — {blk}" + out_of_melee(dist_now()))
        return "none"
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
            r = core.game.try_rpc("nav.reach_toward", {"char": who, "target": tu, "max_m": max(0.5, d - mv.within + 0.3)})
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
    if step is None and mv.label and mv.pos:
        # toward an objective marker (ACT-P1): it may stand on a console or a ledge; walk to the farthest walkable
        # spot toward it (walk_toward's pick), and the partial rule below cuts that to the movement left
        r = core.game.try_rpc("nav.reach_toward", {"char": who, "pos": mv.pos, "max_m": TURN_OBJECTIVE_MAX_M, "loose": True})
        if not (isinstance(r, dict) and r.get("reachable") and r.get("pos")):
            info["short"] = "no_path"
            lines.append(f"move {what}: no walkable ground toward it")
            return "none"
        x, y, z = r["pos"]
        step = actq.Step("move_to", {"x": x, "y": y, "z": z})
    if step is None:
        step = actq.Step("move_to", {"target": mv.target} if mv.target else dict(zip("xyz", mv.pos)))
    rep, text = _turn_run(watcher, who, step)
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
        lines.append(f"STOPPED: {queueing._label(rep.stopped or '')}")
        return "stop"
    if turn.is_fatal(text):
        lines.append(f"move {what}: {text}")
        return "stop"
    left = turn.movement_left(text)
    if mv.partial and turn.move_too_long(text) and (left is None or left >= 1.0):
        if left is None:
            p = core.game.try_rpc("nav.path", {"char": who, **({"target": tu} if tu else {"pos": mv.pos})}) or {}
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
                r = core.game.try_rpc("nav.reach_toward", {"char": who, **({"target": tu} if tu else {"pos": mv.pos}),
                                                      "max_m": budget})
            if isinstance(r, dict) and r.get("reachable") and r.get("pos"):
                x, y, z = r["pos"]
                rep2, text2 = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
                _after(rep2, after)
                need = _path_need(text2) if not rep2.done and turn.move_too_long(text2) else None
                if PARTIAL_SHRINK_RETRY and need and budget * budget / need >= 1.0:
                    budget = budget * budget / need
                    r3 = (_approach(who, tu, mv, mv.within if mv.within is not None else turn.MELEE_M, budget)
                          if ap is not None else
                          core.game.try_rpc("nav.reach_toward", {"char": who, **({"target": tu} if tu else {"pos": mv.pos}),
                                                            "max_m": budget}))
                    if isinstance(r3, dict) and r3.get("reachable") and r3.get("pos"):
                        x, y, z = r3["pos"]
                        rep2, text2 = _run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
                        _after(rep2, after)
                res2 = text2.split(': ', 1)[-1]
                if PARTIAL_SHRINK_RETRY and not rep2.done and turn.move_too_long(text2):
                    # no spot toward it fits the movement left (the walk bends round something right here): the
                    # member stays put, which is no failure of the plan (naut3-r3 #56 ended "FAILED — beyond movement")
                    info["short"] = "movement"
                    lines.append(f"move {what}: {text} → no spot toward it fits the {render.num(left)}m left (the"
                                 f" nearest needs {render.num(_path_need(text2) or 0)}m of walking): stays put")
                    return "none"
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
                    lines.append(f"STOPPED: {queueing._label(rep2.stopped or '')}")
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
                want = mv.within if mv.within is not None else turn.MELEE_M
                if d2 is not None and d2 <= want + 0.3:
                    # naut3 15:40:52: "move e71: FAILED — could not get closer … → along the route: done …, now 1.5m
                    # from e71": the range asked for was reached, so the move is done; the first try is a detail
                    m = re.search(r"dist=([\d.]+)", text)
                    lines.append(f"move {what}: {res2} (along the route; the walk to the spot beside it stopped"
                                 + (f" {m.group(1)}m away)" if m else " short)"))
                    return "ok"
                lines.append(f"move {what}: {text} → along the route: {res2}")
                return "ok"
            if rep2.stop_kind not in (None, "failed"):
                lines.append(f"move {what}: {text}")
                lines.append(f"STOPPED: {queueing._label(rep2.stopped or '')}")
                return "stop"
    if turn.move_too_long(text):
        info["short"] = "movement"
    elif melee and "could not get closer" in text:
        # live 15:1x: Shadowheart's walk to the spot beside a Wraith failed 3.5 m away; the melee was still sent
        # (range miss) and the Dash walked on to 3 m. A blocked walk is a route that ends short.
        m = re.search(r"dist=([\d.]+)", text)
        info["short"] = "route_end"
        text += out_of_melee(float(m.group(1)) if m else dist_now())
    elif "cannot move while" in text:
        # Actions.lua combat_budget (Guard.move_blocked): the plan goes on from where the member stands
        info["short"] = "no_path"
        text += out_of_melee(dist_now())
    elif "never started moving" in text:
        # S074 r2: Claude's walk stood still (the mod gives up after 2.5 s, no longer 5 s of "no path"): the plan
        # goes on from where he stands, melee on a target out of reach skipped like a route that ends here
        info["short"] = "no_path"
        text += out_of_melee(dist_now())
    lines.append(f"move {what}: {text}")
    return "none"


def _ability_range(who: str, ability: Any, info: dict) -> float | None:
    """An ability's range from get_actions (read once per turn into info["ranges"]); None when unknown."""
    if "ranges" not in info:
        acts = core.game.try_rpc("state.actions", {"uuid": who})
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
        if plumbing._uuid(str(a["target"])) != mo["target"]:
            return ""
    except ValueError:
        return ""
    rng = _ability_range(who, sp, info)
    if (rng is None and sp not in MELEE_IDS) or (rng is not None and rng > turn.MELEE_RANGE_M):
        return ""
    d = mo.get("dist")
    return (f"{queueing._label(mo['target'])} is out of range for melee after the move"
            + (f" ({render.num(d)}m)" if d is not None else ""))


def _spent_note(char: str, lines: list) -> str:
    """naut3-r2 #54: Tamsin's turn group stayed open after her attack, walk and end request; the same {move o3,
    Dash} plan was sent again and read 'move: FAILED — no movement left / action: Shout_Dash: FAILED — not enough
    action' as if a new turn had failed. When the move found no movement and the action's first try found no action,
    both were spent before this plan: say so."""
    moved = any(re.match(r"move .*?: (FAILED — )?no movement left", ln) for ln in lines)
    acted = any(re.match(r"action: [^:]+: FAILED — not enough action", ln) for ln in lines)
    if not (moved and acted):
        return ""
    return (f"{queueing._label(char)} had no movement and no action left when this plan started: both were spent earlier in "
            "this same turn (not a new turn). What is left: a bonus action, or end_turn.")


def _hp_held(watcher: actq.Watcher, lines: list) -> bool:
    """combat_turn, at a slot boundary: an HP drop the TurnWatcher held during the last step (it never
    cancels the step in progress) stops the turn here, before anything else is spent."""
    hit = getattr(watcher, "hp_hit", None)
    if not hit:
        return False
    watcher.hp_hit = None
    lines.append(f"STOPPED: {queueing._label(hit[1])} (HP dropped mid-turn; the step in progress finished, "
                 "nothing else spent: decide the rest)")
    return True


def _hp_pending(watcher: actq.Watcher) -> bool:
    """An HP drop held during a step of this slot (review M6): peeked before every further spend inside a slot (the
    next alternative, a step-in walk's re-cast, then_move, the auto fallback); _hp_held then says it and stops."""
    return bool(getattr(watcher, "hp_hit", None))


def _slot_hp_stop(name: str, tried: list, watcher: actq.Watcher, lines: list) -> tuple[str, bool]:
    """_turn_slot: stop inside the slot for a held HP drop: the slot's line so far, then the STOPPED line."""
    if tried:
        lines.append(f"{name}: " + " → ".join(tried))
    _hp_held(watcher, lines)
    return "stop", False


def _reaim_gone(alt: turn.Alt, who: str, why: str, info: dict) -> str | None:
    """combat_turn: an alternative aimed at a hostile that is dead or gone (left the turn order) is re-aimed
    at once at the nearest living hostile in sight within the ability's reach, instead of being skipped (S033
    Umbral Tremors: 4 turns of skipped attacks); every later alternative aimed at it follows (info["gone"]).
    Same side rules as the auto fallback: a hostile target only, never a revive; retarget:false turns it
    off. Returns the new target's uuid, or None (no re-aim: the skip and the auto fallback stay)."""
    if not info.get("retarget", True) or alt.step.tool != "use_ability":
        return None
    a = alt.step.args
    sp, t = a.get("ability"), a.get("target")
    if not t or turn.REVIVE.search(str(sp or "")):
        return None
    try:
        lost = plumbing._uuid(str(t))
    except ValueError:
        return None
    if " is gone (" not in why and " is already dead" not in why:
        return None
    cs = core.game.try_rpc("state.combat") or {}
    if not cs.get("active") or _side(cs, lost, info.get("party")) not in ("hostile", None):
        return None
    gone = info.setdefault("gone", set())
    gone.add(lost)
    near = turn.hostiles_in_reach(cs, who, _ability_range(who, sp, info), gone)
    if not near:
        return None
    a["target"] = core.H.get(near[0])
    for k in ("x", "y", "z"):
        a.pop(k, None)
    picks = _picks(a.get("targets"))
    if picks:
        # review L4: replace only the lost pick (a Magic Missile on 3 stays on 3)
        new: list = []
        for t in picks:
            try:
                tu = plumbing._uuid(str(t))
            except ValueError:
                tu = None
            new.append(core.H.get(near[0]) if tu == lost else t)  # Magic Missile / Scorching Ray may repeat a pick
        a["targets"] = new if isinstance(a.get("targets"), list) else ",".join(new)
    return near[0]


def _picks(targets: Any) -> list[str]:
    """A step's `targets` as a list (a plan list, or the comma string the turn plan keeps)."""
    if isinstance(targets, str):
        return [t.strip() for t in targets.split(",") if t.strip()]
    return [str(t) for t in targets] if isinstance(targets, list) else []


def _drop_dead_targets(step: actq.Step) -> tuple[str, bool]:
    """combat_turn (review L4): picks in `targets` the kept turn-order read shows dead are dropped (the mod refuses a
    whole multi-target cast for one dead pick). (note, nothing left). Only with BATCH_CHECKS' kept read (no extra RPC)."""
    tl = _picks(step.args.get("targets"))
    if (not tl or turn.REVIVE.search(str(step.args.get("ability") or ""))
            or not _turn_batch()):
        return "", False
    cs = _cs_cached() or {}
    dead_rows = {o.get("uuid") for o in cs.get("order") or [] if o.get("dead")}
    keep, dead = [], []
    for t in tl:
        try:
            u = plumbing._uuid(str(t))
        except ValueError:
            u = None
        (dead if u and u in dead_rows else keep).append(u if u and u in dead_rows else t)
    if not dead:
        return "", False
    if not keep:
        return "every pick is already dead (" + ", ".join(queueing._label(u) for u in dead) + ")", True
    step.args["targets"] = keep if isinstance(step.args.get("targets"), list) else ",".join(keep)
    return "dropped dead " + ", ".join(queueing._label(u) for u in dead) + " from the picks", False


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    turn_slots, macro_tools,
)
