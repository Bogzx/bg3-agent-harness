"""The combat_turn tool and its background end.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from .. import actq, delta, render, turn
from ..game import GameError
from . import batch, core, observe, overview, plumbing, queueing, reporting, turn_plan, turn_slots, uitools, world_tools


@core.mcp.tool()
@reporting._guard
def combat_turn(plan: dict[str, Any] | list[dict[str, Any]] | str, who: str | None = None, end: bool = True,
                wait_next: bool = False, ignore: str = "hostile", wait_turn: float = 30.0) -> str:
    """A WHOLE combat turn in one call — or the turns of SEVERAL party members acting now: plan = a list
    [{"who":"Karlach","plan":{...}},{"who":"Gale","plan":{...}}] plays them in order (each as below, each turn ended);
    a later member's plan waits for its turn like a sent-ahead plan (the chain waits ≤80 s in all); a member that already ended its turn is skipped, and one the game wants first is played first; it stops and
    hands back as soon as a turn is interrupted, a member's turn doesn't come in time, or the fight ends, and lists
    who was NOT played. Use it when the result
    says `NOW <a>'s turn: plan it; also <b>, <c>` (S023-S026: 175 back-to-back combat_turn calls, one per member).
    Single turn:  move, action, bonus action, extra attacks, end turn. Each slot lists alternatives tried in order until one works (out of range after the move → the ranged attack; no ranged → Dash; else Disengage). If every action alternative is out of range, it tries the ranged weapon attack itself, else Dash + walk on when the move ran out of movement. A move that ends out of reach says how far it got and why (out of movement / fire on the route / no route closer).
    plan: an object (or the same as a JSON string), e.g. {"move":{"target":"e12","within":1.5},"action":[{"ability":"Target_MainHandAttack","target":"e12"},{"ability":"Projectile_MainHandAttack","target":"e12"},{"ability":"Shout_Dash","then_move":true},"Shout_Disengage"],"bonus":{"ability":"Shout_SecondWind","if":"hp_below:50"},"extra":[[{"ability":"Target_MainHandAttack","target":"e12"}]]}
    move: "e12" (walk next to it), {"target","within":<m>} (stop once that close, e.g. ranged), [x,y,z], "o3" (an objective: toward its marker, e.g. a Dash to the exit). Walks the real route (stairs, detours). If the walk doesn't fit the movement left it walks as far as it does (partial:false turns that off); it keeps out of fire on the way (avoid:false walks through).
    Abilities: the shown name or the id; a weapon attack the character doesn't have (a summon's) becomes its own weapon attack. Extra Attack: after the first weapon attack, an "extra" slot attack costs no action (the game's rule).
    Alternatives: an ability id, or {"ability","target"|"x,y,z","upcast","targets","force","if","unless","then_move"}, or {"tool":<batchable tool>,...}. then_move: after it, walk the move again (a Dash does this by default; then_move:false turns it off).
    order: "move,action,bonus,extra" (default; e.g. "bonus,move,action" for Misty Step first). reactions: {"Divine Smite":"ask"} sets prompts first; on_reaction: "use"|"skip" answers reaction prompts open at the turn's start, mid-turn and while a sent-ahead plan waits (default: stop, nothing played into an open prompt).
    No line of sight to a ranged target: it first steps (within the movement left and the ability's range) to the nearest spot that sees it, then casts again. A downed character has no turn to play (it passes by itself); another of your characters acting first makes a sent-ahead plan return at once; your summon (Spiritual Weapon …) acting first and not in the plan: its turn is played for it (its weapon attack on the plan's first target, else the nearest hostile) and then the plan.
    Before each step: still this character's turn? target still visible and alive? (else the next alternative). Stops on the usual interrupts (combat/dialogue/prompt/hp/teleport; ignore="" adds new hostiles in view).
    who: required when NOW lists more than one of your characters; left out, the plan goes to the only one acting whose actions cover every ability it names (else REFUSED), and a plan sent ahead waits for the character selected when it was sent.
    A slot whose every alternative missed (target dead, out of range, no line of sight) tries the plan's own targeted abilities on the nearest other visible hostile in reach (retarget:false turns it off); fallbacks keep to the planned target's side (an ally-aimed heal only re-aims at a downed party member, a revive never). A melee swing the game refused without a reason steps within 1 m once and swings again. A melee move that ends out of reach says "melee is out of reach this turn" and skips the melee attacks on that target (ranged / Dash next). The move's target (a hostile) dead or out of sight (Shadow Blend, killed first): the plan is re-aimed at the nearest visible hostile, and walks on if movement is left (retarget:false keeps it). No bonus planned and an ally downed: Healing Word on them if this character has it (auto_bonus:false turns it off).
    An empty plan {} just ends that character's turn (e.g. it already acted through single tools).
    end=true ends the turn and returns at once with who acts next, so you can plan the next turn while enemies act (wait_next=true blocks until a party member's turn). When enemies act next, the end itself runs in the background ("ending in the background", then queue_done): what you send next waits for it.
    Not this character's turn yet (enemies still acting): send the plan anyway; it waits up to wait_turn s for the turn (stops on a prompt), up to 120 s longer while enemies are still taking turns, then plays it (still their turns then: NOT PLAYED YET, nothing done: send it again)."""
    plan = batch._as_json(plan)
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
        # A25: only this member's turn may stop the chain. S080 23:23:12: downed Wyll's "nothing to play" (no step,
        # the flag untouched) stopped [Wyll, Gale] as "interrupted" on an earlier call's flag; Gale not played
        reporting._batch["last"] = "done"
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
        if txt.startswith(TURN_NOT_YET):
            stop = f"{w}: not played — {txt[len(TURN_NOT_YET):].split(chr(10))[0]}"
            break
        outs.append(txt)
        played += 1
        i += 1
        if reporting._batch.get("last") == "interrupted":
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
# combat_turn ends the turn in the background when nobody else of the party can act now (the party's last end before
# enemies or the next turn group act): the agent plans the next turn while the game ends this one. naut series (3
# runs): every end blocked 1.6-5.3 s (a game-ignored EndTurn → HUD End Turn: 3.5 s, 9x), ~35 s a run with the model
# idle. The end runs as the character's queue step (RUNNING line, queue_done / queue_interrupted event); anything
# sent for the party meanwhile first waits for it (END_BG_HOLD_S). BG3AI_END_ASYNC=0 reverts (blocking end).
END_ASYNC = os.environ.get("BG3AI_END_ASYNC", "1") != "0"
END_BG_HOLD_S = 16.0  # Lua fails an ignored end at 3 s, the HUD fallback takes ≤ ~10 s more (select + press + 9 s watch)
_bg_ends: dict[str, float] = {}  # char -> when its background end started
# The first (or only) plan: extra wait past wait_turn while enemy turns still progress. 30 s until S080 02:31:45:
# a 12-cultist enemy phase outlasted 30 + 30 s and the plan came back "FAILED: it is not Claude's turn (now ?)".
TURN_EXTEND_S = 120.0
TURN_STILL_ACTING = "enemies still acting"  # _wait_turn's why when the extension ends while turns still progress
TURN_NOT_YET = "combat_turn: NOT PLAYED YET — "  # combat_turn's answer then (nothing played)
# How often a wait for a party turn (combat_turn sent ahead, end_turn wait) reads state.combat (~10-20 ms): the turn's
# start reaches the agent this much late at most (0.3 / 0.5 s until 2026-10-16: ~0.2 s mean per turn, 8-15 a run).
TURN_POLL_S = float(os.environ.get("BG3AI_TURN_POLL_S", "0.15"))
TURN_PROGRESS_S = 10.0  # ... "progress" = the acting combatant changed this recently


def _in_queue_worker() -> bool:
    return threading.current_thread().name.startswith("actq-")


def _end_in_background(char: str) -> bool:
    """Start char's end_turn as its action queue's only step and return at once. Interrupts never cancel it (an end
    is no walk); the queue's usual settings are kept for later queues. False: its queue is busy (end it inline)."""
    if queueing._queues().busy(char):
        return False
    had, prev = char in queueing._queue_ignore, queueing._queue_ignore.get(char)
    batch._enqueue(char, [actq.Step("end_turn", {"wait": False})], ignore=",".join(actq.INTERRUPT_KINDS))
    if had:
        queueing._queue_ignore[char] = prev  # the running executor keeps its own (all-kinds) set
    else:
        queueing._queue_ignore.pop(char, None)
    _bg_ends[char] = time.monotonic()
    return True


def _bg_end_running(char: str) -> bool:
    """char's background turn end is still running (past any hold), seen from outside the queue worker."""
    return char in _bg_ends and not _in_queue_worker() and queueing._queues().busy(char)


def _hold_for_bg_end(char: str | None = None) -> str:
    """Before an action: a background end (of char, or of anyone when None) still running? Wait for it (≤
    END_BG_HOLD_S), so the next turn's steps never land in the turn being ended. A note or ''."""
    if not _bg_ends or _in_queue_worker():
        return ""
    keys = [c for c in list(_bg_ends) if char is None or c == char]
    if not keys:
        return ""
    t0 = time.monotonic()
    held = False
    while any(queueing._queues().busy(c) for c in keys) and time.monotonic() - t0 < END_BG_HOLD_S:
        held = True
        time.sleep(0.05)
    for c in keys:
        if not queueing._queues().busy(c):
            _bg_ends.pop(c, None)
    if not held:
        return ""
    return (f"(waited {render.num(round(time.monotonic() - t0, 1))}s for "
            f"{', '.join(queueing._label(c) for c in keys)}'s turn end to finish first)")


def _summon_first(why: str, plan: str, ignore: str) -> str | None:
    """A turn refused because our own summon (Spiritual Weapon …) must act first and the plan has no turn for it
    (S035: 3 chains stopped 'now e510 Spiritual Weapon', up to 21.7 s waited, a re-send each): play the summon's
    turn as a player would — its weapon attack on the plan's first target (the nearest visible hostile when that
    one is gone or out of sight), else end it — then the caller retries. None: not a summon of ours."""
    m = re.search(r"\(combat_turn who=([^)\s]+)\)", why)
    if not m:
        return None
    try:
        su = plumbing._uuid(m.group(1))
    except ValueError:
        return None
    cs = core.game.try_rpc("state.combat") or {}
    row = next((o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("uuid") == su), None)
    if not row or not row.get("summon"):
        return None
    hostile = {o.get("uuid") for o in cs.get("order") or [] if isinstance(o, dict)
               and o.get("relation") == "hostile" and not o.get("dead")}
    tgt = None
    for t in re.findall(r'"target"\s*:\s*"([^"]+)"', plan or ""):
        try:
            u = plumbing._uuid(t)
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
    sp = ({"move": {"target": core.H.get(tgt), "within": 1.5},
           "action": [{"ability": "Target_MainHandAttack", "target": core.H.get(tgt)}], "on_reaction": "skip"}
          if tgt else {})
    txt = _combat_turn_one(json.dumps(sp), core.H.get(su), True, False, ignore, 5.0)
    return f"{queueing._label(su)} (your summon) had to act first and wasn't in the plan: played it (weapon attack on " \
           f"the plan's target, else ended)\n" + txt


def _chain_first(why: str, later: list[tuple[str, str]]) -> int | None:
    """A member's turn refused because '<eX> (yours) must act first: play that turn (combat_turn who=eX)':
    the index in later of the member that is eX, or None."""
    m = re.search(r"\(combat_turn who=([^)\s]+)\)", why)
    if not m:
        return None
    try:
        first = plumbing._uuid(m.group(1))
    except ValueError:
        return None
    for j, (w, _pl) in enumerate(later):
        try:
            if plumbing._who_uuid(w) == first:
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


def _plan_abilities(p: turn.TurnPlan) -> list[str]:
    """The abilities a plan names (as written), in order, once each."""
    out: list[str] = []
    for alt in [a for k in ("action", "bonus") for a in p.slots.get(k) or []] + \
            [a for slot in p.slots.get("extra") or [] for a in slot]:
        a = alt.step.args.get("ability") if alt.step.tool == "use_ability" else None
        if a and str(a) not in out:
            out.append(str(a))
    return out


def _plan_fits(u: str, abilities: list[str]) -> bool:
    """Whether `u` has every ability the plan names (its own action list, read now)."""
    acts = core.game.try_rpc("state.actions", {"uuid": u})
    overview._learn_actions(u, acts)
    rows = acts.get("actions") if isinstance(acts, dict) else acts
    have = {a.get("spell") for a in rows or [] if isinstance(a, dict)}
    for a in abilities:
        try:
            if overview._ability_id(u, a) not in have:
                return False
        except (GameError, ValueError):
            it = world_tools._carried_scroll(u, a)   # A7: a scroll `u` carries is read in combat too
            if not (it and it.get("owner") == u):
                return False
    return True


def _plan_actor(p: turn.TurnPlan, cands: list[str]) -> list[str]:
    """ACT-2 (S065: Claude's turn "skipped" twice): with `who` left out, the candidates whose actions cover every
    ability the plan names. A plan with no ability (a move, {}) fits all of them."""
    abilities = _plan_abilities(p)
    return [u for u in cands if _plan_fits(u, abilities)] if abilities else list(cands)


def _combat_turn_one(plan: str, who: str | None, end: bool, wait_next: bool, ignore: str, wait_turn: float,
                     extend_s: float = 0.0) -> str:
    was = reporting._batch.get("in_turn")
    reporting._batch["in_turn"] = actq.BATCH_CHECKS  # its steps are moves in combat (see move_to)
    try:
        return _combat_turn_body(plan, who, end, wait_next, ignore, wait_turn, extend_s)
    finally:
        reporting._batch["in_turn"] = was
        turn_plan._turn_cs.clear()


def _combat_turn_body(plan: str, who: str | None, end: bool, wait_next: bool, ignore: str, wait_turn: float,
                      extend_s: float = 0.0) -> str:
    t0 = time.monotonic()
    if core._session_over():
        return reporting._finish("combat_turn", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    p = turn.parse_plan(plan, batch._BATCHABLE)
    bg_held = _hold_for_bg_end()  # the previous turn's background end first: the turn order moves with it
    cs = st0 = None
    if actq.BATCH_CHECKS:  # both reads in one round trip
        try:
            cs, st0 = delta.multi_rpc(core.game, [("state.combat", {}), ("state.status", {})])
        except GameError:
            cs = st0 = None
    if not isinstance(cs, dict):
        cs = core.game.rpc("state.combat")  # its error, as before
    plumbing._remember_names(cs)
    if not isinstance(st0, dict):
        st0 = core.game.try_rpc("state.status") or {}  # one read for the party and the selected character
    party = {c.get("uuid") for c in st0.get("party") or [] if isinstance(c, dict)}
    if not cs.get("active"):
        # S025: sent as the last enemy died; nothing to play is not a failure. Opening a fight is a plain attack.
        return reporting._finish("combat_turn", {"who": who}, "combat_turn: nothing to play — not in combat (combat is over, "
                       "or start a fight with use_ability / do_actions)", t0)
    turn_plan._seen_sides(cs)
    now = turn.acting(cs)
    waited, still_acting = "", ""
    want = plumbing._who_uuid(who) if who else None
    inferred = ""
    if not want:
        # ACT-2: who left out. Several of ours acting (a shared turn group) or a plan sent ahead: the member the
        # plan's abilities fit; acting now and no single fit → refused; sent ahead → the one selected now (the
        # first party turn that comes, a summon's or another member's, used to take the plan and end its turn).
        mine0 = turn.party_acting(cs, party)
        if len(mine0) > 1:
            fits = _plan_actor(p, mine0)
            if len(fits) != 1:
                names = ", ".join(queueing._label(u) for u in mine0)
                why = (f"the plan fits {', '.join(queueing._label(u) for u in fits)}" if fits
                       else "the plan's abilities fit none of them alone")
                return reporting._finish("combat_turn", {}, f"REFUSED: several of your characters can act now ({names}) and "
                               f"{why}: give who=<member> (or a list plan, one entry per member)", t0)
            want, inferred = fits[0], "the only one acting whose actions the plan fits"
        elif not mine0:
            rows = [r for r in st0.get("party") or [] if isinstance(r, dict) and r.get("uuid")
                    and not r.get("dead") and not r.get("off_stage") and not r.get("downed")]
            fits = _plan_actor(p, [r["uuid"] for r in rows]) if _plan_abilities(p) else []
            if len(fits) == 1:
                want, inferred = fits[0], "sent ahead: the only one of yours whose actions the plan fits"
            else:
                try:
                    want, inferred = plumbing._active(), "sent ahead: the character selected when it was sent"
                except GameError:
                    want = None
    if want and want in turn.downed(cs):
        return reporting._finish("combat_turn", {"who": who}, turn_slots._downed_text(want), t0)
    if want and want in turn.passing(cs):
        # S087 06:02:48: "NOW e1's turn: plan it; also e2 Gale" for two Stunned members: nothing to play
        why = next((o.get("passes") for o in cs.get("order") or [] if o.get("uuid") == want), None) or "can't act"
        r = core.game.run_job("act.end_turn", {"char": want}, wait_s=5)
        return reporting._finish("combat_turn", {"who": who}, f"{queueing._label(want)} is {why}: nothing to play, its turn passes ("
                       + ("ended it" if r.status == "done" else f"the game passes it: {r.error or r.status}") + ")", t0)
    ready = (lambda c: want in turn.acting(c)) if want else (lambda c: bool(turn.party_acting(c, party)))

    def blocked(c: dict) -> str:
        if want and want in turn.downed(c):
            return f"{queueing._label(want)} is downed"
        first = turn.blocking(c, party, want) if want else []
        if not first:
            return ""
        if _bg_end_running(first[0]):  # review L1: not "end it" while its end still runs (a 2nd order races it)
            return (f"{queueing._label(first[0])}'s turn end is still running in the background: wait_for(target=\"queue\"), "
                    "then send this again")
        # S084: while the last member's end runs, the game can show a member that ended earlier as open: name the end
        running = [c for c in list(_bg_ends) if _bg_end_running(c)]
        if running:
            return (f"{queueing._label(running[0])}'s turn end is still running in the background (the game shows "
                    f"{queueing._label(first[0])}'s turn open meanwhile): wait_for(target=\"queue\"), then send this again")
        return (", ".join(queueing._label(u) for u in first) + " (yours) must act first: play that turn "
                f"(combat_turn who={core.H.get(first[0])}) or end it (end_turn who={core.H.get(first[0])})")

    answered: list[str] = []

    def on_prompt(ui: dict) -> bool:
        # the plan's on_reaction also answers the prompts that come while it waits (an enemy's attack → Shield
        # Blow / Hellish Rebuke), like the game's "always"/"never" setting; anything else still stops the wait
        if not p.on_reaction or ui.get("screen") != "reaction" or len(answered) >= 6:
            return False
        try:
            answered.append(turn_slots._answer_reaction(p.on_reaction == "use"))
        except Exception as e:
            answered.append(f"reaction: could not answer ({e})")
            return False
        return True

    if not ready(cs) and wait_turn > 0:
        w0 = time.monotonic()
        cs, why = turn_slots._wait_turn(ready, wait_turn, blocked, on_prompt, extend_s, answered)
        plumbing._remember_names(cs)
        turn_plan._seen_sides(cs)
        if not cs.get("active"):
            raise GameError("combat is over: loot, heal, then end_combat_report")
        now = turn.acting(cs)
        waited = f"waited {time.monotonic() - w0:.1f}s for the turn" + (f" ({why})" if why else "")
        still_acting = why[len(TURN_STILL_ACTING):].strip(" ()") if why.startswith(TURN_STILL_ACTING) else ""
        if still_acting:
            still_acting = f"after {time.monotonic() - w0:.0f}s ({still_acting}"
        if answered:
            waited += " — " + "; ".join(answered)
        if want and want in turn.downed(cs):
            return reporting._finish("combat_turn", {"who": who}, turn_slots._downed_text(want), t0)
    mine = turn.party_acting(cs, party)
    if want:
        char = want
    else:
        sel = st0.get("active") if not waited else None
        if not sel:
            sel = plumbing._active()
        char = sel if sel in mine else (mine[0] if mine else sel)
    if char in turn.downed(cs):
        return reporting._finish("combat_turn", {"who": who}, turn_slots._downed_text(char), t0)
    if char not in now and still_acting:
        # S080 02:31:45: "FAILED: it is not e4 Claude's turn (now ?)" after 60 s of a 12-cultist enemy phase
        acting = [queueing._label(u) for u in now if u not in party]
        return reporting._finish("combat_turn", {"who": who}, (
            f"{TURN_NOT_YET}the enemies are still taking their turns {still_acting}; now "
            f"{', '.join(acting) or 'an enemy you cannot see'}). Nothing was played or spent: send the same plan "
            f"again (it waits for {queueing._label(char)}'s turn)"), t0)
    if char not in now:
        ended = any(o.get("uuid") == char and o.get("ended") for o in cs.get("order") or [])
        why = f"{queueing._label(char)} already ended its turn" if ended else f"it is not {queueing._label(char)}'s turn"
        raise GameError(f"{why} (now {', '.join(queueing._label(u) for u in now) or '?'})" + (f"; {waited}" if waited else "")
                        + (f" {bg_held}" if bg_held else ""))  # S084: "waited 0.0s" hid the hold's wait
    lines = [f"TURN {queueing._label(char)}" + (f" (round {cs['round']})" if cs.get("round") is not None else "")
             + (f" — {waited}" if waited else "") + observe._light_note(cs)]
    limit = render.turn_limit_line(observe._story_timer())  # read now: the wait for this turn may have ticked it
    if limit:
        lines.append(limit)
    if bg_held:
        lines.append(bg_held)
    if inferred:
        lines.append(f"who: {queueing._label(char)} ({inferred}; give who= to choose)")
    turn_plan._turn_move_objective(p)
    fight_hint = reporting._fight_hint(cs, party)   # the first turn of a fight: a retry's key facts / past debriefs' warnings
    if fight_hint:
        lines.append(fight_hint)
    notes: list[str] = []
    for alt in [a for k in ("action", "bonus") for a in p.slots.get(k) or []] + \
            [a for slot in p.slots.get("extra") or [] for a in slot]:
        if alt.step.tool == "use_ability" and alt.step.args.get("ability"):
            try:  # shown names → ids first: the fallbacks below compare ids (retarget, variants, tried)
                actor = alt.step.args.get("who") and plumbing._who_uuid(alt.step.args["who"]) or char
                sp, note = overview._own_attack(actor, overview._ability_id(actor, alt.step.args["ability"]))
                alt.step.args["ability"] = sp
                if note and note not in notes:
                    notes.append(note)
            except (GameError, ValueError):
                pass  # use_ability says why when that alternative runs
    lines += notes
    if p.reactions:
        try:
            uitools._reactions_rpc(char, {"char": char, "set": p.reactions})
            lines.append("reactions set: " + ", ".join(f"{k}={v}" for k, v in p.reactions.items()))
        except GameError as e:
            lines.append(f"reactions: FAILED — {e}")
    watcher = turn.TurnWatcher(queueing._watch_conn(), p.on_reaction, turn_slots._answer_reaction, ignore=queueing._parse_ignore(ignore))
    if actq.BATCH_CHECKS:  # the next step check's state.combat comes with the check after each step
        watcher.post_ops = [("state.combat", {})]
        watcher.on_post = lambda got: turn_plan._cs_keep(got.get("state.combat"))
    watcher.baseline()
    if watcher.open_prompt is not None:
        # S094 10:05:14: the chain played Wyll's turn (then the Emperor's) into the Shield Blow prompt Claude's walk
        # had opened: move and Dash 'never started', both turns lost. The prompt is answered first, by the plan's
        # on_reaction, else by the agent: nothing is played into it.
        if not p.on_reaction:
            return reporting._finish("combat_turn", {"who": who}, (
                f"{TURN_NOT_YET}{queueing._label(char)}'s turn not started: {turn_slots._prompt_why(watcher.open_prompt)}. Nothing was "
                "played or spent"), t0)
        watcher.answered.append(turn_slots._answer_reaction(p.on_reaction == "use"))
        watcher.settle()
    after: dict = {}
    info: dict = {"party": party, "retarget": p.retarget}
    stopped = False
    if actq.BATCH_CHECKS:
        turn_plan._cs_keep(cs)
    else:
        turn_plan._turn_cs["cs"] = cs  # nothing ran since this read: the first step check reuses it
    moved = False
    for slot in p.order:
        if turn_plan._hp_held(watcher, lines):
            stopped = True
            break
        if p.move and p.move.target and p.retarget and (slot == "move" or (moved and slot in ("action", "bonus"))):
            # The move's target died or went out of sight (sent ahead / during the walk): aim the plan at the
            # nearest visible hostile, and walk again if the move already ran and movement is left.
            if turn_slots._retarget_lost(p, char, lines, info) and slot != "move" and moved \
                    and not (isinstance(after.get("movement"), (int, float)) and after["movement"] < 1.0):
                if turn_plan._turn_move(p.move, char, watcher, lines, after, info) == "stop":
                    stopped = True
                    break
        if slot == "move":
            # a lost target (said above): no walk (S028: "move e742: FAILED — target is not visible" after it)
            res = turn_plan._turn_move(p.move, char, watcher, lines, after, info) if p.move and not info.get("move_lost") \
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
                if turn_plan._hp_held(watcher, lines):
                    res = "stop"
                    break
                res, _ = turn_slots._turn_slot(f"extra {i}", alts, char, watcher, lines, after, info)
                if (res == "none" and alts and lines and lines[-1].startswith(f"extra {i}:")
                        and turn.missed_target(lines[-1])):
                    res = turn_slots._turn_auto_fallback(p, f"extra {i}", alts, char, watcher, lines, after, info)
                if res == "stop":
                    break
        else:
            res, then_move = turn_slots._turn_slot(slot, p.slots[slot], char, watcher, lines, after, info)
            if res == "ok" and then_move and p.move:
                res = "stop" if turn_plan._hp_held(watcher, lines) else turn_plan._turn_move(p.move, char, watcher, lines, after, info)
            elif (res == "none" and slot in ("action", "bonus") and p.slots[slot] and lines
                  and lines[-1].startswith(f"{slot}:") and turn.missed_target(lines[-1])):
                res = turn_slots._turn_auto_fallback(p, slot, p.slots[slot], char, watcher, lines, after, info)
            if res == "none" and slot in ("action", "bonus") and lines and overview._unknown_ability_line(lines[-1], slot):
                # S035 19:33: action ["Shout_Dodge"] → "unknown action" (refused, nothing spent), then end_turn threw
                # Wyll's action away. A wrong id/name is a plan slip: keep the turn open for the real choice.
                if "Dodge" in lines[-1]:
                    lines.append("BG3 has no Dodge action (5e only): Shout_Disengage, Shout_Dash, Shout_Hide, or an attack")
                res = "stop"
        if not actq.BATCH_CHECKS:  # BATCH_CHECKS: kept until a step runs (_run_one clears it)
            turn_plan._turn_cs.clear()
        if res == "stop":
            stopped = True
            break
    if not actq.BATCH_CHECKS:
        turn_plan._turn_cs.clear()
    if not stopped and turn_plan._hp_held(watcher, lines):
        stopped = True
    if not stopped and p.auto_bonus and not p.slots["bonus"]:
        if turn_slots._turn_auto_bonus(char, watcher, lines, after) == "stop" or turn_plan._hp_held(watcher, lines):
            stopped = True  # M6: a drop during the automatic bonus: "decide the rest", not an end_turn
    lines.extend(watcher.answered)
    spent = turn_plan._spent_note(char, lines)
    if spent:
        lines.append(spent)
    if end and not stopped:
        fatal, why = turn_plan._turn_check(char, None)
        if fatal:
            lines.append(f"end_turn: {why}")
        elif _end_async_ok(char, party, wait_next, turn_plan._cs_cached() if actq.BATCH_CHECKS else None) \
                and _end_in_background(char):
            lines.append("end_turn: ending in the background (the game takes 1-4 s; its result shows as queue_done): "
                         "plan the next turn now, a combat_turn sent meanwhile waits for it")
        else:
            ex = queueing._new_executor(watcher, queueing._watch_conn(), char)
            text, _status, res = ex.call_tool(actq.Step("end_turn", {"wait": wait_next}), char)
            lines.append(re.sub(r" — after=\{[^}]*\}| after=\{[^}]*\}", "", text))
            if isinstance(res, dict) and isinstance(res.get("after"), dict):
                after.update(res["after"])
    elif stopped and char in turn.downed((turn_plan._cs_cached() if actq.BATCH_CHECKS else None)
                                         or core.game.try_rpc("state.combat") or {}):
        lines.append(f"{queueing._label(char)} went down: its turn passes by itself (death saving throws); "
                     "get it up with Healing Word / Help from an ally")
    elif stopped:
        lines.append("turn NOT ended: decide, then continue (combat_turn again, or single tools)")
    turn_plan._turn_cs.clear()
    now = turn_plan._now_line(after)
    if now:
        lines.append(now.replace("NOW ", f"NOW {core.H.get(char)} ", 1))
    changes = delta.changes_text(core.game, core.H)
    if changes:
        lines.append(changes)
    if end and not stopped:
        nxt = turn_slots._turn_next(char, party)
        if nxt:
            lines.append(nxt)
    else:
        lines += turn_slots._disabled_lines()
    if stopped:
        reporting._batch["last"] = "interrupted"
    return reporting._finish("combat_turn", {"who": who}, "\n".join(lines), t0)


def _end_async_ok(char: str, party: set, wait_next: bool, cs: dict | None = None) -> bool:
    """End this turn in the background? Only when asked not to block (wait_next false), outside a batch/queue, and
    nobody else of the party can act now: a shared turn group's other members play right after (their combat_turn
    must see this end done), so ends inside a group stay inline (0.4 s: Actions.lua END_REQUEST_AFTER_MS); so does
    an end whose next turn (visible order) is ours anyway (the result then shows that member's actions)."""
    if not END_ASYNC or wait_next or reporting._batch["active"] or _in_queue_worker():
        return False
    cs = cs or core.game.try_rpc("state.combat") or {}  # cs: the end check's own read (BATCH_CHECKS)
    if not cs.get("active"):
        return False
    if [u for u in turn.party_acting(cs, party) if u != char]:
        return False
    return not turn.next_is_party(cs, party, ended=char)


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    macro_tools,
)
