# pyright: basic
"""The per-tool fairness probe table (packages/bench/fairness_probes.json, research/arch/bench.md 2.5).

Every tool of the bg3 server has a row: `{"tool", "probes": [{"args", "hidden", "expect"}]}` or
`{"tool", "exempt": "<reason>"}`. A probe's args may name `{hidden}` (one of the start save's hidden facts of the
probe's kind, listed by whoever makes the save) and `{visible}` (a handle in view). `expect` is `absent` (the hidden
thing's name and handle do not appear in the result) or `refusal` (the call is refused). This module loads and
checks the table and expands a probe for one hidden fact; the live runner that calls the tools comes later.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA = 1
TABLE = Path(__file__).resolve().parents[2] / "fairness_probes.json"
EXPECTS = ("absent", "refusal")
PLACEHOLDERS = ("{hidden}", "{visible}")


def load(path: str | Path = TABLE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if errs := check(data):
        raise ValueError(f"{path}: " + "; ".join(errs))
    return data


def check(data: Any, tools: Iterable[str] | None = None) -> list[str]:
    """The table's problems (empty: fine). With `tools` (the server's tool names), also every tool covered and
    no row for a tool the server doesn't have."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return [f"not a v{SCHEMA} probe table"]
    kinds = data.get("hidden_kinds")
    if not isinstance(kinds, dict) or not kinds:
        return ["hidden_kinds must be a non-empty object"]
    errs, seen = [], []
    for i, row in enumerate(data.get("tools") or []):
        tool = row.get("tool") if isinstance(row, dict) else None
        if not isinstance(tool, str) or not tool:
            errs.append(f"row {i}: no tool name")
            continue
        seen.append(tool)
        has_probes, has_exempt = "probes" in row, "exempt" in row
        if has_probes == has_exempt or set(row) - {"tool", "probes", "exempt"}:
            errs.append(f"{tool}: give probes or exempt, not both or neither")
        elif has_exempt and not (isinstance(row["exempt"], str) and row["exempt"].strip()):
            errs.append(f"{tool}: exempt needs a reason")
        elif has_probes:
            if not isinstance(row["probes"], list) or not row["probes"]:
                errs.append(f"{tool}: probes must be a non-empty list")
                continue
            for j, p in enumerate(row["probes"]):
                if not isinstance(p, dict) or set(p) != {"args", "hidden", "expect"}:
                    errs.append(f"{tool} probe {j}: needs exactly args, hidden, expect")
                elif not isinstance(p["args"], dict):
                    errs.append(f"{tool} probe {j}: args must be an object")
                elif p["hidden"] not in kinds:
                    errs.append(f"{tool} probe {j}: unknown hidden kind {p['hidden']!r}")
                elif p["expect"] not in EXPECTS:
                    errs.append(f"{tool} probe {j}: expect must be one of {', '.join(EXPECTS)}")
                elif p["expect"] == "refusal" and "{hidden}" not in json.dumps(p["args"]):
                    errs.append(f"{tool} probe {j}: a refusal probe must aim at {{hidden}}")
    if dupes := sorted({t for t in seen if seen.count(t) > 1}):
        errs.append(f"tools listed twice: {', '.join(dupes)}")
    if tools is not None:
        tools = set(tools)
        if missing := sorted(tools - set(seen)):
            errs.append(f"tools without a probe or an exemption: {', '.join(missing)}")
        if extra := sorted(set(seen) - tools):
            errs.append(f"rows for tools the server doesn't have: {', '.join(extra)}")
    return errs


def expand(args: Any, hidden: str, visible: str = "") -> Any:
    """A probe's args with the placeholders filled in (strings anywhere inside, JSON-in-string plans included)."""
    if isinstance(args, dict):
        return {k: expand(v, hidden, visible) for k, v in args.items()}
    if isinstance(args, list):
        return [expand(v, hidden, visible) for v in args]
    if isinstance(args, str):
        return args.replace("{hidden}", hidden).replace("{visible}", visible)
    return args
