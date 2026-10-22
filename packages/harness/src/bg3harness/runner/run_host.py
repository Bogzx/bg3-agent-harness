"""The bench's RunHost on the live game (Phase D.3): `bg3bench run --runner session` plays each job through here.

bg3bench.adapters.session_run.SessionRun calls `start(job, sc, run_dir)` before the run's one session and
`record(job, sc, run_dir, outcome)` after it; the harness never imports the bench, so the match is structural (a
HostStart has session_run.Start's fields). `sc` is the v2 scenario dict with its defaults (bg3bench.scenario.load).

start: a SessionRunner for the run directory (make_runner: the entry point's Runner, which brings the LSF reader),
the client's run files (pi's system text, the bg3 tool count, the manifest), the reset (saves from now on only; for a
save start the mod's nav_*.json moved into <run_dir>/reset/, a new game's host is a new uuid that has none), the game
at the scenario's start (its start save loaded, or a new game), the settings check and seed, and the kickoff for the
job's condition. The session's hooks are the runner's
own (RunnerSessionHooks), so crashes, hangs and caps behave as in a playthrough; SessionRun's Recovery(max_resumes=0)
makes a fault end the run invalid. A goal scenario's job carries the bench's live verdict (job["verdict"]):
GoalWatch asks it while the run plays and ends the run once it says passed or failed.

record: facts.jsonl's run_end, the game stopped (the next run starts a fresh process), the moved nav files put back
(the run's own versions kept in <run_dir>/reset/run/; a failed start puts them back too, and a marker,
runtime/nav-reset.json, lets the next start restore what a run that died left moved), and the run's fields: the
outcome (the bench grades facts.jsonl; the harness decides no goal: a goal run the live verdict ended is "reached"
or "failed" by that verdict), cost and tokens of every play process, wall
time, the settings readout, the manifest fields and the reset.

stop: an operator stop (play.py bench's first SIGINT/SIGTERM, or runtime/STOP, which the runner reads) ends the run's
session at the runner's next check (reason "stopped"); record then writes it as an invalid run (`operator: stop`) with
the game stopped, and no new run starts. abort (a second signal, or an exception out of the matrix) kills the play
process and the game of a run still open and puts its nav files back, without a record.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import bg3harness
from bg3harness import client_setup, runs
from bg3harness.runner import agent_group
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import SessionHooks, SessionOutcome

SID = "S001"  # a bench run is one session
SCENARIO_DIFFICULTY = "BG3AI_SCENARIO_DIFFICULTY"  # bg3ai.tools.newgame.SCENARIO_DIFFICULTY_ENV
GOAL_HIT = "goal"  # until_hit's id when the bench's live verdict decided a goal scenario (GoalWatch)
GOAL_POLL_S = 10.0  # how often GoalWatch asks the verdict: the stop comes one poll + the in-flight tool call late
NO_MEMORY = "memory"  # the bridge's memory tool group (bg3ai.capabilities.GROUPS): off in every bench run
Json = dict[str, Any]


class RunState(Protocol):
    """The runner's runner.json (bg3harness.runner.state.RunnerState) and its run directory."""

    dir: Path

    def __getitem__(self, key: str, /) -> Any: ...
    def __setitem__(self, key: str, value: Any, /) -> None: ...


class RunRunner(Protocol):
    """What the host drives of a SessionRunner (make_runner's result)."""

    run_started: float | None
    settles_budget: bool  # False here: SessionRun's matrix adds the run's cost to budget.json
    stop_ends_session: bool  # True here: an operator stop ends the run's one session at the next check
    stop_requested: bool
    until_hit: Json | None  # GoalWatch sets it: the agent loop stops on it as on an until milestone
    run_env: dict[str, str] | None  # the job's env (the play process, its bg3 server, the play settings)

    @property
    def opt(self) -> rstate.Options: ...
    @property
    def state(self) -> RunState: ...
    @property
    def cap_hit(self) -> Json | None: ...
    @property
    def mod_rev(self) -> str | None: ...

    def event(self, kind: str, **fields: Any) -> None: ...
    def refresh_mod_snapshot(self) -> list[str]: ...
    def ensure_game(self, first: bool) -> str | None: ...
    def hot_reload_mod(self) -> int: ...
    def apply_play_settings(self) -> None: ...
    def check_settings(self) -> None: ...
    def session_hooks(self, sid: str, minutes: float) -> SessionHooks: ...
    def end_facts(self, outcome: str) -> None: ...
    def end_run_game(self, when: str = ..., after: str = ...) -> None: ...
    def kill_agent(self) -> None: ...


