# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""A bench run's nav reset (bg3harness.runner.run_isolation, through LiveRunHost's start and record): the mod's nav
files moved aside for a save start and put back after the run, the run's own files moved out of the shared folder,
and a dead run's left-overs restored by the next start (split out of test_run_host.py for the 1000-line cap)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.runner import run_isolation as RI  # noqa: N812

# rig: the fixture, taken by name
from test_run_host import (  # noqa: F401
    JOB,
    NEW_SC,
    SAVE_SC,
    FakeRunner,
    Rig,
    outcome,
    rig,  # pyright: ignore[reportUnusedImport]
    run_dir,
    se_names,
)


def test_record_puts_the_nav_files_back_and_keeps_the_runs_own(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert H.nav_marker().exists() and se_names() == ["other.json"]
    (renv.SE_BG3AI_DIR / "nav_aa.json").write_text('{"run": 1}')  # the run's game wrote its host's file again
    rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert (renv.SE_BG3AI_DIR / "nav_aa.json").read_text() == "{}"
    assert (run_dir("m-grove") / "reset" / "run" / "nav_aa.json").read_text() == '{"run": 1}'
    assert not H.nav_marker().exists()


def test_the_nav_files_come_back_when_the_game_stop_fails(rig: Rig):
    def prepare(r: FakeRunner) -> None:
        def boom(when: str = "", after: str = "") -> None:
            raise RuntimeError("kill failed")

        r.end_run_game = boom  # pyright: ignore[reportAttributeAccessIssue]

    rig.prepare = prepare
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    with pytest.raises(RuntimeError, match="kill failed"):
        rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]


def test_a_new_game_moves_no_nav_file(rig: Rig):  # its host is a new uuid: no file is its own
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert rig.runners[-1].events == [("run_reset", {"nav_files": []})]
    assert rig.h.record(JOB, NEW_SC, run_dir("m-idol"), outcome())["reset"] == {
        "nav_files": [],
        "restored": [],
        "saves": [],
    }


@pytest.mark.parametrize("sc", [NEW_SC, SAVE_SC])
def test_record_moves_the_runs_own_files_out_of_the_shared_folder(rig: Rig, sc: dict[str, Any]):  # live9 #11
    rig.h.start(JOB, sc, run_dir("m-idol"))
    for name in ("nav_bb.json", "nav_guard_bb.json", "scenario.json"):  # the run's new host and its seed record
        (renv.SE_BG3AI_DIR / name).write_text('{"run": 1}')
    rig.h.record(JOB, sc, run_dir("m-idol"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert sorted(p.name for p in (run_dir("m-idol") / "reset" / "run").iterdir()) == [
        "nav_bb.json",
        "nav_guard_bb.json",
        "scenario.json",
    ]
    assert not H.nav_marker().exists()


def test_a_scenario_record_from_before_the_run_stays(rig: Rig):
    (renv.SE_BG3AI_DIR / "scenario.json").write_text('{"renames": []}')
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))
    rig.h.record(JOB, NEW_SC, run_dir("m-idol"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json", "scenario.json"]
    # it stays, so the run's seed must leave it: scn.undo while the game is still up (it seeded every later load)
    assert rig.calls[-2:] == ["scenario_undo", "end_run_game(run end)"]


def test_the_next_start_restores_what_a_dead_run_left_moved(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))  # the process dies here: no record

    def no_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        return {}

    host = H.LiveRunHost(lambda opt: FakeRunner(opt, []), run_files=no_files, env={}, clock=lambda: 2000.0)
    host.start(JOB, NEW_SC, run_dir("m-idol"))
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert json.loads(H.nav_marker().read_text())["dest"] == str(run_dir("m-idol") / "reset")  # the new run's own


def test_archive_moves_only_the_nav_files(tmp_path: Path):
    se = tmp_path / "se"
    se.mkdir()
    for name in ("nav_guard.json", "nav_x.json", "keep.json"):
        (se / name).write_text("{}")
    assert RI.archive_nav_state(se, tmp_path / "dest") == ["nav_guard.json", "nav_x.json"]
    assert [p.name for p in se.iterdir()] == ["keep.json"]
    assert RI.archive_nav_state(tmp_path / "missing", tmp_path / "dest") == []
