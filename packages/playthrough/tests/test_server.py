import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from bg3play.hub import Hub
from bg3play.server import App, parser, serve
from bg3play.store import RunStore

from .conftest import PT


@pytest.fixture
def web(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>app</title>")
    (d / "assets" / "app-1.js").write_text("console.log(1)")
    return d


@pytest.fixture
def server(store: RunStore, web: Path) -> Iterator[tuple[str, int, Hub]]:
    hub = Hub(store, generation="g1")
    hub.poll()
    srv = serve(App(store, hub, web, keepalive_s=30), "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "127.0.0.1", srv.server_address[1], hub
    hub.close()
    srv.shutdown()
    srv.server_close()


def get(server, path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    host, port, _ = server
    c = http.client.HTTPConnection(host, port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def get_json(server, path: str) -> tuple[int, dict]:
    code, _, body = get(server, path)
    return code, json.loads(body)


def test_binds_localhost_by_default():
    assert parser().parse_args([]).host == "127.0.0.1"


def test_json_endpoints(server):
    code, st = get_json(server, "/api/v1/status")
    assert code == 200 and st["v"] == 1 and st["runner"]["playthrough"] == PT
    code, pts = get_json(server, "/api/v1/playthroughs")
    assert [p["name"] for p in pts["playthroughs"]] == [PT]
    for what in ("sessions", "watch", "memory/tree", "memory/log", "timeline", "bench"):
        code, body = get_json(server, f"/api/v1/playthroughs/{PT}/{what}")
        assert code == 200 and body["v"] == 1 and body["playthrough"] == PT, what
    _, _, hub = server
    assert get_json(server, f"/api/v1/playthroughs/{PT}/watch")[1]["stream_id"] == hub.last_id()
    code, f = get_json(server, f"/api/v1/playthroughs/{PT}/memory/file?path=HANDOFF.md")
    assert code == 200 and f["text"].startswith("# Handoff")


def test_memory_diff(server, git_memory):
    _, log = get_json(server, f"/api/v1/playthroughs/{PT}/memory/log")
    sha = log["commits"][0]["sha"]
    code, d = get_json(server, f"/api/v1/playthroughs/{PT}/memory/diff?path=HANDOFF.md&sha={sha}")
    assert code == 200 and d["sha"] == sha
    assert {"kind": "add", "text": "+- keeper line"} in d["lines"]
    assert get_json(server, f"/api/v1/playthroughs/{PT}/memory/diff?path=HANDOFF.md&sha={'0' * 40}")[0] == 404
    assert get_json(server, f"/api/v1/playthroughs/{PT}/memory/diff?path=HANDOFF.md&sha=HEAD")[0] == 400


def test_errors(server):
    assert get_json(server, "/api/v1/playthroughs/nope/watch")[0] == 404
    assert get_json(server, "/api/v1/nothing")[0] == 404
    assert get_json(server, f"/api/v1/playthroughs/{PT}/memory/file?path=missing.md")[0] == 404
    code, body = get_json(server, f"/api/v1/playthroughs/{PT}/memory/file?path=../../../runtime/runner.status.json")
    assert code == 400 and "outside" in body["error"]
    assert get_json(server, "/api/v1/playthroughs/..%2Fx/watch")[0] == 400


def test_screenshot_and_static(server):
    code, headers, body = get(server, "/api/v1/screenshot")
    assert code == 200 and headers["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    code, headers, body = get(server, "/")
    assert code == 200 and b"<title>app</title>" in body
    assert get(server, "/watch/claude-run-2")[2] == body  # client-side route -> index.html
    code, headers, _ = get(server, "/assets/app-1.js")
    assert code == 200 and "immutable" in headers["Cache-Control"]
    assert get(server, "/assets/missing.js")[0] == 404
    for path in ("/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd"):  # never outside dist: at most index.html
        assert get(server, path)[2] in (body, b"not found")
    assert get(server, "/%2e%2e/%2e%2e/etc/hostname.txt")[0] == 404


class Sse:
    """A minimal EventSource reader over http.client (blocking reads with a socket timeout, no sleeps)."""

    def __init__(self, server, last_id: str | None = None):
        host, port, _ = server
        self.conn = http.client.HTTPConnection(host, port, timeout=10)
        self.conn.request("GET", "/api/v1/stream", headers={"Last-Event-ID": last_id} if last_id else {})
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200 and self.resp.getheader("Content-Type") == "text/event-stream"

    def next(self) -> dict:
        ev: dict = {}
        while True:
            line = self.resp.fp.readline().decode().rstrip("\n")
            if not line:
                if ev:
                    return ev
                continue
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            ev[k] = json.loads(v) if k == "data" else v

    def take(self, n: int) -> list[dict]:
        return [self.next() for _ in range(n)]

    def close(self) -> None:
        self.conn.close()


def test_sse_stream_live_and_resume(server, store: RunStore):
    _, _, hub = server
    buffered = len(hub.events)
    sse = Sse(server)
    first = sse.take(buffered)
    assert first[0]["event"] == "status" and first[0]["id"] == "g1-1"
    assert [int(e["id"].split("-")[1]) for e in first] == list(range(1, buffered + 1))

    line = json.dumps({"t": 1.0, "tool": "look", "args": {}, "ms": 3, "result": "live one"})
    with store.actions_path.open("a") as f:
        f.write(line + "\n")
    hub.poll()
    (ev,) = sse.take(1)
    assert ev["event"] == "action" and ev["data"]["result"] == "live one" and ev["id"] == f"g1-{buffered + 1}"
    sse.close()

    # while disconnected: two more lines; the reconnect with Last-Event-ID gets exactly those
    with store.actions_path.open("a") as f:
        f.write(line.replace("live one", "gap 1") + "\n" + line.replace("live one", "gap 2") + "\n")
    hub.poll()
    sse = Sse(server, last_id=ev["id"])
    gap = sse.take(2)
    assert [e["data"]["result"] for e in gap] == ["gap 1", "gap 2"]
    sse.close()

    # an id from a previous server run: reset, then the whole buffer
    sse = Sse(server, last_id="g0-5")
    assert sse.next() == {"event": "reset", "data": {}}
    assert sse.next()["id"] == "g1-1"
    sse.close()


def test_byte_range():
    from bg3play.server import byte_range

    assert byte_range(None, 100) is None and byte_range("items=1-2", 100) is None
    assert byte_range("bytes=0-", 100) == (0, 99)
    assert byte_range("bytes=10-19", 100) == (10, 19)
    assert byte_range("bytes=90-500", 100) == (90, 99)
    assert byte_range("bytes=-10", 100) == (90, 99)
    for bad in ("bytes=100-", "bytes=5-2", "bytes=-0"):
        with pytest.raises(ValueError):
            byte_range(bad, 100)


def test_video_index_file_ranges_and_no_recorder(server, store: RunStore):
    video = store.runtime / "video"
    video.mkdir()
    (video / "rec-100-0000.mp4").write_bytes(bytes(range(200)))
    (video / "index.json").write_text(
        json.dumps(
            {
                "v": 1,
                "segments": [
                    {"file": "rec-100-0000.mp4", "run": "rec-100", "start": 100.0, "end": 700.0, "bytes": 200},
                    {"file": "rec-100-0001.mp4", "run": "rec-100", "start": 700.0, "end": None, "bytes": 0},
                ],
            }
        )
    )
    code, idx = get_json(server, "/api/v1/video/index")
    assert code == 200 and [s["file"] for s in idx["segments"]] == ["rec-100-0000.mp4"]  # missing file dropped
    code, headers, body = get(server, "/api/v1/video/rec-100-0000.mp4")
    assert code == 200 and body == bytes(range(200)) and headers["Accept-Ranges"] == "bytes"
    code, headers, body = get(server, "/api/v1/video/rec-100-0000.mp4", {"Range": "bytes=10-19"})
    assert code == 206 and body == bytes(range(10, 20)) and headers["Content-Range"] == "bytes 10-19/200"
    assert get(server, "/api/v1/video/rec-100-0000.mp4", {"Range": "bytes=500-"})[0] == 416
    assert get(server, "/api/v1/video/rec-100-0009.mp4")[0] == 404
    assert get(server, "/api/v1/video/..%2Findex.json")[0] == 404
    assert get_json(server, "/api/v1/recorder") == (200, {"v": 1, "recorder": None})
    assert get(server, "/api/v1/screen.jpg")[0] == 204 and get(server, "/api/v1/screen.mjpg")[0] == 204


def test_live_mjpeg_from_the_recorder(store: RunStore, web: Path):
    from bg3play.recorder import Recorder, RecordSpec

    rec = Recorder(RecordSpec(store.runtime / "video"), probe=lambda: None, clock=lambda: 1.0)
    rec.live.put(b"\xff\xd8one")
    hub = Hub(store, generation="g1")
    srv = serve(App(store, hub, web, keepalive_s=30, recorder=rec), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = ("127.0.0.1", srv.server_address[1], hub)
    try:
        assert get(s, "/api/v1/screen.jpg")[2] == b"\xff\xd8one"
        assert get_json(s, "/api/v1/recorder")[1]["recorder"]["reason"] == "starting"
        c = http.client.HTTPConnection(s[0], s[1], timeout=10)
        c.request("GET", "/api/v1/screen.mjpg")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type").startswith("multipart/x-mixed-replace")
        first = r.fp.read(len(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n\xff\xd8one\r\n"))
        assert first.endswith(b"\xff\xd8one\r\n")
        rec.live.put(b"\xff\xd8two")
        assert r.fp.readline() == b"--frame\r\n"
        while r.fp.readline() != b"\r\n":
            pass
        assert r.fp.read(5) == b"\xff\xd8two"
        c.close()
    finally:
        rec.live.close()
        hub.close()
        srv.shutdown()
        srv.server_close()


def test_positions_endpoint(server):
    code, p = get_json(server, f"/api/v1/playthroughs/{PT}/positions?level=END_Main")
    assert code == 200 and p["level"] == "END_Main" and len(p["segments"]) == 2
    assert get_json(server, f"/api/v1/playthroughs/{PT}/positions")[1]["level"] == "EPI_Main_A"


class Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def test_authorize_writes(store: RunStore):
    app = App(store, Hub(store, generation="g"), None)
    assert app.authorize("127.0.0.1", Headers(Host="127.0.0.1:8780")) is None
    assert app.authorize("::1", Headers(Host="[::1]:8780", Origin="http://[::1]:8780")) is None
    assert app.authorize("127.0.0.1", Headers(Host="localhost")) is None
    # DNS rebinding: a page on evil.test, rebound to 127.0.0.1, sends its own name as Origin and Host
    rebound = Headers(Host="evil.test:8780", Origin="http://evil.test:8780")
    assert "localhost URL" in app.authorize("127.0.0.1", rebound)
    assert "localhost URL" in app.authorize("127.0.0.1", Headers(Host="box.test:8780"))
    assert "localhost URL" in app.authorize("127.0.0.1", Headers())
    assert "cross-origin" in app.authorize("127.0.0.1", Headers(Host="127.0.0.1:8780", Origin="http://evil.test"))
    assert "localhost" in app.authorize("192.0.2.7", Headers(Host="box.test:8780"))
    tok = App(store, Hub(store, generation="g"), None, token="s3cret")
    assert tok.authorize("192.0.2.7", Headers(Host="box.test", Authorization="Bearer s3cret")) is None
    assert "token" in tok.authorize("127.0.0.1", Headers(Host="box.test", Authorization="Bearer nope"))


def post(server, path: str, body: object = None, headers: dict | None = None) -> tuple[int, dict]:
    host, port, _ = server
    c = http.client.HTTPConnection(host, port, timeout=10)
    data = json.dumps(body).encode() if body is not None else b""
    c.request("POST", path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def test_control_without_a_supervisor(server):
    assert post(server, f"/api/v1/playthroughs/{PT}/start", {})[0] == 409
    assert post(server, "/api/v1/nothing", {})[0] == 404
    assert get_json(server, "/api/v1/supervisor")[1] == {"v": 1, "supervisor": None, "profiles": []}


def test_control_start_stop_resume(store: RunStore, web: Path, tmp_path: Path):
    from bg3play.supervisor import SessionResult, Supervisor

    gate, entered = threading.Event(), threading.Event()

    class Runner:
        def __init__(self):
            self.reqs = []

        def run_session(self, req, stop):
            self.reqs.append(req)
            entered.set()
            gate.wait(10)
            return SessionResult(f"S{req.session:03d}", "stopped" if stop.is_set() else "ok", True)

    class Keeper:
        def run_keeper(self, pt, result):
            pass

    runner = Runner()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "codex.toml").write_text('client = "codex"\n')
    hub = Hub(store, generation="g1")
    sup = Supervisor(
        runner,
        Keeper(),
        store.runtime,
        store.playthroughs_dir,
        lambda: 0.0,
        lambda v: hub.publish("supervisor", v),
        played=lambda pt: len(store.session_logs(pt)),
    )
    srv = serve(App(store, hub, web, supervisor=sup, profiles=profiles), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = ("127.0.0.1", srv.server_address[1], hub)
    try:
        assert get_json(s, "/api/v1/supervisor")[1]["profiles"] == [
            {
                "name": "codex",
                "client": None,
                "model": None,
                "effort": "",
                "provider": None,
                "api_key_env": None,
                "error": "codex.toml: model must be a non-empty string",
            }
        ]
        code, cfg = get_json(s, "/api/v1/playthroughs/newpt/config")
        assert code == 200 and cfg["config"]["name"] == "newpt" and cfg["sessions"] == 0
        assert get_json(s, f"/api/v1/playthroughs/{PT}/config")[1]["sessions"] == 3  # the runner's S094-S096
        assert post(s, "/api/v1/playthroughs/newpt/start", {"difficulty": "Nightmare"})[0] == 400
        assert post(s, "/api/v1/playthroughs/newpt/start", [1])[0] == 400
        assert post(s, "/api/v1/playthroughs/..%2Fx/start", {})[0] == 400
        assert post(s, "/api/v1/playthroughs/newpt/start", {}, {"Origin": "http://evil.test"})[0] == 403
        code, body = post(
            s, "/api/v1/playthroughs/newpt/start", {"brief": "a bard", "session_minutes": 30, "recording": False}
        )
        assert code == 200 and body["supervisor"]["state"] == "running"
        assert entered.wait(10)
        assert (store.playthroughs_dir / "newpt" / "playthrough.toml").exists()
        assert runner.reqs[0].config.character.brief == "a bard" and runner.reqs[0].config.session.minutes == 30
        assert post(s, "/api/v1/playthroughs/newpt/start", {})[0] == 409
        assert post(s, f"/api/v1/playthroughs/{PT}/stop")[0] == 409  # not the running one
        code, body = post(s, "/api/v1/playthroughs/newpt/stop")
        assert code == 200 and body["supervisor"]["state"] == "stopping"
        gate.set()
        sup.thread.join(10)
        assert get_json(s, "/api/v1/supervisor")[1]["supervisor"]["state"] == "idle"
        assert [e.data["state"] for e in hub.events if e.type == "supervisor"] == [
            "running",
            "running",
            "stopping",
            "stopping",
            "idle",
        ]  # claimed, S001, stop, its keeper, done
        entered.clear()
        code, _ = post(s, "/api/v1/playthroughs/newpt/resume", {})
        assert code == 200 and entered.wait(10) and runner.reqs[1].resume and runner.reqs[1].session == 2
        sup.stop()
        sup.thread.join(10)
    finally:
        gate.set()
        hub.close()
        srv.shutdown()
        srv.server_close()


def test_replay_endpoint(server):
    code, r = get_json(server, f"/api/v1/playthroughs/{PT}/replay/S096")
    assert code == 200 and r["session"] == "S096" and r["events"] and r["segments"] == []
    assert get_json(server, f"/api/v1/playthroughs/{PT}/replay/S001")[0] == 404
    assert get_json(server, "/api/v1/playthroughs/nope/replay/S096")[0] == 404


def test_main_wires_the_app_and_shuts_down(store: RunStore, web: Path, capsys: pytest.CaptureFixture[str]):
    from bg3play import server as srv_mod
    from bg3play.adapters import bridge_status

    served: list[App] = []

    class FakeServer:
        server_address = ("127.0.0.1", 4321)
        closed = False

        def serve_forever(self) -> None:
            raise KeyboardInterrupt  # Ctrl-C: a clean shutdown

        def server_close(self) -> None:
            self.closed = True

    fake = FakeServer()

    def fake_serve(app: App, host: str, port: int, verbose: bool = False) -> FakeServer:
        served.append(app)
        return fake

    class NoStatus:
        def __init__(self, port: int):
            self.port = port

        def status(self) -> None:
            return None

    with (
        mock.patch.object(srv_mod, "serve", fake_serve),
        mock.patch.object(bridge_status, "BridgeStatus", NoStatus),
        mock.patch.object(bridge_status, "bridge_capture", lambda shots, width: (None, "no screen", None)),
    ):
        rc = srv_mod.main(
            [
                "--runtime",
                str(store.runtime),
                "--playthroughs",
                str(store.playthroughs),
                "--web",
                str(web),
                "--port",
                "0",
                "--supervise",
                "--game",
                "--screen",
                "--positions",
                "--debug-port",
                "1",
            ]
        )
    assert rc == 0 and fake.closed
    app = served[0]
    assert app.supervisor is not None and app.hub.closed
    assert app.dashboard.game is not None
    assert app.dashboard.screen is not None and app.dashboard.screen.capture is None
    assert "bg3play: http://127.0.0.1:4321/" in capsys.readouterr().out


def test_the_session_runner_is_the_harness_unless_legacy_is_asked_for(tmp_path):
    from bg3harness.game.host import AppHost
    from bg3play.adapters.harness_runner import AttachedGame, HarnessRunner
    from bg3play.adapters.launched_game import LaunchedGame
    from bg3play.adapters.legacy_cli import LegacyCli
    from bg3play.server import session_runner

    base = ["--supervise", "--runtime", str(tmp_path), "--repo", str(tmp_path)]
    runner, keeper_client = session_runner(parser().parse_args(base))  # the bridge client connects lazily
    assert isinstance(runner, HarnessRunner) and keeper_client.__self__.profiles == tmp_path / "profiles"
    # the app launches the game: GameController over AppHost (launch_game.sh in --repo, the mod snapshot in --runtime)
    assert isinstance(runner.game, LaunchedGame) and isinstance(runner.game.host, AppHost)
    assert runner.game.host.launch_sh == tmp_path / "runner" / "launch_game.sh"
    assert runner.game.host.mod_live == tmp_path / "mod-live" and runner.game.ctl.host is runner.game.host
    runner, _ = session_runner(parser().parse_args([*base, "--attach"]))  # a game the user started: only read
    assert isinstance(runner, HarnessRunner) and isinstance(runner.game, AttachedGame)
    runner, keeper_client = session_runner(parser().parse_args([*base, "--session-runner", "legacy"]))
    assert isinstance(runner, LegacyCli) and keeper_client == runner.keeper_client


def test_a_start_applies_the_playthroughs_recording_format(store: RunStore, web: Path):
    # playthrough.toml [recording] fps/height reach the recorder (they were validated but never used)
    from bg3play import config
    from bg3play.recorder import Recorder, RecordSpec

    (store.playthroughs_dir / "rec").mkdir()
    config.save(
        store.playthroughs_dir / "rec",
        config.validate({"v": 1, "name": "rec", "recording": {"fps": 30, "height": 1080}}),
    )
    started = []

    class Sup:
        pt = None

        def start(self, pt, resume=False, cfg=None):
            started.append((pt, resume))

        def view(self):
            return {}

    recorder = Recorder(RecordSpec(store.runtime / "video"), probe=lambda: None, clock=lambda: 1.0)
    app = App(store, Hub(store, generation="g"), web, recorder=recorder, supervisor=Sup())  # type: ignore[arg-type]
    assert app.control("rec", "start", {"recording": False})[0] == 200 and started == [("rec", False)]
    spec = recorder.spec
    assert (spec.fps, spec.width, spec.height, recorder.enabled) == (30, 1920, 1080, False)


def test_the_recorders_first_spec_is_the_followed_playthroughs(store: RunStore):
    from bg3play import config
    from bg3play.server import record_spec

    pts = store.playthroughs_dir
    (pts / "rec").mkdir()
    config.save(pts / "rec", config.validate({"v": 1, "name": "rec", "recording": {"fps": 10, "height": 480}}))
    spec = record_spec(store.runtime / "video", "libx264", pts, "rec")
    assert (spec.fps, spec.width, spec.height, spec.encoder) == (10, 854, 480, "libx264")
    default = record_spec(store.runtime / "video", "libx264", pts, None)
    assert (default.fps, default.width, default.height) == (15, 1280, 720)
    (pts / "broken").mkdir()
    (pts / "broken" / "playthrough.toml").write_text("v = [")
    assert record_spec(store.runtime / "video", "libx264", pts, "broken").height == 720
