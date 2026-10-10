"""Offline tests: resources & economy (economy.py) and the tools built on it (ALERTS, food/junk/gear filters,
trade sell=junk, stash junk to camp, equip_best, long_rest partial guard).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import economy, render, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402
from bg3ai.render import Handles  # noqa: E402

CLAUDE = "11111111-0000-0000-0000-000000000001"
GALE = "11111111-0000-0000-0000-000000000002"


def _u(n: int) -> str:
    return f"aaaaaaaa-0000-0000-0000-{n:012d}"


PALADIN = ["HeavyArmor", "LightArmor", "MartialWeapons", "MediumArmor", "Shields", "SimpleWeapons"]
WIZARD = ["Daggers", "LightArmor", "LightCrossbows", "Quarterstaffs", "Shields", "Spears"]


def s026_inventory() -> dict:
    """Shaped like state.inventory live in S026: Claude carries the Sentinel Shield and junk; Gale wears a
    Simple Robe and carries a heavy Chain Mail he can't wear; supplies short; Claude Encumbered."""
    items = [
        # Claude worn
        {"uuid": _u(1), "name": "Sword of Justice", "owner": CLAUDE, "equipped_slot": "MeleeMainHand",
         "dmg": "1d8", "prof": "MartialWeapons|Longswords", "rarity": "uncommon", "ench": 1},
        {"uuid": _u(2), "name": "Scale Mail of Devotion", "owner": CLAUDE, "equipped_slot": "Breast",
         "ac": 14, "armor": "ScaleMail", "prof": "MediumArmor"},
        # Claude bag
        {"uuid": _u(3), "name": "Sentinel Shield", "owner": CLAUDE, "slot_type": "MeleeOffHand",
         "ac": 2, "shield": True, "rarity": "rare", "value": 400},
        {"uuid": _u(4), "name": "Cup", "owner": CLAUDE, "cat": "JunkArtificial", "tab": "Misc", "value": 1,
         "weight": 0.05},
        {"uuid": _u(5), "name": "Skull", "owner": CLAUDE, "cat": "JunkBio", "tab": "Misc", "value": 2, "weight": 0.5},
        {"uuid": _u(6), "name": "Longsword", "owner": CLAUDE, "slot_type": "MeleeMainHand", "dmg": "1d8",
         "prof": "MartialWeapons|Longswords", "value": 15, "weight": 1.35},
        {"uuid": _u(7), "name": "Potion of Healing", "owner": CLAUDE, "cat": "PotionHealing", "tab": "Consumable",
         "count": 3, "value": 40},
        {"uuid": _u(8), "name": "Tower-Shaped Key", "owner": CLAUDE, "story": True, "tab": "BooksAndKeys"},
        {"uuid": _u(9), "name": "Camp Supply Sack", "owner": CLAUDE, "container": True},
        {"uuid": _u(10), "name": "Bread", "owner": CLAUDE, "supply": 3, "count": 4, "in": "Camp Supply Sack",
         "cat": "Food", "tab": "Consumable", "value": 2},
        {"uuid": _u(11), "name": "Silver Candlestick", "owner": CLAUDE, "cat": "Luxury", "tab": "Misc", "value": 30},
        {"uuid": _u(12), "name": "Chain Mail", "owner": CLAUDE, "slot_type": "Breast", "ac": 16,
         "armor": "ChainMail", "prof": "HeavyArmor", "value": 75, "weight": 25},
        {"uuid": _u(13), "name": "Thieves' Tools", "owner": CLAUDE, "tab": "Misc", "value": 25},
        # Gale worn + bag
        {"uuid": _u(20), "name": "Simple Robe", "owner": GALE, "equipped_slot": "Breast", "ac": 10, "armor": "None"},
        {"uuid": _u(21), "name": "Quarterstaff", "owner": GALE, "equipped_slot": "MeleeMainHand", "dmg": "1d6",
         "prof": "SimpleWeapons|Quarterstaffs"},
        {"uuid": _u(22), "name": "Robe of Summer", "owner": GALE, "slot_type": "Breast", "ac": 11, "armor": "None",
         "rarity": "uncommon", "value": 150},
        {"uuid": _u(23), "name": "Spear", "owner": GALE, "slot_type": "MeleeMainHand", "dmg": "1d6",
         "prof": "SimpleWeapons|Spears", "value": 5},
    ]
    return {"items": items, "gold": 93, "supplies": 12,
            "members": {CLAUDE: {"prof": PALADIN, "enc": 1, "kg": 162.0, "limit": 159.0},
                        GALE: {"prof": WIZARD, "enc": 0}}}


