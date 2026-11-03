-- One-shot live probe 4 (SERVER context), after probe 3 (v010w8/roll-nodes-probe3.json = [], rolln-diag{1,2}.out):
-- the proxy Ext.Utils.GetDialogManager().Dialogs listed one entry, key 214 with DialogId -91782656 (0xFA878000: the
-- low half of a pointer, so the instance most likely starts with a vtable bg3se's DialogInstance doesn't declare and
-- every proxy field is 8 bytes off), while the party's DialogState.DialogId was 215 and Dialogs[215] nil.
-- This reads the live instance several ways in one load and reports which one reaches the answer nodes' DialogRoll:
--   W0 what bg3le thinks the layouts are (ObjectFields / ObjectFieldAddress offsets) and the party's dialog ids;
--   W1 raw walk of the manager's Dialogs map at +0x20 (LegacyRefMap: {u32 count, u32 hashSize, Node** table},
--      Node {Next@0, Key@8, Value@16}; the layout bg3le's own l_dialog_manager check walks at +0x30);
--   W2 every 8-byte offset of the manager's first 0x140 bytes tried as such a map: which one holds the party's id;
--   W3 each instance found (key = the party's id, or the first one) read as dlg::DialogInstance at its address and
--      at address + 8, then ActiveDialog -> Nodes[CurrentNode] -> Children -> each answer's Roll (re-read as
--      dlg::ActiveRollNode / PassiveRollNode by address when the node reads as the base class);
--   W4 the manager's CachedDialogs (static dialog data): each dialog with RefCount > 0, its roll nodes' text,
--      Skill and Ability (pairs with the answers on screen by text even without the instance).
-- Read-only (Peek, proxies, pcall everywhere). Run at a node with a skill check showing (Lae'zel's cage):
--   call.py ROLL4 lua:server "$(cat tools/dev/live_v010/dlg_roll_nodes_probe4.lua)"
-- MODE (set by sed in the w9 list): "safe" = W0-W2 only (Peek, which reads through bg3le's safe_read), "w4" = W0-W2 +
-- W4 (proxies on the static dialogs), "w3" = W0-W2 + W3 (proxies at guessed instance addresses: the riskiest, last).
local MODE = "all"
local I = Ext._Internal
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
local function peek(a, w) return a and try(I.Peek, a, w or 8) end
local function i32(a)
    local v = peek(a, 4)
    if v and v >= 0x80000000 then v = v - 0x100000000 end
    return v
end
local function isPtr(v) return type(v) == "number" and v > 0x10000 and v < 0x800000000000 and v % 8 == 0 end
local function hex(v) return v and string.format("0x%x", v) or nil end
local function qwords(a, n)
    local out = {}
    for k = 0, n - 1 do out[#out + 1] = hex(peek(a + 8 * k)) or "?" end
    return out
end
local function addressOf(o)
    local m = try(I.RawGetMetatable, o)
    local id = m and m.__bg3leIdentity
    if type(id) == "function" then id = try(id, o) end
    if type(id) ~= "string" then return nil end
    local d = id:match("^o:(%d+):")
    if d then return tonumber(d) end
    local h = id:match("^p:(%x+)$")
    return h and tonumber(h, 16) or nil
end
local function loca(ts)
    local h = get(get(ts, "Handle"), "Handle") or get(ts, "Handle")
    if type(h) ~= "string" then return nil end
    return try(Ext.Loca.GetTranslatedString, h) or h
end
local function nodeText(n)
    for _, tt in ipairs(list(get(n, "TaggedTexts"), 4)) do
        for _, line in ipairs(list(get(tt, "Lines"), 4)) do
            local s = loca(get(line, "TagText"))
            if s then return s:sub(1, 80) end
        end
    end
    return nil
end
local function readRoll(roll)
    if roll == nil then return nil end
    local r = {}
    for _, k in ipairs({ "RollType", "Skill", "Ability", "Advantage" }) do r[k] = plain(get(roll, k)) end
    return next(r) and r or nil
end
local function tagName(g)
    local t = try(Ext.StaticData.Get, g, "Tag")
    return t and (loca(get(t, "DisplayName")) or plain(get(t, "Name"))) or nil
end
local function node(nodes, uuid)
    local n = get(nodes, uuid)
    if n == nil then return { uuid = uuid, missing = true } end
    local cid = plain(get(n, "ConstructorID"))
    local out = { uuid = uuid, constructor = cid, text = nodeText(n), roll = readRoll(get(n, "Roll")), tag_flags = {} }
    local addr = addressOf(n)
    out.addr = hex(addr)
    if out.roll == nil and addr and type(cid) == "string" and cid:find("Roll", 1, true) then
        for _, cls in ipairs({ "dlg::ActiveRollNode", "dlg::PassiveRollNode" }) do
            local r = readRoll(get(try(I.PointedObject, addr, cls), "Roll"))
            if r then out.roll, out.roll_via = r, cls break end
        end
    end
    try(function()
        for kind, arr in pairs(get(get(n, "CheckFlags"), "Flags") or {}) do
            for _, f in ipairs(list(arr, 8)) do
                local u = plain(get(f, "Uuid"))
                out.tag_flags[#out.tag_flags + 1] = { kind = plain(kind), uuid = u, value = plain(get(f, "Value")),
                    tag = tagName(u) }
            end
        end
    end)
    out.children = {}
    for _, c in ipairs(list(get(n, "Children"), 32)) do out.children[#out.children + 1] = plain(c) end
    return out
end
-- A LegacyRefMap at `at`: {count, hashSize, table, keys = {key = value address}}; nil when it doesn't look like one.
local function refMap(at, maxNodes)
    local count, size, tbl = peek(at, 4), peek(at + 4, 4), peek(at + 8)
    if not (count and size and isPtr(tbl)) or count > 4096 or size == 0 or size > 8192 then return nil end
    local m = { at = hex(at), count = count, size = size, table = hex(tbl), keys = {}, n = 0 }
    for b = 0, size - 1 do
        local nd = peek(tbl + 8 * b)
        local guard = 0
        while isPtr(nd) and guard < 64 do
            m.n = m.n + 1
            if m.n > (maxNodes or 256) then return m end
            m.keys[#m.keys + 1] = { key = i32(nd + 8), value = hex(peek(nd + 16)), v = peek(nd + 16) }
            nd, guard = peek(nd), guard + 1
        end
    end
    return m
end

local out = { w0 = {}, w1 = {}, w2 = {}, w3 = {}, w4 = {} }
local A = try(I.DialogManager)
out.manager = hex(A)
if not A then return Ext.Json.Stringify({ error = "no dialog manager (server context?)" }) end

-- W0: the party's dialog ids and bg3le's idea of the layouts.
local want = {}
for _, row in pairs(try(function() return Osi.DB_Players:Get(nil) end) or {}) do
    local id = try(function() return Ext.Entity.Get(row[1]).DialogState.DialogId end)
    out.w0[#out.w0 + 1] = { who = row[1], dialog_id = id }
    if id and id ~= 0 then want[id] = true end
end
local fieldsOf = {}
for _, cls in ipairs({ "dlg::DialogManager", "dlg::DialogInstance", "dlg::ActiveRollNode", "dlg::DialogRoll" }) do
    fieldsOf[cls] = try(I.ObjectFields, cls)
end
out.w0_fields = fieldsOf
out.w0_offsets = {}
for _, f in ipairs({ "Dialogs", "FlagDescriptions", "NextInstanceId", "CachedDialogs" }) do
    local a = try(I.ObjectFieldAddress, A, "dlg::DialogManager", f)
    out.w0_offsets["DialogManager." .. f] = a and hex(a - A)
end
out.w0_next_instance_id = plain(get(try(Ext.Utils.GetDialogManager), "NextInstanceId"))
out.w0_manager_qwords = qwords(A, 40)

-- W1: the Dialogs map at +0x20, raw.
local found = {}
local m1 = refMap(A + 0x20)
if m1 then
    for _, k in ipairs(m1.keys) do
        k.i32_at_0, k.i32_at_8 = i32(k.v), i32(k.v + 8)
        k.head = qwords(k.v, 6)
        if want[k.key] or want[k.i32_at_8] or want[k.i32_at_0] then
            found[#found + 1] = { via = "W1", key = k.key, v = k.v }
        end
    end
end
out.w1 = m1 or { none = true }

-- W2: any map in the manager's first 0x140 bytes holding one of the party's ids.
for off = 0, 0x138, 8 do
    local m = refMap(A + off, 64)
    if m and m.n > 0 then
        local hit
        for _, k in ipairs(m.keys) do
            if want[k.key] then
                hit = k
                local dup = false
                for _, f in ipairs(found) do dup = dup or f.v == k.v end
                if not dup then found[#found + 1] = { via = "W2+" .. hex(off), key = k.key, v = k.v } end
            end
        end
        local keys = {}
        for j = 1, math.min(#m.keys, 8) do keys[j] = m.keys[j].key end
        out.w2[#out.w2 + 1] = { off = hex(off), count = m.count, size = m.size, n = m.n, keys = keys, hit = hit ~= nil }
    end
end
if #found == 0 and m1 and m1.keys[1] then
    found[1] = { via = "W1 first (no id match)", key = m1.keys[1].key, v = m1.keys[1].v }
end

-- W3: each instance, read at its address and 8 bytes later.
for _, f in ipairs((MODE == "all" or MODE == "w3") and found or {}) do
    for _, shift in ipairs({ 0, 8 }) do
        local inst = try(I.PointedObject, f.v + shift, "dlg::DialogInstance")
        local cur = plain(get(get(inst, "CurrentNode"), "UUID"))
        local dlg = get(inst, "ActiveDialog")
        local nodes = get(dlg, "Nodes")
        local e = { via = f.via, key = f.key, addr = hex(f.v), shift = shift, dialog_id = plain(get(inst, "DialogId")),
            res = plain(get(inst, "DialogResourceUUID")), auto = plain(get(inst, "AutomatedDialog")), current = cur,
            file = plain(get(dlg, "FileName")) }
        if cur and nodes then
            e.node = node(nodes, cur)
            e.answers = {}
            for _, c in ipairs(e.node.children) do e.answers[#e.answers + 1] = node(nodes, c) end
        end
        out.w3[#out.w3 + 1] = e
    end
end

-- W4: the static dialogs in use (RefCount > 0) and their roll nodes.
local dm = try(Ext.Utils.GetDialogManager)
local cached = (MODE == "all" or MODE == "w4") and list(get(dm, "CachedDialogs"), 2000) or {}
out.w4_cached = #cached
for _, d in ipairs(cached) do
    local rc = get(d, "RefCount")
    if type(rc) == "number" and rc > 0 then
        local e = { file = plain(get(d, "FileName")), uuid = plain(get(d, "UUID")), refcount = rc, rolls = {},
            nodes = 0 }
        try(function()
            for uuid, n in pairs(get(d, "Nodes") or {}) do
                e.nodes = e.nodes + 1
                local cid = get(n, "ConstructorID")
                if type(cid) == "string" and cid:find("Roll", 1, true) and #e.rolls < 40 then
                    local r = node(get(d, "Nodes"), uuid)
                    r.children = nil
                    e.rolls[#e.rolls + 1] = r
                end
            end
        end)
        out.w4[#out.w4 + 1] = e
        if #out.w4 >= 12 then break end
    end
end
-- the raw addresses are for reading the dump, not for JSON numbers
if m1 then for _, k in ipairs(m1.keys) do k.v = nil end end
out.mode = MODE
return Ext.Json.Stringify(out)
