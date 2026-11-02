-- arena.setup {arena, round_cap} (server context, harness only, refused without BG3AI_ARENA=1): stand the two teams
-- of an arena file up for a match, then Ar.begin. A job; the runner polls it (bg3harness/arena/match.py _setup).
-- `arena` is the arena file (arena.v1) as the runner sends it: teams.A/B (entries {name, uuid?}), location.spawns.A/B
-- ({x, y, z}), seed, karmic, level (the members' character level: setup fails unless every member has it after
-- joining). The teams are the same size, 1 to 4. A member's name is its display name after joining the party (live:
-- Nightwarden Minthara is Minthara once she joins): setup fails when it differs, listing both. Every party member on
-- no team (the avatar too) sits out. The steps are the arena spike's, which the probe ran by hand
-- (tools/dev/probe_arena.py setup + hostile; live 2026-11-01/02, ops/logs/live.md "arena spike" .. "phases live 3"):
--   0. a mirror arena (arena.mirror {pairs = [{template, race, stats, names = {A, B}, kit}]}, made by
--      tools/dev/make_arena_save.py mirror): the twins are made for this match (a save keeps neither our stat
--      entries' grants nor the kit's boosts: live check 12 step 13): every spot cleared of Ground and Cloud surfaces
--      (RemoveSurfaceLayerAtPosition, SURFACE_CLEAR_M), then each pair arena.mirror_spawn + arena.mirror_kit
--      (Server/ArenaMirror.lua), pair i's side Setup.spawn_order(seed, n)[i] made first (recorded as spawn_order);
--      once every kit is done, each twin must read its own name and StatsId within
--      NAME_WAIT_MS of the spawn (both land after the spawn tick) and arena.symmetry over the pairs must find
--      nothing; pair i is teams.A[i] and teams.B[i] (arena.teams, if given, must name them in that order)
--   1. party: SetMaxPartySizeOverride(8); PROC_GLO_PartyMembers_Add(member, host) for each not in DB_PartyMembers
--   2. place: the names checked; every member and sitter detached, each team chained to its first member and
--      teleported to a line through its spawn (the sitters stay where they are, SetCanJoinCombat 0: read back 0)
--   3. where they landed (Setup.check_landed: each pair under SPAWN_DY_MAX_M apart in height, the cross distances
--      the same both ways), then RestoreParty(host), then SetHitpointsPercentage 100 for anyone it left short; the
--      seed and Karmic Dice
--   4. each team its own spare "Hero Player<N>" faction (never the bare "Hero": the companions' factions descend from
--      it), SetRelation hostile both ways, SetHostileAndEnterCombat (EnterCombat pairs if not all members in one
--      combat)
--   5. the IsEnemy matrix read every 0.5 s up to 3 s (live: SetRelation alone doesn't change it; it is right with
--      the combat call): every pair across the teams an enemy, none within, all members in one combat, else the job
--      fails with the matrix; no character on no team in that combat (the job fails naming each)
--   6. a mirror arena: arena.symmetry over the pairs again, statuses included (a twin on a surface reads INSURFACE)
--   7. Ar.begin(teams, {round_cap, positions}): positions = where each member stands, in the result and arena_start
-- Refused at the call: spawns whose heights differ by SPAWN_DY_MAX_M or more, or whose lines don't give each pair the
-- same distances both ways (Setup.check_geometry).
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
Setup.COMBAT_MS = 3000        -- the combat call to all members in one combat (the probe's wait, live)
Setup.ENEMY_WAIT_MS = 3000    -- the IsEnemy matrix read again until right (live: right at once with the combat)
Setup.ENEMY_EVERY_MS = 500
Setup.LINE_GAP_M = 1.5        -- a team's members this far apart on a line along x through its spawn (spike)
Setup.TIMEOUT_MS = 60000
-- A mirror arena's spots are cleared of surfaces this far around before a twin stands on one (live check 13d: Cedar
-- spawned on a surface at spot A and read INSURFACE, Dune not: the pair's statuses differed)
Setup.SURFACE_CLEAR_M = 2.0
Setup.SURFACE_LAYERS = { "Ground", "Cloud" } -- bg3se SurfaceLayer8
Setup.TEAM_MAX = 4             -- two teams of 4 fill the party of 8 the spike stood up
-- Spawn geometry (both sides stand on the same terms): the spawns at most this far apart in height, and every distance
-- across the lines the same both ways (A's member i to B's member j = B's i to A's j) within this
Setup.SPAWN_DY_MAX_M = 0.3
Setup.PAIR_DIST_TOL_M = 0.1

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

-- A team entry ({name, uuid?} or a name) -> the character's uuid. By uuid when given (its name is checked after the
-- join), else the one party member, player or origin whose display name is that name now (exact, ignoring case and
-- punctuation). The avatar sits out.
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

