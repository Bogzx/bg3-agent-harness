"""Live checks for vis's objective markers, walk_toward and large-object (door/ladder) perception.

Run once a game is loaded (any save; the Nautiloid is the reference case):

    uv run --project bridge python tools/dev/check_vis.py            # read-only
    uv run --project bridge python tools/dev/check_vis.py --move     # also walks (walk_toward x2)
    uv run --project bridge python tools/dev/check_vis.py --reload   # Ext.Debug.Reset() first (picks up new Lua)

Checks (each prints PASS / WARN / FAIL with details; exit code 1 if any FAIL):
  1 markers     nav.markers answers; objectives in this area have dist/dir; get_status/get_map show OBJECTIVE lines.
  2 walk        nav.reach_toward dry runs (objective, W). With --move: walk_toward(objective) and
                walk_toward(direction=W) finish without a teleport_blocked event.
  3 perception  dev.perception_probe is calibrated and not broken; no undiscovered trap or secret door is
                shown by the ray rule; dev.fairness_probe's undiscovered_trap/secret_door counts are the same
                with the ray rule off and on. Ray-only objects whose walking path is much longer than the
                straight line (maybe behind a wall) are listed for a human look.
  4 rayapi      The raw hit fields bg3le gives (Position, Distance, Shape.PhysicsObject.Entity), and the
                cost of state.nearby / nav.frontier with the ray rule off vs on.

Not for agent sessions: --reload and the ray on/off toggling change live mod state (rules for live testing:
docs/SETUP.md §6).
"""

from __future__ import annotations

import json
import atexit
import os
import sys
import shutil
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))
if "BG3AI_RUNTIME" not in os.environ:  # keep runtime/actions.jsonl clean; removed at exit
    os.environ["BG3AI_RUNTIME"] = tempfile.mkdtemp(prefix="bg3ai-check-")
    atexit.register(shutil.rmtree, os.environ["BG3AI_RUNTIME"], True)

from bg3ai import render  # noqa: E402
from server_view import S  # noqa: E402
from bg3ai.game import GameError  # noqa: E402

G = S.game
VIS = "Mods.BG3AI.BG3AI.Visibility"
results: list[tuple[str, str, str]] = []


def report(check: str, status: str, msg: str) -> None:
    results.append((check, status, msg))
    print(f"[{status:4s}] {check}: {msg}")


def lua(code: str) -> str:
    return G.eval(code, context="server")


def ray_rule(on: bool) -> None:
    if on:
        G.rpc("dev.perception_probe", {"radius": 1, "reset": True})
    else:
        lua(f"{VIS}.rayBroken = true; {VIS}.rayCache = {{}}; return 'off'")


# ---------------------------------------------------------------------------

def check_markers() -> list:
    res = G.rpc("nav.markers", {})
    ms = res.get("markers") or []
    if res.get("error"):
        report("markers", "FAIL", f"nav.markers error: {res['error']}")
        return ms
    here = [m for m in ms if m.get("here")]
    report("markers", "PASS" if ms else "WARN",
           f"{len(ms)} marker(s), {len(here)} in this area, {res.get('ms')} ms")
    for line in render.render_objectives(ms):
        print("         " + line)
    bad = [m for m in here if m.get("dist") is None or not m.get("dir")]
    if bad:
        report("markers", "FAIL", f"{len(bad)} here-marker(s) without dist/dir: {bad[:2]}")
    if ms and not here:
        report("markers", "WARN", "no marker has a position on this level (trigger without Transform and "
                                  "Osi.GetPosition failed?) — on the Nautiloid the Escape trigger should be here")
    status = S.get_status()
    if here:
        ok = "OBJECTIVE o" in status
        report("markers", "PASS" if ok else "FAIL", "get_status shows OBJECTIVE lines" if ok
               else "get_status has no OBJECTIVE line:\n" + status)
    gm = S.get_map()
    if ms:
        ok = "OBJECTIVES" in gm
        report("markers", "PASS" if ok else "FAIL", "get_map lists objectives" if ok else "get_map: " + gm[:400])
    return ms


def check_walk(markers: list, move: bool) -> None:
    st = G.rpc("state.status")
    char = st.get("active")
    here = [m for m in markers if m.get("here") and m.get("pos")]
    tries = [("dir W", {"char": char, "dir": "W"})]
    if here:
        tries.insert(0, (f"objective {render.objective_label(here[0])}", {"char": char, "pos": here[0]["pos"]}))
    for what, args in tries:
        try:
            r = G.rpc("nav.reach_toward", args)
            report("walk", "PASS" if r.get("reachable") else "WARN",
                   f"reach_toward {what}: " + render.compact(r, S.H))
        except GameError as e:
            report("walk", "FAIL", f"reach_toward {what}: {e}")
    if not move:
        report("walk", "INFO", "read-only run; pass --move to actually walk_toward")
        return
    for kw in ([{"objective": "o1"}] if here else []) + [{"direction": "W", "max_m": 10}]:
        S.get_status()  # so o1 is what the agent would have seen
        out = S.walk_toward(**kw)
        tele = "teleport_blocked" in out
        failed = out.startswith("FAILED") or "walk_toward: FAILED" in out or "INTERRUPTED" in out
        status = "FAIL" if tele else ("WARN" if failed else "PASS")
        report("walk", status, f"walk_toward({kw}):\n         " + out.replace("\n", "\n         "))


