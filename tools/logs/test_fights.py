import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fights

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "bridge/tests/golden/naut_results.jsonl.gz"


def golden_sessions():
    """{session: (original texts, compacted texts)} of the naut golden results, compacted as the bridge does."""
    import gzip, json
    sys.path.insert(0, str(REPO / "bridge/src"))
    from bg3ai import compact
    by = {}
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            by.setdefault(r["session"], []).append(r)
    out = {}
    for sess, rows in by.items():
        c = compact.Compactor(clock=lambda: 0.0)
        out[sess] = ([r["text"] for r in rows], [c.apply(r["tool"], r["text"]) for r in rows])
    return out


class Outcome(unittest.TestCase):
    def test_cut_by_the_session_end_is_unfinished_not_won(self):
        # S045 00:28 Ketheric try 2: enemies had died, combat still on when the session stopped
        self.assertEqual(fights.outcome_of(False, False, True, True), "unfinished")

    def test_finished_fights(self):
        self.assertEqual(fights.outcome_of(False, False, True, False), "won")
        self.assertEqual(fights.outcome_of(False, False, False, False), "ended")
        self.assertEqual(fights.outcome_of(True, False, True, True), "reload")
        self.assertEqual(fights.outcome_of(False, True, False, True), "wipe")



class CompactedResults(unittest.TestCase):
    """bridge compact.py rewrites results (died/cast/CHANGES DEAD); the miner reads both forms the same."""

    def test_death_forms(self):
        names = {"e67": "Mind Flayer"}
        for text in ("  died name=Imp e34", "  died e34 name=Imp", "  died e34 Imp", "  ~ e34 Imp DEAD",
                     "  ~ e34 Imp -Threatened, DEAD, no longer in_combat"):
            self.assertEqual(fights.deaths(text, names), {("Imp", "e34")}, text)
        self.assertEqual(fights.deaths("  died e67", names), {("Mind Flayer", "e67")})
        self.assertEqual(fights.deaths("  died e34 Imp\n  ~ e34 Imp DEAD"), {("Imp", "e34")})
        self.assertEqual(fights.deaths("  ~ e54 Mesh <ladder> 1m N LEADS TO A DEAD END"), set())

    def test_cast_forms(self):
        old = "action: Target_MainHandAttack e31: done — cast=Target_MainHandAttack target={e31 dead} total_damage=9"
        new = "action: Target_MainHandAttack e31: done — target=e31 dead damage=[e31 Imp 9 Slashing]"
        chained = ("action: use_ability e34: skipped (out of range) → Projectile_FireBolt e34: done — "
                   "target=e34 dead")
        self.assertEqual(fights.casts_of(old), {"Target_MainHandAttack": 1})
        self.assertEqual(fights.casts_of(new), {"Target_MainHandAttack": 1})
        self.assertEqual(fights.casts_of(chained), {"Projectile_FireBolt": 1})
        self.assertEqual(fights.casts_of("move e32: done — target=e32"), {})
        # compact.py ACTION_DUPS: the cast= beside a bonus action's own id is gone too
        bonus = "bonus: Target_PommelStrike e67: done — statuses=[e67 Dazed] damage=[e67 Lesser Imp 4 Bludgeoning]"
        self.assertEqual(fights.casts_of(bonus), {"Target_PommelStrike": 1})
        self.assertEqual(fights.casts_of("action: Shout_Dash: done — statuses=[e2 Dash]"), {"Shout_Dash": 1})

    @unittest.skipUnless(GOLDEN.exists(), "golden naut results missing")
    def test_golden_replay_same_deaths_and_casts(self):
        for sess, (orig, comp) in golden_sessions().items():
            a, b = "\n".join(orig), "\n".join(comp)
            self.assertTrue(fights.deaths(a, fights.handle_names(a)), sess)
            self.assertEqual(fights.deaths(a, fights.handle_names(a)), fights.deaths(b, fights.handle_names(b)), sess)
            self.assertEqual(fights.casts_of(a), fights.casts_of(b), sess)




