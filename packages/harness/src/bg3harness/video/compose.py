"""The evidence video's ffmpeg side: the concat lists (the game's clips, the frame's and the column's images with
their durations), the compose command into 1920x1080 and the contact sheet. The ffmpeg process is behind the Ffmpeg
Protocol, so the arguments are tested without it.

    frame pngs (1920x1080, one per second) ─┐
    game clips (scaled to 1408x792, Lanczos) ┴ overlay at 24,96 ─┐   (off during a logged gap: the frame's
                                                                  │    pause card shows; later clips moved by it)
    column pngs (440x960 RGBA, one per panel change) ─────────────┴ overlay at 1456,96
        -> libx264 -preset veryfast -crf 24 (or h264_nvenc -cq 24 on an NVIDIA GPU), 15 fps, yuv420p, +faststart
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .draw import COL_X, COL_Y, GAME_H, GAME_W, GAME_X, GAME_Y
from .timeline import FPS, Clip, Gap, RenderError

CRF = 24
ENCODERS: dict[str, tuple[str, ...]] = {
    "libx264": ("-preset", "veryfast", "-crf", str(CRF)),  # the default: any machine
    "h264_nvenc": ("-preset", "p5", "-rc", "vbr", "-cq", str(CRF), "-b:v", "0"),  # an NVIDIA GPU: the CPU stays free
}
SHEET_EVERY_S = 30  # the contact sheet: one frame per 30 s of run
SHEET_W, SHEET_H = 320, 180


class Ffmpeg(Protocol):
    def __call__(self, args: Sequence[str]) -> None: ...


def run_ffmpeg(args: Sequence[str]) -> None:
    """ffmpeg with args; RenderError with the end of its stderr when it fails."""
    p = subprocess.run(["ffmpeg", *args], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise RenderError(f"ffmpeg exited {p.returncode}: {p.stderr.strip()[-800:]}")


def _quote(path: Path | str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def clips_list(clips: Sequence[Clip], video_dir: Path) -> str:
    """The game's concat list: each clip's file (absolute) with its inpoint and outpoint."""
    out = ["ffconcat version 1.0"]
    for c in clips:
        out += [
            f"file {_quote((video_dir / c.file).resolve())}",
            f"inpoint {c.inpoint:.3f}",
            f"outpoint {c.outpoint:.3f}",
        ]
    return "\n".join(out) + "\n"


def images_list(images: Sequence[tuple[Path, float]]) -> str:
    """An image concat list: each image with how long it shows; the last one repeated (the concat demuxer ignores
    the last entry's duration otherwise)."""
    if not images:
        raise ValueError("no images")
    out = ["ffconcat version 1.0"]
    for path, dur in images:
        out += [f"file {_quote(path.resolve())}", f"duration {dur:.3f}"]
    out.append(f"file {_quote(images[-1][0].resolve())}")
    return "\n".join(out) + "\n"


def gap_filters(gaps: Sequence[Gap], fps: int = FPS) -> tuple[str, str]:
    """(the game's setpts, the game overlay's enable) for logged gaps: each clip after a gap moves to its run time
    (the concatenation's time + the gap's offset), and the overlay is off over the gap (the frame's pause card
    shows). Empty strings without gaps."""
    if not gaps:
        return "", ""
    half = 0.5 / fps  # the first frame after a cut has its exact time: compare half a frame early
    prev, shift = 0.0, list[str]()
    for g in gaps:
        if g.offset != prev:
            shift.append(f"{g.offset - prev:.3f}*gte(T,{g.cut_s - half:.3f})")
        prev = g.offset
    setpts = f"setpts='PTS+({'+'.join(shift) or '0'})/TB'," if shift else ""
    off = "+".join(f"between(t,{g.at_s:.3f},{g.end_s - half:.3f})" for g in gaps)
    return setpts, f":enable='not({off})'"


def compose_args(
    game: Path,
    frame: Path,
    column: Path,
    out: Path,
    duration: float,
    fps: int = FPS,
    encoder: str = "libx264",
    gaps: Sequence[Gap] = (),
) -> list[str]:
    """ffmpeg's arguments: the three concat lists composed into one 1920x1080 H.264 video of `duration` seconds,
    encoded with `encoder` (ENCODERS). The game is scaled to GAME_W x GAME_H (Lanczos; a recording made at that
    size passes through). Logged gaps (gap_filters) keep the game on the run clock."""
    if encoder not in ENCODERS:
        raise ValueError(f"unknown encoder {encoder!r} (one of {sorted(ENCODERS)})")
    norm = f"fps={fps},setsar=1"
    setpts, enable = gap_filters(gaps, fps)
    graph = ";".join(
        [
            f"[0:v]{setpts}scale={GAME_W}:{GAME_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={GAME_W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2,{norm}[g]",
            f"[1:v]{norm}[f]",
            f"[2:v]{norm},format=rgba[c]",
            f"[f][g]overlay={GAME_X}:{GAME_Y}{enable}[fg]",
            f"[fg][c]overlay={COL_X}:{COL_Y},format=yuv420p[v]",
        ]
    )
    concat = ["-f", "concat", "-safe", "0"]
    return [
        "-y", "-hide_banner", "-loglevel", "error",
        *concat, "-i", str(game),
        *concat, "-i", str(frame),
        *concat, "-i", str(column),
        "-filter_complex", graph, "-map", "[v]", "-an",
        "-t", f"{duration:.3f}", "-r", str(fps),
        "-c:v", encoder, *ENCODERS[encoder], "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def sheet_args(video: Path, frames: Path) -> list[str]:
    """The contact sheet: one SHEET_W x SHEET_H JPEG per SHEET_EVERY_S seconds of the video, frames/0001.jpg on."""
    vf = f"fps=1/{SHEET_EVERY_S},scale={SHEET_W}:{SHEET_H}"
    return [
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vf", vf, "-q:v", "5", str(frames / "%04d.jpg"),
    ]  # fmt: skip
