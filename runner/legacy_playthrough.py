"""The playthrough parts of the legacy runner (moved from runner/play.py, Phase D.3) and Runner, the class
`play.py run` plays with: SessionRunner (bg3harness.runner) + these parts + the legacy series' (legacy_series.py).

Playthrough-only (bg3harness.runner.core.PlaythroughParts; bg3play implements them next, Phase D.2): the memory
session (tools/memory/session.py start/stop/session-end), the HANDOFF's session, the memory keeper between sessions,
the plan-line check, the session review, a session (play_session: kickoff, run_session, the history entry) and the
continuous loop (run).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import _harness  # noqa: F401  (packages/harness/src and bridge/src on sys.path)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "memory"))  # memlib, new_playthrough

import legacy_series as lser
import runs
from bg3harness import bg3le_buildinfo
from bg3harness.game import io as game
from bg3harness.game.io import Bg3leStale, LaunchBlocked, LaunchGaveUp, SteamNotOffline
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import core as rcore
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.scenario_file import SettingsMismatch
from bg3harness.session import GAME_FAULTS, Recovery, SessionSpec, run_session

SESSION_PY = renv.REPO / "tools" / "memory" / "session.py"  # the memory session (bg3play.memory.session)
REVIEW_PY = renv.REPO / "tools" / "logs" / "review_session.py"


def parse_handoff_session(text: str) -> int | None:
    """The session that wrote HANDOFF.md: N of its '# Handoff SN → SN+1' heading (memlib.HANDOFF_RE), or None."""
    import memlib as M
    m = M.HANDOFF_RE.search(re.sub(r"<!--.*?-->", "", text, flags=re.S))
    return int(m.group(1)) if m else None


class PlaythroughSide:
    def check_memory(self) -> None:
        from new_playthrough import SetupError, link_play_memory
        if not self.mem.is_dir():
            raise SystemExit(f"no memory at {self.mem}; create it with: "
                             f"python3 tools/memory/new_playthrough.py {self.opt.playthrough}")
        link = renv.PLAY_DIR / "memory"
        if not (link.is_symlink() and link.resolve() == self.mem.resolve()) and not self.opt.dry_run:
            try:
                prev = link_play_memory(renv.PLAY_DIR, self.mem)
            except SetupError as e:
                raise SystemExit(str(e)) from e
            renv.log(f"play/memory → {self.mem} (was {prev})")

    def memory_state(self) -> dict:
        import memlib as M
        return M.load_state(self.mem)

    def next_sid(self) -> str:
        import memlib as M
        reuse = M.reusable_session(self.mem)  # an empty killed session's id is reused by session.py start
        if reuse:
            return rpolicy.session_id(reuse)
        return rpolicy.session_id(rpolicy.predict_session_number(M.session_numbers(self.mem), self.memory_state().get("current")))

    def session_py(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
                              capture_output=True, text=True, cwd=renv.PLAY_DIR, stdin=subprocess.DEVNULL)

    def session_env(self, deadline: float, minutes: float, sid: str | None = None) -> dict:
        """The clock env session.py sees for a session (what run_claude gives claude and the MCP server)."""
        env = dict(os.environ, BG3AI_SESSION_DEADLINE=str(int(deadline)),
                   BG3AI_SESSION_WRAPUP=str(int(deadline - rpolicy.wrapup_minutes(minutes) * 60)),
                   BG3_SESSION_MINUTES=str(minutes), BG3_WRAP_MINUTES=str(rpolicy.wrap_minutes(minutes)),
                   BG3AI_KEEPER="1" if self.opt.keeper and not self.opt.dry_run else "0")
        if sid and getattr(self, "state", None) is not None:   # the playthrough state comes with run()
            env["BG3AI_FIGHT_HOLD_PATH"] = str(rpolicy.fight_hold_path(self.state.dir, sid))
        return env

    def session_cmd(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        """session.py <args> with the session's clock env; never reads the runner's stdin."""
        return subprocess.run([sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
                              capture_output=True, text=True, cwd=renv.PLAY_DIR, env=env, stdin=subprocess.DEVNULL)

    def open_memory_session(self, deadline: float, minutes: float) -> tuple[str | None, str]:
        """session.py start (was the SessionStart hook): opens sessions/Sxxx.md. Returns (sid, the memory briefing for
        the kickoff), or (None, "") when it couldn't (the agent then calls session_start)."""
        try:
            r = self.session_cmd(["start"], self.session_env(deadline, minutes))
            st = self.memory_state()
        except (OSError, ValueError) as e:
            renv.log(f"memory session not opened: {e}")
            return None, ""
        if r.returncode != 0 or not st.get("open") or not st.get("current"):
            renv.log(f"memory session not opened (session.py start exit {r.returncode}): {(r.stdout or r.stderr)[-300:]}")
            return None, ""
        return rpolicy.session_id(st["current"]), r.stdout.strip()

    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None:
        """session.py stop after claude ended its turn (was the Stop hook): the reason the session can't end yet
        (keep playing, write the handoff, fix lint), or None (it ended; on success the memory is committed)."""
        try:
            r = self.session_cmd(["stop"], self.session_env(deadline, minutes, sid))
            out = json.loads(r.stdout) if r.stdout.strip() else {}
        except (OSError, ValueError) as e:
            renv.log(f"{sid}: stop check failed: {e}")
            return None
        if out.get("systemMessage"):
            renv.log(f"{sid}: stop check: {out['systemMessage']}")
        return out.get("reason") if out.get("decision") == "block" else None

    def close_memory_session(self, res: rstate.SessionResult) -> None:
        st = self.memory_state()
        if not st.get("open"):
            renv.log(f"{res.sid}: memory already committed (end_session or the stop check)")
            return
        r = self.session_py("end")
        if r.returncode == 0:
            renv.log(f"{res.sid}: memory committed by session.py end")
            return
        if rcode.until_stop_handoff_only(res.reason, bool(self.opt.until or self.until_hit), r.stdout):
            # The --until stop ends the session at the milestone, before any wrap-up, so HANDOFF.md is still the
            # previous session's: that alone is no notes problem (naut/naut2: a 14 s fix pass + the wait every run).
            # The keeper rewrites HANDOFF; a later session start reconciles the open session.
            renv.log(f"{res.sid}: memory lint: only the handoff check failed (the --until stop came before the wrap-up): "
                "no fix-notes session; memory left open (the keeper writes the handoff)")
            self.event("fix_notes_skipped", session=res.sid, why="until_milestone_handoff_only")
            return
        if (res.reason in ("exited", "hard_stop", "milestone") and res.claude_session
                and res.ended - res.started >= 60 and not (self.stop_requested or self.abort)):   # RUN-7: <1 min: no
            renv.log(f"{res.sid}: memory lint failed; running a {renv.FIX_SESSION_MINUTES}-minute fix-notes session")
            self.run_claude(res.sid, rpolicy.fix_notes_prompt(r.stdout), renv.FIX_SESSION_MINUTES, res.claude_session,
                            resume=True, tag="-fix", needs_game=False)
        if not self.memory_state().get("open"):
            return
        r = self.session_py("end")
        if r.returncode == 0:
            renv.log(f"{res.sid}: memory committed after the fix-notes session")
        elif res.reason in GAME_FAULTS + ("killed", "aborted"):
            # The game died before the agent's wrap-up, so the end lint's HANDOFF check ("written by the current
            # session") can't pass and no fix pass runs. session.py start reconciles the open session (S014: the
            # note below told the next session to rewrite HANDOFF as the crashed one). Pass on only other errors.
            other = rcode.memory_lint_errors(r.stdout, skip=("handoff",))
            why = f"a game {res.reason}" if res.reason in GAME_FAULTS else f"claude {res.reason}"
            renv.log(f"{res.sid}: ended by {why} before wrap-up; memory left open for the next session"
                + (f" ({len(other)} other lint error(s))" if other else ""))
            if other:
                self.pending_notes.append(
                    f"RUNNER NOTE: {res.sid} ended by {why} and its memory is uncommitted. Besides "
                    "the missing handoff (the session ended before wrap-up), fix these lint errors early:\n"
                    + "\n".join(other)[-2500:])
        else:
            renv.log(f"{res.sid}: memory still fails lint; left open for the next session to reconcile")
            self.pending_notes.append(
                f"RUNNER NOTE: memory lint still failed after {res.sid}; the session was left uncommitted. "
                "Fix these lint errors early in this session:\n" + r.stdout[-2500:])

    def check_plan_lines(self, when: str, dry: bool = False) -> list[dict]:
        """tools/dev/check_plans.py (reads only, ~3 s): the stored plan lines for each member's next 2 levels vs the
        game's Progression offers. Logs one line; returns the invalid lines (keeper refused-line format). Needs the
        game Running with the party loaded and no bridge in use (between sessions). Never raises."""
        if (self.opt.dry_run and not dry) or not game.port_open():
            return []
        try:
            if game.game_state() != "Running":
                return []
            json_path = renv.RUNTIME / "check_plans.json"
            json_path.unlink(missing_ok=True)
            env = dict(os.environ, BG3AI_PLAYTHROUGH=self.opt.playthrough)
            p = subprocess.run(rcode.check_plans_cmd(json_path, self.mem), capture_output=True, text=True, cwd=renv.REPO,
                               env=env, timeout=renv.CHECK_PLANS_TIMEOUT)
            rows = json.loads(json_path.read_text())
        except Exception as e:
            tail = (getattr(locals().get("p"), "stderr", "") or "")[-300:]
            renv.log(f"plan check ({when}) skipped: {type(e).__name__}: {e} {tail}".rstrip())
            return []
        text, bad = rcode.plan_check_result(rows)
        renv.log(f"{when}: {text}")
        self.event("plan_check", when=when, checked=len(rows), invalid=[f"{b['name']} L{b['level']}" for b in bad])
        return bad

    def start_keeper(self, sid: str, res: rstate.SessionResult, plan_bad: list[dict] | None = None) -> None:
        """Starts the memory-keeper pass for the session that just ended (runner/memory_keeper.py). It runs in a
        thread while the runner prepares the next session (game check, mod reload, settings, check_all: none of
        them write memory) and is joined before the next player starts, so it never races the player's edits."""
        if not self.opt.keeper or self.opt.dry_run or res.ended - res.started < 120 or getattr(self, "cap_hit", None):
            if plan_bad:   # no keeper pass to fix them: the next player gets the note
                self.pending_notes.append(rcode.plan_note(plan_bad))
            if not self.opt.dry_run:
                self.write_review(sid)
            return
        import memory_keeper as K
        n = int(sid[1:])
        base = self.state.dir / f"{sid}-keeper"
        model = rpolicy.keeper_model(self.opt.keeper_model, res.model or self.opt.model)
        effort, minutes = self.opt.keeper_effort, self.opt.keeper_minutes
        client = self.opt.client

        def keep():
            renv.log(f"{sid}: memory keeper starting ({client} {model}, effort {effort or 'default'}, ≤{minutes:g} min)")
            try:   # stored level-up plan lines the game refused this session: the keeper rewrites them
                refused = K.refused_plans(rpolicy.read_actions_since(res.started))
            except Exception as e:
                refused = []
                renv.log(f"{sid}: refused plan lines not read: {e!r}")
            seen = {(x["name"].lower(), x["level"]) for x in refused}
            refused += [b for b in plan_bad or [] if (b["name"].lower(), b["level"]) not in seen]
            if refused:
                renv.log(f"{sid}: refused plan lines for the keeper: {', '.join(x['name'] + ' L' + str(x['level']) for x in refused)}")
            # the play client keeps the memory too (a benchmark compares one CLI end to end); claude stays on its
            # default pass with Claude Code's file tools, the others go through the bg3 server's keeper mode
            r = K.run_keeper(self.mem, n, base, model, effort, minutes, refused=refused, client=client,
                             **({"sandbox": box} if (box := getattr(self, "box", None)) else {}))
            renv.log(r.summary())
            self.event("memory_keeper", session=sid, status=r.status, seconds=r.seconds, files=r.files,
                       commit=r.commit, timed_out=r.timed_out, reverted=r.reverted, lint_errors=r.lint_errors[:5],
                       model=model, client=client)
            try:
                with open(self.state.dir / "keeper_history.jsonl", "a") as f:
                    f.write(json.dumps({"t": round(time.time(), 1), "client": client, "model": model, "effort": effort,
                                        **r.__dict__}) + "\n")
            except OSError as e:
                renv.log(f"keeper history write failed: {e}")

        def work():
            try:
                keep()
            finally:   # after the keeper: the review reads its memory_keeper event
                self.write_review(sid)

        self.keeper_thread = threading.Thread(target=work, name="memory-keeper", daemon=True)
        self.keeper_thread.start()

    def write_review(self, sid: str) -> Path | None:
        """Sxxx.review.md (tools/logs/review_session.py) for the logs worker, once the keeper pass is done (its
        memory/keeper section and the session's runner events are in by then; tools/logs/wait_new_session.sh wakes
        on it). Covers the session and its resumes. Niced, with a timeout; nothing here raises."""
        cmd = ["nice", "-n", "15", sys.executable, str(REVIEW_PY), self.state.dir.name, sid,
               "--runtime", str(renv.RUNTIME), "--sessions", str(self.state.dir.parent)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=renv.REPO, timeout=renv.REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            renv.log(f"{sid}: review killed after {renv.REVIEW_TIMEOUT} s")
            return None
        except OSError as e:
            renv.log(f"{sid}: review failed: {e}")
            return None
        if p.returncode != 0:
            renv.log(f"{sid}: review failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
            return None
        path = self.state.dir / f"{sid}.review.md"
        renv.log(f"{sid}: review {path}")
        return path

    def recover_keeper(self) -> None:
        """MEM-1: a keeper pass the runner died in (killed, crashed, restarted mid-pass) left its pre-pass snapshot:
        its process is killed and its unvalidated edits put back before any session builds on them."""
        if self.opt.dry_run:
            return
        import memory_keeper as K
        msg = K.recover(self.mem, self.state.dir)
        if msg:
            renv.log(f"memory keeper: {msg}")
            self.event("keeper_recovered", detail=msg)

    def join_keeper(self) -> None:
        t, self.keeper_thread = getattr(self, "keeper_thread", None), None
        if t is None:
            return
        if t.is_alive():
            t0 = time.time()
            t.join(self.opt.keeper_minutes * 60 + 60)  # run_keeper kills claude at its own deadline
            renv.log(f"waited {time.time() - t0:.0f} s for the memory keeper before the next session")
        # A session left open by failed lint (e.g. stale objectives) may pass now that the keeper updated the files.
        if self.memory_state().get("open"):
            r = self.session_py("end")
            if r.returncode == 0:
                renv.log("memory session committed after the keeper pass")

    def plan_dry_run(self, minutes: float) -> rstate.SessionResult:
        """Print what the next session would do; touches neither the game nor the memory."""
        save = self.choose_save()
        sid = self.next_sid()
        print(f"session:        {sid} ({minutes:.0f} min)")
        print(f"memory:         {self.mem}")
        print(f"save to load:   {save.folder if save else '(none: new game from the main menu)'}"
              + (f" ({self.save_choice[1]})" if save and self.save_choice else ""))
        print(f"game:           {'up, state ' + str(game.game_state()) if game.port_open() else 'not running'}")
        if save and self.opt.take_over and game.port_open():
            touched = self.foreign_use(save)
            print(f"take-over:      {'reload ' + save.label + ' (' + touched + ')' if touched else 'play on from the running game'}")
        print(f"stuck:          {rpolicy.is_stuck(self.state['history'])}")
        k = self.opt
        print(f"memory keeper:  {'on (' + rpolicy.keeper_model(k.keeper_model, self.model_for()[0]) + ', effort ' + str(k.keeper_effort) + ', ≤' + format(k.keeper_minutes, 'g') + ' min) after each session' if k.keeper else 'OFF (--no-keeper)'}")
        print("plan check:     tools/dev/check_plans.py at each session end (→ keeper) and in the prep (→ RUNNER NOTE); now:")
        self.check_plan_lines("dry run", dry=True)
        prompt = rpolicy.build_kickoff(sid, self.opt.playthrough, save.label if save else None, list(self.pending_notes),
                               save_why=self.loaded_why(save.label if save else None), pace=self.kickoff_pace())
        return self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))

    def play_session(self, minutes: float, first: bool) -> rstate.SessionResult:
        if self.opt.dry_run:
            return self.plan_dry_run(minutes)
        try:
            self.refresh_mod_snapshot()   # before a relaunch (it loads the snapshot) and the hot reload
        except OSError as e:
            renv.log(f"mod snapshot refresh failed: {e}; the game keeps {self.mod_rev}'s mod")
        relaunched = self.maybe_relaunch(first, minutes)
        loaded = self.ensure_game(first)
        if self.opt.start_save and not any(h.get("kind") == "play" for h in self.state["history"] or []):
            self.pending_notes.append(rpolicy.scenario_start_note(self.opt.scenario, self.profile, loaded))
        if relaunched:
            self.pending_notes.append(
                f"RUNNER NOTE: the runner relaunched the game before this session ({relaunched}) and loaded "
                f"\"{loaded or 'the newest save'}\". long_rest / region fast travel ready checks should work again.")
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
            notes.append("RUNNER NOTE: the game is on the Game Over screen. Load your most recent save "
                         "(load_game) and log the reload with its cause.")
        if rpolicy.is_stuck(self.state["history"]):
            notes.append(rpolicy.stuck_directive(self.state["history"]))

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
        self.check_settings()   # run --scenario-file: a contradiction stops the run
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
        opened, briefing = self.open_memory_session(deadline, minutes)   # was the SessionStart hook
        sid = opened or predicted
        if opened and opened != predicted:
            renv.log(f"session.py start opened {opened} (predicted {predicted})")
        prompt = rpolicy.build_kickoff(sid, self.opt.playthrough, loaded, notes, briefing, self.loaded_why(loaded),
                               self.kickoff_pace())
        out = run_session(SessionSpec(sid, prompt, minutes, str(uuid.uuid4()), Recovery(
            renv.MAX_RESUMES, renv.RESUME_MIN_MINUTES, renv.MAX_STOP_RESUMES, renv.HARD_STOP_GRACE)), rstate.RunnerSessionHooks(self, sid, minutes))
        res, resumes, stop_resumes, gave_up = out.result, out.resumes, out.stop_resumes, out.gave_up
        if res.reason == "dry_run":
            return res
        session_start, session_end = out.started, rpolicy.held_deadline(rpolicy.fight_hold_path(self.state.dir, sid),
                                                                  out.started + minutes * 60)
        crashed = res.reason in GAME_FAULTS
        res = replace(res, started=session_start)
        if not crashed:
            self.ensure_save(res.started)
        try:   # was the SessionEnd hook: commit a finished wrap-up, else log that the session ended before it
            self.session_cmd(["session-end"], self.session_env(session_end, minutes, sid))
        except OSError as e:
            renv.log(f"{sid}: session-end failed: {e}")
        self.close_memory_session(res)
        plan_bad = [] if crashed else self.check_plan_lines(f"{sid} end")   # the keeper rewrites invalid lines
        self.start_keeper(sid, res, plan_bad)
        try:
            if got := self.record_agent_saves(res.started):
                renv.log(f"{sid}: the agent's quicksave(s) {', '.join(got)} are this playthrough's saves too")
        except Exception as e:
            renv.log(f"{sid}: agent saves not recorded: {e!r}")
        end_sig = game.read_progress(renv.log)
        entry = {
            "kind": "play", "session": sid, "claude_session": res.claude_session, "reason": res.reason,
            "exit": res.exit_code, "started": res.started, "ended": res.ended,
            "model": res.model, "effort": res.effort,
            "keeper_model": rpolicy.keeper_model(self.opt.keeper_model, res.model or self.opt.model) if self.opt.keeper else None,
            "stop_resumes": stop_resumes,
            "minutes": round((res.ended - res.started) / 60, 1), "start_sig": start_sig, "end_sig": end_sig,
            "progress": None, "progress_why": "crashed",
            "started_at_game_over": screen == "game_over", "resumes": resumes,
            "story_points": rpolicy.story_points(self.state.dir / f"{sid}.progress.json"),
        }
        if not crashed:
            entry["progress"], entry["progress_why"] = rpolicy.session_progress(start_sig, end_sig, entry["story_points"])
        renv.log(f"{sid}: session progress {entry['progress']} ({entry['progress_why']})")
        b = rpolicy.boundary_s(self.state["history"], res.started)
        if b is not None:
            entry["boundary_s"] = b
        early = rpolicy.early_exit_minutes(res.reason, res.started, res.ended, minutes)
        if early is not None:
            entry["early_exit_min"] = early
            renv.log(f"{sid}: early exit: claude ended with {early:g} of {minutes:g} min left (exit {res.exit_code}); "
                f"starting the next session now" + (f" (boundary before this session: {b:g} s)" if b is not None else ""))
        self.state["history"] = self.state["history"] + [entry]
        self.new_game = False  # from here on the game has a character: confirm by name
        if rpolicy.is_stuck(self.state["history"]):
            renv.log(f"stuck detector: {renv.STUCK_SESSIONS} sessions without progress; next kickoff says change approach")
        if gave_up is not None:
            raise gave_up
        return res

    def run(self) -> int:
        self.check_memory()
        self.recover_keeper()
        h = self.handover or {}
        now = time.time()
        run_started = self.run_t0 = h.get("run_started") or now
        if h:   # same run, new code: keep its --hours budget; the game is the one this run was playing
            run_end = h.get("run_end")
            renv.log(f"runner restarted in place with the new code (pid {os.getpid()})")
            self.event("runner_restarted", pid=os.getpid())
        else:
            run_end = self.run_end(now)
            self.event("runner_start", pid=os.getpid())
        rstate.write_status(playthrough=self.opt.playthrough, runner_pid=os.getpid(), run_end=run_end,
                     session=None, deadline=None, run_started=run_started, heartbeat=time.time(), alert=None,
                     series=self.opt.series, run_index=self.opt.run_index, sandbox=self.opt.sandbox)
        first = not h
        setup_failures = 0
        stop_why = "stopped"
        outcome = None   # --until: how the run ended, when that ending writes a record (not a stop or a restart)
        restarting = False
        try:
            if (why := self.budget_refusal()) is not None:
                stop_why, self.fatal = f"refused: {why}", True
                return 2
            if self.check_until(rpolicy.read_seen(self.state.dir), None):
                what = "the game is finished" if self.game_ended() else f"--until {self.opt.until}"
                renv.log(f"{what}: already reached in this playthrough; nothing to play")
                stop_why = f"{what}: already reached"
                outcome = "already_reached"   # no record: that run was recorded when it got there
                return 0
            if self.opt.until:
                run_started = self.run_started = self.begin_run(run_started)
            if not self.opt.dry_run:
                self.facts_writer()   # time zero: the run's start
            while not self.stop_requested and not (renv.RUNTIME / "STOP").exists():
                self.code_update_check()
                loop = rpolicy.crash_loop(rpolicy.run_events(self.state.dir, run_started - 1), time.time(), run_started)
                if loop:
                    renv.log(f"{loop}: stopping the run (the game keeps dying; see runner_events.jsonl and the launch logs)")
                    self.event("crash_loop", why=loop)
                    rstate.write_status(alert={"t": time.time(), "kind": "crash_loop", "msg": loop})
                    stop_why, outcome = f"gave up: {loop}", "gave_up"
                    return 1
                if self.heat_paused() or self.cost_paused():
                    continue
                self.write_status_line("starting the next session")
                if self.has_usage_caps():   # the finished processes so far, the memory keeper's included
                    done = runs.claude_totals(self.state.dir, run_started)
                    if self.check_usage_caps(done["cost_usd"], runs.token_total(done["tokens"])):
                        stop_why, outcome = self.cap_hit["why"], "over_budget"
                        break
                session_minutes = self.session_minutes()
                minutes = rpolicy.session_budget(time.time(), run_end, session_minutes,
                                         min(renv.MIN_SESSION_MINUTES, session_minutes))
                if minutes is None:
                    stop_why = ("time cap used up (--run-hours/--hours)" if self.opt.run_hours or self.opt.series_end
                                else "--hours budget used up")
                    renv.log(stop_why)
                    outcome = "timeout"
                    break
                try:
                    res = self.play_session(minutes, first)
                except LaunchBlocked as e:
                    # Never a give-up: an operator puts Steam back offline (or closes their own BG3) and the run
                    # continues by itself.
                    renv.log(f"session setup refused: {'Steam not offline' if isinstance(e, SteamNotOffline) else 'launch blocked'} "
                        f"({e}); retrying in {renv.STEAM_RETRY_S} s")
                    if self.abort:
                        break
                    time.sleep(renv.STEAM_RETRY_S)
                    continue
                except rcode.SETUP_ERRORS as e:
                    renv.log(f"session setup failed: {type(e).__name__}: {e}")
                    self.event("setup_failed", error=str(e)[:300])
                    if self.abort:
                        break
                    setup_failures += 1
                    if setup_failures >= renv.MAX_SETUP_FAILURES:
                        renv.log(f"{setup_failures} setup failures in a row; giving up")
                        stop_why = "gave up: setup failures"
                        outcome = "gave_up"
                        return 1
                    try:
                        self.recover(f"failed to start ({e})")
                        self.event("recovered", fault="setup", how=f"game relaunched, loaded "
                                   f"{self.state['loaded_save'] or 'main menu'}")
                    except (LaunchGaveUp, Bg3leStale):
                        raise
                    except Exception as e2:
                        renv.log(f"recovery failed: {e2}; waiting 2 minutes")
                        if not isinstance(e2, LaunchBlocked):
                            self.event("recovery_failed", fault="setup", error=str(e2)[:300])
                        time.sleep(120)
                    continue
                first = False
                if res.reason != "mcp_failed":
                    setup_failures = 0
                if res.reason == "dry_run":
                    break
                if self.until_hit:
                    stop_why = (f"the game is finished: {self.until_hit['label']} in {self.until_hit.get('session')}"
                                if self.game_ended() else
                                f"--until {self.opt.until}: reached in {self.until_hit.get('session')}")
                    outcome = "reached"
                    break
                if self.cap_hit:
                    stop_why, outcome = self.cap_hit["why"], "over_budget"
                    break
                if res.reason == "api_limit":   # RUN-7: wait for the limit's reset, never give up
                    self.quick_failures = 0
                    self.api_limits += 1
                    self.wait_api_limit(res)
                    continue
                self.api_limits = 0
                if res.reason == "mcp_failed":   # RUN-8: a setup failure (the agent had no game tools)
                    setup_failures += 1
                    if setup_failures >= renv.MAX_SETUP_FAILURES:
                        renv.log(f"{setup_failures} setup failures in a row (the bg3 MCP server: {res.detail}); giving up")
                        stop_why = "gave up: the bg3 MCP server fails to start"
                        outcome = "gave_up"
                        return 1
                    time.sleep(min(30 * setup_failures, 300))
                    continue
                if renv.quick_failure(res):
                    self.quick_failures += 1
                    if self.quick_failures >= renv.MAX_QUICK_FAILURES:
                        renv.log(f"the client failed at once {self.quick_failures} times in a row ({res.reason}); "
                            "stopping (see the session logs)")
                        stop_why = "gave up: claude exits at once"
                        outcome = "gave_up"
                        return 1
                    time.sleep(30 * self.quick_failures)
                else:
                    self.quick_failures = 0
        except rcode.RestartRunner as e:
            restarting = True
            rcode.write_handover(self.handover_data(run_end, run_started))
            self.event("runner_restart", files=e.args[0] if e.args else [])
            raise
        except LaunchGaveUp as e:
            self.give_up_launch(e)
            stop_why = "gave up: the game crashes on every launch"
            outcome = "gave_up"
            return 1
        except Bg3leStale as e:
            stop_why = f"stale bg3le build: run {bg3le_buildinfo.REBUILD} ({e})"[:300]
            outcome, self.fatal = "gave_up", True
            return 1
        except SettingsMismatch as e:
            stop_why = f"game settings contradict the scenario: {e}"[:300]
            outcome, self.fatal = "gave_up", True
            return 1
        except BaseException as e:
            stop_why = f"died: {type(e).__name__}: {e}"[:300]
            if not isinstance(e, KeyboardInterrupt):
                outcome, self.fatal = "runner_error", True
            if not isinstance(e, (SystemExit, KeyboardInterrupt)):   # a SystemExit carries its own reason
                renv.log("runner died:\n" + traceback.format_exc().rstrip())
            raise
        finally:
            self.join_keeper()
            self.sandbox_cleanup()
            self.outcome = outcome or ("stopped" if self.opt.until and not restarting else None)
            if not restarting:
                # (a playthrough run without --until gets one record too: the game's end, its terminal milestone)
                if (self.opt.until or self.game_ended()) and outcome in runs.OUTCOMES and not self.opt.dry_run:
                    try:
                        self.write_run_record(run_started, outcome, "" if outcome == "reached" else stop_why)
                    except Exception as e:
                        renv.log(f"run record failed: {type(e).__name__}: {e}")
                (renv.RUNTIME / "STOP").unlink(missing_ok=True)
                rstate.write_status(session=None, deadline=None, runner_pid=None)
                self.write_status_line(f"STOPPED ({stop_why})")
                self.event("runner_stop", why=stop_why)
                renv.log(f"runner stopped ({stop_why})")   # every exit path: operator scripts wait for it
        return 0

    def handoff_session(self, text: str) -> int | None:
        return parse_handoff_session(text)

    def lsf_attributes(self) -> Callable[[bytes], list]:
        sys.path.append(str(renv.REPO / "tools" / "saves"))
        import lsf

        return lsf.attributes


class Runner(PlaythroughSide, lser.SeriesSide, rcore.SessionRunner):
    """`play.py run`: SessionRunner with the playthrough parts (PlaythroughSide) and the series' (SeriesSide)."""
