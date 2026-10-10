"""Offline tests: LEVEL UP line, nudge, stored level-up plans (bg3ai.leveling) and the server wiring.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import time
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

    def test_new_level_up_said_at_once(self):
        # S010: Gale's L3 came 4 min after Wyll's was shown; it must not wait out the 5-min timer
        n = LV.Nudge(every_s=300)
        n.observe({"party": [{"uuid": "u-w", "name": "Wyll", "level": 2, "level_up": 3}]})
        n.text(now=1000, shown=True)
        n.observe({"party": [{"uuid": "u-w", "name": "Wyll", "level": 2, "level_up": 3},
                             {"uuid": "u-gale", "name": "Gale", "level": 2, "level_up": 3}]})
        self.assertIn("Gale (3)", n.text(now=1010))
        self.assertEqual(n.text(now=1020), "")

    def test_stale(self):
        n = LV.Nudge(refresh_s=60)
        self.assertTrue(n.stale(now=100))
        n.observe(ST, now=100)
        self.assertFalse(n.stale(now=150))
        self.assertTrue(n.stale(now=161))


RUN2 = {"party": [   # claude-run-2 after S010: no rogue, no plan lines, no composition section
    {"uuid": "e1", "name": "Wyll", "level": 3, "classes": [{"class": "Warlock", "level": 3, "subclass": "The Fiend"}]},
    {"uuid": "e2", "name": "Gale", "level": 3, "classes": [{"class": "Wizard", "level": 3}]},
    {"uuid": "e3", "name": "Shadowheart", "level": 3, "classes": [{"class": "Cleric", "level": 3}]},
    {"uuid": "e4", "name": "Claude", "level": 3, "classes": [{"class": "Paladin", "level": 3}]},
]}


class PlanHint(unittest.TestCase):
    def setUp(self):
        self.mem = Path(tempfile.mkdtemp(prefix="bg3lv-"))
        (self.mem / "party-build.md").write_text("# Party build\n## Gale\n- Wizard, Evocation\n")

    def test_composition(self):
        self.assertEqual(LV.composition_gaps(RUN2["party"]), ["skills"])
        self.assertEqual(LV.composition_gaps([{"name": "X", "level": 1}]), [])   # classes unknown: no claim

    def test_hint(self):
        n = LV.Nudge()
        n.observe(RUN2)
        h = n.plan_hint(self.mem, now=1000)
        self.assertTrue(h.startswith("PARTY PLAN"), h)
        for want in ("Wyll L4", "Gale L5", "Claude L4", "Astarion"):
            self.assertIn(want, h)
        self.assertEqual(n.plan_hint(self.mem, now=1100), "")      # at most every HINT_S
        lines = "".join(f'- {m["name"]} L{lv}: {{}}\n' for m in RUN2["party"] for lv in (4, 5))
        (self.mem / "party-build.md").write_text("## Party composition\n- skills: Astarion next\n"
                                                 "## Level-up plans\n" + lines)
        self.assertEqual(n.plan_hint(self.mem, now=5000), "")      # all written: silent
        n.observe(dict(RUN2, in_combat=True))
        self.assertEqual(LV.Nudge().plan_hint(self.mem, now=0), "")  # nothing observed yet

    def test_level_12_cap(self):
        n = LV.Nudge()
        n.observe({"party": [{"uuid": "a", "name": "A", "level": 12, "classes": [{"class": "Rogue"}]}]})
        self.assertNotIn("A L13", n.plan_hint(self.mem, now=0))


class FakeSpellPage:
    """The level-up spell grid as ui.level_up_spell_probe and the input shim see it: `names` in order, 8 columns
    of 44 px buttons (1920x1080) whose first centre is at `origin`; a click on a button picks its spell."""

    def __init__(self, names, origin=(326.0, 475.0), cell=44.0, cols=8, open_slots=2, switch_frames=1,
                 rows_visible=None, near_margin=None):
        self.names, self.origin, self.cell, self.cols = list(names), origin, cell, cols
        self.picked: list[str] = []
        self.open = open_slots
        self.cursor = (0, 0)
        self.switch = switch_frames   # probes that answer "switched" first (the sub-tab appears)
        self.moves = 0
        self.rows_visible = rows_visible   # a scroll viewer showing this many rows (None: all)
        self.scroll = 0.0                  # px scrolled down
        self.near_margin = near_margin     # px around the list where the probe says `near` (None: never)
        self.wheels: list[int] = []
        self.intro = False                 # the "Level Up!" intro is up: panels off-screen, nothing hoverable
        self.intro_clicks: list[tuple[int, int]] = []

    def wheel(self, notches, x=None, y=None, sync=True):
        self.wheels.append(notches)
        if self.rows_visible is None:
            return
        rows = -(-len(self.visible()) // self.cols)
        top = max(0.0, (rows - self.rows_visible) * self.cell)
        self.scroll = min(top, max(0.0, self.scroll - notches * self.cell * 0.75))

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
        if self.intro:
            self.intro = False
            self.intro_clicks.append((x, y))
            return
        k = self.under()
        if k and self.open > 0:
            self.picked.append(self.visible()[k - 1])
            self.open -= 1

    # page
    def visible(self):
        return [n for n in self.names if n not in self.picked]

    def under(self):
        if self.intro:
            return None
        x, y = self.cursor
        top = self.origin[1] - self.cell / 2
        if self.rows_visible is not None and not (top <= y < top + self.rows_visible * self.cell):
            return None
        col = (x - (self.origin[0] - self.cell / 2)) // self.cell
        row = (y + self.scroll - top) // self.cell
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
        out = {"index": idx, "n": len(self.visible()), "cols": self.cols, "cell": {"w": 88.0, "h": 88.0},
               "over": h == idx, "hovered": h, "enabled": self.open > 0, "open": self.open,
               "step": "Intro" if self.intro else "IntroComplete"}
        if self.near_margin is not None:
            m, (x, y) = self.near_margin, self.cursor
            rows = self.rows_visible or -(-len(self.visible()) // self.cols)
            x0, y0 = self.origin[0] - self.cell / 2, self.origin[1] - self.cell / 2
            if x0 - m <= x < x0 + self.cols * self.cell + m and y0 - m <= y < y0 + rows * self.cell + m:
                out["near"] = 3
        return out


NAMES = ["Burning Hands", "Charm Person", "Chromatic Orb", "Colour Spray", "Disguise Self", "Expeditious Retreat",
         "False Life", "Feather Fall", "Find Familiar", "Ice Knife", "Enhance Leap", "Longstrider", "Magic Missile",
         "Protection from Evil and Good", "Ray of Sickness", "Shield", "Tasha's Hideous Laughter"]


class SpellClicks(unittest.TestCase):
    """click_spells: hover-search the grid, measure one button, click each pick (hover-checked), confirm it took."""

    def setUp(self):
        self._memo = LV.MEMO
        LV.MEMO = None
        LV._last_hit.clear()

    def tearDown(self):
        LV.MEMO = self._memo
        LV._last_hit.clear()

    def test_intro_skipped_before_search(self):
        # S021: the page sat on its "Level Up!" intro, so 226-365 probes never hovered a button
        page = FakeSpellPage(NAMES, switch_frames=0)
        page.intro = True
        r = LV.click_spells(page.rpc, page, [{"name": "Shield"}, {"name": "Magic Missile"}])
        self.assertEqual(r["failed"], {})
        self.assertEqual(sorted(r["picked"]), ["Magic Missile", "Shield"])
        self.assertEqual(page.intro_clicks, [(960, 918)])

    def test_far_list_found_through_near(self):
        # S018: the list far from where Gale's L2 page had it. `near` (over the list's scroll viewer) lets the
        # coarse search take big steps; without it the fine search alone would need hundreds of probes.
        pg = FakeSpellPage(NAMES[:6], origin=(1500.0, 260.0), cols=6, near_margin=120)
        r = LV.click_spells(pg.rpc, pg, [{"name": NAMES[3], "cantrips": True}])
        self.assertEqual(r["failed"], {})
        self.assertLess(r["probes"], 120)

    def test_scrolled_out_of_view(self):
        # a long list in a scroll viewer showing 3 rows: the target on row 5 is wheeled into view
        names = [f"Spell {i}" for i in range(1, 31)]
        pg = FakeSpellPage(names, cols=6, rows_visible=3)
        r = LV.click_spells(pg.rpc, pg, [{"name": "Spell 27"}])
        self.assertEqual((r["failed"], pg.picked), ({}, ["Spell 27"]))
        self.assertTrue(pg.wheels and all(n < 0 for n in pg.wheels))

    def test_out_of_time(self):
        # nothing hoverable: it gives up at the budget, not after 400+ probes (S018: 110 s, the MCP timeout)
        pg = FakeSpellPage(NAMES, origin=(-500.0, -500.0))
        t = [0.0]

        def clock():
            t[0] += 0.28   # one probe's worth of time per look at the clock
            return t[0]
        c = LV.SpellClicker(pg.rpc, pg, budget_s=10.0, clock=clock)
        with self.assertRaises(LV.SpellClickError) as e:
            c.pick({"name": "Shield"})
        self.assertIn("out of time", str(e.exception))
        self.assertLess(c.probes, 60)

    def test_last_hit_remembered(self):
        pg = FakeSpellPage(NAMES, origin=(1200.0, 800.0))
        r1 = LV.click_spells(pg.rpc, pg, [{"name": "Shield"}])
        pg2 = FakeSpellPage(NAMES, origin=(1200.0, 800.0))
        r2 = LV.click_spells(pg2.rpc, pg2, [{"name": "Shield"}])
        self.assertEqual((r1["failed"], r2["failed"]), ({}, {}))
        self.assertLess(r2["probes"], r1["probes"])

    def test_memo_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            LV.MEMO = Path(d) / "levelup_clicks.json"
            pg = FakeSpellPage(NAMES, origin=(1200.0, 800.0))
            LV.click_spells(pg.rpc, pg, [{"name": "Shield"}])
            self.assertTrue(LV.MEMO.exists())
            LV._last_hit.clear()
            pg2 = FakeSpellPage(NAMES, origin=(1200.0, 800.0))
            r2 = LV.click_spells(pg2.rpc, pg2, [{"name": "Shield"}])
            self.assertEqual(r2["failed"], {})
            self.assertLess(r2["probes"], 30)

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


class ServerRetryAndRefresh(unittest.TestCase):
    def test_unrendered_subclass_labels_retry_once(self):
        from bg3ai import server as S

        calls = []

        def fake_act(tool, op, args, wait_s=25.0, context="server", wait=True, note=""):
            calls.append(op)
            S._batch["result"] = {}
            if len(calls) == 1:
                return "level_up: FAILED — subclass: no 'Evocation'; choices: #1, #2, #3 — stage=subclass"
            return "level_up: done — finished complete"

        saved = (S._act, S._who)
        S._act, S._who = fake_act, (lambda ref: "u-gale")
        try:
            out = S.level_up("Gale", plan='{"subclass": "Evocation", "spells": ["Sleep"]}')
            self.assertEqual((out, len(calls)), ("level_up: done — finished complete", 2))
        finally:
            S._act, S._who = saved

    def test_finish_refreshes_status_for_a_new_level_up(self):
        from bg3ai import server as S

        reads = []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                reads.append(op)
                return ST if op == "state.status" else default

        saved = (S.game, S._LEVEL_NUDGE)
        S.game, S._LEVEL_NUDGE = G(), LV.Nudge()
        try:
            out = S._finish("save_game", {}, "save_game: done", time.monotonic(), events=False)
            self.assertIn("LEVEL UP pending: Gale (2)", out)
            n = reads.count("state.status")
            S._finish("save_game", {}, "save_game: done", time.monotonic(), events=False)
            self.assertEqual(reads.count("state.status"), n)    # fresh: no second read within REFRESH_S
        finally:
            S.game, S._LEVEL_NUDGE = saved