STATUS = {"in_combat": False, "party": [{"uuid": CLAUDE, "name": "Claude", "hp": 34, "max_hp": 88},
                                        {"uuid": GALE, "name": "Gale", "hp": 46, "max_hp": 64}]}


class Junk(unittest.TestCase):
    def test_sells_clutter_and_spare_common_gear_only(self):
        names = {it["name"] for it in economy.junk(s026_inventory())}
        self.assertEqual(names, {"Cup", "Skull", "Longsword", "Silver Candlestick", "Spear"})

    def test_never_sells_equipped_story_consumables_food_keys_tools_magic(self):
        names = {it["name"] for it in economy.junk(s026_inventory())}
        for keep in ("Sword of Justice", "Sentinel Shield", "Potion of Healing", "Tower-Shaped Key", "Bread",
                     "Camp Supply Sack", "Thieves' Tools", "Robe of Summer"):
            self.assertNotIn(keep, names)

    def test_upgrade_targets_and_keep_list_are_kept(self):
        inv = s026_inventory()
        inv["members"][CLAUDE]["prof"] = PALADIN
        names = {it["name"] for it in economy.junk(inv, keep={_u(4)})}
        self.assertNotIn("Cup", names)
        self.assertNotIn("Chain Mail", names)  # AC 16 beats Claude's AC 14: an upgrade, not junk


class Upgrades(unittest.TestCase):
    def ups(self, inv=None):
        return {(u["who"], u["name"]): u for u in economy.upgrades(inv or s026_inventory())}

    def test_sentinel_shield_goes_to_the_paladin_off_hand(self):
        u = self.ups()[(CLAUDE, "Sentinel Shield")]
        self.assertEqual(u["slot"], "MeleeOffHand")
        self.assertIn("empty slot", u["why"])

    def test_heavy_armour_only_for_the_proficient(self):
        ups = self.ups()
        self.assertIn((CLAUDE, "Chain Mail"), ups)
        self.assertNotIn((GALE, "Chain Mail"), ups)

    def test_gale_gets_the_magic_robe_moved_from_his_bag(self):
        u = self.ups()[(GALE, "Robe of Summer")]
        self.assertEqual(u["holder"], GALE)
        self.assertEqual(u["slot"], "Breast")

    def test_no_shield_with_a_two_handed_weapon(self):
        inv = s026_inventory()
        inv["items"][0].update(twohanded=True, name="Greatsword", dmg="2d6")
        self.assertNotIn((CLAUDE, "Sentinel Shield"), self.ups(inv))

    def test_story_item_in_the_slot_is_left_alone(self):
        inv = s026_inventory()
        inv["items"][0] = {"uuid": _u(1), "name": "Moonlantern", "owner": CLAUDE, "equipped_slot": "MeleeMainHand",
                           "story": True}
        self.assertNotIn((CLAUDE, "Longsword"), self.ups(inv))

    def test_accessories_by_rarity_and_boots_need_no_armour_proficiency(self):
        inv = s026_inventory()
        inv["items"] += [{"uuid": _u(30), "name": "Evasive Shoes", "owner": GALE, "slot_type": "Boots", "ac": 0,
                          "armor": "Leather", "rarity": "rare"},
                         {"uuid": _u(31), "name": "Amulet", "owner": GALE, "slot_type": "Amulet", "ac": 0}]
        inv["members"][GALE]["prof"] = ["Daggers", "Quarterstaffs"]  # no armour at all
        ups = self.ups(inv)
        self.assertIn((GALE, "Evasive Shoes"), ups)
        self.assertIn("rare vs empty slot", ups[(GALE, "Evasive Shoes")]["why"])
        self.assertFalse(any(name == "Amulet" for _, name in ups))  # plain common amulet: not worth a line

    def test_each_item_goes_to_one_member(self):
        ups = economy.upgrades(s026_inventory())
        ids = [u["uuid"] for u in ups]
        self.assertEqual(len(ids), len(set(ids)))


