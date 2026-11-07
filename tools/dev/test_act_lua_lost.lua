-- Part of test_act_lua.lua (split for the 1000-line cap): a character whose turn the game runs itself (live.md
-- "## Goaded read": Goading Attack on a twin and on Gale in normal play, the AI's casts, spending and turn end with no
-- order; the HUD's "Can't control: Goaded"; a player's End Turn click ignored; hotbar cells can_use false). The mod
-- reads it from the game's LoseControlComponent (Guard.lost_control); nothing a client sends reaches the game for it.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local WORDS = "can't control: Goaded, the game runs its turn"

-- fn() with P1 in combat, its turn open, Goaded by the game's state (StatusLoseControl) and its status's flag.
local function withGoaded(fn, unnamed)
    combat_setup(9)
    world[P1].comps.StatusLoseControl = { LoseControl = true }
    spell("Target_MH", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    spell("GOADING_ATTACK", { StatusType = "BOOST", StatusPropertyFlags = { "LoseControlFriendly", "OverheadOnTurn",
        "LoseControl" } })
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function()
        return unnamed and {} or { { id = "GOADING_ATTACK", name = "Goaded", turns = 1 } }
    end }, { __index = savedV })
    local ok, err = pcall(fn)
    BG3AI.Visibility = savedV
    world[P1].comps.StatusLoseControl = nil
    if not ok then error(err, 0) end
end

test("lost control: the read is the game's component, with the status that carries the flag as its name", function()
    withGoaded(function()
        eq(G.lost_control(P1).name, "Goaded", "named by its status")
        eq(G.lost_control_text(G.lost_control(P1)), WORDS, "the HUD's words and the fact")
        world[P1].comps.StatusLoseControl = nil
        eq(G.lost_control(P1), nil, "the flag alone isn't the read: no component, not lost")
    end)
    withGoaded(function()
        eq(G.lost_control(P1).name, nil, "no shown status")
        eq(G.lost_control_text(G.lost_control(P1)), "the game runs its turn", "words without a name")
    end, true)
end)

-- live.md "## Goaded read 2": the component is there under all 5 statuses read; its LoseControl bool was false under
-- Command: Approach and Confusion while the game's AI ran them. Presence is the read (orchestrator D319).
test("lost control: the component's presence is the read, whatever its LoseControl bool says", function()
    withGoaded(function()
        world[P1].comps.StatusLoseControl = { LoseControl = false }
        eq(G.lost_control(P1) ~= nil, true, "bool false: still the game's turn")
        eq(G.is_my_turn(P1), false, "not the side's")
        world[P1].comps.StatusLoseControl = { LoseControl = true }
        eq(G.lost_control(P1) ~= nil, true, "bool true")
    end)
end)

