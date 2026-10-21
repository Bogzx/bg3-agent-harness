"""bg3bench.records: the runs table, the series summary and compare (moved from runner/test_runs.py)."""

from __future__ import annotations

import unittest

from bg3bench import records as R

RECS = [
    {
        "run": 1,
        "playthrough": "naut",
        "outcome": "reached",
        "clean": True,
        "wall_min": 12.5,
        "sessions": 1,
        "cost_usd": 4.8,
        "started": 1000.0,
        "reached_at": 1693.0,
        "caps": {"max_cost": 30.0},
        "tokens": {"input": 200, "output": 18000, "cache_read": 6_500_000, "cache_write": 110_000},
    },
    {
        "run": 2,
        "playthrough": "naut-r2",
        "outcome": "reached",
        "clean": True,
        "wall_min": 12.8,
        "sessions": 1,
        "cost_usd": 4.2,
        "time_to_milestone_s": 708.0,
        "latency": {"idle_median_s": 0.5},
        "tokens": {"input": 200, "output": 18600, "cache_read": 6_100_000, "cache_write": 80_000},
    },
    {
        "run": 3,
        "playthrough": "naut-r3",
        "outcome": "over_budget",
        "cap": "cost",
        "clean": False,
        "wall_min": 30.0,
        "sessions": 2,
        "cost_usd": 10.0,
        "time_to_milestone_s": None,
        "latency": {"idle_median_s": 1.5},
        "version_drift": ["repo.commit: a → b"],
        "caps": {"max_cost": 10.0},
        "failures": [{"kind": "cost_cap", "class": "agent"}],
    },
]


