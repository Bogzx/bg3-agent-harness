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

-- What the game holds for (L5 reads, live.md 01:03:22): one InterruptActionState per held event, its Actions naming
-- each interrupt and its Observer, who answers it. The popup's rows don't name a twin, so this is what tells the owner.
-- -> {key, rows = {{uuid, interrupt}}}, nil when nothing is held. The key changes with each interrupt asked.
function S.heldInterrupt()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "InterruptActionState")
    if not ok or not list then return nil end
    local rows, parts = {}, {}
    for _, e in pairs(list) do
        pcall(function()
            local st = e.InterruptActionState
            parts[#parts + 1] = tostring(st.SpellCastGuid)
            for _, a in pairs(st.Actions) do
                local r = { uuid = tostring(a.Observer.Uuid.EntityUuid):lower(),
                    interrupt = tostring(a.Interrupt.InterruptData.Interrupt) }
                rows[#rows + 1] = r
                parts[#parts + 1] = r.uuid .. ":" .. r.interrupt
            end
        end)
    end
    if #rows == 0 then return nil end
    table.sort(parts) -- pairs' order may flip between reads, and a new key restarts the runner's clocks
    return { key = table.concat(parts, "|"), rows = rows }
end

-- The side a prompt belongs to: its observers' when they are all one side's, else its rows' names (a summon: observers
-- are team members or nobody the teams list). nil when neither tells: nobody's.
function S.promptSide(rows, held)
    local side
    for _, r in ipairs(held and held.rows or {}) do
        local s = BG3AI.Arena.member_side(r.uuid)
        if not s or (side and s ~= side) then
            side = nil
            break
        end
        side = s
    end
    return side or S.reactionSide(rows)
end

-- The reaction S.reaction read, as the asking side sees it: a side sees another side's prompt only as there (who it
-- is for and what it offers are that side's). A prompt no team owns is unowned: the harness (no side) answers it.
-- The harness also reads the owner. Outside an arena: unchanged.
function S.sideReaction(reaction)
    local side = RPC.side and RPC.side()
    local arena = BG3AI.Arena and BG3AI.Arena.active and BG3AI.Arena.active()
    if arena and reaction then
        local held = S.heldInterrupt()
        local owner = S.promptSide(reaction.options or {}, held)
        if side and owner ~= side then
            return { other_side = true, unowned = owner == nil or nil }
        elseif not side and owner == nil then
            reaction.unowned = true
        elseif not side then
            reaction.owner = owner
        end
        if held and #held.rows == 1 then
            local who = BG3AI.Arena.name_of(held.rows[1].uuid)
            for _, r in ipairs(reaction.options or {}) do r.who = r.who or who end
        end
    end
    return reaction
end

-- For the harness, on any screen: what the game holds for, popup or not (the bound on an answer starts here, and a
-- hold with no popup is one nobody can answer). nil for a side, outside an arena, and when nothing is held.
function S.heldReaction()
    if RPC.side and RPC.side() then return nil end
    if not (BG3AI.Arena and BG3AI.Arena.active and BG3AI.Arena.active()) then return nil end
    local held = S.heldInterrupt()
    if not held then return nil end
    local ids = {}
    for _, r in ipairs(held.rows) do ids[#ids + 1] = r.interrupt end
    return { key = held.key, side = S.promptSide({}, held), interrupts = ids }
end
