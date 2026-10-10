"""Background saves: save_game returns at once and the agent keeps playing while the game saves.

S009 had 7 saves at ~2.75 s of tool time each (~4 s of agent idle with the turn around it): ~0.25 s of RPCs,
~1.2 s of the mod driving the save menus (open the Save page, New save, name, accept, close the menus), then
~1.5 s of the bridge polling the save folder until the game had written it. Now:

* the menu phase (~2 s with the game's ~1 s save freeze) runs in a thread while the agent reads the result and thinks; the next tool call
  waits for it only if it arrives sooner (the menus are on screen, so world/UI tools must not interleave);
* the write phase (the folder appearing with a stable .lsv) is confirmed in the background; the agent gets
  one line on its next result: "SAVED 'S010-pre-kagha'" or "SAVE FAILED ...: call save_game again";
* when the game can't save right now (a conversation, a roll, a cutscene: the Save command is disabled), the
  save is queued and runs at the next moment it can, between the agent's tool calls.

The mod hides its own save menus from ui.state while it drives them (UIState: screen stays "game",
saving=true), so a running queue's watcher doesn't take them for a popup and interrupt the walk.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .saves import save_file

UI_WAIT_S = 8.0        # the longest another tool waits for the save menus to close
DISK_WAIT_S = 20.0     # the write must show up on disk within this
DEFER_POLL_S = 1.0     # queued save: how often to ask whether the game can save now
DEFER_MAX_S = 600.0    # queued save gives up after this
STABLE_S = 0.25        # the .lsv size must hold this long before the save counts as written


def written_save(save_dir: Path, label: str, since: float) -> Path | None:
    """The save folder written for this label since `since` (wall clock), or None.
    Named saves end in __<label>; quicksaves (label "") contain QuickSave."""
    if not save_dir.is_dir():
        return None
    best = None
    for d in save_dir.iterdir():
        if not d.is_dir():
            continue
        if label and not d.name.endswith("__" + label):
            continue
        if not label and "QuickSave" not in d.name:
            continue
        try:
            st = save_file(d).stat()
        except OSError:
            continue
        if st.st_mtime >= since - 1.0 and st.st_size > 0 and (best is None or st.st_mtime > best[0]):
            best = (st.st_mtime, d)
    return best[1] if best else None


def on_disk(save_dir: Path, label: str, since: float) -> str:
    """For a failed save: what IS on disk for this label, so a wrong check (S017: the .lsv is named after the
    label, not the folder) shows at once instead of passing for a lost save."""
    if not save_dir.is_dir():
        return f"save dir missing: {save_dir}"
    want = (lambda n: n.endswith("__" + label)) if label else (lambda n: "QuickSave" in n)
    seen = []
    for d in sorted((d for d in save_dir.iterdir() if d.is_dir() and want(d.name)),
                    key=lambda d: d.stat().st_mtime, reverse=True)[:3]:
        files = []
        for f in sorted(d.iterdir()):
            try:
                st = f.stat()
                files.append(f"{f.name} {st.st_size}B {st.st_mtime - since:+.0f}s")
            except OSError:
                files.append(f.name)
        seen.append(f"{d.name}: [{', '.join(files) or 'empty'}]")
    if not seen:
        return f"no folder *__{label or 'QuickSave*'} in {save_dir}"
    return "folders with this name (file, size, mtime vs save start): " + "; ".join(seen)


class Saver:
    """One save at a time. Tools call enter()/leave() around their body (the _guard wrapper)."""

    def __init__(self, run_menu: Callable[[str], tuple[bool, str]], can_save: Callable[[], tuple[bool, str]],
                 save_dir: Callable[[], Path], log: Callable[[dict], None] | None = None) -> None:
        self.run_menu = run_menu      # label -> (ok, detail): drives the save menus until the game has saved (blocking, ~2 s)
        self.can_save = can_save      # () -> (ok, why not)
        self.save_dir = save_dir
        self.log = log or (lambda rec: None)
        self.cv = threading.Condition()
        self.ui_busy = False          # the save menus are on screen (tools wait)
        self.active = 0               # tool bodies running (a queued save starts only between them)
        self.pending: dict | None = None
        self.notes: list[str] = []
        self.thread: threading.Thread | None = None

    # ---- tool gate ----

    def enter(self, timeout: float = UI_WAIT_S) -> float:
        """Before a tool body: wait while the save menus are up. Returns the seconds waited."""
        t0 = time.monotonic()
        with self.cv:
            end = t0 + timeout
            while self.ui_busy and time.monotonic() < end:
                self.cv.wait(max(0.0, end - time.monotonic()))
            self.active += 1
        return time.monotonic() - t0

    def leave(self) -> None:
        with self.cv:
            self.active = max(0, self.active - 1)
            self.cv.notify_all()

    # ---- notes for the next tool result ----

    def take_notes(self) -> str:
        with self.cv:
            out, self.notes = self.notes, []
        return "\n".join(out)

    def _note(self, s: str) -> None:
        with self.cv:
            self.notes.append(s)

    def status(self) -> str | None:
        p = self.pending
        if not p:
            return None
        return f"saving '{p['label'] or 'quicksave'}' ({p['phase']})"

    # ---- saving ----

    def save(self, label: str, sync: bool = False) -> str:
        """Start a save and return the line for the agent. Called from inside a tool (active >= 1).
        sync=True runs it to the end (on disk) and returns the outcome (session over: nothing comes after)."""
        name = label or "quicksave"
        with self.cv:
            if self.pending is not None:
                if self.pending["label"] == label:
                    return f"save_game: '{name}' is already being saved ({self.pending['phase']})"
                return (f"save_game: REFUSED — '{self.pending['label'] or 'quicksave'}' is still being saved "
                        f"({self.pending['phase']}); call again once it says SAVED")
        ok, why = self.can_save()
        job = {"label": label, "t0": time.monotonic(), "wall": time.time(), "phase": "menu" if ok else "queued"}
        if sync:
            if not ok:
                return f"save_game: FAILED — the game can't save right now ({why})"
            with self.cv:
                self.pending = job
                self.ui_busy = True
            self._run(job)
            return self.take_notes() or f"save_game: done '{name}'"
        with self.cv:
            self.pending = job
            if ok:
                self.ui_busy = True  # before returning: the agent's next call waits for the menus
        self.thread = threading.Thread(target=self._run, args=(job,), name="saver", daemon=True)
        self.thread.start()
        if ok:
            return (f"save_game: saving '{name}' in the background (~2 s with the game's save freeze; your next call waits for "
                    "them). Keep playing; SAVED shows on a later result.")
        return (f"save_game: queued '{name}': the game can't save right now ({why}). It saves at the next "
                "moment it can, between your calls; keep playing.")

    def _run(self, job: dict) -> None:
        name = job["label"] or "quicksave"
        try:
            if not self.ui_busy:
                if not self._wait_for_moment(job):
                    return
            t_menu = time.monotonic()
            job["wall"] = time.time()
            try:
                ok, detail = self.run_menu(job["label"])
            except Exception as e:  # noqa: BLE001 - any failure becomes a note, never a dead thread
                ok, detail = False, str(e)[:160]
            menu_s = time.monotonic() - t_menu
            with self.cv:
                self.ui_busy = False
                self.cv.notify_all()
            if not ok:
                self._note(f"⚠ SAVE FAILED '{name}': {detail}; call save_game again")
                self.log({"save": name, "ok": False, "menu_s": round(menu_s, 2), "detail": detail})
                return
            job["phase"] = "writing"
            t_disk = time.monotonic()
            found = self._wait_on_disk(job)
            disk_s = time.monotonic() - t_disk
            total = time.monotonic() - job["t0"]
            rec = {"save": name, "ok": bool(found), "menu_s": round(menu_s, 2), "disk_s": round(disk_s, 2),
                   "total_s": round(total, 2), "detail": detail, "folder": found.name if found else None}
            if not found:
                try:
                    rec["seen"] = on_disk(self.save_dir(), job["label"], job["wall"])
                except OSError as e:
                    rec["seen"] = f"save dir unreadable: {e}"
            self.log(rec)
            if found:
                self._note(f"SAVED '{name}' ({total:.1f}s, on disk)")
            else:
                self._note(f"⚠ SAVE FAILED '{name}': no save folder appeared within {DISK_WAIT_S:.0f}s "
                           f"({detail}). On disk: {rec['seen']}. Call save_game again")
        finally:
            with self.cv:
                self.ui_busy = False
                if self.pending is job:
                    self.pending = None
                self.cv.notify_all()

    def _wait_for_moment(self, job: dict) -> bool:
        """Queued save: wait until the game can save and no tool is running, then take the screen."""
        end = job["t0"] + DEFER_MAX_S
        why = ""
        while time.monotonic() < end and not job.get("cancel"):
            with self.cv:
                while self.active > 0 and time.monotonic() < end and not job.get("cancel"):
                    self.cv.wait(0.5)
            ok, why = self.can_save()  # RPCs: outside the lock, so tools don't queue behind them
            with self.cv:
                if job.get("cancel"):
                    return False
                if ok and self.active == 0:
                    self.ui_busy = True
                    job["phase"] = "menu"
                    return True
            time.sleep(DEFER_POLL_S)
        if job.get("cancel"):
            return False
        self._note(f"⚠ SAVE FAILED '{job['label'] or 'quicksave'}': the game could not save for "
                   f"{DEFER_MAX_S / 60:.0f} min ({why}); call save_game again")
        return False

    def _wait_on_disk(self, job: dict) -> Path | None:
        end = time.monotonic() + DISK_WAIT_S
        last: tuple[str, int] | None = None
        while time.monotonic() < end:
            d = written_save(self.save_dir(), job["label"], job["wall"])
            if d is not None:
                try:
                    size = save_file(d).stat().st_size
                except OSError:
                    size = -1
                if last == (d.name, size):
                    return d
                last = (d.name, size)
                time.sleep(STABLE_S)
                continue
            time.sleep(0.2)
        return None

    def settle(self, timeout: float = DISK_WAIT_S + 5) -> str:
        """Before a load: drop a queued save (it would save the world being thrown away), let a running one
        finish writing. Returns a note for the agent ('' if nothing was pending)."""
        p = self.pending
        if p is None:
            return ""
        if p["phase"] == "queued":
            p["cancel"] = True
            note = f"queued save '{p['label'] or 'quicksave'}' dropped (loading)"
        else:
            note = ""
        t = self.thread
        if t is not None:
            t.join(timeout)
        return note
