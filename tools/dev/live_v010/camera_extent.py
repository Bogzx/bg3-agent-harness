"""Check K0 (known ground step 0): how much ground the real camera shows, at default zoom, full zoom-out and in the
tactical view; and the live cost of V.pointSeen.

    uv run --project bridge python tools/dev/live_v010/camera_extent.py read <tag>      # this camera as it is
    uv run --project bridge python tools/dev/live_v010/camera_extent.py zoomout <tag>   # wheel out until it stops
    uv run --project bridge python tools/dev/live_v010/camera_extent.py tactical <tag>  # press O (default key), read
    uv run --project bridge python tools/dev/live_v010/camera_extent.py cost [radius]   # V.pointSeen timing

read: the active camera (Client/Pick.lua P.camera + P.intrinsics, the projection model verified live 2026-10-09)
casts the rays through 9 window points (corners, edge midpoints, centre) onto the ground plane at the controlled
character's feet height and prints, per point, the ground spot's horizontal distance from that character and its
bearing relative to the camera's heading (0 = straight ahead); plus the camera's pitch (degrees below horizontal),
its distance and height over the character, and fx/fy. A ray above the horizon prints "sky". A screenshot is saved
as runtime/probes/v010/K0-<tag>.jpg. Compare: top edge = ground ahead, bottom edge = behind, side midpoints =
sideways; the design assumed ~40 m round the leader and a ~60 degree look down (V.CAMERA_M, V.PEEK_MAX_DOWN).

cost: on the server, V.pointSeen on a 1.5 m grid round the leader (heights from N.floorHeights, the floor nearest
the leader's height), timed with Ext.Utils.MonotonicTime in two bands (0-18.5 m, 18.5-40 m), each under the new
constants and again under the old ones (CAMERA_M 0, EDGE_DROP_M 4, PEEK_H 0.5, PEEK_MAX_DOWN 1); the constants are
put back to the new values at the end. N.known's seen-point memory is not used (pointSeen is called directly).
"""

from __future__ import annotations

import json
import sys
import time

from common import lua_json, rec, save_image, server

LEADER = r"""
local u = Osi.GetHostCharacter()
local x, y, z = Osi.GetPosition(u)
return Ext.Json.Stringify({ uuid = u, pos = { x, y, z } })
"""

CAMERA = r"""
local P = Mods.BG3AI.BG3AI.Pick
local L = Ext.Json.Parse('@ARG@')
local ctrl, cam = P.camera()
if not cam then return Ext.Json.Stringify({ error = "no active camera" }) end
local t = cam.Transform.Transform
local C, q = t.Translate, t.RotationQuat
local fx, fy, aspect = P.intrinsics(ctrl)
local function rot(v) -- v rotated by q = (x, y, z, w)
    local x, y, z, w = q[1], q[2], q[3], q[4]
    local tx = 2 * (y * v[3] - z * v[2])
    local ty = 2 * (z * v[1] - x * v[3])
    local tz = 2 * (x * v[2] - y * v[1])
    return { v[1] + w * tx + (y * tz - z * ty), v[2] + w * ty + (z * tx - x * tz), v[3] + w * tz + (x * ty - y * tx) }
end
local f = rot({ 0, 0, 1 })
local head = math.atan(f[1], f[3])
local out = { pitch_deg = math.deg(math.asin(-f[2] / math.sqrt(f[1] ^ 2 + f[2] ^ 2 + f[3] ^ 2))), fx = fx, fy = fy,
    aspect = aspect, cam = { C[1], C[2], C[3] }, leader = L,
    cam_over_leader_m = C[2] - L[2], cam_dist_m = math.sqrt((C[1] - L[1]) ^ 2 + (C[2] - L[2]) ^ 2 + (C[3] - L[3]) ^ 2),
    points = {} }
local pts = { { "top-left", -1, -1 }, { "top", 0, -1 }, { "top-right", 1, -1 }, { "left", -1, 0 }, { "centre", 0, 0 },
    { "right", 1, 0 }, { "bottom-left", -1, 1 }, { "bottom", 0, 1 }, { "bottom-right", 1, 1 } }
for _, p in ipairs(pts) do
    local d = rot({ p[2] / fx, -p[3] / fy, 1 })
    local row = { name = p[1] }
    if d[2] >= -1e-4 then row.sky = true else
        local s = (L[2] - C[2]) / d[2]
        local gx, gz = C[1] + s * d[1], C[3] + s * d[3]
        local dx, dz = gx - L[1], gz - L[3]
        row.m = math.sqrt(dx * dx + dz * dz)
        local b = math.deg(math.atan(dx, dz) - head)
        row.bearing = (b + 540) % 360 - 180
    end
    out.points[#out.points + 1] = row
end
return Ext.Json.Stringify(out)
"""

