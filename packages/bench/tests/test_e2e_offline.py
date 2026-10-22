# pyright: basic
"""The offline pipeline end to end (B-tests 3): scenario file -> the matrix -> run_session -> pi with the scripted stub
model -> the real bridge -> the fake mod -> facts.jsonl -> grader -> result.json -> stats -> report and leaderboard.
No game, no key, no network.

Two runs of e2e_sell_idol (fixtures/e2e), one matrix job with 2 repeats played by adapters/session_run.SessionRun:
a trader's dialogue is open; the stub model sells the idol in run 1 (gold 100 -> 250: the goal is gold >= 200) and
leaves in run 2. The harness side is a RunHost whose run_session hooks run pi; its facts come from the runner's
progress poller read (bg3harness/facts.py over bridge progress.read_game and the party inventory), from the fake mod
before and after the pi process, with a fake clock.

TODO(fake mod merged): bg3harness.testing (ovh/mod) brings the fake mod and the bridge's rpc_record. Until it is in
this tree, the tool calls go to the pi conformance stub MCP (tools/pi/conformance/stub_mcp.py), and the game state is
a stand-in that sells the idol when the stub's log shows choose_dialogue option 1. Delete the stub path then.

Needs tools/pi/node_modules (cd tools/pi && npm ci --ignore-scripts); skipped without it, like the conformance suite.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_session_run import Hooks, Result

from bg3bench import matrix, report, results, scenario
from bg3bench.adapters.session_run import SessionRun, Start, series_name
from bg3bench.site import build as site

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PI_DIR = REPO / "tools" / "pi"
PI = PI_DIR / "node_modules" / ".bin" / "pi"
CONF = PI_DIR / "conformance"
SCENARIO = HERE / "fixtures" / "e2e" / "e2e_sell_idol.json"
FAKE_MOD = (
    importlib.util.find_spec("bg3harness") is not None and importlib.util.find_spec("bg3harness.testing") is not None
)
PREFIX = "mcp__bg3__"
MODEL = "anthropic/stub-model"
SYSTEM = "Rules of play: you are in a dialogue with a trader. Use the bg3 tools.\n"
T0 = 1_800_000_000.0  # the fake wall clock of the facts writer
WAIT_S = 120

if not PI.exists():
    pytest.skip("tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts", allow_module_level=True)


from bg3harness import facts as facts_w
from bg3harness import profiles


def call(name: str, args: dict | None = None) -> dict:
    return {"name": PREFIX + name, "arguments": args or {}}


def script(option: int) -> list[dict]:
    return [
        {"tool_calls": [call("get_dialogue")]},
        {"tool_calls": [call("choose_dialogue", {"option": option})]},
        {"text": "Sold the idol." if option == 1 else "Left."},
    ]


class StubLLM:
    def __init__(self, ws: Path, steps: list[dict]):
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "script.json").write_text(json.dumps(steps))
        port = ws / "port"
        self.proc = subprocess.Popen(
            [sys.executable, str(CONF / "stub_llm.py"), str(ws / "script.json"), str(ws / "llm.jsonl"), str(port)]
        )
        end = time.monotonic() + WAIT_S
        while not (port.exists() and port.read_text()):  # the stub writes its port once it listens
            assert time.monotonic() < end and self.proc.poll() is None, "the stub model did not start"
            time.sleep(0.02)
        self.port = int(port.read_text())

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait()


def run_pi(ws: Path, llm_port: int, server: dict) -> list[dict]:
    """One pi process (as bg3harness/pi_client.py runs it) against the stub model and `server`; its JSON events."""
    for d in ("home", "agent", "cwd"):
        (ws / d).mkdir(exist_ok=True)
    raw = {
        "client": "pi",
        "model": MODEL,
        "effort": "high",
        "endpoint": {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "BG3_STUB_KEY",
        },
        "model_info": {
            "input": ["text"],
            "reasoning": True,
            "context_window": 200000,
            "max_tokens": 16000,
            "cost": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
        },
    }
    models = profiles.models_json(profiles.parse(raw))
    models["providers"]["openrouter"]["baseUrl"] = f"http://127.0.0.1:{llm_port}/v1"
    (ws / "agent" / "models.json").write_text(json.dumps(models))
    settings = {"compaction": {"enabled": False}, "defaultTools": [], "quietStartup": True}
    (ws / "agent" / "settings.json").write_text(json.dumps(settings))
    (ws / "system.txt").write_text(SYSTEM)
    (ws / "bg3.json").write_text(json.dumps({"server": server, "systemPromptFile": str(ws / "system.txt")}))
    cmd = [str(PI), "--mode", "json", "-ne", "-na", "-nc", "-ns", "-np", "--offline", "-e", str(PI_DIR / "bg3.ts")]
    cmd += ["--tools", PREFIX + "*", "--session-dir", str(ws / "sessions"), "--session-id", "e2e-0001"]
    cmd += ["--model", f"openrouter/{MODEL}", "--thinking", "high", "Play."]
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TERM")}
    env = {**keep, "HOME": str(ws / "home"), "PI_CODING_AGENT_DIR": str(ws / "agent")}
    env |= {"BG3_PI_CONFIG": str(ws / "bg3.json"), "PI_OFFLINE": "1", "BG3_STUB_KEY": "stub-key"}
    p = subprocess.run(cmd, cwd=ws / "cwd", env=env, capture_output=True, text=True, timeout=WAIT_S, check=False)
    assert p.returncode == 0, p.stderr[-2000:]
    return [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]


def usage(events: list[dict]) -> tuple[float, dict]:
    """(billed USD, tokens) from pi's assistant message_end events."""
    cost, tok = 0.0, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for e in events:
        msg = e.get("message") or {}
        if e.get("type") == "message_end" and msg.get("role") == "assistant" and msg.get("usage"):
            u = msg["usage"]
            cost += u["cost"]["total"]
            for k, src in (("input", "input"), ("output", "output"), ("cache_read", "cacheRead")):
                tok[k] += u.get(src, 0)
            tok["cache_write"] += u.get("cacheWrite", 0)
    return round(cost, 6), tok


