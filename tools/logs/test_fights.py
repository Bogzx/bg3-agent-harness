import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fights


class Outcome(unittest.TestCase):
    def test_cut_by_the_session_end_is_unfinished_not_won(self):
        # S045 00:28 Ketheric try 2: enemies had died, combat still on when the session stopped
        self.assertEqual(fights.outcome_of(False, False, True, True), "unfinished")

    def test_finished_fights(self):
        self.assertEqual(fights.outcome_of(False, False, True, False), "won")
        self.assertEqual(fights.outcome_of(False, False, False, False), "ended")
        self.assertEqual(fights.outcome_of(True, False, True, True), "reload")
        self.assertEqual(fights.outcome_of(False, True, False, True), "wipe")


if __name__ == "__main__":
    unittest.main()
