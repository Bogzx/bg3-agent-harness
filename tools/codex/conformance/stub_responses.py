#!/usr/bin/env python3
"""A stub OpenAI Responses-API model for the codex conformance check: POST /v1/responses over SSE, standard
library only.

    python3 stub_responses.py <script.json> <request log.jsonl> <port file>

It listens on 127.0.0.1 (a free port, written to the port file). The step of a request is the number of
function_call_output items in its input, so a retried request gets the same step again. The script is a JSON list
of steps, the last one repeating; a step is
    {"tool_calls": [{"name": "<a name from the request's tools>", "arguments": {...}}, ...]}
    {"text": "..."}
and may add "usage": {...} (replaces the response's usage). Every request body is appended to the log.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

USAGE = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 400},
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 10},
    "total_tokens": 1050,
}


def sse(kind: str, body: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **body})}\n\n".encode()


def items(step: dict, n: int) -> list[dict]:
    out = []
    if "text" in step:
        text = [{"type": "output_text", "text": step["text"], "annotations": []}]
        out.append({"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed", "content": text})
    for i, c in enumerate(step.get("tool_calls") or []):
        out.append(
            {
                "type": "function_call",
                "id": f"fc_{n}_{i}",
                "call_id": f"call_{n}_{i}",
                "name": c["name"],
                **({"namespace": c["namespace"]} if c.get("namespace") else {}),
                "arguments": json.dumps(c.get("arguments", {})),
                "status": "completed",
            }
        )
    return out


def events(step: dict, n: int) -> list[bytes]:
    rid = f"resp_stub_{n}"
    out = [sse("response.created", {"response": {"id": rid, "object": "response", "status": "in_progress"}})]
    for i, item in enumerate(items(step, n)):
        out.append(sse("response.output_item.added", {"output_index": i, "item": item}))
        out.append(sse("response.output_item.done", {"output_index": i, "item": item}))
    done = {"id": rid, "object": "response", "status": "completed", "usage": step.get("usage") or USAGE}
    out.append(sse("response.completed", {"response": done}))
    return out


def main(script_path: str, log_path: str, port_path: str) -> None:
    script = json.loads(Path(script_path).read_text())
    log = Path(log_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with log.open("a") as f:
                f.write(json.dumps({"path": self.path, "body": body}) + "\n")
            if not self.path.endswith("/responses"):
                self.send_error(404)
                return
            n = sum(1 for x in body.get("input") or [] if x.get("type") == "function_call_output")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in events(script[min(n, len(script) - 1)], n):
                self.wfile.write(chunk)
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Path(port_path).write_text(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    main(*sys.argv[1:4])
