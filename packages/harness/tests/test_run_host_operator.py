"""A bench run never takes the playthrough app's operator messages: LiveRunHost._setup drops every BG3AI_OPERATOR_*
from the env the run gets, even when the shell that started the bench exports it."""
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)

from __future__ import annotations

# rig: the fixture, taken by name
from test_run_host import JOB, SAVE_SC, Rig, rig, run_dir  # noqa: F401  # pyright: ignore[reportUnusedImport]


def test_a_bench_runs_env_has_no_operator_dir(rig: Rig):
    h = rig.h
    h.base |= {"BG3AI_OPERATOR_DIR": "/rt/sessions/ilse", "BG3AI_OPERATOR_X": "1", "BG3AI_PLAYTHROUGH": "ilse"}
    h.start(JOB, SAVE_SC, run_dir("m-grove"))
    run_env = rig.runners[-1].run_env
    assert run_env is not None and run_env is h.env and not [k for k in run_env if k.startswith("BG3AI_OPERATOR_")]
    assert run_env["BG3AI_PLAYTHROUGH"] == "ilse"  # only the operator's keys go
