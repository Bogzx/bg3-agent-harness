"""The Watch screen's operator messages against the real server: the panel only when the playthrough allows them, a
message sent from the page goes queued -> delivered when the bridge's delivered row lands, and a --token server
refuses a page without the token. The Launcher's checkbox reaches the session's config."""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from bg3play.hub import Hub
from bg3play.server import DEFAULT_WEB, App, serve
from playwright.sync_api import Page, expect

from ..conftest import PT
from .conftest import Site

pytestmark = pytest.mark.e2e

DELIVERED_AT = 1792312702.5  # in S096, after the fixture's last tool call (1792312701.9)


def toml(site: Site) -> Path:
    return site["store"].playthroughs_dir / PT / "playthrough.toml"


@pytest.fixture
def allowed(site: Site) -> Iterator[Path]:
    """The fixture playthrough with operator messages on; yields its session dir (inbox and delivered files)."""
    toml(site).write_text("[operator]\nmessages = true\n")
    d = site["store"].session_dir(PT)
    yield d
    toml(site).unlink()
    for name in ("operator.jsonl", "operator.delivered.jsonl"):
        (d / name).unlink(missing_ok=True)


def append(path: Path, row: dict[str, object]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def test_no_panel_when_the_playthrough_does_not_allow_messages(page: Page, site: Site):
    page.goto(f"{site['url']}/#/watch/{PT}")
    expect(page.get_by_text("Agent says")).to_be_visible()
    expect(page.get_by_label("Operator messages")).to_have_count(0)


def test_send_then_delivered_with_the_agents_next_call(page: Page, site: Site, allowed: Path):
    page.goto(f"{site['url']}/#/watch/{PT}")
    panel = page.get_by_label("Operator messages")
    expect(panel).to_be_visible()
    expect(panel.get_by_text("No messages yet.")).to_be_visible()
    box = panel.get_by_label("Message for the agent")
    box.fill("x" * 1001)
    expect(panel.locator(".count")).to_have_text("1001/1000")
    panel.get_by_role("button", name="Send").click()
    expect(panel.locator(".senderr")).to_have_text("At most 1000 characters")
    box.fill("Try the north door first.")
    panel.get_by_role("button", name="Send").click()
    item = panel.locator(".ops li").first
    expect(item.locator(".text")).to_have_text("Try the north door first.")
    expect(item.locator(".status")).to_have_text("queued (next session)")  # the fixture's S096 has ended
    expect(box).to_have_value("")
    rows = [json.loads(line) for line in (allowed / "operator.jsonl").read_text().splitlines()]
    assert [r["text"] for r in rows] == ["Try the north door first."]
    mid = rows[0]["id"]

    # the bridge hands it over in a travel_to result, logs that call, and the agent looks next
    append(allowed / "operator.delivered.jsonl", {"id": mid, "t": DELIVERED_AT, "session": "S096", "tool": "travel_to"})
    for t, tool in ((DELIVERED_AT + 0.5, "mcp__bg3__travel_to"), (DELIVERED_AT + 3, "mcp__bg3__look")):
        append(site["store"].actions_path, {"t": t, "tool": tool, "args": {}, "ms": 9, "result": "ok"})
    site["hub"].poll()
    expect(item.locator(".status")).to_contain_text("delivered ")
    expect(item.locator(".status")).to_contain_text(" in S096 via travel_to")
    expect(item.locator(".next")).to_contain_text("then look")

    page.reload()  # the snapshot has it delivered too
    expect(page.get_by_label("Operator messages").locator(".ops li .status").first).to_contain_text("via travel_to")


def test_a_token_server_wants_the_token(page: Page, site: Site, allowed: Path, errors: list[str]):
    hub = Hub(site["store"], generation="optok")
    srv = serve(App(site["store"], hub, DEFAULT_WEB, supervisor=site["sup"], token="s3cret"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#/watch/{PT}")
        panel = page.get_by_label("Operator messages")
        panel.get_by_label("Message for the agent").fill("hello")
        panel.get_by_role("button", name="Send").click()
        expect(panel.locator(".senderr")).to_have_text("needs the operator token")
        assert not (allowed / "operator.jsonl").exists()
        assert [e for e in errors if "403" in e]  # the refused POST's own console line
        errors.clear()
    finally:
        hub.close()
        srv.shutdown()
        srv.server_close()


def test_story_without_the_session_list_still_shows_the_story(page: Page, site: Site, errors: list[str]):
    # the guided line is optional: a failed /sessions must not replace the chapters with an error
    page.route(f"**/api/v1/playthroughs/{PT}/sessions", lambda r: r.fulfill(status=500, json={"error": "boom"}))
    with page.expect_response(f"**/api/v1/playthroughs/{PT}/sessions"):
        page.goto(f"{site['url']}/#/story/{PT}")
    expect(page.get_by_role("button", name="Chapters")).to_be_visible()
    expect(page.locator(".panel h2").first).to_contain_text("Chapter")
    expect(page.get_by_text("boom")).to_have_count(0)
    expect(page.locator(".error")).to_have_count(0)
    errors.clear()  # the 500's own console line


def test_the_launcher_allows_messages_for_the_session(page: Page, site: Site):
    runner = site["runner"]
    page.goto(f"{site['url']}/#/launch")
    name = page.locator("input[list=pts]")
    name.fill("e2e-operator")
    name.press("Tab")
    expect(page.get_by_text("new: Start plays session 1")).to_be_visible()
    check = page.get_by_label("Allow operator messages (the run is marked as guided)")
    expect(check).not_to_be_checked()  # off by default
    check.check()
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".state")).to_contain_text("e2e-operator")
    assert site["sup"].thread is not None
    site["sup"].thread.join(10)
    req = runner.requests[-1]
    assert (req.playthrough, req.config.operator.messages) == ("e2e-operator", True)