-- Which side's twin each of n pairs makes first: A when seed + i is even, else B. It alternates pair to pair and the
-- seed flips it, so no side's twins are always made first (initiative ties may go by creation order; not read live).
function Setup.spawn_order(seed, n)
    if type(seed) ~= "number" or seed % 1 ~= 0 or seed < 0 then
        error("a mirror arena needs arena.seed (an integer 0 or more): it orders each pair's spawn", 0)
    end
    local out = {}
    for i = 1, n do out[i] = (seed + i) % 2 == 0 and "A" or "B" end
    return out
end

local function dist(p, q)
    return math.sqrt((p[1] - q[1]) ^ 2 + (p[2] - q[2]) ^ 2 + (p[3] - q[3]) ^ 2)
end

-- Refuses spawns that put one team higher (|dy| >= SPAWN_DY_MAX_M) or nearer the other's members than the other is
-- to its own (the lines run along x: spawns apart along x make pair i's A member nearer B's later pairs).
function Setup.check_geometry(spawns, n)
    local dy = math.abs(spawns.A[2] - spawns.B[2])
    if dy >= Setup.SPAWN_DY_MAX_M then
        error(("location.spawns A and B are %.2f m apart in height (y); they must be under %.1f m"):format(dy,
            Setup.SPAWN_DY_MAX_M), 0)
    end
    Setup.check_cross(Setup.line(spawns.A, n), Setup.line(spawns.B, n), "location.spawns")
end

