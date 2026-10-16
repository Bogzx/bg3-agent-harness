"""compact.py: tool results said once and short, with no fact lost (research/impl-vis.md "Observation size").

Golden input: bridge/tests/golden/naut_results.jsonl.gz, every text tool result of the three naut sessions
(runtime/sessions/naut*/S001.jsonl, extracted by tools/vis/obs_size.py extract)."""
from __future__ import annotations

import gzip
import json
import re
import sys
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bg3ai import compact, compact_check as cc  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "naut_results.jsonl.gz"


def golden() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out[r["session"]].append(r)
    return dict(out)


def fresh(t=0.0) -> compact.Compactor:
    now = {"t": t}
    c = compact.Compactor(clock=lambda: now["t"])
    c.now = now
    return c


class GoldenReplay(unittest.TestCase):
    """The naut transcripts replayed: every fact survives, and the results shrink."""

    @classmethod
    def setUpClass(cls):
        cls.sessions = {s: cc.replay(rs) for s, rs in golden().items()}

    def test_three_sessions(self):
        self.assertEqual(sorted(self.sessions), ["naut-r2/S001", "naut-r3/S001", "naut/S001"])

    def test_no_fact_lost(self):
        for s, rows in self.sessions.items():
            bad = [(i, r["tool"], r["problems"]) for i, r in enumerate(rows) if r["problems"]]
            self.assertEqual(bad, [], s)

    def test_game_results_shrink_at_least_23_percent(self):
        for s, rows in self.sessions.items():
            game = [r for r in rows if r["tool"] not in compact.PROSE_TOOLS]
            a = sum(cc.est_tokens(r["orig"]) for r in game)
            b = sum(cc.est_tokens(r["out"]) for r in game)
            self.assertLess(b, a * 0.77, f"{s}: {a} -> {b}")

    def test_all_results_shrink_at_least_17_percent(self):
        for s, rows in self.sessions.items():
            a = sum(cc.est_tokens(r["orig"]) for r in rows)
            b = sum(cc.est_tokens(r["out"]) for r in rows)
            self.assertLess(b, a * 0.83, f"{s}: {a} -> {b}")

    def test_prose_untouched_but_the_clock(self):
        for rows in self.sessions.values():
            for r in rows:
                if r["tool"] == "memory_read":
                    self.assertEqual(re.sub(r"\n⏱[^\n]*$", "", r["orig"]).rstrip(),
                                     re.sub(r"\n⏱[^\n]*$", "", r["out"]).rstrip())

    def test_off_is_identity(self):
        self.addCleanup(setattr, compact, "COMPACT", compact.COMPACT)
        compact.COMPACT = False
        rows = cc.replay(golden()["naut/S001"])
        self.assertTrue(all(r["out"] == r["orig"] for r in rows))


class CheckerCatchesLosses(unittest.TestCase):
    """The fact checker is the safety net: it must flag a real loss."""

    TEXT = ("CHANGES\n  e2 Brannoch Vael moved 6m SW, HP 24->20/24, +Burning\n  ~ e68 Lesser Hellsboar DEAD\n"
            "EVENTS\n  damage target={e70 name=Lesser Imp} source={e2 name=Brannoch Vael} type=Fire amount=5\n"
            "⏱ 16:39 · 24m left")

    def test_clean_compaction_passes(self):
        c = fresh()
        out = c.apply("combat_turn", self.TEXT)
        self.assertEqual(cc.check(self.TEXT, out, c.elided, ""), [])

    def test_dropped_line_is_caught(self):
        out = compact.Compactor().apply("combat_turn", self.TEXT).replace("  ~ e68 Lesser Hellsboar DEAD\n", "")
        self.assertTrue(cc.check(self.TEXT, out, [], ""))

    def test_changed_number_is_caught(self):
        out = compact.Compactor().apply("combat_turn", self.TEXT).replace("24->20", "24->21")
        self.assertTrue(cc.check(self.TEXT, out, [], ""))

    def test_moved_binding_is_caught(self):
        # the 5 Fire damage attributed to the wrong line: atoms all present, binding not
        orig = "  e70 Lesser Imp HP 4->2/4\n  e71 Lesser Imp HP 4/4"
        out = "  e70 Lesser Imp HP 4/4\n  e71 Lesser Imp HP 4->2/4 x"
        self.assertEqual(cc.check(orig, orig, [], ""), [])
        self.assertTrue(any("binding" in p for p in cc.check(orig, out.replace("4->2/4 x", "4->2/4"), [], "")) or
                        cc.check(orig, out, [], ""))

    def test_elided_text_must_have_been_shown(self):
        self.assertTrue(cc.check("ALERTS gold 0", "ALERTS (unchanged)", ["ALERTS gold 0"], "nothing like it"))
        self.assertEqual(cc.check("ALERTS gold 0", "ALERTS (unchanged)", ["ALERTS gold 0"], "x\nALERTS gold 0\n"), [])


