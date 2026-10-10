"""Offline tests: camp roster, PARTY hint and swap_party (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import companions, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TABLE = companions.load_ties((REPO / "play/knowledge/companions.md").read_text())

CLAUDE = "11111111-0000-0000-0000-000000000001"
SH = "11111111-0000-0000-0000-000000000002"
GALE = "11111111-0000-0000-0000-000000000003"
KARLACH = "11111111-0000-0000-0000-000000000004"
HALSIN = "11111111-0000-0000-0000-000000000005"
WYLL = "11111111-0000-0000-0000-000000000006"


def row(u, name, cls, lvl=5, **kw):
    return dict({"uuid": u, "name": name, "level": lvl, "classes": [{"class": cls, "level": lvl}]}, **kw)


PARTY = [row(CLAUDE, "Claude", "Paladin", avatar=True), row(SH, "Shadowheart", "Cleric"),
         row(GALE, "Gale", "Wizard"), row(KARLACH, "Karlach", "Barbarian")]
CAMP = [row(WYLL, "Wyll", "Warlock", 3, level_up=4), row(HALSIN, "Halsin", "Druid")]


class Table(unittest.TestCase):
    def test_knowledge_table_parses(self):
        self.assertIn("halsin", TABLE)
        self.assertIn("Shadow-Cursed Lands", TABLE["halsin"]["regions"])
        self.assertIn("Lift the Shadow Curse", TABLE["halsin"]["quests"])
        self.assertIn("lae'zel", TABLE)
        self.assertNotIn("companion", TABLE)

    def test_matches_region_and_quest(self):
        self.assertEqual(companions.matches(TABLE["halsin"], "Shadow-Cursed Lands", []), ["Shadow-Cursed Lands"])
        self.assertEqual(companions.matches(TABLE["karlach"], "Wilderness", ["Our Fiery Friend"]),
                         ['quest "Our Fiery Friend"'])
        self.assertEqual(companions.matches(TABLE["gale"], "Wilderness", ["Rescue the Grand Duke"]), [])


class Talk(unittest.TestCase):
    """The "!" over a companion (State.lua wants_talk) as a TALK line; S023/S026 screenshotted it."""

    def lab(self, u: str) -> str:
        return {"w": "e5 Wyll", "g": "e3 Gale"}.get(u, u)

    def test_party_and_camp(self) -> None:
        st = {"party": [{"uuid": "g", "wants_talk": True}, {"uuid": "c"}], "camp": [{"uuid": "w", "wants_talk": True}]}
        t = companions.TalkTracker()
        text = t.line(st, self.lab)
        self.assertIn("TALK: e3 Gale wants to talk", text)
        self.assertIn("e5 Wyll (at camp) wants to talk", text)
        self.assertEqual(t.line(st, self.lab), "")             # unchanged: said once
        self.assertTrue(t.line(st, self.lab, force=True))      # full view repeats it

    def test_quiet_in_combat_and_without(self) -> None:
        t = companions.TalkTracker()
        self.assertEqual(t.line({"party": [{"uuid": "g", "wants_talk": True}], "in_combat": True}, self.lab), "")
        self.assertEqual(t.line({"party": [{"uuid": "g"}]}, self.lab), "")

    def test_row_flag(self) -> None:
        from bg3ai import render
        H = render.Handles()
        line = render.character_line({"uuid": "11111111-2222-3333-4444-555555555555", "name": "Wyll", "wants_talk": True}, H)
        self.assertIn("wants to talk(!)", line)


class Hint(unittest.TestCase):
    def test_halsin_tied_to_scl(self):
        bits = companions.hint(PARTY, CAMP, "Shadow-Cursed Lands", ["Lift the Shadow Curse"], TABLE)
        self.assertTrue(bits[0].startswith("Halsin (camp, Druid 5) is tied to Shadow-Cursed Lands"), bits)
        # Shadowheart is tied to the SCL too (Gauntlet of Shar) and Claude is the avatar: not suggested out.
        self.assertNotIn('out="Shadowheart"', bits[0])
        self.assertNotIn('out="Claude"', bits[0])
        self.assertIn('into="Halsin"', bits[0])
        self.assertIn("camp level-ups: Wyll L3→4 → level_up(who=\"Wyll\")", bits[-1])

    def test_nothing_in_the_wilderness_but_level_ups(self):
        bits = companions.hint(PARTY, [row(HALSIN, "Halsin", "Druid")], "Wilderness", [], TABLE)
        self.assertEqual(bits, [])

    def test_composition_hole_and_skill_gap(self):
        party = [PARTY[0], PARTY[2], PARTY[3]]  # no healer
        camp = [row(SH, "Shadowheart", "Cleric"), row(WYLL, "Astarion", "Rogue")]
        skills = {CLAUDE: {"SleightOfHand": 0, "Persuasion": 6, "Perception": 3},
                  GALE: {"SleightOfHand": 1, "Perception": 1}, KARLACH: {"SleightOfHand": 1, "Perception": 4},
                  WYLL: {"SleightOfHand": 7}, SH: {}}
        bits = companions.hint(party, camp, "Wilderness", [], TABLE, skills=skills)
        self.assertIn("no healer in the party: Shadowheart (camp)", bits)
        self.assertTrue(any("Sleight of Hand" in b and "Astarion (camp) +7" in b for b in bits), bits)
        self.assertFalse(any("Persuasion" in b for b in bits))

    def test_tracker_prints_on_change_only(self):
        t = companions.HintTracker()
        self.assertTrue(t.line(["a"]).startswith("PARTY: a"))
        self.assertEqual(t.line(["a"]), "")
        self.assertTrue(t.line(["a"], force=True))
        self.assertTrue(t.need_skills(PARTY, CAMP))
        self.assertFalse(t.need_skills(PARTY, CAMP))

    def test_camp_lines(self):
        lines = companions.camp_lines([dict(CAMP[0], can_reach=5, skills={"Persuasion": 7, "Arcana": 2},
                                            equipment={"MeleeMainHand": {"name": "Rapier"}, "Helmet": {"name": "Cap"}})])
        self.assertIn("Wyll (camp) Warlock 3, level up pending → L5", lines[0])
        self.assertIn("Persuasion +7", lines[0])
        self.assertIn("MeleeMainHand=Rapier", lines[0])
        self.assertNotIn("Helmet", lines[0])


class Pickers(unittest.TestCase):
    FULL = [{"i": 1, "text": "True. Meet me at camp."}, {"i": 2, "text": "Gale could do with a rest."},
            {"i": 3, "text": "Take Shadowheart's place."}, {"i": 4, "text": "You can trade places with Wyll."},
            {"i": 5, "text": "I'll be back."}]

    def test_swap_menu(self):
        self.assertEqual(companions.pick_swap(self.FULL, "Shadowheart"), 3)
        self.assertEqual(companions.pick_swap(self.FULL, "Gale"), 2)
        self.assertEqual(companions.pick_swap(self.FULL, "Wyll"), 4)
        self.assertIsNone(companions.pick_swap(self.FULL, "Karlach"))
        self.assertIsNone(companions.pick_join(self.FULL))

    def test_join_not_dismiss(self):
        opts = [{"i": 1, "text": "I think I have a magical artefact you'll be interested in."},
                {"i": 2, "text": "I need you to remain in the camp for a while, rather than on the road with me."},
                {"i": 3, "text": "Leave."}]
        self.assertIsNone(companions.pick_join(opts))
        self.assertEqual(companions.pick_dismiss(opts), 2)
        join = [{"i": 1, "text": "How are you holding up?"}, {"i": 2, "text": "I'd like you to join me."},
                {"i": 3, "text": "Leave."}]
        self.assertEqual(companions.pick_join(join), 2)
        self.assertIsNone(companions.pick_join([{"i": 1, "text": "[PERSUASION] Join me.", "tags": ["PERSUASION"]}]))
        # live 2026-10-10, Halsin's camp menu: a chat question that says "with us"
        self.assertIsNone(companions.pick_join([{"i": 1, "text": "Still feeling at home here with us? The "
                                                 "surroundings are less bucolic, I must admit."}]))
        gale = [{"i": 1, "text": "Is Mystra always such a demanding goddess?"},
                {"i": 4, "text": "Join me, why don't you? There's plenty of adventure to be had."}, {"i": 5, "text": "Leave."}]
        self.assertEqual(companions.pick_join(gale), 4)   # live 2026-10-10
        wyll = [{"i": 4, "text": "How in the Hells did you get involved with Mizora?"},
                {"i": 5, "text": "I'd like you to join me."}, {"i": 6, "text": "Leave."}]
        self.assertEqual(companions.pick_join(wyll), 5)   # live 2026-10-10


class FakeGame:
    """Camp swap: status before/after, state.camp, a scripted dialogue."""

    def __init__(self, dialogue: list[dict], at_camp: bool = False, joins: bool = True):
        self.dialogue = list(dialogue)
        self.at_camp = at_camp
        self.joins = joins
        self.swapped = False
        self.far = False
        self.talking = at_camp  # at_camp tests: a dialogue already up is the companion's (test sets it so)
        self.jobs: list[tuple[str, dict]] = []

    def party(self):
        rows = [dict(r) for r in PARTY]
        if self.swapped:
            rows = [r for r in rows if r["uuid"] != GALE] + [dict(CAMP[1])]
        return rows

    def rpc(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        if op == "state.status":
            return {"active": CLAUDE, "party": self.party(), "camp": [] if self.swapped else [dict(c) for c in CAMP],
                    "region": "Shadow-Cursed Lands"}
        if op == "state.camp":
            return {"party": self.party(), "camp": [dict(c) for c in CAMP]}
        if op == "ui.state":
            if self.dialogue and self.talking:
                return {"screen": "dialogue", "dialogue": self.dialogue[0], "region": "Campsite" if self.at_camp else "x"}
            return {"screen": "game", "region": "Campsite" if self.at_camp else "x"}
        raise AssertionError(f"unexpected op {op}")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        return self.rpc(op, args, context)

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0, **kw) -> JobResult:
        self.jobs.append((op, args))
        if op == "act.go_to_camp":
            self.at_camp = True
        if op == "act.talk":
            if self.far:
                return JobResult(status="failed", error="character is too far away (66m from this character, max 40m)")
            self.talking = True
        if op == "act.move_to":
            self.far = False
        if op == "ui.dialogue_choose":
            self.dialogue.pop(0)
            if not self.dialogue and self.joins:
                self.swapped = True
        return JobResult(status="done", result={})

    def drain_events(self, ctx: str) -> list:
        return []


class SwapParty(unittest.TestCase):
    def use(self, fake: FakeGame) -> None:
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        real_sleep = server.time.sleep  # captured before patching: restoring time.sleep itself here
        server.time.sleep = lambda s: None  # left it a no-op for every later module (test_saver failed)
        self.addCleanup(setattr, server.time, "sleep", real_sleep)

    def test_already_at_camp_refusal_is_not_a_failure(self):
        """Live 11:14: the level-up page hid the camp from ui.state; go_to_camp answered 'already at camp'."""
        menu = {"options": [{"i": 2, "text": "I'd like you to join me."}, {"i": 3, "text": "Leave."}]}
        full = {"options": Pickers.FULL[:1] + [{"i": 2, "text": "Gale could do with a rest."}]}
        fake = FakeGame([menu, full])
        real = fake.run_job

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            if op == "act.go_to_camp":
                fake.jobs.append((op, args))
                fake.at_camp = True
                return JobResult(status="refused", error="already at camp")
            return real(op, args, context, wait_s, **kw)
        fake.run_job = run_job
        self.use(fake)
        out = server.swap_party(into="Halsin", out="Gale", leave_camp=False)
        self.assertIn("swap_party: done", out)
        self.assertNotIn("FAILED at go to camp", out)

    def test_full_swap_from_the_field(self):
        menu = {"options": [{"i": 1, "text": "What do you think of Gale?"}, {"i": 2, "text": "I'd like you to join me."},
                            {"i": 3, "text": "Leave."}]}
        full = {"options": Pickers.FULL[:1] + [{"i": 2, "text": "Gale could do with a rest."}]}
        fake = FakeGame([menu, full])
        self.use(fake)
        out = server.swap_party(into="Halsin", out="Gale")
        ops = [j[0] for j in fake.jobs]
        self.assertEqual(ops[:2], ["act.go_to_camp", "act.talk"])
        self.assertIn(("ui.dialogue_choose", {"i": 2}), fake.jobs)
        self.assertEqual([j for j in fake.jobs if j[0] == "ui.dialogue_choose"], [("ui.dialogue_choose", {"i": 2})] * 2)
        self.assertIn("act.leave_camp", ops)
        self.assertIn("swap_party: done", out)
        self.assertIn("Halsin joined", out)
        self.assertIn("Gale waits at camp", out)

    def test_walks_up_to_a_companion_past_talk_range(self):
        # live 2026-10-10: Halsin stood 66 m from the camp arrival point, out of view
        menu = {"options": [{"i": 1, "text": "I'd like you to join me."}]}
        full = {"options": [{"i": 2, "text": "Gale could do with a rest."}]}
        fake = FakeGame([menu, full])
        fake.far = True
        self.use(fake)
        orig = server._far_move
        server._far_move = lambda *a: None
        self.addCleanup(setattr, server, "_far_move", orig)
        out = server.swap_party(into="Halsin", out="Gale")
        ops = [j[0] for j in fake.jobs]
        self.assertEqual(ops[:4], ["act.go_to_camp", "act.talk", "act.move_to", "act.talk"])
        self.assertIn("walked to Halsin", out)
        self.assertIn("swap_party: done", out)

    def test_far_walk_up_stops_short_of_the_companion(self):
        # acceptance 12:2x: the 59 m walk to Wyll ended on his spot, then a 3.4 m second leg; stop short instead
        seen = {}
        orig = (server._far_move, server._ent_pos, server.travel_to, server.wait_for)
        self.addCleanup(lambda: (setattr(server, "_far_move", orig[0]), setattr(server, "_ent_pos", orig[1]),
                                 setattr(server, "travel_to", orig[2]), setattr(server, "wait_for", orig[3])))
        server._far_move = lambda *a: ([60.0, 1.0, 0.0], 60.0)
        server._ent_pos = lambda u: [0.0, 1.0, 0.0]
        server.travel_to = lambda **kw: seen.update(kw) or "travel_to: started (58m)"
        server.wait_for = lambda **kw: "idle"
        out = server._walk_up_to("me", "wyll", "Wyll", [60.0, 1.0, 0.0])
        self.assertAlmostEqual(seen["x"], 60.0 - server.WALK_UP_SHORT_M)
        self.assertEqual((seen["y"], seen["z"]), (1.0, 0.0))
        self.assertIn("walked to Wyll (60m)", out)

    def test_no_join_answer_leaves_and_mutes_the_hint(self):
        # live 2026-10-10 SCL camp: Halsin's menu had no join answer at all
        menu = {"options": [{"i": 1, "text": "How will the grove fare without you?"},
                            {"i": 2, "text": "Still feeling at home here with us? The surroundings are less bucolic"},
                            {"i": 3, "text": "Leave."}]}
        fake = FakeGame([menu], joins=False)
        self.use(fake)
        self.addCleanup(server._no_join.clear)
        out = server.swap_party(into="Halsin", out="Gale")
        self.assertIn("doesn't offer to join right now", out)
        self.assertIn(("ui.dialogue_choose", {"i": 3}), fake.jobs)
        self.assertIn("act.leave_camp", [j[0] for j in fake.jobs])
        self.assertIn(HALSIN, server._no_join)

    def test_full_party_needs_out(self):
        self.use(FakeGame([]))
        out = server.swap_party(into="Halsin")
        self.assertIn("the party is full", out)
        self.assertIn('into="Halsin", out="', out)
        self.assertNotIn('out="Claude"', out)

    def test_refuses_avatar_and_unknown(self):
        self.use(FakeGame([]))
        self.assertIn("your own character", server.swap_party(into="Halsin", out="Claude"))
        self.assertIn("not waiting at camp", server.swap_party(into="Minsc", out="Gale"))
        self.assertIn("already in the party", server.swap_party(into="Gale", out="Karlach"))

    def test_unknown_answer_stops_with_dialogue(self):
        fake = FakeGame([{"options": [{"i": 1, "text": "Tell me about the curse."}, {"i": 2, "text": "Hmm."}]}],
                        at_camp=True, joins=False)
        self.use(fake)
        out = server.swap_party(into="Halsin", out="Gale")
        self.assertIn("STOPPED: no join/swap answer recognised", out)
        self.assertNotIn("act.go_to_camp", [j[0] for j in fake.jobs])


class ReachLine(unittest.TestCase):
    def test_buckets(self):
        from bg3ai import turn
        cs = {"order": [{"uuid": "h1", "relation": "hostile", "from": {"me": [7.6, 1]}},
                        {"uuid": "h2", "relation": "hostile", "from": {"me": [12.0, 1]}},
                        {"uuid": "h3", "relation": "hostile", "from": {"me": [25.0, 0]}},
                        {"uuid": "h4", "relation": "hostile", "dead": True, "from": {"me": [1.0, 1]}},
                        {"uuid": "p1", "relation": "party", "from": {"me": [1.0, 1]}}]}
        ln = turn.reach_line(cs, "me", 9, lambda u: u)
        self.assertEqual(ln, "REACH me (9m move, ≈straight line): melee this turn h1 7.6m | with Dash h2 12m"
                             " | farther (ranged only) h3 25m")
        self.assertEqual(turn.reach_line(cs, "me", None, str), "")


if __name__ == "__main__":
    unittest.main()
