"""An app session's analysis files, as play.py's Runner writes them (live9 #16: app sessions had none of them).

    <sid>.metrics.jsonl     the bg3 server's tool records ($BG3AI_METRICS_PATH = metrics_path; the bridge writes
                            <sid>.fights.jsonl next to it and reads <sid>.progress.jsonl from it)
    <sid>.turns.jsonl       the stream's arrival times (ClientAgent feeds bg3ai.metrics.StreamTiming)
    <sid>.progress.jsonl    story progress and stuck records while the session plays (ProgressPoller over
                            bg3ai.progress.Timeline, the playthrough's progress_state.json)
    <sid>.latency.json      + latency_history.jsonl   } at the session's end (write_session_files), from the same
    <sid>.progress.json     + progress_history.jsonl  } bg3ai.metrics / bg3ai.progress / bg3harness.triage
    <sid>.triage.md                                   } functions play.py's write_latency/progress/triage call
    <sid>.review.md         after the keeper pass (keeper_side.write_review, play.py's)

Nothing here raises: a file that can't be written is logged and the session goes on, as in play.py.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bg3ai import metrics as latency  # pyright: ignore[reportMissingTypeStubs]
from bg3ai import progress as story  # pyright: ignore[reportMissingTypeStubs]
from bg3harness import triage

Log = Callable[[str], None]
StoryRead = Callable[[], dict[str, Any] | None]  # one poll of the game (bg3ai.progress.read_game), None = no answer


def metrics_path(sdir: Path, sid: str) -> Path:
    return sdir / f"{sid}.metrics.jsonl"


class ProgressPoller:
    """Polls the game's story progress into <sid>.progress.jsonl every `interval` s until `stop` is set (play.py's
    progress_loop without its facts and --until checks)."""

    def __init__(self, sdir: Path, sid: str, start: float, read: StoryRead, log: Log, interval: float = story.POLL_S):
        self.sid, self.read, self.log, self.interval = sid, read, log, interval
        self.timeline = story.Timeline(sdir / f"{sid}.progress.jsonl", sdir / "progress_state.json", start)
        self.actions = story.actions_in(metrics_path(sdir, sid))

    def poll(self, now: float | None = None) -> None:
        try:
            got = self.read()
        except Exception:  # not answering: a poll without an answer
            got = None
        try:
            was_stuck = self.timeline.stuck_since is not None
            self.timeline.observe(got, now, actions_since=self.actions)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            if self.timeline.stuck_since is not None and not was_stuck:
                self.log(f"{self.sid}: stuck signal: no story progress for {story.STUCK_MIN:.0f}+ min while active")
        except Exception as e:
            self.log(f"{self.sid}: progress poll failed: {e}")

    def loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.interval):
            self.poll()


def write_session_files(sdir: Path, sid: str, start: float, end: float, log: Log) -> None:
    """<sid>.latency.json, <sid>.progress.json (+ their history rows) and <sid>.triage.md for the session's
    files."""
    base = sdir / sid
    metrics, turns = Path(f"{base}.metrics.jsonl"), Path(f"{base}.turns.jsonl")
    try:
        lat: dict[str, Any] = latency.summarize_files(metrics, turns, start, end)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        latency.write_summary(lat, Path(f"{base}.latency.json"), sdir / "latency_history.jsonl", sid)  # pyright: ignore[reportUnknownMemberType]
    except Exception as e:
        log(f"{sid}: latency summary failed: {e}")
    try:
        prog: dict[str, Any] = story.summarize(story.load(Path(f"{base}.progress.jsonl")), start, end)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        story.write_summary(prog, Path(f"{base}.progress.json"), sdir / "progress_history.jsonl", sid)  # pyright: ignore[reportUnknownMemberType]
        pts, stuck = prog["points"], prog["stuck_episodes"]
        log(f"{sid}: story progress {pts} pts ({prog['per_hour']}/h), {stuck} stuck episode(s)")
    except Exception as e:
        log(f"{sid}: progress summary failed: {e}")
    try:
        log(f"{sid}: triage {triage.write(base)}")
    except Exception as e:
        log(f"{sid}: triage failed: {e}")
