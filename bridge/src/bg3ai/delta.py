"""Delta observations: "what changed since my last look", in a few short lines.

The bridge keeps one `Tracker` per agent (one MCP server process = one agent session; `TRACKER` below).
Every observation (`look`, `get_status`, `get_map`, and action results that append `changes_text`) turns
the mod's payloads into a flat `snapshot` and diffs it against the previous one. Only the parts that were
fetched are compared, so a status-only call doesn't report nearby entities as gone, and whatever wasn't
looked at stays in the baseline until it is.

Line formats (stable order: mode, where, party, gold, objectives, quests, entities, items, UI):

    MODE exploring -> COMBAT round 1 turn=e5
    WHERE Nautiloid · Ship -> Nautiloid · Helm
    e1 Claude moved 12m NW, HP 12->7/12, +Burning -Bless, DOWNED
    ACTIVE e2 Shadowheart
    GOLD 40->65
    OBJECTIVE + o1 Escape the ship 16m W | OBJECTIVE - Find the helm
    QUEST + Rescue the Trapped Woman: <step> | QUEST Escape the Nautiloid: <new step> | QUEST done <title>
    + e7 Imp hostile HP 9/9 8m NW        (appeared)
    ~ e7 Imp HP 9->3, +Burning, DEAD      (changed)
    GONE e9 Chest, e10 Door               (left view or range)
    ITEM + e30 Healing Potion x2 (Claude) | ITEM - e31 Key | ITEM e32 Arrow x10->7
    <dialogue/roll/popup lines, only when they changed>

`changes_text(game, H)` is the hook for action tools (do_actions, wait_for): it fetches status +
nearby + objective markers in ONE server eval (+ one client eval for ui.state), diffs, and returns
"CHANGES\n  ..." or "" when nothing changed.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from . import render
from .render import Handles

# What an action result gets appended by default (cheap ops only; inventory/journal wait for `look`).
ACTION_PARTS = ("status", "nearby", "markers", "ui")
LOOK_PARTS = ("status", "nearby", "markers", "inventory", "journal", "ui", "hazards")
STATUS_PARTS = ("status", "markers", "ui")

MOVE_MIN_M = 1.0  # party moves shorter than this aren't reported
NPC_MOVE_MIN_M = 3.0  # hostile / in-combat characters: report moves at least this long
MAX_APPEARED = 12  # appeared-entity lines before "(+n more)"
MAX_APPEARED_CHARS = 16  # appeared characters are all listed up to this many, even past MAX_APPEARED
MIN_APPEARED_ITEMS = 4   # leads (doors, containers, ...) listed even when characters filled the budget
MAX_ITEM_LINES = 10
BIG_CHANGE = 30  # more diff lines than this -> the caller should show the full view instead

_DIRS = ("E", "NE", "N", "NW", "W", "SW", "S", "SE")


def direction(a: Any, b: Any) -> str:
    """Compass direction a->b, same convention as Navigation.lua N.direction (+X east, +Z north)."""
    dx, dz = b[0] - a[0], b[2] - a[2]
    if abs(dx) < 0.5 and abs(dz) < 0.5:
        return ""
    i = int(math.floor(math.atan2(dz, dx) / (2 * math.pi) * 8 + 0.5)) % 8
    return _DIRS[i]


def _dist(a: Any, b: Any) -> float:
    return math.dist(a[:3], b[:3])


def _is_pos(p: Any) -> bool:
    return isinstance(p, (list, tuple)) and len(p) >= 3 and all(isinstance(x, (int, float)) for x in p[:3])


def _status_names(c: dict) -> tuple:
    out = []
    for s in c.get("statuses") or []:
        n = (s.get("name") or s.get("id")) if isinstance(s, dict) else s
        if n:
            out.append(str(n))
    return tuple(sorted(set(out)))


def _hp_tuple(c: dict) -> tuple | None:
    if c.get("hp") is not None and c.get("max_hp"):
        return ("abs", c["hp"], c["max_hp"], c.get("temp_hp") or 0)
    if c.get("hp_pct") is not None:
        return ("pct", c["hp_pct"])
    return None


def _hp_text(h: tuple | None) -> str:
    if not h:
        return ""
    if h[0] == "abs":
        return f"{render.num(h[1])}/{render.num(h[2])}" + (f"+{render.num(h[3])}" if h[3] else "")
    return f"{render.num(h[1])}%"


def _hp_change(old: tuple | None, new: tuple | None) -> str:
    if old == new or new is None:
        return ""
    if old is None or old[0] != new[0]:
        return f"HP {_hp_text(new)}"
    if new[0] == "abs":
        s = f"HP {render.num(old[1])}->{render.num(new[1])}/{render.num(new[2])}"
        if new[3] != old[3]:
            s += f" (temp {render.num(old[3])}->{render.num(new[3])})"
        return s
    return f"HP {render.num(old[1])}%->{render.num(new[1])}%"


def _turn(s: dict, H: Handles) -> str:
    """'round 2 turn=e5 Imp' in combat, else ''."""
    if not s.get("in_combat"):
        return ""
    c = s.get("combat") or {}
    cur = c.get("current")
    if isinstance(cur, str) and render.UUID_RE.search(cur):
        h = H.get(cur)
        cur = f"{h} {H.names[h]}" if H.names.get(h) else h
    return f"round {c.get('round', '?')} turn={cur or '?'}"


def _mode(s: dict, ui: dict | None, H: Handles) -> str:
    mode = []
    if s.get("in_combat"):
        mode.append("COMBAT")
    if s.get("turn_based"):
        mode.append("turn-based")
    if s.get("in_dialogue"):
        mode.append("DIALOGUE")
    if ui and ui.get("screen") not in (None, "game"):
        mode.append(f"screen={ui['screen']}")
    return " ".join(mode) or "exploring"


def _char_rec(c: dict) -> dict:
    return {"name": c.get("name", "?"), "pos": c.get("pos") if _is_pos(c.get("pos")) else None,
            "hp": _hp_tuple(c), "st": _status_names(c), "rel": c.get("relation"),
            "flags": tuple(f for f in ("dead", "downed", "in_combat") if c.get(f)), "dist": c.get("dist")}


_ITEM_FLAGS = ("locked", "opened", "trap", "owned")


def _item_rec(it: dict) -> dict:
    return {"name": it.get("name", "?"), "kind": it.get("kind"), "dist": it.get("dist"),
            "flags": tuple(f for f in _ITEM_FLAGS if it.get(f)), "story": bool(it.get("story")),
            "count": it.get("count")}


def snapshot(H: Handles, status: dict | None = None, ui: dict | None = None, nearby: dict | None = None,
             markers: list | None = None, inventory: dict | None = None, journal: list | None = None,
             map_: dict | None = None) -> dict:
    """Flatten mod payloads into comparable plain data. A part that is None was not fetched."""
    snap: dict[str, Any] = {}
    if status is not None:
        sub = status.get("subregion") or (ui or {}).get("region")
        snap["where"] = " · ".join(str(x) for x in (sub, status.get("region")) if x) or "?"
        snap["mode"] = _mode(status, ui, H)
        snap["turn"] = _turn(status, H)
        snap["gold"] = status.get("gold")
        snap["active"] = status.get("active")
        snap["party"] = {c["uuid"]: _char_rec(c) for c in status.get("party") or [] if c.get("uuid")}
        snap["party_order"] = [c["uuid"] for c in status.get("party") or [] if c.get("uuid")]
    if ui is not None:
        snap["ui"] = tuple(render.render_ui(ui, H, brief=True))
    if nearby is not None:
        ents = {}
        for c in nearby.get("characters") or []:
            if c.get("uuid"):
                ents[c["uuid"]] = dict(_char_rec(c), kind="char")
        for it in nearby.get("items") or []:
            if it.get("uuid") and (it.get("kind") != "object" or it.get("locked") or it.get("trap")):
                ents[it["uuid"]] = dict(_item_rec(it), kind=it.get("kind") or "item")
        snap["ents"] = ents
    if markers is not None:
        objs = {}
        for i, m in enumerate(markers, 1):
            if isinstance(m, dict):
                key = (render.objective_label(m), str(m.get("quest") or ""))
                objs[key] = {"i": i, "here": bool(m.get("here") and m.get("dist") is not None),
                             "dist": m.get("dist"), "dir": m.get("dir")}
        snap["objs"] = objs
    if inventory is not None and isinstance(inventory, dict):
        inv = {}
        for it in inventory.get("items") or []:
            if it.get("uuid"):
                inv[it["uuid"]] = {"name": it.get("name", "?"), "count": it.get("count") or 1,
                                   "owner": it.get("owner"), "eq": it.get("equipped_slot")}
        snap["inv"] = inv
        if inventory.get("gold") is not None and "gold" not in snap:
            snap["gold"] = inventory.get("gold")
    if journal is not None and isinstance(journal, list):
        snap["quests"] = {str(q.get("title")): {"steps": [str(s) for s in q.get("steps") or []],
                                                "done": bool(q.get("done"))}
                          for q in journal if isinstance(q, dict)}
    if map_ is not None and isinstance(map_, dict):
        snap["waypoints"] = tuple(sorted(str(w.get("name") or w.get("id")) for w in map_.get("waypoints") or []
                                         if isinstance(w, dict)))
    return snap


def _ref(H: Handles, uuid: str, name: str) -> str:
    return f"{H.get(uuid, name)} {name}"


def _party_lines(old: dict, new: dict, H: Handles) -> list[str]:
    out = []
    op, np_ = old.get("party") or {}, new.get("party") or {}
    moves = {}
    for u in new.get("party_order") or []:
        o, n = op.get(u), np_[u]
        if o and o["pos"] and n["pos"] and _dist(o["pos"], n["pos"]) >= MOVE_MIN_M:
            moves[u] = f"moved {render.num(round(_dist(o['pos'], n['pos'])))}m {direction(o['pos'], n['pos'])}".rstrip()
    together = len(np_) > 1 and len(moves) == len(np_) and len(set(moves.values())) == 1
    if together:  # the usual case: everyone followed the leader
        out.append(f"PARTY {next(iter(moves.values()))}")
    for u in new.get("party_order") or []:
        n = np_[u]
        o = op.get(u)
        if o is None:
            out.append(f"PARTY + {_ref(H, u, n['name'])} HP {_hp_text(n['hp'])}".rstrip())
            continue
        bits = [moves[u]] if u in moves and not together else []
        hp = _hp_change(o["hp"], n["hp"])
        if hp:
            bits.append(hp)
        bits += _status_bits(o["st"], n["st"])
        bits += _flag_bits(o["flags"], n["flags"], ("dead", "downed"))
        if bits:
            out.append(f"{_ref(H, u, n['name'])} " + ", ".join(bits))
    for u, o in op.items():
        if u not in np_:
            out.append(f"PARTY - {_ref(H, u, o['name'])}")
    return out


def _status_bits(old: tuple, new: tuple) -> list[str]:
    gained = [s for s in new if s not in old]
    lost = [s for s in old if s not in new]
    if not (gained or lost):
        return []
    return [" ".join([f"+{s}" for s in gained] + [f"-{s}" for s in lost])]


def _flag_bits(old: tuple, new: tuple, flags: tuple) -> list[str]:
    out = []
    for f in flags:
        if f in new and f not in old:
            out.append(f.upper())
        elif f in old and f not in new:
            out.append(f"no longer {f}")
    return out


def _appeared(H: Handles, u: str, n: dict) -> str:
    bits = [f"+ {_ref(H, u, n['name'])}"]
    if n["kind"] == "char":
        if n.get("rel"):
            bits.append(str(n["rel"]))
        if n.get("hp"):
            bits.append(f"HP {_hp_text(n['hp'])}")
        bits += [f for f in n["flags"] if f != "in_combat"]
        if n["st"]:
            bits.append("[" + ",".join(n["st"]) + "]")
    else:
        bits.append(f"<{n['kind']}>")
        bits += list(n["flags"])
        if n.get("story"):
            bits.append("story")
    if n.get("dist") is not None:
        bits.append(f"{render.num(n['dist'])}m")
    return " ".join(bits)


def _ent_lines(old: dict, new: dict, H: Handles) -> list[str]:
    oe, ne = old.get("ents") or {}, new.get("ents") or {}
    appeared, changed, gone = [], [], []

    def order(u: str) -> tuple:  # characters first, then by distance
        n = ne.get(u) or oe.get(u) or {}
        return (n.get("kind") != "char", n.get("dist") if n.get("dist") is not None else 999, H.get(u))

    new_chars, new_items = [], []
    for u in sorted(ne, key=order):
        n, o = ne[u], oe.get(u)
        if o is None:
            (new_chars if n["kind"] == "char" else new_items).append(u)
            continue
        bits = []
        if n["kind"] == "char":
            if n.get("rel") != o.get("rel") and n.get("rel"):
                bits.append(f"now {n['rel']} (was {o.get('rel') or '?'})")
            hp = _hp_change(o["hp"], n["hp"])
            if hp and "dead" not in n["flags"]:
                bits.append(hp)
            bits += _status_bits(o["st"], n["st"])
            bits += _flag_bits(o["flags"], n["flags"], ("dead", "downed", "in_combat"))
            if (o["pos"] and n["pos"] and "dead" not in n["flags"]
                    and (n.get("rel") == "hostile" or "in_combat" in n["flags"])
                    and _dist(o["pos"], n["pos"]) >= NPC_MOVE_MIN_M):
                bits.append(f"moved {render.num(round(_dist(o['pos'], n['pos']), 1))}m, now {render.num(n['dist'])}m")
        else:
            for f in _ITEM_FLAGS:
                if f in n["flags"] and f not in o["flags"]:
                    bits.append(f)
                elif f in o["flags"] and f not in n["flags"]:
                    bits.append({"locked": "unlocked", "opened": "closed", "trap": "trap gone"}.get(f, f"not {f}"))
            if n.get("count") != o.get("count") and n.get("count") is not None:
                bits.append(f"x{o.get('count') or 1}->{n['count']}")
        if bits:
            changed.append(f"~ {_ref(H, u, n['name'])} " + ", ".join(bits))
    for u in sorted((u for u in oe if u not in ne), key=order):
        gone.append(_ref(H, u, oe[u]["name"]))
    # Arrival in a new place: every character (who's here is what the next decision needs; S009 followed
    # 5 of 26 arrivals with get_nearby(kinds=characters) because items had filled the list), then the
    # leads (doors, containers, ladders, story items) up to the budget, and the rest as counts by kind.
    appeared = [_appeared(H, u, ne[u]) for u in new_chars[:MAX_APPEARED_CHARS]]
    if len(new_chars) > MAX_APPEARED_CHARS:
        appeared.append(f"(+{len(new_chars) - MAX_APPEARED_CHARS} more characters; get_nearby)")
    room = max(MAX_APPEARED - len(appeared), MIN_APPEARED_ITEMS)
    if len(new_items) > room:
        new_items.sort(key=lambda u: (0 if ne[u].get("story") else render._KIND_RANK.get(str(ne[u]["kind"]), 4),
                                      ne[u].get("dist") if ne[u].get("dist") is not None else 999, H.get(u)))
    appeared += [_appeared(H, u, ne[u]) for u in new_items[:room]]
    if len(new_items) > room:
        kinds: dict[str, int] = {}
        for u in new_items[room:]:
            kinds[ne[u]["kind"]] = kinds.get(ne[u]["kind"], 0) + 1
        appeared.append(f"(+{len(new_items) - room} more: "
                        + ", ".join(f"{k} {kinds[k]}" for k in sorted(kinds, key=lambda k: -kinds[k]))
                        + "; get_nearby / get_frontier)")
    out = []
    out += appeared + changed
    if gone:
        out.append("GONE " + ", ".join(gone[:8]) + (f" (+{len(gone) - 8})" if len(gone) > 8 else ""))
    return out


def _inv_lines(old: dict, new: dict, H: Handles) -> list[str]:
    oi, ni = old.get("inv") or {}, new.get("inv") or {}
    out = []

    def owner(r: dict) -> str:
        o = r.get("owner")
        return f" ({H.names.get(H.get(o)) or H.get(o)})" if isinstance(o, str) and o else ""

    for u, n in ni.items():
        o = oi.get(u)
        cnt = f" x{n['count']}" if n["count"] != 1 else ""
        if o is None:
            out.append(f"ITEM + {_ref(H, u, n['name'])}{cnt}{owner(n)}")
        else:
            if o["count"] != n["count"]:
                out.append(f"ITEM {_ref(H, u, n['name'])} x{o['count']}->{n['count']}")
            if o.get("owner") != n.get("owner"):
                out.append(f"ITEM {_ref(H, u, n['name'])} moved to{owner(n) or ' ?'}")
            if o.get("eq") != n.get("eq"):
                out.append(f"ITEM {_ref(H, u, n['name'])} " + (f"equipped {n['eq']}" if n.get("eq") else "unequipped"))
    for u, o in oi.items():
        if u not in ni:
            out.append(f"ITEM - {_ref(H, u, o['name'])}")
    if len(out) > MAX_ITEM_LINES:
        out = out[:MAX_ITEM_LINES] + [f"(+{len(out) - MAX_ITEM_LINES} more item changes; get_inventory)"]
    return out


def _quest_lines(old: dict, new: dict, H: Handles) -> list[str]:
    oq, nq = old.get("quests") or {}, new.get("quests") or {}
    out = []
    for t, n in nq.items():
        o = oq.get(t)
        last = H.scrub(n["steps"][-1]) if n["steps"] else ""
        if o is None:
            out.append(f"QUEST + {t}" + (f": {last}" if last else ""))
            continue
        if n["done"] and not o["done"]:
            out.append(f"QUEST done {t}")
        elif len(n["steps"]) > len(o["steps"]) or (n["steps"] and o["steps"] and n["steps"][-1] != o["steps"][-1]):
            out.append(f"QUEST {t}: {last}")
    for t in oq:
        if t not in nq:
            out.append(f"QUEST - {t}")
    return out


def _obj_lines(old: dict, new: dict) -> list[str]:
    oo, no = old.get("objs") or {}, new.get("objs") or {}
    out = []
    for k, n in no.items():
        if k not in oo or oo[k]["here"] != n["here"]:
            where = f"{render.num(n['dist'])}m {n.get('dir') or ''}".rstrip() if n["here"] else "(another area)"
            quest = f" [{k[1]}]" if k[1] and k[1] != k[0] else ""
            out.append(f"OBJECTIVE {'+ ' if k not in oo else ''}o{n['i']} {k[0]} {where}{quest}")
    for k in oo:
        if k not in no:
            out.append(f"OBJECTIVE - {k[0]}")
    return out


def diff(old: dict, new: dict, H: Handles) -> list[str]:
    """Lines describing old -> new, comparing only parts present in `new`."""
    out: list[str] = []
    if "mode" in new and "mode" in old and old["mode"] != new["mode"]:
        out.append(f"MODE {old['mode']} -> {new['mode']}" + (f" ({new['turn']})" if new.get("turn") else ""))
    elif new.get("turn") and new["turn"] != old.get("turn"):
        out.append(f"TURN {new['turn']}")
    if "where" in new and "where" in old and old["where"] != new["where"]:
        out.append(f"WHERE {old['where']} -> {new['where']}")
    if "party" in new and "party" in old:
        out += _party_lines(old, new, H)
    if "active" in new and "active" in old and new["active"] != old["active"] and new["active"]:
        a = new["active"]
        out.append(f"ACTIVE {_ref(H, a, (new.get('party') or {}).get(a, {}).get('name', '?'))}")
    if "gold" in new and "gold" in old and new["gold"] != old["gold"] and new["gold"] is not None:
        out.append(f"GOLD {old['gold']}->{new['gold']}")
    if "objs" in new and "objs" in old:
        out += _obj_lines(old, new)
    if "quests" in new and "quests" in old:
        out += _quest_lines(old, new, H)
    if "waypoints" in new and "waypoints" in old:
        out += [f"WAYPOINT + {w}" for w in new["waypoints"] if w not in old["waypoints"]]
    if "ents" in new and "ents" in old:
        out += _ent_lines(old, new, H)
    if "inv" in new and "inv" in old:
        out += _inv_lines(old, new, H)
    if "ui" in new and new["ui"] and new["ui"] != old.get("ui"):
        out += list(new["ui"])
    return out


class Tracker:
    """The agent's last-seen state. `observe` diffs a fresh snapshot against it and merges it in."""

    def __init__(self, H: Handles | None = None) -> None:
        self.H = H or Handles()
        self.last: dict = {}
        self.at = 0.0

    def bind(self, H: Handles) -> "Tracker":
        """Use the server's handle table (so e12 means the same thing everywhere)."""
        self.H = H
        return self

    def reset(self) -> None:
        self.last, self.at = {}, 0.0

    def has(self, *parts: str) -> bool:
        return all(p in self.last for p in parts)

    def observe(self, snap: dict) -> list[str]:
        lines = diff(self.last, snap, self.H) if self.last else []
        self.last = {**self.last, **snap}
        self.at = time.monotonic()
        return lines


