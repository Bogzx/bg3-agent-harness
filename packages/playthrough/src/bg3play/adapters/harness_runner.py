"""The Supervisor's SessionRunner over the harness's run_session (bg3harness.session).

One play session:
    memory.start                       opens memory/sessions/Sxxx.md (session.py start): the sid and the briefing
    start_sig = game.progress()
    run_session(SessionSpec(sid, kickoff, minutes, conversation), AppSessionHooks)
        the agent process (ClientAgent), resumed by run_session after a fault (game.recover) or when the stop
        check (memory.stop_check: session.py stop) says the session can't end yet
    memory.end                         session.py session-end: commits a finished wrap-up
    end_sig = game.progress()          progress = the save signature changed (runner/play.py made_progress)
    runner.json + runner.status.json   the files the store reads, as play.py writes them

The kickoff is the runner's (bg3play.runner.prompts.build_kickoff: the memory briefing, the playbooks, WRAP UP, the
pace pre-flight of a coaching condition, the save the game recovered to) with the request's notes (the Supervisor's
stuck directive and crash note, the character). A soft stop (the Supervisor's
stop event) lets the session play out and resumes nothing after a fault. The prompt condition (config [agent]
condition, default rules+coaching for a playthrough) goes to the agent and the bg3 server as BG3AI_PROMPT_CONDITION.
What runs between sessions (the memory keeper) is the Supervisor's Keeper: keeper_client gives it every client.

A session never ends mid-fight (S078): the bg3 MCP server holds the session's clock while a fight that was on at WRAP
UP lasts and writes the moved times to $BG3AI_FIGHT_HOLD_PATH (Sxxx.hold.json, also read by session.py's stop
check); the session's end and hard stop move with it, at most FIGHT_HOLD_S past the planned end
(bg3harness.session.held_deadline).

The game: adapters/launched_game.py LaunchedGame (the server's default) relaunches it through bg3harness's
GameController over AppHost and loads the playthrough's newest save (also when it is down at a session start); the
controller's notes (an older save loaded after the newest crashed) go into the resume prompt or the kickoff.
AttachedGame (`bg3play-serve --attach`) reads a running game over the bridge and can't relaunch it, so a game fault
ends the session. A game death by OOM counts toward the Supervisor's OOM streak: at OOM_STREAK in a row the game is
not relaunched and the run ends. `--session-runner legacy` (adapters/legacy_cli.py) runs play.py, which launches it.

Spend: a paid client (bg3harness.spend: pi) plays only with the global budget runtime/budget.json, as play.py does:
the session is refused without it or once it is used up, its cost cap ([session] max_cost_usd) is cut to what the
budget has left, and each agent process's cost is added to it when the process ends. `abort` (the server shutting
down) kills the running agent; the session ends "stopped" with its memory ended and its files written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bg3harness import client_setup, spend
from bg3harness.runner import policy
from bg3harness.session import (
    RecoverFailure,
    Recovery,
    SessionOutcome,
    SessionSpec,
    fight_hold_path,
    held_deadline,
    run_session,
)

from .. import config
from ..jsontypes import is_obj
from ..memory import memlib
from ..runner import memory_session, prompts
from ..supervisor import OOM_STREAK, SessionRequest, SessionResult, SupervisorError
from .agent_process import AgentCall, AgentRun, ClientAgent


class Agent(Protocol):
    model: str  # what plays (a client profile's model, else the config's)
    effort: str | None

    @property
    def client_name(self) -> str: ...  # the client CLI (the kickoff's pace pre-flight names its tools)

    def run(self, call: AgentCall) -> AgentRun: ...
    def kill(self) -> None: ...  # ends the running process now and any later one


class Game(Protocol):
    def begin(self, sdir: Path, mem: Path) -> None: ...  # the session's playthrough (its runner.json dir, memory)
    def ok(self) -> bool: ...  # alive and answering
    def progress(self) -> dict[str, Any] | None: ...  # the save signature (bg3harness.game.io.progress_signature)
    def oom(self) -> str | None: ...  # the dead game's OOM cause (None: alive, or it died otherwise)
    def recover(self, why: str) -> str | None: ...  # relaunch and load the latest save; its name; raises if it can't
    def notes(self) -> list[str]: ...  # RUNNER NOTEs from the recoveries since the last call


class Memory(Protocol):
    def start(self, env: dict[str, str]) -> tuple[str | None, str]: ...  # (the opened sid, the briefing)
    def stop_check(self, env: dict[str, str]) -> str | None: ...  # why the session can't end yet
    def end(self, env: dict[str, str]) -> None: ...


# ------------------------------------------------------------ pure


def session_env(
    req: SessionRequest, sid: str, start: float, runtime: Path, playthroughs: Path, keeper: bool
) -> dict[str, str]:
    """The session's clock (bg3play.runner.memory_session.clock_env, the runner's) and paths, for the agent, the bg3
    server and session.py."""
    m = req.config.session.minutes
    return {
        "BG3AI_PLAYTHROUGH": req.playthrough,
        "BG3AI_SESSION": sid,
        "BG3AI_RUNTIME": str(runtime),
        "BG3AI_PLAYTHROUGHS": str(playthroughs),
        "BG3_MEMORY_DIR": str(playthroughs / req.playthrough / "memory"),
        **memory_session.clock_env(start + m * 60, m, keeper),
        "BG3AI_PROMPT_CONDITION": req.config.agent.condition,
        "BG3AI_AUTOSKIP": "1" if req.config.game.autoskip else "0",  # the bg3 server's movie skipper
    }


def kickoff(
    req: SessionRequest,
    sid: str,
    briefing: str,
    loaded: str | None = None,
    client: str = "claude",
    game_notes: Sequence[str] = (),
) -> str:
    """The runner's kickoff (prompts.build_kickoff) for an app session: the request's notes and the character, the
    save the game was recovered to and the recovery's notes, the pace pre-flight when the condition has coaching."""
    cfg = req.config
    notes = [*req.notes, *game_notes]
    if cfg.character.brief:
        notes.append(f"Your character: {cfg.character.brief} (roleplay: {cfg.character.roleplay}).")
    pace = client_setup.kickoff_pace(client, {"BG3AI_PROMPT_CONDITION": cfg.agent.condition})
    return prompts.build_kickoff(sid, req.playthrough, loaded, notes, briefing.strip(), pace=pace)


