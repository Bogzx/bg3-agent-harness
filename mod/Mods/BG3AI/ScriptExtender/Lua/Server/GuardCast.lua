-- Guard, part 4 of 4 (Guard.lua loads the parts in order; split for the 1000-line cap):
-- ranges, reach, jumps, line of sight, blasts, blocking statuses and check_cast.
local U = BG3AI.Util
local G = BG3AI.Guard
local I = G.inner
local b, spell_flags = I.b, I.spell_flags

-- Range (m) of a weapon slot's equipped weapon from its stats (WeaponRange is
-- in cm: 150 = melee 1.5 m, 1800 = longbow 18 m), or nil.
local function weapon_range(char, slot)
    if not char then return nil end
    local item = G.q("GetEquippedItem", char, slot)
    if not item then return nil end
    local name = G.q("GetStatString", item)
    local st = name and Ext.Stats.Get(name)
    local ok, wr = pcall(function() return st and tonumber(st.WeaponRange) end)
    if ok and wr and wr > 0 then return wr / 100 end
    return nil
end

-- Resolve the range of a spell in metres. Returns range, melee (bool), or
-- nil if unknown. Weapon-range spells (attacks) use the equipped weapon.
-- Live (bg3le): Target_MainHandAttack/Target_OffhandAttack/Target_ConcussiveSmash
-- have TargetRadius "MeleeMainWeaponRange" and Range 0 (Range is 0 on every
-- spell probed, never a real range), Projectile_MainHandAttack
-- "RangedMainWeaponRange", Target_Shove 1.5, Target_HolyRebuke 18;
-- WPN_Warhammer/WPN_Longsword WeaponRange 150, WPN_Shortbow 1800.
G.DEFAULT_RANGED_M = 18.0 -- bows/crossbows when the weapon's stats are unreadable
G.DEFAULT_MELEE_M = 1.5   -- unarmed / unreadable melee weapon
function G.spell_range(st, char)
    if not st then return nil end
    local tr = tostring(st.TargetRadius or "")
    if tr:find("Melee") then
        local slot = tr:find("OffHand") and "Melee Offhand Weapon" or "Melee Main Weapon"
        return weapon_range(char, slot) or weapon_range(char, "Melee Main Weapon") or G.DEFAULT_MELEE_M, true
    end
    if tr:find("Ranged") then
        local slot = tr:find("OffHand") and "Ranged Offhand Weapon" or "Ranged Main Weapon"
        return weapon_range(char, slot) or weapon_range(char, "Ranged Main Weapon") or G.DEFAULT_RANGED_M, false
    end
    local r = tonumber(st.TargetRadius)
    if r and r > 0 then return r, r <= 3 end
    r = tonumber(st.Range)
    if r and r > 0 then return r, r <= 3 end
    return nil
end

-- Bounding radius of a character (melee reach is edge to edge, positions are
-- centres). bg3le has no Bound.Bound.Radius; the per-type radii live in
-- Bound.Bound.AIBounds.{Move,Hit,Stand}. Stand is the footprint the engine's
-- AiPath uses for bounds (Ai.inl SetSourceEntity). Live (Nautiloid): medium
-- creatures Stand 0.50 (Move 0.25, Hit 0.20), imps/devourers 0.45-0.49, a hell
-- boar 0.70. Falls back to Move, then a medium creature's 0.5 m.
function G.radius(uuid)
    local e = G.entity(uuid)
    local ok, r = pcall(function()
        local b = e.Bound.Bound.AIBounds
        return (b.Stand and b.Stand.Radius) or (b.Move and b.Move.Radius)
    end)
    if ok and type(r) == "number" and r > 0 and r < 5 then return r end
    return 0.5
end

-- The bound melee reach is measured by: AIBounds.Move (window 9: every creature in the Balthazar fight read Move
-- 0.249, Hit 0.200, Stand 0.499); a medium creature's 0.25 m when it doesn't read. A fallback is never silent: one
-- bg3le warning per entity, and the count goes into the bridge's mem metrics record (sys.mem hit_fallbacks ->
-- hit_radius_fallbacks: names from the Hit model, kept so the metric stays one series).
G.REACH_RADIUS_M = 0.25
G.hit_fallback = G.hit_fallback or {} -- guid -> true: warned once
G.hit_fallback_n = G.hit_fallback_n or 0
function G.reach_radius(uuid)
    local e = G.entity(uuid)
    local ok, r = pcall(function() return e.Bound.Bound.AIBounds.Move.Radius end)
    if ok and type(r) == "number" and r > 0 and r < 5 then return r end
    local k = G.guid(uuid) or tostring(uuid)
    if not G.hit_fallback[k] then
        G.hit_fallback[k] = true
        G.hit_fallback_n = G.hit_fallback_n + 1
        local okn, name = pcall(function() return U.loca(G.q("GetDisplayName", uuid)) end)
        Ext.Utils.PrintWarning(string.format("[BG3AI] melee reach: no AIBounds.Move radius for %s (%s): %.2f m assumed"
            .. " (a medium creature's)", tostring(okn and name or "?"), k, G.REACH_RADIUS_M))
    end
    return G.REACH_RADIUS_M
end

-- Melee reach is edge to edge by the Move bounds: the furthest centre distance at which a weapon (or Shove) with
-- `range` m reaches `target`. The one measure for the guard's range check (check_cast), move_to's stop and its
-- already-there test (ActionsMove), the cast's walk into range (ActionsCast) and the refusal text (Actions).
-- Window 8 (both check flags dropped, the engine measures): Karlach's 1.5 m greataxe on an Apostate Destroyer was
-- refused (InvalidTarget) from 2.1 m centres 10/10 and cast from 1.4 m; the Stand radii gave 2.5 m (1.5 + 0.5 +
-- 0.5). Window 9 measured the edge: from 2.0 m (2.005 read) the engine cast, from 2.05 m it refused InvalidTarget;
-- the Move radii give 1.5 + 0.249 + 0.249 = 1.998 m (the Hit radii's 1.9 m refused 2.0 m: 0.1 m too strict).
function G.melee_reach(char, target, range)
    return range + G.reach_radius(char) + G.reach_radius(target)
end

-- The range of the character's own melee weapon (Main Hand Attack's), the default for a plain move_to.
function G.weapon_melee_range(char)
    local r = G.spell_range(Ext.Stats.Get("Target_MainHandAttack"), char)
    return r or G.DEFAULT_MELEE_M
end

