"""The screens under the control paths the fixture run alone doesn't reach: Stop on a running playthrough, and a
server restart under an open Watch page (the stream's reset)."""

import json
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
from bg3play.hub import Hub
from bg3play.server import DEFAULT_WEB, App, serve
from bg3play.store import RunStore
from bg3play.supervisor import SessionRequest, SessionResult
from playwright.sync_api import Page, expect

from ..conftest import FIXTURE, PT
from .conftest import Site

pytestmark = pytest.mark.e2e


class HeldRunner:
    """A SessionRunner whose session plays until Stop, then finishes when the test lets it (`release`), so the
    page can be seen "stopping" first."""

    def __init__(self):
        self.playing, self.release = threading.Event(), threading.Event()

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        self.playing.set()
        assert stop.wait(30), "Stop never reached the runner"
        assert self.release.wait(30), "the test never released the session"
        return SessionResult(f"S{req.session:03d}", "stopped", None)


def test_stop_from_the_launcher_ends_the_session_and_offers_resume(page: Page, site: Site):
    sup, held = site["sup"], HeldRunner()
    instant, sup.runner = sup.runner, held
    try:
        page.goto(f"{site['url']}/#/launch")
        name = page.locator("input[list=pts]")
        name.fill("e2e-stop")
        name.press("Tab")
        expect(page.get_by_text("new: Start plays session 1")).to_be_visible()
        page.get_by_role("button", name="Start").click()
        assert held.playing.wait(10)
        state = page.locator(".state")
        expect(state).to_contain_text("running · e2e-stop · S001 · playing")
        page.get_by_role("button", name="Stop after this session").click()
        expect(state).to_contain_text("stopping · e2e-stop · S001 · finishing the current session")
        expect(page.get_by_text("e2e-stop is stopping; one playthrough runs at a time.")).to_be_visible()
        held.release.set()
        expect(state).to_contain_text("idle · e2e-stop · S001 · stopped")
        # the session counts: the next is a resume (S002), not a second session 1
        expect(page.get_by_text("1 sessions so far: Resume plays the next one")).to_be_visible()
        expect(page.get_by_role("button", name="Resume")).to_be_visible()
        expect(page.get_by_role("button", name="Start")).to_have_count(0)
        assert [(h["session"], h["outcome"]) for h in sup.history("e2e-stop")] == [("S001", "stopped")]
    finally:
        held.release.set()
        if sup.thread is not None:
            sup.thread.join(10)
        sup.runner = instant


def test_watch_reloads_after_a_server_restart(page: Page, tmp_path: Path):
    """A restart starts a new stream generation: the page's EventSource reconnects with an id the new server
    doesn't know, gets `reset`, reloads its snapshot and follows the new stream (no manual reload)."""
    run = tmp_path / "run"
    shutil.copytree(FIXTURE, run)
    store = RunStore(run / "runtime", run / "playthroughs")

    def start(generation: str, port: int = 0):
        hub = Hub(store, generation=generation)
        hub.poll()
        srv = serve(App(store, hub, DEFAULT_WEB, keepalive_s=5), "127.0.0.1", port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return hub, srv

    hub, srv = start("first")
    port = srv.server_address[1]
    try:
        page.goto(f"http://127.0.0.1:{port}/#/watch/{PT}")
        expect(page.get_by_text("connected", exact=True)).to_be_visible()
        srv.shutdown()
        hub.close()  # ends the open stream
        srv.server_close()
        expect(page.get_by_text("offline", exact=True)).to_be_visible()
        # S096 ended at 1792312705.6 and the snapshot keeps the session's window
        line: dict[str, Any] = {
            "t": 1792312703.0,
            "tool": "mcp__bg3__after_restart",
            "args": {},
            "ms": 3,
            "result": "new server",
        }
        with store.actions_path.open("a") as f:  # written while the server was down: only the new snapshot has it
            f.write(json.dumps(line) + "\n")
        with page.expect_request(f"**/playthroughs/{PT}/watch", timeout=30000):  # EventSource retries on its own
            hub, srv = start("second", port)
        expect(page.get_by_text("connected", exact=True)).to_be_visible()
        expect(page.locator(".calls .tool").first).to_have_text("after_restart")
        live = dict(line, t=1792312704.0, tool="mcp__bg3__on_the_new_stream")
        with store.actions_path.open("a") as f:
            f.write(json.dumps(live) + "\n")
        hub.poll()
        expect(page.locator(".calls .tool").first).to_have_text("on_the_new_stream")
    finally:
        hub.close()
        srv.shutdown()
        srv.server_close()
