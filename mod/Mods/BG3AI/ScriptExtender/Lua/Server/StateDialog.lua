-- State, part 4 (State.lua loads the parts in order): state.dialogue_tags, the skill/ability and class tags the
-- dialogue's answer list shows as "[PALADIN] [DECEPTION] " runs. The client can't read them: the tagged rows carry
-- one extra ls.LSRun per tag, but the runs' Text reads nil (v010 window 10 L1, ans5-client.json; probes 1-2: the tag
-- params' VMs hold numbers only). The bridge merges these labels by answer index behind count checks (reporting.py
-- _merge_dialogue_tags); REVIEW-10 (ops/logs/REVIEW-10-dialogue-tags.md) is the design.
--
-- Read from the game's own selection: the party member's open dialog instance (Guard.dialogue_instance, the same
-- rules as the dialogue guard), its NodeSelection (the answers presented, after conditions, in the order the screen
-- shows them: AnswerIdx 0.. on the client is the index into it), one Jump/Alias hop to the node that carries the
-- roll. Never the current node's Children: they hold the variants the game filtered out (window 9: 12 children, 6
-- answers shown).
--
-- Label rule, from window 10 L1's probe 5 (ans5.json, Lae'zel's cage, the screen in ans5-answers.txt):
--   answer 3: ActiveRoll, Roll.Skill Persuasion, CheckFlags Tag REALLY_SHADOWHEART value=false -> [PERSUASION]
--   answer 4: ActiveRoll, Roll.Skill Deception, CheckFlags Tag PALADIN value=true            -> [PALADIN] [DECEPTION]
--   answers 1, 2, 5, 6: TagQuestion, Dialog/Global flags or none                              -> no tag
-- So: the Tag-kind CheckFlags whose value is true (the tag's DisplayName), then the roll's Skill (its Ability when
-- the roll has no skill: an ability check). A value=false flag is a condition the player never sees and is never a
-- label. Only an ActiveRoll node has a roll (a RollResult node read as one is garbage, window 9 W4).
-- Output per answer: exactly idx, roll, tags. No DC, advantage, node ids or texts (fairness: the screen shows the
-- tag names only; the DC shows on the roll screen).
local RPC = BG3AI.RPC
local V = BG3AI.Visibility

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end
local function get(o, k) return try(function() return o[k] end) end

-- bg3le reads a dialog node through its base layout (no node polymorphism): the subclass fields (Roll,
-- JumpTarget, SourceNode) read through the same address as the subclass (probe 5's way).
local function typed(o, class)
    local I = Ext._Internal
    local m = try(I.RawGetMetatable, o)
    local id = m and m.__bg3leIdentity
    if type(id) == "function" then id = try(id, o) end
    if type(id) ~= "string" then return nil end
    local addr = tonumber(id:match("^o:(%d+):") or "") or tonumber(id:match("^p:(%x+)$") or "", 16)
    return addr and try(I.PointedObject, addr, class) or nil
end

-- A list proxy's values in key order.
local function values(c)
    local keys, out = {}, {}
    try(function() for k in pairs(c) do keys[#keys + 1] = k end end)
    table.sort(keys)
    for _, k in ipairs(keys) do out[#out + 1] = get(c, k) end
    return out
end

local function loca(ts)
    if type(ts) == "string" then return try(Ext.Loca.GetTranslatedString, ts) or nil end
    local h = get(get(ts, "Handle"), "Handle") or get(ts, "Handle")
    return type(h) == "string" and try(Ext.Loca.GetTranslatedString, h) or nil
end

local function tagName(uuid)
    local t = try(Ext.StaticData.Get, uuid, "Tag")
    local s = t and loca(get(t, "DisplayName"))
    return s ~= nil and s ~= "" and s or nil
end

-- {roll, tags} of one presented answer node, or nil, why.
local function labels(nodes, node)
    local cid = get(node, "ConstructorID")
    if cid == "Jump" or cid == "Alias" then
        local t = cid == "Jump" and get(typed(node, "dlg::JumpNode"), "JumpTarget")
            or get(typed(node, "dlg::AliasNode"), "SourceNode")
        node = t and get(nodes, t)
        if node == nil then return nil, cid .. " target unread" end
        cid = get(node, "ConstructorID")
    end
    local out = { tags = {} }
    if cid == "ActiveRoll" then
        local r = get(typed(node, "dlg::ActiveRollNode"), "Roll")
        local skill = get(r, "Skill")
        out.roll = (type(skill) == "string" and skill ~= "" and skill ~= "None") and skill or get(r, "Ability")
        if type(out.roll) ~= "string" then return nil, "roll unread" end
    end
    for kind, arr in pairs(get(get(node, "CheckFlags"), "Flags") or {}) do
        if kind == "Tag" then
            for _, f in ipairs(values(arr)) do
                if get(f, "Value") == true then
                    local n = tagName(get(f, "Uuid"))
                    if not n then return nil, "tag name unread" end
                    out.tags[#out.tags + 1] = n
                end
            end
        end
    end
    return out
end

-- state.dialogue_tags -> {answers = {{idx, roll, tags}, ...}} for the party's open dialogue, else {why = ...}.
RPC.register("state.dialogue_tags", function()
    local G = BG3AI.Guard
    local inst, id
    for _, u in ipairs(V.party()) do
        local i = G.dialogue_instance(u)
        local d = i and get(i, "DialogId")
        if d and d ~= id then
            if id then return { why = "more than one open dialogue in the party" } end
            inst, id = i, d
        end
    end
    if not inst then return { why = "no open dialogue" } end
    local nodes = get(get(inst, "ActiveDialog"), "Nodes")
    local answers = {}
    for i, s in ipairs(values(get(inst, "NodeSelection"))) do
        local uuid = get(get(s, "Node"), "UUID")
        local node = uuid and get(nodes, uuid)
        if node == nil then return { why = "answer " .. i .. " node unread" } end
        local l, why = labels(nodes, node)
        if not l then return { why = "answer " .. i .. ": " .. why } end
        answers[#answers + 1] = { idx = i - 1, roll = l.roll, tags = l.tags }
    end
    return { answers = answers }
end)
