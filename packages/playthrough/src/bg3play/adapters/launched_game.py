"""A game the app launched and relaunches (HarnessRunner's Game, the recovering one; AttachedGame only reads).

    begin(sdir, mem)    the session's playthrough: runner.json (sdir) and memory (mem/HANDOFF.md); the host's too
    ok / progress       the bridge's reads (AttachedGame's)
    fault               "crash" when the game process is gone, "hang" when it doesn't answer (ClientAgent's watch)
    oom                 why the dead game died, when it ran out of memory (GameController.check_oom, once per launch)
    recover(why)        kill what is left, choose the save, GameController.launch_and_load; the loaded save's label
                        (runner.json loaded_save = its folder: the controller's check_oom and previous_good_save
                        read it)
    notes               the controller's RUNNER NOTEs since the last call (the host's take_notes)

The save choice (choose_save) is runner/play.py Runner.choose_save over bg3harness.game.saves: the newest save of
this playthrough (or HANDOFF's "Save to load"), never another owner's, never one written while another playthrough
played (play windows), never one older than the playthrough's run start (runner.json saves_after: a series reuses a
character name) and never an OOM-blocked one. No owner known = no save is safe to load: recover refuses.

The game process goes through the Controller Protocol, which bg3harness.game.controller.GameController fills over
bg3harness.game.host.AppHost (launch_game.sh, the mod snapshot, the Steam/bg3le/display guards, the session's
runner.json as its state store): server.session_runner wires them; `bg3play-serve --attach` wires AttachedGame instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from bg3harness.game import saves as hsaves
from bg3harness.game.io import Save
from bg3harness.game.saves import parse_handoff_save, pick_save_why

from ..jsontypes import is_list, is_obj
from ..memory import memlib


class Controller(Protocol):
    """The game process (bg3harness.game.controller.GameController)."""

    def alive(self) -> bool: ...
    def kill(self) -> None: ...
    def launch_and_load(self, save: Save) -> Save: ...  # the save that loaded; LaunchGaveUp after its retries
    def check_oom(self) -> str | None: ...  # after a death: the OOM cause, or None


class Host(Protocol):
    """The controller's GameHost as the app drives it (bg3harness.game.host.AppHost)."""

    def begin(self, sdir: Path) -> None: ...  # the session's runner.json dir, before the controller touches state
    def take_notes(self) -> list[str]: ...


class Reads(Protocol):
    """The bridge's reads of the running game (AttachedGame)."""

    def ok(self) -> bool: ...
    def progress(self) -> dict[str, Any] | None: ...


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


def choose_save(saves: list[Save], state: dict[str, Any], handoff: str, sdir: Path) -> tuple[Save | None, str]:
    """The save a recovery loads, and why (play.py choose_save + own_saves; the OOM-blocked saves left out)."""
    owner = state.get("save_owner")
    if not owner:
        return None, "no save owner known for this playthrough (runner.json save_owner): any save could be another's"
    after = state.get("saves_after")
    blocked = set(state.get("oom_blocked") or [])
    ours = set(state.get("runner_saves") or []) | set(state.get("agent_saves") or [])
    own = [s for s in saves if s.folder not in blocked and not (after and s.mtime < after)]
    raw: object = state.get("history")
    history: list[dict[str, Any]] = [h for h in raw if is_obj(h)] if is_list(raw) else []
    windows = hsaves.play_windows(history, sdir)  # pyright: ignore[reportUnknownMemberType]
    ends = hsaves.session_ends(history, sdir)  # pyright: ignore[reportUnknownMemberType]
    return pick_save_why(own, parse_handoff_save(handoff), owner, ours, windows=windows,
                         handoff_session=handoff_session(handoff), ends=ends)  # fmt: skip


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
    ):
        self.reads, self.ctl, self.host, self.saves, self.leader, self.log = reads, controller, host, saves, leader, log
        self.sdir: Path | None = None
        self.mem: Path | None = None

    def begin(self, sdir: Path, mem: Path) -> None:
        self.sdir, self.mem = sdir, mem
        self.host.begin(sdir)

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
        save, how = choose_save(self.saves(), read_state(self.sdir / "runner.json"), text, self.sdir)
        self.log(f"recovering (the game {why}): save choice {save.folder if save else 'none'}: {how}")
        if save is None:
            raise RuntimeError(f"no save to load: {how}")
        self.ctl.kill()
        loaded = self.ctl.launch_and_load(save)
        write_state(self.sdir / "runner.json", loaded_save=loaded.folder)
        return loaded.label

    def notes(self) -> list[str]:
        return self.host.take_notes()
