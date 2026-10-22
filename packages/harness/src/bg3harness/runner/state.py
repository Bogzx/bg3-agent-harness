# pyright: basic
"""The runner's run state and options (moved from runner/play.py, Phase D.3): RunnerState
(runtime/sessions/<pt>/runner.json), runner.status.json, Options (one run's flags), the play client, SessionResult,
and the two adapters the harness's session and game code call back through: RunnerSessionHooks (bg3harness.session
run_session's SessionHooks) and RunnerGameHost (bg3harness.game.controller's GameHost).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bg3harness import bg3le_buildinfo, clients, sandbox
from bg3harness.game import host as ghost
from bg3harness.game import io as game
from bg3harness.game.io import LaunchGaveUp, Save
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.session import FAULTS, RecoverFailure

if TYPE_CHECKING:  # annotations only: core imports the mixins, which import this module
    from bg3harness.runner import core as rcore


class RunnerState:
    """runtime/sessions/<playthrough>/runner.json: save owner, runner quicksaves, session history."""

    def __init__(self, playthrough: str):
        self.dir = renv.RUNTIME / "sessions" / playthrough
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "runner.json"
        self.data: dict[str, Any]
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("history", [])
        self.data.setdefault("runner_saves", [])

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def __getitem__(self, k) -> Any:
        return self.data.get(k)

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()


def write_status(**fields) -> None:
    ghost.write_status(renv.RUNTIME, **fields)


@dataclass
class Options:
    playthrough: str
    hours: float | None
    session_minutes: float
    model: str
    take_over: bool = False
    dry_run: bool = False
    save_owner: str | None = None
    effort: str | None = None          # claude --effort; None = Claude Code's default
    fix_model: str | None = None       # the fix-notes pass (no game, small edits); None = same as play
    fix_effort: str | None = "low"
    fallback_model: str | None = None
    allow_short: bool = False          # --allow-short-sessions: session-config minutes below MIN_SESSION_MINUTES too
    keeper: bool = True                # memory-keeper pass between sessions (runner/memory_keeper.py)
    keeper_model: str = "same"         # "same" = the session's play model (a benchmark compares one model end to end)
    keeper_effort: str | None = "low"
    keeper_minutes: float = 5.0
    autocompact: int | None = None     # claude --autocompact <tokens> (play sessions); None = Claude Code's default
    until: str | None = None           # run --until <milestone id> (bg3ai.milestones): stop there, write runs.jsonl
    character_brief: str | None = None  # run --character-brief: written to profile.json (CHAR-1 phase 0)
    roleplay: str | None = None        # run --roleplay strict|light (profile.json character.roleplay)
    difficulty: str | None = None      # run --difficulty (profile.json difficulty, phase 0's new_game)
    run_hours: float | None = None     # run --run-hours: cap of one --until run (then it ends as a timeout)
    series: str | None = None          # run --runs: the series (the base playthrough); its runs.jsonl gets the records
    run_index: int | None = None       # run --runs: this run's number in the series (1 = the base playthrough)
    series_end: float | None = None    # run --runs --hours: the whole series' end (epoch s)
    max_cost: float | None = None      # run --max-cost USD: cost cap of one --until run (then it ends over_budget)
    max_tokens: int | None = None      # run --max-tokens N: token cap of one --until run (runs.token_total)
    pins: dict | None = None           # version pins this run plays with (runs.version_pins), recorded in runs.jsonl
    version_drift: list | None = None  # --allow-version-drift: the pins that differ from the series' first run
    cap_changes: list | None = None    # --allow-cap-change: the caps that differ from series.json
    scenario: str | None = None        # run --scenario NAME: scenarios/<name>.json (frozen in series.json)
    start_save: str | None = None      # its start save folder: loaded for the first session, kept out of saves_after
    judge: bool = False                # run --judge (default with --runs): score the run's persona after its record
    judge_model: str | None = None     # the judge's model (series.json "judge_model" for a series)
    judge_change: str | None = None    # --allow-judge-change: the judge model differs from series.json's
    client: str = "claude"             # run --client: the CLI that plays (runner/clients.py; frozen in series.json)
    client_change: str | None = None   # --allow-client-change: the client differs from series.json's
    sandbox_change: str | None = None  # --allow-sandbox-change: the sandbox mode differs from series.json's
    sandbox: str = "none"              # run --sandbox: none | container (runner/sandbox.py)
    box: sandbox.Sandbox | None = None  # --sandbox container: prepared once by cmd_run (firewall checked, proxy up)
    restart_run: bool = False          # run --runs --restart-run: start the next run over even after finished sessions
    keep_game: bool = False            # run --until/--runs --keep-game: leave the harness game up when the run/series ends
    max_cost_per_day: float | None = None  # run --max-cost-per-day USD: pause while the trailing 24 h spent this much
    profile: object | None = None      # run --profile: profiles.Profile (pi: endpoint, model, effort)
    system_file: str | None = None     # pi: the system text, dumped once per run (client_setup.dump_server)
    tools_expected: int | None = None  # pi, claude: the bg3 tools every play process must get (else mcp_failed)
    manifest: dict[str, Any] | None = None  # pi, claude: run_manifest.manifest_fields (the run record's "manifest")
    pace: str | None = None            # the kickoff's pace pre-flight: client_setup.kickoff_pace ("": none); None: claude's
    seed: int | None = None            # run --seed (scenario game_settings.seed, D9): scn.seed once per run
    scenario_settings: dict[str, Any] | None = None  # run --scenario-file: {difficulty, karmic_dice, timing} (check_settings)


# claude --autocompact accepts 100k-1M tokens. Context-cost model (tools/logs/context_model.py, S021-S026): a
# compaction is a ~1-2 min summary stall (a >10 s gap) and a generic summary, so shorter sessions with our own
# handoff are the default lever; this is an opt-in safety net / A-B switch (e.g. 350000 with 30-min sessions).
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000


def make_client(opt: Options) -> clients.Client:
    """The play client: the bg3 stdio server of play/.mcp.json, or with --sandbox container the http one on the
    sandbox network's gateway (the client runs in the container)."""
    kw = {"profile": opt.profile, "system_file": opt.system_file} if opt.client == "pi" else {}
    if opt.sandbox == "container":
        return clients.make_http(opt.client, sandbox.URL, **kw)
    return clients.make(opt.client, **kw)


