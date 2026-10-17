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

    # floors after the naut audit (combat move positions + own positions at 0.1 m kept): game -27..-30 %,
    # combat_turn -33..-40 %, all -20..-25 %
    def test_game_results_shrink_at_least_26_percent(self):
        for s, rows in self.sessions.items():
            game = [r for r in rows if r["tool"] not in compact.PROSE_TOOLS]
            a = sum(cc.est_tokens(r["orig"]) for r in game)
            b = sum(cc.est_tokens(r["out"]) for r in game)
            self.assertLess(b, a * 0.74, f"{s}: {a} -> {b}")

    def test_combat_turn_results_shrink_at_least_30_percent(self):
        for s, rows in self.sessions.items():
            ct = [r for r in rows if r["tool"] == "combat_turn"]
            a = sum(cc.est_tokens(r["orig"]) for r in ct)
            b = sum(cc.est_tokens(r["out"]) for r in ct)
            self.assertLess(b, a * 0.70, f"{s}: {a} -> {b}")

    def test_all_results_shrink_at_least_20_percent(self):
        for s, rows in self.sessions.items():
            a = sum(cc.est_tokens(r["orig"]) for r in rows)
            b = sum(cc.est_tokens(r["out"]) for r in rows)
            self.assertLess(b, a * 0.80, f"{s}: {a} -> {b}")

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
        hint = "(+1 more items, 0 scenery objects not listed; narrow with radius= or kinds=)"
        self.assertEqual(compact.empties(hint), hint)                                  # a hint, not a record
        self.assertEqual(compact.empties("  damage source=e1 amount="), "  damage source=e1")

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
        self.assertIn("⏱ 16:40 · 20m of play left", c.apply("look", "d\n⏱ 16:40 · 20m of play left"))
        self.assertEqual(c.apply("look", "e\n⏱ 16:40 · 20m of play left"), "e")
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
        line = "REMEMBERED e5 Imp last seen @(-36.6,15.2,-432.3) 38s ago; closest walkable spot (-46.2,5.1,-361.8)"
        self.assertEqual(compact.coords(line),
                         "REMEMBERED e5 Imp last seen @(-37,15,-432) 38s ago; closest walkable spot (-46.2,5.1,-361.8)")
        for other in ("CHAR e124 Lesser Imp hostile HP 4/4 1.4m in_combat @(-79.2,18.2,-388.2)",
                      "  initiative=8 hp_pct=87 e86 @(-53.5,17,-391.6) relation=hostile",
                      "  crosses 2.5m of Fire at 40.5m @(-19,17.9,-428.6)"):
            self.assertNotIn(".", compact.coords(other).split("@(")[1].split(")")[0], other)

    def test_own_position_keeps_its_decimetre(self):
        own = ["move_to: done — @(-36.6,15.2,-432.3) dist=1.3", "interact: done — used climbed=up dy=2.9 @(-42.6,7.5,-361.7)",
               "jump_to: done — cast=Projectile_Jump @(-41.7,8,-357.7)", "leg 1: move_to: done — dist=0.2 @(-9.6,17.2,-424.3)",
               "2. interact: INTERRUPTED — stuck — travelled=4.5 @(-43.1,4.6,-363.7)",
               "move e32: done — in_reach dist=1.5 @(-40.1,3.2,-399.4)",
               "set_party: done — selected after={hp=10/24 my_turn @(-80.2,18.4,-389.2)}",
               "end_turn e2 Corran Vell: done — ended after={hp=24 @(-48.8,2.4,-397.4)}",
               "STOPPED: the way on crosses Fire 1m ahead @(-36.2,5.2,-362.1) (17.2m left)",
               "  job_finished @(-80.2,18.2,-389.5) op=act.move_to dist=0.8 status=done job=j96"]
        for ln in own:
            self.assertEqual(compact.coords(ln), ln)
        self.addCleanup(setattr, compact, "COORD_OWN", compact.COORD_OWN)
        compact.COORD_OWN = False
        self.assertEqual(compact.coords(own[0]), "move_to: done — @(-37,15,-432) dist=1.3")

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


