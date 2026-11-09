"""Turn-order helpers (pure): who may act now, downed and passing members, DISABLED lines, melee reach, walk
refusal texts. The combat tools (end_turn, its wait, the REACH line) read state.combat through them.
"""

from __future__ import annotations

import re
from typing import Any, Callable

MOVE_SHORT = ("beyond remaining movement", "beyond movement (", "no movement left", "not enough movement")


MELEE_REACH_PAD = 1.0  # weapon range is edge to edge: add both bounding radii (0.5 m each, medium creatures)


def reach_line(combat: dict, who: str, movement: Any, label: Callable[[str], str]) -> str:
    """'REACH e4 Claude (9m move): melee this turn e1018 7.6m, e1017 6.2m | with Dash e1019 12m | farther e1020 16m'.
    S023-S025: 17 moves 'beyond remaining movement' (164 s) planned melee on hostiles 12-20 m away. Straight-line
    distance (a path can be longer: '≈'), weapon reach edge to edge (1.5 m + pad). '' without movement data."""
    if not isinstance(movement, (int, float)):
        return ""
    reach = 1.5 + MELEE_REACH_PAD
    now, dash, far = [], [], []
    for o in (combat or {}).get("order") or []:
        u = o.get("uuid")
        if not u or o.get("dead") or o.get("relation") != "hostile":
            continue
        d = (o.get("from") or {}).get(who)
        if not (isinstance(d, (list, tuple)) and d and isinstance(d[0], (int, float))):
            continue
        dist = float(d[0])
        bucket = now if dist <= movement + reach else dash if dist <= 2 * movement + reach else far
        bucket.append((dist, f"{label(u)} {round(dist, 1):g}m"))
    if not (now or dash or far):
        return ""
    parts = []
    for name, b in (("melee this turn", now), ("with Dash", dash), ("farther (ranged only)", far)):
        if b:
            parts.append(name + " " + ", ".join(t for _, t in sorted(b)))
    return f"REACH {label(who)} ({round(float(movement), 1):g}m move, ≈straight line): " + " | ".join(parts)


def move_too_long(text: str) -> bool:
    low = text.lower()
    return any(f in low for f in MOVE_SHORT)


def movement_left(text: str) -> float | None:
    """'destination is beyond remaining movement (path 14.2 m, 5.5 m left)' -> 5.5."""

    m = re.search(r"([\d.]+) m left", text)
    return float(m.group(1)) if m else None


def acting(combat: dict) -> list[str]:
    """Combatants who may act right now, in turn-order order.

    Party members with equal initiative share one turn group: all of them are current together (the mod
    lists them in `turn_group`; each has `is_current`) and they can act in any order until each one ends
    its turn (`ended`: the game then hands control to the next member). `current` alone is just one of
    them, so never compare a character with `current` to decide whether it may act."""
    c = combat or {}
    group = c.get("turn_group")
    if isinstance(group, list) and group:
        return [u for u in group if isinstance(u, str)]
    out = [o["uuid"] for o in c.get("order") or []
           if o.get("is_current") and not o.get("ended") and not o.get("dead") and o.get("uuid")]
    if out:
        return out
    # S087 06:02:59: a group whose members all ended or pass (stunned) keeps one of them as `current` (the game is
    # about to move on): that one can't act
    cur = c.get("current")
    if cur and any(o.get("uuid") == cur and (o.get("ended") or o.get("passes")) for o in c.get("order") or []):
        return []
    return [cur] if cur else []


def passing(combat: dict) -> list[str]:
    """Party members whose open turn passes by itself: an INCAPACITATED status (Stunned, Paralysed …; state.combat
    `passes`, Guard.turn_passes). Not acting, never blocking another member (S087)."""
    return [o["uuid"] for o in (combat or {}).get("order") or []
            if o.get("uuid") and o.get("passes") and o.get("is_current") and o.get("relation") == "party"
            and not o.get("dead")]


def is_downed(o: dict) -> bool:
    """A turn-order row the game has downed: the mod's `downed` (the DOWNED status, or 0 HP), not dead. Never the
    rounded hp_pct: S076 00:29-00:32 Karlach at 1/218 HP read hp_pct 0 and was called downed, so end_turn and
    combat_turn refused her (~3.5 min of a Cazador round, played by hand)."""
    return bool(o.get("downed")) and not o.get("dead")


def downed(combat: dict) -> set[str]:
    """Party members downed and not dead: their turn is only the game's death saving throw (it passes by
    itself; end_turn is refused 'character is downed', S018)."""
    return {o["uuid"] for o in (combat or {}).get("order") or []
            if o.get("uuid") and o.get("relation") == "party" and is_downed(o)}


def disabled_lines(combat: dict, label: Callable[[str], str], only: str | None = None) -> list[str]:
    """'DISABLED e3 Gale: Sleeping (6 turns): cannot act; an adjacent ally's Help (an action) ends it; any damage
    ends it', one line per living party member with a disabling status (state.combat `disabled`, Guard.disabling),
    for every combat read. S076 00:39:38: Gale's +Sleeping showed once, in a CHANGES line; her spells were planned
    for rounds 4-6 ("Gale is awake now") and nobody woke her. only: that member's line alone."""
    out = []
    for o in (combat or {}).get("order") or []:
        u, dis = o.get("uuid"), o.get("disabled")
        if not u or not isinstance(dis, list) or not dis or o.get("dead") or (only and u != only):
            continue
        bits = []
        for d in dis:
            if not isinstance(d, dict):
                continue
            t = str(d.get("name") or "?")
            if isinstance(d.get("turns"), int):
                t += f" ({d['turns']} turn{'s' if d['turns'] != 1 else ''})"
            how = [str(d[k]) for k in ("effect", "fix") if d.get(k)]
            bits.append(t + (": " + "; ".join(how) if how else ""))
        if bits:
            out.append(f"DISABLED {label(u)}: " + " | ".join(bits))
    return out


def party_acting(combat: dict, party: set) -> list[str]:
    """Party members (summons too) who may act now; a downed one can't (its turn passes by itself)."""
    down = downed(combat)
    return [u for u in acting(combat) if u in party and u not in down]


def zero_hp_turns(combat: dict, party: set) -> list[str]:
    """Party members at 0 HP without the Downed status whose open turn the game keeps (state.combat `zero_hp_turn`).
    They read downed, so party_acting leaves them out, but no death saving throw passes this turn: until it is ended
    the game waits (windmill 01:39:19: 81 s of end_turn's wait, then 15 s of wait_for, on Gale's turn)."""
    rows = {o.get("uuid"): o for o in (combat or {}).get("order") or [] if isinstance(o, dict)}
    return [u for u in acting(combat) if u in party and rows.get(u, {}).get("zero_hp_turn")]


