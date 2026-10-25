"""bg3harness.runner.run_host: the bench's live RunHost with a fake runner (no game, no client process).

A save start and a new-game start become their Options and kickoff; start resets the run (saves from now, a save
start's nav files moved into the run dir), puts the game at the start in order and hands back the runner's session
hooks; record ends facts.jsonl, stops the game, puts the nav files back and returns the run's fields with the outcome
the harness can tell (live7: the SE folder stayed empty after a bench run)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bg3harness import client_setup
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.runner import state as rstate
from bg3harness.runner.checks_side import ChecksSide
from bg3harness.session import Recovery, SessionOutcome, SessionSpec, run_session
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_env, call
from bg3harness.testing.scripted import FakeClock, GameModel
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SAVE_SC: dict[str, Any] = {
    "name": "grove_fight",
    "title": "The grove gate fight",
    "start": {
        "kind": "save",
        "save": "Lae'zel-12__SCN-grove_fight",
        "note": {"where": "The grove gate", "objective": "Win the fight at the gate"},
    },
    "goal": {"hostiles_dead": {"area": "gate"}},
    "caps": {"run_hours": 0.5, "max_cost": 2.0, "max_tokens": None},
    "game_settings": {"difficulty": "Balanced", "karmic_dice": False, "seed": 7},
    "timing": "turn_based",
    "profile": {"character": {"name": "Lae'zel", "brief": "A githyanki warrior.", "roleplay": "light"}},
    "reset": {"nav_state": "archive"},
}
NEW_SC: dict[str, Any] = {
    **SAVE_SC,
    "name": "sell_idol",
    "start": {"kind": "new_game"},
    "profile": {"character": {"brief": "A careful trader.", "roleplay": "strict"}},
}
UNTIL_SC: dict[str, Any] = {k: v for k, v in NEW_SC.items() if k != "goal"} | {
    "name": "nautiloid",
    "until": "nautiloid_escaped",
}
JOB = {
    "model": "opus",
    "client": "claude",
    "effort": "high",
    "condition": "rules",
    "prompt_condition": "rules",
    "max_cost": None,
}


class State(dict[str, Any]):
    def __init__(self, d: Path):
        super().__init__(history=[], loaded_save=None)
        self.dir = d


class FakeRunner:
    """The SessionRunner calls the host makes, recorded in order."""

    def __init__(self, opt: rstate.Options, calls: list[str]):
        self.opt, self.calls = opt, calls
        self.state = State(renv.RUNTIME / "sessions" / opt.playthrough)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cap_hit: dict[str, Any] | None = None
        self.until_hit: dict[str, Any] | None = None
        self.mod_rev: str | None = None
        self.run_started: float | None = None
        self.settles_budget = True
        self.stop_ends_session = False
        self.stop_requested = False
        self.abort = False
        self.run_env: dict[str, str] | None = None
        self.game_settings: dict[str, Any] = {"difficulty": "Balanced"}  # game_setup's readout
        if opt.seed is not None:  # the mod applied the run's seed
            self.game_settings["seed"] = {"seed": opt.seed, "applied": {"seed": opt.seed}}
        self.facts_fail = False
        self.fail_at: dict[str, BaseException] = {}  # a call -> what it raises
        self.loads: str | None = opt.start_save  # the save folder ensure_game loads
        self.state["oom_streak"] = None
        self.plays: list[tuple[str, str, float]] = []  # run_claude's (tag, prompt, minutes)
        self.play: Callable[[FakeRunner, str], tuple[str, float]] = lambda r, tag: ("exited", 60.0)  # (reason, s)
        self.clock = 1000.0  # the session hooks' now (run_claude moves it)
        self.boot: str | None = "boot-start"  # the server Lua state's id (server_boot): a load makes a new one
        self.marks: list[tuple[str, Any]] = []  # facts.jsonl marks (facts_writer)
        self.read_now: Callable[[], None] = lambda: None  # what a facts read now (facts_now) finds in the game

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def server_boot(self) -> str | None:
        return self.boot

    def facts_writer(self) -> FakeRunner:
        return self

    def mark(self, mark: str, detail: Any = None, now: float | None = None) -> None:
        self.marks.append((mark, detail))

    def facts_now(self) -> None:
        self.calls.append("facts_now")
        self.read_now()

    def refresh_mod_snapshot(self) -> list[str]:
        self.calls.append("snapshot")
        return []

    def ensure_game(self, first: bool) -> str | None:
        self.calls.append(f"ensure_game({first})")
        if "ensure_game" in self.fail_at:
            raise self.fail_at["ensure_game"]
        self.state["loaded_save"] = self.loads
        return "SCN-grove_fight" if self.loads else None

    def hot_reload_mod(self) -> int:
        self.calls.append("reload")
        return 0

    def apply_play_settings(self) -> None:
        self.calls.append("settings")

    def check_settings(self) -> None:
        self.calls.append("check_settings")
        if "check_settings" in self.fail_at:
            raise self.fail_at["check_settings"]

    def end_facts(self, outcome: str) -> None:
        self.calls.append(f"end_facts({outcome})")
        if self.facts_fail:
            raise RuntimeError("the game stopped answering")

    def scenario_undo(self) -> None:
        self.calls.append("scenario_undo")

    def end_run_game(self, when: str = "", after: str = "") -> None:
        self.calls.append(f"end_run_game({when})")

    def kill_agent(self) -> None:
        self.calls.append("kill_agent")

    def run_claude(
        self, sid: str, prompt: str, minutes: float, session: str | None, resume: bool = False, tag: str = ""
    ) -> rstate.SessionResult:
        self.plays.append((tag, prompt, minutes))
        reason, seconds = self.play(self, tag)
        start, self.clock = self.clock, self.clock + seconds
        return rstate.SessionResult(sid, session or "c-1", 0, reason, start, self.clock)

    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None:
        return None  # the playthrough memory's wrap-up: nothing in a bench run (no memory session)

    def session_hooks(self, sid: str, minutes: float) -> rstate.RunnerSessionHooks:
        hooks = rstate.RunnerSessionHooks(self, sid, minutes)  # pyright: ignore[reportArgumentType]
        hooks.now = lambda: self.clock  # pyright: ignore[reportAttributeAccessIssue]
        return hooks


@dataclass
class Rig:
    h: H.LiveRunHost
    calls: list[str]
    runners: list[FakeRunner]
    dumped: list[tuple[Path, str, int | None, str | None]]
    env: dict[str, str]
    prepare: Callable[[FakeRunner], None] | None  # set up each new runner (failures, the save it loads)
    now: list[float] = field(default_factory=lambda: [1000.0])  # SaveWatch's clock (its thread never polls)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(renv, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(renv, "SE_BG3AI_DIR", tmp_path / "se" / "BG3AI")
    monkeypatch.setattr(renv, "log", lambda msg: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    renv.SE_BG3AI_DIR.mkdir(parents=True)
    for name in ("nav_guard_aa.json", "nav_aa.json", "other.json"):
        (renv.SE_BG3AI_DIR / name).write_text("{}")
    rig = Rig(H.LiveRunHost(lambda opt: FakeRunner(opt, [])), [], [], [], {}, None)

    def make(opt: rstate.Options) -> FakeRunner:
        rig.runners.append(FakeRunner(opt, rig.calls))
        if rig.prepare:
            rig.prepare(rig.runners[-1])
        return rig.runners[-1]

    def run_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        rig.dumped.append((d, client, seed, timing))
        return {"tools_expected": 90, "manifest": {"m": 1}}

    rig.h = H.LiveRunHost(make, run_files=run_files, env=rig.env, clock=lambda: 1000.0)
    monkeypatch.setattr(renv, "SAVE_DIR", tmp_path / "Story")
    renv.SAVE_DIR.mkdir()

    def save_watch(r: H.RunRunner, own: Callable[[str], bool]) -> H.SaveWatch:
        return H.SaveWatch(r, own, clock=lambda: rig.now[0], poll_s=1e9)  # the tests call check()

    monkeypatch.setattr(rig.h, "save_watch", save_watch)
    return rig


def run_dir(name: str) -> Path:
    return renv.RUNTIME / "sessions" / name


def test_options_of_a_save_start_and_a_new_game():
    opt = H.options_for(JOB, SAVE_SC, "m-grove", {"tools_expected": 90})
    assert (opt.playthrough, opt.session_minutes, opt.run_hours, opt.hours) == ("m-grove", 30.0, 0.5, None)
    assert (opt.start_save, opt.save_owner, opt.scenario, opt.seed) == (
        SAVE_SC["start"]["save"],
        "Lae'zel",
        "grove_fight",
        7,
    )
    assert (opt.keeper, opt.judge, opt.max_cost, opt.tools_expected) == (False, False, 2.0, 90)
    assert opt.scenario_settings == {  # a save start plays under the scenario's character (live B1)
        "difficulty": "Balanced", "karmic_dice": False, "timing": "turn_based", "avatar_name": "Lae'zel"
    }  # fmt: skip
    new = H.options_for({**JOB, "client": "pi", "max_cost": 0.5}, NEW_SC, "m-idol", {})
    assert (new.start_save, new.save_owner, new.max_cost, new.character_brief) == (
        None,
        "Tav",
        0.5,
        "A careful trader.",
    )
    assert new.scenario_settings is not None and new.scenario_settings["avatar_name"] is None  # named in creation


def test_the_matrix_max_cost_caps_a_paid_client_only():
    # fixlive (Window C): the matrix's $0.25 DeepSeek cap stopped a Claude Code nautiloid run at its first end_turn
    # ($2.12 notional, over_budget); a subscription run is never charged (bg3bench.matrix, BENCHMARK.md)
    no_cap = {**UNTIL_SC, "caps": {**UNTIL_SC["caps"], "max_cost": None}}
    assert H.options_for({**JOB, "max_cost": 0.25}, no_cap, "m-naut", {}).max_cost is None
    assert H.options_for({**JOB, "max_cost": 0.25}, UNTIL_SC, "m-naut", {}).max_cost == 2.0  # the scenario's cap
    assert H.options_for({**JOB, "client": "pi", "max_cost": 0.25}, no_cap, "m-naut", {}).max_cost == 0.25


def test_an_until_scenario_ends_at_its_milestone():
    # live7: the nautiloid run reached nautiloid_escaped 12 min in and played on for run_hours (Options had no until)
    opt = H.options_for(JOB, UNTIL_SC, "m-naut", {})
    assert opt.until == "nautiloid_escaped"
    assert H.options_for(JOB, SAVE_SC, "m-grove", {}).until is None  # a goal scenario: graded on facts.jsonl
    events: list[str] = []

    def event(kind: str, **fields: Any) -> None:
        events.append(kind)

    runner = SimpleNamespace(opt=opt, until_hit=None, event=event)
    check_until: Callable[..., dict[str, Any] | None] = ChecksSide.check_until  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    escaped = {"quests": {"Escape the Nautiloid": {"steps": 4, "done": True}}}
    assert check_until(runner, {"quests": {}}, "S001") is None
    hit = check_until(runner, escaped, "S001")
    assert hit is not None and hit["id"] == "nautiloid_escaped" and events == ["milestone"]


def test_the_kickoff_names_the_start_the_goal_and_the_character_and_no_memory_steps():
    text = H.scenario_kickoff(SAVE_SC, "S001", "SCN-grove_fight")
    assert 'loaded save "SCN-grove_fight"' in text and "Never start a new game" in text
    assert "Objective: Win the fight at the gate" in text and "Where: The grove gate" in text
    assert json.dumps(SAVE_SC["goal"], sort_keys=True) in text and "A githyanki warrior." in text
    assert "scored on the game state at its end" not in text  # graders.grade latches the first instant
    assert 'passes the first moment it holds, from the start save on; anything under "never" fails it' in text
    assert "and so does a party wipe (the Game Over screen)" in text  # graders: a wipe fails a goal run
    assert "YOUR TASK: Win the fight at the gate" in text  # no `task`: the start note's objective
    assert "after 30 minutes of play" in text and "WRAP UP" not in text  # the run ends on its verdict or the cap
    assert "from the new game on" in H.scenario_kickoff(NEW_SC, "S001", None)
    for playthrough_only in ("session_start", "playbook", "memory_", "HANDOFF"):
        assert playthrough_only not in text
    new = H.scenario_kickoff(NEW_SC, "S001", None, pace="PACE: two actions per turn")
    assert 'Name your character exactly "Tav"' in new and new.endswith("PACE: two actions per turn")
    assert "PACE" not in H.scenario_kickoff(NEW_SC, "S001", None)


def test_the_kickoff_leads_with_the_task_and_how_it_is_scored():
    sc = {**SAVE_SC, "task": "Win the fight at the gate.", "scored_by": "Passed once a fight is won."}
    lines = H.scenario_kickoff(sc, "S001", "SCN-grove_fight").split("\n\n")
    assert lines[1:3] == ["YOUR TASK: Win the fight at the gate.", "How it is scored: Passed once a fight is won."]


def test_the_kickoff_of_an_until_scenario_names_its_milestone():
    # live7: nautiloid has `until`, no `goal`; the kickoff read sc["goal"] -> KeyError at every start
    until = {k: v for k, v in NEW_SC.items() if k != "goal"} | {"until": "nautiloid_escaped"}
    text = H.scenario_kickoff(until, "S001", None)
    assert json.dumps({"milestone": "nautiloid_escaped"}, sort_keys=True) in text


def test_start_resets_the_run_and_puts_the_game_at_the_scenario_start(rig: Rig):
    h, calls, runners, dumped = rig.h, rig.calls, rig.runners, rig.dumped
    st = h.start(JOB, SAVE_SC, run_dir("m-grove"))
    (r,) = runners
    # check_settings renames a start save's avatar ("Claude") to the save owner before apply_play_settings decides the
    # save/load gates from that name (live B2: the gates were closed for every save-start run)
    assert calls == ["snapshot", "ensure_game(True)", "reload", "check_settings", "settings"]
    assert (r.state["saves_after"], r.run_started) == (1000.0, 1000.0)
    assert not r.settles_budget  # the matrix adds the run's cost to budget.json, not the runner
    assert sorted(p.name for p in (run_dir("m-grove") / "reset").iterdir()) == ["nav_aa.json", "nav_guard_aa.json"]
    assert [p.name for p in renv.SE_BG3AI_DIR.iterdir()] == ["other.json"]
    assert r.events == [("run_reset", {"nav_files": ["nav_aa.json", "nav_guard_aa.json"]})]
    assert h.env["BG3AI_PROMPT_CONDITION"] == "rules" and (r.opt.tools_expected, r.opt.manifest) == (90, {"m": 1})
    assert dumped == [(run_dir("m-grove"), "claude", 7, "turn_based")]
    assert st.prompt == H.scenario_kickoff(SAVE_SC, "S001", "SCN-grove_fight")  # rules: no pace pre-flight
    kickoff = {"file": "kickoff", "sha256": hashlib.sha256(st.prompt.encode()).hexdigest()}
    assert (st.prompt_sha, st.prompt_parts, st.tools, st.client_version) == (None, (kickoff,), None, None)
    assert st.harness_version
    assert isinstance(st.hooks, H.BenchHooks) and isinstance(inner := st.hooks.hooks, rstate.RunnerSessionHooks)
    assert (inner.sid, inner.minutes) == ("S001", 30.0)


def test_the_manifest_gets_the_served_prompt_tools_and_client_version(rig: Rig):  # live9: the kickoff's hash only
    instr = {"file": "server:instructions", "sha256": "a" * 64}
    tools = {"allowlist_sha256": "b" * 64, "count": 82}
    man = {"client": {"name": "claude", "version": "2.1.301"}, "prompt": {"sha256": "c" * 64, "parts": [instr]}}

    def run_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        return {"manifest": man | {"tools": tools}}

    rig.h.run_files = run_files
    st = rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    kickoff = {"file": "kickoff", "sha256": hashlib.sha256(st.prompt.encode()).hexdigest()}
    assert (st.prompt_sha, st.prompt_parts) == ("c" * 64, (instr, kickoff))
    assert (st.tools, st.client_version) == (tools, "2.1.301")


def test_a_bench_run_serves_no_memory_tools_and_its_own_empty_memory_dir(rig: Rig):
    # live7: session_start served play/memory's briefing (another playthrough's notes) to a bench run
    rig.h.start({**JOB, "tools_off": ["wiki_search", "wiki_page"]}, SAVE_SC, run_dir("m-grove"))
    assert rig.h.env["BG3AI_MEMORY_DIR"] == str(run_dir("m-grove") / "memory")
    assert rig.h.env["BG3AI_TOOLS_OFF"] == "memory,wiki_search,wiki_page"
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))  # the next job's env replaces the last one's
    assert rig.h.env["BG3AI_TOOLS_OFF"] == "memory"
    assert rig.h.env["BG3AI_MEMORY_DIR"] == str(run_dir("m-idol") / "memory")


def plant_memory(mem: Path) -> None:
    """Another playthrough's memory where the bridge looks by default (play/memory)."""
    mem.mkdir(parents=True)
    for name in ("PLAYTHROUGH.md", "HANDOFF.md", "CHARACTER.md", "lessons.md"):
        (mem / name).write_text(f"# {name}\nLEAKED-NOTE from {name}\n")


