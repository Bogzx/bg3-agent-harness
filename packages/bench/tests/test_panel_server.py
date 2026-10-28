# pyright: basic
import http.client
import json
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bg3bench.serve import panel, security
from bg3bench.serve.server import TOKEN_SLOT, App, parser, serve
from bg3bench.serve.settings import Settings

REPO = Path(__file__).resolve().parents[3]
FAKE = "fake-value-0123456789abcdefghij-WXYZ"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A harness checkout's panel inputs: two real profiles, a run dir, a matrix progress file, a budget."""
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    for name in ("deepseek-flash", "sonnet-5.5-claude-code"):
        shutil.copy(REPO / "profiles" / f"{name}.toml", repo / "profiles")
    run = repo / "runtime" / "sessions" / "s1"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "s1-r001",
                "series": "s1",
                "created": "2026-10-25T10:00:00Z",
                "scenario": {"name": "act1_gold_max"},
                "model": {"slug": "deepseek-flash"},
                "client": {"name": "pi"},
                "condition": "rules",
            }
        )
    )
    (run / "result.json").write_text(
        json.dumps({"run_id": "s1-r001", "valid": True, "passed": None, "score": {"gain": 12}, "cost_usd": 0.29})
    )
    m = repo / "runtime" / "matrix" / "night1"
    m.mkdir(parents=True)
    (m / "progress.json").write_text(json.dumps({"state": "running", "jobs": 3, "done": 1, "updated": 5}))
    (repo / "runtime" / "budget.json").write_text(json.dumps({"cap": 10.0, "spent": 1.0}))
    return repo


