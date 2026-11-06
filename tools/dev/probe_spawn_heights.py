"""Where a character lands on a grid of spots: one load, game up on the arena's start save, no setup run.

live 15 / v010 window 3 part D: arena-v0's file spawns passed the 0.3 m rule, but setup's twins landed 0.73 m apart
in height (B's pair-1 spot x 229.05 z 276: y 5.85; A's x 229.05 z 268.2: 5.12). The 0b strip read before it was at
an unrecorded x (the snap put it 0.4-2.6 m east of the ask), so no logged read covers the member spots. This probe
teleports one character (the host, unless --char) to each grid spot with setup's own call (ArenaSetup.lua STEPS[2]:
TeleportToPosition u, x, y, z, "", 0, 0, 0, 0, 1), waits setup's settle (Setup.SETTLE_MS 2 s), reads
Osi.GetPosition, and writes every landed spot. Then it picks spawns: A's and B's lines at the same x (members
LINE_GAP_M 1.5 m apart along x, as Setup.line), lines GAP_MIN..GAP_MAX m apart in z, every member landed at its asked
x/z (within 0.2 m) and every member of both lines within --max-dy of the others (the landed check needs pair i under
0.3 m; this asks it of all four).

Run (game up, the arena's save loaded, the debugger port open; ~4-5 min for the default grid):
  uv run python tools/dev/probe_spawn_heights.py --out runtime/probes/spawn-heights.json
  uv run python tools/dev/probe_spawn_heights.py --pick runtime/probes/spawn-heights.json   (offline, again)
The throwaway load is not saved; the host ends where the last spot put it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

LINE_GAP_M = 1.5  # Setup.LINE_GAP_M
SETTLE_S = 2.0  # Setup.SETTLE_MS
GAP_MIN, GAP_MAX = 6.0, 9.0  # A's line to B's (arena-v0: 7.8 m)
XS = [228.3 + 0.75 * i for i in range(9)]  # 228.3 .. 234.3: member spots at grid points, centres between them
ZS = [266.0 + 1.0 * i for i in range(15)]  # 266 .. 280 (south of 265 is blocked: live 15 0b)
ASK_Y = 5.5  # the y asked: the game snaps to the ground


def teleport_lua(char: str | None, x: float, y: float, z: float) -> str:
    who = json.dumps(char) if char else "Osi.GetHostCharacter()"
    return (
        f"local u = {who}\n"
        f"local ok = pcall(Osi.TeleportToPosition, u, {x!r}, {y!r}, {z!r}, '', 0, 0, 0, 0, 1)\n"
        f"if not ok then ok = pcall(Osi.TeleportToPosition, u, {x!r}, {y!r}, {z!r}, '') end\n"
        "return Ext.Json.Stringify({ok = ok, u = u})"
    )


def read_lua(char: str | None) -> str:
    who = json.dumps(char) if char else "Osi.GetHostCharacter()"
    return f"local x, y, z = Osi.GetPosition({who})\nreturn Ext.Json.Stringify({{x = x, y = y, z = z}})"


def probe(char: str | None, xs: list[float], zs: list[float], out: Path) -> list[dict[str, Any]]:
    from bg3harness.game import io

    rows: list[dict[str, Any]] = []
    for z in zs:
        for x in xs:
            t = json.loads(io.lua_eval(teleport_lua(char, x, ASK_Y, z), context="server"))
            time.sleep(SETTLE_S)
            p = json.loads(io.lua_eval(read_lua(char), context="server"))
            rows.append({"ask": [x, z], "ok": t.get("ok"), "at": [p.get("x"), p.get("y"), p.get("z")]})
            print(json.dumps(rows[-1]), flush=True)
            out.write_text(json.dumps({"rows": rows}, indent=1))  # each spot kept if the game goes down mid-probe
    return rows


def pick(rows: list[dict[str, Any]], max_dy: float = 0.2, max_off: float = 0.2) -> list[dict[str, Any]]:
    """Every level spawn pair the rows allow, best first: [{A: [x, y, z], B: [x, y, z], dy, gap}] (spawn = the line's
    centre: x, the mean landed y, z)."""
    at: dict[tuple[float, float], float] = {}
    for r in rows:
        (ax, az), (lx, ly, lz) = r["ask"], r["at"]
        if r.get("ok") and None not in (lx, ly, lz) and abs(lx - ax) <= max_off and abs(lz - az) <= max_off:
            at[(round(ax, 2), round(az, 2))] = float(ly)
    xs = sorted({k[0] for k in at})
    zs = sorted({k[1] for k in at})
    out = []
    for x0 in xs:
        x1 = round(x0 + LINE_GAP_M, 2)
        for za in zs:
            for zb in zs:
                if not GAP_MIN <= zb - za <= GAP_MAX:
                    continue
                ys = [at.get(k) for k in ((x0, za), (x1, za), (x0, zb), (x1, zb))]
                if any(y is None for y in ys):
                    continue
                dy = max(ys) - min(ys)  # type: ignore[type-var,operator]
                if dy < max_dy:
                    cx = round(x0 + LINE_GAP_M / 2, 3)
                    ya, yb = (ys[0] + ys[1]) / 2, (ys[2] + ys[3]) / 2  # type: ignore[operator]
                    out.append(
                        {
                            "A": [cx, round(ya, 2), za],
                            "B": [cx, round(yb, 2), zb],
                            "dy": round(dy, 3),
                            "gap": round(zb - za, 2),
                        }
                    )
    return sorted(out, key=lambda c: (c["dy"], abs(c["gap"] - 7.8)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Where a character lands on a grid of spots; level arena spawns.")
    ap.add_argument("--out", type=Path, help="probe the grid live and write the rows here")
    ap.add_argument("--pick", type=Path, help="pick spawns from a rows file written before (offline)")
    ap.add_argument("--char", help="the character to move (default: the host)")
    ap.add_argument("--max-dy", type=float, default=0.2)
    a = ap.parse_args(argv)
    if bool(a.out) == bool(a.pick):
        ap.error("one of --out (live) or --pick (offline)")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        rows = probe(a.char, XS, ZS, a.out)
    else:
        rows = json.loads(a.pick.read_text())["rows"]
    cands = pick(rows, a.max_dy)
    print(json.dumps({"candidates": cands[:10], "n": len(cands)}, indent=1))
    return 0 if cands else 1


if __name__ == "__main__":
    sys.exit(main())
