"""Live probe: can two teams of 4 player characters fight each other in one turn-based combat, each side acting
through the mod's own action ops? One script for the 8 spike questions, in their order; no model.

It drives the game through the Lua debugger port: raw server Lua for the setup calls and the reads (a probe library
installed as Mods.BG3AI.BG3AI.ArenaProbe, with Osiris listeners that log every combat/turn/reaction/faction event),
and the mod's act.* jobs (act.move_to, act.cast, act.end_turn) for the actions, the same ops the bridge's tools send.
Each question writes one evidence file <out>/<q>.json: {"question", "verdict" (PASS / FAIL / OPEN), "why", the
reads, the calls with what each returned, the event log}; teams.json carries the teams between steps.

Run it on a THROWAWAY load of a copy of the save (it changes factions, the party, positions and HP; save nothing),
from the repo root, the game loaded with the pinned mod set only:
    P="uv run --project bridge python tools/dev/probe_arena.py"
    $P conns [--conns 6]                         # q1: that many debugger connections at once, each evaluating
    $P roster                                    # the candidates: who is a player / in the party / their factions
    $P factions                                  # the faction table (static data) + who in the level uses which
    $P setup --team-a Tav,Shadowheart,Gale,Astarion --team-b Karlach,Wyll,Laezel,Halsin \\
       --spot-a X,Y,Z --spot-b X,Y,Z [--seed 7] [--pinned-mods a,b]
                                                 # q2: 8 in DB_PartyMembers (+ the loaded mods), chains, places, HP
    $P hostile [--faction-b auto|<Name_guid>]    # q3: team B's faction, IsEnemy across teams, one combat
    $P turns [--turns 10 --idle-s 8 --down-at 4] # q3: who starts each turn, does the game act for anyone
    $P act                                       # q4: move + attack + end turn for one member of each side
    $P tie [--no-force]                          # q5: an initiative tie across teams (seen, else forced): one turn?
    $P end [--loser B]                           # q6: the loser team downed (death-save turns, HUD), then dead
    $P gameover                                  # q6: the avatar downed, then dead (a fresh load + setup + hostile)
    $P react                                     # q7: an opportunity attack, who gets the prompt, ui.react
    $P npc                                       # q8: one team-B member made an NPC (a fresh load + setup + hostile)
    $P all ...setup args...                      # conns, roster, factions, setup, hostile, act, tie, turns, react, end
`--dry-run` prints the plan of a step only (no game). Evidence: runtime/probes/arena-s0/ (--out), one
q<n>-<what>.json per question.
Spots: open ground with no NPCs near, the two ~8-20 m apart (q4's attack needs the mover to reach melee in one turn:
under ~8 m for most classes; 20 m is fine for the rest).
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "runtime" / "probes" / "arena-s0"
SIDES = ("A", "B")
NULL = "NULL_00000000-0000-0000-0000-000000000000"
HOSTILE = 0  # Osiris relation values: 0 hostile, 50 neutral, 100 allied (DOS2's convention; the probe reads them back)
LINE_GAP_M = 1.5  # members of a side stand this far apart on a line through their spot
AT_SPOT_M = 3.0  # a member counts as placed within this of its slot
MOVED_M = 0.5  # an idle turn's character moved: the game acted for it
SETTLE_S = 2.0
TURN_WAIT_S = 25.0  # after an end_turn: the next TurnStarted within this
REACH_M = 2.5  # q7: the mover counts as in melee of the enemy within this
CONNS = 6  # q1: debugger connections opened at once (two bridges with their side connections + the harness)

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


# ---- Lua building (pure) ----


def lua(v: Any) -> str:
    """A Lua literal for a str / number / bool / None / list (JSON's string escapes are valid Lua for these)."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(lua(x) for x in v) + "}"
    return json.dumps(str(v))


def call_code(fn: str, *forms: list[Any]) -> str:
    """Osi.<fn> with the first form the build accepts, as JSON {fn, ok, form, ret | err}."""
    return f"return P.js(P.call({lua(fn)}, {lua(list(forms))}))"


def line_slots(spot: list[float], n: int, gap: float = LINE_GAP_M) -> list[list[float]]:
    """n positions on a line along x through spot, centred on it."""
    x, y, z = spot
    return [[round(x + (i - (n - 1) / 2) * gap, 2), y, z] for i in range(n)]


def dist(a: Any, b: Any) -> float:
    if not (isinstance(a, list) and isinstance(b, list) and len(a) >= 3 and len(b) >= 3):
        return math.inf
    return math.dist(a[:3], b[:3])


def toward(src: list[float], dst: list[float], m: float) -> list[float]:
    """The point m metres from src toward dst (y kept)."""
    d = math.dist([src[0], src[2]], [dst[0], dst[2]]) or 1.0
    k = m / d
    return [round(src[0] + (dst[0] - src[0]) * k, 2), src[1], round(src[2] + (dst[2] - src[2]) * k, 2)]


def away(src: list[float], frm: list[float], m: float) -> list[float]:
    return toward(src, [2 * src[0] - frm[0], src[1], 2 * src[2] - frm[2]], m)


def spot_arg(s: str) -> list[float]:
    parts = [float(x) for x in s.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"want X,Y,Z, got {s!r}")
    return parts


def truthy(v: Any) -> bool:
    return v not in (None, 0, False, "0", "")


# ---- judges (pure: tested offline) ----

Verdict = tuple[str, str]


def side_of(teams: dict[str, Any], u: str | None) -> str | None:
    return next((s for s in SIDES if u in teams[s]), None)


def pick_faction_b(factions: list[dict[str, Any]], fa: str | None) -> str | None:
    """auto: a Hero faction other than team A's, preferring one nobody in the level uses (multiplayer's spare player
    factions), then the lowest relation to A."""
    hero = [f for f in factions if "hero" in str(f.get("name") or "").lower() and f.get("osi") != fa]
    if not hero:
        return None
    hero.sort(key=lambda f: (f.get("used") or 0, f.get("to_a") if isinstance(f.get("to_a"), int) else 100))
    return hero[0]["osi"]


def judge_setup(
    teams: dict[str, Any], facts: dict[str, dict[str, Any]], slots: dict[str, list[list[float]]]
) -> Verdict:
    bad = []
    for s in SIDES:
        for u, slot in zip(teams[s], slots[s], strict=True):
            f = facts.get(u) or {}
            who = f.get("name") or u[:8]
            if not truthy(f.get("in_party_db")):
                bad.append(f"{who} not in DB_PartyMembers")
            if dist(f.get("pos"), slot) > AT_SPOT_M:
                bad.append(f"{who} {dist(f.get('pos'), slot):.1f} m from its slot")
            if f.get("hp") is None or f.get("hp") != f.get("max_hp"):
                bad.append(f"{who} hp {f.get('hp')}/{f.get('max_hp')}")
    if bad:
        return "FAIL", "; ".join(bad)
    return "PASS", "8 party members, each at its slot with full HP"


def judge_conns(rows: list[dict[str, Any]], want: int) -> Verdict:
    """q1: every connection opened and answered while all were open."""
    ok = [r for r in rows if r.get("connected") and r.get("answer") is not None]
    if len(ok) == want:
        return "PASS", f"{want} connections open at once, each answered"
    bad = "; ".join(f"#{r['i']}: {r.get('error') or 'no answer'}" for r in rows if r not in ok)
    return "FAIL", f"{len(ok)}/{want} answered: {bad}"


def judge_party(
    teams: dict[str, Any], facts: dict[str, dict[str, Any]], mods: list[dict[str, Any]], pinned: list[str] | None
) -> Verdict:
    """q2: all 8 in DB_PartyMembers at once, with no mod loaded beyond the pinned set (when one is given)."""
    out = [
        f.get("name") or u[:8]
        for u in teams["A"] + teams["B"]
        if not truthy((f := facts.get(u) or {}).get("in_party_db"))
    ]
    extra = [
        m.get("name") or m.get("uuid") for m in mods if pinned and not {m.get("name"), m.get("uuid")} & set(pinned)
    ]
    why = []
    if out:
        why.append(f"not in DB_PartyMembers: {', '.join(out)}")
    if extra:
        why.append(f"mods beyond the pinned set: {', '.join(map(str, extra))}")
    if why:
        return "FAIL", "; ".join(why)
    names = ", ".join(str(m.get("name") or m.get("uuid")) for m in mods)
    return "PASS", "8 in DB_PartyMembers at once; " + (
        "only the pinned mods loaded" if pinned else f"{len(mods)} mods loaded (no pinned list given): {names}"
    )


def tie_info(order: dict[str, Any], teams: dict[str, Any]) -> dict[str, Any]:
    """From a raw turn order: groups holding both sides, cross-side members with equal initiative, and cross-side
    neighbours in the order (where a tie can be forced)."""
    groups = order.get("groups") or []
    mixed = [
        [m.get("name") or m.get("uuid") for m in g]
        for g in groups
        if len({side_of(teams, m.get("uuid")) for m in g} - {None}) > 1
    ]
    flat = [m for g in groups for m in g]
    equal = [
        [a.get("uuid"), b.get("uuid")]
        for a, b in itertools.combinations(flat, 2)
        if a.get("initiative") is not None
        and a.get("initiative") == b.get("initiative")
        and {side_of(teams, a.get("uuid")), side_of(teams, b.get("uuid"))} == {"A", "B"}
    ]
    adjacent = [
        [a.get("uuid"), b.get("uuid"), a.get("initiative")]
        for a, b in itertools.pairwise(flat)
        if {side_of(teams, a.get("uuid")), side_of(teams, b.get("uuid"))} == {"A", "B"}
    ]
    return {"mixed_groups": mixed, "equal": equal, "adjacent": adjacent}


def judge_tie(r: dict[str, Any]) -> Verdict:
    """q5: with a cross-team tie in the order, do both sides hold one turn (a mixed group, or both sides' members
    open at once)?"""
    shared = r.get("shared_turns") or []
    mixed = (r.get("after") or {}).get("mixed_groups") or (r.get("before") or {}).get("mixed_groups")
    if shared or mixed:
        return "FAIL", "the teams shared a turn (" + "; ".join(
            [f"group {g}" for g in mixed or []] + [f"open together: {t}" for t in shared]
        ) + "): needs serialising"
    if not ((r.get("after") or {}).get("equal") or (r.get("before") or {}).get("equal")):
        return "OPEN", "no cross-team initiative tie seen or forced: " + str(r.get("force") or "not forced")
    return "PASS", "a cross-team initiative tie, and each side took its own turn"


def judge_hostile(teams: dict[str, Any], facts: dict[str, dict[str, Any]], enemy: dict[str, Any]) -> Verdict:
    combats = {f.get("combat") for f in facts.values()}
    cross = [v for k, v in enemy.items() if k.startswith("AB:")]
    within = [k for k, v in enemy.items() if not k.startswith("AB:") and truthy(v)]
    why = []
    if not cross or not all(truthy(v) for v in cross):
        why.append(f"IsEnemy across the teams: {sum(truthy(v) for v in cross)}/{len(cross)}")
    if within:
        why.append(f"enemies within a team: {', '.join(within)}")
    if len(combats) != 1 or None in combats or NULL in combats:
        why.append(f"combat guids: {sorted(str(c) for c in combats)}")
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", "teams hostile to each other (not within), all 8 in one combat"


def judge_turns(teams: dict[str, Any], turns: list[dict[str, Any]], downed: dict[str, Any] | None) -> Verdict:
    """q3: both sides got turns, no turn group held both sides, each round started in initiative order, and the
    fight went on after a member was downed."""
    why = []
    seen = {side_of(teams, t.get("uuid")) for t in turns}
    if not {"A", "B"} <= seen:
        why.append(f"turns seen for sides {sorted(s for s in seen if s)} only")
    mixed = [t for t in turns if len({side_of(teams, u) for u in t.get("group") or []}) > 1]
    if mixed:
        why.append(f"{len(mixed)} turn group(s) held both sides: " + ", ".join(t.get("name") or "?" for t in mixed))
    rounds: dict[Any, list[float]] = {}
    for t in turns:
        if isinstance(t.get("initiative"), (int, float)):
            rounds.setdefault(t.get("round"), []).append(t["initiative"])
    out_of_order = [r for r, ini in rounds.items() if any(b > a for a, b in itertools.pairwise(ini))]
    if out_of_order:
        why.append(f"rounds {out_of_order} not in initiative order")
    if downed is not None:
        if not downed.get("downed"):
            why.append(f"the downing didn't take ({downed.get('who')})")
        elif not downed.get("combat_after") or not downed.get("turns_after"):
            why.append(
                f"after {downed.get('who')} was downed: combat {downed.get('combat_after')}, "
                f"{downed.get('turns_after')} turns"
            )
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", f"{len(turns)} turns, both sides, separate groups, initiative order" + (
        ", the fight went on after a downing" if downed else ""
    )


