"""Turn mod JSON into compact, token-cheap text for the agent.

Entity GUIDs never reach the agent: `Handles` maps them to short ids (e1, e2, ...) that are stable for
the life of the MCP server process (one agent session). Renderers are defensive: unknown keys still
render as `key=value`, so the bridge keeps working while the mod's output shapes evolve.
"""

from __future__ import annotations

import json
import math
import re
import time
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
        matches = self.matches(r)
        if len(matches) == 1:
            return self.by_handle[matches[0]]
        if len(matches) > 1:
            opts = ", ".join(f"{h} ({self.names[h]})" for h in matches[:8])
            raise ValueError(f"'{ref}' is ambiguous: {opts}")
        raise ValueError(f"unknown target '{ref}'; use an id like e12 from get_nearby/get_status, or a party member name")

    def matches(self, ref: str) -> list[str]:
        """Handles whose name equals ref (or, failing that, starts with it), case-insensitive."""
        low = str(ref).strip().lower()
        out = [h for h, n in self.names.items() if n and n.lower() == low]
        out = out or [h for h, n in self.names.items() if n and n.lower().startswith(low)]
        if out or len(low) < 3:
            return out
        # S045 00:15: look_at("Aylin") for "Dame Aylin" → unknown target: a whole word of the name matches too
        # S055 06:41 "Zanner Toobin" for "Gondian Zanner Toobin": whole words in a row match too
        words = " ".join(re.findall(r"[\w-]+", low))
        return [h for h, n in self.names.items()
                if n and words and f" {words} " in " " + " ".join(re.findall(r"[\w-]+", n.lower())) + " "]

    def scrub(self, text: str) -> str:
        """Replace any GUID that slipped into free text with its handle."""
        text = PREFIXED_UUID_RE.sub(lambda m: self.get(m.group(1)), text)
        return UUID_RE.sub(lambda m: self.get(m.group(0)), text)



def raised_ladder(l: dict) -> str:
    """A ladder pulled up out of reach (Navigation.lua N.ladderRaised; S059 08:56 Wyrm's Rock e683)."""
    gap = num(l.get("raised_m"))
    if l.get("leads") == "up":
        return f"PULLED UP: its foot hangs {gap}m above the floor, can't be climbed from below (lowered from above)"
    return f"(its foot hangs {gap}m above the floor below)"

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
    if c.get("wants_talk"):  # the "!" over them (State.lua HasExclamationDialog)
        bits.append("wants to talk(!)")
    if c.get("summon"):  # party row of a Spiritual Weapon, familiar...: an ally, not a companion
        bits.append(f"summon of {H.get(c['owner'])}" if c.get("owner") else "summon")
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
    if s.get("turn_based") and not s.get("in_combat"):
        lines.append("TURN-BASED outside combat (the game switched it on): walks leave it first by themselves (the "
                     "HUD's Exit TB button); set_party(action='turn_based_off') leaves it now")
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


SAME_MIN = 3  # this many plain items with one name and kind are listed as one line
_FLAGS = ("locked", "opened", "owned", "trap", "closed", "open", "story", "rarity", "count", "near", "effect")


def _same_key(it: dict) -> tuple | None:
    """Grouping key of a plain item (name, kind); None when a flag sets it apart."""
    if any(it.get(f) not in (None, False, "Common", "common", 1) for f in _FLAGS):
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


# Event kinds that only duplicate what an action's own result already says. rest_flow: the End-the-Day diagnostics
# (Events.lua REST_FLOW; long_rest/fast_travel read them themselves): their ready-check ids leaked an engine name
# (S044 23:50:48 look: `rest_flow step=ready_ok:MOO_Assault_ReadyCheck`, fairness audit LEAK template).
_REDUNDANT_EVENTS = {"moved_done", "rest_flow"}
# Consecutive events of these kinds share one line ("turn_ended Claude e4; Wyll e1; ...").
_RUN_EVENTS = {"turn_ended", "turn_started", "item_effect"}


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


