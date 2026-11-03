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
G.rules_last = G.rules_last or {} -- caster guid -> {spell, terms}: the unread terms of its last rule check

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
        local list = Ext.Entity.Get(u).ServerCharacter.StatusManager.Statuses
        read = list ~= nil -- an empty list is read: no status, so in no group
        for _, s in pairs(list) do
            local st = Ext.Stats.Get(tostring(s.StatusId))
            for _, g in ipairs(st and st.StatusGroups or {}) do
                if tostring(g) == id then found = true end
            end
        end
    end)
    if found then return true end
    if read then return false end -- ("read and false or nil" was always nil: groups never read as off)
    return nil
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

-- Any of these statuses or status groups (thoth HasAnyStatus): true if one is on, false if all read off.
local function any_status(u, ids)
    local unknown = false
    for _, id in ipairs(ids) do
        local v = G.rule_has_status(u, id)
        if v == true then return true end
        if v == nil then unknown = true end
    end
    if unknown then return nil end
    return false
end

-- The status lists of the game's own helpers (Shared CommonConditions.khn).
G.HELPABLE = { "SG_Helpable_Condition", "DOWNED", "HAG_DOWNED", "SCL_DOWNED", "SG_Prone", "SG_Restrained", "PRONE",
    "SLEEPING", "SLEEP", "ENSNARING_STRIKE", "WEB", "BURNING", "HYPNOTIC_PATTERN", "COL_NIGHTSONG_SOULCAGE" }
G.CROWD_CONTROLLED = { "SG_Fleeing", "SG_Incapacitated", "SG_Stunned", "SG_Unconscious", "SG_Restrained" }

-- A bg3se bitfield as a set of names (it reads as a table of flag names, as CastOptions does live), or nil.
local function flag_set(v)
    if type(v) ~= "table" then return nil end
    local out = {}
    for _, n in ipairs(v) do out[tostring(n)] = true end
    return out
end

-- IsProficientWithEquippedWeapon: the character's Proficiency flags against the equipped weapons' ProficiencyGroup
-- flags (bg3se eoc::stats::proficiency::*). True if one equipped weapon shares a group; false only when every
-- weapon read and none does; nil when nothing reads.
function G.proficient_weapon(u)
    local okp, mine = pcall(function() return flag_set(Ext.Entity.Get(u).Proficiency.Flags) end)
    if not okp or not mine then return nil end
    local any_read = false
    for _, slot in ipairs({ "Melee Main Weapon", "Ranged Main Weapon" }) do
        local item = G.q("GetEquippedItem", u, slot)
        if item then
            local okg, groups = pcall(function() return flag_set(Ext.Entity.Get(item).ProficiencyGroup.Flags) end)
            if okg and groups and next(groups) then
                any_read = true
                for g in pairs(groups) do if mine[g] then return true end end
            end
        end
    end
    if any_read then return false end
    return nil
end