-- Fairness (D319): Minor Illusion carries the flag but hides from the portrait and shows no HUD bar; a status the
-- visible filter drops is never named, and no other shown status is named in its place.
test("lost control: a status a player can't see isn't named: the words are the fact alone", function()
    withGoaded(function()
        spell("BLESS", { StatusType = "BOOST" }) -- shown, no LoseControl flag
        local savedV = BG3AI.Visibility
        BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "BLESS", name = "Blessed" } } end },
            { __index = savedV })
        local ok, err = pcall(function()
            eq(G.lost_control(P1).name, nil, "not named after another status")
            local j = job(op("cast", { char = P1, spell = "Target_MH", target = NPC }))
            eq(j.error, "the game runs its turn", "refused, no name and no HUD words")
            G._disabling = {}
            local d = G.disabling(P1)
            eq(d[#d].name, "the game runs its turn", "the DISABLED entry")
            eq(d[#d].effect, nil, "said once")
        end)
        BG3AI.Visibility = savedV
        if not ok then error(err, 0) end
    end, true)
end)

test("lost control: is_my_turn is false while the game runs the turn, true once it doesn't", function()
    withGoaded(function()
        eq(G.is_my_turn(P1), false, "the game's turn, not the side's")
        world[P1].comps.StatusLoseControl = nil
        eq(G.is_my_turn(P1), true, "the same open turn without the component")
    end)
end)

test("lost control: a cast is refused at once in the one wording, no UseSpell sent", function()
    withGoaded(function()
        local n0 = #called("UseSpell")
        local j = job(op("cast", { char = P1, spell = "Target_MH", target = NPC }))
        eq(j.status, "failed", "refused")
        eq(j.error, WORDS, "the words")
        eq(#called("UseSpell"), n0, "nothing reached the game")
    end)
end)

test("lost control: end_turn is refused at once in the same words, no EndTurn sent (a player's click is ignored)",
    function()
    withGoaded(function()
        local n0 = #called("EndTurn")
        local j = job(op("end_turn", { char = P1 }))
        eq(j.status, "failed", "refused")
        eq(j.error, WORDS, "the words")
        eq(#called("EndTurn"), n0, "no EndTurn")
    end)
end)

test("lost control: move_to and stop are refused, no walk and no purge sent", function()
    withGoaded(function()
        local m0, p0 = #called("CharacterMoveToPosition"), #called("PurgeOsirisQueue")
        local j = job(op("move_to", { char = P1, target = NPC }))
        eq(j.status, "failed", "move refused")
        eq(j.error, WORDS, "move's words")
        local k = job(op("stop", { char = P1 }))
        eq(k.status, "failed", "stop refused")
        eq(k.error, WORDS, "stop's words")
        eq(#called("CharacterMoveToPosition"), m0, "no walk")
        eq(#called("PurgeOsirisQueue"), p0, "no purge (it would cut the game's cast)")
    end)
end)

-- live.md case B: an order a moment before the game took the turn never started; the game cast instead.
test("lost control: a cast ordered before the status came fails in the same words, with no purge and no re-order",
    function()
    cast_seen = "none" -- the game took no cast from the order: it never starts
    combat_setup(9)
    spell("Target_MH", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_MH", target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    local u0, p0 = #called("UseSpell"), #called("PurgeOsirisQueue")
    world[P1].comps.StatusLoseControl = { LoseControl = true }
    tick(55) -- 3.3 s, past the combat no-start bound
    world[P1].comps.StatusLoseControl = nil
    eq(j.status, "failed", "never started")
    eq(j.error, "the game runs its turn", "the words, not the order before it")
    eq(#called("UseSpell"), u0, "not ordered again")
    eq(#called("PurgeOsirisQueue"), p0, "the game's cast not purged")
end)

test("lost control: a walk ordered before the status came fails in the same words, the game's cast not purged",
    function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    local p0 = #called("PurgeOsirisQueue")
    world[P1].comps.StatusLoseControl = { LoseControl = true }
    tick(43) -- past the 2.5 s no-start window
    world[P1].comps.StatusLoseControl = nil
    eq(j.status, "failed", "never started")
    eq(j.error, "the game runs its turn", "the words")
    eq(#called("PurgeOsirisQueue"), p0, "no purge")
end)

test("lost control: an end_turn doesn't list a member the game runs as still to act", function()
    combat_setup(9)
    party_rows = { P1, P2 }
    world[P2] = { pos = { 1, 0, 0 }, comps = { TurnBased = { IsActiveCombatTurn = true },
        StatusLoseControl = { LoseControl = true } } }
    osiv[key("IsInCombat", P2)], osiv[key("IsCharacter", P2)], osiv[key("IsOnStage", P2)] = 1, 1, 1
    local j = job(op("end_turn", { char = P1 }))
    tick(30)
    eq(j.result and j.result.still_to_act, nil, "nobody waits on the game's turn")
end)

test("lost control: G.osi lets no order for that character through, whatever route sends it", function()
    withGoaded(function()
        for _, name in ipairs({ "UseSpell", "EndTurn", "PurgeOsirisQueue", "CharacterMoveToPosition" }) do
            local ok, err = pcall(G.osi, name, P1, "x", NPC, 0)
            eq(ok, false, name .. " refused")
            eq(G.is_refusal(err) and err.reason, WORDS, name .. "'s words")
        end
        eq(#called("EndTurn"), 0, "none reached the game")
    end)
end)

-- live.md (e): an end on record for a member from before; another member's end_turn re-sent it an EndTurn while the
-- game ran that member's next turn.
test("lost control: another member's end_turn doesn't re-send an old end to a member the game runs", function()
    combat_setup(9)
    party_rows = { P1, P2 }
    world[P2] = { pos = { 1, 0, 0 }, comps = { TurnBased = { IsActiveCombatTurn = true },
        StatusLoseControl = { LoseControl = true } } }
    osiv[key("IsInCombat", P2)], osiv[key("IsCharacter", P2)], osiv[key("IsOnStage", P2)] = 1, 1, 1
    G.end_requested[P2] = G.now_ms()
    local j = job(op("end_turn", { char = P1 }))
    world[P1].comps.TurnBased.RequestedEndTurn = true
    A.dispatch("TurnEnded", { P1 }) -- P1's end went through: the route looks for dropped ends to send again
    G.end_requested[P2] = nil
    eq(j.status, "done", "P1 ended " .. tostring(j.error))
    local resent = 0
    for _, c in ipairs(called("EndTurn")) do if c[2] == P2 then resent = resent + 1 end end
    eq(resent, 0, "no EndTurn for the member the game runs")
    eq(j.result and j.result.ended_again, nil, "nothing reported as ended again")
end)

test("lost control: state.actions lists every action unavailable in the same words", function()
    withGoaded(function()
        local savedV = BG3AI.Visibility
        BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
            local okf, v = pcall(f, ...)
            if okf then return v end
        end }, { __index = savedV })
        if not BG3AI.State or not BG3AI.State.actions then dofile(LUA .. "/Server/State.lua") end
        stats.Target_PommelStrike = { SpellType = "Target", UseCosts = "BonusActionPoint:1" }
        world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_PommelStrike",
            OriginatorPrototype = "Target_PommelStrike" }, PrepareType = "Unknown" } } }
        BG3AI.State.spellCache = {}
        local ok, list = pcall(BG3AI.State.actions, P1)
        BG3AI.Visibility = savedV
        world[P1].comps.SpellBook = nil
        eq(ok, true, "read: " .. tostring(list))
        eq(#list > 0, true, "listed")
        for _, a in ipairs(list) do
            eq(a.available, false, a.spell)
            eq(a.reason, WORDS, a.spell .. "'s reason")
        end
    end)
end)

test("lost control: the DISABLED entry carries the same words on the status's own row", function()
    withGoaded(function()
        G._disabling = {}
        local d = G.disabling(P1)
        eq(d and #d, 1, "one entry")
        eq(d[1].name, "Goaded", "the status")
        eq(d[1].turns, 1, "its turns")
        eq(d[1].effect, WORDS, "the words")
    end)
end)

-- REVIEW-reactions F1: every read was of a combat turn; out of combat the component on changes nothing, an order runs
-- as before this rule, and back in combat it is refused again.
test("lost control: out of combat the component changes nothing, an order runs as before", function()
    withGoaded(function()
        osiv[key("IsInCombat", P1)] = nil
        eq(G.lost_control(P1), nil, "not lost out of combat")
        local m0 = #called("CharacterMoveToPosition")
        local j = job(op("move_to", { char = P1, target = NPC }))
        eq(j.error ~= WORDS, true, "move not refused in the lost words")
        eq(#called("CharacterMoveToPosition") > m0, true, "the walk is sent")
        eq((pcall(G.osi, "CharacterMoveToPosition", P1, 0, 0, 0)), true, "G.osi lets it through")
        osiv[key("IsInCombat", P1)] = 1
        eq(G.lost_control(P1) ~= nil, true, "in combat: lost again")
        local ok, err = pcall(G.osi, "CharacterMoveToPosition", P1, 0, 0, 0)
        eq(ok == false and G.is_refusal(err) and err.reason, WORDS, "in combat: refused")
    end)
end)
