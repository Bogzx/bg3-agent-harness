"""Grading a run: evaluate a goal (predicates.py) over its facts timeline (facts.py).

`grade(goal, timeline)` folds the records in order and evaluates the goal after each one, from the run's time zero
(the first run_start mark, else the first record) to the run_end mark (else the last record). The run passes at the
first instant the goal is true; `t_pass_s` is that instant in seconds since time zero.

Clock (decision D7): `clock="game"` (default) measures time by the records' `game_t` (game seconds since run_start,
think-pause excluded); every record in the run needs one, else FactsError (carrying the last snapshot's time forward
would move gains between snapshots earlier). `clock="wall"` uses `t` minus time zero.

Result: `{passed, t_pass_s, violations, curve, progress}`.
- `violations`: every `never` that fired before the verdict, `{"t_s", "node"}`; a fired `never` stays false.
- `curve`: a numeric fact at fixed run times (default party gold at 10/20/30 min): `value` at that time and the
  `peak` up to it, both null when the run ended earlier.
- `progress`: with `progress={"fact": f, "target": n}`, min(1, peak / n) over the whole run, so failed runs still
  order against each other; else null.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bg3bench.facts import FactsError, State, check
from bg3bench.predicates import All, Any_, Leaf, Mode, Node, Not, parse

CURVE_AT_S = (600, 1200, 1800)
NUMERIC_FACTS = ("gold", "party_level", "companions")


def fact(state: State, name: str) -> float:
    """A numeric fact of the current state (curves and progress)."""
    if name == "gold":
        return state.gold
    if name == "party_level":
        return state.party_level()
    if name == "companions":
        return state.companions()
    raise ValueError(f"unknown numeric fact {name!r}; one of {NUMERIC_FACTS}")


def _cmp(value: float, leaf: Leaf) -> bool:
    return (leaf.gte is None or value >= leaf.gte) and (leaf.lte is None or value <= leaf.lte)


def leaf_true(leaf: Leaf, state: State, elapsed: float) -> bool:
    k = leaf.kind
    if k == "gold":
        return _cmp(state.gold, leaf)
    if k == "party_level":
        return bool(state.real_party()) and _cmp(state.party_level(leaf.agg), leaf)
    if k == "companions":
        return _cmp(state.companions(), leaf)
    if k == "combats_won":
        return _cmp(state.combats_won, leaf)
    if k == "combats_started":
        return _cmp(state.combats_started, leaf)
    if k == "party_dead":
        return _cmp(state.party_dead(), leaf)
    if k == "elapsed_s":
        return _cmp(elapsed, leaf)
    if k == "item":
        return state.item_count(leaf.name, leaf.tpl) >= (leaf.gte or 0)
    if k == "combat":
        return state.combat
    if k == "wipe":
        return state.wiped()
    if k == "game_end":
        return state.game_end
    seen = {"area": state.areas, "region": state.regions, "quest_done": state.quests_done}[k]
    return any(leaf.rx.search(x) for x in seen)


class _Eval:
    """One goal's latches. step() must visit every node every time so each latch sees every instant."""

    def __init__(self, root: Node):
        self.root = root
        self.latched: dict[int, bool] = {}  # id(Mode node) -> ever fired / never violated
        self.violations: list[dict] = []

    def step(self, node: Node, state: State, elapsed: float) -> bool:
        match node:
            case Leaf():
                return leaf_true(node, state, elapsed)
            case All(children=kids):
                return all([self.step(c, state, elapsed) for c in kids])  # noqa: C419 (no short-circuit)
            case Any_(children=kids):
                return any([self.step(c, state, elapsed) for c in kids])  # noqa: C419 (no short-circuit)
            case Not(child=c):
                return not self.step(c, state, elapsed)
            case Mode(mode="state", child=c):
                return self.step(c, state, elapsed)
            case Mode(mode=mode, child=c):
                now = self.step(c, state, elapsed)
                fired = self.latched.get(id(node), False)
                if now and not fired:
                    self.latched[id(node)] = fired = True
                    if mode == "never":
                        self.violations.append({"t_s": elapsed, "node": _describe(c)})
                return fired if mode == "ever" else not fired
        raise TypeError(f"not a goal node: {node!r}")


