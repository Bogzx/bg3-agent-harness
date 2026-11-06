-- ui.state in an arena (split from UIState.lua for the 1000-line cap): a reaction prompt is its side's.
BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local S = BG3AI.UIState

-- In an arena: the side whose characters the prompt's rows are for (by the team names the server sent), nil when a
-- row names nobody of either team or the rows name both.
function S.reactionSide(rows)
    local side
    for _, r in ipairs(rows) do
        local s = BG3AI.Arena.name_side(r.who)
        if not s or (side and s ~= side) then return nil end
        side = s
    end
    return side
end

-- The reaction S.reaction read, as the asking side sees it: a side sees another side's prompt only as there (who it
-- is for and what it offers are that side's). A prompt no team owns (no rows read, a name on neither team or on both)
-- is unowned: the harness (no side) answers it. Outside an arena: unchanged.
function S.sideReaction(reaction)
    local side = RPC.side and RPC.side()
    local arena = BG3AI.Arena and BG3AI.Arena.active and BG3AI.Arena.active()
    if arena and reaction then
        local owner = S.reactionSide(reaction.options or {})
        if side and owner ~= side then
            return { other_side = true, unowned = owner == nil or nil }
        elseif not side and owner == nil then
            reaction.unowned = true
        end
    end
    return reaction
end
