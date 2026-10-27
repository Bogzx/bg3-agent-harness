"""Live probe (night round 2026-10-27, BACKLOG "[act/se] After Tav died in a jump ... the game ran none of Tav's
orders"): what keeps a revived character from taking orders?

Bench B3 job 13 (Haiku crash_site_grove a1, 2026-10-25): 16:47:04 jump_to (250,0,192) from (257.4,0.1,194.0) at a
Chasm's edge -> "landed_m_from_target=18.6 @(264.6,0.4,206.5) hp=18->0": Tav fell into the Chasm, the game's chasm
recovery put the body back on the ledge, and the fall damage killed him (no FELL in the reply: same height). 16:47:32
Shadowheart's Scroll of Revivify revived him (HP 1), then Cure Wounds + short rest (18/18). From 16:47:44 to the end
(17:18) every walk of Tav was `travelled=0` ("no path to the destination": the mod's walk saw no step in 5 s after
CharacterMoveToPosition), every jump_to "the game never started it", the Uses of carried books stayed in his Osiris
queue (purged_tasks=1); fast_travel, camp go/leave (teleports) and chain/unchain worked, and Shadowheart walked fine.
So the state sits on Tav, survives teleports, and stops everything the Osiris queue runs.

Hypotheses this probe tells apart (each read is in `snap`):
  H1 chasm recovery left running: the TeleportFalling action (esv::ASTeleportFalling, ActionType TeleportFalling) or a
     TELEPORT_FALLING status still on the character after the death cut it short -> `action` / `statuses`
  H2 the revive itself: any Revivify revive leaves the character unable to act (Death / DeathState / Downed
     components, Flags Dead, CanMove / CanDoActions flags cleared) -> variant A (Osi.Die, no chasm) shows it too
  H3 a cast left open: the jump's SpellCastIsCasting (or JumpFollow) still on the character -> `comps` / `cast`
  H4 the Osiris task controller stalled: tasks queue but never start (UpdateInProgress / RemoveNextTask_M stuck) while
     no action runs -> `osi`
  H5 a movement/steering layer stuck (MSFalling) -> `movement` / `steering`
  H6 a dialog / reserved flag (InDialog, ReservedForDialog, Dialog id, HasOsirisDialog) -> `flags` / `dialog`
  (Our own queue is ruled out by the logs: the act.move_to RPCs reached the server and failed there.)

Commands (server context, the game's Lua debugger port; a THROWAWAY load: save nothing):
    P="uv run --project bridge python tools/dev/probe_revive_stuck.py"
    $P snap Tav --label base          # one full snapshot, appended to --out (default runtime/probes/revive/snaps.jsonl)
    $P watch Tav Shadowheart          # record every change of the compact state every tick (120 s, --secs)
    $P dump                           # the watch's change log (and stop it with `unwatch`)
    $P move Tav --to Shadowheart      # raw Osi.CharacterMoveToPosition next to that member, reports metres moved in 6 s
    $P jump Tav 250 -0.3 195          # raw Osi.UseSpellAtPosition Projectile_Jump (no guard), reports after 8 s
    $P kill Tav | $P scroll Shadowheart | $P hp Tav 1 | $P purge Tav | $P tp Tav x y z
    $P diff base revived              # what differs between two labelled snapshots

Steps for the live worker (one game, each variant from a fresh load; party = Tav + Shadowheart):
  Variant B first (the job-13 replay), load the start save SCN-crash_site_grove (Tav-34221263934__SCN-crash_site_grove):
    1. $P snap Tav --label B-base ; $P move Tav --to Shadowheart           (expect moved > 1 m: the raw walk works)
    2. MCP travel_to(x=257.4, y=0.1, z=194.0, who=Tav) (the job's spot at the chasm edge; any spot beside the Chasm
       SW of (255.8,0.9,200.3) does). $P watch Tav Shadowheart
    3. MCP jump_to(x=250, y=0, z=192, who=Tav). If the guard refuses the landing now: $P jump Tav 250 -0.3 195
       (the job's actual landing). Wait 10 s. $P snap Tav --label B-fallen
       (if Tav survived the fall: also $P move Tav --to Shadowheart and note it: chasm without death)
    4. Revive as the agent did: MCP move_to(target=<Tav eN>, who=Shadowheart, within=3), wait_for it, then
       use_ability(ability="Scroll of Revivify", target=<Tav eN>, who=Shadowheart). $P snap Tav --label B-revived
    5. $P move Tav --to Shadowheart ; MCP walk_toward(direction=<a walkable one>, max_m=6, who=Tav) + wait_for ;
       MCP jump_to(<a near floor spot>, who=Tav). $P snap Tav --label B-after ; $P dump > B-watch.txt
    6. If Tav is stuck: $P purge Tav ; $P move Tav --to Shadowheart (does a purge free him?). Then save_game +
       load_game of that save, $P snap Tav --label B-reloaded ; $P move Tav --to Shadowheart (does a reload?).
  Variant A (no chasm, the revive alone), a fresh load of the same save:
    $P snap Tav --label A-base ; $P kill Tav ; $P snap Tav --label A-dead ; revive as step 4 ;
    $P snap Tav --label A-revived ; $P move Tav --to Shadowheart ; MCP walk_toward(..., who=Tav)
  Report: the `move` lines, `diff B-base B-revived`, `diff A-base A-revived`, `diff A-revived B-revived`, the dump.
PASS for the probe: variant B reproduces the stuck Tav (move 0 m) and the diff/dump name a state the healthy runs
lack (or variant A is stuck too: then the revive path is the cause).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

DEFAULT_OUT = REPO / "runtime" / "probes" / "revive" / "snaps.jsonl"
SCROLL_TEMPLATE = "c1c3e4fb-d68c-4e10-afdc-d4550238d50e"  # LOOT_SCROLL_Revivify (ref-D2.json item_added)

# Installed once into the server state (Mods.BG3AI.BG3AI.ReviveProbe); every command sends it first.
INSTALL = r"""
local B = Mods.BG3AI.BG3AI
local P = B.ReviveProbe
if not P then
P = { recs = {}, last = {}, watch = {} }
B.ReviveProbe = P
local function ser(x)
    local ok, t = pcall(Ext.Types.Serialize, x)
    if ok and t ~= nil then return t end
    return tostring(x)
