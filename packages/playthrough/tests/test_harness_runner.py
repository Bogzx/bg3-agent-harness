"""adapters/harness_runner.py: whole playthroughs through the Supervisor and bg3harness run_session, with a fake
agent, game and memory on a fake clock."""

import json
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from bg3harness import client_setup
from bg3harness.game.host import AppHost
from bg3harness.game.io import Save
from bg3harness.session import Recovery
from bg3play import config
from bg3play.adapters import harness_runner as hr
from bg3play.adapters.agent_process import AgentCall, AgentRun
from bg3play.config import PlaythroughConfig
from bg3play.memory.new_playthrough import create_memory
from bg3play.runner import prompts
from bg3play.supervisor import CRASH_NOTE, SessionRequest, SessionResult, Supervisor, SupervisorError

PT = "ilse"


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeAgent:
    """Each process takes `minutes` of the fake clock and ends with the next scripted reason (default exited 0)."""

    model: str = "opus"
    effort: str | None = None
    client_name: str = "claude"

    def __init__(self, clock: Clock, script: list[tuple[str, float]], during: Callable[[int], None] | None = None):
        self.clock, self.script, self.during = clock, list(script), during
        self.calls: list[AgentCall] = []
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1

    def run(self, call: AgentCall) -> AgentRun:
        self.calls.append(call)
        t0 = self.clock.t
        if self.during:
            self.during(len(self.calls))
        reason, minutes = self.script.pop(0) if self.script else ("exited", 30.0)
        self.clock.t += minutes * 60
        return AgentRun(reason, 0, t0, self.clock.t, call.conversation, self.clock.t if reason != "exited" else None,
                        0.5)  # fmt: skip


class FakeGame:
    """`ooms`: each death's OOM cause in turn (None = not an OOM); recover() brings the game back up."""

    def __init__(
        self,
        sigs: list[dict[str, Any] | None],
        recover_to: str | None = None,
        ok: bool = True,
        ooms: Sequence[str | None] = (),
    ):
        self.sigs, self.recover_to, self.is_ok, self.ooms = list(sigs), recover_to, ok, list(ooms)
        self.recovered: list[str] = []
        self.began: list[tuple[Path, Path]] = []
        self.pending_notes: list[str] = []  # the controller's RUNNER NOTEs, taken by notes()

    def begin(self, sdir: Path, mem: Path) -> None:
        self.began.append((sdir, mem))

    def ok(self) -> bool:
        return self.is_ok

    def oom(self) -> str | None:
        return self.ooms.pop(0) if self.ooms else None

    def progress(self) -> dict[str, Any] | None:
        return self.sigs.pop(0) if self.sigs else {"level": "TUT"}

    def recover(self, why: str) -> str | None:
        self.recovered.append(why)
        if self.recover_to is None:
            raise RuntimeError("can't relaunch")
        self.is_ok = True
        return self.recover_to

    def notes(self) -> list[str]:
        out, self.pending_notes = self.pending_notes, []
        return out


class FakeMemory:
    def __init__(self, blocks: list[str] | None = None):
        self.blocks = list(blocks or [])
        self.log: list[tuple[str, dict[str, str]]] = []
        self.n = 0

    def start(self, env: dict[str, str]) -> tuple[str | None, str]:
        self.n += 1
        self.log.append(("start", dict(env)))
        return f"S{self.n:03d}", f"BRIEFING {self.n}"

    def stop_check(self, env: dict[str, str]) -> str | None:
        self.log.append(("stop", dict(env)))
        return self.blocks.pop(0) if self.blocks else None

    def end(self, env: dict[str, str]) -> None:
        self.log.append(("end", dict(env)))


class FakeKeeper:
    def __init__(self):
        self.ran: list[str] = []

    def run_keeper(self, pt: str, result: SessionResult) -> None:
        self.ran.append(result.session)


