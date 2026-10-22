# pyright: basic
"""The Launcher's control requests over real HTTP against the real server, Supervisor, HarnessRunner and ClientAgents
(the client profiles read from disk), offline: every case here is refused before any agent or game starts, so no
model, game or key is needed.

    O1 (B1) a pi profile without a cost cap: the first Start fails as a session error the UI shows; the playthrough
       has no session, so Start is allowed again (not "resume it") and asks for session 1 again
    O4 (B4) a pi profile with a cap but the global budget used up: refused with the budget's reason, nothing played
    O5 (B2) a Start refused because another playthrough runs (409) leaves the recorder as the running one set it
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from bg3play import config
from bg3play import supervisor as sup
from bg3play.adapters.harness_runner import ClientAgents, HarnessRunner
from bg3play.hub import Hub
from bg3play.recorder import Recorder, RecordSpec
from bg3play.server import App, serve
from bg3play.store import RunStore

pytestmark = pytest.mark.e2e

WAIT_S = 60
PI_PROFILE = """client = "pi"
model = "anthropic/stub-model"

[endpoint]
provider = "openrouter"
base_url = "http://127.0.0.1:9/v1"
api_key_env = "BG3_STUB_KEY"

[model_info]
input = ["text"]
reasoning = true
context_window = 200000
max_tokens = 16000
cost = { input = 4.0, output = 20.0, cache_read = 0.2, cache_write = 5.0 }
"""


class NoGame:
    """The runner refuses before it touches the game; any use is a test failure."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the game was used ({name}) by a session that should not start")


def post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def wait_idle(url: str) -> dict[str, Any]:
    end = time.monotonic() + WAIT_S
    while (s := get(f"{url}/api/v1/supervisor")["supervisor"])["state"] != "idle":
        assert time.monotonic() < end, "timed out waiting for the supervisor"
        time.sleep(0.05)
    return s


@pytest.fixture
def app(tmp_path: Path):
    runtime, pts, profiles = tmp_path / "runtime", tmp_path / "playthroughs", tmp_path / "profiles"
    for d in (runtime, profiles):
        d.mkdir()
    for pt in ("a", "b"):
        (pts / pt).mkdir(parents=True)
        config.save(pts / pt, config.validate({"v": 1, "name": pt}))
    (profiles / "stub-pi.toml").write_text(PI_PROFILE)
    agents = ClientAgents(profiles, tmp_path, time.time, system_text=lambda p, c: p)
    runner = HarnessRunner(runtime, pts, agents, NoGame(), lambda mem: NoGame(), time.time)  # type: ignore[arg-type]
    store = RunStore(runtime, pts)
    hub = Hub(store, generation="ctl")
    supervisor = sup.Supervisor(runner, sup.NoKeeper(), runtime, pts, time.time,
                                played=lambda pt: len(store.session_logs(pt)))  # fmt: skip
    recorder = Recorder(RecordSpec(runtime / "video"), probe=lambda: None, clock=time.time)
    srv = serve(App(store, hub, None, recorder=recorder, supervisor=supervisor, profiles=profiles), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "runtime": runtime, "sup": supervisor,
               "recorder": recorder}  # fmt: skip
    finally:
        hub.close()
        srv.shutdown()
        srv.server_close()


def test_a_failed_first_start_can_be_started_again(app):
    url = app["url"]
    launch = {"profile": "stub-pi", "hours": 1}  # no max_cost: pi refuses to play
    assert post(f"{url}/api/v1/playthroughs/a/start", launch)[0] == 200
    s = wait_idle(url)
    assert s["detail"] == "session error: client profile stub-pi: pi needs a cost cap (session.max_cost_usd > 0)"
    assert get(f"{url}/api/v1/playthroughs/a/config")["sessions"] == 0
    code, body = post(f"{url}/api/v1/playthroughs/a/resume", {})
    assert code == 409 and "no sessions to resume" in body["error"]
    assert post(f"{url}/api/v1/playthroughs/a/start", launch)[0] == 200  # not "already has 1 sessions"
    wait_idle(url)
    hist = sup.read_history(sup.history_path(app["runtime"], "a"))
    assert [(h["session"], h["outcome"]) for h in hist] == [("?", "error"), ("?", "error")]


def test_a_used_up_budget_refuses_a_paid_session(app):
    url = app["url"]
    (app["runtime"] / "budget.json").write_text('{"cap": 1.0, "spent": 1.0}')
    assert post(f"{url}/api/v1/playthroughs/a/start", {"profile": "stub-pi", "max_cost": 2, "hours": 1})[0] == 200
    s = wait_idle(url)
    assert s["detail"] == "session error: the global budget is used up ($1.00 of $1)"
    assert not (app["runtime"] / "sessions" / "a" / "runner.json").exists()  # nothing played


def test_a_refused_start_keeps_the_running_playthroughs_recording(app):
    url, supervisor, recorder = app["url"], app["sup"], app["recorder"]
    gate, entered = threading.Event(), threading.Event()

    class Holding:
        def run_session(self, req, stop):
            entered.set()
            gate.wait(WAIT_S)
            return sup.SessionResult(f"S{req.session:03d}", "stopped")

    supervisor.runner = Holding()
    try:
        assert post(f"{url}/api/v1/playthroughs/a/start", {"recording": True, "hours": 1})[0] == 200
        assert entered.wait(WAIT_S) and recorder.enabled
        code, body = post(f"{url}/api/v1/playthroughs/b/start", {"recording": False})
        assert (code, body["error"]) == (409, "a is running")
        assert recorder.enabled  # a's recording goes on
    finally:
        gate.set()
    wait_idle(url)