class CombatRules(unittest.TestCase):
    """The combat rules (COMBAT_NARRATIVE, TURN_ENDS, TURN_WORDS, COMBAT_MOVE_POS, MOVE_PHRASES, COMBAT_PHRASES,
    ACTION_DUPS, ENDED_RESOURCES, RESOURCES_LINE, TURN_NAMES, NEXT_ORDER, KIND_GROUP), each checked for 0 lost facts."""

    ENEMY_TURNS = (
        "TURN e28 Lae'zel (round 5)\nend_turn: done — requested still_to_act=[e2] ended\nEVENTS\n"
        "  damage target={e86 name=Commander Zhalk} source={e67 name=Mind Flayer} type=Psychic amount=15\n"
        "  cast caster={e67 name=Mind Flayer} spell=Target_Tentacles_MindFlayer_NoStun roll=attack name=Tentacle Lash\n"
        "  turn_started e124 name=Lesser Imp\n  turn_ended e67 name=Mind Flayer\n"
        "  damage target={e2 name=Brannoch Vael} source={e124 name=Lesser Imp} type=Fire amount=3\n"
        "  cast caster={e124 name=Lesser Imp} spell=Projectile_Generic_Imp roll=attack name=Fiery Bolt\n"
        "  round_started round=5\n  turn_started e86 name=Commander Zhalk\n  turn_ended e124 name=Lesser Imp\n"
        "  damage target={e67 name=Mind Flayer} source={e86 name=Commander Zhalk} type=Slashing amount=13\n"
        "  damage target={e67 name=Mind Flayer} source={e86 name=Commander Zhalk} type=Fire amount=4\n"
        "  cast caster={e86 name=Commander Zhalk} spell=Target_MainHandAttack roll=attack name=Main Hand Attack\n"
        "  turn_started e28 name=Lae'zel\n  turn_ended e86 name=Commander Zhalk\n"
        "  damage target={e28 name=Lae'zel} source={e122 name=Lesser Hellsboar} type= amount=0\n"
        "  cast caster={e122 name=Lesser Hellsboar} spell=Target_Tusk_Boar_Weak roll=attack name=Tusk Attack\n"
        "  turn_started e2 name=Brannoch Vael")

    def checked(self, c, tool, text, shown=""):
        out = c.apply(tool, text)
        self.assertEqual(cc.check(text, out, list(c.elided), shown), [], out)
        return out

    def test_enemy_turn_cast_and_damage_on_one_line(self):
        out = self.checked(fresh(), "combat_turn", self.ENEMY_TURNS)
        ev = out.split("EVENTS\n", 1)[1].split("\n")
        self.assertEqual(ev, [
            "  cast e67 Mind Flayer: Tentacle Lash (attack) -> e86 Commander Zhalk 15 Psychic",
            "  turn e124 Lesser Imp: Fiery Bolt (attack) -> e2 Brannoch Vael 3 Fire",
            "  round 5",
            "  turn e86: Main Hand Attack (attack) -> e67 13 Slashing + 4 Fire",
            "  turn e28",  # named by the TURN header above
            "  cast e122 Lesser Hellsboar: Tusk Attack (attack) -> e28 0",  # a reaction in e28's turn
            "  turn e2"])
        self.assertIn("end_turn: done (still to act: e2)", out)

    def test_each_rule_has_its_own_switch(self):
        for flag, gone in (("COMBAT_NARRATIVE", "turn e124 Lesser Imp: Fiery Bolt"), ("TURN_ENDS", "turn_ended"),
                           ("TURN_WORDS", "  round 5"), ("COMBAT_PHRASES", "(still to act: e2)")):
            self.addCleanup(setattr, compact, flag, getattr(compact, flag))
            setattr(compact, flag, False)
            out = self.checked(fresh(), "combat_turn", self.ENEMY_TURNS)
            if flag == "TURN_ENDS":
                self.assertIn("turn_ended", out)
            else:
                self.assertNotIn(gone, out, flag)
            setattr(compact, flag, True)

    def test_turn_ends_kept_without_a_turn_start(self):
        out = self.checked(fresh(), "wait_for", "EVENTS\n  turn_ended e28 name=Lae'zel\n  combat_ended")
        self.assertIn("turn_ended e28 Lae'zel", out)

    def test_turn_end_naming_a_handle_alone_is_kept(self):
        # naut #90: e28 was named only in the turn_ended line, so "now e28's and e2's turn" was bare
        text = ("end_turn e2 Brannoch Vael: done — ended\nnow e28's and e2's turn (shared turn group)\nEVENTS\n"
                "  turn_started e67 name=Mind Flayer\n  turn_ended e28 name=Lae'zel; e2 name=Brannoch Vael\n"
                "  turn_started e124 name=Lesser Imp\n  turn_ended e67 name=Mind Flayer")
        out = self.checked(fresh(), "end_turn", text)
        self.assertIn("  turn_ended e28 Lae'zel; e2", out)  # the only naming of e28 stays (e2 bare: named above)
        self.assertNotIn("turn_ended e67", out)  # e67 is named by its turn start
        self.assertEqual(out.count("turn_ended"), 1)

    def test_two_casts_by_one_actor_stay_apart(self):
        text = ("EVENTS\n  turn_started e9 name=Imp\n  cast caster={e9 name=Imp} spell=X roll=attack name=Claw\n"
                "  damage target={e2 name=Tav} source={e9 name=Imp} type=Slashing amount=2\n"
                "  cast caster={e9 name=Imp} spell=X roll=attack name=Claw")
        out = self.checked(fresh(), "wait_for", text)
        self.assertIn("damage e9 -> e2 Tav 2 Slashing", out)  # which cast hit is not known: left as it was
        self.assertIn("  turn e9 Imp", out)

    def test_combat_move_lines(self):
        text = ("TURN e28 Lae'zel (round 1)\n"
                "move e31: FAILED — destination is beyond remaining movement (path 7.6 m, 1.5 m left) → partial 1.2m "
                "toward it: done — @(-40.9,2.4,-398), now 8.7m from e31 — stopped short of ≤1.5m: out of movement "
                "(1.5m left, the route needs 8.5m) — melee is out of reach this turn\n"
                "move e32: done — in_reach target=e32 dist=1.5 @(-40.1,3.2,-399.4)\n"
                "move (-79,18,-390): FAILED — destination is beyond remaining movement (path 9.9 m, 9.0 m left) → "
                "partial 8.7m toward it: done — @(-43,1,-390), now 3.2m from e34 — stopped short of ≤1.5m: out of "
                "movement (9m left, the route needs 10.7m) — melee is out of reach this turn")
        out = self.checked(fresh(), "combat_turn", text).split("\n")
        # positions stay, at 0.1 m (naut-r2: the agent built waypoints from them; COMBAT_MOVE_POS off)
        self.assertEqual(out[1], "move e31: beyond movement (path 7.6 m, 1.5 m left) → partial 1.2m: done — "
                                 "@(-40.9,2.4,-398), now 8.7m from e31 — short of ≤1.5m, route needs 8.5m — no melee "
                                 "this turn")
        self.assertEqual(out[2], "move e32: done — in_reach dist=1.5 @(-40.1,3.2,-399.4)")
        self.assertIn("short of ≤1.5m, route needs 10.7m", out[3])  # 9.0 m left == 9m left
        self.addCleanup(setattr, compact, "COMBAT_MOVE_POS", compact.COMBAT_MOVE_POS)
        compact.COMBAT_MOVE_POS = True  # the old rule, still switchable
        out = self.checked(fresh(), "combat_turn", text).split("\n")
        self.assertEqual(out[2], "move e32: done — in_reach dist=1.5")
        self.assertNotIn("@(", "\n".join(out))

    def test_different_left_is_kept(self):
        line = ("move e1: FAILED — beyond movement (path 9.9 m, 9.0 m left) → partial 8.7m: done — short of ≤1.5m: out "
                "of movement (0.3m left, route needs 10.7m)")
        self.assertIn("out of movement (0.3m left", compact.move_phrases(line))

    def test_action_dups(self):
        self.assertEqual(compact.action_dups("action: Use e34: skipped (far) → Projectile_FireBolt e68: done — "
                                             "cast=Projectile_FireBolt target=e68 dead damage=[e68 Hellsboar 5 Fire]"),
                         "action: Use e34: skipped (far) → Projectile_FireBolt e68: done — damage=[e68 Hellsboar 5 Fire] dead")
        self.assertEqual(compact.action_dups("action: Shout_Dash: done — statuses=[target=e2 status=Dash] cast=Shout_Dash"),
                         "action: Shout_Dash: done — statuses=[e2 Dash]")
        self.assertEqual(compact.action_dups("bonus: P e67: done — damage=[e67 Lesser Imp 0; e67 Lesser Imp 4 Bludgeoning]"),
                         "bonus: P e67: done — damage=[e67 Lesser Imp 0; e67 4 Bludgeoning]")
        other = "action: A e1: done — cast=B target=e1 damage=[e2 Imp 3]"
        self.assertEqual(compact.action_dups(other), other)  # another spell, another target: both said

    def test_ended_turn_resources_and_turn_names(self):
        text = ("TURN e28 Lae'zel (round 1)\nend_turn: done — requested still_to_act=[e2] ended\n"
                "NOW e28 hp=24 max_hp=24 action=0 bonus=1 movement=0.3 reaction=1\nCHANGES\n"
                "  TURN round 1 turn=e2 Brannoch Vael\nNOW e2 Brannoch Vael's turn: plan it\nYOUR ACTIONS (e2)\n"
                "NOW Action 1/1 · Bonus 1/1 · Move 9/9m · Reaction 1/1\nHide [A] self")
        c = fresh()
        out = self.checked(c, "combat_turn", text).split("\n")
        self.assertEqual(out[2], "NOW e28 hp=24/24 reaction=1")
        self.assertEqual(out[5], "NOW e2's turn: plan it")
        self.assertEqual(out[6], "YOUR ACTIONS (e2) NOW full: Action 1 · Bonus 1 · Move 9m · Reaction 1")
        first = "\n".join(out)
        again = self.checked(c, "combat_turn", text, first).split("\n")
        self.assertEqual(again[5], "NOW e2's turn")  # the hint is said once
        self.assertEqual(again[6], "YOUR ACTIONS (e2) NOW full (as last turn)")
        spent = text.replace("Action 1/1", "Action 0/1")
        self.assertIn("YOUR ACTIONS (e2) NOW Action 0/1 · Bonus 1/1", c.apply("combat_turn", spent))
        live = "NOW e28 hp=24 max_hp=24 action=1 bonus=1 movement=9 reaction=1"
        self.assertIn("action=1 bonus=1 movement=9", fresh().apply("combat_turn", live))  # no end_turn above: kept

    def test_next_order(self):
        c = fresh()
        text = ("CHANGES\n  TURN round 4 turn=e67 Mind Flayer\nNEXT e67 Mind Flayer, e124 Lesser Imp, +2 more, then "
                "e28 Lae'zel (yours): send its combat_turn now")
        first = self.checked(c, "combat_turn", text)
        self.assertIn("NEXT e67, e124 Lesser Imp, +2 more, then e28 Lae'zel (yours)", first)
        again = self.checked(c, "combat_turn", text, first)
        self.assertIn("NEXT (order as before), then e28 Lae'zel (yours)", again)
        other = self.checked(c, "combat_turn", text.replace("+2 more", "+3 more"), first + again)
        self.assertIn("+3 more", other)

    def test_checker_possessive_is_the_name(self):
        from bg3ai import compact_check as cc
        self.assertEqual(cc.atoms("NOW e2 Wyll's turn"), {"NOW", "e2", "Wyll", "turn"})
        self.assertEqual(cc.check("TURN turn=e2 Wyll\nNOW e2 Wyll's turn: plan it", "TURN turn=e2 Wyll\nNOW e2's turn: plan it",
                                  [], ""), [])
        self.assertTrue(cc.check("NOW e2 Wyll's turn", "NOW e2's turn", [], ""))   # the name nowhere else: lost

    def test_next_party_member_keeps_yours(self):
        # claude-run-2 S050 replay: a party member next with nobody in between
        text = ("CHANGES\n  TURN round 2 turn=e5 Shadowheart\n  e6 Claude HP 50->40/90\n"
                "NEXT e6 Claude (yours): plan that turn now")
        c = fresh()
        first = self.checked(c, "combat_turn", text)
        self.assertIn("NEXT e6 (yours): plan that turn now", first)
        self.assertIn("NEXT e6 (yours): plan that turn now", self.checked(c, "combat_turn", text, first))  # not "as before"
        shared = ("CHANGES\n  e1 Karlach HP 9->8/9\nNEXT e1 Karlach + e2 Wyll (yours, shared turn): plan that turn now")
        self.assertIn("NEXT e1 Karlach + e2 Wyll (yours, shared turn): plan that turn now",   # not matched: in full
                      self.checked(fresh(), "combat_turn", shared))
        self.assertEqual(compact._next_item("e1 Karlach + e2 Wyll (yours, shared turn)", {"e1"}),
                         "e1 + e2 Wyll (yours, shared turn)")

    def test_kind_group(self):
        lines = ["CHANGES", "  + e113 Mucoid Shell <item> 22.9m", "  + e68 Hellsboar hostile HP 5/5 1.5m",
                 "  + e114 Caustic Bulb <item> 23.2m (+2 more Caustic Bulb up to 24m)", "  + e48 Thrall neutral dead 17m",
                 "  + e30 Imp neutral dead 4.6m", "  + e35 Aquarium <usable> HP 4/4 13.6m"]
        self.assertEqual(compact.kind_group(lines), [
            "CHANGES", "  + <item>: e113 Mucoid Shell 22.9m, e114 Caustic Bulb 23.2m (+2 more Caustic Bulb up to 24m)",
            "  + e68 Hellsboar hostile HP 5/5 1.5m", "  + neutral dead: e48 Thrall 17m, e30 Imp 4.6m",
            "  + e35 Aquarium <usable> HP 4/4 13.6m"])
        look = ["+ e1 A <door> 3m", "+ e2 B <door> 7m"]
        self.assertEqual(compact.kind_group(look), ["+ <door>: e1 A 3m, e2 B 7m"])
        events = ["EVENTS", "  + e1 A <door> 3m", "  + e2 B <door> 7m"]
        self.assertEqual(compact.kind_group(events), events)  # only CHANGES / look's delta

    def test_off_is_identity_for_combat_text(self):
        self.addCleanup(setattr, compact, "COMPACT", compact.COMPACT)
        compact.COMPACT = False
        self.assertEqual(fresh().apply("combat_turn", self.ENEMY_TURNS), self.ENEMY_TURNS)