def _drop_flicker(evs: list[dict]) -> list[dict]:
    """A status gained and lost again (or lost and regained) by the same target within one EVENTS batch is no
    change: S023-S027 printed ~200 Moonshield on/off lines a session as the party walked through lantern light."""
    open_: dict[tuple, int] = {}
    drop: set[int] = set()
    for i, e in enumerate(evs):
        kind, d = e.get("kind"), e.get("data")
        if kind not in ("status_applied", "status_removed") or not isinstance(d, dict):
            continue
        t = d.get("target")
        key = ((t.get("uuid") or t.get("name")) if isinstance(t, dict) else t, d.get("name") or d.get("status"))
        j = open_.pop(key, None)
        if j is not None and evs[j].get("kind") != kind:
            drop.update((i, j))
        else:
            open_[key] = i
    return [e for i, e in enumerate(evs) if i not in drop] if drop else evs


def _status_key(e: dict) -> tuple | None:
    kind, d = e.get("kind"), e.get("data")
    if kind not in ("status_applied", "status_removed") or not isinstance(d, dict):
        return None
    t = d.get("target")
    return ((t.get("uuid") or t.get("name")) if isinstance(t, dict) else t, d.get("name") or d.get("status"))


class StatusFlap:
    """Across calls: a status that goes on/off on one target FLAP_TOGGLES times within FLAP_WINDOW_S is an aura
    or a light (S026-S027: SCL_MOONSHIELD on Scratch every tick, dozens of EVENTS lines per get_status, which
    _drop_flicker misses when the on and the off land in different calls). Its events are replaced by one
    status_flapping line and muted until it has been quiet for FLAP_MUTE_S."""
    FLAP_TOGGLES = 6
    FLAP_WINDOW_S = 60.0
    FLAP_MUTE_S = 120.0
    # Bystanders: one status going on AND off on many non-party targets (S026/S027 Moonrise: Moonshield on/off on
    # 20 pilgrims, each once per call, so no single target flaps: ~25 lines per wait_for). Muted by status name for
    # non-party targets; the party's own statuses keep the per-target rule (Moonshield on Claude matters).
    CROWD_EACH = 3      # >= this many applied and this many removed ...
    CROWD_TARGETS = 3   # ... on >= this many distinct non-party targets within FLAP_WINDOW_S

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.hist: dict[tuple, list[float]] = {}
        self.muted: dict[tuple, float] = {}  # key -> time of its last event while muted
        self.crowd: dict[str, list[tuple[float, str, Any]]] = {}  # status name -> (t, kind, target)
        self.crowd_muted: dict[str, float] = {}

    def _bystander(self, e: dict, key: tuple, now: float, party: set | None) -> str:
        """'' = not a bystander event (or party unknown); 'mute' = drop it; 'flap' = it just tipped the crowd rule."""
        if party is None or key[0] in party:
            return ""
        name = key[1]
        last = self.crowd_muted.get(name)
        if last is not None and now - last < self.FLAP_MUTE_S:
            self.crowd_muted[name] = now
            return "mute"
        self.crowd_muted.pop(name, None)
        h = [x for x in self.crowd.get(name, []) if now - x[0] < self.FLAP_WINDOW_S] + [(now, e.get("kind"), key[0])]
        self.crowd[name] = h
        on = sum(1 for x in h if x[1] == "status_applied")
        if on >= self.CROWD_EACH and len(h) - on >= self.CROWD_EACH and len({x[2] for x in h}) >= self.CROWD_TARGETS:
            self.crowd_muted[name] = now
            del self.crowd[name]
            return "flap"
        return ""

    def filter(self, evs: list[dict], party: set | None = None) -> list[dict]:
        """party: the party's uuids (their statuses are never crowd-muted); None = per-target rule only."""
        now = self.clock()
        out = []
        for e in evs:
            key = _status_key(e)
            if key is None:
                out.append(e)
                continue
            b = self._bystander(e, key, now, party)
            if b == "mute":
                continue
            if b == "flap":
                out = [x for x in out if not ((k := _status_key(x)) and k[1] == key[1] and k[0] not in party)]
                n = len({k[0] for x in evs if (k := _status_key(x)) and k[1] == key[1] and k[0] not in party})
                out.append({"seq": e.get("seq"), "kind": "status_flapping",
                            "data": {"name": key[1], "note": f"on/off on {max(n, self.CROWD_TARGETS)}+ others nearby "
                                                             "(aura/light): muted for non-party"}})
                continue
            last = self.muted.get(key)
            if last is not None and now - last < self.FLAP_MUTE_S:
                self.muted[key] = now
                continue
            self.muted.pop(key, None)
            h = [t for t in self.hist.get(key, []) if now - t < self.FLAP_WINDOW_S] + [now]
            self.hist[key] = h
            if len(h) >= self.FLAP_TOGGLES:
                self.muted[key] = now
                del self.hist[key]
                d = e["data"]
                out = [x for x in out if _status_key(x) != key]
                out.append({"seq": e.get("seq"), "kind": "status_flapping",
                            "data": {"name": key[1], "target": d.get("target"),
                                     "note": f"on/off {len(h)}x in {int(self.FLAP_WINDOW_S)}s (aura/light): muted"}})
                continue
            out.append(e)
        if len(self.hist) > 500:  # bounded: forget keys quiet for a window
            self.hist = {k: v for k, v in self.hist.items() if now - v[-1] < self.FLAP_WINDOW_S}
        if len(self.crowd) > 200:
            self.crowd = {k: v for k, v in self.crowd.items() if v and now - v[-1][0] < self.FLAP_WINDOW_S}
        return out


