-- A mirror arena's field, cleaned before any twin spawns (arena.setup steps -2 and -1; research R1,
-- ops/research/arena-mods.md in the website repo: one side could start next to an explosive barrel, a lever, a
-- pickable potion or a usable object, since only the surfaces were cleared). Every loose item (in no inventory, on
-- stage) within `radius` of the spawns' midpoint (the surface clear's reach, Setup.surface_reach), and every item a
-- dead body there carries (its loot, bags included: w10 L3, two dead mind flayers held a Potion of Speed and a bulb
-- each, 8.7 m on B's half and 19.1 m on A's; the clean saw neither), is made inert (loot at the body's spot):
--   pickable          RequestDelete (a story item: SetCanPickUp 0, never deleted)
--   an armed trap     SetTrapArmed 0
--   locked            Unlock; closed (doors, lids): Open
--   interactable      SetCanInteract 0 and SetDisableUse 1 (levers, containers, usable objects)
--   destructible      INVULNERABLE_NOT_SHOWN for the match (barrels)
-- Then, a tick later (a delete lands after its call), the items are read again: each one still active (any of the
-- above still true; a query that fails reads as "unread", fail closed) is counted on its half of the field (A's or B's
-- side of the plane halfway between the spawns, as Setup.check_surfaces splits it) by its kinds and template, and
-- setup fails unless both halves keep the same set. The counts go into the setup result and arena_start (`field`).

BG3AI = BG3AI or {}
local U = BG3AI.Util
local V = BG3AI.Visibility
local Ar = BG3AI.Arena
local truthy = U.truthy

local F = {}
Ar.Field = F
F.INVULNERABLE = "INVULNERABLE_NOT_SHOWN" -- Status_BOOST.txt: Invulnerable(), no overhead, log or portrait mark
F.SETTLE_MS = 500 -- the clean's deletes land before the read-back
F.SHOWN = 5       -- items named in a failure

-- An Osiris query's value; error when the call fails (a missing query or an item it doesn't take).
local function q(fn, ...)
    local f = Osi[fn]
    local ok, r = pcall(f, ...)
    if not ok then error(fn, 0) end
    return r
end

