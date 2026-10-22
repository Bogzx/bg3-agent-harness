"""Raw UI tools: ability_preview, ui_action, dismiss_popups, confirm, reactions, ui_dump, press_key, click.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .. import hotbar, render, turn
from ..game import GameError
from . import core, plumbing, reporting, world_tools


@core.mcp.tool()
@reporting._guard
def ability_preview(ability: str, target: str, who: str | None = None) -> str:
    """What the game shows when targeting: hit chance / save DC, damage preview, range and line of sight."""
    t0 = time.monotonic()
    res = core.game.rpc("state.preview", {"char": plumbing._who(who), "spell": ability, "target": plumbing._uuid(target)})
    return reporting._finish("ability_preview", {"ability": ability, "target": target}, render.compact(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def ui_action(command: str, widget: str = "", param: str = "") -> str:
    """Escape hatch for screens without a dedicated tool (level up, trade, character creation, popups): run a named UI command on the current screen, as get_dialogue/ui dump lists them."""
    if _saveload_word(command):
        return (f"REFUSED: ui_action {command} saves/loads/deletes a save; use save_game / load_game (gated, "
                "prefix-checked, the saver's settle and the DELTA reset)")
    args: dict[str, Any] = {"command": command}
    if widget:
        args["widget"] = widget
    if param:
        args["param"] = param
    return plumbing._act("ui_action", "ui.command", args, context="client", wait_s=15)


# Audit UI-3: the escape hatches (ui_action, press_key) must not save or load around save_game/load_game. The mod
# refuses the same (UIActions.SAVELOAD_*); this catches it before a round trip and with an older mod build.
_SAVELOAD_WORDS = ("quicksave", "quickload", "savegame", "loadgame", "continuegame", "overwrite", "deletesave",
                   "loadsave")
_SAVELOAD_KEYS = {"F5": "quicksave", "F8": "quickload"}  # the game's default bindings


def _saveload_word(name: str) -> str | None:
    flat = re.sub(r"[\s_.\-]", "", str(name or "").lower())
    return next((w for w in _SAVELOAD_WORDS if w in flat), None)


@core.mcp.tool()
@reporting._guard
def dismiss_popups() -> str:
    """Close informational popups (tutorials, info boxes, reward/book panels). Decision popups are never closed; they're listed so you can answer with confirm."""
    t0 = time.monotonic()
    res = core.game.rpc("ui.dismiss", {}, context="client")
    return reporting._finish("dismiss_popups", {}, render.compact(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def confirm(answer: str = "yes", amount: int = 0) -> str:
    """Answer the open Yes/No (OK/Cancel, Confirm...) question box: answer = "yes" | "no" | a button label | its number. Shows the next question if the game asks another. A stack-split box ("how many?", POPUP ... split): amount=N sets how many before "yes"."""
    args: dict[str, Any] = {"answer": answer}
    if amount:
        args["amount"] = int(amount)
    if str(answer).strip().lower() not in ("no", "cancel", "decline", "2") and \
            (core.game.try_rpc("ui.state", context="client") or {}).get("screen") == "reward":
        # S042 22:50:08: confirm("Accept") on Selûne's Reward panel (screen=reward) failed "no confirmation box is
        # open"; a player clicks its Accept button, which is what dismiss_popups' GetRewardsAndClose does.
        t0 = time.monotonic()
        res = core.game.rpc("ui.dismiss", {}, context="client")
        return reporting._finish("confirm", {"answer": answer}, "confirm: done — the Reward panel: " + render.compact(res, core.H), t0)
    return world_tools._box_act("confirm", "ui.confirm", args, wait_s=8)


def _hud_has(char: str) -> bool:
    """Does the HUD (the client's ClientControl, what every ui.* op and the HUD's buttons act on) have `char`
    selected? A16: state.status `active` is the server's view; in S074 it said Claude while the HUD had another member."""
    hud = (core.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
    return bool(hud) and hud[-36:] == char[-36:]


def _reactions_rpc(char: str, args: dict) -> Any:
    """ui.reactions works on the HUD's selected character (the client's ClientControl): select first when the HUD
    shows another member. act.select is done only once the client shows the character (Game._select, A16: in S041,
    S069 and S074 the server's own view said selected while the HUD kept another member, and ui.reactions refused)."""
    if not _hud_has(char):
        _reaction_prompt_block(char)
        sel = core.game.run_job("act.select", {"char": char}, wait_s=5)
        if sel.status != "done":
            raise GameError(f"{queueing._label(char)} could not be selected: {sel.error or sel.status}" + _enemy_turn_why(char))
    try:
        return _job_reply(core.game.rpc("ui.reactions", args, context="client"))
    except GameError as e:
        if "not the selected character" not in str(e):
            raise
        last = e
    # selected away between the check and the call (the game picks a member at a turn start): select once more
    _reaction_prompt_block(char)
    sel = core.game.run_job("act.select", {"char": char}, wait_s=5)
    if sel.status != "done":
        raise GameError(f"{last} ({queueing._label(char)} could not be selected: {sel.error or sel.status})" + _enemy_turn_why(char))
    return _job_reply(core.game.rpc("ui.reactions", args, context="client"))


def _enemy_turn_why(char: str) -> str:
    """S087 06:10:18 (and S069 02:43): set_reactions(Wyll) during the enemies' turns: the HUD keeps the member it has
    while no party member is acting. Say so and when it works ('' when a party member is acting: another cause)."""
    cs = core.game.try_rpc("state.combat") or {}
    if not cs.get("active") or turn.party_acting(cs, turn_plan._party_set()):
        return ""
    return (f". It is the enemies' turn: the HUD doesn't switch members until a party member's turn comes. Set "
            f"{queueing._label(char)}'s reactions on a party turn (its own, or after selecting it then), or the selected "
            "member's now")


def _reaction_prompt_block(char: str) -> None:
    """S081 #8 02:43:22: set_reactions(Wyll) during an enemy's turn, a Counterspell prompt open: `Wyll could not be
    selected: the HUD kept another member selected`. The HUD doesn't change members under a reaction prompt, and
    the answer is the agent's: say so instead of trying the select."""
    ui = core.game.try_rpc("ui.state", context="client") or {}
    if not isinstance(ui, dict) or ui.get("screen") != "reaction":
        return
    rx = ui.get("reaction") if isinstance(ui.get("reaction"), dict) else {}
    prompt = core.H.scrub(str(rx.get("prompt") or "").replace(" | Choose how to react.", "").split(" | ")[0])
    raise GameError(f"a reaction prompt is on screen{f' ({prompt})' if prompt else ''}: the HUD can't select "
                    f"{queueing._label(char)} until it is answered. Answer it with react(use=true|false), then set the "
                    "reactions again")


def _job_reply(res: Any, wait_s: float = 5.0) -> Any:
    """A client op that answered with a job (ui.reactions when it changes a mode: the commands apply a frame or two
    later): its result once it settles. S081 #258: set_reactions(Counterspell=never) printed only `job=1`."""
    if not (isinstance(res, dict) and "job" in res):
        return res
    view, deadline = res, time.monotonic() + wait_s
    while view.get("status") in (None, "running") and time.monotonic() < deadline:
        time.sleep(0.1)
        view = core.game.rpc("job.poll", {"id": res["job"]}, "client")
    if view.get("status") != "done":
        part = (view.get("result") or view.get("partial") or {}) if isinstance(view, dict) else {}
        raise GameError(f"the reaction change did not take ({view.get('error') or view.get('status')})"
                        + (f"; now: {render.compact(part, core.H)}" if part else ""))
    return view.get("result")


@core.mcp.tool()
@reporting._guard
def set_reactions(who: str | None = None, settings: dict[str, str] | str = "") -> str:
    """See or change a character's reaction prompts (e.g. Shield, Divine Smite, Opportunity Attack). settings: an object (or the same as a JSON string) {"<reaction name>": "never"|"always"|"ask"}; empty = list current."""
    args: dict[str, Any] = {"char": plumbing._who(who)}
    if settings:
        args["set"] = json.loads(settings) if isinstance(settings, str) else settings  # D15
    t0 = time.monotonic()
    res = _reactions_rpc(args["char"], args)
    plumbing._remember_names(res)
    return reporting._finish("set_reactions", {"who": who}, render.compact(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def ui_dump(text_only: bool = True) -> str:
    """Text dump of what's on screen in the UI (visible text, buttons and their commands). Cheaper than a screenshot."""
    t0 = time.monotonic()
    res = core.game.rpc("ui.dump", {"text_only": text_only}, context="client")
    body = res if isinstance(res, str) else render.compact(res, core.H)
    return reporting._finish("ui_dump", {}, body[:6000], t0)


@core.mcp.tool()
@reporting._guard
def press_key(key: str) -> str:
    """Press a keyboard key in the game (SDL scancode name, e.g. ESCAPE, SPACE, RETURN, F5, 1). Prefer dedicated tools. ESCAPE with nothing open is refused (it would open the game menu)."""
    key = key.upper()
    if key in ("ESCAPE", "ESC"):
        return _press_escape()
    if key in _SAVELOAD_KEYS:
        if key == "F5":  # quicksave: the gated save path does the same
            return "press_key F5 is quicksave -> save_game(\"quicksave\"): " + getattr(saving.save_game, "fn", saving.save_game)("quicksave")
        return ("REFUSED: F8 is quickload; reloading goes through load_game (prefix-checked; resets the "
                "change tracking)")
    return plumbing._act("press_key", "ui.key", {"key": key}, context="client", wait_s=5)


def _ui_screen() -> tuple[str, list]:
    """(screen, panels_open) from ui.state; ('?', []) when the client can't say."""
    ui = core.game.try_rpc("ui.state", context="client")
    if not isinstance(ui, dict) or ui.get("error"):
        return "?", []
    return ui.get("screen") or "game", list(ui.get("panels_open") or [])


def _press_escape() -> str:
    """S021 05:38 / S026 09:42 (sale lost): ESCAPE used as 'close' on the plain game view opened the game menu.
    Refused there; and when a press still lands on the game menu (the panel had closed itself), it is closed again."""
    t0 = time.monotonic()
    before, panels = _ui_screen()
    if before == "game" and not panels:
        return reporting._finish("press_key", {"key": "ESCAPE"}, "REFUSED: nothing is open to close: ESCAPE here "
                       "would open the game menu. Panels our tools open close themselves.", t0)
    out = plumbing._act("press_key", "ui.key", {"key": "ESCAPE"}, context="client", wait_s=5)
    if before not in ("game_menu", "options", "save_load"):
        time.sleep(0.3)
        if _ui_screen()[0] == "game_menu":
            # The menu's own Resume button (live 12:1x: a second key press left the menu open 3/3 while the
            # result said "closed it again"); then check, and say so if it is still up.
            core.game.try_rpc("ui.press", {"button": "Resume", "widget": "GameMenu"}, context="client")
            closed = False
            for _ in range(6):
                time.sleep(0.15)
                if _ui_screen()[0] != "game_menu":
                    closed = True
                    break
            if not closed:
                core.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
                time.sleep(0.3)
                closed = _ui_screen()[0] != "game_menu"
            out += ("\nthe ESCAPE opened the game menu (what it was meant to close had already closed): "
                    + ("closed it again" if closed else "it is STILL OPEN (press_key ESCAPE once more closes it)"))
            if before == "dialogue":
                out += "\n(ESCAPE does not leave a conversation: pick its Leave/exit answer with choose_dialogue)"
    return out


@core.mcp.tool()
@reporting._guard
def click(x: float, y: float, button: str = "left") -> str:
    """Last-resort mouse click at a screen position given as fractions 0-1 of the latest screenshot.
    button: "left", "right", or "none" (= hover: only moves the cursor there, nothing is clicked; for a tooltip or
    the cursor text of a targeting mode, then take a screenshot)."""
    t0 = time.monotonic()
    b = (button or "left").strip().lower()
    kind = {"left": "L", "l": "L", "right": "R", "r": "R", "none": "", "hover": "", "move": ""}.get(b)
    if kind is None:
        # S039 21:47:42: an unknown button used to commit a LEFT click (with the Jump targeting on: a 5 m jump)
        raise GameError(f"unknown button {button!r}: use left, right, or none (= hover, no click)")
    inp = world_tools._inp()
    if inp is None:
        raise GameError("input shim not available")
    w, h = inp.window_size()
    px, py = int(x * w), int(y * h)
    if not kind:
        inp.move(px, py)
        time.sleep(0.3)
        return reporting._finish("click", {"x": x, "y": y, "button": "none"}, f"hovered at {px},{py} (nothing clicked)", t0)
    inp.click(px, py, kind)
    time.sleep(0.3)
    out = f"clicked {'right' if kind == 'R' else 'left'} at {px},{py}" + (_in_hand_note() if kind == "L" else "")
    return reporting._finish("click", {"x": x, "y": y}, out, t0)


def _in_hand_note() -> str:
    """S083 03:55:16: a blind left click on the hotbar picked up Netherese Orb Blast instead of Knock (the deck had
    another layout) and the reply said only `clicked left at 881,1004`: the next left click on the world would have
    cast it. A player sees the spell in hand (the cursor changes, its cell lights up): say which one."""
    st = hotbar.cast_state(core.game, None)
    if (st.get("armed") or st.get("upcast_open")) and not st.get("spell") and st.get("char"):
        # S091 11/11 `IN HAND: a spell`: the slot VMs carry no id and the client entity's hotbar map may be missing;
        # the HUD character's server map names the active cell
        m = hotbar.server_map(core.game, st["char"])
        if m:
            st = hotbar.cast_state(core.game, st["char"], m) or st
    # S086-r1/S087 (14x): plain clicks on the world while the party walked said `IN HAND: a spell`: one of the weaker
    # armed signals (a task preview, the cursor task) fires for a walk too, and no spell was ever named. A spell in
    # hand is one the game names (the hotbar's active slot or the client cast state), or the upcast picker open.
    if not (st.get("upcast_open") or (st.get("armed") and st.get("spell"))):
        return ""
    what = st.get("spell_name") or st.get("spell") or "a spell"
    return (f"\nIN HAND: {what} (the next left click on the world casts it at the cursor; press_key ESCAPE puts it"
            " back)")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    newgame, queueing, turn_plan, saving,
)