async def served(home: Path, env: dict[str, str], calls: list[str]) -> tuple[str, set[str], list[str]]:
    """The real bridge (over the fake mod) with this env: its instructions, its tools and the first results."""
    with FakeMod(GameModel(clock=FakeClock())) as fake:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "bg3ai.server"], env=bridge_env(fake.port, home, env), cwd=str(home)
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            init = await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            return init.instructions or "", tools, [await call(s, name) for name in calls]


@pytest.mark.integration
def test_a_bench_run_sees_no_playthrough_memory_through_the_real_bridge(rig: Rig, tmp_path: Path):
    plant_memory(tmp_path / "home" / "play" / "memory")
    playthrough = {"BG3AI_SESSION": "S001"}  # the runner's env for a session it opened
    _, tools, (briefing,) = asyncio.run(
        asyncio.wait_for(served(tmp_path / "home", playthrough, ["session_start"]), 120)
    )
    assert "LEAKED-NOTE" in briefing and "memory_read" in tools  # the leak live7 saw: the fixture serves it
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    bench = {k: v for k, v in rig.h.env.items() if k.startswith("BG3AI_")} | playthrough
    text, tools, results = asyncio.run(
        asyncio.wait_for(served(tmp_path / "home", bench, ["get_status", "get_party", "get_journal"]), 99)
    )
    assert not tools & {"session_start", "memory_read", "memory_search", "recall", "note", "end_session"}
    assert "get_status" in tools and "Memory:" not in text
    for out in (text, *results):
        assert "LEAKED-NOTE" not in out and "PLAYTHROUGH MEMORY" not in out and "HARNESS CHANGES" not in out
    assert not (run_dir("m-grove") / "memory").exists()  # nothing filled the run's own memory dir