def judge_ai(turns: list[dict[str, Any]]) -> Verdict:
    """q3: on no idle turn of a standing member did the game act (move, cast, end the turn) by itself."""
    acted = [
        t
        for t in turns
        if not t.get("downed_at_start")
        and (
            (t.get("idle") or {}).get("moved_m", 0) > MOVED_M
            or (t.get("idle") or {}).get("casts")
            or (t.get("idle") or {}).get("ended_by_itself")
        )
    ]
    if not turns:
        return "OPEN", "no turns watched"
    if acted:
        return "FAIL", "the game acted on idle turns of: " + ", ".join(
            f"{t.get('name')} ({json.dumps(t.get('idle'))})" for t in acted
        )
    return "PASS", f"{len(turns)} idle turns, the game acted for nobody"


def res_amount(res: Any, name: str) -> float | None:
    """A resource's level-0 amount (or the sum over levels) from G.resources' {name: {level: amount}}."""
    lv = (res or {}).get(name) if isinstance(res, dict) else None
    if isinstance(lv, dict) and lv:
        vals = [v for v in lv.values() if isinstance(v, (int, float))]
        return float(sum(vals)) if vals else None
    if isinstance(lv, list):
        vals = [v for v in lv if isinstance(v, (int, float))]
        return float(sum(vals)) if vals else None
    return None


