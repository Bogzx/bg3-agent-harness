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
TRAVEL_PROGRESS_M = 1.0  # a leg that gets less than this much closer is a stall
TRAVEL_STALL_LEGS = 2    # stalls in a row before travel_to gives up (S001 retried in place while burning)
TRAVEL_LEG_RETRIES = 2   # failed legs per trip that get an alternative leg instead of ending it (S002)
STEP_INTO_RANGE_M = 3.0  # combat_turn: a cast this far past its range steps in first (S002: 18.6 m vs 18 m)
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
                skipped.append(f"{name} (already opened; opened=true loots it anyway)")
                continue
            out.append({"kind": "container", "uuid": it["uuid"], "name": name, "pos": it.get("pos"),
                        "dist": it.get("dist")})
    ordered = nearest_order(out, start)
    if len(ordered) > limit:
        skipped.append(f"{len(ordered) - limit} more beyond the first {limit}")
        ordered = ordered[:limit]
    return ordered, skipped


def dialogue_filler(d: dict | None, strict: bool = False) -> tuple[bool, int, str]:
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
    if strict and not FILLER.match(text):
        # The bridge's own autopilot: only a bare Continue/"..."/End; any spoken answer is the agent's to give.
        return False, 0, f"the only answer is yours to give: '{text[:60]}'"
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


class TravelProgress:
    """travel_to's stall detector: each leg must get the character TRAVEL_PROGRESS_M closer than the best so
    far, else it is a stall; TRAVEL_STALL_LEGS stalls in a row end the trip (S001: legs kept returning
    'stopped_short ... path blocked' at the same spot while the character burned)."""

    def __init__(self, start: Any, dest: Any) -> None:
        self.dest = dest
        self.best = dist(start, dest)
        self.stalls = 0

    def leg(self, here: Any, stopped_short: bool = False) -> str | None:
        """Record where a leg ended. Returns why to stop, or None to go on."""
        d = dist(here, self.dest)
        if d <= self.best - TRAVEL_PROGRESS_M:
            self.best = d
            self.stalls = 0
            return None
        self.stalls += 1
        if self.stalls >= TRAVEL_STALL_LEGS:
            why = "stopped short" if stopped_short else "made no progress"
            return f"{why} {self.stalls} legs in a row ({round(d, 1)}m left): the way is blocked here"
        return None


def hazard_way_out(h: dict | None) -> str:
    """The advice after 'go around it': a chasm or lava never burns out, so offer a jump or a ladder instead."""
    s = str((h or {}).get("surface") or "").lower() if isinstance(h, dict) else ""
    if "chasm" in s or "lava" in s or "void" in s:
        return "jump_to across it (if it is narrow) or take a ladder/door (get_frontier)"
    return "wait for it to burn out"


def hazard_text(h: dict | None) -> str:
    """{surface, pos, at_m} -> 'Fire 6m ahead @(-18,18,-424)'."""
    if not isinstance(h, dict) or not h.get("surface"):
        return ""
    out = str(h["surface"])
    if h.get("at_m") is not None:
        out += f" {round(float(h['at_m']))}m ahead"
    p = h.get("pos")
    if isinstance(p, (list, tuple)) and len(p) >= 3:
        out += " @(" + ",".join(f"{float(v):g}" for v in p[:3]) + ")"
    return out


def leg_stopped_short(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("stopped_short") or result.get("reached") is False)


# A walk that failed on the ground itself (not an interrupt, a conversation or the game state): another leg may work.
_LEG_PATH_FAIL = re.compile(r"no walkable path|could not get closer|path blocked|not reachable|stopped_short|"
                            r"needs a jump|teleport", re.I)
_LEG_NOT_PATH = re.compile(r"interrupted|conversation|in combat|dialogue|cancel|downed|dead|timed out", re.I)


def leg_retryable(stop_kind: str | None, stopped: str | None, text: str) -> bool:
    """travel_to: did this leg fail because of where it went (retry with another leg), rather than an
    interrupt or a state that stops any walk? stop_kind/stopped: the Report's; text: the leg's line."""
    if stop_kind not in (None, "failed") or (stopped and not str(stopped).endswith(" failed")):
        return False
    return bool(_LEG_PATH_FAIL.search(text or "")) and not _LEG_NOT_PATH.search(text or "")


