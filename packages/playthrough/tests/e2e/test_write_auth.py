# pyright: basic
"""Who may write (POST start/resume/stop) to the real server: another site's page, a DNS-rebound one, a token."""

import http.client
import threading
from urllib.parse import urlparse

import pytest
from bg3play.hub import Hub
from bg3play.server import App, serve

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
