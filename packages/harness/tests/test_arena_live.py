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
from bg3harness import clients, pi_client, profiles
from bg3harness.arena import agents as aagents
from bg3harness.arena import live
from bg3harness.arena.spec import ArenaSpec
from bg3harness.game import io as gio
from bg3harness.runner import state as rstate
from bg3harness.runner.core import SessionRunner

REPO = Path(__file__).resolve().parents[3]
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
    c = aagents.ModelClock()
    for e, t in [
        ev("init", 100),
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


def test_model_time_starts_at_the_clients_init_not_the_process_start():
    """The process start, the client's startup and its MCP server's connect before init are not the side's model
    time (S3-7: the first phase's side and a resumed side were charged them); a second init doesn't restart it."""
    c = aagents.ModelClock()
    assert c.model_s(90) == 0.0  # started, no init yet
    for e, t in [ev("init", 95), ev("tool_call", 97, call_id="1"), ev("init", 98)]:
        c.on_event(e, t)
    assert c.model_s(120) == pytest.approx(2.0)  # 95 to 97; the call still open


def test_a_resumed_pi_process_is_charged_its_first_response():
    """pi's resume has no system message (runner/fixtures/pi/resume.jsonl: session, bg3.tools, the prompt, the
    answer): init comes at the prompt, so the time to the first answer is the side's (REVIEW-18i: it was charged 0)."""
    p, c = pi_client.PiParser("m"), aagents.ModelClock()
    kinds: dict[float, list[str]] = {}
    for t, line in enumerate((REPO / "runner" / "fixtures" / "pi" / "resume.jsonl").read_text().splitlines()):
        for e in p.parse(line):
            c.on_event(e, float(t))
            kinds.setdefault(float(t), []).append(e.kind)
    prompt = next(t for t, ks in kinds.items() if "init" in ks)
    answer = next(t for t, ks in kinds.items() if "text" in ks)
    done = next(t for t, ks in kinds.items() if "result" in ks)
    assert prompt < answer <= done  # init at the prompt's message_end, before the answer's
    assert c.model_s(done + 10) == pytest.approx(done - prompt)


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
    assert req.env["BG3AI_HANDLES_PATH"] == str(tmp_path / "B.handles.json")  # the side's: no tag, a resume keeps it
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


def test_an_agent_process_charges_no_model_time_before_its_clients_init(tmp_path):
    t = [1000.0]
    p = aagents.AgentProcess(
        "A",
        aagents.SideClient(SpendingClient([]), "m", None, {}),
        "go",
        "c",
        False,
        "",
        tmp_path,
        {},
        clock=lambda: t[0],
    )
    t[0] = 1030.0  # 30 s of startup: no init came
    assert p.model_s() == 0.0
    p.stop()


class GatedClient(ScriptClient):
    """Prints its first line, waits for the file `gate`, prints its second, then thinks on until stopped."""

    def __init__(self, first: dict, second: dict, gate: Path) -> None:
        super().__init__([first, second])
        self.gate = gate

    def command(self, req: clients.Request) -> list[str]:
        a, b = (json.dumps(x) + "\n" for x in self.lines)
        code = (
            f"import os, sys, time; sys.stdout.write({a!r}); sys.stdout.flush()\n"
            f"while not os.path.exists({str(self.gate)!r}): time.sleep(0.01)\n"
            f"sys.stdout.write({b!r}); sys.stdout.flush(); time.sleep(60)"
        )
        return [sys.executable, "-c", code]


def test_silent_model_time_runs_from_the_streams_last_line(tmp_path):
    gate = tmp_path / "gate"
    client = GatedClient({"kind": "init", "tools": 3, "mcp": []}, {"kind": "usage", "cost_usd": 0.03}, gate)
    t = [1000.0]
    p = aagents.AgentProcess(
        "A", aagents.SideClient(client, "m", None, {}, 3), "go", "c", False, "", tmp_path, {}, clock=lambda: t[0]
    )
    deadline = time.monotonic() + 20
    while p.model.since is None and time.monotonic() < deadline:  # the pump read the init: the model clock runs
        time.sleep(0.01)
    t[0] = 1060.0
    assert (p.model_s(), p.silent_model_s()) == (60.0, 60.0)
    gate.touch()
    while p.cost_usd() < 0.03 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (p.model_s(), p.silent_model_s()) == (60.0, 0.0)  # the line reset it
    t[0] = 1090.0
    assert (p.model_s(), p.silent_model_s()) == (90.0, 30.0)
    p.stop()


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


OVERFLOW = [
    {"kind": "error", "text": "400 maximum context length is 200000 tokens"},
    {"kind": "result", "subtype": "error_context_overflow", "text": "maximum context length", "is_error": True},
]
CALL = {"kind": "tool_call", "tool": "end_turn", "call_id": "c1"}


def test_an_overflow_before_the_sides_first_tool_call_is_a_fault_not_a_forfeit(tmp_path):
    """A6: the side that caused it forfeits. With no tool call yet the harness's prompt filled the context, so the
    exit is a fault (client_fault, rerun), named for what it is."""
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient(OVERFLOW), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    ex = run_to_end(p)
    assert (ex.reason, ex.fault) == ("kickoff_overflow", True)


def test_a_resume_with_no_call_of_its_own_counts_the_sides_earlier_calls(tmp_path):
    """A resumed process overflowing before its own first call sends a conversation the model grew: context_overflow.
    A side that has made no call in any process is still at the kickoff."""
    client = ScriptClient([CALL])
    agents = aagents.LiveAgents({"A": aagents.SideClient(client, "m", None, {})}, tmp_path, {}, 45)
    run_to_end(agents.start("A", "go", None, ""))
    client.lines = OVERFLOW
    assert run_to_end(agents.start("A", "go", "c", "-r1")).reason == "context_overflow"

    client = ScriptClient([])
    agents = aagents.LiveAgents({"A": aagents.SideClient(client, "m", None, {})}, tmp_path, {}, 45)
    run_to_end(agents.start("A", "go", None, ""))
    client.lines = OVERFLOW
    assert run_to_end(agents.start("A", "go", "c", "-r2")).reason == "kickoff_overflow"


@pytest.mark.parametrize(
    "text",
    [
        "ThrottlingException: Too many tokens, please wait before trying again.",
        "Too many tokens, please wait before trying again.",
        "429 Too many tokens per minute",
        "429 Request too large: too many tokens, limit 40000 TPM",
        "429 quota exceeded: too many tokens",
    ],
)
def test_a_throttle_the_parser_calls_an_overflow_never_forfeits_a_match(tmp_path, text):
    """The parser's OVERFLOW_RE matches "too many tokens": after the side's first call this must stay a client
    failure (rerun), never a valid loss from a transient limit."""
    lines = [
        CALL,
        {"kind": "error", "text": text},
        {"kind": "result", "subtype": "error_context_overflow", "text": text, "is_error": True},
    ]
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient(lines), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    ex = run_to_end(p)
    assert (ex.reason, ex.fault) == ("api_error", True)


ANTHROPIC = "input length and `max_tokens` exceed context limit: 190000 + 20000 > 200000, decrease input length"


@pytest.mark.parametrize(("before", "reason"), [([CALL], "context_overflow"), ([], "kickoff_overflow")])
def test_anthropics_context_limit_wording_reaches_a6_though_the_parser_misses_it(tmp_path, before, reason):
    """The parser leaves this text error_during_execution (bench and playthrough stay as they were): a match still
    reads it as an overflow, with the same two ends."""
    lines = [
        *before,
        {"kind": "error", "text": ANTHROPIC},
        {"kind": "result", "subtype": "error_during_execution", "text": ANTHROPIC, "is_error": True},
    ]
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient(lines), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    assert run_to_end(p).reason == reason


def test_a_context_overflow_is_its_own_exit_even_after_a_transient_api_error(tmp_path):
    """pi exits 0 after an overflow: the exit says context_overflow (the match forfeits on it, A6), not `exited`
    (which resumed the full conversation) and not api_error from an earlier overloaded response."""
    lines = [
        {"kind": "tool_call", "tool": "end_turn", "call_id": "c1"},
        {"kind": "error", "text": "API Error: overloaded", "api_error": True},
        *OVERFLOW,
    ]
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient(lines), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    assert run_to_end(p).reason == "context_overflow"


def test_a_stream_ending_on_a_400_is_a_client_failure_not_a_session_end(tmp_path):
    """A6: a 400 that is no overflow stays infrastructure. pi exits 0 and its text matches no API pattern, so it read
    as the model ending its session, three of which lost the match (client_exit)."""
    lines = [{"kind": "result", "subtype": "error_during_execution", "text": "400 invalid_request", "is_error": True}]
    p = aagents.AgentProcess(
        "A", aagents.SideClient(ScriptClient(lines), "m", None, {}), "go", "c", False, "", tmp_path, {}
    )
    ex = run_to_end(p)
    assert (ex.reason, ex.fault) == ("api_error", True)


def test_a_process_stuck_in_pis_overflow_loop_is_stopped_as_an_overflow(tmp_path):
    """pi #11161: pi would go on after the parser cuts the loop, so the process is stopped."""
    lines = [
        CALL,
        OVERFLOW[0],
        {"kind": "result", "subtype": "error_overflow_loop", "text": "pi started overflow recovery 4 times"},
    ]
    client = SpendingClient(lines)
    p = aagents.AgentProcess("A", aagents.SideClient(client, "m", None, {}), "go", "c", False, "", tmp_path, {})
    deadline = time.monotonic() + 20
    while p.overflow is None and time.monotonic() < deadline:  # the pump thread reads the line
        time.sleep(0.02)
    ex = p.done()
    assert ex is not None and ex.reason == "context_overflow"


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


@pytest.mark.parametrize(("name", "cap"), [("arena-v0.1", "90"), ("crash-companions-v1", "60")])
def test_a_sides_rules_state_its_arenas_turn_cap(tmp_path, name, cap):
    # arena-builds C4: the per-turn cap is a fact in the match rules, the arena file's own number
    from bg3harness import prompts

    a = json.loads((Path(__file__).resolve().parents[2] / "bench" / "arenas" / f"{name}.json").read_text())
    a.pop("draft", None)  # crash-companions-v1 is a draft (its spawns' heights); the cap is what this reads
    env = live.side_env(ArenaSpec.from_dict(a), "A", None, tmp_path)
    assert env["BG3AI_ARENA_TURN_CAP_S"] == cap
    text = " ".join(prompts().assemble("rules", arena=True, turn_cap_s=float(cap), turn_order="alternate").text.split())
    assert f"A turn gets {cap} s of model time per character in it" in text and "{turn_cap_s}" not in text
    with pytest.raises(ValueError, match="turn_cap_s"):
        prompts().assemble("rules", arena=True, turn_order="alternate")


def test_a_sides_rules_state_its_arenas_turn_order(tmp_path):
    """The auditor's O1 S3: the rules say which turn order the arena plays (a human is told the house rule), the
    arena file's turn_order through the side's env; no placeholder is left and an unknown order is refused."""
    from bg3harness import prompts, run_manifest

    a = json.loads((Path(__file__).resolve().parents[2] / "bench" / "arenas" / "arena-v0.1.json").read_text())
    texts = {}
    for order in ("alternate", "snake"):
        env = live.side_env(ArenaSpec.from_dict(dict(a, turn_order=order)), "B", None, tmp_path)
        assert env["BG3AI_ARENA_TURN_ORDER"] == run_manifest.arena_turn_order(env) == order
        asm = prompts().assemble("rules", arena=True, turn_cap_s=90.0, turn_order=env["BG3AI_ARENA_TURN_ORDER"])
        texts[order] = " ".join(asm.text.split())
        assert "{turn_order}" not in texts[order]
    assert "each turn group opens with the side of its first living character in the rolled" in texts["alternate"]
    assert "the sides take turns opening rounds, in every turn group" in texts["snake"]
    assert run_manifest.arena_turn_order({}) is None  # not a side
    with pytest.raises(ValueError, match="BG3AI_ARENA_TURN_ORDER"):  # a side without it: no silent default
        run_manifest.arena_turn_order({"BG3AI_SIDE": "A"})
    for bad in ("initiative", None):
        with pytest.raises(ValueError, match="turn_order"):
            prompts().assemble("rules", arena=True, turn_cap_s=90.0, turn_order=bad)


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


def test_the_matchs_game_takes_the_mouse_after_its_settings(monkeypatch):
    """Live window 7: the shim's agent mode and a centred cursor before the match (the scripted sides send no mouse
    command; the real pointer on the window's bottom row edge-scrolled the view). After the settings check."""
    done: list[str] = []

    class R:
        def __init__(self) -> None:
            self.state = {"loaded_save": "Tav-1__SCN-arena_test"}

        def refresh_mod_snapshot(self) -> None:
            done.append("snapshot")

        def ensure_game(self, first: bool) -> None:
            done.append("ensure")

        def hot_reload_mod(self) -> None:
            done.append("reload")

        def check_settings(self) -> None:
            done.append("settings")

    monkeypatch.setattr(live.agent_group, "reap_leftover", lambda *a: None)
    monkeypatch.setattr(gio, "take_input", lambda: done.append("take_input") or "input shim: taken")
    logs: list[str] = []
    monkeypatch.setattr(live.renv, "log", logs.append)
    live.start_game(R(), ArenaSpec.from_dict(ARENA))
    assert done == ["snapshot", "ensure", "reload", "settings", "take_input"]
    assert "input shim: taken" in logs