class Summary(unittest.TestCase):
    RECS = RECS

    def test_time_to_milestone(self):
        self.assertEqual(R.time_to_milestone(100.0, 760.25), 660.2)
        self.assertIsNone(R.time_to_milestone(100.0, None))

    def test_result_tok_grade_vs_naut(self):
        self.assertEqual((R.RESULT_TOK_BASELINE_TOTAL, R.RESULT_TOK_BASELINE_PER_RESULT), (42075.3, 393.2))
        self.assertEqual(
            [R.result_tok_grade(v, 1000) for v in (900, 950, 1099, 1100, None)],
            ["better", "same", "same", "worse", None],
        )
        self.assertIsNone(R.result_tok_grade(5, None))

    def test_stats(self):
        st = R.stats([1, None, 3])
        self.assertEqual((st["n"], st["min"], st["max"], st["mean"]), (2, 1.0, 3.0, 2.0))
        self.assertAlmostEqual(st["stddev"], 1.41421356, places=6)
        self.assertEqual(R.stats([5])["stddev"], 0.0)
        self.assertIsNone(R.stats([None]))

    def test_series_summary(self):
        out = R.series_summary(self.RECS)
        lines = out.splitlines()
        self.assertEqual(lines[0], "series: n=3, reached 2/3")
        row = {ln.split("  ")[0]: ln.split() for ln in lines[3:8]}
        self.assertEqual(row["wall min"][-5:], ["3", "12.5", "30.0", "18.4", "10.0"])
        self.assertEqual(row["to milestone min"][-5:], ["2", "11.6", "11.8", "11.7", "0.2"])  # 693 s and 708 s
        self.assertEqual(row["cost $"][-5:-3], ["3", "4.20"])
        self.assertEqual(row["tokens M"][-5:-3], ["2", "6.20"])  # run 3 has no tokens
        self.assertEqual(row["idle median s"][-5:], ["2", "0.50", "1.50", "1.00", "0.71"])
        self.assertIn("version drift (--allow-version-drift): run 3: repo.commit: a → b", out)
        self.assertIn("caps differ between runs", out)

    def test_table_columns(self):
        lines = R.runs_table(self.RECS).splitlines()
        self.assertTrue(lines[0].startswith("run  playthrough  outcome"))
        self.assertIn("to M min", lines[0])
        self.assertIn("over_budget (cost)", lines[4])
        self.assertIn("11.6", lines[2])  # time to the milestone of an old record (reached_at - started)
        self.assertIn("6.63", lines[2])  # tokens, millions

    def test_result_tokens_in_the_table_and_summary(self):
        recs = [
            dict(self.RECS[0], result_tokens={"total": 41944, "n": 115, "per_result": 364.7}),
            dict(self.RECS[1], result_tokens={"total": 30000, "n": 100, "per_result": 300.0}),
            self.RECS[2],
        ]  # run 3: no field
        lines = R.runs_table(recs).splitlines()
        self.assertIn("result tok k (/res)", lines[0])
        self.assertIn("41.9 (365)", lines[2])
        self.assertIn("30.0 (300)", lines[3])
        out = R.series_summary(recs)
        row = {ln.split("  ")[0]: ln.split() for ln in out.splitlines()}
        self.assertEqual(row["result tok k"][-5:], ["2", "30.0", "41.9", "36.0", "8.4"])
        self.assertIn(
            "result tokens vs the naut baseline (42.1k/run, 393/result): run 1 same/same, "
            "run 2 better/better (total/per result; ±10 % = same)",
            out,
        )
        self.assertNotIn("result tokens vs", R.series_summary(self.RECS))

    def test_table_and_summary_show_persona_and_model_time(self):
        recs = [
            dict(
                Summary.RECS[0],
                persona={"status": "ok", "score": 8.0, "judge_model": "j", "cost_usd": 0.1},
                model_time={
                    "adjusted_model_s_ref_ttft": 300.0,
                    "adjusted_model_s": 330.0,
                    "model_s": 400.0,
                    "api_floor": {"p10_s": 1.0},
                },
            ),
            dict(
                Summary.RECS[1],
                persona={"status": "judge_failed", "judge_model": "j", "cost_usd": 0.05},
                model_time={"adjusted_model_s_ref_ttft": 420.0, "adjusted_model_s": 500.0, "api_floor": {"p10_s": 1.1}},
            ),
            dict(Summary.RECS[2], persona={"status": "ok", "score": 6.0, "judge_model": "k", "cost_usd": 0.1}),
        ]
        lines = R.runs_table(recs).splitlines()
        self.assertIn("persona  adj model min  API floor s", lines[0])
        cells = [ln.split() for ln in lines[2:5]]
        # adj model min = the TTFT-normalised adjusted_model_s_ref_ttft, not adjusted_model_s
        self.assertEqual((cells[0][13:16], cells[1][13:16]), (["8", "5.0", "1.00"], ["fail", "7.0", "1.10"]))
        self.assertIn("6  ", lines[4][lines[0].index("persona") :])
        self.assertIn("-", lines[4][lines[0].index("adj model min") :].split()[0])
        self.assertEqual(R.persona_cell({}), "-")
        out = R.series_summary(recs)
        row = {ln.split("  ")[0]: ln.split() for ln in out.splitlines()[3:11]}
        self.assertEqual(row["persona score"][-5:], ["2", "6.0", "8.0", "7.0", "1.4"])  # failed: not a score
        self.assertEqual(row["adj model min"][-5:], ["2", "5.0", "7.0", "6.0", "1.4"])
        self.assertEqual(row["judge $"][-5:-3], ["3", "0.05"])
        self.assertIn("judge models differ between runs (scores don't compare): j | k", out)
        self.assertNotIn("API floor differs", out)  # 1.0 vs 1.1 s: same API
        self.assertIn("API floor s", out)

    def test_api_floor_difference_is_flagged_not_a_regression(self):
        mt = lambda adj, floor: {
            "model_s": adj + 40,
            "adjusted_model_s_ref_ttft": adj,
            "api_floor": {"p10_s": floor},
            "model_turns": 80,
        }
        naut = [dict(Summary.RECS[0], model_time=mt(193.0, 1.0)), dict(Summary.RECS[1], model_time=mt(190.0, 1.02))]
        naut2 = [dict(Summary.RECS[0], model_time=mt(167.0, 1.5)), dict(Summary.RECS[1], model_time=mt(166.0, 1.6))]
        out = R.series_summary(naut + [dict(Summary.RECS[2], run=3, model_time=mt(170.0, 1.55))])
        self.assertIn("API floor differs (run 1 1.00 s … run 3 1.55 s per request, > 0.3 s)", out)
        cmp = R.series_compare("naut", naut, "naut2", naut2)
        self.assertIn("naut (n=2) vs naut2 (n=2), means", cmp)
        row = {ln.split("  ")[0]: ln for ln in cmp.splitlines()}
        self.assertIn("-0.42 (-13 %)", row["adj model min"])
        self.assertIn("+0.54 (+53 %)", row["API floor s"])
        self.assertIn("API floor differs (naut 1.01 s … naut2 1.55 s per request", cmp)
        self.assertNotIn("API floor differs", R.series_compare("a", naut, "b", naut))
        old = [dict(Summary.RECS[0], model_time={"adjusted_model_s": 1.0})]
        self.assertIn("API floor unknown", R.series_compare("a", old, "b", naut))
        self.assertIsNone(R.record_adjusted_model_s(old[0]))  # the headline only: no fallback to adjusted_model_s
