# pyright: basic
import http.client
import json
import shutil
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from bg3harness import doctor as hdoctor

from bg3bench.serve import launch, panel, security, video
from bg3bench.serve.server import TOKEN_SLOT, App, parser, serve
from bg3bench.serve.settings import Settings

REPO = Path(__file__).resolve().parents[3]
FAKE = "fake-value-0123456789abcdefghij-WXYZ"
SHA = "ab" * 32
RUN_HASH = "cd" * 32


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path]] = []
        self.st: dict | None = None

    def start(self, name, matrix_file, log, *, record):
        self.started.append((name, matrix_file))
        self.record = record
        self.st = {"name": name, "running": True, "stopping": False}

    def stop(self):
        if self.st is None or not self.st["running"]:
            raise panel.PanelError("no matrix of this panel is running")
        self.st["stopping"] = True

    def state(self):
        return self.st

    def escalate(self):
        self.escalated = getattr(self, "escalated", 0) + 1
        return self.escalated == 1


class FakeBundler:
    def __init__(self) -> None:
        self.literals: list[str] = []

    def pack(self, run_dir, out_dir, *, attempt, unsubmitted_valid, literals):
        self.literals = list(literals)
        b = out_dir / "bundle"
        b.mkdir(parents=True)
        (b / "bundle.json").write_text(json.dumps({"files": [], "run_hash": RUN_HASH}))
        return SHA, b

    def validate(self, bundle_dir, *, literals, submission):
        return {"accepted": True, "flags": [], "checks": [], "submission": submission}

    def add_render(self, render_dir, out_dir, *, literals):
        self.added = (render_dir, out_dir, list(literals))
        b = out_dir / "bundle"
        (b / "render.json").write_text("{}")
        (b / "bundle.json").write_text(json.dumps({"files": [{"name": "render.json"}], "run_hash": RUN_HASH}))
        return "ef" * 32, b

    def add_video(self, mp4, out_dir, *, youtube_id):
        self.video = (mp4, out_dir, youtube_id)
        b = out_dir / "bundle"
        env = json.loads((b / "bundle.json").read_text())
        (b / "bundle.json").write_text(json.dumps({**env, "video": {"youtube_id": youtube_id}}))
        return "0a" * 32, b


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
        self.rc: int | None = None

    def state(self):
        if not self.jobs:
            return None
        j = self.jobs[-1]
        return {"run": j.run_dir.name, "source": str(j.source), "out": str(j.out), "running": self.rc is None,
                "returncode": self.rc}  # fmt: skip


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
                "created": 1792953630.85,  # manifest.v1: epoch seconds
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
        proc=lambda pid: a.procs.get(pid),  # type: ignore[attr-defined]
        doctor=lambda: DOCTOR,
    )
    a.fake_site = FakeSite()  # type: ignore[attr-defined]
    a.procs = {}  # type: ignore[attr-defined] - pid -> (argv, cwd) of the fake /proc
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
        {"matrix": "night1", "state": "unknown", "jobs": 3, "done": 1, "updated": 5,
         "stop_reason": "no process plays it; its progress.json says running"}
    ]  # fmt: skip
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


# bg3harness.doctor's report shape (doctor.report(doctor.doctor()))
DOCTOR = {
    "ready": False, "fails": 1, "warns": 0,
    "checks": [
        {"name": "ffmpeg", "status": "PASS", "detail": "/usr/bin/ffmpeg with libx264", "fix": ""},
        {"name": "shim", "status": "FAIL", "detail": "no input shim", "fix": "native/bg3sdl/build.sh"},
    ],
}  # fmt: skip
RUN_FORM = {"profiles": ["deepseek-flash"], "scenarios": ["act1_get_item"], "max_cost_usd": 2.0}


def test_start_and_stop_a_run(server):
    assert get(server, "/api/v1/run")[1] == {"v": 1, "run": None, "found": None, "can_start": True}
    code, r = post(server, "/api/v1/run/start", {**RUN_FORM, "record": False})
    assert code == 200 and r["started"]["name"] == launch.matrix_name(0.0) and server.runner.record is False
    run = get(server, "/api/v1/run")[1]["run"]
    assert run["choices"] == {"name": run["name"], "record": False, "submit_when_done": False, "auto": {}}
    ((name, f),) = server.runner.started
    assert f == server.runtime / "panel" / f"{name}.toml" and 'scenarios = ["act1_get_item"]' in f.read_text()
    code, r = post(server, "/api/v1/run/start", RUN_FORM)
    assert code == 400 and "is running" in r["error"] and len(server.runner.started) == 1
    code, r = post(server, "/api/v1/run/stop", {})
    assert code == 200 and r["run"]["stopping"] is True and not (server.runtime / "STOP").exists()


