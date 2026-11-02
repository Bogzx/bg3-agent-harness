-- arena.setup {arena, round_cap} (server context, harness only, refused without BG3AI_ARENA=1): stand the two teams
-- of an arena file up for a match, then Ar.begin. A job; the runner polls it (bg3harness/arena/match.py _setup).
-- `arena` is the arena file (arena.v1) as the runner sends it: teams.A/B (entries {name, uuid?}), location.spawns.A/B
-- ({x, y, z}), seed, karmic. The steps are the arena spike's, which the probe ran by hand (tools/dev/probe_arena.py
-- setup + hostile; live 2026-11-01/02, ops/logs/live.md "arena spike" .. "phases live 3"):
--   1. party: SetMaxPartySizeOverride(8); PROC_GLO_PartyMembers_Add(member, host) for each not in DB_PartyMembers
--   2. place: every member detached, each team chained to its first member, teleported to a line through its spawn
--   3. RestoreParty(host), then SetHitpointsPercentage 100 for anyone it left short; the seed and Karmic Dice
--   4. each team its own spare "Hero Player<N>" faction (never the bare "Hero": the companions' factions descend from
--      it), SetRelation hostile both ways, SetHostileAndEnterCombat (EnterCombat pairs if not all 8 in one combat)
--   5. the IsEnemy matrix read every 0.5 s up to 3 s (live: SetRelation alone doesn't change it; it is right with
--      the combat call): every pair across the teams an enemy, none within, all 8 in one combat, else the job fails
--      with the matrix; the avatar out of the fight (it sits out: not a team member, never in combat)
--   6. Ar.begin(teams, {round_cap})
-- The result records each member's name, level, max HP and faction before and after (joining the party reset two
-- companions' stat blocks in the spike: Minthara 52 -> 11 max HP, Karlach 32 -> 14).

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local Ar = BG3AI.Arena
local truthy = U.truthy

