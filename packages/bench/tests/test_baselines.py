import asyncio
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from bg3harness import clients
from mcp import Client

from bg3bench.baselines import agent, client, donothing, random_agent

HERE = Path(__file__).resolve().parent
STUB = HERE / "fixtures" / "baselines" / "stub_server.py"
sys.path.insert(0, str(STUB.parent))
import stub_server

FORBIDDEN = {
    "load_game", "save_game", "new_game", "memory_read", "memory_write", "memory_append", "memory_edit",
    "memory_list", "memory_search", "note", "recall", "wiki_page", "wiki_search", "click", "click_on", "press_key",
    "ui_action", "screenshot",
}  # fmt: skip


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def run_agent(play, deadline, **kw):
    log, out, clock = [], io.StringIO(), FakeClock()

    async def main():
        async with Client(stub_server.build(log)) as c:
            session = agent.McpSession(c)
            allowed = random_agent.ALLOWED if play is random_agent.play else donothing.ALLOWED
            await play(session, agent.Player(session, allowed, out), deadline, now=clock.now, sleep=clock.sleep, **kw)

    asyncio.run(main())
    return [json.loads(line) for line in out.getvalue().splitlines()], log, clock


def test_allowlists_hold_no_forbidden_tool():
    assert not FORBIDDEN & set(random_agent.ALLOWED)
    assert set(donothing.ALLOWED) == {"session_start", "end_session"}


def test_donothing_waits_for_the_deadline():
    lines, log, clock = run_agent(donothing.play, deadline=1000.0 + 95)
    assert [e["tool"] for e in log] == ["session_start", "end_session"]
    assert clock.t == 1095.0  # 30 + 30 + 30 + 5: slept to the deadline, not past it
    assert lines[0] == {"type": "init", "agent": "donothing", "seed": None, "tools": 15}  # the stub's tools
    assert lines[-1] == {"type": "result", "subtype": "success", "calls": 2, "duration_ms": 95000.0, "cost_usd": 0.0}
    lines, log, _ = run_agent(donothing.play, deadline=999.0, session_start=False)
    assert [e["tool"] for e in log] == ["end_session"]


def test_random_agent_is_reproducible_and_stays_in_its_allowlist():
    a, log_a, clock = run_agent(random_agent.play, deadline=1000.0 + 40, seed=7)
    b, log_b, _ = run_agent(random_agent.play, deadline=1000.0 + 40, seed=7)
    _, log_c, _ = run_agent(random_agent.play, deadline=1000.0 + 40, seed=8)
    assert a == b and log_a == log_b
    assert log_a != log_c
    tools = [e["tool"] for e in log_a]
    assert tools[0] == "session_start" and tools[-1] == "end_session"
    assert tools.count("look") == 40 and clock.t == 1040.0  # one look + one action per second
    assert set(tools) <= set(random_agent.ALLOWED) and "load_game" not in tools and "save_game" not in tools
    acted = set(tools) - {"look", "session_start", "end_session"}
    assert len(acted) >= 5  # it really varies its actions
    # a handle action uses a handle the look just before it showed
    shown = [None, {"e3", "e12"}, {"e7", "e12", "e40"}]
    looks = 0
    for e in log_a:
        if e["tool"] == "look":
            looks += 1
        for k in ("target", "npc", "item"):
            if k in e["args"]:
                assert e["args"][k] in shown[looks % 3]
    calls = [line for line in a if line["type"] == "call"]
    results = [line for line in a if line["type"] == "tool_result"]
    assert len(calls) == len(results) == len(log_a) and a[-1]["calls"] == len(log_a)


def test_choose():
    import random

    rng = random.Random(0)
    assert random_agent.choose(rng, ["move_to"], [])[0] == "walk_toward"
    tool, args = random_agent.choose(rng, ["combat_turn"], ["e5"])
    assert tool == "combat_turn" and json.loads(args["plan"])["move"] == "e5"
    assert random_agent.choose(rng, ["loot"], []) == ("loot", {})
    assert 1 <= random_agent.choose(rng, ["choose_dialogue"], [])[1]["option"] <= 4
    assert random_agent.handles("e12 and e3, e12, ae4, e3x") == ["e3", "e12"]


def test_random_agent_needs_the_bg3_server_and_the_player_refuses_other_tools():
    class Empty:
        async def list_tools(self):
            return ["look"]

    with pytest.raises(RuntimeError, match="walk_toward"):
        asyncio.run(random_agent.play(Empty(), agent.Player(Empty(), [], io.StringIO()), 0.0, seed=1))
    with pytest.raises(PermissionError, match="load_game"):
        asyncio.run(agent.Player(Empty(), random_agent.ALLOWED, io.StringIO()).call("load_game"))


def test_without_look_the_random_agent_only_walks():
    class NoLook:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            return ["walk_toward", "move_to"]

        async def call(self, tool, args):
            self.calls.append(tool)
            return "ok", False

    s, clock = NoLook(), FakeClock()
    asyncio.run(
        random_agent.play(
            s,
            agent.Player(s, random_agent.ALLOWED, io.StringIO()),
            0,
            seed=1,
            now=clock.now,
            sleep=clock.sleep,
            max_steps=5,
            session_start=True,
        )
    )
    assert s.calls == []  # the deadline (0) is already past
    asyncio.run(
        random_agent.play(
            s,
            agent.Player(s, random_agent.ALLOWED, io.StringIO()),
            10**9,
            seed=1,
            now=clock.now,
            sleep=clock.sleep,
            max_steps=5,
        )
    )
    assert s.calls == ["walk_toward"] * 5


