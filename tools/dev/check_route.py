"""Live checks for vis's nav.route and journal cache. Read-only: safe between agent sessions (and, being light,
even during one: no moves, no reloads, no UI). One command for the orchestrator:

    uv run --project bridge python tools/dev/check_route.py                 # goal = objective o1, else the Helm
    uv run --project bridge python tools/dev/check_route.py --pos X Y Z     # any goal
    uv run --project bridge python tools/dev/check_route.py --json          # also dump the raw nav.route reply

Checks (PASS / WARN / FAIL / INFO lines; exit 1 on any FAIL):
  1 route    nav.route answers (FAIL "unknown op" = the mod was not hot-reloaded yet) for the goal; reachable or
             closest+connectors, ms (< 50 ms PASS, < 150 WARN) and the ms per step (phases). No floors line: nav.route
             stopped reporting levels_here in 604264c (fairness: floors only from seen ground). Printed through route.render_route,
             i.e. what the agent's `route` tool shows.
  2 ladder   For each ladder in view (nav.route connectors=true): the floors at the ladder's own column relative
             to its pivot, and on rings round it (Guard.LADDER_TOP_RADII: the top landing is often off the column,
             behind the ladder). A floor near 0 and near +height (column or ring) = pivot at the FOOT (what
             N.connectors assumes: PASS); a floor near -height under the pivot, none at 0 there and none at
             +height round it = pivot at the TOP (FAIL: flip `leads`/`far_dy`); floors at 0 and -height in the
             column and no landing found above = AMBIGUOUS (WARN: a foot on a floor over a lower one, S085 Guildhall
             19353fd1, reads the same as a top); near +-height/2 = MIDDLE (FAIL: halve). Also leads=up must mean
             far_dy > 0.
  3 links    A raw path (BeginPathfindingImmediate + FindPath) to the goal, else the closest spot, else the
             nearest ladder: node count, the fields a node has, how many carry a valid Portal handle, and the
             steep steps (|dy| >= 1 m, steeper than 1:1). Zero portals on a path that climbs = links come only
             from the steep-step rule (WARN, informational).
  4 journal  state.journal twice after dropping the journal cache (a cache, not game state): the first call
             rebuilds (expect ~100 ms in state.timings), the second must be ~0 ms and the same list (PASS);
             S.questGen is printed (it grows on quest events; 0 after hours of play = listeners not firing).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))

from bg3ai.game import Game, GameError, NotReady  # noqa: E402

HELM = [-31.8, 18.2, -354.8]  # S002: the Helm deck goal, 9 m above the lower deck (impl-vis "Route ahead")
results: list[tuple[str, str, str]] = []


def report(check: str, status: str, msg: str) -> None:
    results.append((check, status, msg))
    print(f"[{status:4s}] {check}: {msg}", flush=True)


def lua_json(g: Game, body: str) -> object:
    """Run `body` (Lua that sets `out`) in the server context and decode its JSON."""
    code = ("local B = Mods.BG3AI.BG3AI\nlocal N, V, S, U = B.Nav, B.Visibility, B.State, B.Util\n"
            "local out\n" + body + "\nreturn Ext.Json.Stringify(out, { Beautify = false })")
    raw = g.eval(code, context="server")
    return json.loads(raw) if raw not in ("", "nil") else None


# --------------------------------------------------------------------------- 1 route

def pick_goal(g: Game, pos: list[float] | None) -> tuple[list[float], str]:
    if pos:
        return pos, "--pos"
    try:
        ms = (g.rpc("nav.markers", {}) or {}).get("markers") or []
        here = [m for m in ms if m.get("here") and m.get("pos")]
        if here:
            return here[0]["pos"], f"objective o1 ({here[0].get('label') or here[0].get('title') or '?'})"
    except GameError as e:
        report("route", "WARN", f"nav.markers failed ({e}); using the Helm constant")
    return HELM, "Helm constant (S002)"


def phases(r: dict) -> str:
    """The ms per step (live4 FAIL 274 ms with nothing saying where): probe = the path searches, then lower ground,
    connectors, rest, and nav.route's own ways and note. Short: check_all cuts the line at ~150 chars."""
    ph = r.get("phases") or {}
    order = ["probe", "lower", "connectors", "rest", "ways", "note"]
    return " ".join(f"{k}={ph[k]:.0f}" for k in order if isinstance(ph.get(k), (int, float))) or "no phases"


