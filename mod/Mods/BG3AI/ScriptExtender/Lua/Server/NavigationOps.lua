-- Navigation, part 5 of 5 (Navigation.lua loads the parts in order; split for the 1000-line cap):
-- the nav.* RPC ops and the local layout.
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav

local truthy = U.truthy
local osi = V.osi
local round = U.round
local I = N.inner
local currentLevel, helperBox, inBox, activeChar = I.currentLevel, I.helperBox, I.inBox, I.activeChar
local movementLeft = I.movementLeft

function N.exitName(name)
    local l = type(name) == "string" and name:lower() or ""
    for _, w in ipairs(N.EXIT_NAME) do
        if l:find("%f[%a]" .. w) then return true end
    end
    return false
end

RPC.register("nav.reach_toward", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local toward = type(args.pos) == "table" and args.pos or nil
    if args.target then
        local te = V.entity(args.target)
        local tp = te and V.pos(te)
        if not tp then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tp)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        toward = tp
    end
    local maxM = math.min(tonumber(args.max_m) or 30, N.PATH_MAX_M)
    local step = math.max(tonumber(args.step) or 2, 1)
    local t0 = Ext.Utils.MonotonicTime()
    -- direct=true (travel_to): the engine's own route in one long leg when the
    -- whole way is plain walking; else the sampled legs below.
    local riskyDirect = nil
    if args.direct and toward and not args.alt then
        local d, why, risky = N.directLeg(char, toward, args.avoid ~= false)
        riskyDirect = risky
        if d then
            local under = N.harmfulUnder(char)
            if under then d.standing_in = under end
            d.ms = Ext.Utils.MonotonicTime() - t0
            return d
        end
        args.direct_why = why
    end
    local r, err = N.reachToward(char, args.dir, toward, maxM, step, args.avoid ~= false, args.loose == true)
    if not r then error(err, 0) end
    -- alts=true (walk_toward by direction): on failure, which directions do work
    if args.alts and args.dir and not r.reachable then
        r.others = N.otherDirections(char, tostring(args.dir):upper(), math.min(maxM, 20), args.avoid ~= false)
    end
    -- avoid="least" (travel_to): when no clear walk gets >= 1 m closer, look
    -- around (detours) and, failing that, take the least-exposure crossing.
    if args.avoid == "least" and toward then
        local e = V.entity(char)
        local from = e and V.pos(e)
        local gain = (r.reachable and r.pos and from) and (U.dist(from, toward) - U.dist(r.pos, toward)) or 0
        if gain < 1.0 or (r.hazard_beyond and gain < 0.5 * maxM) then
            local c = N.crossing(char, toward, maxM)
            if c and c.reachable and (gain < 1.0 or c.score > gain + 0.5) then
                c.straight = { reachable = r.reachable, hazard = r.hazard or r.hazard_beyond }
                r = c
            elseif c then
                r.cross_samples = c.samples
            end
        end
        -- Still nothing that gets on: the engine's own route across the surface
        -- (the bridge weighs the exposure: crossing_ok by policy and HP).
        if riskyDirect then
            local g2 = (r.reachable and r.pos and from) and (U.dist(from, toward) - U.dist(r.pos, toward)) or 0
            if g2 < 1.0 then
                riskyDirect.straight = { reachable = r.reachable, hazard = r.hazard or r.hazard_beyond }
                r = riskyDirect
            end
        end
    end
    -- travel_to: verify the exact leg end we hand out; when it fails, or the
    -- bridge asks for another leg after a failed walk (alt=true, exclude=that
    -- leg's end), take a verified alternative (N.altLeg).
    if toward and not (r.direct and r.verified) and (args.alt or (r.reachable and r.pos)) then
        local fine = nil
        -- reachToward searched this exact (0.1 m) point this frame: no second search
        if not args.alt then fine = r.searched or N.verifyPoint(char, r.pos) end
        if fine == true then
            r.verified = true
        elseif args.alt or fine == false then
            local bad = type(args.exclude) == "table" and args.exclude or (not args.alt and r.pos) or nil
            local a = N.altLeg(char, toward, maxM, bad, args.avoid ~= false)
            if a then
                if fine == false then a.replaced = r.pos end
                r = a
            elseif fine == false then
                r = { reachable = false, samples = r.samples, unverified = r.pos }
            else
                r.alt_failed = true
                if r.reachable then r = { reachable = false, samples = r.samples, alt_failed = true } end
            end
        end
    end
    local under = N.harmfulUnder(char)
    if under then r.standing_in = under end
    r.searched = nil
    r.direct_why = args.direct_why
    if r.reachable == false then
        local ce = V.entity(char)
        r.opened = N.geoNote(ce and V.pos(ce))
    end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.approach {char?, target|pos, within?=1.5, budget_m?, avoid?=true}: where
-- to walk along the real route to get within `within` m (see N.approach).
-- budget_m defaults to the movement left (in combat) minus 0.2.
RPC.register("nav.approach", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local target, tpos = nil, type(args.pos) == "table" and args.pos or nil
    if args.target then
        local te = V.entity(args.target)
        tpos = te and V.pos(te)
        if not tpos then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tpos)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tpos))
        if not visible then error("not visible", 0) end
        target = args.target
    end
    if type(tpos) ~= "table" or #tpos < 3 then error("need pos [x,y,z] or target", 0) end
    local ce = V.entity(char)
    local cp = ce and V.pos(ce)
    if cp and U.dist(cp, tpos) > N.PATH_MAX_M then
        return { reachable = false, reason = "too far", straight_m = round(U.dist(cp, tpos), 1) }
    end
    if not target and cp and not N.known(ctx, tpos) then
        return { reachable = nil, unknown_goal = true, reason = "a spot the party hasn't seen or walked",
            straight_m = round(U.dist(cp, tpos), 1) }
    end
    local mv = movementLeft(char)
    local budget = tonumber(args.budget_m)
    if mv and Osi.IsInCombat and truthy(osi(Osi.IsInCombat, char)) then
        budget = math.min(budget or math.huge, math.max(0, mv - 0.2))
    end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.approach(char, target or tpos, tpos, math.max(0, tonumber(args.within) or N.APPROACH_M),
        budget, args.avoid ~= false)
    if not r then error(err, 0) end
    if mv then r.movement_left = round(mv, 1) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

