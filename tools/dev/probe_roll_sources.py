"""Live probe (LIVE-FIRST, fix round 2026-10-24 bug 2): where does this build expose a roll's natural d20 and DC?

RollLog.lua's `finished` source (ServerRollFinishedEvent) never fired on bg3le (live3, live4, live5): natural/dc come
only from the RequestedRoll entity, which exists for passive/active/dialogue checks but not for a spell's saving
throw or an attack roll (live9: a GAMEPLAY_SpellSavingThrowSave record with srcs ["osi"] only). This probe logs
every candidate during the rolls the live worker makes, so the fix can read the source that fires:

  one-frame components (bg3se definitions, vendored in third_party/bg3le), read three ways: OnCreate, inside the
    frame from the post-update of every Roll/Hit/SpellCast/Stats system, and from Ext.Entity's change trace:
      SavingThrowRolledEvent (esv::stats: ConditionRoll.Difficulty, Roll.Result.NaturalRoll, Total, Success)
      HitResultEvent (esv::hit), SpellCastTargetHitEvent, SpellCastSpellRollAbortEvent (Hit.ConditionRolls)
      ServerRollFinishedEvent, ServerRollStartRequest, ServerRollStartSpellRequest,
      PassiveConditionalRollInterruptEvent, ConcentrationDamageCheck, ServerStatusDispelRollCheck,
      HitNotification, HitNotificationRequest
  components that live longer, polled every tick (new entities only): SpellCastRolls (a cast's per-target rolls),
    RequestedRoll, ServerRollInProgress, RollModifiers, ServerSpellHitRegister, UnresolvedHitNotification
  Osiris (timeline only, they carry no natural): RollResult/6, DialogRollResult/5, AttackedBy/7, CastedSpell/5
Each record keeps every NaturalRoll / Difficulty / DC / Total / Success / RollType field found anywhere in the
component (path = value), and the first 3000 chars of the whole component.

Steps (server context, a throwaway load; nothing is saved):
    uv run --project bridge python tools/dev/probe_roll_sources.py install
    # 1 passive skill check:  the same call check_rolllog.py makes (Osi.RequestPassiveRoll on the host)
    # 2 a saving throw:       a spell with a save at a creature (in a fight: use_ability Thunderwave / Sacred Flame /
    #                         Command, or Osi.UseSpell(host, "Target_SacredFlame", <npc>) in a throwaway load)
    # 3 an attack roll:       a weapon attack (use_ability Main Hand Attack, or Osi.UseSpell(host,
    #                         "Target_MainHandAttack", <npc>))
    uv run --project bridge python tools/dev/probe_roll_sources.py dump --out runtime/probes/rolls.json
    uv run --project bridge python tools/dev/probe_roll_sources.py remove

`dump` prints, per source, how many records it got and which natural/DC fields it carried. PASS for the probe: the
dump names at least one source with a NaturalRoll and a Difficulty/DC for step 2 (and step 3).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

INSTALL = r"""
local B = Mods.BG3AI.BG3AI
local P = B.RollProbe
if P then return "already installed: " .. #P.recs .. " records" end
P = { recs = {}, hooks = {}, subs = {}, seen = {}, frame = 0 }
B.RollProbe = P
local ONE_FRAME = { "SavingThrowRolledEvent", "HitResultEvent", "SpellCastTargetHitEvent",
    "SpellCastSpellRollAbortEvent", "ServerRollFinishedEvent", "ServerRollStartRequest", "ServerRollStartSpellRequest",
    "PassiveConditionalRollInterruptEvent", "ConcentrationDamageCheck", "ServerStatusDispelRollCheck",
    "HitNotification", "HitNotificationRequest" }
local LASTING = { "SpellCastRolls", "RequestedRoll", "ServerRollInProgress", "RollModifiers", "ServerSpellHitRegister",
    "UnresolvedHitNotification" }
local KEYS = { NaturalRoll = 1, Difficulty = 1, DC = 1, Total = 1, Success = 1, RollType = 1, Ability = 1, Skill = 1,
    AC = 1, Critical = 1 }
local function ser(x)
    local ok, t = pcall(Ext.Types.Serialize, x)
    if ok and t ~= nil then return t end
    return x
end
local function walk(t, path, out, depth)
    if type(t) ~= "table" or depth > 10 or #out >= 40 then return end
    for k, v in pairs(t) do
        local p = path .. "." .. tostring(k)
        if KEYS[k] and type(v) ~= "table" then out[#out + 1] = p:sub(2) .. "=" .. tostring(v) end
        walk(v, p, out, depth + 1)
    end
end
local function rec(src, comp, entity, raw)
    if #P.recs >= 2000 then return end
    local t = ser(raw)
    local fields = {}
    walk(t, "", fields, 0)
    local ok, js = pcall(Ext.Json.Stringify, t, { Beautify = false })
    P.recs[#P.recs + 1] = { t = Ext.Utils.MonotonicTime(), frame = P.frame, src = src, comp = comp,
        entity = tostring(entity), fields = fields, raw = ok and tostring(js):sub(1, 3000) or ("unserializable: " ..
        tostring(js)) }
end
local function readAll(names, src, once)
    for _, name in ipairs(names) do
        local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, name)
        if ok and type(list) == "table" then
            for _, e in ipairs(list) do
                local key = name .. "|" .. tostring(e) .. (once and "" or ("|" .. P.frame))
                if not P.seen[key] then
                    P.seen[key] = true
                    local okc, c = pcall(function() return e[name] end)
                    rec(src, name, e, okc and c or ("read failed: " .. tostring(c)))
                end
            end
        end
    end
