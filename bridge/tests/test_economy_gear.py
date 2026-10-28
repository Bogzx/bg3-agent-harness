"""Offline tests: weapons and gear (main hands, weapon ranking, contested and pact weapons, give equipped, spell
gear), scroll use and the level-up stage log. Fixtures: test_economy.py."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from bg3ai import hotbar
from test_economy import (  # sets BG3AI_RUNTIME before importing bg3ai
    GALE,
    WIZARD,
    GameError,
    Handles,
    HotbarCastResult,
    JobResult,
    _u,
    combat,
    core,
    economy,
    inventory,
    overview,
    plumbing,
    reporting,
    turn_tool,
    world_tools,
)


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

        saved = (core.game, plumbing._act, inventory._item_uuid, core.H)
        core.H = Handles()
        core.H.get(amulet, "Spellcrux Amulet"), core.H.get(karlach, "Karlach"), core.H.get(gale, "Gale")
        core.game, plumbing._act = G(), fake_act
        inventory._item_uuid = lambda item, who=None, worn=False: amulet
        try:
            out = inventory.manage_inventory("give", "e1", who="Karlach", to="Gale")
        finally:
            core.game, plumbing._act, inventory._item_uuid, core.H = saved
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
        plumbing._log_stage_ms("ui.level_up", r)
        self.assertNotIn("_stage_ms", r.result)
        lines = (core.RUNTIME / "stage_ms.jsonl").read_text().strip().splitlines()
        self.assertEqual(_json.loads(lines[-1])["stages"], "open=900 spells=4200")


class RuntimeLogsMakeTheirDir(unittest.TestCase):
    """The runtime logs a tool writes before any actions.jsonl line made runtime/ (a new BG3AI_RUNTIME: the test
    profile's, 2026-10-28) create it as core._log does; they lost every record to the swallowed OSError."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name) / "runtime"  # not made yet
        patcher = mock.patch.object(core, "RUNTIME", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, name: str) -> list[dict]:
        return [json.loads(x) for x in (self.runtime / name).read_text().splitlines()]

    def test_stage_ms(self):
        plumbing._log_stage_ms("ui.level_up", JobResult(status="done", result={"_stage_ms": "open=900"}, elapsed_ms=900))
        self.assertEqual(self.rows("stage_ms.jsonl")[0]["stages"], "open=900")

    def test_character_creation_timing(self):
        from bg3ai.tools import newgame
        newgame._log_cc_timing({"apply_ms": 1200})
        self.assertEqual(self.rows("stage_ms.jsonl")[0]["op"], "create_character")

    def test_hotbar_cast(self):
        combat._log_hotbar("Target_Bless", HotbarCastResult(ok=True))
        self.assertEqual(self.rows("hotbar_cast.jsonl")[0]["spell"], "Target_Bless")


class ScrollUse(unittest.TestCase):
    """S030 16:34: use_ability(Shadowheart, "Scroll of Invisibility") → "no action called"; manage_inventory use
    → "done" with the game waiting for a target; screenshot + click_on by hand. The scroll is read and the target
    clicked in one call now."""
    SH, GALE, SCROLL = ("11111111-0000-0000-0000-0000000000a1", "11111111-0000-0000-0000-0000000000a2",
                        "11111111-0000-0000-0000-0000000000a3")
    cancel_how: str | None = "escape"  # hotbar.cancel's answer: the spell in hand put back with ESCAPE

    def _run(self, who, ability, target="", owner=None, click_ok=True, name="Scroll of Invisibility",
             res="use_ability: done — used", screen=None, rpc=None, targets="", pos=None, ground=None, jobs=None):
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
                if op == "ui.cast_state":
                    return {"armed": True}   # the scroll's spell in hand after its Use
                return default

            rpc = try_rpc

            def run_job(self, op, args=None, wait_s=None):
                rpcs.append((op, dict(args or {})))
                return (jobs or {}).get(op, lambda a: JobResult("done", {}))(args or {})

            def drain_events(self, ctx=None):
                return []

        def fake_act(tool, op, args, **kw):
            calls.append((op, dict(args)))
            reporting._batch["last"] = "done"
            return res + "\nEVENTS\n  used item={name=" + name + "}"

        def no_action(char, ability):
            raise GameError(f"{char} has no action called '{ability}' (get_actions lists them)")

        def fake_click(game, inp, uuid, button="L", **kw):
            clicks.append(uuid)
            return clicker.ClickResult(ok=click_ok, reason=None if click_ok else "hover_miss")

        def fake_ground(game, inp, caster, spell, p, should_stop=None):
            self.grounds.append((caster, spell, list(p)))
            return ground or HotbarCastResult(True, picked=1)

        saved = (core.game, plumbing._act, overview._ability_id, core.H, world_tools._inp, clicker.click_entity,
                 plumbing._who, twostep.ground_click, hotbar.cancel)
        twostep.ground_click = fake_ground
        self.cancels = []
        hotbar.cancel = lambda game, inp, char: (self.cancels.append(char), self.cancel_how)[1]
        core.H = Handles()
        for u, n in ((sh, "Shadowheart"), (self.GALE, "Gale"), (scroll, "Scroll of Invisibility")):
            core.H.get(u, n)
        core.game, plumbing._act, overview._ability_id = G(), fake_act, no_action
        world_tools._inp = lambda: object()
        clicker.click_entity = fake_click
        plumbing._who = lambda w: {"Shadowheart": sh, "Gale": self.GALE}.get(w, w)
        try:
            x, y, z = pos or (None, None, None)
            out = combat.use_ability(ability, target=core.H.get(target) if target else "", who=who,
                                     targets=targets, x=x, y=y, z=z)
        except GameError as e:
            out = "ERR " + str(e)
        finally:
            (core.game, plumbing._act, overview._ability_id, core.H, world_tools._inp, clicker.click_entity,
             plumbing._who, twostep.ground_click, hotbar.cancel) = saved
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

    FAR = {"ui.cast_state": lambda a: {"armed": True, "spell": "Target_Longstrider", "spell_name": "Longstrider"},
           "act.spell_reach": lambda a: {"range_m": 1.5, "melee": True, "reach_m": 2.5, "dist_m": 9.47},
           "act.scroll_settle": lambda a: {"read": True, "charged": []}}

    def reach_after(self, *dists):
        """act.spell_reach answering 9.47 m before the walk, then `dists` after each leg (touch, reach 2.5 m)."""
        seq = [9.47, *dists]
        return dict(self.FAR, **{"act.spell_reach": lambda a: {"range_m": 1.5, "melee": True, "reach_m": 2.5,
                                                                "dist_m": seq.pop(0) if len(seq) > 1 else seq[0]}})

    def test_a_target_beyond_the_spells_reach_is_walked_to_first(self):
        """Live N9 step 3: Longstrider (touch) read 9.47 m from Wyll: the click landed, nothing cast, nobody walked.
        The reader walks (move_to range = the spell's reach, centres, as for a ranged spell: live N10-N13, move_to
        within's melee ring ended 3.3-3.9 m off) and spell_reach says when it is in reach."""
        out, calls, clicks = self._run("Shadowheart", "Scroll of Longstrider", target=self.GALE,
                                       name="Scroll of Longstrider", rpc=self.reach_after(2.0),
                                       jobs={"act.move_to": lambda a: JobResult("done", {"dist": 2.0, "in_reach": True})})
        head = out.split("\n")[0]
        self.assertEqual(self.cancels, [self.SH])   # the spell put back before the walk
        moves = [a for op, a in self.rpcs if op == "act.move_to"]
        self.assertEqual(moves, [{"char": self.SH, "target": self.GALE, "range": 2.5}])
        self.assertEqual(calls, [("act.interact", {"char": self.SH, "target": self.SCROLL})] * 2)   # read again
        self.assertEqual(clicks, [self.GALE])
        self.assertTrue(head.startswith("use_ability: done"), head)
        self.assertIn("; Gale was 9.5 m from Shadowheart, beyond Longstrider's 2.5 m reach; the spell in hand was put back"
                      " (escape); Shadowheart walked into reach first (now 2.0 m from Gale) and read it again "
                      "(Scroll of Longstrider read, cast on", head)

    def test_a_walk_that_ends_out_of_reach_is_not_walked_again(self):
        """Live N10-N12 step 2: Claude (-42.0,26.4,-44.7), Wyll (-38.5,26.4,-53.5), 9.4 m, Longstrider's 2.5 m reach:
        the walk ended 3.3 m from Wyll each time; N11/N12's second leg from there walked 10 m on "a 0.0 m route" and
        ended 7.2 m off, past him. One walk, then an honest FAILED with the distance."""
        seq = [9.4, 3.3]
        rpc = dict(self.FAR, **{"act.spell_reach": lambda a: {"range_m": 1.5, "melee": True, "reach_m": 2.5,
                                                               "dist_m": seq.pop(0) if len(seq) > 1 else seq[0]}})
        out, calls, clicks = self._run("Shadowheart", "Scroll of Longstrider", target=self.GALE,
                                       name="Scroll of Longstrider", rpc=rpc,
                                       jobs={"act.move_to": lambda a: JobResult("done", {"dist": 3.3})})
        head = out.split("\n")[0]
        self.assertEqual(len([op for op, _ in self.rpcs if op == "act.move_to"]), 1)
        self.assertTrue(head.startswith("use_ability: FAILED"), head)
        self.assertIn("Gale was 9.4 m from Shadowheart, beyond Longstrider's 2.5 m reach; the spell in hand was put back"
                      " (escape); the walk into reach ended 3.3 m from Gale, still beyond the 2.5 m reach, nothing cast",
                      head)
        self.assertEqual((len(calls), clicks), (1, []))

    def test_a_ranged_spell_walks_only_into_its_range(self):
        """App review of #37: move_to `within` is a melee reach (2.2 m at most): a Fireball read 20 m off walked the
        reader into its own blast. A ranged spell's walk is move_to `range` (stop on the way in)."""
        far = dict(self.FAR, **{"act.spell_reach": lambda a: {"range_m": 18, "melee": False, "reach_m": 18,
                                                                "dist_m": 20.0}})
        self._run("Shadowheart", "Scroll of Fireball", target=self.GALE, name="Scroll of Fireball", rpc=far,
                  jobs={"act.move_to": lambda a: JobResult("done", {"dist": 17.0, "in_reach": True})})
        moves = [a for op, a in self.rpcs if op == "act.move_to"]
        self.assertEqual(moves, [{"char": self.SH, "target": self.GALE, "range": 18}])

    def test_a_spell_that_stays_in_hand_is_not_walked_with(self):
        self.cancel_how = "failed (still armed)"
        out, calls, clicks = self._run("Shadowheart", "Scroll of Longstrider", target=self.GALE,
                                       name="Scroll of Longstrider", rpc=self.FAR)
        head = out.split("\n")[0]
        self.assertTrue(head.startswith("use_ability: FAILED"), head)
        self.assertIn("beyond Longstrider's 2.5 m reach: not walked; the spell is still in Shadowheart's hand (failed "
                      "(still armed)), nothing cast", head)
        self.assertEqual([op for op, _ in self.rpcs if op == "act.move_to"], [])
        self.assertEqual((len(calls), clicks), (1, []))

    def test_a_failed_walk_into_reach_reads_nothing_more(self):
        out, calls, clicks = self._run("Shadowheart", "Scroll of Longstrider", target=self.GALE,
                                       name="Scroll of Longstrider", rpc=self.FAR,
                                       jobs={"act.move_to": lambda a: JobResult("failed", {}, "no path to the target")})
        head = out.split("\n")[0]
        self.assertTrue(head.startswith("use_ability: FAILED"), head)
        self.assertIn("beyond Longstrider's 2.5 m reach; the spell in hand was put back (escape); the walk into reach "
                      "failed: no path to the target (Shadowheart now 9.5 m from Gale), nothing cast", head)
        self.assertEqual((len(calls), clicks), (1, []))

    def test_a_failed_walk_into_reach_says_where_it_ended(self):
        """Live N13 step 1: the walk failed 'could not get closer to the destination' 3.9 m from Wyll and the reply
        did not say how far from him it ended."""
        why = "the walk ended 3.9 m from the target, beyond the 2.5 m range"
        out, calls, clicks = self._run("Shadowheart", "Scroll of Longstrider", target=self.GALE,
                                       name="Scroll of Longstrider", rpc=self.reach_after(3.9),
                                       jobs={"act.move_to": lambda a: JobResult("failed", {"dist": 3.9}, why)})
        head = out.split("\n")[0]
        self.assertTrue(head.startswith("use_ability: FAILED"), head)
        self.assertIn(f"the walk into reach failed: {why} (Shadowheart now 3.9 m from Gale), nothing cast", head)
        self.assertEqual((len(calls), clicks), (1, []))

    def test_failed_click_puts_the_spell_back(self):
        # live N8 7b: after a FAILED the scroll's spell stayed in hand, previewing its target, until a raw Escape
        out, _, _ = self._run("Shadowheart", "Scroll of Invisibility", click_ok=False)
        head = out.split("\n")[0]
        self.assertTrue(head.startswith("use_ability: FAILED"), head)
        self.assertIn("for Scroll of Invisibility's target failed (hover_miss): nothing cast; the spell in hand was put "
                      "back (escape)", head)
        self.assertEqual(self.cancels, [self.SH])

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
        saved, world_tools.SCROLL_SETTLE_S = world_tools.SCROLL_SETTLE_S, 0.0
        self.addCleanup(setattr, world_tools, "SCROLL_SETTLE_S", saved)
        out, _, _ = self._run("Shadowheart", "Scroll of Fear", target=self.GALE, name="Scroll of Fear",
                              res="use_ability: done — combat_scroll",
                              rpc={"act.scroll_settle": lambda a: {"read": False}})
        head = out.split("\n")[0]
        self.assertTrue(head.startswith("use_ability: FAILED — read in combat"), head)
        self.assertIn("is still in Shadowheart's bags and Shadowheart cast nothing after it: nothing cast, no action "
                      "spent", head)
        self.assertTrue(any(op == "act.scroll_settle" and a.get("final") for op, a in self.rpcs), "final settle")

    def test_out_of_combat_scroll_is_done_on_evidence_it_cast(self):
        out, _, clicks = self._run("Shadowheart", "Scroll of Invisibility", target=self.GALE,
                                   rpc={"act.scroll_settle": lambda a: {"read": True, "charged": []}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [self.GALE])
        self.assertTrue(head.startswith("use_ability: done — used (Scroll of Invisibility read, cast on"), head)
        self.assertNotIn("action is spent", head)
        self.assertEqual(reporting._batch["last"], "done")

    def test_a_click_that_casts_nothing_is_failed_with_the_facts(self):
        # live N7 2d (2026-10-28): "use_ability: done ... cast on e1 Shadowheart by a click", the scroll still in her
        # bags: the click landed, the game took no target
        saved, world_tools.SCROLL_SETTLE_S = world_tools.SCROLL_SETTLE_S, 0.0
        self.addCleanup(setattr, world_tools, "SCROLL_SETTLE_S", saved)
        out, _, clicks = self._run("Shadowheart", "Scroll of Invisibility", target=self.GALE,
                                   rpc={"act.scroll_settle": lambda a: {"read": False}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [self.GALE])
        self.assertTrue(head.startswith("use_ability: FAILED — used (Scroll of Invisibility read, "), head)
        self.assertNotIn("cast on", head)
        self.assertIn("; Scroll of Invisibility is still in Shadowheart's bags and Shadowheart cast nothing after it: "
                      "nothing cast", head)
        self.assertEqual(reporting._batch["last"], "failed")
        self.assertTrue(any(op == "act.scroll_settle" and a.get("final") for op, a in self.rpcs), "final settle")
        # live N8 7b: Knock at Gale / Remove Curse: FAILED, and the spell stayed in hand; now it is put back
        self.assertTrue(head.endswith("nothing cast; the spell in hand was put back (escape)"), head)
        self.assertEqual(self.cancels, [self.SH])
        self.cancel_how = "failed (still reads in hand: Target_Knock)"
        out, _, _ = self._run("Shadowheart", "Scroll of Invisibility", target=self.GALE,
                              rpc={"act.scroll_settle": lambda a: {"read": False}})
        self.assertTrue(out.split("\n")[0].endswith("; the spell is still in Shadowheart's hand (failed (still reads in "
                                                     "hand: Target_Knock))"), out)

    def test_out_of_combat_revive_drops_the_reads_settle_entry(self):
        # app review of #31: the two-step revive route has its own evidence (REVIVED); out of combat no settle ran and
        # the read's entry stayed in the mod
        with mock.patch.object(world_tools, "_is_dead", lambda u: True), \
                mock.patch.object(world_tools, "_body_far", lambda c, t: None), \
                mock.patch.object(world_tools, "_revive_spot", lambda inp, n, c, t: "; Gale REVIVED"):
            out, _, clicks = self._run("Shadowheart", "Scroll of Revivify", target=self.GALE, name="Scroll of Revivify")
        self.assertEqual(clicks, [self.GALE])
        self.assertIn("; Gale REVIVED", out.split("\n")[0])
        self.assertIn(("act.scroll_settle", {"char": self.SH, "final": True}), self.rpcs)

    def test_a_failed_target_click_is_failed(self):
        out, _, _ = self._run("Shadowheart", "Scroll of Invisibility", target=self.GALE, click_ok=False)
        self.assertTrue(out.startswith("use_ability: FAILED"), out)
        self.assertEqual(reporting._batch["last"], "failed")

    def test_scorching_ray_scroll_clicks_once_per_beam(self):
        left = iter([3, 2, 1])   # the read before the first click (the spell in hand), then after each beam

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
        with mock.patch.object(time, "sleep"):
            out, _, clicks = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                                       pos=(24.0, -43.0, 1005.0), rpc={"state.status": status})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [])
        self.assertIn("aimed at the ground at (24,-43,1005) by a click); Shadowheart moved 6 m, now (23.9,-43,1005)",
                      head)
        self.assertIn("act.scroll_settle", [op for op, _ in self.rpcs])  # no answer here: no evidence either way

    def test_misty_step_scroll_into_the_void_is_not_read(self):
        # S093 09:47: Misty Step at (230.5,-55.7,4260.5), off the psyche's platform: Gale fell into the void and died
        why = ("the landing (230.5,-55.7,4260.5) is in the air: no floor within 2 m of that height (the only floor "
               "there is the void's bottom at y=-73.1, a Chasm: a fall there is fatal); pick a spot on the floor you "
               "stand on or can see")
        out, calls, _ = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                                  pos=(230.5, -55.7, 4260.5),
                                  rpc={"act.landing_check": lambda a: {"ok": False, "why": why}})
        self.assertTrue(out.startswith("ERR") or "FAILED" in out, out)
        self.assertIn("Scroll of Misty Step not read: the landing (230.5,-55.7,4260.5) is in the air", out)
        self.assertEqual(calls, [])  # the scroll is never used
        self.assertEqual(self.grounds, [])
        self.assertEqual([a.get("pos") for op, a in self.rpcs if op == "act.landing_check"], [[230.5, -55.7, 4260.5]])

    def test_misty_step_scroll_that_did_not_move_says_so(self):
        saved, world_tools.TELEPORT_WAIT_S = world_tools.TELEPORT_WAIT_S, 0.0
        self.addCleanup(setattr, world_tools, "TELEPORT_WAIT_S", saved)
        out, _, _ = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                              pos=(24.0, -43.0, 1005.0),
                              rpc={"state.status": lambda a: {"party": [{"uuid": self.SH, "pos": [17.8, -42.8, 1004.8]}]}})
        self.assertIn("Shadowheart did NOT move (still (17.8,-42.8,1004.8))", out.split("\n")[0])

    def test_ground_not_taken_spends_nothing(self):
        saved, world_tools.SCROLL_SETTLE_S = world_tools.SCROLL_SETTLE_S, 0.0
        self.addCleanup(setattr, world_tools, "SCROLL_SETTLE_S", saved)
        miss = HotbarCastResult(False, reason="ground_refused", stage="ground",
                                detail={"why": "the game shows a CastWarning cursor there", "cancelled": "escape"})
        out, _, clicks = self._run("Shadowheart", "Scroll of Cone of Cold", name="Scroll of Cone of Cold",
                                   pos=(56.0, -53.0, 996.5), ground=miss, res="use_ability: done — combat_scroll",
                                   rpc={"act.scroll_settle": lambda a: {"read": False}})
        head = out.split("\n")[0]
        self.assertEqual(clicks, [])
        self.assertIn("the ground at (56,-53,996.5) was not taken (the game shows a CastWarning cursor there); the "
                      "spell was put back, nothing cast", head)
        self.assertTrue(head.startswith("use_ability: FAILED — read in combat"), head)
        self.assertIn("Scroll of Cone of Cold is still in Shadowheart's bags and Shadowheart cast nothing after it: "
                      "nothing cast, no action spent", head)

    def test_a17_misty_step_not_taken_says_the_looks_and_a_move_anyway(self):
        # L8 23:58: "a character is under the cursor (3ed74f06…)" x5, "nothing cast", and Karlach stood 5.1 m on
        miss = HotbarCastResult(False, reason="ground_refused", stage="ground", detail={
            "why": f"a character is under the cursor ({self.GALE})", "cancelled": "escape",
            "looks": [{"world_src": "ray_plane", "on_avoid": True, "hover": self.GALE}] * 5})
        where = iter([[17.8, -42.8, 1004.8], [22.6, -42.8, 1006.5]])
        last = [None]

        def status(a):
            last[0] = next(where, last[0])
            return {"party": [{"uuid": self.SH, "pos": last[0]}]}
        out, _, _ = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                              pos=(23.8, -42.35, 1004.8), ground=miss, rpc={"state.status": status})
        head = out.split("\n")[0]
        self.assertRegex(head, r"was not taken \(a character is under the cursor \(e\d+ Gale\)\)")
        self.assertRegex(head, r"\[5 cursor looks; ground point from ray_plane x5; under the cursor: e\d+ Gale x5\]")
        self.assertIn("; yet Shadowheart moved 5 m (now (22.6,-42.8,1006.5)): check whether the scroll is still in "
                      "the bags", head)

    def test_a17_misty_step_landed_clear_of_a_body_says_so(self):
        hit = HotbarCastResult(True, detail={"ground_src": "Position", "clear_of": [self.GALE], "shifted_m": 1.2,
                                             "ground_at": [23.8, -42.35, 1006.0]})
        saved, world_tools.TELEPORT_WAIT_S = world_tools.TELEPORT_WAIT_S, 0.0
        self.addCleanup(setattr, world_tools, "TELEPORT_WAIT_S", saved)
        out, _, _ = self._run("Shadowheart", "Scroll of Misty Step", name="Scroll of Misty Step",
                              pos=(23.8, -42.35, 1004.8), ground=hit)
        self.assertRegex(out.split("\n")[0], r"by a click; ground point from Position; landed the click at "
                         r"\(23\.8,-42\.4,1006\), 1\.2 m from the asked point: e\d+ Gale stood under the cursor there\)")

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
            raise GameError(f"{char} has no action called '{ability}'")
        with mock.patch.object(core, "game", G()), mock.patch.object(overview, "_ability_id", no_action), \
                mock.patch.object(overview, "_learn_actions", lambda *a: None):
            self.assertTrue(turn_tool._plan_fits(self.SH, ["Scroll of Fear"]))
            self.assertFalse(turn_tool._plan_fits(self.GALE, ["Scroll of Fear"]))

    def test_disarm_by_name_is_the_disarm_tool(self):
        from unittest import mock
        with mock.patch.object(world_tools, "disarm", lambda t, w=None: f"disarm: done {t} {w}"):
            out, calls, _ = self._run("Shadowheart", "Disarm", target=self.SH)
        self.assertTrue(out.startswith("disarm: done"), out)
        self.assertEqual(calls, [])

    def test_no_scroll_keeps_the_refusal(self):
        server_inv = None
        out, calls, _ = self._run("Shadowheart", "Fireball")
        self.assertIn("no action called", out)
        self.assertEqual(calls, [])


