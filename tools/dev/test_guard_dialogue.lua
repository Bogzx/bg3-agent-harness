-- Offline tests for Guard.dialogue_info / in_dialogue / not_in_dialogue (worker se).
-- Live S005: combat_turn refused "character is in a conversation" for a whole
-- turn while ServerCharacter.InDialog was false and nothing was on screen.
-- Run: tools/dev/test_guard_dialogue.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local P2 = "aaaaaaaa-0000-0000-0000-000000000002"
local comps, dialogs, speaker_dialog, dm_ok, logged = {}, {}, {}, true, {}

Ext = {
    Utils = {
        MonotonicTime = function() return 0 end,
        Print = function(m) logged[#logged + 1] = m end,
        GetDialogManager = function() if dm_ok then return { Dialogs = dialogs } end return nil end,
    },
    Entity = { Get = function(u) return comps[u] or {} end },
    Require = function(p) return dofile(LUA .. "/" .. p) end, -- Guard.lua loads its parts
}
Osi = setmetatable({}, { __index = function(_, name)
    if name == "SpeakerGetDialog" then return function(u) return speaker_dialog[u] end end
    return function() return nil end
end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Guard.lua")
local G = BG3AI.Guard

local function sp(u) return { Uuid = { EntityUuid = u } } end
local function inst(id, t)
    t = t or {}
    t.DialogId = id
    t.DialogResourceUUID = t.DialogResourceUUID or ("res" .. id)
    return t
end
local function reset()
    comps = {
        [P1] = { ServerCharacter = { InDialog = false }, DialogState = { DialogId = 0 } },
        [P2] = { ServerCharacter = { InDialog = false }, DialogState = { DialogId = 0 } },
    }
    dialogs, speaker_dialog, dm_ok, logged = {}, {}, true, {}
end

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, msg) if a ~= b then error((msg or "") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

test("nothing running: not in dialogue", function()
    eq(G.in_dialogue(P1), false)
end)

test("S005: SpeakerGetDialog set but InDialog false and no instance -> not blocked", function()
    speaker_dialog[P1] = "SomeBark"
    eq(G.in_dialogue(P1), false)
    G.not_in_dialogue(P1) -- must not throw
end)

test("stale DialogState.DialogId (instance gone) -> not blocked", function()
    comps[P1].DialogState.DialogId = 202
    eq(G.in_dialogue(P1), false)
end)

test("InDialog true -> blocked, reason InDialog", function()
    comps[P1].ServerCharacter.InDialog = true
    local b, why = G.dialogue_info(P1)
    eq(b, true); eq(why, "InDialog")
end)

test("live player dialogue via DialogState -> blocked; the dialog id goes to the log, not the refusal", function()
    comps[P1].ServerCharacter.InDialog = true
    comps[P1].DialogState.DialogId = 7
    dialogs[7] = inst(7, { Speakers = { sp(P1) } })
    local ok, err = pcall(G.not_in_dialogue, P1)
    eq(ok, false)
    eq(G.is_refusal(err), true, "refusal")
    eq(tostring(err), "character is in a conversation")
    eq(logged[1], "[BG3AI] guard: in a conversation: dialog 7 res=res7")
end)

-- Bench B3 (act3_gold_max r2, 13:32:24): the refusal carried the dialog's resource
-- ("res=Generated/Public/Shared/Assets/Buildings/...") to the agent: a fairness leak.
test("B3: refusal text names no engine resource", function()
    comps[P1].ServerCharacter.InDialog = true
    comps[P1].DialogState.DialogId = 9864
    dialogs[9864] = inst(9864, { Speakers = { sp(P1) }, DialogResourceUUID = "Generated/Public/Shared/x_dummy.0" })
    local _, err = pcall(G.not_in_dialogue, P1)
    eq(tostring(err), "character is in a conversation")
end)

-- Bench B3: 2-5 s after the dialogue ended (InDialog false on the whole party), the manager
-- still listed it as an instance reading DialogId 0 and a building mesh as its resource.
test("B3: ended dialog still listed (DialogId 0, stale resource) -> not blocked", function()
    local mesh = "Generated/Public/Shared/Assets/Buildings/Generic/BLD_GEN_Platform_Floor_Wood_ABC/x_dummy.0"
    comps[P1].DialogState.DialogId = 9864
    dialogs[9864] = { DialogId = 0, DialogResourceUUID = mesh, Speakers = { sp(P1) } }
    eq(G.in_dialogue(P1), false, "via DialogState")
    comps[P1].DialogState.DialogId = 0
    eq(G.in_dialogue(P1), false, "via the Speakers scan")
    G.not_in_dialogue(P1) -- must not throw
end)

test("listener: DialogState points at a live non-automated instance -> blocked even if InDialog false", function()
    comps[P2].DialogState.DialogId = 9
    dialogs[9] = inst(9, { Speakers = { sp(P1), sp(P2) } })
    eq(G.in_dialogue(P2), true)
end)

test("speaker found by scanning Speakers (DialogState not set) -> blocked", function()
    dialogs[11] = inst(11, { Speakers = { sp(P1:upper()) } })
    eq(G.in_dialogue(P1), true)
    eq(G.in_dialogue(P2), false, "other member not a speaker")
end)

test("automated bark instance never blocks", function()
    comps[P1].DialogState.DialogId = 3
    dialogs[3] = inst(3, { AutomatedDialog = true, Speakers = { sp(P1) } })
    speaker_dialog[P1] = "Bark"
    eq(G.in_dialogue(P1), false)
end)

test("world/behaviour dialog doesn't block when InDialog is false", function()
    dialogs[4] = inst(4, { IsWorld = true, Speakers = { sp(P1) } })
    dialogs[5] = inst(5, { IsBehaviour = true, Speakers = { sp(P2) } })
    eq(G.in_dialogue(P1), false)
    eq(G.in_dialogue(P2), false)
end)

test("world dialog blocks when InDialog is true", function()
    comps[P1].ServerCharacter.InDialog = true
    dialogs[4] = inst(4, { IsWorld = true, Speakers = { sp(P1) } })
    local b, why = G.dialogue_info(P1)
    eq(b, true); eq(why, "dialog 4 res=res4")
end)

test("no manager and no InDialog: SpeakerGetDialog fallback still blocks", function()
    dm_ok = false
    comps[P1].ServerCharacter = nil
    speaker_dialog[P1] = "Real"
    local b, why = G.dialogue_info(P1)
    eq(b, true); eq(why, "SpeakerGetDialog")
    local _, err = pcall(G.not_in_dialogue, P1)
    eq(tostring(err), "character is in a conversation")
end)

test("no manager but InDialog false: SpeakerGetDialog alone doesn't block", function()
    dm_ok = false
    speaker_dialog[P1] = "Real"
    eq(G.in_dialogue(P1), false)
end)

test("throwing component/field reads are tolerated", function()
    comps[P1] = setmetatable({}, { __index = function() error("boom") end })
    dialogs[1] = setmetatable({}, { __index = function() error("boom") end })
    eq(G.in_dialogue(P1), false)
end)

print(("%d/%d passed"):format(count - failures, count))
if failures > 0 then os.exit(1) end
