-- Part of test_act_lua.lua (split for the 1000-line cap): cooldowns (w10 CD1), the guard's refusal and get_actions'
-- UNAVAILABLE reading one rule (Guard.cooldowns).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- w10 CD1 (Pommel Strike, int-v010b): the row read Cooldown=-1.0 CooldownType=UntilShortRest through end_turn and the
-- round close; get_actions said UNAVAILABLE (cooldown) and use_ability cast it again (the guard wanted Cooldown > 0).
-- One rule now: any SpellBookCooldowns row refuses, with how long it lasts.
test("known_spell: a cooldown row refuses whatever its Cooldown value, saying until when (w10 CD1)", function()
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_PommelStrike" } },
        { Id = { Prototype = "Shout_ActionSurge" } }, { Id = { Prototype = "Target_Riposte" } },
        { Id = { Prototype = "Shout_SecondWind" } }, { Id = { Prototype = "Target_MainHandAttack" } } } }
    local function row(id, cd, kind) return { SpellId = { Prototype = id }, Cooldown = cd, CooldownType = kind } end
    world[P1].comps.SpellBookCooldowns = { Cooldowns = { row("Target_PommelStrike", -1.0, "UntilShortRest"),
        row("Shout_ActionSurge", 0, "UntilRest"), row("Target_Riposte", 6.0, "OncePerTurn"),
        row("Shout_SecondWind", -1.0, "OncePerCombat") } }
    refused(function() G.known_spell(P1, "Target_PommelStrike") end,
        "that action is on cooldown until a short rest (UntilShortRest)")
    refused(function() G.known_spell(P1, "Shout_ActionSurge") end, "on cooldown until a long rest (UntilRest)")
    refused(function() G.known_spell(P1, "Target_Riposte") end, "on cooldown until its next turn (OncePerTurn)")
    refused(function() G.known_spell(P1, "Shout_SecondWind") end, "for the rest of this combat (OncePerCombat)")
    eq(G.known_spell(P1, "Target_MainHandAttack").Id.Prototype, "Target_MainHandAttack", "no row: casts")
    world[P1].comps.SpellBookCooldowns = { Cooldowns = {} }
    eq(G.known_spell(P1, "Target_PommelStrike").Id.Prototype, "Target_PommelStrike", "row gone: casts")
    world[P1].comps.SpellBook = nil
end)

-- The cooldown state fails closed: with the rows unread (no SpellBookCooldowns component, or a row that doesn't read)
-- the guard can't see a running cooldown, so a spell whose stats give it one is refused with that fact; a spell
-- without a Cooldown in its stats still casts. Before, an unread state read as "no cooldown" and cast.
test("known_spell: unread cooldown rows refuse a spell that can have a cooldown (fail closed)", function()
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_PommelStrike" } },
        { Id = { Prototype = "Target_MainHandAttack" } }, { Id = { Prototype = "Target_Unlisted" } } } }
    stats.Target_PommelStrike = { Cooldown = "OncePerShortRest" }
    stats.Target_MainHandAttack = { Cooldown = "" }
    local unread = "the cooldown state of Target_PommelStrike could not be read"
    world[P1].comps.SpellBookCooldowns = false -- no component
    refused(function() G.known_spell(P1, "Target_PommelStrike") end, unread)
    refused(function() G.known_spell(P1, "Target_Unlisted") end, "the cooldown state of Target_Unlisted")
    eq(G.known_spell(P1, "Target_MainHandAttack").Id.Prototype, "Target_MainHandAttack", "no cooldown in its stats")
    world[P1].comps.SpellBookCooldowns = { Cooldowns = { { CooldownType = "OncePerTurn" } } } -- SpellId unread
    refused(function() G.known_spell(P1, "Target_PommelStrike") end, unread)
    stats.Target_MainHandAttack.Cooldown = "None"
    eq(G.known_spell(P1, "Target_MainHandAttack").Id.Prototype, "Target_MainHandAttack", "Cooldown None")
    world[P1].comps.SpellBookCooldowns = { Cooldowns = {} }
    eq(G.known_spell(P1, "Target_PommelStrike").Id.Prototype, "Target_PommelStrike", "rows read, none: casts")
    world[P1].comps.SpellBook, world[P1].comps.SpellBookCooldowns = nil, nil
    stats.Target_PommelStrike, stats.Target_MainHandAttack = nil, nil
end)