@dataclass(frozen=True)
class HostStart:
    """bg3bench.adapters.session_run.Start's fields."""

    prompt: str
    hooks: SessionHooks
    client_session: str | None = None
    prompt_sha: str | None = None
    prompt_parts: Sequence[Json] = field(default_factory=tuple[Json, ...])
    harness_version: str | None = None


def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """The reset's nav_state "archive": the mod's machine-global nav_*.json (kill lists, breadcrumb mirrors) moved
    into dest, so the run starts with none from an earlier game (bg3bench.resets.archive_nav_state's rule)."""
    moved: list[str] = []
    try:
        files = sorted(p for p in se_bg3ai.glob("nav_*.json") if p.is_file())
    except OSError:
        return moved
    for p in files:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except OSError:
            continue
    return moved


def isolate_nav(se_bg3ai: Path, dest: Path, marker: Path) -> list[str]:
    """A save start's nav reset: every nav_*.json moved into dest (archive_nav_state), the marker written first so
    restore_nav can put them back even after a crash. The decision of 2026-10-22 03:30 is to move only the start
    host's two (bg3bench.resets.isolate_nav); that needs the save's host uuid, which tools/saves/inspect.py doesn't
    report yet, so all of them go (and all come back). Returns the moved names."""
    try:
        names = sorted(p.name for p in se_bg3ai.glob("nav_*.json") if p.is_file())
    except OSError:
        return []
    if not names:
        return []
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"se": str(se_bg3ai), "dest": str(dest), "files": names}))
    return archive_nav_state(se_bg3ai, dest)


def restore_nav(marker: Path) -> list[str]:
    """After the run (or a failed start, or at the next start after a crash): the marker's files go back from dest;
    a file of the same name the run wrote meanwhile is kept in <dest>/run/. Idempotent; the marker goes last. No
    marker: nothing was moved, []."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    se, dest = Path(m["se"]), Path(m["dest"])
    back: list[str] = []
    for n in m["files"]:
        if not (dest / n).is_file():
            continue
        if (se / n).exists():
            (dest / "run").mkdir(parents=True, exist_ok=True)
            shutil.move(str(se / n), str(dest / "run" / n))
        shutil.move(str(dest / n), str(se / n))
        back.append(n)
    marker.unlink(missing_ok=True)
    return back


def nav_marker() -> Path:
    return renv.RUNTIME / "nav-reset.json"


def stop_file() -> Path:
    """runtime/STOP: the operator's stop file (a playthrough stops after its session, a bench run at once)."""
    return renv.RUNTIME / "STOP"


def stop_on_signals(host: LiveRunHost, fd: int = 2) -> None:
    """SIGINT/SIGTERM for play.py bench: the first one is host.stop() (the run playing now ends and is recorded, no new
    run starts), a second one raises KeyboardInterrupt (the caller's finally calls host.abort()).

    The handler only sets flags and says so with one os.write to `fd` (stderr): a print or a logging call there
    re-enters the buffered stream the main thread may be writing ("reentrant call inside BufferedWriter", the merge
    suite under load). The runner's agent loop logs the stop when it acts on it."""
    seen: list[int] = []

    def on_signal(signum: int, _frame: object) -> None:
        seen.append(signum)
        if len(seen) > 1:
            os.write(fd, f"signal {signum} again: exiting now; the run is not recorded\n".encode())
            raise KeyboardInterrupt
        host.stop()
        os.write(
            fd, f"signal {signum}: the run playing now ends and is recorded (send again to exit at once)\n".encode()
        )

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)


