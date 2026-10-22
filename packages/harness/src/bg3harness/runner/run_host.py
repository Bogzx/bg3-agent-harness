"""The bench's RunHost on the live game (Phase D.3): `bg3bench run --runner session` plays each job through here.

bg3bench.adapters.session_run.SessionRun calls `start(job, sc, run_dir)` before the run's one session and
`record(job, sc, run_dir, outcome)` after it; the harness never imports the bench, so the match is structural (a
HostStart has session_run.Start's fields). `sc` is the v2 scenario dict with its defaults (bg3bench.scenario.load).

start: a SessionRunner for the run directory (make_runner: the entry point's Runner, which brings the LSF reader),
the client's run files (pi's system text, the bg3 tool count, the manifest), the reset (saves from now on only; the
mod's nav_*.json moved into <run_dir>/reset/), the game at the scenario's start (its start save loaded, or a new
game), the settings check and seed, and the kickoff for the job's condition. The session's hooks are the runner's
own (RunnerSessionHooks), so crashes, hangs and caps behave as in a playthrough; SessionRun's Recovery(max_resumes=0)
makes a fault end the run invalid.

record: facts.jsonl's run_end, the game stopped (the next run starts a fresh process), and the run's fields: the
outcome (the bench grades facts.jsonl; the harness decides no goal), cost and tokens of every play process, wall
time, the settings readout, the manifest fields and the reset.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import bg3harness
from bg3harness import client_setup, runs
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import SessionHooks, SessionOutcome

SID = "S001"  # a bench run is one session
Json = dict[str, Any]


class RunState(Protocol):
    """The runner's runner.json (bg3harness.runner.state.RunnerState) and its run directory."""

    dir: Path

    def __getitem__(self, key: str, /) -> Any: ...
    def __setitem__(self, key: str, value: Any, /) -> None: ...


class RunRunner(Protocol):
    """What the host drives of a SessionRunner (make_runner's result)."""

    run_started: float | None

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


def owner_of(sc: Json) -> str:
    """The save owner: the scenario's character (a save start fixes it), else the new game's name."""
    return (sc["profile"]["character"].get("name") or "Tav").strip()


def options_for(job: Json, sc: Json, run_name: str, files: Json) -> rstate.Options:
    """One bench run's Options: one session of the scenario's run_hours, no keeper, no judge, the start save (none
    for a new game), the dice seed and the settings the check compares. `files`: client_setup.run_files' fields."""
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


def scenario_kickoff(sc: Json, sid: str, loaded: str | None, pace: str = "") -> str:
    """A bench run's first message: the scenario, its start, the goal it is scored on, the character, how the
    session ends. No memory or playbook steps (a bench run has no playthrough memory); `pace`: the coaching
    condition's pre-flight ("" under rules)."""
    char, start = sc["profile"]["character"], sc["start"]
    lines = [
        f"This is a benchmark run of the scenario '{sc.get('title') or sc['name']}' ({sc['name']}), session "
        f"{sid}, started by the harness."
    ]
    if start["kind"] == "save":
        note: Json = start.get("note") or {}
        lines.append(
            f'The harness has loaded save "{loaded or start.get("save")}": the character, party, gear and '
            "journal are the save's. Don't reload it; verify state with get_status. Never start a new game."
        )
        lines += [f"{k.capitalize()}: {note[k]}" for k in ("where", "objective", "party", "journal") if note.get(k)]
    else:
        lines.append(rpolicy.new_game_note(owner_of(sc)))
    lines.append(f"The run is scored on the game state at its end: {json.dumps(sc['goal'], sort_keys=True)}.")
    if char.get("brief"):
        lines.append("Character brief:\n> " + char["brief"].strip().replace("\n", "\n> "))
    lines.append(
        rpolicy.ROLEPLAY_TEXT[char.get("roleplay") if char.get("roleplay") in rpolicy.ROLEPLAY_TEXT else "strict"]
    )
    lines.append(
        "Keep playing until the ⏱ line in tool results says WRAP UP NOW, then stop. Nobody will answer "
        "questions: decide yourself."
    )
    if pace:
        lines.append(pace)
    return "\n\n".join(lines)