FLAP = StatusFlap()


class EventEcho:
    """Across calls: the same event (kind + identical data) again within ECHO_WINDOW_S is an echo, not news.
    The engine fires some twice for one act: a door/Sphincter interact gives two UseFinished (`used`), one of
    them after the interact result returned, so the next result (a route, a look) showed it again; a pickup
    gave `item_added ... count=2` twice (naut golden results 21-22, 128-129, 254-261). Real repeats differ in
    their data (a stack count, another item) or come later than the window."""
    ECHO_KINDS = {"used", "item_added", "item_removed", "opened", "closed", "door_opened"}
    ECHO_WINDOW_S = 5.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last: dict[tuple, float] = {}

    def filter(self, evs: list[dict]) -> list[dict]:
        out = []
        for e in evs:
            if e.get("kind") not in self.ECHO_KINDS:
                out.append(e)
                continue
            key = (e.get("kind"), json.dumps(e.get("data"), sort_keys=True, default=str))
            t = e.get("t")
            now = float(t) / 1000.0 if isinstance(t, (int, float)) else self.clock()
            prev = self.last.get(key)
            self.last[key] = now
            if prev is not None and 0 <= now - prev < self.ECHO_WINDOW_S:
                continue
            out.append(e)
        if len(self.last) > 300:
            newest = max(self.last.values())
            self.last = {k: v for k, v in self.last.items() if newest - v < self.ECHO_WINDOW_S}
        return out


ECHO = EventEcho()


def one_line(text: str, cap: int = 240) -> str:
    """A multi-line step result as one line for an event field (queue_interrupted last=): the step's own line
    plus its FAILED/INTERRUPTED/STOPPED lines, up to its CHANGES/EVENTS/RUNNING/NOW block. An event value
    with raw newlines broke the EVENTS block (naut-r2 golden 129: `last=1. travel_to ..` then a stray
    `STOPPED: leg 1 failed` and a fake CHANGES block holding `done=0 who=e2`)."""
    lines = [ln.strip() for ln in str(text).split("\n")]
    head: list[str] = []
    for ln in lines:
        if ln in ("CHANGES", "EVENTS") or ln.startswith(("RUNNING ", "NOW ", "\u23f1")):
            break
        if ln:
            head.append(ln)
    keep = head[:1] + [ln for ln in head[1:] if re.search(r"FAILED|INTERRUPTED|TIMEOUT|STOPPED|REFUSED", ln)]
    out = " | ".join(keep or head[:1])
    return out if len(out) <= cap else out[: cap - 1] + "…"


def _target_text(t: Any, H: Handles) -> str:
    if isinstance(t, dict) and isinstance(t.get("uuid"), str):
        return f"{t.get('name')} {H.get(t['uuid'], t.get('name'))}".strip()
    return compact(t, H)


STATUS_GROUP_MIN = 3  # the same status change on this many targets in one batch shares one line


