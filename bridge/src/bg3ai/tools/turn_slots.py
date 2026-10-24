"""combat_turn's slots and fallbacks: picks, stepping into range, reactions, the next-up text.
"""

from __future__ import annotations

import re
import time

from .. import actq, macros, render, turn
from . import combat, core, observe, overview, plumbing, queueing, turn_plan, world_tools


def _turn_slot(name: str, alts: list, who: str, watcher: actq.Watcher, lines: list,
               after: dict, info: dict | None = None) -> tuple[str, bool]:
    """Try a slot's alternatives in order. ('ok'|'none'|'stop', then_move of the one that worked)."""
    tried: list[str] = []
    for alt in alts:
        if turn_plan._hp_pending(watcher):  # M6: a drop during the last alternative (or the step-in walk) spends nothing more
            return turn_plan._slot_hp_stop(name, tried, watcher, lines)
        fatal, why = turn_plan._turn_check(who, alt.step)
        if fatal:
            lines.append(f"{name}: " + " → ".join(tried + [why]))
            return "stop", False
        if why and info is not None:
            new = turn_plan._reaim_gone(alt, who, why, info)
            if new:
                tried.append(f"{why} → aimed at {queueing._label(new)}")
                fatal, why = turn_plan._turn_check(who, alt.step)
                if fatal:
                    lines.append(f"{name}: " + " → ".join(tried + [why]))
                    return "stop", False
        if not why:
            dropped, empty = turn_plan._drop_dead_targets(alt.step)
            if empty:
                why = dropped
            elif dropped:
                tried.append(dropped)
        why = why or turn_plan._slot_skip(alt, who, info)
        if why:
            tried.append(f"{alt.step.short()}: skipped ({why})")
            continue
        rep, text = turn_plan._turn_run(watcher, who, alt.step)
        text = turn_plan._outcome(alt.step, text)
        turn_plan._after(rep, after)
        if not rep.done and rep.stop_kind in (None, "failed"):
            asked = alt.step.args.get("ability")
            _step_note.clear()
            variant = _pick_variant(alt.step, text)
            step_in = (variant
                       or _step_into_range(who, alt.step, text, watcher, after)
                       or _step_into_sight(who, alt.step, text, watcher, after)
                       or _step_in_melee(who, alt.step, text, watcher, after))
            if step_in:
                # S040-S041: "FAILED: choose a variant: … → took …" read (and triaged) as a failure; the variant pick
                # is the player's quick click on the container's entry, not a failure: name it plainly
                tried.append(f"{asked}: {step_in}" if variant else f"{text} → {step_in}")
                if turn_plan._hp_pending(watcher):  # M6: the walk in provoked an attack: no re-cast
                    return turn_plan._slot_hp_stop(name, tried, watcher, lines)
                rep, text = turn_plan._run_one(watcher, who, alt.step)
                text = turn_plan._outcome(alt.step, text)
                turn_plan._after(rep, after)
            elif _step_note.get("why"):
                text += f" (no step closer: {_step_note['why']})"
        if rep.done:
            lines.append(f"{name}: " + " → ".join(tried + [text]))
            return "ok", alt.then_move
        tried.append(text)
        if rep.skipped:
            continue
        if rep.stop_kind not in (None, "failed") or turn.is_fatal(text) or turn.needs_decision(text):
            lines.append(f"{name}: " + " → ".join(tried))
            if rep.stop_kind not in (None, "failed"):
                lines.append(f"STOPPED: {queueing._label(rep.stopped or '')}")
            return "stop", False
    if alts:
        lines.append(f"{name}: " + " → ".join(tried) + (" (no alternative worked)" if len(alts) > 1 else ""))
    return "none", False


_VARIANTS = re.compile(r"choose a variant: ([\w, ]+)")
_WHICH_VARIANTS = re.compile(r"casts nothing by itself: use_ability which=<one of ([^>]+)>")


