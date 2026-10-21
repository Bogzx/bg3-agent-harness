#!/usr/bin/env python3
"""A stub OpenAI-compatible model for the pi conformance suite: chat completions over SSE, standard library only.

    python3 stub_llm.py <script.json> <request log.jsonl> <port file>

It listens on 127.0.0.1 (a free port, written to the port file). The step of a request is the number of assistant
messages in it, so a retried request gets the same step again. The script is a JSON list of steps, the last one
repeating; a step is one of
    {"tool_calls": [{"name": "mcp__bg3__press_key", "arguments": {...} | "<raw string>"}, ...]}
    {"text": "..."}
    {"error": 529, "body": {...}}            an HTTP error
and may add "usage": {...} (merged into the final chunk's usage) or "delay": <s> (before answering). The final
chunk carries OpenRouter-style usage with `cost`, plus `provider`, like OpenRouter's streams. Every request
(headers + body) is appended to the log.
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROVIDER = "StubProvider"
COST = 0.0125   # billed USD per response


def chunk(cid: str, delta: dict, finish: str | None = None) -> bytes:
    body = {"id": cid, "object": "chat.completion.chunk", "created": 0, "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n".encode()


def events(step: dict, n: int) -> list[bytes]:
    cid = f"gen-stub-{n}"
    out = [chunk(cid, {"role": "assistant", "content": ""})]
    if "text" in step:
        out.append(chunk(cid, {"content": step["text"]}))
    calls = step.get("tool_calls") or []
    for i, c in enumerate(calls):
        args = c.get("arguments", {})
        out.append(chunk(cid, {"tool_calls": [{
            "index": i, "id": f"call_{n}_{i}", "type": "function",
            "function": {"name": c["name"], "arguments": args if isinstance(args, str) else json.dumps(args)}}]}))
    out.append(chunk(cid, {}, "tool_calls" if calls else "stop"))
    usage = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
             "prompt_tokens_details": {"cached_tokens": 800}, "cost": COST, **(step.get("usage") or {})}
    final = {"id": cid, "object": "chat.completion.chunk", "created": 0, "model": "stub", "choices": [],
             "usage": usage, "provider": PROVIDER}
    out += [f"data: {json.dumps(final)}\n\n".encode(), b"data: [DONE]\n\n"]
    return out


def serve(script: list[dict], log: Path, port_file: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with open(log, "a") as f:
                f.write(json.dumps({"t": time.time(), "path": self.path, "headers": dict(self.headers),
                                    "body": body}) + "\n")
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
            for e in events(step, n):
                self.wfile.write(e)
                self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file.write_text(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    serve(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), Path(sys.argv[3]))
