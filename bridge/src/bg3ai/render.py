"""Turn mod JSON into compact, token-cheap text for the agent.

Entity GUIDs never reach the agent: `Handles` maps them to short ids (e1, e2, ...) that are stable for
the life of the MCP server process (one agent session). Renderers are defensive: unknown keys still
render as `key=value`, so the bridge keeps working while the mod's output shapes evolve.
"""

from __future__ import annotations

import re
from typing import Any

from .render_base import (  # noqa: F401  (re-exported: callers use render.<name>)
    PREFIXED_UUID_RE,
    SAME_MIN,
    UUID_RE,
    Handles,
    classes_text,
    compact,
    num,
    pos,
    raised_ladder,
    resources_text,
)
from .render_events import (  # noqa: F401
    ECHO,
    FLAP,
    STATUS_GROUP_MIN,
    EventEcho,
    StatusFlap,
    one_line,
    render_events,
)
from .render_nav import (  # noqa: F401
    LAYOUT_KEY,
    find_objective,
    objective_label,
    opened_note,
    render_frontier,
    render_hazards,
    render_layout,
    render_objectives,
    render_path,
    render_ways,
)

# ---------------------------------------------------------------------------
# Specific renderers (shapes per docs/PROTOCOL.md; fall back to compact()).
# ---------------------------------------------------------------------------

def _statuses(c: dict) -> str:
    st = c.get("statuses") or []
    names = []
    for s in st:
        if isinstance(s, dict):
            n = s.get("name") or s.get("id")
            if s.get("duration") not in (None, -1):
                n = f"{n}({num(s['duration'])}t)"
            names.append(str(n))
        else:
            names.append(str(s))
    return ",".join(names)


def _hp(c: dict) -> str:
    if c.get("hp") is not None and c.get("max_hp"):
        s = f"HP {num(c['hp'])}/{num(c['max_hp'])}"
        if c.get("temp_hp"):
            s += f"+{num(c['temp_hp'])}"
        return s
    if c.get("hp_pct") is not None:
        return f"HP {num(c['hp_pct'])}%"
    return ""




def character_line(c: dict, H: Handles, me: str | None = None) -> str:
    h = H.get(c["uuid"], c.get("name")) if c.get("uuid") else "?"
    bits = [("*" if me and c.get("uuid") == me else "") + f"{h} {c.get('name', '?')}"]
    cls = c.get("classes")
    if isinstance(cls, list) and cls:
        bits.append(classes_text(cls))
    elif c.get("level"):
        bits.append(f"L{c['level']}")
    for key in ("relation",):
        if c.get(key):
            bits.append(str(c[key]))
    hp = _hp(c)
    if hp:
        bits.append(hp)
    if c.get("ac"):
        bits.append(f"AC{c['ac']}")
    if c.get("dist") is not None:
        bits.append(f"{num(c['dist'])}m")
    st = _statuses(c)
    if st:
        bits.append(f"[{st}]")
    for flag in ("downed", "dead", "in_combat", "can_talk", "can_trade", "sneaking", "is_current"):
        if c.get(flag):
            bits.append(flag)
    if c.get("wants_talk"):  # the "!" over them (State.lua HasExclamationDialog)
        bits.append("wants to talk(!)")
    if c.get("summon"):  # party row of a Spiritual Weapon, familiar...: an ally, not a companion
        bits.append(f"summon of {H.get(c['owner'])}" if c.get("owner") else "summon")
    return " ".join(bits)