def session_cap(client: str, max_cost: float, budget: Path) -> tuple[float, str | None]:
    """(the session's cost cap, 0 = none; why it is refused). A paid client's cap is cut to what the global budget
    has left (bg3harness.spend.run_cap); an unreadable budget refuses."""
    if not spend.is_paid(client):
        return max_cost, None
    try:
        b = spend.read_budget(budget)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        cap, why = spend.run_cap(client, max_cost if max_cost > 0 else None, b)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    except spend.BudgetError as e:
        return 0.0, str(e)
    return cap or 0.0, why


class OomStreakError(RuntimeError):
    """The game ran out of memory OOM_STREAK times in a row: not relaunched (the Supervisor ends the run)."""


def outcome_of(out: SessionOutcome, stopped: bool) -> str:
    """The Supervisor's outcome for a run_session outcome."""
    if out.invalid or out.gave_up or out.result.reason in ("crash", "hang", "agent_hang"):
        return "crash"
    if out.result.reason == "exited" and out.result.exit_code not in (0, None):
        return "error"
    return "stopped" if stopped else "ok"


# ------------------------------------------------------------ the hooks


class AppSessionHooks:
    """bg3harness.session.SessionHooks for one app session."""

    def __init__(
        self,
        agent: Agent,
        game: Game,
        memory: Memory,
        sdir: Path,
        sid: str,
        env: dict[str, str],
        stop: threading.Event,
        clock: Callable[[], float],
        grace_s: float,
        max_cost_usd: float,
        log: Callable[[str], None],
        oom_streak: int = 0,
        budget: Path | None = None,
        aborted: Callable[[], bool] = lambda: False,
    ):
        self.agent, self.game, self.memory, self.sdir, self.sid, self.env = agent, game, memory, sdir, sid, env
        self.stop, self.clock, self.grace_s, self.max_cost_usd, self._log = stop, clock, grace_s, max_cost_usd, log
        self.save: str | None = None
        self.runs: list[AgentRun] = []
        self.oom_streak = oom_streak  # OOM deaths in a row so far (the request's, then this session's)
        self.deaths: list[str] = []
        self.budget = budget  # runtime/budget.json each process's cost is added to (a paid client); None = unpaid
        self._aborted = aborted

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str) -> AgentRun:
        spent = sum(r.cost_usd or 0.0 for r in self.runs)
        call = AgentCall(
            self.sdir, self.sid, prompt, minutes * 60 + self.grace_s, client_session or str(uuid.uuid4()), resume,
            self.env, max(self.max_cost_usd - spent, 1e-9) if self.max_cost_usd > 0 else 0.0,
        )  # fmt: skip
        res = self.agent.run(call)
        self.runs.append(res)
        if self.budget is not None and res.cost_usd:
            try:
                spend.add_spend(  # pyright: ignore[reportUnknownMemberType]
                    self.budget,
                    res.cost_usd,
                    playthrough=self.env.get("BG3AI_PLAYTHROUGH"),
                    session=self.sid,
                    role="play",
                )
            except (OSError, spend.BudgetError) as e:
                self._log(f"{self.sid}: budget update failed ({e})")
        return res

    def session_end(self, planned_end: float) -> float:
        return held_deadline(fight_hold_path(self.sdir, self.sid), planned_end)

    def stop_check(self, session_end: float) -> str | None:
        return self.memory.stop_check(self.env)

    def game_ok(self) -> bool:
        return self.game.ok()

    def recover(self, why: str) -> RecoverFailure | None:
        try:
            self.save = recover_game(self.game, why, self)
        except OomStreakError as e:
            return RecoverFailure(e, fatal=True)
        except Exception as e:  # the session ends; the Supervisor decides on the next
            return RecoverFailure(e, fatal=False)
        return None

    def loaded(self) -> str | None:
        return self.save

    def aborted(self) -> bool:
        return self._aborted()

    def stop_requested(self) -> bool:
        return self.stop.is_set()

    def notes(self) -> list[str]:
        return self.game.notes()

    def resume_prompt(self, why: str, notes: list[str]) -> str:
        line = f"The game {why}; it was recovered ({self.save or 'main menu'}). Continue the session from there."
        return "\n\n".join([line, *notes])

    def agent_resume_prompt(self, notes: list[str]) -> str:
        line = "Your process stopped answering and was restarted; the game is as you left it. Continue the session."
        return "\n\n".join([line, *notes])

    def stop_resume_prompt(self, why: str) -> str:
        return why

    def log(self, msg: str) -> None:
        self._log(msg)

    def event(self, kind: str, **fields: Any) -> None:
        self._log(f"{self.sid}: {kind} {json.dumps(fields, default=str)}")

    def now(self) -> float:
        return self.clock()


