"""Every bridge test starts with an empty handle map: core.H is the process-wide name -> handle registry, and a test
that registered a name left it for every later test (test_actq's target "Claude" was "ambiguous" after test_companions,
test_delta, test_game_moved, test_talk_stale or test_travel_party had added another "Claude" in the same run).
Emptied in place, not replaced: core.DELTA keeps the same Handles object (delta.py), and a new one would split them."""

import pytest
from bg3ai.tools import core


@pytest.fixture(autouse=True)
def empty_handles() -> None:
    core.H.__init__()
