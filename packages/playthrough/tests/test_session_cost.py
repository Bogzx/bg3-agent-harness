# pyright: basic
import json
from pathlib import Path

from bg3play.adapters.session_cost import session_cost


def _log(path: Path, *events: dict) -> None:
    lines = [
        json.dumps({"v": 1, "seq": i, "t": 1.0 + i, "src": "client", **e}, separators=(",", ":"))
        for i, e in enumerate(events)
    ]
    path.write_text("\n".join(lines) + "\n")


def test_claude_result_cost(tmp_path: Path):
    _log(
        tmp_path / "S001.events.jsonl",
        {"kind": "usage", "tokens": {"input": 10, "output": 5}, "message_id": "m1"},
        {
            "kind": "result",
            "subtype": "error_during_execution",
            "turns": 212,
            "cost_usd": 4.5169092,
            "tokens": {"input": 378},
        },
    )
    assert session_cost(tmp_path / "S001") == 4.5169


def test_pi_running_usage_cost_and_resumes(tmp_path: Path):
    # pi's usage events carry the process's running (billed) cost and no result event when a cap stops it
    _log(
        tmp_path / "S001.events.jsonl",
        {"kind": "usage", "cost_usd": 0.25, "tokens": {"input": 1}, "message_id": "a"},
        {"kind": "usage", "cost_usd": 0.500185, "tokens": {"input": 2}, "message_id": "b"},
    )
    _log(
        tmp_path / "S001-r1.events.jsonl", {"kind": "usage", "cost_usd": 0.1, "tokens": {"input": 1}, "message_id": "c"}
    )
    _log(tmp_path / "S0011.events.jsonl", {"kind": "usage", "cost_usd": 9.0, "tokens": {"input": 1}, "message_id": "d"})
    assert session_cost(tmp_path / "S001") == 0.6002


def test_no_cost(tmp_path: Path):
    _log(tmp_path / "S001.events.jsonl", {"kind": "usage", "tokens": {"input": 1}, "message_id": "a"})
    assert session_cost(tmp_path / "S001") is None
    assert session_cost(tmp_path / "S002") is None
    assert session_cost(tmp_path / "missing" / "S001") is None


def _start(client: str) -> dict:
    return {"src": "harness", "kind": "session_start", "detail": {"session": "S001", "client": client}}


def _result(cost: float) -> dict:
    return {"kind": "result", "session_id": "c1", "turns": 2, "cost_usd": cost, "tokens": {"input": 10}}


def test_a_resumed_claude_conversation_costs_its_last_running_total(tmp_path: Path):
    # bench diverse-2026-10-25 Opus act1_gold_max: claude's result reports the conversation's cost so far over
    # --resume; the harness writes each resume to its own file (-cN stop resumes, -rN fault resumes)
    for name, cost in (("S001", 5.43), ("S001-c1", 8.45), ("S001-c2", 9.05), ("S001-r1", 9.70)):
        _log(tmp_path / f"{name}.events.jsonl", _start("claude"), _result(cost))
    assert session_cost(tmp_path / "S001") == 9.70


def test_the_apps_resumes_append_to_the_sessions_log(tmp_path: Path):
    # adapters/agent_process: every process of an app session appends to Sxxx.events.jsonl
    _log(tmp_path / "S001.events.jsonl", _start("claude"), _result(2.0), _start("claude"), _result(3.5))
    assert session_cost(tmp_path / "S001") == 3.5


def test_pi_results_over_one_conversation_are_each_their_process_s_cost(tmp_path: Path):
    # pi (DeepSeek) bills per process, never a running total
    _log(tmp_path / "S001.events.jsonl", _start("pi"), _result(0.10))
    _log(tmp_path / "S001-c1.events.jsonl", _start("pi"), _result(0.20))
    assert session_cost(tmp_path / "S001") == 0.3