def _group_statuses(evs: list[dict], H: Handles) -> list[dict]:
    """The same status gained (or lost) by many targets in one batch shares one line, targets listed (S026: 25
    'status_applied status=SCL_MOONSHIELD name=Moonshield target={...}' lines on arriving at Moonrise, ~2 KB):
    'status_applied name=Moonshield source=... on 9: Adept Dorikel e713, Brawler Weneg e714, ...'."""
    groups: dict[tuple, list[int]] = {}
    for i, e in enumerate(evs):
        d = e.get("data")
        if e.get("kind") in ("status_applied", "status_removed") and isinstance(d, dict) and d.get("target"):
            rest = compact({k: v for k, v in d.items() if k != "target"}, H)
            groups.setdefault((e["kind"], rest), []).append(i)
    big = {ix[0]: (k, ix) for k, ix in groups.items() if len(ix) >= STATUS_GROUP_MIN}
    if not big:
        return evs
    skip = {i for _, ix in big.values() for i in ix[1:]}
    out = []
    for i, e in enumerate(evs):
        if i in skip:
            continue
        if i in big:
            (kind, rest), ix = big[i]
            who = ", ".join(_target_text(evs[j]["data"]["target"], H) for j in ix)
            out.append({"seq": e.get("seq"), "kind": kind, "data": f"{rest} on {len(ix)}: {who}"})
        else:
            out.append(e)
    return out


def render_events(evs: list[dict], H: Handles, limit: int = 25, body: str = "", job: str | None = None,
                  flap: StatusFlap | None = None, party: set | None = None, echo: EventEcho | None = None) -> str:
    """EVENTS section. body: the tool result's own text, so events it already reports are left out
    (job: the job id a wait_for just returned). flap: the cross-call status mute (server passes FLAP).
    echo: the cross-call duplicate filter (server passes ECHO)."""
    m = re.match(r"TURN (e\d+)", body or "")
    own = m.group(1) if m else None
    evs = [e for e in evs if e.get("kind") not in _REDUNDANT_EVENTS and not _already_shown(e, H, body, own, job)]
    evs = echo.filter(evs) if echo is not None else evs
    evs = _drop_flicker(flap.filter(evs, party) if flap is not None else evs)
    evs = _group_statuses(evs, H)
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
        txt = re.sub(r"\s*\n\s*", " | ", txt.strip())  # one event, one line: a raw newline would end the block
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
            lines.append("  " + compact(o, H))
    if objs:
        lines.append("OBJECTS on the turn order (props): "
                     + ", ".join(n + (f" x{c}" if c > 1 else "") for n, c in sorted(objs.items(), key=lambda x: -x[1])))
    return "\n".join(lines)


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


_LEAD_KEEP = ("exit", "door", "ladder", "waypoint")  # each one is a distinct way on: never folded


def _lead_key(l: dict) -> tuple | None:
    """Grouping key of a plain frontier lead (name, kind, unvisited); None when it must stay its own line."""
    if l.get("kind") in _LEAD_KEEP or any(l.get(f) for f in ("locked", "waypoint", "relation", "height_m")):
        return None
    return (l.get("name"), l.get("kind"), bool(l.get("unvisited")))


