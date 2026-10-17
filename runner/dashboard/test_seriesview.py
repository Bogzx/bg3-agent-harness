"""The benchmark series view (seriesview.py) on a copy of the real naut series (runtime/sessions/naut*, read-only:
copied to a temp runtime; skipped where it doesn't exist) and on synthetic runs."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL = HERE.parents[1] / "runtime" / "sessions"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
import seriesview as V  # noqa: E402

NAUT = ("naut", "naut-r2", "naut-r3")
HAVE_NAUT = all((REAL / n / "runner.json").exists() for n in NAUT) and (REAL / "naut" / "runs.jsonl").exists()


def copy_naut(dst: Path) -> None:
    for n in NAUT:
        shutil.copytree(REAL / n, dst / "sessions" / n, ignore=shutil.ignore_patterns("sandbox-home", "reset"))


@unittest.skipUnless(HAVE_NAUT, "needs runtime/sessions/naut* (the the test machine naut series)")
class RealNaut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = Path(tempfile.mkdtemp(prefix="bg3dash-series-"))
        copy_naut(cls.rt)
        cls.recs = [json.loads(x) for x in (cls.rt / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()
                    if x.strip()]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.rt, ignore_errors=True)

    def test_rows_match_the_records(self):
        p = V.series_payload(self.rt, "naut", {})
        self.assertEqual(p["series"], "naut")
        self.assertEqual([r["run"] for r in p["rows"]], [1, 2, 3])
        self.assertIsNone(p["live"])
        for row, rec in zip(p["rows"], self.recs):
            self.assertEqual((row["outcome"], row["clean"], row["playthrough"]), (rec["outcome"], rec["clean"],
                                                                                   rec["playthrough"]))
            self.assertAlmostEqual(row["cost_usd"], rec["cost_usd"], places=2)
            self.assertEqual(row["persona"]["score"], rec["persona"]["score"])
            self.assertEqual(row["latency"]["gaps_over_10s"], rec["latency"]["gaps_over_10s"])
            self.assertEqual(row["latency"]["read_rt_median_ms"], round(rec["latency"]["read_rt_median_ms"], 1))
            self.assertEqual(row["sandbox"], "none")                       # the naut runs were before --sandbox
            self.assertEqual(row["failures"], {"harness": 0, "agent": 0, "unknown": 0, "top": []})
            mt, sp = row["model_time"], rec["model_time"]["split"]
            self.assertEqual(mt["harness_only_s"], round(sp["harness_only_s"], 1))
            self.assertLessEqual(mt["harness_pct"] + mt["model_pct"] + mt["overlap_pct"], 101)
            self.assertEqual(len(mt["top_tools"]), V.TOP_TOOLS)
            self.assertEqual(mt["top_tools"][0]["tool"], max(rec["model_time"]["harness_by_tool"].items(),
                                                             key=lambda kv: kv[1]["s"])[0])
        self.assertEqual(p["rows"][0]["to_milestone_min"], round(self.recs[0]["time_to_milestone_s"] / 60, 1))

    def test_spread_is_the_runs_command_line(self):
        s = V.series_payload(self.rt, "naut", {})["spread"]
        self.assertEqual((s["n"], s["reached"], s["clean"], s["clean_streak"]), (3, 3, 3, 3))
        self.assertEqual(s["line"], V.R.runs_table(V.R.series_records(self.rt / "sessions" / "naut" / "runs.jsonl",
                                                                     "naut")).splitlines()[-1])
        self.assertTrue(s["line"].startswith("3 run(s), 3 clean"))
        cost = {m["metric"]: m for m in s["metrics"]}["cost $"]
        self.assertAlmostEqual(cost["mean"], sum(r["cost_usd"] for r in self.recs) / 3, places=2)
        self.assertIn("series: n=3, reached 3/3", s["summary_text"])

    def test_result_tokens_vs_the_naut_baseline(self):
        """The naut records predate result_tokens: the rows backfill it read-only from the raw streams (vis's
        numbers, research/impl-vis.md), graded against the naut mean; runs.jsonl stays byte for byte."""
        f = self.rt / "sessions" / "naut" / "runs.jsonl"
        before = f.read_bytes()
        V._RT_CACHE.clear()
        p = V.series_payload(self.rt, "naut", {})
        rts = [r["result_tokens"] for r in p["rows"]]
        self.assertEqual([rt["total"] for rt in rts], [41944, 38027, 46255])
        self.assertEqual([rt["n"] for rt in rts], [115, 111, 95])
        self.assertEqual({rt["source"] for rt in rts}, {"stream"})
        self.assertEqual([rt["grade_total"] for rt in rts], ["same", "same", "same"])   # all within ±10 % of the mean
        self.assertEqual([rt["grade_per_result"] for rt in rts], ["same", "better", "worse"])   # 365, 343, 487 / 393
        self.assertEqual(p["result_tok_baseline"]["total"], 42075.3)
        m = {x["metric"]: x for x in p["spread"]["metrics"]}
        self.assertEqual((m["result tok k"]["n"], m["result tok k"]["mean"]), (3, 42.1))
        self.assertEqual(f.read_bytes(), before)
        # a record that has the field is shown as recorded (no backfill), a graded better run is green
        rec = dict(self.recs[0], result_tokens={"total": 30000, "n": 100, "per_result": 300.0, "images": 0})
        self.assertEqual(V.row(rec, self.rt)["result_tokens"]["grade_total"], "better")
        self.assertIsNone(V.row(dict(self.recs[0], result_tokens=None), self.rt)["result_tokens"])
        # the live row: run 3's finished session
        st = {"playthrough": "naut-r3", "series": "naut", "run_index": 4, "runner_pid": 1, "heartbeat": time.time(),
              "run_started": self.recs[2]["started"]}
        live = V.series_payload(self.rt, "naut", st, pid_alive=lambda p: True)["live"]
        self.assertEqual(live["result_tokens"]["total"], 46255)

    def test_frozen_settings(self):
        fz = V.series_payload(self.rt, "naut", {})["frozen"]
        self.assertEqual((fz["until"], fz["runs"], fz["client"]), ("nautiloid_escaped", 3, "claude"))
        self.assertEqual((fz["sandbox"], fz["sandbox_unfirewalled"]), ("none", False))

    def test_live_row_recomputes_a_recorded_run(self):
        """Run 3's files read as if it were still going (run_index 4: no record): the live numbers equal what the
        record says, so the live row and the finished rows agree on every definition."""
        rec = self.recs[2]
        st = {"playthrough": "naut-r3", "series": "naut", "run_index": 4, "runner_pid": 12345,
              "heartbeat": time.time(), "run_started": rec["started"], "session": None}
        live = V.series_payload(self.rt, "naut", st, pid_alive=lambda p: True)["live"]
        self.assertEqual((live["run"], live["outcome"], live["playthrough"]), (4, "running", "naut-r3"))
        self.assertAlmostEqual(live["cost_usd"], rec["cost_usd"], places=2)
        self.assertAlmostEqual(live["tokens_m"], V.R.token_total(rec["tokens"]) / 1e6, places=2)
        self.assertFalse(live["usage_estimated"])
        self.assertEqual(live["latency"]["gaps_over_10s"], rec["latency"]["gaps_over_10s"])
        self.assertEqual(live["story_points"], rec["story"]["points"])
        self.assertAlmostEqual(live["model_time"]["harness_s"], rec["model_time"]["harness_s"], delta=1.0)
        self.assertEqual((live["sessions"], live["in_session"]), (1, False))

    def test_no_live_row(self):
        rec = self.recs[2]
        st = {"playthrough": "naut-r3", "series": "naut", "run_index": 3, "runner_pid": 1,
              "heartbeat": time.time(), "run_started": rec["started"]}
        self.assertIsNone(V.series_payload(self.rt, "naut", st, pid_alive=lambda p: True)["live"])  # recorded
        st["run_index"] = 4
        self.assertIsNone(V.series_payload(self.rt, "other", st, pid_alive=lambda p: True)["live"])  # not this one
        st["heartbeat"] = time.time() - 3600
        self.assertIsNone(V.series_payload(self.rt, "naut", st, pid_alive=lambda p: False)["live"])  # runner gone
        st["heartbeat"] = time.time()
        self.assertEqual(V.series_payload(self.rt, "naut", st, pid_alive=lambda p: False)["live"]["outcome"],
                         "runner gone?")

    def test_default_series_and_listing(self):
        p = V.series_payload(self.rt, None, {})
        self.assertEqual(p["series"], "naut")
        self.assertEqual([a["name"] for a in p["available"]], ["naut"])
        self.assertEqual(V.series_payload(self.rt, None, {"series": "naut-x"})["series"], "naut-x")


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.rt = Path(tempfile.mkdtemp(prefix="bg3dash-series-"))
        self.addCleanup(shutil.rmtree, self.rt, True)

    def test_bad_names_are_refused(self):
        for bad in ("../naut", "a/b", ".hidden", "x" * 200):
            p = V.series_payload(self.rt, bad, {})
            self.assertIsNone(p["series"], bad)
            self.assertIn("not a series name", p["error"])

    def test_empty_runtime(self):
        p = V.series_payload(self.rt, None, {})
        self.assertEqual((p["series"], p["rows"], p["live"]), (None, [], None))
        self.assertEqual(p["spread"]["line"], "no runs recorded yet")

    def test_record_fields_and_sandbox(self):
        d = self.rt / "sessions" / "sb"
        d.mkdir(parents=True)
        (d / "series.json").write_text(json.dumps({"series": "sb", "runs": 2, "until": "m", "client": "codex",
                                                   "sandbox": "container", "sandbox_unfirewalled": True}))
        recs = [{"series": "sb", "run": 1, "playthrough": "sb", "outcome": "timeout", "clean": False,
                 "cap": "time", "why": "time cap used up", "sandbox": "container", "sandbox_unfirewalled": True,
                 "client": "codex", "failures": [{"kind": "crash", "class": "harness", "n": 2},
                                                  {"kind": "timeout", "class": "unknown"}],
                 "persona": {"status": "judge_failed"}, "sandbox_change": "sandbox: none → container"}]
        (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        p = V.series_payload(self.rt, "sb", {})
        r = p["rows"][0]
        self.assertEqual((r["sandbox"], r["sandbox_unfirewalled"], r["client"]), ("container", True, "codex"))
        self.assertEqual((r["failures"]["harness"], r["failures"]["unknown"]), (2, 1))
        self.assertEqual(r["failures"]["top"][0], {"kind": "crash(h)", "n": 2})
        self.assertEqual((r["persona"]["cell"], r["persona"]["score"]), ("fail", None))
        self.assertIsNone(r["model_time"])
        self.assertIsNone(r["latency"])
        self.assertIsNone(r["result_tokens"])                       # no field and no session files
        self.assertEqual(r["changes"], ["sandbox: none → container"])
        self.assertEqual((p["frozen"]["sandbox"], p["frozen"]["sandbox_unfirewalled"]), ("container", True))
        self.assertEqual(p["spread"]["clean_streak"], 0)

    def test_adj_model_time_and_api_floor(self):
        d = self.rt / "sessions" / "fl"
        d.mkdir(parents=True)
        mt = lambda adj, floor: {"sessions": 1, "wall_s": 400.0, "split": {"harness_only_s": 200.0},   # noqa: E731
                                 "adjusted_model_s": adj + 50, "adjusted_model_s_ref_ttft": adj,
                                 "api_floor": {"n": 60, "p10_s": floor, "median_s": floor + 0.3}}
        recs = [{"series": "fl", "run": 1, "playthrough": "fl", "outcome": "reached", "clean": True,
                 "model_time": mt(193.0, 1.0)},
                {"series": "fl", "run": 2, "playthrough": "fl-r2", "outcome": "reached", "clean": True,
                 "model_time": mt(167.0, 1.55)}]
        (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        p = V.series_payload(self.rt, "fl", {})
        self.assertEqual([(r["model_time"]["adj_model_s"], r["model_time"]["api_floor_s"]) for r in p["rows"]],
                         [(193.0, 1.0), (167.0, 1.55)])
        self.assertIn("API floor differs (run 1 1.00 s … run 2 1.55 s", p["spread"]["floor_note"])
        metrics = {m["metric"]: m for m in p["spread"]["metrics"]}
        self.assertEqual((metrics["adj model min"]["mean"], metrics["API floor s"]["n"]), (3.0, 2))

    def test_live_usage_of_a_running_stream(self):
        """A play stream without its result yet: live tokens, priced at the run's cost per token so far."""
        d = self.rt / "sessions" / "sb-r2"
        d.mkdir(parents=True)
        t0 = time.time() - 60

        def msg(mid, out):
            return {"type": "assistant", "message": {"id": mid, "usage": {"input_tokens": 10, "output_tokens": out,
                                                                          "cache_read_input_tokens": 100}}}
        (d / "S001.jsonl").write_text(json.dumps(msg("a", 0)) + "\n" + json.dumps(
            {"type": "result", "total_cost_usd": 2.0, "usage": {"input_tokens": 20, "output_tokens": 30,
                                                                "cache_read_input_tokens": 950}}) + "\n")
        (d / "S002.jsonl").write_text(json.dumps(msg("m1", 390)) + "\n")
        os.utime(d / "S001.jsonl", (t0 + 10, t0 + 10))
        u = V.live_usage(d, t0)
        self.assertEqual((u["tokens_m"], u["running_streams"], u["estimated"]), (round(1500 / 1e6, 2), 1, True))
        self.assertAlmostEqual(u["cost_usd"], 3.0)
        # the status names the series run: the live row carries it, a session playing now counts
        st = {"playthrough": "sb-r2", "series": "sb", "run_index": 2, "runner_pid": 1, "heartbeat": time.time(),
              "run_started": t0, "session": "S002", "started": t0 + 20, "session_ended": t0 + 15}
        (d / "runner.json").write_text(json.dumps({"history": [{"kind": "play", "session": "S001",
                                                                "started": t0 + 1}]}))
        live = V.series_payload(self.rt, "sb", st, pid_alive=lambda p: True,
                                latency_live={"idle_median_s": 1.234, "idle_p90_s": 4, "long_gaps": {"total": 2},
                                              "rt_read_median_ms": 77.7, "current_idle_s": 3.1})["live"]
        self.assertEqual((live["sessions"], live["in_session"], live["cost_usd"]), (2, True, 3.0))
        self.assertEqual(live["latency_session"], {"idle_median_s": 1.23, "idle_p90_s": 4.0, "gaps_over_10s": 2,
                                                   "read_rt_median_ms": 77.7, "current_idle_s": 3.1})


if __name__ == "__main__":
    unittest.main()