def _describe(node: Node) -> Any:
    """A JSON-able rendering of a parsed node, for violation reports."""
    match node:
        case Leaf(kind=k):
            if node.rx is not None:
                return {k: node.rx.pattern}
            if k == "item":
                return {k: {"name" if node.name else "tpl": node.name or node.tpl, "gte": node.gte}}
            if node.gte is None and node.lte is None:
                return {k: True}
            return {k: {key: v for key, v in (("gte", node.gte), ("lte", node.lte)) if v is not None}}
        case All(children=kids):
            return {"all": [_describe(c) for c in kids]}
        case Any_(children=kids):
            return {"any": [_describe(c) for c in kids]}
        case Not(child=c):
            return {"not": _describe(c)}
        case Mode(mode=m, child=c):
            return {m: _describe(c)}
    raise TypeError(f"not a goal node: {node!r}")


def _window(records: list[dict]) -> tuple[int, int]:
    """Indices [start, end] of the run: from the first run_start mark to the first run_end after it."""
    start = next((i for i, r in enumerate(records) if r["kind"] == "mark" and r.get("mark") == "run_start"), 0)
    end = next(
        (i for i in range(start, len(records)) if records[i]["kind"] == "mark" and records[i].get("mark") == "run_end"),
        len(records) - 1,
    )
    return start, end


def grade(
    goal: Any,
    timeline: Iterable[dict],
    *,
    clock: str = "game",
    curve: dict | None = None,
    progress: dict | None = None,
    allow_passive: bool = False,
) -> dict:
    """Grade one run. `goal` is goal JSON (or an already parsed node); `timeline` the facts v1 records."""
    if clock not in ("game", "wall"):
        raise ValueError(f"clock is 'game' or 'wall', got {clock!r}")
    root = goal if isinstance(goal, Leaf | All | Any_ | Not | Mode) else parse(goal, allow_passive)
    records = check(timeline)
    if not records:
        raise FactsError("empty timeline")
    curve = curve or {"fact": "gold", "at_s": list(CURVE_AT_S)}
    fact(State(), curve["fact"])
    if progress is not None:
        fact(State(), progress["fact"])
        if not progress.get("target", 0) > 0:
            raise ValueError("progress target must be > 0")
    start, end = _window(records)
    t0 = records[start]["t"]
    state, ev = State(), _Eval(root)
    for r in records[:start]:
        state.apply(r)
    passed, t_pass = False, None
    samples: list[tuple[float, float]] = []  # (elapsed, curve fact) after each record
    peak_progress = None
    for r in records[start : end + 1]:
        state.apply(r)
        if clock == "wall":
            elapsed = r["t"] - t0
        else:
            if r.get("game_t") is not None:
                elapsed = r["game_t"]
            elif r is records[start]:
                elapsed = 0.0  # the run_start mark is game time zero even when it carries no game_t
            else:
                raise FactsError(f"clock='game' but a record at t={r['t']} has no game_t; grade with clock='wall'")
        samples.append((elapsed, fact(state, curve["fact"])))
        if progress is not None:
            v = fact(state, progress["fact"])
            peak_progress = v if peak_progress is None else max(peak_progress, v)
        if not passed and ev.step(root, state, elapsed):
            passed, t_pass = True, elapsed
    return {
        "passed": passed,
        "t_pass_s": t_pass,
        "violations": ev.violations,
        "curve": _curve(samples, curve),
        "progress": None if progress is None else min(1.0, peak_progress / progress["target"]),
    }


def _curve(samples: list[tuple[float, float]], spec: dict) -> dict:
    last = samples[-1][0]
    points = []
    for at in spec["at_s"]:
        upto = [v for e, v in samples if e <= at]
        if last < at or not upto:
            points.append({"at_s": at, "value": None, "peak": None})
        else:
            points.append({"at_s": at, "value": upto[-1], "peak": max(upto)})
    return {"fact": spec["fact"], "points": points}