-- Refuses positions a (team A, by member) and b (team B) where a distance across the teams differs both ways (A's
-- member i to B's j vs B's i to A's j) by more than PAIR_DIST_TOL_M.
function Setup.check_cross(a, b, what)
    for i = 1, #a do
        for j = 1, #a do
            local ab, ba = dist(a[i], b[j]), dist(b[i], a[j])
            if math.abs(ab - ba) > Setup.PAIR_DIST_TOL_M then
                error(("%s: A's member %d is %.2f m from B's member %d, B's member %d %.2f m from A's member %d; "
                    .. "they must be the same within %.1f m"):format(what, i, ab, j, i, ba, j, Setup.PAIR_DIST_TOL_M),
                    0)
            end
        end
    end
end

-- Where the members landed (live 15: the file's spawns 0.18 m apart in height, the twins stood 0.73 m and 0.35 m
-- apart: the game puts a teleported character on the ground under it): pair i (A[i], B[i]) under SPAWN_DY_MAX_M apart
-- in height and the cross distances as check_cross wants, else an error naming the pair and both heights. A position
-- that doesn't read is an error too.
function Setup.check_landed(teams, names)
    local at = {}
    for _, s in ipairs(Ar.SIDES) do
        at[s] = {}
        for i, u in ipairs(teams[s]) do
            local ok, x, y, z = pcall(Osi.GetPosition, u)
            if not ok or type(x) ~= "number" then
                error(("%s (%s): its position doesn't read after placing"):format(tostring(names[u]), u), 0)
            end
            at[s][i] = { x, y, z }
        end
    end
    for i, a in ipairs(at.A) do
        local b = at.B[i]
        local dy = math.abs(a[2] - b[2])
        if dy >= Setup.SPAWN_DY_MAX_M then
            error(("pair %d (%s, %s) landed %.2f m apart in height (y %.2f, %.2f); a pair must stand under %.1f m "
                .. "apart"):format(i, tostring(names[teams.A[i]]), tostring(names[teams.B[i]]), dy, a[2], b[2],
                Setup.SPAWN_DY_MAX_M), 0)
        end
    end
    Setup.check_cross(at.A, at.B, "where the members landed")
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

STEPS[2] = function(job) -- the names after joining; chained per team, on its line; the sitters detached
    local t = job.teams
    local off = {}
    for _, u in ipairs(all_of(t)) do
        local want = job.names[u]
        if want ~= nil and norm(V.name(u)) ~= norm(want) then
            off[#off + 1] = ("%s (%s) is %s"):format(tostring(want), u, tostring(V.name(u)))
        end
    end
    if #off > 0 then
        error("team entries whose display name after joining the party differs: " .. table.concat(off, "; "), 0)
    end
    local member = set_of(all_of(t))
    job.sitters = {}
    for _, db in ipairs({ column("DB_PartyMembers"), job.avatars }) do
        for _, u in ipairs(db) do
            if not member[u] then
                member[u] = true
                job.sitters[#job.sitters + 1] = u
            end
        end
    end
    for _, u in ipairs(all_of(t)) do call(job, "DetachFromPartyGroup", { u }) end
    -- the sitters can't join the fight at all (live 9: the avatar 21 m off joined ~3 min in, when a member came ~18 m
    -- from him); arena.reset gives each its CanJoinCombat back
    job.can_join, off = {}, {}
    for _, u in ipairs(job.sitters) do
        call(job, "DetachFromPartyGroup", { u })
        job.can_join[u] = osi(Osi.CanJoinCombat, u)
        call(job, "SetCanJoinCombat", { u, 0 })
        local now = osi(Osi.CanJoinCombat, u)
        if now ~= 0 then off[#off + 1] = ("%s CanJoinCombat %s"):format(tostring(V.name(u)), tostring(now)) end
    end
    if #off > 0 then
        error("sitters that can still join the fight after SetCanJoinCombat 0: " .. table.concat(off, ", "), 0)
    end
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
    Setup.check_landed(job.teams, job.names)
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

STEPS[6] = function(job) -- all members in one combat, else EnterCombat pairs once
    if one_combat(job.teams) then return 0 end
    local t = job.teams
    for i = 1, math.min(#t.A, #t.B) do call(job, "EnterCombat", { t.A[i], t.B[i] }) end
    return Setup.COMBAT_MS
end

STEPS[7] = function(job) -- the fight as it must be, then the match
    local t = job.teams
    local c, seen = one_combat(t)
    if not c then error("the teams are not in one combat: " .. table.concat(seen, ", "), 0) end
    -- nobody else in it: the sitters, and any other character in the level
    local member, out, others = set_of(all_of(t)), {}, {}
    for _, u in ipairs(job.sitters) do others[#others + 1] = u end
    for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter") or {}) do
        local u = V.entityUuid(e)
        if u then others[#others + 1] = u:lower() end
    end
    for _, u in ipairs(others) do
        if not member[u] and osi(Osi.CombatGetGuidFor, u) == c then
            member[u] = true
            out[#out + 1] = tostring(V.name(u))
        end
    end
    if #out > 0 then error("not on a team and in the fight: " .. table.concat(out, ", "), 0) end
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
    local after, off = {}, {}
    for _, u in ipairs(all_of(t)) do
        local b, a = job.before[u], facts(u)
        after[u] = { name = b.name, level = { b.level, a.level }, max_hp = { b.max_hp, a.max_hp },
            faction = { b.faction, a.faction } }
        if a.level ~= job.level then off[#off + 1] = ("%s %s"):format(b.name, tostring(a.level)) end
    end
    -- every member at the arena's level, or no match (live 2026-11-02: joining reset Karlach 3 -> 1, Wyll 2 -> 1,
    -- Minthara 6 -> 1 while Shadowheart, a member already, stayed 2)
    if #off > 0 then
        RPC.finish_job(job, "failed", { teams = t, members = after, calls = job.calls },
            ("levels after setup differ from the arena's level %d: %s"):format(job.level, table.concat(off, ", ")))
        return
    end
    if job.mirror then -- the pairs once more as the fight starts, statuses included (a surface under one twin)
        local pairs = {}
        for i = 1, #job.mirror.pairs do pairs[i] = { t.A[i], t.B[i] } end
        local sym = Ar.Symmetry.check(pairs)
        if not sym.ok then
            RPC.finish_job(job, "failed", { teams = t, members = after, calls = job.calls },
                "the twins are not mirrors as the fight starts: " .. table.concat(sym.problems, "; "))
            return
        end
    end
    local sitters, positions = {}, {}
    for _, u in ipairs(job.sitters) do sitters[#sitters + 1] = { uuid = u, name = V.name(u) } end
    for _, u in ipairs(all_of(t)) do -- where each member stands as the fight starts (none when it doesn't read)
        local ok, x, y, z = pcall(Osi.GetPosition, u)
        if ok and type(x) == "number" then positions[u] = { x, y, z } end
    end
    local status = Ar.begin(t, { round_cap = job.round_cap, sitters = job.can_join, positions = positions })
    RPC.finish_job(job, "done", { teams = t, factions = job.factions, combat = c, members = after, sitters = sitters,
        enemy = { across = m.across .. "/" .. m.across_of, within = m.within .. "/" .. m.within_of,
            reads = job.enemy_reads }, seed = job.seed, karmic = job.karmic, spawn_order = job.spawn_order,
        positions = positions,
        calls = job.calls, status = status })
end

-- A mirror arena: the twins, spawned and given their kits (their kit jobs run on their own).
STEPS[-1] = function(job)
    local mi, M = job.mirror, Ar.Mirror
    job.kits = {}
    for i = 1, #mi.pairs do -- every spot free of surfaces first: a pair stands on the same ground
        for _, s in ipairs(Ar.SIDES) do
            local p = Setup.line(job.spawns[s], #mi.pairs)[i]
            for _, layer in ipairs(Setup.SURFACE_LAYERS) do
                local args = { p[1], p[2], p[3], layer, Setup.SURFACE_CLEAR_M }
                if not call(job, "RemoveSurfaceLayerAtPosition", args) then
                    error(("RemoveSurfaceLayerAtPosition %s at pair %d's spot %s: %s"):format(layer, i, s,
                        tostring(job.calls[#job.calls].error)), 0)
                end
            end
        end
    end
    for i, p in ipairs(mi.pairs) do
        local spots = {}
        for k, s in ipairs(Ar.SIDES) do spots[k] = Setup.line(job.spawns[s], #mi.pairs)[i] end
        local r = M.spawn({ stats = p.stats, template = p.template, race = p.race, spots = spots,
            names = { p.names.A, p.names.B }, first = job.spawn_order[i] == "A" and 1 or 2 })
        for k, s in ipairs(Ar.SIDES) do
            job.teams[s][i] = r.twins[k]
            job.names[r.twins[k]] = p.names[s]
        end
        job.kits[i] = M.kit({ twins = { r.twins[1], r.twins[2] }, race = p.race, kit = p.kit }).job
    end
    job.names_by = now_ms() + M.NAME_WAIT_MS -- names and StatsId land after the spawn tick (live checks 13, 13b)
    return M.KIT_EVERY_MS
end

STEPS[0] = function(job) -- the kits done and the names landed, then the twins as they must be
    for i, id in ipairs(job.kits) do
        local k = RPC.jobs[id]
        if k.status == "running" then
            job.step = job.step - 1 -- wait again
            return Ar.Mirror.KIT_EVERY_MS
        end
        if k.status ~= "done" then error(("pair %d's kit %s: %s"):format(i, k.status, tostring(k.error)), 0) end
    end
    local off, pairs = {}, {}
    for i, p in ipairs(job.mirror.pairs) do
        pairs[i] = { job.teams.A[i], job.teams.B[i] }
        for _, u in ipairs(pairs[i]) do
            if V.name(u) ~= job.names[u] then
                off[#off + 1] = ("%s (%s) reads %s"):format(job.names[u], u, tostring(V.name(u)))
            end
            local e = V.entity(u)
            local ok, st = pcall(function() return e.Data.StatsId end)
            st = ok and st or nil
            if st ~= p.stats then off[#off + 1] = ("%s (%s) has stats %s"):format(job.names[u], u, tostring(st)) end
        end
    end
    if #off > 0 then
        if now_ms() < job.names_by then
            job.step = job.step - 1 -- wait again
            return Ar.Mirror.KIT_EVERY_MS
        end
        error(("twins that don't read as asked %d ms after the spawn: %s"):format(Ar.Mirror.NAME_WAIT_MS,
            table.concat(off, "; ")), 0)
    end
    local sym = Ar.Symmetry.check(pairs)
    if not sym.ok then error("the twins are not mirrors: " .. table.concat(sym.problems, "; "), 0) end
    return 0
end

local function tick(job)
    if now_ms() < job.next_at then return end
    local wait = STEPS[job.step](job)
    job.step = job.step + 1
    if job.status == "running" and wait then job.next_at = now_ms() + wait end
end

-- A mirror arena's section, checked at once (the kits' own lists are checked by arena.mirror_kit): 1..TEAM_MAX
-- pairs, each a root template, its race, a stats entry, two names (unique across the arena) and a kit; arena.teams,
-- if given, names pair i's twins at index i.
function Setup.check_mirror(mi, teams)
    local ps = type(mi) == "table" and mi.pairs or nil
    if type(ps) ~= "table" or #ps < 1 or #ps > Setup.TEAM_MAX then
        error("arena.mirror.pairs must list 1 to " .. Setup.TEAM_MAX .. " pairs", 0)
    end
    local seen = {}
    for i, p in ipairs(ps) do
        if type(p) ~= "table" or type(p.template) ~= "string" or type(p.race) ~= "string" or type(p.stats) ~= "string"
            or type(p.kit) ~= "table" or type(p.names) ~= "table" then
            error("arena.mirror.pairs[" .. i .. "] needs template, race, stats, names and kit", 0)
        end
        for _, s in ipairs(Ar.SIDES) do
            local n = p.names[s]
            if type(n) ~= "string" or n == "" then error(("pair %d: names.%s must be a name"):format(i, s), 0) end
            if seen[n:lower()] then error(("pair %d: the name %s is used twice"):format(i, n), 0) end
            seen[n:lower()] = true
            local e = type(teams) == "table" and type(teams[s]) == "table" and teams[s][i] or nil
            local tn = type(e) == "table" and e.name or e
            if teams ~= nil and tn ~= n then
                error(("arena.teams.%s[%d] is %s, the mirror's pair %d names %s"):format(s, i, tostring(tn), i, n), 0)
            end
        end
    end
end

-- Check the request and resolve the teams at once (a refusal, no job); the rest runs as the job.
function Setup.start(args)
    local arena = args.arena
    if type(arena) ~= "table" then error("arena.setup needs the arena file as `arena`", 0) end
    local S = BG3AI.Scenario
    if not (S and S.enabled and S.enabled()) then
        error("arena.setup sets the arena's seed and Karmic Dice: start the game with BG3AI_SCENARIO_SETUP=1 too", 0)
    end
    local teams, spawns, seen, names = {}, {}, {}, {}
    local avatarList = column("DB_Avatars")
    local avatars = set_of(avatarList)
    local spawnsIn = type(arena.location) == "table" and arena.location.spawns or nil
    local mirror = arena.mirror
    local spawnOrder
    if mirror ~= nil then
        Setup.check_mirror(mirror, arena.teams)
        spawnOrder = Setup.spawn_order(arena.seed, #mirror.pairs)
    end
    for _, s in ipairs(Ar.SIDES) do
        teams[s] = {}
        spawns[s] = vec3(type(spawnsIn) == "table" and spawnsIn[s] or nil, "location.spawns." .. s)
        local entries = type(arena.teams) == "table" and arena.teams[s] or nil
        if mirror == nil and (type(entries) ~= "table" or #entries < 1 or #entries > Setup.TEAM_MAX) then
            error("team " .. s .. " must list 1 to " .. Setup.TEAM_MAX .. " characters", 0)
        end
        for i, e in ipairs(mirror == nil and entries or {}) do
            local u = Setup.resolve(e, avatars)
            if seen[u] then error(V.name(u) .. " is listed twice", 0) end
            seen[u] = true
            teams[s][i] = u
            names[u] = type(e) == "table" and e.name or e
        end
    end
    if mirror == nil and #teams.A ~= #teams.B then
        error(("team A lists %d characters, team B %d: the teams must be the same size"):format(#teams.A, #teams.B), 0)
    end
    Setup.check_geometry(spawns, mirror and #mirror.pairs or #teams.A)
    if args.round_cap ~= nil and not tonumber(args.round_cap) then error("round_cap must be a number", 0) end
    local level = arena.level
    if type(level) ~= "number" or level % 1 ~= 0 or level < 1 or level > 12 then
        error("arena.level must be the members' character level (an integer 1..12)", 0)
    end
    local host = V.uuid(osi(Osi.GetHostCharacter))
    if not host then error("no host character", 0) end
    local job = RPC.start_job("arena.setup", { timeout_ms = Setup.TIMEOUT_MS, on_tick = tick, arena = arena,
        teams = teams, names = names, spawns = spawns, host = host:lower(), avatars = avatarList,
        round_cap = tonumber(args.round_cap or arena.round_cap), level = level, step = mirror and -1 or 1, next_at = 0,
        before = {}, calls = {}, mirror = mirror, spawn_order = spawnOrder })
    job.partial = { teams = teams, calls = job.calls, spawn_order = spawnOrder }
    return { job = job.id, status = job.status }
end

RPC.register("arena.setup", function(args)
    if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
    if RPC.Ctx.side then error("arena ops are the harness's (no side)", 0) end
    return Setup.start(args or {})
end)

return Setup
