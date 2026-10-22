"""The built web app (web/dist) served by the real server over a copy of the fixture run, for Playwright.

    uv run --project packages/playthrough pytest -m e2e      (after: cd packages/playthrough/web && npm run build)

One headless chromium for the module (RAM); each test gets a fresh page that records console errors. The fixture
run gets git memory commits and a 4-second libx264 test recording placed 60 s into S096.
"""

import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import pytest
from bg3play.hub import Hub
from bg3play.recorder import Capture, Recorder, RecordSpec
from bg3play.server import DEFAULT_WEB, App, serve
from bg3play.store import RunStore
from bg3play.supervisor import NoKeeper, SessionRequest, SessionResult, Supervisor
from playwright.sync_api import Browser, ConsoleMessage, Error, Page

from ..conftest import FIXTURE, PT, git
from .webbuild import build_problem

S096_START = 1792311134.6876369
VIDEO_AT = 1792311194.0  # 60 s into S096


class InstantRunner:
    """A SessionRunner that plays nothing: it records the request and reports a stopped session."""

    def __init__(self):
        self.requests: list[SessionRequest] = []

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        self.requests.append(req)
        return SessionResult(f"S{req.session:03d}", "stopped", None)


class Site(TypedDict):
    url: str
    store: RunStore
    hub: Hub
    runner: InstantRunner
    sup: Supervisor
    video: Path


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    if problem := build_problem(DEFAULT_WEB.parent):
        pytest.fail(problem)
    root = tmp_path_factory.mktemp("e2e")
    run = root / "run"
    shutil.copytree(FIXTURE, run)
    mem = run / "playthroughs" / PT / "memory"
    git(mem, "init", "-q")
    git(mem, "add", "-A")
    git(mem, "commit", "-q", "-m", "S096 memory-keeper: decisions.md", date=1792312786)
    video = run / "runtime" / "video"
    rec = Recorder(
        RecordSpec(
            video,
            width=640,
            height=360,
            encoder="libx264",
            segment_s=2,
            duration_s=4,
            live=False,
            source=("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15"),
        ),
        probe=lambda: Capture(),
        clock=lambda: VIDEO_AT,
    )
    rec.poll()
    assert rec.run is not None and rec.run.proc.wait(60) == 0
    rec.poll()
    store = RunStore(run / "runtime", run / "playthroughs")
    hub = Hub(store, generation="e2e")
    hub.poll()
    runner = InstantRunner()
    sup = Supervisor(
        runner,
        NoKeeper(),
        store.runtime,
        store.playthroughs_dir,
        time.time,
        lambda v: hub.publish("supervisor", v),
        played=lambda pt: len(store.session_logs(pt)),
    )
    profiles = root / "profiles"
    profiles.mkdir()
    (profiles / "codex.toml").write_text('client = "codex"\nmodel = "gpt-5.5"\n')
    shutil.copy(FIXTURE.parents[3].parent / "profiles" / "deepseek-flash.toml", profiles)  # the repo's pi profile
    srv = serve(
        App(store, hub, DEFAULT_WEB, keepalive_s=5, video_dir=video, supervisor=sup, profiles=profiles), "127.0.0.1", 0
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {
        "url": f"http://127.0.0.1:{srv.server_address[1]}",
        "store": store,
        "hub": hub,
        "runner": runner,
        "sup": sup,
        "video": video,
    }
    hub.close()
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    from playwright.sync_api import sync_playwright

    if problem := build_problem(DEFAULT_WEB.parent):  # every page opens the built app: not an old build of it
        pytest.fail(problem)

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def errors() -> list[str]:
    """The page's console errors and uncaught exceptions; a test clears the ones it causes on purpose."""
    return []


@pytest.fixture
def page(browser: Browser, errors: list[str]) -> Iterator[Page]:
    ctx = browser.new_context(viewport={"width": 1440, "height": 950}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(10000)

    def console(m: ConsoleMessage) -> None:
        if m.type == "error":
            errors.append(m.text)

    def pageerror(e: Error) -> None:
        errors.append(str(e))

    pg.on("console", console)
    pg.on("pageerror", pageerror)
    yield pg
    ctx.close()
    assert errors == []
