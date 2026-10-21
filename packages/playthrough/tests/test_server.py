import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

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
    for what in ("sessions", "watch", "memory/tree", "memory/log", "timeline"):
        code, body = get_json(server, f"/api/v1/playthroughs/{PT}/{what}")
        assert code == 200 and body["v"] == 1 and body["playthrough"] == PT, what
    _, _, hub = server
    assert get_json(server, f"/api/v1/playthroughs/{PT}/watch")[1]["stream_id"] == hub.last_id()
    code, f = get_json(server, f"/api/v1/playthroughs/{PT}/memory/file?path=HANDOFF.md")
    assert code == 200 and f["text"].startswith("# Handoff")


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
    (video / "index.json").write_text(json.dumps({"v": 1, "segments": [
        {"file": "rec-100-0000.mp4", "run": "rec-100", "start": 100.0, "end": 700.0, "bytes": 200},
        {"file": "rec-100-0001.mp4", "run": "rec-100", "start": 700.0, "end": None, "bytes": 0}]}))
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
