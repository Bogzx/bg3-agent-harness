# pyright: basic
"""The playthrough path end to end, offline: the app's Launcher starts a pi playthrough, the Supervisor plays two
sessions through bg3harness run_session (adapters/harness_runner.py) with the memory keeper between them, and the
app's API and screens show both sessions. No game, no key, no network.

    the Launcher's POST /start -> Supervisor -> HarnessRunner -> run_session -> ClientAgent -> pi on the scripted
    stub model -> the real bridge -> the fake mod (bg3harness.testing); the game read (progress, health) through
    AttachedGame over the same fake mod; the memory session through SessionPy (session.py start, stop, session-end):
    the stop check sends the agent back ("keep playing") until run_session's stop-resume limit
    between sessions: keeper_pass.MemoryKeeper -> memory/keeper.py -> pi on a second stub model -> the keeper-mode
    bridge -> a commit in the playthrough's memory
    then: /playthroughs, /sessions (cost from the events log), /watch (the shown session's calls only), /replay, the
    memory log, and Watch / Timeline / Replay in Playwright.

Needs tools/pi/node_modules (cd tools/pi && npm ci --ignore-scripts) and the built web app.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[4]
PI = REPO / "tools" / "pi" / "node_modules" / ".bin" / "pi"
STUB_LLM = REPO / "tools" / "pi" / "conformance" / "stub_llm.py"
PT = "e2e-pi"
MODEL = "anthropic/stub-model"
WAIT_S = 300  # upper bound on the whole playthrough; it takes well under a minute

if not PI.exists():
    pytest.skip("tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts", allow_module_level=True)

from bg3ai.game import Game  # noqa: E402
from bg3harness.session import Recovery  # noqa: E402
from bg3harness.testing.fakemod import FakeMod  # noqa: E402
from bg3harness.testing.mcp_bridge import bridge_env  # noqa: E402
from bg3harness.testing.scripted import FakeClock, GameModel  # noqa: E402
from bg3play import supervisor as sup  # noqa: E402
from bg3play.adapters.harness_runner import AttachedGame, ClientAgents, HarnessRunner, SessionPy  # noqa: E402
from bg3play.hub import Hub  # noqa: E402
from bg3play.keeper_pass import MemoryKeeper  # noqa: E402
from bg3play.memory.new_playthrough import create_memory  # noqa: E402
from bg3play.server import DEFAULT_WEB, App, serve  # noqa: E402
from bg3play.store import RunStore  # noqa: E402

from .contract import payloads, type_errors  # noqa: E402


def call(name: str, args: dict | None = None) -> dict:
    return {"name": "mcp__bg3__" + name, "arguments": args or {}}


PLAY_SCRIPT = [
    {"tool_calls": [call("get_status")]},
    {"tool_calls": [call("get_dialogue")]},
    {"tool_calls": [call("choose_dialogue", {"option": 1})]},
    {"text": "Off the first deck."},
]
QUEST = "## Escape the nautiloid (active)\n- Plan: reach the helm and take the ship [seen S001]\n"


def keeper_script(quests: str) -> list[dict]:
    """One whole-file write, so the second pass changes nothing (no_changes) and the first commits."""
    text = quests.replace("## Active\n", "## Active\n\n" + QUEST)
    return [{"tool_calls": [call("memory_write", {"path": "quests.md", "content": text})]}, {"text": "Updated."}]


class StubLLM:
    def __init__(self, ws: Path, steps: list[dict]):
        ws.mkdir(parents=True)
        (ws / "script.json").write_text(json.dumps(steps))
        port = ws / "port"
        cmd = [sys.executable, str(STUB_LLM), str(ws / "script.json"), str(ws / "llm.jsonl"), str(port)]
        self.proc = subprocess.Popen(cmd)
        wait_until(lambda: port.exists() and port.read_text(), "the stub model to listen")
        self.url = f"http://127.0.0.1:{int(port.read_text())}/v1"

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait()


def wait_until(cond, what: str, limit: float = WAIT_S):
    end = time.monotonic() + limit
    while not (got := cond()):
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.05)
    return got


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def profile_toml(base_url: str) -> str:
    return f"""client = "pi"
model = "{MODEL}"
effort = "high"

[endpoint]
provider = "openrouter"
base_url = "{base_url}"
api_key_env = "BG3_STUB_KEY"

