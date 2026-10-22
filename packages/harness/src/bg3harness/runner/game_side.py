"""SessionRunner's game side (moved from runner/play.py, Phase D.3): the GameController it owns, the
save choice and load, relaunches (age, RAM, drift, OOM), the mod snapshot and hot reload, play settings, recovery
after a crash or hang, and the save at a session's end.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime

from bg3ai.luadebug import LuaDebugError

from bg3harness.game import io as game
from bg3harness.game.controller import MENU_STATES, GameController
from bg3harness.game.io import EVAL_TIMEOUT, MOD_READY_LUA, LaunchGaveUp, LaunchWatch, RpcError, Save
from bg3harness.game.oom import OOM_SAVE_LIMIT
from bg3harness.game.saves import (
    SAVE_PREFIX,
    is_ai_label,
    parse_handoff_save,
    pick_save,
    pick_save_why,
    play_windows,
    saves_since,
    session_ends,
)
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate


class GameSide:
    @property
    def gamectl(self) -> GameController:
        """The game this runner launched (bg3harness.game.controller), made on first use."""
        ctl = self.__dict__.get("_gamectl")
        if ctl is None:
            ctl = self.__dict__["_gamectl"] = GameController(rstate.RunnerGameHost(self))
        return ctl

    @property
    def game_proc(self) -> subprocess.Popen | None:
        return self.gamectl.proc

    @game_proc.setter
    def game_proc(self, proc: subprocess.Popen | None) -> None:
        self.gamectl.proc = proc

    @property
    def watch(self) -> LaunchWatch | None:   # crash signs of the game the runner launched (see launch_game)
        return self.gamectl.watch

    @watch.setter
    def watch(self, w: LaunchWatch | None) -> None:
        self.gamectl.watch = w

    def owned_group(self) -> int | None:
        return self.gamectl.owned_group()

    def owns_game(self) -> bool:
        return self.gamectl.owns_game()

    def game_alive(self) -> bool:
        return self.gamectl.alive()

    def launch_game(self, load_folder: str | None) -> None:
        self.gamectl.launch(load_folder)

    def kill_game(self) -> None:
        self.gamectl.kill()

    def wait_for(self, what: str, pred, timeout: float, interval: float = 2.0) -> bool:
        return self.gamectl.wait_for(what, pred, timeout, interval)

    def wait_menu(self) -> bool:
        return self.gamectl.wait_menu()

    def wait_running(self) -> bool:
        return self.gamectl.wait_running()

    def own_saves(self) -> list[Save]:
        """list_saves() minus saves older than this playthrough's run start (runner.json saves_after, set for a
        --runs run): a series reuses one character name, and run 2 must never load run 1's saves."""
        after = self.state["saves_after"]
        saves = rcode.list_saves()
        return [s for s in saves if s.mtime >= after or s.folder == self.opt.start_save] if after else saves

    def play_windows(self) -> list[tuple[float, float]]:
        return play_windows(self.state["history"], getattr(self.state, "dir", None))

    def ours(self) -> set[str]:
        """Save folders pick_save takes whatever their label: the runner's quicksaves, the agent's own quicksaves
        (agent_saves, RUN-6: S059's newest good state was one) and a scenario's start save (older than every save of
        the run, so the agent's first save supersedes it)."""
        return (set(self.state["runner_saves"] or []) | set(self.state["agent_saves"] or [])
                | ({self.opt.start_save} if self.opt.start_save else set()))

    def record_agent_saves(self, since: float) -> list[str]:
        """The quicksave folders the play session's MCP server wrote since `since` (its save_game.bg lines in
        actions.jsonl: the folder found on disk) → runner.json agent_saves (the last AGENT_SAVES_KEPT). Named
        Sxxx- saves need no record (pick_save takes them by label). Only this playthrough's owner's folders."""
        found = rpolicy.agent_quicksaves(rpolicy.read_actions_since(since), self.state["save_owner"] or self.opt.save_owner)
        if found:
            kept = [f for f in self.state["agent_saves"] or [] if f not in found] + found
            self.state["agent_saves"] = kept[-rpolicy.AGENT_SAVES_KEPT:]
        return found

    def choose_save(self) -> Save | None:
        handoff = self.mem / "HANDOFF.md"
        text = handoff.read_text() if handoff.exists() else ""
        saves = self.own_saves()
        save, why = pick_save_why(saves, parse_handoff_save(text), self.state["save_owner"], self.ours(),
                                  windows=self.play_windows(), handoff_session=self.handoff_session(text),
                                  ends=session_ends(self.state["history"], getattr(self.state, "dir", None)))
        blocked = set(self.state["oom_blocked"] or [])
        if save and save.folder in blocked:
            first = save
            save = self.refuse_oom_save(save, saves, blocked)
            why = f"\"{first.label}\" is OOM-blocked ({why}): the newest other save"
        self.save_choice = (save.label, why) if save else None
        renv.log(f"save choice: {save.folder if save else 'none'}: {why}")
        if save and not self.state["save_owner"] and not self.opt.dry_run:
            self.adopt_owner(save.owner)
        return save

    def kickoff_pace(self) -> str:
        """The kickoff's pace pre-flight: cmd_run's (client_setup.kickoff_pace), else claude's."""
        pace = getattr(self.opt, "pace", None)
        return rpolicy.PACE_PREFLIGHT if pace is None else pace

    def loaded_why(self, loaded: str | None) -> str:
        """Why the runner loaded `loaded` (choose_save's reason), "" when it loaded another save (a crash fallback)."""
        choice = getattr(self, "save_choice", None)
        return choice[1] if loaded and choice and choice[0] == loaded else ""

    def refuse_oom_save(self, save: Save, saves: list[Save], blocked: set[str]) -> Save:
        """The newest save OOM-killed the game OOM_SAVE_LIMIT times in a row right after loading: load the newest
        other save of the playthrough (loudly), or stop the runner when there is none (a crash loop needs a human)."""
        alt = pick_save([s for s in saves if s.folder not in blocked], None, self.state["save_owner"], self.ours(),
                        windows=self.play_windows())
        streak = self.state["oom_streak"] or {}
        why = streak.get("why", "OOM")
        if alt is None:
            raise LaunchGaveUp(f"save {save.folder} OOM-killed the game {OOM_SAVE_LIMIT}× in a row after loading "
                               f"({why}) and there is no other save to load")
        msg = (f"REFUSING TO RESUME from {save.folder}: it OOM-killed the game {OOM_SAVE_LIMIT}× in a row right after "
               f"loading ({why}); loading the older {alt.folder} instead")
        renv.log(f"!!!!! {msg} !!!!!")
        self.event("oom_save_refused", save=save.folder, instead=alt.folder, why=why[:300])
        rstate.write_status(alert={"t": time.time(), "kind": "oom_save_refused", "msg": msg[:600]})
        tools = streak.get("tools") or []
        self.pending_notes.append(
            f"RUNNER NOTE: the save \"{save.label}\" made the game run out of memory {OOM_SAVE_LIMIT} times in a row "
            f"right after loading (the kernel killed it); the runner loaded the older save \"{alt.label}\" instead. "
            "Progress after it is lost in the game: replay it, and fix HANDOFF.md/objectives. "
            + (f"The last tool calls before the memory blow-up were: {' | '.join(tools[-5:])}. Avoid repeating "
               "them exactly (another route or approach). " if tools else ""))
        return alt

    def note_game_gone(self, oom: str | None) -> None:
        """The game is down at a session start and the runner didn't kill it after the last session: it crashed at
        (or after) the end of that session. Warn the next session with the last tool calls before the end, once."""
        last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), None)
        if not last or not last.get("ended") or self.state["crash_noted"] == last.get("session"):
            return
        if (self.state["game_killed_at"] or 0) >= last["ended"] - 5:
            return   # killed on purpose (age relaunch, recovery) after that session
        self.state["crash_noted"] = last.get("session")
        why = f"{last.get('session')}: the game was gone at the next session start" + (f"; {oom}" if oom else "")
        renv.log(f"game gone since {last.get('session')} ended: warning the next session with its last tool calls")
        self.pending_notes.append(rpolicy.crash_tools_note(why, rpolicy.read_actions_since(last["ended"] - 600), last["ended"]))

    def check_game_oom(self) -> str | None:
        return self.gamectl.check_oom()

    def foreign_use(self, save: Save) -> str | None:
        """foreign_use_since() for the running game; a relaunch counts only for a game this runner didn't start."""
        started = None if self.owns_game() else rpolicy.game_started_at()
        return rpolicy.foreign_use_since(save, rcode.list_saves(), rpolicy.read_actions_since(save.mtime), started, self.ours())

    def restart_drift(self) -> str | None:
        """state_drift() between the last play session's end and the running game (a runner restart only). Fails
        closed (RUN-6): a game whose progress can't be read is not proven to be where the session ended → reload."""
        last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), None)
        if not last or not last.get("end_sig"):
            return None
        now = game.read_progress(renv.log)
        if now is None:
            return "its progress can't be read (state.status/state.journal), so it isn't known to be where it was"
        return rpolicy.state_drift(last["end_sig"], now)

    def load_save(self, save: Save | None, force: bool = False) -> str | None:
        """Bring the game to this playthrough's latest save (or the main menu for a new game). force: load it even
        when --take-over would play on from the running game (restart_drift found it elsewhere)."""
        ui_load = "ui.menu" in game.available_ops("client") if game.port_open() else False
        self.new_game = save is None
        if save is None:
            if game.port_open() and game.game_state() == "Running" and game.server_ready() and self.opt.take_over:
                # A new game already past character creation (e.g. started by an operator or a previous
                # session that ended before its first save): play on from here.
                host = game.host_character_name()
                owner = self.state.data.get("save_owner")
                if owner is None or (host and host.casefold() == owner.casefold()):
                    renv.log(f"no save yet, but a new game is running (host '{host}'): playing on from here")
                    return None
                raise SystemExit(f"a game is running with host '{host}', not this playthrough's '{owner}'")
            renv.log("no save for this playthrough yet: leaving the game at the main menu (new game)")
            if not game.port_open():
                self.launch_game(None)
            if not self.wait_menu():
                raise RuntimeError("the main menu never came up")
            owner = self.opt.save_owner or self.state["save_owner"]
            char = self.profile.get("character")
            if char:
                # CHAR-1 phase 0: the agent designs the character from the brief; a name only when one is given.
                self.pending_notes.append(rpolicy.character_creation_note(self.profile, char.get("name") or owner))
            elif owner:
                # Saves are matched to this playthrough by the character's name: a "Tav" breaks that.
                self.pending_notes.append(rpolicy.new_game_note(owner))
            return None
        if game.port_open() and game.game_state() == "Running" and game.server_ready() and self.opt.take_over and not force:
            # The operator says: take over the game as it is. If it's this playthrough's character, play on
            # from here instead of reloading (a reload only loses the minutes since the last save).
            host = game.host_character_name()
            owner = self.state.data.get("save_owner") or save.owner
            touched = self.foreign_use(save)
            if host and owner and host.casefold() == owner.casefold() and not touched:
                renv.log(f"--take-over: the running game is '{host}' (this playthrough): playing on from here")
                return None
            if touched:
                renv.log(f"--take-over: not playing on from the running game: {touched}")
        renv.log(f"loading save {save.folder}")
        use_menu = game.port_open() and ui_load
        if use_menu:
            state = game.game_state()
            if state != "Running" and not self.wait_menu():   # splash included: press past it first
                raise RuntimeError(f"the main menu never came up (state {game.game_state()})")
            # The mod refuses a load while its persisted load gate is off (the default on a fresh profile). This
            # is this playthrough's own save, which is exactly when the gates may open; apply_play_settings
            # re-decides them by the loaded character's name.
            try:
                game.rpc("ui.config", {"save_enabled": True, "load_enabled": True}, context="client")
            except Exception as e:
                renv.log(f"opening the save/load gates before the load failed: {e}")
            try:
                # ui.menu load works from the main menu and from in-game (it opens the load screen itself).
                game.rpc("ui.menu", game.load_menu_args(save, rcode.list_saves()), context="client", timeout=30)
            except (RpcError, LuaDebugError) as e:
                # Refused (gate, no such playthrough or date on the page) or the eval failed: restart the game
                # with -loadSaveGame, which needs neither the gate nor the Load page.
                renv.log(f"ui.menu load of {save.folder} refused ({e}); relaunching the game with --load instead")
                self.event("load_refused", save=save.folder, error=str(e)[:300])
                use_menu = False
            if use_menu:
                # Wait for Running only once the load really started. From in-game the state stays Running until
                # the load begins (S002's agent started mid-load); from the menu a load that never starts is an
                # error, not a 10-minute wait.
                here = ("Running",) if state == "Running" else MENU_STATES
                if not self.wait_for("the load to start", lambda: game.game_state() not in here, renv.LOAD_START_TIMEOUT,
                                     0.5):
                    raise RuntimeError(f"the load of {save.folder} never started (state {game.game_state()})")
        if not use_menu:
            if game.port_open():
                if not (self.owns_game() or self.opt.take_over or game.game_state() in MENU_STATES):
                    raise SystemExit("would have to restart a game the runner didn't launch; "
                                     "close it or pass --take-over")
                self.kill_game()
            save = self.launch_and_load(save)
            self.state["loaded_save"] = save.folder
            rstate.write_status(last_loaded=save.folder)
            self.facts_writer().mark("load", save.folder)
            return save.label
        if not self.wait_running():
            raise RuntimeError(f"save {save.folder} didn't finish loading")
        self.state["loaded_save"] = save.folder
        rstate.write_status(last_loaded=save.folder)
        self.facts_writer().mark("load", save.folder)
        return save.label

    def previous_good_save(self, failed: set[str]) -> Save | None:
        """The fallback when a save keeps crashing the game on load: the last save the runner loaded successfully,
        else this playthrough's newest other save. None = nothing else to try."""
        saves = [s for s in self.own_saves() if s.folder not in failed]
        good = self.state["loaded_save"]
        hit = next((s for s in saves if s.folder == good), None)
        return hit or pick_save(saves, None, self.state["save_owner"], self.ours(), windows=self.play_windows())

    def sleep(self, seconds: float) -> None:
        self.gamectl.sleep(seconds)

    def launch_and_load(self, save: Save) -> Save:
        return self.gamectl.launch_and_load(save)

    def give_up_launch(self, e: LaunchGaveUp) -> None:
        self.gamectl.give_up_launch(e)

    def maybe_relaunch(self, first: bool, minutes: float = 0.0) -> str | None:
        """Between sessions: kill a game process that relaunch_reason() condemns for the next session (`minutes`
        long, plus the fight hold), so ensure_game() launches a fresh one with the newest save. Never kills when the relaunch can't happen (Steam not offline: the launch guard
        would refuse) or would lose play (the newest save is older than the last session's last game change, or its
        start without one: lose_play_mark; unless that session
        ended on a load_game of it, ended_on_save, or the runner's quicksave at its end holds its last state,
        boundary_save: then the relaunch loads that one). -> the reason."""
        if self.opt.dry_run or not game.port_open():
            return None
        if first and not (self.owns_game() or self.opt.take_over):
            return None  # ensure_game decides about a game the runner didn't launch
        hist = [h for h in self.state["history"] if h.get("kind") == "play"]
        last = hist[-1] if hist else None
        text = None
        if last:
            try:
                text = (self.state.dir / f"{last['session']}.log").read_text(errors="replace")
            except OSError:
                text = None
        g = self.state["game"] or {}
        started = g.get("launched") if self.owns_game() and g.get("launched") else rpolicy.game_started_at()
        ram = rpolicy.game_ram_mb()
        why = rpolicy.relaunch_reason(started, time.time(), text, session_s=minutes * 60 + rpolicy.FIGHT_HOLD_S if minutes else 0.0,
                              ram_mb=ram, mod_path=rcode.game_mod_path(), mod_live=str(rcode.MOD_LIVE))
        if why is None:
            if started:
                renv.log(f"game process {(time.time() - started) / 3600:.1f} h old, {ram if ram is not None else '?'} MB "
                    f"RAM+swap, no ready-check failure last session: kept (relaunch at {renv.GAME_MAX_AGE_H:g} h by the "
                    f"session's end or {renv.RELAUNCH_RAM_MB} MB)")
            return None
        save, boundary = self.choose_save(), None
        if save is None:
            renv.log(f"game relaunch wanted ({why}) but this playthrough has no save to load: skipped")
            return None
        acts = rpolicy.read_actions_since(last.get("started", 0)) if last else []
        mark, what = rpolicy.lose_play_mark(last, acts) if last else (0.0, "")
        if last and save.mtime < mark:
            if rpolicy.ended_on_save(acts, save):
                renv.log(f"the newest save {save.label} is older than {what}, but {last['session']} "
                    f"ended on load_game {save.label} with nothing changed after it: the game stands at that save")
            else:
                qs, no = rpolicy.boundary_save(self.own_saves(), set(self.state["runner_saves"] or []),
                                       self.state["runner_save_combat"] or {}, last, acts)
                if qs is not None and (now := game.party_in_combat()) is not False:
                    qs, no = None, (f"the party is in a fight now, after {qs.label}" if now else
                                    f"the game doesn't say whether the party is in a fight now (after {qs.label})")
                if qs is None:
                    renv.log(f"game relaunch wanted ({why}) but the newest save {save.label} is older than "
                        f"{what} and {no}: skipped (it would lose play)")
                    return None
                renv.log(f"the newest save {save.label} is older than {what}; the runner's quicksave "
                    f"{qs.label} at its end holds where it ended (out of combat, nothing changed after it)")
                boundary = save = qs
        ok, steam_why = rcode.steam_ready()
        if not ok:
            renv.log(f"game relaunch wanted ({why}) but Steam is not offline ({steam_why}): keeping the running game")
            self.event("game_relaunch_skipped", why=why, steam=steam_why)
            return None
        if boundary is not None:   # ensure_game loads it (choose_save would pick HANDOFF's older save)
            self.relaunch_save = boundary
            self.save_choice = (boundary.label, f"the runner's quicksave at {last['session']}'s end (game relaunch)")
            self.pending_notes.append(
                f"RUNNER NOTE: \"{boundary.label}\" is the runner's quicksave at {last['session']}'s end: the game "
                "stands where that session left off.")
        renv.log(f"relaunching the game: {why}; then loading {save.folder}")
        self.event("game_relaunch", why=why, save=save.folder)
        self.kill_game()
        self.state["loaded_save"] = None
        return why

    def ensure_game(self, first: bool) -> str | None:
        """Make sure the game runs with this playthrough loaded. Returns the loaded save label, if any."""
        state = game.game_state() if game.port_open() else None
        if state is not None and not self.owns_game():
            last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), {})
            started = rpolicy.game_started_at()
            if started and last.get("ended") and started > last["ended"]:
                self.note_game_gone(None)   # someone relaunched the game after the last session: it died then
        if state not in (None, *MENU_STATES, "Running"):
            # Mid-load or similar: settle at a menu or in-game; never wait for Running from the menu/splash.
            renv.log(f"game is busy (state {state}); waiting for it to settle")
            self.wait_for("the game to settle", lambda: game.game_state() in (*MENU_STATES, "Running"), 600, 3)
            state = game.game_state()
        if state == "Running" and not game.server_ready():
            self.wait_for("server context", game.server_ready, 120, 3)
        owned = self.owns_game() and self.state["loaded_save"] is not None
        if state == "Running" and game.server_ready():
            if first and not owned and not self.opt.take_over:
                raise SystemExit("the game is running with a save loaded that the runner didn't load "
                                 "(maybe your own playthrough). Quit to the main menu, close the game, or "
                                 "pass --take-over.")
            drift = self.restart_drift() if first else None
            if drift:
                save = self.choose_save()
                renv.log(f"runner restart: the running game moved since the last session ({drift}): "
                    f"loading {save.folder if save else 'nothing (no save)'}")
                self.event("restart_drift", why=drift, save=save.folder if save else None)
                if save is not None:
                    return self.load_save(save, force=True)
            if not first or owned:
                return None  # keep playing where the last session left off
        if game.port_open() and state is None:
            raise RuntimeError("debug port open but the game doesn't answer")
        if state is None:
            self.note_game_gone(self.check_game_oom())   # gone (e.g. 20:48: an OOM took the runner down with it)
        save, self.relaunch_save = getattr(self, "relaunch_save", None), None   # maybe_relaunch's boundary_save
        return self.load_save(save or self.choose_save())

    def apply_play_settings(self) -> None:
        """Best effort, never fatal: autoskip, no tutorials, dismiss popups, then the save/load gates."""
        self.gates = None
        for op, args in rpolicy.PLAY_SETTINGS:
            try:
                game.rpc(op, args, context="client")
            except Exception as e:
                renv.log(f"play settings: {op} {args} failed: {e}")
        self.apply_ui_strict()
        name = None
        if not self.new_game:
            name = game.host_character_name()
            if name is None:
                renv.log("play settings: couldn't read the avatar's name")
        owner = self.opt.save_owner or self.state["save_owner"]
        allow = self.gates = rpolicy.gates_decision(name, owner, self.new_game)
        if allow is None:
            renv.log(f"save/load gates left unchanged (character {name!r}, save owner {owner!r})")
            return
        try:
            game.rpc("ui.config", {"save_enabled": allow, "load_enabled": allow}, context="client")
            renv.log(f"save/load gates {'enabled' if allow else 'DISABLED'} "
                f"({'new game' if self.new_game else f'character {name!r}, save owner {owner!r}'})")
        except Exception as e:
            renv.log(f"play settings: ui.config gates failed: {e}")

    def apply_ui_strict(self) -> None:
        """BG3AI_UI_STRICT=1/0: bg3le's strict frame lock (patch 0010) on/off in the running game, so a game kept
        across sessions follows the switch without a relaunch (launch_game.sh passes BG3LE_UI_STRICT=1 for the
        launch itself). Unset: left as it is. Logs the lock counters it reads back."""
        on = rpolicy.ui_strict_wanted(os.environ)
        if on is None:
            return
        try:
            renv.log("ui strict: " + rpolicy.ui_strict_text(game.lua_eval(rpolicy.ui_strict_lua(on), "client")))
        except Exception as e:
            renv.log(f"ui strict: setting {'on' if on else 'off'} failed: {e}")

    def refresh_mod_snapshot(self) -> list[str]:
        """At a session boundary (before the relaunch check and the hot reload): MOD_LIVE := the main tree's mod/,
        and mod_rev := the commit it came from, which the session's MCP server (started from the same tree) runs
        too. A merge landing during the copy is copied again. -> the files changed."""
        changed: list[str] = []
        for _ in range(3):
            before = rcode.repo_commit()
            changed += rcode.sync_tree(rcode.MOD_DIR, rcode.MOD_LIVE)
            self.mod_rev = rcode.repo_commit()
            if self.mod_rev == before:
                break
        self.moved_to = None
        shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
        renv.log(f"mod snapshot {rcode.MOD_LIVE} = {self.mod_rev}"
            + (f" ({len(changed)} files updated: {shown})" if changed else " (unchanged)"))
        return changed

    def code_moved(self, label: str) -> None:
        """Logs (once per new commit) when main moved since this session's mod snapshot: an MCP server started now
        runs the newer bridge next to the game's older mod, until the next session's snapshot."""
        if self.mod_rev is None:
            return
        now = rcode.repo_commit()
        if now in (self.mod_rev, self.moved_to):
            return
        self.moved_to = now
        renv.log(f"{label}: main moved since the session's mod snapshot ({self.mod_rev} -> {now}): the game keeps "
            f"{self.mod_rev}'s mod ({rcode.MOD_LIVE}) until the next session start; an MCP server started from now on "
            f"runs {now}'s bridge")
        self.event("main_moved", session=label, snapshot=self.mod_rev, head=now)

    def hot_reload_mod(self) -> int:
        """Between sessions: if any file under MOD_LIVE changed since the game last loaded it, Ext.Debug.Reset() and
        wait for the mod's RPC table to come back. Returns the number of changed files (0 = nothing done)."""
        cur = rcode.mod_tree_hashes()
        changed = rcode.changed_files(self.state["mod_hashes"], cur)
        if not changed:
            return 0
        state = game.game_state() if game.port_open() else None
        if state not in ("Running", "Menu"):
            renv.log(f"mod changed ({len(changed)} files) but the game is {state or 'not up'}; reload skipped")
            return 0
        ctxs = ["server", "client"] if state == "Running" else ["client"]  # no server context at the menu
        try:
            game.lua_eval("Ext.Debug.Reset() return 'reset'", ctxs[0], timeout=EVAL_TIMEOUT)
        except Exception as e:
            renv.log(f"hot reload: Reset eval returned {e!r} (usually harmless)")

        def back() -> bool:
            try:
                return all(game.lua_eval(MOD_READY_LUA, c, timeout=5).strip() == "true" for c in ctxs)
            except Exception:
                return False
        time.sleep(1)
        if not self.wait_for("BG3AI mod after Ext.Debug.Reset()", back, rcode.RELOAD_TIMEOUT, 1):
            raise RuntimeError("the mod didn't come back after a hot reload")
        self.state["mod_hashes"] = cur
        shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
        renv.log(f"mod hot-reloaded ({len(changed)} files changed: {shown})")
        return len(changed)

    def recover(self, why: str) -> None:
        renv.log(f"recovering: {why}")
        heat = None
        if not self.game_alive():
            oom = self.check_game_oom()
            if oom:
                why = f"{why} (out of memory: {oom})"
            heat = self.heat_killed()
            if heat:
                why = f"{why} (the heat guard stopped it: {heat})"
        crashed_at = time.time()
        self.kill_game()
        if heat:
            self.heat_paused(after_kill=True)   # no relaunch into the same heat
        if why == "stopped responding":
            rpolicy.mark_inflight_stuck(renv.SE_BG3AI_DIR)
        save = self.choose_save()
        label = self.load_save(save)
        self.pending_notes.append(
            f"RUNNER NOTE: the game {why} at {datetime.fromtimestamp(crashed_at):%H:%M}; the runner restarted it and "
            f"resumed from save \"{label or 'main menu'}\". Anything after that save is lost: reconcile your notes "
            "and the session log against get_status before continuing.")
        self.pending_notes.append(rpolicy.crash_tools_note(f"the game {why}", rpolicy.read_actions_since(crashed_at - 600), crashed_at))

    def ensure_save(self, since: float) -> None:
        owner = self.state["save_owner"]
        new = saves_since(rcode.list_saves(), since, owner)
        if new:
            if not owner:
                self.adopt_owner(new[-1].owner)
            renv.log(f"session saved: {new[-1].folder}")
            return
        others = [s.folder for s in rcode.list_saves() if s.mtime >= since and is_ai_label(s.label, "")]
        if not self.game_alive() or not game.server_ready():
            renv.log("no save this session and the game isn't ready for one")
            return
        if others:
            # Named saves exist but don't belong to this playthrough's owner/prefix: say why, then quicksave.
            renv.log(f"session saves {others} don't match save owner {owner!r} / BG3AI_SAVE_PREFIX "
                f"{SAVE_PREFIX!r} (wrong character name?); quicksaving")
        else:
            renv.log("the agent didn't save this session; quicksaving")
        before = {s.folder for s in rcode.list_saves()}
        combat = game.party_in_combat()   # recorded with the save: boundary_save never relaunches onto a mid-fight one
        t0 = time.time()
        try:
            if "ui.menu" in game.available_ops("client"):
                game.rpc("ui.menu", {"action": "quicksave"}, context="client", timeout=30)
            else:
                game.lua_eval('Ext.Input.InjectKeyPress("F5") return 1')  # default quicksave binding
        except Exception as e:
            renv.log(f"quicksave request failed: {e}")
            return
        end = time.time() + 60
        while time.time() < end:
            fresh = [s for s in rcode.list_saves() if s.folder not in before and s.mtime >= t0 - 1]
            if fresh:
                s = max(fresh, key=lambda x: x.mtime)
                if owner and s.owner != owner:
                    renv.log(f"quicksave {s.folder} has an unexpected owner; not recording it")
                    return
                self.state["runner_saves"] = self.state["runner_saves"] + [s.folder]
                self.state["runner_save_combat"] = {**(self.state["runner_save_combat"] or {}), s.folder: combat}
                renv.log(f"quicksave written: {s.folder}" + {True: " (mid-fight)", None: " (fight state unknown)"}.get(
                    combat, ""))
                return
            time.sleep(2)
        renv.log("quicksave didn't appear within 60 s")
