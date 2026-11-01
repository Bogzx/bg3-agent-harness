"""What a server playing one side of a two-team match (BG3AI_SIDE) makes of the one game client's screen.

Both sides share one game client, so a reaction prompt (Shield, Counterspell, an opportunity attack) can be the other
side's. The mod marks it (`ui.state` reaction `{other_side: true}`, no rows); as a second guard, rows that name only
the other team's characters (reaction.options[].who) count the same. That prompt is the other side's to answer: for
this side the game is waiting, so its `ui.state` reads `screen: game` with `waiting: <WAITING>`, and the prompt (its
rows name the other team's reactions and spell levels) is left out. Every waiting and interrupt check,
the turn plans' reaction answers and the rendered screen line read `ui.state` through Game, which applies this view
(game.Game._view), so none of them stops on or answers the other side's prompt. Rows of this side's characters stay;
a prompt whose rows name nobody is kept as it is (the mod refuses an answer that is not the side's: "not your
reaction").
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

WAITING = "a reaction prompt of the other side is open: the game waits for that side's answer"


def ui_view(ui: Any, team: Callable[[bool], Iterable[str]]) -> Any:
    """`ui` (a ui.state reply) as this side sees it. team(fresh) gives this side's character names, from a cache
    (fresh=True reads them again); it is asked only for a prompt the mod didn't mark, and read again only when a row
    names someone the cached names miss."""
    if not isinstance(ui, dict) or ui.get("screen") != "reaction":
        return ui
    rx = ui.get("reaction")
    if isinstance(rx, dict) and rx.get("other_side"):
        return _waiting(ui)
    rows = rx.get("options") if isinstance(rx, dict) else None
    if not isinstance(rx, dict) or not isinstance(rows, list):
        return ui
    owned = [r for r in rows if isinstance(r, dict) and r.get("who")]
    if not owned:
        return ui
    names = set(team(False))
    if any(r.get("who") not in names for r in owned):
        names = set(team(True))
    mine = [r for r in owned if r.get("who") in names]
    if len(mine) == len(rows):
        return ui
    if mine:  # this side's rows only; the prompt text reads every row, so it goes
        return {**ui, "reaction": {k: v for k, v in rx.items() if k not in ("options", "prompt")} | {"options": mine}}
    return _waiting(ui)


def _waiting(ui: dict) -> dict:
    out = {k: v for k, v in ui.items() if k not in ("reaction", "page", "blocking")}
    out["screen"] = "game"
    out["waiting"] = WAITING
    return out


# ui.selected with a side: the mod gives no uuid when the HUD (the one client's ClientControl) holds a character of
# the other side. Game.hud_selected reports that as this value, which matches none of this side's characters.
HUD_OTHER_SIDE = "the other side's character"
