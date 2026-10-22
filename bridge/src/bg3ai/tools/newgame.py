"""Starting a playthrough: new_game and character creation.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from .. import naming, render
from ..game import GameError, JobResult, NotReady
from . import core, overview, plumbing, reporting, uitools, world_tools

# ---------------------------------------------------------------------------
# starting a playthrough
# ---------------------------------------------------------------------------

@core.mcp.tool()
@reporting._guard
def new_game(difficulty: str = "Explorer") -> str:
    """From the main menu: start a new game at the given difficulty (Explorer | Balanced | Tactician) and open character creation."""
    if not saving.PLAYTHROUGH:
        raise GameError("new_game is only available in an AI playthrough session")
    if not NEW_GAME_ASYNC:
        return plumbing._act("new_game", "ui.new_game", {"difficulty": difficulty}, context="client", wait_s=NEW_GAME_WAIT_S)
    t0 = time.monotonic()
    if core._session_over():
        return reporting._finish("new_game", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    args = {"difficulty": difficulty}
    th = threading.Thread(target=_new_game_bg, args=(args,), name="new_game", daemon=True)
    _new_game.update(thread=th, result=None, t0=t0)
    th.start()
    return reporting._finish("new_game", args, (
        f"new_game ({difficulty}): loading character creation in the background (~10 s). Write CHARACTER.md and "
        "party-build.md now (memory tools don't wait); the next game tool (character_creation_options) waits for "
        "the screen and says how it went."), t0, events=False)


# vis's proposal (2026-10-17): new_game returns at once, so the agent writes CHARACTER.md / party-build.md during the
# ~10 s load instead of after it; the next game tool waits for the load (_guard -> _await_new_game). Off by default
# until a live window shows it: BG3AI_NEW_GAME_ASYNC=1 turns it on.
NEW_GAME_ASYNC = os.environ.get("BG3AI_NEW_GAME_ASYNC", "").strip() not in ("", "0")
NEW_GAME_WAIT_S = 90.0
_new_game: dict = {"thread": None, "result": None, "t0": 0.0}
_NEW_GAME_NO_WAIT = reporting._SAVE_UNGATED | {"note", "recall"}   # no game screen: they run during the load


def _new_game_bg(args: dict) -> None:
    try:
        with plumbing._InputHeld():
            r = core.game.run_job("ui.new_game", args, context="client", wait_s=NEW_GAME_WAIT_S)
    except Exception as e:
        r = JobResult("failed", None, f"{type(e).__name__}: {e}")
    _new_game["result"] = r


def _await_new_game() -> str:
    """A game tool after an async new_game: wait for its load. -> '' (done at once), a line saying how long it
    waited, or why character creation didn't open (the tool then runs anyway and reports its own state)."""
    th = _new_game["thread"]
    if th is None:
        return ""
    t = time.monotonic()
    th.join(NEW_GAME_WAIT_S + 5)
    if th.is_alive():
        return "new_game: still loading (the next game tool waits again)"
    _new_game["thread"] = None
    r = _new_game["result"]
    if r is None or r.status != "done":
        return (plumbing._job_text("new_game", r) if r is not None else "new_game: no result") \
            + "\nnew_game did NOT open character creation"
    waited = time.monotonic() - t
    return f"(new_game's load finished; waited {waited:.1f} s for it)" if waited >= 0.5 else ""