def test_the_scenario_autoskip_reaches_the_runner_env(rig: Rig):
    """On by default; a scenario's game_settings.autoskip = false turns it off for the runner's ui.config and the
    play server's skipper (BG3AI_AUTOSKIP)."""
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert rig.h.env["BG3AI_AUTOSKIP"] == "1"
    off = {**SAVE_SC, "game_settings": {**SAVE_SC["game_settings"], "autoskip": False}}
    rig.h.start(JOB, off, run_dir("m-grove2"))
    assert rig.h.env["BG3AI_AUTOSKIP"] == "0"


@pytest.mark.integration
def test_a_bench_runs_served_text_and_tools_have_no_memory(rig: Rig, monkeypatch: pytest.MonkeyPatch):
    # the real dump (tools/pi/dump_server.py in the bridge's env) of what a pi bench run is served under "rules"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")  # resolve wants the profile's key; nothing is sent
    for k in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF", "BG3AI_MEMORY_DIR", "BG3AI_PROMPT_CONDITION"):
        monkeypatch.setenv(k, "")  # recorded, so the host's writes to os.environ are undone after the test
    host = H.LiveRunHost(lambda opt: FakeRunner(opt, []), env=os.environ, clock=lambda: 1000.0)
    job = {
        **JOB,
        "client": "pi",
        "model": "deepseek-flash",
        "profile": str(renv.REPO / "profiles" / "deepseek-flash.toml"),
    }
    host.start({**job, "tools_off": ["wiki_search", "wiki_page", "load_game"]}, SAVE_SC, run_dir("m-pi"))
    d = run_dir("m-pi")
    registry = {t["name"] for t in json.loads((d / "bg3-tools.json").read_text())}
    gone = {"memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append"}
    gone |= {"note", "session_start", "end_session", "recall", "wiki_search", "wiki_page", "load_game"}
    assert gone <= registry  # the dump lists the whole registry; the run is served the rest
    (r,) = host.runs.values()
    assert r[0].opt.tools_expected == len(registry - gone)
    assert r[0].opt.manifest is not None and r[0].opt.manifest["prompt"]["sha256"]  # rules: no capability needed
    for name in ("pi-system.txt", "bg3-instructions.txt"):
        text = (d / name).read_text()
        for word in ("memory_read", "session_start", "memory/", "PLAYTHROUGH MEMORY", "HANDOFF", "wiki_search"):
            assert word not in text, (name, word)