def check_perception() -> None:
    try:
        ray_rule(False)
        off = G.rpc("dev.fairness_probe", {"radius": 30})
        ray_rule(True)
        on = G.rpc("dev.fairness_probe", {"radius": 30})
        p = G.rpc("dev.perception_probe", {"radius": 30})
    except GameError as e:
        report("perception", "FAIL", str(e))
        ray_rule(True)
        return
    c = p.get("counts") or {}
    print("         probe: " + json.dumps({k: p.get(k) for k in ("counts", "stats", "calibrated", "ray_broken")}))
    report("perception", "PASS" if p.get("calibrated") else "FAIL",
           "floor calibration ray hit" if p.get("calibrated") else "calibration ray missed the floor: rule is off")
    report("perception", "FAIL" if p.get("ray_broken") else "PASS",
           "ray_broken" if p.get("ray_broken") else "raycasts work (no errors)")
    leak = (c.get("ray_shown_traps") or 0) + (c.get("ray_shown_secret_doors") or 0)
    report("perception", "FAIL" if leak else "PASS", f"ray-shown traps/secret doors = {leak}")
    oi, ni = off.get("items") or {}, on.get("items") or {}
    same = all(oi.get(k) == ni.get(k) for k in ("undiscovered_trap", "secret_door"))
    report("perception", "PASS" if same else "FAIL",
           f"fairness_probe items off→on: {json.dumps(oi)} → {json.dumps(ni)}")
    if (off.get("characters") or {}) != (on.get("characters") or {}):
        report("perception", "WARN", "character counts changed between runs (someone moved?) "
               f"{off.get('characters')} → {on.get('characters')}")
    ray_only = p.get("ray_only") or []
    report("perception", "PASS" if c.get("by_ray") else "WARN",
           f"{c.get('by_ray', 0)} large object(s) visible only via the ray rule "
           "(stand where a door is out of LOS but in view to exercise it)")
    char = (G.rpc("state.status") or {}).get("active")
    for o in ray_only:
        line = f"{o.get('name')} <{o.get('kind')}> {o.get('dist')}m h={o.get('height')} r={o.get('radius')}"
        try:
            path = G.rpc("nav.path", {"char": char, "target": o["uuid"]})
            pl = path.get("length_m")
            line += f" path={pl}m" if pl is not None else " path=none"
            if pl is None or pl > 2.5 * max(o.get("dist") or 1, 1):
                line += "  <-- look: maybe behind a wall"
        except GameError as e:
            line += f" path error: {e}"
        print("         ray-only: " + line)


def check_rayapi() -> None:
    code = r"""
local V = Mods.BG3AI.BG3AI.Visibility
local o = V.context().observers[1]
if not o or not o.pos then return Ext.Json.Stringify({error = "no observer"}) end
local a = { o.pos[1], o.pos[2] + 1.6, o.pos[3] }
local b = { o.pos[1], o.pos[2] - 3, o.pos[3] }
local out = {}
local ok, hit = pcall(Ext.Level.RaycastClosest, a, b, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
out.ok = ok
if not ok then out.error = tostring(hit) elseif not hit then out.hit = false else
  out.hit = true
  local function f(fn) local k, v = pcall(fn); if k then return v end; return "ERR " .. tostring(v) end
  local p = f(function() return hit.Position end)
  out.position = type(p) == "string" and p or f(function() return { p[1], p[2], p[3] } end)
  out.distance = f(function() return hit.Distance end)
  out.group = f(function() return tostring(hit.PhysicsGroup) end)
  out.entity = f(function() local e = hit.Shape.PhysicsObject.Entity; return e and e.Uuid and e.Uuid.EntityUuid or tostring(e) end)
end
local okn, none = pcall(Ext.Level.RaycastClosest, a, { a[1], a[2] + 0.2, a[3] }, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
out.short_up_ray_hit = okn and none ~= nil
return Ext.Json.Stringify(out)
"""
    try:
        raw = lua(code)
        info = json.loads(raw)
    except (GameError, json.JSONDecodeError) as e:
        report("rayapi", "FAIL", f"raw raycast probe: {e}")
        return
    report("rayapi", "PASS" if info.get("hit") else "FAIL", "floor ray: " + json.dumps(info))
    if info.get("short_up_ray_hit"):
        report("rayapi", "WARN", "a 0.2 m ray up from the eye hit something (own capsule not excluded?)")

    def cost() -> dict:
        out = {}
        for op, args in (("state.nearby", {"radius": 25}), ("nav.frontier", {})):
            t = time.perf_counter()
            G.rpc(op, args)
            out[op] = round((time.perf_counter() - t) * 1000)
        return out

    ray_rule(False)
    off = cost()
    ray_rule(True)
    lua(f"{VIS}.rayCache = {{}}; return 'ok'")
    on = cost()
    slow = any(on[k] - off[k] > 40 for k in on)
    report("rayapi", "WARN" if slow else "PASS", f"round trip ms, ray rule off {off} vs on {on}")


def main() -> int:
    args = set(sys.argv[1:])
    if not G.server_ready():
        print("Game/mod not reachable (load a save first, see docs/SETUP.md §4).")
        return 2
    if "--reload" in args:
        lua("Ext.Debug.Reset(); return 'reset'")
        for _ in range(40):
            time.sleep(0.5)
            if G.server_ready():
                break
    markers = []
    try:
        markers = check_markers()
    except GameError as e:
        report("markers", "FAIL", str(e))
    try:
        check_walk(markers, "--move" in args)
    except GameError as e:
        report("walk", "FAIL", str(e))
    check_perception()
    check_rayapi()
    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "WARN"]
    print(f"\n{len(results)} results: {len(fails)} FAIL, {len(warns)} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
