"""manage_inventory: identical items picked instead of 'ambiguous' (S027 equip 'Simple Robe' stopped a batch)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

GALE = "aaaaaaaa-0000-0000-0000-000000000001"
CLAUDE = "aaaaaaaa-0000-0000-0000-000000000002"
R = [f"bbbbbbbb-0000-0000-0000-00000000000{i}" for i in range(4)]


def robe(u, owner, worn=False, rarity="common"):
    return {"uuid": u, "name": "Simple Robe", "owner": owner, "rarity": rarity, "slot_type": "Breast", "ac": 10,
            **({"equipped_slot": "Breast"} if worn else {})}


class Fake:
    def __init__(self, items):
        self.items, self.jobs = items, []

    def rpc(self, op, args=None, context="server"):
        if op == "state.inventory":
            return {"items": self.items}
        if op == "state.status":
            return {"active": GALE, "party": []}
        return {}

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        self.jobs.append((op, args))
        return JobResult(status="done", result={})

    def drain_events(self, ctx):
        return []


class ItemRef(unittest.TestCase):
    def use(self, items):
        fake = Fake(items)
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        for u in (GALE, CLAUDE):
            server.H.get(u, "Gale" if u == GALE else "Claude")
        for it in items:
            server.H.get(it["uuid"], it["name"])
        return fake

    def test_identical_items_pick_one_unworn_in_own_bag(self):
        self.use([robe(R[0], CLAUDE, worn=True), robe(R[1], CLAUDE), robe(R[2], GALE), robe(R[3], GALE, worn=True)])
        self.assertEqual(server._item_uuid("Simple Robe", "Gale"), R[2])
        self.assertEqual(server._item_uuid("simple robe", "Gale", worn=True), R[3])

    def test_different_items_stay_ambiguous(self):
        self.use([robe(R[0], GALE), robe(R[1], GALE, rarity="uncommon")])
        with self.assertRaises(ValueError):
            server._item_uuid("Simple Robe", "Gale")


if __name__ == "__main__":
    unittest.main()