def test_a_coaching_condition_gets_the_pace_preflight(rig: Rig):
    # the bench's id names the coaching bundle's hash; the server and the kickoff go by the prompt condition it maps to
    h = rig.h
    job = {**JOB, "condition": "rules+coaching@1a2b3c4d", "prompt_condition": "rules+coaching"}
    st = h.start(job, NEW_SC, run_dir("m-idol"))
    pace = client_setup.kickoff_pace("claude", {"BG3AI_PROMPT_CONDITION": "rules+coaching"})
    assert pace and st.prompt.endswith(pace)
    assert rig.h.env["BG3AI_PROMPT_CONDITION"] == "rules+coaching"


def test_a_run_dir_the_runner_doesnt_write_to_is_refused(rig: Rig, tmp_path: Path):
    h, calls = rig.h, rig.calls
    with pytest.raises(ValueError, match="runtime/sessions"):
        h.start(JOB, SAVE_SC, tmp_path / "elsewhere" / "m-grove")
    assert calls == []


@pytest.mark.parametrize(
    ("at", "exc"),
    [
        ("ensure_game", SystemExit("the game is running with a save loaded that the runner didn't load")),
        ("ensure_game", SystemExit("would have to restart a game the runner didn't launch")),
        ("check_settings", RuntimeError("difficulty Tactician, scenario says Balanced")),
    ],
)
def test_a_failed_start_forgets_the_run_stops_the_game_and_raises_runtime_error(rig: Rig, at: str, exc: BaseException):
    rig.prepare = lambda r: r.fail_at.update({at: exc})
    with pytest.raises(RuntimeError, match=str(exc)) as e:
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert not isinstance(e.value, SystemExit) and rig.h.runs == {}
    assert rig.calls[-2:] == ["scenario_undo", "end_run_game(start failed)"]
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]  # put back
    assert not H.nav_marker().exists()
    rig.prepare = None  # the rerun in a fresh dir starts clean
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove-r2"))
    assert list(rig.h.runs) == [run_dir("m-grove-r2")]


