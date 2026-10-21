"""References and action plumbing: objective refs, uuid/handle resolution, job text, turn-based mode, exclusive input
and async jobs.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from .. import economy, macros, party, render, turn
from ..game import GameError, JobResult
from . import core, reporting


def _objective_ref(ref: str) -> str:
    """o1/o2 (an objective marker) as a target: the name it is labelled with (S041 22:24 talk_to(npc="o1") for the
    Balthazar marker failed "unknown target 'o1'"). Not an objective handle: ref unchanged."""
    m = re.fullmatch(r"o(\d+)", str(ref or "").strip().lower())
    if not m:
        return ref
    ms = overview._objective_markers(max_age=600)
    try:  # by the persistent id the agent was shown (server._set_objectives)
        hit = render.find_objective(ms, f"o{int(m.group(1))}")
    except ValueError:
        raise ValueError(f"unknown target '{ref}': no objective {ref} now (get_map lists them)") from None
    if hit.get("target"):  # the item/character the marker sits on (vis V32); the mod still checks it is in view
        return str(hit["target"])
    label = render.objective_label(hit)
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
    inv = core.game.try_rpc("state.inventory", {}) or {}
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
            lines.append(f"TELEPORT: {core.H.get(best['uuid'], best.get('name'))} {best.get('name')} ({render.num(rng(best))}m)"
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
        return core.H.resolve(ref)
    except ValueError as e:
        # Live 12:3x: pickpocket(who="Shadowheart") / level_up(who="Wyll") as a fresh bridge's first calls failed
        # "unknown target": names are learnt from reads. A party/camp name is worth one status read.
        if "unknown target" not in str(e) or re.fullmatch(r"e\d+", str(ref or "").strip().lower()):
            raise
        _remember_names(core.game.try_rpc("state.status"))
        try:
            return core.H.resolve(ref)
        except ValueError as e2:
            # S029 16:04: talk_to(npc="Wulbren") as the session's first call: he stood in view, unnamed to a fresh
            # bridge. A character in sight is worth one nearby read (sight-checked by the mod, so fair).
            # S037 20:15: insert_item(into="Riddle of the Night") right after a travel: the pedestal stood in view,
            # unnamed until a get_nearby. Items in sight are learnt by the same read.
            if "unknown target" not in str(e2):
                raise
            _remember_names(core.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters", "items"]}))
            try:
                return core.H.resolve(ref)
            except ValueError as e3:
                # S043 23:15 talk_to("Jaheira"), an ally fighting with the party: the fight's turn order names her
                if "unknown target" not in str(e3):
                    raise
                try:
                    cs = core.game.try_rpc("state.combat") or {}
                except Exception:
                    cs = {}
                if not cs.get("active"):
                    return _objective_named(ref, e3)
                _remember_names(cs)
                try:
                    return core.H.resolve(ref)
                except ValueError as e4:
                    if "unknown target" not in str(e4):
                        raise
                    return _objective_named(ref, e4)


def _objective_named(ref: str, err: ValueError) -> str:
    """An objective's label as a target: the thing its marker sits on (S094 09:54 interact("Netherbrain Stem"), the
    o1 label, for the Brain Stem: "unknown target"). The label exactly (any case); otherwise err stands."""
    low = str(ref or "").strip().lower()
    for m in overview._objective_markers(max_age=600):
        if isinstance(m, dict) and m.get("target") and render.objective_label(m).strip().lower() == low:
            return str(m["target"])
    raise err from None


def _active() -> str:
    return _active_st()[0]


def _active_st() -> tuple[str, dict]:
    """The selected character (a living one, see below) and the state.status read it came from."""
    st = core.game.rpc("state.status")
    _remember_names(st)
    a = st.get("active")
    if not a:
        raise GameError("no active character")
    # S057 07:48:52/07:49:14: with `who` left out, use_ability/move_to acted as the selected Shadowheart, dead →
    # "FAILED — character is dead" twice. A player clicks a living portrait: the first living, on-stage member.
    rows = [r for r in st.get("party") or [] if isinstance(r, dict) and r.get("uuid")]
    me = next((r for r in rows if r["uuid"] == a), None)
    if me and me.get("dead"):
        alive = [r["uuid"] for r in rows if not r.get("dead") and not r.get("off_stage") and not r.get("summon")
                 and not r.get("downed")]
        if alive:
            return alive[0], st
    return a, st


def _who_uuid(ref: str) -> str:
    """A `who` reference: always one of ours. S035 19:32/19:38: a hostile copy named 'Karlach'/'Shadowheart' (Shadow-
    Cursed Lands) made who="Karlach" ambiguous; the party member is the only valid `who`, so take it."""
    try:
        return _uuid(ref)
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        party = turn_plan._party_set()
        mine = [core.H.by_handle[h] for h in core.H.matches(ref) if core.H.by_handle.get(h) in party]
        if len(mine) == 1:
            return mine[0]
        # S034 19:17:15: two summons named Spiritual Weapon (e328 from an earlier cast, e612 now) stopped a chain
        # "is ambiguous". In a fight, the one in the turn order, alive, and acting now if several are, is meant.
        cs = core.game.try_rpc("state.combat") or {}
        cands = set(mine) or {core.H.by_handle.get(h) for h in core.H.matches(ref)}
        rows = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("uuid") in cands
                and not o.get("dead") and o.get("relation") != "hostile"]
        now = set(turn.acting(cs)) if cs.get("active") else set()
        pick = [o["uuid"] for o in rows if o["uuid"] in now] or [o["uuid"] for o in rows]
        if len(pick) == 1 or (pick and len({o["uuid"] for o in rows if o["uuid"] in now}) == 1):
            return pick[0]
        raise


# ACT-P2 (naut2 run 1 19:59:10/16): use_ability Shout_Dash and walk_toward with no `who` acted as the selected Lae'zel,
# whose turn had ended: "FAILED — it is not this character's turn" twice, then the same calls again with who (the
# fight's party_can_act was [e1] alone). In a fight, a left-out who = the one party member acting now when the selected
# one can't act; the result says so. BG3AI_COMBAT_WHO_ACTING=0 reverts (the selected character, as before).
COMBAT_WHO_ACTING = os.environ.get("BG3AI_COMBAT_WHO_ACTING", "1") != "0"
_who_tl = threading.local()  # .note: said in front of the running tool's result (_guard)


def _who(ref: str | None) -> str:
    if ref:
        return _who_uuid(ref)
    char, st = _active_st()
    if COMBAT_WHO_ACTING and st.get("in_combat"):
        cs = core.game.try_rpc("state.combat") or {}
        party = {r.get("uuid") for r in st.get("party") or [] if isinstance(r, dict)}
        mine = turn.party_acting(cs, party) if cs.get("active") else []
        if char not in mine and len(mine) == 1:
            _who_tl.note = f"(who: {queueing._label(mine[0])}, whose turn it is; the selected {queueing._label(char)} can't act now)"
            return mine[0]
    return char


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
    inv = core.game.try_rpc("state.inventory", {"expand": True}) or {}
    _remember_names(inv)
    carried = {it["uuid"]: it.get("owner") for it in inv.get("items") or []
               if isinstance(it, dict) and isinstance(it.get("uuid"), str)}
    if u is not None and u not in carried:
        return u

    def world() -> list[str]:
        return [core.H.by_handle[h] for h in core.H.matches(r) if core.H.by_handle.get(h) not in carried]

    w = world()
    if not w:
        _remember_names(core.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters", "items"]}))
        w = world()
    if len(w) == 1:
        return w[0]
    if not w:
        held = ", ".join(f"{core.H.get(c)} by {overview._name_of(carried[c])}" if carried[c] else core.H.get(c)
                         for c in (core.H.by_handle[h] for h in core.H.matches(r)) if c in carried)
        raise ValueError(f"no '{r}' in sight; the party carries it ({held}): it is in a bag, not in the world "
                         "(get_inventory; get_nearby/get_frontier list what lies around)")
    opts = ", ".join(f"{core.H.get(x)} ({overview._name_of(x)})" for x in w[:8])
    raise ValueError(f"'{r}' is ambiguous: {opts} (party-carried ones skipped)") from err


def _interact_uuid(ref: str) -> str:
    """interact's target: a carried item can be used too (a book), so only an ambiguous name skips the carried."""
    try:
        return _uuid(ref)
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        return _world_uuid(ref)