N.LOS_MARGIN_M = 0.25
N.LOS_MARGIN_WIDE_M = 0.5
N.LOS_DISAGREE_MIN_M = 1.5
-- nav.los_spot {char?, target, range, budget_m?, avoid?=true}: where to walk
-- to have the target in sight and within range (see N.losSpot). Also says
-- whether the engine sees it from where the character stands now
-- (los_now). budget_m defaults to the movement left (in combat) minus 0.2.
RPC.register("nav.los_spot", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local te = args.target and V.entity(args.target)
    local tpos = te and V.pos(te)
    if not tpos then error("unknown target", 0) end
    local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tpos)
        or (U.comp(te, "ServerItem") and V.itemVisible(ctx, args.target, te, tpos))
    if not visible then error("not visible", 0) end
    local range = tonumber(args.range)
    if not range or range <= 0 then error("need range (m)", 0) end
    local mv = movementLeft(char)
    local budget = tonumber(args.budget_m)
    if mv and Osi.IsInCombat and truthy(osi(Osi.IsInCombat, char)) then
        budget = math.min(budget or math.huge, math.max(0, mv - 0.2))
    end
    if budget == math.huge then budget = nil end
    local t0 = Ext.Utils.MonotonicTime()
    -- Spots with a sideways ray margin; a wider one, and no spot within
    -- LOS_DISAGREE_MIN_M of here, when the rays already see the target from
    -- where the engine says there is no line of sight (the model is off here).
    local losNow = truthy(osi(Osi.HasLineOfSight, char, args.target))
    local ce = V.entity(char)
    local here = ce and V.pos(ce)
    local margin, minStep = N.LOS_MARGIN_M, nil
    if not losNow and here and V.losFrom(ctx, args.target, te, tpos, here) == true then
        margin, minStep = N.LOS_MARGIN_WIDE_M, N.LOS_DISAGREE_MIN_M
    end
    local r, err = N.losSpot(char, args.target, tpos, range, budget, args.avoid ~= false,
        function(feet)
            if minStep and here and U.dist(feet, here) < minStep then return false end
            return V.losFrom(ctx, args.target, te, tpos, feet, margin)
        end)
    if not r then error(err, 0) end
    r.los_now = losNow
    if minStep then r.model_off_here = true end
    if mv then r.movement_left = round(mv, 1) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.safe_spot {char?, max_m?}: where to step out of a harmful surface.