def _pick_variant(step: actq.Step, text: str) -> str:
    """combat_turn: a container spell refused 'choose a variant: A, B, ...' (S023-S025 x5: Spiritual Weapon,
    Spirit Guardians, Lay on Hands) is cast as its first variant, like a quick click on the first entry; the
    line names it. Changes step.args in place; '' when it isn't that failure."""
    if step.tool != "use_ability":
        return ""
    m = _VARIANTS.search(text)
    w = None if m or step.args.get("which") else _WHICH_VARIANTS.search(text)
    if w:
        # A27: a linked container (Gather Your Allies) lists its variants by shown name only (S093 fairness leak:
        # the ids): the first usable one goes as which=, the container stays the ability
        names = [v.strip() for v in w.group(1).split(", ") if v.strip()]
        first = next((v for v in names if not v.endswith("[already called]")), "")
        if not first:
            return ""
        step.args["which"] = first
        return f"took the first variant {first}"
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
    # S080 02:41:15: Lay on Hands on downed Gale 8.1 m away and the swing 6.9 m away failed (the old 3 m melee cap)
    # with Claude's whole move unused. A melee/touch step walks as a click on the ability does in the game: as far
    # as the movement left (the mod refuses a longer walk before it starts).
    if not gap or (gap[1] > turn.MELEE_RANGE_M and gap[0] - gap[1] > macros.STEP_INTO_RANGE_RANGED_M):
        return ""
    a = step.args
    if gap[1] <= turn.MELEE_RANGE_M and a.get("target"):
        # Melee: a spot beside the target inside the edge-to-edge reach (the route walk stops at its body).
        rep, _text = turn_plan._run_one(watcher, who, actq.Step("move_to", {"target": a["target"], "within": gap[1]}))
        turn_plan._after(rep, after)
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
            args["target"] = plumbing._uuid(str(a["target"]))
        except ValueError:
            return ""
    elif all(a.get(k) is not None for k in "xyz"):
        args["pos"] = [float(a[k]) for k in "xyz"]
    else:
        return ""
    if args.get("target"):
        # S032 17:45: "out of range (19.6 m, range 18.0 m) → stepped 2.2m into range → FAILED — no line of
        # sight": the route point in range was behind cover. A spot in range AND in sight (nav.los_spot) first.
        sp = core.game.try_rpc("nav.los_spot", {"char": who, "target": args["target"], "range": args["within"],
                                           "avoid": True})
        if isinstance(sp, dict) and sp.get("found") and sp.get("pos"):
            x, y, z = sp["pos"]
            rep, _text = turn_plan._run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
            turn_plan._after(rep, after)
            return f"stepped {render.num(sp.get('walk_m', '?'))}m into range and sight" if rep.done else ""
    ap = core.game.try_rpc("nav.approach", args)  # budget: the movement left (the op's default in combat)
    if not (isinstance(ap, dict) and ap.get("reachable") and ap.get("limit") == "in_range" and ap.get("pos")):
        return ""
    x, y, z = ap["pos"]
    rep, _text = turn_plan._run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
    turn_plan._after(rep, after)
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
        tu = plumbing._uuid(str(step.args["target"]))
    except ValueError:
        return ""
    acts = core.game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    rng = next((a.get("range") for a in acts or [] if isinstance(a, dict)
                and a.get("spell") == step.args.get("ability")), None)
    if not isinstance(rng, (int, float)):
        return ""
    if rng <= turn.MELEE_RANGE_M:
        # S033 18:49: Cure Wounds and Help on downed Shadowheart 1.5 m away: "no line of sight" twice, the turn
        # lost, Claude downed next. A touch cast already in reach looks for a spot beside it that sees it;
        # out of reach, walking next to it is the move's job (out of range → step in).
        d = turn.straight_dist(core.game.try_rpc("state.combat") or {}, who, tu)
        if d is None or d > rng + turn.MELEE_REACH_PAD:
            return ""
    sp = core.game.try_rpc("nav.los_spot", {"char": who, "target": tu, "range": max(1.0, float(rng) - 0.5),
                                       "avoid": True})
    if not (isinstance(sp, dict) and sp.get("found") and sp.get("pos")):
        return ""
    x, y, z = sp["pos"]
    rep, _text = turn_plan._run_one(watcher, who, actq.Step("move_to", {"x": x, "y": y, "z": z}))
    turn_plan._after(rep, after)
    return f"stepped {render.num(sp.get('walk_m', '?'))}m to see it" if rep.done else ""