def _nearest_uuid(ref: str, char: str) -> str:
    """disarm/lockpick/loot_corpse by name: two identical traps, chests or bodies made the name "is ambiguous", a
    wasted round trip (audit ACT-7). The one in sight nearest the actor is meant, as a player clicks the one in
    front of them; a tie (within NEAREST_TIE_M) keeps the refusal."""
    try:
        return _uuid(ref)
    except ValueError as e:
        if "is ambiguous" not in str(e):
            raise
        err = e
    cands = {core.H.by_handle.get(h) for h in core.H.matches(ref)}
    near = core.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters", "items"]}) or {}
    me = next((r.get("pos") for r in (core.game.try_rpc("state.status") or {}).get("party") or []
               if isinstance(r, dict) and r.get("uuid") == char), None)
    rows = []
    for r in (near.get("characters") or []) + (near.get("items") or []):
        if isinstance(r, dict) and r.get("uuid") in cands:
            d = macros.dist(me, r.get("pos")) if me else float("inf")
            d = d if d < float("inf") else r.get("dist")
            if isinstance(d, (int, float)):
                rows.append((d, r["uuid"]))
    rows.sort()
    if rows and (len(rows) == 1 or rows[1][0] - rows[0][0] > NEAREST_TIE_M):
        return rows[0][1]
    raise err


