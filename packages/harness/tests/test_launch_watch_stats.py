"""LaunchWatch.stats_problem: what the last stats scan of a launch's bg3le log says about Ext.Stats' pools."""

import shutil
import tempfile
import unittest
from pathlib import Path

from bg3harness.game import io as rgame


class StatsProblem(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-watch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.w = rgame.LaunchWatch("20261010-020143", self.tmp, started=100.0)

    GOOD_SCAN = (
        "[ 1.0] stats: 15754 stats via the rarity run at 0x3053a197028\n"
        "[ 1.1] stats: string pool at 0x3053a197048 (+32 from the run), 21397 entries\n"
        "[ 1.1] stats: guid pool at pool+32, 184 entries\n[ 1.1] stats: attributes available\n"
    )
    # bg3le-20261106-213327, the S5 launch: the wrong pool, and "attributes available" all the same
    BAD_SCAN = (
        "[ 1.0] stats: 15754 stats via the rarity run at 0x3053a197028\n"
        "[ 1.1] stats: string pool at 0x3053a196ea8 (-384 from the run), 1512 entries\n"
        "[ 1.1] stats: no float pool found; Float attributes report their pool index\n"
        "[ 1.1] stats: no guid pool found; GUID attributes report their pool index\n"
        "[ 1.1] stats: no translated string pool found; TranslatedString attributes report nothing\n"
        "[ 1.1] stats: attributes available\n"
    )

    def write_log(self, text):
        (self.tmp / "bg3le-20261010-020143.log.379707").write_text(text)

    def test_stats_problem_good_scan(self):
        self.write_log(self.GOOD_SCAN)
        self.assertIsNone(self.w.stats_problem())

    def test_stats_problem_names_the_missing_pools(self):
        self.write_log(self.BAD_SCAN)
        why = self.w.stats_problem()
        self.assertIn("no guid pool found", why)
        self.assertIn("no float pool found", why)
        self.assertIn("no translated string pool found", why)

    def test_stats_problem_no_candidate_at_all(self):  # patch 0016's refusal
        self.write_log(
            "[ 1.0] stats: 15754 stats via the rarity run at 0x3\n"
            "[ 1.1] stats: no string pool found after the rarity run; attributes stay unavailable\n"
            "[ 1.1] stats: attributes unavailable\n"
        )
        self.assertIn("no string pool found", self.w.stats_problem())

    def test_stats_problem_without_a_scan(self):
        self.write_log('[ 1.0] bg3le loaded\n[ 1.1] stats: the string table has no entry for "Epic" yet\n')
        self.assertEqual(self.w.stats_problem(), "bg3le logged no stats scan")

    def test_stats_problem_reads_the_last_scan_only(self):
        self.write_log(self.BAD_SCAN + self.GOOD_SCAN)
        self.assertIsNone(self.w.stats_problem())
        self.write_log(self.GOOD_SCAN + self.BAD_SCAN)
        w = rgame.LaunchWatch("20261010-020143", self.tmp, started=100.0)
        self.assertIn("no guid pool found", w.stats_problem())

    def test_stats_problem_sees_lines_the_polls_already_read(self):
        self.write_log(self.BAD_SCAN)
        self.assertIsNone(self.w.log_crash())
        self.assertIn("no guid pool found", self.w.stats_problem())


if __name__ == "__main__":
    unittest.main()
