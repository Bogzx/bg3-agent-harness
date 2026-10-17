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
from unittest import mock

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import economy, render, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402
from bg3ai.hotbar import HotbarCastResult
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

    def test_temporary_follower_is_neither_dressed_nor_stripped(self):
        # S053: equip_best put Ketheric's Shield on Zanner Toobin (a quest follower); moved back by hand
        inv = s026_inventory()
        base = economy.upgrades(inv)
        self.assertTrue(base)
        for u in list(inv["members"]):
            inv["members"][u] = dict(inv["members"][u], follower=True)
        self.assertEqual(economy.upgrades(inv), [])
        inv = s026_inventory()
        who = base[0]["who"]
        inv["members"][who] = dict(inv["members"][who], follower=True)
        ups = economy.upgrades(inv)
        self.assertNotIn(who, {o["who"] for o in ups})
        self.assertNotIn(who, {o["holder"] for o in ups})

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

    def test_deaths_come_first_with_revive_options(self):
        # S043 Jaheira, S045 Aylin (allies) died with Scrolls of Revivify in the bags; nothing said so afterwards.
        inv = s026_inventory()
        inv["items"].append({"uuid": "u-rev", "name": "Scroll of Revivify", "count": 5, "owner": CLAUDE})
        st = {"in_combat": False, "party": [dict(STATUS["party"][0]), dict(STATUS["party"][1], dead=True, hp=0)]}
        al = economy.alerts(st, inv, dead_allies=[{"uuid": "u-j", "name": "Jaheira"}], name=lambda u: "e9 Jaheira")
        self.assertTrue(al[0].startswith("DEAD Gale (revive: 5 Scroll(s) of Revivify in the bags on the body"), al[0])
        self.assertIn("Withers at camp", al[0])
        self.assertTrue(al[1].startswith("ally DIED e9 Jaheira (revive: 5 Scroll(s) of Revivify"), al[1])
        self.assertIn("Withers can't raise an ally", al[1])
        self.assertNotIn("DEAD", " | ".join(economy.alerts(STATUS, inv)))

    def test_dead_allies_reach_the_alerts_line(self):
        import time as _t
        tr = server._fights()
        saved = list(tr.dead_allies)
        self.addCleanup(setattr, tr, "dead_allies", saved)
        tr.dead_allies = [{"uuid": "u-j", "name": "Jaheira", "t": _t.time()},
                          {"uuid": "u-old", "name": "Old", "t": _t.time() - server.DEAD_ALLY_ALERT_S - 1}]
        self.assertEqual([a["name"] for a in server._dead_allies()], ["Jaheira"])

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

    def test_name_filter_alternatives_and_no_match_line(self):
        # S039 21:43: filter "leap,misty,jump,dimension,feather" was one word and matched nothing, silently
        out = render.render_inventory(s026_inventory(), self.H, "misty,potion healing,cup")
        self.assertIn("Potion of Healing", out)
        self.assertIn("Cup", out)
        self.assertNotIn("No item", out)
        out = render.render_inventory(s026_inventory(), self.H, "leap,misty,jump")
        self.assertIn("No item in the party's bags matches 'leap,misty,jump'", out)
        out = render.render_inventory(s026_inventory(), self.H, "food")
        self.assertNotIn("No item", out)

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

    def test_sell_junk_in_barters_of_25(self):
        # acceptance 01:07: 72 junk items in one barter → the job's 15 s timeout, nothing sold
        inv = s026_inventory()
        mine = [it for it in server.economy.junk(inv, set())]
        trade = {"trader_gold": 10 ** 6, "your_items": [{"uuid": it["uuid"], "price": 1} for it in mine]}
        g = self.use(FakeGame(inv, trade))
        saved = server.SELL_CHUNK
        server.SELL_CHUNK = 2
        self.addCleanup(setattr, server, "SELL_CHUNK", saved)
        out = server.trade(sell="junk")
        offers = [a["offer"] for op, a in g.jobs if op == "ui.trade"]
        self.assertEqual(sum(len(o) for o in offers), len(mine))
        self.assertTrue(all(len(o) <= 2 for o in offers))
        self.assertEqual(len(offers), (len(mine) + 1) // 2)
        self.assertIn(f"sold {len(mine)} junk items", out)

    def test_sell_junk_stops_after_a_failed_barter(self):
        inv = s026_inventory()
        mine = server.economy.junk(inv, set())
        trade = {"trader_gold": 10 ** 6, "your_items": [{"uuid": it["uuid"], "price": 1} for it in mine]}
        g = self.use(FakeGame(inv, trade))
        saved = (server.SELL_CHUNK, server._act)
        server.SELL_CHUNK = 2
        n = []

        def fake_act(tool, op, args, **kw):
            n.append(args["offer"])
            return "trade: done — confirmed" if len(n) == 1 else "trade: TIMEOUT — timed out"
        server._act = fake_act
        self.addCleanup(lambda: (setattr(server, "SELL_CHUNK", saved[0]), setattr(server, "_act", saved[1])))
        out = server.trade(sell="junk")
        self.assertEqual(len(n), 2)
        self.assertIn("trade: TIMEOUT", out)
        self.assertIn(f"sold 2 of {len(mine)} junk items in 1 barter(s) before this one failed", out)

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

    def test_equip_best_waits_out_a_fight(self):
        # S034 19:15: equip_best during an ambush → "give failed: … (use the UI)"; now nothing moves
        g = self.use(FakeGame(lantern_inventory()))
        saved = dict(STATUS)
        STATUS["in_combat"] = True
        try:
            out = server.equip_best()
        finally:
            STATUS.clear()
            STATUS.update(saved)
        self.assertIn("NOT NOW — in combat", out)
        self.assertEqual(g.jobs, [])

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

    def test_gold_on_the_table_is_its_stack_shown_once(self):
        # S055 03:45:46: "you give: (no id) ? x60 60g, 60 gold (=120g)": the Balance's gold (a Gold stack) printed
        # as an unnamed item plus the side's shown total as gold. Now the Lua counts the stack once (A13).
        res = dict(TRADE_S023, offer={"you": {"gold": 60, "items": [{"gold": True, "count": 60, "price": 60}]},
                                       "trader": {"gold": 0, "items": [{"uuid": _u(106), "name": "Potion of Healing",
                                                                        "count": 4, "price": 208}]}},
                   balance={"you_give": 60, "you_get": 208})
        out = economy.trade_text(res, self.h, listing=False)
        self.assertIn("TABLE you give: 60 gold (=60g) | you get: e106 Potion of Healing x4 208g (=208g) | short by 148g",
                      out)
        self.assertNotIn("(no id)", out)

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
                if op == "ui.selected":  # the HUD has Claude too (A16: the pickpocket select reads the client)
                    return {"uuid": _u(1)}
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


class ContestedWeapon(unittest.TestCase):
    def test_melee_member_beats_a_caster_for_a_magic_weapon(self):
        sh, gale = _u(3), _u(7)
        inv = {"members": {sh: {"prof": ["SimpleWeapons", "MediumArmor", "Shields"], "str": 13, "dex": 13},
                           gale: {"prof": ["Daggers", "Quarterstaffs", "LightArmor"], "str": 8, "dex": 13}},
               "items": [{"uuid": _u(10), "name": "Mace", "owner": sh, "equipped_slot": "MeleeMainHand",
                          "slot_type": "MeleeMainHand", "dmg": "1d6", "prof": "Maces|SimpleWeapons"},
                         {"uuid": _u(11), "name": "Quarterstaff", "owner": gale, "equipped_slot": "MeleeMainHand",
                          "slot_type": "MeleeMainHand", "dmg": "1d6", "prof": "Quarterstaffs|SimpleWeapons"},
                         {"uuid": _u(12), "name": "Syringe", "owner": gale, "slot_type": "MeleeMainHand", "dmg": "1d4",
                          "ench": 1, "rarity": "uncommon", "finesse": True, "prof": "Daggers|SimpleWeapons"}]}
        ups = economy.upgrades(inv)
        self.assertEqual([(o["who"], o["name"]) for o in ups], [(sh, "Syringe")])


class PactWeapon(unittest.TestCase):
    """S032 17:39: equip_best swapped Pact-of-the-Blade Wyll's rapier for a Greatclub +1. A bound weapon
    (PACT_BLADE / WEAPON_BOND: re-binding costs an action) or a summoned pact weapon stays in hand."""
    WYLL, KARLACH = _u(1), _u(4)

    def inv(self, bound=None, summoned=False):
        rapier = {"uuid": _u(10), "name": "Rapier", "owner": self.WYLL, "equipped_slot": "MeleeMainHand",
                  "slot_type": "MeleeMainHand", "dmg": "1d8", "finesse": True, "prof": "Rapiers|MartialWeapons"}
        if bound:
            rapier["bound"] = bound
        if summoned:
            rapier.update(summoned=True, name="Pact Rapier")
        return {"members": {self.WYLL: {"prof": ["SimpleWeapons", "LightArmor", "Rapiers"], "str": 10, "dex": 12,
                                        "cha": 18, "binds": "pact", "caster": True},
                            self.KARLACH: {"prof": ["SimpleWeapons", "MartialWeapons", "MediumArmor"], "str": 17,
                                           "dex": 12}},
                "items": [rapier,
                          {"uuid": _u(11), "name": "Greataxe", "owner": self.KARLACH, "equipped_slot": "MeleeMainHand",
                           "slot_type": "MeleeMainHand", "dmg": "1d12", "twohanded": True,
                           "prof": "Greataxes|MartialWeapons"},
                          {"uuid": _u(12), "name": "Greatclub +1", "owner": self.WYLL, "slot_type": "MeleeMainHand",
                           "dmg": "1d8", "ench": 1, "rarity": "uncommon", "twohanded": True,
                           "prof": "Greatclubs|SimpleWeapons"}]}

    def test_bound_pact_weapon_kept(self):
        self.assertEqual([o for o in economy.upgrades(self.inv(bound="pact")) if o["who"] == self.WYLL], [])

    def test_summoned_pact_weapon_kept(self):
        self.assertEqual([o for o in economy.upgrades(self.inv(bound="pact", summoned=True))
                          if o["who"] == self.WYLL], [])

    def test_unbound_swap_says_to_bind_and_scores_with_cha(self):
        ups = [o for o in economy.upgrades(self.inv()) if o["who"] == self.WYLL]
        self.assertEqual([o["name"] for o in ups], ["Greatclub +1"])
        self.assertIn("Bind Pact Weapon", ups[0]["why"])
        # CHA 18 counts (+4), not STR 10: same modifier both sides, gain = +1 enchantment ×2 + rarity
        self.assertEqual(economy._ability_mod(ups[0] | {"slot_type": "MeleeMainHand"}, self.inv()["members"][self.WYLL]), 4)

    def test_pact_warlock_is_not_a_spell_only_caster(self):
        # a plain (non-magic) better weapon is still offered: a caster would only take magic ones
        inv = self.inv()
        inv["items"][2].update(name="Longsword", ench=None, rarity=None, twohanded=False, dmg="1d10",
                               prof="Longswords|SimpleWeapons")
        self.assertIn("Longsword", [o["name"] for o in economy.upgrades(inv) if o["who"] == self.WYLL])

    def test_bound_weapon_never_handed_to_another_member(self):
        inv = self.inv()
        inv["items"].append({"uuid": _u(13), "name": "Bonded Longsword +1", "owner": self.WYLL,
                             "slot_type": "MeleeMainHand", "dmg": "1d8", "ench": 1, "rarity": "uncommon",
                             "bound": "weapon_bond", "prof": "Longswords|MartialWeapons"})
        inv["items"][-1].update(dmg="2d8")
        self.assertNotIn("Bonded Longsword +1", [o["name"] for o in economy.upgrades(inv) if o["who"] == self.KARLACH])
        inv["items"][-1].pop("bound")  # control: unbound, Karlach takes it
        self.assertIn("Bonded Longsword +1", [o["name"] for o in economy.upgrades(inv) if o["who"] == self.KARLACH])

    def test_main_hands_names_the_pact_weapon(self):
        line = economy.main_hands(self.inv(bound="pact"), lambda u: {self.WYLL: "Wyll"}.get(u, "Karlach"))
        self.assertIn("Wyll Rapier (rapier 1d8 pact weapon)", line)


class TradeHandover(unittest.TestCase):
    """S028 15:48: Wulbren's 'I have the tools you need - take them.' opened a trade screen with no wares and no
    gold; trade(offer, confirm) refused the donation, and the agent screenshotted, clicked Barter and confirmed the
    'Donate these items?' box by hand (5 calls). An equipped hammer failed 'some items couldn't be moved' silently."""

    MACE = "11111111-2222-3333-4444-5555555555ab"

    def test_handover_screen(self):
        self.assertTrue(economy.handover({"wares": [], "trader_gold": 0}))
        self.assertFalse(economy.handover({"wares": [{"uuid": "x", "price": 3}], "trader_gold": 0}))
        self.assertFalse(economy.handover({"wares": [], "trader_gold": 40}))
        self.assertFalse(economy.handover({"wares": [], "trader_gold": 0, "wares_more": True}))
        self.assertFalse(economy.handover(None))

    def test_problems_rendered(self):
        out = economy.trade_text({"problems": ["item Hammer isn't on this side of the trade screen"]},
                                 lambda it: "e9", listing=False)
        self.assertIn("NOT MOVED: item Hammer isn't on this side", out)

    def test_equipped_offer_named(self):
        inv = {"items": [{"uuid": "u1", "name": "Ceremonial Warhammer", "equipped_slot": "Melee Main Weapon",
                          "owner": "c1"}, {"uuid": "u2", "name": "Mace"}]}
        out = economy.equipped_offers(inv, ["u1", "u2"], lambda u: "Claude")
        self.assertEqual(len(out), 1)
        self.assertIn("Ceremonial Warhammer is equipped by Claude", out[0])

    def _trade(self, screen: dict, job_result: dict, box: dict | None, **kw):
        calls = []
        ui = {"screen": "trade"}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                calls.append((op, dict(args or {})))
                if op == "ui.trade":
                    return screen
                if op == "ui.confirm":
                    return box
                if op == "ui.state":
                    return ui
                return default

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            server._batch["result"] = job_result
            return "trade: done"

        def fake_box(tool, op, args, wait_s=8, rounds=3):
            calls.append(("box:" + op, dict(args)))
            ui["screen"] = "trade"
            return "confirm: done — pressed=Yes"

        def fake_esc():
            calls.append(("escape", {}))
            ui.update(screen="dialogue", dialogue={"speaker": "Wulbren Bongle", "options": []})
            return "press_key: done"

        saved = (server.game, server._act, server._box_act, server._press_escape, server._render_ui, server.H)
        server.H = Handles()
        server.H.get(self.MACE, "Crude Mace")
        server.game, server._act, server._box_act, server._press_escape = G(), fake_act, fake_box, fake_esc
        server._render_ui = lambda u, brief=False: [f"SCREEN {u.get('screen')}"]
        try:
            return server.trade(**kw), calls
        finally:
            (server.game, server._act, server._box_act, server._press_escape, server._render_ui, server.H) = saved

    def test_handover_donates_answers_the_box_and_closes(self):
        moved = [{"uuid": self.MACE, "name": "Crude Mace", "side": "you"}]
        out, calls = self._trade({"wares": [], "trader_gold": 0}, {"donate_box": True, "moved": moved},
                                 {"title": "Donate these items?", "buttons": ["Yes", "No"]},
                                 offer="e1", confirm=True)
        job = next(a for op, a in calls if op == "ui.trade" and a.get("confirm"))
        self.assertTrue(job.get("donate"))
        self.assertIn(("box:ui.confirm", {"answer": "Yes"}), calls)
        self.assertIn("escape", [c[0] for c in calls])
        self.assertIn("gave e1 Crude Mace (donated", out)
        self.assertIn("closed the hand-over screen", out)
        self.assertIn("SCREEN dialogue", out)

    def test_shop_with_wares_does_not_donate(self):
        out, calls = self._trade({"wares": [{"uuid": "w", "price": 5}], "trader_gold": 0},
                                 {"offer": {}, "wares": []}, None, offer="e1", confirm=True)
        job = next(a for op, a in calls if op == "ui.trade" and a.get("confirm"))
        self.assertNotIn("donate", job)
        self.assertNotIn("box:ui.confirm", [c[0] for c in calls])

    def test_box_that_is_not_a_donation_is_left_to_the_agent(self):
        out, calls = self._trade({"wares": [], "trader_gold": 0}, {"donate_box": True, "moved": []},
                                 {"title": "Steal these items?", "buttons": ["Yes", "No"]}, offer="e1", confirm=True)
        self.assertIn("STOPPED", out)
        self.assertNotIn("box:ui.confirm", [c[0] for c in calls])


class TradeConfirmCloses(unittest.TestCase):
    """S067 #125 / S068 #168: trade(request=…, confirm=true) left the window up and the agent pressed ESCAPE after
    it each time. A confirmed deal closes the window and checks the game view is back."""

    WARES = tuple(f"11111111-2222-3333-4444-5555555557{n:02d}" for n in range(4))  # Supply Pack, Pork, Chicken, Potion

    def _trade(self, job_result: dict, closes_after: int | None = 2, **kw):
        calls, ui, reads = [], {"screen": "trade"}, [0]

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                calls.append((op, dict(args or {})))
                if op == "ui.key":
                    ui.update(screen="dialogue", dialogue={"speaker": "Arron", "options": []})  # the goodbye line
                if op == "ui.state":
                    if ui.get("screen") == "dialogue" and closes_after is not None:
                        reads[0] += 1
                        if reads[0] > closes_after:
                            ui.clear()  # the line ended: no screen = the game view
                    return dict(ui)
                return default

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            server._batch["result"] = job_result
            return "trade: done"

        saved = (server.game, server._act, server._render_ui, server.H, server.TRADE_CLOSE_S)
        server.H = Handles()
        handles = ",".join(server.H.get(u, f"ware {i}") for i, u in enumerate(self.WARES))
        server.game, server._act = G(), fake_act
        server._render_ui = lambda u, brief=False: [f"SCREEN {u.get('screen')}"]
        server.TRADE_CLOSE_S = 0.5
        try:
            return server.trade(request=handles, **kw), calls
        finally:
            (server.game, server._act, server._render_ui, server.H, server.TRADE_CLOSE_S) = saved

    def test_s068_confirmed_deal_closes_the_window(self):
        out, calls = self._trade({"confirmed": True, "moved": []}, confirm=True)
        self.assertEqual([a for op, a in calls if op == "ui.key"], [{"key": "ESCAPE"}])
        self.assertIn("closed the trade window: back in the game", out)

    def test_no_confirm_keeps_it_open(self):
        out, calls = self._trade({"moved": []})
        self.assertNotIn("ui.key", [c[0] for c in calls])
        self.assertNotIn("closed the trade window", out)

    def test_deal_not_confirmed_keeps_it_open(self):
        _, calls = self._trade({"moved": [], "problems": ["x"]}, confirm=True)
        self.assertNotIn("ui.key", [c[0] for c in calls])

    def test_window_still_up_is_said(self):
        out, _ = self._trade({"confirmed": True, "moved": []}, closes_after=None, confirm=True)
        self.assertIn("(closing the trade window: screen=dialogue after ESCAPE)", out)
        self.assertIn("SCREEN dialogue", out)


_HALF = {"unmatched": 1, "wares": [{"price": 1040}, {"price": 832}]}
_NAMED = {"wares": [{"uuid": "11111111-2222-3333-4444-555555555701", "name": "Supply Pack", "price": 156,
                     "supply": 40}]}


class TradeWaresFill(unittest.TestCase):
    """L7 22:08: trade() 0.2 s after the screen opened listed 24 wares "(no id) ?" (the trader's side half filled);
    the listing is read again until the wares are named."""

    def _listing(self, results: list[dict], **kw):
        calls = []

        def fake_act(tool, op, args, **k):
            calls.append((op, dict(args)))
            server._batch["result"] = results[min(len(calls), len(results)) - 1]
            return "trade: done" + (" (no id)" if server._wares_unnamed(server._batch["result"]) else " named")

        saved = (server._act, server.TRADE_FILL_S)
        server._act, server.TRADE_FILL_S = fake_act, 0.0
        try:
            return server.trade(**kw), calls
        finally:
            server._act, server.TRADE_FILL_S = saved


    def test_half_filled_wares_are_read_again(self):
        out, calls = self._listing([_HALF, _HALF, _NAMED])
        self.assertEqual(len(calls), 3)
        self.assertIn("named", out)

    def test_named_listing_is_read_once(self):
        _, calls = self._listing([_NAMED])
        self.assertEqual(len(calls), 1)

    def test_never_named_stops_after_the_tries(self):
        out, calls = self._listing([_HALF])
        self.assertEqual(len(calls), 1 + server.TRADE_FILL_TRIES)
        self.assertIn("(no id)", out)

    def test_a_request_is_not_repeated(self):
        saved = server.H
        server.H = Handles()
        try:
            h = server.H.get("11111111-2222-3333-4444-555555555701", "Supply Pack")
            _, calls = self._listing([_HALF], request=h)
        finally:
            server.H = saved
        self.assertEqual(len(calls), 1)


class TradeSellOpens(unittest.TestCase):
    def test_npc_with_sell_junk_opens_first(self):
        calls = []
        saved = (server._open_trade, server._sell_junk)
        server._open_trade = lambda npc: calls.append(("open", npc)) or "(opened: talked to e5, picked 'Let's see your wares.')"
        server._sell_junk = lambda keep="": calls.append(("sell", keep)) or "trade: done"
        try:
            out = server.trade(npc="e5", sell="junk")
        finally:
            server._open_trade, server._sell_junk = saved
        self.assertEqual([c[0] for c in calls], ["open", "sell"])
        self.assertIn("(opened: talked to e5", out)
        self.assertIn("trade: done", out)


class GiveEquipped(unittest.TestCase):
    """S028 15:51: give of a worn amulet → 'unequip it first' → a 3-step batch. give unequips it itself now."""

    def test_give_worn_item_unequips_then_gives(self):
        amulet, karlach, gale = "11111111-2222-3333-4444-5555555555c1", "11111111-2222-3333-4444-5555555555c2", \
            "11111111-2222-3333-4444-5555555555c3"
        calls = []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                if op == "state.inventory":
                    return {"items": [{"uuid": amulet, "name": "Spellcrux Amulet", "owner": karlach,
                                       "equipped_slot": "Amulet"}]}
                return default

            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                calls.append((op, dict(args)))
                return JobResult(status="done", result={"unequipped": True})

        results = iter(["manage_inventory: FAILED — unequip it first", "manage_inventory: done — amount=1 to=e3"])

        def fake_act(tool, op, args, **kw):
            calls.append((op, dict(args)))
            return next(results)

        saved = (server.game, server._act, server._item_uuid, server.H)
        server.H = Handles()
        server.H.get(amulet, "Spellcrux Amulet"), server.H.get(karlach, "Karlach"), server.H.get(gale, "Gale")
        server.game, server._act = G(), fake_act
        server._item_uuid = lambda item, who=None, worn=False: amulet
        try:
            out = server.manage_inventory("give", "e1", who="Karlach", to="Gale")
        finally:
            server.game, server._act, server._item_uuid, server.H = saved
        self.assertEqual([c[0] for c in calls], ["act.transfer", "act.unequip", "act.transfer"])
        self.assertEqual(calls[1][1], {"char": karlach, "item": amulet})
        self.assertIn("manage_inventory: done", out)
        self.assertIn("unequipped from", out)


class SpellGear(unittest.TestCase):
    """S028 15:51: equip_best put the Spellcrux Amulet (very rare, restores a spell slot) on Karlach (Barbarian,
    no spell slots) by rarity alone; the agent moved it to Gale by hand."""
    KARLACH = "11111111-0000-0000-0000-000000000003"
    BARB = ["LightArmor", "MediumArmor", "Shields", "SimpleWeapons", "MartialWeapons"]

    def inv(self, caster_flags=True):
        items = [{"uuid": _u(70), "name": "Spellcrux Amulet", "owner": self.KARLACH, "slot_type": "Amulet",
                  "rarity": "very_rare", "spellgear": True},
                 {"uuid": _u(71), "name": "Aberration Hunters' Amulet", "owner": self.KARLACH,
                  "equipped_slot": "Amulet", "rarity": "uncommon"}]
        members = {self.KARLACH: {"prof": self.BARB}, GALE: {"prof": WIZARD}}
        if caster_flags:
            members[self.KARLACH]["caster"], members[GALE]["caster"] = False, True
        return {"items": items, "members": members}

    def test_spell_gear_goes_to_a_caster(self):
        ups = {(u["who"], u["name"]) for u in economy.upgrades(self.inv())}
        self.assertIn((GALE, "Spellcrux Amulet"), ups)
        self.assertNotIn((self.KARLACH, "Spellcrux Amulet"), ups)

    def test_name_fallback_without_the_flags(self):
        inv = self.inv(caster_flags=False)
        del inv["items"][0]["spellgear"]
        ups = {(u["who"], u["name"]) for u in economy.upgrades(inv)}
        self.assertNotIn((self.KARLACH, "Spellcrux Amulet"), ups)
        self.assertIn((GALE, "Spellcrux Amulet"), ups)


class StageMs(unittest.TestCase):
    def test_stage_ms_logged_not_rendered(self):
        import json as _json
        r = JobResult(status="done", result={"set": {"feat": "x"}, "_stage_ms": "open=900 spells=4200"}, elapsed_ms=5100)
        server._log_stage_ms("ui.level_up", r)
        self.assertNotIn("_stage_ms", r.result)
        lines = (server.RUNTIME / "stage_ms.jsonl").read_text().strip().splitlines()
        self.assertEqual(_json.loads(lines[-1])["stages"], "open=900 spells=4200")


class TradeNoGold(unittest.TestCase):
    """S028 (vis review): a trader at 0 gold and sell=junk with no trade screen open."""

    def test_broke_trader_and_handover_said(self):
        h = lambda it: "e9"
        t = economy.trade_text({"trader_gold": 0, "wares": [{"uuid": "w", "name": "Rope", "price": 3}]}, h)
        self.assertIn("TRADER HAS NO GOLD", t)
        t = economy.trade_text({"trader_gold": 0, "wares": []}, h)
        self.assertIn("HAND-OVER", t)
        self.assertNotIn("NO GOLD", economy.trade_text({"trader_gold": 50, "wares": []}, h))

    def _sell(self, screen):
        calls = []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return {"screen": screen} if op == "ui.state" else default

        saved = (server.game, server._open_trade, server._sell_junk)
        server.game = G()
        server._open_trade = lambda npc: calls.append(("open", npc)) or "(opened: picked 'Let me see your wares.')"
        server._sell_junk = lambda keep="": calls.append(("sell", keep)) or "trade: done"
        try:
            return server.trade(sell="junk"), calls
        finally:
            server.game, server._open_trade, server._sell_junk = saved

    def test_sell_junk_without_a_screen_refuses_clearly(self):
        out, calls = self._sell("game")
        self.assertIn('trade(npc=<trader id>, sell="junk")', out)
        self.assertEqual(calls, [])

    def test_sell_junk_in_a_trader_conversation_opens_it(self):
        out, calls = self._sell("dialogue")
        self.assertEqual([c[0] for c in calls], ["open", "sell"])
        self.assertEqual(calls[0][1], "")

    def test_sell_junk_broke_trader_message(self):
        g = FakeGame(s026_inventory(), {"trader_gold": 0, "your_items": [{"uuid": _u(6), "price": 30}]})
        orig = server.game
        server.game = g
        try:
            out = server.trade(sell="junk")
        finally:
            server.game = orig
        self.assertIn("the trader has no gold left", out)


class ScrollUse(unittest.TestCase):
    """S030 16:34: use_ability(Shadowheart, "Scroll of Invisibility") → "no action called"; manage_inventory use
    → "done" with the game waiting for a target; screenshot + click_on by hand. The scroll is read and the target
    clicked in one call now."""
    SH, GALE, SCROLL = ("11111111-0000-0000-0000-0000000000a1", "11111111-0000-0000-0000-0000000000a2",
                        "11111111-0000-0000-0000-0000000000a3")

    def _run(self, who, ability, target="", owner=None, click_ok=True, name="Scroll of Invisibility",
             res="use_ability: done — used", screen=None, rpc=None, targets="", pos=None, ground=None):
        from bg3ai import clicker, twostep
        calls, clicks = [], []
        self.grounds = []
        scroll, sh = self.SCROLL, self.SH
        own = owner or sh
        self.rpcs = []
        rpcs = self.rpcs

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                rpcs.append((op, dict(args or {})))
                if op == "state.inventory":
                    return {"items": [{"uuid": scroll, "name": name, "owner": own}]}
                if op == "ui.state" and screen:
                    return {"screen": screen}
                if rpc is not None and op in rpc:
                    return rpc[op](args or {})
                return default

            def drain_events(self, ctx=None):
                return []

        def fake_act(tool, op, args, **kw):
            calls.append((op, dict(args)))
            server._batch["last"] = "done"
            return res + "\nEVENTS\n  used item={name=" + name + "}"

        def no_action(char, ability):
            raise server.GameError(f"{char} has no action called '{ability}' (get_actions lists them)")

        def fake_click(game, inp, uuid, button="L", **kw):
            clicks.append(uuid)
            return clicker.ClickResult(ok=click_ok, reason=None if click_ok else "hover_miss")

        def fake_ground(game, inp, caster, spell, p, should_stop=None):
            self.grounds.append((caster, spell, list(p)))
            return ground or HotbarCastResult(True, picked=1)

        saved = (server.game, server._act, server._ability_id, server.H, server._inp, clicker.click_entity,
                 server._who, twostep.ground_click)
        twostep.ground_click = fake_ground
        server.H = Handles()
        for u, n in ((sh, "Shadowheart"), (self.GALE, "Gale"), (scroll, "Scroll of Invisibility")):
            server.H.get(u, n)
        server.game, server._act, server._ability_id = G(), fake_act, no_action
        server._inp = lambda: object()
        clicker.click_entity = fake_click
        server._who = lambda w: {"Shadowheart": sh, "Gale": self.GALE}.get(w, w)
        try:
            x, y, z = pos or (None, None, None)
            out = server.use_ability(ability, target=server.H.get(target) if target else "", who=who,
                                     targets=targets, x=x, y=y, z=z)
        except server.GameError as e:
            out = "ERR " + str(e)
        finally:
            (server.game, server._act, server._ability_id, server.H, server._inp, clicker.click_entity,
             server._who, twostep.ground_click) = saved
        return out, calls, clicks

    def test_scroll_read_and_target_clicked(self):
        out, calls, clicks = self._run("Shadowheart", "Scroll of Invisibility", target=self.SH)
        self.assertEqual(calls, [("act.interact", {"char": self.SH, "target": self.SCROLL})])
        self.assertEqual(clicks, [self.SH])
        self.assertIn("Scroll of Invisibility read, cast on", out.split("\n")[0])

    def test_spell_name_alone_finds_the_scroll_and_defaults_to_self(self):
        out, calls, clicks = self._run("Shadowheart", "Invisibility")
        self.assertEqual(len(calls), 1)
        self.assertEqual(clicks, [self.SH])

    def test_scroll_in_another_members_bags_is_named(self):
        out, calls, clicks = self._run("Gale", "Invisibility", owner=self.SH)
        self.assertTrue(out.startswith("ERR") or "FAILED" in out)
        self.assertIn("carries Scroll of Invisibility", out)
        self.assertIn('who="Shadowheart"', out)
        self.assertEqual(calls, [])

    def test_a_book_opens_for_reading_and_nothing_is_clicked(self):
        # S031 17:12: use_ability(book, target=pedestal) → the Book page opened, the click turned the camera 6x.
        out, calls, clicks = self._run("Shadowheart", "Teachings of Loss: The Nightsinger", target=self.GALE,
                                       name="Teachings of Loss: The Nightsinger", screen="book")
        self.assertEqual(clicks, [])
        self.assertIn("opened for reading", out.split("\n")[0])
        self.assertIn("insert_item", out.split("\n")[0])

    def test_failed_click_says_what_to_do(self):
        out, _, _ = self._run("Shadowheart", "Scroll of Invisibility", click_ok=False)
        self.assertIn("press_key ESCAPE", out)

    def test_potion_in_combat_is_drunk_without_a_click(self):
        # S031 17:05: bonus "Potion of Healing" in combat_turn was refused at 7 HP; the mod now drinks it for its
        # bonus action (result `drunk`), and no target click follows.
        out, calls, clicks = self._run("Shadowheart", "Potion of Healing", name="Potion of Healing",
                                       res="use_ability: done — drunk charged_by_guard=[resource=bonus action]")
        self.assertEqual(len(calls), 1)
        self.assertEqual(clicks, [])
        self.assertIn("drunk", out.split("\n")[0])

    def test_scroll_in_combat_is_read_and_its_action_settled(self):
        # A7: claude-run-2's Orin plan reads scrolls mid-fight; the mod reads it on the reader's turn (combat_scroll)
        out, calls, clicks = self._run(
            "Shadowheart", "Scroll of Fear", target=self.GALE, name="Scroll of Fear",
            res="use_ability: done — combat_scroll",
            rpc={"act.scroll_settle": lambda a: {"read": True, "charged": [{"resource": "action", "amount": 1}]}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [self.GALE])
        self.assertIn("use_ability: done — read in combat (Scroll of Fear read, cast on", head)
        self.assertIn("its action is spent (charged by the guard: action)", head)
        self.assertNotIn("combat_scroll", out)

    def test_scroll_in_combat_not_used_up_spends_nothing(self):
        saved, server.SCROLL_SETTLE_S = server.SCROLL_SETTLE_S, 0.0
        self.addCleanup(setattr, server, "SCROLL_SETTLE_S", saved)
        out, _, _ = self._run("Shadowheart", "Scroll of Fear", target=self.GALE, name="Scroll of Fear",
                              res="use_ability: done — combat_scroll",
                              rpc={"act.scroll_settle": lambda a: {"read": False}})
        self.assertIn("the scroll was not used up (no target taken?): no action spent", out.split("\n")[0])
        self.assertTrue(any(op == "act.scroll_settle" and a.get("final") for op, a in self.rpcs), "final settle")

    def test_out_of_combat_scroll_has_no_settle(self):
        self._run("Shadowheart", "Scroll of Invisibility", target=self.SH)
        self.assertNotIn("act.scroll_settle", [op for op, _ in self.rpcs])

    def test_scorching_ray_scroll_clicks_once_per_beam(self):
        left = iter([2, 1])

        def cast_state(a):
            r = next(left, None)
            return {"armed": True, "remaining": r} if r is not None else {"armed": False}
        out, _, clicks = self._run("Shadowheart", "Scroll of Scorching Ray", target=self.GALE,
                                   name="Scroll of Scorching Ray", rpc={"ui.cast_state": cast_state})
        self.assertEqual(clicks, [self.GALE] * 3)
        self.assertIn("(3 picks)", out.split("\n")[0])

    def test_cone_scroll_in_combat_is_aimed_at_the_point(self):
        # S066 #66 r3 Wyll: "Scroll of Cone of Cold" x,y,z 56,-53,996.5 → "cast on e2 Wyll by a click" (400 g and
        # the action, no damage): the spell in hand is clicked on the ground at the point, never on the reader
        out, _, clicks = self._run(
            "Shadowheart", "Scroll of Cone of Cold", name="Scroll of Cone of Cold", pos=(56.0, -53.0, 996.5),
            res="use_ability: done — combat_scroll",
            rpc={"act.scroll_settle": lambda a: {"read": True, "charged": []}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [])
        self.assertEqual(self.grounds, [(self.SH, "Scroll of Cone of Cold", [56.0, -53.0, 996.5])])
        self.assertIn("read in combat (Scroll of Cone of Cold read, aimed at the ground at (56,-53,996.5) by a "
                      "click); its action is spent", head)
        self.assertNotIn("cast on", head)

    def test_misty_step_scroll_lands_and_says_where(self):
        # S066 #143: use_ability(Karlach, "Scroll of Misty Step", x=24,y=-43,z=1005) → "cast on e1 Karlach by a
        # click", Karlach still @(17.8,-42.8): now the landing is clicked and the result says she moved
        where = iter([[17.8, -42.8, 1004.8], [17.8, -42.8, 1004.8], [23.9, -43.0, 1005.0]])
        last = [None]

        def status(a):
            last[0] = next(where, last[0])
            return {"party": [{"uuid": self.SH, "pos": last[0]}]}
        with mock.patch.object(server.time, "sleep"):
            out, _, clicks = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                                       pos=(24.0, -43.0, 1005.0), rpc={"state.status": status})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [])
        self.assertIn("aimed at the ground at (24,-43,1005) by a click); Shadowheart moved 6 m, now (23.9,-43,1005)",
                      head)
        self.assertNotIn("act.scroll_settle", [op for op, _ in self.rpcs])

    def test_misty_step_scroll_that_did_not_move_says_so(self):
        saved, server.TELEPORT_WAIT_S = server.TELEPORT_WAIT_S, 0.0
        self.addCleanup(setattr, server, "TELEPORT_WAIT_S", saved)
        out, _, _ = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                              pos=(24.0, -43.0, 1005.0),
                              rpc={"state.status": lambda a: {"party": [{"uuid": self.SH, "pos": [17.8, -42.8, 1004.8]}]}})
        self.assertIn("Shadowheart did NOT move (still (17.8,-42.8,1004.8))", out.split("\n")[0])

    def test_ground_not_taken_spends_nothing(self):
        saved, server.SCROLL_SETTLE_S = server.SCROLL_SETTLE_S, 0.0
        self.addCleanup(setattr, server, "SCROLL_SETTLE_S", saved)
        miss = HotbarCastResult(False, reason="ground_refused", stage="ground",
                                detail={"why": "the game shows a CastWarning cursor there", "cancelled": "escape"})
        out, _, clicks = self._run("Shadowheart", "Scroll of Cone of Cold", name="Scroll of Cone of Cold",
                                   pos=(56.0, -53.0, 996.5), ground=miss, res="use_ability: done — combat_scroll",
                                   rpc={"act.scroll_settle": lambda a: {"read": False}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [])
        self.assertIn("the ground at (56,-53,996.5) was not taken (the game shows a CastWarning cursor there); the "
                      "spell was put back, nothing cast", head)
        self.assertIn("the scroll was not used up (no target taken?): no action spent", head)

    def test_a_target_wins_over_a_point(self):
        _, _, clicks = self._run("Shadowheart", "Scroll of Fear", target=self.GALE, name="Scroll of Fear",
                                 pos=(1.0, 2.0, 3.0))
        self.assertEqual((clicks, self.grounds), ([self.GALE], []))

    def test_plan_fits_counts_a_carried_scroll(self):
        # combat_turn's who-inference (ACT-2): a plan naming "Scroll of Fear" fits the member who carries it
        from unittest import mock
        inv = {"items": [{"uuid": self.SCROLL, "name": "Scroll of Fear", "owner": self.SH}]}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return inv if op == "state.inventory" else {"actions": []}

        def no_action(char, ability):
            raise server.GameError(f"{char} has no action called '{ability}'")
        with mock.patch.object(server, "game", G()), mock.patch.object(server, "_ability_id", no_action), \
                mock.patch.object(server, "_learn_actions", lambda *a: None):
            self.assertTrue(server._plan_fits(self.SH, ["Scroll of Fear"]))
            self.assertFalse(server._plan_fits(self.GALE, ["Scroll of Fear"]))

    def test_disarm_by_name_is_the_disarm_tool(self):
        from unittest import mock
        with mock.patch.object(server, "disarm", lambda t, w=None: f"disarm: done {t} {w}"):
            out, calls, _ = self._run("Shadowheart", "Disarm", target=self.SH)
        self.assertTrue(out.startswith("disarm: done"), out)
        self.assertEqual(calls, [])

    def test_no_scroll_keeps_the_refusal(self):
        server_inv = None
        out, calls, _ = self._run("Shadowheart", "Fireball")
        self.assertIn("no action called", out)
        self.assertEqual(calls, [])
