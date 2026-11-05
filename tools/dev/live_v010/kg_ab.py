"""Check K1 (known ground step 1): scripted travel_to, old rule vs new rule, from the same start, no model.

    uv run --project bridge python tools/dev/live_v010/kg_ab.py plan <scenario> [n]     # the goals it would use
    uv run --project bridge python tools/dev/live_v010/kg_ab.py run <scenario> [n] [first_k]
    uv run --project bridge python tools/dev/live_v010/kg_ab.py summary [scenario]
    uv run --project bridge python tools/dev/live_v010/kg_ab.py rule <new|old>           # set the constants only

scenario: crash_site_grove | act1_level_3 (its start save loaded, untouched, nav_*.json moved aside before the
launch as a bench run's reset does). n: goals (default 3).

Goals: kg_goals.json, the launch-v1 travel_to goals in that scenario whose trip stopped with "no walkable ground" or
"not seen or walked yet"; the plan keeps those 30-130 m (horizontal) from the live leader with |dy| <= 25 m, one per
15 m, most-hit first.

run: first a snapshot (party positions, the mod's breadcrumb store PersistentVars.nav, kept in the server Lua state
as BG3AI._kg_snap and in runtime/probes/v010/kg-snap-<scenario>.json). Then per goal both rules, in alternating
order (goal 0 new then old, goal 1 old then new, ...). Each trip runs in its own process (no bridge state carried
from one trip to the next) and starts with a restore: every party member teleported back to its snapshot spot, the
breadcrumb store put back, N.seenPts emptied; then the rule's constants (new: CAMERA_M 16, EDGE_DROP_M 15, PEEK_H 1,
PEEK_MAX_DOWN tan 41, or $V010_NEW_RULE's measured values; old: 0, 4, 0.5, 1 = origin/main's rule), then
travel_to(x, y, z, fast="never") as the agent
calls it (blocking), and one wait_for on its id if it returned "still waiting" (cap 150 s a trip). The new
constants are set back after every trip, whatever happened. Per trip a row in runtime/probes/v010/kg-trips.jsonl
(goal, rule, wall s, outcome, m left, legs, legs "along the route", end position and its distance to the goal, both
result texts) and a screenshot at the end (K1-<scenario>-<k>-<rule>.jpg). A trip that ends in combat or dialogue
stops the run: reload the start save (relaunch) and resume with first_k.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from common import OUT, call, lua_json, rec, save_image, server

HERE = Path(__file__).resolve().parent
TRIPS = OUT / "kg-trips.jsonl"
CAP_S = 150.0
# new = the shipped constants, or the window's measured ones: V010_NEW_RULE="CAMERA_M,EDGE_DROP_M,PEEK_H,PITCH_DEG"
# (orchestrator 2026-11-02: when K0's camera read differs, the window uses what the screen shows)
_M = os.environ.get("V010_NEW_RULE", "").split(",")
NEW = (
    f"{float(_M[0])}, {float(_M[1])}, {float(_M[2])}, math.tan(math.rad({float(_M[3])}))"
    if len(_M) == 4
    else "16, 15, 1.0, math.tan(math.rad(41))"
)
RULES = {"new": NEW, "old": "0, 4, 0.5, 1"}

SET_RULE = r"""
local V = Mods.BG3AI.BG3AI.Visibility
V.CAMERA_M, V.EDGE_DROP_M, V.PEEK_H, V.PEEK_MAX_DOWN = @ARG@
return Ext.Json.Stringify({ V.CAMERA_M, V.EDGE_DROP_M, V.PEEK_H, V.PEEK_MAX_DOWN })
"""

SNAP = r"""
local B = Mods.BG3AI.BG3AI
local V, N = B.Visibility, B.Nav
local party = {}
for _, row in ipairs(Osi.DB_PartyMembers:Get(nil) or {}) do
    local u = row[1]
    local x, y, z = Osi.GetPosition(u)
    if x then party[#party + 1] = { uuid = u, pos = { x, y, z } } end
end
local hx, hy, hz = Osi.GetPosition(Osi.GetHostCharacter())
N.known(nil, { hx, hy, hz }) -- creates the breadcrumb store (PersistentVars.nav) if the save had none
local nav = Mods.BG3AI.PersistentVars and Mods.BG3AI.PersistentVars.nav
B._kg_snap = { party = party, nav = nav and Ext.Json.Stringify(nav) or nil }
local cells = 0
for _, l in pairs(nav and nav.levels or {}) do for _ in pairs(l.cells or {}) do cells = cells + 1 end end
return Ext.Json.Stringify({ party = party, leader = { hx, hy, hz }, nav_cells = cells, seen_pts = N.seenPtsN })
"""

RESTORE = r"""
local B = Mods.BG3AI.BG3AI
local N = B.Nav
local s = B._kg_snap
if not s then return Ext.Json.Stringify({ error = "no snapshot in this Lua state (run kg_ab.py run again)" }) end
for _, p in ipairs(s.party) do
    local x, y, z = p.pos[1], p.pos[2], p.pos[3]
    if not pcall(Osi.TeleportToPosition, p.uuid, x, y, z, "", 0, 0, 0, 0, 1) then
        pcall(Osi.TeleportToPosition, p.uuid, x, y, z, "")
    end
end
if s.nav then Mods.BG3AI.PersistentVars.nav = Ext.Json.Parse(s.nav) end
N.seenPts, N.seenPtsN = {}, 0
return Ext.Json.Stringify({ ok = true, members = #s.party })
"""

CHECK = r"""
local B = Mods.BG3AI.BG3AI
local s = B._kg_snap
local worst = 0
for _, p in ipairs(s and s.party or {}) do
    local x, y, z = Osi.GetPosition(p.uuid)
    local d = math.sqrt((x - p.pos[1]) ^ 2 + (y - p.pos[2]) ^ 2 + (z - p.pos[3]) ^ 2)
    if d > worst then worst = d end
end
local hx, hy, hz = Osi.GetPosition(Osi.GetHostCharacter())
return Ext.Json.Stringify({ worst_m = worst, leader = { hx, hy, hz } })
"""


def check() -> dict:
    """CHECK plus the bridge's own combat/dialogue flags (state.status)."""
    out = lua_json(CHECK)
    st = server().game.try_rpc("state.status") or {}
    out["in_combat"], out["in_dialogue"] = bool(st.get("in_combat")), bool(st.get("in_dialogue"))
    return out


def goals(scenario: str, n: int) -> list[dict]:
    lead = lua_json("local x,y,z = Osi.GetPosition(Osi.GetHostCharacter()) return Ext.Json.Stringify({x,y,z})")
    if not isinstance(lead, list):
        raise SystemExit(f"no leader position: {lead}")
    data = json.loads((HERE / "kg_goals.json").read_text())["goals"][scenario]
    picked: list[dict] = []
    for g in sorted(data, key=lambda g: -g["count"]):
        d = math.hypot(g["x"] - lead[0], g["z"] - lead[2])
        if not (30 <= d <= 130 and abs(g["y"] - lead[1]) <= 25):
            continue
        if any(math.hypot(g["x"] - p["x"], g["z"] - p["z"]) < 15 for p in picked):
            continue
        picked.append({**g, "start_m": round(d, 1)})
        if len(picked) >= n:
            break
    return picked


def set_rule(rule: str) -> object:
    return lua_json(SET_RULE.replace("@ARG@", RULES[rule]))


def parse(text: str) -> dict:
    left = re.findall(r"\(([\d.]+)m left\)", text) or re.findall(r"([\d.]+)m left", text)
    stop = re.search(r"STOPPED: ([^|\n]+)", text)
    return {
        "stopped": stop.group(1).strip()[:200] if stop else None,
        "arrived": not stop and bool(re.search(r"\bdone\b", text)) and not re.search(r"stopped short|FAILED", text),
        "left_m": float(left[-1]) if left else None,
        "legs": len(re.findall(r"\bleg \d+:", text)),
        "route_legs": text.count("along the route"),
    }


def clean_start(chk: dict) -> bool:
    """After the restore: out of combat and dialogue, every member within 3 m of its snapshot spot. worst_m 0 (all
    back exactly) is the perfect start; a missing or non-number worst_m (the read failed) is not clean."""
    worst = chk.get("worst_m")
    if chk.get("in_combat") or chk.get("in_dialogue") or not isinstance(worst, (int, float)):
        return False
    return worst <= 3


def trip(scenario: str, k: int, rule: str, goal: dict) -> int:
    """One trip in this process (spawned by run)."""
    srv = server()
    label = f"K1-{scenario}-{k}-{rule}"
    try:
        r = lua_json(RESTORE, "server", label)
        if r.get("error"):
            print(r["error"])
            return 3
        time.sleep(3.0)
        chk = check()
        if not clean_start(chk):
            print(f"not a clean start: {chk}")
            return 3
        print(label, "rule", set_rule(rule))
        t0 = time.time()
        out = call(label, "travel_to", {"x": goal["x"], "y": goal["y"], "z": goal["z"], "fast": "never"})
        text = out if isinstance(out, str) else str(out)
        more = ""
        m = re.search(r"wait_for\('([^']+)'\)", text)
        if "still waiting" in text and m:
            more = call(label, "wait_for", {"target": m.group(1), "timeout_s": max(5.0, CAP_S - (time.time() - t0))})
            more = more if isinstance(more, str) else str(more)
        wall = time.time() - t0
        end = check()
        lp = end.get("leader") or [0, 0, 0]
        row = {
            "scenario": scenario,
            "k": k,
            "rule": rule,
            "goal": goal,
            "wall_s": round(wall, 1),
            **parse(text + "\n" + more),
            "end": lp,
            "end_to_goal_m": round(math.dist(lp, [goal["x"], goal["y"], goal["z"]]), 1),
            "combat_or_dialogue": bool(end.get("in_combat") or end.get("in_dialogue")),
            "shot": save_image(srv.screenshot(scale=0.5), label),
            "text": text,
            "more": more,
        }
        with TRIPS.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(
            f"{label}: {wall:.0f} s arrived={row['arrived']} stopped={row['stopped']} left={row['left_m']} "
            f"legs={row['legs']} route={row['route_legs']} end->goal {row['end_to_goal_m']} m"
        )
        return 3 if row["combat_or_dialogue"] else 0
    finally:
        print("constants back to new:", set_rule("new"))


def run(scenario: str, n: int, first: int) -> int:
    picked = goals(scenario, n)
    for i, g in enumerate(picked):
        print(f"goal {i}: ({g['x']}, {g['y']}, {g['z']}) {g['start_m']} m, launch-v1 hits {g['count']}")
    if first == 0:
        t0 = time.time()
        rec(f"K1-{scenario}", "snapshot", None, snap := lua_json(SNAP, "server"), t0)
        (OUT / f"kg-snap-{scenario}.json").write_text(json.dumps({"snap": snap, "goals": picked}))
    for k in range(first, len(picked)):
        for rule in ("new", "old") if k % 2 == 0 else ("old", "new"):
            rc = subprocess.call([sys.executable, __file__, "trip", scenario, str(k), rule, json.dumps(picked[k])])
            if rc:
                print(
                    f"trip {k} {rule} ended with {rc}: stopped. Relaunch on the start save, then: "
                    f"kg_ab.py run {scenario} {n} {k}"
                )
                return rc
    return summary(scenario)


def summary(scenario: str = "") -> int:
    rows = [json.loads(x) for x in TRIPS.read_text().splitlines()] if TRIPS.exists() else []
    rows = [r for r in rows if not scenario or r["scenario"] == scenario]
    for rule in ("old", "new"):
        rr = [r for r in rows if r["rule"] == rule]
        if rr:
            arr = sum(r["end_to_goal_m"] <= 3.0 for r in rr)  # reached: ended within 3 m of the goal
            print(
                f"{rule}: {len(rr)} trips, reached (<= 3 m) {arr}, short {len(rr) - arr}, median wall "
                f"{sorted(r['wall_s'] for r in rr)[len(rr) // 2]} s, end->goal "
                f"{[r['end_to_goal_m'] for r in rr]}"
            )
    for r in rows:
        print(
            f"  {r['scenario']} {r['k']} {r['rule']}: {r['wall_s']} s arrived={r['arrived']} "
            f"stopped={r['stopped']} left={r['left_m']} legs={r['legs']}/{r['route_legs']} {r['shot']}"
        )
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "plan":
        for i, g in enumerate(goals(argv[1], int(argv[2]) if len(argv) > 2 else 3)):
            print(i, g)
        return 0
    if cmd == "run":
        return run(argv[1], int(argv[2]) if len(argv) > 2 else 3, int(argv[3]) if len(argv) > 3 else 0)
    if cmd == "trip":
        return trip(argv[1], int(argv[2]), argv[3], json.loads(argv[4]))
    if cmd == "summary":
        return summary(argv[1] if len(argv) > 1 else "")
    if cmd == "rule":
        print(set_rule(argv[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
