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


if __name__ == "__main__":
    unittest.main()
