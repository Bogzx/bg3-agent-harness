-- Offline tests for state.dialogue_tags (Server/StateDialog.lua). The dialog is shaped like window 10 L1's probe 5
-- (ops/logs/live.md "## L1", runtime/probes/v010w10/ans5.json): Lae'zel's cage, 6 answers in NodeSelection, answer 3
-- an ActiveRoll Persuasion with a Tag REALLY_SHADOWHEART value=false flag (screen: [PERSUASION]), answer 4 an
-- ActiveRoll Deception with Tag PALADIN value=true (screen: [PALADIN] [DECEPTION]).
-- Run: tools/dev/test_state_dialog.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local P1, P2 = "p1", "p2"
local handlers, addr_of, by_addr, inst_of = {}, {}, {}, {}
local loca = { hPaladin = "Paladin", hShadowheart = "Shadowheart" }
local tags = { gPaladin = { Name = "PALADIN", DisplayName = { Handle = { Handle = "hPaladin" } } },
    gShadowheart = { Name = "REALLY_SHADOWHEART", DisplayName = { Handle = { Handle = "hShadowheart" } } } }
local next_addr = 4096

Ext = {
    _Internal = {
        RawGetMetatable = function(o) return { __bg3leIdentity = "o:" .. tostring(addr_of[o]) .. ":" } end,
        PointedObject = function(a) return by_addr[a] end,
    },
    StaticData = { Get = function(g, kind) return kind == "Tag" and tags[g] or nil end },
    Loca = { GetTranslatedString = function(h) return loca[h] end },
}
BG3AI = {
    RPC = { register = function(name, fn) handlers[name] = fn end },
    Visibility = { party = function() return { P1, P2 } end },
    Guard = { dialogue_instance = function(u) return inst_of[u] end },
}
dofile(LUA .. "/Server/StateDialog.lua")
local op = handlers["state.dialogue_tags"]

local function node(uuid, t)
    t.UUID = uuid
    addr_of[t], by_addr[next_addr] = next_addr, t
    next_addr = next_addr + 8
    return t
end
local function flags(kind, list) return { Flags = { [kind] = list } } end

local function cage()
    local n = {}
    n.q1 = node("q1", { ConstructorID = "TagQuestion",
        CheckFlags = { Flags = { Dialog = { { Uuid = "d", Value = false } },
            Global = { { Uuid = "g", Value = true } } } } })
    n.q2 = node("q2", { ConstructorID = "TagQuestion",
        CheckFlags = flags("Dialog", { { Uuid = "d", Value = false } }) })
    n.r3 = node("r3", { ConstructorID = "ActiveRoll", Roll = { Skill = "Persuasion", Ability = "Charisma" },
        CheckFlags = flags("Tag", { { Uuid = "gShadowheart", Value = false } }) })
    n.r4 = node("r4", { ConstructorID = "ActiveRoll", Roll = { Skill = "Deception", Ability = "Charisma" },
        CheckFlags = flags("Tag", { { Uuid = "gPaladin", Value = true } }) })
    n.q5 = node("q5", { ConstructorID = "TagQuestion" })
    n.q6 = node("q6", { ConstructorID = "TagQuestion" })
    -- the current node's 12 children (6 presented + 6 filtered variants) are never read
    n.cur = node("cur", { ConstructorID = "TagAnswer", Children = { "q1", "q2", "r3", "r4", "q5", "q6", "x1", "x2",
        "x3", "x4", "x5", "x6" } })
    n.x1 = node("x1", { ConstructorID = "ActiveRoll", Roll = { Skill = "Intimidation" },
        CheckFlags = flags("Tag", { { Uuid = "gPaladin", Value = true } }) })
    local sel = {}
    for i, k in ipairs({ "q1", "q2", "r3", "r4", "q5", "q6" }) do sel[i] = { Node = n[k], LineId = -65536 } end
    return { DialogId = 215, ActiveDialog = { Nodes = n }, NodeSelection = sel }, n
end

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    inst_of = {}
    local ok, err = pcall(fn)
    if not ok then failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, msg)
    if a ~= b then error((msg or "") .. " expected " .. tostring(b) .. ", got " .. tostring(a), 2) end
end
local function keys(t)
    local ks = {}
    for k in pairs(t) do ks[#ks + 1] = k end
    table.sort(ks)
    return table.concat(ks, ",")
end

test("the cage: Paladin (true) is a label, Shadowheart (false) never; the rolls' skills", function()
    inst_of[P1] = cage()
    local r = op({})
    eq(r.why, nil)
    eq(#r.answers, 6, "NodeSelection only, not the 12 children")
    for i, a in ipairs(r.answers) do
        eq(a.idx, i - 1)
        eq(keys(a), a.roll and "idx,roll,tags" or "idx,tags", "keys")
    end
    eq(r.answers[1].roll, nil); eq(#r.answers[1].tags, 0, "Global true is no Tag flag")
    eq(r.answers[3].roll, "Persuasion"); eq(#r.answers[3].tags, 0, "value=false: no label")
    eq(r.answers[4].roll, "Deception"); eq(r.answers[4].tags[1], "Paladin"); eq(#r.answers[4].tags, 1)
end)

test("a Jump in the selection: one hop to the ActiveRoll it targets", function()
    local i, n = cage()
    n.j = node("j", { ConstructorID = "Jump", JumpTarget = "r4" })
    i.NodeSelection[2] = { Node = n.j }
    inst_of[P1] = i
    local a = op({}).answers[2]
    eq(a.roll, "Deception"); eq(a.tags[1], "Paladin")
end)

test("an ability check (no skill) is labelled with the ability", function()
    local i, n = cage()
    n.r3.Roll = { Skill = "None", Ability = "Strength" }
    inst_of[P1] = i
    eq(op({}).answers[3].roll, "Strength")
end)

test("a tag whose name doesn't read refuses the whole answer list", function()
    local i, n = cage()
    n.r4.CheckFlags = flags("Tag", { { Uuid = "gUnknown", Value = true } })
    inst_of[P1] = i
    local r = op({})
    eq(r.answers, nil); eq(r.why, "answer 4: tag name unread")
end)

test("no open dialogue, and two different ones in the party", function()
    eq(op({}).why, "no open dialogue")
    inst_of[P1] = cage()
    local other = cage()
    other.DialogId = 216
    inst_of[P2] = other
    eq(op({}).why, "more than one open dialogue in the party")
    inst_of[P2] = inst_of[P1]
    eq(#op({}).answers, 6, "both members in the same dialogue")
end)

print(("%d/%d passed"):format(count - failures, count))
if failures > 0 then os.exit(1) end