def check_route(g: Game, char: str, goal: list[float], label: str, dump: bool) -> dict | None:
    t0 = time.time()
    try:
        r = g.rpc("nav.route", {"char": char, "pos": goal, "connectors": True})
    except GameError as e:
        hint = " (mod not hot-reloaded yet?)" if "unknown op" in str(e).lower() else ""
        report("route", "FAIL", f"nav.route: {e}{hint}")
        return None
    rt = (time.time() - t0) * 1000
    ms = r.get("ms") or 0
    # timing first: check_all cuts the line at ~150 chars ("mod 222 ms, rou" once read as 222 m of route)
    report("route", "PASS" if ms < 50 else ("WARN" if ms < 150 else "FAIL"),
           f"nav.route took {ms:.0f} ms in the mod ({phases(r)}; round trip {rt:.0f} ms, connectors=true); "
           f"goal {label} {goal}: straight {r.get('straight_m')} m {r.get('dir')} dy={r.get('dy')}")
    if r.get("reachable"):
        links = r.get("links") or []
        report("route", "PASS", f"reachable: {r.get('length_m')}m, {len(r.get('legs') or [])} legs, "
                                f"links={[(l.get('kind'), l.get('dy')) for l in links]}")
    elif r.get("too_far"):
        report("route", "WARN", "goal too far for a path probe")
    else:
        c = r.get("closest")
        report("route", "PASS" if c else "WARN",
               f"unreachable; closest={c and {k: c.get(k) for k in ('pos', 'path_m', 'to_goal_m', 'dy_to_goal', 'via')}}")
    cons = r.get("connectors") or []
    ladders = [c for c in cons if c.get("kind") == "ladder"]
    report("route", "PASS" if cons else "WARN", f"{len(cons)} connector(s), {len(ladders)} ladder(s): "
           + "; ".join(f"{c.get('name')} {c.get('kind')} {c.get('dist')}m dy={c.get('dy')} "
                       f"h={c.get('height_m')} leads={c.get('leads')} path={c.get('path_m')} "
                       f"to_goal={c.get('to_goal_m')}{' helps' if c.get('helps') else ''}" for c in cons))
    if abs(r.get("dy") or 0) >= 2.5 and not ladders:
        report("route", "WARN", "goal is on another floor but no ladder was listed (the Arterial Mesh ladder "
                                "should be, on the Nautiloid)")
    try:
        from bg3ai import route as R
        from bg3ai.render import Handles
        print("         " + R.render_route(r, Handles(), "the goal").replace("\n", "\n         "))
    except Exception as e:  # rendering is a bonus here; the bridge tests cover it
        report("route", "WARN", f"render_route raised {type(e).__name__}: {e}")
    if dump:
        print(json.dumps(r, indent=1))
    return r


# --------------------------------------------------------------------------- 2 ladder

LADDER_LUA = """
out = {}
for _, u in ipairs(%s) do
  local e = V.entity(u); local p = e and V.pos(e)
  if p then
    local b = V.bounds(e)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, p[1], p[3])
    local rel = {}
    if ok and type(hs) == "table" then
      for _, h in ipairs(hs) do if type(h) == "number" and math.abs(h - p[2]) <= 20 then
        rel[#rel + 1] = math.floor((h - p[2]) * 10 + 0.5) / 10 end end
    end
    table.sort(rel)
    local ring, seen = {}, {}
    for _, r in ipairs(B.Guard and B.Guard.LADDER_TOP_RADII or { 1.0, 1.5, 2.2, 3.0 }) do
      for k = 0, 7 do
        local a = k * math.pi / 4
        local ok2, hs2 = pcall(Ext.Level.GetHeightsAt, p[1] + r * math.cos(a), p[3] + r * math.sin(a))
        if ok2 and type(hs2) == "table" then
          for _, h in ipairs(hs2) do if type(h) == "number" and math.abs(h - p[2]) <= 20 then
            local v = math.floor((h - p[2]) * 10 + 0.5) / 10
            if not seen[v] then seen[v] = true; ring[#ring + 1] = v end end end
        end
      end
    end
    table.sort(ring)
    out[#out + 1] = { uuid = u, pos = p, height = b and b.height, floors = rel, ring = ring }
  end
end
"""


def classify(height: float, floors: list[float], ring: list[float] | None = None) -> str:
    """floors: heights at the pivot's column, ring: round it, both relative to the pivot."""
    def has(v: float, xs: list[float]) -> bool:
        return any(abs(f - v) <= 0.8 for f in xs)
    above = has(height, floors) or has(height, ring or [])
    if has(0, floors) and above:
        return "foot"
    if has(-height, floors) and not has(0, floors) and not above:
        return "top"
    if has(0, floors) and has(-height, floors):
        return "ambiguous"  # S085 19353fd1 [-5.3, 0.1]: a foot over a lower floor, or a top over its landing
    if has(height / 2, floors) and has(-height / 2, floors):
        return "middle"
    return "unknown"