class GoalWatch:
    """A goal scenario's live verdict (SessionRun's job["verdict"]: "passed", "failed" or None) asked every poll_s
    while the run plays. Once it decides, the runner's until_hit is set to {"id": GOAL_HIT, "verdict": ...}: the
    agent loop stops the agent on it as on an until milestone (a goal run never plays on past its grade). check()
    is one poll (the tests call it directly)."""

    def __init__(self, r: RunRunner, verdict: Callable[[], str | None], poll_s: float = GOAL_POLL_S):
        self.r, self.verdict, self.poll_s = r, verdict, poll_s
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="goal-watch", daemon=True)

    def check(self) -> bool:
        """True once the run is decided (by this verdict or an earlier stop): nothing left to watch."""
        if self.r.until_hit:
            return True
        try:
            v = self.verdict()
        except Exception as e:  # a verdict that can't be read doesn't end the run
            renv.log(f"goal verdict failed: {type(e).__name__}: {e}")
            return False
        if v not in ("passed", "failed"):
            return False
        self.r.until_hit = {"id": GOAL_HIT, "label": f"the goal {v}", "verdict": v, "session": SID, "t": time.time()}
        renv.log(f"{SID}: the scenario's goal {v}: stopping the run")
        self.r.event("goal_verdict", verdict=v, session=SID)
        return True

    def _loop(self) -> None:
        while not self.done.wait(self.poll_s):
            if self.check():
                return

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.done.set()


def owner_of(sc: Json) -> str:
    """The save owner: the scenario's character (a save start fixes it), else the new game's name."""
    return (sc["profile"]["character"].get("name") or "Tav").strip()


def options_for(job: Json, sc: Json, run_name: str, files: Json) -> rstate.Options:
    """One bench run's Options: one session of the scenario's run_hours, no keeper, no judge, the start save (none
    for a new game), the dice seed and the settings the check compares. An `until` scenario's run ends at its
    milestone (the runner stops the agent once the progress poller sees it: until_hit). `files`:
    client_setup.run_files' fields."""
    caps, char, gs = sc["caps"], sc["profile"]["character"], sc["game_settings"]
    max_cost = job.get("max_cost") if job.get("max_cost") is not None else caps.get("max_cost")
    return rstate.Options(
        run_name,
        None,
        caps["run_hours"] * 60,
        job["model"],
        save_owner=owner_of(sc),
        effort=job.get("effort"),
        allow_short=True,
        keeper=False,
        until=sc.get("until"),
        character_brief=char.get("brief"),
        roleplay=char.get("roleplay"),
        difficulty=gs.get("difficulty"),
        run_hours=caps["run_hours"],
        max_cost=max_cost,
        max_tokens=caps.get("max_tokens"),
        scenario=sc["name"],
        start_save=sc["start"].get("save"),
        client=job["client"],
        seed=gs.get("seed"),
        scenario_settings={
            "difficulty": gs.get("difficulty"),
            "karmic_dice": gs.get("karmic_dice"),
            "timing": sc.get("timing"),
        },
        **files,
    )


def goal_of(sc: Json) -> Json:
    """The scenario's goal predicate: an `until` milestone (nautiloid) is {"milestone": it} (as bg3bench.scenario's
    goal_of; the harness never imports the bench)."""
    return {"milestone": sc["until"]} if "until" in sc else sc["goal"]