MELEE_STEP_IN_M = 1.0  # a melee swing the engine refused without a reason: walk this close (move_to within), swing again
# A CastSpellFailed reason that walking closer can't fix (A.cast_failed_why's words).
NOT_A_REACH_MISS = ("dead", "out of sight", "line of sight", "two-step")


def _step_in_melee(who: str, step: actq.Step, text: str, watcher: actq.Watcher, after: dict) -> str:
    """combat_turn ACT-3 (S065 ×9, S062): the first melee swing right after the move came back CastSpellFailed 'no
    reason given'; later swings, after a step, landed. The move stops ~2.1 m centre to centre (inside our guard's
    edge-to-edge reach, maybe not the engine's) and the cast runs WithoutMove, so the engine never closes the gap.
    With movement left: walk within MELEE_STEP_IN_M of the target once; the caller swings again. Returns what it
    did, or '' (another failure, not melee, no movement, already that close, the walk failed)."""
    if step.tool != "use_ability" or "no reason given" not in text or not step.args.get("target"):
        return ""
    low = text.lower()
    if any(w in low for w in NOT_A_REACH_MISS):
        return ""
    mv = after.get("movement")
    if isinstance(mv, (int, float)) and mv < 0.3:
        return ""
    sp = step.args.get("ability")
    if sp not in turn_plan.MELEE_IDS:
        acts = core.game.try_rpc("state.actions", {"uuid": who})
        acts = acts.get("actions") if isinstance(acts, dict) else acts
        rng = next((a.get("range") for a in acts or [] if isinstance(a, dict) and a.get("spell") == sp), None)
        if not isinstance(rng, (int, float)) or rng > turn.MELEE_RANGE_M:
            return ""
    rep, _text = turn_plan._run_one(watcher, who, actq.Step("move_to", {"target": step.args["target"],
                                                              "within": MELEE_STEP_IN_M}))
    turn_plan._after(rep, after)
    lr = rep.last_result if isinstance(rep.last_result, dict) else {}
    if not rep.done:
        _step_note["why"] = re.sub(r"^\S+ ", "", _text.split("\n")[0])[:140]
        return ""
    if lr.get("already_there"):
        return ""
    d = lr.get("dist")
    return "stepped closer for the swing" + (f" ({render.num(d)}m from it)" if isinstance(d, (int, float)) else "")


AUTO_RANGED = ("Projectile_MainHandAttack", "Projectile_OffhandAttack")