# ------------------------------------------------------------------------------------------------ the game side


def play_fake_mod(ws: Path, steps: list[dict], writer) -> tuple[list[dict], list[str]]:
    """pi -> bg3ai.server -> FakeMod(GameModel); facts from the runner's poll read. (pi's events, the mod ops)."""
    from bg3ai import progress  # the bridge's poll read (play.py progress_loop)
    from bg3ai.luadebug import LuaDebugClient
    from bg3harness.testing.fakemod import FakeMod
    from bg3harness.testing.mcp_bridge import bridge_env
    from bg3harness.testing.scripted import FakeClock, GameModel

    model = GameModel(clock=FakeClock())
    model.open_dialogue("Trader", "Want to sell that idol?", ["Sell the idol for 150 gold.", "Leave."])

    class SellHook:  # the trade the fake mod's model has no op for: option 1 pays, then the model answers
        def reply(self, ctx, op, args):
            if op == "ui.dialogue_choose" and not args.get("dry") and int(args.get("i") or 0) == 1:
                model.add_gold(150)

        def eval_raw(self, ctx, code):
            return None

    with FakeMod(SellHook(), model) as fake:

        def lua_eval(code: str, ctx: str = "client") -> str:
            with LuaDebugClient(port=fake.port) as c:
                return c.evaluate(code, context=ctx, timeout=10)

        def poll(now: float) -> None:
            got = progress.read_game(lua_eval)
            writer.observe(got, facts_w.read_inventory(lua_eval, progress.combined_code), now=now)

        poll(T0 + 1)
        llm = StubLLM(ws, steps)
        try:
            server = {
                "command": sys.executable,
                "args": ["-m", "bg3ai.server"],
                "env": bridge_env(fake.port, ws / "bridge"),
                "timeout": WAIT_S,
            }
            events = run_pi(ws, llm.port, server)
        finally:
            llm.stop()
        poll(T0 + 61)
        return events, [op for _, op, _ in fake.calls]


def play_stub_mcp(ws: Path, steps: list[dict], writer) -> tuple[list[dict], list[str]]:
    """TODO(fake mod merged): delete. pi -> the conformance stub MCP; the stand-in game sells on option 1."""
    party = [{"name": "Tav", "level": 1, "hp": 12, "max_hp": 12}]

    def state(gold: int) -> dict:
        status = {"party": party, "gold": gold, "region": "Act 1", "in_combat": False}
        return {"status": status, "journal": [], "map": None, "markers": None, "explored": None, "events": None}

    writer.observe(state(100), {"items": []}, now=T0 + 1)
    llm = StubLLM(ws, steps)
    try:
        server = {
            "command": sys.executable,
            "args": [str(CONF / "stub_mcp.py")],
            "env": {"STUB_MCP_LOG": str(ws / "mcp.jsonl"), "STUB_MCP_DELAY": "0"},
            "timeout": WAIT_S,
        }
        events = run_pi(ws, llm.port, server)
    finally:
        llm.stop()
    log = [json.loads(line) for line in (ws / "mcp.jsonl").read_text().splitlines()]
    calls = [r for r in log if r.get("ev") == "start"]
    sold = any(r["tool"] == "choose_dialogue" and (r.get("args") or {}).get("option") == 1 for r in calls)
    writer.observe(state(250 if sold else 100), {"items": []}, now=T0 + 61)
    return events, [r["tool"] for r in calls]


# ------------------------------------------------------------------------------------------------ the harness side


