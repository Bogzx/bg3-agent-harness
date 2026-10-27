-- State, part 2 of 3 (State.lua loads the parts in order; split for the 1000-line cap):
-- inventory, combat, journal, map, nearby and the targeting preview.
local U = BG3AI.Util
local V = BG3AI.Visibility
local S = BG3AI.State

local truthy = U.truthy
local osi = V.osi
local comp = U.comp
local round = U.round
local I = S.inner
local weaponRanges, ABBR, pos1, resources = I.weaponRanges, I.ABBR, I.pos1, I.resources
local selectedCharacter, inCombat, concentration = I.selectedCharacter, I.inCombat, I.concentration
local health, abilityBlock, mod, raceName = I.health, I.abilityBlock, I.mod, I.raceName
local spellInfo, relation, standRadius = I.spellInfo, I.relation, I.standRadius

------------------------------------------------------------------------
-- Inventory
------------------------------------------------------------------------

-- What the item tooltip shows, from its stats entry: camp-supply value (food), category, armour class and
-- type, weapon damage, the proficiency it needs. The bridge's economy.py uses these for supplies, junk
-- selling and gear upgrades (S026: bags full of junk, Sentinel Shield unequipped, food unknown).
local function statField(st, k)
    local ok, v = pcall(function() return st[k] end)
    if not ok or v == nil or v == "" then return nil end
    if type(v) == "table" then
        local t = {}
        for _, x in ipairs(v) do t[#t + 1] = tostring(x) end
        return #t > 0 and table.concat(t, "|") or nil
    end
    return v
end

local function statsFacts(st, o)
    local sv = tonumber(statField(st, "SupplyValue"))
    if sv and sv > 0 then o.supply = sv end
    local cat = statField(st, "ObjectCategory")
    if type(cat) == "string" then o.cat = cat:match("^[^;]+") end
    local tab = statField(st, "InventoryTab")
    if tab and tab ~= "Auto" then o.tab = tostring(tab) end
    local kind = statField(st, "ModifierList")
    local boosts = (tostring(statField(st, "Boosts") or "")) .. ";" .. tostring(statField(st, "DefaultBoosts") or "")
    -- Gear that only helps a spellcaster (S028: equip_best put the Spellcrux Amulet on Karlach by rarity alone)
    local spelly = (boosts .. ";" .. tostring(statField(st, "PassivesOnEquip") or "")):lower()
    for _, k in ipairs({ "spellsavedc", "spellattack", "spellslot", "spellcrux", "spelldc", "arcanerecovery" }) do
        if spelly:find(k, 1, true) then o.spellgear = true break end
    end
    if kind == "Armor" then
        o.ac = tonumber(statField(st, "ArmorClass")) or 0
        local plus = tonumber(boosts:match("AC%((%d+)%)"))
        if plus then o.ac = o.ac + plus end
        o.armor = statField(st, "ArmorType")
        if statField(st, "Shield") == "Yes" then o.shield = true end
        o.prof = statField(st, "Proficiency Group")
    elseif kind == "Weapon" then
        o.dmg = statField(st, "Damage")
        o.prof = statField(st, "Proficiency Group")
        local wp = tostring(statField(st, "Weapon Properties") or "")
        if wp:find("Twohanded", 1, true) then o.twohanded = true end
        if wp:find("Torch", 1, true) then o.torch = true end  -- a held light (Moonlantern: its aura needs it in a hand)
        if wp:find("Finesse", 1, true) then o.finesse = true end  -- STR or DEX, whichever is higher
        local ench = tonumber(boosts:match("WeaponEnchantment%((%d+)%)"))
        if ench then o.ench = ench end
    end
end

-- statsFacts per StatsId. A stats entry doesn't change in a running game, and bg3le's Ext.Stats.Get is Lua: a fresh
-- proxy per call, each attribute read a lookup plus a decode. Uncached, every full state.inventory re-read ~14
-- attributes of each carried item twice (supplyTotal and itemView) and of the camp chest's items (V12: the reads
-- look/get_status/get_inventory took 140-350 ms in S066-S070). The facts are scalars; a missing entry isn't kept.
S.statsFactsMemo = {}

-- false: the item names a stats entry that didn't read.
local function itemStats(ie, o)
    local sid
    pcall(function() sid = ie.Data.StatsId end)
    if sid == nil then return end
    local f = S.statsFactsMemo[tostring(sid)]
    if not f then
        local st
        pcall(function() st = Ext.Stats.Get(sid) end)
        if not st then return false end
        f = {}
        statsFacts(st, f)
        S.statsFactsMemo[tostring(sid)] = f
    end
    for k, v in pairs(f) do o[k] = v end
end

-- Class-bound weapons (equip_best keeps them in hand): the bind statuses the bind spells apply to the item
-- (Shout_PactOfTheBlade_Bind -> PACT_BLADE, Shout_WeaponBond -> WEAPON_BOND; 1 action each to redo), and the
-- summoned pact weapons (WPN_Rapier_Pact … from Shout_PactOfTheBlade_<Weapon>; gone once unequipped).
local BOND_STATUSES = { { "PACT_BLADE", "pact" }, { "PACTOFTHEBLADE_HEXBLADE", "pact" },
                        { "WEAPON_BOND", "weapon_bond" }, { "MAG_WEAPON_BOND", "weapon_bond" } }

local function isPactWeapon(ie)
    local sid
    pcall(function() sid = ie.Data.StatsId end)
    return type(sid) == "string" and (sid:find("_Pact$") ~= nil or sid == "_PactWeapon")
end

