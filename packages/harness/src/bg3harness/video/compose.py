"""The evidence video's ffmpeg side: the concat lists (the game's clips, the strip's and the side panel's images with
their durations), the compose command into 1920x1080 and the contact sheet. The ffmpeg process is behind the Ffmpeg
Protocol, so the arguments are tested without it.

    game clips ─┐ (fps, 1280x720)
    strip pngs ─┴ vstack ─┐
    side pngs ────────────┴ hstack -> libx264 -preset veryfast -crf 24, 15 fps, yuv420p, +faststart
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from .draw import GAME_H, GAME_W
from .timeline import FPS, Clip, RenderError

CRF = 24
PRESET = "veryfast"
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


def compose_args(game: Path, strip: Path, side: Path, out: Path, duration: float, fps: int = FPS) -> list[str]:
    """ffmpeg's arguments: the three concat lists composed into one 1920x1080 H.264 video of `duration` seconds."""
    norm = f"fps={fps},format=yuv420p,setsar=1"
    graph = ";".join(
        [
            f"[0:v]scale={GAME_W}:{GAME_H}:force_original_aspect_ratio=decrease,"
            f"pad={GAME_W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2,{norm}[g]",
            f"[1:v]{norm}[s]",
            f"[2:v]{norm}[p]",
            "[g][s]vstack=inputs=2[l]",
            "[l][p]hstack=inputs=2[v]",
        ]
    )
    concat = ["-f", "concat", "-safe", "0"]
    return [
        "-y", "-hide_banner", "-loglevel", "error",
        *concat, "-i", str(game),
        *concat, "-i", str(strip),
        *concat, "-i", str(side),
        "-filter_complex", graph, "-map", "[v]", "-an",
        "-t", f"{duration:.3f}", "-r", str(fps),
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def sheet_args(video: Path, frames: Path) -> list[str]:
    """The contact sheet: one SHEET_W x SHEET_H JPEG per SHEET_EVERY_S seconds of the video, frames/0001.jpg on."""
    vf = f"fps=1/{SHEET_EVERY_S},scale={SHEET_W}:{SHEET_H}"
    return [
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vf", vf, "-q:v", "5", str(frames / "%04d.jpg"),
    ]  # fmt: skip