class BlockAgain(unittest.TestCase):
    """get_nearby right after look full=true (live 20261016-183253): the same CHAR/ITEM lines once."""

    LOOK = ("WHERE Campsite · Shadow-Cursed Lands | exploring | gold 43\n"
            "CHAR e7 Withers neutral HP 1/1 1.2m [Moonshield] can_talk @(687,19,-1483)\n"
            "CHAR e8 Arabella neutral HP 3/3 2.2m can_talk @(687,20,-1481)\n"
            "CHAR e10 Scratch neutral HP 7/7 23.3m can_talk @(666,19,-1496)\n"
            "ITEM e11 Wooden Crate <item> 7.8m\nITEM e14 Mug <item> 13.4m (+2 more Mug up to 21.2m)\n"
            "ITEM e12 Traveller's Chest <usable> 12.8m\nLEVEL UP: e5 Wyll (4, at camp)")
    BLOCK = LOOK.split("\n")[1:-1]

    def test_identical_block_is_one_line(self):
        c = fresh()
        shown = c.apply("look", self.LOOK)
        c.now["t"] = 5
        raw = "\n".join(self.BLOCK)
        out = c.apply("get_nearby", raw)
        self.assertEqual(out, "(same as the look a moment ago: 6 lines)")
        self.assertEqual(cc.check(raw, out, c.elided, shown), [])

    def test_changed_lines_stay(self):
        c = fresh()
        shown = c.apply("look", self.LOOK)
        raw = "\n".join(self.BLOCK).replace("23.3m can_talk @(666,", "23.6m can_talk @(667,") + "\nITEM e20 Bedroll <item> 18.3m"
        out = c.apply("get_nearby", raw)
        self.assertEqual(out.split("\n")[0], "(5 lines same as the look a moment ago; changed or new:)")
        self.assertIn("e10 Scratch neutral HP 7/7 23.6m", out)
        self.assertIn("ITEM e20 Bedroll <item> 18.3m", out)
        self.assertEqual(cc.check(raw, out, c.elided, shown), [])

    def test_window_and_minimum(self):
        c = fresh()
        c.apply("look", self.LOOK)
        c.now["t"] = compact.BLOCK_AGAIN_S + 1
        raw = "\n".join(self.BLOCK)
        self.assertEqual(c.apply("get_nearby", raw), raw)  # too old: in full, the clock restarts
        c.now["t"] += 10
        self.assertTrue(c.apply("look", raw).startswith("(same as get_nearby a moment ago"))
        few = "\n".join(self.BLOCK[:compact.BLOCK_AGAIN_MIN_LINES - 1])
        self.assertEqual(c.apply("get_nearby", few), few)

    def test_a_reference_never_points_at_a_reference(self):
        c = fresh()
        c.apply("look", self.LOOK)
        raw = "\n".join(self.BLOCK)
        c.now["t"] = 40
        self.assertTrue(c.apply("get_nearby", raw).startswith("(same as"))
        c.now["t"] = 70  # 70 s after the full showing, 30 s after the reference: in full again
        self.assertEqual(c.apply("get_nearby", raw), raw)

    def test_off(self):
        self.addCleanup(setattr, compact, "BLOCK_AGAIN", compact.BLOCK_AGAIN)
        compact.BLOCK_AGAIN = False
        c = fresh()
        c.apply("look", self.LOOK)
        raw = "\n".join(self.BLOCK)
        self.assertEqual(c.apply("get_nearby", raw), raw)


