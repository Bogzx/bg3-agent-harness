"""BaselineClient: the runner's Client protocol (runner/clients.py `Client`) for the baseline agents, so a series
runs them like any model client and its cost, token and event accounting sees them the same way.

The model slug picks the agent: `baseline:donothing`, or `baseline:random:<seed>`. The bench does not import the
runner; the runner passes its own `Event` class as `event` (the default is a stand-in with the same fields).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parents[3]  # packages/bench
MCP_CONFIG = BENCH_DIR.parents[1] / "play" / ".mcp.json"
MODEL_RE = re.compile(r"^baseline:(?:(?P<nothing>donothing)|random:(?P<seed>\d+))$")
MODULES = {"donothing": "bg3bench.baselines.donothing", "random": "bg3bench.baselines.random_agent"}
TOKENS = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


@dataclass
class Event:
    """Stand-in for runner.clients.Event (the fields a baseline fills)."""

    kind: str
    text: str = ""
    tool: str = ""
    args: dict | None = None
    call_id: str = ""
    is_error: bool = False
    session_id: str | None = None
    model: str | None = None
    tools: int | None = None
    subtype: str | None = None
    turns: int | None = None
    duration_ms: float | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


def agent_of(model: str) -> tuple[str, int | None]:
    m = MODEL_RE.match(model or "")
    if m is None:
        raise ValueError(f"not a baseline model {model!r}: baseline:donothing or baseline:random:<seed>")
    return ("donothing", None) if m.group("nothing") else ("random", int(m.group("seed")))


class BaselineClient:
    name = "baseline"
    supports_resume = False
    assigns_session = True
    no_tools = "no model: the agent process calls only its allowlist of bg3 tools (baselines/*.py ALLOWED)"

    def __init__(self, mcp_config: Path | str = MCP_CONFIG, event: Any = Event, uv: str = "uv"):
        self.mcp_config, self.event, self.uv = Path(mcp_config), event, uv
        self.session: str | None = None
        self.model: str | None = None

    def write_config(self, req: Any) -> list[Path]:
        return []

    def env(self, req: Any) -> dict:
        return {}

    def command(self, req: Any) -> list[str]:
        agent, seed = agent_of(req.model)
        if req.resume:
            raise ValueError("a baseline run cannot be resumed")
        self.session, self.model = req.session, req.model
        cmd = [self.uv, "run", "--project", str(BENCH_DIR), "--extra", "baselines", "python", "-m", MODULES[agent]]
        cmd += ["--mcp-config", str(self.mcp_config)]
        return cmd if seed is None else [*cmd, "--seed", str(seed)]

    def parse(self, line: str) -> list[Any]:
        try:
            ev = json.loads(line)
        except ValueError:
            return []
        if not isinstance(ev, dict):
            return []
        sid, t = self.session, ev.get("type")
        if t == "init":
            return [self.event("init", session_id=sid, model=self.model, tools=ev.get("tools"))]
        if t == "call":
            return [
                self.event("tool_call", tool=ev["tool"], args=ev.get("args") or {}, call_id=ev["id"], session_id=sid)
            ]
        if t == "tool_result":
            return [
                self.event(
                    "tool_result",
                    text=ev.get("text", ""),
                    call_id=ev["id"],
                    is_error=bool(ev.get("is_error")),
                    session_id=sid,
                )
            ]
        if t == "result":
            return [
                self.event(
                    "result",
                    subtype=ev.get("subtype"),
                    session_id=sid,
                    turns=ev.get("calls"),
                    duration_ms=ev.get("duration_ms"),
                    cost_usd=float(ev.get("cost_usd") or 0.0),
                    tokens=dict(TOKENS),
                )
            ]
        return []
