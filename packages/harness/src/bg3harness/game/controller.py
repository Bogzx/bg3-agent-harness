# pyright: basic
"""The GameController (step 6 of research/arch/harness.md §4, second half; moved from runner/play.py's Runner): the
game process this runner launched (owned or not, launch, kill, the waits up to the menu or a loaded save, the
launch retries) and the OOM check after it died. Which save to load stays with the runner (HANDOFF, profile, notes).

Everything outside the game goes through the GameHost passed in: the playthrough's state store (runner.json: game,
loaded_save, mod_hashes, game_killed_at, oom_*), logging, events, the status file, the Steam/bg3le/display guards'
inputs and the runner's paths. play.py's host reads its module globals on each call, so a test patches play.

    python3 -m unittest runner/test_crash.py runner/test_play_game.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from bg3ai import saves as bsaves

from .. import display_env, ramwatch
from . import io as game
from . import oom as O  # noqa: N812

KILL_GRACE = 3  # the game ignores SIGTERM (S024: still up after 10 s): SIGKILL it after this many s
KILL_CONFIRM = 30  # then wait this long for the processes and port 9998 to go away
WAIT_REPORT_AFTER = 90  # a runner wait longer than this logs what it waits for ...
WAIT_REPORT_EVERY = 30  # ... every this many seconds
LAUNCH_ATTEMPTS = 3  # a launch that dies before the save plays is retried; the 3rd try loads the previous
LAUNCH_BACKOFF = (15, 45)  # good save; then the runner stops (alert). Seconds to wait before the 2nd and 3rd try
MENU_STATES = ("Menu", "LoadMenu")  # client game states at the main menu or its splash ("Press any key")
SPLASH_LOADMENU_S = 10  # LoadMenu this long without the main menu = the splash waiting for a key
MENU_TIMEOUT = 180  # debug port up → main menu (splash pressed past), else wait_menu fails
MENU_POLL = 3  # seconds between menu/splash checks
STALE_EXIT_WITHIN_S = 30  # launch_game.sh exiting STALE_EXIT this soon after the launch = its stale-bg3le refusal


class StateStore(Protocol):
    """The playthrough's runner.json (play.RunnerState): a write saves the file."""

    def __getitem__(self, k: str, /) -> Any: ...
    def __setitem__(self, k: str, v: Any, /) -> None: ...


class GameHost(Protocol):
    """What the controller needs from the runner."""

    runtime: Path  # runtime/ (logs/, launch-*.out, ram_spikes.jsonl, ram_kills.jsonl, actions.jsonl)
    launch_sh: Path  # runner/launch_game.sh
    mod_live: Path  # the session's mod snapshot the game loads
    stale_exit: int  # launch_game.sh's exit status for a stale bg3le build
    rebuild_hint: str  # how to rebuild bg3le (the refusal's log line)

    @property
    def state(self) -> StateStore: ...
    def log(self, msg: str) -> None: ...
    def event(self, kind: str, **fields: Any) -> None: ...
    def status(self, **fields: Any) -> None: ...
    def steam_ready(self) -> tuple[bool, str]: ...
    def bg3le_problems(self) -> list[str]: ...
    def setup_env(self) -> dict[str, str]: ...
    def refresh_mod_snapshot(self) -> list[str]: ...
    def mod_hashes(self) -> dict[str, str]: ...
    def aborted(self) -> bool: ...
    def own_saves(self) -> list[game.Save]: ...
    def previous_good_save(self, failed: set[str]) -> game.Save | None: ...
    def read_actions_since(self, t: float) -> list[dict[str, Any]]: ...
    def note(self, text: str) -> None: ...


class SteamPaths(Protocol):
    def offline_required(self) -> tuple[bool, str]: ...
    def steam_offline(self) -> tuple[bool, str]: ...


