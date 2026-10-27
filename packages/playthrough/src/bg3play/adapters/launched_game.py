"""A game the app launched and relaunches (HarnessRunner's Game, the recovering one; AttachedGame only reads).

    begin(sdir, mem)    the session's playthrough: runner.json (sdir) and memory (mem/HANDOFF.md); the host's too
    ok / progress       the bridge's reads (AttachedGame's)
    fault               "crash" when the game process is gone, "hang" when it doesn't answer (ClientAgent's watch)
    oom                 why the dead game died, when it ran out of memory (GameController.check_oom, once per launch)
    recover(why)        kill what is left, choose the save, GameController.launch_and_load; the loaded save's label
                        (runner.json loaded_save = its folder: the controller's check_oom and previous_good_save
                        read it)
    new_game            a playthrough without a save: launched to the main menu (the agent starts the new game)
    save_env            the MCP server's save identity for the session (policy.save_env, play.py's builder)
    ensure_save(since)  at a session's end (play.py ensure_save): the session's newest save, else a runner quicksave
                        recorded in runner.json runner_saves (SaveOps: IoSaveOps over the debug port)
    notes               the controller's RUNNER NOTEs since the last call (the host's take_notes)

The save choice (choose_save) is runner/play.py Runner.choose_save over bg3harness.game.saves: the newest save of
this playthrough (or HANDOFF's "Save to load"), never another owner's, never one written while another playthrough
played (play windows), never one older than the playthrough's run start (runner.json saves_after: a series reuses a
character name) and never an OOM-blocked one. No owner known: it is learned from the newest agent save written in
this playthrough's play windows; with none, the playthrough is new (NoSaveYetError): a session start launches a new game
to the main menu (new_game), a recovery mid-session has nothing to load.

The game process goes through the Controller Protocol, which bg3harness.game.controller.GameController fills over
bg3harness.game.host.AppHost (launch_game.sh, the mod snapshot, the Steam/bg3le/display guards, the session's
runner.json as its state store): server.session_runner wires them; `bg3play-serve --attach` wires AttachedGame instead.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from bg3harness.game import host as ghost
from bg3harness.game import saves as hsaves
from bg3harness.game.io import Save
from bg3harness.game.saves import parse_handoff_save, pick_save_why
from bg3harness.runner import policy

from ..jsontypes import is_list, is_obj
from ..memory import memlib


class Controller(Protocol):
    """The game process (bg3harness.game.controller.GameController)."""

    def alive(self) -> bool: ...
    def kill(self) -> None: ...
    def launch_and_load(self, save: Save) -> Save: ...  # the save that loaded; LaunchGaveUp after its retries
    def launch(self, load_folder: str | None) -> None: ...  # None: to the main menu
    def wait_menu(self) -> bool: ...  # up to the main menu, past the splash; False: it never came up
    def check_oom(self) -> str | None: ...  # after a death: the OOM cause, or None


class Host(Protocol):
    """The controller's GameHost as the app drives it (bg3harness.game.host.AppHost)."""

    def begin(self, sdir: Path) -> None: ...  # the session's runner.json dir, before the controller touches state
    def take_notes(self) -> list[str]: ...


class Reads(Protocol):
    """The bridge's reads of the running game (AttachedGame)."""

    def ok(self) -> bool: ...
    def progress(self) -> dict[str, Any] | None: ...


class SaveOps(Protocol):
    """Saving the running game (IoSaveOps: bg3harness.game.io over the debug port)."""

    def ready(self) -> bool: ...  # a game is loaded (the server context answers): a save can be made
    def in_combat(self) -> bool | None: ...  # the party is in a fight (None: unknown)
    def quicksave(self) -> None: ...  # asks the game to quicksave; raises when the request fails


