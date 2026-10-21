# pyright: basic
"""What both baseline agents share: the tool session they play through, the JSONL they print, the run loop's clock.

The agents speak MCP to the bg3 server over stdio, launched from play/.mcp.json the way the model's client launches
it (`command` + `args` + `env`, cwd = the config's folder). They print one JSON object per line on stdout:

    {"type": "init", "agent": "random", "seed": 7, "tools": 94}
    {"type": "call", "id": "c1", "tool": "look", "args": {}}
    {"type": "tool_result", "id": "c1", "is_error": false, "text": "<first 400 chars>"}
    {"type": "result", "subtype": "success", "calls": 12, "duration_ms": 1830000.0, "cost_usd": 0.0}

client.BaselineClient.parse turns them into the runner's events. A call to a tool outside the agent's allowlist
raises: a baseline never loads, saves or reads memory or the wiki.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

RESULT_CHARS = 400
DEADLINE_ENV = "BG3AI_SESSION_DEADLINE"  # the session env the runner sets (bridge server.DEADLINE)
SERVER = "bg3"


class ToolSession(Protocol):
    async def list_tools(self) -> list[str]: ...
    async def call(self, tool: str, args: dict) -> tuple[str, bool]: ...


class McpSession:
    """A ToolSession over an `mcp.Client` (connected: `async with mcp.Client(...) as c`)."""

    def __init__(self, client: Any):
        self.client = client

    async def list_tools(self) -> list[str]:
        return [t.name for t in (await self.client.list_tools()).tools]

    async def call(self, tool: str, args: dict) -> tuple[str, bool]:
        res = await self.client.call_tool(tool, args)
        return "\n".join(getattr(c, "text", "") for c in res.content), bool(res.is_error)


class Player:
    """Calls tools through a session, only those allowed, and prints the JSONL."""

    def __init__(self, session: ToolSession, allowed: Sequence[str], out: IO[str]):
        self.session, self.allowed, self.out = session, frozenset(allowed), out
        self.calls = 0

    def emit(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        self.out.flush()

    async def call(self, tool: str, args: dict | None = None) -> tuple[str, bool]:
        if tool not in self.allowed:
            raise PermissionError(f"baseline may not call {tool!r}")
        self.calls += 1
        cid = f"c{self.calls}"
        self.emit({"type": "call", "id": cid, "tool": tool, "args": args or {}})
        text, err = await self.session.call(tool, args or {})
        self.emit({"type": "tool_result", "id": cid, "is_error": err, "text": text[:RESULT_CHARS]})
        return text, err


Sleep = Callable[[float], Awaitable[None]]


def server_params(mcp_config: str | Path, env: dict[str, str] | None = None) -> Any:
    """The bg3 stdio server of an .mcp.json as mcp StdioServerParameters (the process env + the config's env)."""
    from mcp import StdioServerParameters

    path = Path(mcp_config).resolve()
    spec = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER]
    return StdioServerParameters(
        command=spec["command"],
        args=list(spec.get("args") or []),
        env={**(os.environ if env is None else env), **(spec.get("env") or {})},
        cwd=path.parent,
    )


def parser(description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--mcp-config", required=True, help="the .mcp.json whose bg3 server to launch (play/.mcp.json)")
    ap.add_argument("--deadline", type=float, help=f"epoch seconds to stop at (default: ${DEADLINE_ENV})")
    ap.add_argument("--no-session-start", action="store_true", help="skip the session_start call")
    return ap


def deadline_of(args: argparse.Namespace, env: dict[str, str]) -> float:
    deadline = args.deadline if args.deadline is not None else float(env.get(DEADLINE_ENV) or 0)
    if deadline <= 0:
        raise SystemExit(f"no deadline: pass --deadline or set {DEADLINE_ENV}")
    return deadline


def serve(
    play: Callable[[ToolSession, Player, float], Awaitable[None]],
    args: argparse.Namespace,
    allowed: Sequence[str],
    out: IO[str] = sys.stdout,
) -> int:  # pragma: no cover - the process entry point; tests drive `play` with a stub server
    from mcp import Client

    deadline = deadline_of(args, dict(os.environ))

    async def main() -> None:
        async with Client(server_params(args.mcp_config)) as client:
            session = McpSession(client)
            await play(session, Player(session, allowed, out), deadline)

    asyncio.run(main())
    return 0


def wall_clock() -> float:
    return time.time()
