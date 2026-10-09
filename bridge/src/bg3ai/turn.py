"""combat_turn: a whole combat turn in one call (plan parsing and the pure helpers; server.py runs it).

A plan names slots, each a list of alternatives tried in order until one succeeds:

    {"move":   {"target": "e12", "within": 1.5},        # or "e12", [x,y,z], {"x":..,"y":..,"z":..}
     "action": [{"ability": "Target_MainHandAttack", "target": "e12"},
                {"ability": "Projectile_MainHandAttack", "target": "e12"},  # fallbacks, in order
                {"ability": "Shout_Dash", "then_move": true},             # dash, then walk the move again
                "Shout_Disengage"],                                     # BG3 has no Dodge action
     "bonus":  {"ability": "Shout_SecondWind", "if": "hp_below:50"},
     "extra":  [[{"ability": "Target_MainHandAttack", "target": "e12"}]], # more slots (Extra Attack ...)
     "order":  "move,action,bonus,extra",                                   # default
     "reactions": {"Divine Smite": "ask"},                                  # set_reactions prep
     "on_reaction": "use" | "skip",                                         # answer prompts mid-turn
     "retarget": true,      # all alternatives missed (dead / out of range / no line of sight): same abilities
                            # on the nearest other hostile in range and in sight (false: off)
     "auto_bonus": true}    # no bonus slot given and an ally is downed: Healing Word them (false: off)

Every alternative is an ordinary tool step (use_ability by default; "tool" picks another batchable tool),
so the mod's guard checks each one exactly like a direct call. Before each step the bridge also checks
what the player sees: still this character's turn, the target still visible and alive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import takewhile as _takewhile
from typing import Any, Callable

from . import actq

SLOT_NAMES = ("move", "action", "bonus", "extra")
DEFAULT_ORDER = ("move", "action", "bonus", "extra")
MAX_ALTS = 6
MAX_EXTRA = 3

# Failures after which no fallback can help: the turn is over (or was never ours).
FATAL = ("not this character's turn", "session is over", "no active character", "is downed", "is dead",
         "not in combat", "already ended its turn")
# Failures of a move that a shorter walk can fix.
MELEE_M = 1.5  # a plain move to a target means "next to it"
MOVE_SHORT = ("beyond remaining movement", "no movement left", "not enough movement")


class PlanError(ValueError):
    pass


@dataclass
class Alt:
    step: actq.Step
    then_move: bool = False


@dataclass
class Move:
    target: str | None = None  # handle / name as the agent wrote it
    pos: list | None = None
    within: float | None = None  # stop once this close (straight line), e.g. weapon/spell range
    partial: bool = True  # if the full walk doesn't fit the movement left, walk as far as it does
    avoid: bool = True  # keep out of harmful surfaces on the way (false: walk through fire to get there)


@dataclass
class TurnPlan:
    move: Move | None = None
    slots: dict = field(default_factory=dict)  # "action"/"bonus" -> [Alt]; "extra" -> [[Alt]]
    order: tuple = DEFAULT_ORDER
    reactions: dict = field(default_factory=dict)
    on_reaction: str | None = None  # None = stop the turn on a reaction prompt
    retarget: bool = True  # a slot whose every alternative missed its target tries other hostiles in reach
    auto_bonus: bool = True  # unplanned bonus action: Healing Word on a downed ally in range


def _alt(v: Any, allowed: set, where: str) -> Alt:
    if isinstance(v, str):
        v = {"ability": v}
    if not isinstance(v, dict):
        raise PlanError(f"{where}: an alternative must be an ability id or an object")
    v = dict(v)
    then_move = bool(v.pop("then_move", False))
    v.setdefault("tool", "use_ability")
    if v["tool"] in ("end_turn", "do_actions", "combat_turn"):
        raise PlanError(f"{where}: '{v['tool']}' can't be a turn step (end_turn comes last by itself)")
    try:
        step = actq.parse_steps([v], allowed)[0]
    except actq.StepError as e:
        raise PlanError(f"{where}: {str(e).replace('step 1: ', '')}") from None
    if step.tool == "use_ability" and not step.args.get("ability"):
        raise PlanError(f"{where}: use_ability needs an ability")
    return Alt(step, then_move)


def _alts(v: Any, allowed: set, where: str) -> list[Alt]:
    if v in (None, "", []):
        return []
    items = v if isinstance(v, list) else [v]
    if len(items) > MAX_ALTS:
        raise PlanError(f"{where}: at most {MAX_ALTS} alternatives")
    return [_alt(x, allowed, f"{where} #{i}") for i, x in enumerate(items, 1)]


def _move(v: Any) -> Move | None:
    if v in (None, "", {}):
        return None
    if isinstance(v, str):
        return Move(target=v)
    if isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v):
        return Move(pos=[float(x) for x in v])
    if not isinstance(v, dict):
        raise PlanError('move: give "e12", [x,y,z], or {"target":"e12","within":1.5}')
    m = Move(partial=bool(v.get("partial", True)), avoid=bool(v.get("avoid", True)))
    if v.get("target"):
        m.target = str(v["target"])
    elif None not in (v.get("x"), v.get("y"), v.get("z")):
        m.pos = [float(v["x"]), float(v["y"]), float(v["z"])]
    elif isinstance(v.get("pos"), list) and len(v["pos"]) == 3:
        m.pos = [float(x) for x in v["pos"]]
    else:
        raise PlanError("move: needs a target or x,y,z")
    if v.get("within") is not None:
        m.within = max(0.0, float(v["within"]))
    return m


def parse_plan(raw: Any, allowed: set) -> TurnPlan:
    try:
        p = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise PlanError("plan must be JSON") from None
    if not isinstance(p, dict):
        raise PlanError('plan must be a JSON object like {"move":..., "action":[...], "bonus":...}')
    unknown = set(p) - set(SLOT_NAMES) - {"order", "reactions", "on_reaction", "retarget", "auto_bonus"}
    if unknown:
        raise PlanError(f"unknown plan key(s) {', '.join(sorted(unknown))}; use move, action, bonus, extra, "
                        "order, reactions, on_reaction, retarget, auto_bonus")
    plan = TurnPlan(move=_move(p.get("move")), retarget=p.get("retarget") is not False,
                    auto_bonus=p.get("auto_bonus") is not False)
    plan.slots["action"] = _alts(p.get("action"), allowed, "action")
    plan.slots["bonus"] = _alts(p.get("bonus"), allowed, "bonus")
    extra = p.get("extra") or []
    if not isinstance(extra, list):
        raise PlanError("extra: a list of slots, each a list of alternatives")
    if extra and not isinstance(extra[0], list):
        extra = [extra]  # one slot given flat
    if len(extra) > MAX_EXTRA:
        raise PlanError(f"extra: at most {MAX_EXTRA} slots")
    plan.slots["extra"] = [_alts(s, allowed, f"extra {i}") for i, s in enumerate(extra, 1)]
    if p.get("order"):
        order = [x.strip().lower() for x in (p["order"] if isinstance(p["order"], list) else str(p["order"]).split(","))
                 if str(x).strip()]
        bad = [x for x in order if x not in SLOT_NAMES]
        if bad or len(set(order)) != len(order):
            raise PlanError(f"order: a comma list of distinct {', '.join(SLOT_NAMES)}")
        plan.order = tuple(order)
    r = p.get("reactions") or {}
    if not isinstance(r, dict) or any(str(v).lower() not in ("never", "always", "ask") for v in r.values()):
        raise PlanError('reactions: {"<reaction name>": "never"|"always"|"ask"}')
    plan.reactions = {str(k): str(v).lower() for k, v in r.items()}
    o = p.get("on_reaction")
    if o not in (None, "", "stop"):
        o = str(o).lower()
        if o not in ("use", "skip"):
            raise PlanError('on_reaction: "use", "skip" or "stop" (default)')
        plan.on_reaction = o
    if not (plan.move or any(plan.slots.values())):
        raise PlanError("the plan does nothing: give move, action, bonus or extra")
    return plan


# Failures that another target could fix (the planned one died, moved out of range or out of sight).
TARGET_MISS = ("out of range", "line of sight", "already dead", "is dead", "not visible")
MELEE_REACH_PAD = 1.0  # weapon range is edge to edge: add both bounding radii (0.5 m each, medium creatures)
MAX_RETARGET = 3
BONUS_HEALS = ("Target_HealingWord",)  # bonus-action heals that bring a downed ally back up


def missed_target(line: str) -> bool:
    low = line.lower()
    return any(m in low for m in TARGET_MISS)


def hostiles_in_reach(combat: dict, who: str, rng: float | None, skip: set) -> list[str]:
    """Living hostile combatants `who` can see, nearest first, within reach of an ability with range `rng`
    (None: unknown, only melee reach). A melee range (≤ 3 m) reaches edge to edge."""
    reach = 1.5 + MELEE_REACH_PAD if rng is None else (rng + MELEE_REACH_PAD if rng <= 3 else rng)
    out = []
    for o in (combat or {}).get("order") or []:
        u = o.get("uuid")
        if not u or u in skip or o.get("dead") or o.get("relation") != "hostile":
            continue
        d = (o.get("from") or {}).get(who)
        if not (isinstance(d, (list, tuple)) and len(d) >= 2 and isinstance(d[0], (int, float))):
            continue
        if d[1] and d[0] <= reach:
            out.append((float(d[0]), u))
    return [u for _, u in sorted(out)]


def downed_allies(combat: dict, who: str, rng: float) -> list[str]:
    """Party members at 0 HP (downed, not dead) that `who` sees within `rng` m, nearest first."""
    out = []
    for o in (combat or {}).get("order") or []:
        u = o.get("uuid")
        if not u or u == who or o.get("dead") or o.get("relation") != "party" or o.get("hp_pct") != 0:
            continue
        d = (o.get("from") or {}).get(who)
        if isinstance(d, (list, tuple)) and len(d) >= 2 and isinstance(d[0], (int, float)) and d[1] and d[0] <= rng:
            out.append((float(d[0]), u))
    return [u for _, u in sorted(out)]


def is_fatal(text: str) -> bool:
    low = text.lower()
    return any(f in low for f in FATAL)


def move_too_long(text: str) -> bool:
    low = text.lower()
    return any(f in low for f in MOVE_SHORT)


def movement_left(text: str) -> float | None:
    """'destination is beyond remaining movement (path 14.2 m, 5.5 m left)' -> 5.5."""
    import re

    m = re.search(r"([\d.]+) m left", text)
    return float(m.group(1)) if m else None


def straight_dist(combat: dict, who: str, target: str) -> float | None:
    """Distance from `who` to a visible combatant, as the turn order's tactical data has it."""
    for o in (combat or {}).get("order") or []:
        if o.get("uuid") == target:
            d = (o.get("from") or {}).get(who)
            if isinstance(d, (list, tuple)) and d and isinstance(d[0], (int, float)):
                return float(d[0])
    return None


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
    return [c["current"]] if c.get("current") else []


