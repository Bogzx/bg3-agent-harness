"""Check H2 (human-gap item 2, minimap block): what the frontier costs inside the walk-end read on a real level, and
whether the block's heights match the game's positions.

    uv run --project bridge python tools/dev/live_v010/minimap_cost.py [rounds]

Times delta.changes_text (the walk-end read: one server eval) with ACTION_PARTS alone and with ("frontier",)
added, alternating, `rounds` times each (default 8), and prints the medians: the difference is the frontier's
mod time inside an eval the walk end makes anyway (estimate 20-40 ms; the separate nav.frontier eval it replaced
was median 56 ms). Then renders the block from the last read (minimap.block) and, per objective marker, the
height the block should show: marker y minus the active member's y from the same read (shown from 1.5 m, as the
renderer does). A map screenshot is saved as H2-map.jpg to compare with the HUD.
"""

from __future__ import annotations

import statistics
import sys
import time

from common import rec, save_image, server


def main(argv: list[str]) -> int:
    rounds = int(argv[0]) if argv else 8
    srv = server()
    from bg3ai import delta, minimap
    from bg3ai.tools import core

    ms: dict[str, list[float]] = {"plain": [], "frontier": []}
    got: dict = {}
    for _ in range(rounds):
        for name, parts in (("plain", delta.ACTION_PARTS), ("frontier", (*delta.ACTION_PARTS, "frontier"))):
            g: dict = {}
            t0 = time.perf_counter()
            delta.changes_text(core.game, core.H, parts, got=g)
            ms[name].append((time.perf_counter() - t0) * 1000)
            if name == "frontier":
                got = g
            time.sleep(0.3)
    med = {k: round(statistics.median(v), 1) for k, v in ms.items()}
    rec("H2-cost", "changes_text", {"rounds": rounds}, {"median_ms": med, "all_ms": ms}, time.time())
    print(
        f"H2 read cost: plain {med['plain']} ms, with frontier {med['frontier']} ms, "
        f"frontier inside the eval {round(med['frontier'] - med['plain'], 1)} ms"
    )
    block = minimap.block(got, core.H)
    print("H2 block:\n" + block)
    here = minimap.here(got.get("status"))
    markers = got.get("markers")
    for m in markers if isinstance(markers, list) else []:
        if isinstance(m, dict) and isinstance(m.get("pos"), list) and here:
            label = m.get("name") or m.get("label") or m.get("quest")
            print(f"   marker {label}: dy {m['pos'][1] - here[1]:+.1f} m, here={m.get('here')} dist={m.get('dist')}")
    rec("H2-block", "minimap.block", None, {"block": block, "here": here, "markers": markers}, time.time())
    print("   map screenshot:", save_image(srv.screenshot(scale=0.5, map=True), "H2-map"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