local function weaponBond(f, u, o)
    if f.pact then
        o.bound, o.summoned = "pact", true
        return
    end
    for _, b in ipairs(BOND_STATUSES) do
        if truthy(osi(Osi.HasActiveStatus, u, b[1])) then o.bound = b[2] return end
    end
end

-- Bind spells a member knows: "pact" (Pact of the Blade: the bound weapon attacks with CHA), "weapon_bond"
-- (Eldritch Knight).
local BIND_SPELLS = { Shout_PactOfTheBlade_Bind = "pact", Shout_PactOfTheBlade = "pact", Shout_WeaponBond = "weapon_bond" }

local function bindsOf(e)
    local out
    pcall(function()
        for _, sp in ipairs(e.SpellBook.Spells) do
            local b = BIND_SPELLS[sp.Id.Prototype] or BIND_SPELLS[sp.Id.OriginatorPrototype]
            if b then out = (out == nil or out == b) and b or out end
        end
    end)
    return out
end

-- An item's facts that don't change while it exists, read once per item uuid (V12, probe 8fda912 window B: a full
-- rebuild of 194 items spent ~50 ms on the per-item component reads: value, data, server item, equipable, gold ...,
-- each a pcall'd proxy read, in both the supplies walk and the item walk). Kept for this Lua state (a load rebuilds
-- it): .v the view's static fields, .inv the item holds an inventory, .pact a summoned pact weapon. An item whose
-- stats entry didn't read isn't kept. What can change is read on every view: name, stack, worn slot, bind status.
S.itemFactsMemo = {}

local function itemFacts(ie, u)
    local f = u and S.itemFactsMemo[u]
    if f then return f end
    local o = {}
    pcall(function()
        local v = ie.Value
        if v then
            o.value = v.Value
            if v.Rarity and v.Rarity > 0 then o.rarity = ({ "uncommon", "rare", "very_rare", "legendary" })[v.Rarity] or v.Rarity end
            if v.Unique then o.unique = true end
        end
    end)
    pcall(function() o.weight = round((ie.Data.Weight or 0) / 1000, 2) end)
    pcall(function() if ie.ServerItem.CanUse then o.usable = true end end)
    pcall(function() if ie.ServerItem.IsContainer then o.container = true end end)
    pcall(function() if ie.ServerItem.StoryItem then o.story = true end end)
    pcall(function() local t = ie.Equipable; if t then o.slot_type = tostring(t.Slot) end end)
    if comp(ie, "IsGold") then o.gold = true end
    local read = itemStats(ie, o)
    f = { v = o, inv = comp(ie, "InventoryOwner") ~= nil, pact = o.dmg ~= nil and isPactWeapon(ie) }
    if u and read ~= false then S.itemFactsMemo[u] = f end
    return f
end

local function itemView(ie, owner, slot)
    local u = V.entityUuid(ie)
    if not u then return nil end
    local f = itemFacts(ie, u)
    local o = { uuid = u, name = V.name(u), owner = owner }
    for k, v in pairs(f.v) do o[k] = v end
    local amt = osi(Osi.GetStackAmount, u)
    if amt and amt > 1 then o.count = amt end
    if slot then o.equipped_slot = slot end
    if o.dmg then weaponBond(f, u, o) end
    return o
end

-- Camp supplies the party carries (food, loose or in the Camp Supply Sacks), as the rest screen counts them.
local function supplyTotal(invEntity, depth)
    local n = 0
    if not invEntity then return 0 end
    pcall(function()
        for _, it in pairs(invEntity.InventoryContainer.Items) do
            local ie = it.Item
            local u = V.entityUuid(ie)
            local f = itemFacts(ie, u)
            if f.v.supply then
                n = n + f.v.supply * math.max(1, (u and osi(Osi.GetStackAmount, u)) or 1)
            end
            local io = depth > 0 and f.inv and comp(ie, "InventoryOwner")
            if io then n = n + supplyTotal(io.PrimaryInventory, depth - 1) end
        end
    end)
    return n
end

-- Per member: proficiencies (what they can wear/wield without penalty) and encumbrance (0 none,
-- 1 Encumbered, 2 Heavily Encumbered), carried weight, the unencumbered limit and the Heavily threshold (kg).
-- A temporary follower (S053: Zanner Toobin in the party for a quest) is no permanent member: equip_best must not dress
-- it. Osiris IsPartyFollower, else no Origin and no Avatar component (origins and the custom avatar have Origin;
-- live probe 2026-10-11 07:1x on Claude/Wyll/Karlach/Shadowheart).
local function isFollower(u, e)
    if u and truthy(osi(Osi.IsPartyFollower, u)) then return true end
    return e ~= nil and not comp(e, "Origin") and not comp(e, "Avatar")
end
S.isFollower = isFollower