NEAREST_TIE_M = 0.5


def _remember_names(obj: Any) -> None:
    """Register every uuid/name pair in a payload so later references by name work."""
    if isinstance(obj, dict):
        u = obj.get("uuid")
        if isinstance(u, str) and render.UUID_RE.search(u):
            core.H.get(u, obj.get("name"))
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
    if isinstance(r.result, dict) and r.result.get("by_game"):
        return _game_moved_text(name, r.result, char)
    if isinstance(r.result, dict) and r.result.get("by_trip") and r.status == "done":
        return party.trip_moved_text(name, r.result, char or _active(), queueing._label)
    if name == "trade" and isinstance(r.result, dict) and ("wares" in r.result or "offer" in r.result):
        head = f"trade: done" if r.status == "done" else f"trade: {r.status.upper()}" + (f" — {r.error}" if r.error else "")
        return head + "\n" + economy.trade_text(r.result, progression._item_handle, **progression._trade_view)
    if r.status == "done":
        s = f"{name}: done"
        if r.result not in (None, {}, []):
            s += " — " + render.compact(r.result, core.H)
    elif r.status == "still_running":
        s = f"{name}: still running (use wait to keep watching)"
    else:
        s = f"{name}: {r.status.upper()}"
        if r.error:
            s += f" — {r.error}"
        if r.result not in (None, {}, []):
            s += " — " + render.compact(r.result, core.H)
    return s


def _game_moved_text(name: str, res: dict, char: str | None) -> str:
    """A walk the game's story cut short by moving the walker (Actions.lua tele_finish by_game): where it is, who
    stayed behind and whether they can walk there (one nav.path from the nearest; S066 Orin's arena, S063 the
    ritual drop)."""
    who = char or _active()
    st = core.game.try_rpc("state.status") or {}
    behind = party.moved_left_behind(st, who) if isinstance(st, dict) else []
    sealed = None
    pos = res.get("pos")
    if behind and isinstance(pos, list) and len(pos) >= 3:
        near = min(behind, key=lambda r: r["dist"] if r["dist"] is not None else float("inf"))
        if not near["other_level"]:
            p = core.game.try_rpc("nav.path", {"char": near["uuid"], "pos": pos[:3]})
            sealed = near["uuid"] if isinstance(p, dict) and p.get("reachable") is False else None
    area = st.get("subregion") if isinstance(st, dict) else None
    return party.game_moved_text(name, res, who, queueing._label, area, behind, sealed,
                                 in_combat=bool(isinstance(st, dict) and st.get("in_combat")))


# Turn-based mode outside combat (S021 burning inn, S024-S026 Shadow-Cursed Lands: the game forces it; every walk
# then failed "no path / 0 movement" and the agent hand-clicked the HUD's Exit TB button 2-10x per session).
# Ops that walk: before them, leave turn-based mode the way a player does (the HUD button, for the whole party).
_WALK_OPS = {"act.move_to", "act.interact", "act.pickup", "act.talk", "act.loot_corpse", "act.pickpocket"}
TB_REFUSAL = "turn-based mode outside combat"  # Actions.lua A.TB_REFUSAL: the walk op's refusal
TB_EXIT_WAIT_S = 2.0
_tb_wanted = {"on": False}  # the agent turned it on itself (set_party turn_based_on): walks keep it


def _tb_mode() -> dict:
    m = core.game.try_rpc("act.mode")
    return m if isinstance(m, dict) and "ftb" in m else {}