class Alerts(unittest.TestCase):
    def test_camp_chest_counts_toward_supplies(self):
        inv = s026_inventory()
        inv["camp_supplies"] = 30
        al = " | ".join(economy.alerts(STATUS, inv))
        self.assertNotIn("camp supplies", al)  # 12 carried + 30 in the chest >= 40
        inv["camp_supplies"] = 6
        al = " | ".join(economy.alerts(STATUS, inv))
        self.assertIn("camp supplies 18/40 (12 carried + 6 in the camp chest)", al)

    def test_s026_alerts(self):
        al = " | ".join(economy.alerts(STATUS, s026_inventory()))
        self.assertIn("low HP Claude 34/88", al)
        self.assertNotIn("Gale 46/64", al)  # 72%
        self.assertIn("camp supplies 12/40", al)
        self.assertIn("gold 93 < 200", al)
        self.assertIn("Encumbered 162.0/159.0kg (drop 3.0kg)", al)
        self.assertIn("Sentinel Shield", al)

    def test_low_hp_says_how_to_heal(self):
        al = " | ".join(economy.alerts(STATUS, s026_inventory(), short_rests=2))
        self.assertIn("low HP Claude 34/88 (short rests left 2: short_rest)", al)
        al = " | ".join(economy.alerts(STATUS, s026_inventory(), short_rests=0))
        self.assertIn("no short rests left: long rest needs 40 supplies, have 12", al)

    def test_short_rest_text(self):
        after = {"party": [dict(STATUS["party"][0], hp=62), STATUS["party"][1]]}
        self.assertEqual(economy.short_rest_text(STATUS, after, {"short_rests_left": 1, "of": 2}),
                         "HP Claude 34→62/88 · short rests left 1/2")
        self.assertEqual(economy.short_rest_text(STATUS, STATUS, None), "HP unchanged")

    def test_no_hp_alert_in_combat(self):
        st = dict(STATUS, in_combat=True)
        self.assertFalse(any("low HP" in a for a in economy.alerts(st, s026_inventory())))

    def test_tracker_dedups_and_forces(self):
        t = economy.AlertTracker()
        self.assertTrue(t.line(["a"]).startswith("ALERTS a"))
        self.assertEqual(t.line(["a"]), "")
        self.assertEqual(t.line(["a"], force=True), "ALERTS a")
        self.assertEqual(t.line(["a", "b"]), "ALERTS a | b")
        self.assertEqual(t.line([]), "")

    def test_partial_rest_is_remembered_until_a_full_rest(self):
        t = economy.AlertTracker()
        t.rested({"rest": "partial", "supplies_used": 27, "supplies_required": 40})
        self.assertIn("PARTIAL (27/40", t.partial_rest)
        t.rested({"rest": "full"})
        self.assertIsNone(t.partial_rest)
        t.rested("garbage")
        self.assertIsNone(t.partial_rest)


class Report(unittest.TestCase):
    def test_gear_report_lines(self):
        lines = economy.gear_report(s026_inventory(), {CLAUDE: "Claude", GALE: "Gale"}.get)
        text = "\n".join(lines)
        self.assertIn("- Claude: equip Sentinel Shield in MeleeOffHand", text)
        self.assertIn("junk to sell:", text)
        self.assertIn("camp supplies: 12/40 carried", text)


class RenderInventory(unittest.TestCase):
    def setUp(self):
        self.H = Handles()
        self.H.names.update({self.H.get(CLAUDE): "Claude", self.H.get(GALE): "Gale"})

    def test_food_filter_shows_supply_values_and_total(self):
        out = render.render_inventory(s026_inventory(), self.H, "food")
        self.assertIn("CAMP SUPPLIES 12/40 carried", out)
        self.assertIn("Bread x4 supply=3 (x4=12)", out)
        self.assertNotIn("Cup", out)

    def test_junk_filter_and_summary(self):
        out = render.render_inventory(s026_inventory(), self.H, "junk")
        self.assertIn("Skull", out)
        self.assertNotIn("Potion of Healing", out)
        self.assertRegex(out, r"JUNK 5 items ~\d+g")

    def test_default_view_lists_gear_upgrades(self):
        out = render.render_inventory(s026_inventory(), self.H, "")
        self.assertIn("BETTER GEAR IN BAGS (equip_best equips all):", out)
        self.assertIn("Sentinel Shield → MeleeOffHand", out)


class FakeGame:
    def __init__(self, inv: dict, trade: dict | None = None, fail: set[str] | None = None):
        self.inv, self.trade, self.fail = inv, trade or {}, fail or set()
        self.jobs: list[tuple[str, dict]] = []

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float | None = None) -> Any:
        if op == "state.inventory":
            return self.inv
        if op == "state.status":
            return STATUS
        if op == "ui.trade":
            self.trade_reads = getattr(self, "trade_reads", []) + [dict(args or {})]
            return self.trade
        raise AssertionError(f"unexpected op {op}")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        try:
            return self.rpc(op, args, context)
        except AssertionError:
            return default

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0, **kw) -> JobResult:
        self.jobs.append((op, args))
        if args.get("item") in self.fail:
            return JobResult(status="failed", error="refused")
        return JobResult(status="done", result={"confirmed": True})

    def drain_events(self, ctx: str) -> list:
        return []