def camp_text(camp: Any, H: Handles) -> str:
    """state.status camp roster as "e5 Wyll Warlock3(The Fiend) level 3 (level up to 4 ready), wants to talk;
    e6 Halsin Druid5 level 5" (it was the generic dump `[Warlock3(The Fiend) level=3 e5 wants_talk level_up=4
    name=Wyll; ..]`, live 20261016)."""
    if not isinstance(camp, list):
        return compact(camp, H)
    out = []
    for c in camp:
        if not isinstance(c, dict):
            out.append(compact(c, H))
            continue
        bits = [H.get(c["uuid"], c.get("name")) if c.get("uuid") else "", str(c.get("name") or "")]
        if isinstance(c.get("classes"), list) and c["classes"]:
            bits.append(classes_text(c["classes"]))
        lv = c.get("level")
        if lv is not None:
            up = c.get("level_up")
            bits.append(f"level {num(lv)}" + (f" (level up to {num(up)} ready)" if up and up != lv else ""))
        t = " ".join(b for b in bits if b)
        if c.get("wants_talk"):
            t += ", wants to talk"
        rest = {k: v for k, v in c.items() if k not in ("uuid", "name", "classes", "level", "level_up", "wants_talk")}
        if rest:
            t += " " + compact(rest, H)
        out.append(t)
    return "; ".join(out) + " (at camp, not in the party)"


def render_status(s: dict, ui: dict | None, H: Handles) -> str:
    lines = []
    sub = s.get("subregion") or (ui or {}).get("region")
    where = " · ".join(str(x) for x in (sub, s.get("region")) if x)
    mode = []
    if s.get("in_combat"):
        c = s.get("combat") or {}
        cur = c.get("current")
        cur_s = H.get(cur) if isinstance(cur, str) and UUID_RE.search(cur) else cur
        mode.append(f"COMBAT round {c.get('round', '?')} turn={cur_s or '?'}")
    if s.get("turn_based"):
        mode.append("turn-based")
    if s.get("in_dialogue"):
        mode.append("DIALOGUE")
    if ui and ui.get("screen") not in (None, "game"):
        mode.append(f"screen={ui['screen']}")
    lines.append(f"WHERE {where or '?'}" + (f" | {' '.join(mode)}" if mode else " | exploring"))
    if s.get("gold") is not None:
        lines[-1] += f" | gold {s['gold']}"
    if s.get("turn_based") and not s.get("in_combat"):
        lines.append("TURN-BASED outside combat (the game switched it on): walks leave it first by themselves (the "
                     "HUD's Exit TB button); set_party(action='turn_based_off') leaves it now")
    me = s.get("active")
    for c in s.get("party") or []:
        lines.append("PARTY " + character_line(c, H, me))
    # geo_gen: the bridge's own counter of doors/walls changing near the party, seen or not (audit C18)
    extra = {k: v for k, v in s.items() if k not in ("region", "subregion", "level", "in_combat", "combat",
                                                     "turn_based", "in_dialogue", "gold", "active", "party",
                                                     "geo_gen")}
    camp = extra.pop("camp", None)
    if camp:
        lines.append("CAMP " + camp_text(camp, H))
    if extra:
        lines.append("ALSO " + compact(extra, H))
    if ui:
        lines.extend(render_ui(ui, H, brief=True))
    return "\n".join(lines)


MAX_REMEMBERED = 5  # remembered (out of sight) characters listed by get_nearby; the rest by name
_KIND_RANK = {"door": 0, "container": 0, "ladder": 0, "waypoint": 0, "lever": 0, "trap": 0, "plate": 1, "book": 1,
              "item": 2, "usable": 3, "object": 9}


_FLAGS = ("locked", "opened", "owned", "trap", "closed", "open", "story", "rarity", "count", "near", "effect")


def _same_key(it: dict) -> tuple | None:
    """Grouping key of a plain item (name, kind); None when a flag sets it apart. Floor plates are never folded:
    each is a puzzle piece with its own id (S084 vault: 9 Marble Plates, a code to step on)."""
    if it.get("kind") == "plate" or any(it.get(f) not in (None, False, "Common", "common", 1) for f in _FLAGS):
        return None
    return (it.get("name"), it.get("kind"))