def check_ladders(g: Game, route: dict | None) -> None:
    ladders = [c for c in (route or {}).get("connectors") or [] if c.get("kind") == "ladder"]
    if not ladders:
        report("ladder", "WARN", "no ladder in view: stand near one (Nautiloid lower deck below the Helm)")
        return
    uuids = "{" + ",".join(json.dumps(c["uuid"]) for c in ladders) + "}"
    try:
        rows = lua_json(g, LADDER_LUA % uuids) or []
    except (GameError, ValueError) as e:
        report("ladder", "FAIL", f"probe failed: {e}")
        return
    by = {c["uuid"]: c for c in ladders}
    for row in rows:
        c = by.get(row["uuid"], {})
        h = row.get("height")
        name = f"{c.get('name')} {row['uuid'][:8]}"
        if not h:
            report("ladder", "WARN", f"{name}: no AI bounds height; floors at pivot column {row['floors']}")
            continue
        kind = classify(h, row["floors"], row.get("ring"))
        status = {"foot": "PASS", "unknown": "WARN", "ambiguous": "WARN"}.get(kind, "FAIL")
        fix = {"top": " -> flip leads/far_dy in N.connectors + frontier", "middle": " -> pivot is mid: halve",
               "ambiguous": " -> no landing seen above: climb it live to tell"}.get(kind, "")
        at = "(" + ",".join(f"{v:.1f}" for v in row["pos"][:3]) + ")" if row.get("pos") else "?"
        report("ladder", status, f"{name}: pivot at {kind} {at} (height {h:.1f}m, floors rel. pivot {row['floors']},"
                                 f" round it {row.get('ring')}){fix}")
        if c.get("leads") == "up" and (c.get("far_dy") or 0) <= 0:
            report("ladder", "FAIL", f"{name}: leads=up but far_dy={c.get('far_dy')}")
        if c.get("leads") == "down" and (c.get("far_dy") or 0) >= 0:
            report("ladder", "FAIL", f"{name}: leads=down but far_dy={c.get('far_dy')}")
    if len(rows) >= 2:
        ys = sorted(round(r["pos"][1], 1) for r in rows)
        report("ladder", "INFO", f"ladder pivot heights {ys} (two items ~height apart = both ends listed)")


# --------------------------------------------------------------------------- 3 links

LINKS_LUA = """
local char, target = %s, %s
local e = V.entity(char); local from = V.pos(e)
local path = Ext.Level.BeginPathfindingImmediate(e, target)
pcall(function() path.IgnoreEntities = { e } end) -- past the per-frame search budget (see Navigation rawPath)
out = { found = false }
local okf, found = pcall(Ext.Level.FindPath, path)
out.found = okf and found or false
if not out.found then pcall(function() out.error_cause = path.ErrorCause end) end
if out.found then
  local nodes, portals, steep, fields, flags = 0, {}, {}, {}, {}
  local prev = from
  for i, n in ipairs(path.Nodes) do
    nodes = i
    if i == 1 then pcall(function() for k in pairs(n) do fields[#fields + 1] = tostring(k) end end) end
    local p = { n.Position[1], n.Position[2], n.Position[3] }
    local fl = 0; pcall(function() fl = n.Flags end)
    flags[tostring(fl)] = (flags[tostring(fl)] or 0) + 1
    local pv = n.Portal ~= nil and Ext.Utils.IsValidHandle and Ext.Utils.IsValidHandle(n.Portal) or false
    local dy = p[2] - prev[2]
    local hd = math.sqrt((p[1] - prev[1]) ^ 2 + (p[3] - prev[3]) ^ 2)
    if pv and #portals < 12 then portals[#portals + 1] = { i = i, dy = dy, hd = hd, pos = p } end
    if math.abs(dy) >= 1 and math.abs(dy) > hd and #steep < 12 then steep[#steep + 1] = { i = i, dy = dy, hd = hd, flags = fl } end
    prev = p
  end
  out.nodes, out.portals, out.steep, out.fields, out.flags = nodes, portals, steep, fields, flags
  out.end_dy = prev[2] - from[2]
end
pcall(Ext.Level.ReleasePath, path)
"""


