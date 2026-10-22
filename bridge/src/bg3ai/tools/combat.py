"""Combat tools: use_ability (hotbar and two-step casts), end_turn, flee and the end-of-combat report.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .. import boxclick, hotbar, hoverclick, render, turn, twostep
from ..game import GameError, JobResult
from . import core, overview, plumbing, reporting, world_tools

# ---------------------------------------------------------------------------
# act: combat
# ---------------------------------------------------------------------------

@core.mcp.tool()
@reporting._guard
def use_ability(ability: str, target: str = "", x: float | None = None, y: float | None = None,
                z: float | None = None, who: str | None = None, upcast: int = 0, targets: str | list[str] = "",
                wait: bool = True, force: bool = False, which: str = "") -> str:
    """Use an action/spell/attack/item from get_actions: at a target id, at a position (x,y,z), or on self. upcast: spell slot level to cast at. which: the variant of an action that has several, by its name (Gather Your Allies: the ally group, e.g. which="<group name>" with x,y,z = the ground where they appear; the result lists who arrived); without it the refusal lists them. targets: a list (or comma list) for multi-target spells, in click order (the same id twice = 2 darts/beams; all on one target = cast at it, which takes every beam); a cast may use only the first 2 (the result then says \"cast on the first 2 only\"). Costs and rules are the game's. Out of combat at a creature, the result says whether it started a fight (COMBAT: ...) or not (combat: NOT started — a dormant/unaware target). Two-step spells: Dimension Door takes x,y,z = the landing (target = an ally beside you to bring, else you go alone); the Revivify spell takes target = the body; while their route is off they are refused: read a Scroll of Revivify instead. A carried scroll works by name ("Scroll of Invisibility", or the spell's name when it isn't an action): it is read and its target clicked (default: who; targets = one click per beam/pick, e.g. Scorching Ray; x,y,z = the ground for a point/cone scroll: Cone of Cold, Misty Step, Fireball); in combat too, on the reader's turn for an action, like casting. A harmful area spell (Fireball) whose blast would catch a party member, ally or neutral in sight, or that would burst short of its point (the way there blocked, e.g. a ledge), is refused with where it bursts and who is in the blast; force=true casts anyway."""
    char = plumbing._who(who)
    try:
        aid = overview._ability_id(char, ability)
    except GameError as e:
        if "has no action called" not in str(e):
            raise
        word = str(ability).strip().lower()
        if word in ("disarm", "disarm trap") and target:
            # S031 17:10: use_ability("Disarm") after a disarm the agent doubted → "no action called"; it is the
            # disarm tool (the right-click menu entry), not a hotbar action.
            return world_tools.disarm(target, who)
        if word in ("lockpick", "pick lock", "lock pick") and target:
            return world_tools.lockpick(target, who)
        scroll = world_tools._carried_scroll(char, ability)
        if scroll is None:
            raise
        more = (targets.split(",") if isinstance(targets, str) else [str(t) for t in targets]) if targets else []
        return world_tools._use_scroll(char, scroll, target, [t.strip() for t in more if t.strip()],
                           pos=[x, y, z] if None not in (x, y, z) else None)
    spell, note = overview._own_attack(char, aid)
    tail = ""
    args: dict[str, Any] = {"char": char, "spell": spell}
    if target:
        args["target"] = plumbing._uuid(target)
    picks: list[str] = []
    if targets:
        # a list (["e1","e2"], as a plan or a client sends it) or a comma list: the list reached targets.split and
        # failed the step "'list' object has no attribute 'split'" (audit ACT-8)
        names = targets.split(",") if isinstance(targets, str) else [str(t) for t in targets]
        args["targets"] = [plumbing._uuid(t.strip()) for t in names if t.strip()]
        picks = ([args["target"]] if "target" in args else []) + args["targets"]
    if None not in (x, y, z):
        args["pos"] = [x, y, z]
    if upcast:
        args["upcast"] = upcast
    if force:
        args["force"] = True
    if which:
        args["which"] = which
    if twostep.TWOSTEP_ENABLED and twostep.is_two_step(spell) and not upcast and len(picks) <= 1:
        # ACT-9: Revivify (the body, then the ground beside it) and Dimension Door (who goes, then the landing) by
        # the player's clicks; with the route off the mod refuses them (its TargetPos2 write is off)
        return _twostep_use(char, spell, args, note)
    hb_on = hotbar.HOTBAR_CAST_ENABLED and "pos" not in args and not upcast
    if len(picks) > 2 and hb_on:
        # ACT-8: Bless on 3, Magic Missile darts, Scorching Ray / Eldritch Blast beams: the player's route (hotbar
        # cell, one click per target, the tick); the mod checks the cast first and watches it like an Osiris cast
        return _hotbar_use(char, spell, picks, args, note)
    if _one_pick(picks):
        args.pop("targets", None)
        args["target"], tail = picks[0], _one_pick(picks)
    elif len(picks) > 2:
        # S018/S025/S029: Bless on 3 was refused ("more than two targets"), losing the whole action (the game's
        # cast call takes 2 targets). Cast it on the first two and say who was left out.
        keep = 2 - (1 if "target" in args else 0)
        args["targets"] = args["targets"][:keep]
        tail = _two_only(picks, "the hotbar route for more is off" if not hotbar.HOTBAR_CAST_ENABLED
                         else "an upcast or a position goes by the 2-target cast")
    opener = (wait and not reporting._batch["active"] and "target" in args and args["target"] not in (turn_plan._party_cached() or ())
              and not (core.game.try_rpc("state.combat") or {}).get("active"))
    post = (lambda r: _opener_combat(args["target"], r.result) if r.status == "done" else "") if opener else None
    out = plumbing._act("use_ability", "act.cast", args, wait_s=30, wait=wait, post=post)
    return _with_tail(out, tail, note)


def _one_pick(picks: list[str]) -> str:
    """S066 #59: Eldritch Blast targets=[Orin]x3 went to the 2-target cast as UseSpell(Orin, Orin): "the game never
    started it" (Wyll's boss-round action lost), while one target takes every beam (S065 11:52: 3 Force hits on
    Orin). When the first pick repeats, the cast goes at it alone: what to say then ('' = picks as given)."""
    if len(picks) < 2 or picks[0] != picks[1]:
        return ""
    rest = [u for u in dict.fromkeys(picks) if u != picks[0]]
    return (f"targets: {picks.count(picks[0])} picks on {queueing._label(picks[0])}: cast at it alone (one target takes "
            f"every beam/dart)" + (f"; left out: {', '.join(queueing._label(u) for u in rest)}" if rest else ""))


def _two_only(picks: list[str], why: str) -> str:
    return (f"targets: cast on the first 2 only, {len(picks) - 2} of {len(picks)} NOT targeted ({why}); left out: "
            f"{', '.join(queueing._label(u) for u in picks[2:])}")


def _with_tail(out: Any, tail: str, note: str) -> Any:
    if tail and isinstance(out, str):
        out = re.sub(r"(\n⏱[^\n]*)?$", lambda m: f"\n{tail}" + (m.group(0) or ""), out, count=1)
    return f"{note}\n{out}" if note and isinstance(out, str) else out


@plumbing._input_exclusive
def _hotbar_use(char: str, spell: str, picks: list[str], args: dict, note: str) -> str:
    """use_ability on >2 picks through the hotbar (bg3ai.hotbar.hotbar_cast). act.cast {ui=true} checks the cast as
    for an Osiris order (our turn, known/prepared, each pick seen and in range) and then only watches it
    (CastSpell/CastedSpell, hits, statuses; the game charges it, the guard adds nothing). Once that job runs, the
    clicks go in (on_start). If the route fails before anything was cast (every failure cancels the spell in
    hand), the job is cancelled and the first 2 picks are cast by Osiris, as with the route off. Always waits:
    the clicks need the tool's thread (wait=false is ignored here)."""
    hb_args = {k: v for k, v in args.items() if k not in ("target", "targets")}
    hb_args.update(targets=picks, ui=True)
    hb: dict[str, Any] = {}
    sel = _select_for_clicks(char)
    stop = _click_stop()

    def clicks(ctx: str, jid: int) -> None:
        try:
            res = hotbar.hotbar_cast(core.game, world_tools._inp(), char, spell, picks, select=False, should_stop=stop)
        except GameError as e:
            res = hotbar.HotbarCastResult(False, reason="error", stage="slot", detail={"why": str(e)})
        except Exception as e:
            res = _click_error(char, e)
        hb["res"] = res
        if not res.ok and res.reason != "fired_early":
            core.game.try_rpc("job.cancel", {"id": jid}, context=ctx)
        else:
            core.game.try_rpc("act.ui_clicked", {"id": jid}, context=ctx)

    def redo(r: JobResult):
        res = hb.get("res")
        if res is None or res.ok or res.reason == "fired_early":
            return None   # refused before the clicks (the mod's own checks), or the cast went off: no second try
        if res.stage not in HOTBAR_REDO_STAGES or res.reason == "cancelled" or r.status == "done" \
                or (isinstance(r.result, dict) and r.result.get("began")):
            # review M3: past the cell (targets clicked) the cast may have started: never a second cast
            hb["tail"] = (f"hotbar: the route failed at {res.stage} ({res.detail.get('why') or res.reason}); "
                          "not cast again by Osiris (the cast may have started: check the NOW line / CHANGES)")
            return None
        two = {k: v for k, v in args.items() if k not in ("target", "targets")}
        two.update(target=picks[0], **({} if _one_pick(picks) else {"targets": picks[1:2]}))
        why = res.detail.get("why") or res.reason
        hb["tail"] = _one_pick(picks) or _two_only(picks, f"the hotbar route failed at {res.stage}: {why}")
        return two, ""

    out = plumbing._act("use_ability", "act.cast", hb_args, wait_s=30, wait=True, on_start=clicks, redo=redo)
    res = hb.get("res")
    tail = "\n".join(x for x in (sel, hb.get("tail", "")) if x)
    if res is not None and not hb.get("tail"):
        d = res.as_dict()
        tail = (sel + "\n" if sel else "") + (f"hotbar: {res.picked} of {len(picks)} targets clicked in {d['elapsed_s']:g}s"
                + (f" (OVER the {d['budget_s']:g}s budget)" if d.get("over_budget") else ""))
        if res.reason == "fired_early":
            tail += f"; the game cast after {res.picked} (the spell's max), the rest left out"
    tail = "\n".join(x for x in (tail, _hand_back(char)) if x)
    if res is not None:
        _log_hotbar(spell, res)
    return _with_tail(out, tail, note)


@plumbing._input_exclusive
def _twostep_use(char: str, spell: str, args: dict, note: str) -> str:
    """use_ability on a two-step spell through the hotbar (bg3ai.twostep.twostep_cast): act.cast {ui=true} checks
    the cast (the body / who goes seen and in range, the landing in range and on a floor) and watches it; once that
    job runs, the clicks go in. A failed route cancels the spell in hand and the job; there is no Osiris fallback
    (the second pick can't be given that way). Always waits (the clicks need the tool's thread)."""
    ui_args = {k: v for k, v in args.items() if k not in ("targets",)}
    if args.get("targets") and "target" not in ui_args:
        ui_args["target"] = args["targets"][0]
    ui_args["ui"] = True
    landing = ui_args.get("pos") if not twostep.is_revive(spell) else None
    first = ui_args.get("target") or (char if landing is not None else None)
    hb: dict[str, Any] = {}
    sel = _select_for_clicks(char)
    stop = _click_stop()

    def clicks(ctx: str, jid: int) -> None:
        if not first:
            return
        try:
            res = twostep.twostep_cast(core.game, world_tools._inp(), char, spell, first, landing=landing, select=False,
                                       should_stop=stop)
        except GameError as e:
            res = hotbar.HotbarCastResult(False, reason="error", stage="slot", detail={"why": str(e)})
        except Exception as e:
            res = _click_error(char, e)
        hb["res"] = res
        if not res.ok:
            core.game.try_rpc("job.cancel", {"id": jid}, context=ctx)
        else:
            core.game.try_rpc("act.ui_clicked", {"id": jid}, context=ctx)

    out = plumbing._act("use_ability", "act.cast", ui_args, wait_s=30, wait=True, on_start=clicks)
    res = hb.get("res")
    hand = _hand_back(char)
    tail = sel
    if res is not None:
        d = res.as_dict()
        what = "who goes + the landing" if landing is not None else "the body + the ground beside it"
        tail = tail + "\n" if tail else ""
        if res.ok:
            tail += (f"hotbar: {what} clicked in {d['elapsed_s']:g}s"
                    + (f" (OVER the {d['budget_s']:g}s budget)" if d.get("over_budget") else ""))
        else:
            tail += (f"hotbar: the two-step route failed at {res.stage} ({d.get('why') or res.reason}); nothing was"
                    " cast" + ("" if landing is not None else
                               ": a Scroll of Revivify by name, or Withers at camp, also bring a body back"))
        _log_hotbar(spell, res)
    return _with_tail(out, "\n".join(x for x in (tail, hand) if x), note)


def _hand_back(char: str) -> str:
    """A17 (L8 23:59 se, Dimension Door: "the game refused the cast", the spell still in hand): however a hotbar
    route ended (the mod's refusal after the clicks, a cast that never started), no spell stays in hand after it.
    A note when one was put back; "" when the hand was empty (one ui.cast_state read)."""
    inp = world_tools._inp()
    if inp is None:
        return ""
    try:
        how = hotbar.cancel(core.game, inp, char)
    except GameError as e:
        return f"the spell in hand could not be put back ({e}): press_key ESCAPE"
    return f"the spell was still in hand after the cast: put back ({how})" if how else ""


# review M3: a failed hotbar route is cast again by Osiris only from these stages (nothing clicked past the cell)
HOTBAR_REDO_STAGES = ("slot", "locate", "arm")


def _select_for_clicks(char: str, shows: str = "the hotbar") -> str:
    """Review H1: the click routes need the caster selected (the hotbar shows the selected character). Select it BEFORE
    act.cast {ui=true} starts: an act.select inside its on_start is a new action for that character, and the mod's
    start() interrupts the running ui job ("superseded by a new action") while the clicks cast for real. A note."""
    if uitools._hud_has(char):
        return ""
    r = core.game.run_job("act.select", {"char": char}, wait_s=5)
    return f"selected {queueing._label(char)} first ({shows} shows the selected character)" if r.status == "done" \
        else f"could not select {queueing._label(char)} first ({r.error or r.status})"


def _click_stop() -> Callable[[], bool]:
    """Review M1: the click routes stop between clicks once the step running them is cancelled or interrupted
    (the queue/batch executor's flags; a plain use_ability has none)."""
    ex = reporting._batch["exec"]
    if ex is None:
        return lambda: False
    return lambda: bool(ex.interrupt or ex.stop.is_set())


def _click_error(char: str, e: Exception) -> Any:
    """Review M2: an input-shim error mid-clicks: put a spell in hand back (best effort) and say what happened."""
    how = None
    try:
        inp = world_tools._inp()
        if inp is not None:
            how = hotbar.cancel(core.game, inp, char)
    except Exception:
        how = "failed"
    detail: dict[str, Any] = {"why": f"{type(e).__name__}: {e}"}
    if how:
        detail["cancelled"] = how
    return hotbar.HotbarCastResult(False, reason="error", stage="error", detail=detail)


def _log_hotbar(spell: str, res: Any) -> None:
    """Each hotbar cast's stages and time (runtime/hotbar_cast.jsonl): the live acceptance item and the latency
    watch read it, the agent never does."""
    try:
        with open(core.RUNTIME / "hotbar_cast.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "spell": spell, **res.as_dict()}, default=str) + "\n")
    except (OSError, AttributeError):
        pass


OPENER_WAIT_S = 1.0  # combat starts a few frames after the hit: how long an opener's result waits for it


def _opener_combat(tu: str, res: Any) -> str:
    """use_ability out of combat at a character: did it start a fight? Acceptance 13:3x: the first Ranged
    Attack on a dormant Shadow did damage but no combat began, and nothing said so. '' for items."""
    ts = core.game.try_rpc("act.target_state", {"uuid": tu}) or {}
    if not isinstance(ts, dict) or "locked" in ts or "opened" in ts or ts.get("held"):
        return ""
    end = time.monotonic() + OPENER_WAIT_S
    while True:
        cs = core.game.try_rpc("state.combat") or {}
        if cs.get("active"):
            return ("COMBAT: the attack started a fight" + (f" (round {cs['round']})" if cs.get("round") else "")
                    + ": play the turns with combat_turn (get_combat_state shows the order)")
        if time.monotonic() >= end:
            break
        time.sleep(0.15)
    if ts.get("dead") or (core.game.try_rpc("act.target_state", {"uuid": tu}) or {}).get("dead"):
        return f"combat: none started ({queueing._label(tu)} died)"
    dmg = res.get("total_damage") if isinstance(res, dict) else None
    if isinstance(dmg, (int, float)) and dmg > 0:
        return (f"combat: NOT started — {queueing._label(tu)} took {render.num(dmg)} damage but no fight began "
                "(dormant or unaware; it acts only once combat starts): attack again or move on")
    return f"combat: not started (no hit on {queueing._label(tu)} seen)"


END_IGNORED = "the game ignored the end-turn order"  # Actions.lua A.END_IGNORED
END_LUA_IGNORED_S = 3.0   # Actions.lua A.END_IGNORED_MS: the Lua job hands back after this
# S066 19:21 #68, S067 19:42 #46 and 19:46 #65: the last member of a shared turn group (the others' ends requested)
# ended 7-10.4 s after its EndTurn (the whole group's end-of-turn work: a Contagion save and the like), past the Lua
# 3 s and this wait's old 3 s, so "the HUD's End Turn didn't end it either" (false failures: the next combat_turn was
# refused, then end_turn found nothing to end). A press the game takes ends the turn in ~0.4 s; a slower end is the
# game's own work, so it is watched, not failed.
END_HUD_WAIT_S = 9.0
END_HUD_QUICK_S = 1.0   # ended this soon after the press: the press did it
# Proposal "ignored EndTurn -> HUD at once" (naut runs 1/3, ~12 s/run): in a shared turn group, after a companion's
# end was requested, every Osiris EndTurn on the avatar was ignored (3 s Lua timeout, then the HUD ended it in ~0.4 s).
# On: when another party member of the current group has already ended, press the HUD's End Turn at once (the Osiris
# order stays the fallback). OFF until acceptance sec_combat "end_turn after a companion's request (probe)" confirms;
# flip live: BG3AI_END_HUD_FIRST=1 (bridge restart) or server.END_HUD_FIRST = True.
END_HUD_FIRST = os.environ.get("BG3AI_END_HUD_FIRST", "0") == "1"


def _peer_requested(cs: dict, char: str) -> str | None:
    """A party member of char's current turn group who already ended its turn (requested; the group still runs)."""
    party = turn_plan._party_set()
    if char not in turn.acting(cs):
        return None
    for o in cs.get("order") or []:
        u = o.get("uuid")
        if u and u != char and u in party and o.get("is_current") and o.get("ended") and not o.get("dead"):
            return u
    return None


def _end_turn_by_hud(char: str, r: JobResult) -> JobResult:
    """The game ignored Osiris EndTurn (S044/S045: our Spiritual Weapon ×4, turns during Ketheric's mid-fight lines
    ×5, 10 s TIMEOUT each): select that character and press the HUD's End Turn (the shared EndTurn command a player
    clicks), then check the turn moved on."""
    cs = core.game.try_rpc("state.combat") or {}
    if not cs.get("active") or char not in turn.acting(cs):   # it ended after all (a late read): never press twice
        return JobResult("done", {"ended": True}, None, r.events, r.elapsed_ms)
    try:
        with plumbing._InputHeld():  # M7: no click/select from the MCP thread between this select and the press
            if not uitools._hud_has(char):  # the press ends whoever the HUD has selected (A16)
                core.game.run_job("act.select", {"char": char}, wait_s=5)
            core.game.rpc("ui.command", {"widget": "*", "command": "EndTurn"}, context="client")
    except GameError as e:
        return JobResult("failed", r.result, f"{r.error}; the HUD's End Turn failed too: {e}", r.events, r.elapsed_ms)
    pressed = time.monotonic()
    deadline = pressed + END_HUD_WAIT_S
    while time.monotonic() < deadline:
        cs = core.game.try_rpc("state.combat") or {}
        if not cs.get("active") or char not in turn.acting(cs):
            late = time.monotonic() - pressed
            via = ("the HUD's End Turn (the game ignored the order)" if late <= END_HUD_QUICK_S else
                   f"the game, slowly ({late + END_LUA_IGNORED_S:.0f} s after the order: end-of-turn effects; the HUD's "
                   "End Turn was pressed too)")
            return JobResult("done", {"ended": True, "via": via}, None, r.events, r.elapsed_ms)
        time.sleep(0.2)
    return JobResult("failed", r.result, f"{r.error}; the HUD's End Turn didn't end it either (a cutscene or line "
                     "may be holding the turn: wait_for, then end_turn again)", r.events, r.elapsed_ms)


@core.mcp.tool()
@reporting._guard
def end_turn(who: str | None = None, wait: bool = True) -> str:
    """End the current character's turn; blocks through enemy turns until a party member can act or combat ends (wait=false: return right after ending it; then wait_for('my_turn'))."""
    t0 = time.monotonic()
    held = turn_tool._hold_for_bg_end()  # combat_turn's background end of the last turn: this one comes after it
    pre = held + "\n" if held else ""
    if who:
        char = plumbing._who_uuid(who)
    else:
        # The selected character, unless it can't act now and exactly one other party member can (the
        # game moves the selection within a shared turn group; ending that one is what the agent means).
        char = plumbing._active()
        cs = core.game.try_rpc("state.combat") or {}
        mine = turn.party_acting(cs, turn_plan._party_set()) if cs.get("active") else []
        if char not in mine and len(mine) == 1:
            char = mine[0]
    args = {"char": char}
    if core._session_over():
        return reporting._finish("end_turn", args, pre + "REFUSED: the session is over. Save and write your handoff notes.", t0)
    if turn_tool._bg_end_running(char):
        # review L1: an end still running after the hold (END_BG_HOLD_S): a second order would race its HUD press
        reporting._batch["last"] = "done"
        return reporting._finish("end_turn", args, pre + f"end_turn {queueing._label(char)}: its turn end is still running in the "
                       "background (the game is slow to hand the turn on): wait_for(target=\"queue\"), don't end it "
                       "again", t0)
    cs = core.game.try_rpc("state.combat") or {}
    if char in turn.downed(cs):
        # S018: end_turn on downed Claude/Shadowheart -> "FAILED: character is downed" (x2).
        body = f"end_turn {queueing._label(char)}: skipped — {turn_slots._downed_text(char)}"
        if wait and not reporting._batch["active"]:
            body += "\n" + _wait_for_party_turn(80.0)
        return reporting._finish("end_turn", args, pre + body, t0)
    peer = _peer_requested(cs, char) if END_HUD_FIRST and cs.get("active") else None
    r = None
    if peer:
        hud = _end_turn_by_hud(char, JobResult("failed", None, f"after {queueing._label(peer)}'s end", [], 0))
        if hud.status == "done":
            r = JobResult("done", {"ended": True, "via": f"the HUD's End Turn at once ({queueing._label(peer)} had ended first)"},
                          None, hud.events, hud.elapsed_ms)
    if r is None:
        r = core.game.run_job("act.end_turn", args, wait_s=15)
        if r.status == "failed" and END_IGNORED in str(r.error or ""):
            r = _end_turn_by_hud(char, r)
    plumbing._remember_names(r.result)
    reporting._batch["last"] = r.status
    nothing = _nothing_to_end(r)
    if nothing:
        # S023-S025: 24 end_turn calls on a turn combat_turn had already ended (shared turn groups) or that
        # wasn't the character's: the goal (that turn over) already holds, so it isn't a failure. Say whose
        # turn it is instead, and wait like a real end_turn would.
        reporting._batch["last"] = "done"
        body = f"end_turn {queueing._label(char)}: nothing to end — {nothing}"
        if wait:
            body += ("\n" if not reporting._batch["active"] else " — ") + _wait_for_party_turn(80.0)
        return reporting._finish("end_turn", args, pre + body, t0)
    body = plumbing._job_text("end_turn", r)
    if not reporting._batch["active"]:
        # Name whose turn it was: in a shared turn group the game moves the selection, and the agent
        # read Lae'zel's after={...} as Claude's (claude-run-2 S001).
        body = body.replace("end_turn:", f"end_turn {queueing._label(char)}:", 1)
    if r.status == "done" and wait and not reporting._batch["active"]:
        body += "\n" + _wait_for_party_turn(80.0)
    elif r.status == "done" and wait:
        body += " — " + _wait_for_party_turn(80.0)
    return reporting._finish("end_turn", args, pre + body, t0)


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
    cs = core.game.try_rpc("state.combat") or {}
    if not cs.get("active"):
        return why + "; combat is over"
    now = turn.acting(cs)
    if "already ended" in err and not now:
        # S088 06:34:50 (×3): "… the rest of its turn group is still acting); now e6 Claude (yours)": the whole group
        # had ended and the game was handing the turn on
        why = "its turn already ended (its whole turn group has ended: the game hands the turn on by itself)"
    party = turn_plan._party_set()
    if now:
        why += "; now " + ", ".join(queueing._label(u) + (" (yours)" if u in party else "") for u in now)
    return why


# Screens that need an answer from the agent while others act.
_PROMPT_SCREENS = ("reaction", "dialogue", "roll", "popup", "game_over")


def _now_turn_text(mine: list[str]) -> str:
    """'now e1's turn', or for a shared party turn group 'now e46's and e1's turn (shared: any order)'."""
    if len(mine) == 1:
        return f"now {core.H.get(mine[0])}'s turn"
    return "now " + " and ".join(f"{core.H.get(u)}'s" for u in mine) + " turn (shared turn group: any order, end each)"


def _wait_for_party_turn(max_s: float) -> str:
    """After ending a turn: poll until a party member is the active combatant, combat ends,
    or a prompt (reaction, roll, dialogue, popup) needs an answer."""
    st = core.game.try_rpc("state.status") or {}
    party = {c.get("uuid") for c in st.get("party") or [] if isinstance(c, dict)}
    deadline = time.monotonic() + max_s
    passed: set = set()
    notes: list[str] = []
    seen, since = None, time.monotonic()
    while True:
        cs = core.game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            return "combat is over"
        mine = turn.party_acting(cs, party)
        if mine:
            return "\n".join([_now_turn_text(mine)] + notes + turn.disabled_lines(cs, queueing._label))
        key = (cs.get("round"), cs.get("current"), tuple(turn.passing(cs)))
        if key != seen:
            seen, since = key, time.monotonic()
        turn_slots._end_passing(cs, since, passed, notes)
        ui = core.game.try_rpc("ui.state", context="client") or {}
        if ui.get("screen") in _PROMPT_SCREENS:
            reporting._batch["last"] = "interrupted"
            return f"waiting stopped: {ui.get('screen')} on screen\n" + "\n".join(reporting._render_ui(ui))
        if time.monotonic() > deadline:
            reporting._batch["last"] = "interrupted"
            return "other combatants are still acting (use wait or get_combat_state)"
        time.sleep(turn_tool.TURN_POLL_S)


FLEE_MIN_M = 27.0   # live2 §2: the Flee button's tooltip "Enemy too close! (5.5/27m)"; at 47 m it asked
FLEE_SEED = (0.8313, 0.9222)   # live2 §2: Flee at (798, 498) of a 960x540 window, right of End Turn
FLEE_BOX_S = 2.0    # the "Do you want to flee from combat?" box shows within this after the press
FLEE_LEFT_S = 5.0   # FleeFromCombat → LeftCombat took 0.7 s live


@core.mcp.tool()
@reporting._guard
def flee(who: str | None = None) -> str:
    """Flee the fight with one character, as the HUD's Flee button does: the game sends them to camp and they leave the fight; the rest of the party stays in it (the fight ends when the last member leaves or the enemies die). The game allows it only with every enemy at least 27 m away (the button's tooltip); the result names the nearest enemy you can see when one is closer."""
    t0 = time.monotonic()
    char = plumbing._who(who)
    name = overview._name_of(char)
    cs = core.game.try_rpc("state.combat") or {}
    if not cs.get("active") or not _in_fight(cs, char):
        return f"REFUSED: {name} is not in a fight"
    near = _nearest_hostile(cs, char)
    if near and near[0] < FLEE_MIN_M:
        # live4 §5: _name_of gives the handle for a hostile never named in a result ("e7 e7"); state.combat names it
        who_near = near[2] or overview._name_of(near[1])
        return (f"REFUSED: enemy too close: {core.H.get(near[1])} {who_near} at {near[0]:.1f} m; "
                f"fleeing needs every enemy {FLEE_MIN_M:.0f} m away (the Flee button's tooltip). Get farther first.")
    rpc = lambda op, a: core.game.rpc(op, a, context="client")  # noqa: E731
    inp = world_tools._inp()
    if inp is None:
        return "FAILED: no mouse input to answer the flee question (the input helper is not running)"
    with plumbing._InputHeld():
        if not uitools._hud_has(char):   # the HUD's Flee is the selected character's
            core.game.run_job("act.select", {"char": char}, wait_s=5)
        how = _flee_press(rpc, inp)
        if not how.startswith("pressed"):
            return f"FAILED: {how}"
        ans = boxclick.click_box(rpc, inp, "yes")
    if not ans.get("clicked"):
        return f"FAILED: {how}, but the flee question wasn't answered: {boxclick.describe(ans)}"
    end = time.monotonic() + FLEE_LEFT_S
    while True:
        cs = core.game.try_rpc("state.combat") or {}
        if not cs.get("active") or not _in_fight(cs, char):
            break
        if time.monotonic() >= end:
            return f"FAILED: {how} and answered Yes, but {name} is still in the fight"
        time.sleep(0.2)
    rest = [overview._name_of(o["uuid"]) for o in cs.get("order") or []
            if cs.get("active") and o.get("relation") == "party" and o.get("uuid") and not o.get("dead")]
    return reporting._finish("flee", {"who": name}, f"fled: {name} left the fight and is at camp" + (
        f"; still fighting: {', '.join(rest)}" if rest else "; the party is out of the fight"), t0)


def _in_fight(cs: dict, char: str) -> bool:
    return any(o.get("uuid") == char for o in cs.get("order") or [])


def _nearest_hostile(cs: dict, char: str) -> tuple[float, str, str] | None:
    """(metres, uuid, name) of the nearest living hostile `char` can see (state.combat lists only seen combatants)."""
    out = []
    for o in cs.get("order") or []:
        d = (o.get("from") or {}).get(char)
        if o.get("relation") == "hostile" and not o.get("dead") and o.get("uuid") and isinstance(d, (list, tuple)) \
                and d and isinstance(d[0], (int, float)):
            out.append((float(d[0]), o["uuid"], str(o.get("name") or "")))
    return min(out) if out else None


def _flee_press(rpc, inp) -> str:
    """Press the HUD's Flee until the game asks "Do you want to flee from combat?": its command when a page has one
    (as End Turn's), else a hover-checked click on the button (a command can do nothing, as Steal's did)."""
    no_ask = "the game didn't ask to flee (an enemy within 27 m, maybe one you can't see)"
    probe = rpc("ui.flee_probe", {}) or {}
    ran = False
    for c in probe.get("commands") or []:
        if not c.get("can"):
            continue
        ran = True
        rpc("ui.flee", {"name": c["name"], "widget": c.get("widget")})
        if _box_shows(rpc):
            return f"pressed Flee ({c['name']})"
    if not probe.get("button"):
        if ran:
            return no_ask
        if probe.get("commands"):
            return "the game won't flee now (Flee is greyed out: an enemy within 27 m, maybe one you can't see)"
        return "no Flee button on the HUD"

    def button() -> dict:
        b = (rpc("ui.flee_probe", {}) or {}).get("button")
        return {"open": bool(b), "over": bool(b and b.get("over")), "enabled": bool(b and b.get("enabled") is not False)}
    click = hoverclick.hover_click(button, inp, "flee", FLEE_SEED)
    if not click.get("clicked"):
        return f"couldn't click Flee ({click.get('error') or 'the button went away'})"
    if _box_shows(rpc):
        return "pressed Flee (clicked)"
    return no_ask


def _box_shows(rpc) -> bool:
    end = time.monotonic() + FLEE_BOX_S
    while True:
        v = rpc("ui.box_open", {})
        if isinstance(v, dict) and v.get("box"):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


@core.mcp.tool()
@reporting._guard
def end_combat_report(what_worked: str, what_to_change: str, next_goal: str) -> str:
    """Required after each fight: a short reflection that is appended to your session log."""
    t0 = time.monotonic()
    path = session._session_log()
    if path:
        with open(path, "a") as f:
            f.write(f"- {time.strftime('%H:%M')} COMBAT REPORT worked: {what_worked} | change: {what_to_change} | next: {next_goal}\n")
    return reporting._finish("end_combat_report", {}, f"Logged to {path.name if path else '(no session log)'}. Now loot and heal up.", t0)


_NOTE_TIME = re.compile(r"^(\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)\s+")
_NOTE_TAG = re.compile(r"^\[[A-Za-z][\w -]*\]")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    memory, uitools, queueing, turn_plan, turn_slots, turn_tool, session,
)
