-- Part of Server/Guard.lua (split for the 1000-line cap): a spell's own rules that a harness cast skips.
--
-- REVIEW-5 F1/F3: the engine doesn't check a harness cast's stats Requirements / RequirementConditions (live: window 6
-- Jump went through while ENSNARED, window 8 Hide while FLANKED). A human's hotbar greys such an action. Here they are
-- read from the stats and refused before the order, with the plain game fact:
--   Requirements           Combat / !Combat / !Immobile
--   RequirementConditions  on the caster: HasStatus, Immobilized, WearingArmor, Combat, Tagged
-- TargetConditions (and a two-step spell's OriginTargetConditions) are NOT refused here: the engine refuses a target
-- they rule out (window 9 R3, guard off: Sleep on 48 HP, Healing Word on an undead, a swing at a corpse, Help on a
-- healthy ally, all InvalidTarget, nothing spent), so a misread term could only refuse a legal cast. After the
-- engine's InvalidTarget, G.explain_target says which terms the guard reads as unmet (Actions.lua cast_failed_why).
-- A term goes back to a pre-check only with a live case of the engine letting an illegal target through.
-- Three-valued: a term that can't be read (an unknown function, an argument form not handled, a query that answers
-- nothing) is unknown; only a condition that is false whatever the unknown terms are is refused. Unknown terms are
-- logged once per spell and term, never refused.

local G = BG3AI.Guard
local U = BG3AI.Util

G.rules_unread = G.rules_unread or {} -- "spell|term" -> true: logged once
G.rules_last = G.rules_last or {} -- caster guid -> {spell, terms}: the unread terms of its last rule check

local function lower(s) return tostring(s):lower():gsub("_", " ") end

-- A status, a status group or a passive as the game names it: the stats DisplayName when it reads ("Flanked"),
-- else the id in words ("FLANKED" -> "Flanked", "SG_Incapacitated" -> "Incapacitated").
local function game_name(id)
    id = tostring(id)
    if id:sub(1, 3) ~= "SG_" then
        local ok, n = pcall(function() return U.loca(Ext.Stats.Get(id).DisplayName) end)
        if ok and type(n) == "string" and n ~= "" then return n end
    end
    local w = lower(id:gsub("^SG_", ""))
    return (w:gsub("^%l", string.upper))
end
-- "the caster has the Flanked status" / "has an Incapacitated status" (a group): a status name is not a verb's
-- object ("the caster has flanked", window 8).
local function status_words(id)
    local n = game_name(id)
    if tostring(id):sub(1, 3) == "SG_" then
        return "a status of the " .. n .. " kind", "no status of the " .. n .. " kind"
    end
    return "the " .. n .. " status", "the " .. n .. " status"
end

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
        local yes_w, no_w = status_words(str1())
        -- a Shout's target is its caster (window 8 live: "Hide can't be used now: the target has flanked")
        local w = (u == ctx.source or u ~= ctx.target) and "the caster" or "the target"
        local neg = str1():sub(1, 3) == "SG_" and (w .. " has " .. no_w) or (w .. " doesn't have " .. no_w)
        return G.rule_has_status(u, str1()), w .. " has " .. yes_w, neg
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
        return yes(G.q("HasPassive", u, str1())), label(u) .. " has the passive " .. game_name(str1()),
            label(u) .. " doesn't have the passive " .. game_name(str1())
    end
    if name == "IsImmuneToStatus" and str1() and #a <= 2 then
        local u = who(2)
        if not u then return nil end
        local sw = str1():sub(1, 3) == "SG_" and ("statuses of the " .. game_name(str1()) .. " kind")
            or ("the " .. game_name(str1()) .. " status")
        return G.immune_to(u, str1()), label(u) .. " is immune to " .. sw, label(u) .. " is not immune to " .. sw
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
end

-- The pick's TargetConditions (OriginTargetConditions for a two-step spell) read as false: "<spell> can't target
-- that: <fact>", else nil (met, unread, or not the pick's: a Shout, a Zone, an area spell). Not a pre-check (the
-- engine checks targets, window 9 R3): used only when IgnoreTargetChecks stayed set on the cast (ActionsRolls.lua,
-- fail closed), and its unread terms are logged as the old pre-check's were.
function G.target_refusal(char, spell, target)
    local st = spell and Ext.Stats.Get(spell)
    if not st or not target or G.RULES_OFF then return nil end
    local function field(k)
        local ok, v = pcall(function() return st[k] end)
        return ok and v or nil
    end
    local st_type = tostring(field("SpellType") or "")
    local area = (tonumber(field("AreaRadius")) or 0) > 0 or (tonumber(field("ExplodeRadius")) or 0) > 0
    if st_type == "Shout" or st_type == "Zone" or st_type == "Wall" or area then return nil end
    local which = st_type == "Teleportation" and "OriginTargetConditions" or "TargetConditions"
    local v, why, unread = G.rule_eval(field(which), { source = char, target = target, subject = target })
    log_unread(spell, which, unread, {})
    if v ~= false then return nil end
    local name = G.spell_facts(spell)
    return (name and name.name or spell) .. " can't target that: " .. why
