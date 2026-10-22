"""A game the app launched and relaunches (HarnessRunner's Game, the recovering one; AttachedGame only reads).

    begin(sdir, mem)    the session's playthrough: runner.json (sdir) and memory (mem/HANDOFF.md)
    ok / progress       the bridge's reads (AttachedGame's)
    fault               "crash" when the game process is gone, "hang" when it doesn't answer (ClientAgent's watch)
    oom                 why the dead game died, when it ran out of memory (GameController.check_oom, once per launch)
    recover(why)        kill what is left, choose the save, GameController.launch_and_load; the loaded save's label

The save choice (choose_save) is runner/play.py Runner.choose_save over bg3harness.game.saves: the newest save of
this playthrough (or HANDOFF's "Save to load"), never another owner's, never one written while another playthrough
played (play windows), never one older than the playthrough's run start (runner.json saves_after: a series reuses a
character name) and never an OOM-blocked one. No owner known = no save is safe to load: recover refuses.

The game process goes through the Controller Protocol, which bg3harness.game.controller.GameController fills. Its
GameHost (launch_game.sh, the mod snapshot, the Steam/bg3le/display guards, runner.json as its state store) is still
play.py's Runner; until bg3harness has one for the app, the server wires AttachedGame (research/ovh/app.md).
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


def write_owner(path: Path, owner: str) -> None:
    """runner.json's save_owner (play.py adopt_owner), the rest of the file kept."""
    d = read_state(path) | {"save_owner": owner}
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
        saves: Callable[[], list[Save]],  # the save folder (bg3harness.game.controller.list_saves)
        leader: Callable[[], str | None],  # the running game's character (bg3harness.game.io.host_character_name)
        log: Callable[[str], None] = lambda msg: None,
    ):
        self.reads, self.ctl, self.saves, self.leader, self.log = reads, controller, saves, leader, log
        self.sdir: Path | None = None
        self.mem: Path | None = None

    def begin(self, sdir: Path, mem: Path) -> None:
        self.sdir, self.mem = sdir, mem

    def ok(self) -> bool:
        ok = self.reads.ok()
        if ok and self.sdir is not None and not read_state(self.sdir / "runner.json").get("save_owner"):
            owner = self.leader()  # the playthrough's first game: its character owns its saves
            if owner:
                write_owner(self.sdir / "runner.json", owner)
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
        return self.ctl.launch_and_load(save).label
