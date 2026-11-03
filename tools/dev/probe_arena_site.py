"""An arena SITE search on the start save: one load, no setup run, nothing saved.

The arena-v0 height probe (ops/logs/live.md '# arena-v0 height probe', 2026-11-03) found level spawn pairs on the
crash-site strip, but the ground there favours side A: the wreck's raised deck (3.5 m higher) 6.7-9 m from A's line
and only at 9 m+ from B's, and a Nautiloid Tank and a Cartilaginous Chest (CanJoinCombat 1) near A. This searches a
wider region for a fair site and ranks every candidate with its reasons.

Live, in this order (one load):
  1. party: every party member's position before anything moves (the sitters stay there in a match; the probe moves
     the host), then every character and item within the region (+ JOIN_M) that can join a combat (CanJoinCombat 1,
     not dead, not in the party): name, position, HP.
  2. coarse: a grid (--radius around --cx/--cz, --step) of landed heights, as probe_spawn_heights does (setup's own
     TeleportToPosition, its 2 s settle, Osi.GetPosition).
  3. fine: for the --fine best coarse sites, each line's member spots and their neighbours (x +-0.75, z +-1).
Offline (--rank FILE), and at the end of a live run: the ranked sites. A site is two lines of --members members at
the same x (Setup.line: LINE_GAP_M apart along x), GAP_MIN..GAP_MAX m apart in z. It passes when:
  - level: all members' landed heights within LEVEL_M (fine) and robust: every neighbour of a member spot within
    ROBUST_M of its line (a landing drift can't lift a member);
  - no joiner (an item or character that can join the fight) within JOIN_M of a member of either line;
  - no one-sided high ground: every coarse spot within RISE_NEAR_M of a line that is RISE_M or more above it has a
    mirror (the same offset with z flipped, from the other line) that is RISE_M or more above that line, both ways;
  - every sitter SITTER_M or more from every member of both lines (Setup.check_sitters).
Each site lists its reasons; passing sites first (lowest height spread, joiners farthest, gap nearest 7.8 m).

Run (game up, the arena's save loaded, the debugger port open; ~25 min for the defaults):
  uv run python tools/dev/probe_arena_site.py --out runtime/probes/arena-site.json
  uv run python tools/dev/probe_arena_site.py --rank runtime/probes/arena-site.json      (offline, again)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_spawn_heights import ASK_Y, GAP_MAX, GAP_MIN, LINE_GAP_M, SETTLE_S, read_lua, teleport_lua

LEVEL_M = 0.2  # all members' landed heights within this (setup refuses a pair 0.3 m apart)
ROBUST_M = 0.3  # a member spot's neighbours within this of its line
JOIN_M = 15.0  # no combat-capable item or character this near a member
RISE_M = 2.3  # GuardCast.lua HighGroundThreshold: the attack-roll high-ground bonus
RISE_NEAR_M = 9.0  # within a move (Movement 9)
SITTER_M = 10.0  # Setup.SITTER_MIN_M
COARSE_OFF_M = 1.0  # a coarse spot counts when it landed this near its ask (x/z)
FINE_OFF_M = 0.2  # a fine spot: as probe_spawn_heights.pick
MIRROR_TOL_M = 3.0  # a rise's mirror: the nearest coarse spot within this of the mirrored point

MOD = "Mods.BG3AI.BG3AI"
PARTY_LUA = f"""
local V = {MOD}.Visibility
local out = {{}}
local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
for _, row in ipairs(ok and rows or {{}}) do
    local u = V.uuid(row[1])
    local x, y, z = Osi.GetPosition(u)
    out[#out + 1] = {{uuid = u, name = V.name(u), pos = {{x, y, z}}}}
end
return Ext.Json.Stringify(out)
"""


def joiners_lua(cx: float, cz: float, r: float) -> str:
    return f"""
local V = {MOD}.Visibility
local party = {{}}
local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
for _, row in ipairs(ok and rows or {{}}) do party[(V.uuid(row[1]) or ""):lower()] = true end
local out = {{}}
for _, comp in ipairs({{"ServerCharacter", "ServerItem"}}) do
    for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent(comp) or {{}}) do
        local okp, p = pcall(function() return e.Transform.Transform.Translate end)
        if okp and p and math.sqrt((p[1] - {cx!r}) ^ 2 + (p[3] - {cz!r}) ^ 2) <= {r!r} then
            local u = e.Uuid and e.Uuid.EntityUuid
            if u and not party[u:lower()] and Osi.CanJoinCombat(u) == 1 and Osi.IsDead(u) ~= 1 then
                out[#out + 1] = {{uuid = u, name = V.name(u), pos = {{p[1], p[2], p[3]}}, hp = Osi.GetHitpoints(u),
                    item = comp == "ServerItem"}}
            end
        end
    end
