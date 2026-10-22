# pyright: basic
"""SessionRunner: one playthrough directory's game, agent processes and checks (moved from runner/play.py,
Phase D.3). The playthrough app's parts (the memory session, the keeper, the kickoff, the continuous loop) and the
legacy series' parts (run records, resets) are not here: SessionRunner calls them through PlaythroughParts, whose
defaults do nothing, and runner/legacy_playthrough.py's Runner supplies them until bg3play does
(research/ovh/harness.md "for app, Phase D.2").
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from bg3ai import progress as story

from bg3harness import clients
from bg3harness.game import io as game
from bg3harness.game.io import Save
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import game_side as rgame
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate


class SessionRunner(rgame.GameSide, ragent.AgentSide, rchecks.ChecksSide):
    api_limits = 0                     # RUN-7: API limits in a row (the back-off doubles per repeat)
    cap_hit: dict | None = None        # --max-cost/--max-tokens: {cap, cost, tokens, why} once the run reached one
    run_started: float | None = None   # --until: the run's start (claude_totals counts from there)
    _client: clients.Client | None = None
    mod_rev: str | None = None         # the commit of this session's mod snapshot (MOD_LIVE)
    moved_to: str | None = None        # the main commit code_moved last logged

    def __init__(self, opt: rstate.Options):
        self.opt = opt
        self._client = rstate.make_client(opt)   # the CLI every play, resume and fix-notes process runs
        self.mem = renv.PLAYTHROUGHS / opt.playthrough / "memory"
        self.state = rstate.RunnerState(opt.playthrough)
        # --sandbox container: this playthrough's HOME for the client in the container (its sessions to resume)
        self.box = replace(opt.box, home=self.state.dir / "sandbox-home") if opt.box else None
        self._sandbox_live: tuple | None = None   # (server, container) of the play process now running
        self.profile_path = renv.PLAYTHROUGHS / opt.playthrough / "profile.json"
        self.profile = rpolicy.load_profile(self.profile_path)
        if opt.character_brief or opt.roleplay or opt.difficulty:
            self.set_profile(rpolicy.update_profile(self.profile, opt.character_brief, opt.roleplay, opt.difficulty))
        owner = opt.save_owner or os.environ.get("BG3AI_SAVE_OWNER")
        if owner and self.state["save_owner"] != owner:
            if self.state["save_owner"]:
                renv.log(f"save owner for {opt.playthrough}: {self.state['save_owner']} -> {owner}")
            if not opt.dry_run:
                self.state["save_owner"] = owner
            else:
                self.state.data["save_owner"] = owner
        self.stop_requested = False
        self.abort = False
        self.stop_ends_session = False   # a bench run (run_host): an operator stop ends the session it is in
        self.agent_proc: subprocess.Popen | None = None   # the play process while a session runs
        # the env the play process, its bg3 server and the play settings read; None: this process's (a bench run's
        # host passes the job's own, so nothing of one job's env reaches the next through os.environ)
        self.run_env: dict[str, str] | None = None
        self.pending_notes: list[str] = []
        self.relaunch_save: Save | None = None   # maybe_relaunch -> ensure_game: the save the relaunch loads
        self.quick_failures = 0
        self.new_game = False
        self.gates: bool | None = None       # gates_decision for the game this session plays (BG3AI_SAVE_GATES)
        self.until_hit: dict | None = None   # the --until milestone, once the progress poller saw it reached
        self.cost_pause_since: float | None = None   # --max-cost-per-day: paused since (epoch s)
        self.last_triage: tuple[str, Path] | None = None  # (session, Sxxx.triage.md) of the last claude run
        self.code_watch = rcode.CodeWatch()         # this process's repo code, for the self-restart between sessions
        self.bridge_hashes: dict | None = None  # bridge/src as the last session's MCP server ran it
        self.handover = rcode.read_handover()       # set when this process is the execv'd successor of the runner
        self.keeper_thread: threading.Thread | None = None  # memory keeper overlapping the between-session prep
        if self.handover:
            self.pending_notes = list(self.handover.get("pending_notes") or [])
            self.quick_failures = int(self.handover.get("quick_failures") or 0)
            self.new_game = bool(self.handover.get("new_game"))
            self.bridge_hashes = self.handover.get("bridge_hashes")
            lt = self.handover.get("last_triage")
            self.last_triage = (lt[0], Path(lt[1])) if lt else None
            if self.handover.get("series_end"):
                self.opt.series_end = self.handover["series_end"]
        self.outcome: str | None = None      # how run() ended: reached|timeout|gave_up|runner_error|stopped (--until)
        self.fatal = False                   # run() ended on something a next --runs run can't fix (needs a human)
        self.gamectl.resume_watch()   # the game a previous run of the runner launched

    def set_profile(self, profile: dict) -> None:
        """Write profile.json (not in a dry run) and use it."""
        if profile == self.profile:
            return
        if self.state["history"]:
            renv.log(f"profile.json changed for {self.opt.playthrough}, which already played: the character brief only "
                "matters for a new game (phase 0)")
        self.profile = profile
        if self.opt.dry_run:
            return
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.profile_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(profile, indent=1, ensure_ascii=False) + "\n")
            tmp.replace(self.profile_path)
            renv.log(f"profile written: {self.profile_path}")
        except OSError as e:
            renv.log(f"profile write failed: {e}")

    def adopt_owner(self, owner: str) -> None:
        """I3: the character's name (from this playthrough's first Sxxx- save) becomes its save owner: runner.json,
        and profile.json's character.name; the MCP server's prefix follows (save_identity)."""
        if self.opt.dry_run:
            self.state.data["save_owner"] = owner
            return
        self.state["save_owner"] = owner
        renv.log(f"save owner for {self.opt.playthrough}: {owner} (learned from its saves)")
        c = self.profile.get("character")
        if c and not c.get("name"):
            self.set_profile(dict(self.profile, character=dict(c, name=owner)))

    @property
    def environ(self) -> dict[str, str] | os._Environ[str]:
        """run_env, or this process's env (also for a bare Runner a test builds without __init__)."""
        env = getattr(self, "run_env", None)
        return os.environ if env is None else env

    def event(self, kind: str, **fields) -> None:
        """runtime/sessions/<pt>/runner_events.jsonl: runner start/stop, faults, recoveries, refused launches
        (the dashboard's unattended hours and crash/recovery counts). Never breaks the runner. During a play
        process also in its Sxxx.events.jsonl (src "harness")."""
        try:
            with open(self.state.dir / "runner_events.jsonl", "a") as f:
                f.write(json.dumps({"t": round(time.time(), 1), "kind": kind, **fields}) + "\n")
        except OSError as e:
            renv.log(f"event log failed: {e}")
        if (evlog := getattr(self, "events_log", None)) is not None:
            evlog.harness(kind, **fields)

    def end_run_game(self, when: str = "between runs", after: str = "the next run launches a new one") -> None:
        """--runs, between runs: stop the game the run played, so the next run starts a new game in a fresh process
        (the mod's Lua state, the kill list in memory included). Also at the end of a series or an --until run
        (when="series end" / "run end"; --keep-game skips that): naut2 left its last run's game running
        (bg3ai-game-20261017-133954.scope). A game the runner doesn't own is left alone."""
        if not game.port_open() and not self.owns_game():
            return
        if not (self.owns_game() or self.opt.take_over):
            renv.log(f"{when}: the game up is not one this runner launched; left running"
                + (" (the next run needs the main menu)" if when == "between runs" else ""))
            return
        try:
            self.kill_game()
            renv.log(f"{when}: game stopped; {after}")
        except Exception as e:
            renv.log(f"{when}: stopping the game failed: {e}")

    def write_progress(self, session: str, base: Path, start: float, end: float) -> dict | None:
        """Sxxx.progress.json + a line in progress_history.jsonl."""
        try:
            summary = story.summarize(story.load(Path(f"{base}.progress.jsonl")), start, end)
            story.write_summary(summary, Path(f"{base}.progress.json"), self.state.dir / "progress_history.jsonl", session)
        except Exception as e:
            renv.log(f"{session}: progress summary failed: {e}")
            return None
        renv.log(f"{session}: story progress {summary['points']} pts ({summary['per_hour']}/h), {summary['events']} events, "
            f"longest stretch without progress {summary['longest_no_progress_min']} min, {summary['stuck_episodes']} stuck episode(s)")
        return summary

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def wait_api_limit(self, res: rstate.SessionResult) -> None:
        """RUN-7: the play client hit a usage/rate limit or an overload: wait (api_backoff_s: until its reset time,
        else doubling from API_BACKOFF_MIN_S) and let the run loop start the next session. Never a give-up; an
        operator stop (signal, runtime/STOP) ends the wait at once."""
        wait = renv.api_backoff_s(res.resets_at, time.time(), self.api_limits)
        until = time.time() + wait
        msg = (f"API limit ({res.detail[:200]}); waiting {wait / 60:.0f} min until "
               f"{datetime.fromtimestamp(until):%H:%M}" + (" (its reset time)" if res.resets_at else "")
               + f", limit {self.api_limits} in a row; the run continues by itself")
        renv.log(msg)
        self.event("api_limit", detail=res.detail[:300], wait_s=round(wait), resets_at=res.resets_at,
                   repeat=self.api_limits)
        rstate.write_status(alert={"t": time.time(), "kind": "api_limit", "msg": msg[:600]})
        while time.time() < until and not (self.stop_requested or self.abort or (renv.RUNTIME / "STOP").exists()):
            time.sleep(min(15.0, max(until - time.time(), 0.0)))
        rstate.write_status(alert=None)

    def session_minutes(self) -> float:
        """--session-minutes, or session-config.json "minutes" (re-read before every session: switch 20 ↔ 60 live)."""
        want = rstate.session_overrides(self.state.dir / "session-config.json").get("minutes", self.opt.session_minutes)
        if want < renv.MIN_SESSION_MINUTES and not self.opt.allow_short:
            renv.log(f"session-config.json minutes {want:g} ignored: below {renv.MIN_SESSION_MINUTES} "
                "(start the runner with --allow-short-sessions for that)")
            want = self.opt.session_minutes
        if want != getattr(self, "_last_minutes", want):
            renv.log(f"session length now {want:g} min (session-config.json)")
        self._last_minutes = want
        rstate.write_status(session_minutes=want)
        return want

    def code_update_check(self) -> None:
        """Before every session: log bridge edits (the MCP server restarts per session, nothing else to do), and
        raise RestartRunner when the runner's own loaded code changed on disk and the new code imports."""
        cur = rcode.tree_hashes(rcode.BRIDGE_SRC)
        if self.bridge_hashes is not None:
            changed = rcode.changed_files(self.bridge_hashes, cur)
            if changed:
                renv.log(f"bridge code changed since last session: {', '.join(changed[:8])}"
                    + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else ""))
        self.bridge_hashes = cur
        stale = self.code_watch.changed()
        if not stale or self.stop_requested or (renv.RUNTIME / "STOP").exists():
            return
        names = [str(p.relative_to(renv.REPO)) for p in stale]
        try:
            p = subprocess.run(rcode.code_check_cmd(renv.REPO / "runner" / "play.py"), capture_output=True, text=True,
                               timeout=60, cwd=os.getcwd())
            ok, why = p.returncode == 0, (p.stderr or p.stdout).strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, why = False, [str(e)]
        if not ok:
            # Keep playing on the code that works; try again once these files change again.
            renv.log(f"runner code changed ({', '.join(names)}) but the new code fails to import: {why[0][:300]}; "
                "keeping the running code")
            self.event("runner_restart_refused", files=names, error=why[0][:300])
            for f in stale:
                self.code_watch.hashes[f] = rcode._sha1(f)
            return
        renv.log(f"runner code changed: restarting ({', '.join(names)})")
        self.join_keeper()
        raise rcode.RestartRunner(names)

    def handover_data(self, run_end: float | None, run_started: float) -> dict:
        return {"run_end": run_end, "run_started": run_started, "pending_notes": self.pending_notes,
                "quick_failures": self.quick_failures, "new_game": self.new_game,
                "bridge_hashes": self.bridge_hashes,
                "series_end": getattr(getattr(self, "opt", None), "series_end", None),
                "last_triage": [self.last_triage[0], str(self.last_triage[1])] if self.last_triage else None}

    def run_end(self, now: float) -> float | None:
        """When this run stops starting sessions: the earliest of --hours (one run), the series' end (--runs
        --hours) and --run-hours from now."""
        ends = [t for t in (now + self.opt.hours * 3600 if self.opt.hours else None, self.opt.series_end,
                            now + self.opt.run_hours * 3600 if self.opt.run_hours else None) if t]
        return min(ends) if ends else None

    def session_hooks(self, sid: str, minutes: float) -> rstate.RunnerSessionHooks:
        """bg3harness.session's hooks for this runner's session `sid` (run_session calls back through them)."""
        return rstate.RunnerSessionHooks(self, sid, minutes)

    # ---------------------------------------------------------------- PlaythroughParts (defaults: none)

    def handoff_session(self, text: str) -> int | None:
        """The session that wrote the memory's HANDOFF.md (its save choice prefers that session's saves)."""
        return None

    def lsf_attributes(self) -> Callable[[bytes], list]:
        """tools/saves/lsf's reader for the profile's config.lsf (the settings readout); without one that entry is
        unknown, like an unreadable file."""

        def missing(_data: bytes) -> list:
            raise OSError("no LSF reader")

        return missing

    def join_keeper(self) -> None:
        """Wait for the memory keeper of the session before (the playthrough's); nothing without one."""

    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None:
        """Why the agent may not end its session yet (the playthrough's memory wrap-up), else None: none here."""
        return None


class PlaythroughParts(Protocol):
    """What a playthrough adds to SessionRunner (runner/legacy_playthrough.py's Runner today, bg3play next): the
    memory's HANDOFF session, the LSF reader for the settings readout, the keeper join before a restart, the stop
    check (the memory's wrap-up). SessionRunner's defaults do nothing, so a bench run plays without them.

    Not called by SessionRunner (the app's own loop): the memory session (open/close, session-end), the keeper's
    start and recovery, the plan-line check, the session review, a playthrough session (play_session: kickoff,
    run_session, history) and the continuous loop (run). runner/legacy_playthrough.py has all of them today."""

    def handoff_session(self, text: str) -> int | None: ...
    def lsf_attributes(self) -> Callable[[bytes], list]: ...
    def join_keeper(self) -> None: ...
    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None: ...
