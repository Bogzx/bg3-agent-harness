-- One-shot live probe 3 (SERVER context), after tag probes 1-2 (v010w6/tags-probe.json, v010w7/tags-probe2.json):
-- the answer VMs' Tag params carry no readable label (named reads all empty; only roll modifiers). The game data
-- behind "[PERSUASION]" is the answer node itself: an ActiveRollNode's Roll {RollType, Skill, Ability,
-- DifficultyClass} (bg3se GameDefinitions/Dialog.h), reached from the server's dialog instance (CurrentNode.UUID ->
-- ActiveDialog.Nodes -> Children). Class tags ("[PALADIN]") would be Tag-type CheckFlags on the answer node, named
-- by Ext.StaticData.Get(<tag guid>, "Tag"). This reads all of that for every live, non-automated dialog, and pairs
-- each child with its text (to match the answers on screen). bg3le may read a node pointer as the base
-- dlg::DialogNode (no Roll): then the probe re-reads the node's address as dlg::ActiveRollNode / PassiveRollNode.
-- Run at a node with a skill check showing (Lae'zel's cage: "[PERSUASION]", "[PALADIN] [DECEPTION]"):
--   call.py ROLLN lua:server "$(cat tools/dev/live_v010/dlg_roll_nodes_probe.lua)"
local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end
local function get(o, k) return try(function() return o[k] end) end
local function plain(v)
    local t = type(v)
    if v == nil or t == "string" or t == "number" or t == "boolean" then return v end
    return tostring(v)
end
local function list(c, max)
    local out = {}
    try(function()
        for _, v in pairs(c) do
            out[#out + 1] = v
            if #out >= (max or 64) then break end
        end
    end)
    return out
end
local function typeOf(o)
    return try(Ext.Types.GetObjectType, o) or try(function() return Ext.Types.GetTypeName(o) end)
end
local function addressOf(o)
    local m = try(Ext._Internal.RawGetMetatable, o)
    local id = m and m.__bg3leIdentity
    if type(id) == "function" then id = try(id, o) end
    if type(id) ~= "string" then return nil, id end
    local d = id:match("^o:(%d+):$")
    if d then return tonumber(d), id end
    local h = id:match("^p:(%x+)$")
    if h then return tonumber(h, 16), id end
    return nil, id
end
local function loca(ts)
    local h = get(get(ts, "Handle"), "Handle") or get(ts, "Handle")
    if type(h) ~= "string" then return nil end
    return try(Ext.Loca.GetTranslatedString, h) or h
end
local function tagInfo(g)
    local t = try(Ext.StaticData.Get, g, "Tag")
    if not t then return { guid = g } end
    return { guid = g, name = plain(get(t, "Name")),
        display = loca(get(t, "DisplayName")) or plain(get(t, "DisplayName")),
        categories = plain(get(t, "Categories")) }
end
local function readRoll(roll)
    if roll == nil then return nil end
    local r = {}
    for _, k in ipairs({ "RollType", "Skill", "Ability", "Advantage", "DifficultyClass", "RollTargetSpeaker" }) do
        r[k] = plain(get(roll, k))
    end
    return r
end
local function nodeText(n)
    for _, tt in ipairs(list(get(n, "TaggedTexts"), 4)) do
        for _, line in ipairs(list(get(tt, "Lines"), 4)) do
            local s = loca(get(line, "TagText"))
            if s then return s end
        end
    end
    return nil
end
local function flags(fc)
    local out = {}
    try(function()
        for kind, arr in pairs(get(fc, "Flags") or {}) do
            for _, f in ipairs(list(arr, 16)) do
                local e = { kind = plain(kind), uuid = plain(get(f, "Uuid")), type = plain(get(f, "Type")),
                    value = plain(get(f, "Value")) }
                if (e.type or e.kind) == "Tag" or kind == "Tag" then e.tag = tagInfo(e.uuid) end
                out[#out + 1] = e
            end
        end
    end)
    return out
end
local function node(nodes, uuid)
    local n = get(nodes, uuid)
    if n == nil then return { uuid = uuid, missing = true } end
    local cid = plain(get(n, "ConstructorID"))
    local addr, ident = addressOf(n)
    local out = { uuid = uuid, constructor = cid, type = plain(typeOf(n)), ident = plain(ident), text = nodeText(n),
        tags = {}, check_flags = flags(get(n, "CheckFlags")), roll = readRoll(get(n, "Roll")),
        source = plain(get(n, "SourceNode")) }
    for _, g in ipairs(list(get(n, "Tags"), 8)) do out.tags[#out.tags + 1] = tagInfo(plain(g)) end
    if out.roll == nil and addr and type(cid) == "string" and cid:find("Roll", 1, true) then
        for _, cls in ipairs({ "dlg::ActiveRollNode", "dlg::PassiveRollNode" }) do
            local v = try(Ext._Internal.PointedObject, addr, cls)
            local r = v and readRoll(get(v, "Roll"))
            if r then out.roll, out.roll_via = r, cls break end
        end
    end
    out.children = {}
    for _, c in ipairs(list(get(n, "Children"), 32)) do out.children[#out.children + 1] = plain(c) end
    return out
end

local dm = try(Ext.Utils.GetDialogManager)
if not dm then return Ext.Json.Stringify({ error = "no dialog manager (server context?)" }) end
local res = {}
try(function()
    for k, inst in pairs(get(dm, "Dialogs") or {}) do
        if get(inst, "DialogId") == k and get(inst, "AutomatedDialog") ~= true then
            local cur = plain(get(get(inst, "CurrentNode"), "UUID"))
            local dlg = get(inst, "ActiveDialog")
            local nodes = get(dlg, "Nodes")
            local e = { id = k, res = plain(get(inst, "DialogResourceUUID")), state = plain(get(inst, "State")),
                file = plain(get(dlg, "FileName")), current = cur, selection = {} }
            for _, s in ipairs(list(get(inst, "NodeSelection"), 16)) do
                e.selection[#e.selection + 1] = { node = plain(get(get(s, "Node"), "UUID")),
                    line = plain(get(s, "LineId")) }
            end
            if cur and nodes then
                e.node = node(nodes, cur)
                e.answers = {}
                for _, c in ipairs(e.node.children) do e.answers[#e.answers + 1] = node(nodes, c) end
            end
            res[#res + 1] = e
        end
    end
end)
return Ext.Json.Stringify(res)