-- The items an entity's inventories hold, bags opened (InventoryOwner.Inventories -> InventoryContainer.Items);
-- none without an InventoryOwner. Errors when an inventory doesn't read.
local function carried(e, out, depth)
    local owner = e.InventoryOwner
    if not owner or depth > 4 then return out end
    for _, inv in pairs(owner.Inventories) do
        for _, slot in pairs(inv.InventoryContainer.Items) do
            local it = slot.Item
            out[#out + 1] = it.Uuid.EntityUuid
            carried(it, out, depth + 1)
        end
    end
    return out
end

-- The items within radius (horizontal) of mid, on stage: the loose ones at their spot, a dead body's loot at the
-- body's ({u, x, z, body}); by uuid. A list that doesn't read is an error (fails setup: a clean that saw nothing
-- cleaned nothing), and so is a body's inventory.
function F.items(mid, radius)
    local list = Ext.Entity.GetAllEntitiesWithComponent("ServerItem")
    if type(list) ~= "table" then error("the game's items don't read (ServerItem)", 0) end
    local function near(p) return (p[1] - mid[1]) ^ 2 + (p[3] - mid[3]) ^ 2 <= radius * radius end
    local out = {}
    for _, e in ipairs(list) do
        local ok, u, p = pcall(function()
            if e.InventoryMember then return nil end
            return e.Uuid.EntityUuid, e.Transform.Transform.Translate
        end)
        if ok and u and p and near(p) and V.osi(Osi.IsOnStage, u) ~= 0 then
            out[#out + 1] = { u = u, x = p[1], z = p[3] }
        end
    end
    local chars = Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")
    if type(chars) ~= "table" then error("the game's characters don't read (ServerCharacter)", 0) end
    for _, e in ipairs(chars) do
        local ok, u, p = pcall(function() return e.Uuid.EntityUuid, e.Transform.Transform.Translate end)
        if ok and u and p and near(p) and V.osi(Osi.IsDead, u) == 1 and V.osi(Osi.IsOnStage, u) ~= 0 then
            local okc, loot = pcall(carried, e, {}, 0)
            if not okc then error(("a dead body's inventory doesn't read (%s)"):format(tostring(V.name(u))), 0) end
            for _, it in ipairs(loot) do out[#out + 1] = { u = it, x = p[1], z = p[3], body = u } end
        end
    end
    table.sort(out, function(a, b) return a.u < b.u end)
    return out
end

-- What an item still offers a side: its active kinds, sorted ("unread:<query>" when a query fails).
function F.active(u)
    local ok, kinds = pcall(function()
        local k = {}
        if truthy(q("GetCanPickUp", u)) then k[#k + 1] = "pickable" end
        if truthy(q("IsTrapArmed", u)) then k[#k + 1] = "trap" end
        if truthy(q("IsLocked", u)) then k[#k + 1] = "locked" end
        if truthy(q("GetCanInteract", u)) then k[#k + 1] = "usable" end
        if truthy(q("IsDestructible", u)) and not truthy(q("HasActiveStatus", u, F.INVULNERABLE)) then
            k[#k + 1] = "destructible"
        end
        return k
    end)
    if not ok then return { "unread:" .. tostring(kinds) } end
    table.sort(kinds)
    return kinds
end

-- The clean (step -2): each item made inert as the header says. Returns the counts {radius, items, removed,
-- disabled, disarmed, unlocked, opened, invulnerable, errors (calls the game refused)}.
function F.clean(mid, radius, host)
    local n = { radius = radius, items = 0, removed = 0, disabled = 0, disarmed = 0, unlocked = 0, opened = 0,
        invulnerable = 0, errors = 0 }
    local function act(counter, fn, ...)
        local ok = pcall(Osi[fn], ...)
        if ok then n[counter] = n[counter] + 1 else n.errors = n.errors + 1 end
        return ok
    end
    for _, it in ipairs(F.items(mid, radius)) do
        local u = it.u
        n.items = n.items + 1
        if truthy(V.osi(Osi.GetCanPickUp, u)) then
            if truthy(V.osi(Osi.IsStoryItem, u)) then act("disabled", "SetCanPickUp", u, 0)
            else act("removed", "RequestDelete", u) end
        else
            if truthy(V.osi(Osi.IsTrapArmed, u)) then act("disarmed", "SetTrapArmed", u, 0) end
            if truthy(V.osi(Osi.IsLocked, u)) then act("unlocked", "Unlock", u, host) end
            if V.osi(Osi.IsOpened, u) == 0 then act("opened", "Open", u) end
            if truthy(V.osi(Osi.GetCanInteract, u)) then
                act("disabled", "SetCanInteract", u, 0)
                pcall(Osi.SetDisableUse, u, 1)
            end
            if truthy(V.osi(Osi.IsDestructible, u)) then
                act("invulnerable", "ApplyStatus", u, F.INVULNERABLE, -1, 1)
            end
        end
    end
    return n
end

-- The read-back (step -1, before any twin spawns): the items still active per half; error naming the first
-- differences unless both halves keep the same set. Returns {A, B} (counts of active items) for the result.
function F.check(spawns, radius)
    local a, b = spawns.A, spawns.B
    local mid = { (a[1] + b[1]) / 2, (a[2] + b[2]) / 2, (a[3] + b[3]) / 2 }
    local d = math.sqrt((b[1] - a[1]) ^ 2 + (b[3] - a[3]) ^ 2)
    local ux, uz = (b[1] - a[1]) / d, (b[3] - a[3]) / d
    local keep = { A = {}, B = {} }
    for _, it in ipairs(F.items(mid, radius)) do
        local kinds = F.active(it.u)
        if #kinds > 0 then
            local along = (it.x - mid[1]) * ux + (it.z - mid[3]) * uz
            local side = along < 0 and "A" or along > 0 and "B" or nil -- on the plane: as near one line as the other
            if side then
                local key = table.concat(kinds, "+") .. " " .. tostring(V.osi(Osi.GetTemplate, it.u))
                local l = keep[side]
                l[key] = l[key] or {}
                table.insert(l[key], ("%s at (%.1f, %.1f)%s"):format(tostring(V.name(it.u)), it.x, it.z,
                    it.body and (" on " .. tostring(V.name(it.body)) .. "'s body") or ""))
            end
        end
    end
    local off, count = {}, { A = 0, B = 0 }
    for _, s in ipairs(Ar.SIDES) do
        local o = s == "A" and "B" or "A"
        local keys = {}
        for k, l in pairs(keep[s]) do
            count[s] = count[s] + #l
            keys[#keys + 1] = k
        end
        table.sort(keys)
        for _, k in ipairs(keys) do
            local extra = #keep[s][k] - #(keep[o][k] or {})
            for i = 1, extra do
                off[#off + 1] = ("%s's half keeps %s (%s)"):format(s, keep[s][k][#keep[s][k] - extra + i], k)
            end
        end
    end
    if #off > 0 then
        error(("the field doesn't mirror after the clean (%d item(s) within %.1f m): %s"):format(#off, radius,
            table.concat({ table.unpack(off, 1, F.SHOWN) }, "; ")), 0)
    end
    return count
end

return F
