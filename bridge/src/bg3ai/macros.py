"""Macros: common multi-step chores as one tool call (pure helpers; server.py runs them).

loot_area, dialogue_advance, travel_to, regroup, read_all, rest_if_needed. Each macro:
* picks its targets only from what the player sees (state.nearby, the open dialogue, the party's own
  inventory and bars, the agent's own markers and the objective markers the map shows);
* runs every step through the ordinary guarded tools (so the mod's fairness guard checks each one);
* stops at the first interrupt (combat, a conversation, a reaction/roll/decision popup, a new hostile,
  an HP drop, a blocked teleport) and says what was done and what was left.
"""

from __future__ import annotations

import math
import re
from typing import Any

LOOT_RADIUS = 12.0
LOOT_MAX = 10
TRAVEL_LEG_M = 30.0
TRAVEL_ARRIVE_M = 3.0
TRAVEL_MAX_LEGS = 12
REGROUP_NEAR_M = 6.0
DIALOGUE_MAX = 12

# Option texts that only move a conversation along. A single untagged option is never a choice either.
FILLER = re.compile(r"^\W*(continue|\.\.\.|…|next|end|end conversation|\[continue\]|\[end\])\W*$", re.I)
# Single options that are still a commitment a player would pause over.
NOT_FILLER = re.compile(r"\b(attack|fight|kill|steal|give|pay|accept|refuse|leave|agree|take)\b", re.I)

# Inventory names that are reading matter (BG3 books, notes, letters, journals). Scrolls are spells.
READABLE = re.compile(r"\b(book|tome|note|notes|letter|journal|diary|page|pages|manuscript|codex|missive|"
                      r"memo|log|ledger|report|orders|poem|recipe|treatise|guide|chronicle|volume|"
                      r"pamphlet|flyer|notice|message|invitation)\b", re.I)
NOT_READABLE = re.compile(r"\bscroll\b|\bspellbook\b|\bkey\b|\bpotion\b", re.I)


def dist(a: Any, b: Any) -> float:
    try:
        return math.dist([float(x) for x in a][:3], [float(x) for x in b][:3])
    except (TypeError, ValueError):
        return float("inf")


def nearest_order(items: list[dict], start: Any) -> list[dict]:
    """Greedy nearest-neighbour tour from `start` (shorter walking than plain distance order)."""
    left = list(items)
    out = []
    here = start
    while left:
        nxt = min(left, key=lambda it: dist(here, it.get("pos")) if it.get("pos") else it.get("dist", 99.0))
        left.remove(nxt)
        out.append(nxt)
        if nxt.get("pos"):
            here = nxt["pos"]
    return out


def loot_targets(nearby: dict, start: Any, radius: float, done: set, corpses: bool = True,
                 containers: bool = True, owned: bool = False, opened: bool = False,
                 limit: int = LOOT_MAX) -> tuple[list[dict], list[str]]:
    """Visible lootables within radius, in walking order: [{kind: corpse|container, uuid, name, pos, dist}],
    plus notes on what was skipped and why (locked, owned = stealing, a discovered armed trap, already
    opened unless opened=True)."""
    out: list[dict] = []
    skipped: list[str] = []
    if corpses:
        for c in (nearby or {}).get("characters") or []:
            if not c.get("dead") or c.get("uuid") in done or (c.get("dist") or 0) > radius:
                continue
            if c.get("relation") not in (None, "hostile", "neutral") and not owned:
                skipped.append(f"{c.get('name')} (an ally's body)")
                continue
            out.append({"kind": "corpse", "uuid": c["uuid"], "name": c.get("name"), "pos": c.get("pos"),
                        "dist": c.get("dist")})
    if containers:
        for it in (nearby or {}).get("items") or []:
            if it.get("kind") != "container" or it.get("uuid") in done or (it.get("dist") or 0) > radius:
                continue
            name = it.get("name")
            if it.get("locked"):
                skipped.append(f"{name} (locked)")
                continue
            if it.get("owned") and not owned:
                skipped.append(f"{name} (owned: taking is stealing)")
                continue
            if (it.get("trap") or {}).get("armed"):
                skipped.append(f"{name} (trapped)")
                continue
            if it.get("opened") and not opened:
                skipped.append(f"{name} (already opened)")
                continue
            out.append({"kind": "container", "uuid": it["uuid"], "name": name, "pos": it.get("pos"),
                        "dist": it.get("dist")})
    ordered = nearest_order(out, start)
    if len(ordered) > limit:
        skipped.append(f"{len(ordered) - limit} more beyond the first {limit}")
        ordered = ordered[:limit]
    return ordered, skipped


def dialogue_filler(d: dict | None) -> tuple[bool, int, str]:
    """(advance?, option to pick (0 = continue), why not) for the dialogue on screen.
    Advances only when there is no real choice: a continue prompt, or one untagged, enabled, filler-looking
    option. Two or more options, a tagged option (skill check, class, [ATTACK]) or a committing single
    option is a decision for the agent."""
    if not d:
        return False, 0, "no dialogue on screen"
    if d.get("changing") or d.get("listening"):
        return False, 0, "line still playing"
    opts = [o for o in d.get("options") or [] if not o.get("off")]
    if not opts:
        return (True, 0, "") if d.get("continue") else (False, 0, "no options yet")
    if len(opts) > 1:
        return False, 0, f"a real choice ({len(opts)} options)"
    o = opts[0]
    text = str(o.get("text") or "").strip()
    if o.get("tags"):
        return False, 0, f"tagged option [{']['.join(o['tags'])}]"
    if NOT_FILLER.search(text) and not FILLER.match(text):
        return False, 0, f"the only option commits to something: '{text[:60]}'"
    return True, int(o.get("i") or 1), ""


def readable(item: dict) -> bool:
    """A carried book/note worth reading (by its visible name)."""
    name = str(item.get("name") or "")
    if item.get("equipped_slot") or item.get("container") or item.get("gold"):
        return False
    return bool(READABLE.search(name)) and not NOT_READABLE.search(name)


def hp_low(party: list[dict], threshold: float) -> list[tuple[str, int, int]]:
    """Party members (not downed handled separately) at or below threshold % HP: [(uuid, hp, max_hp)]."""
    out = []
    for r in party or []:
        hp, mx = r.get("hp"), r.get("max_hp")
        if r.get("downed") or (isinstance(hp, (int, float)) and isinstance(mx, (int, float)) and mx > 0
                               and 100.0 * hp / mx <= threshold):
            out.append((r.get("uuid"), hp, mx))
    return out
