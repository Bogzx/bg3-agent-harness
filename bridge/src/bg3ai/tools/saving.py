"""Save and load tools: save_game, list_saves, load_game.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .. import delta, metrics, saver, saves
from ..game import GameError, JobResult, NotReady
from . import batch, core, plumbing, queueing, reporting, travel, turn_plan, world_tools

# ---------------------------------------------------------------------------
# meta: saves, waiting, markers, wiki, clock
# ---------------------------------------------------------------------------

# Save identity (runner I1, settings.SaveIdentity): read once at start; newgame/tests set these names.
SAVE_OWNER = core.SETTINGS.saves.owner
SAVE_PREFIX = core.SETTINGS.saves.prefix
PLAYTHROUGH = core.SETTINGS.saves.playthrough
SAVE_GATES = core.SETTINGS.saves.gates
SAVES_AFTER = core.SETTINGS.saves.after
START_SAVE = core.SETTINGS.saves.start_save
SAVES_EXCLUDE = core.SETTINGS.saves.exclude
AI_LABEL = re.compile(r"S\d{3}-")


def _set_save_owner(name: str) -> None:
    """After create_character (I3): a new playthrough's saves are the new character's, from this session on."""
    global SAVE_OWNER, SAVE_PREFIX
    SAVE_OWNER = SAVE_OWNER or name
    SAVE_PREFIX = SAVE_PREFIX or f"{name}-"


def _restore_save_owner(before: tuple[str, str]) -> None:
    global SAVE_OWNER, SAVE_PREFIX
    SAVE_OWNER, SAVE_PREFIX = before


def _is_mine(folder: str) -> bool:
    """A save folder of this playthrough's character: the prefix, then only the game's number (so prefix
    'Claude-' doesn't take 'Claude-Two-123__…', another character)."""
    if not SAVE_PREFIX or not folder.startswith(SAVE_PREFIX):
        return False
    rest = folder.split("__", 1)[0][len(SAVE_PREFIX):].lstrip("-")
    return rest == "" or rest.isdigit()


def _save_record() -> Path | None:
    return core.RUNTIME / "saves" / (re.sub(r"[^A-Za-z0-9_.-]", "_", PLAYTHROUGH) + ".txt") if PLAYTHROUGH else None


def _recorded_saves() -> set[str]:
    """Folders this playthrough's save_game wrote (quicksaves and saves under the game's default name included)."""
    f = _save_record()
    try:
        return set(f.read_text().split("\n")) - {""} if f is not None else set()
    except OSError:
        return set()


def _latest_ok(folder: str) -> bool:
    """What load_game("latest") may pick: the agent's Sxxx- saves and what save_game wrote for this playthrough;
    never a worker's LIVETEST-*, the game's AutoSave_N or a QuickSave_N someone else made."""
    return bool(AI_LABEL.match(folder.split("__", 1)[-1])) or folder in _recorded_saves()


def _my_saves() -> list[Path]:
    """This playthrough's save folders, newest first (none older than SAVES_AFTER, except a scenario's START_SAVE:
    an agent wiped before its first own save can load it)."""
    if not core.SAVE_DIR.is_dir():
        return []
    return sorted((d for d in core.SAVE_DIR.iterdir() if d.is_dir() and d.name not in SAVES_EXCLUDE and (
                   d.name == START_SAVE or (_is_mine(d.name) and d.stat().st_mtime >= SAVES_AFTER))),
                  key=lambda d: d.stat().st_mtime, reverse=True)


def _enable_save_gates() -> None:
    """Saving/loading is gated off in the mod by default (protects a human's own playthrough). The runner
    sets BG3AI_PLAYTHROUGH only for the AI's own game, and BG3AI_SAVE_GATES to what it decided for the game
    that is up (0: not this playthrough's character): the gates open only for the AI's own game."""
    if not PLAYTHROUGH:
        raise GameError("saving/loading is disabled outside an AI playthrough session (BG3AI_PLAYTHROUGH unset)")
    if SAVE_GATES == "0":
        core.game.try_rpc("ui.config", {"save_enabled": False, "load_enabled": False}, context="client")
        raise GameError("saving/loading is disabled for this game: the runner closed the gates (the loaded "
                        "character is not this playthrough's)")
    core.game.rpc("ui.config", {"save_enabled": True, "load_enabled": True}, context="client")


def _save_menus(label: str) -> tuple[bool, str]:
    """Drive the game's save menus for one save (blocking, ~1 s; runs in the saver thread). (ok, detail)"""
    if not label:
        r = core.game.run_job("ui.menu", {"action": "quicksave"}, context="client", wait_s=30)
        return r.status == "done", plumbing._job_text("quicksave", r)
    r = core.game.run_job("ui.menu", {"action": "save", "name": label}, context="client", wait_s=30)
    res = r.result if isinstance(r.result, dict) else {}
    if (r.status == "done" and res.get("needs_text")) or (r.status == "failed" and res.get("needs_key")):
        # An in-game key/text didn't land: send it through the input shim, then let the mod continue.
        inp = world_tools._inp()
        if inp is None:
            return False, "the save menus need the input shim (" + (r.error or "needs_text") + "); try save_game(label=\"quicksave\")"
        if res.get("needs_text"):
            inp.text(label)
        inp.key(res.get("needs_key") or "RETURN")
        r = core.game.run_job("ui.menu", {"action": "save_confirm", "name": label}, context="client", wait_s=40)
        res = r.result if isinstance(r.result, dict) else {}
    ms = res.get("ms")
    detail = "menus " + " ".join(f"{k}={v}" for k, v in ms.items()) if isinstance(ms, dict) else plumbing._job_text("save", r)
    return r.status == "done", detail


def _save_blocker() -> str:
    """A party walk/action still running (a queue, an async job): the save waits for it. S078 00:4x: save_game during
    a queued travel_to failed at the menus ("in-game RETURN did not advance 'new'"); the retry standing still
    worked. "" when nothing runs."""
    if queueing._queues().busy():
        q = next((q for q in queueing._queues().status() if q.get("running") or q.get("waiting")), None)
        what = (q.get("running") or (q.get("waiting") or ["?"])[0]) if q else "?"
        return f"a walk/action is running (queue {queueing._label(q['who']) if q else '?'}: {str(what)[:60]})"
    jobs = queueing._running_jobs()
    if jobs:
        info = plumbing._async.get(jobs[0]) or {}
        return f"a walk/action is running ({jobs[0]} {info.get('tool') or ''})".replace(" )", ")")
    return ""


SAVE_WALK_WAIT_S = 20.0   # a session-end (sync) save waits this long for a running walk/action to finish


def _can_save() -> tuple[bool, str]:
    """Whether the game takes a save now: no party walk/action running, no party member in a conversation,
    nothing modal on screen and the Save command enabled."""
    busy = _save_blocker()
    if busy:
        return False, busy
    # S096 11:36:2x: the Emperor's conversation had started (dialogue_started) but its window wasn't up yet, so
    # ui.state read "game" (our save page masked as "game"): the failed save wasn't requeued. The server knows first.
    w = core.game.try_rpc("act.watch", {"radius": 0}) or {}
    if isinstance(w, dict) and w.get("dialogue"):
        return False, "a conversation is running or starting"
    ui = core.game.try_rpc("ui.state", context="client") or {}
    scr = (ui.get("screen") or "game") if isinstance(ui, dict) else "game"
    if scr not in ("game", "game_menu", "save_load"):
        return False, f"{scr} on screen"
    dry = core.game.try_rpc("ui.menu", {"action": "save", "dry": True}, context="client")
    if not (isinstance(dry, dict) and dry.get("can")):
        return False, "the game has saving disabled here"
    return True, ""


def _save_log(rec: dict) -> None:
    try:
        core.RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(core.RUNTIME / "actions.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "tool": "save_game.bg", **rec}) + "\n")
        folder, rf = rec.get("folder") or rec.get("stray"), _save_record()
        if folder and rf is not None:
            rf.parent.mkdir(parents=True, exist_ok=True)
            with open(rf, "a") as f:
                f.write(folder + "\n")
    except OSError:
        pass


SAVER = saver.Saver(_save_menus, _can_save, lambda: core.SAVE_DIR, _save_log)


@core.mcp.tool()
@reporting._guard
def save_game(label: str = "") -> str:
    """Save the game (returns at once: the save runs in the background; SAVED shows on a later result). label: short name for a named save (e.g. "Sxxx-pre-kagha"); empty = named after the session and the time (Sxxx-HHMM); "quicksave" = the game's quicksave. If the game can't save right now (conversation, roll, a walk/action still running) it is queued for the next moment it can."""
    t0 = time.monotonic()
    _enable_save_gates()
    label = re.sub(r"[^A-Za-z0-9 _-]", "", label)[:40] if label else ""  # the mod's name filter (no dots)
    if label.lower() == "quicksave":
        label = ""
    elif not label.strip():
        # S070 21:14:32: save_game(label="") made a quicksave where a named save was meant (load_game "latest"
        # and the keeper look for Sxxx- names): name it after the session; refused without one
        sess = os.environ.get("BG3AI_SESSION") or ""
        if not sess:
            return reporting._finish("save_game", {"label": label}, "save_game: REFUSED — no label: give one (e.g. "
                           "label=\"S070-before-boss\"), or label=\"quicksave\" for the game's quicksave", t0)
        label = f"{sess}-{time.strftime('%H%M')}"
    sync = core._session_over()  # session over: nothing comes after, so wait for the disk
    end = time.monotonic() + SAVE_WALK_WAIT_S
    while sync and _save_blocker() and time.monotonic() < end:
        time.sleep(0.3)   # the walk ends first (the queued form waits for it between calls instead)
    body = SAVER.save(label, sync=sync)
    return reporting._finish("save_game", {"label": label}, body, t0)


@core.mcp.tool()
@reporting._guard
def list_saves(limit: int = 10) -> str:
    """Your most recent saves (as the load menu shows them); "latest" marks the one load_game("latest") loads."""
    t0 = time.monotonic()
    if SAVE_PREFIX:
        dirs = _my_saves()
    elif not PLAYTHROUGH and core.SAVE_DIR.is_dir():  # a hand-run server: every save
        dirs = sorted((d for d in core.SAVE_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    else:
        dirs = []
    latest = next((d.name for d in dirs if _latest_ok(d.name)), None) if SAVE_PREFIX else None
    body = "\n".join(f"{time.strftime('%m-%d %H:%M', time.localtime(d.stat().st_mtime))}  {d.name.split('__', 1)[-1]}  "
                     f"[{d.name}]{'  <- latest' if d.name == latest else ''}" for d in dirs[:limit])
    if not body and PLAYTHROUGH and not SAVE_PREFIX:
        body = "No saves (this playthrough's character isn't known yet: create it, or save once the runner knows it)."
    return reporting._finish("list_saves", {}, body or "No saves.", t0, events=False)


GAME_ALIVE_MIN_MB = 256  # the Steam runtime's `bg3` parent is ~60 MB; the game itself is GBs once at the menu


def _game_process_alive(proc: Path = Path("/proc")) -> bool | None:
    """Whether the game runs on this machine (None when /proc can't be read). The Steam runtime starts two "bg3"
    processes, a ~60 MB parent and the game as its child: only a bg3 of GAME_ALIVE_MIN_MB+ counts, so a dead game
    child with a lingering parent takes the crash fast path instead of waiting out the load."""
    try:
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    if (d / "comm").read_text().strip() == "bg3" and \
                            (metrics.rss_mb(int(d.name), proc) or 0) >= GAME_ALIVE_MIN_MB:
                        return True
                except OSError:
                    continue
        return False
    except OSError:
        return None


LOAD_START_S = 8.0  # a pressed Load leaves the Running state within this


def _server_boot() -> str | None:
    """The server Lua state's id (sys.ping boot): a new one after every load. None when it can't be read."""
    p = core.game.try_rpc("sys.ping", context="server")
    return p.get("boot") if isinstance(p, dict) else None


def _load_not_done(boot0: str | None) -> str:
    """'' when the load evidently happened, else why not (the game is Running again at this point)."""
    for _ in range(10):  # the Load page can take a moment to go after the state flips
        ui = core.game.try_rpc("ui.state", context="client") or {}
        scr = ui.get("screen") if isinstance(ui, dict) else None
        if scr not in ("save_load", "main_menu", "loading"):
            break
        time.sleep(0.5)
    else:
        return f"the game is Running but the {scr.replace('_', ' ')} screen is still up: the save did not load"
    boot = _server_boot()
    if boot0 and boot and boot == boot0:
        return "the game is Running but nothing was loaded (the same session as before the call)"
    return ""


def _load_started(max_s: float) -> bool:
    """True once the game leaves Running (the load began), or when it can't be read (a load resets the mod)."""
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        try:
            if core.game.game_state() != "Running":
                return True
        except (GameError, NotReady, OSError):
            return True
        time.sleep(0.25)
    return False


LOAD_RETRIES = 1  # extra Load presses when a press didn't start a load


def _load_once(save: str, boot0: str | None, alive_at_start: bool | None) -> tuple[JobResult, bool]:
    """One select+press of the save and the wait for it. (result, True) when the press didn't start a load
    (worth pressing again); a crash, a timeout or a real load is final."""
    # The mod selects the save on the Load page (by playthrough, position and date) and presses Load; the
    # load then resets the mod's Lua, so a poll may fail mid-way: wait for the game to be Running again.
    try:
        r = core.game.run_job("ui.menu", saves.load_args(core.SAVE_DIR, save), context="client", wait_s=40)
        if r.status == "failed" and isinstance(r.result, dict) and r.result.get("needs_key") and world_tools._inp() is not None:
            world_tools._inp().key(r.result["needs_key"])
            r = JobResult("done", {"loading": True, "via": "shim"})
    except (GameError, NotReady) as e:
        r = JobResult("done", {"loading": True, "note": str(e)[:80]})
    if r.status == "done" and not (isinstance(r.result, dict) and r.result.get("loading")):
        # Live 11:2x: during the post-rest sleep the Load press did nothing, the page stayed open and the old check
        # (Running + server answering) said "done" after 1.7 s. A real load leaves Running within a few seconds.
        if not _load_started(LOAD_START_S):
            return JobResult("failed", None, "the Load press didn't start a load (no loading screen within "
                             f"{LOAD_START_S:.0f} s, pressed {LOAD_RETRIES + 1} times; the game may be mid-transition, "
                             "e.g. the long rest's sleep): nothing was loaded"), True
    if r.status == "failed" and isinstance(r.result, dict) and r.result.get("loading"):
        # live window #1 (10-16 18:34): load_game right after a launch met the loading screen. The mod's job waits
        # for it (25 s) without pressing anything; still up: worth one more job (nothing was pressed or loaded).
        return r, True
    if r.status != "done":
        return r, False
    deadline, ok, dead = time.monotonic() + 120, False, False
    time.sleep(1.0)
    # Pass 4 10:40: the game SIGSEGV'd 1 s into a load and this waited the full 120 s: a gone process ends it.
    while time.monotonic() < deadline and not ok:
        if alive_at_start and _game_process_alive() is False:
            dead = True
            break
        try:
            ok = core.game.game_state() == "Running" and core.game.server_ready()
        except (GameError, NotReady, OSError):
            ok = False
        if not ok:
            time.sleep(0.5)
    # vis/se live: a "done" with nothing loaded broke every later step. Running again is not enough: the
    # server Lua state must be a new one (a load rebuilds it) and the Load page must be gone.
    why = _load_not_done(boot0) if ok else ""
    if why:
        return JobResult("failed", None, why), True
    if ok:
        return JobResult("done", {"loaded": save.split("__", 1)[-1]}), False
    if dead:
        return JobResult("failed", None, "the game CRASHED during the load (its process is gone; the bg3le log in "
                         "runtime/logs has the crash): it has to be restarted"), False
    return JobResult("failed", None, "the game did not come back to Running within 120 s"), False


@core.mcp.tool()
@reporting._guard
def load_game(save: str = "latest") -> str:
    """Load one of YOUR saves (folder name from list_saves, or "latest"). Use after a party wipe or a clear mistake."""
    t0 = time.monotonic()
    _enable_save_gates()
    dropped = SAVER.settle()  # a background save finishes writing first; a queued one is dropped
    mine = _my_saves()
    if save == "latest":
        ours = [d for d in mine if _latest_ok(d.name)]
        if not ours:
            raise GameError("no Sxxx- save of this playthrough found " + (
                f"(folders {SAVE_PREFIX}*; name one from list_saves)" if SAVE_PREFIX else
                "(its character isn't known: BG3AI_SAVE_OWNER/BG3AI_SAVE_PREFIX unset)"))
        save = ours[0].name
    elif not any(d.name == save or d.name.endswith("__" + save) for d in mine):
        raise GameError(f"'{save}' is not one of this playthrough's saves; see list_saves")
    else:
        save = next(d.name for d in mine if d.name == save or d.name.endswith("__" + save))
    retried_fight, fight_block = _fight_before_load()
    alive_at_start = _game_process_alive()
    boot0 = _server_boot()
    retried = 0
    while True:
        r, retry = _load_once(save, boot0, alive_at_start)
        if not retry or retried >= LOAD_RETRIES or _game_process_alive() is False:
            break
        # Acceptance 12:13: once the Load press did nothing (the page stayed up, "try again" after 7.7 s) and the
        # next call loaded fine. Press again here: the job reuses an open Load page, reselects and presses.
        retried += 1
        time.sleep(1.0)
    if retry:  # still not loading: close the Load page and the game menu under it, so the game view is back
        ui = core.game.try_rpc("ui.state", context="client") or {}
        if isinstance(ui, dict) and ui.get("screen") in ("save_load", "game_menu"):
            for _ in range(2):
                core.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
                time.sleep(0.4)
    elif retried and r.status == "done" and isinstance(r.result, dict):
        r.result["pressed"] = f"{retried + 1} times (the first press didn't start a load)"
    core.game.event_seq = {"server": 0, "client": 0}
    core.DELTA.reset()  # a loaded save is a new world: the next look is a full one
    batch.DEAD.clear()  # and its ways: a wall hit before the load may be open in the loaded save (NAV-10)
    travel.AREA.block = None  # and a story-area travel block: the save may stand outside it (the HUD says on the next try)
    delta.INV.reset()  # and its inventory: the mod's rev restarts with its Lua state (stale gear/gold after a load)
    body = plumbing._job_text("load_game", r) + (f"\n{dropped}" if dropped else "")
    if fight_block:
        body += "\n" + fight_block
    if retried_fight is not None and r.status == "done":
        reporting._retry.update(rec=retried_fight, status=True, fight=True)
        body += "\n" + reporting._last_attempt_text(retried_fight)
    return reporting._finish("load_game", {"save": save}, body, t0, events=False)


def _fight_before_load() -> tuple[dict | None, str]:
    """load_game: the events not drained yet still belong to the fight (the load resets the event cursor); a fight
    still open is closed as reloaded. Returns (the fight this load retries or None, its debrief block or '')."""
    try:
        tr = reporting._fights()
        evs = []
        for ctx in ("server", "client"):
            try:
                evs += core.game.drain_events(ctx)
            except GameError:
                pass
        reporting.OFFLIMITS.feed(evs)  # a warning in the talk the load cuts off is still known (the player saw it)
        _warns, ended = tr.feed(evs, turn_plan._party_cached())
        if tr.cur is not None:
            block = reporting._close_fight(reloaded=not ended)   # it ended in these events: its own grade, else reloaded
            return tr.retry_of() if ended else tr.last, block
        return tr.retry_of(), ""
    except Exception as e:
        core._log("debrief", {}, f"before load failed: {e}", 0)
        return None, ""


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    session,
)
