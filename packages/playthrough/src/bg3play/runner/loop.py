"""The continuous playthrough loop, `play.py run` (moved from runner/legacy_playthrough.py, Phase D.2): one session
after another until a stop (play.py stop / runtime/STOP), the hours budget, a cap, the --until milestone or the
game's end, with the memory keeper between sessions, waits on API limits and setup failures, the cost and heat
pauses, the crash-loop stop and the self-restart when the runner's own code changed (a handover keeps the run)."""

from __future__ import annotations

import os
import time
import traceback

from bg3harness import bg3le_buildinfo, runs
from bg3harness.game.io import Bg3leStale, LaunchBlocked, LaunchGaveUp, SteamNotOffline
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.scenario_file import SettingsMismatch


class LoopSide:
    def run(self) -> int:
        self.check_memory()
        self.recover_keeper()
        h = self.handover or {}
        now = time.time()
        run_started = self.run_t0 = h.get("run_started") or now
        if h:  # same run, new code: keep its --hours budget; the game is the one this run was playing
            run_end = h.get("run_end")
            renv.log(f"runner restarted in place with the new code (pid {os.getpid()})")
            self.event("runner_restarted", pid=os.getpid())
        else:
            run_end = self.run_end(now)
            self.event("runner_start", pid=os.getpid())
        rstate.write_status(
            playthrough=self.opt.playthrough,
            runner_pid=os.getpid(),
            run_end=run_end,
            session=None,
            deadline=None,
            run_started=run_started,
            heartbeat=time.time(),
            alert=None,
            series=self.opt.series,
            run_index=self.opt.run_index,
            sandbox=self.opt.sandbox,
        )
        first = not h
        setup_failures = 0
        stop_why = "stopped"
        outcome = None  # --until: how the run ended, when that ending writes a record (not a stop or a restart)
        restarting = False
        try:
            if (why := self.budget_refusal()) is not None:
                stop_why, self.fatal = f"refused: {why}", True
                return 2
            if self.check_until(rpolicy.read_seen(self.state.dir), None):
                what = "the game is finished" if self.game_ended() else f"--until {self.opt.until}"
                renv.log(f"{what}: already reached in this playthrough; nothing to play")
                stop_why = f"{what}: already reached"
                outcome = "already_reached"  # no record: that run was recorded when it got there
                return 0
            if self.opt.until:
                run_started = self.run_started = self.begin_run(run_started)
            if not self.opt.dry_run:
                self.facts_writer()  # time zero: the run's start
            while not self.stop_requested and not (renv.RUNTIME / "STOP").exists():
                self.code_update_check()
                loop = rpolicy.crash_loop(rpolicy.run_events(self.state.dir, run_started - 1), time.time(), run_started)
                if loop:
                    renv.log(
                        f"{loop}: stopping the run (the game keeps dying; see runner_events.jsonl and the launch logs)"
                    )
                    self.event("crash_loop", why=loop)
                    rstate.write_status(alert={"t": time.time(), "kind": "crash_loop", "msg": loop})
                    stop_why, outcome = f"gave up: {loop}", "gave_up"
                    return 1
                if self.heat_paused() or self.cost_paused():
                    continue
                self.write_status_line("starting the next session")
                if self.has_usage_caps():  # the finished processes so far, the memory keeper's included
                    done = runs.claude_totals(self.state.dir, run_started)
                    if self.check_usage_caps(done["cost_usd"], runs.token_total(done["tokens"])):
                        stop_why, outcome = self.cap_hit["why"], "over_budget"
                        break
                session_minutes = self.session_minutes()
                minutes = rpolicy.session_budget(
                    time.time(), run_end, session_minutes, min(renv.MIN_SESSION_MINUTES, session_minutes)
                )
                if minutes is None:
                    stop_why = (
                        "time cap used up (--run-hours/--hours)"
                        if self.opt.run_hours or self.opt.series_end
                        else "--hours budget used up"
                    )
                    renv.log(stop_why)
                    outcome = "timeout"
                    break
                try:
                    res = self.play_session(minutes, first)
                except LaunchBlocked as e:
                    # Never a give-up: an operator puts Steam back offline (or closes their own BG3) and the run
                    # continues by itself.
                    what = "Steam not offline" if isinstance(e, SteamNotOffline) else "launch blocked"
                    renv.log(f"session setup refused: {what} ({e}); retrying in {renv.STEAM_RETRY_S} s")
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
                        how = f"game relaunched, loaded {self.state['loaded_save'] or 'main menu'}"
                        self.event("recovered", fault="setup", how=how)
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
                    stop_why = (
                        f"the game is finished: {self.until_hit['label']} in {self.until_hit.get('session')}"
                        if self.game_ended()
                        else f"--until {self.opt.until}: reached in {self.until_hit.get('session')}"
                    )
                    outcome = "reached"
                    break
                if self.cap_hit:
                    stop_why, outcome = self.cap_hit["why"], "over_budget"
                    break
                if res.reason == "api_limit":  # RUN-7: wait for the limit's reset, never give up
                    self.quick_failures = 0
                    self.api_limits += 1
                    self.wait_api_limit(res)
                    continue
                self.api_limits = 0
                if res.reason == "mcp_failed":  # RUN-8: a setup failure (the agent had no game tools)
                    setup_failures += 1
                    if setup_failures >= renv.MAX_SETUP_FAILURES:
                        renv.log(
                            f"{setup_failures} setup failures in a row (the bg3 MCP server: {res.detail}); giving up"
                        )
                        stop_why = "gave up: the bg3 MCP server fails to start"
                        outcome = "gave_up"
                        return 1
                    time.sleep(min(30 * setup_failures, 300))
                    continue
                if renv.quick_failure(res):
                    self.quick_failures += 1
                    if self.quick_failures >= renv.MAX_QUICK_FAILURES:
                        renv.log(
                            f"the client failed at once {self.quick_failures} times in a row ({res.reason}); "
                            "stopping (see the session logs)"
                        )
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
            if not isinstance(e, SystemExit | KeyboardInterrupt):  # a SystemExit carries its own reason
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
                renv.log(f"runner stopped ({stop_why})")  # every exit path: operator scripts wait for it
        return 0
