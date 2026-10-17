"""Offline tests: prepare_spells (A5) — the plan's refusals reach the agent, the spellbook job runs only for a real
change, the result is judged by the server's prepared list afterwards, and get_party / get_status show each caster's
prepared spells and limit. The mod halves have their own tests (test_act_lua.lua: Guard.prep_plan; test_spellbook.lua:
Client/Spellbook.lua).

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_prepare_spells.py'
"""

from __future__ import annotations

import unittest

from test_turn_macros import P1, P2, Base, FakeGame  # noqa: F401 (sets BG3AI_RUNTIME first)

from bg3ai import server, spellprep
from bg3ai.game import JobResult

BLESS = {"id": "Target_Bless", "name": "Bless", "level": 1}
CURE = {"id": "Target_CureWounds", "name": "Cure Wounds", "level": 1}
SHIELD = {"id": "Target_ShieldOfFaith", "name": "Shield of Faith", "level": 1}
SANCT = {"id": "Target_Sanctuary", "name": "Sanctuary", "level": 1}


class PrepGame(FakeGame):
    """FakeGame + the A5 ops: a book per member, the plan as Guard.prep_plan makes it (limit + refusals), and the
    spellbook job flipping the server's list (unless `stuck`)."""

    def __init__(self) -> None:
        super().__init__()
        self.book = {P1: {"prepared": [CURE, SHIELD], "unprepared": [BLESS], "always": [SANCT], "max": 3}}
        self.refused: str | None = None
        self.stuck = False
        self.ui_result: dict = {}

    def _caster(self, u: str) -> dict | None:
        b = self.book.get(u)
        if b is None:
            return None
        return {"uuid": u, "name": "Claude" if u == P1 else "Lae'zel", "max": b["max"], "count": len(b["prepared"]),
                "read": True, "prepared": list(b["prepared"]), "always": list(b["always"])}

    def rpc(self, op, args=None, context="server", timeout=12.0):
        args = args or {}
        if op == "act.prepare_plan":
            self.calls.append((op, args))
            if self.refused:
                return {"refused": self.refused}
            b = self.book[args["char"]]
            find = {r["name"].lower(): r for r in b["prepared"] + b["unprepared"]}
            add = [find[n.lower()] for n in args["add"] if find.get(n.lower()) in b["unprepared"]]
            rem = [find[n.lower()] for n in args["remove"] if find.get(n.lower()) in b["prepared"]]
            notes = [f"{n} is prepared already" for n in args["add"] if find.get(n.lower()) in b["prepared"]]
            return dict(self._caster(args["char"]), add=add, remove=rem, notes=notes, name="Claude")
        if op == "act.prepared":
            self.calls.append((op, args))
            rows = [self._caster(u) for u in (args.get("uuids") or list(self.book))]
            return {"casters": [r for r in rows if r]}
        return super().rpc(op, args, context, timeout)

    def run_job(self, op, args, context="server", wait_s=25.0, poll_s=0.01, on_start=None):
        if op != "ui.prepare_spells":
            return super().run_job(op, args, context, wait_s, poll_s, on_start)
        self.calls.append((op, args))
        b = self.book[P1]
        if not self.stuck:
            for r in args["remove"]:
                b["prepared"] = [x for x in b["prepared"] if x["id"] != r["id"]]
                b["unprepared"].append({"id": r["id"], "name": r["name"], "level": 1})
            for a in args["add"]:
                b["unprepared"] = [x for x in b["unprepared"] if x["id"] != a["id"]]
                b["prepared"].append({"id": a["id"], "name": a["name"], "level": 1})
        return JobResult("done", self.ui_result or {"toggled": [], "before": {"value": 2, "max": 3},
                                                    "after": {"value": 3, "max": 3}, "book": "Paladin"})

    def read(self, server_ops=(), client_ops=()):
        return ([self.try_rpc(op, a) for op, a in server_ops],
                [self.try_rpc(op, a, context="client") for op, a in client_ops])


class Texts(unittest.TestCase):
    def test_names_list_or_comma_string(self):
        self.assertEqual(spellprep.names("Bless, Cure Wounds ,"), ["Bless", "Cure Wounds"])
        self.assertEqual(spellprep.names(["Bless", " "]), ["Bless"])
        self.assertEqual(spellprep.names(None), [])

    def test_caster_and_status_lines(self):
        c = {"name": "Claude", "max": 3, "count": 2, "prepared": [CURE, SHIELD], "always": [SANCT]}
        self.assertEqual(spellprep.caster_text(c), "Claude 2/3: Cure Wounds, Shield of Faith; always: Sanctuary")
        self.assertEqual(spellprep.caster_text(c, always=False, named=False), "2/3: Cure Wounds, Shield of Faith +1 always")
        self.assertTrue(spellprep.status_line([c]).startswith("PREPARED (only these cast; prepare_spells out of combat"))
        self.assertEqual(spellprep.status_line([]), "")