def render_nearby(n: dict, H: Handles, max_items: int = 30, fold: bool = True) -> str:
    lines = []
    chars = n.get("characters") or []
    items = [it for it in (n.get("items") or []) if it.get("kind") != "object" or it.get("locked") or it.get("trap") or it.get("near")]
    for c in chars:
        line = "CHAR " + character_line(c, H)
        if c.get("pos") and not c.get("dead"):
            line += f" @{pos(c['pos'])}"
        lines.append(line)

    def rank(it: dict) -> tuple:
        k = _KIND_RANK.get(str(it.get("kind")), 4)
        if it.get("story") or it.get("near") or it.get("rarity") not in (None, "Common", "common"):
            k = min(k, 1)  # "near": a puzzle fixture (Button under a painting) ranks with the story items
        return (k, it.get("dist") or 0)

    items.sort(key=rank)
    # Plain lookalikes (same name and kind, no flag) are one line: the nearest, plus a count. S026 08:55: 25
    # Candle(s) filled the list and "+10 more items" hid the puzzle's Buttons the agent was looking for.
    # fold=False: the agent searched by name (kinds="Thurible"), so it wants every id. S029 16:12: the fold
    # "e549 Mystic Thurible (+5 more …)" hid the ids it had to cast at; it guessed ids with 22 look_at calls.
    groups: dict = {}
    for it in items if fold else ():
        k = _same_key(it)
        if k is not None:
            groups.setdefault(k, []).append(it)
    items_total = len(items)
    items = [it for it in items if len(groups.get(_same_key(it), ())) < SAME_MIN
             or groups[_same_key(it)][0] is it]
    for it in items[:max_items]:
        h = H.get(it["uuid"], it.get("name")) if it.get("uuid") else "?"
        bits = [f"{h} {it.get('name', '?')}"]
        if it.get("kind"):
            bits.append(f"<{it['kind']}>")
        if it.get("dist") is not None:
            bits.append(f"{num(it['dist'])}m")
        for flag in ("locked", "opened", "owned", "trap", "closed", "open", "story", "rarity"):
            v = it.get(flag)
            if v is True:
                bits.append(flag)
            elif isinstance(v, (str, dict)) and v and v not in ("Common", "common"):
                bits.append(f"{flag}={compact(v, H)}")
        if it.get("count", 1) not in (1, None):
            bits.append(f"x{it['count']}")
        if isinstance(it.get("max_hp"), (int, float)) and not _clutter(it):  # breakable (a membrane, a door)
            bits.append(f"HP {num(it.get('hp'))}/{num(it['max_hp'])}")
        if it.get("near"):
            bits.append(f"({it['near']})")
        if it.get("on"):  # a floor plate: who of the party stands on it (presses it)
            bits.append("[on it: " + ", ".join(f"{H.get(u)} {H.names.get(H.get(u), '?')}" for u in it["on"]) + "]")
        if it.get("effect"):  # a painting lit by its button press (puzzle state)
            bits.append(f"[{it['effect']}]")
        g = groups.get(_same_key(it)) if _same_key(it) is not None else None
        if g is not None and len(g) >= SAME_MIN:
            far = max((o.get("dist") or 0) for o in g)
            bits.append(f"(+{len(g) - 1} more {it.get('name', '?')} up to {num(far)}m)")
        lines.append("ITEM " + " ".join(bits))
    hidden = len(items) - min(len(items), max_items)
    scenery = len(n.get("items") or []) - items_total
    if hidden or scenery:
        lines.append(f"(+{hidden} more items, {scenery} scenery objects not listed; narrow with radius= or kinds=)")
    # Out of sight now, most recent first (the mod sends last_pos/ago_s). S009 printed 20 of these as
    # "@None ?s ago" per get_nearby; the few freshest are what a search for someone needs.
    mem = n.get("remembered") or []
    for r in mem[:MAX_REMEMBERED]:
        h = H.get(r["uuid"], r.get("name")) if r.get("uuid") else "?"
        p, ago = r.get("last_pos", r.get("pos")), r.get("ago_s", r.get("age_s"))
        lines.append(f"REMEMBERED {h} {r.get('name', '?')} last seen"
                     + (f" @{pos(p)}" if p else "") + (f" {num(ago)}s ago" if ago is not None else ""))
    if len(mem) > MAX_REMEMBERED:
        lines.append(f"(+{len(mem) - MAX_REMEMBERED} more remembered out of sight: "
                     + ", ".join(str(r.get("name", "?")) for r in mem[MAX_REMEMBERED:MAX_REMEMBERED + 8]) + ")")
    if not lines:
        lines.append("Nothing visible in range.")
    return "\n".join(lines)


