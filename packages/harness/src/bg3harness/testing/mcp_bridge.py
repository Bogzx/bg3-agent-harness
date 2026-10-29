"""Start the real bridge (bg3ai-mcp over stdio) against a fake mod and talk to it as an MCP client.

    async with bridge_session(fake.port, tmp_path) as s:
        text = await call(s, "get_status")

Everything the bridge would write (runtime/, play/memory, saves) goes under `home`; background threads that would
reach for the real game or desktop (movie skipper, activity sampler, auto session) are off, and job waits poll instead
of listening for the mod's prints (the fake mod's jobs settle inside the call).
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from bg3ai import tool_classes
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def bridge_env(port: int, home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("BG3AI_", "BG3LE_"))}
    env = {
        **keep,
        "BG3LE_DEBUG_PORT": str(port),
        "BG3AI_RUNTIME": str(home / "runtime"),
        "BG3AI_PLAY_DIR": str(home / "play"),
        "BG3AI_MEMORY_DIR": str(home / "play" / "memory"),
        "BG3AI_SAVE_DIR": str(home / "saves"),
        "BG3AI_PROFILE_DIR": str(home / "profile"),
        "BG3AI_AUTO_SESSION": "0",
        "BG3AI_MOVIE_SKIP": "0",
        "BG3AI_METRICS_SAMPLE_S": "0",
        "BG3AI_JOB_PUSH": "0",
    }
    env.update(extra or {})
    return env


@asynccontextmanager
async def bridge_session(
    port: int, home: Path, extra_env: Mapping[str, str] | None = None
) -> AsyncGenerator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bg3ai.server"], env=bridge_env(port, home, extra_env), cwd=str(home)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, args: dict[str, Any] | None = None) -> str:
    """The tool's text; raises AssertionError when the MCP call itself failed (an exception in the tool). A failure
    the tool reports (FAILED, NOT READY, ...: bg3ai.tool_classes.failed) is an error result too, and is returned."""
    res = await session.call_tool(tool, args or {})
    text = "\n".join(getattr(c, "text", "") for c in res.content)
    if res.is_error and not tool_classes.failed(text):
        raise AssertionError(f"{tool} failed: {text[:500]}")
    return text
