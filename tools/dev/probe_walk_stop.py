"""Live probe (night2 vis, for N16/N17): what ends Claude's walk toward Wyll ~6 m in, 3.3-3.9 m short of him?

N10-N13 and N15 step 2 (research/fix/live.md): Claude (-42.0,26.4,-44.7), Wyll unchained at (-38.5,26.4,-53.5), 9.4 m.
act.move_to {target=Wyll, range=2.5}: in N15 the mod aimed 2.5 m past Wyll (route_m 18.3, ETA 5 s), yet the game's
move event came after ~2.5 s / ~6 m, Claude 3.3 or 3.9 m N of Wyll (the N10/N13 spots), and the job failed "the walk
ended 3.9 m from the target". So the goal is not what stops it. This probe records the walk, tick by tick:
  - Claude's, Wyll's and every other character's position within PROBE_NEAR_M of the line (once per tick);
  - Claude's compact state (probe_revive_stuck P.compact: action / movement / steering machine layers, Osiris tasks,
    statuses, flags) and the serialized active movement and steering states and the Steering / Movement components,
    each kept only when it changes;
  - Osiris events on Claude or Wyll (EntityEvent, CharacterMoveToCancelled, CharacterMoveFailedUseJump, AttackedBy,
    StatusApplied/Removed, Attached/DetachedFromPartyGroup, plus a few unknown-to-us names, each tried in a pcall);
  - every G.osi call the mod makes for Claude (the order, purges) and the job's finish (status, error, result);
  - the mod's plan before the order (A.range_point: goal, stop point) and the engine's route to the goal (Nav.route).
Placement, as N15 set it up (no teleport: N16 step 4's teleport placement left every order unstarted for 7-20 s, all
five variants walked 0 m): Wyll unchained, the others chained to Claude ((d): unchained), each a waited-on job; Claude
walks back to the start with act.move_to only when he is more than PLACE_TOL_M off it (on a fresh load he stands
there), (d)'s others walk to their spots 1.5 m behind him, Wyll walks to his spot with act.move_to (as p10-2.py did);
then the probe waits until every member stood still SETTLE_S (measured, at most SETTLE_MAX_S). Each step's job status
and the actual start positions are in the record ("place"); the walk's order time and Claude's first step are in the
summary (no step within NO_START_MS of the order: the start is suspect, not a stop-short result).
Variants:
  a  act.move_to {target=Wyll, range=2.5}            the N15 order (goal past Wyll, on the line)
  b  act.move_to {pos=<2 m beside Wyll, off the line>} a plain point walk, nobody on its end
  c  Wyll walked 3 m aside first, then as (a)         nobody standing on the straight line
  d  as (a) with Karlach and Shadowheart unchained    no party follow behind Claude
  r  raw Osi.CharacterMoveToPosition to (a)'s goal   no mod walker at all (its own event, no job)

Run with the game on PROBE-30251267957__SCN-act3_gold_max (fresh load; a THROWAWAY load: save nothing):
    cd /data/Repos/ovh-e2e && uv run -q --project bridge python tools/dev/probe_walk_stop.py a b c d r \\
        --out runtime/probes/night-live/N16-walkstop.jsonl
Prints one summary line per variant; the full per-tick record is one JSON line per variant in --out.
PASS for the probe: every variant has a record. The finding: which variants stop short (a/c/d/r: Claude ends > 2.5 m
from Wyll; b: > 1.5 m from its point) and, for those, what changed in the 300 ms before the stop (a state, a
component, an event, a G.osi call, another character's position) that the variants that reach lack.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))

import probe_revive_stuck as rs  # noqa: E402  (its INSTALL: P.who, P.js, P.compact, P.comp_names)

CLAUDE0 = (-42.0, 26.4, -44.7)
WYLL0 = (-38.5, 26.4, -53.5)
RANGE = 2.5
BESIDE_M = 2.0  # (b): the point this far from Wyll, perpendicular to the line
ASIDE_M = 3.0  # (c): Wyll this far off the line
BEHIND_M = 1.5  # the other members start this far behind Claude, 1 m to each side
WALK_SECS = 15.0  # the longest wait for the job (or, raw, for the move event)
PLACE_TOL_M = 0.5  # a member this close to its spot is not walked there
JOB_SECS = 25.0  # the longest wait for one placement job
SETTLE_S = 0.75  # placement ends once every member moved < SETTLE_STILL_M over this long...
SETTLE_STILL_M = 0.05
SETTLE_MAX_S = 12.0  # ...or after this long (said in the record)
NO_START_MS = 3000  # no step this long after the order: the start is suspect
VARIANTS = ("a", "b", "c", "d", "r")


def _unit_xz(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float]:
    dx, dz = b[0] - a[0], b[2] - a[2]
    n = math.hypot(dx, dz)
    return dx / n, dz / n


def setup_for(variant: str) -> dict[str, Any]:
    """Start positions and the order for one variant (pure: tested offline)."""
    ux, uz = _unit_xz(CLAUDE0, WYLL0)
    px, pz = -uz, ux  # perpendicular, to the left of the line
    wyll = WYLL0 if variant != "c" else (WYLL0[0] + ASIDE_M * px, WYLL0[1], WYLL0[2] + ASIDE_M * pz)
    back = (CLAUDE0[0] - BEHIND_M * ux, CLAUDE0[1], CLAUDE0[2] - BEHIND_M * uz)
    others = [(back[0] + s * px, back[1], back[2] + s * pz) for s in (1.0, -1.0)]
    order: dict[str, Any] = {"target": "$wyll", "range": RANGE}
    if variant == "b":
        order = {"pos": [round(WYLL0[0] + BESIDE_M * px, 2), WYLL0[1], round(WYLL0[2] + BESIDE_M * pz, 2)]}
    return {
        "variant": variant,
        "claude": CLAUDE0,
        "wyll": wyll,
        "others": others,
        "order": order,
        "unchain_others": variant == "d",
        "raw": variant == "r",
    }


INSTALL = r"""
local B = Mods.BG3AI.BG3AI
local P = B.ReviveProbe
local G, A, RPC, U, N = B.Guard, B.Actions, B.RPC, B.Util, B.Nav
P.ws = P.ws or {}
local W = P.ws
W.EVENTS = { { "EntityEvent", 2 }, { "CharacterMoveToCancelled", 2 }, { "CharacterMoveFailedUseJump", 1 },
    { "AttackedBy", 7 }, { "StatusApplied", 4 }, { "StatusRemoved", 4 }, { "AttachedToPartyGroup", 1 },
    { "DetachedFromPartyGroup", 1 }, { "MovedBy", 2 }, { "StoppedMoving", 1 }, { "CharacterMoveToFailed", 2 },
    { "Moved", 1 } }