def _set_turn_based(on: bool) -> tuple[bool, str]:
    """Enter/leave turn-based mode outside combat for the whole party: press the HUD's Enter/Exit TB button
    (HotBar EnterFTBButton / ExitFTBButton, what the player clicks), else Osiris ForceTurnBasedMode per member."""
    btn = "EnterFTBButton" if on else "ExitFTBButton"
    how = []
    pressed = core.game.try_rpc("ui.press", {"widget": "HotBar", "button": btn}, context="client")
    if isinstance(pressed, dict) and pressed.get("done"):
        how.append(f"pressed the HUD's {'Enter' if on else 'Exit'} TB button")
        end = time.monotonic() + TB_EXIT_WAIT_S
        while time.monotonic() < end:
            if bool(_tb_mode().get("ftb")) == on:
                return True, how[0]
            time.sleep(0.15)
        how[0] += f" (no effect in {TB_EXIT_WAIT_S:g}s)"
    r = core.game.run_job("act.turn_based", {"on": on}, wait_s=6)
    if r.status == "done":
        how.append("ForceTurnBasedMode for the party")
        return True, ", then ".join(how)
    if r.status == "failed" and ("not in turn-based" in str(r.error) or "already in turn-based" in str(r.error)):
        return True, ", then ".join(how) if how else "nothing to do"
    left = (r.result or {}).get("still_turn_based") if isinstance(r.result, dict) else None
    how.append(f"ForceTurnBasedMode {r.status}: {r.error or ''}".rstrip(": ")
               + (f" (still turn-based: {', '.join(core.H.get(u) for u in left)})" if left else ""))
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
        with open(core.RUNTIME / "stage_ms.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "op": op, "status": r.status,
                                "elapsed_ms": r.elapsed_ms, "stages": ms}) + "\n")
    except OSError:
        pass


# Review M7: a background end's HUD fallback (queue thread: act.select A, then the HUD's End Turn, which ends whoever is
# selected) raced with input from the MCP thread (click_on, press_key, a select, the hotbar clicks for B): the press
# could land on B. Both sides take this lock around their selection/click input; the HUD phase holds it ~0.5 s.
_INPUT_LOCK = threading.RLock()
INPUT_LOCK_WAIT_S = 10.0  # never wait longer than this for the other side (then go on, as before)
_INPUT_OPS = {"act.select"}  # plus every client ui.* op


class _InputHeld:
    """with _InputHeld(): selection/click input that must not interleave with another thread's (M7)."""

    def __enter__(self):
        self.got = _INPUT_LOCK.acquire(timeout=INPUT_LOCK_WAIT_S)
        return self

    def __exit__(self, *exc):
        if self.got:
            _INPUT_LOCK.release()
        return False


