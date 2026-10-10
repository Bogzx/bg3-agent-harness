"""Offline tests: resources & economy (economy.py) and the tools built on it (ALERTS, food/junk/gear filters,
trade sell=junk, stash junk to camp, equip_best, long_rest partial guard).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import time
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

    def test_why_names_the_proficiency_that_makes_it_legal(self):
        # S027: the agent undid Gale's Leather Armour + shield, believing a wizard can't wear them (Human can)
        inv = s026_inventory()
        inv["items"].append({"uuid": _u(24), "name": "Studded Leather", "owner": GALE, "slot_type": "Breast",
                             "ac": 12, "armor": "StuddedLeather", "prof": "LightArmor", "value": 10})
        u = self.ups(inv)[(GALE, "Studded Leather")]
        self.assertIn("proficient: LightArmor", u["why"])
        self.assertIn("proficient: Shields", self.ups()[(CLAUDE, "Sentinel Shield")]["why"])

    def test_worn_magic_gear_never_swapped_for_plainer_stats(self):
        inv = s026_inventory()
        inv["items"][1].update(rarity="uncommon", name="Armour of Devotion +Wis")  # magic, AC 14
        self.assertNotIn((CLAUDE, "Chain Mail"), self.ups(inv))  # AC 16 but plain
        self.assertIn((CLAUDE, "Chain Mail"), self.ups())  # vs plain Scale Mail it is an upgrade

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
        self.assertEqual(economy.short_rest_text(STATUS, STATUS, {"short_rests_left": 1.0, "of": 2.0}),
                         "HP unchanged · short rests left 1/2")  # live: the mod sends floats

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


def lantern_inventory() -> dict:
    """Live 2026-10-10 at camp: Claude (paladin) holds the Moonlantern + Sentinel Shield; a Ceremonial
    Warhammer and a two-handed Greatclub sit in bags; Gale (wizard) wears a plain Quarterstaff."""
    items = [
        {"uuid": _u(40), "name": "Moonlantern", "owner": CLAUDE, "equipped_slot": "MeleeMainHand", "story": True,
         "torch": True, "dmg": "1d4", "prof": "SimpleWeapons|Clubs"},
        {"uuid": _u(41), "name": "Sentinel Shield", "owner": CLAUDE, "equipped_slot": "MeleeOffHand", "ac": 2,
         "shield": True, "rarity": "rare"},
        {"uuid": _u(42), "name": "Greatclub +1", "owner": CLAUDE, "slot_type": "MeleeMainHand", "dmg": "1d8",
         "ench": 1, "twohanded": True, "prof": "SimpleWeapons|Greatclubs", "rarity": "uncommon"},
        {"uuid": _u(43), "name": "Ceremonial Warhammer", "owner": GALE, "slot_type": "MeleeMainHand", "dmg": "1d8",
         "prof": "MartialWeapons|Warhammers", "rarity": "uncommon"},
        {"uuid": _u(21), "name": "Quarterstaff", "owner": GALE, "equipped_slot": "MeleeMainHand", "dmg": "1d6",
         "prof": "SimpleWeapons|Quarterstaffs"},
    ]
    return {"items": items, "gold": 500, "supplies": 80,
            "members": {CLAUDE: {"prof": PALADIN}, GALE: {"prof": WIZARD}}}


class Lantern(unittest.TestCase):
    def test_caster_holds_the_light_fighter_gets_a_one_handed_weapon(self):
        inv = lantern_inventory()
        plan = economy.lantern_plan(inv)
        self.assertEqual((plan["frm"], plan["to"], plan["lantern"]), (CLAUDE, GALE, _u(40)))
        ups = economy.upgrades(economy.apply_lantern(inv, plan))
        main = [o for o in ups if o["who"] == CLAUDE and o["slot"] == "MeleeMainHand"]
        self.assertEqual(main[0]["name"], "Ceremonial Warhammer")  # not the two-handed Greatclub: shield kept

    def test_the_casters_freed_staff_is_enough(self):
        inv = lantern_inventory()
        inv["items"] = [it for it in inv["items"] if it["uuid"] not in (_u(42), _u(43))]
        plan = economy.lantern_plan(inv)
        self.assertIsNotNone(plan)
        ups = economy.upgrades(economy.apply_lantern(inv, plan))
        self.assertEqual([o["name"] for o in ups if o["who"] == CLAUDE], ["Quarterstaff"])

    def test_no_plan_when_a_caster_holds_it_or_no_caster(self):
        inv = lantern_inventory()
        inv["items"][0]["owner"] = GALE
        inv["items"] = [it for it in inv["items"] if it["uuid"] != _u(21)]
        self.assertIsNone(economy.lantern_plan(inv))
        inv = lantern_inventory()
        inv["members"] = {CLAUDE: {"prof": PALADIN}, GALE: {"prof": PALADIN}}
        self.assertIsNone(economy.lantern_plan(inv))

    def test_light_note_once_per_fight(self):
        server._light_noted.clear()
        cs = {"active": True, "order": [{"uuid": CLAUDE, "light_in_hand": "Moonlantern"}, {"uuid": GALE}]}
        self.assertIn("fights with the Moonlantern", server._light_note(cs))
        self.assertEqual(server._light_note(cs), "")
        server._light_note({"active": False})
        self.assertIn("LIGHT:", server._light_note(cs))

    def test_alert_names_the_move(self):
        out = economy.alerts({"in_combat": False}, lantern_inventory(), name=lambda u: {CLAUDE: "Claude", GALE: "Gale"}[u])
        self.assertTrue(any("Claude fights with the Moonlantern" in a and "Gale (caster)" in a for a in out), out)


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

    def test_sell_junk_drops_the_sale_s_own_item_events(self):
        """Acceptance 12:21: every sold item of another member came back as 'item_added … owner=Claude' (the game
        moves it to the trader-facing character first) plus 'Gold count=799'; other item events stay."""
        trade = {"trader_gold": 40, "your_items": [{"uuid": _u(6), "price": 30}, {"uuid": _u(4), "price": 1}]}
        g = self.use(FakeGame(s026_inventory(), trade))
        claude = {"uuid": _u(1), "name": "Claude"}
        evs = [{"kind": "item_added", "data": {"owner": claude, "item": {"uuid": _u(6), "name": "Crude Mace"}}},
               {"kind": "item_added", "data": {"owner": claude, "item": {"uuid": _u(90), "name": "Gold"}, "count": 799}},
               {"kind": "item_added", "data": {"owner": claude, "item": {"uuid": _u(91), "name": "Strange Key"}}}]
        g.drain_events = lambda ctx: evs if ctx == "server" else []
        out = server.trade(sell="junk")
        self.assertNotIn("Crude Mace", out)
        self.assertNotIn("count=799", out)
        self.assertIn("Strange Key", out)
        # late events of the sale are still dropped in the next call, then the window closes
        self.assertEqual(server._drop_events([evs[0]]), [])
        server._EV_DROP["until"] = time.monotonic() - 1
        self.assertEqual(server._drop_events([evs[0]]), [evs[0]])
        self.assertEqual(server._EV_DROP["items"], set())

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

    def test_equip_best_hands_the_lantern_to_the_caster_then_arms_the_fighter(self):
        class G(FakeGame):
            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                r = super().run_job(op, args, context, wait_s, **kw)
                if op == "act.equip" and args["item"] == _u(40):  # the game's state after the three moves
                    self.inv = economy.apply_lantern(self.inv, {"lantern": _u(40), "frm": CLAUDE, "to": GALE})
                return r
        g = self.use(G(lantern_inventory()))
        out = server.equip_best()
        ops = [(op, a.get("item"), a.get("char") or a.get("to")) for op, a in g.jobs]
        self.assertEqual(ops[:3], [("act.unequip", _u(40), CLAUDE), ("act.transfer", _u(40), GALE),
                                   ("act.equip", _u(40), GALE)])
        self.assertIn(("act.equip", _u(43), CLAUDE), ops[3:])
        self.assertIn("Moonlantern (light for the party", out)
        self.assertIn("Ceremonial Warhammer", out)

    def test_equip_best_lantern_give_fails_reequips_the_holder(self):
        g = self.use(FakeGame(lantern_inventory()))
        g.fail = set()
        orig = g.run_job

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            if op == "act.transfer" and args["item"] == _u(40) and args["to"] == GALE:
                g.jobs.append((op, args))
                return JobResult(status="failed", error="refused")
            return orig(op, args, context, wait_s, **kw)
        g.run_job = run_job
        out = server.equip_best()
        self.assertIn(("act.equip", {"char": CLAUDE, "item": _u(40)}), g.jobs)
        self.assertIn("transfer failed", out)

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


# S023 Lae'zel's shop (trimmed): one 4.7 KB line per call before.
TRADE_S023 = {
    "mode": "Trade", "trader_gold": 621, "party_gold": 178, "price_modifier_pct": 143,
    "offer": {"you": {"gold": 0, "items": []}, "trader": {"gold": 0, "items": []}},
    "balance": {"you_give": 0, "you_get": 0},
    "wares": [
        {"uuid": _u(101), "name": "Rogue's Morsel", "count": 2, "price": 6, "supply": 1},
        {"uuid": _u(102), "name": "Supply Pack", "price": 56, "supply": 40},
        {"uuid": _u(103), "name": "Leather Armour", "price": 56, "kind": "Armor"},
        {"uuid": _u(104), "name": "Chain Mail +1", "price": 364, "rarity": "Uncommon", "kind": "Armor"},
        {"uuid": _u(105), "name": "Gloves of Missile Snaring", "price": 224, "rarity": "Rare"},
        {"uuid": _u(106), "name": "Potion of Healing", "count": 5, "price": 70},
        {"uuid": _u(107), "name": "Scroll of Revivify", "count": 3, "price": 420, "rarity": "Uncommon"},
        {"uuid": _u(108), "name": "Sea Green Dye", "count": 7, "price": 28, "rarity": "Uncommon"},
    ] + [{"uuid": _u(200 + i), "name": f"Arrow of Fire {i}", "price": 14} for i in range(12)],
    "your_items": [{"uuid": _u(300 + i), "name": f"Bottle {i}", "price": i} for i in range(20)],
    "your_items_more": True,
}


class TradeText(unittest.TestCase):
    def h(self, it):
        return "e" + it["uuid"][-3:].lstrip("0")

    def test_listing_groups_sorts_and_caps(self):
        out = economy.trade_text(TRADE_S023, self.h)
        self.assertLess(len(out), 2200, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("TRADE your gold 178 · trader gold 621"), lines[0])
        food = next(l for l in lines if "food (supply" in l)
        self.assertLess(food.index("Supply Pack"), food.index("Rogue's Morsel"))  # most supplies first
        self.assertIn("42 supplies for 62g", food)
        gear = next(l for l in lines if "gear (best rarity" in l)
        self.assertLess(gear.index("[Rare] e105 Gloves"), gear.index("[Uncommon] e104 Chain Mail +1"))
        self.assertLess(gear.index("Chain Mail +1"), gear.index("Leather Armour"))
        cons = next(l for l in lines if "potions/scrolls" in l)
        self.assertTrue(cons.split(": ", 1)[1].startswith("e107 Scroll of Revivify x3 420g"), cons)
        self.assertIn("more (", cons)  # 14 consumables capped at 10
        self.assertIn("Sea Green Dye", next(l for l in lines if l.strip().startswith("other")))
        yours = next(l for l in lines if l.startswith("YOUR ITEMS"))
        self.assertTrue(yours.startswith("YOUR ITEMS 20+ sellable"), yours)
        self.assertIn("e319 Bottle 19 19g", yours)
        self.assertIn("+8 more", yours)
        self.assertIn("trade(full=true)", out)
        self.assertNotIn("TABLE", out)  # nothing on the table yet

    def test_full_lists_everything(self):
        out = economy.trade_text(TRADE_S023, self.h, full=True)
        self.assertIn("Arrow of Fire 11", out)
        self.assertNotIn("more (", out)

    def test_offer_shows_table_and_balance_only(self):
        res = dict(TRADE_S023, offer={"you": {"gold": 0, "items": [{"uuid": _u(301), "name": "Bottle 1", "price": 1}]},
                                       "trader": {"gold": 0, "items": [{"uuid": _u(102), "name": "Supply Pack", "price": 56}]}},
                   balance={"you_give": 1, "you_get": 56}, feedback="That's hardly a fair trade.")
        out = economy.trade_text(res, self.h, listing=False)
        self.assertIn("TABLE you give: e301 Bottle 1 1g (=1g) | you get: e102 Supply Pack 56g (=56g) | short by 55g", out)
        self.assertIn("TRADER says: That's hardly a fair trade.", out)
        self.assertNotIn("WARES", out)
        self.assertLess(len(out), 400)

    def test_tool_renders_through_trade_text(self):
        class G(FakeGame):
            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                self.jobs.append((op, args))
                return JobResult(status="done", result=TRADE_S023)
        saved = (server.H, server.game)
        server.H, server.game = Handles(), G(s026_inventory())
        self.addCleanup(lambda: (setattr(server, "H", saved[0]), setattr(server, "game", saved[1])))
        out = server.trade()
        self.assertTrue(out.startswith("trade: done\nTRADE "), out[:80])
        self.assertIn("WARES 20 items", out)
        out = server.trade(request="e1", confirm=True)
        self.assertNotIn("WARES", out)
        self.assertIn("wares/your items unchanged", out)
        out = server.trade(full=True)
        self.assertEqual(server.game.jobs[-1][1].get("max"), 500)


if __name__ == "__main__":
    unittest.main()


class Pickpocket(unittest.TestCase):
    """pickpocket lists the pockets with the game's shown odds (Services.lua ui.pickpocket) and takes one."""

    POCKETS = {"items": [{"name": "Gold", "count": 12, "chance": 81, "dc": 8},
                         {"uuid": _u(31), "name": "Dagger", "price": 2, "chance": 54, "dc": 12},
                         {"uuid": _u(32), "name": "Ring", "price": 90, "chance": 20, "dc": 18}]}

    def setUp(self):
        saved = (server.H, server.game, server._click_act)
        server.H = Handles()
        self.addCleanup(lambda: (setattr(server, "H", saved[0]), setattr(server, "game", saved[1]),
                                 setattr(server, "_click_act", saved[2])))
        self.jobs: list = []
        self.window = False
        test = self

        class G:
            def run_job(self, op, args, context="server", wait_s=25.0):
                test.jobs.append((op, dict(args)))
                if op == "act.select":
                    return JobResult("done", {})
                if not test.window:
                    return JobResult("failed", error="no pickpocket window is open: pickpocket(npc) opens it")
                if args.get("take"):
                    return JobResult("done", {"item": test.POCKETS["items"][1], "success": True, "rolled": 15,
                                              "dc": 12, "chance": 54})
                return JobResult("done", test.POCKETS)

            def try_rpc(self, op, *a, **k):
                return self.rpc(op) if op == "state.status" else None

            def drain_events(self, ctx):
                return []

            def rpc(self, op, *a, **k):
                return {"active": _u(1), "party": [{"uuid": _u(1), "name": "Claude"}, {"uuid": _u(2), "name": "Shadowheart"}]}

        server.game = G()

        def click_act(tool, op, args, wait_s=40.0):
            test.window = True
            server._batch["last"] = "done"
            return "pickpocket: done — opened pickpocket"
        server._click_act = click_act
        self.npc = server.H.get(_u(30), "Refugee")

    def test_text_sorted_by_odds(self):
        t = economy.pockets_text(self.POCKETS, lambda it: "")
        self.assertTrue(t.startswith("POCKETS"))
        self.assertLess(t.index("Gold x12 81% DC8"), t.index("Dagger 2g 54% DC12"))
        self.assertLess(t.index("Dagger"), t.index("Ring 90g 20% DC18"))

    def test_unread_odds_are_not_zero(self):
        t = economy.pockets_text({"items": [{"name": "Club", "chance": 0, "dc": 0}, {"name": "Bow", "chance": 45, "dc": 12},
                                            {"name": "Mail", "chance": 0, "dc": 26}]}, lambda it: "")
        self.assertIn("Club odds ?", t)
        self.assertIn("Mail 0% DC26", t)
        self.assertLess(t.index("Bow 45%"), t.index("Club odds ?"))

    def test_opens_then_lists(self):
        out = server.pickpocket(self.npc)
        self.assertIn("opened pickpocket", out)
        self.assertIn("Ring 90g 20% DC18", out)
        self.assertEqual([op for op, _ in self.jobs], ["ui.pickpocket", "ui.pickpocket"])
        self.assertEqual(self.jobs[0][1]["npc"], _u(30))

    def test_other_member_takes_control_first(self):
        """Live 12:3x: who=Shadowheart while Claude was controlled: the window never showed (client UI follows the
        controlled character)."""
        server.H.get(_u(2), "Shadowheart")
        out = server.pickpocket(self.npc, who="Shadowheart")
        self.assertEqual([op for op, _ in self.jobs], ["ui.pickpocket", "act.select", "ui.pickpocket"])
        self.assertEqual(self.jobs[1][1], {"char": _u(2)})
        self.assertIn("took control of Shadowheart", out)
        self.jobs.clear()
        self.window = False
        server.pickpocket(self.npc, who="Claude")                  # already controlled: no select
        self.assertNotIn("act.select", [op for op, _ in self.jobs])

    def test_take_by_handle_in_one_call(self):
        self.window = True
        h = server.H.get(_u(31), "Dagger")
        out = server.pickpocket(self.npc, take=h, min_chance=40)
        self.assertIn("stole", out)
        self.assertIn("rolled 15 vs DC 12", out)
        take = self.jobs[-1][1]
        self.assertEqual((take["take"], take["min_chance"]), (_u(31), 40))

    def test_window_that_never_opens(self):
        # live 12:3x: pickpocket on an Ox: the open step said done but no window came; the 2nd read raised
        test = self

        def click_act(tool, op, args, wait_s=40.0):
            server._batch["last"] = "done"
            return "pickpocket: done — opened=pickpocket via=engine"
        server._click_act = click_act

        class Raising:
            def run_job(self, op, args, context="server", wait_s=25.0):
                test.jobs.append((op, dict(args)))
                raise server.GameError("no pickpocket window is open: pickpocket(npc) opens it")

            def drain_events(self, ctx):
                return []

            def try_rpc(self, *a, **k):
                return None

            def rpc(self, op, *a, **k):
                return {"active": _u(1), "party": [{"uuid": _u(1), "name": "Claude"}]}
        server.game = Raising()
        out = server.pickpocket(self.npc)
        self.assertIn("no pickpocket window", out)
        self.assertGreaterEqual(len(self.jobs), 2)   # read, open, then polls ~3 s for the widget

    def test_failed_steal_says_crime(self):
        t = economy.steal_text({"item": {"name": "Ring"}, "success": False, "rolled": 3, "dc": 18, "chance": 20}, lambda it: "")
        self.assertIn("CAUGHT?", t)
        self.assertIn("rolled 3 vs DC 18", t)


