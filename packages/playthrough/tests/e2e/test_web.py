import json

import pytest
from playwright.sync_api import expect

from ..conftest import PT
from .conftest import VIDEO_AT

pytestmark = pytest.mark.e2e


def no_overflow(page, width: int) -> None:
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_watch_snapshot_map_and_live_stream(page, site):
    page.goto(f"{site['url']}/#/watch/{PT}")
    expect(page.get_by_text("Agent says")).to_be_visible()
    expect(page.locator(".party").get_by_text("Shadowheart")).to_be_visible()
    expect(page.get_by_text("connected", exact=True)).to_be_visible()
    # the breadcrumb: the latest level's trail, and the level picker
    expect(page.locator("svg polyline")).to_have_count(1)
    page.get_by_label("Level").select_option("END_Main")
    expect(page.locator("svg polyline")).to_have_count(2)  # the 300 s gap splits the walk
    # a tool call written while the page is open arrives through SSE
    line = {"t": 1792312800.0, "tool": "mcp__bg3__e2e_probe", "args": {"x": 1}, "ms": 7, "result": "live e2e"}
    with site["store"].actions_path.open("a") as f:
        f.write(json.dumps(line) + "\n")
    site["hub"].poll()
    expect(page.locator(".calls .tool").first).to_have_text("e2e_probe")
    no_overflow(page, 1440)


def test_memory_browser(page, site):
    page.goto(f"{site['url']}/#/memory/{PT}/decisions.md")
    expect(page.get_by_text("Memory commits")).to_be_visible()
    expect(page.get_by_text("S096 memory-keeper: decisions.md").first).to_be_visible()
    expect(page.locator(".md h1, .md h2").first).to_be_visible()
    page.locator(".hist button.chip", has_text="S096").click()   # that commit's change to this file
    expect(page.locator(".diff .add").first).to_be_visible()
    page.get_by_role("button", name="back to the file").click()
    expect(page.locator(".md h1, .md h2").first).to_be_visible()


def test_stats(page, site):
    page.goto(f"{site['url']}/#/stats/{PT}")
    expect(page.get_by_text("Goal scorecard")).to_be_visible()
    expect(page.locator("table.goals th").get_by_text("S096")).to_be_visible()
    expect(page.get_by_text("game polling is off")).to_be_visible()   # the e2e server runs without --game
    no_overflow(page, 1440)


def test_story_party_from_the_snapshot(page, site):
    page.goto(f"{site['url']}/#/story/{PT}")
    expect(page.get_by_text("2 chapters")).to_be_visible()
    page.get_by_role("button", name="Party").click()
    expect(page.get_by_text("The Emperor")).to_be_visible()
    no_overflow(page, 1440)


def test_timeline(page, site):
    page.goto(f"{site['url']}/#/timeline/{PT}")
    expect(page.get_by_text("choices", exact=False).first).to_be_visible()
    row = page.locator("button.row").first
    row.click()
    expect(row).to_have_attribute("aria-expanded", "true")


def test_launcher_resume_offer_and_start_a_new_playthrough(page, site):
    page.goto(f"{site['url']}/#/launch/{PT}")
    expect(page.get_by_role("button", name="Resume")).to_be_visible()  # the runner's 3 sessions count
    name = page.locator("input[list=pts]")
    name.fill("e2e-new")
    name.press("Tab")  # change: the form loads e2e-new's (default) config
    expect(page.get_by_text("new: Start plays session 1")).to_be_visible()
    page.get_by_label("Client profile").select_option("codex")
    page.locator("textarea").fill("a nervous halfling bard")
    page.get_by_label("Tactician").check()
    page.get_by_label("Record video").uncheck()
    name.dispatch_event("change")  # a repeated change of the same name keeps the picks
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".state")).to_contain_text("e2e-new")
    expect(page.locator(".state")).to_contain_text("idle")  # the instant runner's session ended at once
    req = site["runner"].requests[-1]
    cfg = req.config
    assert (req.playthrough, req.session, cfg.agent.profile, cfg.character.brief, cfg.difficulty,
            cfg.recording.enabled) == ("e2e-new", 1, "codex", "a nervous halfling bard", "Tactician", False)
    assert (site["store"].playthroughs_dir / "e2e-new" / "playthrough.toml").exists()


def test_launcher_pi_profile_needs_a_cost_cap(page, site):
    page.goto(f"{site['url']}/#/launch")
    name = page.locator("input[list=pts]")
    name.fill("e2e-pi")
    name.press("Tab")
    expect(page.get_by_text("new: Start plays session 1")).to_be_visible()
    page.get_by_label("Client profile").select_option("deepseek-flash")
    expect(page.get_by_text("pi plays through deepseek")).to_be_visible()
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".error")).to_have_text("A pi profile needs a cost cap (USD per session)")
    page.get_by_label("Cost cap (USD)").fill("2.5")
    page.get_by_role("button", name="Start").click()
    expect(page.locator(".state")).to_contain_text("e2e-pi")
    cfg = site["runner"].requests[-1].config
    assert (cfg.agent.profile, cfg.session.max_cost_usd) == ("deepseek-flash", 2.5)
    no_overflow(page, 1440)


def test_overlay_transparent_and_panel_flags(browser, site):
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = ctx.new_page()
    page.goto(f"{site['url']}/overlay?pt={PT}")
    expect(page.locator(".box.goal")).to_be_visible()
    expect(page.locator(".box.party")).to_be_visible()
    expect(page.locator(".box.tool")).to_be_visible()
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgba(0, 0, 0, 0)"
    assert page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]") == [
        1920, 1080]
    page.goto(f"{site['url']}/overlay?pt={PT}&panels=party")
    expect(page.locator(".box.party")).to_be_visible()
    expect(page.locator(".box")).to_have_count(1)
    ctx.close()


def test_replay_steps_events_and_plays_the_recording(page, site):
    page.goto(f"{site['url']}/#/replay/{PT}/S096")
    expect(page.get_by_text("At the playhead")).to_be_visible()
    expect(page.get_by_text("Not recorded at this time")).to_be_visible()
    time_el = page.locator(".time")
    before = time_el.inner_text()
    page.get_by_label("Next event").click()
    expect(time_el).not_to_have_text(before)
    expect(page.locator(".events li.now")).to_be_visible()
    # seek 1.5 s into the recording: the first segment, 1.5 s in
    page.evaluate(f"""() => {{ const r = document.querySelector('input[type=range]');
        r.value = String({VIDEO_AT + 1.5}); r.dispatchEvent(new Event('input', {{bubbles: true}})); }}""")
    page.wait_for_function("() => { const v = document.querySelector('video');"
                           " return v && v.readyState >= 2 && !v.seeking && v.currentTime > 0 }")
    src = page.evaluate("document.querySelector('video').src")
    assert src.endswith("-0000.mp4")
    assert abs(page.evaluate("document.querySelector('video').currentTime") - 1.5) < 0.2
    expect(page.locator(".screen h2 .right")).to_have_text(src.rsplit("/", 1)[1])
