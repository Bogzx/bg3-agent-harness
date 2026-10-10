"""A working alternative in navigation FAILED replies (claude-run-2 S021).

S021 had 11 jump_to refusals ("landing on a blocked tile", "too far to jump") and 9 walk_toward "no walkable
ground" replies; each was followed by 1-5 model turns (5-10 s each) of guessing another spot. The mod now
computes the alternative (nav.landing `plan`, nav.reach_toward `others`), and these pure helpers word it as
one concrete next call.
"""

from __future__ import annotations

from typing import Any

from . import render

N, P = render.num, render.pos


def jump_plan_text(plan: Any) -> str:
    """' It is walkable: move_to ...' / ' Do it in two steps: move_to ..., then jump_to ...' / ''."""
    if not isinstance(plan, dict) or not plan.get("land"):
        return ""
    off = plan.get("land_off_m") or 0
    near = f" ({N(off)}m from what you asked)" if off else ""
    if plan.get("walk"):
        how = ", the route has a jump link: out of combat move_to takes it" if plan.get("needs_jump") else ""
        return f" It is walkable: move_to {P(plan['land'])}{near} (path {N(plan.get('path_m', '?'))}m{how})."
    if plan.get("stand"):
        return (f" Do it in two steps: move_to {P(plan['stand'])} (walk {N(plan.get('walk_m', '?'))}m), then "
                f"jump_to {P(plan['land'])}{near}, a {N(plan.get('jump_m', '?'))}m jump.")
    return ""


def landing_bad_text(lnd: dict) -> str:
    """What is wrong with the landing, e.g. 'is on a blocked tile (wall or hole)'."""
    if lnd.get("no_floor"):
        fl = lnd.get("floors") or []
        if fl:
            return f"is in the air: no floor at that height (the ground there is at y {N(fl[0])})"
        return "is in the air: no floor at that height"
    bad = f"in {lnd['surface']}" if lnd.get("surface") else "on a blocked tile (wall or hole)"
    if lnd.get("short_pos") and not lnd.get("blocked"):
        return (f"{N(lnd.get('dist_m', '?'))}m away, past reliable reach "
                f"({N(lnd.get('reliable_m', '?'))}m): it may land short {bad}")
    return f"is {bad}"


def jump_refusal(lnd: Any, asked: list[float]) -> str | None:
    """The FAILED reply for a jump nav.landing rules out (with the plan), or None to go ahead.

    None also when the landing has a `suggest` (the caller swaps it in) and is not too far."""
    if not isinstance(lnd, dict):
        return None
    plan = jump_plan_text(lnd.get("plan"))
    if not plan and lnd.get("plan_skipped"):  # the level's kill switch (S038 OOM): no search for a plan here
        plan = f" No jump plan: {lnd['plan_skipped']}; pick the landing from get_layout / a screenshot."
    if lnd.get("safe") is False and not lnd.get("suggest"):
        alt = (f" The nearest clear landing in reach is {P(lnd['nearest_clear'])}, "
               f"{N(lnd.get('nearest_clear_off_m', '?'))}m from it." if lnd.get("nearest_clear") and not plan else "")
        if plan:
            tail = plan
        elif lnd.get("surface") == "Chasm":
            # S035 19:38 / S038 20:44: force=true into a Chasm was a fatal fall both times (the Chasm is the void
            # around the floors; Feather Fall did not save it): never offer force for it.
            tail = (" A Chasm has no floor in jumping height: a forced jump there is a fatal fall. Find the crossing"
                    " with get_layout / route (a ledge in reach, a ladder, the story's own way across).")
        else:
            tail = " Pick another landing (get_frontier lists HAZARD patches) or force=true."
        return (f"jump_to: FAILED — the landing {P(asked)} {landing_bad_text(lnd)} and no clear spot within 3m of it "
                f"is in reach ({N(lnd.get('reliable_m', '?'))}m).{alt}{tail}")
    if lnd.get("too_far") and not lnd.get("suggest"):
        tail = plan or " No walk gets within jumping distance of it: route() for a ladder or another way."
        return (f"jump_to: FAILED — too far to jump ({N(lnd.get('dist_m', '?'))} m, this character can jump "
                f"{N(lnd.get('max_m', '?'))} m).{tail}")
    return None


def other_dirs_text(others: Any) -> str:
    """'. Walkable instead: S 18m, N 12m (winding) → walk_toward(direction="S")' or ''."""
    if not isinstance(others, list) or not others:
        return ""
    parts = [f"{o['dir']} {N(o.get('m', '?'))}m" + (" (winding)" if o.get("winding") else "")
             for o in others if isinstance(o, dict) and o.get("dir")]
    if not parts:
        return ""
    return f". Walkable instead: {', '.join(parts)} → walk_toward(direction=\"{others[0]['dir']}\")"