TRACKER = Tracker()


# ---------------------------------------------------------------------------
# fetching (one server eval for all server ops)
# ---------------------------------------------------------------------------

_OPS = {"status": ("state.status", {}), "nearby": ("state.nearby", {"radius": 25}),
        "markers": ("nav.markers", {}), "inventory": ("state.inventory", {}), "journal": ("state.journal", {}),
        "map": ("state.map", {}), "hazards": ("nav.hazards", {})}


def _bracket(payload: str) -> str:
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    return f"[{eq}[{payload}]{eq}]"


def multi_rpc(game: Any, calls: list[tuple[str, dict]], context: str = "server", events: bool = False) -> list[Any]:
    """Run several mod ops in ONE LuaDebug eval (one game tick instead of n). Each slot is the op's
    result, or None if it failed. events=True also drains the context's events in that eval (Game.multi).
    Falls back to one try_rpc per op for fakes without multi()/eval()."""
    if hasattr(game, "multi"):
        return game.multi(calls, context, events=events)
    if not hasattr(game, "eval") or len(calls) == 1:
        return [game.try_rpc(op, args, context) for op, args in calls]
    parts = [f"R({_bracket(json.dumps({'op': op, 'args': args}, separators=(',', ':')))})" for op, args in calls]
    code = "local R=Mods.BG3AI.BG3AI.rpc return '['.." + "..','..".join(parts) + "..']'"
    t0 = time.time()
    try:
        raw = game.eval(code, context=context)
    finally:
        if getattr(game, "metrics", None) is not None:
            game.metrics.rpc_done("+".join(op for op, _ in calls), t0, time.time())
    if raw in ("", "nil"):
        from .game import NotReady
        raise NotReady(f"BG3AI mod is not loaded in the {context} context")
    replies = json.loads(raw)
    return [r.get("result") if isinstance(r, dict) and r.get("ok") else None for r in replies]