@pytest.mark.parametrize("loads", [None, "Lae'zel-11__other_save"])
def test_a_save_start_whose_save_didnt_load_fails_before_the_kickoff(rig: Rig, loads: str | None):
    def prepare(r: FakeRunner) -> None:
        r.loads = loads

    rig.prepare = prepare
    with pytest.raises(RuntimeError, match="the start save Lae'zel-12__SCN-grove_fight didn't load"):
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    # no reload, no kickoff
    assert rig.calls == ["snapshot", "ensure_game(True)", "scenario_undo", "end_run_game(start failed)"]
    assert rig.h.runs == {}


def outcome(invalid: str | None = None) -> SessionOutcome:
    res = rstate.SessionResult("S001", "c-1", 0, "exited", 1000.0, 1000.0 + 25 * 60)
    return SessionOutcome(res, 1000.0, invalid=invalid)


def test_record_ends_the_facts_stops_the_game_and_returns_the_run(rig: Rig):
    h, calls = rig.h, rig.calls
    h.start(JOB, SAVE_SC, run_dir("m-grove"))
    calls.clear()
    rec = h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert calls == ["end_facts(timeout)", "scenario_undo", "end_run_game(run end)"]
    assert (rec["outcome"], rec["reason"], rec["wall_min"], rec["sessions"]) == ("timeout", "exited", 25.0, 1)
    moved = ["nav_aa.json", "nav_guard_aa.json"]
    assert (rec["cost_usd"], rec["reset"]) == (0.0, {"nav_files": moved, "restored": moved, "saves": []})
    assert rec["served"] is None  # runs.claude_totals' served: no stream, nothing served
    assert (rec["game_settings"], rec["manifest"], rec["loaded_save"]) == (
        {"difficulty": "Balanced", "seed": {"seed": 7, "applied": {"seed": 7}}},
        {"m": 1},
        SAVE_SC["start"]["save"],
    )


def test_a_goal_runs_live_verdict_ends_it_and_names_its_outcome(rig: Rig):
    h, runners = rig.h, rig.runners
    said: list[str | None] = [None]
    h.start({**JOB, "verdict": lambda: said[0]}, SAVE_SC, run_dir("g1"))
    watch, r = h.watches[run_dir("g1")], runners[-1]
    assert not watch.check() and r.until_hit is None  # undecided: the run plays on
    said[0] = "passed"
    assert watch.check() and r.until_hit is not None and r.until_hit["verdict"] == "passed"
    assert ("goal_verdict", {"verdict": "passed", "session": "S001"}) in r.events
    rec = h.record(JOB, SAVE_SC, run_dir("g1"), outcome())
    assert (rec["outcome"], rec["milestone"]) == ("reached", None) and watch.done.is_set() and not h.watches
    said[0] = "failed"
    h.start({**JOB, "verdict": lambda: said[0]}, SAVE_SC, run_dir("g2"))
    assert h.watches[run_dir("g2")].check()
    assert h.record(JOB, SAVE_SC, run_dir("g2"), outcome())["outcome"] == "failed"


def test_a_verdict_that_raises_doesnt_end_the_run(rig: Rig):
    def broken() -> str | None:
        raise OSError("facts.jsonl gone")

    rig.h.start({**JOB, "verdict": broken}, SAVE_SC, run_dir("g"))
    assert not rig.h.watches[run_dir("g")].check() and rig.runners[-1].until_hit is None
    assert rig.h.record(JOB, SAVE_SC, run_dir("g"), outcome())["outcome"] == "timeout"


def test_record_outcomes_and_the_game_stops_even_when_the_facts_fail(rig: Rig):
    h, calls, runners = rig.h, rig.calls, rig.runners
    h.start(JOB, SAVE_SC, run_dir("a"))
    assert h.record(JOB, SAVE_SC, run_dir("a"), outcome(invalid="crash"))["outcome"] == "gave_up"
    h.start(JOB, SAVE_SC, run_dir("b"))
    runners[-1].cap_hit = {"cap": "cost"}
    assert h.record(JOB, SAVE_SC, run_dir("b"), outcome())["outcome"] == "over_budget"
    h.start(JOB, SAVE_SC, run_dir("c"))
    runners[-1].facts_fail = True
    calls.clear()
    with pytest.raises(RuntimeError):
        h.record(JOB, SAVE_SC, run_dir("c"), outcome())
    assert calls == ["end_facts(timeout)", "scenario_undo", "end_run_game(run end)"]


def se_names() -> list[str]:
    return sorted(p.name for p in renv.SE_BG3AI_DIR.iterdir())


