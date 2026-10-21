"""The playthrough app's HTTP server (stdlib): the v1 JSON API, the SSE stream and the built web app.

    uv run --project packages/playthrough bg3play-serve --runtime runtime --playthroughs playthroughs

Endpoints (GET, JSON unless noted, Cache-Control: no-store):
    /api/v1/status                                   runner status
    /api/v1/playthroughs                             every playthrough with sessions and cost
    /api/v1/playthroughs/<pt>/sessions               per session: outcome, minutes, cost, latency, points
    /api/v1/playthroughs/<pt>/watch                  the Watch screen's snapshot
    /api/v1/playthroughs/<pt>/memory/tree            memory files
    /api/v1/playthroughs/<pt>/memory/file?path=P     one file's text and its commits
    /api/v1/playthroughs/<pt>/memory/diff?path=P&sha=S  what commit S changed in P (git show), line by line
    /api/v1/playthroughs/<pt>/memory/log             memory commits
    /api/v1/playthroughs/<pt>/timeline               choices and fights
    /api/v1/playthroughs/<pt>/positions?level=L      breadcrumb tracks (positions.py)
    /api/v1/playthroughs/<pt>/replay/<Sxxx>          one session's events + video segments (replay.py)
    /api/v1/screenshot                               the latest capture (image)
    /api/v1/recorder                                 recorder state (null without --record)
    /api/v1/video/index                              recorded segments (recorder.py index.json)
    /api/v1/video/<file>                             one MP4 segment (HTTP Range)
    /api/v1/screen.mjpg, /api/v1/screen.jpg          the recorder's live view (204 without a frame)
    /api/v1/playthroughs/<pt>/story                  decisions, chapters, overview, party snapshot (story.py)
    /api/v1/live, /api/v1/live/screen                clock, live game, thermal; the latest frame (dashboard.py)
    /api/v1/latency, /api/v1/progress, /api/v1/goals the running session's latency, story progress, goal scorecard
    /api/v1/stream                                   SSE (text/event-stream); Last-Event-ID header or ?last_id=
    /api/state, /api/feed, ... /series               the retired runner/dashboard's paths (dashboard.py)
    anything else                                    the web app (web/dist), index.html for unknown paths

Control (POST, JSON body; needs --supervise): /api/v1/playthroughs/<pt>/start|resume (body: the Launcher's fields,
supervisor.LAUNCH_KEYS), /api/v1/playthroughs/<pt>/stop.
GET /api/v1/supervisor (state + client profiles: client_profiles.py),
GET /api/v1/playthroughs/<pt>/config. Writes are refused cross-origin, and from anywhere but localhost unless the
request carries the --token (Authorization: Bearer). Binds 127.0.0.1 by default.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import api, client_profiles, config, replay
from . import game as gm
from . import positions as pos
from . import recorder as rec
from . import supervisor as sup
from .adapters import legacy_paths
from .dashboard import SERIES_HTML, Dashboard
from .game import GameWatch, ScreenWatch, Viewers
from .hub import Hub
from .jsontypes import is_obj
from .store import RunStore, StoreError

PT_ROUTE = re.compile(
    r"^/api/v1/playthroughs/([^/]+)/(sessions|watch|memory/tree|memory/file|memory/diff|memory/log|timeline|positions|story)$"
)
CONTROL_ROUTE = re.compile(r"^/api/v1/playthroughs/([^/]+)/(start|stop|resume)$")
REPLAY_ROUTE = re.compile(r"^/api/v1/playthroughs/([^/]+)/replay/(S\d{3,})$")
CONFIG_ROUTE = re.compile(r"^/api/v1/playthroughs/([^/]+)/config$")
MAX_BODY = 64 * 1024
VIDEO_FILE = re.compile(r"^/api/v1/video/(rec-\d+-\d{4}\.mp4)$")
KEEPALIVE_S = 15.0
DEFAULT_WEB = Path(__file__).resolve().parents[2] / "web" / "dist"
REPO = Path(__file__).resolve().parents[4]


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """A single "bytes=a-b" / "bytes=a-" / "bytes=-n" Range -> (first, last) inclusive; None = send it all.
    Raises ValueError for an unsatisfiable range."""
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    if not m.group(1):
        n = int(m.group(2))
        if n == 0:
            raise ValueError("empty suffix range")
        return max(0, size - n), size - 1
    first = int(m.group(1))
    last = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if first >= size or last < first:
        raise ValueError("range not satisfiable")
    return first, last


class App:
    def __init__(
        self,
        store: RunStore,
        hub: Hub,
        web_dir: Path | None,
        keepalive_s: float = KEEPALIVE_S,
        recorder: rec.Recorder | None = None,
        video_dir: Path | None = None,
        supervisor: sup.Supervisor | None = None,
        token: str | None = None,
        profiles: Path | None = None,
        dashboard: Dashboard | None = None,
    ):
        self.store, self.hub, self.web_dir, self.keepalive_s = store, hub, web_dir, keepalive_s
        self.dashboard = dashboard or Dashboard(store, None, None, Viewers(time.time))
        self.recorder = recorder
        self.video_dir = video_dir if video_dir is not None else store.runtime / "video"
        self.supervisor, self.token, self.profiles = supervisor, token or None, profiles

    def authorize(self, client_ip: str, headers: Any) -> str | None:
        """Why a write is refused (None: allowed): another site's page (Origin not this host), or a client off
        localhost without the token."""
        origin, host = headers.get("Origin"), headers.get("Host")
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            return "cross-origin write refused"
        if self.token:
            auth = headers.get("Authorization") or ""
            return None if hmac.compare_digest(auth, f"Bearer {self.token}") else "bad or missing token"
        try:
            local = ipaddress.ip_address(client_ip).is_loopback
        except ValueError:
            local = False
        return None if local else "writes need localhost or the server's --token"

    def client_profiles(self) -> list[dict[str, Any]]:
        return client_profiles.read_profiles(self.profiles)

    def control(self, pt: str, what: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST start/resume/stop -> (HTTP code, payload)."""
        v = api.API_VERSION
        s = self.supervisor
        if s is None:
            return 409, {"v": v, "error": "no supervisor: start the server with --supervise"}
        try:
            if what == "stop":
                if s.pt != pt:
                    return 409, {"v": v, "error": f"{pt} is not running"}
                s.stop()
            else:
                cfg = sup.launch_config(config.load(self.store.playthroughs_dir / pt), body)
                if self.recorder is not None:
                    self.recorder.enabled = cfg.recording.enabled
                s.start(pt, resume=what == "resume", cfg=cfg)
        except config.ConfigError as e:
            return 400, {"v": v, "error": str(e)}
        except sup.SupervisorError as e:
            return 409, {"v": v, "error": str(e)}
        return 200, {"v": v, "supervisor": s.view()}

    def route(self, path: str, q: dict[str, list[str]]) -> dict[str, Any] | None:
        """A JSON API path -> its payload (None: not found). Raises StoreError for a refused request."""
        s = self.store
        if (d := self.dashboard.route(path, q)) is not None:
            return d
        if path == "/api/v1/status":
            return api.status(s)
        if path == "/api/v1/playthroughs":
            return api.playthroughs(s)
        if path == "/api/v1/recorder":
            return {"v": api.API_VERSION, "recorder": self.recorder.state() if self.recorder else None}
        if path == "/api/v1/supervisor":
            return {
                "v": api.API_VERSION,
                "supervisor": self.supervisor.view() if self.supervisor else None,
                "profiles": self.client_profiles(),
            }
        if (cm := CONFIG_ROUTE.match(path)) is not None:
            name = s.check_name(unquote(cm.group(1)))
            try:
                cfg = config.to_dict(config.load(s.playthroughs_dir / name))
            except config.ConfigError as e:
                raise StoreError(f"playthrough.toml: {e}") from e
            played = len(s.session_logs(name))
            if self.supervisor is not None:
                played = self.supervisor.sessions_played(name)
            return {"v": api.API_VERSION, "playthrough": name, "config": cfg, "sessions": played}
        if (rm := REPLAY_ROUTE.match(path)) is not None:
            name = s.check_name(unquote(rm.group(1)))
            if name not in s.playthroughs():
                return None
            r = replay.replay(s, name, rm.group(2), self.video_dir)
            return {"v": api.API_VERSION, "playthrough": name, **r} if r else None
        if path == "/api/v1/video/index":
            return {"v": api.API_VERSION, "segments": rec.read_index(self.video_dir)}
        m = PT_ROUTE.match(path)
        if not m:
            return None
        pt, what = s.check_name(unquote(m.group(1))), m.group(2)
        if pt not in s.playthroughs():
            return None
        builders: dict[str, Callable[[], dict[str, Any] | None]] = {
            "sessions": lambda: api.sessions(s, pt),
            "watch": lambda: self.watch(pt),
            "memory/tree": lambda: api.memory_tree(s, pt),
            "memory/file": lambda: api.memory_file(s, pt, q.get("path", [""])[0]),
            "memory/diff": lambda: api.memory_diff(s, pt, q.get("path", [""])[0], q.get("sha", [""])[0]),
            "memory/log": lambda: api.memory_log(s, pt),
            "timeline": lambda: api.timeline(s, pt),
            "positions": lambda: api.positions(s, pt, q.get("level", [""])[0]),
            "story": lambda: self.dashboard.story(pt),
        }
        return builders[what]()

    def watch(self, pt: str) -> dict[str, Any]:
        """The Watch snapshot plus the stream id taken before it was built: reading the stream from that id gives
        every change since (a line may come twice; the client drops repeats)."""
        stream_id = self.hub.last_id()
        return dict(api.watch(self.store, pt), stream_id=stream_id)

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

        def handle(self) -> None:
            with contextlib.suppress(ConnectionResetError):  # a browser dropping an idle keep-alive connection
                super().handle()

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

        def _json(self, obj: dict[str, Any], code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_POST(self) -> None:
            url = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True  # the body stays unread
                return self._json({"v": api.API_VERSION, "error": "bad or too large body"}, 413)
            raw = self.rfile.read(n) if n else b""
            why = app.authorize(self.client_address[0], self.headers)
            if why:
                return self._json({"v": api.API_VERSION, "error": why}, 403)
            m = CONTROL_ROUTE.match(url.path)
            if not m:
                return self._json({"v": api.API_VERSION, "error": "not found"}, 404)
            try:
                body: Any = json.loads(raw) if raw else {}
                if not is_obj(body):
                    raise ValueError("the body is not a JSON object")
                pt = app.store.check_name(unquote(m.group(1)))
            except ValueError as e:  # StoreError is a ValueError
                return self._json({"v": api.API_VERSION, "error": f"bad request: {e}"}, 400)
            code, payload = app.control(pt, m.group(2), body)
            return self._json(payload, code)

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
                if url.path in ("/api/v1/live/screen", "/api/screen"):
                    frame, mime = app.dashboard.frame()
                    return self._send(200, frame, mime or "image/jpeg") if frame else self._send(204, b"", "text/plain")
                if url.path in ("/series", "/series.html"):
                    return self._send(200, SERIES_HTML, "text/html; charset=utf-8")
                if url.path in ("/api/v1/screen.mjpg", "/api/v1/screen.jpg"):
                    return self._screen(url.path.endswith(".mjpg"))
                if (m := VIDEO_FILE.match(url.path)) is not None:
                    return self._file_range(app.video_dir / m.group(1), "video/mp4")
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

        def _file_range(self, path: Path, ctype: str) -> None:
            try:
                size = path.stat().st_size
            except OSError:
                return self._send(404, b"not found", "text/plain")
            try:
                r = byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            first, last = r if r else (0, size - 1)
            self.send_response(206 if r else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(last - first + 1))
            if r:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command == "HEAD":
                return None
            with path.open("rb") as f:
                f.seek(first)
                left = last - first + 1
                while left > 0 and (chunk := f.read(min(left, 256 * 1024))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return None

        def _screen(self, mjpeg: bool) -> None:
            live = app.recorder.live if app.recorder else None
            seq, frame = live.get() if live else (0, None)
            if not mjpeg or frame is None or live is None:
                return self._send(200, frame, "image/jpeg") if frame else self._send(204, b"", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while frame is not None and not live.closed and not app.hub.closed:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                        + frame
                        + b"\r\n"
                    )
                    self.wfile.flush()
                    seq, frame = live.get(seq, app.keepalive_s)
            except (BrokenPipeError, ConnectionResetError):
                return None
            return None

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
                    chunks += [
                        f"id: {app.hub.event_id(e.seq)}\nevent: {e.type}\n"
                        f"data: {json.dumps(e.data, ensure_ascii=False, default=str)}\n\n"
                        for e in events
                    ]
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


def sample_loop(sampler: pos.Sampler, stop: threading.Event, interval: float = 2.0) -> None:
    while not stop.is_set():
        sampler.poll()
        stop.wait(interval)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime", type=Path, default=Path(os.environ.get("BG3AI_RUNTIME", "runtime")))
    ap.add_argument("--playthroughs", type=Path, default=Path("playthroughs"))
    ap.add_argument("--playthrough", help="follow this playthrough (default: the runner's current one)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default localhost only)")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--web", type=Path, default=DEFAULT_WEB, help="the built web app (web/dist)")
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between file checks")
    ap.add_argument("--record", action="store_true", help="record the game while it runs (recorder.py)")
    ap.add_argument("--video", type=Path, help="recording dir (default <runtime>/video)")
    ap.add_argument(
        "--capture",
        choices=("window", "display"),
        default="window",
        help="grab the game window (x11grab -window_id) or the whole display (black under rootless "
        "Xwayland: research/ovh/live1.md 7)",
    )
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":1"))
    ap.add_argument("--encoder", choices=sorted(rec.ENCODER_ARGS), default="h264_nvenc")
    ap.add_argument("--video-cap-gb", type=float, default=20.0, help="delete the oldest segments above this")
    ap.add_argument(
        "--positions",
        action="store_true",
        help="sample party positions from the game's state.status (adapters/bridge_status.py)",
    )
    ap.add_argument(
        "--debug-port",
        type=int,
        default=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")),
        help="the mod's RPC port (--positions, --game)",
    )
    ap.add_argument(
        "--game",
        action="store_true",
        help="poll the game's status and visible events while someone watches /api/v1/live",
    )
    ap.add_argument("--status-interval", type=float, default=3.0, help="seconds between game polls (--game)")
    ap.add_argument(
        "--screen",
        action="store_true",
        help="capture game frames while someone watches (the bridge's ScreenCapture; --record has its own live view)",
    )
    ap.add_argument("--screen-interval", type=float, default=5.0)
    ap.add_argument("--screen-width", type=int, default=1280)
    ap.add_argument(
        "--memory-dir",
        type=Path,
        help="the memory /api/state and /api/story read (default: the runner's playthrough's)",
    )
    ap.add_argument(
        "--supervise",
        action="store_true",
        help="enable start/stop/resume (sessions through adapters/legacy_cli.py: runner/play.py run)",
    )
    ap.add_argument("--repo", type=Path, default=REPO, help="the bg3-ai checkout play.py runs from (--supervise)")
    ap.add_argument(
        "--token",
        default=os.environ.get("BG3PLAY_TOKEN"),
        help="bearer token for writes from off localhost (default $BG3PLAY_TOKEN)",
    )
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    opt = parser().parse_args(argv)

    store = RunStore(opt.runtime, opt.playthroughs)
    hub = Hub(store, generation=format(time.time_ns(), "x"), pt=opt.playthrough)
    stop = threading.Event()
    threading.Thread(target=hub.run, args=(stop, opt.poll), daemon=True, name="hub").start()
    video = opt.video or opt.runtime / "video"
    recorder, rec_thread = None, None
    if opt.record:
        recorder = rec.Recorder(
            rec.RecordSpec(video, encoder=opt.encoder),
            rec.game_probe(opt.display, opt.capture),
            time.time,
            rec.resource_guard(opt.runtime / "thermal.log"),
            cap_bytes=int(opt.video_cap_gb * 1024**3),
        )
        rec_thread = threading.Thread(target=recorder.loop, args=(stop,), daemon=True, name="recorder")
        rec_thread.start()
    if opt.positions:
        from .adapters.bridge_status import BridgeStatus  # temporary; needs the bridge package

        legacy_paths.ensure()

        def path_for() -> Path | None:
            cur = opt.playthrough or store.current()
            return store.positions_path(cur) if cur else None

        sampler = pos.Sampler(
            BridgeStatus(opt.debug_port),
            path_for,
            time.time,
            lambda row: hub.publish("position", dict(row, playthrough=opt.playthrough or store.current())),
        )
        threading.Thread(target=sample_loop, args=(sampler, stop), daemon=True, name="positions").start()
    supervisor = None
    if opt.supervise:
        from .adapters.legacy_cli import LegacyCli  # temporary until the harness's run_session

        supervisor = sup.Supervisor(
            LegacyCli(opt.repo, opt.runtime),
            sup.NoKeeper(),
            opt.runtime,
            opt.playthroughs,
            time.time,
            lambda view: hub.publish("supervisor", view),
            played=lambda pt: len(store.session_logs(pt)),
        )
    viewers = Viewers(time.time)
    game = screen = None
    if opt.game:
        from .adapters.bridge_status import bridge_game  # temporary; needs the bridge package

        legacy_paths.ensure()
        game = GameWatch(lambda: bridge_game(opt.debug_port), viewers, opt.status_interval)
        threading.Thread(target=gm.loop, args=(game, stop, time.time), daemon=True, name="game").start()
    if opt.screen:
        from .adapters.bridge_status import bridge_capture  # temporary; needs the bridge package

        legacy_paths.ensure()
        capture, note, err = bridge_capture(opt.runtime / "shots", opt.screen_width)
        interval = opt.screen_interval if note is None else max(opt.screen_interval, 15.0)
        screen = ScreenWatch(capture, viewers, interval, note=note, error=err)
        if capture is not None:
            threading.Thread(target=gm.loop, args=(screen, stop, time.time), daemon=True, name="screen").start()
    app = App(
        store,
        hub,
        opt.web,
        recorder=recorder,
        video_dir=video,
        supervisor=supervisor,
        token=opt.token,
        profiles=opt.repo / "profiles",
        dashboard=Dashboard(store, game, screen, viewers, memory_dir=opt.memory_dir),
    )
    srv = serve(app, opt.host, opt.port, opt.verbose)
    print(f"bg3play: http://{opt.host}:{srv.server_address[1]}/  (runtime {opt.runtime})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if rec_thread:
            rec_thread.join(15)  # the loop stops ffmpeg so the open segment is closed
        hub.close()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