def sandbox_server_env(base_env: dict, req: clients.Request) -> dict:
    """The env of the http bg3 server of one sandboxed play process: what the stdio server would get (the runner's
    env, play/.mcp.json's, the session's) with the rules served by the server (the container has no play/)."""
    return {**base_env, **(clients.bg3_server().get("env") or {}), **req.env, "BG3AI_SERVE_RULES": "1"}


def session_overrides(path: Path) -> dict:
    """{"model", "effort", "minutes"} from runtime/sessions/<pt>/session-config.json, re-read before every session
    so the operator can retune a long run without restarting it. Unknown keys and bad values are ignored."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    if isinstance(raw, dict):
        if isinstance(raw.get("model"), str) and raw["model"]:
            out["model"] = raw["model"]
        if raw.get("effort") in renv.EFFORTS:
            out["effort"] = raw["effort"]
        m = raw.get("minutes")
        if isinstance(m, (int, float)) and not isinstance(m, bool) and 0 < m <= 24 * 60:
            out["minutes"] = float(m)
        ac = raw.get("autocompact")
        if isinstance(ac, int) and not isinstance(ac, bool) and AUTOCOMPACT_MIN <= ac <= AUTOCOMPACT_MAX:
            out["autocompact"] = ac
    return out


@dataclass
class SessionResult:
    sid: str
    claude_session: str
    exit_code: int | None
    reason: str          # exited | hard_stop | crash | hang | agent_hang | aborted | killed | milestone | dry_run
    #                      | cap | api_limit (RUN-7: usage/rate limit, overload) | mcp_failed (RUN-8)
    #                      | context_overflow (pi: the conversation is full; the next session starts a new one)
    started: float
    ended: float
    notes: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    detail: str = ""     # what the runner saw, for a fault
    fault_at: float | None = None
    resets_at: float | None = None   # api_limit: when the client said the limit resets
    tool_calls: int = 0              # the process's tool calls (a context_overflow without any: at the kickoff)


class RunnerSessionHooks:
    """bg3harness.session.SessionHooks for one of a Runner's sessions."""

    def __init__(self, runner: rcore.SessionRunner, sid: str, minutes: float):
        self.r, self.sid, self.minutes = runner, sid, minutes

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool,
                  tag: str) -> SessionResult:
        res = self.r.run_claude(self.sid, prompt, minutes, client_session, resume=resume, tag=tag)
        if not tag and res.reason not in FAULTS and self.r.state["oom_streak"]:
            self.r.state["oom_streak"] = None   # a whole session from that save without dying: not an OOM loop
        return res

    def session_end(self, planned_end: float) -> float:   # a fight on at WRAP UP held the clock
        return rpolicy.held_deadline(rpolicy.fight_hold_path(self.r.state.dir, self.sid), planned_end)

    def stop_check(self, session_end: float) -> str | None:
        return self.r.stop_check(self.sid, session_end, self.minutes)

    def game_ok(self) -> bool:
        return self.r.game_alive() and game.game_state(renv.HEALTH_EVAL_TIMEOUT) is not None

    def recover(self, why: str) -> RecoverFailure | None:
        try:
            self.r.recover(why)
        except LaunchGaveUp as e:
            return RecoverFailure(e, fatal=True)
        except rcode.SETUP_ERRORS as e:
            return RecoverFailure(e, fatal=False)
        return None

    def loaded(self) -> str | None:
        return self.r.state["loaded_save"]

    def aborted(self) -> bool:
        return self.r.abort

    def stop_requested(self) -> bool:
        return self.r.stop_requested or (renv.RUNTIME / "STOP").exists()

    def notes(self) -> list[str]:
        notes = list(self.r.pending_notes)
        self.r.pending_notes.clear()
        return notes

    def event(self, kind: str, **fields) -> None:
        self.r.event(kind, **fields)

    # play's globals looked up on each call (tests patch them)
    resume_prompt = staticmethod(lambda why, notes: rpolicy.resume_prompt(why, notes))
    agent_resume_prompt = staticmethod(lambda notes: rpolicy.agent_resume_prompt(notes))
    stop_resume_prompt = staticmethod(lambda why: rpolicy.stop_resume_prompt(why))
    log = staticmethod(lambda msg: renv.log(msg))
    now = staticmethod(lambda: time.time())


