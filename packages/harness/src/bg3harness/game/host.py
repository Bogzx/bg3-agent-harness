# pyright: basic
"""The GameController's host outside the runner (AppHost: the playthrough app's game) and the GameHost pieces the
runner's RunnerGameHost shares with it: the save filters (run start, our own saves, the fallback save), the mod
snapshot refresh, runner.status.json, the game's env and the runner_events.jsonl event log.

AppHost is a GameHost for one app process that plays any playthrough: begin(sdir) names the session's
runtime/sessions/<pt>/ before a launch. Its state store re-reads runner.json on every access and writes one key at a
time, since the app writes the same file too (save_owner, history).

    uv run pytest packages/harness/tests/test_game_host.py
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .. import bg3le_buildinfo, game_settings, steam_paths
from ..runner import codewatch as rcode
from ..runner import policy as rpolicy
from . import controller as gamectl
from .io import Save
from .saves import pick_save, play_windows, save_lsv

SNAPSHOT_TRIES = 3  # a merge landing during the mod copy is copied again, at most this often


def own_saves(saves: list[Save], saves_after: float | None, start_save: str | None) -> list[Save]:
    """saves minus those older than the playthrough's run start (runner.json saves_after, set for a --runs run: a
    series reuses one character name, and run 2 must never load run 1's saves); the run's start save stays."""
    if not saves_after:
        return saves
    return [s for s in saves if s.mtime >= saves_after or s.folder == start_save]


def ours(state: Any, start_save: str | None) -> set[str]:
    """Save folders pick_save takes whatever their label: the runner's quicksaves, the agent's own quicksaves
    (agent_saves, RUN-6: S059's newest good state was one) and a scenario's start save (older than every save of
    the run, so the agent's first save supersedes it)."""
    return (set(state["runner_saves"] or []) | set(state["agent_saves"] or [])
            | ({start_save} if start_save else set()))  # fmt: skip


def previous_good_save(saves: list[Save], failed: set[str], state: Any, ours: set[str],
                       windows: list[tuple[float, float]]) -> Save | None:  # fmt: skip
    """The fallback when a save keeps crashing the game on load: the last save loaded successfully (runner.json
    loaded_save), else the playthrough's newest other save. None = nothing else to try."""
    left = [s for s in saves if s.folder not in failed]
    good = state["loaded_save"]
    hit = next((s for s in left if s.folder == good), None)
    return hit or pick_save(left, None, state["save_owner"], ours, windows=windows)


def refresh_snapshot(src: Path, dst: Path, repo: Path, log: Callable[[str], None]) -> tuple[list[str], str]:
    """dst := src (the main tree's mod/ into the game's snapshot), copied again when a merge landed during the copy.
    -> (the files changed, the commit it came from)."""
    changed: list[str] = []
    rev = "unknown"
    for _ in range(SNAPSHOT_TRIES):
        before = rcode.repo_commit(repo)
        changed += rcode.sync_tree(src, dst)
        rev = rcode.repo_commit(repo)
        if rev == before:
            break
    shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
    log(f"mod snapshot {dst} = {rev}" + (f" ({len(changed)} files updated: {shown})" if changed else " (unchanged)"))
    return changed, rev


def write_status(runtime: Path, **fields: Any) -> None:
    """runtime/runner.status.json updated with `fields` (and `updated`)."""
    path = runtime / "runner.status.json"
    try:
        cur = json.loads(path.read_text())
    except (OSError, ValueError):
        cur = {}
    cur.update(fields, updated=time.time())
    runtime.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    tmp.replace(path)


def setup_env(seeded: bool) -> dict[str, str]:
    """The game's env: the mod's scn.* ops are refused unless a run seeds the dice (game_settings.SETUP_ENV)."""
    return {game_settings.SETUP_ENV: "1"} if seeded else {}


def append_event(sdir: Path, kind: str, fields: dict[str, Any], log: Callable[[str], None]) -> None:
    """sdir/runner_events.jsonl: faults, recoveries, refused launches (the dashboard's crash/recovery counts).
    Never raises."""
    try:
        with open(sdir / "runner_events.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "kind": kind, **fields}, default=str) + "\n")
    except OSError as e:
        log(f"event log failed: {e}")


class JsonStore:
    """runner.json as a StateStore that another writer shares: every read loads the file, every write merges one
    key into what is on disk now. A missing key reads None."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def __getitem__(self, k: str) -> Any:
        return self.load().get(k)

    def __setitem__(self, k: str, v: Any) -> None:
        d = self.load()
        d[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(self.path)


class AppHost:
    """bg3harness.game.controller.GameHost for the playthrough app: runtime/ (logs, launch outputs, mod-live,
    runner.status.json, actions.jsonl), the repo (launch_game.sh, mod/) and the save folder; the session's
    runtime/sessions/<pt>/ set by begin() before the controller touches the state."""

    stale_exit = bg3le_buildinfo.STALE_EXIT
    rebuild_hint = bg3le_buildinfo.REBUILD

    def __init__(self, runtime: Path, repo: Path, save_dir: Path | None = None, *,
                 log: Callable[[str], None] = lambda msg: None, aborted: Callable[[], bool] = lambda: False,
                 saves: Callable[[], Iterable[Save]] | None = None):  # fmt: skip
        self.runtime, self.repo = runtime, repo
        self.launch_sh = repo / "runner" / "launch_game.sh"
        self.mod_src = repo / "mod"
        self.mod_live = runtime / "mod-live"
        self.save_dir = steam_paths.saves_dir() if save_dir is None else save_dir
        self._log, self._aborted = log, aborted
        self._saves = saves or (lambda: gamectl.list_saves(self.save_dir, save_lsv))
        self.sdir: Path | None = None
        self.mod_rev: str | None = None  # the commit of the last mod snapshot
        self.notes: list[str] = []  # RUNNER NOTEs for the agent's next prompt (take_notes)

    def begin(self, sdir: Path) -> None:
        self.sdir = sdir

    def _dir(self) -> Path:
        if self.sdir is None:
            raise RuntimeError("AppHost: no session began (begin(sdir)): whose runner.json is unknown")
        return self.sdir

    @property
    def state(self) -> JsonStore:
        return JsonStore(self._dir() / "runner.json")

    def log(self, msg: str) -> None:
        self._log(msg)

    def event(self, kind: str, **fields: Any) -> None:
        self._log(f"{kind} {json.dumps(fields, default=str)}")
        if self.sdir is not None:
            append_event(self.sdir, kind, fields, self._log)

    def status(self, **fields: Any) -> None:
        write_status(self.runtime, **fields)

    def steam_ready(self) -> tuple[bool, str]:
        return gamectl.steam_ready(steam_paths)

    def bg3le_problems(self) -> list[str]:
        return gamectl.bg3le_problems(bg3le_buildinfo.problems, bg3le_buildinfo.DIST)

    def setup_env(self) -> dict[str, str]:
        return setup_env(False)

    def refresh_mod_snapshot(self) -> list[str]:
        changed, self.mod_rev = refresh_snapshot(self.mod_src, self.mod_live, self.repo, self._log)
        return changed

    def mod_hashes(self) -> dict[str, str]:
        return rcode.mod_tree_hashes(self.mod_live)

    def aborted(self) -> bool:
        return self._aborted()

    def own_saves(self) -> list[Save]:
        return own_saves(list(self._saves()), self.state["saves_after"], None)

    def previous_good_save(self, failed: set[str]) -> Save | None:
        st = self.state
        windows = play_windows(st["history"] or [], self._dir())
        return previous_good_save(self.own_saves(), failed, st, ours(st, None), windows)

    def read_actions_since(self, t: float) -> list[dict[str, Any]]:
        return rpolicy.read_actions_since(t, self.runtime / "actions.jsonl")

    def note(self, text: str) -> None:
        self.notes.append(text)

    def take_notes(self) -> list[str]:
        out, self.notes = self.notes, []
        return out