-- {safe=true} when not standing in one; else {standing_in, found, pos?, path_m?, dir?}.
RPC.register("nav.safe_spot", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local toward = type(args.pos) == "table" and #args.pos >= 3 and args.pos or nil
    local r, err = N.safeSpot(char, math.min(tonumber(args.max_m) or 8, 12), toward)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.landing {char?, pos}: is a jump_to landing clear? {safe, dist_m,
-- reliable_m, max_m?, surface?|blocked?, short_pos?, suggest?, suggest_off_m?,
-- suggest_dist_m?} (see N.landing).
RPC.register("nav.landing", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    if type(args.pos) ~= "table" or #args.pos < 3 then error("need pos [x,y,z]", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    -- Breadcrumb (S038: the server hung in here and the log said nothing of where): one line per jump_to check,
    -- and its time once it took long.
    local p = args.pos
    Ext.Utils.Print(string.format("[BG3AI] nav.landing (%s,%s,%s)", tostring(p[1]), tostring(p[2]), tostring(p[3])))
    -- a landing the party hasn't seen or walked: no answer about its ground (act.jump's point_ok refuses it)
    if not N.known(ctx, args.pos) then return { unknown_goal = true, ms = Ext.Utils.MonotonicTime() - t0 } end
    local r, err = N.landing(char, args.pos)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    if r.ms > 100 then Ext.Utils.Print(string.format("[BG3AI] nav.landing took %.0f ms", r.ms)) end
    return r
end)

