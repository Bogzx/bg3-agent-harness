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
    osiv[key("IsEnemy", GOB, P1)], osiv[key("CanSee", GOB, P1)] = 1, 1 -- the engine's view: hostile, sees the mover
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
        -- the goblin at 2.4 m on x; the path from x 1 to x -5 leaves its reach (G.melee_reach) on the way
        local path = { { 1, 0, 0 }, { -2, 0, 0 }, { -5, 0, 0 } }
        local r = G.path_provokes(P1, path)
        eq(#r, 1, "one")
        eq(r[1].uuid, GOB, "the goblin"); eq(r[1].name, "Goblin", "named")
        local reach = G.melee_reach(GOB, P1, G.DEFAULT_MELEE_M) -- the goblin at x 2.4, the path from x 1 west
        eq(r[1].along_m, U.round(1 - (2.4 - reach), 1), "how far along")
        eq(string.format("%.1f", r[1].at[1]), string.format("%.1f", 2.4 - reach), "where it leaves the reach")
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
        osiv[key("IsInCombat", P1)] = 1
        -- what stops the attack, as the engine reads it (each alone; the rest as above)
        stats.DISENGAGE = { Boosts = "IgnoreLeaveAttackRange()" }
        stats.SLEEPING = { StatusType = "INCAPACITATED" }
        stats.SHOCKING_GRASP = { StatusType = "BOOST", Boosts = "ActionResourceBlock(ReactionActionPoint)" }
        eq(#G.path_provokes(P1, path), 1, "the baseline still provokes")
        world[P1].comps.StatusContainer = { Statuses = { h1 = "DISENGAGE" } }
        eq(#G.path_provokes(P1, path), 0, "the mover Disengaged (IgnoreLeaveAttackRange): nothing")
        world[P1].comps.StatusContainer = nil
        for _, id in ipairs({ "SLEEPING", "SHOCKING_GRASP" }) do
            world[GOB].comps.StatusContainer = { Statuses = { h1 = id } }
            eq(#G.path_provokes(P1, path), 0, id .. " on the goblin: it can't react, nothing")
        end
        world[GOB].comps.StatusContainer = nil
        osiv[key("IsEnemy", GOB, P1)] = nil
        eq(#G.path_provokes(P1, path), 0, "the game doesn't call the goblin hostile to the mover: nothing")
        osiv[key("IsEnemy", GOB, P1)] = 1
        osiv[key("CanSee", GOB, P1)] = nil
        eq(#G.path_provokes(P1, path), 0, "the goblin can't see the mover: nothing")
        osiv[key("CanSee", GOB, P1)] = 1
        -- a jump/climb link is no IgnoreAoO spell (Interrupt_AttackOfOpportunity's conditions): it provokes too
        eq(#G.path_provokes(P1, path, { 2 }), 1, "the reach left on a jump/climb link (points[2] ends one): provoked")
    end)
    BG3AI.Visibility = saved
    if not ok then error(err, 0) end
end)

-- Window 11 J1b (live.md L7 (e)): Karlach's jump out of the Destroyer's reach: ReactionInterruptUsed and the jump's
-- CastSpellFailed (outcome Canceled) in the same ms, interrupt first; the AoO's CastSpell +1.97 s, CastedSpell +2.44 s.
test("combat jump: the game's cancel when it draws an opportunity attack is said as that; a Canceled without a "
    .. "foe's interrupt (or after an ally's, or one not within the window of the failure) keeps the engine's words "
    .. "(W11 J1b, REVIEW-17 F6)", function()
    local function jump(interrupter, fail_first)
        reset()
        combat_setup(12)
        stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
        stats.Target_MainHandAttack = { DisplayName = "Main Hand Attack" } -- reset() cleared seen_stub's
        world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
        osiv[key("GetAbility", P1, "Strength")] = 16
        party_rows = { P1, P2 }
        cast_outcome = "Canceled"
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3.5, 0, 0 } }))
        eq(j.status, "running", "ordered " .. tostring(j.error))
        tick()
        local function aoo()
            A.dispatch("ReactionInterruptUsed", { interrupter, "Interrupt_AttackOfOpportunity", 1 })
        end
        if interrupter and not fail_first then aoo() end
        A.dispatch("CastSpellFailed", { P1, "Projectile_Jump", "Projectile", "None", 0 })
        if interrupter and fail_first then aoo() end
        return j
    end
    local function attack()
        tick(32)
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(8)
        A.dispatch("CastedSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(12)
    end
    local restore = seen_stub()
    local ok, err = pcall(function()
        local aoo_why = "the jump didn't happen: the game cancelled it when it drew the attack of opportunity; "
            .. "movement and bonus action not spent"
        -- the interrupt dispatched first (J1b), or right after the CastSpellFailed in the same ms (REVIEW-17 F6 a)
        for _, fail_first in ipairs({ false, true }) do
            local j = jump(NPC, fail_first)
            eq(j.status, "running", "held for the attack it drew")
            attack()
            eq(j.status, "failed", "status")
            eq(j.error, aoo_why, "why, fail first " .. tostring(fail_first))
            eq(#(j.result and j.result.reactions or {}), 1, "the attack listed")
        end
        -- REVIEW-17 F6 b: a foe's reaction earlier in the job (a walk-to phase's), resolved; then the jump is
        -- Canceled with no interrupt near it: the engine's words
        reset()
        combat_setup(12)
        stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
        stats.Target_MainHandAttack = { DisplayName = "Main Hand Attack" }
        world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
        osiv[key("GetAbility", P1, "Strength")] = 16
        party_rows = { P1, P2 }
        cast_outcome = "Canceled"
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3.5, 0, 0 } }))
        tick()
        A.dispatch("ReactionInterruptUsed", { NPC, "Interrupt_AttackOfOpportunity", 1 })
        A.dispatch("CastSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(2)
        A.dispatch("CastedSpell", { NPC, "Target_MainHandAttack", "Target", "None", 0 })
        tick(10)
        eq(j.status, "running", "still running after the earlier reaction")
        A.dispatch("CastSpellFailed", { P1, "Projectile_Jump", "Projectile", "None", 0 })
        tick(12)
        eq(j.status, "failed", "status (b)")
        eq(j.error, "the game refused the cast: the cast was cancelled (the game's Canceled)", "why (b)")
        for _, who in ipairs({ false, P2 }) do
            j = jump(who)
            tick(80)
            eq(j.status, "failed", "status")
            eq(j.error, "the game refused the cast: the cast was cancelled (the game's Canceled)", tostring(who))
        end
    end)
    restore()
    if not ok then error(err, 0) end
end)