def test_record_puts_the_nav_files_back_and_keeps_the_runs_own(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert H.nav_marker().exists() and se_names() == ["other.json"]
    (renv.SE_BG3AI_DIR / "nav_aa.json").write_text('{"run": 1}')  # the run's game wrote its host's file again
    rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert (renv.SE_BG3AI_DIR / "nav_aa.json").read_text() == "{}"
    assert (run_dir("m-grove") / "reset" / "run" / "nav_aa.json").read_text() == '{"run": 1}'
    assert not H.nav_marker().exists()


def test_the_nav_files_come_back_when_the_game_stop_fails(rig: Rig):
    def prepare(r: FakeRunner) -> None:
        def boom(when: str = "", after: str = "") -> None:
            raise RuntimeError("kill failed")

        r.end_run_game = boom  # pyright: ignore[reportAttributeAccessIssue]

    rig.prepare = prepare
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    with pytest.raises(RuntimeError, match="kill failed"):
        rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]


def test_a_new_game_moves_no_nav_file(rig: Rig):  # its host is a new uuid: no file is its own
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert rig.runners[-1].events == [("run_reset", {"nav_files": []})]
    assert rig.h.record(JOB, NEW_SC, run_dir("m-idol"), outcome())["reset"] == {
        "nav_files": [],
        "restored": [],
        "saves": [],
    }


@pytest.mark.parametrize("sc", [NEW_SC, SAVE_SC])
def test_record_moves_the_runs_own_files_out_of_the_shared_folder(rig: Rig, sc: dict[str, Any]):  # live9 #11
    rig.h.start(JOB, sc, run_dir("m-idol"))
    for name in ("nav_bb.json", "nav_guard_bb.json", "scenario.json"):  # the run's new host and its seed record
        (renv.SE_BG3AI_DIR / name).write_text('{"run": 1}')
    rig.h.record(JOB, sc, run_dir("m-idol"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert sorted(p.name for p in (run_dir("m-idol") / "reset" / "run").iterdir()) == [
        "nav_bb.json",
        "nav_guard_bb.json",
        "scenario.json",
    ]
    assert not H.nav_marker().exists()


def test_a_scenario_record_from_before_the_run_stays(rig: Rig):
    (renv.SE_BG3AI_DIR / "scenario.json").write_text('{"renames": []}')
    rig.h.start(JOB, NEW_SC, run_dir("m-idol"))
    rig.h.record(JOB, NEW_SC, run_dir("m-idol"), outcome())
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json", "scenario.json"]
    # it stays, so the run's seed must leave it: scn.undo while the game is still up (it seeded every later load)
    assert rig.calls[-2:] == ["scenario_undo", "end_run_game(run end)"]


def test_the_next_start_restores_what_a_dead_run_left_moved(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))  # the process dies here: no record

    def no_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        return {}

    host = H.LiveRunHost(lambda opt: FakeRunner(opt, []), run_files=no_files, env={}, clock=lambda: 2000.0)
    host.start(JOB, NEW_SC, run_dir("m-idol"))
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]
    assert json.loads(H.nav_marker().read_text())["dest"] == str(run_dir("m-idol") / "reset")  # the new run's own


def test_record_of_a_run_that_reached_its_until_milestone(rig: Rig):
    h, calls, runners = rig.h, rig.calls, rig.runners
    h.start(JOB, UNTIL_SC, run_dir("naut"))
    runners[-1].until_hit = {"id": "nautiloid_escaped", "label": "Nautiloid escaped", "t": 1720.0}
    runners[-1].cap_hit = {"cap": "cost"}  # the milestone came first: the agent was stopped there
    calls.clear()
    rec = h.record(JOB, UNTIL_SC, run_dir("naut"), outcome())
    assert (rec["outcome"], rec["milestone"]) == ("reached", "nautiloid_escaped")
    assert calls == ["end_facts(reached)", "scenario_undo", "end_run_game(run end)"]
    h.start(JOB, SAVE_SC, run_dir("grove"))  # the end credits stop any run; a goal scenario isn't "reached" by them
    runners[-1].until_hit = {"id": "game_end", "label": "The end", "t": 1720.0}
    rec = h.record(JOB, SAVE_SC, run_dir("grove"), outcome())
    assert (rec["outcome"], rec["milestone"]) == ("timeout", None)


def test_archive_moves_only_the_nav_files(tmp_path: Path):
    se = tmp_path / "se"
    se.mkdir()
    for name in ("nav_guard.json", "nav_x.json", "keep.json"):
        (se / name).write_text("{}")
    assert H.archive_nav_state(se, tmp_path / "dest") == ["nav_guard.json", "nav_x.json"]
    assert [p.name for p in se.iterdir()] == ["keep.json"]
    assert H.archive_nav_state(tmp_path / "missing", tmp_path / "dest") == []


def stopped(reason: str = "stopped") -> SessionOutcome:
    res = rstate.SessionResult("S001", "c-1", None, reason, 1000.0, 1000.0 + 5 * 60)
    return SessionOutcome(res, 1000.0)


def test_a_run_ends_its_session_on_an_operator_stop_and_is_recorded_invalid(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    r = rig.runners[-1]
    assert (r.stop_ends_session, r.stop_requested, rig.h.stopped) == (True, False, False)
    rig.h.stop()
    assert r.stop_requested and rig.h.stopped  # the runner's agent loop ends the session on it
    rig.calls.clear()
    rec = rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), stopped())
    assert rig.calls == ["end_facts(gave_up)", "scenario_undo", "end_run_game(run end)"]
    assert (rec["outcome"], rec["reason"], rec["operator"]) == ("gave_up", "stopped", "stop")
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"] and not H.nav_marker().exists()


def test_a_run_that_ended_on_its_own_has_no_operator_mark(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert "operator" not in rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())


def test_no_run_starts_after_a_stop(rig: Rig):
    rig.h.stop()
    with pytest.raises(RuntimeError, match="operator stop: no new run starts"):
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert rig.runners == [] and rig.calls == []  # no runner, no game


