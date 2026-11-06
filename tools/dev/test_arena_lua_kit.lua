-- Part of test_arena_lua.lua (split for the 1000-line cap): what S4 (live 2026-11-06) found in a mirror kit, on the
-- helpers test_arena_lua_mirror.lua shares as MIRROR: the kit's wait for the copy's rebuild, the passive count, the
-- spell rows' abilities, and how a twin is compared with its build's reference.

local M = MIRROR
local guarded, twin, kit, runJob = M.guarded, M.twin, M.kit, M.runJob
local T1, T2, HALFELF = M.T1, M.T2, M.HALFELF

local function start(over)
    return RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit(over) }).job]
end

local function row(id, source, ability, origin)
    return { Id = { Prototype = id, OriginatorPrototype = origin or id, SourceType = source },
        SpellCastingAbility = ability }
end

local function twins()
    twin(T1)
    twin(T2)
    replicated = {}
end

local function book(u, rows, casting)
    world[u].comps.SpellBook.Spells = rows
    world[u].comps.Stats = { SpellCastingAbility = casting or "Intelligence" }
end

-- The game as builds-1 read it: a rebuild `at` ms after the spawn puts each AddPassive'd passive back on top of itself.
local function rebuilds(at)
    M.stub("AddPassive", function(u, id)
        local p = world[u].comps.PassiveContainer.Passives
        p[#p + 1] = { Passive = { PassiveId = id } }
    end)
    local n, due = #tickers, clock + at
    tickers[#tickers + 1] = function()
        if clock < due then return end
        due = math.huge
        for _, u in ipairs({ T1, T2 }) do
            local p = world[u].comps.PassiveContainer.Passives
            for i = 1, #p do p[#p + 1] = { Passive = { PassiveId = p[i].Passive.PassiveId } } end
        end
    end
    return function() for i = #tickers, n + 1, -1 do table.remove(tickers, i) end end
end

test("mirror_kit waits SETTLE_MS on ready twins: nothing applied before, the game's rebuild doubles nothing",
    guarded(function()
        twins()
        local done = rebuilds(1000) -- builds-1 0a: a kit 1.0 s after the spawn read each passive once
        local j, from = start(), clock
        while clock - from < Ar.Mirror.SETTLE_MS - Ar.PHASE_EVERY_MS do
            tick()
            eq(#M.calls(), 0, "no call at " .. (clock - from) .. " ms")
        end
        runJob(j)
        done()
        eq(j.status, "done", tostring(j.error))
        eq(#world[T1].comps.PassiveContainer.Passives, 4, "each of the kit's 4 passives once")
    end))

test("a rebuild that lands after the kit fails the job naming every passive held twice", guarded(function()
    twins()
    local done = rebuilds(Ar.Mirror.SETTLE_MS + 1)
    local j = start()
    runJob(j)
    done()
    eq(j.status, "failed", "a doubled passive is no finished kit")
    assert(tostring(j.error):find(T1 .. ": Darkvision", 1, true), tostring(j.error))
    assert(tostring(j.error):find(T2 .. ": FightingStyle_Defense", 1, true), tostring(j.error))
end))

test("spell_abilities: both twins' rows written, replicated, the calls recorded; a row already right is left alone",
    guarded(function()
        twins()
        for _, u in ipairs({ T1, T2 }) do
            book(u, { row("Target_CureWounds", "Osiris", "None"), row("Target_DancingLights", "Osiris", "None"),
                row("Target_HuntersMark", "Osiris", "Wisdom"), row("Target_MainHandAttack", "WeaponSpell", "None") })
        end
        local j = start({ spell_abilities = { Target_CureWounds = "Wisdom", Target_DancingLights = "Charisma",
            Target_HuntersMark = "Wisdom" } })
        runJob(j)
        eq(j.status, "done", tostring(j.error))
        for _, u in ipairs({ T1, T2 }) do
            local s = world[u].comps.SpellBook.Spells
            eq(enc({ s[1].SpellCastingAbility, s[2].SpellCastingAbility, s[3].SpellCastingAbility,
                s[4].SpellCastingAbility }), enc({ "Wisdom", "Charisma", "Wisdom", "None" }), "the rows of " .. u)
        end
        eq(enc(replicated), enc({ T1 .. ":SpellBook", T2 .. ":SpellBook" }), "replicated once each")
        local c = j.result.calls
        eq(enc({ c[#c - 1], c[#c] }), enc({ { "SpellCastingAbility", T1, 2 }, { "SpellCastingAbility", T2, 2 } }),
            "two rows written on each")
    end))

test("SpellSet rows take the stats' ability, as a real player's do; the kit needs no key for it", guarded(function()
    twins()
    for _, u in ipairs({ T1, T2 }) do
        book(u, { row("Shout_Dash", "SpellSet", "Charisma"), row("Shout_Rage", "ProgressionClass", "Charisma") })
    end
    local j = start()
    runJob(j)
    eq(j.status, "done", tostring(j.error))
    local s = world[T2].comps.SpellBook.Spells
    eq(enc({ s[1].SpellCastingAbility, s[2].SpellCastingAbility }), enc({ "Intelligence", "Charisma" }),
        "the common action written, the class's own row left")
end))

test("a nothing-to-write kit replicates nothing (arena-v0's Fighter and Rogue)", guarded(function()
    twins()
    for _, u in ipairs({ T1, T2 }) do book(u, { row("Shout_Dash", "SpellSet", "Intelligence") }) end
    local j = start()
    runJob(j)
    eq(j.status, "done", tostring(j.error))
    eq(#replicated, 0, "no write")
end))

test("a container spell's variants are found by their originator", guarded(function()
    twins()
    for _, u in ipairs({ T1, T2 }) do
        book(u, { row("Projectile_Orb_Cold", "Osiris", "None", "Projectile_Orb"),
            row("Projectile_Orb", "Osiris", "None") })
    end
    local j = start({ spell_abilities = { Projectile_Orb = "Intelligence" } })
    runJob(j)
    eq(j.status, "done", tostring(j.error))
    eq(world[T1].comps.SpellBook.Spells[1].SpellCastingAbility, "Intelligence", "the variant")
end))

test("a kit's spell that lands late is written when it does; one that never lands fails naming it", guarded(function()
    twins()
    book(T1, {})
    book(T2, {})
    local n = #tickers
    local due = clock + Ar.Mirror.SETTLE_MS + Ar.Mirror.ITEM_WAIT_MS / 2
    tickers[#tickers + 1] = function()
        if clock < due then return end
        due = math.huge
        for _, u in ipairs({ T1, T2 }) do book(u, { row("Target_CureWounds", "Osiris", "None") }) end
    end
    local j = start({ spell_abilities = { Target_CureWounds = "Wisdom" } })
    runJob(j, 400)
    for i = #tickers, n + 1, -1 do table.remove(tickers, i) end
    eq(j.status, "done", tostring(j.error))
    eq(world[T1].comps.SpellBook.Spells[1].SpellCastingAbility, "Wisdom", "written once it landed")
    twins()
    book(T1, {})
    book(T2, {})
    j = start({ spell_abilities = { Target_CureWounds = "Wisdom" } })
    runJob(j, 400)
    eq(j.status, "failed", "fails closed")
    assert(tostring(j.error):find(T2 .. ": Target_CureWounds", 1, true), tostring(j.error))
end))

test("a row write that doesn't stick fails the job naming the spell; a wrong shape is refused at the call",
    guarded(function()
        twins()
        local stuck = setmetatable({ Id = { Prototype = "Target_CureWounds", OriginatorPrototype = "Target_CureWounds",
            SourceType = "Osiris" } }, { __index = function() return "None" end, __newindex = function() end })
        book(T1, { stuck })
        book(T2, { row("Target_CureWounds", "Osiris", "None") })
        local j = start({ spell_abilities = { Target_CureWounds = "Wisdom" } })
        runJob(j)
        eq(j.status, "failed", "a write that doesn't stick")
        assert(tostring(j.error):find("Target_CureWounds reads None after the write of Wisdom", 1, true),
            tostring(j.error))
        refused(function() start({ spell_abilities = "Wisdom" }) end, "spell_abilities must map")
    end))

-- reference_diff (S4's rows, as read)
local function profile(over)
    local p = {}
    for _, f in ipairs(Ar.Symmetry.FIELDS) do p[f] = {} end
    for k, v in pairs(over) do p[k] = v end
    return p
end

local function diff(t, r) return Ar.Symmetry.reference_diff(profile(t), { profile = profile(r) }) end

test("reference: one boost read two ways is one boost, in either direction", function()
    local d = diff({ boosts = { "ActionResource|ActionResource|Movement,9,0|", "IncreaseMaxHP|IncreaseMaxHP|Level|" } },
        { boosts = { "ActionResource|||", "IncreaseMaxHP|||" } })
    eq(d.ok, true, Ar.Symmetry.diff_text(d))
end)

test("reference: weapon_action_dc passes by its exact row only (D148); any other value on either side fails", function()
    local accepted = { { field = "derived", only_twin = { "weapon_action_dc=8" },
        only_reference = { "weapon_action_dc=3" }, why = "D148" } }
    local function read(t, r)
        return Ar.Symmetry.reference_diff(profile({ derived = { weapon_action_dc = t } }),
            { profile = profile({ derived = { weapon_action_dc = r } }), accepted = accepted })
    end
    eq(read(8, 3).ok, true, "the accepted row")
    eq(#read(8, 3).accepted, 1, "listed as accepted")
    eq(read(9, 3).ok, false, "another twin value")
    eq(read(8, 4).ok, false, "another reference value")
end)

test("reference: a boost held twice or lacking still fails, by its type; nothing is accepted for it", function()
    local d = diff({ boosts = { "AC|||", "AC|||" } }, { boosts = { "AC|||" } })
    eq(d.ok, false, "one more AC boost, S4's Defense twice")
    eq(enc(d.diffs[1].only_twin), enc({ "AC" }), "named by type")
    eq(#d.accepted, 0, "not on a list")
    eq(diff({ boosts = {} }, { boosts = { "Tag|||" } }).ok, false, "a boost the twin lacks")
end)

test("reference: Ability boost rows are left out on both sides; `abilities` still compares the scores", function()
    local d = diff({ boosts = { "Ability|Ability|Strength,2|" } }, -- S4: the twin's +2 against the respec's six bases
        { boosts = { "Ability|Ability|Strength,15|", "Ability|Ability|Dexterity,13|", "Ability|||" } })
    eq(d.ok, true, Ar.Symmetry.diff_text(d))
    eq(diff({ abilities = { 15 } }, { abilities = { 17 } }).ok, false, "another score fails")
end)

test("reference: INSURFACE is left out on both sides; any other status still fails", function()
    eq(diff({ statuses = {} }, { statuses = { "INSURFACE" } }).ok, true, "the reference stood in a surface")
    eq(diff({ statuses = { "INSURFACE" } }, { statuses = {} }).ok, true, "the twin did")
    eq(diff({ statuses = { "BURNING" } }, { statuses = {} }).ok, false, "a status of another kind")
end)