def _turn_auto_fallback(p: turn.TurnPlan, slot: str, planned: list, who: str, watcher: actq.Watcher,
                        lines: list, after: dict, info: dict) -> str:
    """Every alternative of a slot missed its target (claude-run-2: S002 the move toward Zhalk stopped short,
    S002 the planned imp died first, S007 two casts with no line of sight wasted the action):
      1. out of range: the ranged weapon attack on the same target if it is within its range and hostile;
      2. retarget: the plan's own targeted abilities on the nearest other hostile in sight and in reach, for an
         alternative aimed at a hostile; a heal aimed at our side: a downed party member in reach; a revive: never;
      3. action slot, the move ran out of movement: Dash and walk on.
    Only abilities the character has available; retarget=false in the plan turns 2 off. ACT-1 (S063 11:00/11:13):
    Lay on Hands on ally e3 became a Ranged Attack on e3, Healing Word on an ally went to an enemy: each fallback
    keeps to the side of the target it replaces."""
    miss = lines[-1] if lines else ""
    tgt = (p.move.target if p.move else None) or next(
        (a.step.args.get("target") for a in planned if a.step.args.get("target")), None)
    tu = None
    if tgt:
        try:
            tu = plumbing._uuid(str(tgt))
        except ValueError:
            tu = None
    acts = core.game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    avail = {a.get("spell"): a for a in acts or [] if isinstance(a, dict) and a.get("available") is not False}
    tried = {a.step.args.get("ability") for a in planned}
    # a skipped melee (out of reach after the move) ran no step since the move's read: reuse it (live window 2026-10-16)
    cs = ((turn_plan._cs_cached() or turn_plan._cs_keep(core.game.try_rpc("state.combat"))) if turn_plan._turn_batch()
          else core.game.try_rpc("state.combat") or {})
    turn_plan._seen_sides(cs)
    party = info.get("party")
    alts: list[turn.Alt] = []
    if tu and "out of range" in miss and turn_plan._side(cs, tu, party) == "hostile":
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
                    planned_targets.add(plumbing._uuid(str(t)))
                except ValueError:
                    pass
        used: set = set()
        for a in planned:
            sp, t = a.step.args.get("ability"), a.step.args.get("target")
            if a.step.tool != "use_ability" or not sp or not t or sp in used or sp not in avail \
                    or turn.REVIVE.search(str(sp)):
                continue
            try:
                was = turn_plan._side(cs, plumbing._uuid(str(t)), party)
            except ValueError:
                continue
            rng = avail[sp].get("range")
            rng = rng if isinstance(rng, (int, float)) else None
            if was in ("hostile", None):  # None: never seen on a turn order (ours are always known)
                near = turn.hostiles_in_reach(cs, who, rng, planned_targets)
            elif was in ("party", "ally") and turn.HEAL.search(str(sp)):
                reach = 18.0 if rng is None else (rng + turn.MELEE_REACH_PAD if rng <= turn.MELEE_RANGE_M else rng)
                near = turn.downed_allies(cs, who, reach, planned_targets)
            else:
                continue  # a buff on an ally, an unknown side: no other target is "the same"
            used.add(sp)
            for u in near[:2]:
                args = {k: v for k, v in a.step.args.items() if k not in ("target", "targets", "x", "y", "z")}
                alts.append(turn.Alt(actq.Step("use_ability", {**args, "target": core.H.get(u)})))
        alts = alts[:1 + turn.MAX_RETARGET]
    if (slot == "action" and p.move and info.get("short") == "movement" and "Shout_Dash" in avail
            and "Shout_Dash" not in tried):
        alts.append(turn.Alt(actq.Step("use_ability", {"ability": "Shout_Dash"}), then_move=True))
    if not alts:
        return "none"
    res, then_move = _turn_slot(f"{slot} (auto fallback)", alts, who, watcher, lines, after, info)
    if res == "ok" and then_move and p.move:
        res = "stop" if turn_plan._hp_held(watcher, lines) else turn_plan._turn_move(p.move, who, watcher, lines, after, info)
    return res


def _retarget_lost(p: turn.TurnPlan, who: str, lines: list, info: dict) -> bool:
    """combat_turn: the move's target is dead or out of sight (acceptance 13:3x: a Shadow vanished into
    Shadow Blend during Claude's walk; both attacks were skipped 'not visible', then the Dash walked
    nowhere and the turn ended with 9 m unused). Re-aim the move and every alternative aimed at it at the
    nearest visible living hostile and say so; none in sight: say so and mark info["move_lost"] (a Dash
    that would walk on is skipped). True when the plan was re-aimed. One state.combat read (left for the
    next step check). Only a hostile move target is re-aimed (ACT-1: "move to dead Shadowheart, Revivify her"
    walked to the nearest enemy and cast Revivify on it); one never seen counts as hostile (ours are always known)."""
    try:
        lost = plumbing._uuid(str(p.move.target))
    except ValueError:
        return False
    if turn_plan._turn_batch():
        cs = turn_plan._cs_cached() or turn_plan._cs_keep(core.game.try_rpc("state.combat"))
    else:
        cs = turn_plan._turn_cs.get("cs") or core.game.try_rpc("state.combat") or {}
        turn_plan._turn_cs["cs"] = cs
    if not cs.get("active"):
        return False
    if turn_plan._side(cs, lost, info.get("party")) not in ("hostile", None):
        return False
    row = next((o for o in cs.get("order") or [] if o.get("uuid") == lost), None)
    if row is not None:
        if not row.get("dead"):
            return False
        why = "is dead"
    else:
        ts = turn_plan._target_state(lost) or {}
        if not isinstance(ts, dict) or (ts.get("visible") and not ts.get("dead")):
            return False
        why = "is dead" if ts.get("dead") else (turn_plan._gone(ts) or "is out of sight")
    if why != "is out of sight":
        info.setdefault("gone", set()).add(lost)
    near = turn.hostiles_in_reach(cs, who, 1e6, {lost})
    if not near:
        said = info.get("move_lost") == f"{queueing._label(lost)} {why}"
        info["move_lost"] = f"{queueing._label(lost)} {why}"
        if not said:  # S028: the same line came 3 times in one turn (move, action, bonus)
            lines.append(f"target: {queueing._label(lost)} {why} and no other hostile is in sight: "
                         "its move and attacks are skipped")
        return False
    new = near[0]
    new_h = core.H.get(new)
    p.move.target = new_h
    for alts in [p.slots.get("action") or [], p.slots.get("bonus") or []] + list(p.slots.get("extra") or []):
        for a in alts:
            t = a.step.args.get("target")
            try:
                if t and plumbing._uuid(str(t)) == lost:
                    a.step.args["target"] = new_h
            except ValueError:
                pass
    d = turn.straight_dist(cs, who, new)
    info.pop("move_lost", None)
    if (p.move.within is None or p.move.within <= turn.MELEE_RANGE_M) and d is not None \
            and d > (p.move.within or turn.MELEE_M) + turn.MELEE_REACH_PAD:
        info["melee_out"] = {"target": new, "dist": d}  # a walk on (movement left) re-checks it
    lines.append(f"target: {queueing._label(lost)} {why} → the plan now aims at the nearest visible hostile "
                 f"{queueing._label(new)}" + (f" ({render.num(d)}m)" if d is not None else "") + " (retarget:false keeps it)")
    return True