def test_the_stop_file_stops_new_runs_too(rig: Rig):
    H.stop_file().parent.mkdir(parents=True, exist_ok=True)
    H.stop_file().write_text("1")
    assert rig.h.stopped
    with pytest.raises(RuntimeError, match="no new run starts"):
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))


def test_a_stop_while_the_game_comes_up_starts_no_agent_and_stops_the_game(rig: Rig):
    def prepare(r: FakeRunner) -> None:  # after start's own check: runtime/STOP written while the game comes up
        H.stop_file().parent.mkdir(parents=True, exist_ok=True)
        H.stop_file().write_text("1")

    rig.prepare = prepare
    with pytest.raises(RuntimeError, match="operator stop before the kickoff"):
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert rig.calls[-2:] == ["scenario_undo", "end_run_game(start failed)"] and rig.h.runs == {}
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"]


def test_abort_kills_the_agent_stops_the_game_and_puts_the_nav_files_back(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    rig.calls.clear()
    rig.h.abort()
    assert rig.calls == ["kill_agent", "scenario_undo", "end_run_game(operator exit)"] and rig.h.runs == {}
    assert se_names() == ["nav_aa.json", "nav_guard_aa.json", "other.json"] and not H.nav_marker().exists()
    rig.h.abort()  # nothing open: a no-op
    assert rig.calls == ["kill_agent", "scenario_undo", "end_run_game(operator exit)"]


def test_a_bench_start_kills_a_leftover_agent_before_the_game_comes_up(rig: Rig, tmp_path: Path):
    # live7: the leftover claude drove the next game (the app session) for 19 min
    from bg3harness.runner import agent_group as A  # noqa: N812
    from test_agent_group import dead_runner_leftover, gone

    leader, child = dead_runner_leftover(tmp_path, renv.RUNTIME / A.FILE)
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert not A.alive(leader) and gone(child) and not (renv.RUNTIME / A.FILE).exists()
    assert rig.calls[:2] == ["snapshot", "ensure_game(True)"]


def test_record_tags_the_runs_failures_and_an_ending_that_was_ours(rig: Rig):
    # H2: a session-path run was valid whatever the harness did, except a crash/hang (run_session's invalid)
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    res = rstate.SessionResult("S001", "c-1", None, "mcp_failed", 1000.0, 1030.0, detail="bg3 serves 12 tools")
    rec = rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), SessionOutcome(res, 1000.0))
    assert (rec["outcome"], rec["harness_failures"], rec["agent_failures"], rec["unknown_failures"]) == (
        "gave_up",  # not a time-out: the run ended on the harness's failure
        2,
        0,
        0,
    )
    assert [(f["kind"], f["class"], f["detail"]) for f in rec["failures"]] == [
        ("mcp_failed", "harness", "bg3 serves 12 tools"),
        ("gave_up", "harness", "bg3 serves 12 tools"),
    ]
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove-2"))
    rec = rig.h.record(JOB, SAVE_SC, run_dir("m-grove-2"), outcome())
    assert (rec["harness_failures"], [f["kind"] for f in rec["failures"]]) == (0, ["timeout"])


def test_bench_hooks_turn_a_play_process_that_cant_start_into_a_setup_failed_result():
    # H3: Popen's OSError escaped run_session and the matrix with the game up
    events: list[tuple[str, dict[str, Any]]] = []

    class Inner:
        raises: Exception = FileNotFoundError(2, "No such file or directory", "claude")

        def run_agent(self, *a: Any) -> Any:
            raise self.raises

        def now(self) -> float:
            return 1234.0

    def event(kind: str, **fields: Any) -> None:
        events.append((kind, fields))

    inner = Inner()
    hooks = H.BenchHooks(inner, event)  # pyright: ignore[reportArgumentType]
    res = hooks.run_agent("KICKOFF", 30, "c-1", False, "")
    assert (res.reason, res.exit_code, res.started, res.ended) == ("setup_failed", None, 1234.0, 1234.0)
    assert res.detail.startswith("FileNotFoundError: ") and events == [
        ("setup_failed", {"session": "S001", "error": res.detail})
    ]
    assert hooks.now() == 1234.0  # the rest is the runner's
    inner.raises = ValueError("a bug")
    with pytest.raises(ValueError, match="a bug"):  # not a setup failure: it still ends the matrix (abort)
        hooks.run_agent("KICKOFF", 30, "c-1", False, "")


def play_session(rig: Rig, sc: dict[str, Any], name: str) -> tuple[FakeRunner, SessionOutcome]:
    """The run's one session as bg3bench's SessionRun plays it (run_session, run_hours, no fault resume)."""
    st = rig.h.start(JOB, sc, run_dir(name))
    spec = SessionSpec(name, st.prompt, sc["caps"]["run_hours"] * 60, st.client_session, Recovery(max_resumes=0))
    return rig.runners[-1], run_session(spec, st.hooks)


def test_an_end_turn_before_the_milestone_is_resumed_until_it_is_reached(rig: Rig):
    # fixlive D4: the agent ended its turn at the Nautiloid's loading screen (7.9 of 240 min, no nautiloid_escaped
    # yet) and the run ended there, outcome timeout; the runner's stop check was the playthrough memory's (None)
    sc = {**UNTIL_SC, "caps": {**UNTIL_SC["caps"], "run_hours": 4}}

    def play(r: FakeRunner, tag: str) -> tuple[str, float]:
        if tag == "-c2":  # the progress poller sees the milestone: the runner stops the agent
            r.until_hit = {"id": "nautiloid_escaped", "label": "Nautiloid escaped", "t": r.clock + 30}
            return "milestone", 30.0
        return "exited", 8 * 60.0 if not tag else 60.0

    rig.prepare = lambda r: setattr(r, "play", play)
    r, out = play_session(rig, sc, "m-naut")
    assert [t for t, _, _ in r.plays] == ["", "-c1", "-c2"] and out.stop_resumes == 2
    assert 'the grader hasn\'t seen its goal {"milestone": "nautiloid_escaped"} yet' in r.plays[1][1]
    assert r.plays[1][2] == pytest.approx(232.0)  # the run's time left
    assert rig.h.record(JOB, sc, run_dir("m-naut"), out)["outcome"] == "reached"