end
P.ser = ser
local function js(x, cap)
    local ok, s = pcall(Ext.Json.Stringify, ser(x), { Beautify = false })
    s = ok and tostring(s) or ("unserializable: " .. tostring(s))
    if cap and #s > cap then s = s:sub(1, cap) .. "..." end
    return s
end
P.js = js
function P.who(s)
    if s:match("^%x+%-%x+%-%x+%-%x+%-%x+$") then return s end
    local rows = Osi.DB_PartyMembers:Get(nil) or {}
    for _, row in ipairs(rows) do
        local g = tostring(row[1])
        local okn, n = pcall(function() return Ext.Loca.GetTranslatedString(Osi.GetDisplayName(g)) end)
        if okn and n and n:lower():find(s:lower(), 1, true) == 1 then return g:sub(-36) end
    end
    error("no party member named " .. s)
end
-- One machine's layers as type names: ActionMachine layers hold .State, the others hold the state itself.
local function layers(m)
    if m == nil then return nil end
    local ok, ls = pcall(function() return m.Layers end)
    if not ok or ls == nil then return "unreadable: " .. tostring(ls) end
    local out = {}
    for i = 1, 4 do
        local okl, l = pcall(function() return ls[i] end)
        if not okl then break end
        local st = l
        local oks, s = pcall(function() return l.State end)
        if oks and s ~= nil then st = s end
        if st == nil then out[#out + 1] = "-" else
            local okt, ty = pcall(function() return st.Type end)
            local okf, fin = pcall(function() return st.Finished end)
            out[#out + 1] = (okt and tostring(ty) or "?") .. ((okf and fin) and "(finished)" or "")
        end
    end
    return table.concat(out, ",")
end
local function field(o, k)
    local ok, v = pcall(function() return o[k] end)
    if ok then return v end
    return nil
end
local KEY_COMPS = { "CanMove", "CanDoActions", "CanInteract", "CanTravel", "Death", "DeathState", "DeathType", "Downed",
    "ServerDeathState", "ServerDeathRequest", "SpellCastIsCasting", "JumpFollow", "TurnBased", "ActionUseConditions",
    "CanEnterChasm", "Steering", "Movement" }
-- The compact state the watch compares every tick (cheap reads only).
function P.compact(c)
    local e = Ext.Entity.Get(c)
    local sc = e and field(e, "ServerCharacter")
    local st = {}
    pcall(function()
        for _, s in pairs(sc.StatusManager.Statuses) do st[#st + 1] = tostring(field(s, "StatusId")) end
    end)
    table.sort(st)
    local okn, names = pcall(function() return e:GetAllComponentNames() end)
    local n = okn and type(names) == "table" and #names or -1
    local osi = sc and field(sc, "OsirisController")
    local okt, ntask = pcall(function() return #osi.Tasks end)
    local ok, hp = pcall(Osi.GetHitpoints, c)
    return string.format("hp=%s dead=%s action=%s move=%s steer=%s osi=%s statuses=%s comps=%d flags=%s",
        tostring(ok and hp or "?"), tostring(Osi.IsDead(c)), tostring(sc and layers(field(sc, "AiActionMachine"))),
        tostring(sc and layers(field(sc, "AiMovementMachine"))),
        tostring(sc and layers(field(sc, "AiSteeringMachine"))),
        tostring(okt and ntask or "?"), table.concat(st, "|"), n, js(sc and field(sc, "Flags"), 600))
end
function P.comp_names(c)
    local set = {}
    local ok, names = pcall(function() return Ext.Entity.Get(c):GetAllComponentNames() end)
    if ok and type(names) == "table" then for _, k in ipairs(names) do set[tostring(k)] = true end end
    return set
end
function P.snap(c, label)
    local r = { uuid = c, label = label, t = Ext.Utils.MonotonicTime() }
    local e = Ext.Entity.Get(c)
    local sc = field(e, "ServerCharacter")
    r.pos = { Osi.GetPosition(c) }
    pcall(function() r.hp = Osi.GetHitpoints(c) end)
    r.dead, r.combat = Osi.IsDead(c), Osi.IsInCombat(c)
    pcall(function() r.in_dialog = Osi.IsInDialog(c) end)
    pcall(function() r.can_move_osi = Osi.CanMove(c) end)
    if sc then
        r.flags, r.flags2, r.flags3 = js(field(sc, "Flags")), js(field(sc, "Flags2")), js(field(sc, "Flags3"))
        -- the raw bits too: job 13's jump death logged "Flags has bits 0x800000000000000 that its enumeration does not
        -- name" (bg3le, 16:47:08), which the named list drops
        pcall(function() r.flags_raw = string.format("0x%x", sc.Flags.__Value) end)
        r.dialog = { id = field(sc, "Dialog"), osiris = field(sc, "HasOsirisDialog") }
        r.follow = tostring(field(sc, "FollowCharacter"))
        local osi = field(sc, "OsirisController")
        if osi then
            local okt, n = pcall(function() return #osi.Tasks end)
            r.osi = { tasks = okt and n or tostring(n), update = field(osi, "UpdateInProgress"),
                remove_next = field(osi, "RemoveNextTask_M"), flush = field(osi, "FlushRequested"),
                list = js(field(osi, "Tasks"), 1500) }
        end
        local am = field(sc, "AiActionMachine")
        r.action = layers(am)
        r.action_entering, r.action_exit = js(am and field(am, "Entering")), js(am and field(am, "ExitRequested"))
        r.action_states = {}
        pcall(function()
            for i = 1, 3 do
                local l = am.Layers[i]
                if l and l.State then r.action_states[#r.action_states + 1] = js(l.State, 800) end
            end
        end)
        local mm = field(sc, "AiMovementMachine")
        r.movement, r.movement_active = layers(mm), mm and field(mm, "Active")
        local sm = field(sc, "AiSteeringMachine")
        r.steering, r.steering_active = layers(sm), sm and field(sm, "Active")
        r.behaviour = layers(field(sc, "AiBehaviourMachine"))
        r.statuses = {}
        pcall(function()
            for _, s in pairs(sc.StatusManager.Statuses) do
                r.statuses[#r.statuses + 1] = tostring(field(s, "StatusId")) .. ":"
                    .. tostring(field(s, "StatusType") or field(s, "Type"))
            end
        end)
        table.sort(r.statuses)
    end
    r.comps = {}
    for k in pairs(P.comp_names(c)) do r.comps[#r.comps + 1] = k end
    table.sort(r.comps)
    r.key = {}
    for _, k in ipairs(KEY_COMPS) do
        local ok, v = pcall(function() return e[k] end)
        if ok and v ~= nil then r.key[k] = js(v, 1200) end
    end
    pcall(function()
        local cast = e.SpellCastIsCasting.Cast
        r.cast = { entity = tostring(cast), comps = {} }
        for k in pairs(P.comp_names(cast)) do r.cast.comps[#r.cast.comps + 1] = k end
        r.cast.state = js(cast.SpellCastState, 1500)
    end)
    return Ext.Json.Stringify(r, { Beautify = false })
end
P.tick = Ext.Events.Tick:Subscribe(function()
    if not P.until_t then return end
    local now = Ext.Utils.MonotonicTime()
    if now > P.until_t then P.until_t = nil; return end
    for _, c in ipairs(P.watch) do
        local ok, sig = pcall(P.compact, c)
        sig = ok and sig or ("read failed: " .. tostring(sig))
        local comps = P.comp_names(c)
        local prev = P.last[c]
        if not prev or prev.sig ~= sig then
            local add, rem = {}, {}
            if prev then
                for k in pairs(comps) do if not prev.comps[k] then add[#add + 1] = k end end
                for k in pairs(prev.comps) do if not comps[k] then rem[#rem + 1] = k end end
            end
            table.sort(add); table.sort(rem)
            if #P.recs < 3000 then
                P.recs[#P.recs + 1] = { t = now, c = c, sig = sig, add = add, rem = rem }
            end
            P.last[c] = { sig = sig, comps = comps }
        end
    end
end)
end
"""

# A queued walk/jump answers only later: these record the start; `after` reads the end.
MOVE = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
local c = P.who(%s)
local x, y, z
if %s then
    x, y, z = Osi.GetPosition(P.who(%s))
else
    local px, py, pz = Osi.GetPosition(c)
    x, y, z = px + %s, py, pz
end
local ok, err = pcall(Osi.CharacterMoveToPosition, c, x, y, z, "Run", "bg3ai_revive_probe_mv", 990001)
local sx, sy, sz = Osi.GetPosition(c)
local n = -1
pcall(function() n = #Ext.Entity.Get(c).ServerCharacter.OsirisController.Tasks end)
return Ext.Json.Stringify({ uuid = c, start = { sx, sy, sz }, goal = { x, y, z }, ok = ok,
    err = ok and nil or tostring(err), tasks = n }, { Beautify = false })
"""

JUMP = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
local c = P.who(%s)
local ok, err = pcall(Osi.UseSpellAtPosition, c, "Projectile_Jump", %s, %s, %s, 0)
if not ok and tostring(err):find("number of IN arguments", 1, true) then
    ok, err = pcall(Osi.UseSpellAtPosition, c, "Projectile_Jump", %s, %s, %s)
end
local sx, sy, sz = Osi.GetPosition(c)
local n = -1
pcall(function() n = #Ext.Entity.Get(c).ServerCharacter.OsirisController.Tasks end)
return Ext.Json.Stringify({ uuid = c, start = { sx, sy, sz }, ok = ok, err = ok and nil or tostring(err), tasks = n },
    { Beautify = false })
"""

AFTER = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
local c = %s
local x, y, z = Osi.GetPosition(c)
local n = -1
pcall(function() n = #Ext.Entity.Get(c).ServerCharacter.OsirisController.Tasks end)
return Ext.Json.Stringify({ pos = { x, y, z }, tasks = n, compact = P.compact(c) }, { Beautify = false })
"""

# Osiris call shapes differ between builds: try each form until one is not an arity error.
FORMS = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
local c = P.who(%s)
local forms = %s
local last
for i, args in ipairs(forms) do
    for j, a in ipairs(args) do if a == "$c" then args[j] = c end end
    local ok, err = pcall(Osi[%s], table.unpack(args))
    if ok then return %s .. " form " .. i .. " ok on " .. c end
    last = tostring(err)
    if not last:find("number of IN arguments", 1, true) then break end
end
return %s .. " failed: " .. tostring(last)
"""

WATCH = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
P.watch, P.last, P.recs = {}, {}, {}
for _, s in ipairs(%s) do P.watch[#P.watch + 1] = P.who(s) end
P.t0 = Ext.Utils.MonotonicTime()
P.until_t = P.t0 + %d * 1000
return "watching " .. table.concat(P.watch, ", ") .. " for %d s"
"""

DUMP = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
return Ext.Json.Stringify({ t0 = P.t0, watching = P.until_t ~= nil, recs = P.recs }, { Beautify = false })
"""

UNWATCH = r"""
local P = Mods.BG3AI.BG3AI.ReviveProbe
if P.tick then pcall(function() Ext.Events.Tick:Unsubscribe(P.tick) end) end
Mods.BG3AI.BG3AI.ReviveProbe = nil
return "removed"
"""


def lua_str(s: str) -> str:
    """A Lua string literal (JSON's escapes are valid Lua for the ASCII names and numbers sent here)."""
    return json.dumps(s)


def lua_list(items: list[Any]) -> str:
    return "{" + ", ".join(lua_str(x) if isinstance(x, str) else repr(x) for x in items) + "}"


def forms_code(fn: str, who: str, forms: list[list[Any]]) -> str:
    """Call Osi.<fn> with the first form the build accepts; "$c" in a form is the character's uuid."""
    body = "{" + ", ".join(lua_list(f) for f in forms) + "}"
    return FORMS % (lua_str(who), body, lua_str(fn), lua_str(fn), lua_str(fn))


def move_code(who: str, to: str | None, dx: float) -> str:
    return MOVE % (lua_str(who), "true" if to else "false", lua_str(to or who), repr(float(dx)))


def jump_code(who: str, x: float, y: float, z: float) -> str:
    return JUMP % (lua_str(who), x, y, z, x, y, z)


def moved_m(start: list[float], end: list[float]) -> float:
    return round(sum((a - b) ** 2 for a, b in zip(start, end, strict=True)) ** 0.5, 2)


def diff_snaps(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """What differs between two snapshots: components gained/lost, then every other changed field."""
    out = []
    ca, cb = set(a.get("comps") or []), set(b.get("comps") or [])
    if cb - ca:
        out.append("components added: " + ", ".join(sorted(cb - ca)))
    if ca - cb:
        out.append("components removed: " + ", ".join(sorted(ca - cb)))
    skip = {"comps", "t", "label", "pos", "uuid"}
    for k in sorted((set(a) | set(b)) - skip):
        if a.get(k) != b.get(k):
            out.append(f"{k}: {json.dumps(a.get(k))} -> {json.dumps(b.get(k))}")
    return out or ["no difference"]


def watch_lines(dump: dict[str, Any]) -> list[str]:
    t0 = dump.get("t0") or 0
    lines = []
    for r in dump.get("recs") or []:
        line = f"+{(r['t'] - t0) / 1000:7.2f}s {r['c'][:8]} {r['sig']}"
        if r.get("add"):
            line += " +[" + ",".join(r["add"]) + "]"
        if r.get("rem"):
            line += " -[" + ",".join(r["rem"]) + "]"
        lines.append(line)
    return lines


def read_snaps(path: Path) -> dict[str, dict[str, Any]]:
    snaps = {}
    for line in path.read_text().splitlines():
        if line.strip():
            s = json.loads(line)
            snaps[s.get("label") or ""] = s
    return snaps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snap")
    p.add_argument("who")
    p.add_argument("--label", required=True)
    p = sub.add_parser("watch")
    p.add_argument("who", nargs="+")
    p.add_argument("--secs", type=int, default=120)
    sub.add_parser("dump")
    sub.add_parser("unwatch")
    p = sub.add_parser("move")
    p.add_argument("who")
    p.add_argument("--to", help="walk next to this party member (a spot known walkable)")
    p.add_argument("--dx", type=float, default=3.0, help="without --to: this many metres along +x")
    p.add_argument("--secs", type=float, default=6.0)
    p = sub.add_parser("jump")
    p.add_argument("who")
    p.add_argument("xyz", type=float, nargs=3)
    p.add_argument("--secs", type=float, default=8.0)
    for name in ("kill", "scroll", "purge"):
        sub.add_parser(name).add_argument("who")
    p = sub.add_parser("hp")
    p.add_argument("who")
    p.add_argument("value", type=int)
    p = sub.add_parser("tp")
    p.add_argument("who")
    p.add_argument("xyz", type=float, nargs=3)
    p = sub.add_parser("diff")
    p.add_argument("a")
    p.add_argument("b")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="snapshot file (JSONL)")
    a = ap.parse_args(argv)

    if a.cmd == "diff":
        snaps = read_snaps(a.out)
        print("\n".join(diff_snaps(snaps[a.a], snaps[a.b])))
        return 0

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()

    def ev(code: str) -> str:
        return c.evaluate(INSTALL + "\n" + code, context="server", timeout=30.0)

    if a.cmd == "snap":
        out = ev(
            f"return Mods.BG3AI.BG3AI.ReviveProbe.snap(Mods.BG3AI.BG3AI.ReviveProbe.who({lua_str(a.who)}), "
            f"{lua_str(a.label)})"
        )
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with a.out.open("a") as f:
            f.write(out + "\n")
        s = json.loads(out)
        print(
            f"{a.label}: hp={s.get('hp')} dead={s.get('dead')} action={s.get('action')} movement={s.get('movement')} "
            f"osi={s.get('osi', {}).get('tasks')} statuses={s.get('statuses')} cast={'cast' in s} -> {a.out}"
        )
    elif a.cmd == "watch":
        print(ev(WATCH % (lua_list(a.who), a.secs, a.secs)))
    elif a.cmd == "dump":
        print("\n".join(watch_lines(json.loads(ev(DUMP)))))
    elif a.cmd == "unwatch":
        print(ev(UNWATCH))
    elif a.cmd in ("move", "jump"):
        code = move_code(a.who, a.to, a.dx) if a.cmd == "move" else jump_code(a.who, *a.xyz)
        start = json.loads(ev(code))
        time.sleep(a.secs)
        end = json.loads(ev(AFTER % lua_str(start["uuid"])))
        print(
            f"{a.cmd} {a.who}: call ok={start['ok']} {start.get('err') or ''} tasks after the call={start['tasks']}; "
            f"after {a.secs:g} s moved {moved_m(start['start'], end['pos'])} m, tasks={end['tasks']}\n"
            f"  {end['compact']}"
        )
    elif a.cmd == "kill":
        print(
            ev(
                forms_code(
                    "Die",
                    a.who,
                    [
                        ["$c", 0, "NULL_00000000-0000-0000-0000-000000000000", 0, 0],
                        ["$c", 0, "NULL_00000000-0000-0000-0000-000000000000", 0],
                        ["$c"],
                    ],
                )
            )
        )
    elif a.cmd == "scroll":
        print(ev(forms_code("TemplateAddTo", a.who, [[SCROLL_TEMPLATE, "$c", 1, 0], [SCROLL_TEMPLATE, "$c", 1]])))
    elif a.cmd == "purge":
        print(ev(forms_code("PurgeOsirisQueue", a.who, [["$c", 1], ["$c"]])))
    elif a.cmd == "hp":
        print(ev(forms_code("SetHitpoints", a.who, [["$c", a.value, "Guaranteed"], ["$c", a.value]])))
    elif a.cmd == "tp":
        x, y, z = a.xyz
        print(ev(forms_code("TeleportToPosition", a.who, [["$c", x, y, z, "", 0, 0, 0, 0, 1], ["$c", x, y, z, ""]])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
