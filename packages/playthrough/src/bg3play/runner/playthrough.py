"""PlaythroughSide: the playthrough's parts of the session runner, what bg3harness.runner.core.SessionRunner calls as
PlaythroughParts (handoff_session, join_keeper, stop_check; lsf_attributes, which reads tools/saves, is the
legacy Runner's) plus what only a playthrough runs (the
memory session, the keeper, the plan-line check, the review, play_session and the continuous loop `run`).

A mixin over SessionRunner (it reads the runner's options, state, notes and game side): runner/legacy_playthrough.py's
Runner is PlaythroughSide + the legacy series' parts + SessionRunner, the class `play.py run` plays with.
"""

from __future__ import annotations

from .keeper_side import KeeperSide
from .loop import LoopSide
from .memory_session import parse_handoff_session
from .memory_side import MemorySide
from .session_side import SessionSide

__all__ = ["PlaythroughSide", "parse_handoff_session"]


class PlaythroughSide(MemorySide, KeeperSide, SessionSide, LoopSide):
    pass