end
for _, name in ipairs(ONE_FRAME) do
    local ok, id = pcall(Ext.Entity.OnCreate, name, function(e)
        local okc, c = pcall(function() return e[name] end)
        rec("create", name, e, okc and c or ("read failed: " .. tostring(c)))
    end)
    P.hooks["create:" .. name] = ok and true or tostring(id)
    if ok then P.subs[#P.subs + 1] = id end
end
local systems = {}
pcall(function()
    for k, v in pairs(Ext.Enums.ExtSystemType) do
        local n = type(k) == "string" and k or tostring(v)
        if n:find("Roll") or n:find("Hit") or n:find("SpellCast") or n:find("Stats") then systems[n] = true end
    end
end)
for n in pairs(systems) do
    local ok, id = pcall(Ext.Entity.OnSystemPostUpdate, n, function() readAll(ONE_FRAME, "post:" .. n, false) end)
    P.hooks["post:" .. n] = ok and true or tostring(id)
    if ok then P.subs[#P.subs + 1] = id end
end
P.tick = Ext.Events.Tick:Subscribe(function()
    P.frame = P.frame + 1
    readAll(ONE_FRAME, "tick", false)
    readAll(LASTING, "tick", true)
end)
P.hooks.tick = P.tick ~= nil
P.osi = {}
for _, ev in ipairs({ { "RollResult", 6 }, { "DialogRollResult", 5 }, { "AttackedBy", 7 }, { "CastedSpell", 5 } }) do
    local ok, err = pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function(...)
        if P.off then return end
        local a = {}
        for i = 1, select("#", ...) do a[i] = tostring((select(i, ...))) end
        rec("osi", ev[1], "", a)
    end)
    P.hooks["osi:" .. ev[1]] = ok and true or tostring(err)
end
local okT, errT = pcall(function()
    if Ext.Entity.SetupTracing then Ext.Entity.SetupTracing() end
    Ext.Entity.EnableTracing(true)
end)
P.hooks.trace = okT and true or tostring(errT)
return "installed"
"""

DUMP = r"""
local B = Mods.BG3AI.BG3AI
local P = B.RollProbe
if not P then return Ext.Json.Stringify({ error = "not installed" }) end
local trace = {}
local ok, err = pcall(function()
    local tr = Ext.Types.Serialize(Ext.Entity.GetTrace()) or {}
    local function walk(t, depth)
        if type(t) ~= "table" or depth > 6 then return end
        for k, v in pairs(t) do
            for _, s in ipairs({ tostring(k), type(v) == "string" and v or "" }) do
                if s:find("Roll") or s:find("Hit") or s:find("SavingThrow") then trace[s] = (trace[s] or 0) + 1 end
            end
            walk(v, depth + 1)
        end
    end
    walk(tr, 0)
end)
return Ext.Json.Stringify({ hooks = P.hooks, frames = P.frame, recs = P.recs, trace = trace,
    trace_error = (not ok) and tostring(err) or nil }, { Beautify = false })
"""

REMOVE = r"""
local B = Mods.BG3AI.BG3AI
local P = B.RollProbe
if not P then return "not installed" end
for _, id in ipairs(P.subs) do pcall(Ext.Entity.Unsubscribe, id) end
if P.tick then pcall(function() Ext.Events.Tick:Unsubscribe(P.tick) end) end
pcall(Ext.Entity.EnableTracing, false)
P.off = true -- Osiris listeners can't be removed: they return at once from now on
B.RollProbe = nil
return "removed"
"""

NATURAL = re.compile(r"NaturalRoll=(\d+)")
DIFFICULTY = re.compile(r"(?:Difficulty|\bDC)=(\d+)")


def summarize(dump: dict[str, Any]) -> list[str]:
    """One line per (source, component): records, and the natural/DC values they carried. Pure (tests)."""
    if dump.get("error"):
        return [f"ERROR {dump['error']}"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in dump.get("recs") or []:
        groups.setdefault((str(r.get("src")), str(r.get("comp"))), []).append(r)
    lines = []
    for (src, comp), rs in sorted(groups.items()):
        text = " ".join(f for r in rs for f in r.get("fields") or [])
        nats = sorted({int(n) for n in NATURAL.findall(text) if int(n) > 0})
        dcs = sorted({int(d) for d in DIFFICULTY.findall(text) if int(d) > 0})
        tag = "NATURAL+DC" if nats and dcs else "natural" if nats else "dc" if dcs else "-"
        lines.append(f"{tag:10} {src:28} {comp:36} {len(rs):4} rec  natural {nats[:10]} dc {dcs[:10]}")
    failed = sorted(k for k, v in (dump.get("hooks") or {}).items() if v is not True)
    lines.append(f"hooks not in place: {failed or 'none'}; frames {dump.get('frames')}")
    lines.append(
        f"trace (one-frame/created component names with Roll/Hit/SavingThrow): {dump.get('trace') or {}}"
        + (f" (trace error: {dump['trace_error']})" if dump.get("trace_error") else "")
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("cmd", choices=["install", "dump", "remove"])
    ap.add_argument("--out", type=Path, help="dump: write the raw records as JSON here")
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()
    code = {"install": INSTALL, "dump": DUMP, "remove": REMOVE}[a.cmd]
    out = c.evaluate(code, context="server", timeout=30.0)
    if a.cmd != "dump":
        print(out)
        return 0
    dump = json.loads(out)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(dump, indent=1, sort_keys=True) + "\n")
    for line in summarize(dump):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