local function memberInfo(e, u)
    local m = {}
    if isFollower(u, e) then m.follower = true end
    pcall(function()
        local t = {}
        for _, f in pairs(e.Proficiency.Flags) do t[#t + 1] = tostring(f) end
        m.prof = t
    end)
    pcall(function()  -- has spell slots (caster gear goes to them): Wizard, Cleric, Paladin, Warlock …
        local r = resources(e)
        m.caster = (r.SpellSlot ~= nil or r.WarlockSpellSlot ~= nil)
    end)
    local ab = abilityBlock(e)  -- weapon choice: STR weapons vs finesse/ranged (DEX)
    if ab then m.str, m.dex, m.cha = ab.str, ab.dex, ab.cha end
    m.binds = bindsOf(e)  -- pact / weapon_bond: equip_best keeps the bound weapon, a pact weapon uses CHA
    pcall(function() m.enc = tonumber(e.EncumbranceState.State) or 0 end)
    pcall(function() m.kg = round((e.InventoryWeight.Weight or 0) / 1000, 1) end)
    pcall(function() m.limit = round(e.EncumbranceStats.UnencumberedWeight / 1000, 1) end)
    -- Above limit: Encumbered (state 1); above heavy: Heavily Encumbered (live probe 2026-10-10).
    pcall(function() m.heavy = round(e.EncumbranceStats.EncumberedWeight / 1000, 1) end)
    return m
end

-- Camp supplies in the camp chest (the rest screen counts them with the carried ones; food sent to camp
-- lands there). DB_Camp_UserCampChest(player, chest); every player shares the first chest.
local function campChestSupplies()
    local n
    pcall(function()
        for _, row in ipairs(Osi.DB_Camp_UserCampChest:Get(nil, nil) or {}) do
            local ce = V.entity(tostring(row[2]):match("(%x+%-%x+%-%x+%-%x+%-%x+)$"))
            local io = ce and comp(ce, "InventoryOwner")
            if io then n = supplyTotal(io.PrimaryInventory, 2); return end
        end
    end)
    return n
end
S.campChestSupplies = campChestSupplies

local function inventoryItems(invEntity, owner, out, depth, inContainer)
    if not invEntity then return end
    pcall(function()
        for _, it in pairs(invEntity.InventoryContainer.Items) do
            local ie = it.Item
            local v = itemView(ie, owner)
            if v then
                v["in"] = inContainer
                out[#out + 1] = v
                if v.container then
                    local io = comp(ie, "InventoryOwner")
                    if io and depth > 0 then
                        inventoryItems(io.PrimaryInventory, owner, out, depth - 1, v.name)
                    elseif io then
                        local n = 0
                        pcall(function() for _ in pairs(io.PrimaryInventory.InventoryContainer.Items) do n = n + 1 end end)
                        if n > 0 then v.contains = n end
                    end
                end
            end
        end
    end)
end

-- Inventory cache for the default read (whole party, bags collapsed, no filter): ~55 ms and 40+ KB live
-- (194 items), and look/get_status read it every call for the ALERTS line and the item delta. Item
-- listeners bump S.invGen; the cache also expires after INV_TTL_MS, a backstop for anything they miss.
-- args.rev = the rev of the copy the caller holds: when it is still current the reply is just
-- { unchanged = true, rev, gold } (gold is always read fresh: it is one Osiris call).
S.invGen = S.invGen or 0
-- A load rebuilds this Lua state; a rev restarting at 0 matched the bridge's held rev of the previous save (live
-- 12:3x: ALERTS/get_inventory kept the other save's gear after load_game). Monotonic ms never repeats in a process.
S.invRev = S.invRev or math.floor(Ext.Utils.MonotonicTime())
S.INV_TTL_MS = 20000
function S.bumpInventory() S.invGen = (S.invGen or 0) + 1 end
for _, ev in ipairs({ { "AddedTo", 3 }, { "RemovedFrom", 3 }, { "Equipped", 2 }, { "Unequipped", 2 },
    { "UseFinished", 3 }, { "TemplateAddedTo", 4 }, { "TemplateRemovedFrom", 3 }, { "CharacterJoinedParty", 1 },
    { "CharacterLeftParty", 1 }, { "LeveledUp", 1 }, { "LongRestFinished", 0 }, { "ShortRested", 1 },
    { "SavegameLoaded", 0 }, { "LevelGameplayStarted", 2 }, { "Combined", 7 } }) do
    if Ext.Osiris then pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function() S.bumpInventory() end) end
end
-- An item that left an inventory may lie on the floor now: the item index rebuilds soon (Visibility.lua).
for _, ev in ipairs({ { "RemovedFrom", 3 }, { "TemplateRemovedFrom", 3 } }) do
    if Ext.Osiris then
        pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function() if V.indexDirty then V.indexDirty() end end)
    end
end

local inventoryFull
function S.inventory(ctx, args)
    local plain = not args.uuid and not args.expand and not (type(args.filter) == "string" and args.filter ~= "")
    local open = BG3AI.Events and BG3AI.Events.openContainer
    if not plain or (open and open.uuid) then return inventoryFull(ctx, args) end
    local key = S.invGen .. "|" .. table.concat(ctx.party, ",")
    local c, t = S.invCache, Ext.Utils.MonotonicTime()
    if not c or c.key ~= key or t - c.at > S.INV_TTL_MS then
        S.invRev = (S.invRev or 0) + 1
        c = { key = key, at = t, rev = S.invRev, out = inventoryFull(ctx, args) }
        c.out.rev = c.rev
        S.invCache = c
    end
    local gold = osi(Osi.PartyGetGold, ctx.party[1])
    if args.rev ~= nil and tonumber(args.rev) == c.rev then
        return { unchanged = true, rev = c.rev, gold = gold }
    end
    c.out.gold = gold
    return c.out
end