def render_frontier(res: Any, H: Handles) -> str:
    leads = res.get("leads") if isinstance(res, dict) else None
    if not isinstance(leads, list):
        return compact(res, H)
    out = []
    # Plain lookalike leads (same name, kind and visited state; not an exit/door/ladder/waypoint) are one line:
    # the nearest plus a count. Live 13:50: 3 Burlap Sacks + 4 Wooden Crates were 7 lines one by one.
    groups: dict = {}
    for l in leads:
        k = _lead_key(l)
        if k is not None:
            groups.setdefault(k, []).append(l)
    leads = [l for l in leads if len(groups.get(_lead_key(l), ())) < SAME_MIN or groups[_lead_key(l)][0] is l]
    for l in leads:
        h = H.get(l["uuid"], l.get("name")) if l.get("uuid") else "?"
        bits = [f"{h} {l.get('name', '?')} <{l.get('kind', '?')}> {num(l.get('dist', '?'))}m {l.get('dir', '')}"]
        dy = l.get("dy")
        if isinstance(dy, (int, float)) and abs(dy) >= 1.5:
            bits.append(f"({num(abs(dy))}m {'up' if dy > 0 else 'down'})")
        if l.get("kind") == "ladder" and l.get("height_m") and l.get("leads"):
            bits.append(f"climbs {num(l['height_m'])}m {l['leads']}"
                        + (f" to {pos(l['to'])}" if l.get("to") else "")
                        + (" (unexplored)" if l.get("to_unvisited") else ""))
        if l.get("kind") == "ladder" and l.get("raised_m"):
            bits.append(raised_ladder(l))
        for flag in ("unvisited", "locked", "waypoint"):
            if l.get(flag):
                bits.append(flag)
        if l.get("relation"):
            bits.append(str(l["relation"]))
        g = groups.get(_lead_key(l)) if _lead_key(l) is not None else None
        if g is not None and len(g) >= SAME_MIN:
            far = max((o.get("dist") or 0) for o in g if isinstance(o.get("dist"), (int, float))) if any(
                isinstance(o.get("dist"), (int, float)) for o in g) else None
            dirs = [d for d in dict.fromkeys(str(o.get("dir") or "") for o in g) if d]
            more = f"(+{len(g) - 1} more {l.get('name', '?')}" + (f" up to {num(far)}m" if far is not None else "")
            if len(dirs) > 1:
                more += " " + "/".join(dirs)
            bits.append(more + ")")
        out.append(" ".join(bits))
    if isinstance(res, dict) and res.get("more"):
        out.append(f"(+{res['more']} more leads)")
    if not out:
        out.append("No leads in view. Try another direction, a door, or get_map.")
    ways = render_ways(res.get("ways") if isinstance(res, dict) else None)
    if ways:
        out.append(ways)
    if isinstance(res, dict) and res.get("ways_off"):  # NAV-2: a killed level searches 2 directions per read
        out.append("(WAYS partial: path searches are throttled on this level; read again for more directions)")
    out.extend(render_hazards(res.get("hazards") if isinstance(res, dict) else None, limit=6))
    return "\n".join(out)


def render_ways(ways: Any, far: float = 20.0) -> str:
    """nav.frontier ways -> one line: how far the ground is walkable (direct path) in each compass direction,
    so finding the way on (S010-S015's most common screenshot) needs no picture. "20m+" = open at least that
    far, "8m" = open 8 m but not 20, blocked = not even 8 m (a wall, a cliff, water)."""
    if not isinstance(ways, list) or not ways:
        return ""
    open_, blocked = [], []
    for w in ways:
        if not isinstance(w, dict) or not w.get("dir"):
            continue
        m = w.get("open_m") or 0
        if m <= 0:
            blocked.append(str(w["dir"]))
            continue
        bit = f"{w['dir']} {num(m)}m" + ("+" if m >= far else "")
        dy = w.get("dy")
        if isinstance(dy, (int, float)) and abs(dy) >= 2:
            bit += f" ({num(abs(dy))}m {'up' if dy > 0 else 'down'})"
        open_.append(bit)
    line = "WAYS walkable: " + (", ".join(open_) if open_ else "none")
    if blocked:
        line += " | blocked: " + " ".join(blocked)
    # Gaps a jump crosses where the walk stops (nav: the ground ends, a chasm, ground again within jump range).
    jumps = []
    for w in ways:
        j = w.get("jump") if isinstance(w, dict) else None
        if isinstance(j, dict) and j.get("to"):
            dy = j.get("dy")
            jumps.append(f"{w['dir']} gap {num(j.get('gap_m', '?'))}m from {pos(j.get('from'))} to {pos(j['to'])}"
                         + (f" ({num(abs(dy))}m {'up' if dy > 0 else 'down'})" if isinstance(dy, (int, float)) else ""))
    if jumps:
        line += " | jump: " + "; ".join(jumps) + " (jump_to the far side from the edge)"
    return line


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
        s = f"OBJECTIVE o{m.get('oid') or i} {label}"
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
        if any("oid" in m for m in ms):  # persistent ids (server._set_objectives): o3 = the one shown as o3
            hit = next((m for m in ms if m.get("oid") == i), None)
            if hit is not None:
                return hit
            raise ValueError(f"no objective o{i} now (done or gone); current: "
                             + ", ".join(f"o{m.get('oid')} {objective_label(m)}" for m in ms[:6]))
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
    # one marker under several quests (S059: o2 Gortash [Servant of None], o3 Gortash [Get Gortash's Netherstone])
    p0 = hits[0].get("pos")
    if isinstance(p0, (list, tuple)) and len(p0) >= 3 and all(
            isinstance(h.get("pos"), (list, tuple)) and len(h["pos"]) >= 3
            and sum((a - b) ** 2 for a, b in zip(h["pos"][:3], p0[:3])) <= 4.0 for h in hits):
        return hits[0]
    raise ValueError(f"'{ref}' matches {len(hits)} objectives: "
                     + ", ".join(f"o{h.get('oid') or ms.index(h) + 1} {objective_label(h)} [{h.get('quest') or ''}]"
                                 for h in hits[:5]) + "; use the id")


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