@pytest.fixture
def web(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(f'<meta name="bg3bench-panel-token" content="{TOKEN_SLOT}"><title>panel</title>')
    (d / "assets" / "app-1.js").write_text("console.log(1)")
    return d


@pytest.fixture
def app(tree: Path, web: Path, tmp_path: Path) -> App:
    story = tmp_path / "Story"
    (story / "Tav-18221264924__SCN-act1_get_item").mkdir(parents=True)
    cfg = tmp_path / "config"
    a = App(
        token="t0ken-for-tests",
        repo=tree,
        runtime=tree / "runtime",
        settings=Settings(tree / ".env", cfg, lambda: a.key_names(), ignored=lambda _: True),
        saves=panel.StorySaves(story),
        checks=lambda: [("PASS", "ffmpeg", "/usr/bin/ffmpeg"), ("FAIL", "steam", "no Steam client running")],
        web_dir=web,
    )
    return a


@pytest.fixture
def server(app: App) -> Iterator[App]:
    srv = serve(app, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield app
    srv.shutdown()
    srv.server_close()


def request(app: App, method: str, path: str, body: object = None, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    h = {"Host": f"127.0.0.1:{app.port}", **(headers or {})}
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, dict(r.getheaders()), raw


def get(app: App, path: str, headers: dict | None = None) -> tuple[int, dict]:
    code, _, raw = request(app, "GET", path, headers=headers)
    return code, json.loads(raw)


def post(app: App, path: str, body: object, headers: dict | None = None) -> tuple[int, dict]:
    h = {"Content-Type": "application/json", security.TOKEN_HEADER: app.token, **(headers or {})}
    code, _, raw = request(app, "POST", path, body, h)
    return code, json.loads(raw)


def test_no_host_option_and_loopback_bind(server):
    assert not any("--host" in a.option_strings for a in parser()._actions)
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    c.request("GET", "/api/v1/about", headers={"Host": f"127.0.0.1:{server.port}"})
    assert c.sock.getpeername()[0] == "127.0.0.1"
    c.getresponse().read()
    c.close()


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1:1", "localhost", "127.0.0.2:{port}", ""])
def test_wrong_host_is_refused_421(server, host):
    code, _ = get(server, "/api/v1/settings", headers={"Host": host.format(port=server.port)})
    assert code == 421
    code, _, raw = request(server, "GET", "/", headers={"Host": host.format(port=server.port)})
    assert code == 421 and server.token.encode() not in raw  # a rebound page can't read the injected token
    code, _ = post(server, "/api/v1/plan", {}, headers={"Host": host.format(port=server.port)})
    assert code == 421


def test_localhost_name_is_allowed(server):
    code, about = get(server, "/api/v1/about", headers={"Host": f"localhost:{server.port}"})
    assert code == 200 and about["suite"].startswith("bench-v") and "Not affiliated" in about["not_affiliated"]


def test_write_without_or_with_a_wrong_token_is_refused(server):
    body = {"name": "DEEPSEEK_API_KEY", "value": FAKE}
    code, r = post(server, "/api/v1/settings/key", body, headers={security.TOKEN_HEADER: ""})
    assert code == 403 and "X-Panel-Token" in r["error"]
    code, _ = post(server, "/api/v1/settings/key", body, headers={security.TOKEN_HEADER: "t0ken-for-testz"})
    assert code == 403
    assert not (server.repo / ".env").exists()


def test_write_needs_json_content_type(server):
    code, r = post(server, "/api/v1/plan", {}, headers={"Content-Type": "text/plain"})
    assert code == 403 and "application/json" in r["error"]


def test_cross_origin_write_is_refused(server):
    code, r = post(server, "/api/v1/plan", {}, headers={"Origin": "http://evil.example"})
    assert code == 403 and r["error"] == "cross-origin write"


def test_no_cors_headers_and_preflight_refused(server):
    code, headers, _ = request(
        server,
        "OPTIONS",
        "/api/v1/plan",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert code == 405
    for resp_headers in (headers, request(server, "GET", "/api/v1/about")[1]):
        assert not [h for h in resp_headers if h.lower().startswith("access-control-")]


def test_token_is_injected_into_index_only(server):
    code, headers, raw = request(server, "GET", "/")
    assert code == 200 and server.token.encode() in raw and TOKEN_SLOT.encode() not in raw
    assert headers["Cache-Control"] == "no-store"
    code, _, raw = request(server, "GET", "/assets/app-1.js")
    assert code == 200 and raw == b"console.log(1)"
    for path in ("/api/v1/about", "/api/v1/settings", "/api/v1/profiles", "/api/v1/results"):
        assert server.token not in json.dumps(get(server, path)[1])


def test_key_is_written_to_dotenv_and_only_masked_back(server):
    (server.repo / ".env").write_text("# mine\nOTHER=1\nexport DEEPSEEK_API_KEY=old\n")
    code, r = post(server, "/api/v1/settings/key", {"name": "DEEPSEEK_API_KEY", "value": FAKE})
    assert code == 200 and r["masked"] == "…WXYZ" and FAKE not in json.dumps(r)
    assert (server.repo / ".env").read_text() == f"# mine\nOTHER=1\nexport DEEPSEEK_API_KEY={FAKE}\n"
    assert stat.S_IMODE((server.repo / ".env").stat().st_mode) == 0o600
    code, s = get(server, "/api/v1/settings")
    assert {"name": "DEEPSEEK_API_KEY", "masked": "…WXYZ"} in s["keys"]
    assert FAKE not in json.dumps(s)
    code, p = get(server, "/api/v1/profiles")
    assert FAKE not in json.dumps(p)
    assert next(r for r in p["profiles"] if r["name"] == "deepseek-flash")["key_set"] is True


def test_key_name_must_be_a_profiles_key(server):
    code, r = post(server, "/api/v1/settings/key", {"name": "PATH", "value": FAKE})
    assert code == 400 and "api_key_env" in r["error"]
    code, r = post(server, "/api/v1/settings/key", {"name": "DEEPSEEK_API_KEY", "value": "a b\nEVIL=1"})
    assert code == 400


def test_key_refused_when_dotenv_is_not_git_ignored(server):
    server.settings.ignored = lambda _: False
    code, r = post(server, "/api/v1/settings/key", {"name": "DEEPSEEK_API_KEY", "value": FAKE})
    assert code == 400 and ".gitignore" in r["error"] and not (server.repo / ".env").exists()


def test_site_token_goes_to_its_own_private_file(server, tmp_path):
    code, r = post(server, "/api/v1/settings/site-token", {"value": "bgt_" + "x" * 30 + "LAST"})
    assert code == 200 and r["site_token"] == "…LAST"
    f = tmp_path / "config" / "site-token"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and not (server.repo / ".env").exists()
    assert get(server, "/api/v1/settings")[1]["site_token"] == "…LAST"


def test_prefs(server):
    code, r = post(server, "/api/v1/settings/prefs", {"youtube_verified": False, "record_cap_gb": 20})
    assert code == 200 and r["prefs"] == {"youtube_verified": False, "record_cap_gb": 20}
    assert post(server, "/api/v1/settings/prefs", {"record_cap_gb": "lots"})[0] == 400


def test_preflight(server):
    code, r = get(server, "/api/v1/preflight")
    assert code == 200 and r["fails"] == 1 and not r["ready"]
    assert r["checks"][1] == {"status": "FAIL", "name": "steam", "detail": "no Steam client running"}


def test_scenarios_with_save_status(server):
    code, r = get(server, "/api/v1/scenarios")
    rows = {s["name"]: s for s in r["scenarios"]}
    assert code == 200 and len(rows) >= 30
    item = rows["act1_get_item"]
    assert item["save_status"]["status"] == "installed" and item["seed"] == 1018 and item["cap_s"] == 2700
    assert rows["act1_gold_1000"]["save_status"]["status"] in ("missing", "none")


def test_queue_and_results(server):
    code, q = get(server, "/api/v1/queue")
    assert code == 200 and q["matrices"] == [
        {"matrix": "night1", "state": "running", "jobs": 3, "done": 1, "updated": 5}
    ]
    code, r = get(server, "/api/v1/results")
    (run,) = r["runs"]
    assert run["run_id"] == "s1-r001" and run["score"] == 12 and run["scenario"] == "act1_gold_max"
    assert run["saves_bytes"] == 0


def test_plan_over_the_endpoint(server):
    body = {"profiles": ["deepseek-flash"], "scenarios": ["act1_get_item"], "repeats": 2, "max_cost_usd": 2.0}
    code, r = post(server, "/api/v1/plan", body)
    assert code == 200 and len(r["jobs"]) == 2 and r["reserve_usd"] == 4.0 and r["fits_budget"]
    assert r["youtube"] == {"verified": None, "long_scenarios": ["act1_get_item"], "warn": True}
    code, r = post(server, "/api/v1/plan", {**body, "profiles": ["sonnet-5.5-claude-code"]})
    assert code == 400 and "pi only" in r["error"]


def test_unknown_paths_and_bad_bodies(server):
    assert get(server, "/api/v1/nothing")[0] == 404
    assert post(server, "/api/v1/nothing", {})[0] == 404
    assert post(server, "/api/v1/plan", [1, 2])[0] == 400
    h = {"Content-Type": "application/json", security.TOKEN_HEADER: server.token}
    assert request(server, "POST", "/api/v1/plan", b"{not json", h)[0] == 400
    assert request(server, "POST", "/api/v1/plan", b"x" * 20000, h)[0] == 413
