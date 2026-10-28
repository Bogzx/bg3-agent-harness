-- Guard, part 3 of 4 (Guard.lua loads the parts in order; split for the 1000-line cap):
-- spell entries, preparation, known spells, container variants, item cooldowns.
local U = BG3AI.Util
local G = BG3AI.Guard
local I = G.inner
local b = I.b

------------------------------------------------------------------------
-- Spell validation (for the guarded Osiris cast path)
------------------------------------------------------------------------

local function spell_entries(char)
    local e = G.entity(char)
    local sb = e and U.comp(e, "SpellBook")
    local out = {}
    if not sb then return out end
    pcall(function()
        for _, s in ipairs(sb.Spells) do
            out[#out + 1] = s
        end
    end)
    return out, e
end

-- Prepared spells (A4, 2026-10-17). A prepared caster's SpellBook holds its whole class list: S065's action list
-- showed Shadowheart every level-1 cleric spell. SpellBookPrepares.PreparedSpells (bg3se SpellMetaId:
-- OriginatorPrototype, SourceType, no Prototype) names the prepared ones, and each SpellBook entry says whether it
-- needs preparing (PrepareType AlwaysPrepared / RequiresPreparation / Unknown). The old check read p.Prototype
-- first: bg3se raises "Property does not exist" there, the pcall swallowed it, and every class spell was castable
-- (Claude's Bless in S065, with no Bless on his hotbar).
-- The set of prepared OriginatorPrototypes, or false when the component can't be read.
function G.prepared_ids(e)
    local prep = e and U.comp(e, "SpellBookPrepares")
    if not prep then return false end
    local set = {}
    local ok = pcall(function()
        for _, p in ipairs(prep.PreparedSpells) do set[tostring(p.OriginatorPrototype)] = true end
    end)
    return ok and set
end

-- The spell a SpellBook entry stands for: its OriginatorPrototype (an upcast entry "Target_Bless_2" has Prototype
-- Target_Bless_2 and OriginatorPrototype Target_Bless; live L4: Claude's book holds level-1 spells x3, level-2 x2),
-- else its Prototype. nil when neither reads.
local function originator(entry) return entry.Id.OriginatorPrototype end
local function prototype(entry) return entry.Id.Prototype end
local function prepare_type(entry) return tostring(entry.PrepareType) end
function G.entry_id(entry)
    local ok, id = pcall(originator, entry)
    if not ok or id == nil or tostring(id) == "" then ok, id = pcall(prototype, entry) end
    return ok and id ~= nil and tostring(id) or nil
end

-- The container a spell is a variant of (Target_Command_Halt -> Target_Command_Container), else nil. Live L4: each
-- variant sits in the book as its own RequiresPreparation entry, while SpellBookPrepares lists only the container.
function G.container_of(id)
    local f = G.spell_facts(id)
    return f and f.box or nil
end

-- A SpellBook entry that needs preparing and isn't prepared (prepared = G.prepared_ids). A container's variant
-- counts as prepared with its container. An unreadable prepared list counts as nothing prepared: refuse rather than
-- cast a spell the player could not.
local function unprepared_id(id, prepared)
    if not prepared or id == nil then return true end
    if prepared[id] then return false end
    local box = G.container_of(id)
    return not (box and prepared[box])
end

function G.unprepared(entry, prepared)
    local ok, kind = pcall(prepare_type, entry)
    if not ok or kind ~= "RequiresPreparation" then return false end
    return unprepared_id(prepared and G.entry_id(entry), prepared)
end

-- Spell preparation (A5, 2026-10-17). What the spellbook's prepare page shows: the class spells that need preparing
-- (prepared or not), the always-prepared ones (domain/oath) and the limit. One row per spell (G.entry_id: upcast
-- entries merge; a container's variants are not rows). The limit (A7, live L4): Stats.MaxPreparedSpells read 9 for
-- Claude while his spellbook page said PreparedSpellsStat 12/12 with 12 prepared. The page's number is per class
-- book; the server keeps per-class counts in SpellBookPrepares.ClassPreparedSpellCount (bg3se field_30,
-- HashMap<class Guid, int>), so that sum is the limit, MaxPreparedSpells only when it doesn't read (GUESS P1:
-- the map holds the page's Max; acceptance act "prepare_spells limit = the page's (A7, read-only)" checks it).

-- What the spellbook page's cell shows for a spell (Client/Spellbook.lua matches cells by it: live L4, a cell has no
-- Name and its ActionId reads "Sentinel"): the stats Icon, level, school, concentration and the main cost as the VM
-- spells it (MainCost Action / BonusAction / BothActions).
local function cell_look(st)
    local look = {}
    local function get(k)
        local ok, v = pcall(function() return st[k] end)
        return ok and v ~= nil and v or nil
    end
    look.icon = get("Icon") and tostring(get("Icon")) or nil
    look.school = get("SpellSchool") and tostring(get("SpellSchool")) or nil
    local flags = get("SpellFlags")
    local conc = false
    if type(flags) == "table" then
        for _, f in ipairs(flags) do if f == "IsConcentration" then conc = true end end
    elseif flags then
        conc = tostring(flags):find("IsConcentration", 1, true) ~= nil
    end
    look.conc = conc
    local costs = tostring(get("UseCosts") or "")
    local act = costs:find("^ActionPoint:") or costs:find("[;%s]ActionPoint:")
    local bonus = costs:find("BonusActionPoint:", 1, true)
    look.cost = (act and bonus) and "BothActions" or act and "Action" or bonus and "BonusAction" or nil
    return look
end

-- What prep_info and the cast checks read from a spell's stats entry, per spell id: its container (SpellContainerID),
-- shown name, level and page look (cell_look). nil when the entry doesn't read (not kept). A stats entry doesn't
-- change in a running game, and bg3le's Ext.Stats.Get is Lua (a fresh proxy per call, each attribute a lookup plus a
-- decode): act.prepared rides in every get_status and walked every caster's whole book through ~2 gets and ~8
-- attribute reads per spell each time (V12: act.prepared alone 338 ms median in S068-S070).
G.spellFacts = {}
function G.spell_facts(id)
    local f = G.spellFacts[id]
    if f then return f end
    local ok, st = pcall(Ext.Stats.Get, id)
    if not ok or not st then return nil end
    local okc, c = pcall(function() return st.SpellContainerID end)
    c = okc and c and tostring(c) or ""
    local okn, h = pcall(function() return st.DisplayName end)
    local okl, l = pcall(function() return st.Level end)
    f = { box = c ~= "" and c or nil, name = (okn and U.loca(h)) or id, level = okl and tonumber(l) or nil,
          look = cell_look(st) }
    G.spellFacts[id] = f
    return f
end

local function class_limit(e)
    local prep = e and U.comp(e, "SpellBookPrepares")
    local sum, n = 0, 0
    pcall(function()
        for _, v in pairs(prep.ClassPreparedSpellCount) do
            if tonumber(v) then sum, n = sum + tonumber(v), n + 1 end
        end
    end)
    return n > 0 and sum or nil
end

-- A class's shown name per ClassUUID (static data: it never changes in a running game; V12 prep_info read it with a
-- StaticData lookup and a loca read per class on every get_status). A name that doesn't read isn't kept.
G.classNames = {}
local function class_names(e)
    local out = {}
    local c = e and U.comp(e, "Classes")
    pcall(function()
        for _, cl in ipairs(c.Classes) do
            local id = tostring(cl.ClassUUID)
            local n = G.classNames[id]
            if not n then
                local ok, r = pcall(Ext.StaticData.Get, cl.ClassUUID, "ClassDescription")
                local okd, h = pcall(function() return r.DisplayName.Handle.Handle end)
                n = (okd and U.loca(h)) or (ok and r and r.Name)
                if n then n = tostring(n) G.classNames[id] = n end
            end
            if n then out[#out + 1] = n end
        end
    end)
    return out
end
G.class_names = class_names

-- { prepares, read, max, count, prepared, unprepared, always = { {id, name, level} }, classes }
function G.prep_info(char)
    local entries, e = spell_entries(char)
    local set = G.prepared_ids(e)
    local info = { prepared = {}, unprepared = {}, always = {}, classes = class_names(e), read = set ~= false }
    local seen = {}
    -- Each entry's PrepareType and id are read once, the id only for an entry that prepares (V12: the warm read of
    -- the party's books, 19 ms live N7, read both twice through G.unprepared and the id of every action and cantrip).
    for _, s in ipairs(entries) do
        local okk, kind = pcall(prepare_type, s)
        local id = okk and (kind == "RequiresPreparation" or kind == "AlwaysPrepared") and G.entry_id(s) or nil
        if id and not seen[id] and not G.container_of(id) then
            seen[id] = true
            local f = G.spell_facts(id)
            local level = f and f.level
            local row = { id = id, name = f and f.name or id, level = level, look = f and f.look }
            if kind == "AlwaysPrepared" then
                if (level or 0) > 0 then info.always[#info.always + 1] = row end
            elseif unprepared_id(id, set) then
                info.unprepared[#info.unprepared + 1] = row
            else
                info.prepared[#info.prepared + 1] = row
            end
        end
    end
    local function order(a, b)
        if (a.level or 0) ~= (b.level or 0) then return (a.level or 0) < (b.level or 0) end
        return a.name < b.name
    end
    table.sort(info.prepared, order)
    table.sort(info.unprepared, order)
    table.sort(info.always, order)
    local okm, max = pcall(function() return e.Stats.MaxPreparedSpells end)
    info.stats_max = okm and tonumber(max) or nil
    info.class_max = class_limit(e)
    info.max = info.class_max or info.stats_max
    info.count = #info.prepared
    info.prepares = #info.prepared + #info.unprepared > 0
    return info
end

-- A spell named by id or shown name (any case) in prep_info: row, "prepared" | "unprepared" | "always".
function G.prep_find(info, want)
    local w = tostring(want or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    for _, kind in ipairs({ "prepared", "unprepared", "always" }) do
        for _, r in ipairs(info[kind]) do
            if r.id:lower() == w or r.name:lower() == w then return r, kind end
        end
    end
    return nil
end

local function names(rows)
    local out = {}
    for _, r in ipairs(rows) do out[#out + 1] = r.name end
    return table.concat(out, ", ")
end

-- The change the spellbook will make, checked as the UI checks it, or a refusal: in combat, a class that doesn't
-- prepare, a spell not in the book, over the limit (naming what is prepared now).
function G.prep_plan(char, add, remove)
    char = G.actor(char)
    local who = U.loca(G.q("GetDisplayName", char)) or "this character"
    if G.in_combat(char) then
        G.refuse("spells are prepared out of combat (the spellbook locks preparing during a fight); after the fight,"
            .. " or after a long rest")
    end
    G.not_in_dialogue(char)
    local info = G.prep_info(char)
    local cls = table.concat(info.classes, "/")
    if not info.prepares then
        G.refuse(who .. (cls ~= "" and (" (" .. cls .. ")") or "") .. " doesn't prepare spells: sorcerers, warlocks,"
            .. " bards and rangers know a fixed list that changes only at level up (level_up); clerics, druids,"
            .. " paladins and wizards prepare")
    end
    if not info.read then G.refuse("the prepared spell list didn't read; nothing was changed") end
    local adds, removes, notes, taken = {}, {}, {}, {}
    for _, want in ipairs(remove or {}) do
        local r, kind = G.prep_find(info, want)
        if not r then
            G.refuse(tostring(want) .. " is not in " .. who .. "'s spellbook (nothing to unprepare)")
        elseif kind == "always" then
            notes[#notes + 1] = r.name .. " is always prepared (it can't be unprepared)"
        elseif kind == "unprepared" then
            notes[#notes + 1] = r.name .. " was not prepared"
        elseif not taken[r.id] then
            taken[r.id] = true
            removes[#removes + 1] = r
        end
    end
    for _, want in ipairs(add or {}) do
        local r, kind = G.prep_find(info, want)
        if not r then
            G.refuse(who .. " doesn't know " .. tostring(want) .. ": not in their spellbook (only "
                .. (cls ~= "" and cls or "their class's") .. " spells of a level they can cast)")
        elseif kind == "always" then
            notes[#notes + 1] = r.name .. " is always prepared already"
        elseif kind == "prepared" and not taken[r.id] then
            notes[#notes + 1] = r.name .. " is prepared already"
        elseif not taken["+" .. r.id] then
            taken["+" .. r.id] = true
            adds[#adds + 1] = r
        end
    end
    -- How many other book spells the page shows alike (same level, school, concentration, cost and prepared state):
    -- with no readable icon the page can tell a spell's cell apart only when that is 0 (Client/Spellbook.lua).
    local function look_key(r)
        local l = r.look or {}
        return table.concat({ tostring(r.level), tostring(l.school), tostring(l.conc), tostring(l.cost) }, "|")
    end
    local function twins(r, kind)
        local n = 0
        for _, o in ipairs(info[kind]) do
            if o.id ~= r.id and look_key(o) == look_key(r) then n = n + 1 end
        end
        return n
    end
    for _, r in ipairs(adds) do r.twins = twins(r, "unprepared") end
    for _, r in ipairs(removes) do r.twins = twins(r, "prepared") end
    local after = info.count - #removes + #adds
    if info.max and after > info.max and #adds > 0 then
        G.refuse(string.format("over the limit: %s can prepare %d spells and has %d prepared (%s); remove %d of them"
            .. " in the same call (remove=[...])", who, info.max, info.count, names(info.prepared), after - info.max))
    end
    return { char = char, name = who, classes = info.classes, max = info.max, count = info.count, after = after,
             stats_max = info.stats_max, class_max = info.class_max,
             prepared = info.prepared, always = info.always, add = adds, remove = removes, notes = notes }
end

-- Is the spell on the character's hotbar/spellbook and usable (prepared if it
-- needs preparing)? Returns the matching SpellBook entry or refuses.
function G.known_spell(char, spell)
    local entries, e = spell_entries(char)
    local function find(name)
        for _, s in ipairs(entries) do
            local ok, proto = pcall(function() return s.Id.Prototype end)
            if ok and proto == name then return s end
        end
    end
    local found = find(spell)
    if not found then
        -- A variant of a container spell (Command -> Halt): the spellbook holds
        -- the container; prepared/cooldown checks apply to the container.
        local st = Ext.Stats.Get(spell)
        local parent = st and st.SpellContainerID
        if parent and parent ~= "" then
            found = find(parent)
            if found then spell = parent end
        end
    end
    if not found then
        -- S031 17:04: Gale's Projectile_MainHandAttack "does not have that action": a ranged weapon attack exists
        -- only with a ranged weapon equipped (a melee weapon attack only with a melee one): say which is missing
        if spell == "Projectile_MainHandAttack" or spell == "Projectile_OffhandAttack" then
            G.refuse("the character does not have that action: no ranged weapon equipped (Ranged Attack needs a bow"
                .. " or crossbow in the ranged slot); use a cantrip, Throw, or the melee attack")
        end
        G.refuse("the character does not have that action")
    end

    if G.unprepared(found, G.prepared_ids(e)) then G.refuse("that spell is not prepared") end

    -- Cooldown (once per turn / short rest / long rest)
    local cds = e and U.comp(e, "SpellBookCooldowns")
    if cds then
        local ok, cooling = pcall(function()
            for _, c in ipairs(cds.Cooldowns) do
                if c.SpellId and c.SpellId.Prototype == spell and (c.Cooldown or 0) > 0 then
                    return true
                end
            end
            return false
        end)
        if ok and cooling then G.refuse("that action is on cooldown") end
    end
    local key = G.item_cd_key(char, spell)
    if key and G.item_uses()[key] then
        G.refuse("that action is used up until the next " .. (G.item_uses()[key] == "short" and "short or long rest"
            or "long rest") .. " (its item allows it once per rest)")
    end
    return found
end

-- A27 (S090, the Netherbrain): Gather Your Allies (Shout_END_AllyAbilities_MultipleSummon) is a linked spell
-- container with no ContainerSpells: its ally groups (Target_END_AllyAbilities_WatchSummon …) name it as their
-- SpellContainerID, and only a group's own cast at a ground point summons anyone. UseSpell on the container "cast" it
-- twice and nobody came; the agent picked the group and the ground by hand (31 clicks over 3 tries).
local function spell_flags(st)
    local f = {}
    pcall(function() for _, x in ipairs(st.SpellFlags) do f[x] = true end end)
    return f
end

-- A container with variants but no ContainerSpells list: flagged IsLinkedSpellContainer, in no container itself.
function G.linked_box(st)
    if not st or tostring(st.ContainerSpells or "") ~= "" or tostring(st.SpellContainerID or "") ~= "" then
        return false
    end
    return spell_flags(st).IsLinkedSpellContainer == true
end

-- The variants of `box` char can pick, in book order: ContainerSpells when the stats list them, else char's SpellBook
-- entries whose stats name `box` as their container and its SpellContainer entries filed under it (the field the DD
-- probe read live). A call-allies box with none read falls back to every group in the game data (from = "game data").
function G.box_variants(char, box)
    local st = Ext.Stats.Get(box)
    local out, seen = {}, {}
    local function add(id)
        id = id and tostring(id) or ""
        if id ~= "" and id ~= box and not seen[id] then seen[id] = true; out[#out + 1] = id end
    end
    for id in tostring(st and st.ContainerSpells or ""):gmatch("[^;]+") do add(id) end
    if #out > 0 then return out end
    local e = G.entity(char)
    pcall(function()
        for _, s in ipairs(e.SpellBook.Spells) do
            local id = s.Id.Prototype
            local f = G.spell_facts(id)
            if f and f.box == box then add(id) end
        end
    end)
    pcall(function()
        for _, m in ipairs(e.SpellContainer.Spells) do
            if tostring(m.ContainerSpell) == box then add(m.SpellId.OriginatorPrototype) end
        end
    end)
    if #out == 0 and st and spell_flags(st).CallAlliesSpell then
        local all = G.boxData[box]
        if not all then
            all = {}
            pcall(function()
                for _, id in ipairs(Ext.Stats.GetStats("SpellData")) do
                    local s = Ext.Stats.Get(id)
                    if s and tostring(s.SpellContainerID or "") == box then all[#all + 1] = id end
                end
            end)
            G.boxData[box] = all -- the game data doesn't change in a running game: one walk of every spell
        end
        for _, id in ipairs(all) do add(id) end
        return out, "game data"
    end
    return out
end
G.boxData = {}

-- The statuses a spell's RequirementConditions forbid, when that is all they say ("not HasStatus('X') and not
-- HasStatus('Y')": an ally group's called/consumed marks); nil otherwise.
function G.forbidden_statuses(st)
    local rc = tostring(st and st.RequirementConditions or "")
    local out = {}
    local rest = rc:gsub("not%s+HasStatus%(%s*'([%w_]+)'%s*%)", function(s) out[#out + 1] = s; return "" end)
    if #out == 0 or rest:gsub("and", ""):match("%S") then return nil end
    return out
end

-- The forbidding status char has for `spell` (the group was already called), else nil.
function G.variant_used(char, spell)
    for _, s in ipairs(G.forbidden_statuses(Ext.Stats.Get(spell)) or {}) do
        if b(G.q("HasActiveStatus", char, s)) then return s end
    end
    return nil
end

-- "The Watch, Gur Huntwardens [already called]" for a refusal; the usable ones first. Names only, as the game shows
-- them (S093 fairness leak: the spell and status ids); the id only for a variant the game gives no name.
function G.variant_list(char, ids)
    local free, used = {}, {}
    for _, id in ipairs(ids) do
        local f = G.spell_facts(id)
        local shown = f and f.name or id
        if G.variant_used(char, id) then used[#used + 1] = shown .. " [already called]" else free[#free + 1] = shown end
    end
    for _, x in ipairs(used) do free[#free + 1] = x end
    return table.concat(free, ", ")
end

-- act.cast {which}: the variant of `box` named `which` (its id, its shown name, or a part of the name, any case).
function G.pick_variant(char, box, which)
    local ids, from = G.box_variants(char, box)
    local want = tostring(which):lower():gsub("^%s+", ""):gsub("%s+$", "")
    local exact, part = {}, {}
    for _, id in ipairs(ids) do
        local f = G.spell_facts(id)
        local name = (f and f.name or id):lower()
        if id:lower() == want or name == want then exact[#exact + 1] = id
        elseif #want >= 3 and name:find(want, 1, true) then part[#part + 1] = id end
    end
    local hit = #exact == 1 and exact or (#exact == 0 and part or exact)
    local shown = G.spell_facts(box)
    shown = shown and shown.name or box
    if #ids == 0 then G.refuse(shown .. " has no variants to pick for this character") end
    if #hit ~= 1 then
        G.refuse((#hit == 0 and "no variant '" or "more than one variant matches '") .. tostring(which) .. "' in "
            .. shown .. ": " .. G.variant_list(char, ids) .. (from and " (from the " .. from .. ")" or ""))
    end
    local used = G.variant_used(char, hit[1])
    if used then
        G.refuse(G.spell_facts(hit[1]).name .. " was already called: pick another of "
            .. G.variant_list(char, ids))
    end
    return hit[1]
end

-- Item spells with a once-per-rest-per-item cooldown (Spellcrux Amulet's Spell Slot Restoration): Osi.UseSpell
-- casts them without their item, so the engine records no cooldown. S030: Gale restored spell slots 3 times in a
-- row for free (the agent flagged it as a game bug and stopped). The guard keeps the ledger itself, in
-- PersistentVars so it travels with the save; a rest clears it (Events.lua).
function G.item_cd_key(char, spell)
    local st = type(spell) == "string" and Ext.Stats.Get(spell)
    local cd = st and tostring(st.Cooldown or "") or ""
    if not cd:find("Rest") or not cd:find("PerItem$") then return nil end
    local root = tostring(st.SpellContainerID or "")
    if root == "" then root = spell end
    local u = G.guid(char)
    return u and (u .. ":" .. root) or nil, cd:find("ShortRest") and "short" or "long"
end

function G.item_uses()
    PersistentVars = PersistentVars or {}
    PersistentVars.item_uses = PersistentVars.item_uses or {}
    return PersistentVars.item_uses
end

function G.item_used(char, spell)
    local key, kind = G.item_cd_key(char, spell)
    if key then G.item_uses()[key] = kind end
end

-- kind "long": every entry; "short": the short-rest ones (of char, when given)
function G.item_rest(kind, char)
    local uses = G.item_uses()
    local u = char and G.guid(char)
    for k, v in pairs(uses) do
        if kind == "long" or (v == "short" and (not u or k:sub(1, #u) == u)) then uses[k] = nil end
    end
end

G.inner.spell_flags = spell_flags
