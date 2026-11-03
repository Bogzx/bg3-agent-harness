"""Offline tests for tools/dev/probe_arena_site.py (no game): the live flow against a fake game, and the verdicts.

Run: uv run pytest -q tools/dev/test_probe_arena_site.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_arena_site as ps

AV = {"uuid": "av", "name": "Tav", "pos": [300.0, 5.0, 300.0]}  # the avatar, far off


def flat(x, z):
    return 5.0


class FakeGame:
    """The debugger's evaluate: a teleport lands on the height map h (x/z as asked), a read returns it."""

    def __init__(self, h, party=(AV,), joiners=()):
        self.h, self.party, self.joiners, self.pos, self.calls = h, list(party), list(joiners), None, []

    def __call__(self, code, context="client", timeout=15):
        if "DB_PartyMembers" in code and "CanJoinCombat" not in code:
            self.calls.append("party")
            return json.dumps(self.party)
        if "CanJoinCombat" in code:
            self.calls.append("joiners")
            return json.dumps(self.joiners)
        if "TeleportToPosition" in code:
            m = re.search(r"u, ([-\d.]+), ([-\d.]+), ([-\d.]+),", code)
            assert m, code
            x, _, z = (float(v) for v in m.groups())
            self.pos = (x, z)
            self.calls.append("teleport")
            return json.dumps({"ok": True})
        assert self.pos is not None, "a read before any teleport"
        x, z = self.pos
        return json.dumps({"x": x, "y": self.h(x, z), "z": z})


def run(tmp_path, game, fine=3):
    a = argparse.Namespace(cx=0.0, cz=0.0, radius=8.0, step=4.0, fine=fine, members=2, char=None,
                           out=tmp_path / "site.json")  # fmt: skip
    data = ps.probe(a, game, sleep=lambda s: None)
    assert json.loads(a.out.read_text()) == data
    return data, ps.rank(data)


def test_the_party_and_the_joiners_are_read_before_anything_moves(tmp_path):
    game = FakeGame(flat)
    data, sites = run(tmp_path, game)
    assert game.calls[:2] == ["party", "joiners"] and set(game.calls[2:]) == {"teleport"}
    assert len(data["coarse"]) == 25 and data["fine_sites"]  # 5 x 5 coarse spots, then the fine reads
    best = sites[0]
    assert best["reasons"] == [] and best["dy"] == 0 and best["gap"] == 8.0
    assert best["A"] == [best["cx"], 5.0, best["za"]] and best["B"] == [best["cx"], 5.0, best["zb"]]


def test_a_deck_near_one_line_only_is_high_ground_for_that_side(tmp_path):
    # the wreck's deck (arena-v0 probe): 3.5 m up, south of A's line only
    def deck(x, z):
        return 8.5 if z <= -8 else 5.0

    _, sites = run(tmp_path, FakeGame(deck))
    a_near = [s for s in sites if s["za"] == -4.0]
    assert a_near and all(any(r.startswith("high ground for A only") for r in s["reasons"]) for s in a_near)


def test_a_mirrored_rise_is_fair(tmp_path):
    def two_decks(x, z):
        return 8.5 if abs(z) >= 8 else 5.0  # a deck behind each line, the same distance

    _, sites = run(tmp_path, FakeGame(two_decks))
    mid = [s for s in sites if (s["za"], s["zb"]) == (-4.0, 4.0)]
    assert mid and not any("high ground" in r for s in mid for r in s["reasons"])


def test_a_joiner_within_15_m_and_a_sitter_within_10_m_are_named(tmp_path):
    tank = {"uuid": "t", "name": "Nautiloid Tank", "pos": [0.0, 5.0, -10.0], "hp": 1, "item": True}
    sh = {"uuid": "sh", "name": "Sha", "pos": [0.0, 5.0, 9.0]}
    _, sites = run(tmp_path, FakeGame(flat, party=(AV, sh), joiners=(tank,)))
    s = next(s for s in sites if (s["cx"], s["za"], s["zb"]) == (0.0, -4.0, 4.0))
    assert any(r.startswith("can join the fight within 15 m: Nautiloid Tank (item) 6.0 m") for r in s["reasons"])
    assert "the sitter Sha 5.1 m from a line" in s["reasons"]  # 5 m in z, 0.75 m in x from a member


def test_a_step_next_to_a_member_spot_is_not_robust(tmp_path):
    # live probe: B's low channel with 5.22-5.29 one grid step away
    def channel(x, z):
        return 5.4 if (z == 5.0 and x < 0) else 5.0

    _, sites = run(tmp_path, FakeGame(channel), fine=99)
    s = next(s for s in sites if (s["cx"], s["za"], s["zb"]) == (0.0, -4.0, 4.0))
    assert any(r.startswith("not robust: (-0.75, 5) +0.40 m next to B's line") for r in s["reasons"]), s["reasons"]
    assert s["dy"] == 0.0  # the members themselves stand level


def test_a_site_without_a_fine_read_never_passes():
    h = {(0.0, -4.0): 5.0, (0.0, 4.0): 5.0}
    data = {"coarse": [{"ask": [x, z], "ok": True, "at": [x, y, z]} for (x, z), y in h.items()], "step": 4.0}
    (s,) = ps.rank(data)
    assert s["reasons"] == ["coarse only (no fine read)"]


def test_a_member_spot_that_snapped_away_in_the_fine_read_is_a_reason_not_a_crash():
    h = {(0.0, -4.0): 5.0, (0.0, 4.0): 5.0}
    data = {"coarse": [{"ask": [x, z], "ok": True, "at": [x, y, z]} for (x, z), y in h.items()], "step": 4.0,
            "fine_sites": [[0.0, -4.0, 4.0]],
            "fine": [{"ask": [-0.75, -4.0], "ok": True, "at": [0.1, 5.0, -4.0]}]}  # fmt: skip
    (s,) = ps.rank(data)
    assert s["reasons"] == ["a member spot didn't land where asked (fine)"] and s["dy"] == 0.0


def test_a_loose_pickup_is_listed_not_a_reason_and_the_lua_skips_contained_items(tmp_path):
    potion = {"uuid": "p", "name": "Potion of Speed", "pos": [0.0, 5.0, -8.0], "hp": 1, "item": True, "pickup": True}
    unread = {"uuid": "b", "name": "Spiked Bulb", "pos": [0.0, 5.0, 8.0], "hp": 1, "item": True}  # flag unread
    _, sites = run(tmp_path, FakeGame(flat, joiners=(potion, unread)), fine=99)
    s = next(s for s in sites if (s["cx"], s["za"], s["zb"]) == (0.0, -4.0, 4.0))
    assert s["pickups"] == ["Potion of Speed (item) 4.1 m"]
    assert s["reasons"] == ["can join the fight within 15 m: Spiked Bulb (item) 4.1 m"]  # unread counts (fail closed)
    assert s["joiner_m"] == 4.1
    lua = ps.joiners_lua(0, 0, 10)
    assert "e.InventoryMember ~= nil or Osi.GetInventoryOwner(u) ~= nil" in lua and "not inside" in lua