end
return Ext.Json.Stringify(out)
"""


def coarse_spots(cx: float, cz: float, radius: float, step: float) -> list[tuple[float, float]]:
    n = int(radius // step)
    return [(round(cx + i * step, 2), round(cz + j * step, 2)) for j in range(-n, n + 1) for i in range(-n, n + 1)]


def member_xs(cx: float, members: int) -> list[float]:
    return [round(cx + (i - (members - 1) / 2) * LINE_GAP_M, 3) for i in range(members)]


def fine_spots(site: dict[str, Any], members: int) -> list[tuple[float, float]]:
    xs = member_xs(site["cx"], members)
    spots = {(round(x + dx, 3), round(z + dz, 2)) for z in (site["za"], site["zb"]) for x in xs
             for dx, dz in ((0, 0), (-0.75, 0), (0.75, 0), (0, -1), (0, 1))}  # fmt: skip
    return sorted(spots, key=lambda s: (s[1], s[0]))


def land(evaluate: Callable[..., str], sleep: Callable[[float], None], spots, char: str | None, log) -> list[dict]:
    rows = []
    for x, z in spots:
        t = json.loads(evaluate(teleport_lua(char, x, ASK_Y, z), context="server"))
        sleep(SETTLE_S)
        p = json.loads(evaluate(read_lua(char), context="server"))
        rows.append({"ask": [x, z], "ok": t.get("ok"), "at": [p.get("x"), p.get("y"), p.get("z")]})
        log(rows[-1])
    return rows


def heights(rows: list[dict], off: float) -> dict[tuple[float, float], float]:
    out = {}
    for r in rows:
        (ax, az), (lx, ly, lz) = r["ask"], r["at"]
        if r.get("ok") and None not in (lx, ly, lz) and abs(lx - ax) <= off and abs(lz - az) <= off:
            out[(round(ax, 3), round(az, 2))] = float(ly)
    return out


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def one_sided(h: dict, a: tuple[float, float, float], b: tuple[float, float, float]) -> list[str]:
    """Coarse spots RISE_M+ above line a within RISE_NEAR_M with no such mirror from line b (z offset flipped)."""
    out = []
    for (x, z), y in h.items():
        if y - a[1] < RISE_M or _dist((x, z), (a[0], a[2])) > RISE_NEAR_M:
            continue
        m = (b[0] + (x - a[0]), b[2] - (z - a[2]))
        near = min(h, key=lambda k: _dist(k, m))
        if _dist(near, m) > MIRROR_TOL_M or h[near] - b[1] < RISE_M:
            out.append(f"({x:g}, {z:g}) {y - a[1]:+.1f} m at {_dist((x, z), (a[0], a[2])):.1f} m, no mirror")
    return out


def coarse_sites(h: dict, step: float) -> list[dict[str, Any]]:
    sites = []
    for (x, za), ya in h.items():
        for (x2, zb), yb in h.items():
            if x2 == x and GAP_MIN <= zb - za <= GAP_MAX + 1e-9 and abs(ya - yb) < ROBUST_M:
                sites.append({"cx": x, "za": za, "zb": zb, "ya": ya, "yb": yb})
    return sites


def judge(site: dict, h: dict, fine: dict | None, party: list[dict], joiners: list[dict], members: int) -> dict:
    """The site's verdict: its reasons (none = it passes), the spawns (landed when fine read), the facts."""
    reasons: list[str] = []
    xs = member_xs(site["cx"], members)
    spots = {"A": [(x, site["za"]) for x in xs], "B": [(x, site["zb"]) for x in xs]}
    ys = {"A": site["ya"], "B": site["yb"]}
    out: dict[str, Any] = {
        "cx": site["cx"],
        "za": site["za"],
        "zb": site["zb"],
        "gap": round(site["zb"] - site["za"], 2),
    }
    out["dy"] = round(abs(site["ya"] - site["yb"]), 3)  # the coarse spread until a fine read says better
    if fine is not None:
        if any((round(x, 3), z) not in fine for s in spots for x, z in spots[s]):
            reasons.append("a member spot didn't land where asked (fine)")
        else:
            got = {s: [fine[(round(x, 3), z)] for x, z in spots[s]] for s in spots}
            flat = [y for s in got for y in got[s]]
            out["dy"] = round(max(flat) - min(flat), 3)
            if out["dy"] >= LEVEL_M:
                reasons.append(f"members {out['dy']:.2f} m apart in height (fine)")
            ys = {s: sum(got[s]) / len(got[s]) for s in got}
            for s in spots:
                for x, z in spots[s]:
                    for n in ((x - 0.75, z), (x + 0.75, z), (x, z - 1), (x, z + 1)):
                        yn = fine.get((round(n[0], 3), round(n[1], 2)))
                        if yn is not None and abs(yn - ys[s]) > ROBUST_M:
                            reasons.append(f"not robust: ({n[0]:g}, {n[1]:g}) {yn - ys[s]:+.2f} m next to {s}'s line")
    else:
        reasons.append("coarse only (no fine read)")
    out["A"] = [site["cx"], round(ys["A"], 2), site["za"]]
    out["B"] = [site["cx"], round(ys["B"], 2), site["zb"]]
    near = []
    for j in joiners:
        d = min(_dist((j["pos"][0], j["pos"][2]), m) for s in spots for m in spots[s])
        if d < JOIN_M:
            near.append(f"{j['name']}{' (item)' if j.get('item') else ''} {d:.1f} m")
    if near:
        reasons.append("can join the fight within 15 m: " + ", ".join(near))
    out["joiner_m"] = round(min((min(_dist((j["pos"][0], j["pos"][2]), m) for s in spots for m in spots[s])
                                 for j in joiners), default=math.inf), 1)  # fmt: skip
    for s, o in (("A", "B"), ("B", "A")):
        a = (site["cx"], ys[s], site["za"] if s == "A" else site["zb"])
        b = (site["cx"], ys[o], site["za"] if o == "A" else site["zb"])
        for r in one_sided(h, a, b):
            reasons.append(f"high ground for {s} only: {r}")
    for p in party:
        d = min(_dist((p["pos"][0], p["pos"][2]), m) for s in spots for m in spots[s])
        if d < SITTER_M:
            reasons.append(f"the sitter {p['name']} {d:.1f} m from a line")
    out["reasons"] = reasons
    return out