def _turn_auto_bonus(who: str, watcher: actq.Watcher, lines: list, after: dict) -> str:
    """No bonus action was planned: if an ally is downed and this character can Healing Word them, do it
    (a downed ally loses its turns and dies on 3 failed saves; the bonus action was unused in ~all
    claude-run-2 turns). 'ok' | 'none' | 'stop'."""
    if after.get("bonus") == 0:
        return "none"
    cs = (turn_plan._cs_cached() if turn_plan._turn_batch() else None) or core.game.try_rpc("state.combat") or {}
    if not any(o.get("relation") == "party" and turn.is_downed(o) for o in cs.get("order") or []):
        return "none"
    acts = core.game.try_rpc("state.actions", {"uuid": who})
    acts = acts.get("actions") if isinstance(acts, dict) else acts
    avail = {a.get("spell"): a for a in acts or [] if isinstance(a, dict) and a.get("available") is not False}
    alts = []
    for sp in turn.BONUS_HEALS:
        if sp in avail:
            rng = avail[sp].get("range")
            for u in turn.downed_allies(cs, who, float(rng) if isinstance(rng, (int, float)) else 18.0)[:2]:
                alts.append(turn.Alt(actq.Step("use_ability", {"ability": sp, "target": core.H.get(u)})))
    if not alts:
        return "none"
    return _turn_slot("bonus (auto: ally downed)", alts, who, watcher, lines, after)[0]


# Live D2 P5 (Orin's Death Growl): a Fearful Wyll walked 4.4 m by himself in 0-2.3 s of his turn start, then stood
# still with the turn his; S066/S067 Karlach's first order (Rage) at such a turn start was dropped ("the game never
# started it"). The first order waits until the member has stood still FEAR_STILL_S (at most FEAR_SETTLE_MAX_S).
FEAR_STILL_S, FEAR_SETTLE_MAX_S, FEAR_POLL_S, FEAR_MOVE_M = 1.0, 5.0, 0.1, 0.05


def _fear_settle(cs: dict, char: str, clock=time.monotonic, sleep=time.sleep) -> str:
    """At a Fearful member's turn: wait for the game's own fear move to stop before the plan's first order. -> the
    line saying how far it moved ("" when it isn't Fearful or never moved)."""
    name = turn.fear_moved(cs, char)
    if not name:
        return ""

    def pos() -> list | None:
        rows = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
        return next((r.get("pos") for r in rows if isinstance(r, dict) and r.get("uuid") == char), None)
    t0 = clock()
    p0 = last = pos()
    still_at = t0
    while clock() - still_at < FEAR_STILL_S and clock() - t0 < FEAR_SETTLE_MAX_S:
        sleep(FEAR_POLL_S)
        p = pos()
        if p and last and macros.dist(p, last) >= FEAR_MOVE_M:
            still_at = clock()
        last = p or last
    moved = macros.dist(p0, last) if p0 and last else 0.0
    if moved < FEAR_MOVE_M:
        return ""
    waited = still_at - t0
    return (f"{name}: the game moved {queueing._label(char)} {render.num(round(moved, 1))}m by itself at its turn "
            f"start (now @{render.pos(last)}); the plan started after it stopped ({waited:.1f}s)"
            + ("" if clock() - still_at >= FEAR_STILL_S else ", still moving when the wait ended"))