function inventoryFull(ctx, args)
    local out = { items = {} }
    local owners = args.uuid and { args.uuid } or ctx.party
    out.members, out.supplies = {}, 0
    for _, u in ipairs(owners) do
        if not ctx.is_party[u] then error("not a party member", 0) end
        local e = V.entity(u)
        local io = e and comp(e, "InventoryOwner")
        if e then out.members[u] = memberInfo(e, u) end
        if io then
            out.supplies = out.supplies + supplyTotal(io.PrimaryInventory, 1)
            -- Bags (camp supplies, pouches) are collapsed unless expand=true or a
            -- filter is given (live S020: filter "key" found only the Keychain bags).
            local f = args.filter
            local deep = args.expand or (type(f) == "string" and f ~= "")
            inventoryItems(io.PrimaryInventory, u, out.items, deep and 1 or 0)
            pcall(function()
                for _, inv in ipairs(io.Inventories) do
                    if tostring(inv.InventoryData.Type) == "Equipment" then
                        for slot, it in pairs(inv.InventoryContainer.Items) do
                            local v = itemView(it.Item, u, tostring(Ext.Enums.ItemSlot[slot]))
                            if v then out.items[#out.items + 1] = v end
                        end
                    end
                end
            end)
        end
    end
    local f = args.filter
    if type(f) == "string" and f ~= "" then
        local lf = f:lower()
        local kept = {}
        for _, it in ipairs(out.items) do
            if it.name:lower():find(lf, 1, true) then kept[#kept + 1] = it end
        end
        out.items = kept
    end
    if not args.uuid then out.camp_supplies = campChestSupplies() end
    out.gold = osi(Osi.PartyGetGold, ctx.party[1])
    -- The container the party currently has open (contents are visible on screen).
    local open = BG3AI.Events and BG3AI.Events.openContainer
    if open and open.uuid and V.entity(open.uuid) then
        local ce = V.entity(open.uuid)
        local io = comp(ce, "InventoryOwner")
        if io then
            local items = {}
            inventoryItems(io.PrimaryInventory, open.uuid, items, 0)
            for _, it in ipairs(items) do it.owner = nil end
            out.open_container = { uuid = open.uuid, name = V.name(open.uuid), items = items }
        end
    end
    return out
end

------------------------------------------------------------------------
-- Combat
------------------------------------------------------------------------

-- Planning data for a visible entity at p: distance and line of sight from
-- every party member that is in the world: from = {uuid = {dist, los}}.
local function tactical(ctx, uuid, p, o)
    local from = {}
    for _, ob in ipairs(ctx.observers) do
        if ob.pos and ob.uuid ~= uuid then
            from[ob.uuid] = { round(U.dist(ob.pos, p), 1), truthy(osi(Osi.HasLineOfSight, ob.uuid, uuid)) and 1 or 0 }
        end
    end
    if next(from) then o.from = from end
    return o
end
S.tactical = tactical

local function combatEntityFor(uuid)
    local e = V.entity(uuid)
    local cp = e and comp(e, "CombatParticipant")
    if not cp then return nil end
    local ok, h = pcall(function() return cp.CombatHandle end)
    if ok and h then return h end
    return nil
end

-- The name of a story light (Moonlantern: Torch|Unstowable) in a member's melee main hand, else nil: it fights
-- with torch damage while its aura shields the party (S025-S027 Claude).
local function heldLight(u)
    local name
    pcall(function()
        local it = osi(Osi.GetEquippedItem, u, "Melee Main Weapon")
        if not it then return end
        local ie = Ext.Entity.Get(it)
        if not (ie and ie.ServerItem and ie.ServerItem.StoryItem) then return end
        local st = Ext.Stats.Get(ie.Data.StatsId)
        local wp = st and st["Weapon Properties"]
        for _, p in pairs(type(wp) == "table" and wp or {}) do
            if p == "Torch" then name = V.name(it) end
        end
    end)
    return name
end

-- A turn-order row's HP percent (pure; tested offline). S076 00:29: Karlach at 1/218 rounded to 0 % and the bridge
-- called her downed: a living member reads at least 1 %; the row's `downed` (the game's state) says downed.
function S.hp_pct(hp, max_hp)
    local p = math.floor(100 * hp / max_hp + 0.5)
    if p == 0 and hp > 0 then p = 1 end
    return p
end

function S.combat(ctx)
    local ce
    for _, u in ipairs(ctx.party) do
        if inCombat(u) then ce = combatEntityFor(u); if ce then break end end
    end
    if not ce then return { active = false } end
    local out = { active = true, order = {} }
    local to = comp(ce, "TurnOrder")
    if not to then return out end
    pcall(function() out.round = to.Round end)
    local ended_current -- a group whose members all ended (the game is about to move on)
    pcall(function()
        for _, g in ipairs(to.CurrentGroups) do
            for _, m in ipairs(g.Members) do
                local u = V.entityUuid(m.Entity)
                local me = m.Entity
                if u then
                    -- Traps join combat too (S025: an undetected Tripwire on the turn order, initiative -20):
                    -- an item member passes the item rules (undiscovered traps stay hidden), not the character ones.
                    local seen
                    if comp(me, "ServerItem") then seen = V.itemVisible(ctx, u) else seen = V.characterVisible(ctx, u, me) end
                    if seen then
                        local tb = comp(me, "TurnBased")
                        local o = { uuid = u, name = V.name(u), relation = relation(ctx, u) }
                        if comp(me, "IsSummon") then o.summon = true end  -- ours or theirs: an ally, not a companion
                        if comp(me, "ServerItem") then o.item = true end  -- a prop/trap on the turn order
                        -- the race the hover tooltip shows: lessons.md triggers match it (Undead, Githyanki …)
                        if not o.item and not ctx.is_party[u] then o.race = raceName(me) end
                        local p = V.pos(me)
                        if p then o.pos = pos1(p); tactical(ctx, u, p, o) end
                        pcall(function() if m.Initiative then o.initiative = m.Initiative end end)
                        local h = health(me)
                        if h and h.max_hp and h.max_hp > 0 and (ctx.is_party[u] or not ctx.rules.HIDE_NPC_HP) then
                            o.hp_pct = S.hp_pct(h.hp, h.max_hp)
                        end
                        if tb then
                            pcall(function()
                                -- A turn group (party members with equal initiative)
                                -- is current together; a member that already ended
                                -- keeps IsActiveCombatTurn with RequestedEndTurn set.
                                local pending = BG3AI.Guard and BG3AI.Guard.end_pending and BG3AI.Guard.end_pending(u)
                                -- A25 (S088): another side's turn has started since this member's: not current,
                                -- whatever its flags show (Guard.turn_moved_on)
                                local over = ctx.is_party[u] and BG3AI.Guard and BG3AI.Guard.turn_moved_on
                                    and BG3AI.Guard.turn_moved_on(u)
                                -- S087: a stunned (INCAPACITATED) member's open turn passes by itself (Guard.turn_passes)
                                local passes = tb.IsActiveCombatTurn and ctx.is_party[u] and not tb.RequestedEndTurn
                                    and not pending and not over and BG3AI.Guard and BG3AI.Guard.turn_passes
                                    and BG3AI.Guard.turn_passes(u)
                                if passes then o.passes = passes; pending = true end
                                if tb.IsActiveCombatTurn and not over then
                                    o.is_current = true
                                    if tb.RequestedEndTurn or pending then
                                        o.ended = true
                                        ended_current = ended_current or u
                                    else
                                        out.turn_group = out.turn_group or {}
                                        table.insert(out.turn_group, u)
                                        out.current = out.current or u
                                    end
                                end
                                if tb.ActedThisRoundInCombat then o.acted = true end
                                if ctx.is_party[u] and tb.CanActInCombat and not tb.ActedThisRoundInCombat
                                    and not (tb.IsActiveCombatTurn and (tb.RequestedEndTurn or pending)) then
                                    o.can_act = true
                                    out.party_can_act = out.party_can_act or {}
                                    table.insert(out.party_can_act, u)
                                end
                            end)
                        end
                        if truthy(osi(Osi.IsDead, u)) then o.dead = true end
                        -- the game's own downed state (bridge turn.is_downed), never a rounded hp_pct
                        if ctx.is_party[u] and not o.dead and (truthy(osi(Osi.HasActiveStatus, u, "DOWNED"))
                            or (h and h.hp == 0)) then
                            o.downed = true
                        end
                        if ctx.is_party[u] then
                            o.light_in_hand = heldLight(u)
                            local Gd = BG3AI.Guard
                            if Gd and Gd.disabling then
                                local okd, d = pcall(Gd.disabling, u)
                                if okd then o.disabled = d end
                            end
                        end
                        o.concentration = concentration(me)
                        out.order[#out.order + 1] = o
                    end
                end
            end
        end
    end)
    -- No count or flag for members that fail the gate (audit C8): it said "something hidden is in this fight"
    -- (an undiscovered trap, a sneaking enemy) and later "it is gone".
    out.current = out.current or ended_current
    return out
end

------------------------------------------------------------------------
-- Journal (quest prototypes parsed once from game data, state via Osiris)
------------------------------------------------------------------------

S.quests = S.quests or nil

local function loadQuests()
    if S.quests then return S.quests end
    local ok, s = pcall(Ext.IO.LoadFile, "Mods/GustavDev/Story/Journal/quest_prototypes.lsx", "data")
    if not ok or not s then S.quests = {}; return S.quests end
    local quests, pos = {}, 1
    while true do
        local qs = s:find('<node id="Quest">', pos, true)
        if not qs then break end
        local qe = s:find('<node id="Quest">', qs + 10, true) or (#s + 1)
        local block = s:sub(qs, qe - 1)
        local q = {
            id = block:match('id="QuestID" type="FixedString" value="([^"]*)"'),
            title = block:match('id="QuestTitle" type="TranslatedString" handle="([^"]*)"'),
            category = block:match('id="CategoryID" type="FixedString" value="([^"]*)"'),
            parent = block:match('id="ParentQuestID" type="FixedString" value="([^"]*)"'),
            visible = block:match('id="QuestVisiblity" type="bool" value="([^"]*)"') ~= "false",
            steps = {},
        }
        for st in block:gmatch('<node id="QuestStep">(.-)</node>') do
            q.steps[#q.steps + 1] = {
                id = st:match('id="ID" type="FixedString" value="([^"]*)"'),
                desc = st:match('id="Description" type="TranslatedString" handle="([^"]*)"'),
            }
        end
        if q.id then quests[#quests + 1] = q end
        pos = qe
    end
    S.quests = quests
    return quests
end
S.loadQuests = loadQuests

-- Journal cache. A full walk calls QuestIsAccepted on every quest prototype
-- (~100 ms of Osiris calls; live get_journal took 143 ms) and get_journal,
-- look and the runner's progress poll all read it. Quest listeners bump
-- S.questGen (Navigation's active-objective list keys on it too); the cache
-- also expires after JOURNAL_TTL_MS, a backstop for anything they miss.
S.questGen = S.questGen or 0
S.JOURNAL_TTL_MS = 30000
function S.bumpQuests() S.questGen = (S.questGen or 0) + 1 end
for _, ev in ipairs({ { "QuestAccepted", 2 }, { "QuestUpdateUnlocked", 3 }, { "QuestClosed", 1 },
    { "SavegameLoaded", 0 }, { "LevelGameplayStarted", 2 } }) do
    if Ext.Osiris then pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function() S.bumpQuests() end) end
end

-- A sub-quest's steps are unlocked under its PARENT quest (probe S032: Find
-- Ketheric Thorm's Relic = GLO_Moonrise_SUB_Relic: QuestUpdateIsUnlocked(who,
-- sub, step) = nil, (who, "GLO_Moonrise", step) = 1). Asking only the sub-quest
-- left every sub-quest without steps, objectives and map markers (S030/S031: the
-- agent read the Gauntlet's gem markers off the map screen by hand).
function S.stepUnlocked(who, q, stepId)
    if truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.id, stepId)) then return true end
    return q.parent ~= nil and q.parent ~= "" and truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.parent, stepId)) or false
