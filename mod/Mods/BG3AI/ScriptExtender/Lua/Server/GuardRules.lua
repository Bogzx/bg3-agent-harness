-- Part of Server/Guard.lua (split for the 1000-line cap): a spell's own rules that a harness cast skips.
--
-- REVIEW-5 F1/F3: a cast the harness orders keeps IgnoreCastChecks and IgnoreTargetChecks (window 6: dropping them at
-- the cast state refuses every cast, the control too), so the engine checks neither the stats Requirements /
-- RequirementConditions nor TargetConditions. Live (window 6) Jump went through while ENSNARED. A human's hotbar
-- greys such an action or the cursor refuses the target. Here they are read from the stats and refused before the
-- order, with the plain game fact:
--   Requirements           Combat / !Combat / !Immobile
--   RequirementConditions  on the caster: HasStatus, Immobilized, WearingArmor, Combat, Tagged
--   TargetConditions       on the target: Tagged, Dead, IsDowned, Ally, Enemy, Character, Item, Self, Summon,
--                          HasStatus, CanShoveWeight
-- Three-valued: a term that can't be read (an unknown function, an argument form not handled, a query that answers
-- nothing) is unknown; only a condition that is false whatever the unknown terms are is refused. Unknown terms are
-- logged once per spell and term, never refused.

local G = BG3AI.Guard

G.rules_unread = G.rules_unread or {} -- "spell|term" -> true: logged once

local function lower(s) return tostring(s):lower():gsub("_", " ") end

------------------------------------------------------------------------
-- The condition language: and / or / not, parentheses, Name(args) with 'string', number or context.X arguments.
------------------------------------------------------------------------

