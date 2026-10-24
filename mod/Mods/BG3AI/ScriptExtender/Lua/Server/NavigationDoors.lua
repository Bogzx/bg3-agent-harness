-- Part of Server/Navigation.lua (split for the 1000-line cap): door links, the doors that took the party far.
--
-- B4 (S037 Rescue Volo): the Shattered Sanctum's Heavy Oak Doors at ~(-108,10.8,460) put the party at
-- (297.2,40.3,-78.2) on the same level (S015 02:43:33), so Volo inside (~(334,40.8,-6)) read 590-610 m SE of the
-- Goblin Camp, while the game's map draws the quest marker at the Sanctum (live Window B 2026-10-24, b4_map.jpg).
-- travel_to then walked toward the straight line and was blocked 219 m short. A player remembers where a door led:
-- a successful UseFinished of an item (not carried, not a waypoint shrine) by a party member who then stands
-- LINK_MIN_M or more from it within LINK_WAIT_MS, on the same level, keeps the link {door, name, from = its position,
-- to = the arrival} in the level's breadcrumb store (saved with them). nav.route names one (door_link) when the goal
-- is not walkable from here and the link's arrival is far nearer the goal. Fair: only doors the party went through,
-- and only what using them did.

local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav
local I = N.inner
local osi = V.osi
local round = U.round

N.LINK_MIN_M = 40     -- a use that leaves its user this far from the item is a link (a ladder climb moves less)
N.LINK_WAIT_MS = 5000 -- the move comes this soon after the UseFinished
N.LINKS_CAP = 40      -- links kept per level, newest last
N.LINK_GAIN_M = 50    -- a link helps when its arrival is this much nearer the goal than the party...
N.LINK_FRAC = 0.5     -- ...and at most this fraction of the party's distance to it
N.uses = N.uses or {} -- party member uuid -> {item, name, pos, level, t}: the last use, until it moved them or expired

local function hdist(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) end

local function isWaypoint(u)
    for _, w in ipairs(N.waypointsHere()) do
        if w.uuid == u then return true end
    end
    return false
end

-- UseFinished(char, item, success): remember where the item stood when a party member used it.
function N.noteUse(char, item, success)
    local c, u = V.uuid(char), V.uuid(item)
    if not (c and u and U.truthy(success) and V.context().is_party[c]) then return end
    if osi(Osi.GetInventoryOwner, u) ~= nil or isWaypoint(u) then return end
    local e = V.entity(u)
    local p = e and V.pos(e)
    local lvl = I.currentLevel()
    if p and lvl then
        N.uses[c] = { item = u, name = V.name(u), pos = U.vec(p, 1), level = lvl, t = Ext.Utils.MonotonicTime() }
    end
end

function N.addLink(level, use, to)
    local l = I.levelStore(level)
    l.links = l.links or {}
    for i, k in ipairs(l.links) do
        if k.door == use.item then
            table.remove(l.links, i)
            break
        end
    end
    l.links[#l.links + 1] = { door = use.item, name = use.name, from = use.pos, to = to }
    while #l.links > N.LINKS_CAP do table.remove(l.links, 1) end
end

-- Tick: a pending use that moved its user LINK_MIN_M from the item becomes a link; another level or a late move
-- drops it.
function N.linkWatch(now)
    if next(N.uses) == nil then return end
    local lvl = I.currentLevel()
    for c, u in pairs(N.uses) do
        local e = V.entity(c)
        local p = e and V.pos(e)
        if lvl ~= u.level or now - u.t > N.LINK_WAIT_MS then
            N.uses[c] = nil
        elseif p and U.dist(p, u.pos) >= N.LINK_MIN_M then
            N.uses[c] = nil
            N.addLink(lvl, u, U.vec(p, 1))
        end
    end
end

-- The link that helps most toward `goal` from `from` (the shortest door walk + arrival-to-goal), or nil:
-- {uuid, name, door, to, door_m, dir (from here to the door), to_goal_m (from its arrival)}.
function N.doorLink(level, from, goal)
    local l = level and I.store().levels[level]
    if not (l and l.links and from and goal) then return nil end
    local d0 = hdist(from, goal)
    local best, bc
    for _, k in ipairs(l.links) do
        local dg, dd = hdist(k.to, goal), hdist(from, k.from)
        if dg <= d0 * N.LINK_FRAC and dg <= d0 - N.LINK_GAIN_M and dd < d0 and (not bc or dd + dg < bc) then
            best, bc = k, dd + dg
        end
    end
    if not best then return nil end
    return { uuid = best.door, name = best.name, door = best.from, to = best.to,
        door_m = round(U.dist(from, best.from), 1), dir = N.direction(from, best.from),
        to_goal_m = round(hdist(best.to, goal), 1) }
end

if Ext.Osiris and not N.linkListening then
    N.linkListening = true
    pcall(Ext.Osiris.RegisterListener, "UseFinished", 3, "after", function(char, item, success)
        local ok, err = pcall(N.noteUse, char, item, success)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] nav door use: " .. tostring(err)) end
    end)
end
