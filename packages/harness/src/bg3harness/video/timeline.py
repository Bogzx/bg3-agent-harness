"""The evidence video's pure core: a run's recording index and its events logs -> what the video shows when.

- clips(): the recorder's segments (bg3harness.recorder index.json, epoch start/end) cut to the run's window; a
  recording that misses more than MAX_GAP_S anywhere in the window (a gap between segments, a late start, an early
  end) is refused, so the video's length is the run's wall time. cuts(): the same with the recorder's events: a gap
  whose reason they give (the guard paused it: low RAM, heat, disk; ffmpeg crashed) is a Gap the video shows as a
  card for its real length, so the run clock stays true; refused are a gap without a logged reason and more than
  MAX_PAUSED_S or MAX_PAUSED_SHARE of the run paused in all.
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

MAX_GAP_S = 5.0  # the most recording a run may miss anywhere in its window without a logged reason
MAX_PAUSED_S = 60.0  # logged pauses: at most this long in all
MAX_PAUSED_SHARE = 0.05  # and at most this share of the run
REASON_SLACK_S = 12.0  # a stop is logged up to ~10 s after its segment's end (the recorder waits for ffmpeg to exit)
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
    wiped_s: float | None = None  # the party wiped at this run second (a WIPED ending shows from then)


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
    # (WIPED: from the wipe's own second, Strip.wiped_s)


@dataclass(frozen=True)
class Gap:
    """A logged pause in the recording: from run second at_s, `length` s, why (the recorder's events). The game
    clips after it start at video second cut_s of their concatenation; `offset` is how far they are moved to keep the
    run clock (the run time minus the concatenation's time from there on)."""

    at_s: float
    length: float
    reason: str
    cut_s: float
    offset: float

    @property
    def end_s(self) -> float:
        return round(self.at_s + self.length, 3)


def gap_reason(events: Sequence[Mapping[str, Any]], g0: float, g1: float) -> str | None:
    """Why the recording misses [g0, g1] (epoch s), from the recorder's events: a stop or an ffmpeg exit at its start
    (logged up to REASON_SLACK_S after the segment's end), else what the start that ends it waited for."""
    for e in events:
        t = float(e.get("t") or 0)
        if e.get("ev") == "stop" and g0 - 1.0 <= t <= min(g1, g0 + REASON_SLACK_S) and e.get("reason"):
            return str(e["reason"])
        if e.get("ev") == "exit" and g0 - 1.0 <= t <= min(g1, g0 + REASON_SLACK_S):
            return f"ffmpeg exited ({e.get('code')})"
    for e in events:
        t = float(e.get("t") or 0)
        if e.get("ev") == "start" and g0 <= t <= g1 + 1.0 and e.get("after") and e["after"] != "starting":
            return str(e["after"])
    return None


def cuts(
    segments: Sequence[Mapping[str, Any]],
    start: float,
    end: float,
    events: Sequence[Mapping[str, Any]] = (),
    max_gap: float = MAX_GAP_S,
) -> tuple[list[Clip], list[Gap]]:
    """The parts of the recorder's segments that cover [start, end] (epoch s), in order, and the logged pauses
    between them. A gap over max_gap is a pause when the recorder's events give its reason, else RenderError; so are
    pauses over MAX_PAUSED_S or MAX_PAUSED_SHARE of the run in all. A segment still being written (no end yet) covers
    nothing."""
    if end <= start:
        raise RenderError(f"empty run window: start {start} end {end}")
    covered, video = start, 0.0
    out: list[Clip] = []
    gaps: list[Gap] = []

    def gap(g0: float, g1: float) -> None:
        if g1 - g0 <= max_gap:
            return
        why = gap_reason(events, g0, g1)
        if why is None:
            raise RenderError(
                f"the recording misses {g1 - g0:.1f} s at run second {g0 - start:.1f} and its index logs no reason"
            )
        gaps.append(Gap(round(g0 - start, 3), round(g1 - g0, 3), why, round(video, 3), round(g1 - start - video, 3)))

    for s in sorted((s for s in segments if s.get("end") is not None), key=lambda s: float(s["start"])):
        s0, s1 = float(s["start"]), float(s["end"])
        if s1 <= covered or s0 >= end:
            continue
        gap(covered, s0)
        a = max(s0, covered)
        b = min(s1, end)
        out.append(Clip(str(s["file"]), round(a - s0, 3), round(b - s0, 3)))
        video += b - a
        covered = b
    gap(covered, end)
    paused = sum(g.length for g in gaps)
    if paused > MAX_PAUSED_S or paused > MAX_PAUSED_SHARE * (end - start):
        raise RenderError(
            f"the recording is paused {paused:.1f} s in all ({len(gaps)} gaps): over {MAX_PAUSED_S:.0f} s or "
            f"{MAX_PAUSED_SHARE:.0%} of the run's {end - start:.0f} s"
        )
    return out, gaps


def clips(segments: Sequence[Mapping[str, Any]], start: float, end: float, max_gap: float = MAX_GAP_S) -> list[Clip]:
    """cuts() without the recorder's events: any gap over max_gap is refused."""
    return cuts(segments, start, end, (), max_gap)[0]


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


def _tokens(e: Mapping[str, Any]) -> int:
    return sum(int(v or 0) for v in cast(Mapping[str, Any], e.get("tokens") or {}).values())


def running_costs(logs: Mapping[str, Sequence[Mapping[str, Any]]], start: float) -> list[tuple[float, float]]:
    """(at_s, the run's cost so far) after every usage and result event that tells it, in time order (runs.UsageMeter
    on the events logs): a play process costs what its usage events say while it runs (pi's carry the process's
    running cost; claude's carry tokens only, priced at the run's cost per token of the processes done so far, so
    nothing shows before the first result), replaced by its result's cost when that comes. A RUNNING_COST client's
    (its session_start names it) result cost_usd is its conversation's running total over --resume, so it adds
    what it raises the conversation's highest figure by (runs.own_cost). A process that ended without a result (a
    cap or the run's stop killed it) keeps its usage's figure, as runs.claude_totals counts it."""
    found: list[tuple[float, str, int, Mapping[str, Any]]] = []
    client: dict[str, str] = {}
    for name, events in logs.items():
        starts = [cast(Mapping[str, Any], e.get("detail") or {}) for e in events if e.get("kind") == "session_start"]
        client[name] = str(starts[0].get("client")) if starts else ""
        found += [
            (float(e["t"]) - start, name, int(e.get("seq") or 0), e)
            for e in events
            if e.get("kind") in ("usage", "result")
        ]
    done_cost, done_tokens = 0.0, 0
    known = False
    running: dict[str, float] = {}  # conversation id -> its highest reported total (RUNNING_COST clients)
    live: dict[str, tuple[float | None, dict[str, int]]] = {}  # an unfinished process: its cost, tokens per message
    out: list[tuple[float, float]] = []
    for at, name, _, e in sorted(found, key=lambda f: f[:3]):
        if e.get("kind") == "usage":
            cost, msgs = live.get(name, (None, {}))
            msgs = {**msgs, str(e.get("message_id") or f"#{len(msgs)}"): _tokens(e)}
            live[name] = (float(e["cost_usd"]) if e.get("cost_usd") is not None else cost, msgs)
        else:
            cost, msgs = live.pop(name, (None, {}))
            if e.get("cost_usd") is None:  # a result that prices nothing: its usage's figure stands
                live[name] = (cost, msgs)
                continue
            got = float(e["cost_usd"])
            sid = e.get("session_id") if client[name] in RUNNING_COST else None
            if sid:
                prev = running.get(str(sid), 0.0)
                running[str(sid)] = max(prev, got)
                got = running[str(sid)] - prev
            done_cost += got
            done_tokens += _tokens(e) or sum(msgs.values())
        rate = done_cost / done_tokens if done_tokens else None
        parts = [c if c is not None else sum(m.values()) * rate if rate is not None else None for c, m in live.values()]
        known = known or done_tokens > 0 or any(p is not None for p in parts) or e.get("kind") == "result"
        if known:
            out.append((at, done_cost + sum(p or 0.0 for p in parts)))
    return out


def wiped_at(facts: Iterable[Mapping[str, Any]], start: float) -> float | None:
    """The run second the party wiped: the facts' first game_over event (the game's Game Over screen), else the first
    time every party member (summons aside) was down or dead by the facts' snapshots and downed / died / revived
    events. None: no wipe."""
    down: dict[str, bool] = {}
    for f in sorted(facts, key=lambda f: float(f["t"])):
        at = float(f["t"]) - start
        if f.get("kind") == "event" and f.get("ev") == "game_over":
            return at
        if f.get("kind") == "snapshot":
            down = {m.name: m.down for m in _party(f)}
        elif f.get("kind") == "event" and f.get("party") and str(f.get("name")) in down:
            if f.get("ev") in ("downed", "died", "revived"):
                down[str(f.get("name"))] = f.get("ev") != "revived"
        else:
            continue
        if down and all(down.values()):
            return at
    return None


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
    the run so far (running_costs; None until something prices it) and the run second of a wipe (wiped_at)."""
    facts = list(facts)
    snaps = sorted(((float(f["t"]) - start, f) for f in facts if f.get("kind") == "snapshot"), key=lambda p: p[0])
    downs = sorted(
        (float(f["t"]) - start, str(f.get("name")), f.get("ev") == "downed")
        for f in facts
        if f.get("kind") == "event" and f.get("ev") in ("downed", "revived")
    )
    costs = running_costs(logs, start)
    wiped_s = wiped_at(facts, start)
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
            cost = costs[ci][1]
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
                None if wiped_s is None else round(wiped_s, 3),
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
