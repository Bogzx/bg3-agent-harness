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
    facts.jsonl, rolls.jsonl  the playthrough's (FactsFeed: play.py's facts_poll on every story read, write_choices at
                            a session's end)

Nothing here raises: a file that can't be written is logged and the session goes on, as in play.py.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bg3ai import metrics as latency  # pyright: ignore[reportMissingTypeStubs]
from bg3ai import progress as story  # pyright: ignore[reportMissingTypeStubs]
from bg3ai.referee import Rpc  # pyright: ignore[reportMissingTypeStubs]
from bg3harness import choices, events, facts, triage

Log = Callable[[str], None]
StoryRead = Callable[[], dict[str, Any] | None]  # one poll of the game (bg3ai.progress.read_game), None = no answer
LuaEval = Callable[[str, str], str]  # (code, context) -> the eval's JSON


def metrics_path(sdir: Path, sid: str) -> Path:
    return sdir / f"{sid}.metrics.jsonl"


class FactsFeed:
    """The playthrough's facts.jsonl (+ rolls.jsonl), as play.py's facts_poll and write_choices write it: the run_start
    mark on first use (a file that has one keeps its time zero), on each story read the referee ring, then the read
    with the party inventory, and at a session's end its dialogue choices. One per playthrough for the server's life
    (play.py: one per runner process). No settings readout: that is a scenario run's (play.py's game_setup)."""

    def __init__(self, sdir: Path, rpc: Rpc, lua_eval: LuaEval, run_start: dict[str, Any]):
        self.writer = facts.FactsWriter(sdir / "facts.jsonl")
        if self.writer.t0 is None:
            self.writer.mark("run_start", run_start)  # pyright: ignore[reportUnknownMemberType]
        self.ring = facts.RingFeed(rpc, self.writer, sdir / "rolls.jsonl")
        self.lua_eval = lua_eval

    def poll(self, got: dict[str, Any] | None) -> None:
        if got:
            self.ring.poll()
        inv: dict[str, Any] | None = facts.read_inventory(self.lua_eval, story.combined_code) if got else None  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
        self.writer.observe(got, inv)  # pyright: ignore[reportUnknownMemberType]

    def choices(self, sid: str, base: Path, log: Log) -> None:
        """The session's dialogue choices (scene, options, pick, stated reason) from its <sid>.events.jsonl."""
        try:
            evs = events.load_events(base, kinds={"text", "thinking", "tool_call", "tool_result"})
            self.writer.timed_events(choices.choices(evs))  # pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            log(f"{sid}: choices failed: {e}")


class ProgressPoller:
    """Polls the game's story progress into <sid>.progress.jsonl every `interval` s until `stop` is set (play.py's
    progress_loop without its --until check); each read also goes to `facts` (FactsFeed.poll) first, as there."""

    def __init__(
        self, sdir: Path, sid: str, start: float, read: StoryRead, log: Log, interval: float = story.POLL_S,
        facts: Callable[[dict[str, Any] | None], None] | None = None,
    ):  # fmt: skip
        self.sid, self.read, self.log, self.interval, self.facts = sid, read, log, interval, facts
        self.timeline = story.Timeline(sdir / f"{sid}.progress.jsonl", sdir / "progress_state.json", start)
        self.actions = story.actions_in(metrics_path(sdir, sid))

    def poll(self, now: float | None = None) -> None:
        try:
            got = self.read()
        except Exception:  # not answering: a poll without an answer
            got = None
        if self.facts is not None:
            try:
                self.facts(got)
            except Exception as e:
                self.log(f"{self.sid}: facts poll failed: {e}")
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
