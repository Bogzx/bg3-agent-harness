-- Part of test_act_lua.lua (split for the 1000-line cap): think-while-acting support.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- ---- think-while-acting support ---------------------------------------------

test("reply: running job carries an ETA, a refusal its outcome", function()
    local r = op("move_to", { char = P1, pos = { 8, 0, 0 } })
    near(r.eta_s, 2.4, "eta"); near(r.route_m, 8, "route"); eq(r.status, nil, "status")
    nav.fn = function() return { reachable = false } end
    r = op("move_to", { char = P1, pos = { 8, 0, 0 } })
    eq(r.status, "failed", "status"); assert(r.error:find("no walkable"), r.error)
end)

test("talk: NPC walks off mid-approach -> re-planned walk with a new move id", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    local first = j.walk.move_id
    world[P1].pos = { 3, 0, 0 }; tick()
    world[NPC].pos = { 10, 0, 6 }; tick()
    eq(j.status, "running", "status " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 2, "walks"); assert(mv[2][8] ~= first, "same move id")
    -- a late cancel of the first walk must not fail the new one
    A.dispatch("CharacterMoveToCancelled", { P1, first })
    eq(j.status, "running", "status after stale cancel")
    arrive(j, { mv[2][3], 0, mv[2][5] })
    eq(j.phase, "requested", "phase")
end)

test("talk: arrival out of reach -> re-plan, gone target -> clear reason", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    for x = 2, 7.8, 1.5 do world[P1].pos = { x, 0, 0 }; tick() end
    world[P1].pos = { 7.8, 0, 0 }; tick()
    world[NPC].pos = { 11.5, 0, 0 }  -- moved 1.5 m: below the mid-walk threshold
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    eq(j.status, "running", "re-planned")
    eq(#called("CharacterMoveToPosition"), 2, "walks")
    osiv[key("CanSee", P1, NPC)] = nil
    world[NPC].pos = { 20, 0, 0 }; tick()
    eq(j.status, "failed", "status"); assert(j.error:find("no longer visible"), j.error)
end)

test("jump: capped at half the Strength score (UNVERIFIED formula), not TargetRadius", function()
    -- live stats: TargetRadius 4.5, below a Str 16 jump
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 8.4, 0, 0 } }))
    eq(j.status, "running", "8.4 m ok " .. tostring(j.error))
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 9, 0, 0 } }))
    eq(j.status, "failed", "status"); assert(j.error:find("too far to jump"), j.error)
    osiv[key("HasActiveStatus", P1, "ENHANCE_LEAP")] = 1
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 20, 0, 0 } }))
    eq(j.status, "running", "enhance leap " .. tostring(j.error))
end)

test("jump: Astral Gravity triples it, by name or by its JumpMaxDistanceMultiplier boost (S087 11.4 m refused at 9.5)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 19
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 11.4, 0, 0 } }))
        eq(j.status, "failed", "11.4 m at 9.5"); assert(j.error:find("can jump 9.5 m"), j.error)
        on = { { id = "UNKNOWN_ASTRAL_ID", name = "Astral Gravity" } }
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 11.4, 0, 0 } }))
        eq(j.status, "running", "astral by name " .. tostring(j.error))
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 29.5, 0, 0 } }))
        eq(j.status, "failed", "past 28.5 m"); assert(j.error:find("can jump 28.5 m"), j.error)
        stats.SOME_GRAVITY = { Boosts = "JumpMaxDistanceMultiplier(3);IgnoreFallDamage()" }
        on = { { id = "SOME_GRAVITY", name = "Something" } }
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 20, 0, 0 } }))
        eq(j.status, "running", "by boost " .. tostring(j.error))
        local m, nf = BG3AI.Guard.jump_status(P1)
        eq(m, 3, "mult"); eq(nf, true, "no fall damage")
    end)
    BG3AI.Visibility = savedV
    if not ok then error(err, 0) end
end)

