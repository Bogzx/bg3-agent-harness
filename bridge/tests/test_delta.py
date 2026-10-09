"""Offline tests: delta observations (look, get_status/get_map diffs, the action hook) on a fake game.

Run: cd bridge && uv run python -m unittest discover -s tests
Sizes: cd bridge && uv run python tests/test_delta.py --sizes   (full vs delta output per scenario)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from typing import Any

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-"))

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