# get_layout: the text grid around a character (Navigation.lua N.layout) with what the party sees on it.
LAYOUT_KEY = ("# wall  . floor  ^ higher floor  v lower floor  _ floor 4-10m below (a drop; jump_to checks the landing)  , ground 14m+ below "
              "(an island/ledge far down; route lists LOWER GROUND)  : darkness  ~ harmful surface  (blank) no floor you can see (chasm, void)")
_LAYOUT_MAX_LEGEND = 12


def _layout_mark(o: dict, kind: str) -> str | None:
    name = str(o.get("name") or "").lower()
    if kind == "char":
        if o.get("dead"):
            return None
        return "E" if o.get("relation") == "hostile" or o.get("hostile") else "n"
    k = o.get("kind")
    if k == "door":
        return "D"
    if k == "ladder":
        return "H"
    if "lever" in name or "button" in name:
        return "L"
    if k == "container":
        return "C"
    return None


MOSAIC_START_M = 6.0  # the picture's nearest cell this close to you = you stand at its start


def mosaic_path(mo: dict, here: Any, goal: Any = None) -> list[tuple[float, float, str]] | None:
    """The way along the mosaic's lines (S036: 15 min of 1-3 m checked steps across the Faith-Leap): breadth-first over
    the picture's '#'/'O' cells (4-neighbour; a diagonal only where the picture draws one), from the cell nearest `here`
    to the cell nearest `goal` (x,z), or, without one, to the line cell farthest from the start (the far side). Only
    what the picture shows: cell centres, 1 m. Returns the corners [(x, z, '#'|'O')], start first, or None."""
    rows, x1, z2 = mo.get("rows") or [], mo.get("x1"), mo.get("z2")
    if not rows or x1 is None or z2 is None or not isinstance(here, (list, tuple)) or len(here) < 3:
        return None
    cells = {(r, c): ch for r, row in enumerate(rows) for c, ch in enumerate(row) if ch in "#O"}
    if not cells:
        return None

    def world(rc):
        return (x1 + rc[1], z2 - rc[0])

    def nearest(x, z):
        return min(cells, key=lambda rc: (world(rc)[0] - x) ** 2 + (world(rc)[1] - z) ** 2)

    start = nearest(here[0], here[2])
    if math.dist(world(start), (here[0], here[2])) > MOSAIC_START_M:
        return None
    prev, order, queue = {start: None}, [start], [start]
    while queue:
        nxt = []
        for r, c in queue:
            steps = [(r + dr, c + dc) for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0))]
            steps += [(r + dr, c + dc) for dr, dc in ((1, 1), (1, -1), (-1, 1), (-1, -1))
                      if (r + dr, c) not in cells and (r, c + dc) not in cells]
            for n in steps:
                if n in cells and n not in prev:
                    prev[n] = (r, c)
                    order.append(n)
                    nxt.append(n)
        queue = nxt
    if goal is not None:
        end = min(order, key=lambda rc: (world(rc)[0] - goal[0]) ** 2 + (world(rc)[1] - goal[-1]) ** 2)
    else:
        end = max(order, key=lambda rc: math.dist(world(rc), world(start)))
    way = []
    while end is not None:
        way.append(end)
        end = prev[end]
    way.reverse()
    corners = [way[0]]
    for i in range(1, len(way) - 1):
        a, b, c = way[i - 1], way[i], way[i + 1]
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]) or cells[b] != cells[a]:
            corners.append(b)
    if len(way) > 1:
        corners.append(way[-1])
    return [(*world(rc), cells[rc]) for rc in corners]