class IoSaveOps:
    def ready(self) -> bool:
        from bg3harness.game import io

        return io.server_ready()

    def in_combat(self) -> bool | None:
        from bg3harness.game import io

        return io.party_in_combat()

    def quicksave(self) -> None:
        from bg3harness.game import io

        if "ui.menu" in io.available_ops("client"):
            io.rpc("ui.menu", {"action": "quicksave"}, context="client", timeout=30)  # pyright: ignore[reportUnknownMemberType]
        else:
            io.lua_eval('Ext.Input.InjectKeyPress("F5") return 1')  # the default quicksave binding


QUICKSAVE_WAIT_S = 60  # how long ensure_save waits for the quicksave's folder


def read_state(path: Path) -> dict[str, Any]:
    try:
        v: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if is_obj(v) else {}


def write_state(path: Path, **fields: Any) -> None:
    """runner.json's fields set (save_owner: play.py adopt_owner; loaded_save: load_save), the rest of the file kept."""
    d = read_state(path) | fields
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(path)


def choose_save(
    saves: list[Save], state: dict[str, Any], handoff: str, sdir: Path, running: tuple[float, float] | None = None
) -> tuple[Save | None, str]:
    """The save a recovery loads, and why (play.py choose_save + own_saves; the OOM-blocked saves left out).
    `running`: the running session's start..now, a play window too (its saves count). The run start (saves_after)
    is judged by the save's own time (game.host.own_saves): a copy of a series' earlier run that Steam restores at a
    launch has mtime = the launch."""
    owner = state.get("save_owner")
    if not owner:
        return None, "no save owner known for this playthrough (runner.json save_owner): any save could be another's"
    after = state.get("saves_after")
    blocked = set(state.get("oom_blocked") or [])
    ours = set(state.get("runner_saves") or []) | set(state.get("agent_saves") or [])
    own = [s for s in ghost.own_saves(saves, after, None) if s.folder not in blocked]
    raw: object = state.get("history")
    history: list[dict[str, Any]] = [h for h in raw if is_obj(h)] if is_list(raw) else []
    windows = hsaves.play_windows(history, sdir, running)  # pyright: ignore[reportUnknownMemberType]
    ends = hsaves.session_ends(history, sdir)  # pyright: ignore[reportUnknownMemberType]
    return pick_save_why(own, parse_handoff_save(handoff), owner, ours, windows=windows,
                         handoff_session=handoff_session(handoff), ends=ends)  # fmt: skip


class NoSaveYetError(RuntimeError):
    """The playthrough has no save owner (no save of its own): a new playthrough. At a session start the runner
    launches a new game instead (LaunchedGame.new_game); mid-session there is nothing to relaunch into."""


def learn_owner(
    saves: list[Save], state: dict[str, Any], sdir: Path, running: tuple[float, float] | None = None
) -> str | None:
    """No save owner yet: the owner of the newest agent save written while this playthrough played (its sessions'
    windows, `running` the running session's), as play.py adopts it from the session's own saves (I3). None: it has
    none."""
    raw: object = state.get("history")
    history: list[dict[str, Any]] = [h for h in raw if is_obj(h)] if is_list(raw) else []
    windows = hsaves.play_windows(history, sdir, running)  # pyright: ignore[reportUnknownMemberType]
    mine = [s for s in saves if hsaves.is_ai_save(s) and hsaves.in_windows(s.mtime, windows)]  # pyright: ignore[reportUnknownMemberType]
    return max(mine, key=lambda s: s.mtime).owner if mine else None


def handoff_session(handoff: str) -> int | None:
    """The session that wrote HANDOFF.md: its '# Handoff Sxxx' heading (play.py parse_handoff_session)."""
    m = memlib.HANDOFF_RE.search(re.sub(r"<!--.*?-->", "", handoff, flags=re.S))
    return int(m.group(1)) if m else None