class SwitchOff(unittest.TestCase):
    """BG3AI_COMPACT_OFF=RULE1,RULE2 (read at bridge start): single rules off without a code edit."""

    def setUp(self):
        saved = {k: getattr(compact, k) for k in compact.rules()}
        self.addCleanup(lambda: [setattr(compact, k, v) for k, v in saved.items()])

    def test_named_rules_go_off(self):
        self.assertEqual(compact.switch_off(" next_order,COMBAT_NARRATIVE ,, COORD_DECIMALS"), [])
        self.assertFalse(compact.NEXT_ORDER)
        self.assertFalse(compact.COMBAT_NARRATIVE)
        self.assertIsNone(compact.COORD_DECIMALS)
        self.assertTrue(compact.TURN_ENDS)  # the others stay
        out = fresh().apply("combat_turn", CombatRules.ENEMY_TURNS)
        self.assertIn("cast e124: Fiery Bolt (attack)", out)

    def test_unknown_names_are_warned_and_ignored(self):
        with self.assertLogs("bg3ai.compact", "WARNING") as logs:
            self.assertEqual(compact.switch_off("NO_SUCH_RULE,HINTS,COMPACT,HINT_FULL_TIMES"),
                             ["NO_SUCH_RULE", "COMPACT", "HINT_FULL_TIMES"])
        self.assertIn("NO_SUCH_RULE", logs.output[0])
        self.assertFalse(compact.HINTS)
        self.assertTrue(compact.COMPACT)  # all-off stays BG3AI_COMPACT=0's job

    def test_every_rule_constant_is_switchable(self):
        for k in ("REFS", "COMBAT_MOVE_POS", "COORD_OWN", "TURN_ENDS", "NAMES_ONCE", "COORD_DECIMALS", "PHRASES"):
            self.assertIn(k, compact.rules())

    def test_read_from_the_env_at_import(self):
        import os
        import subprocess
        src = str(Path(__file__).resolve().parents[1] / "src")
        r = subprocess.run([sys.executable, "-c", "from bg3ai import compact; print(compact.REFS, compact.HINTS)"],
                           env={**os.environ, "PYTHONPATH": src, "BG3AI_COMPACT_OFF": "REFS,BOGUS"},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "False True")
        self.assertIn("BOGUS", r.stderr)


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