def judge_act(runs: dict[str, dict[str, Any]]) -> Verdict:
    """q4: for each side, the mod's own move and attack jobs ran on its member and the game charged them (Movement
    down after the move, ActionPoint down after the attack), and its end_turn ended the turn."""
    why = []
    for s in SIDES:
        r = runs.get(s)
        if not r:
            why.append(f"side {s}: no turn reached")
            continue
        for step, res_name in (("move", "Movement"), ("cast", "ActionPoint")):
            st = r.get(step) or {}
            b, a = res_amount(st.get("res_before"), res_name), res_amount(st.get("res_after"), res_name)
            if st.get("status") != "done":
                why.append(f"side {s} {step}: {st.get('status')} {st.get('error') or ''}".strip())
            elif b is None or a is None or not a < b:
                why.append(f"side {s} {step}: {res_name} {b} -> {a} (not charged)")
        if (r.get("end") or {}).get("status") != "done":
            why.append(f"side {s} end_turn: {(r.get('end') or {}).get('status')}")
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", "both sides: move and attack through act.* charged as in play, end_turn ended the turn"


def judge_react(r: dict[str, Any]) -> Verdict:
    """q7: did a reaction come up for the enemy the mover left, and could the mod answer it (ui.react)?"""
    if r.get("error"):
        return "OPEN", r["error"]
    ev = {e.get("ev") for e in r.get("events") or []}
    popup = bool(r.get("popup_seen"))
    if popup and (r.get("answer") or {}).get("ok"):
        return "PASS", f"prompt for {r.get('reactor_name')} (side {r.get('reactor_side')}), answered by ui.react"
    if popup:
        return "FAIL", f"prompt came up, ui.react failed: {r.get('answer')}"
    if "ReactionInterruptUsed" in ev or "AttackedBy" in ev:
        return "OPEN", "the reaction fired without a prompt (the reaction setting answers it): set it to ask and rerun"
    return "FAIL", "no prompt and no reaction when the mover left melee"


def judge_end(r: dict[str, Any]) -> Verdict:
    """q6: with the loser team all downed, can a turn boundary read it (every member downed or dead)? What the game
    does then (death-save turns, combat end, Game Over) is recorded; a Game Over in a team wipe is that team's loss."""
    if not r.get("all_down_read"):
        return "FAIL", "the loser team's downed state didn't read as downed/dead for every member"
    seen = [f"death-save turns: {len(r.get('death_save_turns') or [])}"]
    seen.append(
        f"combat ended at the {r['ended_at']} stage ({r.get('end_event')})"
        if r.get("ended_at")
        else "the combat never ended by itself"
    )
    if r.get("game_over"):
        seen.append("Game Over screen")
    return "PASS", "the wipe reads at a turn boundary; " + "; ".join(seen)


def judge_gameover(r: dict[str, Any]) -> Verdict:
    if r.get("game_over_downed") or r.get("game_over"):
        return "FAIL", "the avatar " + (
            "downed" if r.get("game_over_downed") else "dead"
        ) + " alone (others standing) is a Game Over: the avatar can't play"
    if not r.get("dead"):
        return "OPEN", "the avatar didn't die"
    return "PASS", "the avatar downed, then dead, with others standing: no Game Over"


def judge_npc(r: dict[str, Any]) -> Verdict:
    """q8 (option N, documented): an NPC team member under the mod's ops and the game AI."""
    why = []
    if not (r.get("act") or {}).get("refused"):
        why.append("act.* accepted the NPC (unexpected)")
    if (r.get("idle") or {}).get("acted"):
        why.append("the game AI acted on its turn")
    return ("FAIL" if why else "OPEN"), ("; ".join(why) or "the NPC kept still and act.* refused it") + (
        " (N needs a second action path either way)"
    )


# ---- the game side ----


