# pyright: basic
"""The arena's live pieces offline: the model-time clock, one side's agent process (a stand-in client that prints a
scripted stream), each side's server env, the runner's game env switch and a side's mod op envelope."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from bg3harness import clients, profiles
from bg3harness.arena import agents as aagents
from bg3harness.arena import live
from bg3harness.arena.spec import ArenaSpec
from bg3harness.game import io as gio
from bg3harness.runner import state as rstate
from bg3harness.runner.core import SessionRunner

ARENA = {
    "name": "test_field",
    "save": {"name": "Tav-1__SCN-arena_test", "sha1": "0" * 40},
    "teams": {"A": [{"name": "Karlach"}], "B": [{"name": "Wyll"}]},
    "seed": 7,
    "karmic": False,
    "round_cap": 20,
    "wall_cap_min": 30,
    "turn_cap_s": 60,
    "condition": "rules",
    "tools_off": "arena-v1",
}


def ev(kind: str, t: float, **kw: Any) -> tuple[clients.Event, float]:
    return clients.Event(kind, **kw), t


def test_model_time_runs_between_tool_calls_only():
    c = aagents.ModelClock(100.0)
    for e, t in [
        ev("text", 103),
        ev("tool_call", 105, call_id="1"),  # 5 s of model time
        ev("tool_call", 105, call_id="2"),
        ev("tool_result", 130, call_id="1"),  # one still open: tools, not the model
        ev("tool_result", 140, call_id="2"),
        ev("tool_call", 142, call_id="3"),  # +2
        ev("tool_result", 200, call_id="3"),
    ]:
        c.on_event(e, t)
    assert c.model_s(201) == pytest.approx(8.0)  # running again since 200
    c.on_event(clients.Event("result"), 204)
    assert c.model_s(300) == pytest.approx(11.0)  # stopped at the result


class ScriptClient:
    """A client whose process prints `lines` (one Event JSON per line) and exits with `code`."""

    name = "fake"
    supports_resume = True
    assigns_session = True
    no_tools = "only the scripted stream"

    def __init__(self, lines: list[dict], code: int = 0):
        self.lines, self.code = lines, code
        self.requests: list[clients.Request] = []

    def write_config(self, req: clients.Request) -> list[Path]:
        self.requests.append(req)
        return []

    def env(self, req: clients.Request) -> dict[str, str]:
        return {}

    def command(self, req: clients.Request) -> list[str]:
        out = "".join(json.dumps(x) + "\n" for x in self.lines)
        return [sys.executable, "-c", f"import sys; sys.stdout.write({out!r}); sys.exit({self.code})"]

    def parse(self, line: str) -> list[clients.Event]:
        return [clients.Event(**json.loads(line))]


def run_to_end(p: aagents.AgentProcess) -> aagents.AgentExit:
    p.proc.wait(timeout=30)
    ex = None
    while ex is None:
        ex = p.done()
    return ex


def test_an_agent_process_writes_its_sides_files_and_reports_its_exit(tmp_path):
    client = ScriptClient(
        [
            {"kind": "init", "tools": 3, "mcp": ["bg3:connected"]},
            {"kind": "tool_call", "tool": "end_turn", "call_id": "c1"},
            {"kind": "tool_result", "call_id": "c1", "text": "ok"},
            {"kind": "result", "cost_usd": 0.02},
        ]
    )
    sc = aagents.SideClient(client, "m", None, {"BG3AI_SIDE": "B"}, tools_expected=3)
    p = aagents.AgentProcess("B", sc, "The fight is on.", "conv-1", False, "-r1", tmp_path, {})
    ex = run_to_end(p)
    assert ex == aagents.AgentExit("exited", 0, "conv-1", 0.02)
    req = client.requests[0]
    assert req.env["BG3AI_SIDE"] == "B" and req.env["BG3AI_METRICS_PATH"] == f"{tmp_path / 'B-r1'}.metrics.jsonl"
    kinds = [json.loads(x)["kind"] for x in (tmp_path / "B.events.jsonl").read_text().splitlines()]
    assert kinds == ["session_start", "init", "tool_call", "tool_result", "result", "session_end"]
    assert (tmp_path / "B-r1.jsonl").read_text().count("\n") == 4
    assert p.stop() is ex  # stopping an ended process changes nothing


class SpendingClient(ScriptClient):
    """Prints its lines, then thinks on (sleeps) until stopped."""

    def command(self, req: clients.Request) -> list[str]:
        out = "".join(json.dumps(x) + "\n" for x in self.lines)
        return [
            sys.executable,
            "-c",
            f"import sys, time; sys.stdout.write({out!r}); sys.stdout.flush(); time.sleep(60)",
        ]


def test_an_agent_process_reports_its_running_cost_after_each_response_and_keeps_it_when_stopped(tmp_path):
    """pi's usage event after each model response carries the process's running cost: cost_usd has it while the
    process runs (the match's money cap reads it), and a process stopped before its result keeps it."""
    client = SpendingClient([{"kind": "usage", "cost_usd": 0.01}, {"kind": "usage", "cost_usd": 0.03}])
    p = aagents.AgentProcess("A", aagents.SideClient(client, "m", None, {}), "go", "c", False, "", tmp_path, {})
    deadline = time.monotonic() + 20
    while p.cost_usd() < 0.03 and time.monotonic() < deadline:  # the pump thread reads the lines
        time.sleep(0.02)
    assert p.cost_usd() == 0.03 and p.done() is None
    ex = p.stop()
    assert ex.cost_usd == 0.03 and ex.reason != "exited"


def test_an_agent_with_the_wrong_tool_count_is_an_mcp_failure(tmp_path):
    client = ScriptClient([{"kind": "init", "tools": 90, "mcp": ["bg3:connected"]}])
    sc = aagents.SideClient(client, "m", None, {"BG3AI_TOOLS_OFF": "arena-v1"}, tools_expected=60)
    p = aagents.AgentProcess("A", sc, "go", "conv-2", False, "", tmp_path, {})
    assert run_to_end(p).reason == "mcp_failed"
    assert "gave 90 tools, expected 60" in (p.mcp or "")


def test_an_agent_that_exits_nonzero_is_a_fault(tmp_path):
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient([], code=3), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    ex = run_to_end(p)
    assert (ex.reason, ex.exit_code, ex.fault) == ("exited", 3, True)


def test_live_agents_give_both_sides_the_same_session_clock(tmp_path):
    t = [1000.0]
    clients_ = {s: ScriptClient([]) for s in "AB"}
    sides = {s: aagents.SideClient(clients_[s], "m", None, {"BG3AI_SIDE": s}) for s in "AB"}
    agents = aagents.LiveAgents(sides, tmp_path, {}, 30, clock=lambda: t[0])
    run_to_end(agents.start("A", "go", None, ""))
    t[0] = 1100.0
    run_to_end(agents.start("B", "go", None, ""))
    run_to_end(agents.start("B", "again", "conv-b", "-r1"))
    deadlines = {c.requests[0].env["BG3AI_SESSION_DEADLINE"] for c in clients_.values()}
    assert deadlines == {str(1000 + 30 * 60)}
    resumed = clients_["B"].requests[1]
    assert (resumed.session, resumed.resume, resumed.tag) == ("conv-b", True, "B-r1")
    assert clients_["A"].requests[0].session != clients_["B"].requests[0].session


def test_each_side_s_server_env(tmp_path):
    spec = ArenaSpec.from_dict(ARENA)
    prof = profiles.load(Path(__file__).resolve().parents[3] / "profiles" / "deepseek-v4.1-flash-pi.toml")
    env = live.side_env(spec, "B", prof, tmp_path / "run1")
    assert env["BG3AI_SIDE"] == "B" and env["BG3AI_PROMPT_CONDITION"] == "rules"
    assert env["BG3AI_TOOLS_OFF"].split(",")[0] == "arena-v1"  # the profile's own go after the arena list
    assert env["BG3AI_MEMORY_DIR"] == str(tmp_path / "run1" / "memory-B")


def test_a_scripted_side_gets_the_scripted_client_its_sides_env_and_no_profile(tmp_path):
    from bg3harness.arena import scripted_client

    spec = ArenaSpec.from_dict(ARENA)
    sc, fields = live.side_client(spec, "A", "scripted", tmp_path / "run1", {}, lambda _m: None)
    assert isinstance(sc.client, scripted_client.ScriptedClient) and (sc.model, sc.tools_expected) == ("none", None)
    assert sc.env["BG3AI_SIDE"] == "A" and sc.env["BG3AI_TOOLS_OFF"] == "arena-v1"
    assert fields == {"profile": "scripted", "client": "scripted", "model": "none"}


def test_the_game_gets_the_arena_switch_only_in_a_match():
    spec = ArenaSpec.from_dict(ARENA)
    opt = live.options(spec, "arena-run")
    assert (opt.start_save, opt.seed, opt.arena) == ("Tav-1__SCN-arena_test", 7, True)
    assert opt.scenario_settings is not None and opt.scenario_settings["karmic_dice"] is False
    r = SessionRunner.__new__(SessionRunner)
    r.opt = opt
    assert r.setup_env() == {"BG3AI_SCENARIO_SETUP": "1", "BG3AI_ARENA": "1"}
    r.opt = rstate.Options("p", None, 20, "opus", seed=1)
    assert r.setup_env() == {"BG3AI_SCENARIO_SETUP": "1"}


def test_a_sides_op_carries_its_side_and_a_harness_op_none(monkeypatch):
    sent: list[str] = []

    def lua_eval(code: str, context: str, timeout: float) -> str:
        sent.append(code)
        return json.dumps({"ok": True, "result": {}})

    monkeypatch.setattr(gio, "lua_eval", lua_eval)
    gio.rpc("act.end_turn", {"char": "u1"}, side="B")
    gio.rpc("arena.status")
    payloads = [json.loads(c.split("[==[")[1].split("]==]")[0]) for c in sent]
    assert payloads == [{"op": "act.end_turn", "args": {"char": "u1"}, "side": "B"}, {"op": "arena.status", "args": {}}]
    assert sent[1] == 'return Mods.BG3AI.BG3AI.rpc([==[{"op": "arena.status", "args": {}}]==])'


def test_the_live_game_sends_an_op_to_the_context_asked_for(monkeypatch):
    """The referee's ui.state and ui.react are the mod's client context's (live 7: sent to the server context, every
    unowned reaction check failed "unknown op 'ui.state' in server context")."""
    sent: list[tuple[str, str]] = []

    def lua_eval(code: str, context: str, timeout: float) -> str:
        sent.append((json.loads(code.split("[==[")[1].split("]==]")[0])["op"], context))
        return json.dumps({"ok": True, "result": {}})

    monkeypatch.setattr(gio, "lua_eval", lua_eval)
    game = live.LiveGame(None)
    game.rpc("ui.state", {}, context="client")
    game.rpc("arena.status", {})
    game.rpc("act.end_turn", {"char": "u1"}, "B")
    assert sent == [("ui.state", "client"), ("arena.status", "server"), ("act.end_turn", "server")]
