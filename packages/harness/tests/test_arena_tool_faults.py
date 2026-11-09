#!/usr/bin/env python3
# pyright: basic
"""A side's tool timeouts and exceptions, counted after the match: one is tolerated and recorded, a second makes the
match invalid (orchestrator's ruling 2026-11-09, the rule of FAULT_RESUMES; v011-pilot L12 m1's lone react TIMEOUT)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from bg3harness import runs
from bg3harness.arena import match as am
from test_arena_match import REPO, SIDES, Run, arena_events, attacker, grade, passer, setup

EVENT_SCHEMA = json.loads((REPO / "packages" / "bench" / "schema" / "arena-facts.v1.schema.json").read_text())

TIMEOUT = "react: TIMEOUT — timed out"
SAME_TOOL_OTHER_ERROR = "use_ability: TIMEOUT — timed out"
EXCEPTION = "Error executing tool use_ability: boom"
GAME_SAID_NO = "use_ability: REFUSED — not enough action"


def write_results(out: Path, side: str, texts: list[str], mode: str = "w", first: int = 0) -> None:
    """The side's events file, one tool_call and result a text (the file every process of a side appends to), and its
    first process's stream, which stream_failures needs to exist."""
    tool = [t.split(":")[0].split(" ")[-1] for t in texts]
    rows = []
    for i, (name, text) in enumerate(zip(tool, texts, strict=True), first):
        rows.append({"v": 1, "seq": 2 * i + 1, "t": 1.0 + i, "src": "client", "kind": "tool_call", "tool": name})
        rows[-1]["call_id"] = f"c{i}"
        rows.append({"v": 1, "seq": 2 * i + 2, "t": 1.5 + i, "src": "client", "kind": "tool_result", "text": text})
        rows[-1].update(call_id=f"c{i}", is_error=text == EXCEPTION)
    with (out / f"{side}.events.jsonl").open(mode) as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    (out / f"{side}.jsonl").touch()


def faults(out: Path) -> list[dict]:
    return [e for e in arena_events(out) if e["ev"] == "arena_tool_faults"]


def the_end(out: Path) -> dict:
    return next(e for e in arena_events(out) if e["ev"] == "arena_end")


def play(tmp_path: Path) -> am.ArenaOutcome:
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    return am.run_arena(spec, SIDES, game, agents, tmp_path, clock)


def test_no_tool_fault_writes_nothing(tmp_path):
    write_results(tmp_path, "A", ["get_status: all fine"])
    out = play(tmp_path)
    assert out.invalid is None and faults(tmp_path) == [] and "invalid" not in the_end(tmp_path)


def test_one_tool_timeout_is_tolerated_and_recorded(tmp_path):
    write_results(tmp_path, "A", [TIMEOUT])
    out = play(tmp_path)
    assert out.invalid is None and "invalid" not in the_end(tmp_path)
    assert [(f["side"], f["n"], f["faults"]) for f in faults(tmp_path)] == [("A", 1, ["A: react: TIMEOUT: timed out"])]
    g = grade(tmp_path)
    assert g["valid"] and g["result"] == "A"


def test_one_on_each_side_is_tolerated_the_bar_is_per_side(tmp_path):
    write_results(tmp_path, "A", [TIMEOUT])
    write_results(tmp_path, "B", [EXCEPTION])
    out = play(tmp_path)
    assert out.invalid is None
    assert [(f["side"], f["n"]) for f in faults(tmp_path)] == [("A", 1), ("B", 1)]
    assert grade(tmp_path)["valid"]


