# pyright: basic
"""The panel's built web app (packages/bench/web/dist) served by the real bg3bench-serve, in headless chromium.

    cd packages/bench/web && npm ci && npm run build; uv run pytest -m e2e packages/bench/tests/test_panel_web.py

Every screen renders without a console error; New run plans a job and shows the YouTube note; Settings saves a key
through the token the server put into index.html, and the page shows it masked.
"""

import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from bg3bench.serve import panel
from bg3bench.serve.server import App, serve
from bg3bench.serve.settings import Settings

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "packages" / "bench" / "web"
SOURCES = ("src", "index.html", "package.json", "package-lock.json", "vite.config.ts", "svelte.config.js")
FIX = "run `npm ci && npm run build` in packages/bench/web"


def build_problem(web: Path) -> str | None:
    """Why web/dist can't be tested (None: it is built from the current sources)."""
    built = web / "dist" / "index.html"
    if not built.is_file():
        return f"no built web app at {built.parent}: {FIX}"
    files = [p for name in SOURCES for p in ([web / name] if (web / name).is_file() else (web / name).rglob("*"))]
    newest = max((p for p in files if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
    if newest is not None and newest.stat().st_mtime > built.stat().st_mtime:
        return f"the built web app is older than {newest.relative_to(web)}: {FIX}"
    return None


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    if why := build_problem(WEB):
        pytest.fail(why)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    for name in ("deepseek-flash", "sonnet-5.5-claude-code"):
        shutil.copy(REPO / "profiles" / f"{name}.toml", repo / "profiles")
    (repo / "runtime").mkdir()
    (repo / "runtime" / "budget.json").write_text(json.dumps({"cap": 10.0, "spent": 0.0}))
    a = App(
        token="e2e-token",
        repo=repo,
        runtime=repo / "runtime",
        settings=Settings(repo / ".env", tmp_path / "config", lambda: a.key_names(), ignored=lambda _: True),
        saves=panel.StorySaves(tmp_path / "Story"),
        checks=lambda: [("PASS", "ffmpeg", "/usr/bin/ffmpeg")],
        web_dir=WEB / "dist",
    )
    srv = serve(a, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield a
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def page(browser: Browser) -> Iterator[tuple[Page, list[str]]]:
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg, errors
    ctx.close()


@pytest.mark.parametrize(
    ("screen", "text"),
    [
        ("preflight", "READY"),
        ("saves", "act1_get_item"),
        ("queue", "No matrix has run"),
        ("results", "No finished run"),
        ("settings", "DEEPSEEK_API_KEY"),
        ("about", "Not affiliated"),
    ],
)
def test_screens_render(app, page, screen, text):
    pg, errors = page
    pg.goto(f"http://127.0.0.1:{app.port}/#/{screen}")
    pg.get_by_text(text).first.wait_for(timeout=15000)
    assert errors == []


def test_new_run_plans_and_warns_about_youtube(app, page):
    pg, errors = page
    (app.repo / ".env").write_text("DEEPSEEK_API_KEY=fake-value-0123456789abcdef\n")
    pg.goto(f"http://127.0.0.1:{app.port}/#/new")
    pg.locator("label:has-text('deepseek-flash') input").check()
    assert pg.locator("label:has-text('sonnet-5.5-claude-code') input").is_disabled()
    pg.locator("label:has-text('act1_get_item') input").first.check()
    pg.locator("label:has-text('Max cost') input").fill("2")
    pg.get_by_role("button", name="Plan").click()
    plan = pg.locator("section[aria-label=Plan]")
    plan.wait_for(timeout=15000)
    assert "deepseek-flash~act1_get_item~rules~r1" in plan.inner_text()
    assert "15 minutes" in pg.get_by_role("alert").inner_text()
    assert errors == []


def test_settings_saves_a_key_with_the_served_token(app, page):
    pg, errors = page
    pg.goto(f"http://127.0.0.1:{app.port}/#/settings")
    row = pg.locator("label:has-text('DEEPSEEK_API_KEY')")
    row.locator("input").fill("fake-value-0123456789abcdef-LAST")
    row.get_by_role("button", name="Save").click()
    pg.get_by_text("…LAST").wait_for(timeout=15000)
    assert (app.repo / ".env").read_text() == "DEEPSEEK_API_KEY=fake-value-0123456789abcdef-LAST\n"
    assert "fake-value" not in pg.content()
    assert errors == []