BOOK_SHOWN = 2500  # chars of an open book shown per result


def render_ui(ui: dict, H: Handles, brief: bool = False) -> list[str]:
    lines = []
    d = ui.get("dialogue")
    if d:
        sp = d.get("speaker")
        if d.get("text"):
            lines.append(f"DIALOGUE {sp + ': ' if sp else ''}{H.scrub(str(d['text']))}")
        for o in d.get("options") or []:
            tags = "".join(f"[{t}]" for t in (o.get("tags") or []))
            chk = f"  ← {o['check']}" if o.get("check") else ""
            lines.append(f"  {o.get('i')}. {tags}{' ' if tags else ''}{H.scrub(str(o.get('text', '')))}{chk}")
        below, above = d.get("more_below"), d.get("more_above")
        if above and d.get("options"):
            lines.append(f"  (+{above} more answers above the visible part of this list, 1-{above})")
        if below and d.get("options"):
            last = max(int(o.get("i") or 0) for o in d["options"]) + int(below)
            lines.append(f"  (+{below} more answers below the visible part of this list, up to {last}: "
                         f"their texts show only when scrolled; the last one, {last}, is usually the exit/Leave)")
    r = ui.get("roll")
    if r:
        lines.append("ROLL " + compact(r, H))
    rx = ui.get("reaction")
    if rx:
        lines.append("REACTION " + compact(rx, H))
    p = ui.get("popup")
    if p and ui.get("screen") == "book" and len(str(p.get("text") or "")) > BOOK_SHOWN:
        # a whole book in every status line while it is open: the start, the rest is kept for recall()
        p = {**p, "text": str(p["text"])[:BOOK_SHOWN] + f"… (+{len(str(p['text'])) - BOOK_SHOWN} chars: recall(\"<words>\") searches the whole text)"}
    if p and p.get("split"):
        # SplitItem (text "/3" = the stack size): S028 the agent clicked its arrow + Confirm by hand.
        size = re.sub(r"\D", "", str(p.get("text") or "")) or "?"
        lines.append(f"POPUP how many of the stack: {p.get('title') or '?'} (1-{size}) → confirm(\"yes\", amount=N) "
                     "or confirm(\"no\")")
    elif p:
        lines.append("POPUP " + compact(p, H) + (" → answer with confirm(\"yes\"|\"no\"|label)"
                                                 if p.get("decision") else ""))
    c = ui.get("container")
    if c:
        lines.append("CONTAINER " + compact(c, H))
    if not brief:
        rest = {k: v for k, v in ui.items() if k not in ("dialogue", "roll", "reaction", "popup", "container", "ms", "region")}
        if rest:
            lines.append("UI " + compact(rest, H))
    elif ui.get("notifications"):
        lines.append("NOTICE " + "; ".join(H.scrub(str(x)) for x in ui["notifications"][-3:]))
    return lines


def _clutter(it: dict) -> bool:
    from . import delta  # (delta imports render)
    return delta.is_clutter(it)


# Level ids → the names the game's map shows (State.lua S.REGION_NAMES). A tool result never prints a level id
# (fairness audit LEAK judged_id: S038/S040 get_explored `level=SCL_Main_A`, S046 look_at `region=SCL_Main_A`).
LEVEL_NAMES = {
    "TUT_Avernus_C": "Nautiloid", "WLD_Main_A": "Wilderness", "CRE_Main_A": "Rosymorn Monastery",
    "SCL_Main_A": "Shadow-Cursed Lands", "INT_Main_A": "Last Light / Moonrise interlude",
    "BGO_Main_A": "Rivington & Wyrm's Crossing", "CTY_Main_A": "Lower City", "IRN_Main_A": "Iron Throne",
    "END_Main": "Upper City", "SYS_CC_I": "Character creation",
}
_LEVEL_ID = re.compile(r"^[A-Z]{3}_[A-Za-z]+(?:_[A-Za-z0-9]+)?$")