def test_start_flags(server):
    code, r = post(server, "/api/v1/run/start", {**RUN_FORM, "record": False, "submit_when_done": True})
    assert code == 400 and "needs Record" in r["error"] and server.runner.started == []
    code, r = post(server, "/api/v1/run/start", {**RUN_FORM, "record": "yes"})
    assert code == 400 and "record must be true or false" in r["error"]
    code, r = post(server, "/api/v1/run/start", {**RUN_FORM, "submit_when_done": True})
    assert code == 200 and server.runner.record is True
    assert get(server, "/api/v1/run")[1]["run"]["choices"]["submit_when_done"] is True


def found_matrix(app: App, pid: int = 31) -> None:
    """play.py bench playing runtime/panel/night2.toml, started by another panel or a terminal."""
    rt = app.runtime
    (rt / "panel").mkdir(exist_ok=True)
    (rt / "panel" / "night2.toml").write_text('name = "night2"\n')
    (rt / "runner.lock").write_text(str(pid))
    argv = ["python", "runner/play.py", "bench", "run", "--matrix", "runtime/panel/night2.toml"]
    app.procs[pid] = (argv, app.repo)  # type: ignore[attr-defined]


def test_a_matrix_this_panel_didnt_start_is_found_and_stopped(server):
    found_matrix(server)
    r = get(server, "/api/v1/run")[1]
    assert r["run"] is None and r["found"]["name"] == "night2" and r["found"]["pid"] == 31
    assert r["found"]["stopping"] is False
    code, r = post(server, "/api/v1/run/start", RUN_FORM)
    assert code == 400 and "matrix night2 is running (pid 31)" in r["error"] and server.runner.started == []
    code, r = post(server, "/api/v1/run/stop", {})
    assert code == 200 and r["found"]["stopping"] is True and (server.runtime / "STOP").is_file()
    code, r = post(server, "/api/v1/run/stop", {})
    assert code == 400 and "stopping already" in r["error"]
    server.procs.clear()  # type: ignore[attr-defined] - it ended
    assert get(server, "/api/v1/run")[1]["found"] is None
    assert post(server, "/api/v1/run/stop", {}) == (400, {"v": 1, "error": "no matrix is running"})


def test_queue_shows_running_only_for_a_matrix_a_live_process_plays(server):
    m = server.runtime / "matrix" / "night2"
    m.mkdir()
    (m / "progress.json").write_text(json.dumps({"state": "waiting", "updated": 6}))
    found_matrix(server)
    states = {r["matrix"]: r["state"] for r in get(server, "/api/v1/queue")[1]["matrices"]}
    assert states == {"night2": "waiting", "night1": "unknown"}  # night1: a stale progress.json, its process gone
    server.procs.clear()  # type: ignore[attr-defined] - night2 ended without writing its last state
    assert {r["state"] for r in get(server, "/api/v1/queue")[1]["matrices"]} == {"unknown"}
    assert post(server, "/api/v1/run/start", RUN_FORM)[0] == 200
    name = server.runner.started[0][0]
    (server.runtime / "matrix" / name).mkdir()
    (server.runtime / "matrix" / name / "progress.json").write_text(json.dumps({"state": "running", "updated": 7}))
    assert get(server, "/api/v1/queue")[1]["matrices"][0] == {"matrix": name, "state": "running", "updated": 7}
    server.runner.st["running"] = False
    assert get(server, "/api/v1/queue")[1]["matrices"][0]["state"] == "unknown"


def test_own_running_matrix_hides_found(server):
    assert post(server, "/api/v1/run/start", RUN_FORM)[0] == 200
    found_matrix(server)  # the lock of the panel's own matrix
    r = get(server, "/api/v1/run")[1]
    assert r["run"]["running"] is True and r["found"] is None


def test_doctor(server):
    assert get(server, "/api/v1/doctor")[1] == {"v": 1, "report": DOCTOR, "error": None}

    def missing():
        raise panel.PanelError("this harness has no bg3harness.doctor yet")

    server.doctor = missing
    assert get(server, "/api/v1/doctor")[1] == {
        "v": 1,
        "report": None,
        "error": "this harness has no bg3harness.doctor yet",
    }


def test_harness_doctor_runs_bg3harness_doctor(monkeypatch):
    seen = []

    def doctor():
        seen.append("doctor")
        return [hdoctor.Check("ffmpeg", "PASS", "/usr/bin/ffmpeg with libx264"),
                hdoctor.Check("shim", "FAIL", "no input shim", "native/bg3sdl/build.sh")]  # fmt: skip

    monkeypatch.setattr(panel.hdoctor, "doctor", doctor)
    assert panel.harness_doctor() == DOCTOR and seen == ["doctor"]  # the real report() of those checks
    monkeypatch.setattr(panel.hdoctor, "report", lambda checks: {"ready": "yes", "checks": []})
    with pytest.raises(panel.PanelError, match="isn't what the Doctor screen reads: ready must be a boolean"):
        panel.harness_doctor()