class TradeOpen(unittest.TestCase):
    """S020/S025/S026: trade(npc) FAILED 'no trade screen is open' every time; the agent then talked and picked
    'Let me see your wares.' by hand. trade(npc) now does that itself."""

    DAMMON = [{"i": 1, "text": "Would you be able to take a look at Karlach's heart?"},
              {"i": 2, "text": "Let me see your wares."}, {"i": 3, "text": "Leave."}]

    def test_trade_answer(self):
        self.assertEqual(economy.trade_answer(self.DAMMON)["i"], 2)
        self.assertEqual(economy.trade_answer([{"i": 1, "text": "What do you have for sale?"}])["i"], 1)
        self.assertEqual(economy.trade_answer([{"i": 4, "text": "[TRADE] Show me."}])["i"], 4)
        self.assertIsNone(economy.trade_answer([{"i": 1, "text": "Who are you?"}, {"i": 2, "text": "Leave."}]))
        self.assertIsNone(economy.trade_answer([{"i": 1, "text": "Let me see your wares.", "off": True}]))

    def run_trade(self, ui_seq: list, dialogue_opts=None, opens=True):
        calls = []
        state = {"ui": ui_seq[0]}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return state["ui"] if op == "ui.state" else default

            def run_job(self, op, args=None, context="server", wait_s=0, **kw):
                calls.append((op, args))
                if op == "ui.dialogue_choose" and opens:
                    state["ui"] = {"screen": "trade"}
                return JobResult(status="done", result={})

        def fake_talk(npc, who=None, wait=True):
            calls.append(("talk_to", npc))
            state["ui"] = {"screen": "dialogue", "dialogue": {"options": dialogue_opts or []}}
            return "talk_to: done — dialogue"

        saved = (server.game, server.talk_to, server._act, server.H)
        server.H = Handles()
        server.H.get("11111111-2222-3333-4444-5555555555aa", "Dammon")
        server.game, server.talk_to = G(), fake_talk
        server._act = lambda tool, op, args, **kw: calls.append((op, args)) or "trade: done — wares=[…]"
        try:
            return server.trade(npc="e1"), calls
        finally:
            server.game, server.talk_to, server._act, server.H = saved

    def test_trade_npc_talks_and_picks_the_wares_answer(self):
        out, calls = self.run_trade([{"screen": "game"}], self.DAMMON)
        self.assertEqual([c[0] for c in calls], ["talk_to", "ui.dialogue_choose", "ui.trade"])
        self.assertEqual(calls[1][1], {"i": 2})
        self.assertIn("picked 'Let me see your wares.'", out)
        self.assertIn("trade: done", out)

    def test_trade_npc_already_open_lists_at_once(self):
        out, calls = self.run_trade([{"screen": "trade"}])
        self.assertEqual([c[0] for c in calls], ["ui.trade"])
        self.assertTrue(out.startswith("trade: done"), out)

    def test_trade_npc_without_a_trade_answer_stops_at_the_conversation(self):
        out, calls = self.run_trade([{"screen": "game"}], [{"i": 1, "text": "Who are you?"}, {"i": 2, "text": "Leave."}])
        self.assertEqual([c[0] for c in calls], ["talk_to"])
        self.assertIn("no trade answer", out)
        self.assertIn("1. Who are you?", out)

    def test_trade_npc_answer_that_opens_nothing_fails_clearly(self):
        out, calls = self.run_trade([{"screen": "game"}], self.DAMMON, opens=False)
        self.assertIn("no trade screen opened", out)
        self.assertNotIn("ui.trade", [c[0] for c in calls])