end

local journalWalk
function S.journal(ctx)
    local who = ctx.party[1]
    local now = Ext.Utils.MonotonicTime()
    local c = S.journalCache
    if c and c.who == who and c.gen == S.questGen and now - c.t < S.JOURNAL_TTL_MS then return c.list end
    local list = journalWalk(who)
    S.journalCache = { who = who, gen = S.questGen, t = now, list = list }
    return list
end

journalWalk = function(who)
    local out = {}
    for _, q in ipairs(loadQuests()) do
        -- Open = accepted OR any journal step unlocked: the Act 2 main quest SHA_Nightsong ("Find the Nightsong"),
        -- the companions' own quests (Daughter of Darkness, The Wizard of Waterdeep, ...) have unlocked steps the
        -- journal shows while QuestIsAccepted stays 0 (probe 23:3x: 10 such quests; S030/S031 the agent read the
        -- Gauntlet's markers off the map by hand). A full step scan of 167 quests costs ~9 ms live.
        local steps = {}
        if q.visible then
            for _, st in ipairs(q.steps) do
                if st.id and S.stepUnlocked(who, q, st.id) then
                    local text = U.loca(st.desc)
                    if text then steps[#steps + 1] = text end
                end
            end
        end
        if q.visible and (#steps > 0 or truthy(osi(Osi.QuestIsAccepted, who, q.id))) then
            local entry = { title = U.loca(q.title) or q.id, category = q.category ~= "" and q.category or nil, steps = steps }
            if q.parent and q.parent ~= "" then entry.parent = q.parent end
            entry.id = q.id
            if truthy(osi(Osi.QuestIsClosed, q.id)) then entry.done = true end
            out[#out + 1] = entry
        end
    end
    -- Resolve sub-quest parent ids to titles (ids stay internal).
    local titles = {}
    for _, e in ipairs(out) do titles[e.id] = e.title end
    for _, e in ipairs(out) do
        if e.parent then e.parent = titles[e.parent] end
        e.id = nil
    end
    return out
end

------------------------------------------------------------------------
-- Map
------------------------------------------------------------------------

local function prettyWaypoint(id)
    local s = tostring(id):gsub("^WAYP_", "")
    local area, rest = s:match("^(%u+)_(.+)$")
    rest = (rest or s):gsub("_", " "):gsub("(%l)(%u)", "%1 %2")
    return rest, area
end

function S.map(ctx)
    local host = ctx.party[1]
    local lvl = osi(Osi.GetRegion, host)
    local out = { level = lvl, region = S.REGION_NAMES[lvl] or lvl, waypoints = {} }
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "PartyWaypoints")
    if ok and list and list[1] then
        pcall(function()
            -- Set-like container: ipairs yields booleans, pairs yields the entries.
            for _, w in pairs(list[1].PartyWaypoints.Waypoints) do
                local name, area = prettyWaypoint(w.Name)
                local real = BG3AI.Nav and BG3AI.Nav.waypointName(tostring(w.Name))
                -- id/level stay internal (fast_travel takes the name); region is what the map shows.
                out.waypoints[#out.waypoints + 1] = { id = tostring(w.Name), name = real or name, area = area,
                    level = tostring(w.Level), region = S.REGION_NAMES[tostring(w.Level)] or tostring(w.Level),
                    here = tostring(w.Level) == lvl or nil }
            end
        end)
    end
    return out
end

------------------------------------------------------------------------
-- Nearby
------------------------------------------------------------------------

-- At most `limit` items, nearest first, but no name takes more than
-- LIMIT_PER_NAME slots while other names are left out (S026 Grand Mausoleum:
-- 25 Candles within 12 m filled the 40 slots and pushed the puzzle Buttons out).
S.LIMIT_PER_NAME = 3
function S.limitItems(items, limit)
    if #items <= limit then return items end
    local seen, pick, rest = {}, {}, {}
    for _, it in ipairs(items) do
        local k = tostring(it.name) .. "|" .. tostring(it.kind)
        seen[k] = (seen[k] or 0) + 1
        if seen[k] <= S.LIMIT_PER_NAME and #pick < limit then pick[#pick + 1] = it else rest[#rest + 1] = it end
    end
    for _, it in ipairs(rest) do
        if #pick >= limit then break end
        pick[#pick + 1] = it
    end
    table.sort(pick, function(a, b) return a.d < b.d end)
    return pick
end

-- Generic puzzle fixtures get the visible thing they belong to: "under
-- Moonrise Towers" (the painting above a Button). What a player sees on
-- screen: the button sits right under the named painting.
S.FIXTURE_NAMES = { Button = true, Lever = true, Switch = true, Handle = true, ["Pressure Plate"] = true,
    ["Marble Plate"] = true, Keyhole = true, Valve = true, Pedestal = true, Socket = true }
S.FIXTURE_NEAR_M = 3.0 -- live 11:3x: the flush button under General (a 4 m painting helper) went untagged at 2 m
function S.fixtureRelation(it, all)
    if not S.FIXTURE_NAMES[it.name] then return nil end
    -- Landmarks only: a lookalike (3+ Candles) names nothing. Something above
    -- the fixture (a painting, a statue) wins over a thing beside it.
    local count = {}
    for _, o in ipairs(all) do if o.name then count[o.name] = (count[o.name] or 0) + 1 end end
    local best, bs
    for _, o in ipairs(all) do
        if o ~= it and o.name and o.name ~= it.name and not S.FIXTURE_NAMES[o.name] and count[o.name] < 3
            and o.name ~= "Object" and o.name ~= "Unknown" then
            local dx, dz = o.p[1] - it.p[1], o.p[3] - it.p[3]
            local h = math.sqrt(dx * dx + dz * dz)
            local score = h - ((o.p[2] - it.p[2] > 0.8) and 1.0 or 0)
            if h <= S.FIXTURE_NEAR_M and (not bs or score < bs) then best, bs = o, score end
        end
    end
    if not best then return nil end
    local dy = best.p[2] - it.p[2]
    local rel = dy > 0.8 and "under" or (dy < -0.8 and "above" or "by")
    return rel .. " " .. best.name
end

-- Party members standing on a floor plate at p (they press it), or nil. vis V25, S084 04:16-04:21: the vault's code
-- reset ("Reset (red)") while companions shuffled onto plates; the agent read that from screenshots. Radius as
-- Actions.lua's plate walks: the AI bounds clamped to 0.4-1.6 m, else 0.8 m.
function S.plateOn(ctx, p, e)
    local b = V.bounds(e)
    local r = math.min(math.max(b and b.radius or 0.8, 0.4), 1.6)
    local on
    for _, ob in ipairs(ctx.observers) do
        local q = ob.pos
        if q and math.sqrt((q[1] - p[1]) ^ 2 + (q[3] - p[3]) ^ 2) <= r and math.abs(q[2] - p[2]) <= 1.0 then
            on = on or {}
            on[#on + 1] = ob.uuid
        end
    end
    return on
end

function S.nearby(ctx, args)
    local radius = tonumber(args.radius) or 25
    local kinds
    if type(args.kinds) == "table" then
        kinds = {}
        for _, k in ipairs(args.kinds) do kinds[k] = true end
    end
    local center = ctx.observers[1] and ctx.observers[1].pos
    local active = selectedCharacter(ctx.party)
    for _, o in ipairs(ctx.observers) do if o.uuid == active and o.pos then center = o.pos end end
    if not center then return { characters = {}, items = {} } end
    local out = { characters = {}, items = {} }
    local shown = {}

    if not kinds or kinds.characters then
        do
            for _, row in ipairs(V.charactersNear(center, radius)) do
                local e, p, d = row[1], row[2], row[3]
                local u = V.entityUuid(e)
                if u and not ctx.is_party[u] and V.characterVisible(ctx, u, e, p) then
                    local o = { uuid = u, name = V.name(u), relation = relation(ctx, u), pos = pos1(p), dist = round(d, 1),
                        statuses = V.statuses(e) }
                    if #o.statuses == 0 then o.statuses = nil end
                    local dead = truthy(osi(Osi.IsDead, u))
                    if dead then o.dead = true end
                    local h = health(e)
                    if h and not dead and not ctx.rules.HIDE_NPC_HP then o.hp, o.max_hp = h.hp, h.max_hp end
                    if inCombat(u) then o.in_combat = true end
                    if not dead and o.relation ~= "hostile" and (truthy(osi(Osi.GetHasDefaultDialog, u)) or truthy(osi(Osi.GetHasOsirisDialog, u))) then
                        o.can_talk = true
                    end
                    -- a merchant (the Trader tag), as Referee.lua's kills: Osi.CanTrade held for the windmill goblins,
                    -- Barth, Arka ... (diverse-2026-10-25). Not hostile: a foe offers no dialogue to trade from.
                    if not dead and o.relation ~= "hostile" and comp(e, "Trader") then o.can_trade = true end
                    if not dead and comp(e, "HasExclamationDialog") then o.wants_talk = true end  -- the "!" over them
                    tactical(ctx, u, p, o)
                    out.characters[#out.characters + 1] = o
                    shown[u] = true
                    V.remember(u, o.name, p, o.relation)
                end
            end
        end
        table.sort(out.characters, function(a, b) return a.dist < b.dist end)
    end

    if not kinds or kinds.items or kinds.door or kinds.container or kinds.item or kinds.ladder or kinds.usable or kinds.trap then
        local limit = tonumber(args.item_limit) or 40
        local cands = V.itemsAround(center, radius)
        local items = {}
        for _, u in ipairs(cands) do
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p then
                local d = U.dist(p, center)
                if d <= radius then
                    local vis, kind = V.itemVisible(ctx, u, e, p)
                    -- A discovered standalone trap (a vent, a tripwire: kind "object") is a trap here, before the
                    -- filter: S064/S065 get_nearby kinds=trap said "Nothing visible" 3-5 m from a spotted Sanguine
                    -- Vent (and the plain listing dropped it as scenery). Floor plates stay plates (below).
                    if vis and kind == "object" and truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u))
                        and not V.PLATE_NAMES[V.name(u) or ""] then
                        kind = "trap"
                    end
                    -- "object" = non-pickup, non-usable scenery-like interactables; only on request.
                    if vis and ((not kinds and (kind ~= "object" or S.FIXTURE_NAMES[V.name(u)])) or (kinds and (kinds[kind] or (kinds.items and kind ~= "object")))) then
                        items[#items + 1] = { u = u, e = e, p = p, d = d, kind = kind }
                    end
                end
            end
        end
        table.sort(items, function(a, b) return a.d < b.d end)
        for _, it in ipairs(items) do it.name = V.name(it.u) end
        local all = items
        items = S.limitItems(items, limit)
        if #all > #items then out.more_items = #all - #items end
        -- Puzzle landmarks (the painting over a Button): their unnamed glow is the puzzle state.
        local near, landmark = {}, {}
        for _, it in ipairs(items) do
            near[it] = S.fixtureRelation(it, all)
            if near[it] then landmark[near[it]:gsub("^%a+ ", "")] = true end
        end
        for _, it in ipairs(items) do
            local u, e = it.u, it.e
            local o = { uuid = u, name = it.name, kind = it.kind, pos = pos1(it.p), dist = round(it.d, 1) }
            if o.name == "Object" or o.name == "Unknown" then goto continue end
            o.near = near[it]
            o.effect = V.itemEffects(e, landmark[o.name] or S.FIXTURE_NAMES[o.name])
            if args.item_los then tactical(ctx, u, it.p, o) end
            if S.lockRead(u) then o.locked = true end
            if it.kind == "door" then
                -- S054 05:54:51: get_nearby said "Control Centre <door> 4.5m"; the door stood open, interact closed
                -- it (a scene started) and the agent had to reopen it. A player sees whether a door is open.
                -- (HasOpened = ever opened: meaningless on a door.)
                local op = osi(Osi.IsOpened, u)
                if op ~= nil then
                    if truthy(op) then o.open = true else o.closed = true end
                end
            elseif comp(e, "HasOpened") then o.opened = true end
            if comp(e, "IsOwned") or comp(e, "InventoryIsOwned") then o.owned = true end
            if truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u)) then
                o.trap = { armed = truthy(osi(Osi.IsTrapArmed, u)) }
                o.kind = it.kind == "object" and "trap" or it.kind
            end
            if it.kind == "object" and V.PLATE_NAMES[o.name] then
                -- a floor plate is a puzzle piece, listed as one (S084: 7 of the vault's 9 code plates were "scenery
                -- objects not listed"), with who of the party stands on it
                o.kind = "plate"
                o.on = S.plateOn(ctx, it.p, e)
            end
            pcall(function() if e.ServerItem.StoryItem then o.story = true end end)
            -- the hover shows a breakable thing's HP (S044 23:51: "Membrane (6 HP)" blocking the colony tunnel was
            -- listed only as "Viscera <usable>"; the agent attacked it for 0, then clicked it by hand)
            if it.kind == "usable" or it.kind == "door" then
                local h = health(e)
                if h and type(h.max_hp) == "number" and h.max_hp > 0 then o.hp, o.max_hp = h.hp, h.max_hp end
            end
            out.items[#out.items + 1] = o
            ::continue::
        end
    end

    local mem = V.recall(shown, center, radius * 2)
    if #mem > 0 then out.remembered = mem end
    return out
end

------------------------------------------------------------------------
-- Preview (what the targeting tooltip would show, cheaply and fairly)
------------------------------------------------------------------------

function S.preview(ctx, args)
    local char, spell, target = args.char, args.spell, args.target
    if not (char and spell and ctx.is_party[char]) then error("need char (party member) and spell", 0) end
    local info = spellInfo(spell)
    if not info then return { unsupported = true } end
    local ce = V.entity(char)
    local out = { spell = spell, name = info.name }
    local rnum = tonumber(info.range)
    local melee = false
    if not rnum and type(info.range) == "string" and info.range:find("WeaponRange") then
        -- Live: attacks have TargetRadius "MeleeMainWeaponRange"/"RangedMainWeaponRange" (Range 0).
        rnum = weaponRanges(ce)[info.range] or (info.range:find("Melee") and 1.5 or nil)
        melee = info.range:find("Melee") ~= nil
    elseif rnum and rnum > 0 and rnum <= 3 then
        melee = true
    end
    if rnum then info = setmetatable({ range = rnum }, { __index = info }) end
    if type(info.range) == "number" then out.range = info.range elseif info.range then out.range = tostring(info.range) end
    local tpos
    if target then
        local te = V.entity(target)
        if not te then error("unknown target", 0) end
        tpos = V.pos(te)
        local visible = comp(te, "ServerCharacter") and V.characterVisible(ctx, target, te, tpos)
            or (comp(te, "ServerItem") and V.itemVisible(ctx, target, te, tpos))
        if not visible then return { visible = false } end
        out.distance = round(U.dist(V.pos(ce), tpos), 1)
        -- the guard's rule (G.los_ok: an item in sight by a ray to its body, S083 doors/walls)
        local G = BG3AI.Guard
        if G and G.los_ok then
            out.line_of_sight = G.los_ok(char, target, out.distance)
        else
            out.line_of_sight = truthy(osi(Osi.HasLineOfSight, char, target))
        end
        if type(info.range) == "number" then
            -- The guard's rule (G.check_cast): melee is edge to edge (both AIBounds.Stand radii).
            local slack = melee and (standRadius(ce) + standRadius(te)) or 0.5
            out.in_range = out.distance <= info.range + slack
        end
    elseif type(args.pos) == "table" then
        out.distance = round(U.dist(V.pos(ce), args.pos), 1)
        if type(info.range) == "number" then out.in_range = out.distance <= info.range + 0.5 end
    end
    -- Save DC / spell attack bonus as the tooltip shows them (8 + PB + mod).
    local tas = info.tooltip_attack_save
    if type(tas) == "string" and tas ~= "" then
        local st = comp(ce, "Stats")
        local pb, castAbility, abil = 0, nil, abilityBlock(ce) or {}
        pcall(function() pb = st.ProficiencyBonus; castAbility = tostring(st.SpellCastingAbility) end)
        local castMod = castAbility and ABBR[castAbility] and mod(abil[ABBR[castAbility]]) or 0
        for part in tas:gmatch("[^;]+") do
            if ABBR[part] then
                out.save = { ability = part, dc = 8 + pb + castMod }
            elseif part == "SpellAttack" or part == "RangedSpellAttack" or part == "MeleeSpellAttack" then
                out.attack_bonus = pb + castMod
            elseif part:find("WeaponAttack") then
                out.weapon_attack = true
            end
        end
    end
    out.hit_chance = nil -- computed by the client targeting UI only; not available server-side
    return out
end

S.inner.itemView = itemView