class Arena:
    def __init__(
        self,
        game: Any,
        out: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.game, self.out, self.sleep, self.clock, self.connect = game, out, sleep, clock, connect

    def ev(self, code: str, timeout: float = 30.0) -> Any:
        txt = self.game.eval(
            INSTALL + "\nlocal P = Mods.BG3AI.BG3AI.ArenaProbe\n" + code, context="server", timeout=timeout
        )
        try:
            return json.loads(txt)
        except (TypeError, ValueError):
            return txt

    def call(self, fn: str, *forms: list[Any]) -> dict[str, Any]:
        return self.ev(call_code(fn, *forms))

    def facts(self, us: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.ev(f"return P.js(P.many({lua(us)}))")
        return {r["uuid"]: r for r in rows or []} if isinstance(rows, list) else {}

    def mark(self) -> int:
        return int(self.ev("return tostring(P.n)"))

    def since(self, i: int) -> list[dict[str, Any]]:
        r = self.ev(f"return P.js(P.since({i}))")
        return r if isinstance(r, list) else []

    def job(self, op: str, args: dict[str, Any], wait_s: float = 20.0) -> dict[str, Any]:
        try:
            r = self.game.run_job(op, args, wait_s=wait_s)
            return {"op": op, "args": args, "status": r.status, "result": r.result, "error": r.error}
        except Exception as e:  # a refusal is the result
            return {"op": op, "args": args, "status": "error", "error": f"{type(e).__name__}: {e}"}

    def client(self, op: str, args: dict[str, Any] | None = None) -> Any:
        try:
            return self.game.rpc(op, args or {}, context="client")
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def screen(self) -> Any:
        return (self.client("ui.state") or {}).get("screen")

    def write(self, name: str, rec: dict[str, Any]) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        p = self.out / f"{name}.json"
        p.write_text(json.dumps(rec, indent=1, sort_keys=True, default=str) + "\n")
        print(f"{rec.get('question', name)} {rec.get('verdict', '')}: {rec.get('why', '')} -> {p}")
        return p

    def teams(self) -> dict[str, Any]:
        p = self.out / "teams.json"
        if not p.exists():
            raise SystemExit(f"no {p}: run `setup` first")
        return json.loads(p.read_text())

    # ---- whose turn ----

    def active(self, teams: dict[str, Any]) -> list[dict[str, Any]]:
        """The 8's facts whose turn is open now (active, not ended)."""
        fs = self.facts(teams["A"] + teams["B"])
        return [
            f for f in fs.values() if (f.get("tb") or {}).get("active") and not (f.get("tb") or {}).get("requested_end")
        ]

    def end_turns(self, us: list[str]) -> list[dict[str, Any]]:
        return [self.job("act.end_turn", {"char": u}, wait_s=15) for u in us]

    def next_turn(self, teams: dict[str, Any], mark: int) -> list[dict[str, Any]]:
        """Wait for a TurnStarted after mark, then the open turn's members (empty when none came)."""
        end = self.clock() + TURN_WAIT_S
        while self.clock() < end:
            if any(e.get("ev") == "TurnStarted" for e in self.since(mark)):
                self.sleep(0.5)
                return self.active(teams)
            self.sleep(0.5)
        return self.active(teams)

    def drive_to(
        self, teams: dict[str, Any], want: Callable[[list[dict[str, Any]]], bool], max_turns: int = 12
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """End turns (act.end_turn) until an open turn passes `want`. Returns (that turn's members, what was ended)."""
        ended: list[dict[str, Any]] = []
        cur = self.active(teams)
        for _ in range(max_turns):
            if cur and want(cur):
                return cur, ended
            m = self.mark()
            ended += self.end_turns([f["uuid"] for f in cur])
            cur = self.next_turn(teams, m)
        return [], ended

    # ---- steps ----

    def conns(self, n: int) -> dict[str, Any]:
        """Open n debugger connections, then evaluate on all of them at once (one thread each)."""
        import threading

        rows: list[dict[str, Any]] = []
        clients: list[tuple[dict[str, Any], Any]] = []
        for i in range(n):
            row: dict[str, Any] = {"i": i}
            try:
                clients.append((row, self.connect()))  # type: ignore[misc]
                row["connected"] = True
            except Exception as e:
                row.update(connected=False, error=f"{type(e).__name__}: {e}")
            rows.append(row)

        def one(row: dict[str, Any], c: Any) -> None:
            try:
                row["answer"] = c.evaluate("return tostring(Ext.Utils.MonotonicTime())", context="server", timeout=15.0)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"

        threads = [threading.Thread(target=one, args=rc, daemon=True) for rc in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for _, c in clients:
            with contextlib.suppress(Exception):
                c.close()
        verdict, why = judge_conns(rows, n)
        rec = {"question": "q1-conns", "verdict": verdict, "why": why, "connections": rows}
        self.write("q1-conns", rec)
        return rec

    def roster(self) -> dict[str, Any]:
        r = self.ev("""
local us = P.chars()
return P.js({ max_party = P.q("GetMaxPartySize"), host = P.uuid(Osi.GetHostCharacter()), hooks = P.hooks,
    party = P.col1("DB_PartyMembers"), players = P.col1("DB_Players"), avatars = P.col1("DB_Avatars"),
    origins = P.col1("DB_Origins"), chars = P.many(us) })""")
        rec = {
            "question": "roster",
            "verdict": "INFO",
            "why": f"{len((r or {}).get('chars') or [])} candidates",
            **(r if isinstance(r, dict) else {"raw": r}),
        }
        self.write("roster", rec)
        return rec

    def factions(self, fa: str | None = None) -> list[dict[str, Any]]:
        r = self.ev(f"return P.js(P.factions({lua(fa)}))", timeout=60.0)
        fs = r if isinstance(r, list) else []
        self.write(
            "factions",
            {
                "question": "factions",
                "verdict": "INFO",
                "why": f"{len(fs)} factions",
                "team_a_faction": fa,
                "factions": fs,
            },
        )
        return fs

    def setup(
        self,
        a: list[str],
        b: list[str],
        spot_a: list[float],
        spot_b: list[float],
        seed: int | None,
        pinned: list[str] | None = None,
    ) -> dict:
        calls: list[dict[str, Any]] = []
        names = {"A": a, "B": b}
        teams: dict[str, Any] = {s: [self.ev(f"return P.who({lua(n)})") for n in names[s]] for s in SIDES}
        allu = teams["A"] + teams["B"]
        if len(set(allu)) != 8:
            raise SystemExit(f"want 8 distinct characters, got {allu}")
        host = self.ev("return P.uuid(Osi.GetHostCharacter())")
        before = self.facts(allu)
        calls.append({"max_party_before": self.ev('return tostring(P.q("GetMaxPartySize"))')})
        calls.append(self.call("SetMaxPartySizeOverride", [8]))
        calls.append({"max_party_after": self.ev('return tostring(P.q("GetMaxPartySize"))')})
        for u in allu:
            if not truthy(before[u].get("in_party_db")):
                calls.append(self.call("PROC_GLO_PartyMembers_Add", [u, host]))
        self.sleep(SETTLE_S)
        for u in allu:
            calls.append(self.call("DetachFromPartyGroup", [u]))
        for s in SIDES:
            for u in teams[s][1:]:
                calls.append(self.call("AttachToPartyGroup", [u, teams[s][0]]))
        slots = {"A": line_slots(spot_a, 4), "B": line_slots(spot_b, 4)}
        for s in SIDES:
            for u, (x, y, z) in zip(teams[s], slots[s], strict=True):
                calls.append(self.call("TeleportToPosition", [u, x, y, z, "", 0, 0, 0, 0, 1], [u, x, y, z, ""]))
        self.sleep(SETTLE_S)
        m = self.mark()
        calls.append(self.call("RestoreParty", [host]))
        self.sleep(SETTLE_S)
        mid = self.facts(allu)
        for u in allu:  # what RestoreParty left short
            if mid[u].get("hp") != mid[u].get("max_hp"):
                calls.append(self.call("SetHitpointsPercentage", [u, 100.0, "Guaranteed"], [u, 100.0]))
        if seed is not None:
            try:
                calls.append({"scn.seed": self.game.rpc("scn.seed", {"seed": seed})})
            except Exception as e:
                calls.append({"scn.seed": f"{type(e).__name__}: {e}"})
        self.sleep(SETTLE_S)
        after = self.facts(allu)
        mods = self.ev("return P.js(P.mods())")
        mods = mods if isinstance(mods, list) else []
        verdict, why = judge_party(teams, after, mods, pinned)
        setup_verdict = judge_setup(teams, after, slots)
        teams.update(names=names, host=host, slots=slots, faction_a=after[teams["A"][0]].get("faction"))
        (self.out / "teams.json").parent.mkdir(parents=True, exist_ok=True)
        (self.out / "teams.json").write_text(json.dumps(teams, indent=1) + "\n")
        rec = {
            "question": "q2-party",
            "verdict": verdict,
            "why": why,
            "placed": {"verdict": setup_verdict[0], "why": setup_verdict[1]},
            "mods": mods,
            "pinned_mods": pinned,
            "teams": teams,
            "calls": calls,
            "before": before,
            "after_restore": mid,
            "after": after,
            "events": self.since(m),
        }
        self.write("q2-party", rec)
        return rec

    def enemy_matrix(self, teams: dict[str, Any]) -> dict[str, Any]:
        pairs = [("AB", x, y) for x in teams["A"] for y in teams["B"]]
        pairs += [(s + s, x, y) for s in SIDES for x in teams[s] for y in teams[s] if x != y]
        code = "local o = {}\n" + "".join(
            f'o[{lua(f"{k}:{x}>{y}")}] = P.q("IsEnemy", {lua(x)}, {lua(y)})\n' for k, x, y in pairs
        )
        return self.ev(code + "return P.js(o)") or {}

    def hostile(self, faction_b: str) -> dict[str, Any]:
        teams = self.teams()
        allu = teams["A"] + teams["B"]
        m = self.mark()
        calls: list[dict[str, Any]] = []
        before = self.facts(allu)
        fa = before[teams["A"][0]].get("faction")
        if faction_b == "auto":
            pick = pick_faction_b(self.factions(fa), fa)
            if not pick:
                rec = {
                    "question": "q3-hostile",
                    "verdict": "OPEN",
                    "why": "no spare Hero faction: pass --faction-b (see factions.json)",
                    "faction_a": fa,
                }
                self.write("q3-hostile", rec)
                return rec
            faction_b = pick
        for u in teams["B"]:
            calls.append(self.call("SetFaction", [u, faction_b]))
        calls.append(self.call("SetRelation", [fa, faction_b, HOSTILE]))
        calls.append(self.call("SetRelation", [faction_b, fa, HOSTILE]))
        calls.append(
            {
                "relations": self.ev(
                    f'return P.js({{ ab = P.q("GetRelation", {lua(fa)}, {lua(faction_b)}), '
                    f'ba = P.q("GetRelation", {lua(faction_b)}, {lua(fa)}) }})'
                )
            }
        )
        enemy = self.enemy_matrix(teams)
        a1, b1 = teams["A"][0], teams["B"][0]
        calls.append(self.call("SetHostileAndEnterCombat", [fa, faction_b, a1, b1]))
        self.sleep(3.0)
        mid = self.facts(allu)
        if len({f.get("combat") for f in mid.values()}) != 1 or not truthy(mid[a1].get("combat")):
            for x, y in zip(teams["A"], teams["B"], strict=True):
                calls.append(self.call("EnterCombat", [x, y]))
            self.sleep(3.0)
        after = self.facts(allu)
        verdict, why = judge_hostile(teams, after, self.enemy_matrix(teams))
        teams["faction_b"] = faction_b
        (self.out / "teams.json").write_text(json.dumps(teams, indent=1) + "\n")
        rec = {
            "question": "q3-hostile",
            "verdict": verdict,
            "why": why,
            "faction_a": fa,
            "faction_b": faction_b,
            "calls": calls,
            "enemy_before_combat": enemy,
            "before": before,
            "after_first_call": mid,
            "after": after,
            "order": self.ev(f"return P.js(P.order({lua(a1)}))"),
            "events": self.since(m),
        }
        self.write("q3-hostile", rec)
        return rec

    def watch_idle(self, u: str, idle_s: float) -> dict[str, Any]:
        f0 = self.facts([u])[u]
        m = self.mark()
        self.sleep(idle_s)
        f1 = self.facts([u])[u]
        evs = [e for e in self.since(m) if u in "".join(e.get("args") or [])]
        return {
            "moved_m": round(dist(f0.get("pos"), f1.get("pos")), 2) if f0.get("pos") else 0.0,
            "casts": [
                e["args"][1]
                for e in evs
                if e.get("ev") in ("CastSpell", "UsingSpell", "CastedSpell") and (e.get("args") or [""])[0].endswith(u)
            ],
            "ended_by_itself": any(e.get("ev") == "TurnEnded" for e in evs) or not (f1.get("tb") or {}).get("active"),
        }

    def turns(self, n: int, idle_s: float, down_at: int | None) -> dict[str, Any]:
        teams = self.teams()
        m0 = self.mark()
        rows: list[dict[str, Any]] = []
        downed: dict[str, Any] | None = None
        cur = self.active(teams)
        for i in range(n):
            if not cur:
                break
            order = self.ev(f"return P.js(P.order({lua(cur[0]['uuid'])}))") or {}
            ini = {m.get("uuid"): m.get("initiative") for g in order.get("groups") or [] for m in g}
            lead = cur[0]
            row = {
                "i": i,
                "uuid": lead["uuid"],
                "name": lead.get("name"),
                "side": side_of(teams, lead["uuid"]),
                "group": [f["uuid"] for f in cur],
                "round": order.get("round"),
                "initiative": ini.get(lead["uuid"]),
                "downed_at_start": truthy(lead.get("downed")),
            }
            row["idle"] = self.watch_idle(lead["uuid"], idle_s)
            if down_at is not None and i == down_at and downed is None:
                victim = next((u for u in teams["B"] + teams["A"] if u not in row["group"]), None)
                if victim:
                    f = self.facts([victim])[victim]
                    hit = self.call("ApplyDamage", [victim, int((f.get("hp") or 50) + 5), "Bludgeoning", lead["uuid"]])
                    self.sleep(SETTLE_S)
                    fv = self.facts([victim])[victim]
                    downed = {
                        "who": fv.get("name"),
                        "uuid": victim,
                        "call": hit,
                        "downed": truthy(fv.get("downed")) or truthy(fv.get("dead")),
                        "after": fv,
                        "at_turn": i,
                    }
            mark = self.mark()
            row["end"] = (
                [j["status"] for j in self.end_turns(row["group"])]
                if not row["idle"]["ended_by_itself"]
                else "ended by itself"
            )
            rows.append(row)
            cur = self.next_turn(teams, mark)
        if downed is not None:
            later = [r for r in rows if r["i"] > downed["at_turn"]]
            downed["turns_after"] = len(later)
            downed["combat_after"] = truthy(self.facts([teams["A"][0]])[teams["A"][0]].get("combat"))
        v1, w1 = judge_turns(teams, rows, downed)
        v3, w3 = judge_ai(rows)
        evs = self.since(m0)
        self.write(
            "q3-turns",
            {"question": "q3-turns", "verdict": v1, "why": w1, "turns": rows, "downed": downed, "events": evs},
        )
        self.write(
            "q3-ai",
            {
                "question": "q3-ai",
                "verdict": v3,
                "why": w3,
                "idle_s": idle_s,
                "turns": [{k: r[k] for k in ("name", "side", "idle", "downed_at_start")} for r in rows],
            },
        )
        return {"turns": rows, "downed": downed}

    def nearest_enemy(self, teams: dict[str, Any], u: str) -> dict[str, Any] | None:
        mine = side_of(teams, u)
        other = teams["B" if mine == "A" else "A"]
        fs = self.facts([u, *other])
        me = fs[u]
        live = [fs[o] for o in other if not truthy(fs[o].get("dead")) and not truthy(fs[o].get("downed"))]
        return min(live, key=lambda f: dist(me.get("pos"), f.get("pos")), default=None)

    def act(self, spell: str) -> dict[str, Any]:
        teams = self.teams()
        m = self.mark()
        runs: dict[str, dict[str, Any]] = {}
        drove: list[dict[str, Any]] = []
        for s in SIDES:
            cur, ended = self.drive_to(teams, lambda c, s=s: any(side_of(teams, f["uuid"]) == s for f in c))
            drove += ended
            me = next((f for f in cur if side_of(teams, f["uuid"]) == s), None)
            if me is None:
                continue
            u = me["uuid"]
            foe = self.nearest_enemy(teams, u)
            r: dict[str, Any] = {"who": me.get("name"), "uuid": u, "foe": foe and foe.get("name")}
            if foe:
                b = self.facts([u])[u]
                mv = self.job("act.move_to", {"char": u, "target": foe["uuid"]}, wait_s=30)
                a = self.facts([u])[u]
                r["move"] = {
                    **mv,
                    "res_before": b.get("res"),
                    "res_after": a.get("res"),
                    "moved_m": round(dist(b.get("pos"), a.get("pos")), 2),
                }
                hp0 = self.facts([foe["uuid"]])[foe["uuid"]].get("hp")
                c = self.job("act.cast", {"char": u, "spell": spell, "target": foe["uuid"]}, wait_s=30)
                self.sleep(SETTLE_S)
                a2 = self.facts([u, foe["uuid"]])
                r["cast"] = {
                    **c,
                    "res_before": a.get("res"),
                    "res_after": a2[u].get("res"),
                    "foe_hp": [hp0, a2[foe["uuid"]].get("hp")],
                }
            r["end"] = self.end_turns([f["uuid"] for f in cur])[-1]
            runs[s] = r
        verdict, why = judge_act(runs)
        rec = {
            "question": "q4-act",
            "verdict": verdict,
            "why": why,
            "spell": spell,
            "runs": runs,
            "turns_ended_on_the_way": drove,
            "events": self.since(m),
        }
        self.write("q4-act", rec)
        return rec

    def react(self, use: bool) -> dict[str, Any]:
        teams = self.teams()
        m = self.mark()
        r: dict[str, Any] = {}
        cur, _ = self.drive_to(teams, lambda c: any(side_of(teams, f["uuid"]) == "A" for f in c))
        me = next((f for f in cur if side_of(teams, f["uuid"]) == "A"), None)
        foe = me and self.nearest_enemy(teams, me["uuid"])
        if not me or not foe:
            r["error"] = "no side-A turn with a standing enemy"
        else:
            u = me["uuid"]
            r.update(
                mover=me.get("name"),
                reactor=foe["uuid"],
                reactor_name=foe.get("name"),
                reactor_side=side_of(teams, foe["uuid"]),
            )
            if dist(me.get("pos"), foe.get("pos")) > REACH_M:
                r["close_in"] = self.job("act.move_to", {"char": u, "target": foe["uuid"]}, wait_s=30)
            f = self.facts([u, foe["uuid"]])
            r["gap_m"] = round(dist(f[u].get("pos"), f[foe["uuid"]].get("pos")), 2)
            dst = away(f[u]["pos"], f[foe["uuid"]]["pos"], 6.0)
            m2 = self.mark()
            try:  # start the walk, don't wait: the prompt comes up while it runs
                r["leave"] = self.game.rpc("act.move_to", {"char": u, "pos": dst})
            except Exception as e:
                r["leave"] = f"{type(e).__name__}: {e}"
            end = self.clock() + 10.0
            while self.clock() < end:
                p = self.client("ui.react_probe", {"watch": True})
                if isinstance(p, dict) and p.get("open"):
                    r["popup_seen"] = p
                    r["popup_detail"] = self.game.eval(POPUP, context="client", timeout=10.0)
                    r["answer"] = self.client("ui.react", {"use": use})
                    r["answer"] = {
                        "ok": not (isinstance(r["answer"], dict) and r["answer"].get("error")),
                        "reply": r["answer"],
                    }
                    break
                self.sleep(0.2)
            self.sleep(3.0)
            r["events"] = self.since(m2)
        verdict, why = judge_react(r)
        self.write(
            "q7-react", {"question": "q7-react", "verdict": verdict, "why": why, **r, "all_events": self.since(m)}
        )
        return r

    def end(self, loser: str, max_turns: int = 8) -> dict[str, Any]:
        teams = self.teams()
        m = self.mark()
        winner = "B" if loser == "A" else "A"
        r: dict[str, Any] = {"loser": loser, "stages": [], "death_save_turns": []}
        for stage in ("downed", "dead"):
            for u in teams[loser]:
                f = self.facts([u])[u]
                if truthy(f.get("dead")):
                    continue
                if stage == "downed":
                    c = self.call("ApplyDamage", [u, int((f.get("hp") or 50) + 5), "Bludgeoning", teams[winner][0]])
                else:
                    c = self.call("Die", [u, 0, NULL, 0, 0], [u, 0, NULL, 0], [u])
                r["stages"].append({"stage": stage, "who": f.get("name"), "call": c})
                self.sleep(1.0)
            self.sleep(5.0)
            fs = self.facts(teams["A"] + teams["B"])
            if stage == "downed":
                r["all_down_read"] = all(truthy(fs[u].get("downed")) or truthy(fs[u].get("dead")) for u in teams[loser])
                r["death_save_turns"] = self.downed_turns(teams, loser, max_turns)
            evs = self.since(m)
            ended = next((e for e in evs if e.get("ev") in ("CombatEnded", "LeftCombat")), None)
            screen = self.screen()
            r["stages"].append({"stage": stage, "after": fs, "screen": screen, "hud": self.client("ui.selected")})
            if screen == "game_over":
                r["game_over"] = stage
                break
            if ended and not r.get("ended_at"):
                r.update(ended_at=stage, end_event=ended.get("ev"))
                break
        r["events"] = self.since(m)
        verdict, why = judge_end(r)
        self.write("q6-down", {"question": "q6-down", "verdict": verdict, "why": why, **r})
        return r

    def downed_turns(self, teams: dict[str, Any], loser: str, max_turns: int) -> list[dict[str, Any]]:
        """Drive the fight on (ending the winners' turns) and record every turn a downed loser gets: who, whether the
        game ended it by itself, and what the client HUD selected then."""
        out: list[dict[str, Any]] = []
        cur = self.active(teams)
        for _ in range(max_turns):
            if not cur:
                break
            mine = [f for f in cur if side_of(teams, f["uuid"]) == loser]
            mark = self.mark()
            if mine:
                self.sleep(3.0)
                still = {
                    u for u, f in self.facts([f["uuid"] for f in mine]).items() if (f.get("tb") or {}).get("active")
                }
                out.append(
                    {
                        "who": [f.get("name") for f in mine],
                        "ended_by_itself": not still,
                        "hud": self.client("ui.selected"),
                        "events": self.since(mark),
                    }
                )
                self.end_turns(sorted(still))
            else:
                self.end_turns([f["uuid"] for f in cur])
            cur = self.next_turn(teams, mark)
        return out

    def gameover(self) -> dict[str, Any]:
        teams = self.teams()
        m = self.mark()
        av = self.ev("local a = P.col1('DB_Avatars') or {} return a[1] or ''")
        r: dict[str, Any] = {"avatar": av, "side": side_of(teams, av)}
        if not av:
            r["error"] = "no avatar"
        else:
            f = self.facts([av])[av]
            r["down_call"] = self.call("ApplyDamage", [av, int((f.get("hp") or 50) + 5), "Bludgeoning", NULL])
            self.sleep(10.0)
            r["downed"] = self.facts([av])[av]
            r["game_over_downed"] = self.screen() == "game_over"
            if not r["game_over_downed"]:
                r["call"] = self.call("Die", [av, 0, NULL, 0, 0], [av, 0, NULL, 0], [av])
                self.sleep(10.0)
                f = self.facts([av])[av]
                r.update(dead=truthy(f.get("dead")), facts=f, screen=self.screen())
                r["game_over"] = r["screen"] == "game_over"
        r["events"] = self.since(m)
        verdict, why = judge_gameover(r)
        self.write("q6-gameover", {"question": "q6-gameover", "verdict": verdict, "why": why, **r})
        return r

    def tie(self, force: bool, max_turns: int = 10) -> dict[str, Any]:
        """q5: find a cross-team initiative tie in the turn order (else force one by writing a team-B member's
        initiative to its team-A neighbour's), then play a round and record any turn both sides held open at once."""
        teams = self.teams()
        m = self.mark()
        order = self.ev(f"return P.js(P.order({lua(teams['A'][0])}))") or {}
        r: dict[str, Any] = {"order_before": order, "before": tie_info(order, teams)}
        if not r["before"]["equal"] and force and r["before"]["adjacent"]:
            a, b, ini = r["before"]["adjacent"][0]
            bu = b if side_of(teams, b) == "B" else a
            r["force"] = {
                "who": bu,
                "initiative": ini,
                "write": self.ev(f"return P.js(P.set_initiative({lua(bu)}, {lua(ini)}))"),
            }
        shared: list[list[str]] = []
        cur = self.active(teams)
        for _ in range(max_turns):
            if not cur:
                break
            if len({side_of(teams, f["uuid"]) for f in cur}) > 1:
                shared.append([f.get("name") or f["uuid"] for f in cur])
            mark = self.mark()
            self.end_turns([f["uuid"] for f in cur])
            cur = self.next_turn(teams, mark)
        order2 = self.ev(f"return P.js(P.order({lua(teams['A'][0])}))") or {}
        r.update(order_after=order2, after=tie_info(order2, teams), shared_turns=shared, events=self.since(m))
        verdict, why = judge_tie(r)
        self.write("q5-tie", {"question": "q5-tie", "verdict": verdict, "why": why, **r})
        return r

    def npc(self, idle_s: float) -> dict[str, Any]:
        teams = self.teams()
        m = self.mark()
        u = teams["B"][-1]
        r: dict[str, Any] = {"who": u, "make_npc": self.call("MakeNPC", [u]), "after": self.facts([u])[u]}
        cur, _ = self.drive_to(teams, lambda c: any(f["uuid"] == u for f in c))
        if cur:
            idle = self.watch_idle(u, idle_s)
            r["idle"] = {**idle, "acted": idle["moved_m"] > MOVED_M or bool(idle["casts"]) or idle["ended_by_itself"]}
            foe = self.nearest_enemy(teams, u)
            j = self.job("act.cast", {"char": u, "spell": "Target_MainHandAttack", "target": foe and foe["uuid"]})
            r["act"] = {**j, "refused": j["status"] != "done"}
        else:
            r["idle"] = {"acted": None, "note": "its turn never came (the game may have run it already)"}
        r["make_player"] = self.call("MakePlayer", [u, teams["host"], 0], [u, teams["host"]])
        r["events"] = self.since(m)
        verdict, why = judge_npc(r)
        self.write("q8-npc", {"question": "q8-npc", "verdict": verdict, "why": why, **r})
        return r


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

PLAN = {
    "conns": "open --conns debugger connections, evaluate on all at once",
    "roster": "read DB_PartyMembers/Players/Avatars/Origins + facts of each (IsPlayer, faction, user, hp, combat)",
    "factions": "static-data factions + level usage + relation to team A's",
    "setup": "loaded mods; SetMaxPartySizeOverride(8); PROC_GLO_PartyMembers_Add for the missing; Detach all, "
    "chain each side; TeleportToPosition to two lines; RestoreParty (+SetHitpointsPercentage 100 if short); scn.seed",
    "hostile": "SetFaction(team B, F_B); SetRelation both ways 0; IsEnemy matrix; SetHostileAndEnterCombat(F_A, "
    "F_B, a1, b1), else EnterCombat pairs; turn order",
    "turns": "per open turn: idle --idle-s (did the game act?), act.end_turn; one member downed at --down-at",
    "act": "per side: drive to its turn, act.move_to {target: nearest enemy}, act.cast attack, act.end_turn; "
    "resources before/after",
    "react": "side-A turn: close in, walk 6 m away; watch ui.react_probe; ui.react; Reaction* events",
    "tie": "turn order: a cross-team tie (else write a B member's initiative to its A neighbour's); a round: any "
    "turn open for both sides at once",
    "end": "loser team: ApplyDamage to 0 (downed), its death-save turns + HUD, then Die; CombatEnded/LeftCombat; "
    "game_over screen",
    "gameover": "the avatar alone: ApplyDamage to 0 (downed), then Die; game_over screen after each",
    "npc": "MakeNPC one team-B member; its turn idle (AI?); act.cast on it (refused?); MakePlayer back",
}


# The spike order: connections, the party of 8, hostility + one combat, costs, the tie, then turns (AI), the reaction,
# and last the wipe (it ends the fight).
ALL = ["conns", "roster", "factions", "setup", "hostile", "act", "tie", "turns", "react", "end"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("step", choices=[*PLAN, "all"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--team-a", default="")
    ap.add_argument("--team-b", default="")
    ap.add_argument("--spot-a", type=spot_arg)
    ap.add_argument("--spot-b", type=spot_arg)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--faction-b", default="auto")
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--idle-s", type=float, default=8.0)
    ap.add_argument("--down-at", type=int, default=4)
    ap.add_argument("--spell", default="Target_MainHandAttack")
    ap.add_argument("--react-use", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--loser", choices=SIDES, default="B")
    ap.add_argument("--conns", type=int, default=CONNS)
    ap.add_argument("--pinned-mods", default="", help="comma list of mod names/uuids the load may hold")
    ap.add_argument("--force", action=argparse.BooleanOptionalAction, default=True, help="tie: force one if none")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    steps = ALL if a.step == "all" else [a.step]
    if a.dry_run:
        for s in steps:
            print(f"{s}: {PLAN[s]}")
        return 0
    if "setup" in steps and not (a.team_a and a.team_b and a.spot_a and a.spot_b):
        ap.error("setup needs --team-a, --team-b (4 names each), --spot-a and --spot-b")
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    from bg3ai.game import Game  # the bridge's RPC layer; imported here so tests need no bridge
    from bg3ai.luadebug import LuaDebugClient

    def connect() -> LuaDebugClient:
        c = LuaDebugClient(connect_timeout=5.0)
        c.connect()
        return c

    ar = Arena(Game(), a.out, connect=connect)
    for s in steps:
        if s == "conns":
            ar.conns(a.conns)
        elif s == "roster":
            ar.roster()
        elif s == "factions":
            p = a.out / "teams.json"
            ar.factions(json.loads(p.read_text()).get("faction_a") if p.exists() else None)
        elif s == "setup":
            pinned = [x for x in a.pinned_mods.split(",") if x] or None
            ar.setup(a.team_a.split(","), a.team_b.split(","), a.spot_a, a.spot_b, a.seed, pinned)
        elif s == "hostile":
            ar.hostile(a.faction_b)
        elif s == "turns":
            ar.turns(a.turns, a.idle_s, a.down_at if a.down_at >= 0 else None)
        elif s == "act":
            ar.act(a.spell)
        elif s == "react":
            ar.react(a.react_use)
        elif s == "tie":
            ar.tie(a.force)
        elif s == "end":
            ar.end(a.loser)
        elif s == "gameover":
            ar.gameover()
        elif s == "npc":
            ar.npc(a.idle_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