@pytest.mark.parametrize(
    "texts",
    [[TIMEOUT, TIMEOUT], [TIMEOUT, SAME_TOOL_OTHER_ERROR], [TIMEOUT, EXCEPTION]],
    ids=["the same failure twice", "two timeouts on two tools", "a timeout and an exception"],
)
def test_a_second_tool_fault_on_a_side_makes_the_match_invalid(tmp_path, texts):
    write_results(tmp_path, "B", texts)
    out = play(tmp_path)
    assert out.invalid == "tool_faults: side B had 2 tool timeouts or exceptions (one tolerated)"
    end = the_end(tmp_path)
    assert (end["invalid"], end["reason"], end["winner"]) == ("tool_faults", "wipe", "A")  # what happened stays written
    assert [(f["side"], f["n"]) for f in faults(tmp_path)] == [("B", 2)]
    for e in arena_events(tmp_path):
        jsonschema.validate(e, EVENT_SCHEMA)
    g = grade(tmp_path)
    assert (g["valid"], g["invalid_reason"], g["result"]) == (False, "tool_faults", None)


def test_a_game_refusal_is_no_tool_fault(tmp_path):
    write_results(tmp_path, "A", [GAME_SAID_NO, GAME_SAID_NO, "use_ability: FAILED — no target"])
    out = play(tmp_path)
    assert out.invalid is None and faults(tmp_path) == []


def test_a_resumes_tool_fault_counts_once(tmp_path):
    """The side's events file holds every process; the resume's raw stream names the same calls, and is not read."""
    write_results(tmp_path, "A", [TIMEOUT])
    resume = tmp_path / "A-r1.jsonl"
    call = {"type": "toolCall", "id": "c0", "name": "mcp__bg3__react", "arguments": {}}
    result = {"content": [{"type": "text", "text": TIMEOUT}]}
    rows = [
        {"type": "session", "version": 3, "id": "x"},
        {"type": "message_end", "message": {"role": "assistant", "content": [call]}},
        {
            "type": "tool_execution_end",
            "toolCallId": "c0",
            "toolName": call["name"],
            "result": result,
            "isError": False,
        },
    ]
    resume.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "tool_timeout" in [f["kind"] for f in runs.stream_failures(resume)]  # read on its own, it would count
    out = play(tmp_path)
    assert out.invalid is None and [f["n"] for f in faults(tmp_path)] == [1]


def test_a_fault_in_a_later_process_of_the_side_counts_with_the_first(tmp_path):
    write_results(tmp_path, "A", [TIMEOUT])
    write_results(tmp_path, "A", [EXCEPTION], mode="a", first=1)
    out = play(tmp_path)
    assert out.invalid is not None and [f["n"] for f in faults(tmp_path)] == [2]


def test_the_count_is_read_after_the_agents_stop(tmp_path, monkeypatch):
    """The results a stopped process flushes last count."""
    write_results(tmp_path, "A", [TIMEOUT])
    stop = Run.stop

    def stop_and_flush(run: Run) -> am.AgentExit:
        if run.side == "A":
            write_results(tmp_path, "A", [EXCEPTION], mode="a", first=1)
        return stop(run)

    monkeypatch.setattr(Run, "stop", stop_and_flush)
    out = play(tmp_path)
    assert out.invalid is not None and the_end(tmp_path)["invalid"] == "tool_faults"


def test_an_invalid_end_keeps_its_own_why_and_records_the_count(tmp_path):
    write_results(tmp_path, "A", [TIMEOUT, TIMEOUT])
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, aborted=lambda: True)
    assert out.invalid == "abort: operator stop" and the_end(tmp_path)["reason"] == "abort"
    assert [f["n"] for f in faults(tmp_path)] == [2]


def test_a_scripted_side_stays_invalid_as_scripted(tmp_path):
    write_results(tmp_path, "A", [TIMEOUT, TIMEOUT])
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    sides = {"A": am.Side("A", "scripted/none"), "B": SIDES["B"]}
    out = am.run_arena(spec, sides, game, agents, tmp_path, clock)
    assert (out.invalid or "").startswith("scripted") and the_end(tmp_path)["invalid"] == "scripted"
    assert [f["n"] for f in faults(tmp_path)] == [2]