def public_levels(x: Any) -> Any:
    """x with every level id value (a whole string such as "SCL_Main_A") replaced by its map name; a "level" key
    becomes "region" (dropped when a region is already there)."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            v = public_levels(v)
            if k == "level" and isinstance(v, str):
                if "region" not in x:
                    out["region"] = v
                continue
            out[k] = v
        return out
    if isinstance(x, list):
        return [public_levels(v) for v in x]
    if isinstance(x, str) and _LEVEL_ID.match(x) and (x in LEVEL_NAMES or x.endswith(("_Main_A", "_Main"))):
        return LEVEL_NAMES.get(x, "another area")
    return x




def _cost_text(cost: Any) -> str:
    if not isinstance(cost, dict):
        return ""
    short = {"action": "A", "bonus": "B", "reaction": "R", "movement": "m"}
    parts = []
    for k, v in cost.items():
        if k == "movement":
            parts.append(f"{num(v)}m")
        elif k == "slot":
            parts.append(f"slot L{num(v)}")
        elif k in short:
            parts.append(short[k] if v in (1, 1.0, True) else f"{num(v)}{short[k]}")
        else:
            parts.append(f"{k}:{num(v)}")
    return "+".join(parts)


# Items on the turn order (doors, candles, books: initiative -20) that matter in a fight keep their own line.
_FIGHT_ITEMS = re.compile(r"trap|tripwire|barrel|explosive|powder|keg|oil|turret|vent|totem|statue|crystal", re.I)


def _combat_object(o: dict) -> bool:
    return bool(o.get("item") or (o.get("initiative") == -20 and o.get("relation") == "neutral")) \
        and not _FIGHT_ITEMS.search(str(o.get("name") or ""))


def render_combat(res: dict, H: Handles) -> str:
    """state.combat as one line per combatant (was one ';'-joined blob); props on the turn order (S025: 22
    candles, books and doors at initiative -20, ~2.5 KB with their distance matrices) folded into one
    OBJECTS line of names."""
    order = res.get("order") if isinstance(res.get("order"), list) else []
    head = {k: v for k, v in res.items() if k not in ("order", "active")}
    lines = ["COMBAT " + compact(head, H)] if head else ["COMBAT"]
    objs: dict[str, int] = {}
    for o in order:
        if isinstance(o, dict) and _combat_object(o):
            n = str(o.get("name") or "?")
            objs[n] = objs.get(n, 0) + 1
        else:
            lines.append("  " + compact({k: v for k, v in o.items() if k != "disabled"}, H))  # DISABLED lines
    if objs:
        lines.append("OBJECTS on the turn order (props): "
                     + ", ".join(n + (f" x{c}" if c > 1 else "") for n, c in sorted(objs.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def turn_limit_line(t: Any) -> str:
    """ui.story_timer (the counter under the minimap) as one line, '' when none is on screen. S091: the
    Netherbrain's psyche gave 4 turns, seen only on screenshots; try 4 ran out of them with the brain at 28 HP."""
    if not isinstance(t, dict) or not (t.get("text") or t.get("turns_left") is not None):
        return ""
    left = t.get("turns_left")
    head = "TURN LIMIT (the counter on screen)" + (f": {left} turn{'' if left == 1 else 's'} left" if left is not None
                                                 else "")
    return head + (f" — {t['text']}" if t.get("text") else "")