test("jump: a drop extends the reach (high ground, S040 the game's own jumps 18-28 m out over 18-23 m drops)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 17
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -18.0 } end
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 18, -18, 0 } }))
        eq(j.status, "running", "18 m out, 18 m down " .. tostring(j.error))
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 18, 0, 0 } }))
        eq(j.status, "failed", "18 m flat"); assert(j.error:find("too far to jump"), j.error)
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 30, -18, 0 } }))
        eq(j.status, "failed", "30 m out"); assert(j.error:find("18.0 m down"), j.error)
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: a landing far above the jumper is refused (S039 21:53 jump_to 20 m up froze the game)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 17
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 10.0, 2.0 } end
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3, 10, 0 }, force = true }))
        eq(j.status, "failed", "10 m up"); assert(j.error:find("above you"), j.error)
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3, 2, 0 } }))
        eq(j.status, "running", "2 m up ok " .. tostring(j.error))
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: a jump that went nowhere fails, a short landing says how far off (S039 jump_to done x3, moved 0.6 m)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -8.0 } end -- a floor at the landing's height (Guard.jump_floor_ok)
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, -8, 0 } }))
    Ext.Level.GetHeightsAt = heights
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 0.5, 0, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(40)
    eq(j.status, "failed", "went nowhere"); assert(j.error:find("went nowhere: moved 0.5m"), j.error)
    -- landed 4 m short: done, with the distance
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 8, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 4, 0, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(40)
    eq(j.status, "done", "short"); eq(j.result.landed_m_from_target, 4, "off")
    -- still in the air at the settle: waits for the landing, then a clean done
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    for i = 1, 30 do world[P1].pos = { i * 0.2, 0, 0 }; tick() end -- 1.8 s, flying
    eq(j.status, "running", "airborne")
    tick(5)
    eq(j.status, "done", "landed"); eq(j.result.landed_m_from_target, nil, "on target (6 m)")
    world[P1].pos = { 0, 0, 0 }
end)

test("known_spell: a missing Ranged Attack names the missing ranged weapon (S031 17:04 Gale)", function()
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_MainHandAttack" } } } }
    refused(function() G.known_spell(P1, "Projectile_MainHandAttack") end, "no ranged weapon equipped")
    refused(function() G.known_spell(P1, "Target_Fireball_X") end, "does not have that action")
end)

-- bg3se's SpellMetaId: OriginatorPrototype, SourceType, Source, ProgressionSource; any other name raises
-- "Property does not exist" (LuaCppObjectProxy.inl LightObjectProxyMetatable::Index).
function meta_id(orig)
    local fields = { OriginatorPrototype = orig, SourceType = "Progression" }
    return setmetatable({}, { __index = function(_, k)
        if fields[k] ~= nil then return fields[k] end
        error("Property does not exist: SpellMetaId::" .. tostring(k) .. " - property does not exist")
    end })
end

test("known_spell: an unprepared class spell is refused (A4: S065 Claude's Bless off the hotbar)", function()
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared"),
        book("Target_MainHandAttack", "Unknown") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"), meta_id("Projectile_Jump") } }
    refused(function() G.known_spell(P1, "Target_Bless") end, "that spell is not prepared")
    eq(G.known_spell(P1, "Target_CureWounds").Id.Prototype, "Target_CureWounds", "prepared")
    eq(G.known_spell(P1, "Target_Sanctuary").Id.Prototype, "Target_Sanctuary", "always prepared, not listed")
    eq(G.known_spell(P1, "Target_MainHandAttack").Id.Prototype, "Target_MainHandAttack", "an action")
    -- an unreadable prepared list: nothing counts as prepared
    world[P1].comps.SpellBookPrepares = setmetatable({}, { __index = function() error("bad component") end })
    refused(function() G.known_spell(P1, "Target_CureWounds") end, "that spell is not prepared")
    eq(G.known_spell(P1, "Target_Sanctuary").Id.Prototype, "Target_Sanctuary", "always prepared still casts")
    world[P1].comps.SpellBookPrepares = nil
    world[P1].comps.SpellBook = nil
end)

test("cast: the engine's CastSpellFailed says why (S024-S041 'the action failed' x12)", function()
    osiv[key("HasLineOfSight", P1, GOB)] = 1
    eq(A.cast_failed_why(P1, nil), "the game refused the cast (no reason given)", "no target")
    assert(A.cast_failed_why(P1, GOB):find("path is blocked", 1, true))
    osiv[key("HasLineOfSight", P1, GOB)] = 0
    assert(A.cast_failed_why(P1, GOB):find("no line of sight to the target now", 1, true))
    osiv[key("IsDead", GOB)] = 1
    assert(A.cast_failed_why(P1, GOB):find("already dead", 1, true))
    stats.Teleportation_Revivify = { SpellType = "Teleportation", OriginTargetConditions = "Dead()", TargetRadius = "9" }
    local w = A.cast_failed_why(P1, GOB, "Teleportation_Revivify")
    assert(w:find("two-step cast", 1, true) and w:find("Scroll of Revivify", 1, true), w)
    assert(w:find("not a party member", 1, true), w) -- GOB has no PLAYABLE tag
    osiv[key("IsDead", GOB)] = 0
end)