def scenario_kickoff(sc: Json, sid: str, loaded: str | None, pace: str = "") -> str:
    """A bench run's first message: the task and how it is scored (the scenario's `task` and `scored_by`, first),
    when the run ends (once graded, or at the time cap), its start (`loaded`: the label of the save the harness
    loaded), the grader's exact goal (graders.grade: passed at the first instant it holds, failed by a "never" that
    happened before), the character. No memory or playbook steps (a bench run has no playthrough memory) and no
    "play until WRAP UP NOW": the run ends on its verdict (an until milestone, or GoalWatch). `pace`: the coaching
    condition's pre-flight ("" under rules)."""
    char, start = sc["profile"]["character"], sc["start"]
    note: Json = start.get("note") or {}
    task = sc.get("task") or note.get("objective") or "Make the goal below hold."
    cap = round(sc["caps"]["run_hours"] * 60)
    lines = [
        f"This is a benchmark run of the scenario '{sc.get('title') or sc['name']}' ({sc['name']}), session "
        f"{sid}, started by the harness.",
        f"YOUR TASK: {task}",
    ]
    if sc.get("scored_by"):
        lines.append(f"How it is scored: {sc['scored_by']}")
    lines.append(
        f"The run ends as soon as the task is decided (passed, or it can no longer pass) or after {cap} minutes "
        "of play (the time cap), whichever comes first."
    )
    if start["kind"] == "save":
        lines.append(
            f'The harness has loaded save "{loaded}": the character, party, gear and '
            "journal are the save's. Don't reload it; verify state with get_status. Never start a new game."
        )
        lines += [f"{k.capitalize()}: {note[k]}" for k in ("where", "objective", "party", "journal") if note.get(k)]
    else:
        lines.append(rpolicy.new_game_note(owner_of(sc)))
    since = "the start save" if start["kind"] == "save" else "the new game"
    lines.append(
        f'The grader\'s exact goal: the run passes the first moment it holds, from {since} on; anything under "never" '
        f"fails it if it happens first: {json.dumps(goal_of(sc), sort_keys=True)}."
    )
    if char.get("brief"):
        lines.append("Character brief:\n> " + char["brief"].strip().replace("\n", "\n> "))
    lines.append(
        rpolicy.ROLEPLAY_TEXT[char.get("roleplay") if char.get("roleplay") in rpolicy.ROLEPLAY_TEXT else "strict"]
    )
    lines.append("Nobody will answer questions: decide yourself.")
    if pace:
        lines.append(pace)
    return "\n\n".join(lines)


class BenchHooks:
    """The runner's session hooks for a bench run, except that a play process that can't start (the client's binary
    missing, a leftover agent that survived SIGKILL: OSError, RuntimeError) ends the session as a `setup_failed`
    result instead of raising out of the matrix (H3: the run then went unrecorded with its game up). record lists it
    as a harness failure, so the run is invalid and the matrix reruns it. A playthrough's run loop keeps the raise
    (it counts setup failures and recovers)."""

    def __init__(self, hooks: SessionHooks, event: Callable[..., None]):
        self.hooks, self.event = hooks, event

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str) -> Any:
        try:
            return self.hooks.run_agent(prompt, minutes, client_session, resume, tag)
        except (OSError, RuntimeError) as e:
            detail = f"{type(e).__name__}: {e}"[:300]
            self.event("setup_failed", session=SID + tag, error=detail)
            now = self.hooks.now()
            return rstate.SessionResult(SID, client_session or "", None, "setup_failed", now, now, detail=detail)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.hooks, name)