# Unavailable only because that resource is spent this turn (the NOW line shows it): counted, not listed.
_SPENT_REASONS = {"no action": "[A] ones", "no bonus action": "[B] ones", "no reaction": "[R] ones"}
# Spell ids in action lists ("Fire Bolt (Projectile_FireBolt)"): ~1/3 of a ~2.5 KB list. Off: use_ability and
# combat_turn take the shown name (server._ability_id maps it back from the character's own list).
ACTION_IDS = False
UNAVAILABLE_GROUP_MIN = 3  # other reasons shared by this many actions list the names once


def render_actions(r: Any, H: Handles) -> str:
    """One short line per action: `Name (spell_id) [A+slot L1] 18m entity`, unavailable ones last."""
    acts = r.get("actions") if isinstance(r, dict) else r
    if not isinstance(acts, list):
        return compact(r, H)
    head = []
    if isinstance(r, dict) and r.get("resources"):
        head.append("NOW " + resources_text(r["resources"]))
    ok_lines, no_lines = [], []
    spent: dict[str, int] = {}
    seen: dict[str, int] = {}
    for a in acts:
        seen[str(a.get("name"))] = seen.get(str(a.get("name")), 0) + 1
    for a in acts:
        # ACTION_IDS False: names only (use_ability takes the shown name), the id only where two share a name
        show_id = ACTION_IDS or seen[str(a.get("name"))] > 1
        bits = [f"{a.get('name')} ({a.get('spell')})" if show_id else str(a.get("name"))]
        c = _cost_text(a.get("cost"))
        if a.get("extra_attack"):
            bits.append("[Extra Attack: free]")  # the EXTRA_ATTACK status: no action cost now (Guard.lua)
        elif c:
            bits.append(f"[{c}]")
        if a.get("range") not in (None, "", 0):
            bits.append(f"{num(a['range']) if not isinstance(a['range'], str) else a['range']}m")
        if a.get("target") and a.get("target") != "entity":
            bits.append(str(a["target"]))
        if a.get("aoe"):
            bits.append(f"aoe {num(a['aoe'])}m")
        if a.get("level"):
            bits.append(f"L{a['level']}")
        if a.get("uses") not in (None, ""):
            bits.append(f"uses {compact(a['uses'], H, 1)}")
        if a.get("available") is False:
            why = str(a.get("reason") or "")
            if why in _SPENT_REASONS:  # the NOW line already says it is spent: count, don't list (S025: 60 entries)
                spent[why] = spent.get(why, 0) + 1
            else:
                no_lines.append((why, str(a.get("name")), " ".join(bits) + (f" — {why}" if why else "")))
        else:
            ok_lines.append(" ".join(bits))
    out = head + ok_lines
    no = [f"{why}: all {n} {_SPENT_REASONS[why]}" for why, n in spent.items()]
    # One reason shared by many (S026: 13 "... [slot L3+A] 9m L3 — no spell slot of level 3+", ~1.2 KB per turn):
    # names only, once ("no spell slot of level 3+: Speak with Dead, Beacon of Hope, ...").
    by_why: dict[str, list[str]] = {}
    for why, name, _ in no_lines:
        by_why.setdefault(why, []).append(name)
    grouped: set[str] = set()
    for why, name, line in no_lines:
        if why and len(by_why[why]) >= UNAVAILABLE_GROUP_MIN:
            if why not in grouped:
                grouped.add(why)
                no.append(f"{why}: {', '.join(by_why[why])}")
        else:
            no.append(line)
    if no:
        out.append("UNAVAILABLE: " + "; ".join(no))
    return "\n".join(out)


