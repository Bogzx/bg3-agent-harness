-- Mirror twins (server context, harness only, refused without BG3AI_ARENA=1), for the mirror-save tool
-- (tools/dev/make_arena_save.py):
-- arena.mirror_spawn {stats, template, race, spots = [[x, y, z] x2], names = [a, b]}: two copies of one root template
--   with one of our stat entries (Shared/ArenaStats.lua). The template's Stats is set to the entry for the two CreateAt
--   calls and set back at once (other spawns of the template stay what they were), refused when the template's race is
--   not `race`. Each copy is made a player (MakePlayer, the host's party XP: live, a copy then reads the host's level)
--   and named with SetStoryDisplayName: the copies of one template share their display-name handle, so scn.rename
--   (Ext.Loca on that handle) would rename both (live check 12 step 8: every copy of the template showed the last
--   name; live check 13: each copy gets its own handle). The name lands after the call, not in its tick (live check
--   13: the copies still read the template's name in the call, their own on the next tick). Nothing else of a copy is
--   read as fact in the spawn tick either (live check 13b: its Race component read empty there): `reads` (StatsId,
--   name, level) is what the copy reads at the call, and arena.setup checks names and StatsId once they land
--   (NAME_WAIT_MS). first = 1 or 2 (default 1): the spot whose copy is made (CreateAt, then MakePlayer and the name)
--   first; arena.setup alternates it per pair by the seed, since the initiative tie-break may follow creation order.
--   -> {twins = [u1, u2] (in spot order), order = [spot indices in creation order], reads}
-- arena.mirror_kit {twins = [u1, u2], race, kit} (a job): the same level-1 choices on both twins, in the same order,
--   by the game's own calls, once both twins read ready (READY_WAIT_MS: race set, items readable, the containers the
--   calls write present; a copy fresh from CreateAt has none of it in its spawn tick, live check 13b) and SETTLE_MS has
--   passed. A finished kit fails, naming it, when a twin holds a passive the kit added twice.
--   kit (make_arena_save.py builds it from the spec's kit and the game data):
--     race = {race, boosts, passives, spells}: the race's level-1 fixed parts, its parent race's rows first (the game
--       applies none to a spawned copy; its Movement boost is the copy's only Movement resource: live check 12 step
--       7); race = the race id they are for, which must be the call's race (refused at the call otherwise); spells =
--       the rows' AddSpells lists' ids (a Drow's Dancing Lights), AddSpell'd like the kit's spell choices
--     player = {passives}: what every player character has and a copy lacks (DeathSavingThrows, whose own boost is
--       DownedStatus(DOWNED): Lae'zel's, live check 12 step 10)
--     ability_bonus = {+2 ability, +1 ability} or nil (SelectAbilityBonus)
--     grants = {boosts, passives, passives_removed, spells}: what a subclass's rows up to the level and the
--       improvements (an ASI's Ability boosts, a feat's own parts) give; the game gives a copy only the class rows'
--     choices = [{kind = skills|expertise|passives|spells, list, n, exact, ids}]: one per Select* selector;
--       every id must be in the game's list (Ext.StaticData SkillList / PassiveList / SpellList), #ids == n when
--       exact, else 1..n (a wizard's prepared spells out of its spellbook selector)
--     items = [{item, root, count, equip}]: TemplateAddTo(root, twin, count); the item shows in an inventory a tick
--       later (live check 12 step 7), then Equip(twin, item, 1, 0, 1) when equip (Equip takes 5 arguments)
--     subclass (uuid): written to the copy's one Classes row and replicated (S1 P2: the game grants nothing for it, the
--       kit's grants do; it reads back on both contexts and held 18 min). A read-back that differs fails the job.
--     spell_abilities = {spell id: ability}: AddSpell takes no ability, so a kit's spell reads None in the book (S4,
--       live 2026-11-06: a Ranger's Cure Wounds) where a real character's row has the ability its progression row gave.
--       A finished kit writes them, a container spell's variants by their originator, and every SpellSet row (Jump,
--       Dash...) to the stats' ability: the game gives a copy's SpellSet rows its class's, a real player's its stats'.
--     unlocks = {spell id: ability}: a kit's spell with upcast rows goes in by Sym.unlock_boost, not AddSpell: AddSpell
--       gives the one id it names, and an AddSpell'd X_2 reads in `prepared` where a character's holds X only (S4, live
--       2026-11-07). The spell's id stays in its list above; spell_abilities names the rows to expect.
--     prepared (spell ids): no game call limits a copy to them (S1 P3: a Cleric copy's book is its class list, all
--       AlwaysPrepared, and nothing removes a spell). The job's result names it as not_applied; a build's reference
--       compare then shows what differs. A kit key nobody reads is refused, so a part can't be dropped without a word.
--   Refused at the call, nothing applied, when a twin is missing or a choice fails its list; the job fails, nothing
--   applied, when a ready twin is not of `race` or already holds items, or a twin isn't ready in READY_WAIT_MS.
--   -> job result {twins, calls, not_applied = [kit keys]}

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local V = BG3AI.Visibility
local U = BG3AI.Util
local Ar = BG3AI.Arena

local M = {}
Ar.Mirror = M
M.SOURCE = "BG3AI_ArenaKit" -- AddBoosts' source id
M.ITEM_WAIT_MS = 5000       -- an added item shows in the inventory within this (else the job fails)
M.TIMEOUT_MS = 60000
M.KIT_EVERY_MS = 250        -- arena.setup reads a mirror's kit jobs this often
-- arena.setup waits this long after the renames for every twin to read its own name. The lag is not measured (live
-- check 13: not in the call's tick, there a minute later); a SetStoryDisplayName lands like an added item, on a later
-- tick, so the item bound (ITEM_WAIT_MS) is the bound here too, and the kits run meanwhile. A name still wrong at the
-- limit fails setup naming the copy and what it reads.
M.NAME_WAIT_MS = 5000
-- A copy fresh from CreateAt reads its components a moment later (live check 13b: Race empty in the spawn tick, set
-- "a moment later"; names by the next tick, 10 ms). Not measured beyond that: the same later-tick bound as items.
M.READY_WAIT_MS = 5000
-- The game rebuilds a copy's passives some time after MakePlayer, and a passive AddPassive already put there comes back
-- on top of itself: arena.setup's kit on the first tick read Script x5, Feat, Stats x7, Script x5 (builds-1 load 1),
-- Fighter AC 17 from Defense twice; the same kit 1.0 s after the spawn read each once, AC 16 (builds-1 0a). No read
-- shows the rebuild's tick or what marks its end, so this is a wait: Setup.SETTLE_MS's bound, over the 1.0 s measured.
M.SETTLE_MS = 2000
M.NULL = "00000000-0000-0000-0000-000000000000"
-- kind -> the game's resource and its list field (bg3se GuidResources.h: SkillList.Skills Array<SkillId>,
-- PassiveList.Passives Array<FixedString>, SpellList.Spells HashSet<FixedString>: set = asked per id, never walked)
M.LISTS = { skills = { "SkillList", "Skills" }, expertise = { "SkillList", "Skills" },
    passives = { "PassiveList", "Passives" }, spells = { "SpellList", "Spells", set = true } }

M.KIT_KEYS = { race = true, player = true, ability_bonus = true, grants = true, choices = true, items = true,
    subclass = true, prepared = true, spell_abilities = true, unlocks = true }
M.NOT_APPLIED = { "prepared" } -- kit keys with no game call (S1 P3)

local function osi(fn, ...) return V.osi(fn, ...) end
local function now_ms() return Ext.Utils.MonotonicTime() end

local function read(fn)
    local ok, v = pcall(fn)
    if ok then return v end
    return nil
end

-- One Osiris call, recorded in calls: {fn, args..., error?}. forms: argument lists tried in order (the first the game
-- accepts); an error after the last form is raised.
local function call(calls, fn, ...)
    local err
    for _, args in ipairs({ ... }) do
        local ok, e = pcall(Osi[fn], table.unpack(args))
        if ok then
            calls[#calls + 1] = { fn, table.unpack(args) }
            return
        end
        err = e
    end
    error(fn .. " failed: " .. tostring(err), 0)
end

local function vec3(v, what)
    if type(v) ~= "table" or #v ~= 3 then error(what .. " must be [x, y, z]", 0) end
    for i = 1, 3 do
        if type(v[i]) ~= "number" then error(what .. " must be [x, y, z]", 0) end
    end
    return v
end

local function two(list, what)
    if type(list) ~= "table" or #list ~= 2 then error(what .. " must list 2", 0) end
    return list
end

function M.spawn(args)
    local stats = args.stats
    if type(stats) ~= "string" or stats:sub(1, #Ar.Stats.PREFIX) ~= Ar.Stats.PREFIX or not Ext.Stats.Get(stats) then
        error("no stat entry " .. tostring(stats) .. " (ours are made at StatsLoaded with BG3AI_ARENA=1)", 0)
    end
    local tpl = read(function() return Ext.Template.GetRootTemplate(args.template) end)
    if not tpl then error("no root template " .. tostring(args.template), 0) end
    local race = read(function() return tostring(tpl.Race) end)
    if type(args.race) ~= "string" or race ~= args.race then
        error(("template %s is of race %s, not %s"):format(args.template, tostring(race), tostring(args.race)), 0)
    end
    local spots, names = two(args.spots, "spots"), two(args.names, "names")
    for i = 1, 2 do
        vec3(spots[i], "spots[" .. i .. "]")
        if type(names[i]) ~= "string" or names[i] == "" then error("names[" .. i .. "] must be a name", 0) end
    end
    if names[1]:lower() == names[2]:lower() then error("the twins need two names", 0) end
    local first = args.first == nil and 1 or args.first
    if first ~= 1 and first ~= 2 then error("first must be 1 or 2 (the spot made first)", 0) end
    local order = { first, 3 - first }
    local host = V.uuid(osi(Osi.GetHostCharacter))
    if not host then error("no host character", 0) end
    local old = tpl.Stats
    tpl.Stats = stats
    local ok, made = pcall(function()
        local out = {}
        for _, i in ipairs(order) do
            local p = spots[i]
            out[i] = V.uuid(Osi.CreateAt(args.template, p[1], p[2], p[3], 0, 0, ""))
            if not out[i] then error("CreateAt made no copy", 0) end
        end
        return out
    end)
    tpl.Stats = old
    if not ok then error(made, 0) end
    local calls, reads = {}, {}
    for _, i in ipairs(order) do
        call(calls, "MakePlayer", { made[i], host })
        call(calls, "SetStoryDisplayName", { made[i], names[i] })
    end
    for i, u in ipairs(made) do
        local e = V.entity(u)
        reads[i] = { uuid = u, stats = read(function() return e.Data.StatsId end), name = V.name(u),
            level = read(function() return e.EocLevel.Level end) }
    end
    return { twins = made, order = order, reads = reads, calls = calls,
        template_stats = read(function() return tpl.Stats end) }
end

-- The game's list for a choice as a membership test: fn(id) -> bool; error, naming the list, when it can't be checked
-- (fail closed). An array field (live check 13c: userdata, its elements the ids, tostring their names) is walked
-- 1..# (U.items) and every element must read; a set field (SpellList.Spells) is asked per id: Spells[id] is true or
-- false (bg3se LuaSetProxy.inl Index = HasElement; live check 13d: walking it read false at every index, forever).
local function listHas(kind, list)
    local what = M.LISTS[kind]
    if not what then error("a choice of kind " .. tostring(kind) .. " (skills, expertise, passives or spells)", 0) end
    local where = ("the game's %s %s"):format(what[1], tostring(list))
    local field = read(function() return Ext.StaticData.Get(list, what[1])[what[2]] end)
    if not field then error(where .. " doesn't read", 0) end
    if what.set then
        return function(id)
            local v = read(function() return field[id] end)
            if type(v) ~= "boolean" then
                error(("%s can't be checked: %s reads %s"):format(where, tostring(id), tostring(v)), 0)
            end
            return v
        end
    end
    local ok, items, n = pcall(U.items, field)
    if not ok then error(where .. " doesn't read: " .. tostring(items), 0) end
    local set = {}
    for k = 1, n do
        local id = items[k]
        if id == nil or id == false then
            error(("%s can't be checked: element %d of %d reads %s"):format(where, k, n, tostring(id)), 0)
        end
        set[tostring(id)] = true
    end
    return function(id) return set[tostring(id)] == true end
end

local function checkChoice(c, i)
    local where = ("choice %d (%s)"):format(i, tostring(c.kind))
    local n, ids = tonumber(c.n), c.ids
    if not n or type(ids) ~= "table" then error(where .. " needs n and ids", 0) end
    if (c.exact and #ids ~= n) or (not c.exact and (#ids < 1 or #ids > n)) then
        error(("%s: %d picked, the game's selector takes %s%d"):format(where, #ids, c.exact and "" or "1..", n), 0)
    end
    local has, seen = listHas(c.kind, c.list), {}
    for _, id in ipairs(ids) do
        if seen[id] then error(where .. ": " .. tostring(id) .. " picked twice", 0) end
        seen[id] = true
        if not has(id) then
            error(("%s: %s is not in the game's list %s"):format(where, tostring(id), tostring(c.list)), 0)
        end
    end
end

-- The copy's subclass field (S1 P2), read back; recorded with the calls.
local function setSubclass(calls, u, id)
    local e = V.entity(u)
    local rows = U.items(e.Classes.Classes)
    if #rows ~= 1 then error(("%s has %d class rows, a copy has one"):format(u, #rows), 0) end
    e.Classes.Classes[1].SubClassUUID = id
    e:Replicate("Classes")
    local now = tostring(e.Classes.Classes[1].SubClassUUID)
    if now ~= id then error(("%s's subclass reads %s after the write of %s"):format(u, now, id), 0) end
    calls[#calls + 1] = { "SubClassUUID", u, id }
end

-- One of the kit's spells into a twin's book.
local function addSpell(calls, u, kit, id)
    local ability = (kit.unlocks or {})[id]
    if ability then
        call(calls, "AddBoosts", { u, Ar.Symmetry.unlock_boost(id, ability), M.SOURCE, u })
    else
        call(calls, "AddSpell", { u, id, 0, 1 })
    end
end

-- The non-item calls for one twin, in the kit's order.
local function applyChoices(calls, u, kit)
    if kit.subclass then setSubclass(calls, u, kit.subclass) end
    local race = kit.race or {}
    if race.boosts and race.boosts ~= "" then call(calls, "AddBoosts", { u, race.boosts, M.SOURCE, u }) end
    for _, p in ipairs(race.passives or {}) do call(calls, "AddPassive", { u, p }) end
    for _, id in ipairs(race.spells or {}) do addSpell(calls, u, kit, id) end
    for _, p in ipairs((kit.player or {}).passives or {}) do call(calls, "AddPassive", { u, p }) end
    local ab = kit.ability_bonus
    if ab then call(calls, "AddBoosts", { u, ("Ability(%s,2);Ability(%s,1)"):format(ab[1], ab[2]), M.SOURCE, u }) end
    local g = kit.grants or {}
    if g.boosts and g.boosts ~= "" then call(calls, "AddBoosts", { u, g.boosts, M.SOURCE, u }) end
    for _, p in ipairs(g.passives or {}) do call(calls, "AddPassive", { u, p }) end
    for _, p in ipairs(g.passives_removed or {}) do call(calls, "RemovePassive", { u, p }) end
    for _, id in ipairs(g.spells or {}) do addSpell(calls, u, kit, id) end
    for _, c in ipairs(kit.choices or {}) do
        if c.kind == "skills" or c.kind == "expertise" then
            local fmt = c.kind == "skills" and "ProficiencyBonus(Skill,%s)" or "ExpertiseBonus(%s)"
            local parts = {}
            for _, id in ipairs(c.ids) do parts[#parts + 1] = fmt:format(id) end
            call(calls, "AddBoosts", { u, table.concat(parts, ";"), M.SOURCE, u })
        elseif c.kind == "passives" then
            for _, id in ipairs(c.ids) do call(calls, "AddPassive", { u, id }) end
        else
            for _, id in ipairs(c.ids) do addSpell(calls, u, kit, id) end
        end
    end
end

-- A book row's ability (spell_abilities by id, by originator, then by the container of either), or nil when the kit
-- says nothing of it. A variant's originator is itself, not its container (Guard.unprepared_id), and the kit names the
-- container: Bestow Curse's 9 variants, Ensnaring Strike's 2.
local function rowAbility(e, s, abilities)
    local id = s.Id
    if tostring(id.SourceType) == "SpellSet" then return tostring(e.Stats.SpellCastingAbility) end
    local proto, origin = tostring(id.Prototype), tostring(id.OriginatorPrototype)
    local box = BG3AI.Guard.container_of(origin) or BG3AI.Guard.container_of(proto)
    return abilities[proto] or abilities[origin] or (box and abilities[box])
end

-- The book's rows set to their abilities, read back and replicated. -> the kit's spells not in the book yet.
local function setAbilities(calls, u, kit)
    local e, abilities, late, wrote = V.entity(u), kit.spell_abilities or {}, {}, 0
    local seen = {}
    for _, s in ipairs(U.items(e.SpellBook.Spells)) do
        seen[tostring(s.Id.Prototype)], seen[tostring(s.Id.OriginatorPrototype)] = true, true
        local ab = rowAbility(e, s, abilities)
        if ab and tostring(s.SpellCastingAbility) ~= ab then
            s.SpellCastingAbility = ab
            local now = tostring(s.SpellCastingAbility)
            if now ~= ab then
                error(("%s's %s reads %s after the write of %s"):format(u, tostring(s.Id.Prototype), now, ab), 0)
            end
            wrote = wrote + 1
        end
    end
    for id in pairs(abilities) do
        if not seen[id] then late[#late + 1] = id end
    end
    if wrote > 0 then
        e:Replicate("SpellBook")
        calls[#calls + 1] = { "SpellCastingAbility", u, wrote }
    end
    table.sort(late)
    return late
end

-- Passives the kit AddPassive'd that a twin holds more than once (see SETTLE_MS), sorted. Only the kit's own: the
-- game's class rows can give one twice, as a character has it (a Ranger's UnlockedSpellSlotLevel1 at levels 2 and 3,
-- its reference too: team 2 read 2, live 2026-11-08).
local function doubled(calls, u)
    local added, n, out = {}, {}, {}
    for _, c in ipairs(calls) do
        if c[1] == "AddPassive" and c[2] == u then added[c[3]] = true end
    end
    for _, pe in ipairs(U.items(V.entity(u).PassiveContainer.Passives)) do
        local id = tostring(pe.Passive.PassiveId)
        n[id] = (n[id] or 0) + 1
        if n[id] == 2 and added[id] then out[#out + 1] = id end
    end
    table.sort(out)
    return out
end

-- The last of a kit, once the items are in: the rows' abilities, then the passives' count. Waits (ITEM_WAIT_MS) for a
-- spell the book doesn't show yet: AddSpell lands a tick later (S1 P3).
local function finish(job)
    job.spellsBy = job.spellsBy or now_ms() + M.ITEM_WAIT_MS
    local late, twice = {}, {}
    for _, u in ipairs(job.twins) do
        for _, id in ipairs(setAbilities(job.calls, u, job.kit)) do late[#late + 1] = u .. ": " .. id end
        for _, id in ipairs(doubled(job.calls, u)) do twice[#twice + 1] = u .. ": " .. id end
    end
    local partial = { twins = job.twins, calls = job.calls }
    if #twice > 0 then
        RPC.finish_job(job, "failed", partial, "passives held twice: " .. table.concat(twice, "; "))
    elseif #late == 0 then
        RPC.finish_job(job, "done", { twins = job.twins, calls = job.calls, not_applied = job.not_applied })
    elseif now_ms() >= job.spellsBy then
        RPC.finish_job(job, "failed", partial,
            ("spells not in the book %d ms after the kit: %s"):format(M.ITEM_WAIT_MS, table.concat(late, "; ")))
    end
end

-- What keeps a twin from being kitted yet ("" when it is ready), or error when a ready twin can't be kitted.
local KIT_COMPONENTS = { "BoostsContainer", "PassiveContainer", "SpellBook" } -- AddBoosts, AddPassive, AddSpell
local function notReady(u, race, kit)
    local e = V.entity(u)
    if not e then return "no entity" end
    local r = read(function() return tostring(e.Race.Race) end)
    if r == nil or r == M.NULL then return "race " .. tostring(r) end
    if r ~= race then error(("%s is of race %s, not %s"):format(u, r, tostring(race)), 0) end
    for _, c in ipairs(KIT_COMPONENTS) do
        if not read(function() return e[c] ~= nil end) then return "no " .. c end
    end
    if kit.subclass and not read(function() return e.Classes.Classes ~= nil end) then return "no Classes" end
    local p = Ar.Symmetry.profile(u)
    if not (p.equipment and p.inventory) then return "items don't read" end
    if next(p.equipment) or #p.inventory > 0 then error(u .. " already holds items: the kit starts empty", 0) end
    return ""
end

-- Both twins ready, then every choice on each in the kit's order (one tick).
local function start(job)
    local why = {}
    for _, u in ipairs(job.twins) do
        local ok, w = pcall(notReady, u, job.race, job.kit)
        if not ok then
            RPC.finish_job(job, "failed", { twins = job.twins, calls = job.calls }, tostring(w))
            return
        end
        if w ~= "" then why[#why + 1] = u .. ": " .. w end
    end
    if #why > 0 then
        if now_ms() >= job.readyBy then
            RPC.finish_job(job, "failed", { twins = job.twins, calls = job.calls },
                ("twins not ready %d ms after the kit started: %s"):format(M.READY_WAIT_MS, table.concat(why, "; ")))
        end
        return
    end
    if now_ms() < job.settleAt then return end
    for _, u in ipairs(job.twins) do applyChoices(job.calls, u, job.kit) end
    job.ready = true
end

-- Once started: one item at a time, added to both twins, then (once both hold it) equipped when asked.
local function tick(job)
    if not job.ready then return start(job) end
    local it = job.kit.items[job.item]
    if not it then return finish(job) end
    if not job.added then
        for _, u in ipairs(job.twins) do call(job.calls, "TemplateAddTo", { it.root, u, it.count, 0 }) end
        job.added, job.waitUntil = true, now_ms() + M.ITEM_WAIT_MS
        return
    end
    local got = {}
    for i, u in ipairs(job.twins) do got[i] = V.uuid(osi(Osi.GetItemByTemplateInInventory, it.root, u)) end
    if not (got[1] and got[2]) then
        if now_ms() >= job.waitUntil then
            RPC.finish_job(job, "failed", { twins = job.twins, calls = job.calls },
                ("%s (%s) didn't reach both twins in %d ms"):format(it.item, it.root, M.ITEM_WAIT_MS))
        end
        return
    end
    if it.equip then
        for i, u in ipairs(job.twins) do call(job.calls, "Equip", { u, got[i], 1, 0, 1 }) end
    end
    job.item, job.added = job.item + 1, false
end

-- The kit's race part must be for `race` (a build's kit follows its race: a pair of Drow twins never gets a Half-Elf's
-- Movement and passives). Error, naming both, otherwise.
function M.check_kit_race(kit, race, what)
    local r = type(kit.race) == "table" and kit.race.race or nil
    if r ~= race then
        error(("%s's race part is for race %s, the twins' race is %s"):format(what, tostring(r), tostring(race)), 0)
    end
end

-- The kit's grants, checked at the call (nothing applied on a wrong shape).
local function checkGrants(g)
    if g == nil then return end
    if type(g) ~= "table" or (g.boosts ~= nil and type(g.boosts) ~= "string") then
        error("the kit's grants must be {boosts = text, passives, passives_removed, spells}", 0)
    end
    for _, k in ipairs({ "passives", "passives_removed", "spells" }) do
        if g[k] ~= nil and type(g[k]) ~= "table" then error("the kit's grants." .. k .. " must list ids", 0) end
    end
end

function M.kit(args)
    local twins, kit = two(args.twins, "twins"), args.kit
    if type(kit) ~= "table" then error("arena.mirror_kit needs the kit", 0) end
    for k in pairs(kit) do
        if not M.KIT_KEYS[k] then error("the kit has a part nothing applies: " .. tostring(k), 0) end
    end
    checkGrants(kit.grants)
    if kit.subclass ~= nil and type(kit.subclass) ~= "string" then error("the kit's subclass must be its uuid", 0) end
    if kit.spell_abilities ~= nil and type(kit.spell_abilities) ~= "table" then
        error("the kit's spell_abilities must map spell ids to abilities", 0)
    end
    if kit.unlocks ~= nil and type(kit.unlocks) ~= "table" then
        error("the kit's unlocks must map spell ids to abilities", 0)
    end
    for i, u in ipairs(twins) do
        u = V.uuid(u)
        if not (u and V.entity(u)) then error("no twin " .. tostring(args.twins[i]), 0) end
        twins[i] = u:lower()
    end
    if type(args.race) ~= "string" then error("arena.mirror_kit needs the race", 0) end
    M.check_kit_race(kit, args.race, "the kit")
    if twins[1] == twins[2] then error("the twins are one character", 0) end
    for i, c in ipairs(kit.choices or {}) do checkChoice(c, i) end
    for i, it in ipairs(kit.items or {}) do
        if type(it.root) ~= "string" or not tonumber(it.count) then
            error("item " .. i .. " needs root and count", 0)
        end
    end
    local calls, notApplied = {}, {}
    for _, k in ipairs(M.NOT_APPLIED) do
        if kit[k] ~= nil then notApplied[#notApplied + 1] = k end
    end
    kit.items = kit.items or {}
    local job = RPC.start_job("arena.mirror_kit", { timeout_ms = M.TIMEOUT_MS, on_tick = tick, twins = twins,
        race = args.race, kit = kit, calls = calls, item = 1, added = false, ready = false,
        readyBy = now_ms() + M.READY_WAIT_MS, settleAt = now_ms() + M.SETTLE_MS, not_applied = notApplied })
    job.partial = { twins = twins, calls = calls }
    return { job = job.id, status = job.status }
end

for name, fn in pairs({ ["arena.mirror_spawn"] = M.spawn, ["arena.mirror_kit"] = M.kit }) do
    RPC.register(name, function(args)
        if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
        if RPC.Ctx.side then error("arena ops are the harness's (no side)", 0) end
        return fn(args or {})
    end)
end

return M