def fetch(game: Any, parts: tuple = ACTION_PARTS, events: bool = False) -> dict:
    """{part: payload} for the requested parts (ui from the client context, the rest in one server eval).
    With a real Game both evals run in parallel (one game tick); events=True also drains both contexts'
    events in those evals, so the caller's EVENTS section costs no extra RPC."""
    server = [p for p in parts if p in _OPS]
    if hasattr(game, "read"):
        sres, cres = game.read([_OPS[p] for p in server], [("ui.state", {})] if "ui" in parts else [], events=events)
        got = dict(zip(server, sres))
        if "ui" in parts:
            got["ui"] = cres[0] if cres else None
    else:
        got = dict(zip(server, multi_rpc(game, [_OPS[p] for p in server]))) if server else {}
        if "ui" in parts:
            got["ui"] = game.try_rpc("ui.state", context="client")
    if "markers" in got:
        m = got["markers"]
        got["markers"] = m.get("markers") if isinstance(m, dict) and isinstance(m.get("markers"), list) else None
    if "hazards" in got:
        h = got["hazards"]
        got["hazards"] = h.get("hazards") if isinstance(h, dict) and isinstance(h.get("hazards"), list) else None
    return got


def snapshot_of(H: Handles, got: dict) -> dict:
    """snapshot() from fetch() output; parts that failed to fetch (None) are left out, so not compared."""
    return snapshot(H, status=got.get("status"), ui=got.get("ui"), nearby=got.get("nearby"),
                    markers=got.get("markers"), inventory=got.get("inventory"), journal=got.get("journal"),
                    map_=got.get("map"))


def changes_text(game: Any, H: Handles, parts: tuple = ACTION_PARTS, tracker: Tracker | None = None) -> str:
    """For action tools: "CHANGES\\n  <lines>" since the agent's last look, or "" if nothing changed.
    Never raises (an action result must not fail because the observation did)."""
    tr = tracker or TRACKER.bind(H)
    try:
        lines = tr.observe(snapshot_of(H, fetch(game, parts)))
    except Exception:  # noqa: BLE001 - the game may be mid-load; the action result still stands
        return ""
    if not lines:
        return ""
    return "CHANGES\n" + "\n".join("  " + x for x in lines)