def _input_exclusive(fn):
    """A tool/helper whose whole run selects or clicks (click_on, the hotbar routes): held against the HUD end."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        turn_tool._hold_for_bg_end()  # first (unlocked): the end may need the lock for its HUD fallback
        with _InputHeld():
            return fn(*a, **kw)
    return wrapper


def _act(tool: str, op: str, args: dict, wait_s: float = 25.0, context: str = "server",
         wait: bool = True, note: str = "", post=None, on_start=None, redo=None) -> str:
    if op in _INPUT_OPS or (context == "client" and op.startswith("ui.")):
        if args.get("char"):  # its own background end first, unlocked (_act_run's hold then finds it done)
            held = turn_tool._hold_for_bg_end(args["char"])
            note = "\n".join(x for x in (held, note) if x)
        with _InputHeld():
            return _act_run(tool, op, args, wait_s, context, wait, note, post, on_start, redo)
    return _act_run(tool, op, args, wait_s, context, wait, note, post, on_start, redo)


def _act_run(tool: str, op: str, args: dict, wait_s: float = 25.0, context: str = "server",
             wait: bool = True, note: str = "", post=None, on_start=None, redo=None) -> str:
    t0 = time.monotonic()
    if core._session_over():
        return reporting._finish(tool, args, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    if op in _WALK_OPS and context == "server" and _tb_wanted["on"]:
        args = dict(args, tb_ok=True)
    if op == "act.move_to" and context == "server" and reporting.OFFLIMITS.areas and not args.get("trespass"):
        args = dict(args, avoid=reporting.OFFLIMITS.avoid())  # the mod keeps the walk out of them (Actions.lua, vis V26)
    if not reporting._batch["active"]:
        held = batch._hold_for_early_walks(args.get("char"))
        note = "\n".join(x for x in (held, note) if x)
    if args.get("char"):
        held = turn_tool._hold_for_bg_end(args["char"])  # its turn is still being ended in the background
        note = "\n".join(x for x in (held, note) if x)
    if not wait and not reporting._batch["active"] and context == "server":
        return _start_async(tool, op, args, t0, note)
    ex = reporting._batch["exec"]
    if reporting._batch["active"]:
        wait_s = max(wait_s, 120.0)  # a batch/queue step runs to completion (interrupts cancel it)
    starts = [f for f in (ex.track if ex is not None else None, on_start) if f is not None]
    kw = {"on_start": (lambda ctx, jid: [f(ctx, jid) for f in starts])} if starts else {}
    r = core.game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if redo is not None:
        # redo(r) -> (args, note) or None: run the op once more another way (use_ability: a hotbar cast that failed
        # before anything was cast goes again as an Osiris cast); the caller's on_start is not run again
        again = redo(r)
        if again is not None:
            args, why = again
            note = "\n".join(x for x in (why, note) if x)
            kw = {"on_start": ex.track} if ex is not None else {}
            r = core.game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if op in _WALK_OPS and _tb_refused(r.status, r.error):
        tb = _leave_turn_based_for_walk()
        note = "\n".join(x for x in (tb, note) if x)
        r = core.game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if context == "server" and r.status == "failed" and macro_tools._IN_CONVO in str(r.error or "") and _close_left_trade():
        # S037 20:25:57: walk_toward right after trade(confirm) → "character is in a conversation"; the trade window
        # was still up (get_dialogue + ESCAPE, 2 more calls). Walking off closes a trade for a player: close, rerun.
        note = "\n".join(x for x in ("(closed the trade window first)", note) if x)
        r = core.game.run_job(op, args, context=context, wait_s=wait_s, **kw)
    if context == "server" and r.status == "failed" and macro_tools._convo_refused(r.error) and not reporting._batch["active"]:
        # S055 06:44:08-14: pickpocket ×3 "character is in a conversation" right after choose_dialogue ended it (the
        # flag a moment stale). With no dialogue on screen, retry for up to CONVO_WAIT_S, as the macros do.
        # Also camp's "cannot do that during a conversation" (S053 05:48:09, S057 07:40:25 ×2: a trader's goodbye line).
        # Live window 2026-10-16 18:40 (trade → ESCAPE → camp): the goodbye line ("Take care now.") is a dialogue
        # screen with no answers, so this retry never ran (0.1 s FAILED). Only answers on screen stop it.
        end = time.monotonic() + macro_tools.CONVO_WAIT_S
        while time.monotonic() < end and not macro_tools._dialogue_needs_answer():
            time.sleep(0.4)
            r = core.game.run_job(op, args, context=context, wait_s=wait_s, **kw)
            if not (r.status == "failed" and macro_tools._convo_refused(r.error)):
                break
    _log_stage_ms(op, r)
    if context == "server" and r.status == "still_running":
        r, followed = _follow_on(tool, r, args)
        note = "\n".join(x for x in (followed, note) if x)
    _remember_names(r.result)
    reporting._batch["last"] = r.status
    reporting._batch["result"] = r.result
    if reporting._batch["active"] and isinstance(r.result, dict) and "after" in r.result:
        # In a batch the per-step portrait summary is noise; the run ends with one NOW line + CHANGES.
        r = JobResult(r.status, {k: v for k, v in r.result.items() if k != "after"}, r.error, r.events, r.elapsed_ms)
    if post is not None:  # a line from after the job (use_ability: did the opener start a fight?)
        note = "\n".join(x for x in (post(r), note) if x)
    return reporting._finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)


def _close_left_trade(wait_s: float = 2.0) -> bool:
    """A trade screen left open blocks every action as "in a conversation": close it with ESCAPE (the trade
    screen's own close) and wait for it to go. True = it was open and is closed now."""
    if uitools._ui_screen()[0] != "trade":
        return False
    core.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        time.sleep(0.2)
        if uitools._ui_screen()[0] not in ("trade", "dialogue"):
            return True
    return False


# Async jobs started with wait=false: handle (j12) -> what the agent needs to follow them up.
_async: dict[str, dict] = {}


def _follow_on(tool: str, r: JobResult, args: dict) -> tuple[JobResult, str]:
    """A waited-on job still running at the end of its wait: it was neither followed nor announced (no id, no
    job_finished), so the agent re-sent it and superseded a ladder climb about to finish (audit ACT-10). From now on
    it is an async job: its end arrives as job_finished (job.notify) and wait_for('j12') follows it. A job that ended
    meanwhile comes back as its final view."""
    jid = r.result.get("job") if isinstance(r.result, dict) else None
    if jid is None:
        return r, ""
    view = core.game.try_rpc("job.notify", {"id": jid})
    if isinstance(view, dict) and view.get("status") not in (None, "running"):
        return JobResult(view["status"], view.get("result"), view.get("error"),
                         elapsed_ms=view.get("elapsed_ms", 0)), ""
    h = f"j{jid}"
    _async[h] = {"ctx": "server", "id": jid, "tool": tool, "t0": time.monotonic(), "eta": None,
                 "char": args.get("char")}
    core.METRICS.bg_start(h, None)
    return r, (f"it goes on as {h}: its end shows as job_finished in a later result; wait_for('{h}') waits for it "
               f"(sending {tool} again would supersede it)")


def _start_async(tool: str, op: str, args: dict, t0: float, note: str = "") -> str:
    """Start a mod job and return at once: "move_to: started j12, ETA 3.4s (14m)". The mod announces the
    end with a job_finished event (notify), so any later tool result shows it; wait_for('j12') blocks."""
    started = core.game.rpc(op, dict(args, notify=True))
    if op in _WALK_OPS and isinstance(started, dict) and _tb_refused(started.get("status"), started.get("error")):
        tb = _leave_turn_based_for_walk()
        note = "\n".join(x for x in (tb, note) if x)
        started = core.game.rpc(op, dict(args, notify=True))
    if not isinstance(started, dict) or "job" not in started:
        return reporting._finish(tool, args, _job_text(tool, JobResult("done", result=started)), t0)
    if started.get("status") not in (None, "running"):
        r = JobResult(started["status"], started.get("result"), started.get("error"))
        _remember_names(r.result)
        reporting._batch["last"] = r.status
        return reporting._finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)
    h = f"j{started['job']}"
    _async[h] = {"ctx": "server", "id": started["job"], "tool": tool, "t0": time.monotonic(),
                 "eta": started.get("eta_s"), "char": args.get("char")}
    core.METRICS.bg_start(h, started.get("eta_s"))
    # Settle window: a refusal, a blocked path or a short action that ends within SETTLE_S comes back as
    # its real result now (no wait_for round trip); anything longer keeps running while the agent plans.
    view = _settle(lambda: core.game.try_rpc("job.poll", {"id": started["job"]}))
    if view is not None:
        _async.pop(h, None)
        core.METRICS.bg_end(h)
        r = JobResult(view["status"], view.get("result"), view.get("error"), elapsed_ms=view.get("elapsed_ms", 0))
        _remember_names(r.result)
        reporting._batch["last"] = r.status
        return reporting._finish(tool, args, _job_text(tool, r, args.get("char")) + ("\n" + note if note else ""), t0)
    body = f"{tool}: started {h}"
    if started.get("eta_s") is not None:
        body += f", ETA {render.num(float(started['eta_s']))}s"
    if started.get("route_m") is not None:
        body += f" ({render.num(float(started['route_m']))}m)"
    if note:
        body += "\n" + note
    if tool in _WALKS:
        hint = reporting._next_walk_hint()
        if hint:
            body += "\n" + hint
    reporting._batch["last"] = "done"
    return reporting._finish(tool, args, body, t0, issued=True)


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


PACE_GUARD_NOTE = ("(pace guard: wait=true ignored outside combat: the walk runs while you plan. Do something that "
                   "doesn't depend on the arrival (notes, look, the next leg), then wait_for(\"queue\" | job id) only "
                   "if you still need the outcome. Don't pass wait=true to walks.)")


def _walk_wait(wait: bool) -> tuple[bool, str]:
    """The pace guard (move_to, walk_toward; travel_to never blocks): wait=true on a walk blocks only in combat
    (moving before an attack). Outside it the walk runs while the agent plans: S001 learned wait=true from its own
    results and pipelined 0% (impl-setup part 5). Until 2026-10-13 a client PreToolUse hook (pace_guard.py) also
    rewrote the call; the server's check is the one that holds for any client."""
    if not wait or reporting._batch["active"]:
        return wait, ""
    w = core.game.try_rpc("act.watch")
    if not isinstance(w, dict) or w.get("combat"):
        return True, ""  # in combat, or unknown: keep the old blocking behaviour
    return False, PACE_GUARD_NOTE


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    overview, progression, uitools, batch, queueing, turn_plan, turn_tool, macro_tools,
)