test("cast: a refused melee swing says how far it swung from and our reach (ACT-3, S065 x9)", function()
    osiv[key("HasLineOfSight", P1, GOB)] = 1
    stats.Target_MainHandAttack = { SpellType = "Target", TargetRadius = "MeleeMainWeaponRange" }
    world[GOB].pos = { 2.1, 0, 0 }
    local w = A.cast_failed_why(P1, GOB, "Target_MainHandAttack")
    assert(w:find("no reason given", 1, true) and w:find("melee swing from 2.1 m centre to centre", 1, true), w)
    assert(w:find("our reach 2.5 m: range 1.5 m", 1, true) and not w:find("path is blocked", 1, true), w)
    stats.Projectile_Bolt = { SpellType = "Projectile", TargetRadius = "18" }
    assert(A.cast_failed_why(P1, GOB, "Projectile_Bolt"):find("path is blocked", 1, true)) -- ranged: unchanged
end)

test("cast: Revivify is two-step: the body, then the ground beside it (S057/S058 0 of 14 casts)", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    stats.Projectile_Bolt = stats.Projectile_Bolt or { SpellType = "Projectile" }
    eq(A.two_step_spell("Teleportation_Revivify"), true, "two-step")
    eq(A.two_step_spell("Projectile_Bolt"), false, "one-step")
    eq(A.two_step_spell("No_Such_Spell"), false, "unknown")
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Teleportation_Revivify", target = NPC }))
    Osi.UseSpell = orig
    eq(j.status, "running", "status " .. tostring(j.error))
    local p = tasks[1] and tasks[1].TargetPos2
    assert(p, "TargetPos2 set on the queued task")
    eq(p[1], 4 - A.PLACE_M, "spot beside the body, toward the caster"); eq(p[3], 0, "z")
    eq(A.last_place_why, nil, "no placement error")
    osiv[key("IsDead", NPC)] = 0
    A.dispatch("CastedSpell", { P1, "Teleportation_Revivify" })
    tick(10)
    eq(j.status, "done", "status"); eq(j.result.revived, true, "revived")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: two-step spell without a queued task names the reason", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = {} } }
    local ok, why = A.set_task_pos2(P1, "Teleportation_Revivify", { 1, 0, 1 })
    eq(ok, nil, "not set"); assert(why:find("no queued spell task", 1, true), why)
    world[P1].comps.ServerCharacter = nil
end)

test("cast (ACT-9): with the TargetPos2 write off (default), a two-step spell is refused before any order", function()
    eq(A.TASK_POS2, false, "off by default")
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { { Spell = { OriginatorPrototype = "Teleportation_Revivify" } } } } }
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC }, "Scroll of Revivify")
    eq(#called("UseSpell"), 0, "no UseSpell")
    local ok, why = A.set_task_pos2(P1, "Teleportation_Revivify", { 1, 0, 1 })
    eq(ok, nil, "no write"); assert(why:find("ACT-9", 1, true), why)
    eq(world[P1].comps.ServerCharacter.OsirisController.Tasks[1].TargetPos2, nil, "the task is untouched")
    spell("Teleportation_DimensionDoor", { SpellType = "Teleportation", TargetRadius = "2", AreaRadius = "50",
        OriginTargetConditions = "Ally()", TargetConditions = "CanStand('')", TeleportSelf = "No",
        UseCosts = "ActionPoint:1", Level = 0 })
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 3, 0, 3 } }, "two-step teleport")
    eq(#called("UseSpell"), 0, "no UseSpell"); eq(#called("UseSpellAtPosition"), 0, "no UseSpellAtPosition")
    world[P1].comps.ServerCharacter = nil
end)

local DD = { SpellType = "Teleportation", TargetRadius = "2", AreaRadius = "50",
    TargetConditions = "CanStand('') and not Character()",
    OriginTargetConditions = "Ally() and not Grounded() and (Tagged('WEIGHT_MEDIUM') or Tagged('WEIGHT_SMALL'))",
    TeleportSelf = "No", UseCosts = "ActionPoint:1", Level = 0 }

