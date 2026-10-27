-- Offline tests for Server/Visibility.lua's character filter (V.characterVisible).
-- vis V22, S081 03:01: get_nearby radius 60 at camp listed Withers and Scratch, not Karlach (waiting at camp,
-- past the 18.5 m sight range), and talk_to Karlach was "not visible"; swap_party then walked up to her.
-- vis V22b: State.campRoster said "at camp" by GetRegion (the level id), so on a level shared with the camp (the
-- Act 3 camp and the Lower City, S081) a camp companion's "!" and camp spot showed from anywhere in the city.
-- vis V23, S083 03:44: state.entity sent no position for a visible NPC, so travel_to/talk_to's walk to Nocturne
-- 90 m off was one move_to (capped at 60 m) instead of legs.
-- vis V25, S084 04:16: who of the party stands on a floor plate (S.plateOn).
-- vis V24, S083 03:55:56: a lock read flipping locked -> unlocked counts as the game unlocking it (S.lockRead).
-- vis V29, S091 08:19-08:29: the Netherbrain (a huge body) hidden 20-22.5 m from its anchor point (V.bigPerceived).
-- vis V31, S093: in the psyche the floor calibration ray misses (ray rule off): a big foe in the party's fight.
-- vis V32, S094 High Hall: the item an active objective's marker sits on (the Brain Stem) is seen within sight range.
-- S064/S065 Temple of Bhaal: a spotted Sanguine Vent stays a disarm target (V.itemKnown) after the CanSee that
-- spotted it is gone, and get_nearby (S.nearby) lists a discovered standalone trap, kinds=trap or not.
-- Ext, Osi and RPC are stubbed; Shared/Util.lua and Server/Visibility.lua, State.lua and Events.lua are real.
-- Run: tools/dev/test_fairness_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local CLAUDE = "aaaaaaaa-0000-0000-0000-000000000004"
local KARLACH = "aaaaaaaa-0000-0000-0000-000000000005" -- recruited, waiting at camp
local WITHERS = "cccccccc-0000-0000-0000-000000000352"
local GOB = "cccccccc-0000-0000-0000-000000000009"
local BRAIN = "cccccccc-0000-0000-0000-000000000521"
local STEM = "dddddddd-0000-0000-0000-000000000466"
local VASE = "dddddddd-0000-0000-0000-000000000437"
local VENT = "dddddddd-0000-0000-0000-000000000302"

-- S081: the party arrived at (-1421.2, 8.4, -2601.2); Karlach stands at her camp spot ~35 m off.
local ents = {
    [CLAUDE] = { pos = { -1421.2, 8.4, -2601.2 } },
    [KARLACH] = { pos = { -1450.0, 8.0, -2620.0 }, CampPresence = {}, HasExclamationDialog = {} },
    [WITHERS] = { pos = { -1418.7, 8.3, -2585.5 } },
    [GOB] = { pos = { -1450.0, 8.0, -2620.0 } },
    -- S091 e521 The Unbreakable Will of the Netherbrain, look_at 08:30:00
    [BRAIN] = { pos = { 215.0, -61.0, 4259.0 } },
    -- S094 e466 Brain Stem (usable), 5.7 m SW of Claude by route(o1); e437 a vase beside it
    [STEM] = { pos = { -1425.2, 8.4, -2605.2 }, item = { CanUse = true } },
    [VASE] = { pos = { -1425.2, 8.4, -2605.2 }, item = { CanUse = true } },
    -- S064 e302 Sanguine Vent: a floor trap (not usable, not pickable: kind "object"), 10 m E of where the walk stopped
    [VENT] = { pos = { -1411.2, 8.4, -2601.2 }, item = {} },
}
local inCamp = {}      -- DB_PlayerInCamp rows
local sees = {}        -- uuid -> CanSee from Claude
local onStage = {}     -- uuid -> false when off stage
local los = true       -- HasLineOfSight from any member
local fight = {}      -- uuid -> CombatGetGuidFor
local canInteract = {} -- uuid -> false: scenery
local objectives = {}  -- Nav.objectiveTargets: item uuid -> true
local traps = {}       -- uuid -> "hidden" | "discovered"
local enemies = {}     -- uuid -> true: IsEnemy