class LiveRunHost:
    """bg3bench's RunHost (structural) on the live game, one run at a time.

    make_runner(opt) builds the run's runner (runner/play.py passes its Runner: SessionRunner with the
    playthrough parts, whose LSF reader the settings readout needs); run_files(run_dir, client, seed, timing)
    dumps the client's files (client_setup.run_files by default); env is the process env the MCP server and the
    client inherit (the job's condition goes there)."""

    def __init__(
        self,
        make_runner: Callable[[rstate.Options], RunRunner],
        *,
        run_files: Callable[[Path, str, int | None, str | None], Json] | None = None,
        env: MutableMapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.make_runner, self.clock = make_runner, clock
        self.env = os.environ if env is None else env
        self.run_files = run_files or self._run_files
        self.runs: dict[Path, tuple[RunRunner, float, list[str]]] = {}

    @staticmethod
    def _run_files(d: Path, client: str, seed: int | None, timing: str | None) -> Json:
        return client_setup.run_files(d, client, seed, timing, renv.REPO, renv.log)

    def _setup(self, job: Json, sc: Json, run_dir: Path) -> tuple[Json, str, str | None]:
        """The job's client env (condition, API key, tools) and run files; the kickoff's pace pre-flight."""
        choice = client_setup.resolve(job["client"], job.get("profile"), job["model"], job.get("effort"), None, False)
        self.env.update(client_setup.api_key_env(choice.profile, self.env, renv.REPO / ".env"))
        self.env["BG3AI_PROMPT_CONDITION"] = job["condition"]
        self.env.update(client_setup.session_env(choice.client, choice.profile, True, self.env))
        files: Json = {"profile": choice.profile, "pace": client_setup.kickoff_pace(choice.client, self.env)}
        if choice.client == "claude" or (choice.profile is not None and choice.client == "pi"):
            files |= self.run_files(run_dir, choice.client, sc["game_settings"].get("seed"), sc.get("timing"))
        return files, choice.model, choice.effort

    def start(self, job: Json, sc: Json, run_dir: Path) -> HostStart:
        want = renv.RUNTIME / "sessions" / run_dir.name
        if Path(run_dir).resolve() != want.resolve():  # facts.jsonl and the streams must land in the run dir
            raise ValueError(f"run dir {run_dir}: the runner writes runtime/sessions/<run> under {renv.RUNTIME}")
        files, model, effort = self._setup(job, sc, run_dir)
        opt = options_for(dict(job, model=model, effort=effort), sc, run_dir.name, files)
        r = self.make_runner(opt)
        t0 = self.clock()
        r.run_started = t0
        r.state["saves_after"] = t0  # reset.saves: only saves from now on (and the start save) are the run's
        moved = archive_nav_state(renv.SE_BG3AI_DIR, run_dir / "reset") if sc["reset"]["nav_state"] == "archive" else []
        r.event("run_reset", nav_files=moved)
        self.runs[run_dir] = (r, t0, moved)
        try:
            r.refresh_mod_snapshot()
        except OSError as e:
            renv.log(f"mod snapshot refresh failed: {e}; the game keeps {r.mod_rev}'s mod")
        loaded = r.ensure_game(first=True)
        r.hot_reload_mod()
        r.apply_play_settings()
        r.check_settings()  # the scenario's difficulty / karmic dice against the readout; the seed
        prompt = scenario_kickoff(sc, SID, loaded, opt.pace or "")
        minutes = opt.session_minutes
        return HostStart(
            prompt,
            r.session_hooks(SID, minutes),
            str(uuid.uuid4()),
            hashlib.sha256(prompt.encode()).hexdigest(),
            (),
            bg3harness.__version__,
        )

    def record(self, job: Json, sc: Json, run_dir: Path, outcome: SessionOutcome) -> Json:
        r, t0, moved = self.runs.pop(run_dir)
        res = outcome.result
        name = "gave_up" if outcome.invalid or outcome.gave_up else "over_budget" if r.cap_hit else "timeout"
        try:
            r.end_facts(name)
        finally:
            r.end_run_game("run end", "the next run launches a new one")
        totals: Json = runs.claude_totals(r.state.dir, t0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return {
            "outcome": name,
            "reason": res.reason,
            "cost_usd": totals["cost_usd"],
            "tokens": totals["tokens"],
            "turns": totals["turns"],
            "wall_min": round((res.ended - outcome.started) / 60, 2),
            "sessions": 1,
            "cap_hit": r.cap_hit,
            "game_settings": getattr(r, "game_settings", None),
            "manifest": r.opt.manifest,
            "reset": {"nav_files": moved},
            "loaded_save": r.state["loaded_save"],
        }
