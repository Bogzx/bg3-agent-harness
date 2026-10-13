#!/usr/bin/env python3
"""Smoke test of the bg3 MCP server over streamable HTTP, from inside the sandbox container (or anywhere).

    python3 mcp_smoke.py http://172.30.99.1:8770/mcp [--no-egress-check]

Connects, lists the tools, calls a few harmless ones (memory_list of knowledge/, a refused path, session_clock),
then checks the sandbox network: the internet must be unreachable. Exit 0 when all of that holds.
"""

from __future__ import annotations

import asyncio
import socket
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def check(url: str) -> list[str]:
    problems = []
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            print(f"{len(names)} tools; memory/session tools: "
                  + ", ".join(n for n in names if n.startswith(("memory_", "session_", "end_session"))))
            for want in ("memory_read", "memory_write", "session_start", "end_session", "get_status"):
                if want not in names:
                    problems.append(f"tool {want} missing")
            out = text(await s.call_tool("memory_list", {"path": "knowledge"}))
            print("memory_list(knowledge):", out.splitlines()[-1] if out else "(empty)")
            out = text(await s.call_tool("memory_read", {"path": "memory/.git/config"}))
            print("memory_read(memory/.git/config):", out.splitlines()[0] if out else "(empty)")
            if "REFUSED" not in out:
                problems.append("memory/.git/config was not refused")
            print("session_clock:", (text(await s.call_tool("session_clock", {})).splitlines() or [""])[0])
    return problems


def egress_blocked(host: str = "1.1.1.1", port: int = 443) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return False
    except OSError:
        return True


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = args[0] if args else "http://127.0.0.1:8770/mcp"
    problems = asyncio.run(check(url))
    if "--no-egress-check" not in sys.argv:
        blocked = egress_blocked()
        print("internet from here:", "blocked" if blocked else "REACHABLE")
        if not blocked:
            problems.append("the container reaches the internet (use the --internal network)")
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else "FAILED")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