@pytest.mark.parametrize("ends", ["cap", "operator", "verdict", "time"])
def test_no_resume_once_the_run_is_decided_capped_stopped_or_out_of_time(rig: Rig, ends: str):
    def play(r: FakeRunner, tag: str) -> tuple[str, float]:
        if ends == "cap":
            r.cap_hit = {"cap": "cost"}
        elif ends == "operator":
            r.stop_requested = True
        elif ends == "verdict":  # GoalWatch decided just as the agent ended its turn
            r.until_hit = {"id": H.GOAL_HIT, "verdict": "failed"}
        return "exited", 31 * 60.0 if ends == "time" else 60.0  # past the 30 min run_hours, inside the grace

    rig.prepare = lambda r: setattr(r, "play", play)
    r, out = play_session(rig, SAVE_SC, "m-grove")
    assert [t for t, _, _ in r.plays] == [""] and out.stop_resumes == 0


def test_the_stop_check_reads_the_game_now_and_a_wipe_there_is_not_resumed(rig: Rig):
    # B3 job 10: the agent's wait_for saw the Game Over box at 16:23:40 and it ended its turn; the facts poll (every
    # story.POLL_S) hadn't written the last downed yet, so the verdict was still None and the runner resumed it at
    # 16:23:45 ("The run isn't decided"); GoalWatch's stop came at 16:24:07
    said: list[str | None] = [None]
    rig.prepare = lambda r: setattr(r, "read_now", lambda: said.__setitem__(0, "wiped"))
    st = rig.h.start({**JOB, "verdict": lambda: said[0]}, SAVE_SC, run_dir("w"))
    r = rig.runners[-1]
    assert rig.h.watches[run_dir("w")].check() is False  # the watch's last poll: undecided
    spec = SessionSpec("w", st.prompt, 30.0, st.client_session, Recovery(max_resumes=0))
    out = run_session(spec, st.hooks)
    assert [t for t, _, _ in r.plays] == [""] and out.stop_resumes == 0 and "facts_now" in rig.calls
    assert r.until_hit is not None and r.until_hit["verdict"] == "wiped"
    assert rig.h.record(JOB, SAVE_SC, run_dir("w"), out)["outcome"] == "failed"


def test_a_failed_read_now_still_resumes_an_undecided_run(rig: Rig):
    def read_now() -> None:
        raise RuntimeError("the debug port didn't answer")

    rig.prepare = lambda r: setattr(r, "read_now", read_now)
    st = rig.h.start({**JOB, "verdict": lambda: None}, SAVE_SC, run_dir("u"))
    assert "The run isn't decided" in str(st.hooks.stop_check(1e12))  # pyright: ignore[reportAttributeAccessIssue]


def test_an_agent_that_keeps_ending_its_turn_is_recorded_as_an_early_exit_not_a_time_out(rig: Rig):
    # D4: failures said "the run's time ran out before the milestone" at 8 of 240 min
    sc = {**UNTIL_SC, "caps": {**UNTIL_SC["caps"], "run_hours": 4}}
    r, out = play_session(rig, sc, "m-naut")
    assert len(r.plays) == 1 + Recovery().max_stop_resumes and out.result.reason == "exited"
    rec = rig.h.record(JOB, sc, run_dir("m-naut"), out)
    assert (rec["outcome"], [(f["kind"], f["class"], f["detail"]) for f in rec["failures"]]) == (
        "timeout",
        [("timeout", "agent", "S001: the agent ended its turn with 233.0 min of the run left, before the goal")],
    )


MAX_SC: dict[str, Any] = {k: v for k, v in SAVE_SC.items() if k != "goal"} | {
    "name": "act1_gold_max",
    "maximise": {"fact": "gold"},
    "caps": {**SAVE_SC["caps"], "run_hours": 0.75},
}


def test_the_kickoff_of_a_maximise_scenario_says_it_plays_its_whole_cap_and_how_it_is_scored():
    text = H.scenario_kickoff(MAX_SC, "S001", "SCN-act1_gold_1000")
    assert "The run has no pass: it plays all 45 minutes of its time cap, then it is scored." in text
    assert "A party wipe (the Game Over screen) ends it sooner." in text  # graders.measure stops there
    assert "the party's gold when the run ends minus its gold at the start save" in text
    assert "as soon as the task is decided" not in text and "exact goal" not in text


def test_a_maximise_run_resumes_an_agent_that_ends_its_turn_until_the_time_cap(rig: Rig):
    # a maximise goal is never reached: the stop check must not read "no goal yet" as "may end" or as decided
    def play(r: FakeRunner, tag: str) -> tuple[str, float]:
        return "exited", 10 * 60.0

    rig.prepare = lambda r: setattr(r, "play", play)
    r, out = play_session(rig, MAX_SC, "m-gold")
    assert [t for t, _, _ in r.plays] == ["", "-c1", "-c2", "-c3", "-c4"] and out.stop_resumes == 4
    assert "it has no pass, it scores the party's gold gained by its time cap" in r.plays[1][1]
    assert [round(m) for _, _, m in r.plays[1:]] == [35, 25, 15, 5]  # each resume gets the time left
