#!/usr/bin/env python3
"""Unit tests for the benchmark fields' play.py wiring (runner/runs.py re-exports bg3harness.runs, whose own tests are
packages/harness/tests/test_runs.py, and the bench parts): version pins and the series' drift refusal, the cost/token
caps ending a run, the series summary, and the persona judge / model time in the run record (a fake judge client: a python
one-liner in place of `claude -p`). Never launches claude or the game.

    python3 -m unittest runner/test_runs.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: E402
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
import legacy_series as lser
import runs as R  # noqa: E402
from test_play_series import _res, _stream, _use  # noqa: E402
import test_judge as TJ  # noqa: E402  (its stream builders and CHARACTER.md fixture)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(ragent.client_setup, "run_files", lambda *a: {}),   # main(): no tool dump (uv, the bridge)
                  mock.patch.dict(os.environ)):   # main(): session_env sets BG3AI_PROMPT_CONDITION
            p.start()
            self.addCleanup(p.stop)


# ======================================================================== the bench re-exports

class Shims(unittest.TestCase):
    def test_runs_reexports_the_harness_core(self):
        from bg3harness import runs as core
        for name in ("classify_run", "UsageMeter", "claude_totals", "run_metrics", "backfill_model_time",
                     "model_time_field", "HARNESS", "METRIC_FIELDS", "CLAUDE_STREAM_RE"):
            self.assertIs(getattr(R, name), getattr(core, name), name)
        self.assertIs(R.model_time, core.model_time)

    def test_runs_reexports_the_bench_and_its_contract_copies_agree(self):
        """The benchmark parts live in bg3bench (runs.py re-exports them); bg3bench.records reads run records with its
        own copies of model_time's headline key and floor limit and of runs.py's failure classes: they must agree."""
        from bg3bench import pins, records, resets, scenario_v1, series
        self.assertIs(R.series_caps, series.series_caps)
        self.assertIs(R.version_pins, pins.version_pins)
        self.assertIs(R.lock_tree, resets.lock_tree)
        self.assertIs(R.runs_table, records.runs_table)
        self.assertIs(R.load_scenario, scenario_v1.load_scenario)
        self.assertEqual((records.HEADLINE, records.FLOOR_DIFF_S), (R.model_time.HEADLINE, R.model_time.FLOOR_DIFF_S))
        self.assertEqual((records.HARNESS, records.AGENT, records.UNKNOWN), (R.HARNESS, R.AGENT, R.UNKNOWN))
        from bg3harness import runs as core
        for started, reached in ((100.0, 550.04), (100.0, None), (None, 5.0)):
            self.assertEqual(core.time_to_milestone(started, reached), records.time_to_milestone(started, reached))
        self.assertEqual(records.token_total({"input": 1, "output": None, "cache_read": 3}),
                         R.token_total({"input": 1, "output": None, "cache_read": 3}))


# ======================================================================== caps

class KnowledgeLock(TmpDir):
    """FAIR-2: play/knowledge's files are read-only for a --runs series, and their modes come back after."""

    def tree(self) -> Path:
        k = self.tmp / "knowledge"
        (k / "playbooks").mkdir(parents=True)
        (k / "a.md").write_text("a")
        (k / "playbooks" / "b.md").write_text("b")
        (k / "x.sh").write_text("#!/bin/sh")
        (k / "x.sh").chmod(0o755)
        (k / "a.md").chmod(0o640)
        return k

    def modes(self, k: Path) -> dict:
        return {str(p.relative_to(k)): p.stat().st_mode & 0o777 for p in k.rglob("*") if p.is_file()}

    def test_a_series_locks_for_its_duration_only(self):
        k, rec = self.tree(), self.tmp / "rt" / "lock.json"
        before, seen = self.modes(k), []

        def fake_series(*a, **kw):
            seen.append(self.modes(k))
            return 0
        with mock.patch.object(renv, "KNOWLEDGE_DIR", k), mock.patch.object(renv, "KNOWLEDGE_LOCK", rec), \
                mock.patch.object(lser, "run_series", side_effect=fake_series), \
                mock.patch.object(lser, "current_pins", return_value={}), \
                mock.patch.object(lser, "series_meta", return_value={}), \
                mock.patch.dict(os.environ, {"BG3AI_KNOWLEDGE_LOCK": "1"}), \
                mock.patch("bg3ai.skipper.start_background", return_value=None), \
                mock.patch.object(rpolicy.ramwatch, "start_background", return_value=None), \
                mock.patch.object(ragent.signal, "signal"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = P.main(["run", "--playthrough", "naut", "--until", "nautiloid_escaped", "--runs", "2"])
        self.assertEqual((rc, len(seen)), (0, 1))
        self.assertTrue(all(m & 0o222 == 0 for m in seen[0].values()))
        self.assertEqual(self.modes(k), before)
        self.assertFalse(rec.exists())


# ======================================================================== the runs command

class Summary(unittest.TestCase):
    RECS = [
        {"run": 1, "playthrough": "naut", "outcome": "reached", "clean": True, "wall_min": 12.5, "sessions": 1,
         "cost_usd": 4.8, "started": 1000.0, "reached_at": 1693.0, "caps": {"max_cost": 30.0},
         "tokens": {"input": 200, "output": 18000, "cache_read": 6_500_000, "cache_write": 110_000}},
        {"run": 2, "playthrough": "naut-r2", "outcome": "reached", "clean": True, "wall_min": 12.8, "sessions": 1,
         "cost_usd": 4.2, "time_to_milestone_s": 708.0, "latency": {"idle_median_s": 0.5},
         "tokens": {"input": 200, "output": 18600, "cache_read": 6_100_000, "cache_write": 80_000}},
        {"run": 3, "playthrough": "naut-r3", "outcome": "over_budget", "cap": "cost", "clean": False,
         "wall_min": 30.0, "sessions": 2, "cost_usd": 10.0, "time_to_milestone_s": None,
         "latency": {"idle_median_s": 1.5}, "version_drift": ["repo.commit: a → b"],
         "caps": {"max_cost": 10.0}, "failures": [{"kind": "cost_cap", "class": "agent"}]}]


# ======================================================================== play.py wiring

class Wiring(TmpDir):
    def setUp(self):
        super().setUp()
        self.saved = {n: getattr(testenv.home(n), n) for n in ("RUNTIME", "PLAYTHROUGHS", "read_actions_since", "current_pins")}
        self.addCleanup(lambda: [setattr(testenv.home(n), n, v) for n, v in self.saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        rpolicy.read_actions_since = lambda t, path=None: []

    PINS = {"game_build": "25605617", "bg3le": {"sha256": "a", "source": "v0.3.6-dirty"}, "mod": {"sha1": "m"},
            "repo": {"commit": "c1", "dirty": False, "diff": None}}

    def runner(self, pt="naut-r2", k=2, **kw) -> lpt.Runner:
        return lpt.Runner(rstate.Options(pt, None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", run_index=k, **kw))

    def test_series_json_freezes_pins_and_caps_once(self):
        caps = {"run_hours": 4, "max_cost": 30.0, "max_tokens": None}
        lser.freeze_series("naut", 3, "nautiloid_escaped", {"p": 1}, self.PINS, caps)
        lser.freeze_series("naut", 3, "nautiloid_escaped", {"p": 2}, {"other": 1}, {"max_cost": 1})
        meta = lser.series_meta("naut")
        self.assertEqual((meta["profile"], meta["pins"], meta["caps"]), ({"p": 1}, self.PINS, caps))
        # a series from before pins and caps gets them from its next run, the rest untouched
        (renv.RUNTIME / "sessions" / "old").mkdir(parents=True)
        (renv.RUNTIME / "sessions" / "old" / "series.json").write_text(json.dumps({"series": "old", "profile": {"p": 0}}))
        lser.freeze_series("old", 3, "nautiloid_escaped", {"p": 9}, self.PINS, caps)
        meta = lser.series_meta("old")
        self.assertEqual((meta["profile"], meta["pins"], meta["caps"]), ({"p": 0}, self.PINS, caps))

    def test_a_drifted_run_is_refused_unless_allowed(self):
        lser.freeze_series("naut", 3, "nautiloid_escaped", {}, self.PINS, {})
        same = json.loads(json.dumps(self.PINS))
        r = self.runner()
        self.assertIsNone(lser.gate_series_run("naut", r, same, False))
        self.assertEqual((r.opt.pins, r.opt.version_drift), (same, None))
        drifted = json.loads(json.dumps(self.PINS))
        drifted["repo"]["commit"] = "c2"
        msg = lser.gate_series_run("naut", self.runner(), drifted, False)
        self.assertIn("REFUSING run 2", msg)
        self.assertIn("repo.commit: c1 → c2", msg)
        r = self.runner()
        self.assertIsNone(lser.gate_series_run("naut", r, drifted, True))
        self.assertEqual(r.opt.version_drift, ["repo.commit: c1 → c2"])
        r.begin_run(time.time())
        self.assertEqual(r.state["run"]["version_drift"], ["repo.commit: c1 → c2"])
        # a run under way (a self-restart on changed code) keeps the pins it started with: never refused mid-run
        r2 = self.runner()
        self.assertIsNone(lser.gate_series_run("naut", r2, {"repo": {"commit": "c3"}}, False))
        self.assertEqual(r2.opt.pins, drifted)

    def test_cli_refuses_a_cap_change_in_a_series(self):
        lser.freeze_series("naut", 3, "nautiloid_escaped", {}, self.PINS,
                        {"run_hours": 4, "max_cost": 30.0, "max_tokens": None})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(P.main(["run", "--playthrough", "naut", "--until", "nautiloid_escaped", "--runs", "3",
                                     "--max-cost", "50"]), 2)
            self.assertEqual(P.main(["run", "--playthrough", "x", "--max-tokens", "5"]), 2)        # needs --until
            self.assertEqual(P.main(["run", "--playthrough", "x", "--until", "nautiloid_escaped",
                                     "--max-cost", "0"]), 2)
        self.assertIn("max_cost: 30.0 → 50.0", err.getvalue())
        self.assertIn("--allow-cap-change", err.getvalue())

    def test_usage_caps_end_the_run(self):
        r = self.runner(max_cost=5.0)
        self.assertTrue(r.has_usage_caps())
        self.assertFalse(r.check_usage_caps(4.99, 10))
        self.assertTrue(r.check_usage_caps(5.0, 10))
        self.assertEqual(r.cap_hit["cap"], "cost")
        self.assertIn("--max-cost $5", r.cap_hit["why"])
        self.assertTrue(r.check_usage_caps(0, 0))                   # once hit, stays hit
        self.assertFalse(self.runner("naut-r3", 3).has_usage_caps())

    def test_a_spend_cap_ends_the_run_but_the_record_keeps_the_series_caps(self):
        r = self.runner(max_cost=None, spend_cap=5.0, run_hours=1.0)   # --max-cost 5 on a scenario with no cost cap
        self.assertTrue(r.has_usage_caps())
        self.assertFalse(r.check_usage_caps(4.99, 10))
        self.assertTrue(r.check_usage_caps(5.0, 10))
        self.assertIn("--max-cost $5", r.cap_hit["why"])
        rec = r.write_run_record(time.time() - 60, "over_budget", r.cap_hit["why"])
        self.assertEqual((rec["caps"]["max_cost"], rec["spend_cap"], rec["cap_changes"]), (None, 5.0, None))

    def test_the_run_record_carries_the_benchmark_fields(self):
        r = self.runner(max_tokens=1000, run_hours=4.0)
        r.opt.pins = self.PINS
        t0 = time.time() - 600
        r.begin_run(t0)
        d = r.state.dir
        _stream(d / "S001.jsonl", _use("1", "mcp__bg3__look", {}), _res("1", "door DC 12"),
                {"type": "result", "total_cost_usd": 1.0, "num_turns": 3, "usage": {"output_tokens": 1500}})
        _stream(d / "S001.fights.jsonl", {"party": [{"name": "Tav", "downed": 1, "died": False}]})
        (d / "S001.latency.json").write_text(json.dumps({"idle_s": {"all": {"n": 5, "median": 0.4, "p90": 2.0}},
                                                         "long_gaps": {"total": 1}}))
        _stream(d / "progress_history.jsonl", {"session": "S001", "t": t0 + 500, "points": 42, "events": 7})
        r.check_usage_caps(1.0, 1500)
        rec = r.write_run_record(t0, "over_budget", r.cap_hit["why"])
        self.assertEqual((rec["outcome"], rec["cap"], rec["clean"]), ("over_budget", "tokens", False))
        # the DC in S001's result is a fairness LEAK (harness), so the cap counts as unknown, like a timeout would
        self.assertIn(("tokens_cap", "unknown"), [(f["kind"], f["class"]) for f in rec["failures"]])
        self.assertEqual(rec["caps"], {"run_hours": 4.0, "max_cost": None, "max_tokens": 1000})
        self.assertEqual((rec["pins"], rec["version_drift"]), (self.PINS, None))
        self.assertIsNone(rec["time_to_milestone_s"])
        self.assertEqual(rec["deaths"], {"fights": 1, "downs": 1, "deaths": 0, "wipes": 0})
        self.assertEqual(rec["fairness_flags"]["kinds"], {"dc_outside_roll": 1})
        self.assertEqual(rec["latency"]["idle_median_s"], 0.4)
        self.assertEqual(rec["story"]["points"], 42)
        self.assertEqual((rec["result_tokens"]["n"], rec["result_tokens"]["source"]), (1, "stream"))   # S001.jsonl
        self.assertEqual(rec["milestones_reached"], 0)
        self.assertNotIn("sandbox_change", rec)
        line = json.loads((renv.RUNTIME / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["series"], line["run"], line["cap"]), ("naut", 2, "tokens"))
        # the time cap: outcome timeout, cap "time"; reached: time to the milestone
        rec = lser.run_record(d, t0, t0 + 600, {}, "naut-r2", [], "timeout", "time cap used up")
        self.assertEqual(rec["cap"], "time")
        self.assertIn("timeout", [f["kind"] for f in rec["failures"]])
        rec = lser.run_record(d, t0, t0 + 600, {"id": "nautiloid_escaped", "t": t0 + 450.0}, "naut-r2", [])
        self.assertEqual((rec["outcome"], rec["cap"], rec["time_to_milestone_s"]), ("reached", None, 450.0))
        r.opt.sandbox_change = "sandbox: container → none"                  # --allow-sandbox-change
        self.assertEqual(r.write_run_record(t0, "timeout")["sandbox_change"], "sandbox: container → none")

    def test_runs_command_prints_the_summary(self):
        d = renv.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        (d / "runs.jsonl").write_text("".join(json.dumps({"series": "naut", **r}) + "\n" for r in Summary.RECS))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["runs", "--playthrough", "naut"]), 0)
        self.assertIn("series: n=3, reached 2/3", buf.getvalue())
        self.assertIn("to milestone min", buf.getvalue())


# ======================================================================== persona judge + model time in the record

REAL_CLAUDE_CLI = lser.judge.ClaudeCLI   # its output parser; the tests patch judge.ClaudeCLI with FakeJudgeCLI


class FakeJudgeCLI:
    """A judge client in judge.ClaudeCLI's shape whose command is a python one-liner printing a `claude -p
    --output-format json` result: the verdict, a cost, optionally after a sleep or as an error. Counts its calls."""

    def __init__(self, model="judge-x", verdict=None, cost=0.02, sleep=0.0, fail=False, log: Path | None = None):
        self.model, self.verdict, self.cost, self.sleep, self.fail, self.log = model, verdict, cost, sleep, fail, log

    def command(self) -> list[str]:
        out = json.dumps({"type": "result", "is_error": self.fail, "total_cost_usd": self.cost,
                          "result": "boom" if self.fail else self.verdict})
        return [sys.executable, "-c", f"import sys, time; p = sys.stdin.read(); time.sleep({self.sleep}); "
                f"open({str(self.log)!r}, 'a').write('call\\n') if {self.log is not None} else None; "
                f"print({out!r})"]

    def parse(self, stdout: str):
        return REAL_CLAUDE_CLI.parse(self, stdout)


class Judging(TmpDir):
    T0 = 1_000_000.0
    VERDICT = TJ._verdict(7.5, {1: 7.5})

    def setUp(self):
        super().setUp()
        self.saved = {n: getattr(testenv.home(n), n) for n in ("RUNTIME", "PLAYTHROUGHS", "read_actions_since")}
        self.addCleanup(lambda: [setattr(testenv.home(n), n, v) for n, v in self.saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        rpolicy.read_actions_since = lambda t, path=None: []
        for name, value in (("PLAYTHROUGHS", renv.PLAYTHROUGHS), ("DEFAULT_MODEL", "judge-x")):
            patcher = mock.patch.object(lser.judge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = self.tmp / "judge-calls.log"
        (renv.PLAYTHROUGHS / "naut" / "memory").mkdir(parents=True)
        (renv.PLAYTHROUGHS / "naut" / "memory" / "CHARACTER.md").write_text(TJ.CHARACTER)
        self.dir = renv.RUNTIME / "sessions" / "naut"
        self.dir.mkdir(parents=True)

    def play(self, t0: float) -> None:
        """S001 in naut: one in-character choice inside a run started at t0."""
        TJ._write(self.dir, "S001", [
            TJ._use("b", "dialogue_advance", {}), TJ._res("b", TJ.LAEZEL),
            TJ._say("Asking for her plan, like a sergeant would."),
            TJ._use("c", "choose_dialogue", {"option": 2}), TJ._res("c", "conversation ended"),
        ], {"b": t0 + 2, "c": t0 + 3})

    def fake(self, **kw):
        kw.setdefault("verdict", self.VERDICT)
        return mock.patch.object(lser.judge, "ClaudeCLI", lambda model: FakeJudgeCLI(model, log=self.calls, **kw))

    def runner(self, **kw) -> lpt.Runner:
        return lpt.Runner(rstate.Options("naut", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", **{"run_index": 1, **kw}))

    def n_calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    # ---------------------------------------------------------------- the runner

    def test_the_record_gets_persona_and_model_time_after_it_is_written(self):
        t0 = time.time() - 60
        self.play(t0)
        r = self.runner(judge=True, judge_model="judge-x")
        r.begin_run(t0)
        with self.fake(cost=0.03):
            rec = r.write_run_record(t0, "timeout", "time cap used up")
        on_disk = R.series_records(self.dir / "runs.jsonl", "naut")
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0], rec)
        p = rec["persona"]
        self.assertEqual((p["status"], p["score"], p["judge_model"], p["decisions"], p["cost_usd"]),
                         ("ok", 7.5, "judge-x", 1, 0.03))
        self.assertEqual(rec["cost_usd"], 0)                       # the judge's cost is not the run's (capped) cost
        self.assertIn("model_time", rec)
        self.assertNotIn("measured", rec["model_time"])
        self.assertEqual(self.n_calls(), 1)

    def test_judge_off_and_judge_failure_never_fail_the_run(self):
        t0 = time.time() - 60
        self.play(t0)
        r = self.runner()                                          # judge off (a single run's default)
        r.begin_run(t0)
        with self.fake():
            rec = r.write_run_record(t0, "reached")
        self.assertNotIn("persona", rec)
        self.assertIn("model_time", rec)
        self.assertEqual(self.n_calls(), 0)
        r = self.runner(judge=True, judge_model="judge-x", run_index=2)
        r.begin_run(t0)
        with self.fake(fail=True, cost=0.01):
            rec = r.write_run_record(t0, "reached")
        self.assertEqual(rec["persona"]["status"], "judge_failed")
        self.assertIn("boom", rec["persona"]["error"])
        self.assertGreater(rec["persona"]["cost_usd"], 0.0)         # an is_error answer's cost is still counted
        self.assertEqual(R.series_records(self.dir / "runs.jsonl", "naut")[-1]["persona"], rec["persona"])
        with mock.patch.object(lser.judge, "judge_run", side_effect=RuntimeError("bug")):
            p = lser.judge_record(self.dir, rec, "judge-x")
        self.assertEqual((p["status"], p["error"]), ("judge_failed", "RuntimeError: bug"))

    def test_the_judge_has_an_overall_timeout(self):
        t0 = time.time() - 60
        self.play(t0)
        rec = {"playthrough": "naut", "started": t0, "t": time.time(), "model": "opus"}
        t = time.time()
        with self.fake(sleep=5):
            p = lser.judge_record(self.dir, rec, "judge-x", timeout=1.5)
        self.assertLess(time.time() - t, 4.8)  # before the 5 s judge could answer (4 s was a tight margin under load)
        self.assertEqual(p["status"], "judge_failed")
        self.assertIn("timed out", p["error"])
        with self.fake():
            p = lser.judge_record(self.dir, rec, "judge-x", stop=lambda: True)
        self.assertIn("interrupted", p["error"])
        self.assertEqual(self.n_calls(), 0)

    # ---------------------------------------------------------------- the CLI

    def cli(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = P.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_play_judge_rejudges_a_record_in_place(self):
        t0 = time.time() - 600
        self.play(t0)
        old = [{"series": "naut", "run": 1, "playthrough": "naut", "started": round(t0, 1), "t": round(t0 + 60, 1),
                "model": "opus", "cost_usd": 4.0},
               {"series": "naut", "run": 2, "playthrough": "naut-r2", "started": round(t0 + 100, 1),
                "t": round(t0 + 160, 1), "model": "opus", "cost_usd": 4.0}]
        (self.dir / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
        (self.dir / "series.json").write_text(json.dumps({"series": "naut", "profile": {}}))
        with self.fake():
            rc, out, err = self.cli("judge", "--playthrough", "naut", "--run", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("run 1 (naut): score 7.5/10 by judge-x on 1 decision(s), $0.02", out)
        recs = R.series_records(self.dir / "runs.jsonl", "naut")
        self.assertEqual((recs[0]["persona"]["score"], recs[0]["cost_usd"]), (7.5, 4.0))
        self.assertIn("model_time", recs[0])
        self.assertNotIn("persona", recs[1])
        self.assertEqual(lser.series_meta("naut")["judge_model"], "judge-x")          # frozen from now on
        # no --run: only the runs without a score; another judge model is refused
        with self.fake():
            rc, out, _ = self.cli("judge", "--playthrough", "naut")
        self.assertEqual(rc, 1)                                                     # run 2: no CHARACTER.md
        self.assertNotIn("run 1", out)
        self.assertIn("run 2 (naut-r2): no_character", out)
        with self.fake():
            rc, _, err = self.cli("judge", "--playthrough", "naut", "--run", "1", "--judge-model", "other")
        self.assertEqual(rc, 2)
        self.assertIn("judge_model: judge-x → other", err)
        self.assertEqual(self.cli("judge", "--playthrough", "naut", "--run", "9")[0], 1)
        rc, out, _ = self.cli("runs", "--playthrough", "naut")
        self.assertIn("persona score", out)
        self.assertEqual(self.n_calls(), 1)

    def test_run_flags(self):
        calls = []
        patches = [mock.patch.object(lcli, "acquire_lock", lambda: None), mock.patch.object(lser, "current_pins", dict),
                   mock.patch.object(lser, "run_series", lambda *a: calls.append(a) or 0),
                   mock.patch.object(lpt, "Runner", lambda opt: mock.Mock(run=lambda: calls.append((opt,)) or 0)),
                   mock.patch("bg3ai.skipper.start_background", return_value=None),
                   mock.patch.object(rpolicy.ramwatch, "start_background", return_value=None),
                   mock.patch.object(ragent.signal, "signal"), mock.patch.dict(renv.os.environ, {}, clear=False)]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        renv.os.environ.pop("BG3AI_JUDGE_MODEL", None)
        series = ("run", "--playthrough", "naut", "--until", "nautiloid_escaped", "--runs", "3")
        self.assertEqual(self.cli(*series)[0], 0)
        self.assertEqual((calls[-1][0].judge, calls[-1][0].judge_model), (True, "judge-x"))   # on for a series
        self.assertEqual(self.cli(*series, "--no-judge")[0], 0)
        self.assertEqual((calls[-1][0].judge, calls[-1][0].judge_model), (False, None))
        self.assertEqual(self.cli("run", "--playthrough", "x", "--until", "nautiloid_escaped")[0], 0)
        self.assertFalse(calls[-1][0].judge)                                                 # off for one run
        self.assertEqual(self.cli("run", "--playthrough", "x", "--until", "nautiloid_escaped", "--judge",
                                  "--judge-model", "j2")[0], 0)
        self.assertEqual((calls[-1][0].judge, calls[-1][0].judge_model), (True, "j2"))
        n = len(calls)
        for args, want in ((("run", "--playthrough", "x", "--judge"), "needs --until"),
                           ((*series, "--judge-model", "claude-opus-5-5"), "is the model under test")):
            rc, _, err = self.cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn(want, err)
        # a series' judge model is frozen like its caps
        lser.freeze_series("naut", 3, "nautiloid_escaped", {}, judge_model="judge-x")
        rc, _, err = self.cli(*series, "--judge-model", "j2")
        self.assertEqual(rc, 2)
        self.assertIn("judge_model: judge-x → j2", err)
        with mock.patch.dict(renv.os.environ, {"BG3AI_JUDGE_MODEL": "j3"}):
            self.assertIn("judge_model: judge-x → j3", self.cli(*series)[2])
        self.assertEqual(len(calls), n)
        self.assertEqual(self.cli(*series, "--judge-model", "j2", "--allow-judge-change")[0], 0)
        self.assertEqual((calls[-1][0].judge_model, calls[-1][0].judge_change), ("j2", "judge_model: judge-x → j2"))
        lser.freeze_series("naut", 3, "nautiloid_escaped", {}, judge_model="other")
        self.assertEqual(lser.series_meta("naut")["judge_model"], "judge-x")                  # written once


if __name__ == "__main__":
    unittest.main(verbosity=1)
