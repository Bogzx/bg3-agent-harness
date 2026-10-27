"""Live probe (fix round 2026-10-24 bugs 3 and 6): where the inventory rebuild and a far nav.route spend their time.

Read-only (no moves, no UI; the inventory cache rebuilds on the next read anyway). Two parts:

  inventory  V12 acceptance FAILs live2/live5/live9: the stats memos don't cut the state.inventory rebuild (58 -> 72 ms
             in live9, 194 items) or act.prepared's prep_info (29 -> 33 ms), so Ext.Stats.Get is not where the time goes
             on this build. Times 5 rebuilds with the memo emptied and 5 with it filled (min/median, not one sample),
             one with the GC stopped, the Lua heap growth of one rebuild, and per piece over every carried/worn item:
             the uuid read, V.name (GetDisplayName + loca), GetStackAmount, the Value/Data/ServerItem/Equipable reads,
             IsGold, a cold Ext.Stats.Get with its ~14 attributes; then the camp chest's supplies and party gold.
  route      live4 check_all route FAIL from Last Light Inn: nav.route 274 ms for objective o1 133 m away. Runs
             nav.route to the goal 3 times (its phases: probe, lower, connectors, rest, ways, note) and times the
             steps an unknown far goal runs: N.known (rays), V.itemsAround + the visibility test of the ladders/doors in
             CONNECT_M, N.connectors, N.unlockedWaypointsHere, N.nearestVisited, N.harmfulUnder.

    uv run --project bridge python tools/dev/probe_read_cost.py [--part inventory|route|both] [--pos X Y Z]
        [--out FILE]

Run route from the live4 spot (PROBE-5010126591__S025-end, Last Light Inn; goal = objective o1 unless --pos).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

PRELUDE = r"""
local B = Mods.BG3AI.BG3AI
local S, V, U, G, N = B.State, B.Visibility, B.Util, B.Guard, B.Nav
local now = Ext.Utils.MonotonicTime
local function stats(xs)
    table.sort(xs)
    return { min = xs[1], median = xs[math.floor((#xs + 1) / 2)], max = xs[#xs], n = #xs }
end
"""

INVENTORY = (
    PRELUDE
    + r"""
local ctx = V.context()
local function inv() S.invCache = nil local t = now() local r = S.inventory(ctx, {}) return now() - t, #r.items end
local out = { cold = {}, warm = {} }
local keep, keepI = S.statsFactsMemo, S.itemFactsMemo
for _ = 1, 5 do
    S.statsFactsMemo, S.itemFactsMemo = {}, {}
    out.cold[#out.cold + 1], out.items = inv()
    for k, v in pairs(S.statsFactsMemo) do keep[k] = v end
    for k, v in pairs(S.itemFactsMemo) do keepI[k] = v end
    S.statsFactsMemo, S.itemFactsMemo = keep, keepI
    out.warm[#out.warm + 1] = inv()
end
out.cold, out.warm = stats(out.cold), stats(out.warm)
collectgarbage("collect")
local kb = collectgarbage("count")
inv()
out.heap_kb = collectgarbage("count") - kb
collectgarbage("stop")
out.no_gc = inv()
collectgarbage("restart")
if G and G.prep_info and G.party then
    local function prep()
        local t = now()
        for _, u in ipairs(G.party()) do local g = G.guid(u) if g then G.prep_info(g) end end
        return now() - t
    end
    local keepG, pc, pw = G.spellFacts, {}, {}
    for _ = 1, 5 do
        G.spellFacts = {}
        pc[#pc + 1] = prep()
        for k, v in pairs(G.spellFacts) do keepG[k] = v end
        G.spellFacts = keepG
        pw[#pw + 1] = prep()
    end
    out.prep_cold, out.prep_warm = stats(pc), stats(pw)
end
local items = {}
for _, u in ipairs(ctx.party) do
    local e = V.entity(u)
    local io = e and U.comp(e, "InventoryOwner")
    if io then
        pcall(function()
            for _, it in pairs(io.PrimaryInventory.InventoryContainer.Items) do items[#items + 1] = it.Item end
        end)
        pcall(function()
            for _, iv in ipairs(io.Inventories) do
                if tostring(iv.InventoryData.Type) == "Equipment" then
                    for _, it in pairs(iv.InventoryContainer.Items) do items[#items + 1] = it.Item end
                end
            end
        end)
    end
end
out.pieces_items = #items
local function piece(f)
    local t = now()
    for _, ie in ipairs(items) do pcall(f, ie) end
    return now() - t
end
local ATTRS = { "SupplyValue", "ObjectCategory", "InventoryTab", "ModifierList", "Boosts", "DefaultBoosts",
    "PassivesOnEquip", "ArmorClass", "ArmorType", "Shield", "Proficiency Group", "Damage", "Weapon Properties" }
out.pieces = {
    uuid = piece(function(ie) return V.entityUuid(ie) end),
    name = piece(function(ie) return V.name(V.entityUuid(ie)) end),
    stack = piece(function(ie) return V.osi(Osi.GetStackAmount, V.entityUuid(ie)) end),
    value = piece(function(ie) local v = ie.Value return v and v.Value, v and v.Rarity, v and v.Unique end),
    data = piece(function(ie) return ie.Data.Weight, ie.Data.StatsId end),
    server_item = piece(function(ie)
        local s = ie.ServerItem return s.CanUse, s.IsContainer, s.StoryItem end),
    equipable = piece(function(ie) local q = ie.Equipable return q and q.Slot end),
    is_gold = piece(function(ie) return U.comp(ie, "IsGold") end),
    stats_get_cold = piece(function(ie)
        local st = Ext.Stats.Get(ie.Data.StatsId)
        for _, k in ipairs(ATTRS) do pcall(function() return st[k] end) end
    end),
}
local t = now()
out.camp_chest = { supplies = S.campChestSupplies() }
out.camp_chest.ms = now() - t
t = now()
V.osi(Osi.PartyGetGold, ctx.party[1])
out.gold_ms = now() - t
return Ext.Json.Stringify(out, { Beautify = false })
"""
)

ROUTE = (
    PRELUDE
    + r"""
local ctx = V.context()
local char = %s
local goal = %s
if not goal then
    local ms = (B.RPC and B.RPC.ops and B.RPC.ops["nav.markers"] and B.RPC.ops["nav.markers"]({}) or {}).markers or {}
    for _, m in ipairs(ms) do if m.here and m.pos then goal = m.pos break end end
end
if not char then
    for _, u in ipairs(ctx.party) do local e = V.entity(u) if e and U.comp(e, "ClientControl") then char = u end end
    char = char or ctx.party[1]
end
if not goal then return Ext.Json.Stringify({ error = "no goal: give --pos (no objective here)" }) end
local out = { goal = goal, runs = {} }
local from = V.pos(V.entity(char))
out.straight_m = U.dist(from, goal)
for i = 1, 3 do
    local t = now()
    local r = B.RPC.ops["nav.route"]({ char = char, pos = goal, connectors = true })
    out.runs[i] = { ms = now() - t, phases = r.phases, unknown_goal = r.unknown_goal, reachable = r.reachable,
        far = r.far, connectors = #(r.connectors or {}) }
end
local function lap(f) local t = now() local ok, v = pcall(f) return now() - t, ok and v or tostring(v) end
out.steps = {}
out.steps.known_ms, out.steps.known = lap(function() N.seenPts = {} return N.known(ctx, goal) end)
local around
out.steps.items_around_ms = lap(function() around = V.itemsAround(from, N.CONNECT_M) end)
out.steps.items_around_n = around and #around or nil
local nVis = 0
out.steps.visibility_ms = lap(function()
    for _, u in ipairs(around or {}) do
        local ie = V.entity(u)
        local p = ie and V.pos(ie)
        local k0 = p and U.dist(from, p) <= N.CONNECT_M and V.itemKind(u, ie)
        if k0 == "ladder" or k0 == "door" then nVis = nVis + 1 V.itemVisible(ctx, u, ie, p) end
    end
end)
out.steps.visibility_tested = nVis
out.steps.connectors_ms = lap(function() return N.connectors(ctx, char, goal, N.CONNECT_M, N.CONNECT_MAX, 1000) end)
out.steps.waypoints_ms = lap(function() return N.unlockedWaypointsHere(goal, from) end)
out.steps.nearest_visited_ms = lap(function()
    N.cellCenter = {} return N.nearestVisited(N.inner.currentLevel(), goal) end)
out.steps.harmful_under_ms = lap(function() return N.harmfulUnder(char) end)
return Ext.Json.Stringify(out, { Beautify = false })
"""
)


def lua_list(v: list[float] | None) -> str:
    return "{" + ",".join(repr(float(x)) for x in v) + "}" if v else "nil"


def route_code(char: str | None, pos: list[float] | None) -> str:
    return ROUTE % (json.dumps(char) if char else "nil", lua_list(pos))


def summarize(part: str, r: dict[str, Any]) -> list[str]:
    """Lines naming the biggest costs. Pure (tests)."""
    if r.get("error"):
        return [f"{part}: ERROR {r['error']}"]
    if part == "inventory":
        cold, warm = r.get("cold") or {}, r.get("warm") or {}
        lines = [
            f"inventory rebuild ({r.get('items')} items): memo emptied min {cold.get('min')} median "
            f"{cold.get('median')} ms; filled min {warm.get('min')} median {warm.get('median')} ms; GC stopped "
            f"{r.get('no_gc')} ms; heap +{round(r.get('heap_kb') or 0)} KB per rebuild",
        ]
        if r.get("prep_cold"):
            lines.append(
                f"prep_info: emptied median {r['prep_cold'].get('median')} ms, filled {r['prep_warm'].get('median')} ms"
            )
        pieces = sorted((r.get("pieces") or {}).items(), key=lambda kv: -(kv[1] or 0))
        lines.append(
            f"per piece over {r.get('pieces_items')} carried/worn items (ms): "
            + ", ".join(f"{k} {v:.1f}" for k, v in pieces)
        )
        cc = r.get("camp_chest") or {}
        lines.append(f"camp chest supplies {cc.get('supplies')}: {cc.get('ms')} ms; party gold {r.get('gold_ms')} ms")
        return lines
    runs = r.get("runs") or []
    lines = [
        f"nav.route x{len(runs)} to {r.get('goal')} ({round(r.get('straight_m') or 0)} m): "
        + "; ".join(
            f"{x.get('ms'):.0f} ms {x.get('phases')} unknown={x.get('unknown_goal')} reach={x.get('reachable')}"
            for x in runs
        )
    ]
    steps = r.get("steps") or {}
    lines.append("steps: " + ", ".join(f"{k} {v}" for k, v in sorted(steps.items())))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--part", choices=["inventory", "route", "both"], default="both")
    ap.add_argument("--pos", nargs=3, type=float, metavar=("X", "Y", "Z"), help="route goal (default: objective o1)")
    ap.add_argument("--char", help="route: party member uuid (default: the active one)")
    ap.add_argument("--out", type=Path, help="write the raw replies as JSON here")
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()
    raw: dict[str, Any] = {}
    parts = ["inventory", "route"] if a.part == "both" else [a.part]
    for part in parts:
        code = INVENTORY if part == "inventory" else route_code(a.char, a.pos)
        raw[part] = json.loads(c.evaluate(code, context="server", timeout=60.0))
        for line in summarize(part, raw[part]):
            print(line)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