@core.mcp.tool()
@reporting._guard
def character_creation_options() -> str:
    """What the character creation screen offers right now: origins, races/subraces, classes/subclasses, backgrounds, ability points, skill and spell picks, current step."""
    t0 = time.monotonic()
    res = core.game.rpc("ui.cc_options", {}, context="client")
    return reporting._finish("character_creation_options", {}, render.compact(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def create_character(plan: dict[str, Any] | str) -> str:
    """Fill in character creation, type the character's name, finish it ("Venture forth") and check the name in game. plan: an object with your build (or the same as a JSON string), e.g. {"race":"Elf","subrace":"High Elf","class":"Wizard","subclass":"School of Evocation","background":"Sage","abilities":"recommended","name":"Ilse"}; or {"origin":"Lae'zel"}. "name" is required for a custom character unless the playthrough already has a save owner (then it must be that name). Omitted fields keep the game's defaults/recommendations."""
    if not saving.PLAYTHROUGH:
        raise GameError("create_character is only available in an AI playthrough session")
    t0 = time.monotonic()
    if core._session_over():
        return reporting._finish("create_character", {}, "REFUSED: the session is over. Save and write your handoff notes.", t0)
    p = json.loads(plan) if isinstance(plan, str) else plan  # D15: an object or its JSON text
    if not isinstance(p, dict):
        raise GameError("plan must be an object, e.g. {\"class\":\"Wizard\",\"name\":\"Ilse\"}")
    want = (str(p["name"]).strip() or None) if p.get("name") is not None else None
    if want and saving.SAVE_OWNER and want.casefold() != saving.SAVE_OWNER.casefold():
        raise GameError(f"this playthrough's saves belong to '{saving.SAVE_OWNER}': name the character '{saving.SAVE_OWNER}' "
                        "(or leave \"name\" out)")
    want = want or saving.SAVE_OWNER or None
    if want is None and not p.get("origin"):
        raise GameError("the plan has no \"name\" and the playthrough has no save owner: put the character's "
                        "name in the plan (\"name\": \"...\", at most 20 characters)")
    if want is not None:
        try:
            naming.check_name(want)
        except naming.NamingError as e:
            raise GameError(str(e)) from None
        p["name"] = want
    cc_args: dict = {"plan": p, "finish": False}
    if naming.paced():
        cc_args["paced"] = True  # BG3AI_CC_PACED=1: cc_apply's old fixed tab/pick waits too
    try:
        r = core.game.run_job("ui.cc_apply", cc_args, context="client",
                         wait_s=max(0.5, CC_BUDGET_S - CC_NAMING_S - (time.monotonic() - t0)))
    except GameError as e:
        r = JobResult("failed", None, str(e))
    plumbing._log_stage_ms("ui.cc_apply", r)
    if r.status == "failed" and "character creation is not open" in str(r.error or ""):
        done = _avatar_name(0, want)
        return reporting._finish("create_character", {"plan": p}, (
            f"REFUSED: character creation isn't open: the character is already created ('{done}'; see get_status). "
            "Don't call create_character again" if done else
            "REFUSED: character creation isn't open (from the main menu: new_game first)"), t0)
    if r.status != "done":
        return reporting._finish("create_character", {"plan": p}, plumbing._job_text("create_character", r)
                       + "\ncharacter creation NOT finished" + (
                           " (still applying the plan: call create_character again in a moment)"
                           if r.status == "still_running" else ""), t0)
    t_apply = time.monotonic()
    notes: list[str] = []
    timing = {"apply_ms": round((t_apply - t0) * 1000), "pacing": "fixed" if naming.paced() else "state"}
    try:
        nf = naming.name_and_finish(lambda op, a: core.game.rpc(op, a, context="client"), world_tools._inp() if want else None,
                                    want, notes.append)
    except naming.NamingError as e:
        _log_cc_timing(dict(timing, ok=False, naming_ms=round((time.monotonic() - t_apply) * 1000),
                            error=str(e)[:200], notes=notes[-4:]))
        raise GameError(f"{e}; character creation NOT finished (call create_character again)") from None
    t_named = time.monotonic()
    nm = nf.get("name") or {}
    timing.update(naming_ms=round((t_named - t_apply) * 1000), via=nm.get("via"), how=nm.get("how"),
                  typed=nm.get("typed"), popups=nm.get("popups"), naming_stages=nm.get("ms"), notes=notes[-4:])
    before = (saving.SAVE_OWNER, saving.SAVE_PREFIX)
    if want:
        saving._set_save_owner(want)  # now, before the wait: a client timeout during the load mustn't lose it (I3)
    left = max(0.0, CC_BUDGET_S - (time.monotonic() - t0))
    got = _avatar_name(left, want)
    timing.update(ok=got is not None, world_ms=round((time.monotonic() - t_named) * 1000),
                  total_ms=round((time.monotonic() - t0) * 1000))
    if got is not None and want is not None and got.casefold() != want.casefold():
        _log_cc_timing(timing)
        saving._restore_save_owner(before)
        raise GameError(f"character creation finished, but the character in the world is called '{got}', not "
                        f"'{want}': its saves won't match this playthrough. Say so in your notes and stop")
    who = got or want
    if who:
        saving._set_save_owner(who)
    body = (f"character created and finished: '{who}' (name checked in game)" if got else
            f"character created and finished: '{who}' (the name was set on the naming page, but the world hadn't "
            "loaded yet to check it: check get_status once it has; don't call create_character again)")
    if got is not None:
        t_look = time.monotonic()
        body += _cc_first_look()
        timing["first_look_ms"] = round((time.monotonic() - t_look) * 1000)   # ours: look(full) + the save's start
    _log_cc_timing(timing)
    return reporting._finish("create_character", {"plan": p}, body, t0)


CC_FIRST_LOOK = os.environ.get("BG3AI_CC_FIRST_LOOK", "1").strip() not in ("0", "")


def _cc_first_look() -> str:
    """After a checked create_character: the playthrough's first save, queued (the playbook's Sxxx-start), and the
    full first look (look(full=True): the status read-back of name/race/class, objectives, what's nearby), so the
    first move needs no more calls. naut2 (2026-10-17): 12-14 s idle in every run right after creation, 8-9 calls
    (get_status, save_game, memory_edit, note, memory_read, look, get_frontier, get_map) before the first move.
    BG3AI_CC_FIRST_LOOK=0: the bare result of before."""
    if not CC_FIRST_LOOK:
        return ""
    out = []
    prev = (reporting._batch["active"], reporting._batch["last"])
    reporting._batch["active"] = True   # a nested read: no event drain of its own, logged as a nested call
    try:
        fl = overview.look(full=True)
    except Exception as e:
        fl = f"(first look failed: {type(e).__name__}: {e}; call look)"
    finally:
        reporting._batch["active"], reporting._batch["last"] = prev
    out.append("FIRST LOOK (this is get_status + look: don't read again before moving):\n" + fl)
    # after the look: a save that starts at once holds the menus ~2 s, and the nested look would wait for them
    sess = os.environ.get("BG3AI_SESSION") or ""
    if re.fullmatch(r"S\d{3,}", sess):
        try:
            saving._enable_save_gates()
            out.append("first save (no save_game needed): " + saving.SAVER.save(f"{sess}-start"))
        except Exception as e:
            out.append(f"first save not started ({type(e).__name__}: {e}): save_game(label=\"{sess}-start\")")
    first = "the first move" if "DIALOGUE" not in fl.split("\n", 1)[0].upper() else \
        "dialogue_advance (the opening conversation is up), then the first move"
    out.append(f"NEXT: issue {first} now; write PLAYTHROUGH.md's Character line and the [decision] note "
               "while it runs (fix CHARACTER.md's header only if the status above spells name/race/class "
               "differently)")
    return "\n" + "\n".join(out)


CC_BUDGET_S = 95.0  # the whole tool, under play/.mcp.json's 110 s MCP timeout (a client timeout invites a retry)
CC_NAMING_S = 25.0  # kept for naming + finish after cc_apply
CC_WORLD_POLL_S = 0.25  # the loaded-world check after "Venture forth" (was 2 s: ~1-3 s late; BG3AI_CC_PACED=1 -> 2 s)


def _log_cc_timing(rec: dict) -> None:
    """create_character's phases to runtime/stage_ms.jsonl as op "create_character": apply_ms, naming_ms (+ the
    finish press) and first_look_ms are ours; world_ms (finish -> the world checked) is the game's load, ~8-12 s
    since the check waits out the CC scene; total_ms = apply + naming + world. SESSION-WATCH / acceptance read time
    and attempts from there (never shown)."""
    try:
        with open(core.RUNTIME / "stage_ms.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "op": "create_character", **rec}) + "\n")
    except OSError:
        pass


CC_LEVEL = "SYS_CC_I"  # the character creation scene's level (render.REGION_NAMES "Character creation")


def _avatar_name(timeout: float, want: str | None = None) -> str | None:
    """The created character's name once the world has loaded (state.status's party: on the Nautiloid only the
    avatar), or None if nothing readable came within `timeout`. With `want`, a party member of that name wins.
    Only on the game view (or the opening dialogue), never on the CC scene: right after "Venture forth" it reads
    as screen=game with the page's dummy in the party (naut3, all 3 runs: name "checked" 0.3 s after the press,
    then the FIRST LOOK showed "WHERE Character creation" and the agent re-read 10 s later on the Nautiloid)."""
    end = time.monotonic() + timeout
    while True:
        st = None
        try:
            if core.game.game_state() == "Running" and uitools._ui_screen()[0] in ("game", "dialogue"):
                st = core.game.try_rpc("state.status", context="server")
        except (GameError, NotReady, OSError):
            st = None
        if isinstance(st, dict) and st.get("level") == CC_LEVEL:
            st = None
        names = [str(b.get("name")) for b in (st or {}).get("party") or []
                 if isinstance(b, dict) and b.get("name") not in (None, "", "Unknown")]
        if names:
            hit = [n for n in names if want is not None and n.casefold() == want.casefold()]
            return (hit or names)[0]
        if time.monotonic() >= end:
            return None
        time.sleep(2.0 if naming.paced() else CC_WORLD_POLL_S)


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    batch, saving,
)