Ext = {
    Utils = { MonotonicTime = function() return 1000 end, PrintWarning = function() end, Print = function() end },
    Osiris = { RegisterListener = function() end },
    Loca = { GetTranslatedString = function(h) return h end },
    Events = { Tick = { Subscribe = function() end } },
    Entity = { Get = function(u)
        local d = ents[u]
        if not d then return nil end
        local e = { Uuid = { EntityUuid = u }, Transform = { Transform = { Translate = d.pos } } }
        e.CampPresence, e.HasExclamationDialog, e.Trader = d.CampPresence, d.HasExclamationDialog, d.Trader
        if d.item then e.ServerItem = d.item return e end
        e.ServerCharacter = {}
        if d.size then e.ObjectSize = { Size = d.size } end
        if d.bounds then e.Bound = { Bound = { AIBounds = { Hit = d.bounds } } } end
        return e
    end },
}
Osi = {
    IsOnStage = function(u) return onStage[u] == false and 0 or 1 end,
    IsInvisibleByScript = function() return 0 end,
    IsInvisible = function() return 0 end,
    HasActiveStatus = function() return 0 end,
    IsAlly = function(_, u) return u == KARLACH and 1 or 0 end,
    CanSee = function(_, u) return sees[u] and 1 or 0 end,
    HasLineOfSight = function() return los and 1 or 0 end,
    CombatGetGuidFor = function(u) return fight[u] end,
    GetCanInteract = function(u) return canInteract[u] == false and 0 or 1 end,
    IsTrap = function(u) return traps[u] and 1 or 0 end,
    IsTrapDiscovered = function(u) return traps[u] == "discovered" and 1 or 0 end,
    DB_PlayerInCamp = { Get = function() if inCamp == "error" then error("no such DB") end return inCamp end },
    DB_Origins = { Get = function() return { { "S_Player_Karlach_" .. KARLACH }, { "S_Player_Claude_" .. CLAUDE } } end },
    GetRegion = function() return "CTY_Main_A" end, -- the camp and the city: one level
    GetDisplayName = function(u) return u == VENT and "Sanguine Vent" or nil end,
    IsDead = function() return 0 end,
    CanTrade = function() return 1 end, -- diverse-2026-10-25: true for the windmill goblins too
    IsEnemy = function(_, u) return enemies[u] and 1 or 0 end,
}