def recover_game(game: Game, why: str, hooks: AppSessionHooks) -> str | None:
    """A game death counted (its OOM cause, the streak), then the game recovered; OomStreakError at OOM_STREAK."""
    oom = game.oom()
    hooks.deaths.append(oom or "")
    hooks.oom_streak = hooks.oom_streak + 1 if oom else 0
    if oom:
        hooks.log(f"{hooks.sid}: the game ran out of memory ({oom}), {hooks.oom_streak} in a row")
    if hooks.oom_streak >= OOM_STREAK:
        raise OomStreakError(
            f"the game ran out of memory {hooks.oom_streak} times in a row ({oom}); not relaunching it"
        )
    return game.recover(why)


# ------------------------------------------------------------ the runner


@dataclass
class HarnessRunner:
    runtime: Path
    playthroughs: Path
    agent_for: Callable[[config.PlaythroughConfig, Path], Agent]  # (config, session dir); SupervisorError: can't
    game: Game
    memory_for: Callable[[Path], Memory]  # the playthrough's memory dir -> its session lifecycle
    clock: Callable[[], float]
    recovery: Recovery = field(default_factory=Recovery)
    log: Callable[[str], None] = lambda msg: None
    _abort: threading.Event = field(default_factory=threading.Event, init=False)
    _agent: Agent | None = field(default=None, init=False)

    def abort(self) -> None:
        """The server is going down: the running session's agent is killed and the session ends (no resume)."""
        self._abort.set()
        if (agent := self._agent) is not None:
            agent.kill()

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        cfg, pt = req.config, req.playthrough
        sdir = self.runtime / "sessions" / pt
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            agent = self.agent_for(cfg, sdir)
        except SupervisorError as e:
            return SessionResult("?", "error", detail=str(e))
        budget = self.runtime / "budget.json"
        max_cost, refused = session_cap(agent.client_name, cfg.session.max_cost_usd, budget)
        if refused:
            return SessionResult("?", "error", detail=refused)
        self._agent = agent
        if self._abort.is_set():
            agent.kill()
        try:
            return self._play(req, stop, agent, max_cost, budget if spend.is_paid(agent.client_name) else None)
        finally:
            self._agent = None

    def _play(
        self, req: SessionRequest, stop: threading.Event, agent: Agent, max_cost: float, budget: Path | None
    ) -> SessionResult:
        cfg, pt = req.config, req.playthrough
        sdir = self.runtime / "sessions" / pt
        predicted = f"S{req.session:03d}"
        self.game.begin(sdir, self.playthroughs / pt / "memory")
        hooks = AppSessionHooks(
            agent, self.game, self.memory_for(self.playthroughs / pt / "memory"), sdir, predicted, {}, stop,
            self.clock, self.recovery.hard_stop_grace_s, max_cost, self.log, req.oom_streak, budget,
            self._abort.is_set,
        )  # fmt: skip
        if not self.game.ok():
            try:
                hooks.save = recover_game(self.game, "wasn't answering at the session start", hooks)
            except Exception as e:
                outcome = "crash" if isinstance(e, OomStreakError) else "error"
                return SessionResult("?", outcome, detail=f"the game doesn't answer and wasn't recovered: {e}",
                                     game_deaths=tuple(hooks.deaths))  # fmt: skip
        start = self.clock()
        env = session_env(req, predicted, start, self.runtime, self.playthroughs, cfg.keeper.enabled)
        memory = hooks.memory
        opened, briefing = memory.start(env)
        sid = opened or predicted
        env["BG3AI_SESSION"] = sid
        env["BG3AI_FIGHT_HOLD_PATH"] = str(fight_hold_path(sdir, sid))
        hooks.sid, hooks.env = sid, env
        start_sig = self.game.progress()
        write_status(self.runtime, playthrough=pt, session=sid, model=agent.model, effort=agent.effort,
                     started=start, log=str(sdir / f"{sid}.log"), session_ended=None, updated=start)  # fmt: skip
        prompt = kickoff(req, sid, briefing, hooks.save, agent.client_name, self.game.notes())
        spec = SessionSpec(sid, prompt, cfg.session.minutes, str(uuid.uuid4()), self.recovery)
        try:
            out = run_session(spec, hooks)
        finally:
            memory.end(env)
        progress = (
            None if out.result.reason in ("crash", "hang") else policy.made_progress(start_sig, self.game.progress())  # pyright: ignore[reportUnknownMemberType]
        )
        ended = out.result.ended
        cost = sum(r.cost_usd for r in hooks.runs if r.cost_usd is not None) if hooks.runs else None
        entry = {
            "kind": "play",
            "session": sid,
            "claude_session": out.result.claude_session,
            "reason": out.result.reason,
            "exit": out.result.exit_code,
            "started": out.started,
            "ended": ended,
            "model": agent.model,
            "effort": agent.effort,
            "condition": cfg.agent.condition,
            "minutes": round((ended - out.started) / 60, 1),
            "progress": progress,
            "resumes": out.resumes,
            "stop_resumes": out.stop_resumes,
        }
        append_history(sdir / "runner.json", entry)
        write_status(self.runtime, session_ended=ended, updated=ended)
        detail = f"reason={out.result.reason} exit={out.result.exit_code}"
        if out.invalid or out.gave_up:
            detail += f" ({out.invalid or out.gave_up})"
        outcome = "stopped" if self._abort.is_set() else outcome_of(out, stop.is_set())
        return SessionResult(sid, outcome, progress, out.started, ended, cost, detail, tuple(hooks.deaths))


