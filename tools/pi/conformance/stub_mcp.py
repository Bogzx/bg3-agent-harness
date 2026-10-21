#!/usr/bin/env python3
"""A stub bg3 MCP server for the pi conformance suite: raw JSON-RPC over stdio, no game, standard library only.

It serves tools.json (the real server's tools/list, dumped with tools/pi/dump_server.py) and runs each call in a
thread like the real server (mcp runs sync tools in worker threads), so parallel calls from the client would
overlap. Every message received and every call's start and end go to $STUB_MCP_LOG (JSONL, with times), which the
tests read. Replies:
- screenshot: an image (the canary PNG) and no text;
- press_key with key "boom": an isError result;
- wait with `seconds`: sleeps that long (the timeout check);
- the tool named by $STUB_MCP_REFUSE: $STUB_MCP_REFUSAL as a normal result (the server's stop refusal);
- anything else: "ok <tool> <args>" after $STUB_MCP_DELAY seconds (default 0.3);
every text result ends with a clock line, like the real server's.
$STUB_MCP_TOOLS caps how many tools are listed (the tool-count check).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = json.loads((HERE / "tools.json").read_text())
CLOCK = "[clock] 41:12 left of 45:00"
INSTRUCTIONS = "STUB-INSTRUCTIONS-CANARY"
# A 2x2 red PNG; the tests look for its base64 in the model request.
CANARY_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg==")

LOG = os.environ.get("STUB_MCP_LOG")
DELAY = float(os.environ.get("STUB_MCP_DELAY") or 0.3)
LIMIT = int(os.environ.get("STUB_MCP_TOOLS") or len(TOOLS))
REFUSE = os.environ.get("STUB_MCP_REFUSE")
_out = threading.Lock()


def log(**rec) -> None:
    if LOG:
        with _out, open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), **rec}) + "\n")


def send(msg: dict) -> None:
    with _out:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call(rid, name: str, args: dict) -> None:
    log(ev="start", tool=name, args=args, id=rid)
    if name == "screenshot":
        time.sleep(DELAY)
        result = {"content": [{"type": "image", "data": CANARY_B64, "mimeType": "image/png"}]}
    elif name == "press_key" and args.get("key") == "boom":
        result = {"content": [{"type": "text", "text": f"press_key failed: boom\n{CLOCK}"}], "isError": True}
    elif name == REFUSE:
        result = {"content": [{"type": "text", "text": f"{os.environ.get('STUB_MCP_REFUSAL')}\n{CLOCK}"}]}
    else:
        time.sleep(float(args["seconds"]) if name == "wait" and "seconds" in args else DELAY)
        text = f"ok {name} {json.dumps(args, sort_keys=True)}\n{CLOCK}"
        result = {"content": [{"type": "text", "text": text}]}
    log(ev="end", tool=name, id=rid)
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def handle(msg: dict) -> None:
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    log(ev="recv", method=method, id=rid, params=params if method != "tools/call" else None)
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}}, "serverInfo": {"name": "bg3", "version": "stub"},
            "instructions": INSTRUCTIONS}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:LIMIT]}})
    elif method == "tools/call":
        threading.Thread(target=call, args=(rid, params.get("name"), params.get("arguments") or {}),
                         daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}})


def main() -> None:
    log(ev="boot", pid=os.getpid())
    for line in sys.stdin:
        if line.strip():
            handle(json.loads(line))
    log(ev="eof")


if __name__ == "__main__":
    main()