-- nav.level_guard {clear?: true|level, kill?: level, why?}: the levels the kill switch turned off ({slow = {level =
-- {why, ms}}}, warm = async warm-up counters); clear=true clears all, clear=<level> one; kill=<level> turns one off
-- by hand (a level known to freeze the engine's searches, e.g. SCL_Main_A for tests on Act 2 saves).
RPC.register("nav.level_guard", function(args)
    local g = N.guardState()
    if type(args.kill) == "string" then N.killLevel(args.kill, args.why or "turned off by hand") end
    if args.clear == true then g.slow, g.strikes, N.slowHits = {}, {}, {}
    elseif type(args.clear) == "string" then
        g.slow[args.clear] = nil
        if g.strikes then g.strikes[args.clear] = nil end
        N.slowHits[args.clear] = nil
    end
    if args.clear ~= nil then pcall(Ext.IO.SaveFile, N.guardFile(), Ext.Json.Stringify(g, { Beautify = false })) end
    local wa = N.warmAsync
    return { slow = g.slow, level = currentLevel(), here = N.levelSlow(), file = N.guardFile(),
        slow_hits = N.slowHits[currentLevel() or ""], ttl_s = N.KILL_TTL_S,
        warm = { ok = wa.ok, timeouts = wa.timeouts, broken = wa.broken } }
end)

-- nav.hazards {char?, radius?, limit?}: harmful patches the party sees nearby.
RPC.register("nav.hazards", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local list, err = N.hazardPatches(char, math.min(tonumber(args.radius) or N.HAZARD_SCAN_M, 20))
    if not list then error(err, 0) end
    local limit = tonumber(args.limit) or 6
    local out = { hazards = {} }
    for i, h in ipairs(list) do
        if i > limit then out.more = #list - limit; break end
        out.hazards[i] = h
    end
    out.ms = Ext.Utils.MonotonicTime() - t0
    return out
end)

------------------------------------------------------------------------
-- Local layout: the walls, openings and floor around a character as a text
-- grid, what the overhead camera shows (S030 Gauntlet of Shar trials: 11 min
-- and 16 screenshots in the Soft-Step maze and the Faith-Leap room).
-- Cells from the AI grid tiles: '#' wall (WalkBlock), '.' floor at our level,
-- '^'/'v' a floor > 1.2 m above/below, ':' inside a darkness cloud, '~' a
-- harmful surface, ' ' no floor a player can see (drop, chasm), '?' not seen
-- or walked yet (N.known).
-- Fairness: the Faith-Leap safe path is a walkway layer of invisible-platform
-- helpers (V.helperItem); that layer is never drawn as floor. The floor
-- mosaic by the bowl is read off the screen (screenshot/zoom), as a player
-- does (user 2026-10-18).
------------------------------------------------------------------------

N.LAYOUT_R = 12
N.LAYOUT_MAX_R = 20
N.LAYOUT_NEAR_M = 2.0   -- cells this near are drawn unchecked (around your feet)

local function tileFlags(t)
    local f = {}
    pcall(function() for _, v in ipairs(t.Flags or {}) do f[tostring(v)] = true end end)
    return f
end

-- A floor this far below with nothing nearer is a drop, drawn "_" (S032 17:32: the Gauntlet's broken staircase
-- drew blank like the chasm beside it while route said the floor lay 9.4 m below; the agent tried two jumps into
-- the chasm and parked Yurgir). Deeper than DROP_MAX_M stays blank (the Faith-Leap chasm bottom is ~11.7 m down).
N.DROP_MIN_M, N.DROP_MAX_M = 4.0, 10.0
-- Deeper still, "," = deep ground DEEP_MIN_M..DEEP_MAX_M below (S039, Nightsong's Prison: the islands of the way down
-- lie 20-30 m below the arrival ledge and drew blank, like the void). From 14 m, so the Faith-Leap chasm bottom
-- (~11.7 m) stays blank.
N.DEEP_MIN_M, N.DEEP_MAX_M = 14.0, 120.0
function N.layoutCell(x, z, y0, box)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return " " end
    local best, drop, deep
    local helperHere = box and inBox(box, x, z, N.HELPER_MARGIN_M)
    for _, h in ipairs(hs) do
        if type(h) == "number" and not (helperHere and math.abs(h - (box.y + 0.2)) <= N.HELPER_LAYER_M) then
            if math.abs(h - y0) <= N.DROP_MIN_M then
                if not best or math.abs(h - y0) < math.abs(best - y0) then best = h end
            elseif y0 - h <= N.DROP_MAX_M and (not drop or h > drop) then
                drop = h
            elseif y0 - h >= N.DEEP_MIN_M and y0 - h <= N.DEEP_MAX_M and (not deep or h > deep) then
                deep = h
            end
        end
    end
    local low, mark = drop, "_"
    if not drop and deep then low, mark = deep, "," end
    if not best and low and not helperHere then
        local okt, t = pcall(Ext.Level.GetTileDebugInfo, { x, low, z })
        if not (okt and t) then return " " end
        local f = tileFlags(t)
        if f.WalkBlock then return " " end
        local g, cl = "", ""
        pcall(function() g, cl = tostring(t.GroundSurface), tostring(t.CloudSurface) end)
        if N.HARMFUL[g] or N.HARMFUL[cl] then return " " end
        return mark, low
    end
    if not best then return " " end
    local okt, t = pcall(Ext.Level.GetTileDebugInfo, { x, best, z })
    if okt and t then
        local f = tileFlags(t)
        if f.WalkBlock then return "#", best end
        local g, cl = "", ""
        pcall(function() g, cl = tostring(t.GroundSurface), tostring(t.CloudSurface) end)
        if N.HARMFUL[g] or N.HARMFUL[cl] then return "~", best end
        if cl == "DarknessCloud" or g == "Darkness" then return ":", best end
    end
    local dy = best - y0
    if dy > 1.2 then return "^", best end
    if dy < -1.2 then return "v", best end
    return ".", best
end

function N.layout(char, radius, cell)
    local e = V.entity(char)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    radius = math.max(4, math.min(tonumber(radius) or N.LAYOUT_R, N.LAYOUT_MAX_R))
    cell = math.max(1, math.min(tonumber(cell) or 1.5, 3))
    local n = math.floor(radius / cell + 0.5)
    local box = helperBox(from, radius)
    -- Only ground the party sees or has walked (N.known): beyond LAYOUT_NEAR_M a cell's floor (or, with none, the
    -- air at our height) must be known, else "?". A wall cell's face is what is seen (a ray to its middle stops at
    -- the face): it shows when a cell next to it shows.
    local ctx = V.context()
    local grid = {}
    for iz = n, -n, -1 do
        local row = {}
        for ix = -n, n do
            local x, z = from[1] + ix * cell, from[3] + iz * cell
            local ch, y = N.layoutCell(x, z, from[2], box)
            if ch ~= "#" and (ix * ix + iz * iz) * cell * cell > N.LAYOUT_NEAR_M ^ 2
                and not N.known(ctx, { x, y or from[2], z }) then
                ch = "?"
            end
            row[#row + 1] = ch
        end
        grid[#grid + 1] = row
    end
    local rows = {}
    for r = 1, #grid do
        local row = {}
        for c = 1, #grid[r] do
            local ch = grid[r][c]
            local ix, iz = c - n - 1, n + 1 - r
            if ch == "#" and (ix * ix + iz * iz) * cell * cell > N.LAYOUT_NEAR_M ^ 2 then
                ch = "?"
                for dr = -1, 1 do
                    for dc = -1, 1 do
                        local o = grid[r + dr] and grid[r + dr][c + dc]
                        if o and o ~= "#" and o ~= "?" then ch = "#" end
                    end
                end
            end
            row[c] = ch
        end
        rows[r] = table.concat(row)
    end
    return { rows = rows, cell = cell, n = n, center = U.vec(from, 1) }
end

-- nav.layout {char?, radius?=12, cell?=1.5}: N.layout.
RPC.register("nav.layout", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.layout(char, args.radius, args.cell)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.route {char?, pos | target, connectors?}: the route ahead (N.routeAhead).
-- A target must be visible; a pos is the agent's own goal (objective marker,
-- map marker, coordinates it read). Ladders/doors are listed when there is no
-- path, or always with connectors=true.
-- nav.wedged {char?, goal?}: N.wedged for walk_toward/travel_to failures (route carries it as r.wedged).
RPC.register("nav.wedged", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local goal = type(args.goal) == "table" and #args.goal == 3 and args.goal or nil
    return N.wedged(char, nil, goal) or { wedged = false }
end)

RPC.register("nav.route", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local goal = type(args.pos) == "table" and #args.pos >= 3 and args.pos or nil
    if not goal and args.target then
        local te = V.entity(args.target)
        local tp = te and V.pos(te)
        if not tp then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tp)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        local ce = V.entity(char)
        goal = U.comp(te, "ServerItem") and N.ladderTop(ce and V.pos(ce), args.target, te) or tp
    end
    if not goal then error("need pos [x,y,z] or target", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.routeAhead(ctx, char, goal, args.connectors == true, not args.pos and args.target and true or nil)
    if not r then error(err, 0) end
    local t1 = Ext.Utils.MonotonicTime()
    -- No path: the open directions too, as get_frontier gives them (S023 06:23:
    -- 8 guessed route/wiki/zoom calls across a chasm while the way on, a stone
    -- bridge, lay SW).
    if r.reachable == false and args.ways ~= false then
        local ok, ways = pcall(N.ways, char, 20, 8, 80)
        if ok and ways and #ways > 0 then r.ways = ways end
    end
    local t2 = Ext.Utils.MonotonicTime()
    if r.reachable == false then
        local ce = V.entity(char)
        r.opened = N.geoNote(ce and V.pos(ce))
    end
    if r.phases then r.phases.ways, r.phases.note = t2 - t1, Ext.Utils.MonotonicTime() - t2 end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- Cheap activity sample for latency metrics (the bridge polls it ~1/s on its
-- own connection): party positions (the bridge diffs them to detect movement),
-- the selected member (`active`: ClientControl, else the first member), running
-- jobs, dialogue and combat flags. Party-only data, nothing hidden.
RPC.register("nav.activity", function(args)
    local party = V.party()
    local out = { pos = {}, jobs = 0, job_kinds = {}, in_dialogue = false, in_combat = false }
    for _, u in ipairs(party) do
        if truthy(osi(Osi.IsOnStage, u)) then
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p then
                out.pos[u] = U.vec(p, 2)
                local s = N.surfaceAt(p)
                if s then out.surfaces = out.surfaces or {}; out.surfaces[u] = s end
            end
            if truthy(osi(Osi.IsInCombat, u)) then
                out.in_combat = true
                -- party_turn: a party member can act now (false: enemies' turns, the agent can only wait)
                local G = BG3AI.Guard
                if not out.party_turn and G and G.is_my_turn then
                    local ok, mine = pcall(G.is_my_turn, u)
                    out.party_turn = ok and mine == true
                end
            end
            if not out.in_dialogue and e then out.in_dialogue = V.inDialog(u, e) end
        end
    end
    out.active = V.selected(party) or party[1]
    for _, j in pairs(RPC.jobs or {}) do
        if j.status == "running" then
            out.jobs = out.jobs + 1
            out.job_kinds[#out.job_kinds + 1] = j.kind
        end
    end
    return out
end)
