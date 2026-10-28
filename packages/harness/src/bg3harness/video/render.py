"""Render a run's evidence video: the game recording beside the agent's tool calls and visible text.

    python -m bg3harness.video <run_dir> --index <recording>/index.json --out <dir> --run-id ID --run-hash SHA256 \
        [--site HOST] [--title T] [--task T] [--cap-min N] [--cost-kind billed] [--t-pass-s S] [--sample-s N] \
        [--encoder libx264|h264_nvenc]

It reads only files that also go into the run's bundle (every Sxxx*.events.jsonl, facts.jsonl) and the recorder's
segments, and writes into --out: render.json (the sync table), evidence.mp4 (1920x1080) and frames/*.jpg (the
contact sheet). The run's window is facts.jsonl's run_start to run_end mark (the events' first and last time when a
mark is missing). A recording with a gap over 5 s in that window, or an event past its end, is refused (exit 2).
--sample-s N renders only the first N seconds (events after them left out), for a look at the layout.
Deterministic: the same inputs give the same images and the same render.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import compose, draw, timeline
from .timeline import Clip, Item, Json, Meta, RenderError, Strip


@dataclass(frozen=True)
class Plan:
    """Everything the video shows, before any pixel is drawn."""

    start: float
    end: float
    clips: list[Clip]
    items: list[Item]
    changes: list[tuple[float, int]]
    strips: list[Strip]
    sync: Json

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def run_hash(s: str) -> str:
    """--run-hash: a lowercase sha256 hex digest (the run's record hash, known before the render)."""
    if not re.fullmatch(r"[0-9a-f]{64}", s):
        raise argparse.ArgumentTypeError(f"not a sha256 hex digest: {s!r}")
    return s


def read_jsonl(path: Path) -> list[Json]:
    """The JSON objects of a .jsonl file; a line that isn't one (a half-written last line) is skipped."""
    out: list[Json] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(dict(v))  # pyright: ignore[reportUnknownArgumentType]
    return out


def events_logs(run_dir: Path) -> dict[str, list[Json]]:
    """{file stem: events} for every play process's events log (S001.events.jsonl, S001-c1.events.jsonl, ...)."""
    return {p.name.removesuffix(".events.jsonl"): read_jsonl(p) for p in sorted(run_dir.glob("*.events.jsonl"))}


def run_window(
    facts: Sequence[Mapping[str, Any]], logs: Mapping[str, Sequence[Mapping[str, Any]]]
) -> tuple[float, float]:
    """The run's start and end (epoch s): facts.jsonl's run_start and run_end marks, else the events' first and last."""
    marks = {
        f.get("mark"): float(f["t"])
        for f in facts
        if f.get("kind") == "mark" and f.get("mark") in ("run_start", "run_end")
    }
    times = [float(e["t"]) for evs in logs.values() for e in evs]
    if not times and len(marks) < 2:
        raise RenderError("no events and no run_start/run_end marks: nothing to render")
    return marks.get("run_start", min(times or [0.0])), marks.get("run_end", max(times or [0.0]))


def plan(
    segments: Sequence[Mapping[str, Any]],
    logs: Mapping[str, Sequence[Mapping[str, Any]]],
    facts: Sequence[Mapping[str, Any]],
    start: float,
    end: float,
    t_pass_s: float | None = None,
    clip_events: bool = False,
) -> Plan:
    """The video of [start, end]: its clips, panel items and changes, strip states and render.json. clip_events:
    leave out the events after `end` (a sample) instead of refusing the run."""
    cuts = timeline.clips(segments, start, end)
    items = timeline.panel_items(logs, start)
    if clip_events:
        items = [i for i in items if i.at_s <= end - start]
    sync = timeline.sync_table(items, start, end)
    strips = timeline.strip_states(end - start, facts, logs, start, t_pass_s)
    return Plan(start, end, cuts, items, timeline.changes(items), strips, sync)


def write_images(p: Plan, meta: Meta, work: Path) -> tuple[Path, Path]:
    """The side panel's images (one per change) and the strip's (one per second) with their concat lists; returns
    the lists (strip, side)."""
    work.mkdir(parents=True, exist_ok=True)
    side: list[tuple[Path, float]] = []
    for k, (at, n) in enumerate(p.changes):
        nxt = p.changes[k + 1][0] if k + 1 < len(p.changes) else p.duration
        if nxt <= at:
            continue
        path = work / f"side-{k:05d}.png"
        draw.side_image(p.items[:n]).save(path)
        side.append((path, round(nxt - at, 3)))
    strip: list[tuple[Path, float]] = []
    for s in p.strips:
        path = work / f"strip-{s.clock_s:05d}.png"
        draw.strip_image(s, meta).save(path)
        strip.append((path, round(min(1.0, p.duration - s.clock_s), 3)))
    side_list, strip_list = work / "side.ffconcat", work / "strip.ffconcat"
    side_list.write_text(compose.images_list(side), encoding="utf-8")
    strip_list.write_text(compose.images_list(strip), encoding="utf-8")
    return strip_list, side_list


def render(
    p: Plan,
    meta: Meta,
    video_dir: Path,
    out: Path,
    work: Path,
    ffmpeg: compose.Ffmpeg = compose.run_ffmpeg,
    encoder: str = "libx264",
) -> Path:
    """render.json, evidence.mp4 (encoded with `encoder`: compose.ENCODERS) and frames/ into `out`; the images and
    lists into `work`. Returns the video.
    RenderError before anything is written when a clip's file is gone (the recorder's disk cap deletes the oldest)."""
    gone = [c.file for c in p.clips if not (video_dir / c.file).is_file()]
    if gone:
        raise RenderError(f"recording segment(s) missing in {video_dir}: {', '.join(gone)}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "render.json").write_text(json.dumps(p.sync, indent=1) + "\n", encoding="utf-8")
    strip_list, side_list = write_images(p, meta, work)
    game_list = work / "game.ffconcat"
    game_list.write_text(compose.clips_list(p.clips, video_dir), encoding="utf-8")
    video = out / "evidence.mp4"
    ffmpeg(compose.compose_args(game_list, strip_list, side_list, video, p.duration, encoder=encoder))
    frames = out / "frames"
    frames.mkdir(exist_ok=True)
    ffmpeg(compose.sheet_args(video, frames))
    return video


def main(argv: Sequence[str] | None = None, ffmpeg: compose.Ffmpeg = compose.run_ffmpeg) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3harness.video", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--index", type=Path, required=True, help="the recorder's index.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--run-hash", type=run_hash, required=True, help="the run hash (64 hex; the id line shows its first 12)"
    )
    ap.add_argument("--site", default="", help="the leaderboard's host, shown after the run hash")
    ap.add_argument("--title", default="")
    ap.add_argument("--task", default="")
    ap.add_argument("--cap-min", type=float)
    ap.add_argument("--cost-kind", default="billed", choices=("billed", "notional", "estimated"))
    ap.add_argument("--t-pass-s", type=float)
    ap.add_argument("--sample-s", type=float, help="render only the run's first N seconds")
    ap.add_argument(
        "--encoder", default="libx264", choices=sorted(compose.ENCODERS), help="h264_nvenc: an NVIDIA GPU encodes"
    )
    ap.add_argument("--work", type=Path, help="where the images go (default: a temporary directory)")
    a = ap.parse_args(argv)
    logs = events_logs(a.run_dir)
    facts_path = a.run_dir / "facts.jsonl"
    facts = read_jsonl(facts_path) if facts_path.exists() else []
    index = json.loads(a.index.read_text(encoding="utf-8"))
    cap = a.cap_min * 60 if a.cap_min else None
    meta = Meta(a.run_id, a.run_hash, a.title, a.task, cap, a.cost_kind, a.t_pass_s, a.site)
    try:
        start, end = run_window(facts, logs)
        if a.sample_s:
            end = min(end, start + a.sample_s)
        p = plan(index.get("segments") or [], logs, facts, start, end, a.t_pass_s, clip_events=bool(a.sample_s))
        if a.work:
            video = render(p, meta, a.index.parent, a.out, a.work, ffmpeg, a.encoder)
        else:
            with tempfile.TemporaryDirectory(prefix="bg3-video-") as tmp:
                video = render(p, meta, a.index.parent, a.out, Path(tmp), ffmpeg, a.encoder)
    except RenderError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"{video} ({p.duration:.0f} s, {len(p.items)} panel items, {len(p.changes)} panel changes)")
    return 0