-- Jump distance (Projectile_Jump has no numeric range, so the guard caps it).
-- Its stats TargetRadius is 4.5 (live, bg3le). BG3: about half the Strength
-- score in metres; Enhance Leap triples it, Step of the Wind doubles it.
-- UNVERIFIED formula and status ids: tune here.
G.JUMP_SPELL = "Projectile_Jump"
G.JUMP_MIN_M = 4.0
G.JUMP_SLACK_M = 0.5
G.JUMP_MULTIPLIERS = { ENHANCE_LEAP = 3, STEP_OF_THE_WIND = 2, STEP_OF_THE_WIND_DASH = 2 }
-- Statuses that change a jump through their stats Boosts: JumpMaxDistanceMultiplier(k), IgnoreFallDamage(). S086/S087
-- (the Astral Prism, END_Main): every party member has Astral Gravity ("Immune to Falling damage and its Jump distance
-- is tripled", bg3.wiki); jump_to refused the island hops at "this character can jump 9.5 m" and route/ways never
-- offered them (a 4.5 m gap, 4 m down), while the jumps the agent then made by hand landed (15.2 m out, 6.7 m down).
-- Its status id and Boosts string are UNVERIFIED, so its display name (live: "Astral Gravity") counts too.
G.JUMP_STATUS_NAMES = { ["Astral Gravity"] = { mult = 3, no_fall = true } }
G._jumpBoosts = G._jumpBoosts or {} -- status id -> {mult, no_fall} or false (stats don't change in a session)
local function jumpBoosts(id)
    local c = G._jumpBoosts[id]
    if c ~= nil then return c end
    c = false
    local ok, s = pcall(function() return tostring(Ext.Stats.Get(id).Boosts or "") end)
    if ok then
        local k = tonumber(s:match("JumpMaxDistanceMultiplier%(%s*([%d%.]+)%s*%)"))
        local nf = s:find("IgnoreFallDamage", 1, true) ~= nil
        if (k and k > 1) or nf then c = { mult = k and k > 1 and k or 1, no_fall = nf or nil } end
    end
    G._jumpBoosts[id] = c
    return c
end
-- -> jump multiplier (1 = none), no fall damage (true/false) from char's active statuses
function G.jump_status(char)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return 1, false end
    local mult, noFall = 1, false
    for _, s in ipairs(V.statuses(e) or {}) do
        local j = G.JUMP_STATUS_NAMES[s.name] or jumpBoosts(s.id)
        if j then
            if (j.mult or 1) > mult then mult = j.mult end
            noFall = noFall or j.no_fall == true
        end
    end
    return mult, noFall
end
function G.jump_max(char)
    local str = tonumber((G.q("GetAbility", char, "Strength"))) or 10
    local m = math.max(G.JUMP_MIN_M, str / 2)
    local mult = 1
    for status, k in pairs(G.JUMP_MULTIPLIERS) do
        if b(G.q("HasActiveStatus", char, status)) and k > mult then mult = k end
    end
    local ok, k = pcall(G.jump_status, char)
    if ok and type(k) == "number" and k > mult then mult = k end
    return m * mult
end

-- Jumping down carries further: Projectile_Jump has the HasHighGroundRangeExtension flag, and the game's Data.txt
-- has HighGroundThreshold 2.3 / HighGroundRangeMultiplier 1, so the reach grows by the drop. PROVED S040 (22:02-22:10,
-- the game's own jump, Z + click): (-443.7,-6.9,-1416.5) -> ~(-470,-30,-1405) = 28 m out, 23 m down, Str 17 (flat
-- 8.5); (-489.2,-38.6,-1383.7) -> ~(-507,-56.5,-1385) = 18 m out, 18 m down. Our 8.5 m cap refused both ("too far to
-- jump (18.1 m, this character can jump 8.5 m)"), and the agent hand-clicked 33 jumps. (S039's "Target is too far!"
-- cursor reading over the same gap was a hover at the void.) jump_floor_ok still refuses void/air landings.
G.JUMP_UP_LIMIT_M = 6.0 -- generous (N.JUMP_UP_MAX_M 2.5 is the planner's UNVERIFIED climb); only absurd climbs refused
G.HIGH_GROUND_THRESHOLD_M = 2.3
G.HIGH_GROUND_JUMP_MULT = 1
function G.jump_reach(char, from, pos)
    local m = G.jump_max(char)
    if from and pos then
        local drop = from[2] - pos[2]
        if drop > G.HIGH_GROUND_THRESHOLD_M then return m + drop * G.HIGH_GROUND_JUMP_MULT, m end
    end
    return m, m
end

-- A jump landing must have ground under it near its height, even with force: S038 20:48 and S039 21:53 the game
-- froze (an engine allocation to 13 GB, OOM-killed) on a jump cast at a column with no AI-grid floor at all (the
-- Shadowfell's void, (-408.5,1,-1437) and (-414,20,-1447)); a forced jump into the air over a Chasm was a fatal fall
-- (S035/S038). The player's cursor refuses both.
G.JUMP_FLOOR_SLACK_M = 4.0
function G.jump_floor_ok(pos)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, pos[1], pos[3])
    if not ok or type(hs) ~= "table" then return end -- can't tell
    local near
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - pos[2]) <= G.JUMP_FLOOR_SLACK_M then near = true end
    end
    if #hs == 0 then
        G.refuse(string.format("no ground at all under (%.1f,%.1f,%.1f) (the void): a jump there froze the game"
            .. " twice", pos[1], pos[2], pos[3]))
    elseif not near then
        G.refuse(string.format("the landing (%.1f,%.1f,%.1f) is in the air: no floor within %.0f m of that height;"
            .. " give the floor's own y (get_layout / route)", pos[1], pos[2], pos[3], G.JUMP_FLOOR_SLACK_M))
    end
end

-- HasLineOfSight(char, item) is false for a door, chest or barricade the
-- character stands next to (its own collision blocks the ray: S024/S025 Knock
-- on 3 locked doors from 1.5-2 m, attacks on a barricade and a door, 10 casts
-- refused). An item that passed G.visible this close is in plain sight.
G.ITEM_LOS_NEAR_M = 4.0

-- Farther away the same holds (S083: Knock on e284 Threshold of Loss from 6.3 m, its position 5.9 m up at the
-- door's top; Eldritch Blast on the e703 sewer Wall; Knock on e720 from ~4 m: all refused "no line of sight" while
-- the game's cursor showed 100 %, ~48 hand inputs). HasLineOfSight aims at the item's position, which its own
-- collision or frame hides. An item is in sight from the caster when a physics ray from the caster's eye to a
-- point on a column through its position hits the item itself first (nothing stands between them), or reaches a
-- point inside its AI bounds unblocked (Visibility's large-object rule). Characters keep the engine's check.
-- nil = no usable ray API (uncalibrated / broken): the refusal stands.
G.ITEM_RAY_FRACS = { 0.0, 0.5, 0.85, -0.5, -1.0 } -- x the bounds height from the position; < 0 counts self hits only
G.ITEM_RAY_SLACK = 0.3

local function ray_hit_uuid(hit)
    local ok, u = pcall(function() return hit.Shape.PhysicsObject.Entity.Uuid.EntityUuid end)
    return ok and type(u) == "string" and u:lower() or nil
end

local function ray_hit_dist(hit, from)
    local ok, d = pcall(function() local p = hit.Position; return U.dist(from, { p[1], p[2], p[3] }) end)
    if ok and type(d) == "number" then return d end
    local okd, dd = pcall(function() return hit.Distance end)
    return okd and type(dd) == "number" and dd or nil
end

function G.item_ray_los(char, target)
    local V = BG3AI.Visibility
    local cast = Ext.Level and Ext.Level.RaycastClosest
    if type(V) ~= "table" or V.rayBroken or type(V.rayCalibrated) ~= "function" or not cast then return nil end
    local p, t, uuid = G.pos(char), G.pos(target), G.guid(target)
    if not (p and t and uuid) then return nil end
    local okc, cal = pcall(V.rayCalibrated, { pos = p })
    if not (okc and cal) then return nil end
    local okb, bd = pcall(function() return type(V.bounds) == "function" and V.bounds(G.entity(target)) or nil end)
    bd = okb and bd or nil
    local h = math.max(bd and bd.height or 2.0, 0.5)
    local r = math.max(bd and bd.radius or 0.5, 0.3)
    local eye = { p[1], p[2] + (V.EYE_H or 1.6), p[3] }
    local pts = {}
    for _, f in ipairs(G.ITEM_RAY_FRACS) do pts[#pts + 1] = { { t[1], t[2] + h * f, t[3] }, f >= 0 } end
    local dx, dz = t[1] - eye[1], t[3] - eye[3]
    local len = math.sqrt(dx * dx + dz * dz)
    if len > 0.01 then
        local ux, uz, side, pull, mid = dx / len, dz / len, math.min(r * 0.8, 1.5), math.min(r, 0.75), t[2] + h * 0.5
        pts[#pts + 1] = { { t[1] - uz * side, mid, t[3] + ux * side }, true }
        pts[#pts + 1] = { { t[1] + uz * side, mid, t[3] - ux * side }, true }
        pts[#pts + 1] = { { t[1] - ux * pull, mid, t[3] - uz * pull }, true }
    end
    local ok, res = pcall(function()
        for _, s in ipairs(pts) do
            local pt, inside = s[1], s[2]
            local hit = cast(eye, pt, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
            if hit == nil then
                if inside then return true end
            elseif ray_hit_uuid(hit) == uuid then
                return true
            elseif inside then
                local d = ray_hit_dist(hit, eye)
                if d and d >= U.dist(eye, pt) - G.ITEM_RAY_SLACK then return true end
            end
        end
        return false
    end)
    if not ok then return nil end
    return res
end

-- Line of sight from char to target for a cast: the engine's, else (items only) arm's length or the rays above.
function G.los_ok(char, target, d)
    if b(G.q("HasLineOfSight", char, target)) then return true end
    if b(G.q("IsCharacter", target)) then return false end
    if not d then
        local p, t = G.pos(char), G.pos(target)
        d = p and t and U.dist(p, t)
    end
    if d and d <= G.ITEM_LOS_NEAR_M then return true end
    return G.item_ray_los(char, target) == true
end

-- S085 05:03:37: Gale's Fireball at a point ~10 m below (-27,17,736) from the Guildhall's upper floor hit the ledge
-- and burst on e606 Lady Croup, a neutral Guild ally on the caster's floor (listed in get_combat_state): killed. A
-- player sees the aim preview before the click: the path, where it bursts, who stands in the ring. A harmful area
-- spell (radius = ExplodeRadius / AreaRadius) is refused, saying where it bursts and who is in the blast there, when
-- the blast would catch a party member, ally or neutral the party sees, or when a projectile aimed at a point bursts
-- farther than its radius from it (the straight eye ray to the point hits something first). force=true casts anyway.
-- The path is the straight line from the caster's eye; the game's projectile may arc. No usable ray API: no path
-- check (never refused for that), the ring at the point is still checked.
G.BLAST_TYPES = { Projectile = true, Target = true }
G.BLAST_LIFT_M = 0.5  -- the path ray ends this far above the point (a floor point grazes its own floor)
G.BLAST_SLACK_M = 1.0 -- a hit this close to the ray's end is the point itself

function G.blast_radius(st)
    local r = 0
    for _, f in ipairs({ "ExplodeRadius", "AreaRadius" }) do
        local ok, v = pcall(function() return tonumber(st[f]) end)
        if ok and v and v > r then r = v end
    end
    return r
end

-- Where a projectile aimed at `aim` bursts: aim (clear way or no ray API), else the eye ray's first hit.
-- Returns the point, blocked, the hit entity's uuid.
function G.blast_path(char, aim)
    local V = BG3AI.Visibility
    local cast = Ext.Level and Ext.Level.RaycastClosest
    local p = G.pos(char)
    if type(V) ~= "table" or V.rayBroken or type(V.rayCalibrated) ~= "function" or not cast or not p then
        return aim, false
    end
    local okc, cal = pcall(V.rayCalibrated, { pos = p })
    if not (okc and cal) then return aim, false end
    local eye = { p[1], p[2] + (V.EYE_H or 1.6), p[3] }
    local tgt = { aim[1], aim[2] + G.BLAST_LIFT_M, aim[3] }
    local ok, hit = pcall(cast, eye, tgt, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
    if not ok or hit == nil then return aim, false end
    local len, d = U.dist(eye, tgt), ray_hit_dist(hit, eye)
    if not d or d >= len - G.BLAST_SLACK_M then return aim, false end
    local okp, hp = pcall(function() local q = hit.Position; return { q[1], q[2], q[3] } end)
    if not (okp and type(hp[1]) == "number") then
        local f = d / len
        hp = { eye[1] + (tgt[1] - eye[1]) * f, eye[2] + (tgt[2] - eye[2]) * f, eye[3] + (tgt[3] - eye[3]) * f }
    end
    return hp, true, ray_hit_uuid(hit)
end

-- Living characters the party sees within r of pt (skip: the cast's own target), nearest first:
-- {uuid, name, side = party/ally/neutral/hostile, d}. nil without Visibility.
function G.in_blast(char, pt, r, skip)
    local V = BG3AI.Visibility
    if type(V) ~= "table" or type(V.charactersNear) ~= "function" then return nil end
    local ctx = V.context()
    local out = {}
    for _, row in ipairs(V.charactersNear(pt, r)) do
        local u = G.guid(V.entityUuid(row[1]) or "")
        if u and u ~= skip and not G.is_dead(u)
            and (G.is_party(u) or V.characterVisible(ctx, u, row[1], row[2])) then
            -- an arena: the other team is hostile from the team tables, whatever the engine says
            local Ar = BG3AI.Arena
            local side = (G.is_party(u) and "party")
                or ((b(G.q("IsEnemy", char, u)) or (U.side() and Ar.side_of(u))) and "hostile")
                or (V.isAlly(ctx, u) and "ally") or "neutral"
            out[#out + 1] = { uuid = u, name = V.name(u), side = side, d = row[3] }
        end
    end
    table.sort(out, function(x, y) return x.d < y.d end)
    return out
end

-- Window 9 MR2 (live.md): in combat the game's path preview marks the point where a path leaves an enemy's reach
-- ring ("Provokes Attack of Opportunity" in its tooltip) and shows nothing for an enemy whose reaction is spent. The
-- same facts for a path (its points): each enemy the mover's side can see, in combat, standing, with its reaction
-- left, whose melee reach (its main hand's, edge to edge) the path leaves: {{uuid, name, at, along_m}} by along_m,
-- nil out of combat. An enemy is the other team in a match, else one the game calls the mover's enemy. Only what the
-- engine would attack: none while the mover has a status with IgnoreLeaveAttackRange() (Disengage's boost); no enemy
-- with an INCAPACITATED status (stunned, asleep, paralysed …) or one blocking its reaction
-- (ActionResourceBlock(ReactionActionPoint)), that the game doesn't call the mover's enemy (Osi.IsEnemy) or that can't
-- see the mover (Osi.CanSee); no leaving on a jump or climb link (portals: the points a link ends at).
G.AOO_NEAR_M = 5.0 -- only characters this near a path point can have it in reach
local function foe_of(char, u)
    local Ar = BG3AI.Arena
    if Ar and Ar.active and Ar.active() and Ar.side_of then
        local x, y = Ar.side_of(char), Ar.side_of(u)
        if x and y then return x ~= y end
    end
    return not G.is_party(u) and b(G.q("IsEnemy", char, u))
end

local function reaction_left(u)
    local r = G.resources(u).ReactionActionPoint
    return r ~= nil and (r[0] or 0) > 0
end

-- Every active status's stats entry (technical ones too: a boost can sit on a hidden status).
local function status_stats(u)
    local out = {}
    local e = G.entity(u)
    local sc = e and U.comp(e, "StatusContainer")
    if not sc then return out end
    pcall(function()
        for _, id in pairs(sc.Statuses) do
            local ok, st = pcall(Ext.Stats.Get, tostring(id))
            if ok and st then out[#out + 1] = st end
        end
    end)
    return out
end

local function has_boost(u, boost)
    for _, st in ipairs(status_stats(u)) do
        local ok, bs = pcall(function() return tostring(st.Boosts or "") end)
        if ok and bs:find(boost, 1, true) then return true end
    end
    return false
end

local function cant_react(u)
    for _, st in ipairs(status_stats(u)) do
        local ok, typ = pcall(function() return tostring(st.StatusType or "") end)
        if ok and typ == "INCAPACITATED" then return true end
    end
    return has_boost(u, "ActionResourceBlock(ReactionActionPoint)")
end

-- A jump or climb link on the path provokes like the walk around it: the game's Interrupt_AttackOfOpportunity fires
-- OnLeaveAttackRange and is skipped only for a move whose spell has SpellFlags IgnoreAoO (Shared.pak
-- Public/Shared/Stats/Generated/Data/Interrupt.txt:451-464); Projectile_Jump's flags have none (Spell_Projectile.txt
-- :1469-1499; only Projectile_Jump_Cloaker and Projectile_Fly_TempestuousMagic carry it), and a link is no spell.
function G.path_provokes(char, points)
    local V = BG3AI.Visibility
    if not (char and G.in_combat(char)) or type(points) ~= "table" or #points < 2 then return nil end
    local me, ctx, out = G.guid(char), V.context(), {}
    if has_boost(me, "IgnoreLeaveAttackRange()") then return out end -- Disengaged: no one's reach provokes
    local attack = Ext.Stats.Get("Target_MainHandAttack")
    for _, row in ipairs(V.charactersNear(points, G.AOO_NEAR_M)) do
        local u = G.guid(V.entityUuid(row[1]) or "")
        if u and u ~= me and not G.is_dead(u) and not G.is_downed(u) and G.in_combat(u) and foe_of(me, u)
            and V.characterVisible(ctx, u, row[1], row[2]) and reaction_left(u) and not cant_react(u)
            and b(G.q("IsEnemy", u, me)) and b(G.q("CanSee", u, me)) then
            local ep = G.pos(u)
            local reach = G.melee_reach(u, me, G.spell_range(attack, u) or G.DEFAULT_MELEE_M)
            local along, inside = 0, U.dist(points[1], ep) <= reach
            for i = 2, #points do
                local p, q = points[i - 1], points[i]
                local seg, qin = U.dist(p, q), U.dist(q, ep) <= reach
                if inside and not qin then -- the crossing, halved
                    local lo, hi = 0, 1
                    for _ = 1, 12 do
                        local m = (lo + hi) / 2
                        local pt = { p[1] + (q[1] - p[1]) * m, p[2] + (q[2] - p[2]) * m, p[3] + (q[3] - p[3]) * m }
                        if U.dist(pt, ep) <= reach then lo = m else hi = m end
                    end
                    local t = (lo + hi) / 2
                    local at = { p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t, p[3] + (q[3] - p[3]) * t }
                    out[#out + 1] = { uuid = u, name = V.name(u), along_m = U.round(along + seg * t, 1),
                        at = U.vec(at, 1) }
                    break
                end
                inside, along = qin, along + seg
            end
        end
    end
    table.sort(out, function(x, y) return x.along_m < y.along_m end)
    return out
end

local function fmt_pos(p) return string.format("(%.1f,%.1f,%.1f)", p[1], p[2], p[3]) end

function G.blast_risk(char, st, flags, spell, target, pos)
    local kind = tostring(st.SpellType or "")
    if not flags.IsHarmful or not G.BLAST_TYPES[kind] then return end
    local r = G.blast_radius(st)
    local aim = pos or (target and not G.same(char, target) and G.pos(target))
    if r <= 0 or not aim then return end
    local at, blocked, what = aim, false, nil
    if pos and kind == "Projectile" then at, blocked, what = G.blast_path(char, pos) end
    local short = blocked and U.dist(at, aim) > r
    local hit = G.in_blast(char, at, r, target and G.guid(target)) or {}
    local friends, hostile = {}, 0
    for _, h in ipairs(hit) do
        if h.side == "hostile" then hostile = hostile + 1
        else friends[#friends + 1] = string.format("%s %s (%s, %.1f m)", h.uuid, h.name, h.side, h.d) end
    end
    if #friends == 0 and not short then return end
    local name = U.loca(st.DisplayName) or spell
    local head
    if blocked then
        local on = what and b(G.q("IsItem", what)) and BG3AI.Visibility.name(what)
        local p = G.pos(char)
        head = string.format("%s: the way to that point is blocked: it would burst at %s%s, %.1f m from the caster"
            .. " and %.1f m from the point", name, fmt_pos(at), on and on ~= "Unknown" and (" on " .. on) or "",
            p and U.dist(p, at) or 0, U.dist(at, aim))
    else
        head = string.format("%s at %s", name, target and "that target" or "that point")
    end
    local ring = string.format("in its blast (%g m) there: ", r)
    if #friends > 0 then
        ring = ring .. table.concat(friends, ", ") .. (hostile > 0 and (" and " .. hostile .. " hostile") or "")
    else
        ring = ring .. (hostile > 0 and (hostile .. " hostile only") or "nobody")
    end
    G.refuse(head .. "; " .. ring .. " (force=true casts anyway)")
end

-- Extra Attack (live stats, 2026-10-10): after a weapon attack the ExtraAttack passive applies EXTRA_ATTACK,
-- whose boost is UnlockSpellVariant(ExtraAttackCheck(), ModifyUseCosts(Replace,ActionPoint,0,0,ActionPoint))
-- and which goes on the next cast (RemoveEvents OnSpellCast): the next weapon attack costs no action. We
-- checked the raw UseCosts (ActionPoint:1), so every Extra Attack was refused "not enough action" (S023-S028:
-- 0 of 7). A weapon attack here = what the game's ExtraAttackSpellCheck takes, with an Action to free: main-hand
-- attacks, Divine Smite, Reckless Attack, a weapon action (read live, Hamstring Shot), an unarmed attack, a throw.
-- Every status with that boost at no cost, in the game's own order (HasHigherPriorityExtraAttackQueued). Fighter 11+
-- has EXTRA_ATTACK_2 first and EXTRA_ATTACK after it. Not here: EXTRA_ATTACK_WAR_MAGIC and _WAR_PRIEST make the attack
-- cost a bonus action, not nothing; MIND_MASTERY's holder can't cast.
G.EXTRA_ATTACK_STATUSES = { "EXTRA_ATTACK_2", "EXTRA_ATTACK", "MAG_MARTIAL_EXERTION", "WILDSTRIKE_EXTRA_ATTACK",
    "STALKERS_FLURRY", "EXTRA_ATTACK_THIRSTING_BLADE", "WILDSTRIKE_2_EXTRA_ATTACK" }
local EXTRA_ATTACK_ROLLS = { "WeaponAttack", "UnarmedAttack", "ThrowAttack" }
local EXTRA_ATTACK_SPELLS = { Target_CommandersStrike = true, Target_Bufotoxin_Frog_Summon = true,
    Projectile_ArrowOfSmokepowder = true, Projectile_ArcaneShot_SeekingArrow = true,
    Zone_ArcaneShot_PiercingArrow = true }

function G.extra_attack_status(char)
    for _, status in ipairs(G.EXTRA_ATTACK_STATUSES) do
        if b(G.q("HasActiveStatus", char, status)) then return status end
    end
    return nil
end

function G.extra_attack_spell(spell, st)
    local roll, taken = "", EXTRA_ATTACK_SPELLS[spell]
    pcall(function() roll = tostring(st.SpellRoll and st.SpellRoll.Default or "") end)
    for _, kind in ipairs(EXTRA_ATTACK_ROLLS) do taken = taken or roll:find(kind, 1, true) end
    if not taken then return false end
    -- The status only frees an Action: v010-pilot act3 r2 c2 seq 55 read a bonus-cost Frenzied Strike as extra_attack.
    for _, c in ipairs(G.parse_costs(st.UseCosts)) do
        if c.name == "ActionPoint" then return true end
    end
    return false
end

-- Full pre-validation for an Osiris-originated cast. Returns
-- {stats, costs, payers} or refuses with the reason the UI would show.

-- The first status on char that stops its orders: {name, type}, or nil. S067 #43/#54: Wyll's Eldritch Blast, ordered
-- while Stunned, went to the game and never started (3 s each): an INCAPACITATED status (Stunned, Paralysed, Asleep)
-- leaves no action to take. S066 #100/#130, S067 #32: Karlach's Rage, the first order of her turn while Fearful (Orin's
-- Piercing Growl: FEARED, StatusType FEAR), never started either (3 of 3), though her move and attacks after it did.
G.BLOCKING_STATUS_TYPES = { INCAPACITATED = true, FEAR = true }
-- INCAPACITATED in the stats but not in play. S072 21:53:33: Karlach's turn started "+Lethargic -Hastened"
-- (HASTE_LETHARGY, `using "STUNNED"`); the guard refused her Rage "(it takes the whole turn)", yet the same turn's
-- move and both attacks worked.
G.ACTING_STATUSES = { HASTE_LETHARGY = true }
-- ...but no Lethargic walk ever started (S074 23:21:13, S089 06:56:41 "never started moving", travelled 0), and its
-- turn was over once the rest of the party's turn group had ended (S091 08:30:24 Wyll ended last, Claude's turn gone
-- by 08:30:34 with no end order; S088 06:19:53): orders sent before that worked (S072 two attacks, S089 Dash). S091
-- showed `DISABLED e5 Claude: Lethargic (1 turn)` beside `Move 9/9m` and the agent planned to walk off the Orb of
-- Negation's platform.
G.LETHARGY_EFFECT = "cannot move; its turn passes once the rest of its turn group has ended"
-- A FEAR status (FEARED from Orin's Death Growl: StatusType FEAR, SG_Fleeing, saved with FearStatusSavingCheck() at the
-- turn start). S065 11:51/11:53: end_turn said "now e4's turn"; the game moved Claude 2 m / 5 m away by itself and his
-- move_to was refused "not this character's turn" (round 2: his turn ended 6 s in; round 3: still his on the turn
-- order). S066 #100 / S067 #32: Karlach's first order at such a turn start was dropped, her later ones worked.
G.FEAR_EFFECT = "the game may move it away by itself at its turn start (orders sent meanwhile are dropped) and may end"
    .. " that turn itself: read the combat state before planning it"
-- with_acting: those too (a walk that never started names them, S074).
function G.blocking_status(char, with_acting)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return nil end
    for _, s in ipairs(V.statuses(e) or {}) do
        local ok, typ = pcall(function() return tostring(Ext.Stats.Get(s.id).StatusType) end)
        if ok and G.BLOCKING_STATUS_TYPES[typ] and (with_acting or not G.ACTING_STATUSES[s.id]) then return { name = s.name, type = typ } end
    end
    return nil
end

-- S087 06:02:51-06:05:57: Mind Blast stunned Wyll, Gale and Shadowheart; their open turns read as acting, so Claude's
-- sent-ahead combat_turn was refused "e3 Shadowheart (yours) must act first" (and the agent was told to plan stunned
-- Wyll), while the game passes such a turn by itself. A party member whose INCAPACITATED status (Stunned, Paralysed,
-- Asleep …) takes the whole turn: its turn passes (state.combat `passes`: not acting, never blocking). Its name, or nil.
function G.turn_passes(char)
    local blk = G.blocking_status(char)
    return blk and blk.type == "INCAPACITATED" and blk.name or nil
end

-- Statuses that take a party member out of the fight, listed on every combat read (state.combat `disabled`) with
-- what the game offers against them. S076 00:39:38: Gale's +Sleeping (Chatterteeth's Eyebite) showed once, in a
-- CHANGES line; the agent planned her spells for rounds 4-6 ("Gale is awake now") and nobody woke her.
G.DISABLING_TYPES = { INCAPACITATED = true, FEAR = true, KNOCKED_DOWN = true }
G.DISABLING_GROUPS = { SG_Incapacitated = true, SG_Unconscious = true, SG_Sleeping = true, SG_Paralyzed = true,
    SG_Stunned = true, SG_Petrified = true, SG_Frightened = true, SG_Prone = true, SG_Restrained = true,
    SG_Dominated = true, SG_Possessed = true, SG_Confused = true, SG_Fleeing = true }
-- What Target_Help removes (its SpellProperties in Shared Spell_Target.txt: RemoveStatus of these groups and ids).
G.HELP_GROUPS = { SG_Sleeping = true, SG_Helpable_Condition = true, SG_Prone = true, SG_Restrained = true }
G.HELP_STATUSES = { BURNING = true, PRONE = true, SLEEPING = true, SLEEP = true, ENSNARING_STRIKE = true, WEB = true,
    HYPNOTIC_PATTERN = true }

-- A stats flag list (a table of names, a name->true map or a ';' string) as a set.
local function flagset(v)
    local out = {}
    if type(v) == "table" then
        for k, x in pairs(v) do
            if type(k) == "string" and x then out[k] = true elseif type(x) == "string" then out[x] = true end
        end
    elseif v ~= nil then
        for x in tostring(v):gmatch("[^;%s]+") do out[x] = true end
    end
    return out
end

-- The game runs this character's turn itself, and a player gets the same (live.md "## Goaded read", on Goading Attack:
-- casts, spending and the turn's end with no order, in a match and in normal play; the HUD's "Can't control: Goaded",
-- its End Turn click ignored, hotbar cells can_use false). -> {name = the status, when one on it carries the stats'
-- LoseControl flag and a player sees it on the character (V.statuses' filter)}, or nil. The one place this rule lives.
-- It reads the game's own state: eoc::status::LoseControlComponent present by entity, which lasts as long as the status
-- (added at its apply, removed at its end, across other characters' turns; present on every sample of 5 statuses,
-- live.md "## Goaded read" and "## Goaded read 2"). Never its LoseControl bool, false under Command: Approach and
-- Confusion while the game's AI ran, nor the stats' flag (45 statuses, effect unread). Every status counts: under
-- Compelled Duel an order once ran as ordered, but a player's hotbar is grey (orchestrator D319). A status arriving
-- mid-turn reads the same. In combat only: every read was of a combat turn (REVIEW-reactions F1, orchestrator D321).
function G.lost_control(char)
    if not G.in_combat(char) then return nil end
    local e = G.entity(char)
    if not (e and U.comp(e, "StatusLoseControl")) then return nil end
    local V = BG3AI.Visibility
    for _, s in ipairs(V and V.statuses and V.statuses(e) or {}) do
        local ok, flags = pcall(function() return Ext.Stats.Get(s.id).StatusPropertyFlags end)
        if ok and flagset(flags).LoseControl then return { name = s.name } end
    end
    return {}
end

-- The one wording, for the list, the guard and the order's result: the HUD's words and the fact. A status a player
-- can't see on the character goes unnamed, and the HUD shows no bar for it (Minor Illusion): the fact alone.
function G.lost_control_text(lost)
    if not lost.name then return "the game runs its turn" end
    return "can't control: " .. tostring(lost.name) .. ", the game runs its turn"
end

-- Refused while the game runs char's turn: an order would only cut the game's cast (live.md, case B: 0 of 3 orders ran
-- as ordered, one cut the AI's cast) and its EndTurn would end a turn a player can't (6 of 6 against a player's click).
function G.lost_control_gate(char)
    local lost = G.lost_control(char)
    if lost then G.refuse(G.lost_control_text(lost)) end
end

-- Prone (KNOCKED_DOWN, SG_Prone): BG3 lets a prone creature do nothing until it stands up at its turn start, which
-- costs half its movement (REVIEW-5 F4; in a match Arena.lua's phase start does the standing up, REVIEW-12 (a)).

G._disabling = G._disabling or {}  -- status id -> {effect, fix} or false (stats don't change in a session)

function G.disabling_info(id)
    local c = G._disabling[id]
    if c ~= nil then return c end
    c = false
    local st = Ext.Stats.Get(id)
    if st then
        local function field(k) local ok, v = pcall(function() return st[k] end); if ok then return v end end
        local typ = tostring(field("StatusType") or "")
        local groups = flagset(field("StatusGroups"))
        local hit = G.DISABLING_TYPES[typ]
        for g in pairs(groups) do hit = hit or G.DISABLING_GROUPS[g] end
        if hit and typ ~= "DOWNED" then  -- downed members have their own line (turn.downed; SG_Incapacitated too)
            local effect
            -- Knocked down (Shared Status_KNOCKED_DOWN.txt PRONE, :1-20): StatusType KNOCKED_DOWN (:3) lies the
            -- character down (StillAnimationType KO, :9); none of its Boosts (:12) is what holds it, the status
            -- type is; it stands up only when the status is removed: at its turn start (RemoveEvents OnTurn, :15)
            -- when it has half its movement (RemoveConditions HasActionResource('Movement', 50, 0, true), :13),
            -- which the removal takes in
            -- turn-based play (OnRemoveFunctors UseActionResource(Movement, 50%), :17). KNOCKED_DOWN (:49-60) has no
            -- RemoveEvents: it stays down until the status ends.
            local prone = (typ == "KNOCKED_DOWN" or groups.SG_Prone) or nil
            local half
            if prone then
                local onTurn = flagset(field("RemoveEvents")).OnTurn
                half = tostring(field("RemoveConditions") or ""):find("HasActionResource('Movement', 50", 1, true)
                effect = onTurn and half and "stands up at its turn start for half its movement"
                    or onTurn and "may stand up at its turn start" or "stays down until the status ends"
            elseif typ == "INCAPACITATED" and not G.ACTING_STATUSES[id] then
                effect = "cannot act"
            elseif G.ACTING_STATUSES[id] then
                effect = G.LETHARGY_EFFECT
            elseif typ == "FEAR" or groups.SG_Fleeing then
                effect = G.FEAR_EFFECT
            elseif tostring(field("Boosts") or ""):find("ActionResourceBlock(Movement)", 1, true) then
                effect = "cannot move"
            end
            local fix = {}
            local help = G.HELP_STATUSES[id]
            for g in pairs(groups) do help = help or G.HELP_GROUPS[g] end
            if help then fix[#fix + 1] = "an adjacent ally's Help (an action) ends it" end
            if flagset(field("RemoveEvents")).OnDamage then fix[#fix + 1] = "any damage ends it" end
            -- SavingThrow(…), FearStatusSavingCheck()
            if tostring(field("RemoveConditions") or ""):find("Saving", 1, true) then
                fix[#fix + 1] = "it saves again each turn"
            end
            c = { effect = effect, fix = #fix > 0 and table.concat(fix, "; ") or nil, prone = prone,
                prone_half = prone and half and true or nil }
        end
    end
    G._disabling[id] = c
    return c
end

-- char's disabling statuses: {{name, turns?, effect?, fix?}, ...} (nil when none).
function G.disabling(char)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return nil end
    local out
    local lost = G.lost_control(char)
    for _, s in ipairs(V.statuses(e) or {}) do
        local ok, info = pcall(G.disabling_info, s.id)
        local mine = lost and lost.name == s.name
        if (ok and info) or mine then
            local d = ok and info or {}
            out = out or {}
            out[#out + 1] = { name = s.name, turns = s.turns,
                effect = mine and G.lost_control_text(lost) or d.effect, fix = d.fix, prone = d.prone,
                prone_half = d.prone_half, id = s.id }
        end
    end
    if lost and not lost.name then
        out = out or {}
        out[#out + 1] = { name = G.lost_control_text(lost) }
    end
    return out
end

-- char's prone statuses ({{name, id, ...}, ...} as G.disabling's), or nil.
function G.prone(char)
    local out
    for _, d in ipairs(G.disabling(char) or {}) do
        if d.prone then out = out or {}; out[#out + 1] = d end
    end
    return out
end

-- char's knocked-down status during its own combat turn ({name, effect, ...} as G.disabling's), or nil: a player can't
-- walk or act with it until it stands up at a turn start (disabling_info). Outside its own turn nothing changes.
-- still_to_act, not is_my_turn: in a match a member's turn is open from the round's burst while its phase waits, and
-- it lies there until Arena.lua stands it up at its phase start (REVIEW-12 (a)); without a match they are the same.
function G.prone_now(char)
    if not (G.in_combat(char) and G.still_to_act(char)) then return nil end
    for _, d in ipairs(G.disabling(char) or {}) do
        if d.prone then return d end
    end
    return nil
end

-- What keeps char from walking: the name of a disabling status that says "cannot act" or "cannot move …", or of the
-- knocked-down status in its own turn (G.prone_now), or nil.
-- The movement figure (State.lua moveView) and the combat walk guard (Actions.lua combat_budget) use it.
function G.move_blocked(char)
    for _, d in ipairs(G.disabling(char) or {}) do
        local eff = d.effect or ""
        if eff == "cannot act" or eff:find("^cannot move") then return d.name end
    end
    local p = G.prone_now(char)
    return p and p.name or nil
end

-- The fix the game offers for char's status `name` ("" when none), for refusals.
function G.disabling_fix(char, name)
    for _, d in ipairs(G.disabling(char) or {}) do
        if d.name == name and d.fix then return "; " .. d.fix end
    end
    return ""
end

-- The variant ids in `spell`'s stats ContainerSpells that are a spell's stats entry (ModifierList SpellData), and
-- the count of entries that are not: nil when it lists none. fights-v09 Balthazar S001 15:59:38: Target_LayOnHands'
-- list once read "BLD_GEN_Crane_Elevator_B.BLD_GEN_Crane_Elevator_B_Mesh.0", a world mesh name, between two good
-- casts of its Cure variant; every other logged read gave its three variants. The value is a slot of the stats string
-- pool, a global string-table entry (bg3se StatsObject.inl GetFixedStringRef), so the wrong text came from that entry.
-- No check on the variants' SpellContainerID: Lay On Hands' variants name no container (they are rows of their own in
-- the action list).
local function container_spells(spell)
    local st = Ext.Stats.Get(spell)
    local raw = tostring(st and st.ContainerSpells or "")
    if raw == "" then return nil end
    local ids, bad = {}, 0
    for id in raw:gmatch("[^;]+") do
        local v = Ext.Stats.Get(id)
        local ok, kind = pcall(function() return v and v.ModifierList end)
        if ok and kind == "SpellData" then ids[#ids + 1] = id else bad = bad + 1 end
    end
    if #ids == 0 and bad == 0 then return nil end
    return ids, bad
end

function G.check_cast(char, spell, target, pos, upcast, force)
    if type(spell) ~= "string" or spell == "" then G.refuse("no action given") end
    local st = Ext.Stats.Get(spell)
    if not st then G.refuse("unknown action") end
    local variants, bad = container_spells(spell)
    if bad and bad > 0 then variants, bad = container_spells(spell) end -- one more read before giving up
    -- live.md, L12 read 5: a twin without Fire Shield was told to choose between its variants, each then refused.
    if variants and not G.holds_any(char, spell, variants) then G.refuse("the character does not have that action") end
    if variants and #variants == 0 then
        G.refuse("choose a variant (this action's variant list did not read as spell names)")
    elseif variants then
        G.refuse("choose a variant: " .. table.concat(variants, ", ") .. (bad > 0
            and " (" .. bad .. " more " .. (bad == 1 and "entry" or "entries") .. " of its variant list did not read"
                .. " as spell names)" or ""))
    end
    if G.linked_box(st) then
        local ids, from = G.box_variants(char, spell)
        -- from = game data: nothing of this box read in char's own book or containers, so it may not hold the box
        if from and not G.holds_any(char, spell, ids) then G.refuse("the character does not have that action") end
        local free = {}
        for _, id in ipairs(ids) do if not G.variant_used(char, id) then free[#free + 1] = id end end
        local shown = G.spell_facts(spell)
        shown = shown and shown.name or spell
        if #free > 0 then
            -- by shown name, the usable ones first (combat_turn's quick pick takes the first)
            G.refuse(shown .. " casts nothing by itself: its variants (which=) are " .. G.variant_list(char, ids)
                .. (spell_flags(st).CallAlliesSpell and ", x,y,z = the ground where they appear" or "")
                .. (from and " (from the " .. from .. ")" or ""))
        elseif #ids > 0 then
            G.refuse(shown .. ": every variant was already used: " .. G.variant_list(char, ids))
        elseif spell_flags(st).CallAlliesSpell then
            G.refuse(shown .. " casts nothing by itself and no ally group could be read for this character")
        end
    end
    G.known_spell(char, spell)
    G.check_rules(char, spell, st, target) -- REVIEW-5 F1/F3: the stats' own requirements (GuardRules.lua)

    local level = tonumber(st.Level) or 0
    if upcast then
        upcast = tonumber(upcast)
        if not upcast or upcast < level or upcast > 9 then G.refuse("bad upcast level") end
        if level == 0 then G.refuse("cantrips cannot be upcast") end
    end

    -- Verbal components while silenced.
    local flags = {}
    pcall(function() for _, f in ipairs(st.SpellFlags) do flags[f] = true end end)
    if flags.HasVerbalComponent and b(G.q("HasActiveStatus", char, "SILENCED")) then
        G.refuse("cannot cast spells with a verbal component while silenced")
    end
    local blk = G.in_combat(char) and G.blocking_status(char)
    if blk and blk.type == "INCAPACITATED" then
        G.refuse("cannot act while " .. tostring(blk.name) .. " (it takes the whole turn)"
            .. G.disabling_fix(char, blk.name))
    end
    local down = G.prone_now(char)
    -- Still down in its own turn: a status that stands up at the turn start for half the movement didn't, so its
    -- rule is not restated; the reason it stayed down is (REVIEW-13 #4). Other prone statuses: their own effect.
    if down then
        G.refuse("cannot act while " .. tostring(down.name) .. " ("
            .. (down.prone_half and "it did not stand up this turn: standing needs half its movement"
                or ("it " .. down.effect)) .. ")")
    end

    -- Resources.
    local snap = G.resources(char)
    local costs = G.parse_costs(st.UseCosts, upcast)
    local payers = {}
    local in_combat = G.in_combat(char)
    local extra_status = in_combat and G.extra_attack_spell(spell, st) and G.extra_attack_status(char)
    if extra_status then
        for i = #costs, 1, -1 do
            if costs[i].name == "ActionPoint" then table.remove(costs, i) end
        end
    end
    for _, c in ipairs(costs) do
        -- Out of combat, action/bonus/movement points do not limit you.
        local turn_resource = (c.name == "ActionPoint" or c.name == "BonusActionPoint"
            or c.name == "Movement" or c.name == "ReactionActionPoint")
        if in_combat or not turn_resource then
            local n, l = G.payer(snap, c)
            if not n then
                local A = BG3AI.Actions
                if c.name == "ActionPoint" and A and A.cast_unlanded(char) and G.extra_attack_spell(spell, st) then
                    G.refuse(A.CAST_UNLANDED)
                end
                G.refuse("not enough " .. G.friendly(c.name))
            end
            payers[#payers + 1] = { name = n, level = l, amount = c.amount }
        end
    end

    -- Target checks.
    -- Range is enforced in and out of combat: a cast we originate never walks
    -- (WithoutMove=1 whenever the range is known, see act.cast), because the
    -- Osiris spell task's approach walk has no path check (teleport risk), and
    -- an unchecked WithoutMove cast would hit from any distance.
    local st_type = tostring(st.SpellType or "")
    local range, melee = G.spell_range(st, char)
    if target then
        G.target_visible(target)
        local d = G.within(char, target, G.MAX_INTERACT_DIST, "target")
        -- Melee (weapon or a short numeric reach like Shove) is edge to edge:
        -- centres may be both bounding radii farther apart than the reach
        -- (1.5 m warhammer + 0.5 + 0.49 for Claude vs an imp = 2.49 m).
        local reach = range and (melee and G.melee_reach(char, target, range) or range + 0.5)
        if reach and not G.same(char, target) and d > reach then
            G.refuse(string.format("target is out of range (%.1f m, range %.1f m)", d, range))
        end
        if st_type ~= "Shout" and not G.same(char, target) and not G.los_ok(char, target, d) then
            G.refuse("no line of sight to target")
        end
        if not force then G.blast_risk(char, st, flags, spell, target, nil) end
    elseif pos then
        G.point_ok(char, pos)
        local p = G.pos(char)
        if spell == G.JUMP_SPELL then G.jump_floor_ok(pos) end
        if spell == G.JUMP_SPELL and p then
            local dx, dz = pos[1] - p[1], pos[3] - p[3]
            local d = math.sqrt(dx * dx + dz * dz)
            -- a jump climbs a little, never 20 m (S039 21:53: jump_to (-414,20,-1447), 20 m above the jumper, froze
            -- the engine; the void check catches that one, this catches a floor high above too)
            if pos[2] - p[2] > G.JUMP_UP_LIMIT_M then
                G.refuse(string.format("the landing is %.1f m above you: a jump climbs at most ~%.0f m; find a ladder,"
                    .. " stairs or a ramp (route)", pos[2] - p[2], G.JUMP_UP_LIMIT_M))
            end
            local maxd, flat = G.jump_reach(char, p, pos)
            if d > maxd + G.JUMP_SLACK_M then
                G.refuse(maxd > flat + 0.05
                    and string.format("too far to jump (%.1f m, this character can jump %.1f m, %.1f m down to there)",
                        d, maxd, p[2] - pos[2])
                    or string.format("too far to jump (%.1f m, this character can jump %.1f m)", d, maxd))
            end
        end
        -- Projectile_Jump has TargetRadius 4.5 (live), below a strong
        -- character's jump; jump_max above is its cap instead.
        if range and p and spell ~= G.JUMP_SPELL and U.dist(p, pos) > range + 0.5 then
            G.refuse(string.format("target point is out of range (%.1f m, range %.1f m)",
                U.dist(p, pos), range))
        end
        if not force and spell ~= G.JUMP_SPELL then G.blast_risk(char, st, flags, spell, nil, pos) end
    end
    return { stats = st, costs = costs, payers = payers, snapshot = snap, level = level, range = range,
        extra_attack = extra_status and true or nil, extra_status = extra_status or nil }
end
