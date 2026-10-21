-- Guard, part 2 of 4 (Guard.lua loads the parts in order; split for the 1000-line cap):
-- the Osiris gateway (cheat blocklist), keys and action resources.
local U = BG3AI.Util
local G = BG3AI.Guard

------------------------------------------------------------------------
-- Osiris gateway: the cheat blocklist. Action code calls game-changing
-- Osiris functions ONLY through G.osi(); anything not allowlisted is refused.
------------------------------------------------------------------------

G.ALLOW = {
    CharacterMoveToPosition = true, PurgeOsirisQueue = true,
    Use = true, Pickup = true, ToInventory = true, SendToCampChest = true,
    Equip = true, Unequip = true, UseSpell = true, UseSpellAtPosition = true,
    EndTurn = true, MakePlayerActive = true, AttachToPartyGroup = true,
    DetachFromPartyGroup = true, ForceTurnBasedMode = true,
    TeleportToFromCamp = true,   -- raising the engine's own "camp button" event
    DialogStartRequested = true, -- raising the engine's own "player clicked NPC" event
    RequestPickpocket = true,    -- raising the engine's own "player clicked to pickpocket" event
    OpenCharacterLootUI = true,  -- loot UI on a corpse, only where the story would answer "yes" (see act.loot_corpse)
}

-- Never allowed, whatever the reason (research/03-actions.md §7).
G.BLOCK = {
    TeleportTo = true, TeleportToPosition = true, ApplyStatus = true, RemoveStatus = true,
    RemoveStatusesWithGroup = true, Open = true, Unlock = true, Lock = true,
    AddGold = true, UserAddGold = true, PartyAddGold = true, TemplateAddTo = true,
    AddSpell = true, AddTadpolePower = true, SetFlag = true, ClearFlag = true,
    RequestActiveRoll = true, RequestPassiveRoll = true, ActivateTrade = true,
    StartRespec = true, AddExplorationExperience = true, AddExperience = true,
    SetHitpointsPercentage = true, Resurrect = true, MakePlayer = true,
    SetFaction = true, SetRelation = true, SetRelationTemporaryHostile = true,
    RequestLongRest = true, RequestLongRestConfirmed = true, AutoSave = true,
    QRY_StartDialog_Fixed = true, QRY_StartDialog = true, CharacterMoveToAndTalk = true,
    CharacterMoveTo = true, -- the target-entity form walks with no path check and teleports when no path (ACT-13)
    SetCanJoinCombat = true, RequestGatherAtCamp = true, EndCombat = true, LeaveCombat = true, Die = true,
}

function G.osi(name, ...)
    if G.BLOCK[name] or not G.ALLOW[name] then
        error("guard: Osiris call '" .. tostring(name) .. "' is not permitted", 0)
    end
    if name:sub(1, 3) == "DB_" then error("guard: database writes are not permitted", 0) end
    return Osi[name](...)
end