def _turn_over_text(char: str, fear: str, when: str) -> str:
    """The game ended a Fearful member's turn by itself (live D6 B8: Shadowheart's, during the fear settle)."""
    return (f"{fear}: the game ended {queueing._label(char)}'s turn by itself {when} (a Fearful member's turn may end "
            "on its own): nothing more to play for it this round")


def _downed_text(u: str) -> str:
    return (f"{queueing._label(u)} is downed: nothing to play, its turn passes by itself (the game rolls its death "
            "saving throw). Get it up from an ally's turn: Healing Word, Help (adjacent) or a potion.")


# S087: a stunned member's open turn the game itself passes; if it is still open this long with nothing else moving,
# the wait ends it as a player would (End Turn for a character that can't act) and says so.
PASS_END_S = 5.0


def _end_passing(cs: dict, since: float, done: set, notes: list | None) -> None:
    """End the open turns of passing (stunned) party members when nothing moved for PASS_END_S (once each)."""
    if time.monotonic() - since < PASS_END_S or turn.acting(cs):
        return
    rows = {o.get("uuid"): o for o in cs.get("order") or [] if isinstance(o, dict)}
    for u in turn.passing(cs):
        if u in done:
            continue
        done.add(u)
        r = core.game.run_job("act.end_turn", {"char": u}, wait_s=5)
        if notes is not None:
            why = rows.get(u, {}).get("passes") or "can't act"
            notes.append(f"ended {queueing._label(u)}'s turn ({why}: it can't act)" if r.status == "done"
                         else f"{queueing._label(u)}'s turn ({why}) did not end: {r.error or r.status}")


def _wait_turn(ready, max_s: float, blocked=None, on_prompt=None, extend_s: float = 0.0,
               notes: list | None = None) -> tuple[dict, str]:
    """combat_turn sent ahead (planned while enemies act): poll until ready(combat) or combat ends, a prompt
    needs an answer, blocked(combat) names why the turn can't come (another party member must act first),
    or max_s passes. on_prompt(ui) answers a prompt itself (returns True: keep waiting). Past max_s it waits up
    to extend_s more while the enemies' turns still move on (a turn changed in the last TURN_PROGRESS_S).
    (last state.combat, why it stopped waiting or ''): 'timed out' (nothing moved on), or, at the extension's
    end while turns still progress, 'enemies still acting (N turn changes in the wait, …)' (TURN_STILL_ACTING)."""
    t0 = time.monotonic()
    deadline = t0 + max_s
    n = 0
    seen, moved, turns = None, float("-inf"), 0  # moved: when the acting combatant last changed during the wait
    passed: set = set()
    while True:
        cs = core.game.try_rpc("state.combat") or {}
        if not cs.get("active") or ready(cs):
            return cs, ""
        # S080 02:31:45: an enemy acting in Darkness is not on the visible order (`now ?`): its hits and kills
        # (HP, downs, deaths) count as progress too, or a long enemy phase looks stalled
        key = (cs.get("round"), tuple(turn.acting(cs)),
               tuple((o.get("uuid"), o.get("hp"), o.get("hp_pct"), bool(o.get("dead")), bool(o.get("downed")))
                     for o in cs.get("order") or [] if isinstance(o, dict)))
        if key != seen:
            if seen is not None:
                moved = time.monotonic()
                turns += 1
            seen = key
        why = blocked(cs) if blocked else ""
        if why:
            return cs, why
        _end_passing(cs, max(moved, t0), passed, notes)
        n += 1
        if n % 3 == 0:
            ui = core.game.try_rpc("ui.state", context="client") or {}
            if ui.get("screen") in combat._PROMPT_SCREENS:
                if on_prompt and on_prompt(ui):
                    continue
                return cs, _prompt_why(ui)
        t = time.monotonic()
        if t > deadline:
            # S031 17:04: a 30 s wait timed out mid enemy phase (8 ghosts), the plan came back "not played" and
            # was sent again (another 5 s of thinking + 34 s of waiting). Enemies still taking turns: wait on.
            if t - moved >= turn_tool.TURN_PROGRESS_S or extend_s <= 0:
                return cs, "timed out"
            if t >= t0 + max_s + extend_s:
                return cs, f"{turn_tool.TURN_STILL_ACTING} ({turns} turn changes in the wait, the last {t - moved:.0f}s ago)"
        time.sleep(turn_tool.TURN_POLL_S)