local function gid(x) return x and tostring(x):sub(-36) or nil end
local function pos(u) local ok, x, y, z = pcall(Osi.GetPosition, u); return ok and x and { x, y, z } or nil end
W.pos = pos
local function rnd(p) return p and { math.floor(p[1] * 100 + 0.5) / 100, math.floor(p[2] * 100 + 0.5) / 100,
    math.floor(p[3] * 100 + 0.5) / 100 } or nil end
W.rnd = rnd
local function rec(kind, data)
    if not W.on or #W.log >= 4000 then return end
    data.t, data.k = Ext.Utils.MonotonicTime() - W.t0, kind
    W.log[#W.log + 1] = data
end
W.rec = rec
if not W.listening then
    W.listening, W.registered = true, {}
    for _, ev in ipairs(W.EVENTS) do
        local ok = pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function(...)
            if not W.on then return end
            local a, hit = { ... }, false
            for i = 1, #a do
                local g = type(a[i]) == "string" and gid(a[i])
                if g == W.claude or g == W.wyll then hit = true end
                a[i] = tostring(a[i])
            end
            if hit then rec("osi_event", { name = ev[1], args = a }) end
        end)
        W.registered[#W.registered + 1] = ev[1] .. (ok and "" or "(not registered)")
    end
    W.osi0 = G.osi
    G.osi = function(fn, ...)
        local a = { ... }
        if W.on and gid(a[1]) == W.claude then
            local s = {}
            for i = 1, #a do s[i] = tostring(a[i]) end
            rec("g_osi", { fn = fn, args = s })
        end
        return W.osi0(fn, ...)
    end
    W.finish0 = RPC.finish_job
    RPC.finish_job = function(job, status, result, err)
        if W.on and job and job.id == W.job then
            rec("job_finish", { status = status, err = err and tostring(err),
                result = P.js(result, 800), walk = job.walk and P.js({ pos = job.walk.pos, done = job.walk.done,
                travelled = job.walk.travelled, still_ms = job.walk.still_ms, stop_at = job.walk.stop_at }, 400) })
        end
        return W.finish0(job, status, result, err)
    end
end
-- The serialized active state of an AI machine layer (movement / steering), or "-".
local function layerState(sc, machine)
    local ok, s = pcall(function()
        local m = sc[machine]
        local out = {}
        for i = 1, 3 do
            local l = m.Layers[i]
            local st = l and (l.State or l)
            if st ~= nil then out[#out + 1] = P.js(st, 700) end
        end
        return table.concat(out, " | ")
    end)
    return ok and s or ("unreadable: " .. tostring(s))
end
local function sample()
    if not W.on then return end
    local now = Ext.Utils.MonotonicTime()
    local r = { c = rnd(pos(W.claude)), w = rnd(pos(W.wyll)), near = {} }
    for _, u in ipairs(W.near) do r.near[#r.near + 1] = rnd(pos(u)) end
    local j = W.job and RPC.jobs[W.job]
    r.job = j and j.status or nil
    rec("tick", r)
    local e = Ext.Entity.Get(W.claude)
    local sc = e and e.ServerCharacter
    local sig = { compact = select(2, pcall(P.compact, W.claude)),
        move = sc and layerState(sc, "AiMovementMachine"), steer = sc and layerState(sc, "AiSteeringMachine") }
    for _, k in ipairs({ "Steering", "Movement", "PathingComponent", "ServerPathfinding" }) do
        local ok, v = pcall(function() return e[k] end)
        if ok and v ~= nil then sig[k] = P.js(v, 700) end
    end
    local comps = P.comp_names(W.claude)
    for k, v in pairs(sig) do
        if W.last[k] ~= v then rec("state", { key = k, value = v }); W.last[k] = v end
    end
    if W.lastComps then
        for k in pairs(comps) do if not W.lastComps[k] then rec("comp_added", { name = k }) end end
        for k in pairs(W.lastComps) do if not comps[k] then rec("comp_removed", { name = k }) end end
    end
    W.lastComps = comps
    if W.end_at and now >= W.end_at then W.on = false end
    if W.on and j and j.status ~= "running" and not W.end_at then W.end_at = now + 1500 end
    if W.on and now - W.t0 > W.max_ms then W.on = false end
end
if not W.tick then W.tick = Ext.Events.Tick:Subscribe(function() pcall(sample) end) end
"""

# Fill-ins: variant setup JSON (positions, order, flags).
START = r"""
local B = Mods.BG3AI.BG3AI
local P, W, G, A, U, N = B.ReviveProbe, B.ReviveProbe.ws, B.Guard, B.Actions, B.Util, B.Nav
local S = Ext.Json.Parse(%s)
W.on = false
local claude, wyll = P.who("Claude"), P.who("Wyll")
W.claude, W.wyll = claude, wyll
-- record, then order
local near = {}
local c0, w0 = W.pos(claude), W.pos(wyll)
for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")) do
    local okg, g = pcall(function() return e.Uuid.EntityUuid end)
    if okg and g and g ~= claude and g ~= wyll then
        local p = W.pos(g)
        if p and c0 and (U.dist(p, c0) <= %s or (w0 and U.dist(p, w0) <= %s)) then near[#near + 1] = g end
    end
end
W.near, W.log, W.last, W.lastComps, W.job, W.end_at = near, {}, {}, nil, nil, nil
W.t0, W.max_ms, W.on = Ext.Utils.MonotonicTime(), %d, true
local names = {}
for _, g in ipairs(near) do
    local okn, n = pcall(function() return Ext.Loca.GetTranslatedString(Osi.GetDisplayName(g)) end)
    names[#names + 1] = { uuid = g, name = okn and n or "?", pos = W.rnd(W.pos(g)) }
end
local plan, route
local order = S.order
if order.target then order.target = wyll end
pcall(function()
    if order.range then
        local ok, p = pcall(A.range_point, claude, wyll, math.max(1.0, order.range - A.RANGE_STOP_SLACK), order.range)
        plan = ok and { pos = W.rnd(p.pos), stop = W.rnd(p.stop), length_m = p.length_m, stop_m = p.stop_m }
            or tostring(p)
    end
    local goal = order.pos or (type(plan) == "table" and plan.pos)
    route = goal and N.route(claude, goal) or nil
end)
local res
if S.raw then
    local goal = type(plan) == "table" and plan.pos
    local ok, err = pcall(Osi.CharacterMoveToPosition, claude, goal[1], goal[2], goal[3], "Run", "bg3ai_walkstop_raw",
        990077)
    res = { raw = true, ok = ok, err = ok and nil or tostring(err), goal = goal }
else
    res = Ext.Json.Parse(B.rpc(Ext.Json.Stringify({ op = "act.move_to", args = (function()
        local a = { char = claude }
        for k, v in pairs(order) do a[k] = v end
        return a
    end)() })))
    W.job = res and res.result and tonumber(res.result.job) or nil
end
W.rec("order", { res = P.js(res, 600), plan = plan, route = route, near = names, registered = W.registered })
return Ext.Json.Stringify({ ok = true, job = W.job })
"""

# Every party member: uuid, display name, position.
PARTY = r"""
local out = {}
for _, row in ipairs(Osi.DB_PartyMembers:Get(nil) or {}) do
    local g = tostring(row[1]):sub(-36)
    local okn, n = pcall(function() return Ext.Loca.GetTranslatedString(Osi.GetDisplayName(g)) end)
    local x, y, z = Osi.GetPosition(g)
    out[#out + 1] = { uuid = g, name = okn and n or g, pos = { x, y, z } }
end
return Ext.Json.Stringify(out)
"""

STATUS = r"""
local W = Mods.BG3AI.BG3AI.ReviveProbe.ws
return Ext.Json.Stringify({ on = W.on == true, n = #W.log })
"""

DUMP = r"""
local W = Mods.BG3AI.BG3AI.ReviveProbe.ws
W.on = false
return Ext.Json.Stringify({ log = W.log }, { Beautify = false })
"""

NEAR_M = 15.0


def start_code(setup: dict[str, Any]) -> str:
    return START % (rs.lua_str(json.dumps(setup)), NEAR_M, NEAR_M, int(WALK_SECS * 1000) + 3000)


def rpc_code(op: str, args: dict[str, Any]) -> str:
    return f"return Mods.BG3AI.BG3AI.rpc({rs.lua_str(json.dumps({'op': op, 'args': args}))})"


def _member(party: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(m for m in party if str(m.get("name", "")).startswith(name))


def place_steps(setup: dict[str, Any], party: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """The placement jobs for one variant from where the party stands (pure: tested offline): (label, op, args)."""
    claude, wyll = _member(party, "Claude"), _member(party, "Wyll")
    others = [m for m in party if m is not claude and m is not wyll]
    steps: list[tuple[str, str, dict[str, Any]]] = [("unchain Wyll", "act.unchain", {"char": wyll["uuid"]})]
    for o in others:
        if setup["unchain_others"]:
            steps.append((f"unchain {o['name']}", "act.unchain", {"char": o["uuid"]}))
        else:
            steps.append((f"chain {o['name']} to Claude", "act.chain", {"char": o["uuid"], "leader": claude["uuid"]}))
    if math.dist(claude["pos"], setup["claude"]) > PLACE_TOL_M:
        steps.append(("walk Claude back", "act.move_to", {"char": claude["uuid"], "pos": list(setup["claude"])}))
    if setup["unchain_others"]:
        for o, spot in zip(others, setup["others"], strict=False):
            steps.append((f"walk {o['name']} behind", "act.move_to", {"char": o["uuid"], "pos": list(spot)}))
    if math.dist(wyll["pos"], setup["wyll"]) > PLACE_TOL_M:
        steps.append(("walk Wyll", "act.move_to", {"char": wyll["uuid"], "pos": list(setup["wyll"])}))
    return steps


def still(prev: list[dict[str, Any]], cur: list[dict[str, Any]]) -> bool:
    """Every member moved less than SETTLE_STILL_M between two reads."""
    at = {m["uuid"]: m["pos"] for m in prev}
    return all(m["uuid"] in at and math.dist(at[m["uuid"]], m["pos"]) < SETTLE_STILL_M for m in cur)


def _dist(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b:
        return None
    return round(math.dist(a, b), 2)


def summarize(variant: str, log: list[dict[str, Any]]) -> str:
    """One line: how the walk ended, where, and what happened in the 300 ms before Claude stopped moving."""
    ticks = [r for r in log if r.get("k") == "tick" and r.get("c")]
    if not ticks:
        return f"{variant}: no ticks recorded"
    start, end = ticks[0], ticks[-1]
    stop_t, first_t = end["t"], None
    for prev, cur in itertools.pairwise(ticks):  # the first and the last tick Claude moved
        if (_dist(prev["c"], cur["c"]) or 0) > 0.02:
            stop_t = cur["t"]
            first_t = cur["t"] if first_t is None else first_t
    order = next((r for r in log if r.get("k") == "order"), {})
    order_t = order.get("t", 0)
    if first_t is None or first_t - order_t > NO_START_MS:
        return (
            f"{variant}: SETUP SUSPECT: order at {order_t} ms, "
            + ("Claude never moved" if first_t is None else f"first step only at {first_t} ms")
            + f"; start {_dist(start['c'], start['w'])} m from Wyll"
        )
    plan = order.get("plan")
    goal = plan.get("pos") if isinstance(plan, dict) else None
    fin = next((r for r in log if r.get("k") == "job_finish"), None)
    walked = round(sum(_dist(p["c"], q["c"]) or 0 for p, q in itertools.pairwise(ticks)), 1)
    near = [
        f"{r['k']}:{r.get('name') or r.get('key') or r.get('fn') or ''}"
        for r in log
        if r.get("k") not in ("tick", "order") and stop_t - 300 <= r["t"] <= stop_t + 100
    ]
    return (
        f"{variant}: order at {order_t} ms, first step at {first_t} ms; walked {walked} m, stopped at"
        f" {stop_t / 1000:.2f} s; end {_dist(end['c'], end['w'])} m from Wyll"
        + (f", {_dist(end['c'], goal)} m from the goal" if goal else "")
        + (f"; job {fin['status']}: {fin.get('err') or ''}" if fin else "; no job finish")
        + f"; start {_dist(start['c'], start['w'])} m off; around the stop: {', '.join(near) or 'nothing'}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("variants", nargs="+", choices=VARIANTS)
    ap.add_argument("--out", type=Path, required=True, help="JSONL: one full record per variant")
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()

    def ev(code: str) -> str:
        return c.evaluate(rs.INSTALL + "\n" + INSTALL + "\n" + code, context="server", timeout=30.0)

    def party() -> list[dict[str, Any]]:
        return json.loads(ev(PARTY))

    def run_job(op: str, args: dict[str, Any]) -> dict[str, Any]:
        res = json.loads(ev(rpc_code(op, args)))
        job = (res.get("result") or {}).get("job") if isinstance(res.get("result"), dict) else None
        if not res.get("ok") or job is None:
            return {"status": "not started", "error": res.get("error"), "result": res.get("result")}
        end = time.monotonic() + JOB_SECS
        while True:
            view = json.loads(ev(rpc_code("job.poll", {"id": job}))).get("result") or {}
            if view.get("status") != "running" or time.monotonic() > end:
                return {k: view.get(k) for k in ("status", "error", "elapsed_ms")}
            time.sleep(0.2)

    def place(s: dict[str, Any]) -> dict[str, Any]:
        done = []
        for label, op, args in place_steps(s, party()):
            done.append({"step": label, **run_job(op, args)})
        t0, prev, settled = time.monotonic(), party(), False
        while time.monotonic() - t0 < SETTLE_MAX_S:
            time.sleep(SETTLE_S)
            cur = party()
            if still(prev, cur):
                settled = True
                break
            prev = cur
        return {"steps": done, "settled": settled, "settle_s": round(time.monotonic() - t0, 2), "party": prev}

    a.out.parent.mkdir(parents=True, exist_ok=True)
    for v in a.variants:
        s = setup_for(v)
        pl = place(s)
        c0, w0 = _member(pl["party"], "Claude")["pos"], _member(pl["party"], "Wyll")["pos"]
        print(
            f"{v}: placed {[(d['step'], d['status']) for d in pl['steps']]} settled={pl['settled']} "
            f"in {pl['settle_s']} s; Claude {_dist(c0, list(s['claude']))} m off his start, Wyll "
            f"{_dist(w0, list(s['wyll']))} m off his",
            flush=True,
        )
        print(f"{v}: go {ev(start_code(s))}", flush=True)
        end = time.monotonic() + WALK_SECS + 4.0
        while time.monotonic() < end and json.loads(ev(STATUS)).get("on"):
            time.sleep(0.5)
        log = json.loads(ev(DUMP)).get("log") or []
        with a.out.open("a") as f:
            f.write(json.dumps({"variant": v, "setup": s, "place": pl, "log": log}) + "\n")
        print(summarize(v, log), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
