"""A session's <base>.latency.json counts the operator messages its results carried (operator_messages), from its
metrics `operator` records' ids; 0 for a session without them."""

from __future__ import annotations

import json
from pathlib import Path

from bg3harness import session_summary

T0 = 1_790_000_000.0


def tool(t: float, **extra: object) -> dict[str, object]:
    return {"type": "tool", "tool": "look", "t0": t, "t1": t + 0.2, "ms": 200.0, **extra}


def told(t: float, *ids: str) -> dict[str, object]:
    return {"type": "operator", "t": t, "tool": "look", "ids": list(ids)}


def write_session(base: Path, recs: list[dict[str, object]]) -> None:
    Path(f"{base}.metrics.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_latency_json_counts_the_delivered_operator_messages(tmp_path: Path):
    base = tmp_path / "S002"
    recs = [tool(T0), tool(T0 + 5), told(T0 + 5.2, "m1", "m2"), tool(T0 + 9), told(T0 + 20.2, "m3")]
    write_session(base, [*recs, tool(T0 + 30, operator=["m4"])])  # a tool record's field: not one
    logs: list[str] = []
    summary = session_summary.write_latency("S002", base, T0, T0 + 60, tmp_path, logs.append)
    assert summary is not None, logs
    assert json.loads(Path(f"{base}.latency.json").read_text())["operator_messages"] == 3


def test_a_session_without_operator_messages_counts_0(tmp_path: Path):
    base = tmp_path / "S001"
    write_session(base, [tool(T0), tool(T0 + 5)])
    assert session_summary.write_latency("S001", base, T0, T0 + 60, tmp_path, print) is not None
    assert json.loads(Path(f"{base}.latency.json").read_text())["operator_messages"] == 0