class MainHands(unittest.TestCase):
    """Acceptance 12:2x: equip_best 'left Shadowheart with a Syringe': it is MAG_Surgeon_Syringe, a +1 finesse dagger
    (1d4+1, +1 to hit) — her best one-hander beside her shield (Mace/Handaxe 1d6 plain; Greatclub +1 is 2-handed)."""
    SH, CL = _u(3), _u(4)

    def inv(self):
        return {"members": {self.SH: {"prof": ["SimpleWeapons", "Shields", "MediumArmor", "LightArmor"]},
                            self.CL: {"prof": ["MartialWeapons", "SimpleWeapons"]}},
                "items": [
                    {"uuid": _u(10), "name": "Syringe", "owner": self.SH, "equipped_slot": "MeleeMainHand",
                     "slot_type": "MeleeMainHand", "dmg": "1d4", "ench": 1, "rarity": "uncommon", "prof": "Daggers|SimpleWeapons"},
                    {"uuid": _u(11), "name": "Shield", "owner": self.SH, "equipped_slot": "MeleeOffHand",
                     "slot_type": "MeleeOffHand", "ac": 2, "shield": True},
                    {"uuid": _u(12), "name": "Mace", "owner": self.SH, "slot_type": "MeleeMainHand", "dmg": "1d6",
                     "prof": "Maces|SimpleWeapons"},
                    {"uuid": _u(13), "name": "Greatclub +1", "owner": self.CL, "slot_type": "MeleeMainHand", "dmg": "1d8",
                     "ench": 1, "rarity": "uncommon", "twohanded": True, "prof": "SimpleWeapons|Greatclubs"},
                    {"uuid": _u(14), "name": "Moonlantern", "owner": self.CL, "equipped_slot": "MeleeMainHand",
                     "slot_type": "MeleeMainHand", "dmg": "1d4", "story": True, "torch": True, "prof": "Clubs|SimpleWeapons"}]}

    def test_syringe_stays_and_reads_as_a_weapon(self):
        inv = self.inv()
        self.assertFalse([o for o in economy.upgrades(inv) if o["who"] == self.SH and o["slot"] == "MeleeMainHand"])
        line = economy.main_hands(inv, {self.SH: "Shadowheart", self.CL: "Claude"}.get)
        self.assertEqual(line, "MAIN HANDS Shadowheart Syringe (+1 dagger 1d4+1 uncommon) | Claude Moonlantern (light)")