class PiHooks(Hooks):
    """run_session's hooks for one run: the agent process is pi on the stub model (`option`: the sale or not)."""

    def __init__(self, ws: Path, option: int, writer, clock: list[float]):
        super().__init__([], clock)
        self.ws, self.option, self.writer = ws, option, writer
        self.events_out: list[dict] = []
        self.called: list[str] = []

    def run_agent(self, prompt, minutes, client_session, resume, tag):
        assert (prompt, resume) == ("Play.", False)
        play = play_fake_mod if FAKE_MOD else play_stub_mcp
        self.events_out, self.called = play(self.ws, script(self.option), self.writer)
        return Result("exited", 0, T0, T0 + 62, client_session or "e2e-0001")


class PiHost:
    """bench.adapters.session_run.RunHost: run 1 sells the idol, run 2 leaves; facts.jsonl in the run directory."""

    def __init__(self, tmp: Path):
        self.tmp, self.clock, self.hooks = tmp, [T0], []

    def start(self, job, sc, run_dir):
        writer = facts_w.FactsWriter(run_dir / "facts.jsonl", clock=lambda: T0)  # every record passes its time
        writer.mark("run_start", {"playthrough": run_dir.name}, now=T0)
        self.hooks.append(PiHooks(self.tmp / f"ws{len(self.hooks) + 1}", len(self.hooks) + 1, writer, self.clock))
        sha = hashlib.sha256(SYSTEM.encode()).hexdigest()
        return Start("Play.", self.hooks[-1], None, sha, (), "0.1.0")

    def record(self, job, sc, run_dir, outcome):
        hooks = self.hooks[-1]
        hooks.writer.mark("run_end", "gave_up", now=T0 + 62)
        assert ("ui.dialogue_choose" if FAKE_MOD else "choose_dialogue") in hooks.called  # the model's call got there
        cost, tokens = usage(hooks.events_out)
        assert cost > 0  # billed from the stub's usage at the profile's prices
        return {"outcome": "gave_up", "cost_usd": cost, "tokens": tokens, "wall_min": 1.0, "play_min": 1.0}


# ------------------------------------------------------------------------------------------------ the test


def test_scenario_to_leaderboard(tmp_path):
    sc = scenario.load(SCENARIO)
    sessions = tmp_path / "runtime" / "sessions"
    budget = tmp_path / "runtime" / "budget.json"
    budget.parent.mkdir()
    budget.write_text(json.dumps({"cap": 5.0, "spent": 0.0}))
    host = PiHost(tmp_path)
    model = {"slug": MODEL, "client": "pi", "effort": "high"}
    jobs = matrix.plan([model], [sc["name"]], ["rules"], 2, max_cost={sc["name"]: sc["caps"]["max_cost"]})
    run_one = SessionRun("e2e", host, scenarios={sc["name"]: sc}, sessions=sessions)
    mx = matrix.Matrix(tmp_path / "runtime" / "matrix" / "e2e", run_one, budget, now=lambda: T0, sleep=lambda s: 0)
    mx.create("e2e", jobs)
    assert mx.run() == matrix.FINISHED
    series = series_name("e2e", jobs[0])
    ends = [a["result"] for st in mx.state().values() for a in st["attempts"]]
    assert [(r["run_id"], r["valid"], r["passed"]) for r in ends] == [
        (f"{series}-r001", True, True),
        (f"{series}-r002", True, False),
    ]
    assert json.loads(budget.read_text())["spent"] == round(sum(r["cost_usd"] for r in ends), 6)  # the matrix billed
    for k, run_dir in ((1, sessions / series), (2, sessions / f"{series}-r2")):  # series.run_name
        res = results.load(run_dir / "result.json")
        assert res["passed"] is (k == 1) and res["grade"]["progress"] is None
        assert (res["t_pass_s"] == 61.0) is (k == 1)  # game time of the poll after the sale
        assert res["methods"] is not None

    pairs = report.load_runs(sessions)
    assert [m["run_id"] for m, _ in pairs] == [f"{series}-r001", f"{series}-r002"]
    out = tmp_path / "report"
    assert report.main([str(sessions), str(out)]) == 0
    md = (out / "report.md").read_text()
    assert f"{series}-r001" in md and f"{series}-r002" in md

    rel = results.build_results(pairs, seed=1, created=T0, b=200)
    (group,) = rel["groups"]
    assert (group["model"], group["condition"], group["n_runs"], group["n_invalid"]) == (MODEL, "rules", 2, 0)
    assert group["stats"] is not None and group["cost_usd"]["per_success"] > 0
    results.write(rel, tmp_path / "results.json")
    assert site.main([str(tmp_path / "results.json"), str(tmp_path / "site" / "index.html")]) == 0
    page = (tmp_path / "site" / "index.html").read_text()
    assert MODEL in page and f"{series}-r001" in page
