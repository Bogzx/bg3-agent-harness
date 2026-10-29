"""The evidence video's pure core: a run's recording index and its events logs -> what the video shows when.

- clips(): the recorder's segments (bg3harness.recorder index.json, epoch start/end) cut to the run's window; a
  recording that misses more than MAX_GAP_S anywhere in the window (a gap between segments, a late start, an early
  end) is refused, so the video's length is the run's wall time.
- panel_items(): the side panel's entries from every play process's events (Sxxx.events.jsonl): the agent's visible
  text, its tool calls and the first line of each result (no image file paths). Never its hidden reasoning:
  thinking events and every field but the visible ones are dropped, whatever a client's stream carries.
  A call shows as a one-line summary of its arguments (summary()); a result carries its call's id.
- changes(): when the side panel changes, coalesced to at most one change per MIN_CHANGE_S.
- strip_states(): the header and the party card, one state per second of run clock (the clock, the cost so far, the
  gold and the party's HP from facts.jsonl snapshots with their time, a member downed or revived since by the facts
  event's own time, the goal).
- lanes(): the timeline under the game over the whole run: fights, kills, downs, failed tool results, agent text.
- sync_table(): render.json (render.v1), which the run page and the validator read: {schema: 1, run_hash,
  video_start, video_end, fps, events: [{file, seq, at_s}]}, `file` the events log's name (S001.events.jsonl).

Times in the video are seconds from the run's start (`at_s`); the events carry epoch `t`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from ..clients import RUNNING_COST

MAX_GAP_S = 5.0  # the most recording a run may miss anywhere in its window
MIN_CHANGE_S = 0.5  # the side panel changes at most twice a second
FPS = 15  # the recorder's
RENDER_SCHEMA = 1  # render.json's version (the bench's render.v1 schema)
# Claude's tool results name the image file it saved ("[Image: source: /home/<user>/.claude/...jpg]"): a local path
IMAGE_SOURCE = re.compile(r"\s*\[Image: source: [^\]]*\]")
Json = dict[str, Any]


class RenderError(ValueError):
    """A run the renderer refuses (a recording with gaps, events past the video's end)."""


@dataclass(frozen=True)
class Clip:
    """One recording segment's part inside the run: the file and the offsets (s) into it."""

    file: str
    inpoint: float
    outpoint: float

    @property
    def duration(self) -> float:
        return self.outpoint - self.inpoint


@dataclass(frozen=True)
class Item:
    """One side-panel entry: kind "call" (a tool call: `tool` and the summary of its arguments), "result" (its
    result's first line), "error" (a result the tool marked as an error) or "text" (the agent's visible text). `ref`
    is the call's id on a call and on its result ("" when the client gives none)."""

    at_s: float
    file: str
    seq: int
    kind: str
    text: str
    tool: str = ""
    ref: str = ""


@dataclass(frozen=True)
class Member:
    name: str
    hp: int
    max_hp: int
    down: bool = False


@dataclass(frozen=True)
class Strip:
    """The header and the party card at one second of run clock."""

    clock_s: int
    cost_usd: float | None = None
    gold: int | None = None
    gold_start: int | None = None
    party: tuple[Member, ...] = ()
    passed_s: float | None = None  # the goal was met at this run second (shown once the clock reaches it)
    party_at_s: float | None = None  # the run second of the facts snapshot the party's HP comes from


@dataclass(frozen=True)
class Lanes:
    """The timeline's marks, in run seconds: fights (start, end), kills (at, the goal pass), downs, failed tool
    results and agent text."""

    fights: tuple[tuple[float, float], ...] = ()
    kills: tuple[tuple[float, bool], ...] = ()
    downs: tuple[float, ...] = ()
    fails: tuple[float, ...] = ()
    texts: tuple[float, ...] = ()


@dataclass(frozen=True)
class Meta:
    """What the strip and the id line say about the run (manifest.json, result.json, the run hash)."""

    run_id: str
    run_sha256: str  # the run's record hash: its record files, known before the render (unlike the bundle's)
    title: str = ""
    task: str = ""
    cap_s: float | None = None
    cost_kind: str = "billed"
    t_pass_s: float | None = None
    site: str = ""  # the leaderboard's host, shown on the id line
    pilot: bool = False  # a pilot run: the header carries a pilot badge
    ending: str = ""  # how a run that didn't pass ended (WIPED, COST CAP, TIME CAP), shown over its last seconds


def clips(segments: Sequence[Mapping[str, Any]], start: float, end: float, max_gap: float = MAX_GAP_S) -> list[Clip]:
    """The parts of the recorder's segments that cover [start, end] (epoch s), in order. RenderError when the
    recording misses more than max_gap seconds anywhere in the window; a segment still being written (no end yet)
    covers nothing."""
    if end <= start:
        raise RenderError(f"empty run window: start {start} end {end}")
    covered = start
    out: list[Clip] = []
    for s in sorted((s for s in segments if s.get("end") is not None), key=lambda s: float(s["start"])):
        s0, s1 = float(s["start"]), float(s["end"])
        if s1 <= covered or s0 >= end:
            continue
        if s0 - covered > max_gap:
            raise RenderError(f"the recording misses {s0 - covered:.1f} s at run second {covered - start:.1f}")
        a = max(s0, covered)
        b = min(s1, end)
        out.append(Clip(str(s["file"]), round(a - s0, 3), round(b - s0, 3)))
        covered = b
    if end - covered > max_gap:
        raise RenderError(f"the recording misses {end - covered:.1f} s at run second {covered - start:.1f}")
    return out


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _args(args: Mapping[str, Any]) -> str:
    """A tool call's arguments in one line: values as JSON, `k=v` joined (`attack e12` reads `action="attack"
    target="e12"`)."""
    return " ".join(f"{k}={json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in args.items())


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, int | float) and not isinstance(v, bool) else str(v)


def _move(m: Any) -> str:
    if isinstance(m, Mapping):
        m_ = cast(Mapping[str, Any], m)
        if m_.get("within") is not None:
            return f"move ≤ {_num(m_['within'])} m"
        return f"move → {m_.get('target')}"
    return f"move ≤ {_num(m)} m" if isinstance(m, int | float) else f"move → {m}"


def _abilities(step: Any) -> list[str]:
    out: list[str] = []
    steps: list[Any] = cast(list[Any], step) if isinstance(step, list) else [step]
    for a in steps:
        if not isinstance(a, Mapping):
            continue
        a_ = cast(Mapping[str, Any], a)
        targets: Any = a_.get("targets") or ([a_["target"]] if a_.get("target") else [])
        on = f" → {','.join(str(t) for t in cast(list[Any], targets))}" if isinstance(targets, list) and targets else ""
        out.append(f"{a_.get('ability')}{on}")
    return out


def _member_plan(who: Any, plan: Mapping[str, Any]) -> str:
    """`Vessa: Sneak Attack → e299 · move ≤ 10 m`: the member's actions, then its bonus action and its move."""
    parts = _abilities(plan.get("action")) + [f"bonus {b}" for b in _abilities(plan.get("bonus"))]
    tail = f" · {_move(plan['move'])}" if plan.get("move") is not None else ""
    return f"{str(who).split(' ')[0]}: {', '.join(parts) or '-'}{tail}"


def summary(tool: str, args: Mapping[str, Any]) -> str:
    """A tool call's arguments in one line: combat_turn's plan per member (`Vessa: Sneak Attack → e299 · move ≤ 10
    m`, members joined by `; `), walk_toward's objective and range, screenshot's scale; any other tool's arguments as
    `k=v` (_args)."""
    if tool == "combat_turn":
        plan: Any = args.get("plan")
        if isinstance(plan, Mapping) and "who" in plan:
            plan = [plan]
        elif isinstance(plan, Mapping):
            plan = [{"who": args.get("who", "?"), "plan": plan}]
        if isinstance(plan, list):
            members = [cast(Mapping[str, Any], m) for m in cast(list[Any], plan) if isinstance(m, Mapping)]
            return "; ".join(
                _member_plan(m.get("who", "?"), cast(Mapping[str, Any], m.get("plan") or {})) for m in members
            )
    if tool == "walk_toward" and args.get("objective") is not None:
        rng = f", ≤ {_num(args['max_m'])} m" if args.get("max_m") is not None else ""
        return f"→ objective {args['objective']}{rng}"
    if tool == "screenshot" and isinstance(args.get("scale"), int | float):
        return f"({_num(args['scale'])}\u00d7)"
    return _args(args)


def panel_items(logs: Mapping[str, Iterable[Mapping[str, Any]]], start: float) -> list[Item]:
    """The side panel's entries from {events log name: its events}, in time order (file and seq break ties).
    Only visible output: text, tool calls and results; thinking, usage, results and the harness's own events are
    left out."""
    out: list[Item] = []
    for name, events in logs.items():
        for e in events:
            kind, at = e.get("kind"), round(float(e["t"]) - start, 3)
            if e.get("src") != "client":
                continue
            seq = int(e["seq"])
            text = IMAGE_SOURCE.sub("", str(e.get("text") or "")).strip()
            if kind == "text" and text:
                out.append(Item(at, name, seq, "text", text))
            elif kind == "tool_call":
                args: Mapping[str, Any] = e.get("args") or {}
                tool = str(e.get("tool"))
                out.append(Item(at, name, seq, "call", summary(tool, args), tool, str(e.get("call_id") or "")))
            elif kind == "tool_result":
                kind_ = "error" if e.get("is_error") else "result"
                out.append(Item(at, name, seq, kind_, _first_line(text), ref=str(e.get("call_id") or "")))
    return sorted(out, key=lambda i: (i.at_s, i.file, i.seq))


def changes(items: Sequence[Item], min_gap: float = MIN_CHANGE_S) -> list[tuple[float, int]]:
    """(at_s, how many items the panel shows from then on), starting with the empty panel at 0. An item shows at
    its own time, or min_gap after the last change when that is later, together with every item that came by then:
    at most one change per min_gap, and no item shows before it happened."""
    out: list[tuple[float, int]] = []
    i = 0
    while i < len(items):
        at = max(items[i].at_s, 0.0)
        if out:
            at = max(at, out[-1][0] + min_gap)
        while i < len(items) and items[i].at_s <= at:
            i += 1
        out.append((round(at, 3), i))
    if not out or out[0][0] > 0:
        out.insert(0, (0.0, 0))
    return out


def _party(snapshot: Mapping[str, Any]) -> tuple[Member, ...]:
    party: list[Any] = snapshot.get("party") or []
    out: list[Member] = []
    for m in party:
        if isinstance(m, dict) and not cast(Mapping[str, Any], m).get("summon"):
            m_ = cast(Mapping[str, Any], m)
            down = bool(m_.get("dead") or m_.get("downed"))
            out.append(Member(str(m_.get("name") or "?"), int(m_.get("hp") or 0), int(m_.get("max_hp") or 0), down))
    return tuple(out)


def result_costs(logs: Mapping[str, Sequence[Mapping[str, Any]]], start: float) -> list[tuple[float, float]]:
    """(at_s, what it adds) for every play process's `result` cost, in time order. A RUNNING_COST client's (its
    session_start names it) cost_usd is its conversation's running total over --resume, so it adds what it raises
    the conversation's highest figure by (runs.own_cost); another client's adds itself."""
    found: list[tuple[float, str, float, str | None]] = []
    for events in logs.values():
        starts = [cast(Mapping[str, Any], e.get("detail") or {}) for e in events if e.get("kind") == "session_start"]
        client = str(starts[0].get("client")) if starts else ""
        for e in events:
            if e.get("kind") == "result" and e.get("cost_usd") is not None:
                sid = e.get("session_id") if client in RUNNING_COST else None
                found.append((float(e["t"]) - start, client, float(e["cost_usd"]), sid and str(sid)))
    running: dict[str, float] = {}
    out: list[tuple[float, float]] = []
    for at, _, cost, sid in sorted(found, key=lambda f: f[0]):
        if sid is None:
            out.append((at, cost))
            continue
        prev = running.get(sid, 0.0)
        running[sid] = max(prev, cost)
        out.append((at, running[sid] - prev))
    return out


def strip_states(
    duration_s: float,
    facts: Iterable[Mapping[str, Any]],
    logs: Mapping[str, Sequence[Mapping[str, Any]]],
    start: float,
    t_pass_s: float | None = None,
) -> list[Strip]:
    """One Strip per whole second of the run (0 .. ceil(duration) - 1): the gold and party of the last facts
    snapshot at or before that second (the first snapshot's gold is the start) and that snapshot's time, a member
    downed (at 0 HP) or revived after it by the facts event (downed / revived) at or before that second, the cost of
    the play processes that had ended by then (result_costs; None until one has)."""
    facts = list(facts)
    snaps = sorted(((float(f["t"]) - start, f) for f in facts if f.get("kind") == "snapshot"), key=lambda p: p[0])
    downs = sorted(
        (float(f["t"]) - start, str(f.get("name")), f.get("ev") == "downed")
        for f in facts
        if f.get("kind") == "event" and f.get("ev") in ("downed", "revived")
    )
    costs = result_costs(logs, start)
    gold_start = next((int(s["gold"]) for _, s in snaps if s.get("gold") is not None), None)
    out: list[Strip] = []
    si = ci = di = 0
    snap: Mapping[str, Any] | None = None
    snap_at: float | None = None
    cost: float | None = None
    downed: dict[str, tuple[float, bool]] = {}  # name: (when, down) from the facts events
    for sec in range(max(1, math.ceil(duration_s))):
        while si < len(snaps) and snaps[si][0] <= sec:
            snap_at, snap = snaps[si]
            si += 1
        while di < len(downs) and downs[di][0] <= sec:
            downed[downs[di][1]] = (downs[di][0], downs[di][2])
            di += 1
        while ci < len(costs) and costs[ci][0] <= sec:
            cost = (cost or 0.0) + costs[ci][1]
            ci += 1
        gold = snap.get("gold") if snap else None
        party = tuple(
            replace(m, down=downed[m.name][1], hp=0 if downed[m.name][1] else m.hp)
            if m.name in downed and downed[m.name][0] > (snap_at or -math.inf)
            else m
            for m in (_party(snap) if snap else ())
        )
        out.append(
            Strip(
                sec,
                None if cost is None else round(cost, 2),
                None if gold is None else int(gold),
                gold_start,
                party,
                t_pass_s,
                None if snap_at is None else round(snap_at, 3),
            )
        )
    return out


def lanes(
    facts: Iterable[Mapping[str, Any]],
    items: Sequence[Item],
    start: float,
    duration_s: float,
    t_pass_s: float | None = None,
) -> Lanes:
    """The timeline's marks inside [0, duration_s]: a fight from combat_started to its combat_ended (by the combat's
    id; from 0 when it started before the run, to the end when it hadn't ended), a kill per facts `kill` (the one
    nearest t_pass_s, within a second, is the goal pass), a down per facts `downed`, a fail per error result, a
    tick per agent text."""
    evs = sorted(((float(f["t"]) - start, f) for f in facts if f.get("kind") == "event"), key=lambda p: p[0])
    inside = [(at, f) for at, f in evs if 0 <= at <= duration_s]
    opened: dict[str, float] = {}
    fights: list[tuple[float, float]] = []
    for at, f in evs:
        cid = str(f.get("combat"))
        if f.get("ev") == "combat_started":
            opened.setdefault(cid, at)
        elif f.get("ev") == "combat_ended":
            a = opened.pop(cid, 0.0)
            if at >= 0 and a <= duration_s:
                fights.append((max(a, 0.0), min(at, duration_s)))
    fights += [(max(a, 0.0), duration_s) for a in opened.values() if a <= duration_s]
    kills = [at for at, f in inside if f.get("ev") == "kill"]
    goal = min(kills, key=lambda at: abs(at - t_pass_s), default=None) if t_pass_s is not None else None
    return Lanes(
        tuple(sorted((round(a, 3), round(b, 3)) for a, b in fights)),
        tuple((round(at, 3), goal is not None and at == goal and abs(at - (t_pass_s or 0)) <= 1) for at in kills),
        tuple(round(at, 3) for at, f in inside if f.get("ev") == "downed"),
        tuple(i.at_s for i in items if i.kind == "error"),
        tuple(i.at_s for i in items if i.kind == "text"),
    )


def sync_table(items: Sequence[Item], start: float, end: float, run_hash: str, fps: int = FPS) -> Json:
    """render.json (render.v1): the run it was rendered for (its run hash), the video's window (epoch s), its fps and
    every panel item's place in the video. RenderError when an item lies before the video's start (the run's
    run_start mark, its time zero) or past its end."""
    early = [i for i in items if i.at_s < 0]
    if early:
        raise RenderError(f"{len(early)} event(s) before the run's start, the first {early[0].file}#{early[0].seq}")
    late = [i for i in items if i.at_s > end - start + 1e-6]
    if late:
        raise RenderError(f"{len(late)} event(s) after the video's end, the first {late[0].file}#{late[0].seq}")
    return {
        "schema": RENDER_SCHEMA,
        "run_hash": run_hash,
        "video_start": start,
        "video_end": end,
        "fps": fps,
        "events": [{"file": i.file, "seq": i.seq, "at_s": i.at_s} for i in items],
    }
