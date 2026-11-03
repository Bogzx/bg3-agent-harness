"""Every bridge test starts with an empty handle map: core.H is the process-wide name -> handle registry, and a test
that registered a name left it for every later test (test_actq's target "Claude" was "ambiguous" after test_companions,
test_delta, test_game_moved, test_talk_stale or test_travel_party had added another "Claude" in the same run).
Emptied in place, not replaced: core.DELTA keeps the same Handles object (delta.py), and a new one would split them.
The objective ids (overview._objective_ids, o1, o2, ... for the session) likewise: test_delta read o4 for its o1 after
test_metrics' ServerHook had shown three objectives."""

import pytest
from bg3ai.tools import core, overview


@pytest.fixture(autouse=True)
def empty_registries() -> None:
    core.H.__init__()
    overview._objective_ids.clear()
