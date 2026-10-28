"""bg3play.recorder is bg3harness.recorder itself (the recorder moved to the harness), so the app's imports and a
monkeypatch on either name reach the same code."""

from __future__ import annotations

import bg3harness.recorder
import bg3play.recorder
from bg3play import recorder, replay


def test_the_app_name_is_the_harness_module():
    assert bg3play.recorder is bg3harness.recorder
    assert recorder.Recorder is bg3harness.recorder.Recorder
    assert replay.rec is bg3harness.recorder