def check_links(g: Game, char: str, route: dict | None, goal: list[float]) -> None:
    r = route or {}
    tries: list[tuple[str, object]] = [("goal", goal)]
    if r.get("closest") and r["closest"].get("pos"):
        tries.append(("closest spot", r["closest"]["pos"]))
    for c in r.get("connectors") or []:
        if c.get("kind") == "ladder" and c.get("path_m") is not None:
            tries.append((f"ladder {c.get('name')}", c["uuid"]))
            break
    for what, tgt in tries:
        t_lua = json.dumps(tgt).replace("[", "{").replace("]", "}") if isinstance(tgt, list) else \
            f"V.pos(V.entity({json.dumps(tgt)}))"
        try:
            res = lua_json(g, LINKS_LUA % (json.dumps(char), t_lua)) or {}
        except (GameError, ValueError) as e:
            report("links", "FAIL", f"path to {what}: {e}")
            return
        if not res.get("found"):
            report("links", "INFO", f"no path to {what} (error_cause={res.get('error_cause')})")
            continue
        pt, st = res.get("portals") or [], res.get("steep") or []
        climbs = abs(res.get("end_dy") or 0) >= 2.5
        status = "PASS" if pt else ("WARN" if climbs else "INFO")
        report("links", status, f"path to {what}: {res.get('nodes')} nodes, end dy={res.get('end_dy', 0):.1f}, "
               f"valid Portal on {len(pt)} node(s) {pt[:4]}, steep steps {len(st)} {st[:4]}")
        report("links", "INFO", f"node fields: {res.get('fields')}; Flags value -> node count {res.get('flags')} "
                                f"(goal {tgt if isinstance(tgt, list) else what})")
        if "Portal" not in (res.get("fields") or []):
            report("links", "WARN", "path nodes have no Portal field on this runtime: rawPath's portal links never "
                                    "fire; links come from the steep-step rule (and maybe Flags, see above)")
        if not pt and climbs:
            report("links", "WARN", "climbing path without Portal handles: links come only from the steep-step rule")
        return
    report("links", "WARN", "no path found to the goal, the closest spot or a ladder; node flags not sampled")


# --------------------------------------------------------------------------- 4 journal

def check_journal(g: Game) -> None:
    try:
        gen = lua_json(g, "S.journalCache = nil; out = { gen = S.questGen, ttl = S.JOURNAL_TTL_MS }") or {}
    except (GameError, ValueError) as e:
        report("journal", "FAIL", f"cache probe failed (mod not hot-reloaded?): {e}")
        return
    if gen.get("gen") is None:
        report("journal", "FAIL", "S.questGen is nil: the journal cache code is not loaded (hot reload pending)")
        return
    out = []
    for i in range(2):
        t0 = time.time()
        j = g.rpc("state.journal")
        rt = (time.time() - t0) * 1000
        mod_ms = (g.rpc("state.timings") or {}).get("state.journal")
        out.append((j, mod_ms, rt))
        print(f"         call {i + 1}: mod {mod_ms} ms, round trip {rt:.0f} ms, {len(j or [])} quest(s)")
    (j1, m1, _), (j2, m2, _) = out
    report("journal", "PASS" if (m2 or 0) <= 5 else "FAIL",
           f"first {m1} ms (rebuild), second {m2} ms (cached; expect ~0)")
    report("journal", "PASS" if j1 == j2 else "FAIL", "same list from cache" if j1 == j2 else "cached list differs!")
    report("journal", "INFO", f"S.questGen={gen.get('gen')} (grows on QuestAccepted/UpdateUnlocked/Closed/"
                              f"SavegameLoaded/LevelGameplayStarted), TTL {gen.get('ttl')} ms")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pos", nargs=3, type=float, metavar=("X", "Y", "Z"))
    ap.add_argument("--char", help="party member uuid (default: the active one)")
    ap.add_argument("--json", action="store_true", help="dump the raw nav.route reply")
    ap.add_argument("--skip", default="", help="comma list of checks to skip: route,ladder,links,journal")
    a = ap.parse_args()
    skip = set(filter(None, a.skip.split(",")))
    g = Game()
    try:
        char = a.char or (g.rpc("state.status") or {}).get("active")
    except (GameError, NotReady) as e:
        print(f"game not ready: {e}")
        return 2
    if not char:
        print("no active character (is a save loaded?)")
        return 2
    goal, label = pick_goal(g, a.pos)
    route = None
    if "route" not in skip or "ladder" not in skip or "links" not in skip:
        route = check_route(g, char, goal, label, a.json)
    if "ladder" not in skip:
        check_ladders(g, route)
    if "links" not in skip:
        check_links(g, char, route, goal)
    if "journal" not in skip:
        try:
            check_journal(g)
        except GameError as e:
            report("journal", "FAIL", str(e))
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    print(f"\n{len(results)} lines: {len(fails)} FAIL, {len(warns)} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
