# pyright: basic
import http.client
import json
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bg3bench.serve import panel, security, video
from bg3bench.serve.server import TOKEN_SLOT, App, parser, serve
from bg3bench.serve.settings import Settings

REPO = Path(__file__).resolve().parents[3]
FAKE = "fake-value-0123456789abcdefghij-WXYZ"
SHA = "ab" * 32


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []
        self.st: dict | None = None

    def start(self, name, matrix_file, log):
        self.started.append((name, matrix_file))
        self.st = {"name": name, "running": True, "stopping": False}

    def stop(self):
        if self.st is None or not self.st["running"]:
            raise panel.PanelError("no matrix of this panel is running")
        self.st["stopping"] = True

    def state(self):
        return self.st


class FakeBundler:
    def __init__(self) -> None:
        self.literals: list[str] = []

    def pack(self, run_dir, out_dir, *, attempt, unsubmitted_valid, literals):
        self.literals = list(literals)
        b = out_dir / "bundle"
        b.mkdir(parents=True)
        (b / "bundle.json").write_text(json.dumps({"files": []}))
        return SHA, b

    def validate(self, bundle_dir, *, literals):
        return {"accepted": True, "flags": [], "checks": []}


class FakeSite:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def post_submission(self, token, files, youtube_url):
        self.calls.append(("post", token, youtube_url))
        return 202, {"id": "sub1", "status": "received"}

    def get_submission(self, token, submission_id):
        self.calls.append(("get", token, submission_id))
        return 200, {"status": "accepted", "flags": []}


class FakeRenderer:
    def __init__(self) -> None:
        self.jobs: list[video.Job] = []

    def start(self, job):
        self.jobs.append(job)

    def state(self):
        return {"run": self.jobs[-1].run_dir.name, "running": True} if self.jobs else None


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
        site="https://bench.example",
        runner=FakeRunner(),
        bundler=FakeBundler(),
        site_client=lambda base: a.fake_site,  # type: ignore[attr-defined]
        renderer=FakeRenderer(),
        now=lambda: 0.0,
    )
    a.fake_site = FakeSite()  # type: ignore[attr-defined]
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


RUN_FORM = {"profiles": ["deepseek-flash"], "scenarios": ["act1_get_item"], "max_cost_usd": 2.0}


def test_start_and_stop_a_run(server):
    assert get(server, "/api/v1/run")[1] == {"v": 1, "run": None, "can_start": True}
    code, r = post(server, "/api/v1/run/start", RUN_FORM)
    assert code == 200 and r["started"]["name"] == "panel-19700101-000000"
    ((name, f),) = server.runner.started
    assert f == server.runtime / "panel" / f"{name}.toml" and 'scenarios = ["act1_get_item"]' in f.read_text()
    code, r = post(server, "/api/v1/run/start", RUN_FORM)
    assert code == 400 and "is running" in r["error"] and len(server.runner.started) == 1
    code, r = post(server, "/api/v1/run/stop", {})
    assert code == 200 and r["run"]["stopping"] is True


def test_start_refuses_a_plan_over_budget_and_writes_no_matrix(server):
    code, r = post(server, "/api/v1/run/start", {**RUN_FORM, "max_cost_usd": 50.0})
    assert code == 400 and "doesn't fit the budget" in r["error"]
    assert server.runner.started == [] and not (server.runtime / "panel").exists()
    assert post(server, "/api/v1/run/stop", {})[0] == 400


def test_start_needs_the_token(server):
    code, _ = post(server, "/api/v1/run/start", RUN_FORM, headers={security.TOKEN_HEADER: "wrong"})
    assert code == 403 and server.runner.started == []


def test_a_panel_without_runner_starts_nothing(server):
    server.runner = None
    code, r = post(server, "/api/v1/run/start", RUN_FORM)
    assert code == 400 and "starts no run" in r["error"]