class LaunchedGame:
    def __init__(
        self,
        reads: Reads,
        controller: Controller,
        host: Host,
        saves: Callable[[], list[Save]],  # the save folder (bg3harness.game.controller.list_saves)
        leader: Callable[[], str | None],  # the running game's character (bg3harness.game.io.host_character_name)
        log: Callable[[str], None] = lambda msg: None,
        save_ops: SaveOps | None = None,  # None: ensure_save makes no save
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        actions: Callable[[float], list[dict[str, Any]]] = lambda since: [],  # the MCP server's tool log since t
    ):
        self.reads, self.ctl, self.host, self.saves, self.leader, self.log = reads, controller, host, saves, leader, log
        self.save_ops, self.clock, self.sleep, self.actions = save_ops, clock, sleep, actions
        self.sdir: Path | None = None
        self.mem: Path | None = None
        self.began = 0.0

    def begin(self, sdir: Path, mem: Path) -> None:
        self.sdir, self.mem, self.began = sdir, mem, self.clock()
        self.host.begin(sdir)

    def running(self) -> tuple[float, float]:
        """The running session's play window: from begin (its start) to now."""
        return self.began, self.clock()

    def save_env(self) -> dict[str, str]:
        """The MCP server's save identity for this session (policy.save_env, as play.py's): the playthrough's owner
        and prefix, its saves-after, and the owner's saves written outside its play windows (excluded). Without it
        the server's list_saves is empty and load_game refuses the playthrough's own saves (live9 S002)."""
        if self.sdir is None:
            return {}
        state = read_state(self.sdir / "runner.json")
        owner, after = state.get("save_owner"), state.get("saves_after")
        ours = set(state.get("runner_saves") or []) | set(state.get("agent_saves") or [])
        raw: object = state.get("history")
        history: list[dict[str, Any]] = [h for h in raw if is_obj(h)] if is_list(raw) else []
        windows = hsaves.play_windows(history, self.sdir, self.running())  # pyright: ignore[reportUnknownMemberType]
        listed = self.saves() if owner and not after else None
        return policy.save_env(owner, listed, ours, windows, after)  # pyright: ignore[reportUnknownMemberType]

    def ok(self) -> bool:
        ok = self.reads.ok()
        if ok and self.sdir is not None and not read_state(self.sdir / "runner.json").get("save_owner"):
            owner = self.leader()  # the playthrough's first game: its character owns its saves
            if owner:
                write_state(self.sdir / "runner.json", save_owner=owner)
        return ok

    def progress(self) -> dict[str, Any] | None:
        return self.reads.progress()

    def fault(self) -> str | None:
        if not self.ctl.alive():
            return "crash"
        return None if self.reads.ok() else "hang"

    def oom(self) -> str | None:
        return None if self.ctl.alive() else self.ctl.check_oom()

    def recover(self, why: str) -> str | None:
        if self.sdir is None or self.mem is None:
            raise RuntimeError("no session began: which playthrough's save to load is unknown")
        handoff = self.mem / "HANDOFF.md"
        try:
            text = handoff.read_text(encoding="utf-8")
        except OSError:
            text = ""
        saves, state = self.saves(), read_state(self.sdir / "runner.json")
        if not state.get("save_owner"):
            owner = learn_owner(saves, state, self.sdir, self.running())
            if owner is None:
                raise NoSaveYetError("this playthrough has no save yet (no save owner known)")
            self.log(f"save owner {owner}: learned from this playthrough's own saves")
            write_state(self.sdir / "runner.json", save_owner=owner)
            state["save_owner"] = owner
        save, how = choose_save(saves, state, text, self.sdir, self.running())
        self.log(f"recovering (the game {why}): save choice {save.folder if save else 'none'}: {how}")
        if save is None:
            raise RuntimeError(f"no save to load: {how}")
        self.ctl.kill()
        loaded = self.ctl.launch_and_load(save)
        write_state(self.sdir / "runner.json", loaded_save=loaded.folder)
        return loaded.label

    def is_new(self) -> bool:
        """No save owner known or learnable from this playthrough's saves: its first game is still to be made."""
        if self.sdir is None:
            return False
        state = read_state(self.sdir / "runner.json")
        return not state.get("save_owner") and learn_owner(self.saves(), state, self.sdir) is None

    def new_game(self) -> None:
        """A playthrough without a save: the game launched to the main menu, where the agent starts a new game (and
        creates the character, whose name becomes the save owner)."""
        if self.sdir is None:
            raise RuntimeError("no session began: whose game is unknown")
        self.log("no save for this playthrough yet: launching the game to the main menu (new game)")
        self.ctl.kill()
        self.ctl.launch(None)
        if not self.ctl.wait_menu():
            raise RuntimeError("the main menu never came up")
        write_state(self.sdir / "runner.json", loaded_save=None)

    def ensure_save(self, since: float) -> str | None:
        """play.py ensure_save at a session's end: the newest save this session wrote (the first one names the save
        owner of a new playthrough), else a quicksave the runner asks for and records (runner.json runner_saves, its
        fight state in runner_save_combat), so the next session doesn't go back to an older save. None: no save."""
        if self.sdir is None:
            return None
        path = self.sdir / "runner.json"
        owner = read_state(path).get("save_owner")
        new = hsaves.saves_since(self.saves(), since, owner)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if new and not owner:
            owner = new[-1].owner
            write_state(path, save_owner=owner)
            self.log(f"save owner {owner}: learned from the session's save")
        quick = self.record_agent_saves(since, owner)
        if new:
            last: Save = new[-1]
            self.log(f"session saved: {last.folder}")
            return last.folder
        if quick:
            self.log(f"session saved: the agent's quicksave {quick[-1]}")
            return quick[-1]
        ops = self.save_ops
        if ops is None or not self.ctl.alive() or not ops.ready():
            self.log("no save this session and the game isn't ready for one")
            return None
        self.log("the agent didn't save this session; quicksaving")
        before = {s.folder for s in self.saves()}
        combat = ops.in_combat()  # recorded with the save: a relaunch never lands mid-fight by choice
        t0 = self.clock()
        try:
            ops.quicksave()
        except Exception as e:
            self.log(f"quicksave request failed: {e}")
            return None
        while True:
            fresh = [s for s in self.saves() if s.folder not in before and s.mtime >= t0 - 1]
            if fresh:
                s = max(fresh, key=lambda x: x.mtime)
                if owner and s.owner != owner:
                    self.log(f"quicksave {s.folder} has an unexpected owner; not recording it")
                    return None
                st = read_state(path)
                saved: object = st.get("runner_saves")
                fights: object = st.get("runner_save_combat")
                write_state(path, runner_saves=[*(saved if is_list(saved) else []), s.folder],
                            runner_save_combat={**(fights if is_obj(fights) else {}), s.folder: combat})  # fmt: skip
                self.log(f"quicksave written: {s.folder}" + (" (mid-fight)" if combat else ""))
                return s.folder
            if self.clock() - t0 >= QUICKSAVE_WAIT_S:
                self.log(f"quicksave didn't appear within {QUICKSAVE_WAIT_S} s")
                return None
            self.sleep(2)

    def record_agent_saves(self, since: float, owner: str | None) -> list[str]:
        """play.py record_agent_saves: the owner's quicksaves the MCP server wrote since `since` (save_game.bg lines
        in actions.jsonl) -> runner.json agent_saves (the last AGENT_SAVES_KEPT), so a recovery may load them
        (choose_save takes a quicksave only when it is the runner's or the agent's)."""
        if self.sdir is None:
            return []
        found: list[str] = policy.agent_quicksaves(self.actions(since), owner)  # pyright: ignore[reportUnknownMemberType]
        if found:
            path = self.sdir / "runner.json"
            raw: object = read_state(path).get("agent_saves")
            kept = [f for f in (raw if is_list(raw) else []) if f not in found] + found
            write_state(path, agent_saves=kept[-policy.AGENT_SAVES_KEPT :])
            self.log(f"the agent's quicksave(s) {', '.join(found)} are this playthrough's saves too")
        return found

    def notes(self) -> list[str]:
        return self.host.take_notes()