def render_journal(quests: Any, H: Handles, quest: str = "") -> str:
    """Active quests with their latest step(s); finished quests as a title list. quest= drills into one."""
    if not isinstance(quests, list):
        return compact(quests, H)
    if quest:
        q = [x for x in quests if quest.lower() in str(x.get("title", "")).lower()]
        if not q:
            return f"No quest matching '{quest}'."
        return "\n".join(f"{x.get('title')} [{x.get('category', '')}]{' (done)' if x.get('done') else ''}\n" +
                          "\n".join(f"  - {H.scrub(str(st))}" for st in x.get("steps") or []) for x in q)
    active = [x for x in quests if not x.get("done")]
    done = [x for x in quests if x.get("done")]
    lines = [f"ACTIVE ({len(active)})"]
    for x in active:
        steps = x.get("steps") or []
        last = H.scrub(str(steps[-1])) if steps else ""
        lines.append(f"- {x.get('title')} [{x.get('category', '')}]: {last}")
    if done:
        lines.append(f"DONE ({len(done)}): " + "; ".join(str(x.get("title")) for x in done))
    lines.append("(get_journal quest=<title words> for all steps of one quest)")
    return "\n".join(lines)


def render_map(m: Any, H: Handles) -> str:
    """get_map's REGION + WAYPOINTS: the names a player reads on the map. Level/waypoint ids (WLD_Main_A,
    WAYP_CHA_Top) stay internal (fairness #8); fast_travel/travel_to take these names."""
    if not isinstance(m, dict):
        return compact(m, H)
    where = " · ".join(str(x) for x in (m.get("subregion"), m.get("region")) if x)
    lines = [f"REGION {where or '?'}"]
    here, other = [], {}
    for w in m.get("waypoints") or []:
        if not isinstance(w, dict) or not w.get("name"):
            continue
        if w.get("here"):
            here.append(str(w["name"]))
        else:
            other.setdefault(str(w.get("region") or "elsewhere"), []).append(str(w["name"]))
    if here or other:
        lines.append("WAYPOINTS (fast_travel waypoint=<name>)")
        if here:
            lines.append("  here: " + ", ".join(here))
        for reg, names in other.items():
            lines.append(f"  {reg}: " + ", ".join(names))
    else:
        lines.append("WAYPOINTS none unlocked")
    extra = {k: v for k, v in m.items() if k not in ("level", "region", "subregion", "waypoints")}
    if extra:
        lines.append(compact(extra, H))
    return "\n".join(lines)


_CONSUMABLE = re.compile(r"potion|elixir|scroll|arrow of|grenade|alchemist's fire|acid vial|bomb|oil of|"
                         r"thieves' tools|trap disarm|healing|antitoxin|camp suppl|key\b|lockpick|tools", re.I)
_SKIP_SLOTS = {"Underwear", "VanityBody", "VanityBoots"}
_RARE = {"uncommon": 1, "rare": 2, "very_rare": 3, "legendary": 4, "story": 5}


