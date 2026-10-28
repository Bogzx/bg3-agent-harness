"""The evidence video's pure core: a run's recording index and its events logs -> what the video shows when.

- clips(): the recorder's segments (bg3harness.recorder index.json, epoch start/end) cut to the run's window; a
  recording that misses more than MAX_GAP_S anywhere in the window (a gap between segments, a late start, an early
  end) is refused, so the video's length is the run's wall time.
- panel_items(): the side panel's entries from every play process's events (Sxxx.events.jsonl): the agent's visible
  text, its tool calls and the first line of each result (no image file paths). Never its hidden reasoning:
  thinking events and every field but the visible ones are dropped, whatever a client's stream carries.
- changes(): when the side panel changes, coalesced to at most one change per MIN_CHANGE_S.
- strip_states(): the strip under the game, one state per second of run clock (the clock, the cost so far, the
  gold and the party's HP from facts.jsonl snapshots, the goal).
- sync_table(): render.json, which the run page and the validator read: {video_start, video_end, fps, events:
  [{file, seq, at_s}]}.

Times in the video are seconds from the run's start (`at_s`); the events carry epoch `t`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..clients import RUNNING_COST

MAX_GAP_S = 5.0  # the most recording a run may miss anywhere in its window
MIN_CHANGE_S = 0.5  # the side panel changes at most twice a second
FPS = 15  # the recorder's
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
    """One side-panel entry: kind "call" (a tool call), "result" (its result's first line), "error" (a result the
    tool marked as an error) or "text" (the agent's visible text)."""

    at_s: float
    file: str
    seq: int
    kind: str
    text: str


@dataclass(frozen=True)
class Member:
    name: str
    hp: int
    max_hp: int
    down: bool = False


@dataclass(frozen=True)
class Strip:
    """The strip under the game at one second of run clock."""

    clock_s: int
    cost_usd: float | None = None
    gold: int | None = None
    gold_start: int | None = None
    party: tuple[Member, ...] = ()
    passed_s: float | None = None  # the goal was met at this run second (shown once the clock reaches it)


@dataclass(frozen=True)
class Meta:
    """What the strip and the id line say about the run (manifest.json, result.json, the bundle)."""

    run_id: str
    bundle_sha256: str
    title: str = ""
    task: str = ""
    cap_s: float | None = None
    cost_kind: str = "billed"
    t_pass_s: float | None = None
    site: str = ""  # the leaderboard's host, shown on the id line


def clips(segments: Sequence[Mapping[str, Any]], start: float, end: float, max_gap: float = MAX_GAP_S) -> list[Clip]:
    """The parts of the recorder's segments that cover [start, end] (epoch s), in order. RenderError when the
    recording misses more than max_gap seconds anywhere in the window."""
    if end <= start:
        raise RenderError(f"empty run window: start {start} end {end}")
    covered = start
    out: list[Clip] = []
    for s in sorted(segments, key=lambda s: float(s["start"])):
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


def panel_items(logs: Mapping[str, Iterable[Mapping[str, Any]]], start: float) -> list[Item]:
    """The side panel's entries from {events file stem: its events}, in time order (file and seq break ties).
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
                out.append(Item(at, name, seq, "call", f"{e.get('tool')} {_args(args)}".rstrip()))
            elif kind == "tool_result":
                kind_ = "error" if e.get("is_error") else "result"
                out.append(Item(at, name, seq, kind_, _first_line(text)))
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
        if isinstance(m, dict):
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
    snapshot at or before that second (the first snapshot's gold is the start), the cost of the play processes that
    had ended by then (result_costs; None until one has)."""
    snaps = sorted(((float(f["t"]) - start, f) for f in facts if f.get("kind") == "snapshot"), key=lambda p: p[0])
    costs = result_costs(logs, start)
    gold_start = next((int(s["gold"]) for _, s in snaps if s.get("gold") is not None), None)
    out: list[Strip] = []
    si = ci = 0
    snap: Mapping[str, Any] | None = None
    cost: float | None = None
    for sec in range(max(1, math.ceil(duration_s))):
        while si < len(snaps) and snaps[si][0] <= sec:
            snap = snaps[si][1]
            si += 1
        while ci < len(costs) and costs[ci][0] <= sec:
            cost = (cost or 0.0) + costs[ci][1]
            ci += 1
        gold = snap.get("gold") if snap else None
        out.append(
            Strip(
                sec,
                None if cost is None else round(cost, 2),
                None if gold is None else int(gold),
                gold_start,
                _party(snap) if snap else (),
                t_pass_s,
            )
        )
    return out


def sync_table(items: Sequence[Item], start: float, end: float, fps: int = FPS) -> Json:
    """render.json: the video's window (epoch s), its fps and every panel item's place in the video. RenderError when
    an item lies past the video's end."""
    late = [i for i in items if i.at_s > end - start + 1e-6]
    if late:
        raise RenderError(f"{len(late)} event(s) after the video's end, the first {late[0].file}#{late[0].seq}")
    return {
        "video_start": start,
        "video_end": end,
        "fps": fps,
        "events": [{"file": i.file, "seq": i.seq, "at_s": i.at_s} for i in items],
    }