_OUT_OF_RANGE = re.compile(r"out of range \(([\d.]+) m, range ([\d.]+) m\)")


def range_gap(text: str) -> tuple[float, float] | None:
    """'target is out of range (18.6 m, range 18.0 m)' -> (18.6, 18.0); None for other failures."""
    m = _OUT_OF_RANGE.search(text or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


# What walking through a harmful surface costs, roughly (BG3 rules, [wiki]): (what happens, worst damage per
# entry, worst lingering damage after it, worst damage per metre inside). travel_to quotes the text and checks
# the worst case against HP before a crossing.
SURFACE_COST: dict[str, tuple[str, float, float, float]] = {
    "Fire": ("~1-4 dmg + Burning", 4, 8, 0), "TrialFire": ("~1-4 dmg + Burning", 4, 8, 0),
    "Hellfire": ("~2-12 dmg + Burning", 12, 8, 0),
    "Acid": ("Acid: -2 AC", 0, 0, 0), "CausticBrine": ("~1-4 acid dmg + Acid", 4, 0, 0),
    "Poison": ("Poisoned", 0, 4, 0), "WyvernPoison": ("Poisoned", 0, 8, 0),
    "WaterElectrified": ("~2-8 lightning dmg", 8, 0, 0), "BloodElectrified": ("~2-8 lightning dmg", 8, 0, 0),
    "Grease": ("may fall Prone", 0, 0, 0), "WaterFrozen": ("may fall Prone", 0, 0, 0),
    "BloodFrozen": ("may fall Prone", 0, 0, 0), "Web": ("may get stuck (Enwebbed)", 0, 0, 0),
    "SpikeGrowth": ("2-8 dmg per 1.5m", 0, 0, 5.4), "BlackTentacles": ("Restrained + ~3-18 dmg", 18, 0, 0),
}
SURFACE_COST_DEFAULT = ("~1-10 dmg or a condition", 10, 0, 0)
NEVER_CROSS = {"Lava", "Chasm", "BladeBarrier"}
CROSS_MAX_M = 6.0          # longer crossings are never taken on their own
CROSS_HP_RESERVE_PCT = 25  # after the worst case, at least this much HP must be left
AVOID_POLICIES = ("auto", "strict", "cross")


def exposure_text(ex: dict | None) -> str:
    """{m, surface, patches} -> '1.5m of Fire (~1-4 dmg + Burning)'."""
    if not isinstance(ex, dict) or not ex.get("surface"):
        return ""
    s = str(ex["surface"])
    m = float(ex.get("m") or 0)
    out = f"{m:g}m of {s}"
    if (ex.get("patches") or 1) > 1:
        out += f" in {ex['patches']} patches"
    what = SURFACE_COST.get(s, SURFACE_COST_DEFAULT)[0] if s not in NEVER_CROSS else "deadly"
    return out + f" ({what})"


def crossing_worst(ex: dict) -> float:
    _, entry, linger, per_m = SURFACE_COST.get(str(ex.get("surface")), SURFACE_COST_DEFAULT)
    return entry * max(1, int(ex.get("patches") or 1)) + linger + per_m * float(ex.get("m") or 0)


def crossing_ok(ex: dict, hp: Any, max_hp: Any, policy: str = "auto") -> tuple[bool, str]:
    """May travel_to walk this crossing? policy: auto (unless HP is low), strict (never), cross (even at
    low HP, but never into lava, a chasm or a blade barrier, nor more than CROSS_MAX_M)."""
    s = str(ex.get("surface"))
    if policy == "strict":
        return False, "avoid_hazards=strict"
    if s in NEVER_CROSS:
        return False, f"{s} is deadly"
    if float(ex.get("m") or 0) > CROSS_MAX_M:
        return False, f"more than {CROSS_MAX_M:g}m of it"
    if policy == "cross":
        return True, ""
    worst = crossing_worst(ex)
    if isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)) and max_hp > 0 and worst > 0:
        if hp - worst < max(3.0, max_hp * CROSS_HP_RESERVE_PCT / 100.0):
            return False, (f"HP {hp:g}/{max_hp:g} is too low for the worst case (~{worst:g} dmg): heal first "
                           "(potion, restoration pod, short rest) or travel_to(avoid_hazards='cross')")
    return True, ""