class Tools(unittest.TestCase):
    def setUp(self):
        # Fresh handles and alerts: other test modules expect e1 to be their own party member.
        saved = (server.H, server.ALERTS)
        server.H, server.ALERTS = Handles(), server.economy.AlertTracker()
        self.addCleanup(lambda: (setattr(server, "H", saved[0]), setattr(server, "ALERTS", saved[1])))

    def use(self, fake: FakeGame) -> FakeGame:
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        return fake

    def test_stash_junk_sends_each_junk_item_to_camp(self):
        g = self.use(FakeGame(s026_inventory()))
        out = server.manage_inventory("camp", "junk")
        sent = {a["item"] for op, a in g.jobs if op == "act.transfer" and a["to"] == "camp"}
        self.assertEqual(sent, {_u(4), _u(5), _u(6), _u(11), _u(23)})
        self.assertIn("sent 5 junk items to camp", out)

    def test_sell_junk_offers_junk_on_screen_within_trader_gold(self):
        trade = {"trader_gold": 40, "your_items": [
            {"uuid": _u(6), "price": 30}, {"uuid": _u(11), "price": 20}, {"uuid": _u(4), "price": 1},
            {"uuid": _u(7), "price": 40, "count": 3}]}  # the potions are on screen but never sold
        g = self.use(FakeGame(s026_inventory(), trade))
        out = server.trade(sell="junk")
        op, args = g.jobs[-1]
        self.assertEqual(op, "ui.trade")
        self.assertEqual(args["offer"], [_u(6), _u(4)])  # 30 + 1 fits in 40; the candlestick (20) doesn't
        self.assertTrue(args["confirm"])
        self.assertIn("sold 2 junk items, ~31 g", out)
        self.assertIn("1 more left", out)
        self.assertGreaterEqual(g.trade_reads[0].get("max", 0), 500)  # every slot, not the screen's first 60

    def test_sell_junk_stack_price_is_whole_stack(self):
        # TradePrice is the stack's price (live: Cup x3 = 24): 24 fits in 30, never 24 x 3 = 72
        trade = {"trader_gold": 30, "your_items": [{"uuid": _u(6), "price": 24, "count": 3}]}
        g = self.use(FakeGame(s026_inventory(), trade))
        out = server.trade(sell="junk")
        self.assertEqual(g.jobs[-1][1]["offer"], [_u(6)])
        self.assertIn("~24 g", out)

    def test_sell_junk_nothing_on_screen(self):
        self.use(FakeGame(s026_inventory(), {"trader_gold": 500, "your_items": []}))
        self.assertIn("nothing sold (none of the junk is on this trade screen)", server.trade(sell="junk"))

    def test_equip_best_equips_upgrades(self):
        g = self.use(FakeGame(s026_inventory()))
        out = server.equip_best()
        equipped = {(a["char"], a["item"]) for op, a in g.jobs if op == "act.equip"}
        self.assertIn((CLAUDE, _u(3)), equipped)
        self.assertIn((GALE, _u(22)), equipped)
        self.assertIn("equip_best: done", out)

    def test_equip_best_moves_from_another_bag_first(self):
        inv = s026_inventory()
        inv["items"][2]["owner"] = GALE  # the shield sits in Gale's bag
        g = self.use(FakeGame(inv))
        server.equip_best(server.H.get(CLAUDE))
        ops = [(op, a.get("item"), a.get("to") or a.get("char")) for op, a in g.jobs]
        i = ops.index(("act.transfer", _u(3), CLAUDE))
        self.assertEqual(ops[i + 1], ("act.equip", _u(3), CLAUDE))

    def test_alerts_line_in_full_status_only_once_in_deltas(self):
        server.ALERTS.last = ()
        st, inv = STATUS, s026_inventory()
        self.assertIn("ALERTS low HP Claude 34/88", server._alerts_line(st, inv, force=True))
        self.assertEqual(server._alerts_line(st, inv), "")
        inv["gold"] = 300
        self.assertNotIn("gold", server._alerts_line(st, inv))

    def test_short_rest_result_shows_hp_and_rests_left(self):
        class G(FakeGame):
            n = 0

            def rpc(self, op, args=None, context="server", timeout=None):
                if op == "state.status":
                    G.n += 1
                    return STATUS if G.n == 1 else {"party": [dict(STATUS["party"][0], hp=70), STATUS["party"][1]]}
                return super().rpc(op, args, context)

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                return JobResult(status="done", result={"short_rests_left": 1, "of": 2})
        self.use(G(s026_inventory()))
        out = server.short_rest()
        self.assertIn("HP Claude 34→70/88 · short rests left 1/2", out)
        self.assertEqual(server.ALERTS.short_rests, 1)

    def test_alerts_line_never_raises(self):
        self.assertEqual(server._alerts_line(None, {"items": "bad"}), "")


if __name__ == "__main__":
    unittest.main()
