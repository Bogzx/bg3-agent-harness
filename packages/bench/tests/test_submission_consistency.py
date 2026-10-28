# pyright: basic
"""bg3bench.submission.consistency: each check's failure branch on small hand-made bundles (validate.Loaded), the
whole-bundle path being test_submission_validate.py's."""

from __future__ import annotations

from pathlib import Path

from bg3bench import scenario
from bg3bench.submission import consistency as c
from bg3bench.submission import pack, validate


def loaded(
    facts: list[dict] | None = None,
    events: dict[str, list[dict]] | None = None,
    result: dict | None = None,
    rolls: list[dict] | None = None,
    release_scenario: dict | None = None,
    release_condition: dict | None = None,
    manifest: dict | None = None,
    sc: dict | None = None,
) -> validate.Loaded:
    return validate.Loaded(
        Path("."),
        {},
        {},
        manifest or {"condition": "rules"},
        result or {},
        facts or [],
        rolls,
        {"S001.events.jsonl": []} if events is None else events,
        sc or {},
        release_scenario or {},
        release_condition or {},
    )


def mark(t: float, name: str, **detail) -> dict:
    return {"v": 1, "kind": "mark", "t": t, "mark": name} | ({"detail": detail} if detail else {})


def ev(t: float, kind: str, **kw) -> dict:
    return {"v": 1, "t": t, "kind": kind} | kw


def test_fingerprint_needs_a_snapshot_and_x01_a_published_one():
    assert c.fingerprint([mark(0, "run_start")]) is None
    assert c.x01_start_state(loaded()) == (False, "the suite publishes no start-state fingerprint for this scenario")
    ok, detail = c.x01_start_state(loaded(release_scenario={"fingerprint": {"gold": 1}}))
    assert ok is False and "differs" in detail


def test_x02_time_marks():
    assert c.x02_time_marks(loaded([mark(0, "run_start")]))[1] == "1 run_start and 0 run_end marks (want 1 each)"
    marks = [mark(0, "run_start"), mark(100, "run_end")]
    assert "wall_s 50" in c.x02_time_marks(loaded(marks, result={"wall_s": 50}))[1]
    late = {"S001.events.jsonl": [ev(500, "text")]}
    assert c.x02_time_marks(loaded(marks, late, {"wall_s": 100})) == (False, "1 events outside the run's marks")
    assert c.x02_time_marks(loaded(marks, {"S001.events.jsonl": [ev(50, "text")]}, {"wall_s": 100})) == (True, "")


def test_x04_rolls():
    assert c.x04_rolls(loaded(result={"luck": None})) == (True, "")
    assert c.x04_rolls(loaded(result={"luck": {"z": 1}})) == (False, "result.luck without rolls.jsonl")
    ok, detail = c.x04_rolls(loaded(rolls=[], result={"luck": {"z": 1}}))
    assert ok is False and "luck recomputed" in detail


def test_x05_tokens_and_cost():
    usage = {"S001.events.jsonl": [ev(1, "usage", tokens={"input": 5})]}
    assert "events' usage" in c.x05_tokens_cost(loaded(events=usage, result={"tokens": {}}))[1]
    assert c.x05_tokens_cost(loaded(result={"tokens": {}, "cost_usd": None}))[0] is True
    assert c.x05_tokens_cost(loaded(result={"tokens": {}, "cost_usd": 1.0}))[0] is False  # claimed, no usage costs
    priced = {"S001.events.jsonl": [ev(1, "usage", cost_usd=1.0), ev(2, "usage")]}
    assert c.process_costs(loaded(events=priced)) == {"S001.events.jsonl": 1.0}
    assert c.x05_tokens_cost(loaded(events=priced, result={"tokens": {}, "cost_usd": 1.0})) == (True, "")


def test_x06_resumes():
    start = ev(0, "session_start", detail={"session": "S001", "conversation": "a"})
    two = {"S001.events.jsonl": [start], "S001-c1.events.jsonl": [start]}
    assert "2 events logs, 2 session starts, 1 distinct" in c.x06_resumes(loaded(events=two))[1]
    one = {"S001.events.jsonl": [start]}
    ok, detail = c.x06_resumes(loaded(events=one, result={"first_stop_s": 50, "wall_s": 10}))
    assert ok is False and "first_stop_s 50" in detail
    assert c.x06_resumes(loaded(events=one, result={"first_stop_s": 5, "wall_s": 10})) == (True, "")


def test_x07_outlier():
    assert c.x07_outlier(loaded(), None)[0] is None
    fast = loaded(result={"passed": True, "t_pass_s": 10})
    assert "under half" in c.x07_outlier(fast, {"t_pass_min": 100})[1]
    rich = loaded(result={"score": {"gain": 500}})
    assert "over twice" in c.x07_outlier(rich, {"gain_max": 100})[1]
    assert c.x07_outlier(rich, {"gain_max": 300, "t_pass_min": None}) == (True, "")


def test_g01_and_g02_on_a_goal_scenario():
    sc = scenario.with_defaults(pack.load_scenarios()["fight_act2_balthazar"])
    run = [mark(0, "run_start") | {"game_t": 0}, mark(10, "run_end") | {"game_t": 10}]
    b = loaded(run, sc=sc, result={"passed": True, "t_pass_s": 1.0, "grade": {"violations": []}})
    ok, detail = c.g01_regrade(b)
    assert ok is False and "re-grade (passed, t_pass_s, violations)" in detail
    zero = {"fights": 0, "downs": 0, "deaths": 0, "wipes": 0}
    reload = loaded([mark(0, "load"), mark(5, "load")], result={"deaths": zero, "reloads": 0})
    assert "reloads 0 != the facts' 1" in c.g02_losses(reload)[1]


def test_a01_fairness_audit():
    b = loaded(result={"fairness": {"leak": 0, "check": 0, "blocks": 0}})
    assert c.a01_fairness(b, lambda paths: {"leak": 2, "check": 0, "blocks": 0}) == (
        False,
        "2 leaks in the tool results",
    )
    assert "audit" in c.a01_fairness(b, lambda paths: {"leak": 0, "check": 1, "blocks": 0})[1]
    assert c.a01_fairness(b, lambda paths: {"leak": 0, "check": 0, "blocks": 0}) == (True, "")


def test_a02_allowlist():
    assert c.a02_allowlist(loaded()) == (False, "the suite publishes no tool list for this condition")
    calls = {"S001.events.jsonl": [ev(1, "tool_call", tool="load_game")]}
    tools = {"tools": {"names": ["load_game"]}}
    assert c.a02_allowlist(loaded(events=calls, release_condition=tools)) == (
        False,
        "load_game called without a reloads condition",
    )
    reloads = loaded(events=calls, release_condition=tools, manifest={"condition": "rules+reloads:2"})
    assert c.a02_allowlist(reloads) == (True, "")