test("cast: Dimension Door alone: the caster goes, the point is the landing (S059 08:59:47 'range 2.0 m')", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    eq(A.revive_spell("Teleportation_DimensionDoor"), false, "not a revive")
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 13.0 } end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 13.1, 5 } }))
    Osi.UseSpell, Ext.Level.GetHeightsAt = orig, heights
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("UseSpellAtPosition"), 0, "not cast at a bare position")
    local c = called("UseSpell")
    eq(c[1][2], P1, "caster"); eq(c[1][4], P1, "first pick: the caster")
    local p = tasks[1] and tasks[1].TargetPos2
    assert(p, "TargetPos2 = the landing")
    eq(p[1], 0); eq(p[2], 13.1); eq(p[3], 5)
    A.dispatch("CastedSpell", { P1, "Teleportation_DimensionDoor" })
    world[P1].pos = { 0, 13.0, 5 }
    tick(10)
    eq(j.status, "done", "status"); eq(j.result.teleported, true, "teleported")
    eq(j.result.revived, nil, "no revive flag"); near(j.result.landed_m_from_target, 0.1, "landed")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: Dimension Door with an ally: the ally is the first pick, the landing the second (S059 08:57:42)", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    world[GOB].pos = { 1.5, 0, 0 }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB }, pos = { 8, 0, 8 } }))
    Osi.UseSpell = orig
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][4], GOB, "first pick: the ally")
    local p = tasks[1].TargetPos2
    eq(p[1], 8, "landing x, not the Revivify spot beside the ally"); eq(p[3], 8, "landing z")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: Dimension Door landing checks: range from AreaRadius, a landing in the air names the floor", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 55 } },
        "the landing is out of range (55.0 m, range 50.0 m)")
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 13.0 } end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 7, 5 } }, "nearest floor there is at y=13.0")
    -- audit C19: only a floor the party sees or has walked is named
    nav.unseen = function(p) return p[2] > 10 end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 7, 5 } }, "nearest floor there is at y=0.0")
    nav.unseen = function(p) return p[2] > -1 and p[3] > 4 end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 7, 5 } }, "in view")
    nav.unseen = function(p) return p[2] ~= 7 end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 7, 5 } }, "no floor within 2 m of that height; give")
    nav.unseen = nil
    Ext.Level.GetHeightsAt = heights
    fails({ char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB, NPC }, pos = { 0, 0, 5 } }, "one who goes")
end)

