-- Live probe (night, BACKLOG [act/vis] "approach rings vs a raised floor", S035 19:25: interact e285 Wooden Trunk in
-- Yurgir's chamber refused a bare "no walkable path there"). Read-only. For each item named like __PAT__ within __R__ m
-- of the party leader, as the leader: the target and its bounds, Guard's approach plan (G.approach_point at
-- A.USE_REACH, or its refusal), N.path's own entity answer, N.closestReach, N.wedged; and per approach ring point
-- (Guard's radii up to USE_REACH - 0.2, 8 directions) EVERY floor height there within 4 m of the target's height:
-- the one the rings use (nearest the target's height) and the others, each with its 3D distance to the target and an
-- uncached path check. The raised-floor hypothesis holds when a ring point's chosen height fails while another height
-- in the same column is reachable and within USE_REACH of the target.
-- Run with the lua_server helper (night-live/lib plib.lua), __R__ and __PAT__ replaced (as night-live reads.py does).
local R, PAT = __R__, __PAT__
local B = Mods.BG3AI.BG3AI
local G, N, A, V, U = B.Guard, B.Nav, B.Actions, B.Visibility, B.Util
local function pc(f, ...) local ok, v = pcall(f, ...) if ok then return v end
    return { err = type(v) == "table" and v.reason or tostring(v) } end
local function r1(x) return type(x) == "number" and math.floor(x * 10 + 0.5) / 10 or x end
local function v3(p) return p and { r1(p[1]), r1(p[2]), r1(p[3]) } or nil end
local lead = V.uuid(Osi.DB_PartyMembers:Get(nil)[1][1])
local from = V.pos(V.entity(lead))
local reach = A.USE_REACH
local out = { lead = V.name(lead), from = v3(from), reach = reach, targets = {} }
for _, u in ipairs(V.itemsAround and V.itemsAround(from, R) or {}) do
    local n = V.name(u)
    if tostring(n):lower():find(PAT, 1, true) then
        local t = V.pos(V.entity(u))
        local o = { uuid = u, name = n, pos = v3(t), dist = r1(U.dist(from, t)) }
        o.bounds = pc(function() return V.bounds(V.entity(u)) end)
        o.plan = pc(function()
            local p = G.approach_point(lead, u, reach)
            return { here = p.here, pos = v3(p.pos), length_m = r1(p.length_m), rim = p.rim, unverified = p.unverified }
        end)
        o.npath = pc(function()
            local r = N.path(lead, u)
            return { reachable = r.reachable, partial = r.partial, approach_pos = r.approach_pos, length_m = r1(r.length_m),
                needs_jump = r.needs_jump }
        end)
        o.closest = pc(N.closestReach, lead, t)
        o.wedged = pc(N.wedged, lead, nil, t)
        o.rings = {}
        local base = math.atan(from[3] - t[3], from[1] - t[1])
        for _, rad in ipairs(G.APPROACH_RADII) do
            if rad <= reach - 0.2 then
                for i = 0, 7 do
                    local ang = base + i * math.pi / 4
                    local x, z = t[1] + rad * math.cos(ang), t[3] + rad * math.sin(ang)
                    local chosen = G.floor_at(x, z, t[2])
                    local col = { r = rad, dir = i, x = r1(x), z = r1(z), chosen = r1(chosen), heights = {} }
                    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
                    for _, h in ipairs(ok and type(hs) == "table" and hs or {}) do
                        if type(h) == "number" and math.abs(h - t[2]) <= 4.0 then
                            local p = { x, h, z }
                            local reachable, raw = N.verifyPoint(lead, p)
                            col.heights[#col.heights + 1] = { y = r1(h), d3 = r1(U.dist(p, t)), reachable = reachable,
                                partial = raw and raw.partial or nil, needs_jump = raw and raw.needs_jump or nil }
                        end
                    end
                    o.rings[#o.rings + 1] = col
                end
            end
        end
        out.targets[#out.targets + 1] = o
    end
end
return Ext.Json.Stringify(out)
