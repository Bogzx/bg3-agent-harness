"""Offline tests for tools/dev/probe_spawn_heights.py (no game): the spawn pick from landed rows.

Run: uv run pytest -q tools/dev/test_probe_spawn_heights.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_spawn_heights as ph


def rows(height):
    return [{"ask": [x, z], "ok": True, "at": [x, height(x, z), z]} for z in ph.ZS for x in ph.XS]


def test_live_15s_slope_rejects_its_own_spawns_and_finds_the_flat_east():
    # live 15's landed reads as the slope: flat at z 268, rising west of x ~231 from z 274 on (B x 229.05 z 276: 5.85)
    def h(x, z):
        return 5.1 + (max(0.0, z - 273) * max(0.0, 231.5 - x) * 0.25)

    got = ph.pick(rows(h))
    assert got, "a level pair exists east of x 231.5"
    assert not [c for c in got if c["A"][0] == 229.8 and c["B"][2] == 276.0]  # arena-v0's lines: B's 0.6 m up
    for c in got:  # every member of both lines within 0.2 m, read from the slope itself
        ys = [h(c[s][0] + d, c[s][2]) for s in "AB" for d in (-0.75, 0.75)]
        assert max(ys) - min(ys) < 0.2 and ph.GAP_MIN <= c["gap"] <= ph.GAP_MAX


def test_a_spot_that_landed_elsewhere_or_failed_doesnt_count():
    rs = rows(lambda x, z: 5.0)
    for r in rs:
        if r["ask"] == [ph.XS[0], ph.ZS[0]]:
            r["at"][0] += 0.5  # snapped east, like live 15 0b
    assert not any(c["A"] == [ph.XS[0] + 0.75, 5.0, ph.ZS[0]] for c in ph.pick(rs))
    assert ph.pick([dict(r, ok=False) for r in rs]) == []


def test_nothing_level_returns_no_candidate():
    assert ph.pick(rows(lambda x, z: z * 0.1)) == []  # 0.1 m per m in z: 6 m apart is 0.6 m