def run_failures(
    state_dir: Path, t0: float, outcome: SessionOutcome, name: str, why: str, cap: str | None = None
) -> list[Json]:
    """Every failure of a bench run, tagged harness/agent/unknown as the legacy series tags them (runs.classify_run
    over the run's runner_events.jsonl, its play streams and the agent's reloads; its one session as the history),
    plus the session's ending when it was ours (runs.reason_failures: mcp_failed, setup_failed, aborted, api_limit).
    A harness failure makes the run invalid (bg3bench results.validity): it doesn't score the model. name: the
    outcome to classify ("stopped" for an operator stop: listed apart, not a failure)."""
    res = outcome.result
    logged: list[Json] = runs.read_jsonl(state_dir / "runner_events.jsonl")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    events = [e for e in logged if e.get("t", 0) >= t0 - 1]
    session = {"kind": "play", "session": SID, "started": outcome.started, "ended": res.ended}
    session |= {"reason": res.reason, "exit": res.exit_code}
    tools: list[Json] = rpolicy.read_actions_since(t0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    actions = [a for a in tools if a.get("t", res.ended) <= res.ended + 1]
    ending = (res.reason, why)
    gave_up_why = why or res.reason if name == "gave_up" else ""  # a time-out or a cap says its own why
    return runs.classify_run(state_dir, t0, res.ended, [session], events, actions, name, gave_up_why, cap, ending)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


class LiveRunHost:
    """bg3bench's RunHost (structural) on the live game, one run at a time.

    make_runner(opt) builds the run's runner (runner/play.py passes its Runner: SessionRunner with the
    playthrough parts, whose LSF reader the settings readout needs); run_files(run_dir, client, seed, timing)
    dumps the client's files (client_setup.run_files by default); env is the base env (this process's, read once):
    each job's env is a fresh copy with the job's vars (condition, API key, tools, memory dir), handed to its runner
    (run_env: the play process, its bg3 server and the play settings read it), so nothing of one job reaches the next
    and os.environ is never changed (H1)."""

    def __init__(
        self,
        make_runner: Callable[[rstate.Options], RunRunner],
        *,
        run_files: Callable[[Path, str, int | None, str | None], Json] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.make_runner, self.clock = make_runner, clock
        self.base = dict(os.environ if env is None else env)
        self.env: dict[str, str] = dict(self.base)  # the current job's (_setup)
        self.run_files = run_files or self._run_files
        self.runs: dict[Path, tuple[RunRunner, float, list[str]]] = {}
        self.watches: dict[Path, GoalWatch] = {}  # a goal scenario's live verdict while its run plays
        self.stopping = False  # stop(): an operator signal

    @property
    def stopped(self) -> bool:
        """An operator stop (a signal, or runtime/STOP): no new run starts."""
        return self.stopping or stop_file().exists()

    def stop(self) -> None:
        """An operator stop (play.py bench's first SIGINT/SIGTERM): the run playing now ends its session at the
        runner's next check and is recorded (operator: invalid); no new run starts."""
        self.stopping = True
        for r, _, _ in self.runs.values():
            r.stop_requested = True

    def abort(self, when: str = "operator exit") -> None:
        """A hard exit (a second signal, or an exception out of the matrix): every run still open loses its play
        process and the game it brought up, and its moved nav files go back. Its record is not written."""
        while self.runs:
            run_dir, (r, _, _) = self.runs.popitem()
            if (w := self.watches.pop(run_dir, None)) is not None:
                w.stop()
            try:
                r.kill_agent()
                r.end_run_game(when, "the run is not recorded")
            finally:
                restore_nav(nav_marker())

    def _run_files(self, d: Path, client: str, seed: int | None, timing: str | None) -> Json:
        return client_setup.run_files(d, client, seed, timing, renv.REPO, renv.log, environ=self.env)

    def _setup(self, job: Json, sc: Json, run_dir: Path) -> tuple[Json, str, str | None]:
        """The job's client env (condition, API key, tools) and run files; the kickoff's pace pre-flight.

        A bench run sees no playthrough memory (knowledge contamination, live7: session_start served play/memory's
        briefing): the memory tool group is off and the server's memory dir is the run's own `memory/`, which
        nothing fills, so no tool reads another playthrough's notes. job["tools_off"]: the condition's removed
        tools (the bench's conditions.removed_tools: wiki, load_game); job["prompt_condition"]: the prompt condition
        the bench's id maps to (conditions.Condition.prompt_condition)."""
        choice = client_setup.resolve(job["client"], job.get("profile"), job["model"], job.get("effort"), None, False)
        self.env = dict(self.base)
        self.env.update(client_setup.api_key_env(choice.profile, self.env, renv.REPO / ".env"))
        self.env["BG3AI_PROMPT_CONDITION"] = job["prompt_condition"]  # rules | rules+coaching, not the bench's id
        self.env["BG3AI_MEMORY_DIR"] = str(run_dir / "memory")
        self.env["BG3AI_TOOLS_OFF"] = ",".join([NO_MEMORY, *job.get("tools_off", ())])  # the profile's go on top
        # the scenario's autoskip (default on): the runner's ui.config each session, and the play server's skipper
        self.env["BG3AI_AUTOSKIP"] = "0" if sc["game_settings"].get("autoskip", True) is False else "1"
        difficulty = sc["game_settings"].get("difficulty") if sc["start"]["kind"] == "new_game" else None
        if difficulty:  # the bridge's new_game starts it, whatever the agent picks (live7: Explorer, not Balanced)
            self.env[SCENARIO_DIFFICULTY] = difficulty
        self.env.update(client_setup.session_env(choice.client, choice.profile, True, self.env))
        files: Json = {"profile": choice.profile, "pace": client_setup.kickoff_pace(choice.client, self.env)}
        if choice.client == "claude" or (choice.profile is not None and choice.client == "pi"):
            files |= self.run_files(run_dir, choice.client, sc["game_settings"].get("seed"), sc.get("timing"))
        return files, choice.model, choice.effort

    def start(self, job: Json, sc: Json, run_dir: Path) -> HostStart:
        """The run's runner, reset, game at the scenario's start and kickoff. A start that fails forgets the run,
        stops the game it brought up and raises RuntimeError (the runner's SystemExits too: an operator refusal
        such as ensure_game's foreign game would otherwise end the process), so SessionRun records one invalid
        attempt and the matrix reruns. A save start whose save didn't load (missing, or --take-over playing on
        from another) fails before the kickoff. The nav files a failed start moved are put back."""
        want = renv.RUNTIME / "sessions" / run_dir.name
        if Path(run_dir).resolve() != want.resolve():  # facts.jsonl and the streams must land in the run dir
            raise ValueError(f"run dir {run_dir}: the runner writes runtime/sessions/<run> under {renv.RUNTIME}")
        if self.stopped:
            raise RuntimeError("operator stop: no new run starts")
        r: RunRunner | None = None
        try:
            agent_group.reap_leftover(renv.RUNTIME / agent_group.FILE, renv.log)  # live7: it drove the next game
            files, model, effort = self._setup(job, sc, run_dir)
            opt = options_for(dict(job, model=model, effort=effort), sc, run_dir.name, files)
            r = self.make_runner(opt)
            r.run_env = self.env
            st = self._begin(r, opt, sc, run_dir)
            if job.get("verdict") is not None:
                self.watches[run_dir] = GoalWatch(r, job["verdict"])
                self.watches[run_dir].start()
            return st
        except (Exception, SystemExit) as e:
            try:
                if r is not None:
                    self.runs.pop(run_dir, None)
                    r.end_run_game("start failed", "the rerun launches a new one")
            finally:
                restore_nav(nav_marker())
            if isinstance(e, SystemExit):
                raise RuntimeError(f"start failed: {e.code}") from e
            raise

    def _begin(self, r: RunRunner, opt: rstate.Options, sc: Json, run_dir: Path) -> HostStart:
        t0 = self.clock()
        r.run_started = t0
        r.settles_budget = False  # the matrix settles a paid run's cost (once): the runner must not add it too
        r.stop_ends_session = True
        r.stop_requested = self.stopping
        r.state["saves_after"] = t0  # reset.saves: only saves from now on (and the start save) are the run's
        restore_nav(nav_marker())  # what a run that died between start and record left moved
        archive = sc["reset"]["nav_state"] == "archive" and sc["start"]["kind"] == "save"
        moved = isolate_nav(renv.SE_BG3AI_DIR, run_dir / "reset", nav_marker()) if archive else []
        r.event("run_reset", nav_files=moved)
        self.runs[run_dir] = (r, t0, moved)
        try:
            r.refresh_mod_snapshot()
        except OSError as e:
            renv.log(f"mod snapshot refresh failed: {e}; the game keeps {r.mod_rev}'s mod")
        loaded = r.ensure_game(first=True)
        if sc["start"]["kind"] == "save":
            start_save = sc["start"].get("save")
            if not start_save or r.state["loaded_save"] != start_save:
                raise RuntimeError(
                    f"the start save {start_save} didn't load (loaded: {r.state['loaded_save']}): "
                    "the run would play from elsewhere"
                )
            loaded = loaded or start_save
        r.hot_reload_mod()
        r.apply_play_settings()
        r.check_settings()  # the scenario's difficulty / karmic dice against the readout; the seed
        if self.stopped:  # stopped while the game came up: no agent starts (start's handler stops the game)
            raise RuntimeError("operator stop before the kickoff")
        prompt = scenario_kickoff(sc, SID, loaded, opt.pace or "")
        minutes = opt.session_minutes
        return HostStart(
            prompt,
            cast(SessionHooks, BenchHooks(r.session_hooks(SID, minutes), r.event)),
            str(uuid.uuid4()),
            hashlib.sha256(prompt.encode()).hexdigest(),
            (),
            bg3harness.__version__,
        )

    def record(self, job: Json, sc: Json, run_dir: Path, outcome: SessionOutcome) -> Json:
        r, t0, moved = self.runs.pop(run_dir)
        if (w := self.watches.pop(run_dir, None)) is not None:
            w.stop()
        res = outcome.result
        hit = r.until_hit if r.until_hit and r.until_hit.get("id") == sc.get("until") else None
        goal = r.until_hit if r.until_hit and r.until_hit.get("id") == GOAL_HIT and "goal" in sc else None
        stopped = res.reason == "stopped"  # an operator stop ended the session: invalid (bench results.validity)
        invalid = outcome.invalid or outcome.gave_up or stopped or res.reason in runs.HARNESS_REASONS
        if invalid:
            name = "gave_up"
        elif goal:
            name = "reached" if goal["verdict"] == "passed" else "failed"  # the live verdict ended the run
        else:
            name = "reached" if hit else "over_budget" if r.cap_hit else "timeout"
        try:
            r.end_facts(name)
        finally:
            try:
                r.end_run_game("run end", "the next run launches a new one")
            finally:
                back = restore_nav(nav_marker())  # with the game stopped: it writes no nav file any more
        totals: Json = runs.claude_totals(r.state.dir, t0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        why, cap = getattr(res, "detail", ""), (r.cap_hit or {}).get("cap")
        failures = run_failures(r.state.dir, t0, outcome, "stopped" if stopped else name, why, cap)
        counts = {cls: sum(int(f.get("n") or 1) for f in failures if f["class"] == cls) for cls in runs.CLASSES}
        return (
            {
                "outcome": name,
                "reason": res.reason,
                "cost_usd": None if totals["cost_unknown"] else totals["cost_usd"],  # unknown is not $0
                "tokens": totals["tokens"],
                "served": totals["served"],  # what the endpoint served (pi: thinking levels, providers)
                "turns": totals["turns"],
                "wall_min": round((res.ended - outcome.started) / 60, 2),
                "sessions": 1,
                "cap_hit": r.cap_hit,
                "milestone": hit and hit["id"],
                "game_settings": getattr(r, "game_settings", None),
                "manifest": r.opt.manifest,
                "reset": {"nav_files": moved, "restored": back},
                "loaded_save": r.state["loaded_save"],
                "harness_failures": counts[runs.HARNESS],
                "agent_failures": counts[runs.AGENT],
                "unknown_failures": counts[runs.UNKNOWN],
                "failures": failures,
            }
            | ({"operator": "stop"} if stopped else {})
            | ({"cost_unknown": True} if totals["cost_unknown"] else {})
        )
