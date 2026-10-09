"""Offline tests: LEVEL UP line, nudge, stored level-up plans (bg3ai.leveling) and the server wiring.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bg3ai import leveling as LV

# state.status as the mod returns it now (live 2026-10-10: Gale EocLevel 1, AvailableLevel 2)
ST = {"in_combat": False, "party": [
    {"uuid": "u-claude", "name": "Claude", "level": 2},
    {"uuid": "u-sh", "name": "Shadowheart", "level": 2},
    {"uuid": "u-gale", "name": "Gale", "level": 1, "level_up": 2},
    {"uuid": "u-dead", "name": "Ghost", "level": 1, "level_up": 2, "dead": True},
]}
BUILD = """# Party build
<!-- - Example L2: {"subclass": "x"} -->
## Level-up plans
- Gale L2: {"subclass": "Evocation", "spells": ["Magic Missile", "Shield"]}
- Claude L4: {"feat": "Ability Improvement", "abilities": {"Strength": 2}}
* Shadowheart Level 3: {"spells": ["Spiritual Weapon"]}
- Broken L5: {not json}
"""


def label(u: str) -> str:
    return {"u-gale": "e3 Gale"}.get(u, u)


class Leveling(unittest.TestCase):
    def setUp(self):
        self.mem = Path(tempfile.mkdtemp(prefix="bg3lv-"))
        (self.mem / "party-build.md").write_text(BUILD)

    def test_pending(self):
        self.assertEqual(LV.pending(ST), [{"uuid": "u-gale", "name": "Gale", "to": 2}])
        self.assertEqual(LV.pending(None), [])

    def test_plans(self):
        plans = LV.load_plans(BUILD)
        self.assertEqual(plans[("gale", 2)]["subclass"], "Evocation")
        self.assertEqual(plans[("claude", 4)]["abilities"], {"Strength": 2})
        self.assertIn(("shadowheart", 3), plans)
        self.assertNotIn(("example", 2), plans)        # inside a comment
        self.assertNotIn(("broken", 5), plans)
        self.assertEqual(LV.plan_for(self.mem, "Gale", 2)["spells"], ["Magic Missile", "Shield"])
        self.assertIsNone(LV.plan_for(self.mem, "Gale", 3))
        self.assertIsNone(LV.plan_for(self.mem / "nope", "Gale", 2))

    def test_line(self):
        line = LV.line(ST, label, self.mem)
        self.assertTrue(line.startswith("LEVEL UP: e3 Gale (2) → level_up(who) now"), line)
        self.assertNotIn("no plan yet", line)
        (self.mem / "party-build.md").write_text("")
        self.assertIn("no plan yet for Gale L2", LV.line(ST, label, self.mem))
        self.assertEqual(LV.line(dict(ST, in_combat=True), label, self.mem), "")   # not in combat
        self.assertEqual(LV.line({"party": [{"uuid": "a", "name": "A", "level": 3}]}, label), "")

    def test_nudge(self):
        n = LV.Nudge(every_s=300)
        n.observe(ST)
        self.assertIn("LEVEL UP pending: Gale (2)", n.text(now=1000))
        self.assertEqual(n.text(now=1100), "")                  # at most every 5 min
        self.assertIn("Gale", n.text(now=1301))
        n.text(now=1400, shown=True)                            # get_status printed it: timer restarts
        self.assertEqual(n.text(now=1600), "")
        n.observe(dict(ST, in_combat=True))
        self.assertEqual(n.text(now=5000), "")                  # never in combat
        n.observe(dict(ST, in_dialogue=True))
        self.assertEqual(n.text(now=5000), "")
        n.observe(ST)
        n.done("u-gale")
        self.assertEqual(n.text(now=9000), "")


class FakeSpellPage:
    """The level-up spell grid as ui.level_up_spell_probe and the input shim see it: `names` in order, 8 columns
    of 44 px buttons (1920x1080) whose first centre is at `origin`; a click on a button picks its spell."""

    def __init__(self, names, origin=(326.0, 475.0), cell=44.0, cols=8, open_slots=2, switch_frames=1):
        self.names, self.origin, self.cell, self.cols = list(names), origin, cell, cols
        self.picked: list[str] = []
        self.open = open_slots
        self.cursor = (0, 0)
        self.switch = switch_frames   # probes that answer "switched" first (the sub-tab appears)
        self.moves = 0

    # input shim
    def window_size(self):
        return 1920, 1080

    def move(self, x, y, sync=True):
        self.cursor = (x, y)
        self.moves += 1

    def wait_frames(self, n, sync=True):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        self.cursor = (x, y)
        k = self.under()
        if k and self.open > 0:
            self.picked.append(self.visible()[k - 1])
            self.open -= 1

    # page
    def visible(self):
        return [n for n in self.names if n not in self.picked]

    def under(self):
        x, y = self.cursor
        col = (x - (self.origin[0] - self.cell / 2)) // self.cell
        row = (y - (self.origin[1] - self.cell / 2)) // self.cell
        if col < 0 or col >= self.cols or row < 0:
            return None
        k = int(row * self.cols + col) + 1
        return k if k <= len(self.visible()) else None

    def rpc(self, op, args):
        assert op == "ui.level_up_spell_probe"
        name = args["name"]
        if name in self.picked:
            return {"placed": True}
        if name not in self.names:
            raise RuntimeError(f"spell {name}: not offered")
        if self.switch > 0:
            self.switch -= 1
            return {"switched": True}
        idx = self.visible().index(name) + 1
        h = self.under()
        return {"index": idx, "n": len(self.visible()), "cols": self.cols, "cell": {"w": 88.0, "h": 88.0},
                "over": h == idx, "hovered": h, "enabled": self.open > 0, "open": self.open}


NAMES = ["Burning Hands", "Charm Person", "Chromatic Orb", "Colour Spray", "Disguise Self", "Expeditious Retreat",
         "False Life", "Feather Fall", "Find Familiar", "Ice Knife", "Enhance Leap", "Longstrider", "Magic Missile",
         "Protection from Evil and Good", "Ray of Sickness", "Shield", "Tasha's Hideous Laughter"]


class SpellClicks(unittest.TestCase):
    """click_spells: hover-search the grid, measure one button, click each pick (hover-checked), confirm it took."""

    def test_two_picks(self):
        pg = FakeSpellPage(NAMES)
        r = LV.click_spells(pg.rpc, pg, [{"name": "Magic Missile", "cantrips": False}, {"name": "Shield"}])
        self.assertEqual(r["failed"], {})
        self.assertEqual(pg.picked, ["Magic Missile", "Shield"])
        self.assertLess(r["probes"], 40)

    def test_grid_elsewhere(self):
        # the list further left and lower than the search start, at a fractional offset
        pg = FakeSpellPage(NAMES, origin=(180.5, 700.25))
        r = LV.click_spells(pg.rpc, pg, [{"name": "Shield"}])
        self.assertEqual((r["failed"], pg.picked), ({}, ["Shield"]))

    def test_already_placed_and_unknown(self):
        pg = FakeSpellPage(NAMES)
        pg.picked = ["Shield"]
        r = LV.click_spells(pg.rpc, pg, [{"name": "Shield"}, {"name": "Fireball"}])
        self.assertEqual(r["picked"], ["Shield"])
        self.assertIn("not offered", r["failed"]["Fireball"])

    def test_no_open_slot(self):
        pg = FakeSpellPage(NAMES, open_slots=0)
        r = LV.click_spells(pg.rpc, pg, [{"name": "Shield"}])
        self.assertIn("disabled", r["failed"]["Shield"])

    def test_nothing_under_the_cursor(self):
        pg = FakeSpellPage(NAMES, origin=(-500.0, -500.0))   # never hoverable
        r = LV.click_spells(pg.rpc, pg, [{"name": "Shield"}])
        self.assertIn("no spell button found", r["failed"]["Shield"])
        self.assertEqual(pg.picked, [])

    def test_spiral(self):
        pts = LV.spiral(100, 100, 10, 1920, 1080, limit=9)
        self.assertEqual(pts[0], (100, 100))
        self.assertEqual(len(set(pts)), 9)
        self.assertTrue(all(abs(x - 100) <= 10 and abs(y - 100) <= 10 for x, y in pts))


class ServerWiring(unittest.TestCase):
    """level_up() with no plan sends the stored plan; get_status shows the LEVEL UP line."""

    def test_level_up_uses_stored_plan(self):
        from bg3ai import server as S

        mem = Path(tempfile.mkdtemp(prefix="bg3lv-"))
        (mem / "party-build.md").write_text(BUILD)
        sent = {}

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                return ST if op == "state.status" else default

        def fake_act(tool, op, args, wait_s=25.0, context="server", wait=True, note=""):
            sent.update(op=op, args=args, note=note)
            return "level_up: done"

        saved = (S.game, S._act, S.MEMORY, S._who)
        S.game, S._act, S.MEMORY, S._who = G(), fake_act, mem, (lambda ref: "u-gale")
        try:
            S._LEVEL_NUDGE.observe(ST)
            out = S.level_up("Gale")
            self.assertEqual(out, "level_up: done")
            self.assertEqual(sent["op"], "ui.level_up")
            self.assertEqual(sent["args"], {"uuid": "u-gale", "plan": {"subclass": "Evocation",
                                                                       "spells": ["Magic Missile", "Shield"]}})
            self.assertIn("Gale L2 from memory/party-build.md", sent["note"])
            self.assertEqual(S._LEVEL_NUDGE.rows, [])            # done: no more nudging for Gale
            S.level_up("Gale", plan='{"subclass": "Divination"}')  # an explicit plan wins
            self.assertEqual(sent["args"]["plan"], {"subclass": "Divination"})
            self.assertTrue(S._level_up_line(ST).startswith("LEVEL UP: u-gale (2) → level_up(who)"))
        finally:
            S.game, S._act, S.MEMORY, S._who = saved

    def test_level_up_clicks_spells_then_reruns(self):
        from bg3ai import server as S

        calls = []

        def fake_act(tool, op, args, wait_s=25.0, context="server", wait=True, note=""):
            calls.append(note)
            if len(calls) == 1:
                S._batch["result"] = {"needs_clicks": [{"name": "Hex", "cantrips": False}]}
                return "level_up: FAILED — finish: spell picks need mouse clicks (Hex)"
            S._batch["result"] = {"finished": True}
            return "level_up: done — finished"

        clicked = {}

        def fake_click(rpc, inp, needs, log=None):
            clicked["needs"] = needs
            return {"picked": ["Hex"], "failed": {}, "probes": 12}

        saved = (S._act, S._who, S._inp, LV.click_spells)
        S._act, S._who, S._inp, LV.click_spells = fake_act, (lambda ref: "u-wyll"), (lambda: object()), fake_click
        try:
            out = S.level_up("Wyll", plan='{"spells": ["Hex"], "passives": ["Agonizing Blast"]}')
            self.assertEqual(out, "level_up: done — finished")
            self.assertEqual(clicked["needs"], [{"name": "Hex", "cantrips": False}])
            self.assertEqual(len(calls), 2)
            self.assertIn("clicked spells: Hex", calls[1])
        finally:
            S._act, S._who, S._inp, LV.click_spells = saved


if __name__ == "__main__":
    unittest.main()
