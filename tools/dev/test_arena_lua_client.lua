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
    Utils = { MonotonicTime = function() return 1000 end, Print = function() end, PrintWarning = function() end,
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
UI.commands = {}
function UI.command(_, name) UI.commands[#UI.commands + 1] = name return true end

BG3AI = { UI = UI, Util = { loca = function(h) return h end } }
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Shared/Arena.lua")
dofile(LUA .. "/Client/UIState.lua")
local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
end
BG3AI.UIActions = { inner = { try = try, check = function(ok, err) if not ok then error(err, 0) end return ok end,
    sig = function() return "" end, waitJob = function() return { job = 1 } end, dialogueSnapshot = function() end,
    settleDialogue = function() end }, waitJob = function() return { job = 1 } end }
dofile(LUA .. "/Client/UIActionsPrompts.lua")
dofile(LUA .. "/Client/UISettings.lua")
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
    pages, entities, UI.commands = { HotBar = { name = "HotBar" } }, {}, {}
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

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
