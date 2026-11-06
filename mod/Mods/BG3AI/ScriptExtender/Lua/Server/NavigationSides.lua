-- Navigation, part (split for the 1000-line cap; Navigation.lua loads it first): walked cells per side in a match.

local N = BG3AI.Nav
local U = BG3AI.Util
local V = BG3AI.Visibility
local I = N.inner
local truthy = U.truthy
local osi = V.osi
local round = U.round

-- A match (two teams in one game, BG3AI.RPC sides): each side remembers only where its own team walked. The level
-- store (Navigation.lua store(), saved with the game) is the harness's view, both teams' steps, and is never read
-- under a side: side B's known ground, explored cells and trail must not name a spot only side A stood on. Cleared
-- at the match's end (RPC.clear_sides).
local function sideWalked(level)
    local w = U.sided(N, "walked")
    local l = w[level]
    if not l then
        l = { cells = {}, n = 0, places = {} }
        w[level] = l
    end
    return l
end

-- The walked cells and trail a reader uses: the side's own under a side, else the level's store (nil when none).
function N.walkedStore(level)
    if U.side() then return sideWalked(level) end
    return I.store().levels[level]
end

-- In a match, each side's team into its own walked store (sideWalked): the cells and the trail of its first member.
function N.recordSides(lvl, now)
    local R = BG3AI.RPC
    if not (R and R.arena_active and R.arena_active()) then return end
    for _, side in ipairs(R.SIDES) do
        U.with_side(side, function()
            local l = sideWalked(lvl)
            local team = V.party()
            for _, u in ipairs(team) do
                if truthy(osi(Osi.IsOnStage, u)) then
                    local e = V.entity(u)
                    local p = e and V.pos(e)
                    if p then
                        local k = I.cellKey(I.cellOf(p))
                        if not l.cells[k] then
                            l.cells[k] = 1
                            l.n = l.n + 1
                        end
                    end
                end
            end
            local lead = team[1] and V.entity(team[1])
            local lp = lead and V.pos(lead)
            local last = l.places[#l.places]
            if lp and (not last or U.dist({ last.x, last.y, last.z }, lp) >= N.PLACE_GAP) then
                l.places[#l.places + 1] = { x = round(lp[1], 1), y = round(lp[2], 1), z = round(lp[3], 1),
                    sub = N.subregion, gt = now }
                while #l.places > N.PLACES_CAP do table.remove(l.places, 1) end
            end
        end)
    end
end

