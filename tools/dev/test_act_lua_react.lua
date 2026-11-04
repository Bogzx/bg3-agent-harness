-- Part of test_act_lua.lua (split for the 1000-line cap): a move in combat answers after the reactions it set off.
-- test_act_lua.lua runs it after its stubs and helpers (combat_setup from test_act_lua_move.lua), shared as globals.

-- Window 8 seed 7 m1 (arena-fight-analysis.md S2): Ash's move_to left Birch's reach; it answered "done, hp 20/20" at
-- 132.9 s, Birch's opportunity attack (Target_MainHandAttack, critical 13 Slashing) landed at 133.5 s.
local function seen_stub()
    local saved = BG3AI.Visibility
    BG3AI.Visibility = { context = function() return { is_party = { [P1] = true } } end,
        characterVisible = function() return true end, entity = function() return {} end,
        name = function(u) return u == NPC and "Birch" or "Ash" end }
    stats["Target_MainHandAttack"] = { DisplayName = "Main Hand Attack" }
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    return function() BG3AI.Visibility, Ext.Loca.GetTranslatedString = saved, loca end
end

test("combat move_to: answers after the opportunity attack its walk provoked, naming it with its damage (W8 m1)",
    function()
    combat_setup(12)
    local restore = seen_stub()
    local ok, err = pcall(function()
        local j = job(op("move_to", { char = P1, target = NPC }))
        world[P1].pos = { 2, 0, 0 }
        tick()
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 }) -- Birch reacts mid-walk
        arrive(j, { 8.5, 0, 0 })
        eq(j.status, "running", "arrived, the reaction still resolving: no answer yet")
        tick(5)
        eq(j.status, "running", "still resolving")
        A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 13, "Attack", 0 })
        A.dispatch("CastedSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(4)
        eq(j.status, "running", "its damage settles first")
        tick(6)
        eq(j.status, "done", "then the move answers " .. tostring(j.error))
        local r = j.result.reactions
        eq(#(r or {}), 1, "one reaction")
        eq(r[1].by.uuid, NPC, "by Birch"); eq(r[1].name, "Main Hand Attack", "what")
        eq(r[1].damage, 13, "damage"); eq(r[1].type, "Slashing", "type"); eq(r[1].resolved, true, "resolved")
        near(j.result.after.movement, 3.5, "the move's own result kept")
    end)
    restore()
    if not ok then error(err, 0) end
end)

