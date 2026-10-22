# pyright: basic
"""Who may write (POST start/resume/stop) to the real server: another site's page, a DNS-rebound one, a token, and
the Launcher of a token server opened with and without ?token=."""

import http.client
import threading
from urllib.parse import urlparse

import pytest
from bg3play.hub import Hub
from bg3play.server import DEFAULT_WEB, App, serve
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def post(url: str, path: str, headers: dict[str, str]) -> int:
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
    c.request("POST", path, b"{}", {"Content-Type": "application/json", **headers})
    code = c.getresponse().status
    c.close()
    return code


def test_writes_from_another_site_are_refused(site):
    port = urlparse(site["url"]).port
    stop = "/api/v1/playthroughs/nobody-runs-this/stop"  # allowed: 409 (not running); refused: 403
    assert post(site["url"], stop, {}) == 409
    assert post(site["url"], stop, {"Origin": "http://evil.example"}) == 403
    # DNS rebinding: evil.example resolves to 127.0.0.1, so the page's Origin and Host agree, both attacker's
    rebound = {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}
    assert post(site["url"], stop, rebound) == 403
    assert post(site["url"], stop, {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}) == 409


def test_writes_with_a_token_server(site):
    hub = Hub(site["store"], generation="tok")
    srv = serve(App(site["store"], hub, None, supervisor=site["sup"], token="s3cret"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    stop = "/api/v1/playthroughs/nobody-runs-this/stop"
    try:
        assert post(url, stop, {}) == 403  # a token server wants it from localhost too
        assert post(url, stop, {"Authorization": "Bearer nope"}) == 403
        assert post(url, stop, {"Authorization": "Bearer s3cret", "Host": "box.tailnet:8780"}) == 409
    finally:
        hub.close()
        srv.shutdown()
        srv.server_close()


def test_the_launcher_sends_the_token_it_was_opened_with(page, site):
    hub = Hub(site["store"], generation="tokui")
    srv = serve(App(site["store"], hub, DEFAULT_WEB, supervisor=site["sup"], token="s3 cret"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    runner = site["runner"]

    def start() -> None:
        name = page.locator("input[list=pts]")
        name.fill("e2e-token")
        name.press("Tab")
        expect(page.get_by_text("new: Start plays session 1")).to_be_visible()
        page.get_by_role("button", name="Start").click()

    try:
        played = len(runner.requests)
        page.goto(f"{url}/#/launch")
        start()
        expect(page.locator(".error")).to_have_text("bad or missing token")
        assert len(runner.requests) == played
        assert [e for e in page.errors if "403" in e]  # the refused POST's own console line
        page.errors.clear()
        page.goto(f"{url}/?token=s3%20cret#/launch")
        expect(page).to_have_url(f"{url}/#/launch")  # the address bar (and history) lose it, the tab keeps it
        page.reload()
        start()
        expect(page.locator(".state")).to_contain_text("e2e-token")
        expect(page.locator(".error")).to_have_count(0)
        site["sup"].thread.join(10)  # the accepted start's session (the instant runner's) has run
        assert (runner.requests[-1].playthrough, runner.requests[-1].session) == ("e2e-token", 1)
    finally:
        if site["sup"].thread is not None:
            site["sup"].thread.join(10)
        hub.close()
        srv.shutdown()
        srv.server_close()
