"""Navigation renderers: frontier leads, ways, paths, objectives, hazards and the ASCII layout.
render.py re-exports these names."""

from __future__ import annotations

import re
from typing import Any

from .render_base import SAME_MIN, Handles, compact, num, pos, raised_ladder

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


def opened_note(res: Any, H: Handles) -> str:
    """A no-path reply soon after a door/wall the party can see opened or closed (nav `opened`, V13: S071's Wyrmway
    Stone Wall): while it may still be moving the answer can change, so try again; later the answer was searched
    after it, so it is current. '' when there is none."""
    o = res.get("opened") if isinstance(res, dict) else None
    if not isinstance(o, dict) or not o.get("uuid"):
        return ""
    what = f"{H.get(o['uuid'], o.get('name'))} {o.get('name') or ''}".rstrip()
    how = "closed" if o.get("how") == "closed" else "opened"
    ago = f"{num(o.get('ago_s', 0))}s ago"
    if o.get("settling") and how == "opened":
        return f"{what} opened {ago} and may still be moving: this passage just opened, try again in a few seconds"
    return f"(searched after {what} {how} {ago}: up to date)"


def render_path(res: Any, H: Handles) -> str:
    """nav.path -> one line. Drops the route points (dozens of coordinates) and says plainly when there is
    no path; error_cause is the engine's opaque code, so it is left out."""
    if not isinstance(res, dict):
        return compact(res, H)
    if res.get("reachable") is False:
        note = opened_note(res, H)
        return (f"UNREACHABLE: no walkable path ({num(res.get('straight_m', '?'))}m straight line): "
                "it may need a ladder, a jump (jump_to), a door, or another route" + (f"\n{note}" if note else ""))
    if res.get("unknown_goal"):
        return (f"not seen or walked yet: {num(res.get('straight_m', '?'))}m {res.get('dir') or ''} straight line only "
                "(path_preview checks ground the party has seen or walked)").replace("  ", " ")
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
    ids: list[list[str]] = []  # per line: the ids it stands for
    for i, m in enumerate(markers, 1):
        if not isinstance(m, dict):
            continue
        here = m.get("here") and m.get("dist") is not None
        if here_only and not here:
            elsewhere += 1
            continue
        label = objective_label(m)
        s = label
        if here:
            s += f" {num(m['dist'])}m {m.get('dir') or ''}".rstrip()
        else:
            s += " (another area)"
        quest = m.get("quest")
        if quest and quest != label:
            s += f" [{quest}]"
        # two markers of one target (live 20261016: o1, o2 both "Zariel's Asset 1508.9m N"): one line, both ids
        if s in lines:
            ids[lines.index(s)].append(f"o{m.get('oid') or i}")
            continue
        if limit is not None and len(lines) >= limit:
            elsewhere += 1
            continue
        lines.append(s)
        ids.append([f"o{m.get('oid') or i}"])
    lines = [f"OBJECTIVE {'/'.join(d)} {s}" for d, s in zip(ids, lines)]
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
              "(an island/ledge far down; route lists LOWER GROUND)  : darkness  ~ harmful surface  (blank) no floor you can see (chasm, void)  "
              "? not seen or walked yet")
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


def render_layout(lay: dict, H: Handles, nearby: dict | None = None, party: list | None = None,
                  me: str | None = None) -> str:
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
    return "\n".join(out)