-- S093 09:47 (the Netherbrain's psyche): Gale on her platform (230,-52,4245) cast Misty Step at (230.5,-55.7,4260.5),
-- 1.5 m off its edge; no landing check, she fell to y -73.1 (the void's bottom, a Chasm floor) and died, and the
-- result read "FAILED — the game refused the cast (no reason given)".
local MISTY = { SpellType = "Target", TargetRadius = "18", UseCosts = "BonusActionPoint:1", Level = 2,
    TargetConditions = "CanStand('') and not Character() and not Self()",
    SpellProperties = { { TextKey = "Ground", Functors = { { TypeId = "TeleportSource" } } } } }
local GALE_AT, VOID, EDGE, PLATFORM = { 230, -52, 4245 }, { 230.5, -55.7, 4260.5 }, { 231.3, -56, 4261.5 },
    { 232, -56, 4262 }
-- the psyche's columns: the platform's floor at -56 (its edge at x = 231), the void's bottom (Chasm) at -73.1
local function psyche(fn)
    local heights, savedNav = Ext.Level.GetHeightsAt, BG3AI.Nav
    Ext.Level.GetHeightsAt = function(x) return x >= 231 and { -56.0, -73.1 } or { -73.1 } end
    BG3AI.Nav = setmetatable({ surfaceNear = function(p, m)
        return (p[2] < -70 or p[1] - (m or 0) < 231) and "Chasm" or nil
    end }, { __index = savedNav })
    local ok, err = pcall(fn)
    Ext.Level.GetHeightsAt, BG3AI.Nav = heights, savedNav
    if not ok then error(err, 0) end
end

test("teleport: point_teleport reads Misty Step's TeleportSource (not DD, Revivify or a plain point spell)", function()
    spell("Target_MistyStep", MISTY)
    spell("Target_MistyStep_Free", { SpellType = "Target", TargetRadius = "18" }) -- no readable functors: the id
    spell("Teleportation_DimensionDoor", DD)
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target" })
    eq(A.point_teleport("Target_MistyStep"), true, "functor"); eq(A.point_teleport("Target_MistyStep_Free"), true, "id")
    eq(A.point_teleport("Teleportation_DimensionDoor"), false, "two-step"); eq(A.point_teleport("Target_Thing"), false, "plain")
end)

test("teleport: Misty Step into the psyche's void / at its edge is refused before any cost (S093 09:47 Gale)", function()
    spell("Target_MistyStep", MISTY)
    world[P1].pos = GALE_AT
    psyche(function()
        local function fails(pos, pat)
            local ok, r = pcall(op, "cast", { char = P1, spell = "Target_MistyStep", pos = pos })
            local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
            assert(msg:find(pat, 1, true), msg)
        end
        fails(VOID, "the only floor there is the void's bottom at y=-73.1, a Chasm")
        fails(EDGE, "is in a Chasm or within 0.5 m of one")
        eq(#called("UseSpellAtPosition"), 0, "nothing cast")
        local j = job(op("cast", { char = P1, spell = "Target_MistyStep", pos = PLATFORM }))
        eq(j.status, "running", "on the platform " .. tostring(j.error))
    end)
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return {} end
    local j = job(op("cast", { char = P1, spell = "Target_MistyStep", pos = VOID }))
    Ext.Level.GetHeightsAt = heights
    eq(j.status, "failed", "no floor at all")
    assert(j.error:find("no ground at all under the landing", 1, true), j.error)
    world[P1].pos = { 0, 0, 0 }
end)

test("teleport: the result says where the caster landed; a fall to death reads FELL ... DIED, not 'refused'", function()
    spell("Target_MistyStep", MISTY)
    world[P1].pos = GALE_AT
    local j
    psyche(function() j = job(op("cast", { char = P1, spell = "Target_MistyStep", pos = PLATFORM })) end)
    A.dispatch("CastSpell", { P1, "Target_MistyStep", "Target", "None", 0 })
    world[P1].pos = { 232, -56, 4262.2 }
    A.dispatch("CastedSpell", { P1, "Target_MistyStep" })
    tick(10)
    eq(j.status, "done", "landed " .. tostring(j.error)); eq(j.result.teleported, true, "teleported")
    eq(j.result.now_at[2], -56, "now at"); near(j.result.landed_m_from_target, 0.2, "on the spot")
    -- S093's outcome (the check bypassed: a column the heights read as floor): CastSpellFailed after the fall
    world[P1].pos = GALE_AT
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { -55.7 } end
    j = job(op("cast", { char = P1, spell = "Target_MistyStep", pos = VOID }))
    Ext.Level.GetHeightsAt = heights
    eq(j.status, "running", "cast " .. tostring(j.error))
    world[P1].pos = { 230.5, -73.1, 4260.5 }
    osiv[key("IsDead", P1)] = 1
    A.dispatch("CastSpellFailed", { P1, "Target_MistyStep" })
    tick(20)
    osiv[key("IsDead", P1)] = nil
    world[P1].pos = { 0, 0, 0 }
    eq(j.status, "failed", "status")
    assert(j.error:find("teleported, then FELL 17.4m below the landing (to y=-73.1) and DIED", 1, true), j.error)
    assert(not j.error:find("refused", 1, true), j.error)
end)

test("cast ui=true (ACT-9 player route): Dimension Door alone/with an ally, no order and no task write", function()
    eq(A.TASK_POS2, false, "the write stays off")
    spell("Teleportation_DimensionDoor", DD)
    local tasks = { { Spell = { OriginatorPrototype = "Teleportation_DimensionDoor" } } }
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0 } end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 10 }, ui = true }))
    eq(j.status, "running", "alone: " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell: the bridge clicks"); eq(#called("UseSpellAtPosition"), 0, "no position cast")
    eq(tasks[1].TargetPos2, nil, "nothing written")
    A.dispatch("CastSpell", { P1, "Teleportation_DimensionDoor", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Teleportation_DimensionDoor" })
    world[P1].pos = { 0, 0, 9.8 }
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); eq(j.result.via, "hotbar", "via")
    eq(j.result.teleported, true, "teleported"); near(j.result.landed_m_from_target, 0.2, "landed")
    eq(j.result.landing[3], 10, "landing reported")
    world[P1].pos = { 0, 0, 0 }
    world[GOB].pos = { 1.5, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB }, pos = { 0, 0, 10 }, ui = true }))
    eq(j.status, "running", "with an ally: " .. tostring(j.error))
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 55 }, ui = true }, "the landing is out of range")
    fails({ char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB, NPC }, pos = { 0, 0, 5 }, ui = true }, "one who goes")
    Ext.Level.GetHeightsAt = heights
    world[P1].comps.ServerCharacter = nil
