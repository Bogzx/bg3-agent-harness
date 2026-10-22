"""The S3/S4 pickpocket probes' shared part: open a named NPC's pockets with the bridge's pickpocket tool, take the
best-odds item (the tool clicks Steal), and the referee records around it."""

from __future__ import annotations

import re

from probelib import Probe, find_char

RADIUS_M = 25.0
FIRST = re.compile(r"POCKETS[^:]*: (e\d+) ([^|]*?)(?: \||$)", re.M)


def best(text: str) -> tuple[str, str] | None:
    """(id, entry) of the first listed pocket item: the tool lists the best odds first."""
    m = FIRST.search(text)
    return (m.group(1), m.group(2).strip()) if m else None


def steal(p: Probe, name: str, thief: str | None = None) -> tuple[str, list[dict]]:
    """('' or why it couldn't try, the referee records from the open to a few seconds after Steal)."""
    who = find_char(p.nearby(RADIUS_M), name)
    if who is None:
        return f"no {name} within {RADIUS_M:.0f} m", []
    npc = p.handle(who)
    mark = p.ref_mark()
    text = p.S.pickpocket(npc, who=thief)
    p.log(text[:1500])
    item = best(text)
    if item is None:
        p.S.pickpocket(npc, who=thief, leave=True)
        return f"{who['name']}'s pockets listed nothing to take: {text[:200]}", p.ref_after(mark)
    p.log(p.S.pickpocket(npc, who=thief, take=item[0])[:800])
    p.sleep(4.0)
    return "", p.ref_after(mark)


def pp_records(events: list[dict], ok: bool) -> list[dict]:
    return [e for e in events if e.get("ev") == "pickpocket" and e.get("ok") is ok]
