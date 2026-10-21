"""The playthrough app's HTTP server (stdlib): the v1 JSON API, the SSE stream and the built web app.

    uv run --project packages/playthrough bg3play-serve --runtime runtime --playthroughs playthroughs

Endpoints (GET, JSON unless noted, Cache-Control: no-store):
    /api/v1/status                                   runner status
    /api/v1/playthroughs                             every playthrough with sessions and cost
    /api/v1/playthroughs/<pt>/sessions               per session: outcome, minutes, cost, latency, points
    /api/v1/playthroughs/<pt>/watch                  the Watch screen's snapshot
    /api/v1/playthroughs/<pt>/memory/tree            memory files
    /api/v1/playthroughs/<pt>/memory/file?path=P     one file's text and its commits
    /api/v1/playthroughs/<pt>/memory/log             memory commits
    /api/v1/playthroughs/<pt>/timeline               choices and fights
    /api/v1/screenshot                               the latest capture (image)
    /api/v1/stream                                   SSE (text/event-stream); Last-Event-ID header or ?last_id=
    anything else                                    the web app (web/dist), index.html for unknown paths

Read-only. Binds 127.0.0.1 by default; writes (P1) will need a token off localhost.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import api
from .hub import Hub
from .store import RunStore, StoreError

PT_ROUTE = re.compile(r"^/api/v1/playthroughs/([^/]+)/(sessions|watch|memory/tree|memory/file|memory/log|timeline)$")
KEEPALIVE_S = 15.0
DEFAULT_WEB = Path(__file__).resolve().parents[2] / "web" / "dist"


class App:
    def __init__(self, store: RunStore, hub: Hub, web_dir: Path | None, keepalive_s: float = KEEPALIVE_S):
        self.store, self.hub, self.web_dir, self.keepalive_s = store, hub, web_dir, keepalive_s

    def route(self, path: str, q: dict[str, list[str]]) -> dict | None:
        """A JSON API path -> its payload (None: not found). Raises StoreError for a refused request."""
        s = self.store
        if path == "/api/v1/status":
            return api.status(s)
        if path == "/api/v1/playthroughs":
            return api.playthroughs(s)
        m = PT_ROUTE.match(path)
        if not m:
            return None
        pt, what = s.check_name(unquote(m.group(1))), m.group(2)
        if pt not in s.playthroughs():
            return None
        builders: dict[str, Callable[[], dict | None]] = {
            "sessions": lambda: api.sessions(s, pt),
            "watch": lambda: api.watch(s, pt),
            "memory/tree": lambda: api.memory_tree(s, pt),
            "memory/file": lambda: api.memory_file(s, pt, q.get("path", [""])[0]),
            "memory/log": lambda: api.memory_log(s, pt),
            "timeline": lambda: api.timeline(s, pt),
        }
        return builders[what]()

    def static(self, path: str) -> Path | None:
        if self.web_dir is None or not self.web_dir.is_dir():
            return None
        root = self.web_dir.resolve()
        p = (root / unquote(path).lstrip("/")).resolve()
        if p.is_relative_to(root) and p.is_file():
            return p
        index = root / "index.html"
        return index if index.is_file() and "." not in path.rsplit("/", 1)[-1] else None


def make_handler(app: App, verbose: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "bg3play/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            if verbose:
                super().log_message(format, *args)

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/api/v1/stream":
                    return self._stream(self.headers.get("Last-Event-ID") or q.get("last_id", [None])[0])
                if url.path == "/api/v1/screenshot":
                    shot = app.store.latest_screenshot()
                    if shot is None:
                        return self._send(204, b"", "text/plain")
                    ctype = mimetypes.guess_type(shot.name)[0] or "application/octet-stream"
                    return self._send(200, shot.read_bytes(), ctype)
                if url.path.startswith("/api/"):
                    payload = app.route(url.path, q)
                    if payload is None:
                        return self._json({"v": api.API_VERSION, "error": "not found"}, 404)
                    return self._json(payload)
                f = app.static(url.path)
                if f is None:
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                cache = "public, max-age=31536000, immutable" if "/assets/" in url.path else "no-cache"
                return self._send(200, f.read_bytes(), ctype, cache)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except StoreError as e:
                return self._json({"v": api.API_VERSION, "error": str(e)}, 400)

        def _stream(self, last_id: str | None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            timeout = 0.0
            try:
                while not app.hub.closed:
                    events, reset, last_id = app.hub.read(last_id, timeout)
                    chunks = ["event: reset\ndata: {}\n\n"] if reset else []
                    chunks += [f"id: {app.hub.event_id(e.seq)}\nevent: {e.type}\n"
                               f"data: {json.dumps(e.data, ensure_ascii=False, default=str)}\n\n" for e in events]
                    if not chunks and timeout:
                        chunks = [": keepalive\n\n"]
                    if chunks:
                        self.wfile.write("".join(chunks).encode())
                        self.wfile.flush()
                    timeout = app.keepalive_s
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(app: App, host: str, port: int, verbose: bool = False) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app, verbose))
    srv.daemon_threads = True
    return srv


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime", type=Path, default=Path(os.environ.get("BG3AI_RUNTIME", "runtime")))
    ap.add_argument("--playthroughs", type=Path, default=Path("playthroughs"))
    ap.add_argument("--playthrough", help="follow this playthrough (default: the runner's current one)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default localhost only)")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--web", type=Path, default=DEFAULT_WEB, help="the built web app (web/dist)")
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between file checks")
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    opt = parser().parse_args(argv)

    store = RunStore(opt.runtime, opt.playthroughs)
    hub = Hub(store, generation=format(time.time_ns(), "x"), pt=opt.playthrough)
    stop = threading.Event()
    threading.Thread(target=hub.run, args=(stop, opt.poll), daemon=True, name="hub").start()
    srv = serve(App(store, hub, opt.web), opt.host, opt.port, opt.verbose)
    print(f"bg3play: http://{opt.host}:{srv.server_address[1]}/  (runtime {opt.runtime})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        hub.close()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
