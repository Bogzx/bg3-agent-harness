"""Click-on-entity: the generic fallback for flows the game only starts from a click.

Looting a corpse, lockpicking a door or chest, disarming a discovered trap, pickpocketing, and anything else
without an engine call. Server ops (act.loot_corpse / act.lockpick / act.disarm / act.pickpocket) do the
fairness checks, walk the actor next to the target and select it, then finish with
``{"needs_click": uuid, "expect": "container" | "roll" | "pickpocket"}``. This module does the click:

    project (client op ui.screen_pos) -> move the virtual cursor (input shim) -> wait 2 frames
    -> verify the hover (client op ui.hover_check, i.e. the engine's picking helper)
    -> click -> verify the expected screen (client op ui.state)

The hover check makes the click verifiable: we only click when the engine itself says the cursor is on
the intended entity, so a miss never interacts with the wrong thing. If the hover misses, we retry
with small pixel offsets and different aim heights.

    from bg3ai.game import Game
    from bg3ai.input import InputClient
    from bg3ai.clicker import run_click_action
    res = run_click_action(Game(), InputClient(), "act.loot_corpse", {"char": me, "corpse": body})
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .game import Game, GameError, JobResult
from .input import InputClient

# ui.state "screen" values that count as success for each expectation.
EXPECT_SCREENS: dict[str, set[str]] = {
    "container": {"container"},
    "roll": {"roll"},
    "pickpocket": {"pickpocket", "container", "trade"},
    "any": set(),  # any change of screen
}

# Pixel nudges tried around the projected point, then aim heights.
OFFSETS = [(0, 0), (0, -14), (0, 14), (-12, 0), (12, 0), (0, -30), (0, 28), (-24, -10), (24, -10)]
AIMS = ["auto", "chest", "low", "feet"]


@dataclass
class ClickResult:
    ok: bool
    reason: str | None = None          # off_screen | hover_miss | no_effect | error text
    screen: str | None = None          # ui.state screen after the click
    pos: tuple[int, int] | None = None  # window coords that were clicked
    tries: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "tries": self.tries}
        for k in ("reason", "screen", "pos"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.detail:
            d.update(self.detail)
        return d


def _screen(game: Game) -> dict:
    try:
        return game.rpc("ui.state", {}, context="client") or {}
    except GameError:
        return {}


def hover_point(game: Game, inp: InputClient, uuid: str, frames: int = 2) -> tuple[tuple[int, int] | None, int, dict]:
    """Move the virtual cursor until the engine's picking helper reports `uuid` under it.

    Returns ((x, y) or None, tries, last hover_check)."""
    w, h = inp.window_size()
    tries = 0
    last: dict = {}
    for aim in AIMS:
        try:
            sp = game.rpc("ui.screen_pos", {"uuid": uuid, "aim": aim}, context="client")
        except GameError as e:
            last = {"error": str(e)}
            continue
        if not sp.get("on_screen"):
            last = {"off_screen": True, **sp}
            continue
        nx, ny = sp["ndc"]
        bx, by = (nx * 0.5 + 0.5) * w, (ny * 0.5 + 0.5) * h
        for dx, dy in OFFSETS:
            x, y = int(round(bx + dx)), int(round(by + dy))
            if not (0 <= x < w and 0 <= y < h):
                continue
            tries += 1
            inp.move(x, y)              # blocks until the event reached the game
            inp.wait_frames(frames)     # let the picking helper update
            try:
                last = game.rpc("ui.hover_check", {"uuid": uuid}, context="client")
            except GameError as e:
                last = {"error": str(e)}
                continue
            if last.get("hovered"):
                return (x, y), tries, last
    return None, tries, last


def click_entity(game: Game, inp: InputClient, uuid: str, button: str = "L", expect: str | None = None,
                 verify_timeout: float = 4.0) -> ClickResult:
    """Hover-verify then click `uuid`; if `expect` is given, wait until ui.state shows that screen."""
    before = _screen(game).get("screen")
    pos, tries, last = hover_point(game, inp, uuid)
    if pos is None:
        reason = "off_screen" if last.get("off_screen") else "hover_miss"
        return ClickResult(False, reason, before, None, tries, {"hover": last.get("hover")} if last.get("hover") else {})
    inp.click(pos[0], pos[1], button=button)
    if not expect:
        return ClickResult(True, None, None, pos, tries)
    wanted = EXPECT_SCREENS.get(expect, {expect})
    deadline = time.monotonic() + verify_timeout
    st: dict = {}
    while time.monotonic() < deadline:
        st = _screen(game)
        scr = st.get("screen")
        if (wanted and scr in wanted) or (not wanted and scr != before):
            return ClickResult(True, None, scr, pos, tries)
        time.sleep(0.05)
    if expect == "roll":
        # A key in the party opens the lock without a roll; a trap kit may
        # resolve instantly. Accept the visible state change instead.
        try:
            ts = game.rpc("act.target_state", {"uuid": uuid})
        except GameError:
            ts = {}
        if ts.get("locked") is False or ts.get("trap_armed") is False:
            return ClickResult(True, None, "unlocked" if ts.get("locked") is False else "disarmed", pos, tries)
    return ClickResult(False, "no_effect", st.get("screen"), pos, tries,
                       {"notifications": st["notifications"]} if st.get("notifications") else {})


def finish_needs_click(game: Game, inp: InputClient, result: dict | None) -> dict | None:
    """If a server job result asks for a click, perform it and merge the outcome into the result."""
    if not isinstance(result, dict) or not result.get("needs_click"):
        return result
    cr = click_entity(game, inp, result["needs_click"], result.get("button", "L"), result.get("expect"))
    out = {k: v for k, v in result.items() if k not in ("needs_click", "button")}
    out["click"] = cr.as_dict()
    if cr.ok:
        out["opened"] = cr.screen
    return out


def run_click_action(game: Game, inp: InputClient | None, op: str, args: dict, wait_s: float = 40.0) -> JobResult:
    """Run a click-on-entity server op end to end (approach, click, verify).

    The job's status is downgraded to "failed" if the click could not be completed."""
    jr = game.run_job(op, args, wait_s=wait_s)
    if jr.status != "done" or not isinstance(jr.result, dict) or not jr.result.get("needs_click"):
        return jr
    if inp is None:
        jr.status, jr.error = "failed", "input shim not available for the click"
        return jr
    merged = finish_needs_click(game, inp, jr.result)
    jr.result = merged
    if not merged.get("click", {}).get("ok"):
        jr.status = "failed"
        reason = merged.get("click", {}).get("reason", "click failed")
        jr.error = {"off_screen": "the target is not on screen",
                    "hover_miss": "could not point at the target",
                    "no_effect": "nothing happened"}.get(reason, reason)
    return jr