def rank(data: dict, members: int = 2) -> list[dict]:
    h = heights(data["coarse"], COARSE_OFF_M)
    fine = heights(data.get("fine") or [], FINE_OFF_M)
    fine_keys = {tuple(s) for s in data.get("fine_sites") or []}
    out = []
    for site in coarse_sites(h, data.get("step", 4.0)):
        f = fine if (site["cx"], site["za"], site["zb"]) in fine_keys else None
        out.append(judge(site, h, f, data.get("party") or [], data.get("joiners") or [], members))
    return sorted(out, key=lambda s: (len(s["reasons"]), s["dy"], -min(s["joiner_m"], 99), abs(s["gap"] - 7.8)))


def probe(a: argparse.Namespace, evaluate: Callable[..., str], sleep: Callable[[float], None] = time.sleep) -> dict:
    data: dict[str, Any] = {"cx": a.cx, "cz": a.cz, "radius": a.radius, "step": a.step, "members": a.members}

    def save(row: Any = None) -> None:
        if row is not None:
            print(json.dumps(row), flush=True)
        a.out.write_text(json.dumps(data, indent=1))  # each spot kept if the game goes down mid-probe

    data["party"] = json.loads(evaluate(PARTY_LUA, context="server", timeout=60))  # before the host moves
    data["joiners"] = json.loads(evaluate(joiners_lua(a.cx, a.cz, a.radius + JOIN_M), context="server", timeout=120))
    save()
    data["coarse"] = []
    for spot in coarse_spots(a.cx, a.cz, a.radius, a.step):
        data["coarse"] += land(evaluate, sleep, [spot], a.char, save)
    h = heights(data["coarse"], COARSE_OFF_M)
    best = sorted((judge(s, h, None, data["party"], data["joiners"], a.members) for s in coarse_sites(h, a.step)),
                  key=lambda s: (len(s["reasons"]), s["dy"], abs(s["gap"] - 7.8)))[: a.fine]  # fmt: skip
    data["fine_sites"], data["fine"] = [[s["cx"], s["za"], s["zb"]] for s in best], []
    for s in best:
        data["fine"] += land(evaluate, sleep, fine_spots(s, a.members), a.char, save)
    save()
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="An arena site search: level, robust, fair ground on the start save.")
    ap.add_argument("--out", type=Path, help="probe live and write everything here")
    ap.add_argument("--rank", type=Path, help="rank the sites of a file written before (offline)")
    ap.add_argument("--cx", type=float, default=233.0, help="the region's centre x (default: the crash-site strip)")
    ap.add_argument("--cz", type=float, default=272.0)
    ap.add_argument("--radius", type=float, default=40.0)
    ap.add_argument("--step", type=float, default=4.0, help="the coarse grid's step (m)")
    ap.add_argument("--fine", type=int, default=8, help="how many coarse sites get a fine read")
    ap.add_argument("--members", type=int, default=2, help="members per line (arena-v0: 2)")
    ap.add_argument("--char", help="the character to move (default: the host)")
    a = ap.parse_args(argv)
    if bool(a.out) == bool(a.rank):
        ap.error("one of --out (live) or --rank (offline)")
    if a.out:
        from bg3harness.game import io

        a.out.parent.mkdir(parents=True, exist_ok=True)
        data = probe(a, io.lua_eval)
    else:
        data = json.loads(a.rank.read_text())
    sites = rank(data, data.get("members", a.members))
    print(json.dumps({"sites": sites[:10], "n": len(sites), "passing": sum(not s["reasons"] for s in sites)}, indent=1))
    return 0 if sites and not sites[0]["reasons"] else 1


if __name__ == "__main__":
    sys.exit(main())