def write_status(runtime: Path, **fields: Any) -> None:
    """runner.status.json updated with `fields` (runner/play.py write_status)."""
    path = runtime / "runner.status.json"
    try:
        cur: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = None
    cur = (cur if is_obj(cur) else {}) | fields
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    tmp.replace(path)


def append_history(path: Path, entry: dict[str, Any]) -> None:
    """runner.json's history + one play entry (the rest of the file kept)."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    d: dict[str, Any] = raw if is_obj(raw) else {}
    hist = d.get("history")
    d["history"] = [*(hist if isinstance(hist, list) else []), entry]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------ real I/O


class AttachedGame:
    """The running game over the bridge's debug-port client (bg3ai.game.Game); it can't relaunch it."""

    def __init__(self, game: Any):
        self.g = game

    def begin(self, sdir: Path, mem: Path) -> None:
        return None

    def oom(self) -> str | None:
        return None

    def ok(self) -> bool:
        try:
            return bool(self.g.game_state())
        except Exception:  # not answering
            return False

    def progress(self) -> dict[str, Any] | None:
        from bg3harness.game import io

        sig: dict[str, Any] | None = io.progress_signature(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            self.g.try_rpc("state.status"), self.g.try_rpc("state.journal")
        )
        return sig

    def fault(self) -> str | None:
        """For ClientAgent's game watch: "hang" when the game stopped answering."""
        return None if self.ok() else "hang"

    def recover(self, why: str) -> str | None:
        raise RuntimeError(f"the game {why} and the app can't relaunch it (bg3play-serve without --attach can)")

    def notes(self) -> list[str]:
        return []