local function tokens(src)
    local out, i, n = {}, 1, #src
    while i <= n do
        local c = src:sub(i, i)
        if c:match("%s") then
            i = i + 1
        elseif c == "(" or c == ")" or c == "," then
            out[#out + 1] = { k = c }
            i = i + 1
        elseif c == "'" or c == '"' then
            local j = src:find(c, i + 1, true)
            if not j then return nil end
            out[#out + 1] = { k = "str", v = src:sub(i + 1, j - 1) }
            i = j + 1
        else
            local w = src:match("^[%w_%.%-]+", i)
            if not w then return nil end -- an operator this reader doesn't know (+, >=, ...)
            out[#out + 1] = (w == "and" or w == "or" or w == "not") and { k = w } or { k = "id", v = w }
            i = i + #w
        end
    end
    return out
end

-- AST: {op="and"|"or", a, b} | {op="not", a} | {op="call", name, args} | {op="unknown"}
local function parse(src)
    local t = tokens(src)
    if not t then return { op = "unknown" } end
    local p = 1
    local function peek() return t[p] and t[p].k end
    local expr
    local function primary()
        if peek() == "(" then
            p = p + 1
            local e = expr()
            if peek() ~= ")" then error("expected )") end
            p = p + 1
            return e
        end
        local tk = t[p]
        if not tk or tk.k ~= "id" then error("expected a term") end
        p = p + 1
        local node = { op = "call", name = tk.v, args = {} }
        if peek() == "(" then
            p = p + 1
            while peek() and peek() ~= ")" do
                local a = t[p]
                if a.k == "str" or a.k == "id" then node.args[#node.args + 1] = a else error("bad argument") end
                p = p + 1
                if peek() == "," then p = p + 1 end
            end
            if peek() ~= ")" then error("expected )") end
            p = p + 1
        end
        return node
    end
    local function unary()
        if peek() == "not" then
            p = p + 1
            return { op = "not", unary() }
        end
        return primary()
    end
    local function chain(kind, sub)
        return function()
            local e = sub()
            while peek() == kind do
                p = p + 1
                e = { op = kind, e, sub() }
            end
            return e
        end
    end
    expr = chain("or", chain("and", unary))
    local ok, ast = pcall(expr)
    if not ok or p <= #t then return { op = "unknown" } end
    return ast
end

------------------------------------------------------------------------
-- Terms
------------------------------------------------------------------------

local function yes(x)
    if x == nil then return nil end
    return G.b(x)
end

G._tag_ids = G._tag_ids or nil -- tag name -> uuid (the static Tag resources)
local function tag_id(name)
    if G._tag_ids == nil then
        G._tag_ids = {}
        pcall(function()
            for _, id in ipairs(Ext.StaticData.GetAll("Tag")) do
                local r = Ext.StaticData.Get(id, "Tag")
                if r and r.Name then G._tag_ids[tostring(r.Name)] = tostring(id) end
            end
        end)
    end
    return G._tag_ids[name]
end

function G.rule_has_status(u, id)
    if id:sub(1, 3) ~= "SG_" then return yes(G.q("HasActiveStatus", u, id)) end
    local found, read = false, false
    pcall(function()
        for _, s in pairs(Ext.Entity.Get(u).ServerCharacter.StatusManager.Statuses) do
            read = true
            local st = Ext.Stats.Get(tostring(s.StatusId))
            for _, g in ipairs(st and st.StatusGroups or {}) do
                if tostring(g) == id then found = true end
            end
        end
    end)
    if found then return true end
    return read and false or nil
end

local function armour(u)
    local ok, r = pcall(function()
        local item = G.q("GetEquippedItem", u, "Breast")
        if item == nil then return false end
        local id = Ext.Entity.Get(item).Data.StatsId
        local t = tostring(Ext.Stats.Get(id).ArmorType or "None")
        return t ~= "None" and t ~= "Cloth"
    end)
    if ok then return r end
    return nil
end

-- The value of one term (true / false / nil = unknown) and its plain words when it is true / false.
-- ctx = {source, target, subject (the default entity: the target for TargetConditions, the caster otherwise)}
local function term(node, ctx)
    local a = node.args
    local function who(i)
        local x = a[i]
        if x == nil then return ctx.subject end
        if x.k == "id" and x.v == "context.Source" then return ctx.source end
        if x.k == "id" and x.v == "context.Target" then return ctx.target end
        return nil
    end
    local function str1() return #a >= 1 and a[1].k == "str" and a[1].v or nil end
    local name = node.name
    local subj = ctx.subject == ctx.target and "the target" or "the caster"
    if name == "Tagged" and str1() and #a <= 2 then
        local u, id = who(2), tag_id(str1())
        if not u or not id then return nil end
        local v = yes(G.q("IsTagged", u, id))
        return v, (u == ctx.target and "the target" or "the caster") .. " is " .. lower(str1()),
            (u == ctx.target and "the target" or "the caster") .. " is not " .. lower(str1())
    end
    if name == "HasStatus" and str1() and #a <= 2 then
        local u = who(2)
        if not u then return nil end
        local s = lower(str1():gsub("^SG_", ""))
        local w = u == ctx.target and "the target" or "the caster"
        return G.rule_has_status(u, str1()), w .. " has " .. s, w .. " doesn't have " .. s
    end
    if #a > 1 or (#a == 1 and not who(1)) then return nil end
    local u = who(1)
    if name == "Dead" then return yes(G.q("IsDead", u)), subj .. " is dead", subj .. " is not dead" end
    if name == "IsDowned" then return G.is_downed(u), subj .. " is downed", subj .. " is not downed" end
    if name == "Self" then
        if not ctx.target or #a > 0 then return nil end
        return G.same(ctx.source, ctx.target), "it is the caster itself", "it is not the caster"
    end
    if name == "Character" then return yes(G.q("IsCharacter", u)), "it is a character", "it is not a character" end
    if name == "Item" then return yes(G.q("IsItem", u)), "it is an item", "it is not an item" end
    if name == "Summon" then return yes(G.q("IsSummon", u)), "it is a summon", "it is not a summon" end
    if name == "Ally" and ctx.target and #a == 0 then
        return yes(G.q("IsAlly", ctx.source, ctx.target)), "it is an ally", "it is not an ally"
    end
    if name == "Enemy" and ctx.target and #a == 0 then
        return yes(G.q("IsEnemy", ctx.source, ctx.target)), "it is an enemy", "it is not an enemy"
    end
    if name == "Combat" and #a == 0 then
        return G.in_combat(ctx.source), "the caster is in combat", "the caster is not in combat"
    end
    if name == "Immobilized" then
        local blk = G.move_blocked(u)
        return blk ~= nil, subj .. " can't move (" .. tostring(blk) .. ")", subj .. " can move"
    end
    if name == "WearingArmor" then return armour(u), subj .. " wears armour", subj .. " wears no armour" end
    if name == "CanShoveWeight" and #a == 0 and ctx.target then
        local str = tonumber((G.q("GetAbility", ctx.source, "Strength")))
        local okw, w = pcall(function() return Ext.Entity.Get(ctx.target).Data.Weight end)
        if not str or not okw or tonumber(w) == nil then return nil end
        local mult = G.b(G.q("HasActiveStatus", ctx.source, "ENLARGE")) and 28.2
            or G.b(G.q("HasActiveStatus", ctx.source, "REDUCE")) and 5.1 or 12
        return str * mult >= tonumber(w) / 1000, "it is light enough to shove", "it is too heavy to shove"
    end
    return nil
end

-- Three-valued evaluation: value (true/false/nil) and, when false, the plain reason; unread collects unknown terms.
local function eval(node, ctx, unread)
    if node.op == "unknown" then
        unread[#unread + 1] = "(the whole condition)"
        return nil
    end
    if node.op == "not" then
        local v, whyf, whyt = eval(node[1], ctx, unread)
        if v == nil then return nil end
        return not v, whyt, whyf -- "not X" is false when X is true: X's true words
    end
    if node.op == "and" or node.op == "or" then
        local va, fa, ta = eval(node[1], ctx, unread)
        local vb, fb, tb = eval(node[2], ctx, unread)
        if node.op == "and" then
            if va == false then return false, fa end
            if vb == false then return false, fb end
            if va == nil or vb == nil then return nil end
            return true, nil, ta or tb
        end
        if va == true then return true, nil, ta end
        if vb == true then return true, nil, tb end
        if va == nil or vb == nil then return nil end
        return false, fa .. " and " .. fb
    end
    local v, when_true, when_false = term(node, ctx)
    if v == nil then
        unread[#unread + 1] = node.name
        return nil
    end
    return v, when_false, when_true
end

-- {value, why (when false), unread terms} of a condition string.
function G.rule_eval(src, ctx)
    local unread = {}
    if src == nil or tostring(src):match("^%s*$") then return true, nil, unread end
    local v, why = eval(parse(tostring(src)), ctx, unread)
    return v, why, unread
end

local function log_unread(spell, what, unread)
    for _, t in ipairs(unread) do
        local k = spell .. "|" .. what .. "|" .. t
        if not G.rules_unread[k] then
            G.rules_unread[k] = true
            Ext.Utils.PrintWarning("[BG3AI] guard rules: " .. spell .. " " .. what .. ": term " .. t
                .. " not read (not refused on it)")
        end
    end
end

-- Requirements as {name, negated} pairs (a "Combat;!Immobile" string or bg3se's list of {Requirement, Not}).
local function requirements(st)
    local ok, v = pcall(function() return st.Requirements end)
    local out = {}
    if not ok or v == nil then return out end
    if type(v) == "table" then
        for _, r in ipairs(v) do
            local okr, name, neg = pcall(function() return tostring(r.Requirement), r.Not end)
            if okr then out[#out + 1] = { name, G.b(neg) } end
        end
    else
        for w in tostring(v):gmatch("[^;,%s]+") do
            local neg = w:sub(1, 1) == "!"
            out[#out + 1] = { neg and w:sub(2) or w, neg }
        end
    end
    return out
end

-- Refuses a cast the game's rules forbid (G.refuse), with the plain game fact. target: a uuid or nil (a position).
-- RULES_OFF: probe only (tools/dev/probe_cast_checks.py measures the engine's own verdict).
G.RULES_OFF = G.RULES_OFF or false
function G.check_rules(char, spell, st, target)
    if G.RULES_OFF then return end
    local name = G.spell_facts(spell)
    name = name and name.name or spell
    for _, r in ipairs(requirements(st)) do
        local req, neg = r[1], r[2]
        if req == "Combat" and not neg and not G.in_combat(char) then
            G.refuse(name .. " can only be used in combat")
        elseif req == "Combat" and neg and G.in_combat(char) then
            G.refuse(name .. " can't be used in combat")
        elseif req == "Immobile" and neg then
            local blk = G.move_blocked(char)
            if blk then G.refuse(name .. " can't be used while the caster can't move (" .. tostring(blk) .. ")") end
        end
    end
    local function field(k)
        local ok, v = pcall(function() return st[k] end)
        return ok and v or nil
    end
    local v, why, unread = G.rule_eval(field("RequirementConditions"), { source = char, target = target,
        subject = char })
    log_unread(spell, "RequirementConditions", unread)
    if v == false then G.refuse(name .. " can't be used now: " .. why) end
    if target then
        -- a two-step spell (Revivify, Dimension Door) checks its first pick against OriginTargetConditions; its
        -- TargetConditions are the second pick's (the landing)
        local which = tostring(field("SpellType") or "") == "Teleportation" and "OriginTargetConditions"
            or "TargetConditions"
        v, why, unread = G.rule_eval(field(which), { source = char, target = target, subject = target })
        log_unread(spell, which, unread)
        if v == false then G.refuse(name .. " can't target that: " .. why) end
    end
end