-- IsImmuneToStatus: true when the status (or its group) is in the entity's StatusImmunities; not being listed is
-- not read as "not immune" (immunities from boosts may not show there), so that is unknown.
function G.immune_to(u, id)
    local ok, r = pcall(function()
        local m = Ext.Entity.Get(u).StatusImmunities.PersonalStatusImmunities
        return m[id] ~= nil
    end)
    if ok and r then return true end
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
    local subj = (ctx.subject == ctx.source or ctx.subject ~= ctx.target) and "the caster" or "the target"
    if name == "Tagged" and str1() and #a <= 2 then
        local u, id = who(2), tag_id(str1())
        if not u or not id then return nil end
        local v = yes(G.q("IsTagged", u, id))
        local w = (u == ctx.source or u ~= ctx.target) and "the caster" or "the target"
        return v, w .. " is " .. lower(str1()), w .. " is not " .. lower(str1())
    end
    if name == "HasStatus" and str1() and #a <= 2 then
        local u = who(2)
        if not u then return nil end
        local s = lower(str1():gsub("^SG_", ""))
        -- a Shout's target is its caster (window 8 live: "Hide can't be used now: the target has flanked")
        local w = (u == ctx.source or u ~= ctx.target) and "the caster" or "the target"
        return G.rule_has_status(u, str1()), w .. " has " .. s, w .. " doesn't have " .. s
    end
    local function num(i) return a[i] and a[i].k == "id" and tonumber(a[i].v) or nil end
    local function label(u) return (u == ctx.source or u ~= ctx.target) and "the caster" or "the target" end
    -- terms the game's helpers define (Shared CommonConditions.khn) or that read from Osi / components
    if name == "HasHPMoreThan" and num(1) and #a <= 2 then
        local u = #a == 2 and who(2) or ctx.target -- thoth: entity or context.Target
        local hp = u and tonumber((G.q("GetHitpoints", u)))
        if not hp then return nil end
        return hp > num(1), string.format("%s has more than %d HP", label(u), num(1)),
            string.format("%s has %d HP or less", label(u), num(1))
    end
    if name == "HasPassive" and str1() and #a <= 2 then
        local u = who(2)
        if not u then return nil end
        return yes(G.q("HasPassive", u, str1())), label(u) .. " has " .. lower(str1()),
            label(u) .. " doesn't have " .. lower(str1())
    end
    if name == "IsImmuneToStatus" and str1() and #a <= 2 then
        local u = who(2)
        if not u then return nil end
        return G.immune_to(u, str1()), label(u) .. " is immune to " .. lower(str1():gsub("^SG_", "")),
            label(u) .. " is not immune to " .. lower(str1():gsub("^SG_", ""))
    end
    if name == "IsProficientWithEquippedWeapon" then
        local u = (a[1] and a[1].v == "context.Target") and ctx.target or ctx.source
        return G.proficient_weapon(u), "the caster is proficient with the weapon",
            "the caster is not proficient with the weapon held"
    end
    if name == "IsCrowdControlled" and #a <= 1 then
        local u = #a == 1 and who(1) or ctx.target -- thoth: entity or context.Target
        if not u then return nil end
        return any_status(u, G.CROWD_CONTROLLED), label(u) .. " can't act", label(u) .. " can act"
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
    if name == "Player" then
        return yes(G.q("IsPlayer", u)), subj .. " is a player character", subj .. " is not a player character"
    end
    if name == "Locked" then return yes(G.q("IsLocked", u)), "it is locked", "it is not locked" end
    if name == "HasHelpableCondition" and #a == 0 then
        return any_status(u, G.HELPABLE), "it needs help (downed, prone, held, asleep, burning...)",
            "it has nothing to help with (not downed, prone, held, asleep or burning)"
    end
    if name == "MobileShootingCasterCondition" then
        local v = any_status(u, { "DASH", "DISENGAGE" })
        return v, "the caster has dashed or disengaged", "the caster has neither dashed nor disengaged this turn"
    end
    if name == "CanUseWeaponActions" and #a == 0 then
        -- thoth: not Tagged('AI_BLOCKWEAPONACTIONS') | Player()
        if G.b(G.q("IsPlayer", u)) then return true, "it can use weapon actions" end
        local id = tag_id("AI_BLOCKWEAPONACTIONS")
        local t = id and yes(G.q("IsTagged", u, id))
        if t == nil then return nil end
        return not t, "it can use weapon actions", "it can't use weapon actions"
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
    -- a stats string may end in ';' (Target_Grease: "not Item();"): a separator, not part of the condition
    local v, why = eval(parse((tostring(src):gsub("[;%s]+$", ""))), ctx, unread)
    return v, why, unread
end

local function log_unread(spell, what, unread, last)
    for _, t in ipairs(unread) do
        last[#last + 1] = what .. ": " .. t
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
    -- this check's unread terms, per caster: a cast the engine then doesn't check either (a check flag that stayed
    -- set) carries them in its cast_unchecked record (ActionsRolls.lua)
    local last = {}
    G.rules_last[G.guid(char) or tostring(char)] = { spell = spell, terms = last }
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
    log_unread(spell, "RequirementConditions", unread, last)
    if v == false then G.refuse(name .. " can't be used now: " .. why) end
    -- A Shout, a Zone or an area spell (AreaRadius / ExplodeRadius) applies its TargetConditions to each creature it
    -- reaches, not to the pick: Turn Undead's "Tagged('UNDEAD')" filters who flees, the caster isn't checked
    -- (launch-v1 replay: Aid with targets given, Prayer of Healing, Turn Undead would be refused).
    local st_type = tostring(field("SpellType") or "")
    local area = (tonumber(field("AreaRadius")) or 0) > 0 or (tonumber(field("ExplodeRadius")) or 0) > 0
    if target and st_type ~= "Shout" and st_type ~= "Zone" and st_type ~= "Wall" and not area then
        -- a two-step spell (Revivify, Dimension Door) checks its first pick against OriginTargetConditions; its
        -- TargetConditions are the second pick's (the landing)
        local which = st_type == "Teleportation" and "OriginTargetConditions"
            or "TargetConditions"
        v, why, unread = G.rule_eval(field(which), { source = char, target = target, subject = target })
        log_unread(spell, which, unread, last)
        if v == false then G.refuse(name .. " can't target that: " .. why) end
    end
end
