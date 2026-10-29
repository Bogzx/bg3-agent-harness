# pyright: basic
"""The validator's consistency checks over a bundle's own files: the start state (X01), the run's time marks (X02),
the facts against the tool calls: loads, trades, kills (X03), the rolls (X04), tokens and cost (X05), resumed processes (X06), the outcome
against the verified cell (X07), the re-grade (G01, G02) and the fairness audit (A01, A02).

Each check takes the loaded bundle (validate.Loaded) and returns (ok, detail); validate.py runs them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bg3harness import runs

from bg3bench import conditions, facts, graders, luck, scenario

if TYPE_CHECKING:
    from bg3bench.submission.validate import Loaded

Json = dict[str, Any]
Outcome = tuple[bool | None, str]

WALL_SLACK_S = 5.0
LOAD_WINDOW_S = 60.0
TRADE_WINDOW_S = 60.0
TRADE_TOOLS = ("trade", "choose_dialogue")
KILL_WINDOW_S = 120.0
COST_TOLERANCE = 0.25


def _marks(timeline: list[Json], name: str) -> list[Json]:
    return [r for r in timeline if r.get("kind") == "mark" and r.get("mark") == name]


def settings_mark(timeline: list[Json]) -> Json:
    found = _marks(timeline, "settings")
    detail = found[0].get("detail") if found else None
    return detail if isinstance(detail, dict) else {}


def fingerprint(timeline: list[Json]) -> Json | None:
    """The start state a run's first facts snapshot shows: party (name, level), gold, area, difficulty and the
    avatar's name. None without a snapshot."""
    snap = next((r for r in timeline if r.get("kind") == "snapshot"), None)
    if snap is None:
        return None
    s = settings_mark(timeline)
    party = sorted([str(m.get("name")), int(m.get("level") or 0)] for m in snap.get("party") or [])
    return {
        "party": party,
        "gold": snap.get("gold"),
        "area": snap.get("area"),
        "difficulty": s.get("difficulty"),
        "name": s.get("avatar_name"),
    }


def x01_start_state(b: Loaded) -> Outcome:
    want = b.release_scenario.get("fingerprint")
    if not want:
        return False, "the suite publishes no start-state fingerprint for this scenario"
    got = fingerprint(b.facts)
    if got != want:
        diff = sorted(k for k in want if (got or {}).get(k) != want.get(k))
        return False, f"first snapshot differs from the published start state in: {', '.join(diff) or 'all'}"
    return True, ""


def x02_time_marks(b: Loaded) -> Outcome:
    start, end = _marks(b.facts, "run_start"), _marks(b.facts, "run_end")
    if len(start) != 1 or len(end) != 1:
        return False, f"{len(start)} run_start and {len(end)} run_end marks (want 1 each)"
    t0, t1 = start[0]["t"], end[0]["t"]
    wall = b.result.get("wall_s")
    if wall is None or abs((t1 - t0) - wall) > WALL_SLACK_S:
        return False, f"wall_s {wall} vs the marks' {t1 - t0:.1f} s"
    out = [e["t"] for e in b.all_events() if not t0 - WALL_SLACK_S <= e["t"] <= t1 + WALL_SLACK_S]
    if out:
        return False, f"{len(out)} events outside the run's marks"
    return True, ""


def _tool_calls(b: Loaded, name: str | None = None) -> list[Json]:
    return [e for e in b.all_events() if e.get("kind") == "tool_call" and (name is None or e.get("tool") == name)]


def _near(ts: Iterable[float], t: float, window: float) -> bool:
    return any(abs(x - t) <= window for x in ts)


def _events(timeline: list[Json], ev: str) -> list[Json]:
    return [r for r in timeline if r.get("kind") == "event" and r.get("ev") == ev]


def _after(ts: Iterable[float], t: float, window: float) -> bool:
    """Some call in ts came at most `window` seconds before t."""
    return any(0 <= t - x <= window for x in ts)


