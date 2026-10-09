"""Turn mod JSON into compact, token-cheap text for the agent.

Entity GUIDs never reach the agent: `Handles` maps them to short ids (e1, e2, ...) that are stable for
the life of the MCP server process (one agent session). Renderers are defensive: unknown keys still
render as `key=value`, so the bridge keeps working while the mod's output shapes evolve.
"""

from __future__ import annotations

import re
from typing import Any

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Template-prefixed GUIDs ("S_Player_Laezel_58a69333-...") also occur in Osiris land.
PREFIXED_UUID_RE = re.compile(r"\b\w+_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.I)


class Handles:
    def __init__(self) -> None:
        self.by_uuid: dict[str, str] = {}
        self.by_handle: dict[str, str] = {}
        self.names: dict[str, str] = {}  # handle -> last known display name
        self._n = 0

    def get(self, uuid: str, name: str | None = None) -> str:
        uuid = self._bare(uuid)
        h = self.by_uuid.get(uuid)
        if h is None:
            self._n += 1
            h = f"e{self._n}"
            self.by_uuid[uuid] = h
            self.by_handle[h] = uuid
        if name:
            self.names[h] = name
        return h

    @staticmethod
    def _bare(uuid: str) -> str:
        m = PREFIXED_UUID_RE.fullmatch(uuid)
        return (m.group(1) if m else uuid).lower()

    def resolve(self, ref: str) -> str:
        """Agent reference (handle, or a unique visible/party name) -> uuid."""
        if ref is None:
            raise ValueError("missing target")
        r = str(ref).strip()
        if r.lower() in self.by_handle:
            return self.by_handle[r.lower()]
        low = r.lower()
        matches = [h for h, n in self.names.items() if n and n.lower() == low]
        if not matches:
            matches = [h for h, n in self.names.items() if n and n.lower().startswith(low)]
        if len(matches) == 1:
            return self.by_handle[matches[0]]
        if len(matches) > 1:
            opts = ", ".join(f"{h} ({self.names[h]})" for h in matches[:8])
            raise ValueError(f"'{ref}' is ambiguous: {opts}")
        raise ValueError(f"unknown target '{ref}'; use an id like e12 from get_nearby/get_status, or a party member name")

    def scrub(self, text: str) -> str:
        """Replace any GUID that slipped into free text with its handle."""
        text = PREFIXED_UUID_RE.sub(lambda m: self.get(m.group(1)), text)
        return UUID_RE.sub(lambda m: self.get(m.group(0)), text)


def num(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return str(x)


def pos(p: Any) -> str:
    if isinstance(p, (list, tuple)) and len(p) >= 3:
        return f"({num(p[0])},{num(p[1])},{num(p[2])})"
    return str(p)


def compact(v: Any, H: Handles, depth: int = 0) -> str:
    """Generic fallback renderer: JSON-ish but terse, with GUIDs swapped for handles."""
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if x is None or x == [] or x == {} or x is False:
                continue
            if k in ("uuid", "id", "guid") and isinstance(x, str) and UUID_RE.search(x):
                parts.append(H.get(x, v.get("name")))
                continue
            if x is True:
                parts.append(k)
            elif k == "resources" and isinstance(x, dict):
                parts.append(f"resources: {resources_text(x)}")
            elif k == "classes" and isinstance(x, list):
                parts.append(classes_text(x))
            elif k in ("pos", "position"):
                parts.append(f"@{pos(x)}")
            else:
                parts.append(f"{k}={compact(x, H, depth + 1)}")
        s = " ".join(parts)
        return s if depth == 0 else "{" + s + "}"
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return "[" + ",".join(compact(x, H, depth + 1) for x in v) + "]"
        if depth == 0:
            return "\n".join("- " + compact(x, H, 1).strip("{}") for x in v)
        return "[" + "; ".join(compact(x, H, depth + 1).strip("{}") for x in v) + "]"
    if isinstance(v, str):
        return H.scrub(v)
    return num(v)


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


def classes_text(cls: list) -> str:
    out = []
    for x in cls:
        if isinstance(x, dict):
            name = x.get("class") or x.get("name") or "?"
            t = f"{name}{x.get('level', '')}"
            if x.get("subclass"):
                t += f"({x['subclass']})"
            out.append(t)
        else:
            out.append(str(x))
    return "/".join(out)


_RES_ALIASES = {"ActionPoint": "Action", "BonusActionPoint": "Bonus", "ReactionActionPoint": "Reaction",
                "Movement": "Move", "SpellSlot": "Slots"}


def resources_text(res: dict) -> str:
    """{'ActionPoint':[1,1],'SpellSlot':{'1':[4,4]}} -> 'Action 1/1 · Slots L1 4/4 ...' (fixed order first)."""
    order = ["ActionPoint", "BonusActionPoint", "Movement", "ReactionActionPoint", "SpellSlot"]
    keys = [k for k in order if k in res] + sorted(k for k in res if k not in order)
    parts = []
    for k in keys:
        v = res[k]
        name = _RES_ALIASES.get(k, k)
        if isinstance(v, dict):
            slots = " ".join(f"L{lvl} {num(cm[0])}/{num(cm[1])}" for lvl, cm in sorted(v.items(), key=lambda kv: str(kv[0]))
                             if isinstance(cm, (list, tuple)) and len(cm) == 2)
            parts.append(f"{name} {slots}")
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            parts.append(f"{name} {num(v[0])}/{num(v[1])}" + ("m" if k == "Movement" else ""))
        else:
            parts.append(f"{name} {num(v)}")
    return " · ".join(parts)


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
    for flag in ("downed", "dead", "in_combat", "can_talk", "is_trader", "sneaking", "is_current"):
        if c.get(flag):
            bits.append(flag)
    return " ".join(bits)


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
    me = s.get("active")
    for c in s.get("party") or []:
        lines.append("PARTY " + character_line(c, H, me))
    extra = {k: v for k, v in s.items() if k not in ("region", "subregion", "level", "in_combat", "combat",
                                                     "turn_based", "in_dialogue", "gold", "active", "party")}
    if extra:
        lines.append("ALSO " + compact(extra, H))
    if ui:
        lines.extend(render_ui(ui, H, brief=True))
    return "\n".join(lines)


MAX_REMEMBERED = 5  # remembered (out of sight) characters listed by get_nearby; the rest by name
_KIND_RANK = {"door": 0, "container": 0, "ladder": 0, "waypoint": 0, "lever": 0, "trap": 0, "book": 1,
              "item": 2, "usable": 3, "object": 9}


def render_nearby(n: dict, H: Handles, max_items: int = 30) -> str:
    lines = []
    chars = n.get("characters") or []
    items = [it for it in (n.get("items") or []) if it.get("kind") != "object" or it.get("locked") or it.get("trap")]
    for c in chars:
        line = "CHAR " + character_line(c, H)
        if c.get("pos") and not c.get("dead"):
            line += f" @{pos(c['pos'])}"
        lines.append(line)

    def rank(it: dict) -> tuple:
        k = _KIND_RANK.get(str(it.get("kind")), 4)
        if it.get("story") or it.get("rarity") not in (None, "Common", "common"):
            k = min(k, 1)
        return (k, it.get("dist") or 0)

    items.sort(key=rank)
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
        lines.append("ITEM " + " ".join(bits))
    hidden = len(items) - min(len(items), max_items)
    scenery = len(n.get("items") or []) - len(items)
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
    r = ui.get("roll")
    if r:
        lines.append("ROLL " + compact(r, H))
    rx = ui.get("reaction")
    if rx:
        lines.append("REACTION " + compact(rx, H))
    p = ui.get("popup")
    if p:
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


# Event kinds that only duplicate what an action's own result already says.
_REDUNDANT_EVENTS = {"moved_done"}
# Consecutive events of these kinds share one line ("turn_ended Claude e4; Wyll e1; ...").
_RUN_EVENTS = {"turn_ended", "turn_started"}


def _ref_handle(x: Any, H: Handles) -> str | None:
    return H.get(x["uuid"], x.get("name")) if isinstance(x, dict) and isinstance(x.get("uuid"), str) else None


def _already_shown(e: dict, H: Handles, body: str, own: str | None, job: str | None) -> bool:
    """True when the result's own text already reports this event (S013: 'think' grows with every repeated
    line): the turn owner's cast/damage/statuses (combat_turn prints them), the job a wait_for returned, and
    the queue report a wait_for('queue') printed."""
    kind, d = e.get("kind"), e.get("data")
    if not isinstance(d, dict):
        return False
    if own and kind in ("cast", "damage", "status_applied"):
        src = _ref_handle(d.get("caster") or d.get("source"), H)
        if src == own or (kind == "status_applied" and src is None and _ref_handle(d.get("target"), H) == own):
            return True
    if job and kind == "job_finished" and d.get("job") == job:
        return True
    if kind in ("queue_done", "queue_interrupted") and body and isinstance(d.get("who"), str):
        return f"queue {H.scrub(d['who'])} " in body
    return False


def render_events(evs: list[dict], H: Handles, limit: int = 25, body: str = "", job: str | None = None) -> str:
    """EVENTS section. body: the tool result's own text, so events it already reports are left out
    (job: the job id a wait_for just returned)."""
    m = re.match(r"TURN (e\d+)", body or "")
    own = m.group(1) if m else None
    evs = [e for e in evs if e.get("kind") not in _REDUNDANT_EVENTS and not _already_shown(e, H, body, own, job)]
    if not evs:
        return ""
    shown = evs[-limit:]
    out = []
    if len(evs) > limit:
        out.append(f"(+{len(evs) - limit} earlier events)")
    prev = None
    for e in shown:
        data = e.get("data")
        txt = compact(data, H) if isinstance(data, (dict, list)) else (H.scrub(str(data)) if data is not None else "")
        kind = e.get("kind")
        if kind in _RUN_EVENTS and prev == kind:
            out[-1] += f"; {txt}"
        else:
            out.append(f"{kind} {txt}".strip())
        prev = kind
    return "EVENTS\n" + "\n".join("  " + x for x in out)


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


def render_actions(r: Any, H: Handles) -> str:
    """One short line per action: `Name (spell_id) [A+slot L1] 18m entity`, unavailable ones last."""
    acts = r.get("actions") if isinstance(r, dict) else r
    if not isinstance(acts, list):
        return compact(r, H)
    head = []
    if isinstance(r, dict) and r.get("resources"):
        head.append("NOW " + resources_text(r["resources"]))
    ok_lines, no_lines = [], []
    for a in acts:
        bits = [f"{a.get('name')} ({a.get('spell')})"]
        c = _cost_text(a.get("cost"))
        if c:
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
            no_lines.append(" ".join(bits) + (f" — {a['reason']}" if a.get("reason") else ""))
        else:
            ok_lines.append(" ".join(bits))
    out = head + ok_lines
    if no_lines:
        out.append("UNAVAILABLE: " + "; ".join(no_lines))
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
    lines = [f"GOLD {inv.get('gold', '?')}"]
    if inv.get("open_container"):
        lines.append("OPEN CONTAINER " + compact(inv["open_container"], H))
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
        else:
            words = f.split()
            sel_eq = []
            sel = [it for it in its if all(w in it.get("name", "").lower() for w in words)]
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
    return "\n".join(lines)


def render_frontier(res: Any, H: Handles) -> str:
    leads = res.get("leads") if isinstance(res, dict) else None
    if not isinstance(leads, list):
        return compact(res, H)
    out = []
    for l in leads:
        h = H.get(l["uuid"], l.get("name")) if l.get("uuid") else "?"
        bits = [f"{h} {l.get('name', '?')} <{l.get('kind', '?')}> {num(l.get('dist', '?'))}m {l.get('dir', '')}"]
        dy = l.get("dy")
        if isinstance(dy, (int, float)) and abs(dy) >= 1.5:
            bits.append(f"({num(abs(dy))}m {'up' if dy > 0 else 'down'})")
        if l.get("kind") == "ladder" and l.get("height_m") and l.get("leads"):
            bits.append(f"climbs {num(l['height_m'])}m {l['leads']}")
        for flag in ("unvisited", "locked", "waypoint"):
            if l.get(flag):
                bits.append(flag)
        if l.get("relation"):
            bits.append(str(l["relation"]))
        out.append(" ".join(bits))
    if isinstance(res, dict) and res.get("more"):
        out.append(f"(+{res['more']} more leads)")
    if not out:
        out.append("No leads in view. Try another direction, a door, or get_map.")
    out.extend(render_hazards(res.get("hazards") if isinstance(res, dict) else None, limit=6))
    return "\n".join(out)


def render_path(res: Any, H: Handles) -> str:
    """nav.path -> one line. Drops the route points (dozens of coordinates) and says plainly when there is
    no path; error_cause is the engine's opaque code, so it is left out."""
    if not isinstance(res, dict):
        return compact(res, H)
    if res.get("reachable") is False:
        return (f"UNREACHABLE: no walkable path ({num(res.get('straight_m', '?'))}m straight line): "
                "it may need a ladder, a jump (jump_to), a door, or another route")
    if res.get("reachable") is None:
        return compact({k: v for k, v in res.items() if k != "points"}, H)
    bits = [f"reachable: walk {num(res.get('length_m', '?'))}m"]
    if res.get("needs_jump"):
        bits.append("(route uses a jump/climb link)")
    if res.get("estimate"):
        bits.append("(estimate)")
    if res.get("approach_pos"):
        bits.append(f"ends @{pos(res['approach_pos'])}")
    if "within_movement" in res:
        bits.append(("fits" if res["within_movement"] else "does NOT fit")
                    + f" movement left {num(res.get('movement_left', '?'))}m")
    if res.get("hazard"):
        bits.append(f"hazard on the way: {compact(res['hazard'], H, 1)}")
    return " ".join(bits)


def objective_label(m: dict) -> str:
    return str(m.get("label") or m.get("objective") or m.get("quest") or "?")


def render_objectives(markers: Any, here_only: bool = False, limit: int | None = None) -> list[str]:
    """Quest/objective markers (nav.markers) as `OBJECTIVE o1 <label> 16m W [quest]` lines.

    Ids o1, o2, ... follow the list order (here first, nearest first), which is the order the server
    returns; walk_toward accepts them. Markers in another area have no distance; with here_only they
    collapse into one "(+n elsewhere)" line.
    """
    if not isinstance(markers, list):
        return []
    lines, elsewhere = [], 0
    for i, m in enumerate(markers, 1):
        if not isinstance(m, dict):
            continue
        here = m.get("here") and m.get("dist") is not None
        if here_only and not here:
            elsewhere += 1
            continue
        if limit is not None and len(lines) >= limit:
            elsewhere += 1
            continue
        label = objective_label(m)
        s = f"OBJECTIVE o{i} {label}"
        if here:
            s += f" {num(m['dist'])}m {m.get('dir') or ''}".rstrip()
        else:
            s += " (another area)"
        quest = m.get("quest")
        if quest and quest != label:
            s += f" [{quest}]"
        lines.append(s)
    if elsewhere:
        lines.append(f"(+{elsewhere} more objective{'s' if elsewhere != 1 else ''}; get_map lists all)")
    return lines


def find_objective(markers: Any, ref: str) -> dict:
    """Pick a marker by id (o2) or by words from its label/quest/objective text."""
    ms = [m for m in markers if isinstance(m, dict)] if isinstance(markers, list) else []
    if not ms:
        raise ValueError("no active objective markers")
    r = (ref or "").strip().lower()
    m_id = re.fullmatch(r"o?(\d+)", r)
    if m_id:
        i = int(m_id.group(1))
        if 1 <= i <= len(markers):
            return markers[i - 1]
        raise ValueError(f"no objective o{i}; there are {len(markers)}")
    words = r.split()
    hits = [m for m in ms if all(w in " ".join(str(m.get(k) or "") for k in ("label", "quest", "objective")).lower()
                                 for w in words)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no objective matches '{ref}'")
    raise ValueError(f"'{ref}' matches {len(hits)} objectives; use an id like o1")


def render_hazards(hazards: Any, limit: int = 4) -> list[str]:
    """nav.hazards patches -> ['HAZARD Fire 3m W (3x4m)', 'HAZARD Fire here (you stand in it; 2x2m)']."""
    if not isinstance(hazards, list):
        return []
    out = []
    for h in hazards[:limit]:
        if not isinstance(h, dict) or not h.get("surface"):
            continue
        size = f"{num(h.get('w_m', '?'))}x{num(h.get('d_m', '?'))}m"
        if h.get("under"):
            out.append(f"HAZARD {h['surface']} here (you stand in it; {size})")
        else:
            out.append(f"HAZARD {h['surface']} {num(h.get('dist', '?'))}m {h.get('dir') or ''} ({size})".replace("  ", " "))
    if len(hazards) > limit:
        out.append(f"(+{len(hazards) - limit} more hazard patches)")
    return out
