"""Offline tests for tools/dev/arena_reference.py (no game): the page plans made from a build's script, and that the
respec and dress steps stop at the first thing they can't trust.
Run: uv run pytest -q tools/dev/test_arena_reference.py"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arena_reference as ar
import make_arena_save as mas
import probe_route_h as rh

STATIC = mas.load_static()
REPO = Path(__file__).resolve().parents[2]


def build(level: int, bid: str) -> dict[str, Any]:
    d = json.loads((REPO / f"packages/bench/builds/l{level}_4v4_v1.json").read_text())
    return next(b for b in d["builds"] if b["id"] == bid)


class Job:
    def __init__(self, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.status, self.result, self.error = status, result, error


class FakeGame:
    def __init__(self, jobs: list[Job], levels: list[dict[str, Any]] | None = None) -> None:
        self.jobs, self.finished, self.levels = jobs, 0, levels

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        return json.dumps(self.levels)

    def run_job(self, op: str, args: dict[str, Any], context: str = "server", wait_s: float = 0.0) -> Job:
        return self.jobs.pop(0)

    def rpc(self, op: str, args: dict[str, Any], context: str = "server") -> Any:
        self.finished += 1
        return {"finished": True}


class FakeRoute:
    def __init__(
        self,
        opens: bool = True,
        deny: tuple[str, ...] = (),
        inventory: bool = True,
        applies: bool = True,
        items_go: bool = True,
    ) -> None:
        self.opens, self.deny, self.inventory, self.sent = opens, deny, inventory, []
        self.applies, self.items_go, self.left = applies, items_go, 2

    def ev(self, code: str, context: str = "server") -> Any:
        self.sent.append(code)
        if "Ext.Stats.Get" in code:
            return {"Target_Bless": "Bless", "FightingStyle_Defense": "Defense", "PrecisionAttack": "Precision"}
        for fn in self.deny:
            if fn in code:
                return {"ok": False, "error": "refused"}
        if "GetItemByTemplateInInventory" in code:
            return {"ok": True, "ret": "item-1" if self.inventory else None}
        if code == rh.PRE:
            return {"mark": 7}
        if code == rh.PAGES:
            return [] if self.applies else ["CharacterRespec"]
        if code == rh.BOX_READ:
            return {"box": False}
        if "Osi.RequestDelete" not in code and "deleted = n" in code:  # the count: one read later they're gone
            self.left = self.left if not self.items_go else max(self.left - 1, 0)
            return {"deleted": self.left}
        return {"ok": True, "deleted": 2}

    def page_open(self, widget: str) -> tuple[bool, Any]:
        return self.opens, [widget] if self.opens else []

    def events(self, mark: int) -> list[dict[str, Any]]:
        self.mark = mark
        return [{"e": "RespecCompleted", "args": ["u1"]}] if self.applies else []

    def wait(self, read: Any, done: Any, wait_s: float) -> tuple[bool, Any]:
        for _ in range(4):
            r = read()
            if done(r):
                return True, r
        return False, r


def live(game: FakeGame, route: FakeRoute, level_up: Any = None, click: Any = None, prepare: Any = None) -> ar.Live:
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s

    return ar.Live(
        route,
        game,
        level_up or (lambda who, plan: "level_up: done"),
        click or (lambda needs: {"picked": [n["name"] for n in needs], "failed": {}}),
        prepare or (lambda who, add: "prepare_spells: done"),
        sleep,
        lambda: t[0],
    )


PLANS = {"cc": {"class": "Fighter", "passives": ["FightingStyle_Defense"]}, "level_ups": [{"level": 2, "plan": {}}],
         "names": ["FightingStyle_Defense"]}  # fmt: skip
DONE = {"complete": True, "problems": []}


class PlanTest(unittest.TestCase):
    def test_a_fighters_plan_holds_each_level_and_its_choices(self):
        p = ar.ui_plans(build(5, "fighter"), 5, STATIC)
        self.assertEqual(p["cc"]["class"], "Fighter")
        self.assertEqual(p["cc"]["bonus"], ["Strength", "Constitution"])
        self.assertEqual(p["cc"]["passives"], ["FightingStyle_Defense"])
        self.assertNotIn("subclass", p["cc"])
        ups = {u["level"]: u["plan"] for u in p["level_ups"]}
        self.assertEqual(sorted(ups), [2, 3, 4, 5])
        self.assertEqual(ups[3]["subclass"], "Battle Master")
        self.assertEqual(ups[3]["passives"], ["PrecisionAttack", "Riposte", "GoadingAttack"])
        self.assertEqual(ups[4]["abilities"], {"Strength": 2})
        self.assertIn("PrecisionAttack", p["names"])

    def test_a_cleric_picks_its_domain_on_the_respec_page_and_its_cantrips_are_cantrips(self):
        p = ar.ui_plans(build(2, "cleric"), 2, STATIC)
        self.assertEqual(p["cc"]["subclass"], "Life")
        self.assertEqual(p["cc"]["cantrips"], ["Target_SacredFlame", "Target_TollTheDead", "Target_Guidance",
                                               "Projectile_FireBolt"])  # fmt: skip
        self.assertNotIn("spells", p["cc"])

    def test_a_wizards_spellbook_is_spells_and_its_school_a_level_up_pick(self):
        p = ar.ui_plans(build(5, "wizard"), 5, STATIC)
        self.assertEqual(len(p["cc"]["spells"]), 6)
        self.assertEqual(len(p["cc"]["cantrips"]), 3)
        self.assertEqual(next(u["plan"] for u in p["level_ups"] if u["level"] == 2)["subclass"], "Evocation")

    def test_a_rogues_expertise_is_its_own_pick_not_a_second_class_skill(self):
        cc = ar.ui_plans(build(2, "rogue"), 2, STATIC)["cc"]
        script = build(2, "rogue")["script"]["cc"]
        self.assertEqual(cc["expertise"], script["expertise"])
        self.assertEqual((cc["class_skills"], cc["race_skills"]), (script["skills"], script["race_skills"]))

    def test_a_bards_college_and_expertise_are_level_3_picks(self):
        ups = {u["level"]: u["plan"] for u in ar.ui_plans(build(5, "bard"), 5, STATIC)["level_ups"]}
        self.assertEqual(ups[3]["subclass"], "Lore")
        self.assertEqual(ups[3]["expertise"], ["Persuasion", "Performance"])
        self.assertEqual(ups[3]["skills"], ["Arcana", "Perception", "Deception"])

    def test_every_tier_build_has_a_plan(self):
        for lv in (2, 5, 12):
            for bid in ("fighter", "cleric" if lv == 2 else "bard", "wizard", "rogue"):
                with self.subTest(level=lv, build=bid):
                    self.assertEqual(len(ar.ui_plans(build(lv, bid), lv, STATIC)["level_ups"]), lv - 1)

    def test_the_l2_fallback_team_has_plans_with_the_rangers_picks_at_level_2(self):
        d = json.loads((REPO / "packages/bench/builds/l2_hirelings_v1.json").read_text())
        plans = {b["id"]: ar.ui_plans(b, 2, STATIC) for b in d["builds"]}
        self.assertEqual(sorted(plans), ["barbarian", "fighter", "ranger", "rogue"])
        up = plans["ranger"]["level_ups"][0]["plan"]
        self.assertEqual(up["spells"], ["Target_HuntersMark", "Target_CureWounds"])
        self.assertEqual(up["passives"], ["FightingStyle_Archery"])

    def test_an_unlisted_subclass_is_refused_and_a_name_the_game_gave_none_too(self):
        b = build(5, "fighter") | {"subclass": "Champion"}
        with self.assertRaisesRegex(ar.RefStepError, "no page label for subclass Champion"):
            ar.ui_plans(b, 5, STATIC)
        with self.assertRaisesRegex(ar.RefStepError, "no display name read for X"):
            ar.translate({"spells": ["X"]}, {"X": ""})


class RespecTest(unittest.TestCase):
    def test_a_stop_keeps_what_the_page_opened_with(self):
        before = {"skills": {"class": ["Stealth"]}}
        g = FakeGame([Job("done", {"problems": ["passive Bounty Hunter: not offered"], "before": before})])
        with self.assertRaises(ar.RefStepError) as stop:
            live(g, FakeRoute()).respec(PLANS, "u1")
        self.assertEqual((stop.exception.log or {})["cc"]["before"], before)
        self.assertEqual(g.finished, 0)

    def test_the_rangers_plan_names_its_two_lists_in_the_rows_order(self):
        doc = json.loads((REPO / "packages/bench/builds/l2_hirelings_v1.json").read_text())
        ranger = next(b for b in doc["builds"] if b["class"] == "Ranger")
        cc = ar.ui_plans(ranger, doc["level"], STATIC)["cc"]
        self.assertEqual([x[:8] for x in cc["passive_lists"]], ["d0f13dda", "72c63024"])

    def test_the_whole_flow_with_spell_clicks_applies_again_finishes_and_levels(self):
        g = FakeGame([Job("done", {"needs_clicks": [{"name": "Bless"}]}), Job("done", DONE)])
        ups: list[dict[str, Any]] = []
        r = FakeRoute()
        log = live(g, r, level_up=lambda w, p: ups.append(p) or "level_up: done").respec(PLANS, "u1")
        self.assertEqual(log["clicked"][0]["picked"], ["Bless"])
        self.assertEqual(g.finished, 1)
        self.assertEqual(len(ups), 1)
        self.assertEqual((r.mark, log["events"][0]["e"]), (7, "RespecCompleted"))

    def test_a_pick_the_page_offers_only_after_the_first_clicks_gets_a_second_pass(self):
        off, on = {"name": "Sleep", "remove": True}, {"name": "Bless"}
        g = FakeGame([Job("done", {"needs_clicks": [off]}), Job("done", {"needs_clicks": [on]}), Job("done", DONE)])
        seen: list[Any] = []
        log = live(g, FakeRoute(), click=lambda n: seen.append(n) or {"picked": [], "failed": {}}).respec(PLANS, "u1")
        self.assertEqual(seen, [[off], [on]])
        self.assertEqual((len(log["clicked"]), g.finished), (2, 1))

    def test_clicks_the_page_keeps_asking_for_stop_it_before_the_finish(self):
        need = {"needs_clicks": [{"name": "Bless"}], "complete": True, "problems": []}
        g = FakeGame([Job("done", need) for _ in range(ar.CLICK_PASSES + 1)])
        with self.assertRaisesRegex(ar.RefStepError, "still open after 3 passes"):
            live(g, FakeRoute()).respec(PLANS, "u1")
        self.assertEqual(g.finished, 0)

    def test_no_level_up_before_the_respec_has_landed(self):
        ups: list[Any] = []
        g = FakeGame([Job("done", DONE)])
        with self.assertRaisesRegex(ar.RefStepError, "respec not applied"):
            live(g, FakeRoute(applies=False), level_up=lambda w, p: ups.append(p) or "x").respec(PLANS, "u1")
        self.assertEqual((g.finished, ups), (1, []))

    def test_a_level_up_that_says_anything_but_done_stops_it(self):
        g = FakeGame([Job("done", DONE)])
        with self.assertRaisesRegex(ar.RefStepError, "nothing to do"):
            live(g, FakeRoute(), level_up=lambda w, p: "level_up: nothing to do").respec(PLANS, "u1")

    def test_an_incomplete_page_says_what_it_bought(self):
        res = {"complete": False, "problems": [], "set": {"abilities": {"points_left": 6}}}
        with self.assertRaisesRegex(ar.RefStepError, "points_left': 6"):
            live(FakeGame([Job("done", res)]), FakeRoute()).respec(PLANS, "u1")

    def test_it_stops_at_a_page_that_doesnt_open_a_failed_job_problems_clicks_and_level_ups(self):
        with self.assertRaisesRegex(ar.RefStepError, "didn't open"):
            live(FakeGame([]), FakeRoute(opens=False)).respec(PLANS, "u1")
        with self.assertRaisesRegex(ar.RefStepError, "failed: boom"):
            live(FakeGame([Job("failed", {}, "boom")]), FakeRoute()).respec(PLANS, "u1")
        g = FakeGame([Job("done", {"complete": False, "problems": ["skill: not offered"]})])
        with self.assertRaisesRegex(ar.RefStepError, "not offered"):
            live(g, FakeRoute()).respec(PLANS, "u1")
        self.assertEqual(g.finished, 0)  # never finished
        g = FakeGame([Job("done", {"needs_clicks": [{"name": "Bless"}]})])
        with self.assertRaisesRegex(ar.RefStepError, "picks failed"):
            live(g, FakeRoute(), click=lambda n: {"picked": [], "failed": {"Bless": "no"}}).respec(PLANS, "u1")
        with self.assertRaisesRegex(ar.RefStepError, "level 2"):
            live(FakeGame([Job("done", DONE)]), FakeRoute(), level_up=lambda w, p: "level_up: FAILED x").respec(
                PLANS, "u1"
            )
        with self.assertRaisesRegex(ar.RefStepError, "StartRespec"):
            live(FakeGame([]), FakeRoute(deny=("StartRespec",))).respec(PLANS, "u1")


class ReadLevelsTest(unittest.TestCase):
    def test_it_prints_what_a_level_up_needs(self):
        g = FakeGame([], [{"uuid": "u1", "level": 1, "available": 2, "can_level_up": True}])
        self.assertEqual(ar.level_state(g, ["u1"])[0]["available"], 2)


class SetupCheckTest(unittest.TestCase):
    def test_done_is_0_and_a_failure_is_1_with_its_rows(self):
        rows = [{"pair": 1, "ok": False, "diffs": [{"field": "passives"}]}]
        code, rec = ar.setup_check(FakeGame([Job("failed", {"reference": rows}, "the twins are not their builds")]), {})
        self.assertEqual((code, rec["reference"]), (1, rows))
        self.assertIn("not their builds", rec["error"])
        code, rec = ar.setup_check(
            FakeGame([Job("done", {"xp": {"added": 6072}, "positions": {"Ash": [1, 2, 3]}})]), {}
        )
        self.assertEqual((code, rec["xp"], rec["positions"]), (0, {"added": 6072}, {"Ash": [1, 2, 3]}))

    def test_a_twin_with_a_level_up_pending_after_the_party_join_fails_it(self):
        # S1 read AvailableLevel and CanLevelUp before any join, and Sym.FIELDS doesn't compare them
        teams = {"teams": {"A": ["a1"], "B": ["b1"]}}
        ok = {"uuid": "a1", "level": 2, "available": 2, "can_level_up": False}
        code, rec = ar.setup_check(FakeGame([Job("done", teams)], [ok, ok | {"uuid": "b1"}]), {})
        self.assertEqual((code, len(rec["level_state"])), (0, 2))
        for bad in ({"available": 3}, {"can_level_up": True}):
            code, rec = ar.setup_check(FakeGame([Job("done", teams)], [ok, ok | {"uuid": "b1"} | bad]), {})
            self.assertEqual(code, 1)
            self.assertIn("b1", rec["error"])


KIT = {"items": [{"item": "WPN_X", "root": "r", "count": 1, "equip": True}], "prepared": ["Target_Bless"]}


class DressTest(unittest.TestCase):
    def test_items_out_kit_in_equipped_then_prepared(self):
        r = FakeRoute()
        added: list[Any] = []
        log = live(FakeGame([]), r, prepare=lambda w, add: added.append(add) or "ok").dress(KIT, "u1")
        self.assertEqual(log["items"], ["WPN_X"])
        self.assertEqual(added, [["Bless"]])
        order = [next((f for f in ("RequestDelete", "TemplateAddTo", "Equip") if f in c), "") for c in r.sent]
        self.assertEqual([x for x in order if x], ["RequestDelete", "TemplateAddTo", "Equip"])
        counts = [i for i, c in enumerate(r.sent) if c == ar.ITEMS_LEFT_LUA % ar._lua_str("u1")]
        added_at = next(i for i, c in enumerate(r.sent) if "TemplateAddTo" in c)
        self.assertEqual(len(counts), 2)  # 1 left, then 0
        self.assertLess(counts[-1], added_at)

    def test_items_that_never_go_stop_it_before_anything_is_added(self):
        r = FakeRoute(items_go=False)
        with self.assertRaisesRegex(ar.RefStepError, "didn't go"):
            live(FakeGame([]), r).dress(KIT, "u1")
        self.assertFalse(any("TemplateAddTo" in c for c in r.sent))

    def test_an_item_that_never_arrives_or_a_refused_prepare_stops_it(self):
        with self.assertRaisesRegex(ar.RefStepError, "didn't reach"):
            live(FakeGame([]), FakeRoute(inventory=False)).dress(KIT, "u1")
        with self.assertRaisesRegex(ar.RefStepError, "prepare_spells"):
            live(FakeGame([]), FakeRoute(), prepare=lambda w, a: "prepare_spells: REFUSED no").dress(KIT, "u1")


if __name__ == "__main__":
    unittest.main()