def render_inventory(inv: Any, H: Handles, filter: str = "") -> str:
    """Per owner: equipment, then notable items (story/magic/consumables/tools/bags); junk summarised.
    filter: 'equipped' | 'consumables' | 'all' | any words to match item names."""
    if not isinstance(inv, dict) or "items" not in inv:
        return compact(inv, H)
    f = (filter or "").strip().lower()
    items = inv.get("items") or []
    owners: dict[str, list] = {}
    for it in items:
        owners.setdefault(it.get("owner") or "?", []).append(it)
    from . import economy  # resources & economy (owner: setup): supply values, junk, gear upgrades

    lines = [f"GOLD {inv.get('gold', '?')}"
             + (f" · CAMP SUPPLIES {economy.supplies_text(inv)}" if economy.supplies_text(inv) else "")]
    junk_ids = {it["uuid"] for it in economy.junk(inv)} if f in ("junk", "", "notable") else set()
    if inv.get("open_container"):
        lines.append("OPEN CONTAINER " + compact(inv["open_container"], H))
    matched, by_name = 0, False
    for owner, its in owners.items():
        oname = H.names.get(H.get(owner)) if owner != "?" else "?"
        head = f"== {H.get(owner) if owner != '?' else '?'} {oname or ''}".rstrip()
        eq = [it for it in its if it.get("equipped_slot") and it["equipped_slot"] not in _SKIP_SLOTS]
        rest = [it for it in its if not it.get("equipped_slot")]

        def line(it: dict) -> str:
            h = H.get(it["uuid"], it.get("name"))
            b = [f"{h} {it.get('name')}"]
            if it.get("count", 1) not in (1, None):
                b.append(f"x{it['count']}")
            if it.get("rarity"):
                b.append(it["rarity"].replace("_", " "))
            if it.get("story"):
                b.append("story")
            if it.get("container"):
                b.append(f"bag({it.get('contains', 0)})")
            if it.get("slot_type") and not it.get("equipped_slot"):
                b.append(f"<{it['slot_type']}>")
            if it.get("value", 0) >= 50:
                b.append(f"{it['value']}g")
            if it.get("supply"):
                b.append(f"supply={it['supply']}" + (f" (x{it['count']}={it['supply'] * it['count']})"
                                                      if it.get("count", 1) not in (1, None) else ""))
            return " ".join(b)

        if f == "equipped":
            sel_eq, sel = eq, []
        elif f in ("", "notable"):
            sel_eq = eq
            sel = [it for it in rest if it.get("story") or it.get("rarity") in _RARE or it.get("container")
                   or _CONSUMABLE.search(it.get("name", ""))]
        elif f == "consumables":
            sel_eq, sel = [], [it for it in rest if _CONSUMABLE.search(it.get("name", ""))]
        elif f == "all":
            sel_eq, sel = eq, rest
        elif f in ("food", "supplies", "supply", "camp supplies"):
            sel_eq, sel = [], [it for it in its if economy.is_food(it)]
        elif f == "junk":
            sel_eq, sel = [], [it for it in rest if it["uuid"] in junk_ids]
        else:
            # "leap,misty,jump" (S039 21:43: one comma-joined word matched nothing, read as "no Misty Step"):
            # commas or | separate alternatives, spaces inside one must all match ("potion healing")
            alts = [a.split() for a in re.split(r"[,|]", f) if a.strip()]
            sel_eq = []
            sel = [it for it in its
                   if any(all(w in it.get("name", "").lower() for w in ws) for ws in alts)]
            matched, by_name = matched + len(sel), True
        if not (sel_eq or sel):
            continue
        lines.append(head)
        if sel_eq:
            lines.append("  equipped: " + "; ".join(f"{it['equipped_slot']}={H.get(it['uuid'], it.get('name'))} {it.get('name')}"
                                                   + (f" ({it['rarity'].replace('_', ' ')})" if it.get("rarity") else "")
                                                   for it in sel_eq))
        for it in sorted(sel, key=lambda x: (-_RARE.get(x.get("rarity") or "", 0) - (5 if x.get("story") else 0),
                                            x.get("name", ""))):
            lines.append("  " + line(it))
        if f in ("", "notable"):
            junk = [it for it in rest if it not in sel]
            if junk:
                lines.append(f"  (+{len(junk)} other items, {sum(i.get('value', 0) * (i.get('count') or 1) for i in junk)}g total; filter='all' to list)")
    if f in ("", "notable", "gear", "equipped"):
        ups = economy.upgrades(inv)
        if ups:
            lines.append("BETTER GEAR IN BAGS (equip_best equips all): " + "; ".join(
                f"{H.names.get(H.get(u['who'])) or H.get(u['who'])}: {H.get(u['uuid'])} {u['name']} → {u['slot']} ({u['why']})"
                for u in ups))
        elif f == "gear":
            lines.append("No better gear in the bags.")
    if junk_ids and f in ("", "notable", "junk"):
        js = [it for it in items if it["uuid"] in junk_ids]
        lines.append(f"JUNK {len(js)} items ~{sum((i.get('value') or 0) * (i.get('count') or 1) for i in js)}g "
                     f"{sum((i.get('weight') or 0) * (i.get('count') or 1) for i in js):.1f}kg: trade sell=junk at a "
                     "trader, or manage_inventory op=camp item=junk")
    if by_name and not matched:
        lines.append(f"No item in the party's bags matches {filter.strip()!r} (names; separate alternatives with "
                     "commas: 'misty,scroll').")
    return "\n".join(lines)
