-- arena.setup {arena, round_cap} (server context, harness only, refused without BG3AI_ARENA=1): stand the two teams
-- of an arena file up for a match, then Ar.begin. A job; the runner polls it (bg3harness/arena/match.py _setup).
-- `arena` is the arena file (arena.v1) as the runner sends it: teams.A/B (entries {name, uuid?}), location.spawns.A/B
-- ({x, y, z}), seed, karmic, level (the members' character level: setup fails unless every member has it after
-- joining). The teams are the same size, 1 to 4. A member's name is its display name after joining the party (live:
-- Nightwarden Minthara is Minthara once she joins): setup fails when it differs, listing both. Every party member on
-- no team (the avatar too) sits out. The steps are the arena spike's, which the probe ran by hand
-- (tools/dev/probe_arena.py setup + hostile; live 2026-11-01/02, ops/logs/live.md "arena spike" .. "phases live 3"):
--  -4. a mirror arena with mirror.party_xp {total, below?}: the host's XP brought to the tier before any twin exists
--      (a copy snaps to the party's level at MakePlayer: live builds-1 load 2, 900 XP gave a level-3 Fighter): when it
--      reads under total, AddExplorationExperience for the difference, read back until it reads at least total
--      (XP_WAIT_MS). Above the window (below, the next level's total) setup fails: no call takes XP away. The host
--      itself stays at its level with a level-up pending (same live read), so the twins' level is what step 7 checks.
--  -3. a mirror arena: the field cleaned (Server/ArenaField.lua: within the surface clear's reach, pickable items
--      deleted, traps disarmed, locks opened, doors opened, usable objects disabled, destructibles invulnerable),
--      read back a tick later in step -2: the items still active must be the same set on both halves (else setup
--      fails naming them, before any twin spawns); the counts are the result's and arena_start's `field`
--   0. a mirror arena (arena.mirror {pairs = [{template, race, stats, names = {A, B}, kit}]}, made by
--      tools/dev/make_arena_save.py mirror): the twins are made for this match (a save keeps neither our stat
--      entries' grants nor the kit's boosts: live check 12 step 13): every spot cleared of Ground and Cloud surfaces
--      (RemoveSurfaceLayerAtPosition over the arena, then SURFACE_CLEAR_M at each spot) and the surfaces read
--      mirrored across the midplane (Setup.check_surfaces; the result's surfaces; read again until they do, at most
--      SURFACE_WAIT_MS after the clear, which lands after its call), then each pair
--      arena.mirror_spawn + arena.mirror_kit
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
--   4. a mirror arena whose pairs carry a build's `reference`: each pair's A twin against it (its B twin is its mirror
--      by then; Symmetry.reference_diff), after RestoreParty so the resources read full, before anyone is hostile.
--      Every pair is read, then setup fails listing what differs and isn't accepted; the result's `reference` has them.
--      each team its own spare "Hero Player<N>" faction (never the bare "Hero": the companions' factions descend from
--      it), SetRelation hostile both ways, SetHostileAndEnterCombat (EnterCombat pairs if not all members in one
--      combat)
--   5. the IsEnemy matrix read every 0.5 s up to 3 s (live: SetRelation alone doesn't change it; it is right with
--      the combat call): every pair across the teams an enemy, none within, all members in one combat, else the job
--      fails with the matrix; no character on no team in that combat (the job fails naming each)
--   6. a mirror arena: arena.symmetry over the pairs again, statuses included (a twin on a surface reads INSURFACE)
--   7. Ar.begin(teams, {round_cap, positions, seed, turn_order}): positions = where each member stands, in the result
--      and arena_start; seed = the arena's (Arena.lua tie_fallback's side)
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
Setup.LINE_GAP_M = 1.5        -- a team's members this far apart on a line along x through its spawn (spike), unless
                              -- the arena's location.line_gap says otherwise
Setup.TIMEOUT_MS = 60000
-- A mirror arena's spots are cleared of surfaces this far around before a twin stands on one (live check 13d: Cedar
-- spawned on a surface at spot A and read INSURFACE, Dune not: the pair's statuses differed)
Setup.SURFACE_CLEAR_M = 2.0
Setup.SURFACE_LAYERS = { "Ground", "Cloud" } -- bg3se SurfaceLayer8
-- ... and the whole arena first: one clear per layer at the spawns' midpoint, out to half the spawns' distance plus a
-- line's half-length plus this (fairness audit, site 2: every match opened with Fire 2 m from B's line, 8 m from A's).
-- Then the surfaces must mirror across the midplane: a SURFACE_GRID_M grid on A's half within that radius, each tile
-- against its reflection on B's half, else setup fails naming them.
Setup.SURFACE_MARGIN_M = 6.0
Setup.SURFACE_GRID_M = 1.0
-- The clear reaches out to half the spawns' distance plus two turns of a Dash (2 x 9 m): seed-7 m2's later fights ran
-- into Fire 14.6-20 m from the midpoint, outside the checked radius (arena-fight-analysis.md, cross-review 7828a6f8).
-- Between the checked radius and this the surfaces are read too, report-only (the result's surfaces.wide), and the
-- whole grid again at round 2's start and at the match's end (Ar.reread_surfaces: arena_surfaces records).
Setup.SURFACE_REACH_M = 18.0
-- The checked radius must read at least this fraction of its tile pairs, else setup fails (a check that read a few
-- tiles checked little). 0.5 until the first live counts give a better floor (cross-review af5b2a91).
Setup.SURFACE_READ_MIN = 0.5
-- RemoveSurfaceLayerAtPosition lands after the call returns (live w10 L3: the check in the clear's own tick read 14
-- unmirrored pairs of 168, Fire and BloodSilver within the checked radius; the same load's setup 40 s later read them
-- cleared). The check reads again every SURFACE_EVERY_MS until it reads and mirrors, for at most SURFACE_WAIT_MS after
-- the clear, then fails closed with the counts.
Setup.SURFACE_WAIT_MS = 5000
Setup.SURFACE_EVERY_MS = 250
Setup.TEAM_MAX = 4             -- two teams of 4 fill the party of 8 the spike stood up
-- Spawn geometry (both sides stand on the same terms): the spawns at most this far apart in height, and every distance
-- across the lines the same both ways (A's member i to B's member j = B's i to A's j) within this
Setup.SPAWN_DY_MAX_M = 0.3
Setup.PAIR_DIST_TOL_M = 0.1
Setup.SITTER_MIN_M = 10.0 -- a sitter this far or more from every member of both lines (fairness audit S1-3)
-- AddExplorationExperience shows in the host's Experience within this. Not measured (live builds-1 read it seconds
-- later); the same later-tick bound as a kit's items. A grant that never lands fails setup.
Setup.XP_WAIT_MS = 5000
Setup.XP_EVERY_MS = 250

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

-- n positions on a line along x through spot, gap (default LINE_GAP_M) apart, centred on it (the probe's line_slots).
function Setup.line(spot, n, gap)
    local out = {}
    for i = 0, n - 1 do
        out[#out + 1] = { spot[1] + (i - (n - 1) / 2) * (gap or Setup.LINE_GAP_M), spot[2], spot[3] }
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
function Setup.check_geometry(spawns, n, gap)
    local dy = math.abs(spawns.A[2] - spawns.B[2])
    if dy >= Setup.SPAWN_DY_MAX_M then
        error(("location.spawns A and B are %.2f m apart in height (y); they must be under %.1f m"):format(dy,
            Setup.SPAWN_DY_MAX_M), 0)
    end
    Setup.check_cross(Setup.line(spawns.A, n, gap), Setup.line(spawns.B, n, gap), "location.spawns")
end

-- The ground and cloud surface names ("None" for none) of the tile at (x, z) on its own ground: the floor height there
-- nearest y (the side's spawn height; site 2 is not flat), or nil when no floor or no tile reads.
local function tile_surfaces(x, y, z)
    local ok, g, c = pcall(function()
        local h
        for _, v in ipairs(Ext.Level.GetHeightsAt(x, z) or {}) do
            if type(v) == "number" and (not h or math.abs(v - y) < math.abs(h - y)) then h = v end
        end
        local t = Ext.Level.GetTileDebugInfo({ x, assert(h), z })
        return tostring(t.GroundSurface), tostring(t.CloudSurface)
    end)
    if not ok then return nil end
    return g .. "/" .. c
end

-- The radius around the spawns' midpoint that the mirror check must find mirrored, for n per line.
function Setup.surface_radius(spawns, n, gap)
    return dist(spawns.A, spawns.B) / 2 + (n - 1) / 2 * (gap or Setup.LINE_GAP_M) + Setup.SURFACE_MARGIN_M
end

-- The radius the surface clear covers (and the report-only read reaches).
function Setup.surface_reach(spawns)
    return dist(spawns.A, spawns.B) / 2 + Setup.SURFACE_REACH_M
end

-- Whether the arena's surfaces mirror across the plane halfway between the spawns: every grid point p on A's half
-- with from < |p - midpoint| <= to (from: -1 = all) against p's reflection, each on its own ground (tile_surfaces).
-- A pair where either tile doesn't read is unread, not a mismatch. {points, read (pairs both read), radius = to,
-- off = {text}, off_keys = {"x,z" of A's tile} (off's order), keys = {"x,z" = true}}.
function Setup.check_surfaces(spawns, n, from, to, gap)
    local a, b = spawns.A, spawns.B
    local mx, mz = (a[1] + b[1]) / 2, (a[3] + b[3]) / 2
    local d = math.sqrt((b[1] - a[1]) ^ 2 + (b[3] - a[3]) ^ 2)
    local ux, uz = (b[1] - a[1]) / d, (b[3] - a[3]) / d
    local r, step = to or Setup.surface_radius(spawns, n, gap), Setup.SURFACE_GRID_M
    local r0 = from or -1
    local points, read, off, offk, keys = 0, 0, {}, {}, {}
    local k = math.floor(r / step)
    for i = -k, k do
        for j = -k, k do
            local x, z = mx + i * step, mz + j * step
            local along = (x - mx) * ux + (z - mz) * uz
            local d2 = (x - mx) ^ 2 + (z - mz) ^ 2
            if along < 0 and d2 <= r * r and (r0 < 0 or d2 > r0 * r0) then
                local x2, z2 = x - 2 * along * ux, z - 2 * along * uz
                local sa, sb = tile_surfaces(x, a[2], z), tile_surfaces(x2, b[2], z2)
                points = points + 1
                if sa and sb then read = read + 1 end
                if sa and sb and sa ~= sb then
                    offk[#off + 1] = ("%.1f,%.1f"):format(x, z)
                    keys[offk[#off + 1]] = true
                    off[#off + 1] = ("(%.1f, %.1f) on A's half reads %s, its mirror (%.1f, %.1f) %s"):format(x, z,
                        sa, x2, z2, sb)
                end
            end
        end
    end
    return { points = points, read = read, radius = r, off = off, off_keys = offk, keys = keys }
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
    return at
end

-- The sitters (fairness audit S1-3: bodies in the field, out of the fight) stand SITTER_MIN_M or more from every
-- member of both lines as landed (on the ground: x/z), else an error naming the sitter, the member and the distance.
-- A sitter whose position doesn't read is an error too.
function Setup.check_sitters(at, teams, sitters, names)
    for _, u in ipairs(sitters) do
        local ok, x, _, z = pcall(Osi.GetPosition, u)
        if not ok or type(x) ~= "number" then
            error(("the sitter %s (%s): its position doesn't read"):format(tostring(V.name(u)), u), 0)
        end
        for _, s in ipairs(Ar.SIDES) do
            for i, p in ipairs(at[s]) do
                local d = math.sqrt((p[1] - x) ^ 2 + (p[3] - z) ^ 2)
                if d < Setup.SITTER_MIN_M then
                    error(("the sitter %s stands %.1f m from %s (team %s's line); a sitter must stand %g m or more "
                        .. "from both lines"):format(tostring(V.name(u)), d, tostring(names[teams[s][i]]), s,
                        Setup.SITTER_MIN_M), 0)
                end
            end
        end
    end
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
        for i, p in ipairs(Setup.line(job.spawns[s], #t[s], job.line_gap)) do
            local u = t[s][i]
            call(job, "TeleportToPosition", { u, p[1], p[2], p[3], "", 0, 0, 0, 0, 1 }, { u, p[1], p[2], p[3], "" })
        end
    end
    return Setup.SETTLE_MS
end

STEPS[3] = function(job)
    Setup.check_sitters(Setup.check_landed(job.teams, job.names), job.teams, job.sitters, job.names)
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

-- Each pair with a reference: its A twin read against it. -> every failed pair's diff as text (job.reference: the rows)
function Setup.check_references(job)
    local rows, bad = {}, {}
    for i, p in ipairs(job.mirror.pairs) do
        if p.reference ~= nil then
            local u = job.teams.A[i]
            local d = Ar.Symmetry.reference_diff(Ar.Symmetry.profile(u), p.reference)
            rows[#rows + 1] = { pair = i, build = p.build, name = job.names[u], ok = d.ok, diffs = d.diffs,
                accepted = d.accepted, unread = d.unread }
            if not d.ok then
                bad[#bad + 1] = ("pair %d (%s, build %s): %s"):format(i, tostring(job.names[u]), tostring(p.build),
                    Ar.Symmetry.diff_text(d))
            end
        end
    end
    job.reference = rows
    return bad
end

-- Where each member stands (none when it doesn't read).
local function positions_of(t)
    local positions = {}
    for _, u in ipairs(all_of(t)) do
        local ok, x, y, z = pcall(Osi.GetPosition, u)
        if ok and type(x) == "number" then positions[u] = { x, y, z } end
    end
    return positions
end

STEPS[5] = function(job) -- the builds' references; hostile teams, one combat
    local t = job.teams
    if job.mirror then
        local bad = Setup.check_references(job)
        if #bad > 0 then
            RPC.finish_job(job, "failed", { teams = t, reference = job.reference, calls = job.calls,
                not_applied = job.not_applied, positions = positions_of(t) },
                "the twins are not their builds: " .. table.concat(bad, "; "))
            return
        end
    end
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
    -- nobody else in it: the sitters, any other character in the level, and any item (live 14: a Nautiloid Tank and
    -- a Cartilaginous Chest took turns in the combat; fairness audit S1-3)
    local member, out, others, item = set_of(all_of(t)), {}, {}, {}
    for _, u in ipairs(job.sitters) do others[#others + 1] = u end
    for _, comp in ipairs({ "ServerCharacter", "ServerItem" }) do
        for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent(comp) or {}) do
            local u = V.entityUuid(e)
            if u then
                others[#others + 1] = u:lower()
                if comp == "ServerItem" then item[u:lower()] = true end
            end
        end
    end
    for _, u in ipairs(others) do
        if not member[u] and osi(Osi.CombatGetGuidFor, u) == c then
            member[u] = true
            out[#out + 1] = tostring(V.name(u)) .. (item[u] and " (an item)" or "")
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
        RPC.finish_job(job, "failed", { teams = t, members = after, calls = job.calls,
            not_applied = job.not_applied, positions = positions_of(t) },
            ("levels after setup differ from the arena's level %d: %s"):format(job.level, table.concat(off, ", ")))
        return
    end
    if job.mirror then -- the pairs once more as the fight starts, statuses included (a surface under one twin)
        local pairs = {}
        for i = 1, #job.mirror.pairs do pairs[i] = { t.A[i], t.B[i] } end
        local sym = Ar.Symmetry.check(pairs, { start = true }) -- current and temp HP too (audit S3-8)
        if not sym.ok then
            RPC.finish_job(job, "failed", { teams = t, members = after, calls = job.calls,
                not_applied = job.not_applied, positions = positions_of(t) },
                "the twins are not mirrors as the fight starts: " .. table.concat(sym.problems, "; "))
            return
        end
    end
    local sitters, positions = {}, positions_of(t) -- where each member stands as the fight starts
    for _, u in ipairs(job.sitters) do sitters[#sitters + 1] = { uuid = u, name = V.name(u) } end
    local status = Ar.begin(t, { round_cap = job.round_cap, sitters = job.can_join, positions = positions,
        seed = job.arena.seed, turn_order = job.arena.turn_order, surfaces = job.mirror and { spawns = job.spawns,
            n = #job.mirror.pairs, setup_keys = job.surface_keys or {} } or nil,
        field = job.field })
    RPC.finish_job(job, "done", { teams = t, factions = job.factions, combat = c, members = after, sitters = sitters,
        enemy = { across = m.across .. "/" .. m.across_of, within = m.within .. "/" .. m.within_of,
            reads = job.enemy_reads }, seed = job.seed, karmic = job.karmic, spawn_order = job.spawn_order,
        positions = positions, surfaces = job.surfaces, field = job.field, xp = job.xp, reference = job.reference,
        not_applied = job.not_applied, calls = job.calls, status = status })
end

-- The host's total XP; error when it doesn't read (a tier's level can't be checked without it).
function Setup.host_xp(host)
    local ok, v = pcall(function() return V.entity(host).Experience.TotalExperience end)
    if not ok or type(v) ~= "number" then error("the host's XP doesn't read: " .. tostring(v), 0) end
    return v
end

-- A mirror arena with party_xp: the host's XP inside the tier's window before any twin exists.
STEPS[-4] = function(job)
    local px, have = job.mirror.party_xp, Setup.host_xp(job.host)
    if px.below and have >= px.below then
        error(("the host has %d XP, the arena's tier stops below %d (it starts at %d): no call takes XP away")
            :format(have, px.below, px.total), 0)
    end
    if not job.xp then
        job.xp = { before = have, total = px.total, below = px.below, added = 0 }
        if have < px.total then
            job.xp.added = px.total - have
            if not call(job, "AddExplorationExperience", { job.host, job.xp.added }) then
                error("AddExplorationExperience: " .. tostring(job.calls[#job.calls].error), 0)
            end
            job.xp_by = now_ms() + Setup.XP_WAIT_MS
        end
    end
    job.xp.after = have
    if have >= px.total then return 0 end
    if now_ms() >= job.xp_by then
        error(("the host reads %d XP %d ms after AddExplorationExperience of %d (it had %d, the tier needs %d)")
            :format(have, Setup.XP_WAIT_MS, job.xp.added, job.xp.before, px.total), 0)
    end
    job.step = job.step - 1 -- read again
    return Setup.XP_EVERY_MS
end

-- A mirror arena: the field's items made inert before the surfaces are cleared (a burning barrel makes a surface).
STEPS[-3] = function(job)
    local mid, radius = {}, Setup.surface_reach(job.spawns)
    for k = 1, 3 do mid[k] = (job.spawns.A[k] + job.spawns.B[k]) / 2 end
    job.field = Ar.Field.clean(mid, radius, job.host)
    return Ar.Field.SETTLE_MS
end

-- A mirror arena: the field read back, then the surfaces cleared (read back from SURFACE_EVERY_MS on: step -1).
STEPS[-2] = function(job)
    local mi = job.mirror
    local mid, radius = {}, Setup.surface_reach(job.spawns)
    for k = 1, 3 do mid[k] = (job.spawns.A[k] + job.spawns.B[k]) / 2 end
    job.field.left = Ar.Field.check(job.spawns, radius)
    for _, layer in ipairs(Setup.SURFACE_LAYERS) do -- the whole arena, then each spot (in case the radius is capped)
        if not call(job, "RemoveSurfaceLayerAtPosition", { mid[1], mid[2], mid[3], layer, radius }) then
            error(("RemoveSurfaceLayerAtPosition %s over the arena (%.1f m): %s"):format(layer, radius,
                tostring(job.calls[#job.calls].error)), 0)
        end
    end
    for i = 1, #mi.pairs do -- every spot free of surfaces first: a pair stands on the same ground
        for _, s in ipairs(Ar.SIDES) do
            local p = Setup.line(job.spawns[s], #mi.pairs, job.line_gap)[i]
            for _, layer in ipairs(Setup.SURFACE_LAYERS) do
                local args = { p[1], p[2], p[3], layer, Setup.SURFACE_CLEAR_M }
                if not call(job, "RemoveSurfaceLayerAtPosition", args) then
                    error(("RemoveSurfaceLayerAtPosition %s at pair %d's spot %s: %s"):format(layer, i, s,
                        tostring(job.calls[#job.calls].error)), 0)
                end
            end
        end
    end
    job.cleared_at = now_ms()
    return Setup.SURFACE_EVERY_MS
end

-- A mirror arena: the surfaces read until they read and mirror (at most SURFACE_WAIT_MS after the clear), then the
-- twins, spawned and given their kits (their kit jobs run on their own).
STEPS[-1] = function(job)
    local mi, M = job.mirror, Ar.Mirror
    local radius = Setup.surface_reach(job.spawns)
    local sc = Setup.check_surfaces(job.spawns, #mi.pairs, nil, nil, job.line_gap)
    local short = sc.read < Setup.SURFACE_READ_MIN * sc.points
    local waited = now_ms() - job.cleared_at
    if (short or #sc.off > 0) and waited < Setup.SURFACE_WAIT_MS then
        job.step = job.step - 1 -- read again
        return Setup.SURFACE_EVERY_MS
    end
    job.kits = {}
    local wide = Setup.check_surfaces(job.spawns, #mi.pairs, sc.radius, radius) -- report-only
    job.surfaces = { tiles_total = sc.points, tiles_read = sc.read, radius = sc.radius, off = #sc.off,
        wide = { tiles_total = wide.points, tiles_read = wide.read, radius = wide.radius, off = #wide.off,
            tiles = { table.unpack(wide.off, 1, 5) } } }
    job.surface_keys = wide.keys -- the re-reads tell these setup mismatches from surfaces made during the match
    if short then -- fails closed: a check that read little checked little
        error(("only %d of %d tile pairs on A's half within %.1f m of the arena's midpoint read their surfaces (under "
            .. "%d%%) %.1f s after the clear"):format(sc.read, sc.points, sc.radius, Setup.SURFACE_READ_MIN * 100,
            waited / 1000), 0)
    end
    if #sc.off > 0 then
        local shown = { table.unpack(sc.off, 1, 5) }
        error(("the surfaces don't mirror %.1f s after the clear (%d of the %d tile pairs read within %.1f m differ): "
            .. "%s"):format(waited / 1000, #sc.off, sc.read, sc.radius, table.concat(shown, "; ")), 0)
    end
    for i, p in ipairs(mi.pairs) do
        local spots = {}
        for k, s in ipairs(Ar.SIDES) do spots[k] = Setup.line(job.spawns[s], #mi.pairs, job.line_gap)[i] end
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
        local na = type(k.result) == "table" and k.result.not_applied or nil
        if na and #na > 0 then
            job.not_applied = job.not_applied or {}
            job.not_applied[#job.not_applied + 1] = { pair = i, build = job.mirror.pairs[i].build, parts = na }
        end
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

-- mirror.party_xp, when given: {total = the tier's XP total, below = the next level's total (none at the top)}.
function Setup.check_party_xp(px)
    if px == nil then return end
    local function count(v) return type(v) == "number" and v % 1 == 0 and v >= 0 end
    if type(px) ~= "table" or not count(px.total)
        or (px.below ~= nil and not (count(px.below) and px.below > px.total)) then
        error("arena.mirror.party_xp must be {total, below?}: XP totals, below above total", 0)
    end
end

-- A mirror arena's section, checked at once (the kits' own lists are checked by arena.mirror_kit): 1..TEAM_MAX
-- pairs, each a root template, its race, a stats entry, two names (unique across the arena) and a kit whose race part
-- is for the pair's race (each pair may have its own race: a build's); arena.teams, if given, names pair i's twins at
-- index i.
function Setup.check_mirror(mi, teams)
    local ps = type(mi) == "table" and mi.pairs or nil
    if type(ps) ~= "table" or #ps < 1 or #ps > Setup.TEAM_MAX then
        error("arena.mirror.pairs must list 1 to " .. Setup.TEAM_MAX .. " pairs", 0)
    end
    Setup.check_party_xp(mi.party_xp)
    local seen = {}
    for i, p in ipairs(ps) do
        if type(p) ~= "table" or type(p.template) ~= "string" or type(p.race) ~= "string" or type(p.stats) ~= "string"
            or type(p.kit) ~= "table" or type(p.names) ~= "table" then
            error("arena.mirror.pairs[" .. i .. "] needs template, race, stats, names and kit", 0)
        end
        Ar.Mirror.check_kit_race(p.kit, p.race, "pair " .. i .. ": the kit")
        if p.reference ~= nil and (type(p.reference) ~= "table" or type(p.reference.profile) ~= "table") then
            error("arena.mirror.pairs[" .. i .. "].reference needs its profile", 0)
        end
        -- only a reference shows a twin holds these
        for _, k in ipairs({ "subclass", "grants", "prepared", "unlocks" }) do
            if p.kit[k] ~= nil and p.reference == nil then
                error(("pair %d: its kit's %s has no reference to be checked against"):format(i, k), 0)
            end
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
    local lineGap = type(arena.location) == "table" and arena.location.line_gap or nil
    if lineGap ~= nil and (type(lineGap) ~= "number" or lineGap <= 0) then
        error("location.line_gap must be a number above 0 (metres)", 0)
    end
    Setup.check_geometry(spawns, mirror and #mirror.pairs or #teams.A, lineGap)
    if args.round_cap ~= nil and not tonumber(args.round_cap) then error("round_cap must be a number", 0) end
    if arena.turn_order ~= nil and not Ar.TURN_ORDERS[arena.turn_order] then
        error("turn_order must be \"alternate\" or \"snake\"", 0)
    end
    local level = arena.level
    if type(level) ~= "number" or level % 1 ~= 0 or level < 1 or level > 12 then
        error("arena.level must be the members' character level (an integer 1..12)", 0)
    end
    local host = V.uuid(osi(Osi.GetHostCharacter))
    if not host then error("no host character", 0) end
    local job = RPC.start_job("arena.setup", { timeout_ms = Setup.TIMEOUT_MS, on_tick = tick, arena = arena,
        teams = teams, names = names, spawns = spawns, line_gap = lineGap, host = host:lower(), avatars = avatarList,
        round_cap = tonumber(args.round_cap or arena.round_cap), level = level,
        step = mirror and (mirror.party_xp and -4 or -3) or 1, next_at = 0,
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
