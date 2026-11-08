-- Fair event stream (server context). Osiris listeners push only facts a
-- player would see: the combat log, toasts, the journal, what the party sees.
-- Never story flags, dialog resource names, hidden rolls or roll subjects.
-- See docs/PROTOCOL.md (Events) and research/02-visibility.md §4.2-4.3.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local E = BG3AI.Events or {}
BG3AI.Events = E

local truthy = U.truthy
local osi = V.osi

-- What the stream remembers between events (the combats announced, approvals, a party Use, the open container, the
-- rest flow, gold, traps) is kept per side in an arena (U.sided / U.sget): every listener runs once per side.
E.openContainer = E.openContainer or nil
E.approval = E.approval or {}
E.approvalAsked = E.approvalAsked or {}
E.combats = E.combats or {}
E.actions = E.actions or {} -- storyActionID -> the cast it belongs to (CastSpell)

-- Per-event visibility context, cached for one game tick's worth of events.
local ctxCache, ctxTime = nil, 0
local function ctx()
    if U.side() then return V.context() end -- a side's context (V.context caches it per frame and side)
    local t = Ext.Utils.MonotonicTime()
    if not ctxCache or t - ctxTime > 250 then
        ctxCache, ctxTime = V.context(), t
    end
    return ctxCache
end

local function isCharacter(uuid)
    local e = V.entity(uuid)
    return e ~= nil and U.comp(e, "ServerCharacter") ~= nil, e
end

-- Visible character (party always visible).
local function visibleChar(uuid)
    uuid = V.uuid(uuid)
    if not uuid then return nil end
    local isChar, e = isCharacter(uuid)
    if not isChar then return nil end
    local c = ctx()
    if c.is_party[uuid] or V.characterVisible(c, uuid, e) then return uuid end
    return nil
end

local function ref(uuid)
    return { uuid = uuid, name = V.name(uuid) }
end

local function partyInCombat(combatGuid)
    for _, u in ipairs(ctx().party) do
        if osi(Osi.CombatGetGuidFor, u) == combatGuid then return true end
    end
    return false
end

-- Each event runs fn once with no side and, in an arena, once per side: what fn pushes then is that side's.
local function listen(name, arity, fn)
    local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(...)
        local ok2, err2 = pcall(U.each_side, fn, ...)
        if not ok2 then Ext.Utils.PrintWarning("[BG3AI] event " .. name .. ": " .. tostring(err2)) end
    end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] cannot listen to " .. name .. ": " .. tostring(err)) end
end

------------------------------------------------------------------------
-- Combat
------------------------------------------------------------------------

listen("CombatStarted", 1, function(combat)
    -- Party involvement may be registered a frame later; remember and check on round start too.
    local combats = U.sided(E, "combats")
    combats[combat] = { announced = false }
    if partyInCombat(combat) then
        combats[combat].announced = true
        RPC.push("combat_started", {})
    end
end)

listen("CombatRoundStarted", 2, function(combat, round)
    local combats = U.sided(E, "combats")
    local c = combats[combat]
    if not partyInCombat(combat) then return end
    if not c or not c.announced then
        combats[combat] = { announced = true }
        RPC.push("combat_started", {})
    end
    RPC.push("round_started", { round = round })
end)

listen("CombatEnded", 1, function(combat)
    local combats = U.sided(E, "combats")
    local c = combats[combat]
    combats[combat] = nil
    if c and c.announced then RPC.push("combat_ended", {}) end
end)

-- turn_started / turn_ended {uuid, name, round}: round = the combat round the turn STARTED in (nil when the mod never
-- saw that combat's CombatRoundStarted, e.g. a save loaded mid-combat). The game fires a round's last group's
-- TurnEnded after the next round's TurnStarted (live 2026-11-02): that turn_ended carries the older round. Pushed by
-- Actions.dispatch after its TurnStarted/TurnEnded pairing (Guard.turn_open / end_owed), not by a listener here, so
-- the round never depends on which listener runs first.
function E.turn(kind, char, round)
    local ok, err = pcall(U.each_side, function(c)
        local u = visibleChar(c)
        if not u then return end
        local d = ref(u)
        d.round = round
        RPC.push(kind, d)
    end, char)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] event " .. kind .. ": " .. tostring(err)) end
end

-- What dealt a hit, as far as the game names it. A side only remembers what it saw: a visible caster's casts, by the
-- game's storyActionID, which AttackedBy repeats, and the item that left a visible thrower during its own Throw.
local ACTION_KEEP_MS = 60000
local THROW_ITEM_MS = 6000 -- a Throw's windup, from its CastSpell to the item leaving the inventory

local function isThrow(spell)
    return tostring(spell):find("^Throw_") ~= nil
end

-- CastSpell(caster, spell, spellType, spellElement, storyActionID)
listen("CastSpell", 5, function(caster, spell, _, _, story)
    local u = visibleChar(caster)
    if not u or not story or story == 0 then return end
    local actions, t = U.sided(E, "actions"), Ext.Utils.MonotonicTime()
    for k, v in pairs(actions) do
        if t - v.t > ACTION_KEEP_MS then actions[k] = nil end
    end
    local st = osi(Ext.Stats.Get, spell)
    actions[story] = { t = t, caster = u, name = st and U.loca(st.DisplayName) or nil, throw = isThrow(spell) }
end)

-- RemovedFrom(object, inventoryHolder): the item of the holder's own open Throw (begun, not yet landed, none set), and
-- only that: anything else leaving an inventory isn't a thing a player is told. Read now, a bomb is gone once it has hit.
listen("RemovedFrom", 2, function(item, holder)
    local h, i = visibleChar(holder), V.uuid(item)
    if not (h and i) then return end
    local t, open = Ext.Utils.MonotonicTime(), nil
    for _, v in pairs(U.sided(E, "actions")) do
        if v.throw and v.caster == h and not v.item and not v.landed and t - v.t <= THROW_ITEM_MS
            and (not open or v.t > open.t) then
            open = v
        end
    end
    if not open then return end
    local n = V.name(i)
    if n and n ~= "Unknown" then open.item = n end
end)

