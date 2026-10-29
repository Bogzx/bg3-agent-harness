#!/usr/bin/env python3
"""A stub model for the pi conformance suite: OpenAI chat completions over SSE, or Anthropic's Messages API over
SSE for a request to .../messages; standard library only.

    python3 stub_llm.py <script.json> <request log.jsonl> <port file>

It listens on 127.0.0.1 (a free port, written to the port file), or on $STUB_LLM_BIND (host:port; the container
sandbox smoke, tools/container/sandbox_smoke.py). The step of a request is the number of assistant
messages in it, so a retried request gets the same step again. The script is a JSON list of steps, the last one
repeating; a step is one of
    {"tool_calls": [{"name": "mcp__bg3__press_key", "arguments": {...} | "<raw string>"}, ...]}
    {"text": "..."}
    {"error": 529, "body": {...}}            an HTTP error
and may add "usage": {...} (merged into the final chunk's usage) or "delay": <s> (before answering). The final
chunk carries OpenRouter-style usage with `cost`, plus `provider`, like OpenRouter's streams. A Messages API answer
carries Anthropic's usage instead (token counts, no cost: ANTHROPIC_USAGE, "usage" merged into it) on message_start
and message_delta, the way Anthropic streams it. Every request (headers + body) is appended to the log.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PROVIDER = "StubProvider"
COST = 0.0125  # billed USD per response
ANTHROPIC_USAGE = {
    "input_tokens": 1000,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 8000,
    "output_tokens": 50,
}


def chunk(cid: str, delta: dict, finish: str | None = None) -> bytes:
    body = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def events(step: dict, n: int) -> list[bytes]:
    cid = f"gen-stub-{n}"
    out = [chunk(cid, {"role": "assistant", "content": ""})]
    if "text" in step:
        out.append(chunk(cid, {"content": step["text"]}))
    calls = step.get("tool_calls") or []
    for i, c in enumerate(calls):
        args = c.get("arguments", {})
        out.append(
            chunk(
                cid,
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": f"call_{n}_{i}",
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": args if isinstance(args, str) else json.dumps(args),
                            },
                        }
                    ]
                },
            )
        )
    out.append(chunk(cid, {}, "tool_calls" if calls else "stop"))
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "total_tokens": 1050,
        "prompt_tokens_details": {"cached_tokens": 800},
        "cost": COST,
        **(step.get("usage") or {}),
    }
    final = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [],
        "usage": usage,
        "provider": PROVIDER,
    }
    out += [f"data: {json.dumps(final)}\n\n".encode(), b"data: [DONE]\n\n"]
    return out


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n".encode()


def anthropic_events(step: dict, n: int) -> list[bytes]:
    """One Messages API stream: message_start (the input side of the usage), a block per text or tool call,
    message_delta (the final usage and the stop reason), message_stop."""
    usage = {**ANTHROPIC_USAGE, **(step.get("usage") or {})}
    start = {k: v for k, v in usage.items() if k != "output_tokens"} | {"output_tokens": 1}
    msg = {
        "id": f"msg_stub_{n}",
        "type": "message",
        "role": "assistant",
        "model": "stub",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": start,
    }
    out = [sse("message_start", {"message": msg})]
    blocks: list[tuple[dict, dict]] = []
    if "text" in step:
        blocks.append(({"type": "text", "text": ""}, {"type": "text_delta", "text": step["text"]}))
    calls = step.get("tool_calls") or []
    for i, c in enumerate(calls):
        args = c.get("arguments", {})
        blocks.append(
            (
                {"type": "tool_use", "id": f"toolu_{n}_{i}", "name": c["name"], "input": {}},
                {"type": "input_json_delta", "partial_json": args if isinstance(args, str) else json.dumps(args)},
            )
        )
    for i, (block, delta) in enumerate(blocks):
        out += [
            sse("content_block_start", {"index": i, "content_block": block}),
            sse("content_block_delta", {"index": i, "delta": delta}),
            sse("content_block_stop", {"index": i}),
        ]
    stop = {"stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None}
    return [*out, sse("message_delta", {"delta": stop, "usage": usage}), sse("message_stop", {})]


def serve(script: list[dict], log: Path, port_file: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with open(log, "a") as f:
                f.write(
                    json.dumps({"t": time.time(), "path": self.path, "headers": dict(self.headers), "body": body})
                    + "\n"
                )
            n = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant")
            step = script[min(n, len(script) - 1)]
            time.sleep(float(step.get("delay") or 0))
            if "error" in step:
                data = json.dumps(step.get("body") or {"error": {"message": f"stub error {step['error']}"}}).encode()
                self.send_response(int(step["error"]))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for e in (anthropic_events if urlparse(self.path).path.endswith("/messages") else events)(step, n):
                self.wfile.write(e)
                self.wfile.flush()

    host, _, port = (os.environ.get("STUB_LLM_BIND") or "127.0.0.1:0").rpartition(":")
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    port_file.write_text(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    serve(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), Path(sys.argv[3]))