class RetryVerdict(unittest.TestCase):
    """vis V6: claude-run-2's Act 3 retries as fights.py mined them (rounds, hp, downs, killed from the debrief)."""

    def f(self, outcome, downs, hp, killed, abil, debrief="reloaded"):
        return {"outcome": outcome, "downs": downs, "hp_lost": hp, "killed": killed, "debrief": debrief,
                "party_died": [], "abilities": dict.fromkeys(abil, 1)}

    def test_orin_try_2_got_further(self):
        # S065 11:43 (killed 0/9, 1 down) → 11:50 (killed 2/9, 4 downs, Karlach died): "NOT better" before
        t1 = self.f("reload", 1, 434, 0, ["Guiding Bolt", "Silence", "Insect Plague"])
        t2 = self.f("reload", 4, 848, 2, ["Guiding Bolt", "Insect Plague", "Eldritch Blast"])
        self.assertEqual(fights.retry_verdict(t1, t2), "further (killed 2 vs 0)")
        self.assertEqual(fights.same_tactic(t1, t2), [])

    def test_the_gortash_win_is_no_same_tactic(self):
        # S061 09:37 wipe → S062 10:00 won (costly): same top abilities, a different plan
        kit = ["Divine Smite", "Frenzied Strike", "Target_MainHandAttack", "Eldritch Blast"]
        lost = self.f("reload", 12, 705, 3, kit)
        won = self.f("won", 2, 377, 5, kit, debrief="costly")
        self.assertEqual(fights.retry_verdict(lost, won), "better")
        self.assertEqual(fights.same_tactic(lost, won), [])
        again = self.f("reload", 12, 900, 3, kit)
        self.assertEqual(fights.retry_verdict(lost, again), "NOT better")
        self.assertEqual(fights.same_tactic(lost, again), sorted(kit[:4]))

    def test_killed_from_the_debrief_block(self):
        txt = ("load_game: done\nFIGHT DEBRIEF (reloaded, 7 rounds, 8m51s) vs Orin\n  DIED: Karlach — revive now\n"
               "  enemies used: Orin: Slay\n  killed 2/9 · dealt Karlach 21\n")
        self.assertEqual(fights.KILLED_RE.search(txt).groups(), ("2", "9"))

    def test_downs_from_the_debrief_block(self):
        # vis V7: S065 Orin try 2's debrief (its results showed none of the 4 downs as +Downed)
        txt = ("FIGHT DEBRIEF (reloaded, 7 rounds, 8m51s) vs Orin\n  killed 2/9 · dealt Karlach 21\n"
               "  downed: Wyll r2 by Orin, Karlach r6 by Orin, Karlach r6 by Orin, Shadowheart r7 by Death's Head "
               "of Bhaal Fustian\n  ours: Wyll Eldritch Blast x3\nnext: downed: Claude r1 by Orin\n")
        self.assertEqual(fights.debrief_downs(txt), {"Wyll": 1, "Karlach": 2, "Shadowheart": 1})
        # S061: a summon is listed too (mine() keeps party names only); the ability in parentheses (v2)
        self.assertEqual(fights.debrief_downs("FIGHT DEBRIEF (wipe, 11 rounds, 9m) vs Gortash\n  downed: Spiritual "
                                              "Weapon r9 by Iron Consul Lo, Karlach r10 by Gortash (Slay, Bite)\n"),
                         {"Spiritual Weapon": 1, "Karlach": 1})
        self.assertEqual(fights.debrief_downs("FIGHT DEBRIEF (clean, 2 rounds, 1m) vs Imp\n  killed 3/3\n"), {})
        self.assertIsNone(fights.debrief_downs("downed: nothing to play, its turn passes by itself"))

    def test_repeat_warning_flag_reads_the_debrief_warnings(self):
        # the debrief's WARNING wording (S061-S065 showed 8 distinct ones; the old pattern matched none)
        rx = fights.FLAGS["repeat_warning"]
        self.assertTrue(rx.search("WARNING: Wyll went down first again (last time vs these enemies too: r6, S061)"))
        self.assertTrue(rx.search("WARNING: Radiant did little to Orin last time too (9 hits, avg 1.0, S065)"))
        self.assertFalse(rx.search("WARNING: Karlach is still in the party"))


S080_DEBRIEF = ("load_game: done — loaded=S080-prefight\nFIGHT DEBRIEF (reloaded, 5 rounds, 8m41s) vs Viconia DeVir, "
                "Justiciar Crusader Owltalon +16 more\n  DIED: Gale (r4, by Sharran Novice Greymask), Wyll (r4, by "
                "Justiciar Crusader Owltalon), Shadowheart (r5, by Sharran Sentry Hendly) — revive now: Scroll of "
                "Revivify / Revivify on the body; Withers (camp) raises only party members; or reload before the "
                "fight\n  killed 5/18 (Sharran Fidelian Hyrald r1, Viconia DeVir r4) · dealt Gale 172, Wyll 67, "
                "Shadowheart 14, Claude 113 · took Gale 217, Wyll 262, Shadowheart 250, Claude 310\n")