-- Window 9 MR1 (live.md), times from the move's arrival: the Destroyer's ReactionInterruptUsed -1.30 s, its
-- CastSpell Target_MainHandAttack +0.93 s, AttackedBy +1.07 s, CastedSpell +1.40 s (no REACTIONS: the move had
-- answered at its arrival, before the cast began).
test("combat move_to: a foe's reaction that starts its cast after the arrival is waited for from its interrupt (W9 "
    .. "MR1)", function()
    combat_setup(12)
    local restore = seen_stub()
    local ok, err = pcall(function()
        local j = job(op("move_to", { char = P1, target = NPC }))
        tick()
        A.dispatch("ReactionInterruptUsed", { NPC, "Interrupt_AttackOfOpportunity", 1 })
        tick(16) -- ~1.0 s of the walk
        arrive(j, { 8.5, 0, 0 }) -- +0.3 s: the interrupt 1.3 s before the arrival
        eq(j.status, "running", "arrived, no cast yet: held by the interrupt")
        tick(15)
        eq(j.status, "running", "+0.9 s: still no cast, still held")
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(2)
        A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 9, "Attack", 0 })
        tick(6)
        A.dispatch("CastedSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(4)
        eq(j.status, "running", "its damage settles")
        tick(6)
        eq(j.status, "done", "then the move answers " .. tostring(j.error))
        local r = j.result.reactions or {}
        eq(#r, 1, "one reaction")
        eq(r[1].by.uuid, NPC, "by the Destroyer"); eq(r[1].name, "Main Hand Attack", "what")
        eq(r[1].damage, 9, "damage"); eq(r[1].resolved, true, "resolved")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("combat move_to: an interrupt whose cast never starts holds MOVE_INTERRUPT_WAIT_MS from it; an ally's or an "
    .. "earlier one holds nothing", function()
    combat_setup(12)
    local restore = seen_stub()
    local ok, err = pcall(function()
        A.dispatch("ReactionInterruptUsed", { NPC, "Interrupt_AttackOfOpportunity", 1 }) -- before the move
        tick()
        local j = job(op("move_to", { char = P1, target = NPC }))
        party_rows = { P1, P2 }
        A.dispatch("ReactionInterruptUsed", { P2, "Interrupt_AttackOfOpportunity", 1 }) -- an ally's
        arrive(j, { 8.5, 0, 0 })
        eq(j.status, "done", "no foe's interrupt during it: at once " .. tostring(j.error))
        reset()
        combat_setup(12)
        j = job(op("move_to", { char = P1, target = NPC }))
        A.dispatch("ReactionInterruptUsed", { NPC, "Interrupt_AttackOfOpportunity", 1 })
        arrive(j, { 8.5, 0, 0 }) -- 0.3 s after the interrupt
        local left = math.floor((A.MOVE_INTERRUPT_WAIT_MS - 300) / 60)
        tick(left - 1)
        eq(j.status, "running", "held while the cast may still come")
        tick(3)
        eq(j.status, "done", "released once the wait from the interrupt is over")
        eq(j.result.reactions, nil, "no cast, no reaction")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("combat move_to reactions: none = no wait; an ally's or an old cast holds nothing; an unresolved one is cut at "
    .. "MOVE_REACT_HOLD_MS", function()
    combat_setup(12)
    local restore = seen_stub()
    local ok, err = pcall(function()
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 }) -- before the move
        tick()
        local j = job(op("move_to", { char = P1, target = NPC }))
        party_rows = { P1, P2 }
        A.dispatch("CastSpell", { P2, "Target_MainHandAttack", "Target", "None", 0 }) -- an ally's
        arrive(j, { 8.5, 0, 0 })
        eq(j.status, "done", "no foe's cast began during it: at once")
        eq(j.result.reactions, nil, "no reactions")
        reset()
        combat_setup(12)
        stats["Target_MainHandAttack"] = { DisplayName = "Main Hand Attack" }
        j = job(op("move_to", { char = P1, target = NPC }))
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        arrive(j, { 8.5, 0, 0 })
        tick(math.floor(A.MOVE_REACT_HOLD_MS / 60) - 2)
        eq(j.status, "running", "held")
        tick(3)
        eq(j.status, "done", "cut at the cap")
        eq(j.result.reactions[1].resolved, nil, "not resolved")
        eq(j.result.reactions[1].damage, 0, "no damage seen")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("out of combat a move_to doesn't wait for anyone's cast", function()
    local restore = seen_stub()
    local j = job(op("move_to", { char = P1, target = NPC }))
    A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
    arrive(j, { 8.5, 0, 0 })
    restore()
    eq(j.status, "done", "at once " .. tostring(j.error))
end)

-- A reactor the party can't see (hidden, invisible): REACTIONS names neither it nor its spell, but the damage the
-- mover took shows, as the mover's own HP drop does (it was dropped from the result before).
test("combat move_to: an unseen enemy's reaction is not named, its damage to the mover still shows", function()
    combat_setup(12)
    local restore = seen_stub()
    BG3AI.Visibility.characterVisible = function() return false end
    local ok, err = pcall(function()
        local j = job(op("move_to", { char = P1, pos = { 8.5, 0, 0 } })) -- away from it: a spot, not the unseen one
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        arrive(j, { 8.5, 0, 0 })
        A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 13, "Attack", 0 })
        A.dispatch("CastedSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(10)
        eq(j.status, "done", "answered " .. tostring(j.error))
        local r = j.result.reactions or {}
        eq(#r, 1, "one reaction")
        eq(r[1].unseen, true, "unseen"); eq(r[1].by, nil, "no reactor named"); eq(r[1].name, nil, "no spell named")
        eq(r[1].damage, 13, "its damage"); eq(r[1].type, "Slashing", "type")
    end)
    restore()
    if not ok then error(err, 0) end
end)

-- Window 9 MR2 (live.md): the game's path preview marks where a path leaves an enemy's reach ring ("Provokes Attack
-- of Opportunity"), and shows nothing once that enemy's reaction is spent (0/1). G.path_provokes is the same fact.
test("path_provokes: a visible enemy with its reaction whose reach the path leaves, where and how far along; none "
    .. "for a spent reaction, an unseen enemy, a non-enemy, or out of combat", function()
    combat_setup(12)
    osiv[key("IsEnemy", P1, GOB)], osiv[key("IsInCombat", GOB)] = 1, 1
    world[GOB].comps.ActionResources = { Resources = { ReactionActionPoint = { { Level = 0, Amount = 1 } } } }
    local seen = true
    local saved = BG3AI.Visibility
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        charactersNear = function(centers, radius)
            local out = {}
            for _, u in ipairs({ GOB, P1 }) do
                local p = world[u].pos
                for _, c in ipairs(centers) do
                    local d = math.sqrt((p[1] - c[1]) ^ 2 + (p[2] - c[2]) ^ 2 + (p[3] - c[3]) ^ 2)
                    if d <= radius then out[#out + 1] = { { u = u }, p, d }; break end
                end
            end
            return out
        end,
        entityUuid = function(e) return e.u end,
        name = function(u) return u == GOB and "Goblin" or "Ash" end,
        characterVisible = function(_, u) return u ~= GOB or seen end,
    }
    local ok, err = pcall(function()
        -- the goblin at 2.4 m on x; reach 1.5 m + both Move radii 0.25 (the fallback: no AIBounds here) = 2.0 m: the
        -- path from x 1 to x -5 leaves it at 0.4
        local path = { { 1, 0, 0 }, { -2, 0, 0 }, { -5, 0, 0 } }
        local r = G.path_provokes(P1, path)
        eq(#r, 1, "one")
        eq(r[1].uuid, GOB, "the goblin"); eq(r[1].name, "Goblin", "named")
        eq(r[1].along_m, 0.6, "0.6 m along")
        eq(string.format("%.1f,%.1f,%.1f", table.unpack(r[1].at)), "0.4,0.0,0.0", "where it leaves the reach")
        eq(#G.path_provokes(P1, { { -5, 0, 0 }, { -9, 0, 0 } }), 0, "a path never in its reach")
        world[GOB].comps.ActionResources.Resources.ReactionActionPoint[1].Amount = 0
        eq(#G.path_provokes(P1, path), 0, "its reaction spent: nothing, as the game's preview")
        world[GOB].comps.ActionResources.Resources.ReactionActionPoint[1].Amount = 1
        seen = false
        eq(#G.path_provokes(P1, path), 0, "unseen: nothing")
        seen = true
        osiv[key("IsEnemy", P1, GOB)] = nil
        eq(#G.path_provokes(P1, path), 0, "not an enemy: nothing")
        osiv[key("IsEnemy", P1, GOB)] = 1
        osiv[key("IsInCombat", P1)] = nil
        eq(G.path_provokes(P1, path), nil, "out of combat: no claim")
    end)
    BG3AI.Visibility = saved
    if not ok then error(err, 0) end
end)
