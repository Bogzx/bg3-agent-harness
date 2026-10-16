#!/usr/bin/env python3
"""Unit tests for the benchmark fields of runner/runs.py and their play.py wiring: version pins and the series'
drift refusal, the cost/token caps, the run metrics (time to the milestone, deaths, fairness flags, latency, story
points) and the series summary. Never launches claude or the game.

    python3 -m unittest runner/test_runs.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import play as P  # noqa: E402
import runs as R  # noqa: E402
from test_play import _res, _stream, _use  # noqa: E402


def _git(answers: dict):
    """A fake runs._git: {first arg: stdout} (None: git failed)."""
    return lambda cwd, *args: answers.get(args[0])


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ======================================================================== pins

class Pins(TmpDir):
    def test_buildinfo_mod_version_and_digest(self):
        info = R.buildinfo("bg3le commit: 3728a68\npatch: 0001-a.patch 126c\npatch: 0002-b.patch 51bc\n"
                           "built: 2026-10-13\nsha256: 7d4c\n")
        self.assertEqual(info, {"commit": "3728a68", "sha256": "7d4c", "patches": ["0001-a.patch 126c",
                                                                                    "0002-b.patch 51bc"]})
        self.assertEqual(R.mod_version('<attribute id="Version64" type="int64" value="36028797018963968"/>'),
                         "1.0.0.0")
        v = (1 << 55) | (2 << 47) | (3 << 31) | 4
        self.assertEqual(R.mod_version(f'id="Version64" type="int64" value="{v}"'), "1.2.3.4")
        self.assertIsNone(R.mod_version("<save/>"))
        self.assertEqual(R.tree_digest({"a": "1", "b": "2"}), R.tree_digest({"b": "2", "a": "1"}))
        self.assertNotEqual(R.tree_digest({"a": "1"}), R.tree_digest({"a": "2"}))

    def test_version_pins(self):
        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "BUILDINFO").write_text("bg3le commit: abc\npatch: 0001-x.patch 1\nsha256: feed\n")
        meta = self.tmp / "meta.lsx"
        meta.write_text('<attribute id="Version64" type="int64" value="36028797018963968"/>')
        git = _git({"rev-parse": "92634f4\n", "status": " M runner/runs.py\n?? runner/new.py\n",
                    "diff": "diff --git a/runner/runs.py\n", "describe": "v0.3.6-dirty\n"})
        pins = R.version_pins(self.tmp, "25605617", dist, meta, {"Mods/x.lua": "aa"}, git)
        self.assertEqual(pins["game_build"], "25605617")
        self.assertEqual(pins["bg3le"], {"source": "v0.3.6-dirty", "local_changes": True, "commit": "abc",
                                         "sha256": "feed", "patches": 1})
        self.assertEqual(pins["mod"]["version"], "1.0.0.0")
        self.assertEqual((pins["repo"]["commit"], pins["repo"]["dirty"]), ("92634f4", True))
        self.assertEqual(len(pins["repo"]["diff"]), 16)
        # another edit → another diff hash; a clean tree has none
        other = R.repo_pin(self.tmp, _git({"rev-parse": "92634f4", "status": " M runner/runs.py\n",
                                           "diff": "something else"}))
        self.assertNotEqual(other["diff"], pins["repo"]["diff"])
        clean = R.repo_pin(self.tmp, _git({"rev-parse": "92634f4", "status": "", "diff": ""}))
        self.assertEqual(clean, {"commit": "92634f4", "dirty": False, "diff": None})
        # no BUILDINFO: the binary's own sha256; no git, no manifest: None, never an exception
        (dist / "BUILDINFO").unlink()
        (dist / "libbg3le.so").write_bytes(b"elf")
        bare = R.version_pins(self.tmp, "", dist, self.tmp / "missing.lsx", {}, _git({}))
        self.assertEqual(len(bare["bg3le"]["sha256"]), 64)
        self.assertEqual((bare["game_build"], bare["mod"], bare["repo"]["commit"], bare["bg3le"]["local_changes"]),
                         (None, {"version": None, "sha1": None}, None, None))

    def test_pin_drift(self):
        first = {"game_build": "1", "bg3le": {"sha256": "a", "source": "v0.3.6-dirty"}, "mod": {"sha1": "m"},
                 "repo": {"commit": "c1", "dirty": False, "diff": None}}
        self.assertEqual(R.pin_drift(first, json.loads(json.dumps(first))), [])
        self.assertEqual(R.pin_drift(None, first), [])
        now = json.loads(json.dumps(first))
        now["game_build"] = "2"
        now["repo"].update(commit="c2", dirty=True, diff="d")
        self.assertEqual(R.pin_drift(first, now), ["game_build: 1 → 2", "repo.commit: c1 → c2",
                                                   "repo.dirty: False → True", "repo.diff: None → d"])


# ======================================================================== caps

class Caps(TmpDir):
    def test_series_caps(self):
        cli = {"run_hours": 4, "max_cost": 30.0, "max_tokens": None}
        self.assertEqual(R.series_caps(None, cli, False), (cli, None, []))           # the first run sets them
        frozen = {"run_hours": 4, "max_cost": 30.0, "max_tokens": 9_000_000}
        self.assertEqual(R.series_caps(frozen, {"run_hours": None, "max_cost": None, "max_tokens": None}, False),
                         (frozen, None, []))                                         # later runs take series.json's
        self.assertEqual(R.series_caps(frozen, cli, False)[1], None)                # same values, one left out
        caps, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, False)
        self.assertIn("--allow-cap-change", err)
        self.assertEqual(changes, ["max_cost: 30.0 → 50.0"])
        caps, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, True)
        self.assertEqual((caps["max_cost"], err, changes), (50.0, None, ["max_cost: 30.0 → 50.0"]))

    def test_usage_meter(self):
        def msg(mid, out):
            return {"type": "assistant", "message": {"id": mid, "usage": {"input_tokens": 10, "output_tokens": out,
                                                                          "cache_read_input_tokens": 100}}}
        m = R.UsageMeter()
        m.on_event(msg("m1", 5))
        m.on_event(msg("m1", 20))            # the same message again (a later content block): counted once
        m.on_event(msg("m2", 0))
        self.assertEqual(m.tokens, 130 + 110)
        self.assertEqual(m.cost, 0.0)        # no result yet: no cost per token known
        m.on_event({"type": "result", "total_cost_usd": 2.0,
                    "usage": {"input_tokens": 20, "output_tokens": 30, "cache_read_input_tokens": 950}})
        self.assertEqual((m.tokens, m.cost), (1000, 2.0))   # the result's exact totals replace the live sum
        m.on_event(msg("m3", 390))
        self.assertEqual(m.tokens, 1500)
        self.assertAlmostEqual(m.cost, 3.0)  # 500 live tokens at the run's $0.002 per token
        base = R.UsageMeter(10.0, 5000)      # earlier processes of the run
        base.on_event(msg("x", 890))
        self.assertEqual((base.tokens, base.cost), (6000, 12.0))
        self.assertIsNone(R.cap_exceeded({"max_cost": None, "max_tokens": None}, 99, 10 ** 9))
        self.assertEqual(R.cap_exceeded({"max_cost": 12.0}, 12.0, 0), "cost")
        self.assertEqual(R.cap_exceeded({"max_cost": 50.0, "max_tokens": 6000}, 12.0, 6000), "tokens")
        self.assertIsNone(R.cap_exceeded(None, 1, 1))

    def test_claude_totals(self):
        t0 = time.time() - 10
        _stream(self.tmp / "S001.jsonl", {"type": "result", "total_cost_usd": 1.5, "num_turns": 4,
                                          "usage": {"input_tokens": 1, "output_tokens": 2}})
        _stream(self.tmp / "S001-keeper.jsonl", {"type": "result", "total_cost_usd": 0.5, "num_turns": 1,
                                                 "usage": {"cache_creation_input_tokens": 7}})
        _stream(self.tmp / "notes.jsonl", {"type": "result", "total_cost_usd": 100})
        tot = R.claude_totals(self.tmp, t0)
        self.assertEqual((tot["cost_usd"], tot["turns"]), (2.0, 5))
        self.assertEqual(tot["tokens"], {"input": 1, "output": 2, "cache_read": 0, "cache_write": 7})
        self.assertEqual(R.token_total(tot["tokens"]), 10)
        self.assertEqual(R.claude_totals(self.tmp, time.time() + 60)["cost_usd"], 0.0)   # streams before the run

    def test_a_cap_is_the_agents_failure_like_a_timeout(self):
        f = R.outcome_failure("over_budget", "", 9, [], "tokens")
        self.assertEqual([(x["kind"], x["class"]) for x in f], [("tokens_cap", "agent")])
        f = R.outcome_failure("over_budget", "", 9, [{"class": "harness"}], "cost")
        self.assertEqual([(x["kind"], x["class"]) for x in f], [("cost_cap", "unknown")])
        self.assertIn("over_budget", R.OUTCOMES)
        self.assertFalse(R.is_clean("over_budget", [], []))


# ======================================================================== run metrics

class Metrics(TmpDir):
    def test_time_to_milestone(self):
        self.assertEqual(R.time_to_milestone(100.0, 760.25), 660.2)
        self.assertIsNone(R.time_to_milestone(100.0, None))

    def test_deaths(self):
        t0 = time.time() - 10
        _stream(self.tmp / "S001.fights.jsonl",
                {"party": [{"name": "Lae'zel", "downed": 1, "died": False}, {"name": "Tav", "downed": 2, "died": True}]},
                {"party": [{"name": "Tav", "downed": 0, "died": False}]})
        _stream(self.tmp / "S002.fights.jsonl", {"party": [{"name": "Gale", "downed": 1, "died": True}]})
        fails = [{"kind": "party_wipe", "class": "agent"}, {"kind": "reload", "class": "agent"}]
        self.assertEqual(R.death_counts(self.tmp, t0, fails), {"fights": 3, "downs": 4, "deaths": 2, "wipes": 1})
        self.assertEqual(R.death_counts(self.tmp, time.time() + 60, []),
                         {"fights": 0, "downs": 0, "deaths": 0, "wipes": 0})

    def test_fairness_flags(self):
        s = _stream(self.tmp / "S001.jsonl",
                    _use("1", "mcp__bg3__look", {}), _res("1", "e3 goblin 7f0c6a1e-1111-2222-3333-444455556666 hp 7"),
                    _use("2", "mcp__bg3__look", {}), _res("2", "door DC 15 locked"),
                    _use("3", "Read", {"file_path": "../runner/play.py"}),
                    _res("3", "Blocked by the sandbox/fairness rule: Read outside memory/", True),
                    _use("4", "mcp__bg3__memory_read", {}), _res("4", "id 7f0c6a1e-1111-2222-3333-444455556666"))
        f = R.fairness_flags([s])
        self.assertEqual((f["leak"], f["blocks"]), (2, 1))
        self.assertEqual(f["kinds"], {"guid": 1, "dc_outside_roll": 1})
        self.assertEqual(f["count"], f["leak"] + f["check"])
        self.assertEqual(R.fairness_flags([]), {"count": 0, "leak": 0, "check": 0, "blocks": 0, "kinds": {}})

    def test_latency_summary(self):
        t0 = time.time() - 10
        self.assertIsNone(R.latency_summary(self.tmp, t0))

        def lat(name, med, p90, n, gaps, rt):
            (self.tmp / name).write_text(json.dumps({
                "idle_s": {"all": {"n": n, "median": med, "p90": p90}}, "long_gaps": {"total": gaps},
                "tool_rt_ms": {"read": {"n": n, "median": rt}}}))
        lat("S001.latency.json", 0.5, 3.0, 100, 2, 60.0)
        self.assertEqual(R.latency_summary(self.tmp, t0), {"sessions": 1, "idle_median_s": 0.5, "idle_p90_s": 3.0,
                                                           "gaps_over_10s": 2, "read_rt_median_ms": 60.0})
        lat("S001-r1.latency.json", 2.0, 9.0, 10, 1, 300.0)     # a short resume: weighs little
        lat("S002.latency.json", 1.0, 4.0, 120, 0, 80.0)
        lat("S002-fix.latency.json", 50.0, 90.0, 1000, 9, 1.0)  # not play
        got = R.latency_summary(self.tmp, t0)
        self.assertEqual(got, {"sessions": 3, "idle_median_s": 1.0, "idle_p90_s": 4.0, "gaps_over_10s": 3,
                               "read_rt_median_ms": 80.0})

    def test_story_gained(self):
        _stream(self.tmp / "progress_history.jsonl",
                {"session": "S000", "t": 50, "points": 999, "events": 9},
                {"session": "S001", "t": 150, "points": 119, "events": 43, "by_kind": {"kill": 10, "area": 1}},
                {"session": "S001-fix", "t": 160, "points": 0, "events": 0, "by_kind": {}},
                {"session": "S002", "t": 300, "points": 20, "events": 3, "by_kind": {"kill": 2}})
        self.assertEqual(R.story_gained(self.tmp, 100), {"points": 139, "events": 46, "sessions": 3,
                                                         "by_kind": {"kill": 12, "area": 1}})
        self.assertEqual(R.story_gained(self.tmp / "none", 0)["points"], 0)


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
        self.assertEqual(row["to milestone min"][-5:], ["2", "11.6", "11.8", "11.7", "0.2"])   # 693 s and 708 s
        self.assertEqual(row["cost $"][-5:-3], ["3", "4.20"])
        self.assertEqual(row["tokens M"][-5:-3], ["2", "6.20"])                       # run 3 has no tokens
        self.assertEqual(row["idle median s"][-5:], ["2", "0.50", "1.50", "1.00", "0.71"])
        self.assertIn("version drift (--allow-version-drift): run 3: repo.commit: a → b", out)
        self.assertIn("caps differ between runs", out)

    def test_table_columns(self):
        lines = R.runs_table(self.RECS).splitlines()
        self.assertTrue(lines[0].startswith("run  playthrough  outcome"))
        self.assertIn("to M min", lines[0])
        self.assertIn("over_budget (cost)", lines[4])
        self.assertIn("11.6", lines[2])            # time to the milestone of an old record (reached_at - started)
        self.assertIn("6.63", lines[2])            # tokens, millions


# ======================================================================== play.py wiring

class Wiring(TmpDir):
    def setUp(self):
        super().setUp()
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "PLAYTHROUGHS", "read_actions_since", "current_pins")}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME, P.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        P.read_actions_since = lambda t, path=None: []

    PINS = {"game_build": "25605617", "bg3le": {"sha256": "a", "source": "v0.3.6-dirty"}, "mod": {"sha1": "m"},
            "repo": {"commit": "c1", "dirty": False, "diff": None}}

    def runner(self, pt="naut-r2", k=2, **kw) -> P.Runner:
        return P.Runner(P.Options(pt, None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", run_index=k, **kw))

    def test_series_json_freezes_pins_and_caps_once(self):
        caps = {"run_hours": 4, "max_cost": 30.0, "max_tokens": None}
        P.freeze_series("naut", 3, "nautiloid_escaped", {"p": 1}, self.PINS, caps)
        P.freeze_series("naut", 3, "nautiloid_escaped", {"p": 2}, {"other": 1}, {"max_cost": 1})
        meta = P.series_meta("naut")
        self.assertEqual((meta["profile"], meta["pins"], meta["caps"]), ({"p": 1}, self.PINS, caps))
        # a series from before pins and caps gets them from its next run, the rest untouched
        (P.RUNTIME / "sessions" / "old").mkdir(parents=True)
        (P.RUNTIME / "sessions" / "old" / "series.json").write_text(json.dumps({"series": "old", "profile": {"p": 0}}))
        P.freeze_series("old", 3, "nautiloid_escaped", {"p": 9}, self.PINS, caps)
        meta = P.series_meta("old")
        self.assertEqual((meta["profile"], meta["pins"], meta["caps"]), ({"p": 0}, self.PINS, caps))

    def test_a_drifted_run_is_refused_unless_allowed(self):
        P.freeze_series("naut", 3, "nautiloid_escaped", {}, self.PINS, {})
        same = json.loads(json.dumps(self.PINS))
        r = self.runner()
        self.assertIsNone(P.gate_series_run("naut", r, same, False))
        self.assertEqual((r.opt.pins, r.opt.version_drift), (same, None))
        drifted = json.loads(json.dumps(self.PINS))
        drifted["repo"]["commit"] = "c2"
        msg = P.gate_series_run("naut", self.runner(), drifted, False)
        self.assertIn("REFUSING run 2", msg)
        self.assertIn("repo.commit: c1 → c2", msg)
        r = self.runner()
        self.assertIsNone(P.gate_series_run("naut", r, drifted, True))
        self.assertEqual(r.opt.version_drift, ["repo.commit: c1 → c2"])
        r.begin_run(time.time())
        self.assertEqual(r.state["run"]["version_drift"], ["repo.commit: c1 → c2"])
        # a run under way (a self-restart on changed code) keeps the pins it started with: never refused mid-run
        r2 = self.runner()
        self.assertIsNone(P.gate_series_run("naut", r2, {"repo": {"commit": "c3"}}, False))
        self.assertEqual(r2.opt.pins, drifted)

    def test_cli_refuses_a_cap_change_in_a_series(self):
        P.freeze_series("naut", 3, "nautiloid_escaped", {}, self.PINS,
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
        self.assertEqual(rec["milestones_reached"], 0)
        line = json.loads((P.RUNTIME / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["series"], line["run"], line["cap"]), ("naut", 2, "tokens"))
        # the time cap: outcome timeout, cap "time"; reached: time to the milestone
        rec = P.run_record(d, t0, t0 + 600, {}, "naut-r2", [], "timeout", "time cap used up")
        self.assertEqual(rec["cap"], "time")
        self.assertIn("timeout", [f["kind"] for f in rec["failures"]])
        rec = P.run_record(d, t0, t0 + 600, {"id": "nautiloid_escaped", "t": t0 + 450.0}, "naut-r2", [])
        self.assertEqual((rec["outcome"], rec["cap"], rec["time_to_milestone_s"]), ("reached", None, 450.0))

    def test_runs_command_prints_the_summary(self):
        d = P.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        (d / "runs.jsonl").write_text("".join(json.dumps({"series": "naut", **r}) + "\n" for r in Summary.RECS))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["runs", "--playthrough", "naut"]), 0)
        self.assertIn("series: n=3, reached 2/3", buf.getvalue())
        self.assertIn("to milestone min", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
