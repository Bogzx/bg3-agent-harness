"""Story milestones: chapter/region beats of a playthrough (stdlib only; the dashboard and runner use it).

Milestones are judged from what bg3ai.progress already records, and only from player-visible facts:
regions (State.lua REGION_NAMES: "Nautiloid", "Wilderness", ...), minimap areas ("Ravaged Beach", "Campsite"),
journal quests, party members and levels. Two sources:

* the playthrough's seen-state (runtime/sessions/<pt>/progress_state.json): what is reached now;
* the sessions' progress events (Sxxx.progress.jsonl): when each one was first reached, so the dashboard can
  say "S003 23:31". A milestone that was already true at the first poll (the silent baseline) has no time.

Patterns are general game knowledge (area names as the game shows them); they're deliberately lenient so a
spelling variant still counts.
"""

from __future__ import annotations

import re
from typing import Any

# (id, act, label, fact kind, pattern). Kinds: region, area, quest_done, quest, companions (count), level (max).
MILESTONES: list[tuple[str, int, str, str, Any]] = [
    ("nautiloid", 1, "Aboard the Nautiloid", "region", r"nautiloid"),
    ("first_companion", 1, "First companion", "companions", 2),
    ("nautiloid_escaped", 1, "Nautiloid escaped", "quest_done", r"escape the nautiloid"),
    ("crash_site", 1, "Reached the surface", "region", r"wilderness"),
    ("beach", 1, "Reached the beach", "area", r"beach|crash site"),
    ("first_camp", 1, "First camp", "area", r"\bcamp(site)?\b"),
    ("level_2", 1, "Party level 2", "level", 2),
    ("grove", 1, "Emerald Grove", "area", r"emerald grove|druid"),
    ("blighted_village", 1, "Blighted Village", "area", r"blighted village"),
    ("level_4", 1, "Party level 4", "level", 4),
    ("goblin_camp", 1, "Goblin camp (Shattered Sanctum)", "area", r"shattered sanctum|goblin camp"),
    ("underdark", 1, "Underdark", "area", r"underdark"),
    ("grymforge", 1, "Grymforge", "area", r"grymforge"),
    ("creche", 1, "Githyanki Creche", "region", r"rosymorn|creche"),
    ("act2", 2, "Act 2: Shadow-Cursed Lands", "region", r"shadow.?cursed"),
    ("last_light", 2, "Last Light Inn", "area", r"last light"),
    ("moonrise", 2, "Moonrise Towers", "area", r"moonrise"),
    ("act3", 3, "Act 3: Rivington", "region", r"rivington|wyrm"),
    ("lower_city", 3, "Lower City (Baldur's Gate)", "region", r"lower city"),
    ("upper_city", 3, "Upper City (endgame)", "region", r"upper city"),
]


def empty_facts() -> dict:
    return {"region": set(), "area": set(), "quest_done": set(), "quest": set(), "companions": set(), "joined": set(), "level": 0}


def facts_from_seen(seen: dict) -> dict:
    """bg3ai.progress seen-state → facts."""
    f = empty_facts()
    f["region"].update(str(r) for r in seen.get("regions") or [])
    f["area"].update(str(a).split("|", 1)[-1] for a in seen.get("areas") or [])
    for t, q in (seen.get("quests") or {}).items():
        f["quest"].add(str(t))
        if isinstance(q, dict) and q.get("done"):
            f["quest_done"].add(str(t))
    levels = seen.get("levels") or {}
    f["companions"].update(str(n) for n in levels)
    f["level"] = max([int(v) for v in levels.values() if isinstance(v, (int, float))], default=0)
    return f


LEVEL_RE = re.compile(r"^(?P<name>.+?) -> level (?P<n>\d+)$")


def add_event(f: dict, ev: dict) -> None:
    """Fold one progress event ({kind, what}) into facts."""
    kind, what = ev.get("kind"), str(ev.get("what") or "")
    if kind in ("region", "area", "quest_done"):
        f[kind].add(what)
    if kind in ("quest_new", "quest_done"):
        f["quest"].add(what)
    elif kind == "companion":
        f["joined"].add(what)  # someone joined the party (the avatar never gets this event)
    elif kind == "level_up":
        m = LEVEL_RE.match(what)
        if m:
            f["level"] = max(f["level"], int(m.group("n")))


def reached(f: dict, kind: str, pat: Any) -> bool:
    if kind == "companions":
        return len(f["companions"]) >= pat or len(f["joined"]) >= pat - 1
    if kind == "level":
        return f["level"] >= pat
    rx = re.compile(pat, re.I)
    return any(rx.search(x) for x in f[kind])


def status(seen: dict | None, sessions: list[tuple[str, list[dict]]] | None = None) -> dict:
    """Milestones reached now (from the seen-state) and when each was first reached (from session events).

    sessions: [(session id, progress records)] in play order. Returns
    {"reached": n, "total": N, "act": current act, "next": label, "items": [{id, act, label, reached, session, t}]}.
    """
    now = facts_from_seen(seen or {})
    first: dict[str, tuple[str, float | None]] = {}
    run = empty_facts()
    for sid, recs in sessions or []:
        for r in recs:
            if r.get("type") != "progress":
                continue
            add_event(run, r)
            for mid, _act, _label, kind, pat in MILESTONES:
                if mid not in first and reached(run, kind, pat):
                    first[mid] = (sid, r.get("t"))
    items = []
    for mid, act, label, kind, pat in MILESTONES:
        ok = reached(now, kind, pat) or mid in first
        sid, t = first.get(mid, (None, None))
        items.append({"id": mid, "act": act, "label": label, "reached": ok, "session": sid if ok else None,
                      "t": t if ok else None})
    done = [i for i in items if i["reached"]]
    nxt = next((i["label"] for i in items if not i["reached"]), None)
    return {"reached": len(done), "total": len(items), "act": max((i["act"] for i in done), default=1),
            "next": nxt, "items": items}


def line(st: dict) -> str:
    """One text line: 'milestones 6/20 (Act 1): ✓ Nautiloid escaped (S003) · ✓ First camp (S005) · next: Emerald Grove'."""
    got = [i for i in st["items"] if i["reached"]]
    parts = [f"✓ {i['label']}" + (f" ({i['session']})" if i["session"] else "") for i in got]
    if st.get("next"):
        parts.append(f"next: {st['next']}")
    return f"milestones {st['reached']}/{st['total']} (Act {st['act']}): " + " · ".join(parts)