[model_info]
input = ["text"]
reasoning = true
context_window = 200000
max_tokens = 16000
cost = {{ input = 4.0, output = 20.0, cache_read = 0.2, cache_write = 5.0 }}
"""


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    if not (DEFAULT_WEB / "index.html").is_file():
        pytest.fail(f"no built web app at {DEFAULT_WEB}: run `npm ci && npm run build` in packages/playthrough/web")
    root = tmp_path_factory.mktemp("pt")
    runtime, pts, home, profile_dir = root / "runtime", root / "playthroughs", root / "home", root / "profiles"
    profile_dir.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setenv("BG3_STUB_KEY", "stub-key")  # the keeper's pi runs in this process's env
    model = GameModel(clock=FakeClock())
    model.open_dialogue("Lae'zel", "Help me get to the helm.", ["We go together.", "Leave."])
    create_memory(PT, pts / PT / "memory")  # new_playthrough.py, as before a first `play.py run`
    runtime.mkdir()
    (runtime / "budget.json").write_text('{"cap": 1.0, "spent": 0.0}')  # the global budget a paid keeper needs
    play_llm = StubLLM(root / "llm-play", PLAY_SCRIPT)
    keeper_llm = StubLLM(root / "llm-keeper", keeper_script((pts / PT / "memory" / "quests.md").read_text()))
    (profile_dir / "stub-pi.toml").write_text(profile_toml(play_llm.url))
    with FakeMod(model) as fake:
        server = {
            "command": sys.executable,
            "args": ["-m", "bg3ai.server"],
            "env": bridge_env(fake.port, home, {"BG3AI_RUNTIME": str(runtime)}),
            "timeout": 60_000,
        }
        (home / "cwd").mkdir(parents=True)

        def system_text(path, condition):  # the dump needs the bridge's own env: a fixed text with the condition
            path.write_text(f"Rules of play ({condition}): you are the party on the nautiloid. Use the bg3 tools.\n")
            return path

        game = AttachedGame(Game(port=fake.port))
        agents = ClientAgents(profile_dir, home / "cwd", time.time, game.fault, server, system_text)

        def keeper_client(cfg):  # the adapter's choice, with the keeper's own stub model behind the same profile
            client, p = agents.keeper_client(cfg) or (None, None)
            assert client == "pi" and p is not None and p.model == MODEL
            return client, dataclasses.replace(p, endpoint=dataclasses.replace(p.endpoint, base_url=keeper_llm.url))

        runner = HarnessRunner(runtime, pts, agents, game, SessionPy, time.time)
        history = sup.history_path(runtime, PT)
        store = RunStore(runtime, pts)
        hub = Hub(store, generation="e2ept")
        supervisor = sup.Supervisor(
            runner,
            MemoryKeeper(runtime, pts, keeper_client, time.time),
            runtime,
            pts,
            lambda: 1800.0 * len(sup.read_history(history)),  # 30 min a session: the 1 h budget is spent after two
            lambda v: hub.publish("supervisor", v),
            played=lambda pt: len(store.session_logs(pt)),
        )
        srv = serve(App(store, hub, DEFAULT_WEB, supervisor=supervisor, profiles=profile_dir), "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            launch = {"profile": "stub-pi", "session_minutes": 30, "hours": 1, "max_cost": 1, "recording": False}
            post(f"{url}/api/v1/playthroughs/{PT}/start", launch)
            wait_until(lambda: get(f"{url}/api/v1/supervisor")["supervisor"]["state"] == "idle", "the playthrough")
            yield {
                "url": url,
                "runtime": runtime,
                "memory": pts / PT / "memory",
                "ops": [op for _, op, _ in fake.calls],
                "state": get(f"{url}/api/v1/supervisor")["supervisor"],
            }
        finally:
            hub.close()
            srv.shutdown()
            srv.server_close()
            play_llm.stop()
            keeper_llm.stop()
            mp.undo()


def test_two_sessions_through_run_session_with_the_keeper_between(played):
    assert played["state"]["detail"] == "hours budget spent", played["state"]
    hist = sup.read_history(sup.history_path(played["runtime"], PT))
    # the dialogue choice changes nothing the progress signature reads (level, region, party, journal)
    assert [(h["session"], h["outcome"], h["progress"]) for h in hist] == [("S001", "ok", False), ("S002", "ok", False)]
    assert {"state.status", "ui.dialogue_choose"} <= set(played["ops"])  # the model's calls reached the fake mod
    sdir = played["runtime"] / "sessions" / PT
    runs = json.loads((sdir / "runner.json").read_text())["history"]
    # 30 min sessions: session.py's stop check sent the agent back to play until run_session's limit, in the same
    # conversation; then session-end left the memory session open (no wrap-up) for the next to warn about
    assert [(r["reason"], r["exit"], r["stop_resumes"]) for r in runs] == [
        ("exited", 0, Recovery().max_stop_resumes)
    ] * 2
    assert "(harness) Claude session ended before wrap-up" in (played["memory"] / "sessions" / "S001.md").read_text()
    # the playthrough's condition reached the bg3 server pi starts
    pi_server = json.loads((sdir / "S001.pi" / "bg3.json").read_text())["server"]
    assert pi_server["env"]["BG3AI_PROMPT_CONDITION"] == "rules+coaching"
    assert "Rules of play (rules+coaching)" in (sdir / "pi-system.rules+coaching.txt").read_text()
    keeps = [json.loads(ln) for ln in (sdir / "keeper_history.jsonl").read_text().splitlines()]
    assert [(k["sid"], k["status"], k["client"]) for k in keeps] == [("S001", "ok", "pi"), ("S002", "no_changes", "pi")]
    # each keeper pass: 2 stub responses billed $0.0125 each, under the default cap ($0.05), added to the budget
    assert [k["cost_usd"] for k in keeps] == [0.025, 0.025]
    # and the play sessions' processes too (B4: the app's sessions charge the global budget, as play.py's do)
    play_cost = sum(h["cost_usd"] for h in hist)
    assert play_cost > 0
    spent = json.loads((played["runtime"] / "budget.json").read_text())["spent"]
    assert spent == pytest.approx(0.05 + play_cost)
    assert QUEST in (played["memory"] / "quests.md").read_text()


def test_the_api_shows_both_sessions(played):
    url = played["url"]
    (row,) = get(f"{url}/api/v1/playthroughs")["playthroughs"]
    assert (row["name"], row["sessions"], row["last_session"]) == (PT, 2, "S002") and row["cost_usd"] is not None
    ss = get(f"{url}/api/v1/playthroughs/{PT}/sessions")["sessions"]
    assert [s["session"] for s in ss] == ["S001", "S002"]
    assert all(s["cost_usd"] and s["cost_usd"] > 0 and s["started"] and s["ended"] for s in ss)  # pi's billed cost
    assert all(s["model"] == MODEL and not s["running"] for s in ss)

    w = get(f"{url}/api/v1/playthroughs/{PT}/watch")
    s2 = ss[1]
    assert w["session"] == "S002" and w["actions"]
    assert all(s2["started"] <= a["t"] <= s2["ended"] for a in w["actions"])  # S001's calls are not shown
    assert w["cost"]["sessions"] == 2 and w["party"] is not None

    for s in ss:
        r = get(f"{url}/api/v1/playthroughs/{PT}/replay/{s['session']}")
        tools = [e["tool"] for e in r["events"] if e["kind"] == "action"]
        assert "mcp__bg3__get_status" in tools or "get_status" in tools, tools
        assert any(e["kind"] == "say" for e in r["events"])
    r1 = get(f"{url}/api/v1/playthroughs/{PT}/replay/S001")
    assert any(e["kind"] == "memory" and e["subject"].startswith("S001 memory-keeper") for e in r1["events"])
    commits = get(f"{url}/api/v1/playthroughs/{PT}/memory/log")["commits"]
    assert commits[0]["subject"] == "S001 memory-keeper: quests.md"


def test_watch_timeline_and_replay_screens(page, played):
    url = played["url"]
    page.goto(f"{url}/#/watch/{PT}")
    expect(page.locator(".calls .tool").first).to_have_text("choose_dialogue")  # S002's calls, newest first
    expect(page.locator(".calls .tool", has_text="get_status")).to_have_count(1)
    expect(page.get_by_text("Off the first deck.").first).to_be_visible()
    page.goto(f"{url}/#/timeline/{PT}")
    expect(page.get_by_text("S002").first).to_be_visible()
    page.goto(f"{url}/#/replay/{PT}/S001")
    expect(page.get_by_text("At the playhead")).to_be_visible()
    expect(page.get_by_text("Nothing yet at this time")).to_be_visible()
    for _ in range(12):  # step to the session's end: its calls and lines are all events before it
        page.get_by_label("Next event").click()
    expect(page.locator(".events").get_by_text("choose_dialogue").first).to_be_visible()
    expect(page.locator(".events").get_by_text("Off the first deck.").first).to_be_visible()


def test_every_screen_and_payload_over_the_played_run(page, played, tmp_path):
    """The run this playthrough leaves (a second shape next to the recorded claude-run-2 fixture): every screen
    renders it without a console error, and its payloads match the client's types."""
    url = played["url"]
    for route, sign in [
        (f"#/stats/{PT}", "Goal scorecard"),
        (f"#/story/{PT}", "chapters"),
        (f"#/memory/{PT}/quests.md", "Memory commits"),
        (f"#/launch/{PT}", "2 sessions so far: Resume plays the next one"),
        (f"#/replay/{PT}/S002", "At the playhead"),
    ]:
        page.goto(f"{url}/{route}")
        expect(page.get_by_text(sign).first).to_be_visible()
    page.goto(f"{url}/overlay?pt={PT}")
    expect(page.locator(".box.tool")).to_be_visible()
    found = payloads(url, PT)
    assert {"replay/S001", "replay/S002", "memory/diff quests.md"} <= set(found)
    errors = type_errors(found, tmp_path)
    assert errors == "", errors
