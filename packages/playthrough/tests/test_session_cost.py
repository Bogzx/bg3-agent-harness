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
