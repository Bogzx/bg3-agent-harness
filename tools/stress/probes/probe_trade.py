"""The T8 trade probes' shared part: open a trade with a named trader through the bridge's trade tool, and read
the wares and your items back from its text (economy.trade_text)."""

from __future__ import annotations

import re

from probelib import Probe, find_char

RADIUS_M = 25.0
ENTRY = re.compile(r"^(?:\[[^\]]*\] )?(e\d+) (.+?)(?: x(\d+))? (\d+)g(.*)$")


def entries(text: str, head: str) -> list[dict]:
    """The `<id> <name> [xN] <price>g [flags]` entries of the lines from `head` (WARES or YOUR ITEMS) on."""
    out: list[dict] = []
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(head)), None)
    if start is None:
        return out
    block = (
        [lines[start]] + [ln for ln in lines[start + 1 :] if ln.startswith("  ")] if head == "WARES" else [lines[start]]
    )
    for ln in block:
        body = ln.split(": ", 1)[1] if ": " in ln else ""
        for part in body.split(" | "):
            m = ENTRY.match(part.strip())
            if m:
                out.append(
                    {
                        "id": m.group(1),
                        "name": m.group(2),
                        "count": int(m.group(3) or 1),
                        "price": int(m.group(4)),
                        "flags": m.group(5).split(),
                    }
                )
    return out


def party_gold(text: str) -> int | None:
    m = re.search(r"your gold (\d+)", text)
    return int(m.group(1)) if m else None


def open_trade(p: Probe, name: str) -> tuple[dict | None, str, list[dict]]:
    """(the trader, trade(npc) text, the referee records since just before the open)."""
    who = find_char(p.nearby(RADIUS_M), name)
    if who is None:
        return None, f"no {name} within {RADIUS_M:.0f} m", []
    mark = p.ref_mark()
    text = p.S.trade(npc=p.handle(who), full=True)
    p.log(text[:3000])
    p.sleep(1.0)
    return who, text, p.ref_after(mark)


def opened(events: list[dict], who: dict) -> bool:
    return any(e.get("ev") == "trade_open" and e.get("npc_uuid") == who["uuid"] for e in events)
