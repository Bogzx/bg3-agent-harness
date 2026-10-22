"""One playthrough session (moved from runner/legacy_playthrough.py, Phase D.2):

    the mod snapshot, a relaunch if due, the game up with the playthrough's save (ensure_game)
    the notes: scenario start, relaunch, Game Over, the stuck directive, the plan check's
    hot reload, play settings, the settings check, check_all, the keeper joined, the plan check
    session.py start (the memory briefing) -> build_kickoff -> run_session (bg3harness.session)
    session-end, the memory commit (or a fix-notes session), the plan check, the keeper started
    the history entry in runner.json (progress, boundary, early exit, story points)

plan_dry_run prints what the next session would do, touching neither the game nor the memory.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace

from bg3harness.game import io as game
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import GAME_FAULTS, Recovery, SessionSpec, run_session

from . import prompts


def relaunch_note(why: str, loaded: str | None) -> str:
    return (
        f"RUNNER NOTE: the runner relaunched the game before this session ({why}) and loaded "
        f'"{loaded or "the newest save"}". long_rest / region fast travel ready checks should work again.'
    )


GAME_OVER_NOTE = (
    "RUNNER NOTE: the game is on the Game Over screen. Load your most recent save "
    "(load_game) and log the reload with its cause."
)


class SessionSide:
    def plan_dry_run(self, minutes: float) -> rstate.SessionResult:
        """Print what the next session would do; touches neither the game nor the memory."""
        save = self.choose_save()
        sid = self.next_sid()
        print(f"session:        {sid} ({minutes:.0f} min)")
        print(f"memory:         {self.mem}")
        print(
            f"save to load:   {save.folder if save else '(none: new game from the main menu)'}"
            + (f" ({self.save_choice[1]})" if save and self.save_choice else "")
        )
        print(f"game:           {'up, state ' + str(game.game_state()) if game.port_open() else 'not running'}")
        if save and self.opt.take_over and game.port_open():
            touched = self.foreign_use(save)
            what = "reload " + save.label + " (" + touched + ")" if touched else "play on from the running game"
            print(f"take-over:      {what}")
        print(f"stuck:          {rpolicy.is_stuck(self.state['history'])}")
        k = self.opt
        keeper = (
            f"on ({rpolicy.keeper_model(k.keeper_model, self.model_for()[0])}, effort {k.keeper_effort}, "
            f"≤{k.keeper_minutes:g} min) after each session"
            if k.keeper
            else "OFF (--no-keeper)"
        )
        print(f"memory keeper:  {keeper}")
        print(
            "plan check:     tools/dev/check_plans.py at each session end (→ keeper) and in the prep "
            "(→ RUNNER NOTE); now:"
        )
        self.check_plan_lines("dry run", dry=True)
        prompt = prompts.build_kickoff(
            sid,
            self.opt.playthrough,
            save.label if save else None,
            list(self.pending_notes),
            save_why=self.loaded_why(save.label if save else None),
            pace=self.kickoff_pace(),
        )
        return self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))

    def play_session(self, minutes: float, first: bool) -> rstate.SessionResult:
        if self.opt.dry_run:
            return self.plan_dry_run(minutes)
        try:
            self.refresh_mod_snapshot()  # before a relaunch (it loads the snapshot) and the hot reload
        except OSError as e:
            renv.log(f"mod snapshot refresh failed: {e}; the game keeps {self.mod_rev}'s mod")
        relaunched = self.maybe_relaunch(first, minutes)
        loaded = self.ensure_game(first)
        if self.opt.start_save and not any(h.get("kind") == "play" for h in self.state["history"] or []):
            self.pending_notes.append(prompts.scenario_start_note(self.opt.scenario, self.profile, loaded))
        if relaunched:
            self.pending_notes.append(relaunch_note(relaunched, loaded))
        notes = list(self.pending_notes)
        self.pending_notes.clear()

        screen = game.ui_screen()
        at_game_over = screen == "game_over"
        last = (self.state["history"] or [{}])[-1]
        if at_game_over and last.get("started_at_game_over"):
            renv.log("second session in a row starting at Game Over: the runner loads the latest save")
            self.kill_game()
            loaded = self.load_save(self.choose_save())
            at_game_over = False
        elif at_game_over:
            notes.append(GAME_OVER_NOTE)
        if rpolicy.is_stuck(self.state["history"]):
            notes.append(prompts.stuck_directive(self.state["history"]))

        try:
            self.hot_reload_mod()  # before the settings: a reset wipes the mod's client-side config
        except RuntimeError:
            raise  # the mod is gone: let session setup recover (restart the game)
        except Exception as e:
            renv.log(f"hot reload failed: {e}")
        try:
            self.apply_play_settings()
        except Exception as e:
            renv.log(f"play settings failed: {e}")
        self.check_settings()  # run --scenario-file: a contradiction stops the run
        try:
            self.between_sessions_check()  # after the reload: checks the Lua the next session plays with
        except Exception as e:
            renv.log(f"check_all failed: {e}")
        self.join_keeper()  # the keeper must be done (and committed) before the player touches memory
        bad = self.check_plan_lines("prep")  # after the keeper: what is still invalid goes to the player
        if bad:
            notes.append(rcode.plan_note(bad))
        notes += self.pending_notes
        self.pending_notes.clear()
        predicted = self.next_sid()
        start_sig = game.read_progress(renv.log)
        deadline = time.time() + minutes * 60
        opened, briefing = self.open_memory_session(deadline, minutes)  # was the SessionStart hook
        sid = opened or predicted
        if opened and opened != predicted:
            renv.log(f"session.py start opened {opened} (predicted {predicted})")
        prompt = prompts.build_kickoff(
            sid, self.opt.playthrough, loaded, notes, briefing, self.loaded_why(loaded), self.kickoff_pace()
        )
        recovery = Recovery(renv.MAX_RESUMES, renv.RESUME_MIN_MINUTES, renv.MAX_STOP_RESUMES, renv.HARD_STOP_GRACE)
        out = run_session(
            SessionSpec(sid, prompt, minutes, str(uuid.uuid4()), recovery),
            rstate.RunnerSessionHooks(self, sid, minutes),
        )
        res, resumes, stop_resumes, gave_up = out.result, out.resumes, out.stop_resumes, out.gave_up
        if res.reason == "dry_run":
            return res
        hold = rpolicy.fight_hold_path(self.state.dir, sid)
        session_start, session_end = out.started, rpolicy.held_deadline(hold, out.started + minutes * 60)
        crashed = res.reason in GAME_FAULTS
        res = replace(res, started=session_start)
        if not crashed:
            self.ensure_save(res.started)
        try:  # was the SessionEnd hook: commit a finished wrap-up, else log that the session ended before it
            self.session_cmd(["session-end"], self.session_env(session_end, minutes, sid))
        except OSError as e:
            renv.log(f"{sid}: session-end failed: {e}")
        self.close_memory_session(res)
        plan_bad = [] if crashed else self.check_plan_lines(f"{sid} end")  # the keeper rewrites invalid lines
        self.start_keeper(sid, res, plan_bad)
        try:
            if got := self.record_agent_saves(res.started):
                renv.log(f"{sid}: the agent's quicksave(s) {', '.join(got)} are this playthrough's saves too")
        except Exception as e:
            renv.log(f"{sid}: agent saves not recorded: {e!r}")
        end_sig = game.read_progress(renv.log)
        entry = {
            "kind": "play",
            "session": sid,
            "claude_session": res.claude_session,
            "reason": res.reason,
            "exit": res.exit_code,
            "started": res.started,
            "ended": res.ended,
            "model": res.model,
            "effort": res.effort,
            "keeper_model": rpolicy.keeper_model(self.opt.keeper_model, res.model or self.opt.model)
            if self.opt.keeper
            else None,
            "stop_resumes": stop_resumes,
            "minutes": round((res.ended - res.started) / 60, 1),
            "start_sig": start_sig,
            "end_sig": end_sig,
            "progress": None,
            "progress_why": "crashed",
            "started_at_game_over": screen == "game_over",
            "resumes": resumes,
            "story_points": rpolicy.story_points(self.state.dir / f"{sid}.progress.json"),
        }
        if not crashed:
            entry["progress"], entry["progress_why"] = rpolicy.session_progress(
                start_sig, end_sig, entry["story_points"]
            )
        renv.log(f"{sid}: session progress {entry['progress']} ({entry['progress_why']})")
        b = rpolicy.boundary_s(self.state["history"], res.started)
        if b is not None:
            entry["boundary_s"] = b
        early = rpolicy.early_exit_minutes(res.reason, res.started, res.ended, minutes)
        if early is not None:
            entry["early_exit_min"] = early
            renv.log(
                f"{sid}: early exit: claude ended with {early:g} of {minutes:g} min left (exit {res.exit_code}); "
                "starting the next session now" + (f" (boundary before this session: {b:g} s)" if b is not None else "")
            )
        self.state["history"] = [*self.state["history"], entry]
        self.new_game = False  # from here on the game has a character: confirm by name
        if rpolicy.is_stuck(self.state["history"]):
            renv.log(
                f"stuck detector: {renv.STUCK_SESSIONS} sessions without progress; next kickoff says change approach"
            )
        if gave_up is not None:
            raise gave_up
        return res