def render_mosaic_path(mo: dict, here: Any, goal: Any = None) -> list[str]:
    way = mosaic_path(mo, here, goal)
    if not way or len(way) < 2:
        return []
    y = mo.get("y")
    yo = (lambda k: y + 0.9 if k == "O" else y) if isinstance(y, (int, float)) else (lambda k: None)
    # off the line's start (> 1.5 m): walk onto it first, never a diagonal across the chasm to the 2nd corner
    first = 0 if math.dist(way[0][:2], (here[0], here[2])) > 1.5 else 1
    legs = [{"tool": "move_to", "x": x, **({"y": round(yo(k), 1)} if yo(k) is not None else {}), "z": z}
            for x, z, k in way[first:]]
    m = sum(math.dist(way[i][:2], way[i + 1][:2]) for i in range(len(way) - 1))
    to = "the cell nearest your goal" if goal is not None else "its far end"
    return [f"MOSAIC PATH (the picture's lines from your end to {to}, {len(legs)} straight legs, {num(round(m))}m;"
            " O = a statue platform, 1 m up): " + " → ".join(f"({num(x)},{num(z)}){'O' if k == 'O' else ''}"
                                                           for x, z, k in way),
            "  walk it: do_actions " + json.dumps(legs, separators=(",", ":"))
            + "  (a step off a line drops you; the trial puts you back at its start)"]


def render_layout(lay: dict, H: Handles, nearby: dict | None = None, party: list | None = None,
                  me: str | None = None, goal: Any = None) -> str:
    rows = [list(r) for r in (lay.get("rows") or [])]
    if not rows:
        return "no layout (no position?)"
    n, cell = int(lay.get("n") or len(rows) // 2), float(lay.get("cell") or 1.5)
    cx, cz = (lay.get("center") or [0, 0, 0])[0], (lay.get("center") or [0, 0, 0])[2]
    legend: list[str] = []

    def place(p: Any, mark: str) -> bool:
        if not (isinstance(p, (list, tuple)) and len(p) >= 3):
            return False
        c, r = n + round((p[0] - cx) / cell), n - round((p[2] - cz) / cell)
        if 0 <= r < len(rows) and 0 <= c < len(rows[r]) and (r, c) != (n, n):
            rows[r][c] = mark
            return True
        return False

    others = [m for m in (party or []) if m.get("uuid") != me]
    for i, m in enumerate(others[:9], 1):
        if place(m.get("pos"), str(i)):
            legend.append(f"{i} {H.get(m['uuid'], m.get('name'))} {m.get('name', '')}".rstrip())
    for kind, objs in (("char", (nearby or {}).get("characters") or []), ("item", (nearby or {}).get("items") or [])):
        for o in objs:
            mark = _layout_mark(o, kind)
            if mark and place(o.get("pos"), mark) and len(legend) < _LAYOUT_MAX_LEGEND and o.get("uuid"):
                extra = " locked" if o.get("locked") else ""
                legend.append(f"{mark} {H.get(o['uuid'], o.get('name'))} {o.get('name', '?')}{extra}")
    rows[n][n] = "@"
    out = [f"LAYOUT {num(n * cell)}m around you (@), N up, 1 char = {num(cell)}m; {LAYOUT_KEY}"]
    out += ["|" + "".join(r) + "|" for r in rows]
    if legend:
        out.append("ON IT: " + " | ".join(legend))
    mo = lay.get("mosaic")
    if isinstance(mo, dict) and mo.get("rows"):
        # S036 19:52: counted from "the right edge", the agent walked 1.4 m beside a line and fell twice; lined up
        # with the room (what a player does with the statue platforms in view) the picture's columns are x, rows z
        anchor = (f"; column 1 = x {num(mo['x1'])}, row 1 = z {num(mo['z2'])}, so a # at column c, row r is about"
                  f" x {num(mo['x1'])}+c-1, z {num(mo['z2'])}-r+1"
                  if mo.get("x1") is not None and mo.get("z2") is not None else "")
        out.append("MOSAIC on the floor of this room (the trial's picture: its safe lines from above, N up, 1 char = "
                   f"1m of the chasm; # line, O statue platform{anchor})")
        out += ["|" + r for r in mo["rows"]]
        out += render_mosaic_path(mo, lay.get("center"), goal)
    return "\n".join(out)
