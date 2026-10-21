# pyright: basic
"""Goal predicates: a small JSON language over the facts a run records (facts.jsonl v1).

A goal is one JSON node. Combinators: `{"all": [..]}`, `{"any": [..]}`, `{"not": node}`. Modes:

- `{"ever": node}`: latched; true from the first instant the node was true.
- `{"never": node}`: an invariant; true until the first instant the node is true, then false for good (a violation).
- `{"state": node}`: the node at this instant, not latched.

A bare leaf at the top of a goal (or under all/any/not with no mode above it) means `{"ever": leaf}`. Inside a mode,
leaves are tested at the instant, so `{"ever": {"all": [a, b]}}` needs a and b at the same time while
`{"all": [a, b]}` takes them in any order. A run passes at the first instant its goal is true (graders.py).

Leaves (`cmp` is `{"gte": n}`, `{"lte": n}` or both):

| leaf | true when |
|---|---|
| `{"gold": cmp}` | party gold |
| `{"item": {"name": s} or {"tpl": s}, optional "gte": n}` | the party holds at least n (default 1); name is case-insensitive |
| `{"party_level": cmp}` | the lowest level of the real party members (summons excluded); `"agg": "max"` for the highest |
| `{"companions": cmp}` | party members other than the avatar and summons |
| `{"combats_won": cmp}` | combats that ended without a wipe while they lasted (hostiles dead or fled) |
| `{"combats_started": cmp}` | combats entered |
| `{"combat": true}` | a combat is on |
| `{"party_dead": cmp}` | real party members dead now |
| `{"wipe": true}` | every real party member is dead or downed |
| `{"area": re}`, `{"region": re}`, `{"quest_done": re}` | a visited area / region, a finished quest matches the regex |
| `{"elapsed_s": cmp}` | seconds since run start (game or wall clock, graders.py) |
| `{"game_end": true}` | the end credits or the epilogue |

Shorthands: `{"milestone": id}` (today's milestone list, `MILESTONES`), `{"survive_s": T}` =
`{"all": [{"elapsed_s": {"gte": T}}, {"never": {"party_dead": {"gte": 1}}}]}`, `{"no_combat": true}` =
`{"never": {"combat": true}}`.

Decision D8 (docs/OVERHAUL-PLAN.md): a goal a do-nothing agent could pass is rejected unless it has a positive
conjunct. `parse` enforces it; `positive(node)` is the test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MODES = ("ever", "never", "state")
CMP_LEAVES = ("gold", "party_level", "companions", "combats_won", "combats_started", "party_dead", "elapsed_s")
REGEX_LEAVES = ("area", "region", "quest_done")
FLAG_LEAVES = ("combat", "wipe", "game_end")
LEAVES = CMP_LEAVES + REGEX_LEAVES + FLAG_LEAVES + ("item",)
SHORTHANDS = ("milestone", "survive_s", "no_combat")
# Leaves a do-nothing agent cannot make true. A gte on these needs progress; everything else can happen to an idle
# party (combat comes to it, time passes, people die).
POSITIVE_CMP = ("gold", "party_level", "companions", "combats_won")
POSITIVE_FLAGS = ("game_end",)

# Today's milestones (bridge/src/bg3ai/milestones.py MILESTONES): id -> (kind, pattern). Kept as data here because
# the benchmark does not import the bridge; tests/test_predicates.py checks the two lists agree when both import.
_MILESTONE_ROWS: list[tuple[str, str, Any]] = [
    ("nautiloid", "region", r"nautiloid"),
    ("first_companion", "companions", 2),
    ("nautiloid_escaped", "quest_done", r"escape the nautiloid"),
    ("crash_site", "region", r"wilderness"),
    ("beach", "area", r"beach|crash site"),
    ("first_camp", "area", r"\bcamp(site)?\b"),
    ("level_2", "level", 2),
    ("grove", "area", r"emerald grove(?! environs)|the hollow|druids'? chambers?"),
    ("blighted_village", "area", r"blighted village"),
    ("level_4", "level", 4),
    ("goblin_camp", "area", r"shattered sanctum|goblin camp"),
    ("underdark", "area", r"underdark"),
    ("grymforge", "area", r"grymforge"),
    ("creche", "region", r"rosymorn|creche"),
    ("act2", "region", r"shadow.?cursed"),
    ("last_light", "area", r"last light"),
    ("moonrise", "area", r"moonrise"),
    ("act3", "region", r"rivington|wyrm"),
    ("lower_city", "region", r"lower city"),
    ("wyrms_rock", "area", r"wyrm'?s rock"),
    ("gortash", "quest_done", r"gortash'?s netherstone"),
    ("iron_throne", "region", r"iron throne"),
    ("bhaal_temple", "area", r"bhaal temple|temple of bhaal"),
    ("orin", "quest_done", r"orin'?s netherstone"),
    ("cazador", "quest_done", r"investigate cazador'?s palace|call of the blood"),
    ("morphic_pool", "area", r"morphic pool"),
    ("upper_city", "region", r"upper city"),
    ("high_hall", "area", r"high hall"),
    ("netherbrain", "area", r"nether.?brain"),
    ("elder_brain", "quest_done", r"confront the elder brain"),
    ("game_end", "end", True),
]


def _milestone_goal(kind: str, pat: Any) -> dict:
    if kind == "companions":
        return {"companions": {"gte": pat - 1}}  # the old count includes the avatar
    if kind == "level":
        return {"party_level": {"gte": pat, "agg": "max"}}  # the old milestone takes the highest level
    if kind == "end":
        return {"game_end": True}
    return {kind: pat}


MILESTONES: dict[str, dict] = {mid: _milestone_goal(kind, pat) for mid, kind, pat in _MILESTONE_ROWS}


class GoalError(ValueError):
    """The goal JSON is malformed, or (D8) a do-nothing agent could pass it."""


@dataclass(frozen=True)
class Leaf:
    kind: str
    gte: float | None = None
    lte: float | None = None
    agg: str = "min"  # party_level only
    rx: re.Pattern | None = None  # regex leaves
    name: str | None = None  # item
    tpl: str | None = None  # item


@dataclass(frozen=True)
class All:
    children: tuple


@dataclass(frozen=True)
class Any_:
    children: tuple


@dataclass(frozen=True)
class Not:
    child: Any


@dataclass(frozen=True)
class Mode:
    mode: str
    child: Any


Node = Leaf | All | Any_ | Not | Mode


def _one_key(obj: Any, where: str) -> tuple[str, Any]:
    if not isinstance(obj, dict) or len(obj) != 1:
        raise GoalError(f"{where}: a node is an object with exactly one key, got {obj!r}")
    return next(iter(obj.items()))


def _number(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float) or v < 0:
        raise GoalError(f"{where}: expected a number >= 0, got {v!r}")
    return v


def _cmp(kind: str, arg: Any, where: str) -> Leaf:
    allowed = {"gte", "lte"} | ({"agg"} if kind == "party_level" else set())
    if not isinstance(arg, dict) or not arg or not set(arg) <= allowed or not set(arg) & {"gte", "lte"}:
        raise GoalError(f"{where}: {kind} takes {{'gte': n}} and/or {{'lte': n}}, got {arg!r}")
    agg = arg.get("agg", "min")
    if agg not in ("min", "max"):
        raise GoalError(f"{where}: agg is 'min' or 'max', got {agg!r}")
    gte = _number(arg["gte"], where) if "gte" in arg else None
    lte = _number(arg["lte"], where) if "lte" in arg else None
    return Leaf(kind, gte=gte, lte=lte, agg=agg)


def _leaf(kind: str, arg: Any, where: str) -> Leaf:
    if kind in CMP_LEAVES:
        return _cmp(kind, arg, where)
    if kind in REGEX_LEAVES:
        if not isinstance(arg, str) or not arg:
            raise GoalError(f"{where}: {kind} takes a regex string, got {arg!r}")
        try:
            return Leaf(kind, rx=re.compile(arg, re.IGNORECASE))
        except re.error as e:
            raise GoalError(f"{where}: bad regex {arg!r}: {e}") from e
    if kind in FLAG_LEAVES:
        if arg is not True:
            raise GoalError(f"{where}: {kind} takes true (use not for false), got {arg!r}")
        return Leaf(kind)
    # item
    if not isinstance(arg, dict) or not set(arg) <= {"name", "tpl", "gte"} or ("name" in arg) == ("tpl" in arg):
        raise GoalError(f"{where}: item takes {{'name': s}} or {{'tpl': s}}, optional 'gte', got {arg!r}")
    key = arg.get("name") or arg.get("tpl")
    if not isinstance(key, str) or not key:
        raise GoalError(f"{where}: item name/tpl must be a non-empty string")
    gte = _number(arg.get("gte", 1), where)
    return Leaf("item", gte=gte, name=arg.get("name"), tpl=arg.get("tpl"))


def _shorthand(key: str, arg: Any, where: str) -> dict:
    if key == "milestone":
        if arg not in MILESTONES:
            raise GoalError(f"{where}: unknown milestone {arg!r}")
        return MILESTONES[arg]
    if key == "survive_s":
        t = _number(arg, where)
        return {"all": [{"state": {"elapsed_s": {"gte": t}}}, {"never": {"party_dead": {"gte": 1}}}]}
    if arg is not True:  # no_combat
        raise GoalError(f"{where}: no_combat takes true, got {arg!r}")
    return {"never": {"combat": True}}


def _build(obj: Any, instant: bool, where: str) -> Node:
    key, arg = _one_key(obj, where)
    here = f"{where}.{key}"
    if key in ("all", "any"):
        if not isinstance(arg, list) or not arg:
            raise GoalError(f"{here}: takes a non-empty list")
        kids = tuple(_build(c, instant, f"{here}[{i}]") for i, c in enumerate(arg))
        return All(kids) if key == "all" else Any_(kids)
    if key == "not":
        return Not(_build(arg, instant, here))
    if key in MODES:
        return Mode(key, _build(arg, True, here))
    if key in SHORTHANDS:
        return _build(_shorthand(key, arg, here), instant, here)
    if key in LEAVES:
        leaf = _leaf(key, arg, here)
        return leaf if instant else Mode("ever", leaf)
    raise GoalError(f"{here}: unknown node {key!r}")


def positive(node: Node) -> bool:
    """True when the node can only be true after the agent made progress (D8)."""
    match node:
        case Leaf(kind="item"):
            return node.gte is not None and node.gte >= 1
        case Leaf(kind=k) if k in POSITIVE_CMP:
            return node.gte is not None and node.gte > 0
        case Leaf(kind=k) if k in REGEX_LEAVES or k in POSITIVE_FLAGS:
            return True
        case Leaf():
            return False
        case All(children=kids):
            return any(positive(c) for c in kids)
        case Any_(children=kids):
            return all(positive(c) for c in kids)
        case Not():
            return False
        case Mode(mode="never"):
            return False
        case Mode(child=c):
            return positive(c)
    raise TypeError(f"not a goal node: {node!r}")


def parse(goal: Any, allow_passive: bool = False) -> Node:
    """Goal JSON -> node tree. Raises GoalError when malformed or, unless allow_passive, when D8 rejects it."""
    node = _build(goal, False, "goal")
    if not allow_passive and not positive(node):
        raise GoalError(
            "goal: a do-nothing agent could pass it (D8); add a positive conjunct such as gold, item, "
            "party_level, companions, combats_won, area, region, quest_done or a milestone"
        )
    return node