@pytest.mark.parametrize(
    ("rep", "why"),
    [
        ([], "not an object"),
        ({**DOCTOR, "fails": "1"}, "fails and warns integers"),
        ({**DOCTOR, "warns": True}, "fails and warns integers"),
        ({**DOCTOR, "checks": None}, "checks must be a list"),
        ({**DOCTOR, "checks": ["x"]}, "checks[0] needs string name"),
        ({**DOCTOR, "checks": [{"name": "a", "status": "PASS", "detail": "d"}]}, "checks[0] needs string name"),
        ({**DOCTOR, "checks": [{"name": "a", "status": "ok", "detail": "d", "fix": ""}]}, "status 'ok' is not PASS"),
    ],
)
def test_doctor_problem(rep, why):
    assert panel.doctor_problem(DOCTOR) is None
    assert why in (panel.doctor_problem(rep) or "")


def test_the_harness_doctor_report_has_the_screens_shape():
    """Contract with bg3harness.doctor: its report is what the Doctor screen reads."""
    checks = [hdoctor.Check("ffmpeg", "PASS", "ok"), hdoctor.Check("shim", "FAIL", "no shim", "build it")]
    rep = hdoctor.report(checks)
    assert panel.doctor_problem(rep) is None and rep["checks"][0]["fix"] == ""


def test_render_is_added_to_its_bundle_once_it_ends(server):
    server.settings.set_site_token("bgb_" + "1" * 40)
    post(server, "/api/v1/submit/prepare", {"run": "s1"})
    post(server, "/api/v1/render", {"run": "s1"})
    assert server.finish_render() is None  # still rendering
    server.renderer.rc = 1
    assert server.finish_render() is None  # a failed render adds nothing
    server.renderer.rc = 0
    assert server.housekeep() == [
        "stop: runtime/STOP didn't end the matrix in time, sent it SIGTERM",
        f"render: s1: bundle {'ef' * 32}",
    ]
    render_dir, out_dir, literals = server.bundler.added
    assert out_dir == server.runtime / "submissions" / "s1" / "1" and render_dir == out_dir / "render-1"
    assert "bgb_" + "1" * 40 in literals  # the scrub gets the panel's secrets
    prepared = get(server, "/api/v1/submissions")[1]["submissions"]["s1"]["prepared"]
    assert prepared["bundle_sha256"] == "ef" * 32 and prepared["render"]["dir"] == str(render_dir)
    assert server.housekeep() == []  # added once
    code, r = post(server, "/api/v1/render", {"run": "s1"})
    assert code == 400 and "has its video's render already" in r["error"]
    # Upload names the new bundle hash
    code, r = post(server, "/api/v1/submit/upload", {"run": "s1", "bundle_sha256": SHA})
    assert code == 400 and "changed since you saw it" in r["error"]


def test_render_for_a_bundle_validated_again_is_reported(server):
    post(server, "/api/v1/submit/prepare", {"run": "s1"})
    post(server, "/api/v1/render", {"run": "s1"})
    post(server, "/api/v1/submit/prepare", {"run": "s1"})  # a new bundle, 2/
    server.renderer.rc = 0
    server.runner = None
    (line,) = server.housekeep()
    assert line.startswith("render: s1: the render was made for a bundle that is no longer the prepared one")


def test_tick_prepares_a_submit_when_done_run(server):
    assert post(server, "/api/v1/run/start", {**RUN_FORM, "submit_when_done": True})[0] == 200
    name = server.runner.started[0][0]
    m = server.runtime / "matrix" / name
    m.mkdir()
    (m / "queue.jsonl").write_text(json.dumps({"type": "end", "result": {"run_id": "s1-r001", "valid": True}}) + "\n")
    assert server.tick() == [{"matrix": name, "run": "s1", "t": 0.0, "bundle_sha256": SHA, "accepted": True}]
    assert server.tick() == []
    subs = get(server, "/api/v1/submissions")[1]["submissions"]
    assert subs["s1"]["prepared"]["bundle_sha256"] == SHA and subs["s1"]["submission"] is None  # never uploaded
    assert server.fake_site.calls == []


