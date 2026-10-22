"""U2 ESC menu open/close cycles (live1 §2, L2): ui.key ESCAPE, poll ui.state for game_menu, ESCAPE, poll for
game. PASS: every menu opened and closed on the first ESC and none stuck; times (RPC to ui.state) in the evidence.

    u2_esc_menu.py [<cycles>=50]
"""

from __future__ import annotations

import json
import statistics as st

from probelib import Probe, Verdict, arg, main


def q(v: list[float], p: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(p * len(s)))]


def judge(rows: list[dict]) -> Verdict:
    cyc = [r["open_s"] + r["close_s"] for r in rows]
    bad_open = sum(r["opened"] != "game_menu" for r in rows)
    bad_close = sum(r["closed"] != "game" for r in rows)
    stuck = sum(r["recovered"] != "game" for r in rows)
    what = (
        f"{len(rows)} cycles, median {st.median(cyc):.3f} s, p90 {q(cyc, 0.9):.3f} s, max {max(cyc):.3f} s; "
        f"not opened {bad_open}, second ESC needed {bad_close}, stuck {stuck}"
    )
    return ("PASS" if not (bad_open or bad_close or stuck) else "FAIL"), what


def run(p: Probe) -> Verdict:
    rows = []
    for i in range(int(arg(p, 0, "50"))):
        t0 = p.clock()
        p.game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
        a = p.wait_screen("game_menu", 5, 0.02)
        t1 = p.clock()
        p.game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
        b = p.wait_screen("game", 5, 0.02)
        t2 = p.clock()
        b2 = b
        if b != "game":
            p.game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
            b2 = p.wait_screen("game", 5, 0.02)
        rows.append({"i": i, "open_s": t1 - t0, "close_s": t2 - t1, "opened": a, "closed": b, "recovered": b2})
        p.sleep(0.3)
    (p.out / "esc_cycles.json").write_text(json.dumps(rows, indent=1))
    return judge(rows)


if __name__ == "__main__":
    raise SystemExit(main(run))
