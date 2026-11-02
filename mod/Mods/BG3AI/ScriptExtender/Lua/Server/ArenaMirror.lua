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
--   (NAME_WAIT_MS). -> {twins = [u1, u2], reads}
-- arena.mirror_kit {twins = [u1, u2], race, kit} (a job): the same level-1 choices on both twins, in the same order,
--   by the game's own calls, once both twins read ready (READY_WAIT_MS: race set, items readable, the containers the
--   calls write present; a copy fresh from CreateAt has none of it in its spawn tick, live check 13b).
--   kit (make_arena_save.py builds it from the spec's kit and the game data):
--     race = {boosts, passives}: the race table's level-1 fixed parts (the game applies none to a spawned copy; its
--       Movement boost is the copy's only Movement resource: live check 12 step 7)
--     player = {passives}: what every player character has and a copy lacks (DeathSavingThrows, whose own boost is
--       DownedStatus(DOWNED): Lae'zel's, live check 12 step 10)
--     ability_bonus = {+2 ability, +1 ability} or nil (SelectAbilityBonus)
--     choices = [{kind = skills|expertise|passives|spells, list, n, exact, ids}]: one per Select* selector;
--       every id must be in the game's list (Ext.StaticData SkillList / PassiveList / SpellList), #ids == n when
--       exact, else 1..n (a wizard's prepared spells out of its spellbook selector)
--     items = [{item, root, count, equip}]: TemplateAddTo(root, twin, count); the item shows in an inventory a tick
--       later (live check 12 step 7), then Equip(twin, item, 1, 0, 1) when equip (Equip takes 5 arguments)
--   Refused at the call, nothing applied, when a twin is missing or a choice fails its list; the job fails, nothing
--   applied, when a ready twin is not of `race` or already holds items, or a twin isn't ready in READY_WAIT_MS.
--   -> job result {twins, calls}

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local V = BG3AI.Visibility
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
M.NULL = "00000000-0000-0000-0000-000000000000"
M.LISTS = { skills = { "SkillList", "Skills" }, expertise = { "SkillList", "Skills" },
    passives = { "PassiveList", "Passives" }, spells = { "SpellList", "Spells" } }

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
    local host = V.uuid(osi(Osi.GetHostCharacter))
    if not host then error("no host character", 0) end
    local old = tpl.Stats
    tpl.Stats = stats
    local ok, made = pcall(function()
        local out = {}
        for i = 1, 2 do
            local p = spots[i]
            out[i] = V.uuid(Osi.CreateAt(args.template, p[1], p[2], p[3], 0, 0, ""))
            if not out[i] then error("CreateAt made no copy", 0) end
        end
        return out
    end)
    tpl.Stats = old
    if not ok then error(made, 0) end
    local calls, reads = {}, {}
    for i, u in ipairs(made) do
        call(calls, "MakePlayer", { u, host })
        call(calls, "SetStoryDisplayName", { u, names[i] })
    end
    for i, u in ipairs(made) do
        local e = V.entity(u)
        reads[i] = { uuid = u, stats = read(function() return e.Data.StatsId end), name = V.name(u),
            level = read(function() return e.EocLevel.Level end) }
    end
    return { twins = made, reads = reads, calls = calls, template_stats = read(function() return tpl.Stats end) }
end

-- The game's list for a choice, as a set of its ids' text; error when it doesn't read. The list field is the
-- extender's array userdata (live check 13c: SkillList.Skills and PassiveList.Passives are userdata, ipairs gives the
-- ids, tostring their names), not a Lua table.
local function listSet(kind, list)
    local what = M.LISTS[kind]
    if not what then error("a choice of kind " .. tostring(kind) .. " (skills, expertise, passives or spells)", 0) end
    local set = read(function()
        local out = {}
        for _, id in ipairs(Ext.StaticData.Get(list, what[1])[what[2]]) do out[tostring(id)] = true end
        return out
    end)
    if not set then error(("the game's %s %s doesn't read"):format(what[1], tostring(list)), 0) end
    return set
end

local function checkChoice(c, i)
    local where = ("choice %d (%s)"):format(i, tostring(c.kind))
    local n, ids = tonumber(c.n), c.ids
    if not n or type(ids) ~= "table" then error(where .. " needs n and ids", 0) end
    if (c.exact and #ids ~= n) or (not c.exact and (#ids < 1 or #ids > n)) then
        error(("%s: %d picked, the game's selector takes %s%d"):format(where, #ids, c.exact and "" or "1..", n), 0)
    end
    local set, seen = listSet(c.kind, c.list), {}
    for _, id in ipairs(ids) do
        if seen[id] then error(where .. ": " .. tostring(id) .. " picked twice", 0) end
        seen[id] = true
        if not set[tostring(id)] then
            error(("%s: %s is not in the game's list %s"):format(where, tostring(id), tostring(c.list)), 0)
        end
    end
end

-- The non-item calls for one twin, in the kit's order.
local function applyChoices(calls, u, kit)
    local race = kit.race or {}
    if race.boosts and race.boosts ~= "" then call(calls, "AddBoosts", { u, race.boosts, M.SOURCE, u }) end
    for _, p in ipairs(race.passives or {}) do call(calls, "AddPassive", { u, p }) end
    for _, p in ipairs((kit.player or {}).passives or {}) do call(calls, "AddPassive", { u, p }) end
    local ab = kit.ability_bonus
    if ab then call(calls, "AddBoosts", { u, ("Ability(%s,2);Ability(%s,1)"):format(ab[1], ab[2]), M.SOURCE, u }) end
    for _, c in ipairs(kit.choices or {}) do
        if c.kind == "skills" or c.kind == "expertise" then
            local fmt = c.kind == "skills" and "ProficiencyBonus(Skill,%s)" or "ExpertiseBonus(%s)"
            local parts = {}
            for _, id in ipairs(c.ids) do parts[#parts + 1] = fmt:format(id) end
            call(calls, "AddBoosts", { u, table.concat(parts, ";"), M.SOURCE, u })
        elseif c.kind == "passives" then
            for _, id in ipairs(c.ids) do call(calls, "AddPassive", { u, id }) end
        else
            for _, id in ipairs(c.ids) do call(calls, "AddSpell", { u, id, 0, 1 }) end
        end
    end
end

-- What keeps a twin from being kitted yet ("" when it is ready), or error when a ready twin can't be kitted.
local KIT_COMPONENTS = { "BoostsContainer", "PassiveContainer", "SpellBook" } -- AddBoosts, AddPassive, AddSpell
local function notReady(u, race)
    local e = V.entity(u)
    if not e then return "no entity" end
    local r = read(function() return tostring(e.Race.Race) end)
    if r == nil or r == M.NULL then return "race " .. tostring(r) end
    if r ~= race then error(("%s is of race %s, not %s"):format(u, r, tostring(race)), 0) end
    for _, c in ipairs(KIT_COMPONENTS) do
        if not read(function() return e[c] ~= nil end) then return "no " .. c end
    end
    local p = Ar.Symmetry.profile(u)
    if not (p.equipment and p.inventory) then return "items don't read" end
    if next(p.equipment) or #p.inventory > 0 then error(u .. " already holds items: the kit starts empty", 0) end
    return ""
end

-- Both twins ready, then every choice on each in the kit's order (one tick).
local function start(job)
    local why = {}
    for _, u in ipairs(job.twins) do
        local ok, w = pcall(notReady, u, job.race)
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
    for _, u in ipairs(job.twins) do applyChoices(job.calls, u, job.kit) end
    job.ready = true
end

-- Once started: one item at a time, added to both twins, then (once both hold it) equipped when asked.
local function tick(job)
    if not job.ready then return start(job) end
    local it = job.kit.items[job.item]
    if not it then
        RPC.finish_job(job, "done", { twins = job.twins, calls = job.calls })
        return
    end
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

function M.kit(args)
    local twins, kit = two(args.twins, "twins"), args.kit
    if type(kit) ~= "table" then error("arena.mirror_kit needs the kit", 0) end
    for i, u in ipairs(twins) do
        u = V.uuid(u)
        if not (u and V.entity(u)) then error("no twin " .. tostring(args.twins[i]), 0) end
        twins[i] = u:lower()
    end
    if type(args.race) ~= "string" then error("arena.mirror_kit needs the race", 0) end
    if twins[1] == twins[2] then error("the twins are one character", 0) end
    for i, c in ipairs(kit.choices or {}) do checkChoice(c, i) end
    for i, it in ipairs(kit.items or {}) do
        if type(it.root) ~= "string" or not tonumber(it.count) then
            error("item " .. i .. " needs root and count", 0)
        end
    end
    local calls = {}
    kit.items = kit.items or {}
    local job = RPC.start_job("arena.mirror_kit", { timeout_ms = M.TIMEOUT_MS, on_tick = tick, twins = twins,
        race = args.race, kit = kit, calls = calls, item = 1, added = false, ready = false,
        readyBy = now_ms() + M.READY_WAIT_MS })
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
