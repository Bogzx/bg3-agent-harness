"""Runner, the class `play.py run` plays with: SessionRunner (bg3harness.runner) + the playthrough's parts
(bg3play.runner.PlaythroughSide: the memory session, the keeper, the plan-line check, the review, play_session and
the continuous loop) + the legacy series' (legacy_series.py). The playthrough code moved to bg3play (Phase D.2).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import _harness  # noqa: F401  (packages/harness/src and bridge/src on sys.path)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "playthrough" / "src"))  # bg3play

import legacy_series as lser
from bg3harness.game.io import LaunchBlocked, SteamNotOffline  # noqa: F401  (the runner's tests name them here)
from bg3harness.runner import core as rcore
from bg3play.runner.playthrough import PlaythroughSide, parse_handoff_session  # noqa: F401  (legacy_cli's)


class Runner(PlaythroughSide, lser.SeriesSide, rcore.SessionRunner):
    """`play.py run`: SessionRunner with the playthrough parts (PlaythroughSide) and the series' (SeriesSide)."""

    def lsf_attributes(self) -> Callable[[bytes], list]:
        from bg3ai import lsf

        return lsf.attributes
