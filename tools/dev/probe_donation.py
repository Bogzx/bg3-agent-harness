"""Live probe (fix round 2026-10-24, feed_gale): what Gale's donation window holds next to the real bags.

vessa S003 19:07:51 / 19:08:06: with the window open (both items shown in it), feed_gale(e425 Beastmaster's Chain)
and feed_gale(Gloves of Power) FAILED "isn't on this side of the donation window": ui.donate's findVMItem found
the item's inventory/slot (itemLocation) but no donate group (DonateSourceInventories -> collectInventories ->
bestInventory) was that inventory with a VM slot of that index. Trade's party side, same matcher, works live.

Read-only (no clicks, no selection): with the Donate widget open it dumps, client side,
  dc         the DCDonate's property names (and their Lua types)
  sources    each DonateSourceInventories entry: its property names, OwnerObject.Name, and per TradeInventories
             VM: property names, Slots count, every occupied slot's Index + the VM item's weight/rarity/type
  groups     what ui.donate's donateGroups made of them: per collected VM, the matched inventory (owner, uuid) or
             none, and the hit/miss of every party inventory (slot index + weight, as matchScore counts them)
  bags       every party inventory (bags too): owner, uuid, each slot's index, item name, uuid, weight
  lists      the "Inventory" ListBoxes (what the player sees): per entry its Index and the VM item's weight
  item       for --name: the item's inventory/slot (itemLocation) and what findVMItem answers

    uv run --project bridge python tools/dev/probe_donation.py [--name "Beastmaster's Chain"] [--out FILE]

Steps: load the save in Gale's donation dialogue (vessa S003-1908, a PROBE- copy), answer "Give Gale a magic
artefact" so the window shows, run the probe, keep --out's JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

PROBE = r"""
local B = Mods.BG3AI.BG3AI
local UI, U, SV = B.UI, B.Util, B.Services
local I = SV.inner
local want = %s
local function keys(obj)
    local out = {}
    for k, v in pairs(UI.props(obj)) do out[#out + 1] = k .. ":" .. type(v) end
    table.sort(out)
    return out
end
local function vmItem(obj)
    local p = UI.pick(obj, { "Count", "Rarity", "ItemType", "IsEquipment", "IsStoryItem", "Name", "DisplayName" })
    p.w = tonumber(UI.path(obj, "Stats.Weight.Value"))
    for k, v in pairs(p) do if type(v) ~= "number" and type(v) ~= "boolean" then p[k] = tostring(v) end end
    return p
end
local function owner(inv)
    local o = U.comp(inv, "InventoryIsOwned")
    local e = o and I.try(function() return o.Owner end)
    return e and (I.nameOf(e) or I.uuidOf(e)) or nil
end
local function weight(e)
    local d = U.comp(e, "Data")
    return d and d.Weight or nil
end
local w = I.visibleWidget("Donate")
if not w then return Ext.Json.Stringify({ error = "no donation window is open" }) end
local dc = I.try(function() return w.DataContext end)
if not dc then return Ext.Json.Stringify({ error = "the donation window has no DataContext" }) end
local out = { dc = keys(dc), sources = {}, groups = {}, bags = {}, lists = {} }
for i, src in UI.items(UI.prop(dc, "DonateSourceInventories"), 8) do
    local s = { keys = keys(src), owner = tostring(UI.path(src, "OwnerObject.Name")), invs = {} }
    for _, vm in UI.items(UI.prop(src, "TradeInventories"), 8) do
        local v = { keys = keys(vm), count = UI.count(UI.prop(vm, "Slots")), slots = {} }
        for _, slot in UI.items(UI.prop(vm, "Slots"), 200) do
            local obj = slot and UI.get(slot, "Object")
            if obj ~= nil then
                local it = vmItem(obj)
                it.index = tonumber(UI.get(slot, "Index"))
                v.slots[#v.slots + 1] = it
            end
        end
        s.invs[#s.invs + 1] = v
    end
    out.sources[i] = s
end
local pcs = I.partyInventories()
for _, inv in ipairs(pcs) do
    local b = { owner = owner(inv), uuid = I.uuidOf(inv), slots = {} }
    for slot, e in pairs(I.containerItems(inv)) do
        b.slots[#b.slots + 1] = { index = slot, name = I.nameOf(e), uuid = I.uuidOf(e), w = weight(e) }
    end
    table.sort(b.slots, function(a, c) return a.index < c.index end)
    out.bags[#out.bags + 1] = b
end
local vms, seen = {}, {}
for _, src in UI.items(UI.prop(dc, "DonateSourceInventories"), 8) do I.collectInventories(src, vms, seen, 0) end
for _, vm in ipairs(vms) do
    local inv, slots = I.bestInventory(vm, pcs)
    local g = { slots = #slots, matched = inv and (owner(inv) or "?") or nil, scores = {} }
    for k, cand in ipairs(pcs) do
        local items, hit, miss = I.containerItems(cand), 0, 0
        for _, s in ipairs(slots) do
            local e = s.index and items[s.index]
            local vw = tonumber(UI.path(s.item, "Stats.Weight.Value"))
            vw = vw and math.floor(vw * 1000 + 0.5) or nil
            if e and (vw == nil or weight(e) == vw) then hit = hit + 1 else miss = miss + 1 end
        end
        g.scores[k] = { owner = owner(cand), hit = hit, miss = miss }
    end
    out.groups[#out.groups + 1] = g
end
for _, lb in ipairs(UI.findAll(w, function(e) return UI.name(e) == "Inventory" end, { prune = true })) do
    local l = {}
    for _, slot in UI.items(UI.get(lb, "Items") or UI.get(lb, "ItemsSource"), 60) do
        local obj = slot and UI.get(slot, "Object")
        local it = obj ~= nil and vmItem(obj) or {}
        it.index = slot and tonumber(UI.get(slot, "Index"))
        l[#l + 1] = it
    end
    out.lists[#out.lists + 1] = l
end
if want then
    local hit
    for _, inv in ipairs(pcs) do
        for slot, e in pairs(I.containerItems(inv)) do
            if I.nameOf(e) == want or I.uuidOf(e) == want then hit = { e = e, slot = slot, inv = inv } end
        end
    end
    local eq = {}
    for _, m in ipairs(Ext.Entity.GetAllEntitiesWithComponent("PartyMember") or {}) do
        local eqp = U.comp(m, "InventoryOwner")
        for _, inv in ipairs(eqp and I.try(function() return eqp.Inventories end) or {}) do
            eq[#eq + 1] = { owner = I.nameOf(m), uuid = I.uuidOf(inv) }
        end
    end
    out.owner_inventories = eq
    if hit then
        local u = I.uuidOf(hit.e)
        local m = U.comp(hit.e, "InventoryMember")
        local ok, vmi, err = pcall(I.findVMItem, u, (function()
            local gs = {}
            for _, vm in ipairs(vms) do
                local inv, slots = I.bestInventory(vm, pcs)
                gs[#gs + 1] = { vm = vm, inv = inv, slots = slots }
            end
            return gs
        end)())
        out.item = { uuid = u, bag = owner(hit.inv), slot = hit.slot,
            member_slot = m and tonumber(m.EquipmentSlot), member_inv = m and I.uuidOf(m.Inventory),
            found = ok and vmi ~= nil or false, err = ok and err or tostring(vmi) }
    else
        out.item = { error = "no party inventory slot holds " .. want }
    end
end
return Ext.Json.Stringify(out, { Beautify = false })
"""


def probe_code(name: str | None) -> str:
    return PROBE % (json.dumps(name) if name else "nil")


def summarize(r: dict[str, Any]) -> list[str]:
    """What the window holds against the bags, one line per piece. Pure (tests)."""
    if r.get("error"):
        return [f"ERROR {r['error']}"]
    lines = [f"dc: {', '.join(r.get('dc') or [])}"]
    for s in r.get("sources") or []:
        invs = s.get("invs") or []
        lines.append(
            f"source {s.get('owner')}: {len(invs)} TradeInventories, "
            + "; ".join(
                f"slots {v.get('count')} occupied {len(v.get('slots') or [])} indexes "
                f"{[x.get('index') for x in v.get('slots') or []]}"
                for v in invs
            )
        )
    for g in r.get("groups") or []:
        scores = ", ".join(f"{x.get('owner')} {x.get('hit')}/{x.get('miss')}" for x in g.get("scores") or [])
        lines.append(f"group ({g.get('slots')} slots) -> {g.get('matched') or 'NO MATCH'}; hit/miss: {scores}")
    for b in r.get("bags") or []:
        lines.append(
            f"bag {b.get('owner')}: " + ", ".join(f"{x.get('index')}={x.get('name')}" for x in b.get("slots") or [])
        )
    for i, lst in enumerate(r.get("lists") or [], 1):
        lines.append(f"list {i}: indexes {[x.get('index') for x in lst]}")
    it = r.get("item")
    if it:
        if it.get("error"):
            lines.append(f"item: {it['error']}")
        else:
            lines.append(
                f"item {it.get('uuid')} in {it.get('bag')} slot {it.get('slot')} (member slot {it.get('member_slot')})"
                f": findVMItem {'found' if it.get('found') else 'FAILED: ' + str(it.get('err'))}"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--name", help="an item's display name or uuid: where it is and what findVMItem says")
    ap.add_argument("--out", type=Path, help="write the raw reply as JSON here")
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()
    raw = json.loads(c.evaluate(probe_code(a.name), context="client", timeout=30.0))
    for line in summarize(raw):
        print(line)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