def test_submit_flow_over_the_endpoints(server):
    server.settings.set_site_token("bgb_" + "0" * 40)
    (server.repo / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE}\n")
    assert post(server, "/api/v1/submit/prepare", {"run": "nope"})[0] == 400
    code, r = post(server, "/api/v1/submit/prepare", {"run": "s1"})
    assert code == 200 and r["bundle_sha256"] == SHA and r["verdict"]["accepted"] and r["attempt"]["n"] == 1
    assert server.bundler.literals == [FAKE, "bgb_" + "0" * 40]  # scrubbed verbatim, never returned
    code, r = post(
        server,
        "/api/v1/submit/upload",
        {"run": "s1", "bundle_sha256": SHA, "youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert code == 200 and r["id"] == "sub1"
    code, r = post(server, "/api/v1/submit/status", {"run": "s1"})
    assert code == 200 and r["status"] == "accepted"
    assert [c[0] for c in server.fake_site.calls] == ["post", "get"]
    assert all(c[1] == "bgb_" + "0" * 40 for c in server.fake_site.calls)
    code, r = get(server, "/api/v1/submissions")
    assert r["submissions"]["s1"]["submission"]["status"] == "accepted"
    assert FAKE not in json.dumps(r) and "0" * 40 not in json.dumps(r)


def test_upload_without_a_site_url_is_refused(server):
    post(server, "/api/v1/submit/prepare", {"run": "s1"})
    server.site = None
    code, r = post(
        server,
        "/api/v1/submit/upload",
        {"run": "s1", "bundle_sha256": SHA, "youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert code == 400 and "no site URL" in r["error"] and server.fake_site.calls == []


def test_render_needs_a_prepared_bundle(server):
    code, r = post(server, "/api/v1/render", {"run": "s1"})
    assert code == 400 and "validate the run first" in r["error"]
    post(server, "/api/v1/submit/prepare", {"run": "s1"})
    code, r = post(server, "/api/v1/render", {"run": "s1"})
    assert code == 200 and r["render"] == {"run": "s1", "running": True}
    (job,) = server.renderer.jobs
    assert job.bundle_sha256 == SHA and job.run_id == "s1-r001" and job.site == "bench.example"
    assert get(server, "/api/v1/render")[1]["render"]["run"] == "s1"


def test_static_without_a_build_head_and_errors(server):
    code, headers, raw = request(server, "HEAD", "/")
    assert code == 200 and raw == b"" and int(headers["Content-Length"]) > 0
    server.web_dir = None
    code, _, raw = request(server, "GET", "/")
    assert code == 404 and b"npm run build" in raw

    def broken(path):
        raise OSError("disk gone")

    server.get = broken
    assert get(server, "/api/v1/about") == (500, {"v": 1, "error": "OSError: disk gone"})
    assert request(server, "DELETE", "/api/v1/plan", headers={"Host": "evil.example"})[0] == 421
    h = {"Content-Type": "application/json", security.TOKEN_HEADER: server.token, "Content-Length": "x"}
    assert request(server, "POST", "/api/v1/plan", None, h)[0] == 413


def test_render_and_prepare_need_their_backends(server):
    server.bundler = None
    code, r = post(server, "/api/v1/submit/prepare", {"run": "s1"})
    assert code == 400 and "no bundler" in r["error"]
    server.renderer = None
    assert get(server, "/api/v1/render")[1]["render"] is None
    assert "no renderer" in post(server, "/api/v1/render", {"run": "s1"})[1]["error"]


def test_default_checks_add_the_panels_own(monkeypatch, tmp_path):
    from bg3bench.serve import server as srv_mod

    monkeypatch.setattr(srv_mod.preflight, "preflight", lambda ctx: [("PASS", "game", str(ctx.repo))])
    lines = srv_mod.default_checks(tmp_path, tmp_path)()
    assert lines[0] == ("PASS", "game", str(tmp_path)) and {name for _, name, _ in lines[1:]} >= {"ffmpeg"}


def test_main_serves_until_interrupted(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from bg3bench.serve import server as srv_mod

    built: list[App] = []

    class Srv:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            built.append(app_seen[0])

    app_seen: list[App] = []

    def fake_serve(app, port, verbose=False):
        app_seen.append(app)
        app.port = 8790
        return Srv()

    monkeypatch.setattr(srv_mod, "serve", fake_serve)
    monkeypatch.setattr(srv_mod, "steam_paths", SimpleNamespace(saves_dir=lambda: None))
    cfg = tmp_path / "cfg"
    argv = ["--repo", str(tmp_path), "--config-dir", str(cfg), "--site", "https://bench.example"]
    assert srv_mod.main([*argv, "--release", str(tmp_path / "release.json")]) == 0
    (app,) = built
    assert app.site == "https://bench.example" and isinstance(app.runner, srv_mod.launch.MatrixProcess)
    assert app.renderer is not None and app.bundler is not None and (cfg / security.TOKEN_FILE).is_file()
    assert "http://127.0.0.1:8790/" in capsys.readouterr().out
    assert app.key_names() == []


def test_verbose_logs_request_lines(app, capsys):
    srv = serve(app, 0, verbose=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        request(app, "GET", "/api/v1/about")
    finally:
        srv.shutdown()
        srv.server_close()
    assert '"GET /api/v1/about HTTP/1.1" 200' in capsys.readouterr().err