end

------------------------------------------------------------------------
-- After the game refused a cast at a creature (InvalidTarget): the spell's target conditions as the guard reads
-- them, the parts not read as met only. Window 8 m2: Sneak Attack (Melee) at 1.0 m was refused twice; its
-- TargetConditions want advantage or an ally near the target, terms the guard doesn't read.
------------------------------------------------------------------------

local function attack_words(x)
    local t = x and x.v and x.v:match("^AttackType%.(.+)$")
    if not t then return "an attack" end
    return "a " .. t:gsub("(%l)(%u)", "%1 %2"):lower() -- MeleeWeaponAttack -> "a melee weapon attack"
end

-- Plain words for terms the guard doesn't evaluate (CommonConditions.khn), as (when true, when false).
local function unread_words(node)
    local a, name = node.args, node.name
    if name == "HasAdvantage" then
        return "the caster has advantage on " .. attack_words(a[1]),
            "the caster doesn't have advantage on " .. attack_words(a[1])
    end
    if name == "HasDisadvantage" then
        return "the caster has disadvantage on " .. attack_words(a[1]),
            "the caster doesn't have disadvantage on " .. attack_words(a[1])
    end
    if name == "HasAllyWithinRange" then
        -- thoth: an ally of the caster within distance (default 1.5) of the target, without exclusionStatus
        local d = a[2] and a[2].k == "id" and tonumber(a[2].v) or 1.5
        local ex = a[1] and a[1].k == "str" and (" that is not " .. lower(a[1].v:gsub("^SG_", ""))) or ""
        local w = string.format("an ally of the caster%s within %s m of the target", ex, tostring(d))
        return "there is " .. w, "there is no " .. w
    end
    local args = {}
    for _, x in ipairs(a) do args[#args + 1] = x.k == "str" and ("'" .. x.v .. "'") or x.v end
    local raw = name .. "(" .. table.concat(args, ",") .. ")"
    return raw, "not " .. raw
end

-- (value, words of what is needed and not read as met; nil when read as met). neg: the node sits under a 'not'.
local function needs(node, ctx, neg)
    if node.op == "unknown" then return nil, "(a condition the guard can't parse)" end
    if node.op == "not" then return needs(node[1], ctx, not neg) end
    if node.op == "and" or node.op == "or" then
        local is_and = (node.op == "and") ~= (neg == true) -- not (a or b) = not a and not b
        local va, wa = needs(node[1], ctx, neg)
        local vb, wb = needs(node[2], ctx, neg)
        if is_and then
            if va == true and vb == true then return true end
            local v = (va == false or vb == false) and false or nil
            return v, (wa and wb) and (wa .. " and " .. wb) or wa or wb
        end
        if va == true or vb == true then return true end
        local v = (va == false and vb == false) and false or nil
        return v, "(" .. wa .. " or " .. wb .. ")"
    end
    local v, when_true, when_false = term(node, ctx)
    if v ~= nil then
        if neg then v, when_true, when_false = not v, when_false, when_true end
        if v then return true end
        return false, when_true .. " (no: " .. (when_false or "not met") .. ")"
    end
    local t, f = unread_words(node)
    return nil, (neg and f or t) .. " (not read)"
end

-- The words for a cast at target: "<spell> needs <...>" or a line saying every term read as met; nil when the
-- spell's target conditions don't apply to the pick (a Shout, a Zone, an area) or there are none.
function G.explain_target(char, spell, target)
    local st = spell and Ext.Stats.Get(spell)
    if not st or not target then return nil end
    local function field(k)
        local ok, v = pcall(function() return st[k] end)
        return ok and v or nil
    end
    local st_type = tostring(field("SpellType") or "")
    local area = (tonumber(field("AreaRadius")) or 0) > 0 or (tonumber(field("ExplodeRadius")) or 0) > 0
    if st_type == "Shout" or st_type == "Zone" or st_type == "Wall" or area then return nil end
    local which = st_type == "Teleportation" and "OriginTargetConditions" or "TargetConditions"
    local src = field(which)
    if src == nil or tostring(src):match("^[;%s]*$") then return nil end
    local name = G.spell_facts(spell)
    name = name and name.name or spell
    local v, w = needs(parse((tostring(src):gsub("[;%s]+$", ""))), { source = char, target = target,
        subject = target }, false)
    if v == true or not w then return name .. "'s target conditions all read as met here" end
    return name .. " needs, at this target: " .. w
end