def test_server_params_and_deadline(tmp_path, monkeypatch):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"bg3": {"command": "uv", "args": ["run", "x"], "env": {"A": "1"}}}}))
    p = agent.server_params(cfg, env={"B": "2"})
    assert (p.command, p.args, p.env, Path(p.cwd)) == ("uv", ["run", "x"], {"B": "2", "A": "1"}, tmp_path)
    ap = agent.parser("x")
    assert agent.deadline_of(ap.parse_args(["--mcp-config", "c", "--deadline", "5"]), {}) == 5.0
    assert agent.deadline_of(ap.parse_args(["--mcp-config", "c"]), {agent.DEADLINE_ENV: "7.5"}) == 7.5
    with pytest.raises(SystemExit, match="no deadline"):
        agent.deadline_of(ap.parse_args(["--mcp-config", "c"]), {})


def stub_config(tmp_path):
    cfg = tmp_path / "play" / ".mcp.json"
    cfg.parent.mkdir()
    server = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(STUB)],
        "env": {"STUB_LOG": str(tmp_path / "calls.jsonl")},
    }
    cfg.write_text(json.dumps({"mcpServers": {"bg3": server}}))
    return cfg


@pytest.mark.integration
@pytest.mark.parametrize(
    "module,extra",
    [
        ("bg3bench.baselines.random_agent", ["--seed", "3", "--max-steps", "4", "--period-s", "0"]),
        ("bg3bench.baselines.donothing", []),
    ],
)
def test_the_processes_speak_mcp_over_stdio(tmp_path, module, extra):
    """The agent processes launch the server from an .mcp.json the way play/.mcp.json is launched."""
    cfg = stub_config(tmp_path)
    deadline = str(time.time() + 600 if extra else time.time() - 1)
    out = subprocess.run(
        [sys.executable, "-m", module, "--mcp-config", str(cfg), "--deadline", deadline, *extra],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    lines = [json.loads(line) for line in out.stdout.splitlines()]
    calls = [json.loads(line)["tool"] for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [line["tool"] for line in lines if line["type"] == "call"] == calls
    assert calls[0] == "session_start" and calls[-1] == "end_session"
    assert len(calls) == (2 + 4 * 2 if extra else 2)
    ev = client.BaselineClient()
    parsed = [e for line in out.stdout.splitlines() for e in ev.parse(line)]
    assert parsed[0].kind == "init" and parsed[-1].kind == "result" and parsed[-1].cost_usd == 0.0


def test_baseline_client_speaks_the_runner_protocol(tmp_path):
    c = client.BaselineClient(mcp_config=tmp_path / ".mcp.json", event=clients.Event)
    for attr in ("name", "supports_resume", "assigns_session", "no_tools"):
        assert hasattr(c, attr)
    req = clients.Request(prompt="p", model="baseline:random:7", session_dir=tmp_path, session="sid-1")
    assert c.write_config(req) == [] and c.env(req) == {}
    cmd = c.command(req)
    assert cmd[:3] == ["uv", "run", "--project"] and Path(cmd[3]) == client.BENCH_DIR
    assert cmd[4:] == [
        "--extra",
        "baselines",
        "python",
        "-m",
        "bg3bench.baselines.random_agent",
        "--mcp-config",
        str(tmp_path / ".mcp.json"),
        "--seed",
        "7",
    ]
    assert clients.process_env(c, req, base={}) == {}
    lines, _, _ = run_agent(random_agent.play, deadline=1003.0, seed=7)
    events = [e for line in lines for e in c.parse(json.dumps(line))]
    assert all(isinstance(e, clients.Event) for e in events)
    kinds = [e.kind for e in events]
    assert kinds[0] == "init" and kinds[-1] == "result" and kinds.count("tool_call") == kinds.count("tool_result")
    assert events[0].session_id == "sid-1" and events[0].model == "baseline:random:7"
    call, res = events[1], events[2]
    assert (call.tool, call.call_id, res.call_id) == ("session_start", "c1", "c1")
    assert events[-1].tokens == dict.fromkeys(clients.TOKEN_KEYS, 0) and events[-1].turns == len(kinds[1:-1]) // 2
    assert c.parse("not json") == [] and c.parse("[1]") == [] and c.parse('{"type": "other"}') == []
    nothing = c.command(SimpleNamespace(model="baseline:donothing", resume=False, session="s"))
    assert nothing[-3:] == ["bg3bench.baselines.donothing", "--mcp-config", str(tmp_path / ".mcp.json")]
    with pytest.raises(ValueError, match="resumed"):
        c.command(SimpleNamespace(model="baseline:donothing", resume=True, session="s"))
    with pytest.raises(ValueError, match="not a baseline"):
        c.command(SimpleNamespace(model="opus", resume=False, session="s"))
    assert client.agent_of("baseline:random:0") == ("random", 0)
    assert client.MCP_CONFIG == HERE.parents[2] / "play" / ".mcp.json" and client.MCP_CONFIG.is_file()