-- The name of what dealt a hit from a visible source (of an unseen one the log says only "Someone"), nil when nothing
-- shown names it. In order: the object that dealt it when that isn't its owner (an item, a visible
-- summon); the cast with the hit's story action (the item of a Throw, else the spell's shown name); damageCause when it
-- is one plain word (an id with _ or digits could carry what a player isn't shown).
local function dealtBy(src, attacker, cause, story)
    local a = V.uuid(attacker)
    if a and a ~= src and (not isCharacter(a) or visibleChar(a)) then
        local n = V.name(a)
        if n and n ~= "Unknown" then return n end
    end
    local act = story and U.sided(E, "actions")[story]
    if act then
        local n = act.item or act.name
        if n then return n end
    end
    local word = type(cause) == "string" and cause:match("^%a+$") or nil
    return word ~= "None" and word or nil
end

-- AttackedBy(defender, attackerOwner, attacker, damageType, damageAmount, damageCause, storyActionID)
listen("AttackedBy", 7, function(defender, attackerOwner, attacker, dtype, amount, cause, story)
    local d = visibleChar(defender)
    if not d then return end
    local data = { target = ref(d), amount = amount, type = dtype }
    local src = visibleChar(attackerOwner) or visibleChar(attacker)
    if src then
        data.source = ref(src)
        data.by = dealtBy(src, attacker, cause, story)
    elseif V.uuid(attacker) or V.uuid(attackerOwner) then
        data.source = { name = "Someone" } -- unseen attacker, as the combat log shows it
    end
    RPC.push("damage", data)
end)

listen("Died", 1, function(char)
    local u = visibleChar(char)
    if u then RPC.push("died", ref(u)) end
end)

listen("DownedChanged", 2, function(char, downed)
    local u = visibleChar(char)
    if u then RPC.push(truthy(downed) and "downed" or "revived", ref(u)) end
end)

-- StatusApplied(object, status, causee, storyActionID): displayable statuses only, by their shown name.
-- Never the engine's status id: a player never sees it, and ids carry hints the name doesn't
-- (MAG_MYRKULITES_UNDEAD_PRESENCE_BUFF = from a Myrkulite item; fairness audit S023-S027).
local function nearParty(u, m)
    local e = V.entity(u)
    local p = e and V.pos(e)
    if not p then return nil end
    for _, o in ipairs(ctx().observers) do
        if o.pos and U.dist(o.pos, p) <= m then return e end
    end
    return nil
end

-- An item the party sees or remembers (V.itemKnown) whose look changes: a painting lighting up. Not one merely
-- near the party: a painting behind a wall lights up unseen (audit C11).
local function itemEffectEvent(obj, status, on)
    local t = V.itemEffect(status)
    if not t then return false end
    if not on and t == "flashing" then return true end -- the flash ending says nothing new
    local u = V.uuid(obj)
    if u and V.itemKnown(ctx(), u) then
        RPC.push("item_effect", { item = ref(u), state = on and t or ("no longer " .. t) })
    end
    return true
end

listen("StatusApplied", 4, function(obj, status, causee, _)
    if itemEffectEvent(obj, status, true) then return end
    if not V.statusInfo(status) then return end
    local u = visibleChar(obj)
    if not u then return end
    local data = { target = ref(u), name = V.statusInfo(status).name }
    local src = causee and visibleChar(causee)
    if src and src ~= u then data.source = ref(src) end
    RPC.push("status_applied", data)
end)

listen("StatusRemoved", 4, function(obj, status, _, _)
    if itemEffectEvent(obj, status, false) then return end
    if not V.statusInfo(status) then return end
    local u = visibleChar(obj)
    if u then RPC.push("status_removed", { target = ref(u), name = V.statusInfo(status).name }) end
end)

------------------------------------------------------------------------
-- An enemy's spell on first sight (act2-fights-analysis fix 4): what a player reads by hovering it, from the game's
-- own data: its shown name and description with the description's numbers filled in (DescriptionParams), the extra
-- description, then level / concentration, range, area, the save or attack, its damage (TooltipDamageList) and how
-- long its status lasts (TooltipStatusApply). Facts only, the tooltip's own text. One spell_info event per spell per
-- fight (E.spellSeen per combat), when an enemy casts it in view or a row shows it concentrating on it.
------------------------------------------------------------------------

local function splitTop(s) -- "DealDamage(5d8,Poison);Distance(3)" -> its top-level parts (commas inside parens kept)
    local out, depth, cur = {}, 0, ""
    for ch in tostring(s or ""):gmatch(".") do
        if ch == "(" then depth = depth + 1 elseif ch == ")" then depth = depth - 1 end
        if (ch == ";" or ch == ",") and depth == 0 then
            if cur ~= "" then out[#out + 1] = cur end
            cur = ""
        else
            cur = cur .. ch
        end
    end
    if cur ~= "" then out[#out + 1] = cur end
    return out
end

-- The caster's numbers in a damage formula, as the tooltip shows them (w10 L2: "MainMeleeWeapon
-- MainMeleeWeaponDamageType damage" and "LevelMapValue(D8Cantrip) Cold damage" reached the agent raw).
-- LevelMapValue(name): the game's level map (Ext.StaticData "LevelMap", by its Name) at the caster's level.
-- <Slot>Weapon tokens are not resolved (REVIEW-13 item 1): the tooltip's number also depends on the off-hand rule,
-- a versatile weapon held in two hands and the caster's own damage boosts (Rage, Dueling, ...), so any number built
-- here could be wrong; the part is left out like any other formula and the combat log shows the real roll.
local DAMAGE_TYPES = { Acid = true, Bludgeoning = true, Cold = true, Fire = true, Force = true, Lightning = true,
    Necrotic = true, Piercing = true, Poison = true, Psychic = true, Radiant = true, Slashing = true, Thunder = true }
local DICE = { "4", "6", "8", "10", "12", "20", "100" } -- bg3se DiceSizeId D4 .. D100, in its order

local function field(o, k) local ok, v = pcall(function() return o[k] end) if ok then return v end return nil end

E.levelMaps = E.levelMaps or nil -- level map Name -> its resource (one scan of the bank; static data doesn't change)
local function levelMap(name)
    if not E.levelMaps then
        local all = osi(Ext.StaticData.GetAll, "LevelMap")
        local maps = {}
        for _, g in ipairs(type(all) == "table" and all or {}) do
            local r = osi(Ext.StaticData.Get, g, "LevelMap")
            local n = r and field(r, "Name")
            if n then maps[tostring(n)] = r end
        end
        E.levelMaps = maps
    end
    return E.levelMaps[name]
end

local function levelMapValue(name, lvl)
    local r = levelMap(name)
    if not (r and lvl) then return nil end
    local v = field(field(r, "LevelMaps") or {}, lvl)
    if v == nil then v = field(r, "FallbackValue") end
    if type(v) == "number" then return tostring(math.floor(v)) end
    if v == nil then return nil end
    local n, d = tonumber(field(v, "AmountOfDices")), field(v, "DiceValue")
    local die = type(d) == "number" and DICE[d + 1] or tostring(d or ""):match("^D(%d+)$")
    return n and die and (n .. "d" .. die) or nil
end

-- A damage/heal amount with the caster's numbers, or nil (the part is left out) when anything stays a formula.
local function amount(x, c)
    local out = tostring(x or ""):gsub("LevelMapValue%(%s*([%w_]+)%s*%)", function(n)
        return levelMapValue(n, c.level) or "?"
    end)
    out = out:gsub("%s+", "")
    return (out:find("%d") and out:match("^[%dd%+%-]+$")) and out or nil
end

local function damageType(x)
    x = tostring(x or ""):gsub("^%s+", ""):gsub("%s+$", "")
    return DAMAGE_TYPES[x] and x or nil
end

-- One tooltip parameter as the tooltip shows it: DealDamage(5d8,Poison) "5d8 Poison damage", Distance(3) "3m",
-- RegainHitPoints(2d4) "2d4 hit points", a bare number or dice as written; nil for anything that stays a formula
-- (its token goes to c.left, which E.spell_info logs once).
local function param(p, c)
    local raw = tostring(p):gsub("^%s+", ""):gsub("%s+$", "")
    local fn, args = raw:match("^([%w_]+)%((.*)%)$")
    local out
    if not fn then
        out = raw:match("^[%d%.%%d%+%-]+$") and raw or nil
    else
        local a = {}
        for x in (args .. ","):gmatch("([^,]*),") do a[#a + 1] = (x:gsub("^%s+", ""):gsub("%s+$", "")) end
        if fn == "DealDamage" then
            local n = amount(a[1], c)
            local t = (a[2] == nil or a[2] == "") and "" or damageType(a[2])
            out = n and t and (n .. (t ~= "" and (" " .. t) or "") .. " damage") or nil
        elseif fn == "Distance" then
            out = tonumber(a[1]) and (a[1] .. "m") or nil
        elseif fn == "RegainHitPoints" then
            local n = amount(a[1], c)
            out = n and (n .. " hit points") or nil
        end
    end
    if not out then c.left[#c.left + 1] = raw end
    return out
end

-- The description with its [n] filled in; nil when a parameter stays a formula (the sentence would be cut).
local function tooltipText(handle, params, c)
    local t = U.loca(handle)
    if not t then return nil end
    local ps, whole = splitTop(params), true
    t = t:gsub("%[(%d+)%]", function(i)
        local x = ps[tonumber(i)] and param(ps[tonumber(i)], c)
        if not x then whole = false end
        return x or ""
    end)
    if not whole then return nil end
    t = t:gsub("<br%s*/?>", " "):gsub("<[^>]+>", ""):gsub("%s+", " ")
    return (t:gsub("^%s+", ""):gsub("%s+$", ""))
end

local SAVES = { Strength = true, Dexterity = true, Constitution = true, Intelligence = true, Wisdom = true,
    Charisma = true }

local function hasFlag(st, flag)
    local yes = false
    pcall(function() for _, fl in ipairs(st.SpellFlags) do if fl == flag then yes = true end end end)
    return yes
end

-- An action every party member has in its own spellbook, and not a spell (w10 L2: Main Hand Attack, Ranged Attack,
-- Jump; Dash): the player reads that tooltip on each hotbar, so an enemy's use of it brings nothing new.
-- Summons are left out (they have their own short spellbooks: a party with a summon would show every common action).
local function everyoneHas(id, st)
    if hasFlag(st, "IsSpell") then return false end
    local n = 0
    for _, u in ipairs(ctx().party or {}) do
        local e, has = V.entity(u), false
        if not (U.comp(e, "IsSummon") or truthy(osi(Osi.IsSummon, u))) then
            n = n + 1
            pcall(function()
                for _, sp in ipairs(e.SpellBook.Spells) do
                    if sp.Id.Prototype == id or sp.Id.OriginatorPrototype == id then has = true break end
                end
            end)
            if not has then return false end
        end
    end
    return n > 0
end

local function casterLevel(u)
    local e = u and V.entity(u)
    local ok, l = pcall(function() return e.EocLevel.Level end)
    return ok and tonumber(l) or nil
end

E.spellInfo = E.spellInfo or {} -- spell id .. "|" .. caster -> its text, or false (nothing to show)
function E.spell_info(id, caster)
    -- in an arena the side too: everyoneHas reads that side's own members (ctx() is the side's context)
    local key = tostring(id) .. "|" .. tostring(caster) .. "|" .. tostring(U.side())
    local c = E.spellInfo[key]
    if c ~= nil then return c or nil end
    c = false
    local st = osi(Ext.Stats.Get, id)
    -- a field the entry's type lacks (Shout_Dash: no TargetRadius) raises; it reads as absent (w10 L2's
    -- "bad argument #1 to 'tonumber'" on every Dash)
    local function f(k) local v = st and field(st, k) if v ~= nil and v ~= "" then return v end return nil end
    local name = st and U.loca(f("DisplayName"))
    if name and not everyoneHas(id, st) then
        local cx = { uuid = caster, level = casterLevel(caster), left = {} }
        local text = { name .. ":" }
        local d = tooltipText(f("Description"), f("DescriptionParams"), cx)
        if d then text[#text + 1] = d end
        local x = tooltipText(f("ExtraDescription"), f("ExtraDescriptionParams"), cx)
        if x then text[#text + 1] = x end
        local facts = {}
        local lvl = tonumber(f("Level"))
        if lvl and lvl > 0 then facts[#facts + 1] = "level " .. lvl end
        if hasFlag(st, "IsConcentration") then facts[#facts + 1] = "concentration" end
        local range, area = tonumber(f("TargetRadius")), tonumber(f("AreaRadius"))
        local function m(v)
            v = U.round(v, 1)
            return (v == math.floor(v) and string.format("%d", v) or tostring(v)) .. "m"
        end
        if range and range > 0 then facts[#facts + 1] = "range " .. m(range) end
        if area and area > 0 then facts[#facts + 1] = "area " .. m(area) end
        -- a ';' list (Ray of Sickness "RangedSpellAttack;Constitution"): each roll as the tooltip names it, in the
        -- data's order, joined by "; " (the data says nothing about which comes first in play)
        local rolls = {}
        for sv in tostring(f("TooltipAttackSave") or ""):gmatch("[^;]+") do
            sv = sv:match("^%s*(.-)%s*$")
            if sv ~= "" then
                rolls[#rolls + 1] = SAVES[sv] and (sv .. " save") or (sv:gsub("(%l)(%u)", "%1 %2"):lower())
            end
        end
        if #rolls > 0 then facts[#facts + 1] = table.concat(rolls, "; ") end
        for _, dmg in ipairs(splitTop(f("TooltipDamageList"))) do facts[#facts + 1] = param(dmg, cx) end
        for _, a in ipairs(splitTop(f("TooltipStatusApply"))) do
            local turns = tonumber(tostring(a):match("^%s*ApplyStatus%([^,]*,[^,]*,%s*(-?%d+)"))
            if turns and turns > 0 then
                facts[#facts + 1] = "lasts " .. turns .. (turns == 1 and " turn" or " turns")
            end
        end
        if #facts > 0 then text[#text + 1] = "(" .. table.concat(facts, ", ") .. ")" end
        c = table.concat(text, " ")
        if #cx.left > 0 then
            Ext.Utils.Print("[BG3AI] spell_info " .. tostring(id) .. ": left out (unresolved): "
                .. table.concat(cx.left, "; "))
        end
    end
    E.spellInfo[key] = c
    return c or nil
end

-- The first sight of a spell in a fight (keyed by the caster's combat; out of combat, "none"): push spell_info.
-- In an arena it is a side's own (its listener run or its op: the event is tagged with that side, and each side's
-- first sight is its own); the harness's no-side run reads both teams, which no side sees, so it pushes none.
E.spellSeen = E.spellSeen or {}
function E.note_spell(caster, id)
    if not id or id == "" then return end
    if RPC.arena_active and RPC.arena_active() and not RPC.side() then return end
    local combat = tostring(osi(Osi.CombatGetGuidFor, caster) or "none")
    local fights = U.sided(E, "spellSeen")
    local seen = fights[combat] or {}
    fights[combat] = seen
    if seen[id] then return end
    local text = E.spell_info(id, caster)
    if not text then return end
    seen[id] = true
    RPC.push("spell_info", text)
end

-- CastedSpell(caster, spell, spellType, spellElement, storyActionID)
listen("CastedSpell", 5, function(caster, spell, _, _, story)
    local u = visibleChar(caster)
    if not u then return end
    local act = story and U.sided(E, "actions")[story]
    if act then act.landed = true end
    local st = osi(Ext.Stats.Get, spell)
    local name = st and U.loca(st.DisplayName) or nil
    if not name then return end -- unnamed/technical casts are not shown in the log
    -- roll: how the spell lands (its stats: the tooltip's "Wisdom save" / attack roll), so a 0-damage result reads
    -- as a save or a miss in the fight debrief rather than a resisted damage type.
    local roll
    pcall(function()
        local r = tostring(st.SpellRoll and st.SpellRoll.Default or "")
        roll = r:find("SavingThrow%(") and "save" or (r:find("Attack%(") and "attack") or nil
    end)
    -- spell: the engine id, only for our own casters (the hotbar's spell, what a plan names). An enemy's cast shows
    -- a player only its display name; its id can carry hints the name doesn't (audit ACT-14, as for status ids).
    -- turn "game's": own casters only, cast in their own open turn the game runs (Guard.lost_control, tb_flags); a
    -- reaction the side answers on another's turn isn't tagged (REVIEW-reactions N1); the other side's as before
    local Gl = ctx().is_party[u] and BG3AI.Guard
    local open, ended
    if Gl and Gl.tb_flags then open, ended = Gl.tb_flags(u) end
    RPC.push("cast", { caster = ref(u), spell = ctx().is_party[u] and spell or nil, name = name, roll = roll,
        thrown = act and act.item or nil, turn = open and not ended and Gl.lost_control and Gl.lost_control(u) and "game's" or nil })
    if not ctx().is_party[u] then E.note_spell(u, spell) end
end)

------------------------------------------------------------------------
-- Rolls: only what the roll UI / overheads show; never the subject or event.
-- RollResult(eventName, roller, rollSubject, resultType, isActiveRoll, criticality)
------------------------------------------------------------------------

listen("RollResult", 6, function(_, roller, _, result, isActive, crit)
    local u = V.uuid(roller)
    local c = ctx()
    if not (u and c.is_party[u]) then return end
    local success = truthy(result)
    local active = truthy(isActive)
    if not active and not success and c.rules.HIDE_FAILED_PERCEPTION_ROLLS then return end
    if not active and c.rules.DIALOGUE_PASSIVE_ROLL_HIDE and V.inDialog(u) then return end
    local data = { roller = ref(u), success = success, active = active }
    -- Osiris criticality: 1 = natural 20, 2 = natural 1 (S009: a nat-1 Persuasion read as "critical=2").
    if crit and crit ~= 0 then data.critical = ({ [1] = "success", [2] = "fail" })[tonumber(crit)] or crit end
    RPC.push("roll_result", data)
end)

------------------------------------------------------------------------
-- Dialogue: speakers only (never the dialog resource).
------------------------------------------------------------------------

-- instance -> its NPC speakers' uuids: E.dialogs with no side (Referee.lua reads it: a dialogue that offers Trade), a
-- side's own in an arena (audit S3-1: one shared table gave the first side's DialogEnded the entry, so the other run
-- never said dialogue_ended).
local function dialogs()
    if U.side() then return U.sided(E, "dialogs") end
    E.dialogs = E.dialogs or {}
    return E.dialogs
end

listen("DialogStarted", 2, function(_, instance)
    local side = U.side()
    local speakers, npcs = {}, {}
    local n = osi(Osi.DialogGetNumberOfInvolvedNPCs, instance) or 0
    for i = 1, n do
        local npc = osi(Osi.DialogGetInvolvedNPC, instance, i)
        local u = npc and V.uuid(npc)
        if u then speakers[#speakers + 1], npcs[#npcs + 1] = ref(u), u end
    end
    local np = osi(Osi.DialogGetNumberOfInvolvedPlayers, instance) or 0
    local party, at = {}, nil
    for i = 1, np do
        local p = osi(Osi.DialogGetInvolvedPlayer, instance, i)
        local u = p and V.uuid(p)
        -- in an arena both teams are players of one party: a side's dialogue is one of its own members (audit S3-1)
        if u and side and BG3AI.Arena.side_of(u) ~= side then u = nil end
        if u then
            party[#party + 1] = ref(u)
            if not at then
                local e = V.entity(u)
                at = e and V.pos(e)
            end
        end
    end
    if #party == 0 then return end -- not our dialogue (ambient NPC chatter)
    dialogs()[instance] = npcs
    -- Party banter / barks (automated dialogs) play while you walk: flagged so queues don't stop on them.
    local auto = osi(Osi.DialogIsAutomated, instance)
    -- at/level: where the party member stood when the talk began (the bridge's off-limits areas, vis V26).
    RPC.push("dialogue_started", { speakers = speakers, party = party,
        automated = (auto == 1 or auto == true) or nil, at = at and U.vec(at, 1) or nil,
        level = V.levelNow and V.levelNow() or nil })
end)

listen("DialogEnded", 2, function(_, instance)
    local dl = dialogs()
    if dl[instance] then
        dl[instance] = nil
        RPC.push("dialogue_ended", {})
    end
end)

------------------------------------------------------------------------
-- Journal, progression, approval
------------------------------------------------------------------------

-- The title of a quest the journal shows (QuestVisiblity, a localised title, as State.journal), else nil: hidden and
-- technical quests ("%%% EMPTY", no prototype) never reach the agent (audit C10).
local function questTitle(questId)
    local S = BG3AI.State
    local quests = S and (S.loadQuests and S.loadQuests() or S.quests)
    for _, q in ipairs(quests or {}) do
        if q.id == questId then
            local title = q.visible and U.loca(q.title)
            if title then return title, q end
            return nil
        end
    end
    return nil
end

-- The game fires QuestAccepted / QuestUpdateUnlocked once per party member, all in the same tick (w12 L1a: a party
-- of 4 put one quest_update 4 times in a talk_to's EVENTS). The journal shows one entry: one row per quest (and
-- step) per tick. E.tickN counts ticks (the Tick handler below).
local function firstThisTick(key)
    local seen = E.questSeen
    if not seen or seen.tick ~= E.tickN then
        seen = { tick = E.tickN, keys = {} }
        E.questSeen = seen
    end
    if seen.keys[key] then return false end
    seen.keys[key] = true
    return true
end

listen("QuestAccepted", 2, function(char, quest)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title = questTitle(quest)
    if title and firstThisTick("accepted|" .. tostring(quest)) then RPC.push("quest_added", { title = title }) end
end)

-- Only a step whose text the journal shows (State.journal skips the others).
listen("QuestUpdateUnlocked", 3, function(char, quest, step)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title, q = questTitle(quest)
    if not title then return end
    for _, st in ipairs(q.steps) do
        local text = st.id == step and U.loca(st.desc)
        if text then
            if firstThisTick("update|" .. tostring(quest) .. "|" .. tostring(step)) then
                RPC.push("quest_update", { title = title, text = text })
            end
            return
        end
    end
end)

listen("LeveledUp", 1, function(char)
    local u = V.uuid(char)
    if ctx().is_party[u or ""] then RPC.push("leveled_up", ref(u)) end
end)

-- ApprovalRatingChanged(ratingOwner, ratedEntity, newValue): the toast shows
-- who approves/disapproves, not numbers; report direction only. E.approval is keyed owner|rated and seeded from
-- GetApprovalRating by the poll (E.seedApproval): unseeded, the first change per companion after every load had no
-- previous value and was dropped (audit ACT-15).
local function approvalKey(owner, rated) return owner .. "|" .. (rated or "") end

function E.seedApproval(c)
    local host = V.uuid(osi(Osi.GetHostCharacter)) or c.party[1]
    if not host then return end
    local asked, approval = U.sided(E, "approvalAsked"), U.sided(E, "approval")
    for _, u in ipairs(c.party) do
        local k = approvalKey(u, host)
        if u ~= host and approval[k] == nil and not asked[k] then
            asked[k] = true -- once per pair and Lua state: a hireling has no rating to read every poll
            local v = osi(Osi.GetApprovalRating, u, host)
            if type(v) == "number" then approval[k] = v end
        end
    end
end

listen("ApprovalRatingChanged", 3, function(npc, rated, value)
    local u = V.uuid(npc)
    if not u then return end
    local k = approvalKey(u, V.uuid(rated))
    local approval = U.sided(E, "approval")
    local prev = approval[k]
    approval[k] = value
    if prev == nil or prev == value then return end
    RPC.push("approval", { npc = ref(u), direction = value > prev and "approves" or "disapproves" })
end)

------------------------------------------------------------------------
-- Items, containers, use
------------------------------------------------------------------------

-- AddedTo(item, holder, addType). Not for a gold stack: its count is the whole stack after the merge, and a
-- payment adds the split-off coins to the payer first (w11 L1: a +52 g sale gave "item_added Gold count=402", paying
-- 48 g "item_added Gold count=48"). GoldChanged below reports gold.
listen("AddedTo", 3, function(item, holder, _)
    local h = V.uuid(holder)
    if not ctx().is_party[h or ""] then return end
    if type(item) == "string" and item:find("^LOOT_Gold") then return end
    local u = V.uuid(item)
    local data = { owner = ref(h), item = ref(u) }
    local n = osi(Osi.GetStackAmount, u)
    if n and n > 1 then data.count = n end
    -- a quest item (the inventory's story mark, get_inventory "story"): progress.py scores the first one in hand
    local ie = V.entity(u)
    pcall(function() if ie and ie.ServerItem and ie.ServerItem.StoryItem then data.story = true end end)
    RPC.push("item_added", data)
end)

-- Doors/walls opening in view: by a script too (Grand Mausoleum: the 3rd right press opens two
-- Stone Walls, PROC_ItemUnlockAndOpen). A player sees and hears them slide.
-- One the party opened itself is in the interact result already ("done — opened").
E.DOOR_OPEN_M = 30
E.partyUse = E.partyUse or {}
listen("UseStarted", 2, function(char, item)
    local c = V.uuid(char)
    if ctx().is_party[c or ""] then U.sided(E, "partyUse")[V.uuid(item) or ""] = Ext.Utils.MonotonicTime() end
end)

listen("Opened", 1, function(item)
    local u = V.uuid(item)
    local e = V.entity(u)
    if not e then return end
    if V.itemKind(u, e) == "door" then
        local partyUse = U.sided(E, "partyUse")
        local used = partyUse[u]
        partyUse[u] = nil
        if used and Ext.Utils.MonotonicTime() - used < 15000 then return end
        if nearParty(u, E.DOOR_OPEN_M) and V.itemVisible(ctx(), u, e) then RPC.push("door_opened", { item = ref(u) }) end
        return
    end
    local c = ctx()
    -- Only containers the party opened near itself are "on screen".
    local p = V.pos(e)
    if not p then return end
    local near = false
    for _, o in ipairs(c.observers) do
        if o.pos and U.dist(o.pos, p) < 4.0 then near = true end
    end
    if not near then return end
    U.sset(E, "openContainer", { uuid = u, t = Ext.Utils.MonotonicTime() })
    RPC.push("opened", { item = ref(u) })
end)

listen("Closed", 1, function(item)
    local u = V.uuid(item)
    local open = U.sget(E, "openContainer")
    if open and open.uuid == u then
        U.sset(E, "openContainer", nil)
        RPC.push("closed", { item = ref(u) })
    end
end)

-- UseFinished(character, item, success)
listen("UseFinished", 3, function(char, item, success)
    local c = V.uuid(char)
    if not ctx().is_party[c or ""] then return end
    RPC.push("used", { char = ref(c), item = ref(V.uuid(item)), success = truthy(success) })
end)

------------------------------------------------------------------------
-- World / rest
------------------------------------------------------------------------

listen("LevelGameplayStarted", 2, function(level, _)
    ctxCache = nil
    U.sset(E, "trapsPrimed", false)
    -- Fairness: the agent sees the region name a player sees (loading screen / map), not the level id.
    RPC.push("level_changed", { region = BG3AI.State and BG3AI.State.REGION_NAMES[level] or level })
end)

listen("TeleportedToCamp", 1, function(char)
    local u = V.uuid(char)
    if ctx().is_party[u or ""] then RPC.push("teleported", { char = ref(u), to = "camp" }) end
end)

-- The camp's End-the-Day handshake (GLO_Camp.txt): HUD button -> EndTheDayRequested -> the engine's gather
-- checks (RequestGatherAtCampSuccess/Fail) -> story checks -> RequestEndTheDay -> RequestEndTheDaySuccess/Fail ->
-- night mode / rest screen. S028 15:52-15:59 + S029 16:04: long_rest "rest screen didn't open" x7 with no box and
-- nothing on screen; these events say where the request died (a player sees the refusal notice the same way).
local REST_FLOW = {
    { "EndTheDayRequested", 1, "requested" }, { "RequestGatherAtCampFail", 1, "gather_failed" },
    { "RequestGatherAtCampSuccess", 1, "gathered" }, { "RequestEndTheDayFail", 0, "end_day_failed" },
    { "RequestEndTheDaySuccess", 0, "end_day_ok" }, { "LongRestStartFailed", 0, "rest_start_failed" },
    { "LongRestCancelled", 0, "rest_cancelled" },
    -- S031 17:15: can_rest clean, requested -> gathered, then no answer for 14 s and no "End the day?" box (the
    -- engine's ready check, shown within ~1 s in every success S024-S026). Did a ready check run, how did it end,
    -- and did the engine deny the rest with a reason? (arg 1 is kept: the check's id / the deny reason)
    { "ReadyCheckPassed", 1, "ready_ok" }, { "ReadyCheckFailed", 1, "ready_failed" },
    { "RequestLongRestDeny", 1, "rest_denied" }, { "RequestLongRestWaiting", 0, "rest_waiting" },
    { "LongRestStarted", 0, "rest_started" },
}
E.restFlow = E.restFlow or {}
for _, ev in ipairs(REST_FLOW) do
    listen(ev[1], ev[2], function(a1)
        local step = ev[3]
        if ev[2] == 1 and step ~= "requested" and step ~= "gathered" and step ~= "gather_failed" and a1 ~= nil then
            step = step .. ":" .. tostring(a1):sub(1, 60)
        end
        local flow = U.sided(E, "restFlow")
        flow[#flow + 1] = { step = step, at = Ext.Utils.MonotonicTime() }
        if #flow > 20 then table.remove(flow, 1) end
        RPC.push("rest_flow", { step = step })
    end)
end

listen("LongRestFinished", 0, function()
    if BG3AI.Guard and BG3AI.Guard.item_rest then BG3AI.Guard.item_rest("long") end
    RPC.push("rest", { type = "long" })
end)

listen("ShortRested", 1, function(char)
    local u = V.uuid(char)
    if BG3AI.Guard and BG3AI.Guard.item_rest then BG3AI.Guard.item_rest("short", u) end
    if ctx().is_party[u or ""] then RPC.push("rest", { type = "short", char = ref(u) }) end
end)

------------------------------------------------------------------------
-- Gold: the game's GoldChanged(holder, delta), one event per change of the party's total (a move between party
-- members nets out), pushed as it happens so the action's own result has it (the old 1 Hz poll of PartyGetGold
-- was never seen in a result: w11 L1 trades showed only the gold stack's item_added).
------------------------------------------------------------------------

E.lastGold = E.lastGold or nil

listen("GoldChanged", 2, function(holder, delta)
    local d = math.floor(tonumber(delta) or 0)
    if d == 0 or not ctx().is_party[V.uuid(holder) or ""] then return end
    local total = tonumber(osi(Osi.PartyGetGold, ctx().party[1]))
    if not total then return end
    total = math.floor(total)
    local before = U.sget(E, "lastGold") or (total - d)
    U.sset(E, "lastGold", total)
    if total ~= before then RPC.push("gold_changed", { gold = total, delta = total - before }) end
end)

------------------------------------------------------------------------
-- Polled facts: newly spotted traps near the party.
------------------------------------------------------------------------

E.knownTraps = E.knownTraps or {}
E.lastPoll = 0

local function pollOnce(t)
    local c = V.context()
    if not U.side() then ctxCache, ctxTime = c, t end
    local host = c.party[1]
    if not host then return end
    E.seedApproval(c)
    -- A trap became discovered near the party (the orange outline appears).
    local center = c.observers[1] and c.observers[1].pos
    if center and V.index.ready then
        for _, u in ipairs(V.itemsAround(center, 18)) do
            local known = U.sided(E, "knownTraps")
            if not known[u] and truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u)) then
                known[u] = true
                local e = V.entity(u)
                local p = e and V.pos(e)
                -- V.itemVisible, not V.perceived alone: it also notes the trap as seen (V.staticSeen), so a disarm
                -- right after the stop finds it known (V.itemKnown). S064 11:37 / S065 11:40: "spotted e302", the
                -- walk stopped 10 m short, then disarm(e302) "target is not visible" (the CanSee that spotted it was
                -- gone) until the agent walked to 3-5 m.
                if U.sget(E, "trapsPrimed") and p and V.itemVisible(c, u, e, p) then
                    RPC.push("spotted", { item = ref(u), kind = "trap", pos = U.vec(p, 1) })
                end
            end
        end
        -- First pass only learns traps that were already discovered before we loaded.
        U.sset(E, "trapsPrimed", true)
    end
end

-- Once a second: each side's own poll in an arena (its approval seed, its traps).
local function poll()
    local t = Ext.Utils.MonotonicTime()
    if t - E.lastPoll < 1000 then return end
    E.lastPoll = t
    if Ext.Utils.GetGameState and tostring(Ext.Utils.GetGameState()) ~= "Running" then return end
    U.each_side(pollOnce, t)
end

if not E.tickSubscribed then
    E.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        E.tickN = (E.tickN or 0) + 1
        local ok, err = pcall(poll)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] events poll: " .. tostring(err)) end
    end)
end

RPC.register("state.open_container", function()
    local open = U.sget(E, "openContainer")
    return open and { uuid = open.uuid, name = V.name(open.uuid) } or {}
end)

-- The engine's own long-rest validation per party member: CanDoRest.LongRestErrorFlags (RestErrorFlags:
-- GlobalDisabled, Script, Dialog, Combat, FTB, NotEnoughResources, DownedOrDead, DropInCCActive, Location). The HUD
-- greys the Long Rest button and its tooltip names the reason, so a player sees these. S030 16:30: the
-- End-the-Day handshake ran (requested -> gathered) and the engine answered RequestEndTheDayFail ~10 s later.
local function flagList(f)
    if type(f) ~= "table" and type(f) ~= "userdata" then return {} end
    local out = {}
    local ok = pcall(function() for _, n in ipairs(f) do out[#out + 1] = tostring(n) end end)
    if not ok or #out == 0 then
        local ok2, parsed = pcall(function() return Ext.Json.Parse(Ext.Json.Stringify(f)) end)
        out = (ok2 and type(parsed) == "table") and parsed or {}
    end
    return out
end
E.flagList = flagList

-- The short-rest side (REVIEW-13 item 12): CanDoRest.ShortRestErrorFlags per member and the GlobalShortRestDisabled
-- component, what the HUD's greyed Short Rest button shows. short_read is false when any member's field didn't read.
function E.canRest()
    local blocked, shortBlocked, shortRead = {}, {}, true
    for _, u in ipairs(V.context().party) do
        local e = V.entity(u)
        local c = e and U.comp(e, "CanDoRest")
        local flags = c and flagList(c.LongRestErrorFlags) or {}
        if #flags > 0 then blocked[#blocked + 1] = { name = V.name(u), uuid = u, flags = flags } end
        local okS, sf = pcall(function() return c.ShortRestErrorFlags end)
        if not (c and okS and sf ~= nil) then shortRead = false end
        local sflags = (c and okS) and flagList(sf) or {}
        if #sflags > 0 then shortBlocked[#shortBlocked + 1] = { name = V.name(u), uuid = u, flags = sflags } end
    end
    local okG, gs = pcall(Ext.Entity.GetAllEntitiesWithComponent, "GlobalShortRestDisabled")
    local ok, g = pcall(Ext.Entity.GetAllEntitiesWithComponent, "GlobalLongRestDisabled")
    local states = {}
    local ok2, ls = pcall(Ext.Entity.GetAllEntitiesWithComponent, "LongRestState")
    if ok2 then
        for _, e in ipairs(ls) do
            pcall(function() states[#states + 1] = Ext.Json.Stringify(e.LongRestState, { Beautify = false,
                MaxDepth = 2, IterateUserdata = true, AvoidRecursion = true }):sub(1, 300) end)
        end
    end
    return { blocked = blocked, global_disabled = ok and #g or -1, long_rest_state = states,
        short_read = shortRead, short_blocked = shortBlocked, short_global_disabled = okG and #gs or -1 }
end

-- camp.can_rest -> {blocked=[{name, flags}], global_disabled, long_rest_state, short_read, short_blocked,
-- short_global_disabled}: asked before the rest button press, and by a blocked camp trip (short rest's state).
RPC.register("camp.can_rest", function() return E.canRest() end)

-- Who the camp story still wants a talk with before the night can end (GLO_Camp.txt "Required talks":
-- DB_Camp_RequiredTalks(character, dialog); the story shows the "!" over each). At night the sleep press runs
-- PROC_Camp_TryLeaveNightMode_CampNightCheck: while any row is left it only opens the "talks left" message box and
-- the night goes on; a row clears when that character joins a conversation (DialogActorJoined). launch-v1 haiku
-- act3_gold_max: 46 long_rest "the night at camp didn't end" until the agent happened to talk to Shadowheart (r2,
-- r4) or Withers (r5). -> {{uuid, name} | {} per character}: named when in the party or perceived (the "!" is on
-- screen), else an empty row (counted, not named). nil when the DB can't be read.
function E.requiredTalks()
    local ok, rows = pcall(function() return Osi.DB_Camp_RequiredTalks:Get(nil, nil) end)
    if not ok or type(rows) ~= "table" then return nil end
    local out, seen = {}, {}
    for _, r in ipairs(rows) do
        local u = V.uuid(r[1])
        if u and not seen[u] then
            seen[u] = true
            out[#out + 1] = visibleChar(u) and ref(u) or {}
        end
    end
    return out
end

-- camp.rest_flow {since_ms?} -> {steps=[...], step_ms=[...], story={...}, can_rest={...}, required_talks}: the
-- End-the-Day handshake steps seen recently (step_ms: how long ago each one fired), the camp story flags and the
-- engine's rest validation (diagnostics for the bridge's metrics log; can_rest's reasons are shown to the agent),
-- and E.requiredTalks (shown: travel.py night_talks_left).
RPC.register("camp.rest_flow", function(args)
    local now = Ext.Utils.MonotonicTime()
    local since = now - (tonumber(args and args.since_ms) or 30000)
    local steps, stepMs = {}, {}
    for _, f in ipairs(U.sget(E, "restFlow") or {}) do
        if f.at >= since then steps[#steps + 1], stepMs[#stepMs + 1] = f.step, math.floor(now - f.at) end
    end
    local story = {}
    for _, n in ipairs({ "DB_GoingToCamp", "DB_CampRequestHandled", "DB_Camp_NightMode", "DB_CAMP_JustWokeUp",
                         "DB_Camp_GoingToSleep", "DB_Camp_LongRest_CodeIsWaiting", "DB_PlayerInCamp" }) do
        local ok, rows = pcall(function() return Osi[n]:Get(nil) end)
        story[n] = ok and rows and #rows or -1
    end
    local okc, can = pcall(E.canRest)
    return { steps = steps, step_ms = stepMs, story = story, can_rest = okc and can or nil,
             required_talks = E.requiredTalks() }
end)

return E