local handlers = {}    -- RPC name -> handler
local pushed = {}      -- RPC.push events
BG3AI = { RPC = { register = function(name, fn) handlers[name] = fn end,
        push = function(kind, data) pushed[#pushed + 1] = { kind = kind, data = data } end, QUIET_OPS = {} },
    Nav = { objectiveTargets = function() return objectives end } }
dofile(LUA .. "/Shared/Util.lua")
Ext.Require = function(p) return dofile(LUA .. "/" .. p) end -- the modules load their parts
dofile(LUA .. "/Server/Visibility.lua")
dofile(LUA .. "/Server/State.lua")
local V, S = BG3AI.Visibility, BG3AI.State
local poll -- Events.lua's tick handler
Ext.Events.Tick.Subscribe = function(_, fn) poll = fn end
dofile(LUA .. "/Server/Events.lua")
Ext.Events.Tick.Subscribe = function() end
local E = BG3AI.Events
dofile(LUA .. "/Server/Referee.lua") -- nearby can_trade asks its R.isTrader

local function ctx()
    return { party = { CLAUDE }, is_party = { [CLAUDE] = true },
        observers = { { uuid = CLAUDE, pos = ents[CLAUDE].pos, sight = 18.5 } } }
end
local function visible(u) return V.characterVisible(ctx(), u, V.entity(u)) end

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    inCamp, sees, onStage, los, fight, canInteract, objectives, traps, enemies = {}, {}, {}, true, {}, {}, {}, {}, {}
    Ext.Level, V.rayBroken, V.rayCache, V.calib = nil, nil, {}, { t = -1e9, ok = false } -- no ray API: uncalibrated
    ents[BRAIN].size, ents[BRAIN].bounds = nil, nil
    V.boundsCache = {}
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

test("S081: a camp companion past sight range is visible while the party is at camp", function()
    inCamp = { { "S_Player_Claude_" .. CLAUDE } } -- the DB stores prefixed names
    eq(visible(KARLACH), true, "Karlach at camp")
    eq(visible(WITHERS), true, "Withers within sight")
end)

test("the same spot without CampPresence keeps the sight rule", function()
    inCamp = { { CLAUDE } }
    eq(visible(GOB), false, "a stranger 35 m off, not seen by the engine")
    sees[GOB] = true
    eq(visible(GOB), true, "the engine sees it")
end)

test("a camp companion is not visible from outside the camp", function()
    eq(visible(KARLACH), false, "party not at camp")
    inCamp = { { "S_GLO_Someone_" .. GOB } } -- a non-party row doesn't count
    eq(visible(KARLACH), false, "only a party member's row")
end)

test("an off-stage camp companion stays hidden", function()
    inCamp = { { CLAUDE } }
    onStage[KARLACH] = false
    eq(visible(KARLACH), false, "off stage")
end)

local function karlachRow()
    for _, o in ipairs(S.campRoster(ctx(), true)) do if o.uuid == KARLACH then return o end end
    error("Karlach not in the camp roster", 2)
end

test("campRoster on the camp's level but away from camp: no camp spot, no \"!\"", function()
    local o = karlachRow()
    eq(o.pos, nil, "pos")
    eq(o.wants_talk, nil, "wants_talk")
end)

test("campRoster at camp: the camp spot and the \"!\"", function()
    inCamp = { { "S_Player_Claude_" .. CLAUDE } }
    local o = karlachRow()
    eq(o.pos and o.pos[1], -1450.0, "pos x")
    eq(o.wants_talk, true, "wants_talk")
end)

test("campRoster with DB_PlayerInCamp unreadable fails closed", function()
    inCamp = "error"
    local o = karlachRow()
    eq(o.pos, nil, "pos")
    eq(o.wants_talk, nil, "wants_talk")
    eq(visible(KARLACH), false, "visible")
end)

test("S083: state.entity gives a visible NPC's position (travel_to walks legs from it)", function()
    sees[WITHERS] = true
    V.context = ctx
    local o = handlers["state.entity"]({ uuid = WITHERS })
    eq(o.pos and o.pos[1], -1418.7, "pos x")
    eq(o.pos and o.pos[3], -2585.5, "pos z")
end)

test("state.entity of an unseen NPC still refuses", function()
    V.context = ctx
    local ok, err = pcall(handlers["state.entity"], { uuid = GOB })
    eq(ok, false, "refused")
    eq(err, "not visible", "error")
end)

test("V24 S083: a listing's locked read then unlocked read tells interact the game just unlocked it", function()
    local seen = {}
    BG3AI.Actions = { unlock_seen = function(u) seen[#seen + 1] = u end }
    local DOOR = "dddddddd-0000-0000-0000-000000000720"
    local lockedNow = 1
    Osi.IsLocked = function(u) return u == DOOR and lockedNow or 0 end
    eq(S.lockRead(DOOR), true, "locked before Knock")
    eq(#seen, 0, "no unlock yet")
    lockedNow = 0
    eq(S.lockRead(DOOR), false, "unlocked after Knock")
    eq(seen[1], DOOR, "unlock_seen")
    eq(S.lockRead(DOOR), false, "read again")
    eq(#seen, 1, "one unlock per locked -> unlocked change")
    eq(S.lockRead(GOB), false, "never locked")
    eq(#seen, 1, "an item never seen locked is no unlock")
    Osi.IsLocked, BG3AI.Actions = nil, nil
end)

test("V25 S084: S.plateOn names the party members standing on a floor plate", function()
    local c = ctx()
    local plate = { -1421.6, 8.4, -2601.0 } -- 0.45 m from Claude
    local on = S.plateOn(c, plate, nil)
    eq(on and on[1], CLAUDE, "Claude on it")
    eq(S.plateOn(c, { -1419.0, 8.4, -2601.2 }, nil), nil, "2.2 m off: not on it")
    eq(S.plateOn(c, { -1421.2, 11.0, -2601.2 }, nil), nil, "a floor above")
end)

-- S091 positions (08:19-08:28): Claude on the platform, the Emperor further along it.
local CLAUDE_PSYCHE = { 230.4, -53.0, 4270.3 }  -- 20.7 m from the brain's anchor, 8 m above it
local EMPEROR_PSYCHE = { 236.3, -54.2, 4261.3 } -- 22.5 m
local function seenFrom(at)
    local c = { party = { CLAUDE }, is_party = { [CLAUDE] = true },
        observers = { { uuid = CLAUDE, pos = at, sight = 18.5 } } }
    return V.characterVisible(c, BRAIN, V.entity(BRAIN))
end

test("V29 S091: a Gargantuan creature is seen when its body, not its anchor point, is within sight", function()
    eq(seenFrom(CLAUDE_PSYCHE), false, "no size, no bounds: the anchor rule")
    ents[BRAIN].size = 5
    eq(seenFrom(CLAUDE_PSYCHE), true, "Gargantuan, 20.7 m to the anchor")
    los = false
    eq(seenFrom(CLAUDE_PSYCHE), false, "no line of sight: still hidden")
end)

test("V29: a big creature by its AI bounds (the Emperor's spot 22.5 m off)", function()
    ents[BRAIN].bounds = { Height = 12.0, ActualHeight = 12.0, Radius = 4.0 }
    eq(seenFrom(EMPEROR_PSYCHE), true, "bounds radius 4")
    V.boundsCache = {}
    ents[BRAIN].bounds = { Height = 2.0, ActualHeight = 2.0, Radius = 0.5 }
    eq(seenFrom(EMPEROR_PSYCHE), false, "a small body there")
end)

test("V29: Medium and smaller creatures keep the anchor rule, and a big one far off stays hidden", function()
    ents[BRAIN].size = 2
    ents[BRAIN].bounds = { Height = 12.0, ActualHeight = 12.0, Radius = 4.0 }
    eq(seenFrom(CLAUDE_PSYCHE), false, "Medium: no bounds read, anchor rule")
    ents[BRAIN].size = 5
    V.boundsCache = {}
    eq(seenFrom({ 245.0, -53.0, 4280.0 }), false, "35 m off: the body is past sight range too")
end)

test("V29: V.bodyDist to a cylinder", function()
    local function near(a, b, what) if math.abs(a - b) > 0.01 then error(what .. ": " .. a .. " vs " .. b, 2) end end
    near(V.bodyDist({ 10, 0, 0 }, { 0, 0, 0 }, 3, 6), 7, "beside it")
    near(V.bodyDist({ 10, 4, 0 }, { 0, 0, 0 }, 3, 6), 7, "level with its middle")
    near(V.bodyDist({ 7, 10, 0 }, { 0, 0, 0 }, 3, 6), 5.66, "above it")
    near(V.bodyDist({ 1, -2, 0 }, { 0, 0, 0 }, 3, 6), 2, "under it")
end)

-- S093: inside the psyche, the brain in the party's fight; no line of sight from the platform.
local FIGHT = "11111111-0000-0000-0000-000000000093"
local function psycheFight()
    ents[BRAIN].size = 5
    los = false
    fight[CLAUDE], fight[BRAIN] = FIGHT, FIGHT
end

test("V31 S093: rays uncalibrated (the psyche floor): a big creature in the party's fight is seen by its body", function()
    psycheFight()
    eq(seenFrom(CLAUDE_PSYCHE), true, "Gargantuan foe, body within sight, no LOS, ray rule off")
    eq(V.calib.ok, false, "uncalibrated")
end)

test("V31: the uncalibrated fallback needs the party's own fight, a big body and the body within sight", function()
    psycheFight()
    fight[BRAIN] = nil
    eq(seenFrom(CLAUDE_PSYCHE), false, "not in a fight")
    fight[BRAIN] = "22222222-0000-0000-0000-000000000000"
    eq(seenFrom(CLAUDE_PSYCHE), false, "another fight")
    fight[BRAIN] = FIGHT
    eq(seenFrom({ 245.0, -53.0, 4280.0 }), false, "35 m off: the body is past sight range")
    ents[BRAIN].size = 2
    eq(seenFrom(CLAUDE_PSYCHE), false, "Medium: the anchor rule")
end)

test("V31: calibrated rays still decide (a wall in the way hides the fighting brain); a broken ray API stays strict", function()
    psycheFight()
    Ext.Level = { RaycastClosest = function(a, b)
        if a[1] == b[1] and a[3] == b[3] and b[2] < a[2] then return { Distance = 1.6 } end -- the floor
        return { Distance = 1.0 } -- a wall 1 m ahead of every eye ray
    end }
    eq(seenFrom(CLAUDE_PSYCHE), false, "floor hit, wall: hidden")
    eq(V.calib.ok, true, "calibrated")
    Ext.Level, V.rayCache, V.calib = nil, {}, { t = -1e9, ok = false }
    V.rayBroken = true
    eq(seenFrom(CLAUDE_PSYCHE), false, "rule switched off (check_vis) or broken: strict")
end)

-- S094 09:54: Claude 2.2 m from the objective's spot; CanSee and LOS to the stem's anchor fail, no ray answers.
local function itemSeen(u, at)
    local c = { party = { CLAUDE }, is_party = { [CLAUDE] = true },
        observers = { { uuid = CLAUDE, pos = at or ents[CLAUDE].pos, sight = 18.5 } } }
    return V.itemVisible(c, u, V.entity(u))
end

test("V32 S094: the item an active objective's marker sits on is seen within sight range (the Brain Stem)", function()
    los = false
    objectives[STEM] = true
    local vis, kind = itemSeen(STEM)
    eq(vis, true, "stem seen")
    eq(kind, "usable", "kind")
    eq(itemSeen(VASE), false, "a vase beside it, no objective: strict rule")
    eq((V.itemKnown(ctx(), STEM, V.entity(STEM))), true, "interact's check")
end)

test("V32: no objective on it, past sight range, or scenery: hidden", function()
    los = false
    eq(itemSeen(STEM), false, "not an objective target")
    objectives[STEM] = true
    eq(itemSeen(STEM, { -1425.2, 8.4, -2630.0 }), false, "24.8 m off")
    canInteract[STEM] = false
    eq(itemSeen(STEM), false, "the game doesn't highlight scenery")
    canInteract[STEM] = nil
    onStage[STEM] = false
    eq(itemSeen(STEM), false, "off stage")
end)

-- S064 11:37: Wyll's passive roll discovers e302 while the walkers face it; the poll pushes "spotted" and the walk
-- stops 10 m short. Then nobody's CanSee/LOS reaches the floor vent, and disarm(e302) was "target is not visible".
test("S064: a spotted trap stays a disarm target after the CanSee that spotted it is gone", function()
    local realCtx, realAround, realReady = V.context, V.itemsAround, V.index.ready
    V.context, V.itemsAround, V.index.ready = ctx, function() return { VENT } end, true
    V.staticSeen[VENT], E.knownTraps, E.trapsPrimed, pushed = nil, {}, false, {}
    local ok, err = pcall(function()
        traps[VENT], canInteract[VENT], los = "hidden", false, false
        E.lastPoll = -1e9; poll()  -- first pass: learns traps found before the load
        traps[VENT], sees[VENT] = "discovered", true
        E.lastPoll = -1e9; poll()
        eq(pushed[1] and pushed[1].kind, "spotted", "spotted pushed")
        eq(pushed[1] and pushed[1].data.kind, "trap", "kind")
        sees[VENT] = false
        eq((V.itemVisible(ctx(), VENT, V.entity(VENT))), false, "no longer in sight (listings: strict)")
        eq((V.itemKnown(ctx(), VENT, V.entity(VENT))), true, "disarm's check (V.itemKnown)")
    end)
    V.context, V.itemsAround, V.index.ready = realCtx, realAround, realReady
    if not ok then error(err, 0) end
end)

test("S065: get_nearby lists a discovered standalone trap (kinds=trap or none), not an undiscovered one", function()
    local realAround = V.itemsAround
    V.itemsAround = function() return { VENT, VASE } end
    local ok, err = pcall(function()
        canInteract[VENT], sees[VENT] = false, true
        local function names(args)
            local out = {}
            for _, o in ipairs(S.nearby(ctx(), args).items) do out[#out + 1] = o.name .. "<" .. o.kind .. ">" end
            return table.concat(out, ",")
        end
        traps[VENT] = "hidden"
        eq(names({ kinds = { "trap" } }), "", "undiscovered: kinds=trap")
        traps[VENT] = "discovered"
        eq(names({ kinds = { "trap" } }), "Sanguine Vent<trap>", "kinds=trap")
        eq(names({ kinds = { "trap", "door" } }), "Sanguine Vent<trap>", "kinds=trap,door")
        eq(names({}), "Sanguine Vent<trap>", "unfiltered (the vase has no name: skipped)")
        eq(names({ kinds = { "usable" } }), "", "kinds=usable")
    end)
    V.itemsAround = realAround
    if not ok then error(err, 0) end
end)

test("get_nearby can_trade: a non-hostile trader (Trader tag or Trade offered), not every CanTrade", function()
    local realChars, gobPos = V.characters, ents[GOB].pos
    ents[GOB].pos = { -1419.0, 8.4, -2590.0 } -- a windmill goblin 11 m off
    V.characters = function() return { { V.entity(WITHERS), ents[WITHERS].pos }, { V.entity(GOB), ents[GOB].pos } } end
    local ok, err = pcall(function()
        sees[WITHERS], sees[GOB], ents[WITHERS].Trader = true, true, {}
        local function flags()
            local out = {}
            for _, o in ipairs(S.nearby(ctx(), { kinds = { "characters" } }).characters) do
                out[#out + 1] = o.uuid:sub(-3) .. "=" .. tostring(o.can_trade)
            end
            table.sort(out)
            return table.concat(out, ",")
        end
        eq(flags(), "009=nil,352=true", "the merchant only")
        -- a dialogue with the goblin offered Trade (Client/UIState.lua's TradeOffered message): a trader from then on
        E.dialogs = { [3] = { GOB } }
        BG3AI.Referee.tradeOffered()
        E.dialogs = {}
        eq(flags(), "009=true,352=true", "Trade offered in a dialogue")
        enemies[WITHERS] = true
        eq(flags(), "009=true,352=nil", "a hostile merchant: no")
    end)
    BG3AI.Referee.traders = {}
    V.characters, ents[WITHERS].Trader, ents[GOB].pos = realChars, nil, gobPos
    if not ok then error(err, 0) end
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