def _prompt_why(ui: dict) -> str:
    """Why the wait for the turn stopped on a prompt. S029: 5 sent-ahead turns stopped on 'reaction on screen'
    (Shield Blow on an enemy's attack); each cost get_dialogue + react + the same combat_turn again."""
    why = f"{ui.get('screen')} on screen"
    rx = ui.get("reaction") if ui.get("screen") == "reaction" else None
    if isinstance(rx, dict):
        prompt = str(rx.get("prompt") or "").replace(" | Choose how to react.", "")
        why += (f": {core.H.scrub(prompt)}" if prompt else "") + \
            ' — answer it with react(use=true|false), then send the plan again; or put "on_reaction":"use"|"skip"' \
            " in the plan to answer such prompts while it waits"
    return why


def _answer_reaction(use: bool) -> str:
    turn_plan._turn_cs.clear()  # a reaction (an opportunity attack) can kill: the kept turn read is stale
    conn = queueing._watch_conn()
    r = conn.run_job("ui.react", {"use": use}, context="client", wait_s=10)
    res = r.result if isinstance(r.result, dict) else {}
    inp = world_tools._inp() if use and res.get("needs_click") else None
    if inp is not None:
        # one row per spell level (S092 Gale's Shield): only a click on the row answers it (reactclick)
        from .. import reactclick
        c = reactclick.click_row(lambda o, a: conn.rpc(o, a, context="client"), inp, int(res["needs_click"]))
        return f"reaction prompt: used ({reactclick.describe(c)})"
    if use and res.get("needs_click"):
        return "reaction prompt: NOT answered (it takes a click on its row and the input shim is not available)"
    return f"reaction prompt: {'used' if use else 'declined'} ({r.status})"


def _disabled_lines(only: str | None = None) -> list[str]:
    """The DISABLED lines (turn.disabled_lines) of a fresh combat read; [] out of combat."""
    cs = core.game.try_rpc("state.combat") or {}
    return turn.disabled_lines(cs, queueing._label, only) if cs.get("active") else []


def _turn_next(who: str, party: set, cs: dict | None = None) -> str:
    """After the turn: who acts next, and the next party member's actions if it is already their turn. cs: a read
    taken after the turn moved on (the HUD end's last one), else read here."""
    cs = cs or core.game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        observe._acts_shown.clear()
        observe._light_noted.clear()
        return "combat is over: loot, heal, then end_combat_report"
    others = [u for u in turn.party_acting(cs, party) if u != who]
    cur = others[0] if others else None
    if cur:
        out = f"NOW {queueing._label(cur)}'s turn: plan it"
        if len(others) > 1:
            out += f"; also {', '.join(queueing._label(u) for u in others[1:])}"
        out += "".join("\n" + x for x in turn.disabled_lines(cs, queueing._label))
        acts = core.game.try_rpc("state.actions", {"uuid": cur})
        overview._learn_actions(cur, acts)
        if acts:
            out += "\nYOUR ACTIONS (" + core.H.get(cur) + ")" + observe._actions_delta(cur, acts)
        return out
    text, mine = turn.next_up(cs, party, queueing._label, ended=who)
    if mine:
        text += ". Plan it while they act and send its combat_turn now (it waits for the turn)"
    return "\n".join([text] + turn.disabled_lines(cs, queueing._label)).strip("\n")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    turn_tool,
)
