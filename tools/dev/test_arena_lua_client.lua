-- Offline tests for two teams in one fight (an arena), client context: the team names the server sends over Ext.Net,
-- a reaction prompt seen and answered only by the side whose character it is for, the HUD's selection read only by
-- the side it belongs to. test_arena_lua.lua has the server half.
-- Ext and the UI tree are stubbed; Shared/RPC.lua, Shared/Arena.lua, Client/UIState.lua, UIActionsPrompts.lua and
-- UISettings.lua are the real ones.
-- Run: tools/dev/test_arena_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local A1 = "aaaaaaaa-0000-0000-0000-0000000000a1"
local B1 = "bbbbbbbb-0000-0000-0000-0000000000b1"

local env = {}
local channel             -- the client's ArenaTeams handler
local pages, entities = {}, {}
local decoded = {}
local clock = 1000        -- Ext.Utils.MonotonicTime

os.getenv = function(n) return env[n] end -- luacheck: ignore 122

local function enc(v)
    local t = type(v)
    if t == "table" then
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = tostring(k) end
        table.sort(keys)
        local out = {}
        for _, k in ipairs(keys) do out[#out + 1] = k .. "=" .. enc(v[k] ~= nil and v[k] or v[tonumber(k)]) end
        return "{" .. table.concat(out, ",") .. "}"
    end
    return t == "string" and string.format("%q", v) or tostring(v)
end

Ext = {
    IsServer = function() return false end,
    IsClient = function() return true end,
    Utils = { MonotonicTime = function() return clock end, Print = function() end, PrintWarning = function() end,
        GetGameState = function() return "Running" end, GetGlobalSwitches = function() return {} end },
    Events = { Tick = { Subscribe = function() end } },
    Json = { Parse = function(s) return load("return " .. s)() end,
        Stringify = function(v) local s = enc(v) decoded[s] = v return s end },
    Net = { CreateChannel = function(_, _, fn) channel = fn end },
    Entity = { GetAllEntitiesWithComponent = function(c)
        local out = {}
        for _, e in ipairs(entities) do if e[c] then out[#out + 1] = e end end
        return out
    end, Get = function() return nil end },
    Stats = { Get = function() return nil end },
}

-- A UI tree of plain tables: {name, type, text, DataContext, children}.
local UI = {}
function UI.widgetMap()
    local m = {}
    for n, el in pairs(pages) do m[n] = { el = el } end
    return m
end
function UI.isVisible(el) return el.visible ~= false end
function UI.content(el) return el.children or {} end
function UI.name(e) return e.name end
function UI.typeName(e) return e.type end
function UI.text(e) return e.text end
function UI.prop(o, k) return type(o) == "table" and o[k] or nil end
function UI.props(o) return type(o) == "table" and o or {} end
function UI.clean(v) if v ~= "" then return v end end
function UI.get() return nil end
function UI.cachedByName() return nil end
function UI.allText(e)
    local out = {}
    local function walk(x)
        if x.text then out[#out + 1] = x.text end
        for _, c in ipairs(x.children or {}) do walk(c) end
    end
    walk(e)
    return table.concat(out, " | ")
end
function UI.findAll(root, pred, opts)
    local out = {}
    local function walk(x, top)
        if not top and pred(x) then
            out[#out + 1] = x
            if opts and opts.prune then return end
        end
        for _, c in ipairs(x.children or {}) do walk(c, false) end
    end
    walk(root, true)
    return out
end
function UI.path(o, p)
    for part in p:gmatch("[^%.]+") do
        if type(o) ~= "table" then return nil end
        o = o[tonumber(part) or part]
    end
    return o
end
function UI.count(c) return type(c) == "table" and #c or 0 end
function UI.items(c, max)
    local i, n = 0, type(c) == "table" and #c or 0
    return function()
        i = i + 1
        if i <= n and (not max or i <= max) then return i, c[i] end
    end
end
function UI.dc() return nil end
function UI.anyDC() return nil end
function UI.pcall(f, ...) return pcall(f, ...) end
UI.commands, UI.params = {}, {}
function UI.command(_, name, param)
    UI.commands[#UI.commands + 1] = name
    UI.params[#UI.commands] = param
    return true
end

BG3AI = { UI = UI, Util = { loca = function(h) return h end } }
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Shared/Arena.lua")
dofile(LUA .. "/Client/UIState.lua")
dofile(LUA .. "/Client/UIStateArena.lua")
local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
end
local function waitJob(kind, ms, fn) -- UIActions.lua's: a job whose check ticks
    local job = BG3AI.RPC.start_job(kind, { timeout_ms = ms })
    job.on_tick = function(j)
        local status, result = fn(j)
        if status then BG3AI.RPC.finish_job(j, status, result) end
    end
    return { job = job.id }
end
BG3AI.UIActions = { inner = { try = try, check = function(ok, err) if not ok then error(err, 0) end return ok end,
    sig = function() return "" end, waitJob = waitJob, dialogueSnapshot = function() end,
    settleDialogue = function() end }, waitJob = waitJob }
dofile(LUA .. "/Client/UIActionsPrompts.lua")
dofile(LUA .. "/Client/UISettings.lua")
dofile(LUA .. "/Client/ArenaCamera.lua")
local RPC, Ar, S = BG3AI.RPC, BG3AI.Arena, BG3AI.UIState

local function rpc(op, args, side)
    return decoded[BG3AI.rpc("{op=" .. string.format("%q", op) .. ",args=" .. (args or "{}")
        .. (side and (",side=" .. string.format("%q", side)) or "") .. "}")]
end

-- A reaction prompt for these characters (one row each).
local function prompt(...)
    local roots = {}
    for _, who in ipairs({ ... }) do
        roots[#roots + 1] = { name = "InterruptorRoot", DataContext = { Character = { Name = who } },
            children = { { name = "ReactButton", children = { { type = "TextBlock", text = "Shield" } } } } }
    end
    pages.ReactionDecisionPopup = { name = "ReactionDecisionPopup", children = roots,
        DataContext = { Data = { InterruptEvents = { {} } } } }
end

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    env = { BG3AI_ARENA = "1" }
    pages, entities, UI.commands, UI.params = { HotBar = { name = "HotBar" } }, {}, {}, {}
    clock, RPC.jobs, RPC.sideInputAt = 1000, {}, {}
    channel({ teams = { A = { A1 }, B = { B1 } }, names = { A = { "Ana" }, B = { "Bo" } } })
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what)
    if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end
end

test("the teams from the server: names to sides; an empty message ends the arena", function()
    eq(Ar.name_side("Bo"), "B", "Bo")
    eq(Ar.name_of(A1), "Ana", "A1's name")
    eq(Ar.active(), true, "active")
    channel({})
    eq(Ar.state, nil, "state")
    eq(RPC.with_side("A", RPC.side), nil, "no side without an arena")
end)

test("a prompt for side A's character: A reads it, B reads only that it is the other side's", function()
    prompt("Ana")
    local a = rpc("ui.state", "{}", "A").result
    eq(a.screen, "reaction", "screen")
    eq(a.reaction.options[1].who, "Ana", "A: who")
    local b = rpc("ui.state", "{}", "B").result
    eq(b.screen, "reaction", "B's screen")
    eq(b.reaction.other_side, true, "B: other_side")
    eq(b.reaction.options, nil, "B: no options")
    eq(b.reaction.prompt, nil, "B: no prompt text")
    eq(rpc("ui.state", "{}").result.reaction.options[1].who, "Ana", "no side: the whole prompt")
end)

test("ui.react: the other side's prompt is 'not your reaction'; its own goes on", function()
    prompt("Ana")
    eq(rpc("ui.react", "{use=false}", "B").error, "not your reaction", "B")
    local a = rpc("ui.react", "{use=false}", "A")
    eq(a.ok, true, "A: " .. tostring(a.error))
    eq(UI.commands[1], "InterruptDontReactPlayerCommand", "A's answer")
end)

-- L5 reads (live.md 01:03:22): the popup's rows don't name a twin; the game's own InterruptActionState does.
local function held(...)
    local actions = {}
    for _, o in ipairs({ ... }) do
        actions[#actions + 1] = { Observer = { Uuid = { EntityUuid = o } },
            Interrupt = { InterruptData = { Interrupt = "Interrupt_Counterspell" } } }
    end
    entities[#entities + 1] = { InterruptActionState = { SpellCastGuid = "g1", Actions = actions } }
end
local function nameless_prompt()
    prompt("Ana")
    pages.ReactionDecisionPopup.children[1].DataContext.Character.Name = nil
end

test("a twin's prompt with no name on its rows is the observer's side's, shown only to it", function()
    nameless_prompt()
    held(B1)
    local a = rpc("ui.state", "{}", "A").result
    eq(a.reaction.other_side, true, "A: the other side's")
    eq(a.reaction.unowned, nil, "A: owned")
    eq(a.reaction.options, nil, "A: no rows")
    eq(a.reaction.prompt, nil, "A: no text")
    local b = rpc("ui.state", "{}", "B").result
    eq(b.reaction.other_side, nil, "B: its own")
    eq(b.reaction.options[1].who, "Bo", "B: who, from the observer")
    local h = rpc("ui.state", "{}").result
    eq(h.reaction.owner, "B", "the harness reads the owner")
    eq(h.reaction.unowned, nil, "not unowned")
end)

test("react: the observer's side answers, the other is refused", function()
    nameless_prompt()
    held(B1)
    eq(rpc("ui.react", "{use=false}", "A").error, "not your reaction", "A")
    eq(rpc("ui.react", "{use=false}", "B").ok, true, "B")
    eq(UI.commands[1], "InterruptDontReactPlayerCommand", "B's answer")
end)

test("the observer outranks a row's name; observers on both sides fall back to the rows", function()
    prompt("Ana")
    held(B1)
    eq(rpc("ui.react", "{use=false}", "A").error, "not your reaction", "the observer's, not the row's")
    entities = {}
    nameless_prompt()
    held(A1, B1)
    eq(rpc("ui.state", "{}").result.reaction.unowned, true, "both sides, no name: nobody's")
end)

test("held_reaction: the harness reads a hold with no popup; a side never does", function()
    held(B1)
    local h = rpc("ui.state", "{}").result.held_reaction
    eq(h.side, "B", "side")
    eq(h.interrupts[1], "Interrupt_Counterspell", "what")
    assert(h.key:find("g1", 1, true), h.key)
    eq(rpc("ui.state", "{}", "A").result.held_reaction, nil, "A")
    eq(rpc("ui.state", "{}", "B").result.held_reaction, nil, "B")
    entities = {}
    eq(rpc("ui.state", "{}").result.held_reaction, nil, "nothing held")
end)

-- L5 reads (live.md 02:00:05): Cutting Words + Shield: use=true read done, nobody reacted, Confirm stayed grey.
test("two interruptors: use=true hands the row to the bridge; only a row that reads chosen is accepted", function()
    prompt("Bo", "Bo")
    held(B1)
    local r = rpc("ui.react", "{use=true,which=2}", "B")
    eq(r.result.needs_click, 2, "the row")
    eq(r.result.confirm, true, "and a confirm after")
    eq(#UI.commands, 0, "nothing accepted")
    local row = pages.ReactionDecisionPopup.children[2].children[1]
    row.DataContext = { Decision = "Undecided" }
    eq(rpc("ui.react", "{use=true,which=2}", "B").result.needs_click, 2, "Undecided isn't chosen")
    -- live.md 02:42:47: the row ticked on screen and IsChecked nil, as here; Decision is what says it
    row.DataContext.Decision = "Use"
    eq(rpc("ui.state", "{}", "B").result.reaction.options[2].chosen, true, "ui.state shows it")
    rpc("ui.react", "{use=true,which=2}", "B")
    eq(UI.commands[1], "InterruptAcceptPlayerCommand", "accepted")
    local bad = rpc("ui.react", "{use=true,which=9}", "B").error
    assert(tostring(bad):find("no reaction option 9 (2 shown)", 1, true), bad)
end)

-- One tick of the newest job, as its side (RPC.lua tick_jobs); the job.
local function tickJob()
    local j = RPC.lastJob
    RPC.with_side(j.side, j.on_tick, j)
    return j
end

-- REVIEW-reactions F1: a job ticks outside the call, so the result of A's answer read B's prompt with no side.
test("an answered prompt with the other side's next: the job's result has only that it's there", function()
    prompt("Ana")
    pages.ReactionDecisionPopup.DataContext.Data.InterruptEvents = { {}, {} }
    local job = rpc("ui.react", "{use=false}", "A")
    eq(job.ok, true, tostring(job.error))
    pages.ReactionDecisionPopup = nil
    prompt("Bo")
    local r = tickJob()
    eq(r.status, "done", "status")
    eq(r.result.pending, 1, "pending")
    eq(r.result.reaction.other_side, true, "other side")
    eq(r.result.reaction.options, nil, "no rows")
    eq(r.result.reaction.prompt, nil, "no text")
end)

test("the same with the side's own next: it reads its prompt", function()
    prompt("Ana")
    pages.ReactionDecisionPopup.DataContext.Data.InterruptEvents = { {}, {} }
    local job = rpc("ui.react", "{use=false}", "A")
    pages.ReactionDecisionPopup.DataContext.Data.InterruptEvents = { {} }
    local r = tickJob()
    eq(r.result.reaction.options[1].who, "Ana", "its row")
end)

-- live.md 03:44:15 read 4b: chosen, then Do Not React: a roll that hit did no damage, nothing paid.
test("Do Not React on a chosen row unchooses it first, and waits for that", function()
    prompt("Ana")
    local root = pages.ReactionDecisionPopup.children[1]
    local row = root.children[1]
    row.DataContext = { Decision = "Use" }
    local job = rpc("ui.react", "{use=false}", "A")
    eq(job.ok, true, tostring(job.error))
    eq(UI.commands[1], "InterruptUndecidedCommand", "unchosen first")
    eq(UI.params[1], root.DataContext, "by the interruptor")
    eq(#UI.commands, 1, "no decline yet")
    eq(tickJob().status, "running", "still chosen: waits")
    eq(#UI.commands, 1, "still no decline")
    row.DataContext.Decision = "Undecided"
    tickJob()
    eq(UI.commands[2], "InterruptDontReactPlayerCommand", "then the decline")
    pages.ReactionDecisionPopup.DataContext.Data.InterruptEvents = {}
    eq(tickJob().status, "done", "closed")
    eq(#UI.commands, 2, "one decline")
end)

test("Do Not React on a chosen row: an event gone before the first tick is declined by nobody (U1)", function()
    prompt("Ana")
    local data = pages.ReactionDecisionPopup.DataContext.Data
    data.InterruptEvents = { { id = "ana" }, { id = "bo" } }
    pages.ReactionDecisionPopup.children[1].children[1].DataContext = { Decision = "Use" }
    rpc("ui.react", "{use=false}", "A")
    data.InterruptEvents = { { id = "bo" } } -- Ana's left with the unchoosing: Bo's prompt is up
    pages.ReactionDecisionPopup.children[1].children[1].DataContext = { Decision = "Undecided" }
    eq(tickJob().status, "done", "closed")
    eq(#UI.commands, 1, "only the unchoosing: Bo's prompt isn't declined, nor Ana's stale event sent")
end)

test("Do Not React on a chosen row: the decline reads the event afresh (U1)", function()
    prompt("Ana")
    local data = pages.ReactionDecisionPopup.DataContext.Data
    data.InterruptEvents = { { id = "old" } }
    local row = pages.ReactionDecisionPopup.children[1].children[1]
    row.DataContext = { Decision = "Use" }
    rpc("ui.react", "{use=false}", "A")
    local now = { id = "now" }
    data.InterruptEvents = { now }
    row.DataContext.Decision = "Undecided"
    tickJob()
    eq(UI.commands[2], "InterruptDontReactPlayerCommand", "the decline")
    eq(UI.params[2], now, "today's event")
end)

test("Do Not React with nothing chosen: only the decline", function()
    prompt("Ana")
    pages.ReactionDecisionPopup.children[1].children[1].DataContext = { Decision = "Undecided" }
    rpc("ui.react", "{use=false}", "A")
    eq(UI.commands[1], "InterruptDontReactPlayerCommand", "decline")
    eq(#UI.commands, 1, "alone")
end)

test("ui.react_spent: the observers' total, a side's own only; ui.react_probe hides the other side's prompt", function()
    nameless_prompt()
    held(B1)
    local ent = { ActionResources = { Resources = { a = { { Amount = 1 } }, b = { { Amount = 2 }, { Amount = 3 } } } } }
    local get = Ext.Entity.Get
    Ext.Entity.Get = function(u) return u == B1 and ent or nil end
    local m = rpc("ui.react_spent", "{}", "B").result
    eq(m.total, 6, "summed")
    eq(m.uuids[1], B1, "the observer")
    eq(#rpc("ui.react_spent", "{}", "A").result.uuids, 0, "A gets nobody")
    eq(rpc("ui.react_spent", '{uuids={"' .. B1 .. '"}}', "A").result.total, 0, "nor by asking for it")
    ent.ActionResources.Resources.a[1].Amount = 0
    eq(rpc("ui.react_spent", '{uuids={"' .. B1 .. '"}}', "B").result.total, 5, "after the spend")
    Ext.Entity.Get = get
    eq(rpc("ui.react_probe", "{}", "A").result.open, false, "A: no prompt")
    eq(rpc("ui.react_probe", "{}", "B").result.open, true, "B: its own")
    -- pQ N3: a watch runs while the game takes the popup down: it walks no rows, whoever asks
    local rows_of = S.reactionRows
    S.reactionRows = function() error("walked the rows on a watch") end
    eq(rpc("ui.react_probe", "{watch=true}", "A").result.open, true, "a watch reads only that it shows")
    S.reactionRows = rows_of
end)

test("the hold's key doesn't follow the order of the game's list", function()
    held(B1)
    entities[1].InterruptActionState.SpellCastGuid = "z"
    held(B1)
    local one = rpc("ui.state", "{}").result.held_reaction.key
    entities[1], entities[2] = entities[2], entities[1]
    eq(rpc("ui.state", "{}").result.held_reaction.key, one, "same key")
end)

test("a prompt naming both sides, or someone of neither, is nobody's", function()
    prompt("Ana", "Bo")
    eq(S.reactionSide(S.reactionRows(pages.ReactionDecisionPopup)), nil, "both")
    eq(rpc("ui.react", "{use=false}", "A").error, "not your reaction", "A")
    prompt("Withers")
    eq(rpc("ui.react", "{use=false}", "B").error, "not your reaction", "B")
end)

test("a prompt for a summon whose name the server sent: its side's", function()
    channel({ teams = { A = { A1 }, B = { B1 } }, names = { A = { "Ana", "Aspect" }, B = { "Bo" } } })
    prompt("Aspect")
    eq(rpc("ui.react", "{use=false}", "A").ok, true, "A answers it")
    eq(rpc("ui.react", "{use=false}", "B").error, "not your reaction", "B")
    eq(rpc("ui.state", "{}", "B").result.reaction.unowned, nil, "B: owned (by A)")
end)

test("an unowned prompt (no rows read, or a name on both teams): flagged for both sides and for the harness", function()
    prompt()
    local b = rpc("ui.state", "{}", "B").result.reaction
    eq(b.other_side, true, "B: other_side")
    eq(b.unowned, true, "B: unowned")
    eq(rpc("ui.state", "{}").result.reaction.unowned, true, "the harness: unowned")
    eq(rpc("ui.react", "{use=false}").ok, true, "the harness answers it")
    channel({ teams = { A = { A1 }, B = { B1 } }, names = { A = { "Ana", "Cat" }, B = { "Bo", "Cat" } } })
    prompt("Cat")
    eq(rpc("ui.state", "{}", "A").result.reaction.unowned, true, "a summon name on both teams")
    eq(rpc("ui.react", "{use=false}", "A").error, "not your reaction", "A")
end)

test("switch off: a side's request answers the prompt as today", function()
    env = {}
    prompt("Ana")
    eq(rpc("ui.react", "{use=false}", "B").ok, true, "B")
    local r = rpc("ui.state", "{}", "B").result.reaction
    eq(r.options[1].who, "Ana", "B reads it")
    eq(r.unowned, nil, "no unowned flag")
    prompt()
    eq(rpc("ui.state", "{}").result.reaction.unowned, nil, "no unowned flag without an arena")
end)

test("the HUD's selection: read only by the side whose character it is", function()
    entities = { { Uuid = { EntityUuid = A1 }, ClientControl = {} } }
    eq(rpc("ui.selected", "{}", "A").result.uuid, A1, "A")
    eq(rpc("ui.selected", "{}", "B").result.uuid, nil, "B")
    eq(rpc("ui.selected", "{}").result.uuid, A1, "no side")
end)

test("ui.reactions for the other side's character: '<name> is not on your side'", function()
    entities = { { Uuid = { EntityUuid = A1 }, ClientControl = {} } }
    eq(rpc("ui.reactions", "{char=" .. string.format("%q", A1) .. "}", "B").error, "Ana is not on your side", "B")
end)

-- arena.camera: a camera entity and the two fighters at these positions (Get by uuid).
local function camera_scene(pa, pb, deadB)
    local cam = { GameCameraBehavior = { PlayerInControl = false, TargetDestination = { 0, 0, 0 } } }
    local ents = { [A1] = { Transform = { Transform = { Translate = pa } } },
        [B1] = { Transform = { Transform = { Translate = pb } }, Death = deadB and {} or nil } }
    entities = { cam }
    Ext.Entity.Get = function(u) return ents[u] end
    return cam.GameCameraBehavior, ents
end

test("arena.camera: the view goes to the living fighters' centroid, at a distance from their spread", function()
    local cam = camera_scene({ 10, 2, 20 }, { 14, 2, 20 })
    local r = rpc("arena.camera").result
    eq(r.moved, true, "moved")
    eq(cam.TargetDestination[1], 12, "x")
    eq(cam.TargetDestination[3], 20, "z")
    eq(cam.PlayerInControl, true, "the camera held")
    eq(cam.DistanceDestination, 8, "spread 2 m + 6 -> 8")
    camera_scene({ 0, 2, 0 }, { 30, 2, 0 })
    eq(rpc("arena.camera").result.distance, 12, "a wide spread caps at 12")
    local cam2 = camera_scene({ 10, 2, 20 }, { 50, 2, 20 }, true)
    rpc("arena.camera")
    eq(cam2.TargetDestination[1], 10, "a dead fighter is out of the centroid")
end)

test("arena.camera: held while the view is ours and the fighters moved <= 2 m; re-written after a focus or a shift",
    function()
    local cam, ents = camera_scene({ 10, 2, 20 }, { 14, 2, 20 })
    rpc("arena.camera")
    eq(rpc("arena.camera").result.moved, false, "held")
    ents[A1].Transform.Transform.Translate = { 13, 2, 20 } -- centroid +1.5 m
    eq(rpc("arena.camera").result.moved, false, "a small shift")
    ents[A1].Transform.Transform.Translate = { 16, 2, 20 } -- +3 m from the written target
    eq(rpc("arena.camera").result.moved, true, "a > 2 m shift")
    cam.PlayerInControl = false -- an attack's focus took the camera
    cam.TargetDestination = { 0, 0, 0 }
    eq(rpc("arena.camera").result.moved, true, "taken back after a focus")
    eq(cam.TargetDestination[1], 15, "x")
end)

test("arena.camera: never during a side's input (a running job, a client op within 2 s)", function()
    local cam = camera_scene({ 10, 2, 20 }, { 14, 2, 20 })
    RPC.register("ui.screen_pos", function() return {} end) -- Client/Pick.lua's (not loaded here)
    rpc("ui.screen_pos", "{}", "A") -- a click's first step: project through the camera
    eq(rpc("arena.camera").result.skipped, "side A's input", "right after A's projection")
    eq(cam.PlayerInControl, false, "not written")
    clock = clock + 2000
    rpc("ui.selected", "{}", "B") -- a read: not input
    eq(rpc("arena.camera").result.moved, true, "2 s later, after B's read")
    RPC.with_side("B", RPC.start_job, "ui.flee", {})
    cam.PlayerInControl = false
    eq(rpc("arena.camera").result.skipped, "side B's ui.flee", "B's job runs")
end)

test("arena.camera: live window 6's scripted match: the sides' reads never hold the camera off", function()
    -- w6-scripted (ops/logs/live.md 'Run 4a'): each get_combat_state sends ui.story_timer, here at the logged times
    -- (s after 22:35:00), the runner ticks every 1 s and the game's attack focus has taken the view before each tick.
    -- Counted as input, these reads left 0.4-1.2 s gaps and the view never came to the fight.
    local cam = camera_scene({ 212.25, 11, 288 }, { 213.75, 11, 296 })
    local reads = { { 9.84, "A" }, { 11.59, "A" }, { 13.97, "B" }, { 16.44, "B" }, { 17.14, "B" }, { 19.5, "A" },
        { 21.14, "A" }, { 24.38, "B" }, { 26.64, "B" } }
    local moved, i = 0, 1
    for tick = 10.2, 29.2, 1.0 do
        while reads[i] and reads[i][1] <= tick do
            clock = math.floor(reads[i][1] * 1000)
            rpc("ui.story_timer", "{}", reads[i][2])
            i = i + 1
        end
        clock = math.floor(tick * 1000)
        cam.PlayerInControl = false
        if rpc("arena.camera").result.moved then moved = moved + 1 end
    end
    eq(moved, 20, "ticks that put the view on the fight")
    eq(cam.TargetDestination[1], 213, "x")
end)

test("arena.camera: a view slid off the fight while still the player's (live window 6 run 7) is taken back", function()
    -- cam-w6-scripted-3 520.6: PlayerInControl true, target (213.0, 7.4, 243.7), the fighters' centroid 51 m north
    local cam = camera_scene({ 212.6, 10.98, 294.06 }, { 212.69, 10.99, 295.56 })
    eq(rpc("arena.camera").result.moved, true, "first write")
    cam.TargetDestination = { 213.0, 7.4, 243.7 }
    eq(cam.PlayerInControl, true, "still the player's")
    eq(rpc("arena.camera").result.moved, true, "re-written")
    eq(cam.TargetDestination[3] > 294, true, "back on the fighters")
    eq(rpc("arena.camera").result.moved, false, "then held")
end)

test("arena.camera: the harness's only (refused with a side and without an arena)", function()
    camera_scene({ 10, 2, 20 }, { 14, 2, 20 })
    eq(rpc("arena.camera", "{}", "A").error, "arena.camera is the harness's (no side)", "side A")
    channel({})
    eq(rpc("arena.camera").error, "no active arena", "no arena")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
