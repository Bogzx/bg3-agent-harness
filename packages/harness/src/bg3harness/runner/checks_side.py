"""SessionRunner's checks (moved from runner/play.py, Phase D.3): the between-session check, the
progress poller and facts.jsonl, the game settings readout and seed, the --until check, the status line, the cost
pause, the heat pause and the usage caps.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from bg3ai import milestones
from bg3ai import progress as story

from bg3harness import choices, events, facts, game_settings, ramwatch, runs, scenario_file, spend, steam_paths
from bg3harness.game import host as ghost
from bg3harness.game import io as game
from bg3harness.game import oom
from bg3harness.game.io import EVAL_TIMEOUT
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.scenario_file import SettingsMismatch
from bg3harness.session import GAME_FAULTS

FACTS_LOCK = threading.Lock()  # facts_poll: the progress loop's thread and a bench run's stop check


class ChecksSide:
    def between_sessions_check(self) -> dict | None:
        """tools/dev/check_all.py after the mod hot reload, before the next kickoff (no claude is in play/ then;
        our own runner.lock is waved through by BG3AI_CHECK_ALL_RUNNER_PID). At most every CHECK_ALL_INTERVAL.
        The table goes to the finished session's triage report, the counts to the log and runner.status.json
        (dashboard). A FAIL never stops play; nothing here raises."""
        now = time.time()
        if not rcode.check_all_due(self.state["check_all_at"], now) or self.stop_requested or (renv.RUNTIME / "STOP").exists():
            return None
        json_path = renv.RUNTIME / "check_all.json"
        json_path.unlink(missing_ok=True)
        env = dict(os.environ, BG3AI_CHECK_ALL_RUNNER_PID=str(os.getpid()))
        t0 = time.time()
        try:
            p = subprocess.run(rcode.check_all_cmd(json_path), capture_output=True, text=True, cwd=renv.REPO, env=env,
                               timeout=renv.CHECK_ALL_TIMEOUT)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = None, ""
        except OSError as e:
            rc, out = -1, str(e)
        secs = time.time() - t0
        try:
            report = json.loads(json_path.read_text())
        except (OSError, ValueError):
            report = None
        res = rcode.check_all_result(rc, out, report)
        session, triage_path = self.last_triage or (None, None)
        renv.log(res["line"] + (f" (after {session}, {secs:.0f} s)" if session else f" ({secs:.0f} s)"))
        if res["ok"]:
            self.state["check_all_at"] = now
        bad = [f"{r.get('name')}: {r.get('detail')}"[:200] for r in res["rows"] if r.get("verdict") == "FAIL"]
        for b in bad:
            renv.log(f"check_all FAIL {b}")
        # S068 started on screen=map (the waypoints check's map was still up, the agent pressed ESCAPE): check_all's
        # last row closes it and says what the next session would start on; no row (timed out): ask ourselves.
        scr = next((r for r in res["rows"] if r.get("name") == "screen"), None)
        if scr is None and (screen := game.ui_screen()) == "map":
            scr = {"verdict": "FAIL", "detail": f"screen {screen} after check_all ({res['why'] or 'no screen row'})"}
        if scr and scr.get("verdict") == "FAIL":
            renv.log(f"PREP FAIL: the game is not on the game view after check_all: {scr.get('detail')}")
            self.pending_notes.append(f"RUNNER NOTE: the between-sessions check left the game off the game view "
                                      f"({scr.get('detail')}). Close that screen first (ESCAPE).")
        rstate.write_status(check_all={"t": now, "session": session, "ok": res["ok"], "counts": res["counts"],
                                "line": res["line"], "fails": bad[:5], "why": res["why"]})
        if triage_path is not None:
            try:
                with open(triage_path, "a") as f:
                    f.write(rcode.check_all_markdown(res, now, secs, "after the session "))
            except OSError as e:
                renv.log(f"check_all: triage append failed: {e}")
        return res

    def progress_loop(self, session: str, base: Path, start: float, stop: threading.Event, human=None) -> None:
        """Poll story progress (fair journal/map/status data) into Sxxx.progress.jsonl until stop is set."""
        tl = story.Timeline(Path(f"{base}.progress.jsonl"), self.state.dir / "progress_state.json", start)
        actions = story.actions_in(Path(f"{base}.metrics.jsonl"))
        while not stop.wait(story.POLL_S):
            try:
                got = story.read_game(lambda code, ctx: game.lua_eval(code, ctx, timeout=EVAL_TIMEOUT))
            except Exception:
                got = None
            try:
                self.facts_poll(got)
            except Exception as e:
                renv.log(f"{session}: facts poll failed: {e}")
            try:
                was_stuck = tl.stuck_since is not None
                evs = tl.observe(got, actions_since=actions)
                stamp = datetime.now().strftime("%H:%M:%S")
                if human is not None:
                    for e in evs:
                        human.write(f"{stamp} [progress] +{e['points']} {e['kind']}: {e['what']}\n")
                    if tl.stuck_since is not None and not was_stuck:
                        human.write(f"{stamp} [progress] STUCK: no progress for {story.STUCK_MIN:.0f}+ min while active\n")
                if tl.stuck_since is not None and not was_stuck:
                    renv.log(f"{session}: stuck signal: no story progress for {story.STUCK_MIN:.0f}+ min while active")
                hit = self.check_until(tl.seen, session)
                if hit and human is not None:
                    human.write(f"{stamp} [runner] milestone {hit['id']} ({hit['label']}) reached: "
                                f"{'the game is finished' if self.game_ended() else '--until stop'}\n")
            except Exception as e:
                renv.log(f"{session}: progress poll failed: {e}")

    def facts_poll(self, got: dict | None) -> None:
        """One facts.jsonl poll on a progress read: the referee ring, then the read and the party inventory. A
        settings check that a new game deferred at the main menu runs on the first poll with the server up; a
        contradiction then stops the session (abort), as it would have at the start. One poll at a time
        (FACTS_LOCK): the progress loop's and facts_now's."""
        with FACTS_LOCK:
            self._facts_poll(got)

    def facts_now(self) -> None:
        """One facts poll now, between the progress loop's (story.POLL_S apart): a bench run's stop check grades the
        game as it is before it resumes the agent (B3 job 10: the Game Over box at 16:23:40, the last downed fact
        at 16:24:01, the agent resumed in between at 16:23:45)."""
        self.facts_poll(story.read_game(lambda code, ctx: game.lua_eval(code, ctx, timeout=EVAL_TIMEOUT)))

    def _facts_poll(self, got: dict | None) -> None:
        if got:
            if self.game_setup() and getattr(self, "settings_deferred", False):
                try:
                    self.check_settings()
                except SettingsMismatch:
                    self.abort = True
            self.ring_feed().poll()
        inv = facts.read_inventory(lambda code, ctx: game.lua_eval(code, ctx, timeout=EVAL_TIMEOUT),
                                   story.combined_code) if got else None
        self.facts_writer().observe(got, inv)

    def end_facts(self, outcome: str) -> float:
        """facts.jsonl's run_end mark, once per run; returns its time. When this runner has read a game, the last
        reads first (a snapshot of the end state; facts.end_reads waits for a region change's lagging area)."""
        ended: float | None = getattr(self, "_facts_ended", None)
        if ended is not None:
            return ended
        w = self.facts_writer()
        def read() -> dict | None:
            got = story.read_game(lambda code, ctx: game.lua_eval(code, ctx, timeout=EVAL_TIMEOUT))
            w.snap_t = None
            self.facts_poll(got)
            return got

        if w.last is not None and game.port_open():
            facts.end_reads(read, w, self.sleep, time.monotonic, renv.log)
        self._facts_ended = ended = w.mark("run_end", {"outcome": outcome})["t"]
        return ended

    def facts_writer(self) -> facts.FactsWriter:
        """This playthrough's facts.jsonl (one per run); the first use writes its run_start mark."""
        w = getattr(self, "_facts", None)
        if w is None:
            w = self._facts = facts.FactsWriter(self.state.dir / "facts.jsonl")
            if w.t0 is None:
                w.mark("run_start", {"playthrough": self.opt.playthrough, "client": self.opt.client,
                                     "model": self.opt.model})
        return w

    def setup_env(self) -> dict:
        """The game's env: the mod's scn.* ops are refused unless a run seeds the dice or its scenario fixes a setting
        the scn.* ops read or set (difficulty, karmic dice, the avatar's name; game_settings.SETUP_ENV)."""
        opt = getattr(self, "opt", None)   # a bare Runner (tests) has no opt
        want = getattr(opt, "scenario_settings", None) or {}
        fixed = any(want.get(k) is not None for k in (*scenario_file.CHECKED, "avatar_name"))
        return ghost.setup_env(getattr(opt, "seed", None) is not None or fixed)

    def game_setup(self) -> bool:
        """Once per runner, with a level loaded: the settings readout (the run record's game_settings, a facts
        `settings` mark), a save start's avatar renamed to the scenario's character (avatar_name: the start saves
        still name it after the model that played them, live B1) and, for a seeded run, scn.seed (the mod re-applies
        the seed and the name after every load). At the main menu (a new game before its first load) the server
        context, where scn.* and roll.snapshot run, doesn't exist yet: nothing is done and the next call tries again
        (live7). True once done."""
        if getattr(self, "game_settings", None) is not None:
            return True
        if not game.server_ready():
            return False
        call = lambda op, args: game.rpc(op, args, timeout=EVAL_TIMEOUT)
        karmic = (self.opt.scenario_settings or {}).get("karmic_dice")
        if karmic is not None:   # the scenario's Karmic Dice, set before the readout reads it back (scn.undo restores)
            try:
                game_settings.apply_karmic(call, karmic)
                self.karmic_set = True
            except Exception as e:
                renv.log(f"!!!!! scn.karmic {karmic} failed: {e}; the settings check reads what the game has !!!!!")
        avatar = (self.opt.scenario_settings or {}).get("avatar_name")
        named: dict = {}
        if avatar is not None:   # the settings check refuses a run whose avatar didn't take the name
            self.avatar_set = True   # also on a failure: the mod records the name before it writes it
            try:
                named = {"avatar_name": game_settings.apply_avatar_name(call, avatar).get("display")}
            except Exception as e:
                named = {"avatar_name": None, "avatar_error": str(e)[:300]}
                renv.log(f"!!!!! scn.rename_avatar {avatar} failed: {e} !!!!!")
        got = game_settings.readout(game_settings.profile_files(steam_paths.profile_dir(), steam_paths.manifest(),
                                                                self.lsf_attributes(), steam_paths.parse_vdf), call)
        got.update(named)
        if self.opt.seed is not None:
            try:
                got["seed"] = {"seed": self.opt.seed, "applied": game_settings.apply_seed(call, self.opt.seed)}
                self.seeded = True
            except Exception as e:
                got["seed"] = {"seed": self.opt.seed, "error": str(e)[:300]}
                renv.log(f"!!!!! scn.seed {self.opt.seed} failed: {e}; this run's dice are NOT seeded !!!!!")
        self.game_settings = got
        self.facts_writer().mark("settings", got)
        seed = got.get("seed")
        seeded = "" if seed is None else (f"; seed {seed['seed']} applied" if "applied" in seed else
                                          f"; seed {seed['seed']} NOT applied")
        renv.log(f"game settings: difficulty {got['difficulty']}, roll mode {got['roll_mode']}, karmic dice "
                 f"{got['karmic_dice']}{seeded}")
        return True

    def check_settings(self) -> None:
        """run --scenario-file: the game's settings readout (game_setup) against the scenario's difficulty and karmic
        dice, once per runner. A contradiction stops the run (SettingsMismatch); a setting the readout can't tell is
        logged and recorded (settings_unverified). At the main menu (a new game) it is deferred (settings_deferred):
        facts_poll runs it, with the seed, once the new game's first level has loaded."""
        want = self.opt.scenario_settings
        if not want or getattr(self, "settings_checked", False):
            return
        if not self.game_setup():
            if not getattr(self, "settings_deferred", False):
                self.settings_deferred = True
                renv.log("settings check and seed: no level loaded yet (main menu); they run once the game has loaded one")
                self.event("settings_deferred")
            return
        self.settings_checked = True
        bad, unknown = scenario_file.mismatches(want, getattr(self, "game_settings", None) or {})
        if unknown:
            renv.log(f"settings check: the game doesn't report {', '.join(unknown)}; the scenario's value is not verified")
            self.event("settings_unverified", keys=unknown)
        if bad:
            msg = "; ".join(bad)
            renv.log(f"!!!!! REFUSING TO PLAY: the game's settings contradict the scenario ({msg}). Change them in the "
                "game's options, then restart the run. !!!!!")
            self.event("settings_mismatch", problems=bad)
            rstate.write_status(alert={"t": time.time(), "kind": "settings_mismatch",
                                "msg": f"runner stopped: game settings contradict the scenario ({msg})"[:600]})
            raise SettingsMismatch(msg)
        renv.log("settings check passed: no game setting contradicts the scenario"
                 + (" (deferred from the main menu)" if getattr(self, "settings_deferred", False) else ""))

    def scenario_undo(self) -> None:
        """scn.undo at the run's end (the seed record would re-seed the next run's loads; a set Karmic Dice goes back
        to the player's, the avatar's name to the save's)."""
        if not any(getattr(self, k, False) for k in ("seeded", "karmic_set", "avatar_set")):
            return
        try:
            game_settings.undo(lambda op, args: game.rpc(op, args, timeout=EVAL_TIMEOUT))
            self.seeded = self.karmic_set = self.avatar_set = False
        except Exception as e:
            renv.log(f"!!!!! scn.undo failed: {e}; BG3AI/scenario.json keeps seeding every load until undone !!!!!")

    def ring_feed(self) -> facts.RingFeed:
        """The referee ring into facts.jsonl and the roll log into rolls.jsonl (kept across sessions: its cursor)."""
        f = getattr(self, "_ring_feed", None)
        if f is None:
            f = self._ring_feed = facts.RingFeed(lambda op, args: game.rpc(op, args, timeout=EVAL_TIMEOUT),
                                                 self.facts_writer(), self.state.dir / "rolls.jsonl")
        return f

    def write_choices(self, sid: str, base: Path) -> None:
        """The process's dialogue choices (scene, options, pick, stated reason) into facts.jsonl."""
        try:
            evs = events.load_events(base, kinds={"text", "thinking", "tool_call", "tool_result"})
            self.facts_writer().timed_events(choices.choices(evs))
        except Exception as e:
            renv.log(f"{sid}: choices failed: {e}")

    def check_until(self, seen: dict | None, session: str | None) -> dict | None:
        """run --until: the milestone, the first time milestones.status() says it's reached (else None). Every run
        also ends at milestones.GAME_END (the end credits / the epilogue): there is nothing left to play."""
        if self.until_hit:
            return None
        for mid in dict.fromkeys(m for m in (self.opt.until, milestones.GAME_END) if m):
            hit = rpolicy.until_reached(seen, mid)
            if hit:
                self.until_hit = dict(hit, session=session, t=round(time.time(), 1))
                end = mid == milestones.GAME_END
                renv.log(f"{session or 'runner'}: milestone {hit['id']} ({hit['label']}) reached: stopping "
                    + ("(the game is finished)" if end else "(--until)"))
                self.event("milestone", milestone=hit["id"], session=session, **({"terminal": True} if end else {}))
                return self.until_hit
        return None

    def game_ended(self) -> bool:
        return bool(self.until_hit and self.until_hit.get("id") == milestones.GAME_END)

    def write_status_line(self, state: str) -> str:
        """runtime/runner.status.txt (+ runner.status.json "status_line"). Never breaks the runner."""
        try:
            now = time.time()
            t0 = getattr(self, "run_t0", None) or now
            n = sum(1 for h in self.state["history"] or [] if h.get("kind") == "play" and (h.get("started") or 0) >= t0)
            faults = sum(1 for e in rpolicy.run_events(self.state.dir, max(now - renv.CRASH_LOOP_WINDOW_S, t0 - 1))
                         if (e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS) or e.get("kind") == "setup_failed")
            line = rpolicy.status_line(now, self.opt.playthrough, state, t0, n, rpolicy.read_seen(self.state.dir),
                               rpolicy.day_cost(self.state.dir, now), self.opt.max_cost_per_day, faults)
            (renv.RUNTIME / renv.STATUS_LINE).write_text(line + "\n")
            rstate.write_status(status_line=line)
            return line
        except Exception as e:
            renv.log(f"status line failed: {type(e).__name__}: {e}")
            return ""

    def cost_paused(self) -> bool:
        """--max-cost-per-day: True (after waiting up to COST_PAUSE_CHECK_S) while the trailing 24 h spent the cap.
        A pause, not an end: the run goes on once old sessions leave the window. Checked between sessions, so a day
        can go over by about one session."""
        cap = self.opt.max_cost_per_day
        if not cap:
            return False
        spent = rpolicy.day_cost(self.state.dir, time.time())
        if spent < cap:
            if self.cost_pause_since is not None:
                renv.log(f"cost: ${spent:.2f} in the last 24 h, under --max-cost-per-day ${cap:g}: playing again "
                    f"(paused {(time.time() - self.cost_pause_since) / 60:.0f} min)")
                self.event("cost_resume", spent=round(spent, 2), cap=cap)
                self.cost_pause_since = None
            return False
        if self.cost_pause_since is None:
            self.cost_pause_since = time.time()
            renv.log(f"cost: ${spent:.2f} in the last 24 h >= --max-cost-per-day ${cap:g}: pausing (no session starts; "
                f"checked every {renv.COST_PAUSE_CHECK_S / 60:g} min)")
            self.event("cost_pause", spent=round(spent, 2), cap=cap)
        self.write_status_line(f"PAUSED: ${spent:.2f} in 24 h >= --max-cost-per-day ${cap:g}")
        until = time.time() + renv.COST_PAUSE_CHECK_S
        while time.time() < until and not (self.stop_requested or self.abort or (renv.RUNTIME / "STOP").exists()):
            time.sleep(min(5.0, max(0.0, until - time.time())))
        return True

    def heat_killed(self) -> str | None:
        """Why ramwatch's heat guard killed the game of this launch (runtime/heat_kills.jsonl), or None."""
        launched = (self.state["game"] or {}).get("launched")
        kills = oom.read_spikes(launched, renv.RUNTIME / "heat_kills.jsonl") if launched else []
        return kills[-1].get("why", "too hot") if kills else None

    def heat_paused(self, after_kill: bool = False) -> bool:
        """Heat guard: while runtime/thermal.log (ramwatch, or an operator's thermal_log.sh) says the CPU or the
        GPU stayed at the heat-pause limit for pause_s (ramwatch.heat_limits: 92 C / 85 C for 60 s), no session
        starts: waits until both stayed under it for pause_s, then True. after_kill (the heat guard killed the game):
        waits unless it is cool already. The test box cut its power twice under the naut series load (2026-10-16 ~20:07,
        2026-10-17 ~16:01). A stale or missing log pauses nothing."""
        path = renv.RUNTIME / "thermal.log"
        h = ramwatch.read_heat(path)
        if not (h["pause"] or (after_kill and not h["cool"])):
            return False
        lim = ramwatch.heat_limits()
        t0 = time.time()
        what = (f"CPU {h['cpu']} C / GPU {h['gpu']} C" + (f" for {h['hot_s']:.0f} s" if h["pause"] else ""))
        renv.log(f"heat: {what}{' after the heat kill' if after_kill else ''} (pause at CPU >= {lim['pause_cpu_c']:g} C "
            f"or GPU >= {lim['pause_gpu_c']:g} C for {lim['pause_s']:g} s): pausing until both stay under it for "
            f"{lim['pause_s']:g} s")
        self.event("heat_pause", cpu_c=h["cpu"], gpu_c=h["gpu"], seconds=round(h["hot_s"]), after_kill=after_kill)
        menu = not after_kill and self.heat_menu_open()   # after a heat kill there is no game to pause
        try:
            while not (self.stop_requested or self.abort or (renv.RUNTIME / "STOP").exists()):
                self.write_status_line(f"PAUSED: too hot (CPU {h['cpu']} C, GPU {h['gpu']} C; "
                                       f"limit {lim['pause_cpu_c']:g}/{lim['pause_gpu_c']:g} C)")
                time.sleep(renv.HEAT_PAUSE_CHECK_S)
                h = ramwatch.read_heat(path)
                if h["cool"]:
                    break
        finally:
            if menu:
                self.heat_menu_close()
        if not h["cool"]:
            return True   # stopped while paused: the run loop sees the stop
        renv.log(f"heat: CPU {h['cpu']} C / GPU {h['gpu']} C after {(time.time() - t0) / 60:.0f} min: playing again")
        self.event("heat_resume", cpu_c=h["cpu"], gpu_c=h["gpu"], seconds=round(time.time() - t0))
        return True

    @staticmethod
    def wait_screen(want: str, timeout: float = renv.HEAT_MENU_WAIT_S) -> str | None:
        """ui.state's screen, polled until it is want or timeout passed; the last screen seen."""
        end = time.monotonic() + timeout
        while True:
            screen = game.ui_screen()
            if screen == want or time.monotonic() >= end:
                return screen
            time.sleep(0.15)

    def heat_menu_open(self) -> bool:
        """Heat pause: the ESC game menu over the running game, as a player pauses (the world stops and our
        tools idle under it), so it cools. Only from the plain game view: in a dialogue, a trade, a movie (ESCAPE
        skips it), on a loading screen or with a panel open an ESCAPE does something else, so those are skipped
        with a log line. True when it opened the menu (heat_menu_close closes it again). Never raises."""
        try:
            st = game.ui_state()
            if st is None:
                renv.log("heat: game menu not opened (the game's UI doesn't answer)")
                return False
            screen, panels = st.get("screen") or "game", st.get("panels_open") or []
            if screen != "game" or panels:
                renv.log(f"heat: game menu not opened: the {screen} screen{' with ' + ', '.join(panels) if panels else ''}"
                    " is up (an ESCAPE there does something else); the game keeps rendering")
                return False
            game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
            got = self.wait_screen("game_menu")
            if got != "game_menu":
                renv.log(f"heat: ESCAPE did not open the game menu (screen {got}); the game keeps rendering")
                return False
            renv.log("heat: game menu open (ESCAPE) while it cools")
            return True
        except Exception as e:
            renv.log(f"heat: game menu not opened: {e!r}")
            return False

    def heat_menu_close(self) -> None:
        """Closes the game menu heat_menu_open opened: its Resume button (a second ESCAPE left it open 3/3 live,
        bridge server._press_escape), ESCAPE if it is still up. Never raises."""
        try:
            screen = game.ui_screen()
            if screen != "game_menu":
                renv.log(f"heat: the game menu is already closed (screen {screen})")
                return
            game.rpc("ui.press", {"button": "Resume", "widget": "GameMenu"}, context="client")
            if (got := self.wait_screen("game")) == "game_menu":
                game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
                got = self.wait_screen("game")
            renv.log("heat: game menu closed" if got != "game_menu" else "heat: the game menu is STILL OPEN after Resume "
                "and ESCAPE (the next session's agent sees it)")
        except Exception as e:
            renv.log(f"heat: game menu not closed: {e!r}")

    def caps(self) -> dict:
        """The caps this run stops at (runs.CAP_KEYS): max_cost is the spend cap when there is one (opt.spend_cap)."""
        return {"run_hours": self.opt.run_hours, "max_cost": self.opt.spend_cap or self.opt.max_cost,
                "max_tokens": self.opt.max_tokens}

    def has_usage_caps(self) -> bool:
        """--max-cost (also without --until: then it caps this runner's run) / --max-tokens (with --until)."""
        return self.caps()["max_cost"] is not None or bool(self.opt.until and self.opt.max_tokens is not None)

    def check_usage_caps(self, cost: float, tokens: int) -> bool:
        """--max-cost / --max-tokens: True (and cap_hit set, an event logged) once the run reached one."""
        if self.cap_hit:
            return True
        caps = self.caps()
        if not (cap := runs.cap_exceeded(caps, cost, tokens) if self.has_usage_caps() else None):
            return False
        limit = f"--max-cost ${caps['max_cost']:g}" if cap == "cost" else f"--max-tokens {self.opt.max_tokens}"
        why = f"{cap} cap used up: ${cost:.2f}, {tokens} tokens ({limit})"
        self.cap_hit = {"cap": cap, "cost": round(cost, 4), "tokens": tokens, "why": why}
        renv.log(why + ": the run ends")
        self.event("cap", cap=cap, cost=round(cost, 4), tokens=tokens)
        return True

    def budget_refusal(self) -> str | None:
        """A paid client (spend.PAID_CLIENTS): why this run can't start, else None with its cost cap (self.caps())
        cut to what runtime/budget.json has left (every run of a series checks again)."""
        if not spend.is_paid(self.opt.client) or self.opt.dry_run:
            return None
        try:
            cap, err = spend.run_cap(self.opt.client, self.caps()["max_cost"],
                                     spend.read_budget(renv.RUNTIME / "budget.json"))
        except spend.BudgetError as e:
            cap, err = None, str(e)
        if err:
            renv.log(f"refusing the run: {err}")
            return err
        self.opt = replace(self.opt, **{"spend_cap" if self.opt.spend_cap else "max_cost": cap})
        return None