class RunnerGameHost:
    """bg3harness.game.controller.GameHost for a Runner: its runner.json, log, events and hooks; the module's paths
    and guards read on each call (tests patch play)."""

    def __init__(self, runner: rcore.SessionRunner):
        self.r = runner

    runtime = property(lambda self: renv.RUNTIME)
    launch_sh = property(lambda self: renv.LAUNCH_SH)
    mod_live = property(lambda self: rcode.MOD_LIVE)
    stale_exit = property(lambda self: bg3le_buildinfo.STALE_EXIT)
    rebuild_hint = property(lambda self: bg3le_buildinfo.REBUILD)
    state = property(lambda self: self.r.state)

    def log(self, msg: str) -> None:
        renv.log(msg)

    def event(self, kind: str, **fields) -> None:
        self.r.event(kind, **fields)

    def status(self, **fields) -> None:
        write_status(**fields)

    def steam_ready(self) -> tuple[bool, str]:
        return rcode.steam_ready()

    def bg3le_problems(self) -> list[str]:
        return rcode.bg3le_problems()

    def setup_env(self) -> dict:
        return self.r.setup_env()

    def refresh_mod_snapshot(self) -> list[str]:
        return self.r.refresh_mod_snapshot()

    def mod_hashes(self) -> dict[str, str]:
        return rcode.mod_tree_hashes()

    def aborted(self) -> bool:
        return bool(getattr(self.r, "abort", False))

    def own_saves(self) -> list[Save]:
        return self.r.own_saves()

    def previous_good_save(self, failed: set[str]) -> Save | None:
        return self.r.previous_good_save(failed)

    def read_actions_since(self, t: float) -> list[dict]:
        return rpolicy.read_actions_since(t)

    def note(self, text: str) -> None:
        self.r.pending_notes.append(text)