end)

test("cast ui=true (ACT-9 player route): Revivify clicks one body; the result says revived; no write", function()
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    local tasks = { { Spell = { OriginatorPrototype = "Teleportation_Revivify" } } }
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC, pos = { 3, 0, 0 }, ui = true }, "not a position")
    fails({ char = P1, spell = "Teleportation_Revivify", targets = { NPC, GOB }, ui = true }, "one body")
    local j = job(op("cast", { char = P1, spell = "Teleportation_Revivify", target = NPC, ui = true }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell"); eq(tasks[1].TargetPos2, nil, "nothing written")
    osiv[key("IsDead", NPC)] = 0
    A.dispatch("CastedSpell", { P1, "Teleportation_Revivify" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); eq(j.result.revived, true, "revived")
    eq(j.result.via, "hotbar", "via"); eq(j.result.teleported, nil, "not a teleport")
    -- without ui, still ACT-9's refusal
    osiv[key("IsDead", NPC)] = 1
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC }, "Scroll of Revivify")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: a refused Dimension Door says the landing must be in sight, not 'the shot's path is blocked'", function()
    stats.Teleportation_DimensionDoor = DD
    A.last_place_why = nil
    local w = A.cast_failed_why(P1, P1, "Teleportation_DimensionDoor")
    assert(w:find("two-step teleport", 1, true) and w:find("SEE", 1, true), w)
    assert(not w:find("path is blocked", 1, true), w)
    assert(not A.cast_failed_why(P1, GOB, "Teleportation_DimensionDoor"):find("Revivify", 1, true))
end)

test("set_party: unchain/chain already in that state is done, not FAILED (S062 10:00:20 x3)", function()
    local j = job(op("unchain", { char = P1 }))
    tick(1)
    eq(j.status, "done", "unchain status " .. tostring(j.error)); eq(j.result.already, true, "already")
    eq(#called("DetachFromPartyGroup"), 0, "no order sent")
    osiv[key("InSamePartyGroup", P1, GOB)] = 1
    party_rows = { P1, GOB }
    local c = job(op("chain", { char = P1, leader = GOB }))
    tick(1)
    eq(c.status, "done", "chain status " .. tostring(c.error)); eq(c.result.already, true, "already")
    eq(#called("AttachToPartyGroup"), 0, "no order sent")
end)

test("jump: a landing far below the aim is a FALL; a death says DIED, not done (S038 20:44:25)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -1.0 } end
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 6, -47, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    osiv[key("IsDead", P1)] = 1
    tick(60)
    eq(j.status, "failed", "dead"); assert(j.error:find("FELL 47.0m below the landing", 1, true), j.error)
    assert(j.error:find("DIED", 1, true), j.error)
    -- alive after a fall: done, with fell_m
    osiv[key("IsDead", P1)] = 0
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 6, -6, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(60)
    Ext.Level.GetHeightsAt = heights
    eq(j.status, "done", "alive " .. tostring(j.error)); eq(j.result.fell_m, 6, "fell_m")
    world[P1].pos = { 0, 0, 0 }
end)

test("jump: never at the void or into the air, even forced (S038 20:48 / S039 21:53: two OOM freezes)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    local ok, err = pcall(function()
        Ext.Level.GetHeightsAt = function() return {} end
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 20, 0 }, force = true }))
        eq(j.status, "failed", "void"); assert(j.error:find("no ground at all"), j.error)
        Ext.Level.GetHeightsAt = function() return { -155.0 } end
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, -12, 0 } }))
        eq(j.status, "failed", "air over a chasm"); assert(j.error:find("is in the air"), j.error)
        Ext.Level.GetHeightsAt = function() return { 0.0 } end
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 0, 0 } }))
        eq(j.status, "running", "a floor: goes " .. tostring(j.error))
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: the cast runs under the nav in-flight marker, cleared at the end (a crash inside kills the level)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local marks = {}
    BG3AI.Nav.inflight = function(o) marks[#marks + 1] = o or "clear" end
    BG3AI.Nav.inflightDone = function(o) marks[#marks + 1] = "clear " .. tostring(o) end -- NAV-7: only its own
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 0, 0 } }))
        eq(marks[1], "jump", "marked at the order")
        A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
        world[P1].pos = { 4, 0, 0 }
        A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
        tick(60)
        eq(j.status, "done", "landed " .. tostring(j.error))
        eq(marks[#marks], "clear jump", "cleared at the end")
    end)
    BG3AI.Nav.inflight, BG3AI.Nav.inflightDone = nil, nil
    world[P1].pos = { 0, 0, 0 }
    if not ok then error(err, 0) end
end)

