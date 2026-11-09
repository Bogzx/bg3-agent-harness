"""The run record's referee ring health (REVIEW-16 A1): LiveRunHost.record reads it after run_end, with the game up."""
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)

from __future__ import annotations

from typing import Any

# rig: the fixture, taken by name
from test_run_host import JOB, SAVE_SC, Rig, outcome, rig, run_dir  # noqa: F401  # pyright: ignore[reportUnusedImport]


def test_record_carries_the_referee_rings_health(rig: Rig):
    """What the ring dropped and which listeners never fired, read after run_end and before the game stops; None
    when the runner never polled the ring."""
    h = rig.h
    h.start(JOB, SAVE_SC, run_dir("m-grove"))
    r = rig.runners[-1]

    def health() -> dict[str, Any]:
        rig.calls.append("ring_health")
        return {"ring_lost": 3, "never_fired": ["Osiris:ShortRested"]}

    r.ring_health = health
    rig.calls.clear()
    rec = h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert rec["referee"] == {"ring_lost": 3, "never_fired": ["Osiris:ShortRested"]}
    assert rig.calls == ["end_facts(agent_exited)", "ring_health", "scenario_undo", "end_run_game(run end)"]
    h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())["referee"] is None  # no ring polled