-- w10 CD1: get_actions and the guard read one cooldown rule: what state.actions lists UNAVAILABLE for a cooldown,
-- the guard refuses, and nothing else (the CD1 row: Cooldown=-1.0 UntilShortRest).
test("state.actions and the guard agree on cooldowns (w10 CD1's -1 UntilShortRest row)", function()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
        local okf, v = pcall(f, ...)
        if okf then return v end
    end }, { __index = savedV })
    if not BG3AI.State or not BG3AI.State.actions then dofile(LUA .. "/Server/State.lua") end
    for _, id in ipairs({ "Target_PommelStrike", "Shout_SecondWind", "Target_Trip" }) do
        stats[id] = { SpellType = "Target", UseCosts = "BonusActionPoint:1" }
    end
    local function book(id) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = "Unknown" } end
    world[P1].comps.SpellBook = { Spells = { book("Target_PommelStrike"), book("Shout_SecondWind"),
        book("Target_Trip") } }
    world[P1].comps.SpellBookCooldowns = { Cooldowns = {
        { SpellId = { Prototype = "Target_PommelStrike" }, Cooldown = -1.0, CooldownType = "UntilShortRest" },
        { SpellId = { Prototype = "Shout_SecondWind" }, Cooldown = 0, CooldownType = "UntilShortRest" } } }
    BG3AI.State.spellCache = {}
    local ok, list = pcall(BG3AI.State.actions, P1)
    BG3AI.Visibility = savedV
    eq(ok, true, "actions read: " .. tostring(list))
    for _, a in ipairs(list) do
        local gok, gerr = pcall(G.known_spell, P1, a.spell)
        local cd = (a.reason or ""):find("^cooldown") ~= nil
        local refusedCd = not gok and tostring(G.is_refusal(gerr) and gerr.reason or gerr):find("on cooldown") ~= nil
        eq(refusedCd, cd, a.spell .. ": guard refusal vs get_actions cooldown")
        if a.spell == "Target_PommelStrike" then eq(a.reason, "cooldown (UntilShortRest)", "Pommel's reason") end
    end
    world[P1].comps.SpellBook, world[P1].comps.SpellBookCooldowns = nil, nil
end)

-- REVIEW-17c: get_actions reads the same fail-closed rule: with the cooldown rows unread, a spell that can have a
-- cooldown is listed UNAVAILABLE with the guard's fact, one without a Cooldown stays available, and the guard refuses
-- exactly what is listed so.
test("state.actions lists a spell UNAVAILABLE when its cooldown rows don't read, as the guard refuses it", function()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
        local okf, v = pcall(f, ...)
        if okf then return v end
    end }, { __index = savedV })
    if not BG3AI.State or not BG3AI.State.actions then dofile(LUA .. "/Server/State.lua") end
    stats.Target_PommelStrike = { SpellType = "Target", UseCosts = "BonusActionPoint:1", Cooldown = "OncePerShortRest" }
    stats.Target_Trip = { SpellType = "Target", UseCosts = "BonusActionPoint:1", Cooldown = "" }
    local function book(id) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = "Unknown" } end
    world[P1].comps.SpellBook = { Spells = { book("Target_PommelStrike"), book("Target_Trip") } }
    world[P1].comps.SpellBookCooldowns = false -- no component
    BG3AI.State.spellCache = {}
    local ok, list = pcall(BG3AI.State.actions, P1)
    BG3AI.Visibility = savedV
    eq(ok, true, "actions read: " .. tostring(list))
    local by = {}
    for _, a in ipairs(list) do
        by[a.spell] = a
        local gok, gerr = pcall(G.known_spell, P1, a.spell)
        local why = not gok and tostring(G.is_refusal(gerr) and gerr.reason or gerr) or ""
        eq(why:find("could not be read") ~= nil, (a.reason or ""):find("^the cooldown state of") ~= nil,
            a.spell .. ": guard refusal vs get_actions")
    end
    eq(by.Target_PommelStrike.available, false, "Pommel unavailable")
    eq(by.Target_PommelStrike.reason, "the cooldown state of Target_PommelStrike could not be read", "Pommel's reason")
    eq((by.Target_Trip.reason or ""):find("cooldown"), nil, "Trip (no Cooldown): no cooldown reason")
    world[P1].comps.SpellBook, world[P1].comps.SpellBookCooldowns = nil, nil
    stats.Target_PommelStrike, stats.Target_Trip = nil, nil
end)
