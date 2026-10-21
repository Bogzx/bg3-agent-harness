"""The retired runner/dashboard's endpoints, served by the new app (its test_serve.py assertions, ported), and the
v1 live / stats / story endpoints the Stats and Story screens read. Temp runtime, no game, fake game watch."""

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bg3play.dashboard import Dashboard
from bg3play.game import GameWatch, ScreenWatch, Viewers
from bg3play.hub import Hub
from bg3play.server import App, parser, serve
from bg3play.store import RunStore

from .test_game import STATUS, Clock, FakeGame


@pytest.fixture
def root(tmp_path: Path) -> Path:
    rt, mem = tmp_path / "runtime", tmp_path / "playthroughs" / "run1" / "memory"
    (mem / "sessions").mkdir(parents=True)
    (mem / "HANDOFF.md").write_text("# Handoff S001 → S002\nSave to load: x\n")
    (mem / ".session.json").write_text(json.dumps({"current": 1, "open": True}))
    (mem / "sessions" / "S001.md").write_text("# S001\n- 10:00 [quest] hi\n")
    rt.mkdir()
    (rt / "actions.jsonl").write_text(
        json.dumps({"t": 1, "tool": "get_status", "args": {}, "ms": 5, "result": "WHERE x"}) + "\n")
    (rt / "runner.status.json").write_text(json.dumps({"playthrough": "run1", "deadline": 4e9}))
    (rt / "thermal.log").write_text(
        "2026-10-17 16:25:39 cpu=51 acpi=51 amdgpu=49 nvme=36 nv=[47,8.09,210,0,0x0000000000000001] "
        "profile=balanced load=0.21 bg3=down\n")
    logs = rt / "sessions" / "run1"
    logs.mkdir(parents=True)
    (logs / "S001.log").write_text("[say] I will head north.\n[tool] get_status {}\n")
    return tmp_path


@pytest.fixture
def server(root: Path) -> Iterator[tuple[str, int, Dashboard]]:
    store = RunStore(root / "runtime", root / "playthroughs")
    hub = Hub(store, generation="g1")
    clock = Clock()
    viewers = Viewers(clock)
    game = GameWatch(lambda: FakeGame(), viewers)
    screen = ScreenWatch(lambda: (b"\xff\xd8jpg", "image/jpeg"), viewers)
    dash = Dashboard(store, game, screen, viewers, clock=clock)
    srv = serve(App(store, hub, None, keepalive_s=30, dashboard=dash), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "127.0.0.1", srv.server_address[1], dash
    hub.close()
    srv.shutdown()
    srv.server_close()


def get(server, path: str) -> tuple[int, dict, bytes]:
    host, port, _ = server
    c = http.client.HTTPConnection(host, port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def get_json(server, path: str) -> dict:
    code, _, body = get(server, path)
    assert code == 200, (path, body)
    return json.loads(body)


def test_state(server):
    _, _, dash = server
    s = get_json(server, "/api/state")
    assert s["playthrough"] == "run1"
    assert s["clock"]["source"] == "runner"
    assert s["memory"]["session"] == "S001"
    assert "Handoff S001" in s["memory"]["handoff"]
    assert s["agent_log"]["path_id"] == "S001.log"
    assert dash.viewers.watching(dash.clock())
    th = s["thermal"]   # runtime/thermal.log's last line (runner/ramwatch.py or thermal_log.sh)
    assert (th["cpu"], th["gpu"], th["pause_cpu_c"], th["pause_gpu_c"]) == (51, 47, 92, 85)
    assert "profile=balanced" in th["line"]
    assert s["game"] == {"reachable": False, "error": "not polled yet"}
    dash.game.poll(dash.clock())   # what the game thread does while someone watches
    s = get_json(server, "/api/state")
    assert s["game"]["reachable"] and s["game"]["status"]["party"][0]["name"] == STATUS["party"][0]["name"]


def test_feed_incremental(server):
    r = get_json(server, "/api/feed?after=-1")
    assert [i["tool"] for i in r["items"]] == ["get_status"]
    assert get_json(server, f"/api/feed?after={r['offset']}")["items"] == []


def test_agentlog(server):
    r = get_json(server, "/api/agentlog?after=-1&path=")
    assert r["path_id"] == "S001.log"
    assert "[say] I will head north." in r["text"]
    again = get_json(server, f"/api/agentlog?after={r['offset']}&path=S001.log")
    assert again["text"] == ""


def test_story(server, root):
    (root / "playthroughs" / "run1" / "memory" / "decisions.md").write_text(
        "## Decisions\n\n## D-01 Freed Shadowheart (S002)\n- Choice: opened the pod\n")
    for path in ("/api/story", "/api/v1/playthroughs/run1/story"):
        r = get_json(server, path)
        assert r["exists"]
        assert [d["id"] for d in r["decisions"]] == ["D-01"]
        assert r["chapters"] == []
    v1 = get_json(server, "/api/v1/playthroughs/run1/story")
    assert (v1["v"], v1["playthrough"]) == (1, "run1")
    assert v1["overview"] == get_json(server, "/api/story")["playthrough"]   # PLAYTHROUGH.md, renamed in v1


def test_series_moved(server):
    code, headers, body = get(server, "/series")
    assert code == 200 and "text/html" in headers["Content-Type"] and b"bg3bench" in body
    r = get_json(server, "/api/series")
    assert (r["series"], r["rows"], r["live"]) == (None, [], None)
    assert "benchmark package" in r["error"]


def test_screen(server):
    _, _, dash = server
    assert get(server, "/api/screen")[0] == 204
    dash.screen.poll(dash.clock())
    for path in ("/api/screen", "/api/v1/live/screen"):
        code, headers, body = get(server, path)
        assert code == 200 and headers["Content-Type"] == "image/jpeg" and body.startswith(b"\xff\xd8")
    assert get_json(server, "/api/state")["screen"]["mime"] == "image/jpeg"


def test_stats_and_live_v1(server):
    live = get_json(server, "/api/v1/live")
    assert live["v"] == 1 and live["clock"]["phase"] == "play" and live["thermal"]["cpu"] == 51
    assert "memory" not in live
    for what in ("latency", "progress", "goals"):
        v1 = get_json(server, f"/api/v1/{what}")
        legacy = get_json(server, f"/api/{what}")
        assert v1["v"] == 1 and set(v1) - {"v"} == set(legacy), what
    assert get_json(server, "/api/v1/goals")["goals"][0]["key"] == "idle"


def test_without_pollers(root):
    store = RunStore(root / "runtime", root / "playthroughs")
    app = App(store, Hub(store, generation="g"), None)
    s = app.route("/api/state", {})
    assert s["game"]["reachable"] is False and "--game" in s["game"]["error"]
    assert s["screen"]["enabled"] is False
    assert app.dashboard.frame() == (None, None)


def test_memory_dir_flag(root):
    other = root / "elsewhere"
    (other / "sessions").mkdir(parents=True)
    (other / "HANDOFF.md").write_text("# Handoff S041 → S042\n")
    store = RunStore(root / "runtime", root / "playthroughs")
    dash = Dashboard(store, None, None, Viewers(Clock()), memory_dir=other)
    assert "S041" in dash.state()["memory"]["handoff"]
    opt = parser().parse_args(["--game", "--screen", "--memory-dir", str(other), "--status-interval", "2"])
    assert (opt.game, opt.screen, opt.memory_dir, opt.status_interval) == (True, True, other, 2.0)