SESSION_PY_TIMEOUT_S = 120


class SessionPy:
    """The playthrough memory's session lifecycle (bg3play.memory.session's start, stop, session-end), run as
    runner/play.py runs it: a subprocess with the session's clock env, so its env reads are the session's."""

    def __init__(self, mem: Path, python: str = sys.executable, run: Callable[..., Any] = subprocess.run):
        self.mem, self.python, self.run = mem, python, run

    def _run(self, cmd: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """session.py's answer; one that times out is no answer (exit 124, no output): no session opened, nothing
        blocks the stop, the end is skipped, as when session.py fails."""
        args = [self.python, "-m", "bg3play.memory.session", cmd, "--memory-dir", str(self.mem)]
        try:
            return self.run(args, capture_output=True, text=True, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                            timeout=SESSION_PY_TIMEOUT_S)  # fmt: skip
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", f"session.py {cmd} timed out")

    def start(self, env: dict[str, str]) -> tuple[str | None, str]:
        r = self._run("start", env)
        sid = memory_session.opened_sid(r.returncode, memlib.load_state(self.mem))
        return (sid, r.stdout.strip()) if sid else (None, "")

    def stop_check(self, env: dict[str, str]) -> str | None:
        r = self._run("stop", env)
        try:
            return memory_session.stop_reason(r.stdout)[0]
        except ValueError:
            return None

    def end(self, env: dict[str, str]) -> None:
        self._run("session-end", env)


def dump_system(path: Path, condition: str, repo: Path) -> Path:
    """pi's system text (D2) for a condition: the bg3 server's instructions and rules, dumped offline in the bridge's
    env by tools/pi/dump_server.py (bg3harness.client_setup dump_server)."""
    env = {k: v for k, v in os.environ.items() if k not in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF")}
    subprocess.run(
        ["uv", "run", "--project", str(repo / "bridge"), "python", str(repo / "tools/pi/dump_server.py"),
         "--system", str(path)],
        check=True, capture_output=True, timeout=300, env={**env, "BG3AI_PROMPT_CONDITION": condition},
    )  # fmt: skip
    return path


@dataclass
class ClientAgents:
    """HarnessRunner.agent_for: a ClientAgent for a config's client (a client profile, or client/model/effort).
    pi plays from its profile with a system text dumped for the config's condition and needs a cost cap, as
    play.py --profile does."""

    profiles: Path
    cwd: Path  # where the agent runs (play/: claude reads its CLAUDE.md and .claude/settings.json there)
    clock: Callable[[], float]
    fault: Callable[[], str | None] = lambda: None  # the game watch (AttachedGame.fault)
    server: dict[str, Any] | None = None  # the bg3 MCP server for pi/codex/gemini; None = play/.mcp.json's
    system_text: Callable[[Path, str], Path] | None = None  # (path, condition) -> pi's system file; None = dump_system
    repo: Path = Path(__file__).resolve().parents[5]

    def __call__(self, cfg: config.PlaythroughConfig, sdir: Path) -> ClientAgent:
        from bg3harness import clients, profiles
        from bg3harness.pi_client import PiClient

        a = cfg.agent
        profile = None
        if a.profile:
            try:
                profile = profiles.load(self.profiles / f"{a.profile}.toml")
            except (OSError, ValueError) as e:
                raise SupervisorError(f"client profile {a.profile}: {e}") from e
        client_name = profile.client if profile else a.client
        model = profile.model if profile else a.model
        effort = (profile.effort if profile else a.effort) or None
        kw: dict[str, Any] = {} if self.server is None else {"server": self.server}
        if client_name == "pi":
            if profile is None:
                raise SupervisorError("client pi: pick a pi client profile (profiles/*.toml)")
            if cfg.session.max_cost_usd <= 0:
                raise SupervisorError(f"client profile {a.profile}: pi needs a cost cap (session.max_cost_usd > 0)")
            path = sdir / f"pi-system.{a.condition}.txt"
            system = (
                self.system_text(path, a.condition) if self.system_text else dump_system(path, a.condition, self.repo)
            )
            client: Any = PiClient(profile, system, cwd=self.cwd, **kw)
        elif client_name == "claude":
            client = clients.ClaudeClient()
        else:
            client = clients.make(client_name, **kw)  # pyright: ignore[reportUnknownMemberType]
        return ClientAgent(client, model, effort, self.cwd, self.clock, self.fault)

    def keeper_client(self, cfg: config.PlaythroughConfig) -> tuple[str, Any] | None:
        """keeper_pass.MemoryKeeper's client_for: here nobody else keeps the memory, so every client's keeper runs
        in the app (pi with the playthrough's profile). None = the keeper is off."""
        if not cfg.keeper.enabled:
            return None
        if not cfg.agent.profile:
            return cfg.agent.client, None
        from bg3harness import profiles

        try:
            p = profiles.load(self.profiles / f"{cfg.agent.profile}.toml")
        except (OSError, ValueError) as e:
            raise SupervisorError(f"client profile {cfg.agent.profile}: {e}") from e
        return p.client, p if p.client == "pi" else None