class Rules(unittest.TestCase):
    def test_refs_both_orders_and_bare(self):
        self.assertEqual(compact.refs("item={name=Sphincter e23} char={e1 name=Orsen Vale}"),
                         "item=e23 Sphincter char=e1 Orsen Vale")
        self.assertEqual(compact.refs("  turn_started name=Lae'zel e29; name=Orsen Vale e1"),
                         "  turn_started e29 Lae'zel; e1 Orsen Vale")
        self.assertEqual(compact.refs("target={e31}"), "target=e31")
        # a grouped status line keeps its shape (",", ":" end a name)
        line = "  status_removed name=Threatened on 3: Lesser Imp e71, Orsen Vale e1, Lae'zel e29"
        self.assertEqual(compact.refs(line), line)

    def test_empties(self):
        self.assertEqual(compact.empties("  damage target=x type= amount=0"), "  damage target=x amount=0")
        self.assertEqual(compact.empties("  combat_started []"), "  combat_started")

    def test_event_templates(self):
        n = compact._Names()
        self.assertEqual(compact.event_line("  damage target=e2 Brannoch Vael source=e34 Imp type=Fire amount=3", n),
                         "  damage e34 Imp -> e2 Brannoch Vael 3 Fire")
        self.assertEqual(compact.event_line("  cast caster=e34 Imp spell=Projectile_Generic_Imp roll=attack "
                                            "name=Fiery Bolt", n), "  cast e34: Fiery Bolt (attack)")
        self.assertEqual(compact.event_line("  used item=e22 Sphincter char=e2 Brannoch Vael success", n),
                         "  used e2: e22 Sphincter success")
        self.assertEqual(compact.event_line("  status_removed target=e28 Lae'zel name=Threatened", n),
                         "  status_removed e28 Lae'zel: Threatened")
        self.assertEqual(compact.event_line("  item_added item=e10 Potion count=2 owner=e1 Orsen", n),
                         "  item_added e1 Orsen: e10 Potion count=2")
        self.assertEqual(compact.event_line("  round_started round=2", n), "  round_started 2")
        self.assertEqual(compact.event_line("  turn_ended e28; e2", n), "  turn_ended e28; e2")
        odd = "  job_finished @(-79,18,-390) op=act.move_to dist=0 status=done job=j94"
        self.assertEqual(compact.event_line(odd, n), odd)  # unknown kinds stay as they are

    def test_events_already_in_changes_and_names_from_changes(self):
        text = ("CHANGES\n  e2 Corran Vell moved 8m W, +Threatened\n  ~ e31 Imp DEAD\nEVENTS\n"
                "  status_applied target={e2 name=Corran Vell} name=Threatened\n  died name=Imp e31\n"
                "  damage target={e2 name=Corran Vell} source={e9 name=Imp} type=Fire amount=3")
        out = fresh().apply("combat_turn", text)
        self.assertNotIn("status_applied", out)
        self.assertNotIn("died", out)
        self.assertIn("  damage e9 Imp -> e2 3 Fire", out)

    def test_hints_full_once_then_short(self):
        c = fresh()
        pace = ("PACE: you waited right after issuing. Next time first do independent work (queue the arrival steps, "
                "look, plan, notes): the character acts while you think.")
        self.assertIn(pace, c.apply("wait_for", pace))
        self.assertEqual(c.apply("wait_for", pace), "PACE: you waited right after issuing (do independent work first).")

    def test_hint_short_forms_keep_their_groups(self):
        for key, pat, short in compact.HINT_RULES:
            for g in re.compile(pat).groupindex:
                self.assertIn(f"\\g<{g}>", short, key)

    def test_whole_line_hint_leaves_no_empty_line(self):
        c = fresh()
        nxt = compact.HINT_RULES[1][1].replace("\\", "")
        text = f"travel_to: started\n{nxt}\nRUNNING queue"
        c.apply("travel_to", text)
        self.assertEqual(c.apply("travel_to", text), "travel_to: started\nRUNNING queue")

    def test_static_lines_unchanged_then_again_after_reshow(self):
        c = fresh(1000.0)
        line = "PARTY: no healer in the party | no ranged damage in the party"
        self.assertIn(line, c.apply("look", "WHERE x\n" + line))
        self.assertIn("PARTY: (unchanged)", c.apply("look", "WHERE x\n" + line))
        self.assertEqual(c.elided, [line])
        c.now["t"] += compact.STATIC_RESHOW_S + 1
        self.assertIn(line, c.apply("look", "WHERE x\n" + line))
        self.assertIn(line + " | x", c.apply("look", "WHERE x\n" + line + " | x"))  # changed: in full

    def test_clock_only_when_it_changed(self):
        c = fresh()
        self.assertIn("⏱ 16:38 · 25m left", c.apply("look", "a\n⏱ 16:38 · 25m left"))
        self.assertEqual(c.apply("look", "b\n⏱ 16:38 · 25m left"), "b")
        self.assertIn("⏱ 16:39 · 24m left", c.apply("look", "c\n⏱ 16:39 · 24m left"))
        wrap = "⏱ 16:39 · 3m left: WRAP UP NOW. Reach a safe spot, save_game, then follow x."
        self.assertIn(wrap, c.apply("look", "d\n" + wrap))
        self.assertIn(wrap, c.apply("look", "e\n" + wrap))  # never dropped

    def test_layout_legend_once(self):
        c = fresh()
        line = "LAYOUT 10.5m around you (@), N up, 1 char = 1.5m; " + "# wall  . floor  ^ higher floor"
        self.assertIn("# wall", c.apply("get_layout", line + "\n|..|"))
        self.assertEqual(c.apply("get_layout", line + "\n|..|"),
                         "LAYOUT 10.5m around you (@), N up, 1 char = 1.5m; key as before\n|..|")

    def test_dialogue_line_already_quoted(self):
        c = fresh()
        out = c.apply("dialogue_advance", "DIALOGUE Lae'zel: Tsk'va. You are no thrall.\n  1. Who are you?\n"
                                          "EVENTS\n  dialogue_line speaker=Lae'zel text=Tsk'va. You are no thrall.\n"
                                          "  dialogue_line speaker=Lae'zel text=Abomination.")
        self.assertEqual(out.count("Tsk'va"), 1)
        self.assertIn("text=Abomination.", out)
        out = c.apply("get_dialogue", "EVENTS\n  dialogue_line speaker=Lae'zel text=Tsk'va. You are no thrall.")
        self.assertEqual(out, "")
        self.assertEqual(c.elided, ["Tsk'va. You are no thrall."])

    def test_quest_text_once(self):
        c = fresh()
        q = "We met a githyanki named Lae'zel. She told us we can escape."
        c.apply("note", f"EVENTS\n  quest_update text={q} title=Escape")
        self.assertEqual(c.apply("look", f"QUEST Escape: {q}"), "QUEST Escape: (text as before)")

    def test_actions_list_again(self):
        c = fresh()
        acts = "\n".join(f"Action {i} [A] 1.5m" for i in range(9))
        text = f"YOUR ACTIONS (e2)\nNOW Action 1/1 · Bonus 1/1\n{acts}\nEVENTS\n  x"
        self.assertIn("Action 8", c.apply("combat_turn", text))
        out = c.apply("combat_turn", text)
        self.assertNotIn("Action 8", out)
        self.assertIn("NOW Action 1/1", out)
        self.assertIn("(actions as listed before)", out)
        self.assertIn("Action 8", c.apply("combat_turn", text.replace("(e2)", "(e3)")))  # per member

    def test_combat_row(self):
        row = ("  initiative=8 hp_pct=87 e86 race=Cambion @(-53.5,17,-391.6) name=Commander Zhalk "
               "from={e2=[10,1] e28=[7.4,0]} acted relation=hostile")
        self.assertEqual(compact.combat_row(compact.coords(row)),
                         "  e86 Commander Zhalk (Cambion) hostile init 8 hp 87% acted @(-54,17,-392) "
                         "from e2 10m, e28 7.4m no-LOS")
        folded = "  is_current relation=party e29 Lae'zel @(-45,3,-404) initiative=4 can_act from={e1=[1.6,1]} hp_pct=100"
        self.assertEqual(compact.combat_row(folded),
                         "  e29 Lae'zel party init 4 hp 100% current can_act @(-45,3,-404) from e1 1.6m")

    def test_reported_positions_round_offered_targets_do_not(self):
        line = "move_to: done — @(-36.6,15.2,-432.3); closest walkable spot (-46.2,5.1,-361.8)"
        self.assertEqual(compact.coords(line), "move_to: done — @(-37,15,-432); closest walkable spot (-46.2,5.1,-361.8)")

    def test_harness_fields(self):
        lines = ["action: X e1: done — charged_by_guard=[action 1] damage=[e1 Imp 9 Slashing]",
                 "leg 1: move_to: done — @(-10,17,-424) dist=0.2", "leg 2: move_to: done — @(-23,16,-430) dist=0.6"]
        self.assertEqual(compact.harness_fields(lines),
                         ["action: X e1: done — damage=[e1 Imp 9 Slashing]", "leg 1: move_to: done —",
                          "leg 2: move_to: done — @(-23,16,-430) dist=0.6"])

    def test_dup_fields(self):
        self.assertEqual(compact.dup_fields("move e32: done — in_reach target=e32 dist=1.5"),
                         "move e32: done — in_reach dist=1.5")
        self.assertEqual(compact.dup_fields("action: A e32: done — target={e32 dead} total_damage=9 "
                                            "damage=[e32 Imp 9 Slashing]"),
                         "action: A e32: done — target=e32 dead damage=[e32 Imp 9 Slashing]")
        two = "x total_damage=4 damage=[e1 Imp 0; e1 Imp 4 Fire]"
        self.assertEqual(compact.dup_fields(two), two)  # the total says something the list doesn't

    def test_numbers(self):
        self.assertEqual(compact.numbers("NOW e29 hp=24 max_hp=24 action=0"), "NOW e29 hp=24/24 action=0")
        self.assertEqual(compact.numbers("charged_by_guard=[resource=bonus action amount=1]"),
                         "charged_by_guard=[bonus action 1]")
        self.assertEqual(compact.numbers("damage=[target=e33 type=Slashing name=Imp amount=7]"),
                         "damage=[e33 Imp 7 Slashing]")

    def test_groups(self):
        self.assertEqual(compact.gone_group("  GONE e15 Mind Flayer, e16 Pod, e18 Pod"),
                         "  GONE e15 Mind Flayer, e16/e18 Pod")
        lines = ["  + e80 Dead Thrall neutral dead 6m", "  + e70 Imp hostile HP 4/4 7m",
                 "  + e82 Dead Thrall neutral dead 9.5m", "  + e84 Dead Thrall neutral dead 10.4m"]
        self.assertEqual(compact._same_group(lines), ["  + Dead Thrall neutral dead: e80 6m, e82 9.5m, e84 10.4m",
                                                      "  + e70 Imp hostile HP 4/4 7m"])
        self.assertEqual(compact.path_legs(["  1. 17.6m W", "  2. 19.5m W", "  3. 6.6m W", "  4. 10m NW"]),
                         ["  1-3. 17.6m, 19.5m, 6.6m W", "  4. 10m NW"])

    def test_prose_tools_keep_their_text(self):
        c = fresh()
        md = "# Title\n\nitem_added x= \n- a\n- a\n⏱ 16:38 · 25m left"
        self.assertEqual(c.apply("memory_read", md), md)
        self.assertEqual(c.apply("memory_read", md), md.rsplit("\n", 1)[0])  # the repeated clock only


class ServerWiring(unittest.TestCase):
    def test_every_tool_is_compacted_and_plain(self):
        from bg3ai import server
        tools = server.mcp._tool_manager.list_tools()
        self.assertGreater(len(tools), 50)
        for t in tools:
            self.assertTrue(getattr(t.fn, "__compacted__", False), t.name)
            if server.PLAIN_RESULTS:
                self.assertIsNone(t.output_schema, t.name)

    def test_plain_text_result_has_no_structured_copy(self):
        from bg3ai import server
        tool = server.mcp._tool_manager.get_tool("look")
        res = tool.fn_metadata.convert_result("WHERE x")
        self.assertEqual(res.content[0].text, "WHERE x")
        self.assertIsNone(res.structured_content)

    def test_briefing_is_not_compacted(self):
        from bg3ai import server
        brief = "BRIEFING\nitem_added x= \n⏱ 16:38 · 25m left"
        body = "look\n  combat_started []"
        fn = server._compacted("look", lambda: brief + server.BRIEFING_SEP + body)
        self.assertEqual(fn(), brief + server.BRIEFING_SEP + "look\n  combat_started")


if __name__ == "__main__":
    unittest.main()