def test_ticker_runs_tick_until_stopped(app, capsys):
    class Ev:
        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            return self.waits > 2  # two passes, then set

    out = iter([[{"run": "d1", "accepted": True}], [{"run": "d2", "error": "pack refused it"}]])
    app.tick = lambda: next(out)
    house = iter([["stop: sent it SIGTERM"], []])
    app.housekeep = lambda: next(house)
    from bg3bench.serve import server as srv_mod

    srv_mod.ticker(app, Ev(), every=0).join(timeout=10)  # type: ignore[arg-type]
    assert capsys.readouterr().out.splitlines() == [
        "stop: sent it SIGTERM", "submit when done: d1: validated, accepted=True",
        "submit when done: d2: pack refused it",
    ]  # fmt: skip


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
    yt = "https://youtu.be/dQw4w9WgXcQ"
    code, r = post(server, "/api/v1/submit/upload", {"run": "s1", "bundle_sha256": SHA})
    assert code == 400 and "render the evidence video first" in r["error"] and server.fake_site.calls == []
    code, r = post(server, "/api/v1/submit/video", {"run": "s1", "bundle_sha256": SHA, "youtube_url": yt})
    assert code == 400 and "render the evidence video first" in r["error"]
    assert post(server, "/api/v1/render", {"run": "s1"})[0] == 200
    server.renderer.rc = 0
    server.runner = None
    assert server.housekeep() == [f"render: s1: bundle {'ef' * 32}"]  # the ticker adds it to the bundle
    prepared = get(server, "/api/v1/submissions")[1]["submissions"]["s1"]["prepared"]
    assert prepared["final"] is True and prepared["verdict"]["submission"] is True
    code, r = post(server, "/api/v1/submit/upload", {"run": "s1", "bundle_sha256": "ef" * 32})
    assert code == 400 and "add the YouTube link" in r["error"] and server.fake_site.calls == []  # no video yet
    mp4 = Path(prepared["render"]["dir"]) / video.VIDEO
    mp4.parent.mkdir()
    mp4.write_bytes(b"mp4")  # the renderer's output
    code, r = post(server, "/api/v1/submit/video", {"run": "s1", "bundle_sha256": "ef" * 32, "youtube_url": yt})
    assert code == 200 and r["video"] == {"youtube_id": "dQw4w9WgXcQ"} and r["bundle_sha256"] == "0a" * 32
    assert server.bundler.video == (mp4, mp4.parent.parent, "dQw4w9WgXcQ")
    code, r = post(server, "/api/v1/submit/upload", {"run": "s1", "bundle_sha256": "0a" * 32})
    assert code == 200 and r["id"] == "sub1"
    assert server.fake_site.calls[0][2] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # bundle.json's video
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
    code, r = post(server, "/api/v1/submit/upload", {"run": "s1", "bundle_sha256": SHA})
    assert code == 400 and "no site URL" in r["error"] and server.fake_site.calls == []


def test_render_needs_a_prepared_bundle(server):
    code, r = post(server, "/api/v1/render", {"run": "s1"})
    assert code == 400 and "validate the run first" in r["error"]
    post(server, "/api/v1/submit/prepare", {"run": "s1"})
    code, r = post(server, "/api/v1/render", {"run": "s1"})
    assert code == 200 and r["render"]["run"] == "s1" and r["render"]["running"] is True
    (job,) = server.renderer.jobs
    assert job.run_hash == RUN_HASH and job.run_id == "s1-r001" and job.site == "bench.example"
    assert job.source == server.runtime / "submissions" / "s1" / "1" / "bundle"  # the packed, scrubbed record
    assert job.out == server.runtime / "submissions" / "s1" / "1" / "render-1"
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
    assert "no bundler" in post(server, "/api/v1/submit/video", {"run": "s1"})[1]["error"]
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
    monkeypatch.setattr(srv_mod, "ticker", lambda app, stop: stopped.append(stop))
    stopped: list = []
    monkeypatch.setattr(srv_mod, "steam_paths", SimpleNamespace(saves_dir=lambda: None))
    cfg = tmp_path / "cfg"
    argv = ["--repo", str(tmp_path), "--config-dir", str(cfg), "--site", "https://bench.example"]
    assert srv_mod.main([*argv, "--release", str(tmp_path / "release.json")]) == 0
    (app,) = built
    assert app.site == "https://bench.example" and isinstance(app.runner, srv_mod.launch.MatrixProcess)
    assert app.renderer is not None and app.bundler is not None and (cfg / security.TOKEN_FILE).is_file()
    assert "http://127.0.0.1:8790/" in capsys.readouterr().out
    assert app.key_names() == []
    assert stopped[0].is_set()  # the ticker's event is set on the way out


def test_release_defaults_to_the_suites_release_file():
    from bg3bench.serve import server as srv_mod

    want = srv_mod.suite.PACKAGE_ROOT / "releases" / f"{srv_mod.suite.suite_version()}.json"
    assert srv_mod.parser().parse_args([]).release == want


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
