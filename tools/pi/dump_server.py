#!/usr/bin/env python3
"""What the bg3 server gives a client, dumped offline (no game): the system text pi gets (D2 in
docs/OVERHAUL-PLAN.md: for now the server's instructions with the rules, i.e. what BG3AI_SERVE_RULES=1 serves) and
the tool list (`tools/list`, wire form). Runs in the bridge's env:

    uv run --project bridge python tools/pi/dump_server.py --system <file> [--tools <file>]

The conformance stub MCP server serves tools/pi/conformance/tools.json, made with --tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--system", type=Path, help="write the system text here")
    ap.add_argument("--tools", type=Path, help="write the tools/list result here (JSON)")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="bg3ai-dump-") as rt:
        os.environ["BG3AI_RUNTIME"] = rt  # importing the server must not touch the real runtime/
        os.environ["BG3AI_SERVE_RULES"] = "1"
        from bg3ai import rules, server

        if a.system:
            a.system.write_text(rules.instructions(server.INSTRUCTIONS, server.RULES.rules) + "\n", encoding="utf-8")
        if a.tools:
            # the wire form, as tools/list sends it
            tools = [
                t.model_dump(mode="json", by_alias=True, exclude_none=True)
                for t in asyncio.run(server.mcp.list_tools())
            ]
            a.tools.write_text(json.dumps(tools, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