def x03_events_vs_facts(b: Loaded) -> Outcome:
    """The facts against the agent's tool calls. Loads: every load_game call has a load mark within 60 s and every
    load mark after the start a load_game call (or ended the run). Trades: every trade_open has a trade or
    choose_dialogue call at most 60 s before it (pilot: at most 16 s). Kills by the party: each comes at most 120 s
    after some tool call (pilot: 62 s; a combat_turn call plays the allies' turns too). A failed trade call opens
    no trade, so calls without a trade are fine."""
    calls = [e["t"] for e in _tool_calls(b, "load_game")]
    loads = [m["t"] for m in _marks(b.facts, "load")][1:]  # the first is the start save
    ends = [m["t"] for m in _marks(b.facts, "run_end")]
    orphan_calls = [t for t in calls if not _near(loads, t, LOAD_WINDOW_S)]
    orphan_loads = [t for t in loads if not _near(calls, t, LOAD_WINDOW_S) and not _near(ends, t, LOAD_WINDOW_S)]
    if orphan_calls or orphan_loads:
        return False, f"{len(orphan_calls)} load_game calls without a load, {len(orphan_loads)} loads without a call"
    opens = [e["t"] for e in _tool_calls(b) if e.get("tool") in TRADE_TOOLS]
    if trades := [r["t"] for r in _events(b.facts, "trade_open") if not _after(opens, r["t"], TRADE_WINDOW_S)]:
        return False, f"{len(trades)} trades opened without a trade or dialogue call before them"
    every = [e["t"] for e in _tool_calls(b)]
    kills = [r for r in _events(b.facts, "kill") if r.get("by_party") and not _after(every, r["t"], KILL_WINDOW_S)]
    if kills:
        return False, f"{len(kills)} kills by the party with no tool call in the {KILL_WINDOW_S:.0f} s before"
    return True, ""


def x04_rolls(b: Loaded) -> Outcome:
    if b.rolls is None:
        return (True, "") if b.result.get("luck") is None else (False, "result.luck without rolls.jsonl")
    got = luck.summary(b.rolls)
    if got != b.result.get("luck"):
        return False, f"luck recomputed {got} != result.luck {b.result.get('luck')}"
    return True, ""


def process_costs(b: Loaded) -> dict[str, float]:
    """Each play process's cost: its last usage event's cost_usd (clients report a running total per process)."""
    out: dict[str, float] = {}
    for name, evs in b.events.items():
        costs = [e["cost_usd"] for e in evs if e.get("kind") == "usage" and e.get("cost_usd") is not None]
        if costs:
            out[name] = float(costs[-1])
    return out


def x05_tokens_cost(b: Loaded) -> Outcome:
    keys = ("input", "output", "cache_read", "cache_write")
    tok = dict.fromkeys(keys, 0)
    for e in b.all_events():
        if e.get("kind") == "usage":
            for k in keys:
                tok[k] += int((e.get("tokens") or {}).get(k) or 0)
    if tok != {k: int((b.result.get("tokens") or {}).get(k) or 0) for k in keys}:
        return False, f"events' usage {tok} != result.tokens"
    claimed, costs = b.result.get("cost_usd"), process_costs(b)
    if claimed is None or not costs:
        return (claimed is None and not costs), "no cost reported" if claimed is None else "no usage costs"
    total = sum(costs.values())
    if abs(total - claimed) > COST_TOLERANCE * max(total, claimed):
        return False, f"cost_usd {claimed} vs {total:.6f} from the events"
    return True, ""


