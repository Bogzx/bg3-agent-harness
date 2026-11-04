-- Part of Server/Navigation.lua (split for the 1000-line cap): the free spots around an occupied target, which
-- nav.path asks when a target id's centre path ends short (NavigationReach.lua).

local U = BG3AI.Util
local N = BG3AI.Nav
local I = N.inner
local RING, floorAt = I.RING, I.floorAt

-- The free spots `rad` around an occupied target `tpos`, nearest to `from` first: the first whose path `passes` is the
-- answer (a copy with approach_pos), nil when none does. The engine's path to an occupied centre stops where its own
-- goal handling does (w15 b2: 2.95 m from a Destroyer it then walked 1.7 m closer to). No new spot past
-- FREE_RING_MS (REVIEW-18k: up to 8 engine searches on the server thread): unfinished, reachable unknown. -> r, ms
N.FREE_RING_MS = 300
function N.freeSpotPath(charUuid, tpos, rad, from, passes)
    local t0, ring = Ext.Utils.MonotonicTime(), {}
    for _, d in ipairs(RING) do
        local q = { tpos[1] + d[1] * rad, tpos[2], tpos[3] + d[2] * rad }
        ring[#ring + 1] = { q, from and U.dist(from, q) or 0 }
    end
    table.sort(ring, function(a, b) return a[2] < b[2] end)
    local out
    for _, it in ipairs(ring) do
        if Ext.Utils.MonotonicTime() - t0 > N.FREE_RING_MS then
            out = { reachable = nil, unfinished = true }
            break
        end
        local fy = floorAt and floorAt(it[1][1], it[1][3], tpos[2]) or tpos[2]
        local r2 = N.path(charUuid, { it[1][1], fy, it[1][3] })
        if passes(r2) then -- a copy: r2 is the path cache's
            out = { approach_pos = U.vec({ it[1][1], fy, it[1][3] }, 1) }
            for k, v in pairs(r2) do out[k] = out[k] or v end
            break
        end
    end
    return out, Ext.Utils.MonotonicTime() - t0
end