def make(
    tmp_path: Path,
    agent_script: Sequence[tuple[str, float]] = (),
    sigs: Sequence[dict[str, Any] | None] = (),
    blocks: Sequence[str] = (),
    hours: float = 1.0,
    recover_to: str | None = None,
    during: Callable[[int], None] | None = None,
    ooms: Sequence[str | None] = (),
    **cfg_kw: Any,
) -> tuple[Supervisor, FakeAgent, FakeGame, FakeMemory, FakeKeeper, Path]:
    clock = Clock()
    pts, runtime = tmp_path / "playthroughs", tmp_path / "runtime"
    (pts / PT).mkdir(parents=True)
    cfg = config.validate({"v": 1, "name": PT, "session": {"minutes": 30.0, "hours": hours, **cfg_kw}})
    config.save(pts / PT, cfg)
    agent, game, memory = (
        FakeAgent(clock, list(agent_script), during),
        FakeGame(list(sigs), recover_to, ooms=ooms),
        FakeMemory(list(blocks)),
    )
    runner = hr.HarnessRunner(runtime, pts, lambda c, d: agent, game, lambda mem: memory, clock)
    keeper = FakeKeeper()
    sup = Supervisor(runner, keeper, runtime, pts, clock)
    return sup, agent, game, memory, keeper, runtime


def test_two_sessions_with_the_keeper_between_and_the_files_the_store_reads(tmp_path: Path):
    sup, agent, _, memory, keeper, runtime = make(tmp_path, sigs=[{"level": "TUT"}, {"level": "WLD"}] * 2)
    sup.start(PT, wait=True)
    assert sup.detail == "hours budget spent" and keeper.ran == ["S001", "S002"]
    hist = sup.history(PT)
    assert [(h["session"], h["outcome"], h["progress"], h["cost_usd"]) for h in hist] == [
        ("S001", "ok", True, 0.5),
        ("S002", "ok", True, 0.5),
    ]
    first = agent.calls[0]
    # the runner's kickoff (bg3play.runner.prompts): the memory briefing first, no deadline, coaching's pace
    assert first.prompt.startswith("BRIEFING 1\n\nThis is session S001 of the BG3 playthrough 'ilse'")
    assert "WRAP UP NOW" in first.prompt and "PACE PRE-FLIGHT" in first.prompt and "30 min" not in first.prompt
    assert not first.resume and first.cap_s == 30 * 60 + Recovery().hard_stop_grace_s and first.max_cost_usd == 0
    # the condition and the clock reach the agent and the bg3 server; the memory's sid names the session
    assert first.env["BG3AI_PROMPT_CONDITION"] == "rules+coaching" and first.env["BG3AI_SESSION"] == "S001"
    assert first.env["BG3AI_SESSION_DEADLINE"] == str(1000 + 30 * 60) and first.env["BG3_WRAP_MINUTES"] == "27.0"
    assert [k for k, _ in memory.log] == ["start", "stop", "end", "start", "stop", "end"]
    rj = json.loads((runtime / "sessions" / PT / "runner.json").read_text())["history"]
    assert [(h["kind"], h["session"], h["reason"], h["progress"], h["condition"]) for h in rj] == [
        ("play", "S001", "exited", True, "rules+coaching"),
        ("play", "S002", "exited", True, "rules+coaching"),
    ]
    status = json.loads((runtime / "runner.status.json").read_text())
    assert status["playthrough"] == PT and status["session"] == "S002" and status["session_ended"] == 1000 + 3600


def test_the_stuck_nudge_reaches_the_kickoff(tmp_path: Path):
    # S001 and S002 end where they started (only the position moved): S003's kickoff carries the nudge
    same = [{"level": "TUT", "pos": [1, 2, 3]}, {"level": "TUT", "pos": [9, 9, 9]}]
    sup, agent, *_ = make(tmp_path, sigs=same * 3, hours=1.5)
    sup.start(PT, wait=True)
    assert [h["progress"] for h in sup.history(PT)] == [False, False, False]
    assert ["CHANGE APPROACH" in c.prompt for c in agent.calls] == [False, False, True]


def test_the_stop_check_resumes_the_same_conversation(tmp_path: Path):
    sup, agent, *_ = make(tmp_path, [("exited", 10), ("exited", 20)], blocks=["Write HANDOFF.md first."], hours=0.5)
    sup.start(PT, wait=True)
    a, b = agent.calls
    assert b.resume and b.conversation == a.conversation and b.prompt == "Write HANDOFF.md first."
    assert b.cap_s == 20 * 60 + Recovery().hard_stop_grace_s  # the time left
    (h,) = sup.history(PT)
    assert h["outcome"] == "ok"
    rj = json.loads((tmp_path / "runtime" / "sessions" / PT / "runner.json").read_text())["history"]
    assert rj[0]["stop_resumes"] == 1