def steam_ready(steam_paths: SteamPaths) -> tuple[bool, str]:
    """(ok to launch BG3?, why): a Steam client is running and, on a machine that requires it
    (steam_paths.offline_required: $BG3AI_REQUIRE_STEAM_OFFLINE=1 or ~/.config/bg3ai/require-steam-offline; machine
    2), Steam's last login was offline. Not required by default (RUN-11: a public user's runner launched never)."""
    required, req_why = steam_paths.offline_required()
    if not required:
        ok, why = True, f"Steam offline {req_why}"
    else:
        ok, why = steam_paths.steam_offline()
        why = f"{why} [offline required: {req_why}]"
    if not ok:
        return False, why
    r = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, text=True, check=False)
    if not r.stdout.strip():
        # libsteam_api would start Steam itself, maybe online
        return False, f"Steam isn't running ({why})"
    return True, why


def bg3le_problems(problems: Callable[[Path], list[str]], dist: Path) -> list[str]:
    """Why the bg3le build launch_game.sh would preload is stale ([] = current, or $BG3AI_ALLOW_STALE_BG3LE=1).
    problems = tools/bg3le_buildinfo.problems, dist its DIST."""
    if os.environ.get("BG3AI_ALLOW_STALE_BG3LE") == "1":
        return []
    lib = Path(os.environ.get("BG3AI_BG3LE_LIB", dist / "libbg3le.so"))
    return problems(lib.parent)


def list_saves(save_dir: Path, save_lsv: Callable[[Path, str], Path | None]) -> list[game.Save]:
    """The saves under save_dir; a save's time is its <folder>/<label>.lsv's (save_lsv = runs.save_lsv), its saved
    time the one its meta.lsf holds."""
    out: list[game.Save] = []
    try:
        entries = list(os.scandir(save_dir))
    except OSError:
        return out
    for e in entries:
        if not e.is_dir():
            continue
        lsv = save_lsv(save_dir, e.name)
        try:
            mtime = lsv.stat().st_mtime if lsv is not None else e.stat().st_mtime
        except OSError:
            continue
        s = game.parse_save_folder(e.name, mtime)
        if s:
            meta = bsaves.read_meta(lsv) if lsv is not None else None
            out.append(dataclasses.replace(s, saved=meta[1]) if meta else s)
    return out


