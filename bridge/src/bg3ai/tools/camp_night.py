"""The night at camp's required talks (DB_Camp_RequiredTalks): the refusal long_rest / camp leave / fast_travel give,
and the TALK line's part. Moved out of travel.py unchanged (the 1000-line cap).
"""

from __future__ import annotations

import time

from . import core


def _night_talks() -> tuple[list[str], int] | None:
    """At night at camp with required talks left: (the named ones as "e5 Shadowheart", how many are not in view);
    None otherwise. The camp story's night check (GLO_Camp.txt PROC_Camp_TryLeaveNightMode_CampNightCheck) refuses
    the sleep on any DB_Camp_RequiredTalks row, unfiltered; the story itself drops a row when that character joins
    a dialogue, fights, is defeated or leaves camp, or on a long rest (GLO_Camp.txt "Required talks" region), so a
    row that is still there is one the game still waits for. Each row shows the camp's '!' over its character
    (PROC_ExclamationMark_Show "GLO_Camp_RequiredTalk"); Events.lua names a row's character only when in the party or
    in sight."""
    flow = core.game.try_rpc("camp.rest_flow", {"since_ms": 1})
    if not isinstance(flow, dict):
        return None
    night = (flow.get("story") or {}).get("DB_Camp_NightMode")
    talks = [t for t in flow.get("required_talks") or [] if isinstance(t, dict)]
    if not (isinstance(night, int) and night > 0 and talks):
        return None
    named = [f"{core.H.get(t['uuid'])} {t.get('name') or ''}".strip() for t in talks if t.get("uuid")]
    return named, len(talks) - len(named)


def night_talks_left(log: bool = True) -> str:
    """launch-v1 haiku act3_gold_max (r1, r2, r4, r5): 46 long_rest "the night at camp didn't end" and camp leaves
    refused for up to 25 min, until the agent happened to talk to Shadowheart or Withers: the sleep press only
    brought up the story's "talks left" message box. The refusal long_rest / camp leave / fast_travel give instead
    of that press ('' when it is not night at camp or no talk is left). Someone not in view is said as such, never
    counted: the box doesn't count them either. log=False: no night_talks metrics row."""
    got = _night_talks()
    if got is None:
        return ""
    named, unseen = got
    who = ", ".join(named) + (" (the '!' over them)" if named else "")
    if unseen:
        who += (" and " if named else "") + "someone not in view"
    if log:
        core.METRICS.write({"type": "night_talks", "t": round(time.time(), 3), "named": len(named), "unseen": unseen})
    return (f"the night at camp can't end yet: the game still wants a talk with {who} first; "
            "going to sleep only brings up its 'talks left' notice until then, and a talk with them clears it")


def night_talk_marks() -> str:
    """get_status/look's part of the TALK line at night camp: only what the screen shows, the camp '!' over the
    characters in view ("night at camp: the '!' over e5 Shadowheart"); '' when none is in view."""
    got = _night_talks()
    return f"night at camp: the '!' over {', '.join(got[0])}" if got and got[0] else ""