class WeaponRanking(unittest.TestCase):
    """Task (acceptance): rank weapons by proficiency, damage, magic and the wielder's STR/DEX; never a torch/tool."""
    A = _u(5)

    def inv(self, member: dict, items: list[dict]) -> dict:
        return {"members": {self.A: member}, "items": items}

    def mh(self, n: int, name: str, dmg: str, prof: str, worn=False, **kw) -> dict:
        it = {"uuid": _u(n), "name": name, "owner": self.A, "slot_type": "MeleeMainHand", "dmg": dmg, "prof": prof, **kw}
        if worn:
            it["equipped_slot"] = "MeleeMainHand"
        return it

    def test_torch_never_a_weapon(self):
        inv = self.inv({"prof": ["SimpleWeapons"], "str": 14},
                       [self.mh(10, "Torch", "1d4", "Clubs|SimpleWeapons", torch=True)])
        self.assertEqual(economy.upgrades(inv), [])

    def test_real_weapon_replaces_a_held_torch(self):
        inv = self.inv({"prof": ["SimpleWeapons", "MediumArmor"], "str": 10},
                       [self.mh(10, "Torch", "1d4", "Clubs|SimpleWeapons", worn=True, torch=True),
                        self.mh(11, "Club", "1d4", "Clubs|SimpleWeapons")])
        self.assertEqual([o["name"] for o in economy.upgrades(inv)], ["Club"])

    def test_no_group_is_not_a_weapon(self):
        inv = self.inv({"prof": []}, [self.mh(10, "Frying Pan", "1d4", "")])
        self.assertEqual(economy.upgrades(inv), [])

    def test_dex_fighter_prefers_finesse(self):
        # STR 8 DEX 18: Rapier 1d8 finesse (4.5+4) beats Longsword 1d8 (4.5-1); STR build the other way round.
        items = [self.mh(10, "Longsword", "1d8", "Longswords|MartialWeapons", worn=True),
                 self.mh(11, "Rapier", "1d8", "Rapiers|MartialWeapons", finesse=True)]
        dex = self.inv({"prof": ["MartialWeapons"], "str": 8, "dex": 18}, items)
        self.assertEqual([o["name"] for o in economy.upgrades(dex)], ["Rapier"])
        items[0], items[1] = dict(items[0]), dict(items[1])
        items[0].pop("equipped_slot"); items[1]["equipped_slot"] = "MeleeMainHand"
        st = self.inv({"prof": ["MartialWeapons"], "str": 18, "dex": 8}, items)
        self.assertEqual(economy.upgrades(st), [])  # equal: no churn

    def test_plus_one_dagger_beats_plain_mace_at_equal_str_dex(self):
        inv = self.inv({"prof": ["SimpleWeapons", "MediumArmor"], "str": 13, "dex": 13},
                       [self.mh(10, "Mace", "1d6", "Maces|SimpleWeapons", worn=True),
                        self.mh(11, "Syringe", "1d4", "Daggers|SimpleWeapons", ench=1, rarity="uncommon", finesse=True)])
        self.assertEqual([o["name"] for o in economy.upgrades(inv)], ["Syringe"])

    def test_unproficient_martial_skipped(self):
        inv = self.inv({"prof": ["SimpleWeapons"], "str": 16},
                       [self.mh(10, "Greatsword", "2d6", "Greatswords|MartialWeapons", twohanded=True)])
        self.assertEqual(economy.upgrades(inv), [])