def test_a_game_hang_the_game_recovers_from_resumes_the_session(tmp_path: Path):
    sup, agent, game, *_ = make(tmp_path, [("hang", 5), ("exited", 25)], hours=0.5, recover_to="S001-end")
    sup.start(PT, wait=True)
    assert game.recovered == ["stopped responding"]
    a, b = agent.calls
    assert b.resume and b.conversation == a.conversation and "recovered (S001-end)" in b.prompt
    (h,) = sup.history(PT)
    assert (h["outcome"], h["progress"]) == ("ok", False)


def test_faults_and_recoveries_reach_the_dashboards_event_log(tmp_path: Path):
    # live7 D.4b: the app wrote no runner_events.jsonl, so the dashboard counted no crash or recovery of an app run
    from bg3play.live import unattended_payload

    sup, *_, runtime = make(tmp_path, [("hang", 5), ("exited", 25), ("crash", 5)], hours=1.0, recover_to="S001-end")
    sup.start(PT, wait=True)
    sdir = runtime / "sessions" / PT
    evs = [json.loads(ln) for ln in (sdir / "runner_events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["fault"], e["session"]) for e in evs] == [
        ("fault", "hang", "S001"),
        ("recovered", "hang", "S001"),
        ("fault", "crash", "S002"),
        ("recovered", "crash", "S002"),
    ]
    u = unattended_payload(sdir, {})
    assert u is not None and u["faults"]["hang"] == 1 and u["faults"]["crash"] == 1 and u["recovered"] == 2


def test_a_fault_the_app_cant_recover_ends_the_session_and_the_next_gets_the_crash_note(tmp_path: Path):
    sup, agent, game, *_ = make(tmp_path, [("crash", 5)], hours=0.5)
    sup.start(PT, wait=True)
    assert game.recovered == ["crashed"] and len(agent.calls) == 2
    assert [(h["outcome"], h["progress"]) for h in sup.history(PT)] == [("crash", None), ("ok", False)]
    note = CRASH_NOTE.format(why="reason=crash exit=0")
    assert note not in agent.calls[0].prompt and note in agent.calls[1].prompt


def test_a_soft_stop_plays_the_session_out_and_resumes_nothing_after_a_fault(tmp_path: Path):
    def during(n: int) -> None:
        sup.stop()  # the user's stop while S001 plays

    sup, agent, game, *_ = make(tmp_path, [("hang", 5)], hours=2.0, recover_to="S001-end", during=during)
    sup.start(PT, wait=True)
    assert game.recovered == ["stopped responding"] and len(agent.calls) == 1  # recovered, not resumed
    assert [h["outcome"] for h in sup.history(PT)] == ["crash"] and sup.state == "idle"


def test_a_soft_stop_ends_an_ok_session_as_stopped(tmp_path: Path):
    def during(n: int) -> None:
        sup.stop()

    sup, agent, *_ = make(tmp_path, hours=2.0, during=during)
    sup.start(PT, wait=True)
    assert [h["outcome"] for h in sup.history(PT)] == ["stopped"] and len(agent.calls) == 1


def test_the_cost_cap_is_what_the_session_has_left(tmp_path: Path):
    sup, agent, *_ = make(tmp_path, [("exited", 10), ("exited", 20)], blocks=["Keep playing."], hours=0.5,
                          max_cost_usd=2.0)  # fmt: skip
    sup.start(PT, wait=True)
    assert [c.max_cost_usd for c in agent.calls] == [2.0, 1.5]
    assert sup.history(PT)[0]["cost_usd"] == 1.0


def test_no_game_or_an_unplayable_config_is_a_session_error(tmp_path: Path):
    sup, agent, game, *_ = make(tmp_path)
    game.is_ok = False
    sup.start(PT, wait=True)
    assert sup.detail.startswith("session error: the game doesn't answer") and agent.calls == []

    runtime = tmp_path / "runtime"

    def refuse(cfg: PlaythroughConfig, sdir: Path) -> hr.Agent:
        raise SupervisorError("client pi: pick a pi client profile")

    runner = hr.HarnessRunner(runtime, tmp_path / "playthroughs", refuse, FakeGame([]), lambda m: FakeMemory(), Clock())
    cfg = config.load(tmp_path / "playthroughs" / PT)
    res = runner.run_session(SessionRequest(PT, 2, cfg, resume=True), threading.Event())
    assert (res.outcome, res.detail) == ("error", "client pi: pick a pi client profile")


def test_a_fight_hold_moves_the_session_end_and_the_stop_resume_gets_the_held_time(tmp_path: Path):
    sdir = tmp_path / "runtime" / "sessions" / PT

    def during(n: int) -> None:  # the bg3 MCP server: a fight was on at WRAP UP, the clock held 10 min
        if n == 1:
            (sdir / "S001.hold.json").write_text(json.dumps({"deadline": 1000 + 40 * 60}))

    sup, agent, *_ = make(tmp_path, [("exited", 30), ("exited", 10)], blocks=["Finish the fight first."],
                          hours=0.5, during=during)  # fmt: skip
    sup.start(PT, wait=True)
    a, b = agent.calls
    assert a.env["BG3AI_FIGHT_HOLD_PATH"] == str(sdir / "S001.hold.json")  # the MCP server and session.py hold
    assert b.resume and b.cap_s == 10 * 60 + Recovery().hard_stop_grace_s  # the held 10 min, not 0


def test_two_oom_deaths_in_a_session_end_the_run_without_a_second_relaunch(tmp_path: Path):
    sup, agent, game, *_ = make(tmp_path, [("crash", 5), ("crash", 5)], hours=5.0, recover_to="S001-end",
                                ooms=["oom A", "oom B"])  # fmt: skip
    sup.start(PT, wait=True)
    assert game.recovered == ["crashed"] and len(agent.calls) == 2  # relaunched after the 1st, not the 2nd
    (h,) = sup.history(PT)
    assert h["outcome"] == "crash" and h["game_deaths"] == ["oom A", "oom B"]
    assert sup.detail == "the game ran out of memory 2 times in a row (last: oom B); not relaunching it"


def test_the_oom_streak_spans_sessions_and_a_death_by_something_else_breaks_it(tmp_path: Path):
    # S001: an OOM, then a crash that wasn't one (both resumed). S002: an OOM, recovered too late to resume.
    # S003: another OOM: 2 in a row (S001's OOM doesn't count), the run ends without relaunching.
    script = [("crash", 5), ("crash", 5), ("exited", 20), ("crash", 25), ("crash", 5)]
    sup, _, game, *_ = make(tmp_path, script, hours=5.0, recover_to="S-end", ooms=["oom 1", None, "oom 3", "oom 4"])
    sup.start(PT, wait=True)
    hist = sup.history(PT)
    assert [(h["outcome"], h["game_deaths"]) for h in hist] == [
        ("ok", ["oom 1", ""]),
        ("crash", ["oom 3"]),
        ("crash", ["oom 4"]),
    ]
    assert len(game.recovered) == 3 and sup.detail.startswith("the game ran out of memory 2 times in a row (last: oom")


def test_a_game_down_at_the_session_start_is_recovered_first(tmp_path: Path):
    sup, agent, game, *_ = make(tmp_path, hours=0.5, recover_to="S000-end")
    game.is_ok = False
    game.pending_notes = ["RUNNER NOTE: the newest save crashed on load"]
    sup.start(PT, wait=True)
    assert game.recovered == ["wasn't answering at the session start"] and len(agent.calls) == 1
    assert "RUNNER NOTE: the newest save crashed on load" in agent.calls[0].prompt
    assert game.began == [(tmp_path / "runtime" / "sessions" / PT, tmp_path / "playthroughs" / PT / "memory")]
    (h,) = sup.history(PT)
    assert h["outcome"] == "ok" and h["game_deaths"] == [""]


def test_the_supervisors_detail_says_launching_and_relaunching_not_playing(tmp_path: Path):
    # live7 D.4b: /api/v1/supervisor said "playing S001" for the 2.5 minutes the app was launching the game
    sup, _, game, *_ = make(tmp_path, [("crash", 5), ("exited", 20)], hours=0.4, recover_to="S000-end")
    game.is_ok = False
    details: list[str] = []
    sup.on_change = lambda view: details.append(view["detail"])
    sup.start(PT, wait=True)
    assert details == [
        "",
        "playing",
        "launching the game",
        "playing",
        "relaunching the game (it crashed)",
        "playing",
        "memory keeper",
        "hours budget spent",
    ]


class FakeController:
    """bg3harness's GameController over the real AppHost: the game dies when the agent crashes; launch_and_load
    falls back to the older save with the controller's note (as when the newest crashed on load)."""

    def __init__(self, host: AppHost, older: Save):
        self.host, self.older, self.is_alive = host, older, True
        self.calls: list[str] = []

    def alive(self) -> bool:
        return self.is_alive

    def kill(self) -> None:
        self.calls.append("kill")

    def launch_and_load(self, save: Save) -> Save:
        self.calls.append(f"launch {save.folder} ({self.host.state['save_owner']})")
        self.host.note(f"RUNNER NOTE: {save.label} crashed on load; loaded {self.older.label}")
        self.is_alive = True
        return self.older

    def check_oom(self) -> str | None:
        return None


def test_a_crash_relaunches_through_the_controller_and_the_session_resumes(tmp_path: Path):
    from bg3play.adapters.launched_game import LaunchedGame

    clock, pts, runtime = Clock(), tmp_path / "playthroughs", tmp_path / "runtime"
    (pts / PT).mkdir(parents=True)
    config.save(pts / PT, config.validate({"v": 1, "name": PT, "session": {"minutes": 30.0, "hours": 0.5}}))
    older, newest = (
        Save("Ilse-1__S001-start", "Ilse", "S001-start", 1100),
        Save("Ilse-2__S001-x", "Ilse", "S001-x", 1200),
    )
    host = AppHost(runtime, tmp_path, tmp_path / "saves", saves=lambda: [older, newest])
    ctl = FakeController(host, older)
    reads = FakeGame([])
    game = LaunchedGame(reads, ctl, host, lambda: [older, newest], lambda: "Ilse")

    def during(n: int) -> None:  # the first process: the game dies under it
        if n == 1:
            ctl.is_alive = False

    agent = FakeAgent(clock, [("crash", 5), ("exited", 25)], during)
    runner = hr.HarnessRunner(runtime, pts, lambda c, d: agent, game, lambda mem: FakeMemory(), clock)
    sup = Supervisor(runner, FakeKeeper(), runtime, pts, clock)
    sup.start(PT, wait=True)
    sdir = runtime / "sessions" / PT
    assert host.sdir == sdir and ctl.calls == ["kill", "launch Ilse-2__S001-x (Ilse)"]
    a, b = agent.calls
    assert b.resume and b.conversation == a.conversation and "recovered (S001-start)" in b.prompt
    assert "RUNNER NOTE: S001-x crashed on load; loaded S001-start" in b.prompt and host.notes == []
    rj = json.loads((sdir / "runner.json").read_text())
    assert rj["loaded_save"] == older.folder and rj["save_owner"] == "Ilse"
    (h,) = sup.history(PT)
    assert h["outcome"] == "ok" and h["game_deaths"] == [""]


def test_the_kickoff_is_the_runners():
    cfg = config.validate({"v": 1, "name": PT, "character": {"brief": "a dwarf cleric", "roleplay": "light"}})
    req = SessionRequest(PT, 3, cfg, True, notes=("Load S002-end.",))
    text = hr.kickoff(req, "S003", "", "S002-end")
    assert text == prompts.build_kickoff(
        "S003", PT, "S002-end", ["Load S002-end.", "Your character: a dwarf cleric (roleplay: light)."],
        pace=client_setup.kickoff_pace("claude", {"BG3AI_PROMPT_CONDITION": "rules+coaching"}),
    )  # fmt: skip
    assert "First call session_start" in text and 'already loaded save "S002-end"' in text
    rules = config.validate({"v": 1, "name": PT, "agent": {"condition": "rules"}})
    assert "PACE PRE-FLIGHT" not in hr.kickoff(SessionRequest(PT, 1, rules, False), "S001", "BRIEFING")


def test_the_keeper_runs_for_every_client(tmp_path: Path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "codex.toml").write_text('client = "codex"\nmodel = "gpt-5.5"\n')
    agents = hr.ClientAgents(profiles, tmp_path, Clock())
    base = config.validate({"v": 1, "name": PT})
    assert agents.keeper_client(base) == ("claude", None)
    assert agents.keeper_client(config.validate({"v": 1, "name": PT, "agent": {"profile": "codex"}})) == ("codex", None)
    assert agents.keeper_client(config.validate({"v": 1, "name": PT, "keeper": {"enabled": False}})) is None


def test_client_agents_refuse_pi_without_a_profile_or_a_cap(tmp_path: Path):
    agents = hr.ClientAgents(tmp_path, tmp_path, Clock(), system_text=lambda p, c: p)
    with pytest.raises(SupervisorError, match="pick a pi client profile"):
        agents(config.validate({"v": 1, "name": PT, "agent": {"client": "pi"}}), tmp_path)
    claude = agents(config.validate({"v": 1, "name": PT}), tmp_path)
    assert claude.client.name == "claude" and claude.model == "opus"


def test_session_py_opens_checks_and_ends_the_memory_session(tmp_path: Path):
    mem = tmp_path / PT / "memory"
    create_memory(PT, mem)
    sp = hr.SessionPy(mem)
    sid, briefing = sp.start({})
    assert sid == "S001" and briefing and (mem / "sessions" / "S001.md").is_file()
    assert sp.stop_check({}) is None  # no deadline: a manual session may pause
    sp.end({})  # no wrap-up: left open, with a note in the session log
    assert "ended before wrap-up" in (mem / "sessions" / "S001.md").read_text()


def test_abort_kills_the_agent_and_the_session_ends_stopped_with_its_files(tmp_path: Path):
    # B3: the server shutting down mid-session: the agent is killed, run_session resumes nothing, memory end runs,
    # runner.json gets the play row and runner.status.json session_ended
    sup, agent, _, memory, keeper, runtime = make(tmp_path, [("aborted", 5)], blocks=["Keep playing."], hours=0)
    sup.abort = sup.runner.abort  # type: ignore[attr-defined]
    agent.during = lambda n: sup.shutdown()  # pyright: ignore[reportUnknownLambdaType]
    sup.start(PT, wait=True)
    assert agent.kills == 1 and len(agent.calls) == 1
    assert [h["outcome"] for h in sup.history(PT)] == ["stopped"] and keeper.ran == []
    assert [k for k, _ in memory.log] == ["start", "end"]
    assert json.loads((runtime / "sessions" / PT / "runner.json").read_text())["history"][0]["reason"] == "aborted"
    assert json.loads((runtime / "runner.status.json").read_text())["session_ended"] == 1300.0


class PaidAgent(FakeAgent):
    client_name = "pi"


def test_a_paid_session_is_capped_by_the_global_budget_and_charges_it(tmp_path: Path):
    # B4: budget.json cap 1.0 spent 0.9 -> the session's 5 USD cap is cut to 0.1; each process's cost is added
    sup, agent, *_, runtime = make(tmp_path, [("exited", 10), ("exited", 20)], blocks=["Keep playing."], hours=0.5,
                                   max_cost_usd=5.0)  # fmt: skip
    paid = PaidAgent(agent.clock, list(agent.script))
    sup.runner.agent_for = lambda c, d: paid  # type: ignore[attr-defined]
    runtime.mkdir()
    (runtime / "budget.json").write_text('{"cap": 1.0, "spent": 0.9}')
    sup.start(PT, wait=True)
    assert [c.max_cost_usd for c in paid.calls] == [pytest.approx(0.1), 1e-9]  # the 2nd: nothing left of the cap
    budget = json.loads((runtime / "budget.json").read_text())
    assert budget["spent"] == 1.9 and budget["last"] == {"usd": 0.5, "playthrough": PT, "session": "S001",
                                                         "role": "play"}  # fmt: skip
    # used up: the next session is refused before it starts, and the refusal is what the UI shows
    sup.resume(PT, wait=True)
    assert sup.detail.startswith("session error: the global budget is used up") and len(paid.calls) == 2
    assert sup.sessions_played(PT) == 1


def test_a_paid_session_without_a_budget_is_refused(tmp_path: Path):
    sup, agent, *_ = make(tmp_path, max_cost_usd=5.0)
    sup.runner.agent_for = lambda c, d: PaidAgent(agent.clock, [])  # type: ignore[attr-defined]
    sup.start(PT, wait=True)
    assert "needs a global budget, runtime/budget.json" in sup.detail and sup.sessions_played(PT) == 0


def test_session_py_timeouts_are_no_answer_and_the_session_plays_on(tmp_path: Path):
    # B5: session.py hanging past its timeout neither opens, blocks nor ends the session; the run goes on
    def hang(args: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    sup, *_ = make(tmp_path, hours=1.0)
    sup.runner.memory_for = lambda mem: hr.SessionPy(mem, run=hang)  # type: ignore[attr-defined]
    sup.start(PT, wait=True)
    assert [(h["session"], h["outcome"]) for h in sup.history(PT)] == [("S001", "ok"), ("S002", "ok")]
    assert sup.detail == "hours budget spent"
