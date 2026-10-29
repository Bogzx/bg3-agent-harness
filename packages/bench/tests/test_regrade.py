"""bg3bench.regrade: a run voided by a stream failure today's classifier no longer counts is reported valid, and the
run's files are left as they were."""

from __future__ import annotations

import json
from pathlib import Path

from bg3bench import regrade

RESULT = (
    "queue e4 Tav: done (2 steps)\nEVENTS\n  queue_interrupted why=leg 2: move_to: TIMEOUT — timed out who=e3\nNOW x"
)


def test_a_background_timeout_no_longer_voids_the_run(tmp_path: Path) -> None:
    evs = [
        {"v": 1, "seq": 1, "t": 10.0, "src": "client", "kind": "tool_call", "tool": "wait_for", "call_id": "c1"},
        {"v": 1, "seq": 2, "t": 11.0, "src": "client", "kind": "tool_result", "text": RESULT, "call_id": "c1"},
    ]
    (tmp_path / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    (tmp_path / "S001.jsonl").write_text("")
    rec = {
        "run": 1,
        "outcome": "timeout",
        "started": 0,
        "harness_failures": 1,
        "manifest": {"seed": None},
        "failures": [
            {
                "t": 11.0,
                "kind": "tool_timeout",
                "class": "harness",
                "detail": "S001: move_to: TIMEOUT: timed out who=#id",
            },
            {"t": 20.0, "kind": "timeout", "class": "unknown", "detail": "the run's time ran out before the milestone"},
        ],
    }
    (tmp_path / "runs.jsonl").write_text(json.dumps(rec) + "\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    out = regrade.report(tmp_path)
    assert out[0].endswith("CHANGED: harness_failures 1 -> 0, valid False (harness_failure) -> True (None)")
    assert "  + bg_timeout [unknown] S001: move_to: TIMEOUT: timed out who=#id" in out
    assert "  + timeout [agent] the run's time ran out before the milestone" in out
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_run_k_is_regraded_from_its_own_dir_not_the_series_dir(tmp_path: Path) -> None:
    # all records sit in the series dir's runs.jsonl; run 3 (a failed start, no play) has its own empty dir
    series, r3 = tmp_path / "base", tmp_path / "base-r3"
    series.mkdir()
    r3.mkdir()
    evs = [
        {"v": 1, "seq": 1, "t": 10.0, "src": "client", "kind": "tool_call", "tool": "wait_for", "call_id": "c1"},
        {"v": 1, "seq": 2, "t": 11.0, "src": "client", "kind": "tool_result", "text": RESULT, "call_id": "c1"},
    ]
    (series / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    (series / "S001.jsonl").write_text("")
    rec = {"series": "base", "playthrough": "base-r3", "run": 3, "outcome": "runner_error", "harness_failures": 0}
    (series / "runs.jsonl").write_text(json.dumps(rec) + "\n")
    # before: run 1's S001 bg_timeout was listed as run 3's (+ line)
    assert regrade.report(series) == [
        "base run 3: unchanged: harness_failures 0 -> 0, valid False (runner_error) -> False (runner_error)"
    ]


def test_deaths_are_recounted_from_the_facts(tmp_path: Path) -> None:
    """METRICS #4: result.json deaths were zeros in the 2026-10-25 dirs, written before the facts losses counter."""
    (tmp_path / "S001.jsonl").write_text("")
    (tmp_path / "S001.events.jsonl").write_text("")
    zeros = {"fights": 0, "downs": 0, "deaths": 0, "wipes": 0}
    (tmp_path / "result.json").write_text(json.dumps({"deaths": zeros}))
    rows = [
        {"v": 1, "kind": "mark", "t": 0.0, "game_t": 0.0, "mark": "run_start"},
        {
            "v": 1,
            "kind": "snapshot",
            "t": 1.0,
            "game_t": 1.0,
            "gold": 0,
            "region": None,
            "area": None,
            "areas": [],
            "combat": False,
            "inv": [],
            "quests": {},
            "party": [{"name": n, "level": 3, "dead": False, "downed": False} for n in ("Tav", "Gale")],
        },
        {"v": 1, "kind": "event", "t": 2.0, "game_t": 2.0, "ev": "combat_started"},
        {"v": 1, "kind": "event", "t": 3.0, "game_t": 3.0, "ev": "downed", "name": "Gale", "party": True},
    ]
    (tmp_path / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    rec = {"run": 1, "outcome": "failed", "started": 0, "harness_failures": 0, "manifest": {"seed": None}}
    (tmp_path / "runs.jsonl").write_text(json.dumps(rec) + "\n")
    out = regrade.report(tmp_path)
    assert f"  deaths {zeros} -> {{'fights': 1, 'downs': 1, 'deaths': 0, 'wipes': 0}}" in out