class PrepareSpells(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g = PrepGame()
        server.game = self.g
        server._watch_game = self.g

    def call(self, **kw) -> str:
        return server.prepare_spells.fn(**kw) if hasattr(server.prepare_spells, "fn") else server.prepare_spells(**kw)

    def test_prepares_through_the_spellbook_and_lists_the_server_set(self):
        out = self.call(who="Claude", add=["Bless"])
        self.assertIn("prepare_spells: done — prepared Bless", out)
        self.assertIn("PREPARED Claude 3/3: Cure Wounds, Shield of Faith, Bless; always: Sanctuary", out)
        job = [a for op, a in self.g.calls if op == "ui.prepare_spells"]
        self.assertEqual(job, [{"name": "Claude", "add": [{"id": "Target_Bless", "name": "Bless", "level": 1}],
                                "remove": []}])

    def test_a_loading_screen_is_waited_out_before_the_page(self):
        # S070 21:00:08: pressed K 6 s after a region change with the loading screen up: "the spellbook didn't open"
        seen, real = [], self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.state":
                seen.append(op)
                return {"screen": "loading" if len(seen) <= 3 else "game"}
            if op == "ui.travel_block":
                return {"hud": True}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        self.g.try_rpc = lambda op, args=None, context="server", default=None: rpc(op, args, context)
        real_sleep = server.time.sleep
        server.time.sleep = lambda s: None
        self.addCleanup(setattr, server.time, "sleep", real_sleep)
        out = self.call(who="Claude", add=["Bless"])
        self.assertGreaterEqual(len(seen), 4)  # read until the load ended, then the page
        self.assertIn("prepare_spells: done — prepared Bless", out)
        self.assertIn("for the loading screen to end first", out)
        self.assertEqual(len([1 for op, _ in self.g.calls if op == "ui.prepare_spells"]), 1)

    def test_the_page_gets_each_spells_look_and_twins(self):
        # A7, live L4: the page's cells carry no names; the plan's look + twins ride to the page job
        look = {"icon": "Spell_Enchantment_Bless", "school": "Enchantment", "conc": True, "cost": "Action"}
        self.g.book[P1]["unprepared"] = [dict(BLESS, look=look, twins=0)]
        self.call(who="Claude", add=["Bless"])
        job = [a for op, a in self.g.calls if op == "ui.prepare_spells"][0]
        self.assertEqual(job["add"], [{"id": "Target_Bless", "name": "Bless", "level": 1, "look": look, "twins": 0}])

    def test_an_unasked_change_is_named(self):
        # the page clicked Heroism's cell (a look-alike) instead of nothing: the server's list shows it, and so do we
        self.g.stuck = True
        orig = self.g.run_job

        def wrong_cell(op, args, *a, **kw):
            res = orig(op, args, *a, **kw)
            if op == "ui.prepare_spells":
                b = self.g.book[P1]
                b["prepared"] = [x for x in b["prepared"] if x["id"] != SHIELD["id"]]
            return res
        self.g.run_job = wrong_cell
        saved, spellprep.SETTLE_S = spellprep.SETTLE_S, 0.0
        self.addCleanup(setattr, spellprep, "SETTLE_S", saved)
        out = self.call(who="Claude", add=["Bless"])
        self.assertIn("prepare_spells: FAILED — Bless not prepared", out)
        self.assertIn("ALSO CHANGED (not asked: the spellbook cells carry no names and a look-alike's cell was "
                      "clicked): unprepared Shield of Faith — prepare_spells again to put it back", out)

    def test_refusal_is_said_and_nothing_is_clicked(self):
        self.g.refused = ("over the limit: Claude can prepare 3 spells and has 3 prepared (Bless, Cure Wounds, "
                          "Shield of Faith); remove 1 of them in the same call (remove=[...])")
        out = self.call(who="Claude", add="Command")
        self.assertTrue(out.startswith("prepare_spells: FAILED — over the limit: Claude can prepare 3"), out)
        self.assertFalse([c for c in self.g.calls if c[0] in ("ui.prepare_spells", "act.select")])

    def test_nothing_to_change_opens_no_page(self):
        out = self.call(who="Claude", add="Cure Wounds")
        self.assertIn("prepare_spells: nothing to change (Cure Wounds is prepared already)", out)
        self.assertNotIn("ui.prepare_spells", [c[0] for c in self.g.calls])

    def test_the_server_list_decides_not_the_page(self):
        # the page said done, the server's list did not change: FAILED with the page's own reason when it has one
        self.g.stuck = True
        self.g.ui_result = {"toggled": [{"name": "Bless", "op": "add", "ok": False,
                                         "why": "clicked, but the cell didn't change"}]}
        saved, spellprep.SETTLE_S = spellprep.SETTLE_S, 0.0
        self.addCleanup(setattr, spellprep, "SETTLE_S", saved)
        out = self.call(who="Claude", add=["Bless"])
        self.assertIn("prepare_spells: FAILED — Bless not prepared: clicked, but the cell didn't change", out)
        self.assertIn("PREPARED Claude 2/3: Cure Wounds, Shield of Faith", out)

    def test_swap_removes_first_and_selects_another_member(self):
        self.g.book[P2] = self.g.book.pop(P1)
        self.g.book[P1] = self.g.book[P2]   # the fake's job edits P1's book: the same dict
        out = self.call(who="Lae'zel", add=["Bless"], remove=["Cure Wounds"])
        ops = [c[0] for c in self.g.calls]
        self.assertLess(ops.index("act.select"), ops.index("ui.prepare_spells"), "selected before the page opens")
        self.assertIn("prepare_spells: done — prepared Bless, unprepared Cure Wounds", out)
        self.assertIn("(the spellbook shows the selected character)", out)

    def test_get_party_shows_prepared_and_limit(self):
        out = server.get_party.fn() if hasattr(server.get_party, "fn") else server.get_party()
        self.assertIn("   prepared 2/3: Cure Wounds, Shield of Faith; always: Sanctuary", out)

    def test_get_status_prepared_line_on_a_full_look_and_on_change(self):
        st = server.get_status.fn if hasattr(server.get_status, "fn") else server.get_status
        self.assertIn("PREPARED (only these cast; prepare_spells out of combat changes them): Claude 2/3: Cure Wounds, "
                      "Shield of Faith +1 always", st(full=True))
        self.assertNotIn("PREPARED", st())
        self.call(who="Claude", add=["Bless"])
        self.assertIn("Claude 3/3: Cure Wounds, Shield of Faith, Bless +1 always", st())


if __name__ == "__main__":
    unittest.main()