def x06_resumes(b: Loaded) -> Outcome:
    starts = [
        ((e.get("detail") or {}).get("session"), (e.get("detail") or {}).get("conversation"))
        for e in b.all_events()
        if e.get("kind") == "session_start"
    ]
    if len(starts) != len(b.events) or len(set(starts)) != len(starts):
        return False, f"{len(b.events)} events logs, {len(starts)} session starts, {len(set(starts))} distinct"
    claimed, costs = b.result.get("cost_usd"), process_costs(b)
    if claimed is not None and costs and claimed > sum(costs.values()) * 1.01 + 1e-6:
        return False, f"cost_usd {claimed} over the processes' sum {sum(costs.values()):.6f} (a process counted twice)"
    stop, wall = b.result.get("first_stop_s"), b.result.get("wall_s")
    if stop is not None and (wall is None or not 0 <= stop <= wall + WALL_SLACK_S):
        return False, f"first_stop_s {stop} outside the run (wall_s {wall})"
    return True, ""


def x07_outlier(b: Loaded, reference: Json | None) -> Outcome:
    """Flag a pass faster than half the verified minimum or a gain over twice the verified maximum."""
    if reference is None:
        return None, "no verified cell to compare with"
    t, best = b.result.get("t_pass_s"), reference.get("t_pass_min")
    if b.result.get("passed") and t is not None and best and t < best / 2:
        return False, f"t_pass_s {t} under half the verified minimum {best}"
    gain, top = (b.result.get("score") or {}).get("gain"), reference.get("gain_max")
    if gain is not None and top is not None and top > 0 and gain > 2 * top:
        return False, f"gain {gain} over twice the verified maximum {top}"
    return True, ""


def g01_regrade(b: Loaded) -> Outcome:
    sc = b.scenario
    res = b.result
    if (fact := scenario.maximised(sc)) is not None:
        m = graders.measure(fact, b.facts)
        want = {k: m[k] for k in ("fact", "start", "end", "gain", "peak")}
        got = res.get("score")
        if got != want or res.get("passed") is not None:
            return False, f"re-grade score {want} != result {got}"
        return True, ""
    g = graders.grade(scenario.goal_of(sc), b.facts)
    mine = (g["passed"], g["t_pass_s"] if g["passed"] else None, g["violations"])
    theirs = (res.get("passed"), res.get("t_pass_s"), (res.get("grade") or {}).get("violations"))
    if mine != theirs:
        return False, f"re-grade (passed, t_pass_s, violations) {mine} != result {theirs}"
    return True, ""


def g02_losses(b: Loaded) -> Outcome:
    losses = facts.party_losses(b.facts)
    want = {k: int(losses.get(k) or 0) for k in ("fights", "downs", "deaths", "wipes")}
    if b.result.get("deaths") != want:
        return False, f"deaths {b.result.get('deaths')} != the facts' {want}"
    reloads = max(0, len(_marks(b.facts, "load")) - 1)
    if b.result.get("reloads") != reloads:
        return False, f"reloads {b.result.get('reloads')} != the facts' {reloads} load marks after the start"
    return True, ""


def a01_fairness(b: Loaded, audit: Callable[[list[Path]], Json] = runs.fairness_flags) -> Outcome:
    streams = [b.dir / name.removesuffix(".events.jsonl") for name in sorted(b.events)]
    got = audit([s.with_name(s.name + ".jsonl") for s in streams])
    claimed = b.result.get("fairness") or {}
    if got["leak"]:
        return False, f"{got['leak']} leaks in the tool results"
    if any(got[k] != claimed.get(k) for k in ("leak", "check", "blocks")):
        return False, f"audit {[got[k] for k in ('leak', 'check', 'blocks')]} != result.fairness {claimed}"
    return True, ""


def a02_allowlist(b: Loaded) -> Outcome:
    allowed = set(b.release_condition.get("tools", {}).get("names") or [])
    if not allowed:
        return False, "the suite publishes no tool list for this condition"
    used = {e.get("tool") for e in _tool_calls(b)}
    extra = sorted(str(t) for t in used - allowed)
    if extra:
        return False, f"tools outside the allowlist: {', '.join(extra)}"
    cond = conditions.parse(b.manifest["condition"])
    if not cond.reloads and _tool_calls(b, "load_game"):
        return False, "load_game called without a reloads condition"
    return True, ""