COST = r"""
local B = Mods.BG3AI.BG3AI
local V, N = B.Visibility, B.Nav
local R = @ARG@
local NEW = { V.CAMERA_M, V.EDGE_DROP_M, V.PEEK_H, V.PEEK_MAX_DOWN }
local OLD = { 0, 4, 0.5, 1 }
local function set(c) V.CAMERA_M, V.EDGE_DROP_M, V.PEEK_H, V.PEEK_MAX_DOWN = c[1], c[2], c[3], c[4] end
local ctx = V.context()
local lead = ctx.leader or (ctx.observers[1] and ctx.observers[1].pos)
if not lead then return Ext.Json.Stringify({ error = "no leader" }) end
local bands = { near = {}, far = {} }
for ix = -R, R, 1.5 do
    for iz = -R, R, 1.5 do
        local d = math.sqrt(ix * ix + iz * iz)
        if d <= R then
            local x, z = lead[1] + ix, lead[3] + iz
            local ok, hs = N.floorHeights(x, z)
            local best
            if ok and type(hs) == "table" then
                for _, h in ipairs(hs) do
                    local dh = type(h) == "number" and math.abs(h - lead[2])
                    if dh and dh <= 20 and (not best or dh < math.abs(best - lead[2])) then best = h end
                end
            end
            if best then table.insert(d <= 18.5 and bands.near or bands.far, { x, best, z }) end
        end
    end
end
local out = { leader = lead, observers = #ctx.observers }
for _, rule in ipairs({ { "new", NEW }, { "old", OLD } }) do
    set(rule[2])
    for name, pts in pairs(bands) do
        local seen, no, err = 0, 0, 0
        local t0 = Ext.Utils.MonotonicTime()
        for _, p in ipairs(pts) do
            local s = V.pointSeen(ctx, p)
            if s == true then seen = seen + 1 elseif s == false then no = no + 1 else err = err + 1 end
        end
        local ms = Ext.Utils.MonotonicTime() - t0
        out[rule[1] .. "_" .. name] = { n = #pts, ms = ms, per_point_ms = #pts > 0 and ms / #pts or 0, seen = seen,
            not_seen = no, no_answer = err }
    end
end
set(NEW)
out.restored = { V.CAMERA_M, V.EDGE_DROP_M, V.PEEK_H, V.PEEK_MAX_DOWN }
return Ext.Json.Stringify(out)
"""


def read(tag: str) -> dict:
    srv = server()
    lead = lua_json(LEADER, "server")
    if "error" in lead:
        print("no leader:", lead["error"])
        return lead
    t0 = time.time()
    r = lua_json(CAMERA.replace("@ARG@", json.dumps(lead["pos"])), "client")
    rec(f"K0-{tag}", "camera", None, r, t0)
    if "error" not in r:
        print(
            f"K0 {tag}: pitch {r['pitch_deg']:.1f} deg down, camera {r['cam_dist_m']:.1f} m from the leader "
            f"({r['cam_over_leader_m']:.1f} m above), fx {r['fx']:.3f} fy {r['fy']:.3f}"
        )
        for p in r["points"]:
            print(f"   {p['name']:<13} " + ("sky" if p.get("sky") else f"{p['m']:6.1f} m  bearing {p['bearing']:6.1f}"))
    print("   screenshot:", save_image(srv.screenshot(scale=0.5), f"K0-{tag}"))
    return r


def cam_dist() -> float | None:
    lead = lua_json(LEADER, "server")
    r = lua_json(CAMERA.replace("@ARG@", json.dumps(lead.get("pos") or [0, 0, 0])), "client")
    return r.get("cam_dist_m")


def zoomout(tag: str) -> None:
    inp = server()._inp()
    if inp is None:
        print("no input shim")
        return
    w, h = inp.window_size()
    last = cam_dist()
    for i in range(12):  # 2 notches a step, as clicker.py zooms; stop when the camera stops moving out
        inp.wheel(-2, w // 2, h // 2)
        time.sleep(0.8)  # the camera eases into place
        d = cam_dist()
        print(f"   wheel step {i + 1}: camera {d} m")
        if d is not None and last is not None and abs(d - last) < 0.05:
            break
        last = d
    read(tag)


def tactical(tag: str) -> None:
    inp = server()._inp()
    if inp is None:
        print("no input shim")
        return
    inp.key("O")
    time.sleep(1.5)
    read(tag)


def cost(radius: str) -> None:
    t0 = time.time()
    r = lua_json(COST.replace("@ARG@", str(float(radius))), "server")
    rec("K0-cost", "pointSeen", {"radius": radius}, r, t0)
    for k in ("new_near", "new_far", "old_near", "old_far"):
        b = r.get(k) or {}
        print(
            f"   {k:<9} n {b.get('n')} {b.get('ms')} ms ({b.get('per_point_ms', 0):.3f} ms/point) seen "
            f"{b.get('seen')} not {b.get('not_seen')} no-answer {b.get('no_answer')}"
        )
    print("   constants after:", r.get("restored"), "(want [40, 15, 1, 1.732...])")


def main(argv: list[str]) -> int:
    cmd, arg = argv[0], (argv[1] if len(argv) > 1 else "")
    if cmd == "read":
        read(arg or "now")
    elif cmd == "zoomout":
        zoomout(arg or "zoomout")
    elif cmd == "tactical":
        tactical(arg or "tactical")
    elif cmd == "cost":
        cost(arg or "40")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