test("target not visible: names where the party last saw it (S026 09:02)", function()
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        characterVisible = function() return false end,
        itemVisible = function() return false end,
        lastSeen = function(u) if u == NPC then return { pos = { -67.2, 28.2, 141.7 }, ago_s = 669 } end end,
    }
    local ok, err = pcall(function()
        refused(function() G.target_visible(NPC) end, "target is not visible; last seen @(-67.2,28.2,141.7) 669s ago: travel_to")
        BG3AI.Visibility.lastSeen = function() return nil end
        refused(function() G.target_visible(NPC) end, "target is not visible")
        eq(G.last_seen_hint(NPC), "", "never seen: no hint")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

test("target not visible: one that turned invisible says so (S030 Soft-Step Shadow 1.8 m away)", function()
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        characterVisible = function() return false end,
        itemVisible = function() return false end,
        lastSeen = function() return { pos = { -806.7, -6.5, -746.3 }, ago_s = 2 } end,
        vanished = function(u) if u == NPC then return true, { pos = { -806.7, -6.5, -746.3 }, ago_s = 2 } end return false end,
    }
    local ok, err = pcall(function()
        refused(function() G.target_visible(NPC) end, "target is not visible; it turned invisible 2s ago")
        assert(not G.last_seen_hint(NPC):find("travel_to"), "no walk advice for an invisible one")
        BG3AI.Visibility.vanished = function() return false end
        assert(G.last_seen_hint(NPC):find("travel_to that spot first"), "a plain out-of-sight one keeps the old hint")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

test("act.watch: party bars, mode, visible characters with hostility", function()
    world[P1].comps.Health = { Hp = 7, MaxHp = 12 }
    osiv[key("IsEnemy", P1, GOB)] = 1
    osiv[key("IsInCombat", P1)] = 1
    world.FAR = { pos = { 500, 0, 0 } }
    local scans = 0
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        -- the shared per-frame scan; act.watch no longer walks ServerCharacter itself
        charactersNear = function(centers, radius)
            scans = scans + 1
            local out = {}
            for _, u in ipairs({ NPC, GOB, P1, "FAR" }) do
                local p = world[u].pos
                for _, c in ipairs(centers) do
                    local d = math.sqrt((p[1] - c[1]) ^ 2 + (p[2] - c[2]) ^ 2 + (p[3] - c[3]) ^ 2)
                    if d <= radius then out[#out + 1] = { { u = u }, p, d }; break end
                end
            end
            return out
        end,
        entityUuid = function(e) return e.u end,
        name = function(u) return u == GOB and "Goblin" or "Npc" end,
        characterVisible = function(_, u) return u ~= NPC end,
    }
    local w = RPC.ops["act.watch"]({})
    BG3AI.Visibility = nil
    eq(w.combat, true, "combat"); eq(w.party[1].hp, 7, "hp")
    eq(#w.seen, 1, "seen"); eq(w.seen[1].uuid, GOB, "goblin"); eq(w.seen[1].hostile, true, "hostile")
    eq(scans, 1, "one shared radius query")
    world.FAR = nil
end)

test("act.watch: Chasm underfoot is no surface interrupt (S040: the queue cancelled two jumps over a gap)", function()
    local savedNav = BG3AI.Nav
    local under = "Chasm"
    BG3AI.Nav = setmetatable({ surfaceAt = function() return under end }, { __index = savedNav })
    local ok, err = pcall(function()
        local w = RPC.ops["act.watch"]({})
        eq(w.party[1].surface, nil, "Chasm not reported")
        under = "Fire"
        w = RPC.ops["act.watch"]({})
        eq(w.party[1].surface, "Fire", "fire still reported")
    end)
    BG3AI.Nav = savedNav
    if not ok then error(err, 0) end
end)

test("wedged hint: a landing only away from the goal says don't jump (naut2 run 3: back down the ladder just climbed)", function()
    local savedNav = BG3AI.Nav
    local got
    BG3AI.Nav = setmetatable({ wedged = function(_, _, goal)
        got = goal
        if goal then return { wedged = true, away = "Lae'zel", away_dy = -2.9 } end
        return { wedged = true, to = { -42.3, 4.5, -363.3 }, near = "Lae'zel", dist_m = 0.6 }
    end }, { __index = savedNav })
    local ok, err = pcall(function()
        local h = G.wedged_hint(P1, { -41, 7.7, -357 })
        eq(got[2], 7.7, "the goal reaches N.wedged")
        assert(h:find("next to Lae'zel%) is 2.9m below, away from the goal: don't jump there"), h)
        assert(not h:find("jump_to"), h)
        assert(G.wedged_hint(P1):find("jump_to %(%-42.3,4.5,%-363.3%) next to Lae'zel"), "no goal: as before")
    end)
    BG3AI.Nav = savedNav
    if not ok then error(err, 0) end
end)

test("act.watch: a climber on a ladder reads climbing (the queue's next step waits for it, naut2 helm)", function()
    eq(RPC.ops["act.watch"]({}).party[1].climbing, nil, "standing")
    osiv[key("HasActiveStatus", P1, "CLIMBING")] = 1
    eq(RPC.ops["act.watch"]({}).party[1].climbing, true, "climbing")
    osiv[key("HasActiveStatus", P1, "CLIMBING")] = nil
end)

test("act.note: only queue_* kinds reach the event stream", function()
    RPC.ops["act.note"]({ kind = "queue_done", data = { n = 2 } })
    eq(RPC.events[#RPC.events].kind, "queue_done", "kind")
    assert(not pcall(RPC.ops["act.note"], { kind = "combat_started" }), "spoofed event accepted")
end)

test("act.target_state: carried item -> held", function()
    osiv[key("GetInventoryOwner", GEM)] = P1
    local r = RPC.ops["act.target_state"]({ uuid = GEM })
    eq(r.held, true, "held")
end)

test("act.target_state: a combatant out of sight says only that it left the turn order (S033, audit C9)", function()
    osiv[key("CanSee", P1, NPC)] = nil
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", NPC)] = 1
    local bar = { uuid = NPC, on_bar = true }
    local r = RPC.ops["act.target_state"](bar)
    eq(r.visible, false, "hidden")
    eq(r.gone, nil, "only out of sight: still in the fight")
    osiv[key("IsInCombat", NPC)] = 0
    eq(RPC.ops["act.target_state"](bar).gone, "left_fight", "left the turn order")
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, nil, "never on the bar the party saw: nothing")
    osiv[key("IsOnStage", NPC)] = 0
    eq(RPC.ops["act.target_state"](bar).gone, "left_fight", "despawned: the same, no why")
    osiv[key("IsDead", NPC)] = 1
    osiv[key("IsInCombat", NPC)] = 1
    r = RPC.ops["act.target_state"](bar)
    eq(r.gone, "left_fight", "died out of sight: the same, no why")
    eq(r.dead, nil, "no dead for an unseen character")
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, nil, "died unseen, never on the bar: nothing")
    osiv[key("IsInCombat", NPC)] = 0
    osiv[key("IsDead", NPC)] = 0
    osiv[key("IsOnStage", NPC)] = 1
    osiv[key("IsInCombat", P1)] = 0
    eq(RPC.ops["act.target_state"](bar).gone, nil, "no fight: nothing to leave")
    osiv[key("CanSee", P1, NPC)] = 1
    r = RPC.ops["act.target_state"](bar)
    eq(r.visible, true, "in sight")
    eq(r.gone, nil, "in sight: never gone")
    osiv[key("CanSee", P1, DOOR)] = nil
    eq(RPC.ops["act.target_state"]({ uuid = DOOR, on_bar = true }).gone, nil, "items: no gone")
end)