class WaitReporter:
    """Watchdog for long runner waits: past WAIT_REPORT_AFTER s, logs what it waits for every WAIT_REPORT_EVERY s
    (so a silent runner log always says why it's silent)."""

    def __init__(self, what: str, timeout: float | None, start: float | None = None,
                 logf: Callable[[str], None] = print):  # fmt: skip
        self.what, self.timeout = what, timeout
        self.start = time.time() if start is None else start
        self.next = self.start + WAIT_REPORT_AFTER
        self.log = logf

    def tick(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now < self.next:
            return False
        left = f", {self.timeout - (now - self.start):.0f} s left" if self.timeout else ""
        self.log(f"still waiting for {self.what} ({now - self.start:.0f} s so far{left})")
        while self.next <= now:
            self.next += WAIT_REPORT_EVERY
        return True


class GameController:
    """The game process this runner launched, and the waits on it."""

    def __init__(self, host: GameHost):
        self.host = host
        self.proc: subprocess.Popen[bytes] | None = None
        self.watch: game.LaunchWatch | None = None

    def resume_watch(self) -> None:
        """The game a previous run of the runner launched (runner.json's record): keep watching its bg3le logs."""
        g = self.host.state["game"] or {}
        if g.get("stamp"):
            self.watch = game.LaunchWatch(g["stamp"], self.host.runtime / "logs", g.get("launched"))

    def owned_group(self) -> int | None:
        """The launcher's process group from runner.json's game record, only while it is provably that launch
        (RUN-4): its leader is runtime launch_game.sh with the start time recorded at launch (records from before
        2026-10-16 have none: the command line alone). None after a reboot or a pid reuse: never signalled then."""
        g = self.host.state["game"] or {}
        pg = g.get("pgid")
        if not pg:
            return None
        # after a self-restart (execv) the game is still our child: reap it if it exited, or it stays a zombie
        with contextlib.suppress(OSError):
            os.waitpid(pg, os.WNOHANG)
        start = game.proc_start(pg)
        if start is None:  # the leader (launch_game.sh) is gone: the group is ours only if it holds a harness game
            return pg if any(game._pgid(p) == pg for p in game.pids_of_game()) else None
        if b"launch_game.sh" not in game.proc_cmdline(pg):
            return None
        if g.get("pg_start") is not None and g["pg_start"] != start:
            return None
        return pg

    def owns_game(self) -> bool:
        """True if the running game is one this runner (or a previous run of it) launched."""
        if self.proc is not None and self.proc.poll() is None:
            return True
        pg = self.owned_group()
        if pg is None:
            return False
        try:
            os.killpg(pg, 0)
            return True
        except OSError:
            return False

    def alive(self) -> bool:
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return game.port_open()

    def launch(self, load_folder: str | None) -> None:
        h = self.host
        ok, why = h.steam_ready()
        h.status(steam_offline=ok, steam_why=why)
        if not ok:
            h.log(
                f"!!!!! REFUSING TO LAUNCH BG3: Steam not ready: {why}. Start Steam (in offline mode where offline "
                "is required: Steam > Go Offline...) and leave it running; the runner keeps retrying. !!!!!"
            )
            h.event("steam_refused", why=why)
            raise game.SteamNotOffline(why)
        self.refuse_stale_bg3le(h.bg3le_problems())
        foreign = game.foreign_bg3_pids()
        if foreign:  # RUN-4: a user's own BG3 (Steam would hand a second launch to it, or two games fight)
            why = f"a BG3 this harness didn't launch is running (pid {', '.join(map(str, foreign))})"
            h.status(alert={"t": time.time(), "kind": "foreign_game", "msg": f"not launching: {why}"[:600]})
            h.log(
                f"!!!!! REFUSING TO LAUNCH BG3: {why}; it is never killed by the runner. Close it to let the runner "
                "launch its own game; the runner keeps retrying. !!!!!"
            )
            h.event("foreign_game", pids=foreign)
            raise game.ForeignGameRunning(why)
        if game.port_open():
            raise RuntimeError(f"port {game.DEBUG_PORT} is still in use; can't launch another game")
        # Xwayland restarted since the runner started: its new -auth file
        filled = display_env.fill(os.environ)  # pyright: ignore[reportArgumentType]
        if filled:
            h.log("display env refreshed from the desktop session: " + " ".join(f"{k}={v}" for k, v in filled.items()))
        no_display = display_env.problem(dict(os.environ))
        if no_display:
            h.log(f"!!!!! REFUSING TO LAUNCH BG3: no usable display: {no_display}; the runner keeps retrying. !!!!!")
            h.event("display_refused", why=no_display)
            raise game.NoDisplay(no_display)
        if not (h.mod_live / "Mods").is_dir():
            h.refresh_mod_snapshot()
        # the session's mod snapshot, also for a relaunch mid-session (never main's newer mod/)
        args = [str(h.launch_sh), "--mod-path", str(h.mod_live)] + (["--load", load_folder] if load_folder else [])
        stamp = base = f"{datetime.now():%Y%m%d-%H%M%S}"  # launch_game.sh names its logs with it (BG3AI_LAUNCH_STAMP)
        n = 1
        while list((h.runtime / "logs").glob(f"*-{stamp}.log*")):  # never another launch's logs (LaunchWatch)
            stamp, n = f"{base}-{n}", n + 1
        logf = open(h.runtime / f"launch-{stamp}.out", "w")  # noqa: SIM115 - handed to the game, closed below
        h.log(f"launching game: {' '.join(args)}")
        self.proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True,
                                     env={**os.environ, "BG3AI_LAUNCH_STAMP": stamp, **h.setup_env()})  # fmt: skip
        logf.close()  # the game has its own copy of the descriptor
        now = time.time()
        self.watch = game.LaunchWatch(stamp, h.runtime / "logs", now)
        h.state["game"] = {"pgid": self.proc.pid, "pg_start": game.proc_start(self.proc.pid), "launched": now,
                           "load": load_folder, "stamp": stamp}  # fmt: skip
        h.state["mod_hashes"] = h.mod_hashes()  # a fresh game loads the mod as it is now
        h.status(game_pgid=self.proc.pid)

    def refuse_stale_bg3le(self, bad: list[str]) -> None:
        """A stale bg3le build (bg3le_problems, or launch_game.sh's exit 4): status, event, loud log, Bg3leStale."""
        h = self.host
        h.status(bg3le_stale=bad or None)
        if not bad:
            return
        msg = "; ".join(bad)
        h.log(
            f"!!!!! REFUSING TO LAUNCH BG3: stale bg3le build ({msg}). Run {h.rebuild_hint} (with the game "
            "closed), then restart the runner; BG3AI_ALLOW_STALE_BG3LE=1 launches it anyway. !!!!!"
        )
        h.event("bg3le_stale", problems=bad)
        h.status(
            alert={"t": time.time(), "kind": "bg3le_stale", "msg": f"runner stopped: stale bg3le build ({msg})"[:600]}
        )
        raise game.Bg3leStale(msg)

    def kill(self) -> None:
        """Stop the game: SIGTERM, then SIGKILL after KILL_GRACE s (bg3 ignores SIGTERM), on both the
        launcher's process group (only while owned_group proves it is that launch) and this harness's bg3 processes
        (pids_of_game: never a user's own BG3, RUN-4); then require port 9998 to be free."""
        h = self.host
        h.log("killing the game")
        groups: list[int] = []
        if self.proc is not None and self.proc.poll() is None:
            groups.append(self.proc.pid)
        else:
            pg = self.owned_group()
            if pg:
                groups.append(pg)
            elif (h.state["game"] or {}).get("pgid"):
                h.log(
                    f"not signalling process group {h.state['game']['pgid']}: not provably our launch any more "
                    "(a reboot or a reused pid)"
                )
        foreign = game.foreign_bg3_pids()
        if foreign:
            h.log(f"leaving bg3 pid(s) {', '.join(map(str, foreign))} alone: not launched by this harness")

        def send(sig: int) -> None:
            for pgid in groups:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, sig)
            for pid in game.pids_of_game() + game.crash_reporter_pids():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)

        def gone() -> bool:
            return not game.pids_of_game() and not game.crash_reporter_pids() and not game.port_open()

        send(signal.SIGTERM)
        end = time.time() + KILL_GRACE
        while time.time() < end and not gone():
            time.sleep(1)
        if not gone():
            h.log(f"game still running {KILL_GRACE} s after SIGTERM; SIGKILL")
            send(signal.SIGKILL)
            end = time.time() + KILL_CONFIRM
            while time.time() < end and not gone():
                time.sleep(1)
        if self.proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
        self.proc = None
        self.watch = None
        h.state["game"] = None
        h.state["game_killed_at"] = time.time()
        if game.pids_of_game():
            raise RuntimeError(f"bg3 processes survive SIGKILL: {game.pids_of_game()}")
        if game.port_open():
            raise RuntimeError(f"port {game.DEBUG_PORT} is still in use after the game was killed")

    def wait_for(self, what: str, pred: Callable[[], bool], timeout: float, interval: float = 2.0) -> bool:
        h = self.host
        start = time.time()
        end = start + timeout
        report = WaitReporter(what, timeout, start, h.log)
        while time.time() < end:
            report.tick()
            if self.proc is not None and self.proc.poll() is not None:
                launched = (h.state["game"] or {}).get("launched") or 0
                if self.proc.returncode == h.stale_exit and time.time() - launched < STALE_EXIT_WITHIN_S:
                    # launch_game.sh's own stale-build refusal (the backstop of the check in launch)
                    self.refuse_stale_bg3le([f"runner/launch_game.sh exited {h.stale_exit} "
                                             f"(stale bg3le build, see {h.runtime}/launch-*.out)"])  # fmt: skip
                raise game.GameDied(f"game exited (status {self.proc.returncode}) while waiting for {what}")
            if h.aborted():
                raise RuntimeError(f"aborted while waiting for {what}")
            why = self.watch.check() if self.watch is not None else None
            if why:
                h.log(f"game died while waiting for {what}: {why}")
                raise game.GameDied(f"{why} while waiting for {what}")
            if pred():
                return True
            time.sleep(interval)
        h.log(f"timed out after {timeout:.0f} s waiting for {what}")
        return False

    def wait_menu(self) -> bool:
        """Up to the main menu, pressing past the "Press any key" splash. The client state there is Menu or LoadMenu
        (it flips between them on the splash): never a reason to wait for Running."""
        if not self.wait_for("debug port", game.port_open, 240):
            return False
        if not self.wait_for("client game state Menu", lambda: game.game_state() in MENU_STATES, 300):
            return False
        if "ui.state" not in game.available_ops("client"):
            for _ in range(2):  # without ui.state (no mod yet): two blind presses
                self.try_menu_key()
                time.sleep(MENU_POLL)
            return True
        loadmenu_since: float | None = None
        presses = 0

        def at_menu() -> bool:
            nonlocal loadmenu_since, presses
            state, st = game.game_state(), game.ui_state() or {}
            if state == "Menu" and st.get("screen") == "main_menu":
                return True
            loadmenu_since = (loadmenu_since or time.time()) if state == "LoadMenu" else None
            # Only on the splash: a key on the main menu itself would hit Continue (someone else's save, maybe).
            if (state == "Menu" and game.at_splash(st)) or (
                loadmenu_since is not None and time.time() - loadmenu_since >= SPLASH_LOADMENU_S
            ):
                presses += 1
                how = self.try_menu_key()
                self.host.log(f"splash screen (state {state}): pressed {how} ({presses})")
                loadmenu_since = None  # give the press time to act before the next one
            return False

        return self.wait_for("the main menu", at_menu, MENU_TIMEOUT, MENU_POLL)

    def try_menu_key(self) -> str:
        try:
            return game.press_menu_key(self.host.log)
        except Exception as e:
            self.host.log(f"key press failed: {e}")
            return "nothing (failed)"

    def wait_running(self) -> bool:
        """After a load was started: Running, then the server context. A splash on the way gets a key press."""

        def running() -> bool:
            state = game.game_state()
            if state == "Running":
                return True
            if state == "Menu" and game.at_splash(game.ui_state()):
                self.host.log(f"splash screen while waiting for the load: pressed {self.try_menu_key()}")
            return False

        return self.wait_for("game state Running", running, 600, MENU_POLL) and self.wait_for(
            "server context", game.server_ready, 120, 3
        )

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.host.aborted():
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def launch_and_load(self, save: game.Save) -> game.Save:
        """Launch the game with this save and wait until it plays; returns the save that loaded. A launch that dies on
        the way (LaunchWatch, launcher exit, load timeout) is killed and retried after LAUNCH_BACKOFF; after the
        second failure with the host's previous_good_save(). LAUNCH_ATTEMPTS failures in a row → LaunchGaveUp (the
        runner stops: a crash loop needs a human, e.g. a bg3le fix)."""
        h = self.host
        failed: list[str] = []
        errors: list[str] = []
        target = save
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            try:
                self.launch(target.folder)
                if not self.wait_for("debug port", game.port_open, 240):
                    raise game.GameDied("the debug port never opened")
                if not self.wait_running():
                    raise game.GameDied(f"save {target.folder} didn't finish loading")
            except game.GameDied as e:
                failed.append(target.folder)
                errors.append(f"{target.folder}: {e}")
                h.log(f"launch {attempt}/{LAUNCH_ATTEMPTS} with {target.folder} failed: {e}")
                h.event("launch_failed", attempt=attempt, save=target.folder, error=str(e)[:300])
                self.kill()
                if attempt == LAUNCH_ATTEMPTS:
                    break
                if h.aborted():
                    raise RuntimeError("aborted between launch attempts") from e
                if attempt >= 2:
                    prev = h.previous_good_save(set(failed))
                    if prev is not None:
                        target = prev
                wait = LAUNCH_BACKOFF[min(attempt, len(LAUNCH_BACKOFF)) - 1]
                h.log(f"retrying in {wait} s with {target.folder}")
                self.sleep(wait)
                continue
            if failed:
                h.log(f"game up after {len(failed)} failed launch(es): {target.folder} loaded")
                if target.folder != save.folder:
                    h.note(
                        f'RUNNER NOTE: the newest save "{save.label}" crashed the game on load {len(failed)} times; '
                        f'the runner loaded the older save "{target.label}" instead. Progress after it is lost '
                        "in the game: replay it, and fix HANDOFF.md/objectives if they assume the newer save."
                    )
            return target
        raise game.LaunchGaveUp(
            f"{LAUNCH_ATTEMPTS} launches in a row died before the save played: " + "; ".join(errors)
        )

    def give_up_launch(self, e: game.LaunchGaveUp) -> None:
        """The loud stop after LaunchGaveUp: runner log, runner_events (dashboard) and a status alert."""
        h = self.host
        h.log(f"!!!!! GAME CRASH LOOP: {e}. The runner stops; fix the crash (bg3le log in runtime/logs), "
              "then restart the runner. !!!!!")  # fmt: skip
        h.event("launch_gave_up", error=str(e)[:600])
        h.status(alert={"t": time.time(), "kind": "launch_gave_up",
                        "msg": f"runner stopped: the game crashes on every launch ({e})"[:600]})  # fmt: skip

    def check_oom(self) -> str | None:
        """After the game died (recover(), or a runner start finding it gone): did the kernel OOM-kill it? Updates
        the consecutive-OOM streak for the save that game had loaded; at OOM_SAVE_LIMIT that save is blocked
        (the runner's choose_save refuses it). Checked once per launch. -> the OOM reason, or None."""
        h, st = self.host, self.host.state
        g = st["game"] or {}
        stamp, launched = g.get("stamp"), g.get("launched")
        if not stamp or not launched or st["oom_checked"] == stamp:
            return None
        st["oom_checked"] = stamp
        oom = O.find_game_oom(O.user_journal(launched), stamp, O.read_spikes(launched, h.runtime / "ram_spikes.jsonl"))
        kills = [k for k in O.read_spikes(launched, h.runtime / "ram_kills.jsonl") if k.get("kind") == "kill"]
        if not oom and kills:
            oom = (f"ramwatch killed the game at {datetime.fromtimestamp(kills[-1]['t']):%H:%M:%S} before the OOM "
                   f"killer could: {kills[-1].get('why', '?')}")  # fmt: skip
        acts = h.read_actions_since(launched)
        save = O.last_loaded_save(acts, h.own_saves(), st["loaded_save"])
        streak = O.oom_streak_update(st["oom_streak"], save, oom)
        if streak:
            spikes = O.read_spikes(launched, h.runtime / "ram_spikes.jsonl")
            last = ramwatch.last_tools(acts[-1]["t"], h.runtime / "actions.jsonl") if acts else []
            streak["tools"] = spikes[-1].get("tools", []) if spikes else last
        st["oom_streak"] = streak
        if not oom:
            return None
        h.log(
            f"!!!!! GAME OOM: {oom}; save loaded before it: {save} (OOM {streak['n'] if streak else 1} in a row) !!!!!"
        )
        h.event("game_oom", why=oom[:300], save=save, streak=streak["n"] if streak else 1,
                tools=(streak or {}).get("tools", [])[-8:])  # fmt: skip
        if streak and save and streak["n"] >= O.OOM_SAVE_LIMIT:
            st["oom_blocked"] = sorted(set(st["oom_blocked"] or []) | {save})
        return oom
