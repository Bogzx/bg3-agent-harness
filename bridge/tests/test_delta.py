"""Offline tests: delta observations (look, get_status/get_map diffs, the action hook) on a fake game.

Run: cd bridge && uv run python -m unittest discover -s tests
Sizes: cd bridge && uv run python tests/test_delta.py --sizes   (full vs delta output per scenario)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import shutil
import tempfile
import re
import unittest
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import delta, server  # noqa: E402
from bg3ai.render import Handles  # noqa: E402


def U(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


CLAUDE, SHART, LAE, GALE = U(1), U(2), U(3), U(4)
IMP1, IMP2, IMP3, LAEZEL_NPC = U(101), U(102), U(103), U(104)
CHEST, DOOR = U(201), U(202)
POTION, KEY = U(301), U(302)


def party_member(u, name, cls, hp, pos, statuses=()):
    return {"uuid": u, "name": name, "hp": hp, "max_hp": 12, "level": 1, "pos": pos,
            "classes": [{"class": cls, "level": 1}], "statuses": [{"id": s.upper(), "name": s} for s in statuses]}


def base_world() -> dict:
    status = {"region": "Nautiloid", "subregion": "Ship Hold", "level": "TUT_Avernus_C", "active": CLAUDE,
              "gold": 40, "in_combat": False, "turn_based": False, "in_dialogue": False,
              "party": [party_member(CLAUDE, "Claude", "Paladin", 12, [10.0, 0.0, 10.0], ["Bless"]),
                        party_member(SHART, "Shadowheart", "Cleric", 10, [11.0, 0.0, 9.0]),
                        party_member(LAE, "Lae'zel", "Fighter", 12, [9.0, 0.0, 9.0]),
                        party_member(GALE, "Gale", "Wizard", 8, [10.0, 0.0, 8.0], ["Mage Armour"])]}
    chars = [{"uuid": LAEZEL_NPC, "name": "Mind Flayer Thrall", "relation": "neutral", "pos": [20.0, 0.0, 12.0],
              "dist": 10.2, "hp": 20, "max_hp": 20}]
    chars += [{"uuid": U(110 + i), "name": f"Corpse {i}", "relation": "neutral", "pos": [15.0 + i, 0.0, 15.0],
               "dist": 7.0 + i, "dead": True} for i in range(5)]
    items = [{"uuid": CHEST, "name": "Chest", "kind": "container", "pos": [14.0, 0.0, 12.0], "dist": 4.5, "locked": True},
             {"uuid": DOOR, "name": "Sphincter Door", "kind": "door", "pos": [0.0, 0.0, 10.0], "dist": 10.0}]
    items += [{"uuid": U(400 + i), "name": f"Tadpole Pod {i}", "kind": "item", "pos": [12.0 + i, 0.0, 6.0],
               "dist": 3.0 + i} for i in range(18)]
    items += [{"uuid": U(500 + i), "name": f"Brazier {i}", "kind": "usable", "pos": [5.0, 0.0, 5.0 + i],
               "dist": 7.0 + i} for i in range(10)]
    inv = {"gold": 40, "items": []}
    for owner, names in ((CLAUDE, ["Longsword", "Shield", "Scale Mail", "Healing Potion", "Torch", "Rope"]),
                         (SHART, ["Mace", "Chain Shirt", "Holy Symbol", "Scroll of Bless", "Bread", "Apple"]),
                         (LAE, ["Greatsword", "Half Plate", "Javelin", "Rations", "Waterskin", "Tinderbox"]),
                         (GALE, ["Quarterstaff", "Spellbook", "Scroll of Magic Missile", "Wine", "Book", "Candle"])):
        for i, n in enumerate(names):
            inv["items"].append({"uuid": U(600 + len(inv["items"])), "name": n, "count": 1, "owner": owner,
                                 "equipped_slot": "MainHand" if i == 0 else None})
    journal = [{"title": "Escape the Nautiloid", "category": "Main", "steps": ["Find the helm.", "Reach the helm."]},
               {"title": "Rescue the Trapped Woman", "category": "Side", "steps": ["Open the pod."]}]
    markers = [{"label": "Escape the ship", "quest": "Escape the Nautiloid", "here": True, "dist": 16.0, "dir": "W",
                "pos": [-6.0, 0.0, 10.0]},
               {"label": "Find the helm", "quest": "Escape the Nautiloid", "here": True, "dist": 40.2, "dir": "N"}]
    return {"status": status, "nearby": {"characters": chars, "items": items}, "inventory": inv,
            "journal": journal, "markers": markers, "map": {"region": "Nautiloid", "waypoints": [{"id": 1, "name": "Ship"}]},
            "ui": {"screen": "game", "region": "Ship Hold"}}


class FakeGame:
    """Serves a mutable world dict; no eval(), so delta.multi_rpc falls back to per-op try_rpc."""

    def __init__(self, world: dict):
        self.w = world
        self.calls: list[str] = []

    def rpc(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        self.calls.append(op)
        w = self.w
        table = {"state.status": w["status"], "state.nearby": w["nearby"], "state.inventory": w["inventory"],
                 "state.journal": w["journal"], "nav.markers": {"markers": w["markers"]}, "state.map": w["map"],
                 "ui.state": w["ui"], "nav.set_subregion": {}, "nav.hazards": {"hazards": w.get("hazards", [])}}
        if op not in table:
            raise AssertionError(f"unexpected op {op}")
        return copy.deepcopy(table[op])

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        return self.rpc(op, args, context)

    def drain_events(self, ctx: str) -> list:
        return []


# ---- scenario steps: each mutates the world like the game would ----

def walk(w):  # the party walks 12 m west; pods shift distance, two new things come into range
    for c in w["status"]["party"]:
        c["pos"] = [c["pos"][0] - 12.0, c["pos"][1], c["pos"][2]]
    for it in w["nearby"]["items"]:
        it["dist"] = round(it["dist"] + 3.1, 1)
    w["nearby"]["items"] = [it for it in w["nearby"]["items"] if not it["name"].startswith("Tadpole Pod 1")]
    w["nearby"]["items"].append({"uuid": U(700), "name": "Lever", "kind": "lever", "dist": 2.0})
    w["markers"][0]["dist"] = 4.0


def combat(w):  # three imps arrive, combat starts, Gale is hit and burning
    s = w["status"]
    s["in_combat"], s["turn_based"] = True, True
    s["combat"] = {"round": 1, "current": IMP1}
    gale = s["party"][3]
    gale["hp"] = 3
    gale["statuses"].append({"id": "BURNING", "name": "Burning", "turns": 2})
    for u in (IMP1, IMP2, IMP3):
        w["nearby"]["characters"].insert(0, {"uuid": u, "name": "Imp", "relation": "hostile", "pos": [5.0, 0.0, 5.0],
                                             "dist": 7.1, "hp": 9, "max_hp": 9, "in_combat": True})
    w["nearby"]["characters"][3]["relation"] = "hostile"  # the thrall turns
    w["nearby"]["characters"][3]["in_combat"] = True


def loot(w):  # chest unlocked and opened, two items and 25 gold taken
    ch = w["nearby"]["items"][0]
    ch.pop("locked", None)
    ch["opened"] = True
    w["status"]["gold"] = w["inventory"]["gold"] = 65
    w["inventory"]["items"] += [{"uuid": POTION, "name": "Potion of Healing", "count": 2, "owner": CLAUDE},
                                {"uuid": KEY, "name": "Rusty Key", "count": 1, "owner": CLAUDE}]


def dialogue(w):
    w["status"]["in_dialogue"] = True
    w["ui"] = {"screen": "dialogue", "region": "Ship Hold",
               "dialogue": {"speaker": "Lae'zel", "text": "You. Tadpole. Help me or die.",
                            "options": [{"i": 1, "text": "I'll help."}, {"i": 2, "tags": ["Intimidation"], "text": "Back off."},
                                        {"i": 3, "text": "Who are you?"}]}}


def quest(w):
    w["journal"][0]["steps"].append("Take the helm and steer the ship.")
    w["journal"].append({"title": "Find a Cure", "category": "Main", "steps": ["Find a healer."]})
    w["markers"].append({"label": "Find a healer", "quest": "Find a Cure", "here": False})


SCENARIOS = [("nothing changed", lambda w: None), ("walk 12m", walk), ("combat starts", combat),
             ("loot a chest", loot), ("dialogue opens", dialogue), ("quest update", quest)]


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGame(base_world())
        orig = server.game
        server.game = self.fake
        self.addCleanup(setattr, server, "game", orig)
        server.DELTA.reset()
        delta.INV.reset()
        server._objectives["last"] = []


class LookTool(Base):
    def test_first_look_is_full_then_no_change(self):
        out = server.look()
        self.assertIn("WHERE Ship Hold · Nautiloid", out)
        self.assertIn("ITEM", out)  # nearby listing
        self.assertEqual(server.look(), "No change since your last look (full=true shows everything).")

    def test_walk(self):
        server.look()
        walk(self.fake.w)
        out = server.look().splitlines()
        self.assertEqual(out[0], "PARTY moved 12m W")
        self.assertTrue(any(x.startswith("+ ") and "Lever <lever> 2m" in x for x in out), out)
        self.assertTrue(any(x.startswith("GONE ") and "Tadpole Pod 1" in x for x in out), out)
        self.assertFalse(any("Tadpole Pod 2" in x for x in out), "distance-only changes are not reported")
        self.assertFalse(any("OBJECTIVE" in x for x in out), "objective distance changes are not reported")

    def test_long_walk_spread_is_one_party_line(self):
        """S026: 152/146/152/151 m S took four lines; give or take a few metres it is one PARTY line."""
        server.look()
        for i, c in enumerate(self.fake.w["status"]["party"]):
            c["pos"] = [c["pos"][0], c["pos"][1], c["pos"][2] - 150.0 + 2 * i]
        out = server.look().splitlines()
        self.assertTrue(out[0].startswith("PARTY moved ~"), out[0])
        self.assertFalse(any(" moved " in x for x in out[1:] if not x.startswith(("~", "+"))), out)

    def test_followers_trailing_the_leader_are_one_range_line(self):
        """S040: 'Wyll moved 12m SE, Karlach 11m S, Shadowheart 13m SE, Claude 21m SE' = 4 lines per walk result."""
        server.look()
        for i, c in enumerate(self.fake.w["status"]["party"]):
            d = 21.0 if i == 0 else 12.0 + i
            c["pos"] = [c["pos"][0] + d * 0.7, c["pos"][1], c["pos"][2] - d * 0.7 - (3.0 if i == 1 else 0.0)]
        out = server.look().splitlines()
        self.assertTrue(re.match(r"PARTY moved \d+-21m SE$", out[0]), out[0])
        self.assertFalse(any(" moved " in x for x in out[1:]), out)

    def test_straggler_keeps_its_own_line(self):
        server.look()
        party = self.fake.w["status"]["party"]
        for c in party:
            c["pos"] = [c["pos"][0] - 12.0, c["pos"][1], c["pos"][2]]
        party[0]["pos"][0] += 9.0
        out = server.look().splitlines()
        self.assertFalse(any(x.startswith("PARTY moved") for x in out), out)
        self.assertEqual(sum(" moved " in x for x in out), len(party))

    def test_combat(self):
        server.look()
        combat(self.fake.w)
        out = server.look()
        self.assertRegex(out, r"MODE exploring -> COMBAT turn-based \(round 1 turn=e\d+ Imp\)")
        self.assertRegex(out, r"e\d+ Gale HP 8->3/12, \+Burning")
        self.assertEqual(out.count("Imp hostile HP 9/9 7.1m"), 3)
        self.assertRegex(out, r"~ e\d+ Mind Flayer Thrall now hostile \(was neutral\), IN_COMBAT")
        self.assertNotIn(IMP1, out)

    def test_loot(self):
        server.look()
        loot(self.fake.w)
        out = server.look()
        self.assertRegex(out, r"~ e\d+ Chest unlocked, opened|~ e\d+ Chest opened, unlocked")
        self.assertIn("GOLD 40->65", out)
        self.assertRegex(out, r"ITEM \+ e\d+ Potion of Healing x2 \(Claude\)")
        self.assertRegex(out, r"ITEM \+ e\d+ Rusty Key \(Claude\)")

    def test_door_opens_and_trap_disarmed(self):  # NAV-4: no diff line before (S054 06:11/06:13 extra interacts)
        self.fake.w["nearby"]["items"][1]["closed"] = True
        self.fake.w["nearby"]["items"][0]["trap"] = {"armed": True}
        server.look()
        door, chest = self.fake.w["nearby"]["items"][1], self.fake.w["nearby"]["items"][0]
        door.pop("closed")
        door["open"] = True
        chest["trap"] = {"armed": False}
        out = server.look()
        self.assertRegex(out, r"~ e\d+ Sphincter Door now open")
        self.assertRegex(out, r"~ e\d+ Chest trap disarmed")

    def test_painting_lights_up(self):  # Grand Mausoleum: a button press lights its painting (puzzle state)
        server.look()
        ch = self.fake.w["nearby"]["items"][0]
        ch["effect"] = "lit"
        self.assertRegex(server.look(), r"~ e\d+ Chest now lit")
        ch.pop("effect")
        self.assertRegex(server.look(), r"~ e\d+ Chest no longer lit")
        self.fake.w["nearby"]["items"].append({"uuid": U(701), "name": "General", "kind": "usable", "dist": 3.6,
                                               "effect": "lit"})
        self.assertRegex(server.look(), r"\+ e\d+ General <usable> 3.6m \[lit\]")

    def test_dialogue_and_reminder(self):
        server.look()
        dialogue(self.fake.w)
        out = server.look()
        self.assertIn("DIALOGUE Lae'zel: You. Tadpole. Help me or die.", out)
        self.assertIn("  2. [Intimidation] Back off.", out)
        again = server.look()
        self.assertEqual(again, "No change since your last look (full=true shows everything).\n(DIALOGUE still open, unchanged; full=true repeats it)")
        self.assertIn("2. [Intimidation] Back off.", server.look(full=True))

    def test_quest(self):
        server.look()
        quest(self.fake.w)
        out = server.look()
        self.assertIn("QUEST Escape the Nautiloid: Take the helm and steer the ship.", out)
        self.assertIn("QUEST + Find a Cure: Find a healer.", out)
        self.assertIn("OBJECTIVE + o3 Find a healer (another area) [Find a Cure]", out)

    def test_new_area_gives_full_view(self):
        server.look()
        self.fake.w["status"]["subregion"] = "Helm"
        out = server.look()
        self.assertIn("WHERE Helm · Nautiloid", out)
        self.assertIn("PARTY", out)

    def test_output_is_stable(self):
        server.look()
        combat(self.fake.w)
        a = server.look()
        server.DELTA.reset()
        self.fake.w = base_world()
        server.look()
        combat(self.fake.w)
        self.assertEqual(server.look(), a)


class StatusAndMap(Base):
    def test_status_delta_does_not_touch_nearby(self):
        server.look()
        server.get_status()
        walk(self.fake.w)
        out = server.get_status()
        self.assertIn("PARTY moved 12m W", out)
        self.assertNotIn("Lever", out)  # nearby not fetched by get_status...
        self.assertIn("Lever", server.look())  # ...so the next look still reports it

    def test_status_full(self):
        server.get_status()
        out = server.get_status(full=True)
        self.assertIn("PARTY", out)
        self.assertIn("OBJECTIVE o1 Escape the ship 16m W", out)

    def test_map_delta(self):
        first = server.get_map()
        self.assertIn("OBJECTIVES", first)
        self.assertTrue(server.get_map().startswith("No map change (Nautiloid; 2 objectives"))
        self.fake.w["map"]["waypoints"].append({"id": 2, "name": "Roadside Cliffs"})
        quest(self.fake.w)
        out = server.get_map()
        self.assertIn("WAYPOINT + Roadside Cliffs", out)
        self.assertIn("OBJECTIVE + o3 Find a healer", out)

    def test_nearby_and_inventory_update_baseline(self):
        server.look()
        walk(self.fake.w)
        loot(self.fake.w)
        server.get_nearby()
        server.get_inventory()
        out = server.look()
        self.assertNotIn("Lever", out)
        self.assertNotIn("Rusty Key", out)
        self.assertIn("PARTY moved 12m W", out)


class ActionHook(Base):
    def test_changes_text(self):
        server.look()
        self.assertEqual(delta.changes_text(self.fake, server.H), "")
        combat(self.fake.w)
        txt = delta.changes_text(self.fake, server.H)
        self.assertTrue(txt.startswith("CHANGES\n  MODE exploring -> COMBAT"), txt)
        self.assertNotIn("state.inventory", self.fake.calls[-5:])

    def test_changes_text_never_raises(self):
        class Broken:
            def try_rpc(self, *a, **k):
                raise RuntimeError("boom")
        self.assertEqual(delta.changes_text(Broken(), server.H), "")


class MultiRpc(unittest.TestCase):
    def test_one_eval(self):
        class EvalGame:
            metrics = None

            def __init__(self):
                self.codes = []

            def eval(self, code, context="client", timeout=12.0):
                self.codes.append((code, context))
                return json.dumps([{"ok": True, "result": {"a": 1}}, {"ok": False, "error": "x"}])

        g = EvalGame()
        out = delta.multi_rpc(g, [("state.status", {}), ("nav.markers", {"x": "]]"})])
        self.assertEqual(out, [{"a": 1}, None])
        self.assertEqual(len(g.codes), 1)
        code, ctx = g.codes[0]
        self.assertEqual(ctx, "server")
        self.assertTrue(code.startswith("local R=Mods.BG3AI.BG3AI.rpc return '['..R([=[{"), code)
        self.assertIn("]=])..','..R([=[", code)


class InventoryRev(Base):
    """look sends the rev of the inventory it holds; the mod answers {unchanged} until an item event."""

    def setUp(self):
        super().setUp()
        self.rev, self.sent = 1, []
        inner = self.fake.rpc

        def rpc(op, args=None, context="server"):
            if op != "state.inventory":
                return inner(op, args, context)
            self.sent.append(dict(args or {}))
            self.fake.calls.append(op)
            if (args or {}).get("rev") == self.rev:
                return {"unchanged": True, "rev": self.rev, "gold": self.fake.w["inventory"].get("gold")}
            return {**copy.deepcopy(self.fake.w["inventory"]), "rev": self.rev}
        self.fake.rpc = rpc

    def test_second_look_reuses_the_held_inventory(self):
        server.look()
        self.assertEqual(self.sent[-1], {})
        server.look()
        self.assertEqual(self.sent[-1], {"rev": 1})
        self.assertIsNotNone(delta.INV.data)
        # gold rides on the unchanged reply; items come from the held copy, so no item lines
        self.fake.w["inventory"]["gold"] = (self.fake.w["inventory"].get("gold") or 0) + 25
        out = server.look()
        self.assertIn("gold", out.lower())
        self.assertNotIn("lost", out.lower())

    def test_new_rev_sends_the_full_list_and_the_delta_sees_it(self):
        server.look()
        self.fake.w["inventory"]["items"].append({"uuid": U(901), "name": "Healing Potion", "owner": self.fake.w["status"]["party"][0]["uuid"]})
        self.rev = 2  # an item event bumped the mod's cache
        out = server.look()
        self.assertIn("Healing Potion", out)
        self.assertEqual(delta.INV.data["rev"], 2)

    def test_foreign_unchanged_reply_is_not_reused(self):
        self.assertIsNone(delta.INV.merge({"unchanged": True, "rev": 7, "gold": 1}))
        delta.INV.merge({"items": [], "rev": 3, "gold": 5})
        self.assertIsNone(delta.INV.merge({"unchanged": True, "rev": 4, "gold": 1}))
        self.assertEqual(delta.INV.args(), {})


class Helpers(unittest.TestCase):
    def test_direction_matches_lua(self):
        self.assertEqual(delta.direction([0, 0, 0], [10, 0, 0]), "E")
        self.assertEqual(delta.direction([0, 0, 0], [0, 0, 10]), "N")
        self.assertEqual(delta.direction([0, 0, 0], [-10, 0, -10]), "SW")
        self.assertEqual(delta.direction([0, 0, 0], [0.1, 0, 0]), "")

    def test_partial_snapshots_compare_only_fetched_parts(self):
        H = Handles()
        tr = delta.Tracker(H)
        w = base_world()
        tr.observe(delta.snapshot(H, status=w["status"], nearby=w["nearby"]))
        self.assertEqual(tr.observe(delta.snapshot(H, status=w["status"])), [])


class Arrival(unittest.TestCase):
    """A walk ends somewhere new: every character is listed, items are led by doors/containers, rest counted."""

    def arrive(self, n_chars: int, n_items: int) -> list[str]:
        H = Handles()
        tr = delta.Tracker(H)
        tr.observe(delta.snapshot(H, nearby={"characters": [], "items": []}))
        chars = [{"uuid": U(700 + i), "name": f"Druid {i}", "relation": "neutral", "pos": [i, 0.0, 0.0],
                  "dist": 2.0 + i, "hp": 5, "max_hp": 5} for i in range(n_chars)]
        items = [{"uuid": U(800 + i), "name": f"Vase {i}", "kind": "usable", "dist": 1.0 + i} for i in range(n_items)]
        items += [{"uuid": U(900), "name": "Stone Door", "kind": "door", "dist": 30.0},
                  {"uuid": U(901), "name": "Wicker Chest", "kind": "container", "dist": 25.0}]
        return tr.observe(delta.snapshot(H, nearby={"characters": chars, "items": items}))

    def test_all_characters_listed_past_the_budget(self):
        lines = self.arrive(14, 20)
        self.assertEqual(sum(1 for ln in lines if "Druid" in ln), 14)
        self.assertNotIn("more characters", "\n".join(lines))
        # leads first among the items, even though they are the farthest
        items = [ln for ln in lines if ln.startswith("+ ") and "Druid" not in ln]
        self.assertEqual(len(items), delta.MIN_APPEARED_ITEMS)
        self.assertIn("Wicker Chest", items[0])  # leads by distance
        self.assertIn("Stone Door", items[1])
        self.assertEqual(lines[len(lines) - 1], "(+18 more: usable 18; get_nearby / get_frontier)")

    def test_few_things_listed_plainly(self):
        lines = self.arrive(2, 3)
        self.assertEqual(len(lines), 7)
        self.assertFalse(any(ln.startswith("(+") for ln in lines))

    def test_lookalikes_fold_onto_the_nearest(self):
        # live 13:01, Mausoleum gate: 10 "+ eN Skeleton <usable>" lines then "+28 more"
        H = Handles()
        tr = delta.Tracker(H)
        tr.observe(delta.snapshot(H, nearby={"characters": [], "items": []}))
        items = [{"uuid": U(800 + i), "name": "Skeleton", "kind": "usable", "dist": 1.5 + i} for i in range(12)]
        items += [{"uuid": U(850), "name": "Black Oleander", "kind": "item", "dist": 4.7},
                  {"uuid": U(851), "name": "Skeleton", "kind": "container", "dist": 3.0}]
        lines = tr.observe(delta.snapshot(H, nearby={"characters": [], "items": items}))
        sk = [ln for ln in lines if "Skeleton <usable>" in ln]
        self.assertEqual(len(sk), 1)
        self.assertIn("1.5m (+11 more Skeleton up to 12.5m)", sk[0])
        self.assertTrue(any("Black Oleander" in ln for ln in lines))
        self.assertTrue(any("Skeleton <container>" in ln for ln in lines))  # another kind stays its own line

    def test_character_cap(self):
        lines = self.arrive(delta.MAX_APPEARED_CHARS + 3, 0)
        self.assertIn("(+3 more characters; get_nearby)", lines)


class Remembered(unittest.TestCase):
    def test_remembered_uses_mod_keys_and_is_capped(self):
        from bg3ai import render
        mem = [{"uuid": U(950 + i), "name": f"Npc {i}", "last_pos": [1.0, 2.0, 3.0], "ago_s": 10 * i}
               for i in range(8)]
        text = render.render_nearby({"characters": [], "items": [], "remembered": mem}, Handles())
        self.assertNotIn("None", text)
        self.assertNotIn("?s ago", text)
        self.assertEqual(text.count("REMEMBERED"), render.MAX_REMEMBERED)
        self.assertIn("last seen @(1,2,3) 10s ago", text)
        self.assertIn("(+3 more remembered out of sight: Npc 5, Npc 6, Npc 7)", text)


class Lookalikes(unittest.TestCase):
    def test_plain_lookalikes_are_one_line(self):
        """S026 08:55: 25 Candle(s) filled get_nearby and '+10 more items' hid the puzzle's Buttons."""
        from bg3ai import render
        items = [{"uuid": U(600 + i), "name": "Candles", "kind": "usable", "dist": 4 + i} for i in range(40)]
        items += [{"uuid": U(700), "name": "Button", "kind": "usable", "dist": 20},
                  {"uuid": U(701), "name": "Candle", "kind": "item", "dist": 5},
                  {"uuid": U(702), "name": "Candle", "kind": "item", "dist": 6, "locked": True}]
        text = render.render_nearby({"characters": [], "items": items}, Handles())
        self.assertIn("Candles <usable> 4m (+39 more Candles up to 43m)", text)
        self.assertIn("Button <usable> 20m", text)
        self.assertEqual(text.count("Candles"), 2)  # one line, named twice
        self.assertIn("Candle <item> 6m locked", text)  # a flag sets it apart
        self.assertNotIn("more items", text)
        two = render.render_nearby({"characters": [], "items": items[:2]}, Handles())
        self.assertEqual(two.count("ITEM"), 2)  # below SAME_MIN: listed one by one

    def test_name_search_lists_every_lookalike(self):
        """S029 16:12: get_nearby kinds="Mystic Thurible" folded 6 thuribles into one line; the agent had to cast at
        each and guessed their ids with 22 look_at calls (~3 min)."""
        from unittest import mock
        from bg3ai import server
        items = [{"uuid": U(1600 + i), "name": "Mystic Thurible", "kind": "usable", "dist": 9 + i} for i in range(6)]
        items += [{"uuid": U(1700 + i), "name": "Candles", "kind": "usable", "dist": 3 + i} for i in range(5)]
        with mock.patch.object(server, "_read1", return_value={"characters": [], "items": items}), \
                mock.patch.object(server, "_finish", lambda name, args, text, t0: text):  # no live game events
            text = server.get_nearby(radius=60, kinds="Mystic Thurible")
            props = server.get_nearby(radius=60, kinds="props")
        self.assertEqual(text.count("Mystic Thurible <usable>"), 6, text)
        self.assertNotIn("more Mystic Thurible", text)
        self.assertNotIn("Candles", text)
        self.assertIn("(+4 more Candles", props)  # a props search still folds

    def test_item_searches_drop_unrelated_remembered_characters(self):
        """S035 19:37: get_nearby kinds="umbral,gem" / "item" listed 4 REMEMBERED Self-Same mirrors every time."""
        from unittest import mock
        from bg3ai import server
        items = [{"uuid": U(1800), "name": "Umbral Transporter", "kind": "usable", "dist": 0.9}]
        mem = [{"uuid": U(1801), "name": "Karlach", "last_pos": [1, 2, 3], "ago_s": 140},
               {"uuid": U(1802), "name": "Umbral Horror", "last_pos": [4, 5, 6], "ago_s": 30}]
        with mock.patch.object(server, "_read1", return_value={"characters": [], "items": items, "remembered": mem}), \
                mock.patch.object(server, "_finish", lambda name, args, text, t0: text):  # no live game events
            words = server.get_nearby(radius=40, kinds="umbral,gem")
            kinds = server.get_nearby(radius=40, kinds="items")
            chars = server.get_nearby(radius=40, kinds="characters")
            plain = server.get_nearby(radius=40)
        self.assertIn("Umbral Transporter", words)
        self.assertIn("REMEMBERED", words)  # the remembered one the search names
        self.assertIn("Umbral Horror", words)
        self.assertNotIn("Karlach", words)
        self.assertNotIn("REMEMBERED", kinds)
        self.assertIn("Karlach", chars)
        self.assertIn("Karlach", plain)


    def test_name_search_with_no_match_says_so(self):
        """S042 22:51: get_nearby kinds="Jaheira,characters" answered "Nothing visible in range." (she was not there)."""
        from unittest import mock
        from bg3ai import server
        chars = [{"uuid": U(1900), "name": "Isobel", "dist": 5}, {"uuid": U(1901), "name": "Harper Donner", "dist": 8}]
        with mock.patch.object(server, "_read1", return_value={"characters": chars, "items": []}), \
                mock.patch.object(server, "_finish", lambda name, args, text, t0: text):
            text = server.get_nearby(radius=60, kinds="Jaheira,characters")
            hit = server.get_nearby(radius=60, kinds="isobel")
        self.assertIn("No 'jaheira' in view within 60m", text)
        self.assertIn("Characters in view:", text)
        self.assertIn("Isobel", text); self.assertIn("Harper Donner", text)
        self.assertNotIn("Nothing visible", text)
        self.assertIn("Isobel", hit); self.assertNotIn("No 'isobel'", hit)

    def test_name_search_finds_party_members(self):
        """S057 07:49 / S058 07:52: kinds="Shadowheart,Wyll" → "No 'shadowheart', 'wyll' in view" ×4 while both lay dead
        nearby (state.nearby lists only others); the agent took screenshots to find the bodies for a Revivify."""
        from unittest import mock
        from bg3ai import server
        st = {"active": U(2101), "party": [
            {"uuid": U(2101), "name": "Claude", "hp": 41, "max_hp": 157, "pos": [0, 0, 0]},
            {"uuid": U(2102), "name": "Shadowheart", "hp": 0, "max_hp": 132, "dead": True, "pos": [-5, 0, 0]},
            {"uuid": U(2103), "name": "Karlach", "hp": 5, "max_hp": 177, "pos": [0, 0, 3]}]}
        chars = [{"uuid": U(2104), "name": "Patriar", "dist": 5}]
        real = server.game.try_rpc
        with mock.patch.object(server, "_read1", return_value={"characters": chars, "items": []}), \
                mock.patch.object(server.game, "try_rpc", lambda op, *a, **k: st if op == "state.status" else real(op, *a, **k)), \
                mock.patch.object(server, "_finish", lambda name, args, text, t0: text):
            text = server.get_nearby(radius=25, kinds="Shadowheart,Wyll")
            both = server.get_nearby(radius=25, kinds="karlach,patriar")
            door = server.get_nearby(radius=25, kinds="door")
        sh = server.H.get(U(2102), "Shadowheart")
        self.assertIn(f"PARTY {sh} Shadowheart dead (its body) 5m W (your party: target it by this id)", text)
        self.assertNotIn("No 'shadowheart'", text)
        self.assertIn("PARTY", both); self.assertIn("HP 5/177", both); self.assertIn("Patriar", both)
        self.assertNotIn("PARTY", door)

    def test_breakable_usable_shows_its_hp(self):
        """S044 23:51: the colony's Membrane (6 HP in the game's hover) read only as 'Viscera <usable>'."""
        from bg3ai import render
        H = Handles()
        it = {"uuid": U(2000), "name": "Viscera", "kind": "usable", "dist": 1.7, "hp": 6, "max_hp": 6}
        self.assertIn("Viscera <usable> 1.7m HP 6/6", render.render_nearby({"characters": [], "items": [it]}, H))
        old = delta.snapshot(H, nearby={"characters": [], "items": []})
        new = delta.snapshot(H, nearby={"characters": [], "items": [it]})
        self.assertIn("HP 6/6", "\n".join(delta.diff(old, new, H)))

