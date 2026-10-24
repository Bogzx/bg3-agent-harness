"""manage_inventory: identical items picked instead of 'ambiguous' (S027 equip 'Simple Robe' stopped a batch)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import render  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402
from bg3ai.tools import core, inventory  # noqa: E402

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
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        # Fresh handles: other modules register their own "Gale"/"Simple Robe" (then names are ambiguous).
        saved_h, core.H = core.H, render.Handles()
        self.addCleanup(setattr, core, "H", saved_h)
        for u in (GALE, CLAUDE):
            core.H.get(u, "Gale" if u == GALE else "Claude")
        for it in items:
            core.H.get(it["uuid"], it["name"])
        return fake

    def test_identical_items_pick_one_unworn_in_own_bag(self):
        self.use([robe(R[0], CLAUDE, worn=True), robe(R[1], CLAUDE), robe(R[2], GALE), robe(R[3], GALE, worn=True)])
        self.assertEqual(inventory._item_uuid("Simple Robe", "Gale"), R[2])
        self.assertEqual(inventory._item_uuid("simple robe", "Gale", worn=True), R[3])

    def test_different_items_stay_ambiguous(self):
        self.use([robe(R[0], GALE), robe(R[1], GALE, rarity="uncommon")])
        with self.assertRaises(ValueError):
            inventory._item_uuid("Simple Robe", "Gale")

    def test_one_carried_namesake_wins_over_a_traders_stock(self):
        # S057 07:49: e239 in the bags, e460 a trader's stack seen in the trade window (not in state.inventory)
        pot = lambda u: {"uuid": u, "name": "Potion of Superior Healing", "owner": CLAUDE, "rarity": "uncommon"}  # noqa: E731
        fake = self.use([pot(R[0])])
        core.H.get(R[1], "Potion of Superior Healing")
        self.assertEqual(inventory._item_uuid("Potion of Superior Healing", "Claude"), R[0])

    def test_nothing_carried_stays_ambiguous(self):
        self.use([])
        core.H.get(R[0], "Potion of Superior Healing")
        core.H.get(R[1], "Potion of Superior Healing")
        with self.assertRaises(ValueError):
            inventory._item_uuid("Potion of Superior Healing", "Claude")

    def test_worn_item_by_name_in_a_fresh_bridge(self):
        # D8: feed_gale("Gloves of Power") on Claude's worn gloves was 'unknown target' until a get_inventory
        gloves = {"uuid": R[0], "name": "Gloves of Power", "owner": CLAUDE, "rarity": "uncommon",
                  "slot_type": "Gloves", "equipped_slot": "Gloves"}
        fake = self.use([])
        fake.items = [gloves]
        out = getattr(inventory.feed_gale, "fn", inventory.feed_gale)("Gloves of Power")
        self.assertEqual(fake.jobs, [("ui.donate", {"item": R[0]})])
        self.assertIn("feed_gale: done", out)

    def test_name_nobody_carries_stays_unknown(self):
        self.use([])
        with self.assertRaisesRegex(ValueError, "unknown target"):
            inventory._item_uuid("Gloves of Power")


if __name__ == "__main__":
    unittest.main()


class UuidRef(unittest.TestCase):
    def test_a_known_uuid_resolves_to_itself(self):
        """live4: trade(npc=<uuid>) failed "unknown target 'e246'" while trade(npc='e246') worked."""
        h = render.Handles()
        e = h.get(GALE, "Gale")
        self.assertEqual(h.resolve(GALE), GALE)
        self.assertEqual(h.resolve(GALE.upper()), GALE)
        self.assertEqual(h.resolve("S_Player_Gale_" + GALE), GALE)
        self.assertEqual(h.resolve(e), GALE)

    def test_an_unknown_uuid_stays_unknown(self):
        h = render.Handles()
        with self.assertRaisesRegex(ValueError, "unknown target"):
            h.resolve(CLAUDE)