local Setup = {}
Ar.Setup = Setup
Setup.SETTLE_MS = 2000        -- after the party, placing, RestoreParty, the seed (the probe's SETTLE_S, live)
Setup.COMBAT_MS = 3000        -- the combat call to all 8 in one combat (the probe's wait, live)
Setup.ENEMY_WAIT_MS = 3000    -- the IsEnemy matrix read again until right (live: right at once with the combat)
Setup.ENEMY_EVERY_MS = 500
Setup.LINE_GAP_M = 1.5        -- a team's members this far apart on a line along x through its spawn (spike)
Setup.TIMEOUT_MS = 60000
Setup.TEAM_SIZE = 4

local function osi(fn, ...) return V.osi(fn, ...) end
local function now_ms() return Ext.Utils.MonotonicTime() end

-- One Osiris call (a setup step), recorded in the job: {fn, ok, error?}. forms: argument lists tried in order (the
-- first the game accepts).
local function call(job, fn, ...)
    local rec = { fn = fn }
    local f = Osi[fn]
    for _, args in ipairs({ ... }) do
        local ok, err = pcall(f, table.unpack(args))
        rec.ok, rec.error = ok, (not ok) and tostring(err) or nil
        if ok then break end
    end
    job.calls[#job.calls + 1] = rec
    return rec.ok
end

local function column(db)
    local ok, rows = pcall(function() return Osi[db]:Get(nil) end)
    local out = {}
    for _, row in ipairs(ok and type(rows) == "table" and rows or {}) do
        local u = V.uuid(row[1])
        if u then out[#out + 1] = u:lower() end
    end
    return out
end

local function set_of(list)
    local s = {}
    for _, u in ipairs(list) do s[u] = true end
    return s
end

local function norm(s) return (tostring(s or ""):lower():gsub("[^%w]", "")) end

-- A team entry ({name, uuid?} or a name) -> the character's uuid. By uuid when given, else the one party member,
-- player or origin whose display name is that name (exact, ignoring case and punctuation). The avatar sits out.
function Setup.resolve(entry, avatars)
    local name = type(entry) == "table" and entry.name or entry
    local want = type(entry) == "table" and entry.uuid
    if want then
        local u = V.uuid(want)
        if not u or not V.entity(u) then
            error("no character " .. tostring(want) .. " (" .. tostring(name) .. ")", 0)
        end
        u = u:lower()
        if avatars[u] then error(tostring(name) .. " is the avatar: the avatar sits out of arena fights", 0) end
        return u
    end
    if type(name) ~= "string" or name == "" then error("a team entry has no name", 0) end
    local hits, seen = {}, {}
    for _, db in ipairs({ "DB_PartyMembers", "DB_Players", "DB_Origins" }) do
        for _, u in ipairs(column(db)) do
            if not seen[u] and norm(V.name(u)) == norm(name) then hits[#hits + 1] = u end
            seen[u] = true
        end
    end
    if #hits == 0 then error("no party member, player or origin named " .. name, 0) end
    if #hits > 1 then error(name .. " names " .. #hits .. " characters (" .. table.concat(hits, ", ")
        .. "): give its uuid in the arena file", 0) end
    if avatars[hits[1]] then error(name .. " is the avatar: the avatar sits out of arena fights", 0) end
    return hits[1]
end

local function vec3(v, what)
    if type(v) ~= "table" or #v ~= 3 then error(what .. " must be [x, y, z]", 0) end
    for i = 1, 3 do
        if type(v[i]) ~= "number" then error(what .. " must be [x, y, z]", 0) end
    end
    return v
end

-- n positions on a line along x through spot, centred on it (the probe's line_slots).
function Setup.line(spot, n)
    local out = {}
    for i = 0, n - 1 do
        out[#out + 1] = { spot[1] + (i - (n - 1) / 2) * Setup.LINE_GAP_M, spot[2], spot[3] }
    end
    return out
end

-- The two lowest-numbered "Hero Player<N>" factions nobody in the level uses and no team member holds.
function Setup.spare_factions(held)
    local used = {}
    for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter") or {}) do
        local u = V.entityUuid(e)
        local f = u and osi(Osi.GetFaction, u)
        if f then used[f] = (used[f] or 0) + 1 end
    end
    local spare = {}
    for _, g in ipairs(Ext.StaticData.GetAll("Faction") or {}) do
        local ok, r = pcall(Ext.StaticData.Get, g, "Faction")
        local name = ok and r and r.Faction
        local n = name and tonumber(name:match("^Hero Player(%d+)$"))
        local form = name and (name .. "_" .. tostring(g))
        if n and not used[form] and not held[form] then spare[#spare + 1] = { n = n, form = form } end
    end
    table.sort(spare, function(a, b) return a.n < b.n end)
    if #spare < 2 then error("fewer than two spare Hero Player<N> factions in the level", 0) end
    return spare[1].form, spare[2].form
end

local function facts(u)
    return { name = V.name(u), level = osi(Osi.GetLevel, u), max_hp = osi(Osi.GetMaxHitpoints, u),
        faction = osi(Osi.GetFaction, u) }
end

local function all_of(teams)
    local out = {}
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(teams[s]) do out[#out + 1] = u end
    end
    return out
end

-- The IsEnemy matrix: across = every ordered pair of different teams, within = every ordered pair of one team.
function Setup.matrix(teams)
    local m = { across = 0, across_of = 0, within = 0, within_of = 0, wrong = {} }
    for _, s in ipairs(Ar.SIDES) do
        for _, x in ipairs(teams[s]) do
            for _, s2 in ipairs(Ar.SIDES) do
                for _, y in ipairs(teams[s2]) do
                    if x ~= y then
                        local enemy = truthy(osi(Osi.IsEnemy, x, y))
                        if s ~= s2 then
                            m.across_of = m.across_of + 1
                            if enemy then m.across = m.across + 1 end
                        else
                            m.within_of = m.within_of + 1
                            if enemy then m.within = m.within + 1 end
                        end
                        if enemy ~= (s ~= s2) then
                            m.wrong[#m.wrong + 1] = V.name(x) .. (enemy and " enemy of " or " not an enemy of ")
                                .. V.name(y)
                        end
                    end
                end
            end
        end
    end
    return m
end

-- All members in one combat (its guid), else nil and the guids seen.
local function one_combat(teams)
    local c, seen = nil, {}
    for _, u in ipairs(all_of(teams)) do
        local g = osi(Osi.CombatGetGuidFor, u)
        seen[#seen + 1] = V.name(u) .. ":" .. tostring(g)
        if not g or (c and g ~= c) then return nil, seen end
        c = g
    end
    return c, seen
end

-- The job's steps, each run once its wait is over; each returns the next wait (ms) or nil (the last).
local STEPS = {}

STEPS[1] = function(job) -- the party
    local t = job.teams
    for _, u in ipairs(all_of(t)) do job.before[u] = facts(u) end
    call(job, "SetMaxPartySizeOverride", { 8 })
    local inParty = set_of(column("DB_PartyMembers"))
    for _, u in ipairs(all_of(t)) do
        if not inParty[u] then call(job, "PROC_GLO_PartyMembers_Add", { u, job.host }) end
    end
    return Setup.SETTLE_MS
end

STEPS[2] = function(job) -- chained per team, on its line
    local t = job.teams
    for _, u in ipairs(all_of(t)) do call(job, "DetachFromPartyGroup", { u }) end
    for _, s in ipairs(Ar.SIDES) do
        for i = 2, #t[s] do call(job, "AttachToPartyGroup", { t[s][i], t[s][1] }) end
        for i, p in ipairs(Setup.line(job.spawns[s], #t[s])) do
            local u = t[s][i]
            call(job, "TeleportToPosition", { u, p[1], p[2], p[3], "", 0, 0, 0, 0, 1 }, { u, p[1], p[2], p[3], "" })
        end
    end
    return Setup.SETTLE_MS
end

STEPS[3] = function(job)
    call(job, "RestoreParty", { job.host })
    return Setup.SETTLE_MS
end

STEPS[4] = function(job) -- full HP; the arena's seed and Karmic Dice
    for _, u in ipairs(all_of(job.teams)) do
        if osi(Osi.GetHitpoints, u) ~= osi(Osi.GetMaxHitpoints, u) then
            call(job, "SetHitpointsPercentage", { u, 100.0, "Guaranteed" }, { u, 100.0 })
        end
    end
    local S = BG3AI.Scenario
    job.seed = S.set_seed(job.arena.seed)
    job.karmic = S.setKarmic(job.arena.karmic == true)
    return Setup.SETTLE_MS
end

STEPS[5] = function(job) -- hostile teams, one combat
    local t = job.teams
    local held = {}
    for _, u in ipairs(all_of(t)) do held[osi(Osi.GetFaction, u) or ""] = true end
    job.factions = {}
    job.factions.A, job.factions.B = Setup.spare_factions(held)
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(t[s]) do call(job, "SetFaction", { u, job.factions[s] }) end
    end
    call(job, "SetRelation", { job.factions.A, job.factions.B, 0 })
    call(job, "SetRelation", { job.factions.B, job.factions.A, 0 })
    call(job, "SetHostileAndEnterCombat", { job.factions.A, job.factions.B, t.A[1], t.B[1] })
    return Setup.COMBAT_MS
end

STEPS[6] = function(job) -- all 8 in one combat, else EnterCombat pairs once
    if one_combat(job.teams) then return 0 end
    local t = job.teams
    for i = 1, math.min(#t.A, #t.B) do call(job, "EnterCombat", { t.A[i], t.B[i] }) end
    return Setup.COMBAT_MS
end

STEPS[7] = function(job) -- the fight as it must be, then the match
    local t = job.teams
    local c, seen = one_combat(t)
    if not c then error("the teams are not in one combat: " .. table.concat(seen, ", "), 0) end
    local m = Setup.matrix(t)
    job.enemy_reads = (job.enemy_reads or 0) + 1
    job.enemy_from = job.enemy_from or now_ms()
    local right = m.across == m.across_of and m.within == 0
    if not right then
        if now_ms() - job.enemy_from < Setup.ENEMY_WAIT_MS then
            job.step = job.step - 1 -- read again
            return Setup.ENEMY_EVERY_MS
        end
        error(("IsEnemy after %d reads: across the teams %d/%d, within a team %d/%d: %s"):format(job.enemy_reads,
            m.across, m.across_of, m.within, m.within_of, table.concat(m.wrong, "; ")), 0)
    end
    for _, a in ipairs(job.avatars) do
        if truthy(osi(Osi.IsInCombat, a)) then
            error("the avatar " .. tostring(V.name(a)) .. " joined the fight (it sits out)", 0)
        end
    end
    local after = {}
    for _, u in ipairs(all_of(t)) do
        local b, a = job.before[u], facts(u)
        after[u] = { name = b.name, level = { b.level, a.level }, max_hp = { b.max_hp, a.max_hp },
            faction = { b.faction, a.faction } }
    end
    local status = Ar.begin(t, { round_cap = job.round_cap })
    RPC.finish_job(job, "done", { teams = t, factions = job.factions, combat = c, members = after,
        enemy = { across = m.across .. "/" .. m.across_of, within = m.within .. "/" .. m.within_of,
            reads = job.enemy_reads }, seed = job.seed, karmic = job.karmic, calls = job.calls, status = status })
end

local function tick(job)
    if now_ms() < job.next_at then return end
    local wait = STEPS[job.step](job)
    job.step = job.step + 1
    if job.status == "running" and wait then job.next_at = now_ms() + wait end
end

-- Check the request and resolve the teams at once (a refusal, no job); the rest runs as the job.
function Setup.start(args)
    local arena = args.arena
    if type(arena) ~= "table" then error("arena.setup needs the arena file as `arena`", 0) end
    local S = BG3AI.Scenario
    if not (S and S.enabled and S.enabled()) then
        error("arena.setup sets the arena's seed and Karmic Dice: start the game with BG3AI_SCENARIO_SETUP=1 too", 0)
    end
    local teams, spawns, seen = {}, {}, {}
    local avatarList = column("DB_Avatars")
    local avatars = set_of(avatarList)
    local spawnsIn = type(arena.location) == "table" and arena.location.spawns or nil
    for _, s in ipairs(Ar.SIDES) do
        local entries = type(arena.teams) == "table" and arena.teams[s] or nil
        if type(entries) ~= "table" or #entries ~= Setup.TEAM_SIZE then
            error("team " .. s .. " must list " .. Setup.TEAM_SIZE .. " characters", 0)
        end
        teams[s] = {}
        for i, e in ipairs(entries) do
            local u = Setup.resolve(e, avatars)
            if seen[u] then error(V.name(u) .. " is listed twice", 0) end
            seen[u] = true
            teams[s][i] = u
        end
        spawns[s] = vec3(type(spawnsIn) == "table" and spawnsIn[s] or nil, "location.spawns." .. s)
    end
    if args.round_cap ~= nil and not tonumber(args.round_cap) then error("round_cap must be a number", 0) end
    local host = V.uuid(osi(Osi.GetHostCharacter))
    if not host then error("no host character", 0) end
    local job = RPC.start_job("arena.setup", { timeout_ms = Setup.TIMEOUT_MS, on_tick = tick, arena = arena,
        teams = teams, spawns = spawns, host = host:lower(), avatars = avatarList,
        round_cap = tonumber(args.round_cap or arena.round_cap), step = 1, next_at = 0, before = {}, calls = {} })
    job.partial = { teams = teams, calls = job.calls }
    return { job = job.id, status = job.status }
end

RPC.register("arena.setup", function(args)
    if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
    if RPC.Ctx.side then error("arena ops are the harness's (no side)", 0) end
    return Setup.start(args or {})
end)

return Setup
