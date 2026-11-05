"""Check H4 (human-gap item 4, blocking walks): a walk still going after 90 s returns its progress and job id before
the clients' 110 s tool timeout, over the same MCP transport the bench clients use (streamable HTTP).

    BG3AI_AUTO_SESSION=0 uv run --project bridge bg3ai-mcp --transport http --port 8771 &   # its own server
    uv run --project bridge python tools/dev/live_v010/mcp_walk.py <tool> '<json args>' [url]

Both bench clients get the server with timeout 110000 ms (bg3harness/clients.py http_server: claude's mcp config
and pi's, :141/:420/:528), so one HTTP call checks the limit both of them apply. The call is made with
read_timeout_seconds=110: a result after that is a client timeout (FAIL). Prints the wall time and the result;
PASS when it arrived in < 110 s and, for a walk that was still going, says "still waiting for <id> after 90s" with a
RUNNING line and a wait_for(...) id. Then (same session) wait_for on that id up to 100 s, to show the walk goes on
and ends with its own result. Appended to runtime/probes/v010/calls.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time

from common import rec
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

LIMIT_S = 110.0


def text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])


async def check(url: str, tool: str, args: dict) -> int:
    async with streamable_http_client(url) as streams, ClientSession(streams[0], streams[1]) as s:
        await s.initialize()
        t0 = time.time()
        try:
            out = text(await s.call_tool(tool, args, read_timeout_seconds=LIMIT_S))
        except Exception as e:  # the client's own timeout lands here
            out = f"EXCEPTION {type(e).__name__}: {e}"
        dt = time.time() - t0
        rec("H4-http", f"mcp:{tool}", args, out, t0)
        waiting = "still waiting for" in out
        m = re.search(r"wait_for\('([^']+)'\)", out)
        ok = dt < LIMIT_S and not out.startswith("EXCEPTION")
        print(
            f"H4: {tool} returned in {dt:.1f} s (limit {LIMIT_S:.0f} s): {'PASS' if ok else 'FAIL'}; "
            f"still going at the budget: {waiting}; RUNNING line: {'RUNNING' in out}; id: {m and m.group(1)}"
        )
        if waiting and m:
            t1 = time.time()
            more = text(
                await s.call_tool("wait_for", {"target": m.group(1), "timeout_s": 100}, read_timeout_seconds=LIMIT_S)
            )
            rec("H4-http", "mcp:wait_for", {"target": m.group(1)}, more, t1)
            print(f"H4: wait_for {m.group(1)} returned in {time.time() - t1:.1f} s")
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    tool, args = argv[0], json.loads(argv[1]) if len(argv) > 1 else {}
    url = argv[2] if len(argv) > 2 else "http://127.0.0.1:8771/mcp"
    return asyncio.run(check(url, tool, args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