-- Like G.osi, for calls whose argument count differs between game builds.
-- Tries each argument list in order and only moves on when bg3le rejects the
-- count. Put the engine form first: bg3le routes a count the engine lacks to a
-- story-declared form of that name if one exists (lua_host.cpp), so a short
-- form could run story code instead. Live (bg3le, 2026-10-09): engine forms
-- are UseSpellAtPosition/6, Equip/5, UseSpell/5, Unequip/2.
function G.osi_forms(name, ...)
    local forms = { ... }
    local last
    for _, args in ipairs(forms) do
        local ok, r = pcall(G.osi, name, table.unpack(args, 1, args.n or #args))
        if ok then return r end
        last = r
        if not tostring(r):find("number of IN arguments", 1, true) then break end
    end
    error(last, 0)
end

------------------------------------------------------------------------
-- Keys. A locked door/chest carries eoc::lock::LockComponent.Key_M; a key
-- item carries eoc::lock::KeyComponent.Key with the same name. In the game,
-- clicking a locked door with its key in your bags opens it (the engine raises
-- Unlocked(item, char, key)). The player knows their own inventory, so the
-- match is fair; the key name itself is never returned to the agent.
------------------------------------------------------------------------

function G.lock_key(item)
    local k
    pcall(function() k = G.entity(item).Lock.Key_M end)
    k = k and tostring(k) or nil
    if k == "" or k == "None" then return nil end
    return k
end

local function item_key(ie)
    local k
    pcall(function() k = ie.Key.Key end)
    k = k and tostring(k) or nil
    if k == "" or k == "None" then return nil end
    return k
end

local function find_in_inventory(inv, want, depth)
    local found
    pcall(function()
        for _, it in pairs(inv.InventoryContainer.Items) do
            local ie = it.Item
            if item_key(ie) == want then
                found = ie.Uuid.EntityUuid
                return
            end
            if depth > 0 then
                local io
                pcall(function() io = ie.InventoryOwner end)
                if io and io.PrimaryInventory then
                    found = find_in_inventory(io.PrimaryInventory, want, depth - 1)
                    if found then return end
                end
            end
        end
    end)
    return found
end

-- Uuid of the key in `char`'s bags (keychain and other bags included) that opens `item`, or nil.
function G.key_in(char, item)
    local want = G.lock_key(item)
    if not want then return nil end
    local inv
    pcall(function() inv = G.entity(char).InventoryOwner.PrimaryInventory end)
    if not inv then return nil end
    return G.guid(find_in_inventory(inv, want, 3))
end

-- The party member carrying `item`'s key, actor first: holder, key (or nil).
function G.party_key(item, actor)
    if not G.lock_key(item) then return nil end
    local order = { actor }
    for _, p in ipairs(G.party()) do
        if not G.same(p, actor) then order[#order + 1] = p end
    end
    for _, p in ipairs(order) do
        local k = p and G.key_in(p, item)
        if k then return p, k end
    end
    return nil
end

-- The one sanctioned Unlock: `char` stands at `item` with its key in their bags,
-- which is exactly when the game unlocks it on a click. Rechecks everything.
function G.unlock_with_key(item, char, reach)
    local k = G.key_in(char, item)
    if not k then error("guard: no matching key carried; Unlock is not permitted", 0) end
    local a, t = G.pos(char), G.pos(item)
    if not a or not t or U.dist(a, t) > (reach or 2.5) then
        error("guard: not next to the lock; Unlock is not permitted", 0)
    end
    Osi.Unlock(item, char)
    return k
end

-- Does a story database have a row whose first column is `uuid`? Reading a
-- DB is only used to choose an execution path, never returned to the agent.
function G.db_has(name, arity, uuid, second)
    local ok, rows = pcall(function()
        local args = {}
        for i = 1, arity do args[i] = nil end
        return Osi[name]:Get(table.unpack(args, 1, arity))
    end)
    if not ok or not rows then return false end
    for _, r in ipairs(rows) do
        if G.same(r[1], uuid) and (second == nil or G.same(r[2], second)) then return true end
    end
    return false
end

-- Engine cast options that must never be set on a cast we originate.
G.FORBIDDEN_CAST_OPTIONS = {
    "IgnoreHasSpell", "IgnoreCastChecks", "IgnoreSpellRolls", "IgnoreTargetChecks", "Forced",
}

-- opts: nil, a list of option names, or a table name->bool.
function G.cast_options_ok(opts)
    if opts == nil then return true end
    for k, v in pairs(opts) do
        local name = (type(k) == "number") and v or (v and k)
        for _, bad in ipairs(G.FORBIDDEN_CAST_OPTIONS) do
            if name == bad then G.refuse("cast option '" .. bad .. "' is not allowed") end
        end
    end
    return true
end

------------------------------------------------------------------------
-- Action resources
------------------------------------------------------------------------

local resource_names = {}  -- resource UUID -> Name (static data), cached
local function resource_name(uuid)
    local key = tostring(uuid)
    local n = resource_names[key]
    if n == nil then
        local ok, d = pcall(Ext.StaticData.Get, key, "ActionResource")
        n = (ok and d and d.Name) or false
        resource_names[key] = n
    end
    return n or nil
end

-- Snapshot: name -> level -> amount (level 0 for unleveled resources).
function G.resources(char)
    local out = {}
    local e = G.entity(char)
    local ar = e and U.comp(e, "ActionResources")
    if not ar then return out end
    pcall(function()
        for uuid, list in pairs(ar.Resources) do
            local name = resource_name(uuid)
            if name then
                out[name] = out[name] or {}
                for _, r in ipairs(list) do
                    out[name][r.Level or 0] = r.Amount
                end
            end
        end
    end)
    return out
end

-- Parse a stats UseCosts string ("ActionPoint:1;SpellSlotsGroup:1:1:2").
-- Returns a list of {name, amount, level}. Spell slot groups become
-- {name="SpellSlotsGroup", amount, level}.
function G.parse_costs(str, upcast)
    local costs = {}
    if type(str) ~= "string" then return costs end
    for part in str:gmatch("[^;]+") do
        local fields = {}
        for f in part:gmatch("[^:]+") do fields[#fields + 1] = f:match("^%s*(.-)%s*$") end
        local name = fields[1]
        if name and name ~= "" then
            if name == "SpellSlotsGroup" or name == "SpellSlot" or name == "WarlockSpellSlot" then
                local amount = tonumber(fields[2]) or 1
                local level = tonumber(fields[4] or fields[3]) or 1
                if upcast and upcast > level then level = upcast end
                costs[#costs + 1] = { name = name, amount = amount, level = level }
            else
                costs[#costs + 1] = { name = name, amount = tonumber(fields[2]) or 1, level = 0 }
            end
        end
    end
    return costs
end

-- Which concrete resource pays for a cost entry, given a snapshot.
-- Returns name, level or nil if nothing can pay.
function G.payer(snap, cost)
    if cost.name == "SpellSlotsGroup" then
        for _, n in ipairs({ "SpellSlot", "WarlockSpellSlot" }) do
            local lv = snap[n]
            if lv then
                if (lv[cost.level] or 0) >= cost.amount then return n, cost.level end
                if n == "WarlockSpellSlot" then
                    -- pact slots are all of one level, at least the spell's level
                    for l, amt in pairs(lv) do
                        if l >= cost.level and amt >= cost.amount then return n, l end
                    end
                end
            end
        end
        return nil
    end
    local lv = snap[cost.name]
    if lv and (lv[cost.level] or lv[0] or 0) >= cost.amount then
        return cost.name, (lv[cost.level] and cost.level or 0)
    end
    return nil
end

local FRIENDLY = {
    ActionPoint = "action", BonusActionPoint = "bonus action", ReactionActionPoint = "reaction",
    Movement = "movement", SpellSlotsGroup = "spell slot", SpellSlot = "spell slot",
    WarlockSpellSlot = "pact slot", KiPoint = "ki point", SorceryPoint = "sorcery point",
    Rage = "rage charge", BardicInspiration = "Bardic Inspiration", ChannelDivinity = "Channel Divinity",
    SuperiorityDie = "superiority die", ArcaneRecoveryPoint = "Arcane Recovery", WildShape = "Wild Shape charge",
}
function G.friendly(name) return FRIENDLY[name] or name end

-- Writes a deduction to the ActionResources component (used only to make an
-- Osiris-originated cast pay what the UI cast would have paid).
function G.deduct(char, name, level, amount)
    local e = G.entity(char)
    local ar = e and U.comp(e, "ActionResources")
    if not ar then return false end
    local done = false
    pcall(function()
        for uuid, list in pairs(ar.Resources) do
            if resource_name(uuid) == name then
                for _, r in ipairs(list) do
                    if (r.Level or 0) == (level or 0) and not done then
                        r.Amount = math.max(0, r.Amount - amount)
                        done = true
                    end
                end
            end
        end
        if done then e:Replicate("ActionResources") end
    end)
    return done
end
