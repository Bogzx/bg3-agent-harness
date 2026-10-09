#!/usr/bin/env python3
"""Live dashboard for watching the AI play BG3 (stdlib-only HTTP server plus one static page).

    python3 runner/dashboard/serve.py --port 8765                      # screen as raw PNG (no Pillow)
    uv run --project bridge python runner/dashboard/serve.py --port 8765   # screen as small JPEGs

Shows the game screen (for the human only, never sent to the agent), the live status and party,
the session clock, the agent's tool calls (runtime/actions.jsonl), live latency (idle gaps, think time, APM, round trips;
bridge/src/bg3ai/metrics.py), story progress per hour with a stuck flag
(bridge/src/bg3ai/progress.py) and its own text
(runtime/sessions/<playthrough>/Sxxx.log), visible game events, and the playthrough memory
(HANDOFF.md, objectives.md, the session log tail).

Read-only toward the game: only state.status / events.since / sys.info RPCs and screen captures. Captures
share the Mesa layer's single control slot through the file lock in bridge/src/bg3ai/screen.py, and run
only while someone has the page open.

Endpoints: /  /api/state  /api/feed?after=N  /api/agentlog?after=N&path=ID  /api/screen  /api/latency  /api/progress  /api/goals
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "bridge" / "src"))
import feeds as F  # noqa: E402

RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
VIEWER_IDLE_S = 30  # stop capturing/polling the game when nobody has looked for this long
EVENTS_KEEP = 150


class Hub:
    """Shared caches filled by background pollers and read by request handlers."""

    def __init__(self, opt: argparse.Namespace):
        self.opt = opt
        self.lock = threading.Lock()
        self.last_view = 0.0
        self.game: dict = {"reachable": False, "error": "not polled yet"}
        self.events: list[dict] = []
        self.event_seq = None
        self.screen: dict = {"enabled": not opt.no_screen, "error": None, "t": None, "mime": None}
        self.frame: bytes | None = None
        self.stop = threading.Event()

    def viewed(self) -> None:
        self.last_view = time.time()

    def watching(self) -> bool:
        return time.time() - self.last_view < VIEWER_IDLE_S

    # ---------------------------------------------------------------- game poller

    def game_loop(self) -> None:
        game = None
        ops: set[str] = set()
        ops_t = 0.0
        while not self.stop.is_set():
            if not self.watching():
                self.stop.wait(1.0)
                continue
            delay = self.opt.status_interval
            try:
                if game is None:
                    from bg3ai.game import Game  # noqa: PLC0415
                    game = Game(port=self.opt.debug_port)
                if time.time() - ops_t > 30:
                    info = game.rpc("sys.info", context="server", timeout=5.0)
                    ops, ops_t = set(info.get("ops") or []), time.time()
                st = game.rpc("state.status", context="server", timeout=6.0) if "state.status" in ops else None
                evs = self._poll_events(game) if "events.since" in ops else []
                with self.lock:
                    self.game = {"reachable": True, "error": None, "t": time.time(),
                                 "status": F.shape_status(st) if isinstance(st, dict) else None,
                                 "note": None if st is not None else "state.status not available yet"}
                    if evs:
                        self.events.extend(evs)
                        self.events = self.events[-EVENTS_KEEP:]
            except Exception as e:  # noqa: BLE001 - the dashboard must survive anything the game does
                with self.lock:
                    self.game = {"reachable": False, "error": f"{type(e).__name__}: {e}"[:300], "t": time.time()}
                if game is not None:
                    try:
                        game.close()
                    except Exception:  # noqa: BLE001
                        pass
                game, ops_t = None, 0.0
                delay = max(delay, 10.0)
            self.stop.wait(delay)

    def _poll_events(self, game) -> list[dict]:
        if self.event_seq is None:  # start from "now"; history before the dashboard opened is skipped
            res = game.rpc("events.since", {"seq": 10**12, "limit": 1}, "server", timeout=5.0)
            self.event_seq = res.get("latest", 0)
            return []
        res = game.rpc("events.since", {"seq": self.event_seq, "limit": 100}, "server", timeout=5.0)
        evs = res.get("events") or []
        if evs:
            self.event_seq = evs[-1].get("seq", self.event_seq)
        elif res.get("latest", 0) < self.event_seq:
            self.event_seq = res.get("latest", 0)  # ring buffer reset (reload)
        return [{"t": e.get("t"), "kind": e.get("kind"), "text": F.event_text(e)} for e in evs]

    # ---------------------------------------------------------------- screen poller

    def screen_loop(self) -> None:
        try:
            from bg3ai.screen import ScreenCapture  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self.screen.update(enabled=False, error=f"screen capture unavailable: {e}")
            return
        try:
            import PIL  # noqa: F401, PLC0415
            have_pil = True
        except ImportError:
            have_pil = False
        interval = self.opt.screen_interval if have_pil else max(self.opt.screen_interval, 15.0)
        sc = ScreenCapture(RUNTIME / "shots")
        while not self.stop.is_set():
            if not self.watching():
                self.stop.wait(1.0)
                continue
            delay = interval
            try:
                if have_pil:
                    jpg, _ = sc.capture(max_width=self.opt.screen_width, quality=70)
                    data, mime = jpg, "image/jpeg"
                else:
                    path = sc.capture_png()
                    data, mime = path.read_bytes(), "image/png"
                    path.unlink(missing_ok=True)
                with self.lock:
                    self.frame = data
                    self.screen.update(error=None, t=time.time(), mime=mime, bytes=len(data),
                                       note=None if have_pil else "Pillow missing: full-size PNG every 15 s "
                                                                  "(run via `uv run --project bridge`)")
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.screen.update(error=f"{type(e).__name__}: {e}"[:200])
                delay = max(delay, 15.0)
            self.stop.wait(delay)

    # ---------------------------------------------------------------- snapshot for /api/state

    def state(self) -> dict:
        runner = F.read_json(RUNTIME / "runner.status.json")
        session_file = F.read_json(RUNTIME / "session.json")
        mem = F.resolve_memory(REPO, runner, self.opt.memory_dir)
        mem_state = F.read_json(mem / ".session.json")
        agent_log = F.resolve_agent_log(RUNTIME, runner)
        with self.lock:
            game = dict(self.game)
            events = list(self.events[-40:])
            screen = dict(self.screen)
        return {
            "now": time.time(),
            "playthrough": runner.get("playthrough") or os.environ.get("BG3AI_PLAYTHROUGH"),
            "runner": {k: runner.get(k) for k in ("session", "started", "deadline", "hard_stop", "runner_pid",
                                                   "run_end", "updated", "claude_session", "check_all") if k in runner},
            "clock": F.session_clock(runner, session_file, mem_state),
            "game": game,
            "events": events,
            "screen": screen,
            "memory": F.memory_view(mem),
            "agent_log": {"path_id": agent_log.name if agent_log else None},
        }


def make_handler(hub: Hub):
    index_html = (HERE / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "bg3ai-dashboard/1"

        def log_message(self, fmt, *args):  # quiet
            if hub.opt.verbose:
                super().log_message(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                after = int(q.get("after", ["-1"])[0])
            except ValueError:
                after = -1
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, index_html, "text/html; charset=utf-8")
                if url.path == "/api/state":
                    hub.viewed()
                    return self._json(hub.state())
                if url.path == "/api/feed":
                    off, text, reset = F.read_new(RUNTIME / "actions.jsonl", after)
                    return self._json({"offset": off, "reset": reset, "items": F.parse_actions(text)})
                if url.path == "/api/agentlog":
                    runner = F.read_json(RUNTIME / "runner.status.json")
                    log = F.resolve_agent_log(RUNTIME, runner)
                    if log is None:
                        return self._json({"offset": 0, "reset": True, "text": "", "path_id": None})
                    if q.get("path", [None])[0] != log.name:
                        after = -1  # a new session log: start over near its end
                    off, text, reset = F.read_new(log, after, initial_bytes=32 * 1024)
                    return self._json({"offset": off, "reset": reset, "text": text, "path_id": log.name})
                if url.path == "/api/latency":
                    runner = F.read_json(RUNTIME / "runner.status.json")
                    return self._json(F.latency_payload(RUNTIME, runner))
                if url.path == "/api/progress":
                    runner = F.read_json(RUNTIME / "runner.status.json")
                    return self._json(F.progress_payload(RUNTIME, runner))
                if url.path == "/api/goals":
                    runner = F.read_json(RUNTIME / "runner.status.json")
                    return self._json(F.goals_payload(RUNTIME, runner))
                if url.path == "/api/screen":
                    hub.viewed()
                    with hub.lock:
                        frame, mime = hub.frame, hub.screen.get("mime")
                    if not frame:
                        return self._send(204, b"", "text/plain")
                    return self._send(200, frame, mime or "image/jpeg")
                return self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to watch from the LAN)")
    ap.add_argument("--no-screen", action="store_true", help="don't capture game frames")
    ap.add_argument("--screen-interval", type=float, default=5.0)
    ap.add_argument("--screen-width", type=int, default=1280)
    ap.add_argument("--status-interval", type=float, default=3.0)
    ap.add_argument("--memory-dir", help="default: playthroughs/<runner playthrough>/memory, else play/memory")
    ap.add_argument("--debug-port", type=int, default=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
    ap.add_argument("--verbose", action="store_true")
    opt = ap.parse_args(argv)

    hub = Hub(opt)
    threading.Thread(target=hub.game_loop, daemon=True, name="game").start()
    if not opt.no_screen:
        threading.Thread(target=hub.screen_loop, daemon=True, name="screen").start()
    srv = ThreadingHTTPServer((opt.host, opt.port), make_handler(hub))
    srv.daemon_threads = True
    print(f"dashboard: http://{opt.host}:{opt.port}/  (runtime {RUNTIME})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