class Fixtures(unittest.TestCase):
    """S025/S026 Grand Mausoleum: the Buttons under the paintings were the puzzle; ~23 min lost over two sessions."""

    def test_near_is_shown_and_ranked_first(self):
        from bg3ai import render
        items = [{"uuid": U(800 + i), "name": f"Bottle {i}", "kind": "item", "dist": 1 + i} for i in range(30)]
        items += [{"uuid": U(830), "name": "Button", "kind": "usable", "dist": 12, "near": "under Moonrise Towers"},
                  {"uuid": U(831), "name": "Marble Plate", "kind": "object", "dist": 13, "near": "by Statue"},
                  {"uuid": U(832), "name": "Rock", "kind": "object", "dist": 2}]
        text = render.render_nearby({"characters": [], "items": items}, Handles(), max_items=5)
        self.assertIn("Button <usable> 12m (under Moonrise Towers)", text)
        self.assertIn("Marble Plate <object> 13m (by Statue)", text)  # an object, but a fixture: listed
        self.assertNotIn("Rock", text)

    def test_delta_fixture_is_not_a_prop(self):
        H = Handles()
        old = delta.snapshot(H, nearby={"characters": [], "items": []})
        items = [{"uuid": U(900 + i), "name": "Candles", "kind": "usable", "dist": 1 + i} for i in range(20)]
        items += [{"uuid": U(930), "name": "Marble Plate", "kind": "object", "dist": 3, "near": "under Grief"}]
        new = delta.snapshot(H, nearby={"characters": [], "items": items})
        text = "\n".join(delta.diff(old, new, H))
        self.assertIn("Marble Plate <object> 3m (under Grief)", text)
        self.assertIn("props 20", text)

    def test_get_nearby_kinds_words(self):
        self.assertEqual(server._nearby_kinds("doors,usable"), (["door", "usable"], []))
        mod, words = server._nearby_kinds("Buttons,props")
        self.assertEqual(words, ["buttons", "props"])
        self.assertEqual(mod, ["characters", "items", "object"])
        res = {"characters": [{"name": "Buttonmaker"}, {"name": "Isobel"}],
               "items": [{"name": "Button", "kind": "usable"}, {"name": "Candles", "kind": "usable"},
                         {"name": "Grief", "kind": "usable"}], "more_items": 3}
        got = server._nearby_match(res, words)
        self.assertEqual([i["name"] for i in got["items"]], ["Button", "Candles"])
        self.assertEqual([c["name"] for c in got["characters"]], ["Buttonmaker"])
        self.assertNotIn("more_items", got)


def sizes() -> list[tuple[str, int, int, int]]:
    """(scenario, chars of the old way: get_status + get_nearby, chars of look(full=True), chars of look())."""
    rows = []
    for name, step in SCENARIOS:
        fake = FakeGame(base_world())
        server.game = fake
        server.DELTA.reset()
        server.look()
        step(fake.w)
        old = len(server.get_status(full=True)) + len(server.get_nearby())
        server.DELTA.reset()
        fresh = FakeGame(base_world())
        server.game = fresh
        server.look()
        step(fresh.w)
        d = server.look()
        full = len(server.look(full=True))
        rows.append((name, old, full, len(d)))
    return rows


if __name__ == "__main__" and "--sizes" in sys.argv:
    print(f"{'scenario':18} {'status+nearby':>14} {'look full':>10} {'look delta':>11}   (chars; ~tokens = chars/4)")
    for name, old, full, d in sizes():
        print(f"{name:18} {old:>14} {full:>10} {d:>11}   ~{old // 4} -> ~{d // 4} tok")
elif __name__ == "__main__":
    unittest.main()