class S080GameOver(unittest.TestCase):
    """vis V21: S080 Viconia try 7 went game over; its results had no died event, the debrief listed 3 deaths, and
    fights.py printed `died -` and labelled the fight [Campsite] (the camp exit's WHERE line before it)."""

    def test_deaths_from_the_debrief_block(self):
        self.assertEqual(fights.debrief_deaths(S080_DEBRIEF), {"Gale", "Wyll", "Shadowheart"})
        self.assertEqual(fights.debrief_deaths("FIGHT DEBRIEF (reloaded, 4 rounds, 2m) vs Gortash\n  DIED: Wyll, "
                                               "Claude — revive now\n"), {"Wyll", "Claude"})   # S061 (v1)
        self.assertEqual(fights.debrief_deaths("FIGHT DEBRIEF (near_wipe, 3 rounds, 2m) vs Imp\n  DIED: Gale (r2, "
                                               "by Imp: Fire Bolt), Aylin (ally, r3) — revive now\n"), {"Gale"})
        self.assertEqual(fights.debrief_deaths("FIGHT DEBRIEF (clean, 2 rounds, 1m) vs Imp\n  killed 3/3\n"), set())
        self.assertIsNone(fights.debrief_deaths("DIED: nobody"))

    def test_the_place_of_a_move(self):
        self.assertEqual(fights.place("Campsite · Lower City -> Cloister of Sombre Embrace · Lower City "),
                         "Cloister of Sombre Embrace")
        self.assertEqual(fights.place("Bhaal Temple · Lower City -> Lower City"), "Bhaal Temple")   # S066 Orin
        self.assertEqual(fights.place("Cloister of Sombre Embrace · Lower City "), "Cloister of Sombre Embrace")
        self.assertEqual(fights.place("Lower City"), "Lower City")

    def test_mine_reads_the_game_over(self):
        import json, tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sd = Path(tmp.name) / "claude-run-2"
        sd.mkdir()
        t0 = 1_000_000.0
        calls = [(t0 - 9, "do_actions", ("1. move_to: done\nCHANGES\n  WHERE Campsite · Lower City -> Cloister of "
                                         "Sombre Embrace · Lower City\n  PARTY e2 Gale HP 100/100")),
                 (t0 + 5, "combat_turn", "TURN e2 Gale (round 1)\nend_turn: done\nPARTY e2 Gale HP 90/100"),
                 (t0 + 60, "combat_turn", ("TURN e1 Wyll (round 5)\nend_turn: done\nPARTY e1 Wyll HP 9/80\n"
                                           "PARTY e3 Shadowheart HP 5/90\nPARTY e4 Claude HP 50/190")),
                 (t0 + 90, "load_game", S080_DEBRIEF)]
        with open(sd / "S999.turns.jsonl", "w") as tf, open(sd / "S999.jsonl", "w") as jf:
            for i, (t, tool, res) in enumerate(calls):
                tf.write(json.dumps({"type": "use", "id": f"c{i}", "t": t}) + "\n")
                tf.write(json.dumps({"type": "result", "id": f"c{i}", "t": t + 1}) + "\n")
                jf.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": f"c{i}",
                                                              "name": "mcp__bg3__" + tool, "input": {}}]}}) + "\n")
                jf.write(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": f"c{i}",
                                                              "content": json.dumps({"result": res})}]}}) + "\n")
        with open(sd / "S999.metrics.jsonl", "w") as mf:
            mf.writelines(json.dumps({"type": "sample", "t": t0 + k, "combat": True}) + "\n" for k in range(0, 70, 10))
            mf.write(json.dumps({"type": "sample", "t": t0 + 200, "combat": False}) + "\n")
        old = fights.SESSIONS
        fights.SESSIONS = Path(tmp.name)
        self.addCleanup(setattr, fights, "SESSIONS", old)
        f = fights.mine("claude-run-2", "S999")[0]
        self.assertEqual(f["party_died"], ["Gale", "Shadowheart", "Wyll"])   # old code: [] (no died event)
        self.assertEqual(f["where"], "Cloister of Sombre Embrace")         # old code: Campsite
        self.assertEqual(f["outcome"], "reload")


if __name__ == "__main__":
    unittest.main()