class ScrollSettleCast(unittest.TestCase):
    """Live 2026-10-24 (S049-end): Karlach read Revivify on Shadowheart in combat; the scroll was still in her bags
    2.6 s after Shadowheart stood up (the cast still playing), so the reply said "not used up: no action spent" while
    her action was spent and the scroll gone a moment later. The settle waits while the mod says the cast plays."""

    def _settle(self, answers, raw=False):
        clock, seen = [0.0], []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                seen.append((clock[0], dict(args or {})))
                return answers(clock[0], args or {})

        def sleep(s):
            clock[0] += s
        with mock.patch.object(core, "game", G()), mock.patch.object(time, "monotonic", lambda: clock[0]), \
                mock.patch.object(time, "sleep", sleep):
            got = world_tools._scroll_settle("c")
            if raw:
                return got, seen
            return (got[1] if got[0] else "the scroll was not used up"), seen

    def test_a_cast_still_playing_is_waited_for(self):
        out, seen = self._settle(lambda t, a: {"read": True, "charged": []} if t >= 4.0 else {"read": False,
                                 "casting": t < 3.5 or None})
        self.assertEqual(out, "; its action is spent")
        self.assertFalse(any(a.get("final") for _, a in seen), "no final while it plays")

    def test_a_cast_that_never_ends_stops_at_the_cap(self):
        with mock.patch.object(overview, "_name_of", lambda u: "Karlach"):
            out, seen = self._settle(lambda t, a: {"read": False, "casting": True}, raw=True)
        self.assertEqual(out, (False, "; Karlach was still casting it 12 s after the click (the wait's cap): no cast "
                                      "read"))
        self.assertFalse(any(a.get("final") for _, a in seen), "the entry is kept (no final)")
        self.assertLessEqual(seen[-1][0], world_tools.SCROLL_CAST_MAX_S + 0.5)

    def test_a_reader_walking_into_range_is_waited_for(self):
        # app review of #31: a touch-range scroll clicked on a member 8-10 m off: the reader walks 4 s, then casts
        def answers(t, a):
            if t >= 5.0:
                return {"read": True, "charged": [{"resource": "action", "amount": 1}]}
            return {"read": False, "approaching": t < 4.0 or None, "casting": t >= 4.0 or None}
        out, seen = self._settle(answers)
        self.assertEqual(out, "; its action is spent (charged by the guard: action)")
        self.assertFalse(any(a.get("final") for _, a in seen), "no final while it walks")

    def test_a_walk_that_never_ends_fails_at_the_cap_and_keeps_the_entry(self):
        with mock.patch.object(overview, "_name_of", lambda u: "Shadowheart"):
            out, seen = self._settle(lambda t, a: {"read": False, "approaching": True}, raw=True)
        self.assertEqual(out, (False, "; Shadowheart was still walking 12 s after the click (the wait's cap): no cast "
                                      "read"))
        self.assertFalse(any(a.get("final") for _, a in seen), "the entry is kept (no final)")
        self.assertLessEqual(seen[-1][0], world_tools.SCROLL_CAST_MAX_S + 0.5)

    def test_no_cast_keeps_the_short_window(self):
        out, seen = self._settle(lambda t, a: {"read": False})
        self.assertIn("the scroll was not used up", out)
        self.assertLess(seen[-1][0], world_tools.SCROLL_SETTLE_S + 0.5)


if __name__ == "__main__":
    unittest.main()
