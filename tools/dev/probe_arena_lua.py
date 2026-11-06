"""The Lua the two-team probe (tools/dev/probe_arena.py) sends: the server library it installs (Osiris listeners, reads,
argument-form fallback) and the client read of an open reaction prompt, and the Lua literals it builds them with."""

import json
from typing import Any


def lua(v: Any) -> str:
    """A Lua literal for a str / number / bool / None / list / dict (JSON's string escapes are valid Lua for these)."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(lua(x) for x in v) + "}"
    if isinstance(v, dict):
        return "{" + ", ".join(f"[{lua(k)}] = {lua(x)}" for k, x in v.items()) + "}"
    return json.dumps(str(v))


# q9: the mod's own arena calls (server) and what the client's Lua state holds of the teams. The debugger evaluates in
# its own env: the mod's table is Mods.BG3AI.BG3AI there (a bare BG3AI is nil).
MOD = "Mods.BG3AI.BG3AI"
SERVER_TRY = (
    "local ok, r = pcall(function() return {call} end)\n"
    "return Ext.Json.Stringify({{ok = ok, error = (not ok) and tostring(r) or nil}})"
)
CLIENT_ARENA = (
    f"local Ar = {MOD}.Arena\n"
    "return Ext.Json.Stringify({state = Ar.state ~= nil, active = Ar.active(), names = Ar.state and Ar.state.names})"
)

INSTALL = r"""
local B = Mods.BG3AI.BG3AI
local P = B.ArenaProbe
if not P then
P = { log = {}, n = 0, hooks = {}, t0 = Ext.Utils.MonotonicTime() }
B.ArenaProbe = P
local LOG_CAP = 4000
function P.now() return Ext.Utils.MonotonicTime() - P.t0 end
function P.uuid(g)
    if g == nil then return nil end
    return tostring(g):match("(%x+%-%x+%-%x+%-%x+%-%x+)$")
end
function P.name(u)
    local ok, n = pcall(function() return Ext.Loca.GetTranslatedString(Osi.GetDisplayName(u)) end)
    if ok and n and n ~= "" then return n end
    return nil
end
function P.js(x)
    local ok, s = pcall(Ext.Json.Stringify, x, { Beautify = false })
    if ok then return s end
    return Ext.Json.Stringify({ error = "unserializable: " .. tostring(s) })
end
local function q(fn, ...)
    local args, n = { ... }, select("#", ...)
    local ok, v = pcall(function() return Osi[fn](table.unpack(args, 1, n)) end)
    if ok then return v end
    return nil