def party_acting(combat: dict, party: set) -> list[str]:
    return [u for u in acting(combat) if u in party]


def next_up(combat: dict, party: set, label: Callable[[str], str], ended: str | None = None) -> tuple[str, str | None]:
    """('NEXT e14 Goblin, e15 Imp, then Lae'zel (yours)', uuid of the next party member to act or None).

    Reads the visible turn order only, starting at the current turn group (skipping `ended`, the character
    whose turn just ended, and the members of its group who have ended too, if the order hasn't moved on
    yet) and wrapping round."""
    order = [o for o in (combat or {}).get("order") or [] if not o.get("dead") and o.get("uuid")]
    if not order:
        return "", None
    cur = set(acting(combat)) | {o["uuid"] for o in order if o.get("is_current")}
    idx = next((i for i, o in enumerate(order) if o.get("uuid") in cur), 0)
    seq = order[idx:] + order[:idx]
    done = {ended} | {o["uuid"] for o in order if o.get("is_current") and o.get("ended")}
    # Members who already ended act again next round: move them to the end of the cycle.
    lead = 0
    while lead < len(seq) and seq[lead].get("uuid") in done:
        lead += 1
    seq = seq[lead:] + seq[:lead]
    names: list[str] = []
    mine = None
    group: list[str] = []
    for i, o in enumerate(seq):
        if o["uuid"] in party:
            mine = o["uuid"]
            # Party members right after it with the same initiative share its turn (any order).
            ini = o.get("initiative")
            group = [mine] + ([x["uuid"] for x in _takewhile(
                lambda x: x["uuid"] in party and x.get("initiative") == ini, seq[i + 1:])] if ini is not None else [])
            break
        names.append(label(o["uuid"]))
    if len(names) > 6:
        names = names[:6] + [f"+{len(names) - 6} more"]
    tail = (" + ".join(label(u) for u in group) + (" (yours, shared turn)" if len(group) > 1 else " (yours)")
            if mine else "")
    if not names:
        return (f"NEXT {tail}: plan that turn now", mine) if mine else ("", None)
    return "NEXT " + ", ".join(names) + (f", then {tail}" if mine else ""), mine


class TurnWatcher(actq.Watcher):
    """The default interrupts, except that a reaction prompt can be answered by the plan's policy
    (on_reaction use/skip) instead of stopping the turn: like the game's own "always"/"never" setting,
    but chosen for this turn. answer(use) -> text runs on the watcher's own connection."""

    def __init__(self, game, policy: str | None, answer: Callable[[bool], str], **kw):
        super().__init__(game, **kw)
        self.policy = policy
        self.answer = answer
        self.answered: list[str] = []

    def check(self, tool, next_tool, ui=None):
        hit = super().check(tool, next_tool, ui)
        if hit and self.policy and hit[0] == "prompt" and hit[1].startswith("reaction"):
            try:
                self.answered.append(self.answer(self.policy == "use"))
            except Exception as e:  # noqa: BLE001 - an unanswerable prompt stops the turn after all
                self.answered.append(f"reaction: could not answer ({e})")
                return hit
            with self._lock:
                self._screen = "game"
            return None
        return hit