end
P.q = q
-- Osi.<fn> with the first argument form the build accepts.
function P.call(fn, forms)
    local last
    for i, form in ipairs(forms) do
        local ok, r = pcall(function() return Osi[fn](table.unpack(form, 1, form.n or #form)) end)
        if ok then return { fn = fn, ok = true, form = i, ret = r ~= nil and tostring(r) or nil } end
        last = tostring(r)
    end
    return { fn = fn, ok = false, err = last }
end
-- First column of a story database (1-3 columns tried).
function P.col1(db)
    for n = 1, 3 do
        local ok, rows = pcall(function() return Osi[db]:Get(table.unpack({}, 1, n)) end)
        if ok and type(rows) == "table" then
            local out = {}
            for _, row in ipairs(rows) do out[#out + 1] = P.uuid(row[1]) end
            return out
        end
    end
    return nil
end
function P.chars()
    local seen, out = {}, {}
    for _, db in ipairs({ "DB_PartyMembers", "DB_Players", "DB_Avatars", "DB_Origins" }) do
        for _, u in ipairs(P.col1(db) or {}) do
            if u and not seen[u] then seen[u] = true; out[#out + 1] = u end
        end
    end
    return out
end
function P.who(s)
    if P.uuid(s) then return P.uuid(s) end
    local want = s:lower():gsub("[^%w]", "")
    for _, u in ipairs(P.chars()) do
        local n = (P.name(u) or ""):lower():gsub("[^%w]", "")
        if n ~= "" and n:sub(1, #want) == want then return u end
    end
    error("no player, party member or origin named " .. s)
end
local function inDb(db, u)
    for _, v in ipairs(P.col1(db) or {}) do if v == u then return true end end
    return false
end
function P.facts(u)
    local f = { uuid = u, name = P.name(u) }
    f.in_party_db, f.in_players_db, f.avatar = inDb("DB_PartyMembers", u), inDb("DB_Players", u), inDb("DB_Avatars", u)
    f.player, f.party_member, f.controlled = q("IsPlayer", u), q("IsPartyMember", u, 0), q("IsControlled", u)
    f.user, f.faction, f.base_faction = q("GetReservedUserID", u), q("GetFaction", u), q("GetBaseFaction", u)
    f.hp, f.max_hp, f.dead = q("GetHitpoints", u), q("GetMaxHitpoints", u), q("IsDead", u)
    f.level = q("GetLevel", u)
    f.downed, f.on_stage, f.immortal = q("HasActiveStatus", u, "DOWNED"), q("IsOnStage", u), q("IsImmortal", u)
    f.in_combat, f.combat, f.can_join = q("IsInCombat", u), q("CombatGetGuidFor", u), q("CanJoinCombat", u)
    pcall(function() f.pos = { Osi.GetPosition(u) } end)
    pcall(function() -- name -> {"<level>": amount}: string keys, so a level-0 row always serialises as an object
        f.res = {}
        for name, lv in pairs(B.Guard.resources(u)) do
            f.res[name] = {}
            for l, amt in pairs(lv) do f.res[name][tostring(l)] = amt end
        end
    end)
    pcall(function()
        local tb = Ext.Entity.Get(u).TurnBased
        f.tb = { active = tb.IsActiveCombatTurn, requested_end = tb.RequestedEndTurn, can_act = tb.CanActInCombat,
            acted = tb.ActedThisRoundInCombat }
    end)
    return f
end
function P.many(us)
    local out = {}
    for i, u in ipairs(us) do out[i] = P.facts(u) end
    return out
end
-- The raw turn order of u's combat (every group, unfiltered: harness evidence, never shown to an agent).
function P.order(u)
    local out = {}
    local ok, err = pcall(function()
        local to = Ext.Entity.Get(u).CombatParticipant.CombatHandle.TurnOrder
        out.round, out.groups = to.Round, {}
        for gi, g in ipairs(to.CurrentGroups) do
            local mem = {}
            for _, m in ipairs(g.Members) do
                local mu = P.uuid(m.Entity.Uuid.EntityUuid)
                local okI, ini = pcall(function() return m.Initiative end)
                mem[#mem + 1] = { uuid = mu, name = mu and P.name(mu), initiative = okI and ini or nil }
            end
            out.groups[gi] = mem
        end
    end)
    if not ok then out.error = tostring(err) end
    return out
end
-- Every faction in static data (name, guid, the Osiris form) and how many level characters use it.
function P.factions(fa)
    local used = {}
    pcall(function()
        for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter") or {}) do
            local u = P.uuid(e.Uuid.EntityUuid)
            local f = u and q("GetFaction", u)
            if f then used[f] = (used[f] or 0) + 1 end
        end
    end)
    local out = {}
    for _, g in ipairs(Ext.StaticData.GetAll("Faction") or {}) do
        local okF, r = pcall(Ext.StaticData.Get, g, "Faction")
        local name = okF and r and r.Faction or nil
        local osi = name and (name .. "_" .. tostring(g)) or tostring(g)
        out[#out + 1] = { name = name, guid = tostring(g), osi = osi, used = used[osi] or 0,
            to_a = fa and q("GetRelation", fa, osi), from_a = fa and q("GetRelation", osi, fa) }
    end
    return out
end
-- The loaded mods, in load order.
function P.mods()
    local out = {}
    for _, id in ipairs(Ext.Mod.GetLoadOrder() or {}) do
        local okn, name = pcall(function() return Ext.Mod.GetMod(id).Info.Name end)
        out[#out + 1] = { uuid = tostring(id), name = okn and name or nil }
    end
    return out
end
-- Force an initiative tie: u's entries in its combat's turn order (group and member) take initiative `ini`.
function P.set_initiative(u, ini)
    local out = { writes = {} }
    local ok, err = pcall(function()
        local ce = Ext.Entity.Get(u).CombatParticipant.CombatHandle
        local to = ce.TurnOrder
        for _, list in ipairs({ "Groups", "CurrentGroups" }) do
            local okl, groups = pcall(function() return to[list] end)
            for gi, g in ipairs(okl and groups or {}) do
                for _, m in ipairs(g.Members) do
                    if P.uuid(m.Entity.Uuid.EntityUuid) == u then
                        local okm = pcall(function() m.Initiative = ini end)
                        local okg = pcall(function() g.Initiative = ini end)
                        out.writes[#out.writes + 1] = { list = list, group = gi, member = okm, group_field = okg }
                    end
                end
            end
        end
        out.replicated = pcall(function() ce:Replicate("TurnOrder") end)
    end)
    if not ok then out.error = tostring(err) end
    return out
end
local function rec(ev, ...)
    local a, names = {}, {}
    for i = 1, select("#", ...) do
        local v = select(i, ...)
        a[i] = v ~= nil and tostring(v) or "nil"
        local u = P.uuid(v)
        if u then names[i] = P.name(u) end
    end
    P.n = P.n + 1
    P.log[P.n] = { i = P.n, t = P.now(), ev = ev, args = a, names = names }
    P.log[P.n - LOG_CAP] = nil
end
P.EVENTS = { { "TurnStarted", 1 }, { "TurnEnded", 1 }, { "CombatStarted", 1 }, { "CombatRoundStarted", 2 },
    { "CombatEnded", 1 }, { "EnteredCombat", 2 }, { "LeftCombat", 2 }, { "EnterCombatFailed", 2 },
    { "SwitchedCombat", 3 }, { "FleeFromCombat", 2 }, { "CombatTurnTimedOut", 1 }, { "ShareInitiative", 1 },
    { "DownedChanged", 2 }, { "Dying", 1 }, { "Died", 1 }, { "Resurrected", 1 }, { "AttackedBy", 7 },
    { "CastSpell", 5 }, { "UsingSpell", 5 }, { "CastedSpell", 5 }, { "ReactionInterruptAdded", 2 },
    { "ReactionInterruptActionNeeded", 1 }, { "ReactionInterruptUsed", 3 }, { "RelationChanged", 4 },
    { "BaseFactionChanged", 3 }, { "TemporaryHostileRelationRemoved", 3 }, { "CharacterJoinedParty", 1 },
    { "CharacterLeftParty", 1 }, { "CharacterMadePlayer", 1 }, { "GainedControl", 1 }, { "Teleported", 9 },
    { "RestorePartyFinished", 0 }, { "EnteredForceTurnBased", 1 }, { "LeftForceTurnBased", 1 } }
for _, e in ipairs(P.EVENTS) do
    local ok, err = pcall(Ext.Osiris.RegisterListener, e[1], e[2], "after", function(...) rec(e[1], ...) end)
    P.hooks[e[1]] = ok and true or tostring(err)
end
function P.since(i)
    local out = {}
    for k = math.max(i + 1, P.n - LOG_CAP + 1), P.n do out[#out + 1] = P.log[k] end
    return out
end
end
"""


# The open reaction prompt's first event, as far as the view model shows it (client; best effort).
POPUP = r"""
local UI = Mods.BG3AI.BG3AI.UI
local out = {}
local ok, err = pcall(function()
    local w = UI.widgetMap().ReactionDecisionPopup
    local ev = UI.path(w.DataContext, "Data.InterruptEvents.1")
    for k, v in pairs(UI.props(ev) or {}) do out[tostring(k)] = tostring(v) end
end)
if not ok then out.error = tostring(err) end
return Ext.Json.Stringify(out, { Beautify = false })
"""


# q10: u's combat's turn groups, read and split. Sent before the step's code on every eval (outside INSTALL's
# install-once guard), so a game that already holds an older probe library still gets these.
# TurnOrder (bg3se GameDefinitions/Components/Combat.h): CurrentGroups / NextRoundGroups are arrays of
# TurnBasedGroup {Members = {Entity, Initiative}, Team (guid), Round, Initiative, IsPlayer}; each member's
# TurnBased.CombatTeam and CombatParticipant.CombatGroupId / InitiativeRoll are read with it. An Array is written
# whole from a Lua table (bg3se Lua/Helpers/LuaUnserialize.h: clear, then push_back each).
GROUPS = r"""
local P = Mods.BG3AI.BG3AI.ArenaProbe
local function s(x) if x == nil then return nil end return tostring(x) end
local function turn_order(u)
    local ce = Ext.Entity.Get(u).CombatParticipant.CombatHandle
    return ce, ce.TurnOrder
end
P.GROUP_LISTS = { current = "CurrentGroups", next = "NextRoundGroups" }
function P.turn_groups(u)
    local out = {}
    local ok, err = pcall(function()
        local _, to = turn_order(u)
        out.round = to.Round
        for key, list in pairs(P.GROUP_LISTS) do
            local rows = {}
            for gi, g in ipairs(to[list]) do
                local mem = {}
                for _, m in ipairs(g.Members) do
                    local e = m.Entity
                    local mu = P.uuid(e.Uuid.EntityUuid)
                    local row = { uuid = mu, name = mu and P.name(mu), initiative = m.Initiative }
                    pcall(function() row.combat_team = s(e.TurnBased.CombatTeam) end)
                    pcall(function()
                        local cp = e.CombatParticipant
                        row.combat_group, row.roll = s(cp.CombatGroupId), cp.InitiativeRoll
                    end)
                    row.user = P.q("GetReservedUserID", mu)
                    mem[#mem + 1] = row
                end
                rows[gi] = { team = s(g.Team), initiative = g.Initiative, is_player = g.IsPlayer, round = g.Round,
                    members = mem }
            end
            out[key] = rows
        end
    end)
    if not ok then out.error = tostring(err) end
    return out
end
function P.fresh_guid()
    local h = function(n)
        local t = {}
        for i = 1, n do t[i] = string.format("%x", math.random(0, 15)) end
        return table.concat(t)
    end
    return h(8) .. "-" .. h(4) .. "-4" .. h(3) .. "-a" .. h(3) .. "-" .. h(12)
end
-- Split one list of u's turn groups ("current" / "next") by side: a group whose members span both sides becomes one
-- group per run of same-side members, in the order the game holds them. The first run keeps the group's Team; each
-- later run takes its first member's own TurnBased.CombatTeam when no other group has it, else a fresh guid, and its
-- members' TurnBased.CombatTeam is set to that team. sides: {uuid = "A" | "B"}; others count as their own side.
function P.split_groups(u, sides, key)
    local list = P.GROUP_LISTS[key]
    local out = { list = list, groups_before = 0, team_writes = {} }
    local ok, err = pcall(function()
        local ce, to = turn_order(u)
        local new, used = {}, {}
        for _, g in ipairs(to[list]) do used[s(g.Team)] = true end
        for _, g in ipairs(to[list]) do
            out.groups_before = out.groups_before + 1
            local runs, cur = {}, nil
            for _, m in ipairs(g.Members) do
                local mu = P.uuid(m.Entity.Uuid.EntityUuid)
                local sd = sides[mu] or ("other:" .. tostring(mu))
                if not cur or cur.side ~= sd then
                    cur = { side = sd, members = {} }
                    runs[#runs + 1] = cur
                end
                local okt, ct = pcall(function() return s(m.Entity.TurnBased.CombatTeam) end)
                cur.members[#cur.members + 1] = { Entity = m.Entity, Initiative = m.Initiative, uuid = mu,
                    team = okt and ct or nil }
            end
            if #runs == 0 then
                new[#new + 1] = { Members = {}, Team = s(g.Team), Round = g.Round, Initiative = g.Initiative,
                    IsPlayer = g.IsPlayer }
            end
            for ri, r in ipairs(runs) do
                local team = s(g.Team)
                if ri > 1 then
                    local own = r.members[1].team
                    team = (own and not used[own]) and own or P.fresh_guid()
                    used[team] = true
                end
                local members = {}
                for _, x in ipairs(r.members) do
                    members[#members + 1] = { Entity = x.Entity, Initiative = x.Initiative }
                    if ri > 1 and x.team ~= team then
                        out.team_writes[#out.team_writes + 1] = { uuid = x.uuid, from = x.team, to = team }
                    end
                end
                new[#new + 1] = { Members = members, Team = team, Round = g.Round, Initiative = r.members[1].Initiative,
                    IsPlayer = g.IsPlayer }
            end
        end
        out.groups_written = #new
        Ext.Types.Unserialize(to[list], new)
        out.groups_after = #to[list]
        for _, w in ipairs(out.team_writes) do
            w.ok, w.err = pcall(function()
                local e = Ext.Entity.Get(w.uuid)
                e.TurnBased.CombatTeam = w.to
                e:Replicate("TurnBased")
            end)
            w.err = (not w.ok) and tostring(w.err) or nil
        end
        out.replicated = pcall(function() ce:Replicate("TurnOrder") end)
    end)
    if not ok then out.error = tostring(err) end
    return out
end
"""
