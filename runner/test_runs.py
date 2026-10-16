#!/usr/bin/env python3
"""Unit tests for the benchmark fields of runner/runs.py and their play.py wiring: version pins and the series'
drift refusal, the cost/token caps, the run metrics (time to the milestone, deaths, fairness flags, latency, story
points), the series summary, and the persona judge / model time in the run record (a fake judge client: a python
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
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: E402
import runs as R  # noqa: E402
from test_play import _res, _stream, _use  # noqa: E402
import test_judge as TJ  # noqa: E402  (its stream builders and CHARACTER.md fixture)


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

    def test_lock_unlock_restores_modes(self):
        k, rec = self.tree(), self.tmp / "rt" / "lock.json"
        before = self.modes(k)
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), {"a.md": 0o440, "playbooks/b.md": 0o444, "x.sh": 0o555})
        with self.assertRaises(PermissionError) if os.geteuid() else contextlib.nullcontext():
            (k / "a.md").write_text("agent edit")
        # an execv'd successor (or a restart after a kill) locks again: the record keeps the first modes
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(R.unlock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), before)
        self.assertFalse(rec.exists())
        self.assertEqual(R.unlock_tree(k, rec), 0)   # nothing locked: no-op
        self.assertEqual(R.lock_tree(self.tmp / "none", rec), 0)

    def test_a_series_locks_for_its_duration_only(self):
        k, rec = self.tree(), self.tmp / "rt" / "lock.json"
        before, seen = self.modes(k), []

        def fake_series(*a, **kw):
            seen.append(self.modes(k))
            return 0
        with mock.patch.object(P, "KNOWLEDGE_DIR", k), mock.patch.object(P, "KNOWLEDGE_LOCK", rec), \
                mock.patch.object(P, "run_series", side_effect=fake_series), \
                mock.patch.object(P, "current_pins", return_value={}), \
                mock.patch.object(P, "series_meta", return_value={}), \
                mock.patch.dict(os.environ, {"BG3AI_KNOWLEDGE_LOCK": "1"}), \
                mock.patch("bg3ai.skipper.start_background", return_value=None), \
                mock.patch.object(P.ramwatch, "start_background", return_value=None), \
                mock.patch.object(P.signal, "signal"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = P.main(["run", "--playthrough", "naut", "--until", "nautiloid_escaped", "--runs", "2"])
        self.assertEqual((rc, len(seen)), (0, 1))
        self.assertTrue(all(m & 0o222 == 0 for m in seen[0].values()))
        self.assertEqual(self.modes(k), before)
        self.assertFalse(rec.exists())


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
        claude = R.clients.ClaudeClient()   # the meter takes clients.Events, as play.py feeds it

        class Meter(R.UsageMeter):
            def on_event(self, ev):
                for e in claude.parse(json.dumps(ev)):
                    super().on_event(e)
        m = Meter()
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
        base = Meter(10.0, 5000)             # earlier processes of the run
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
        self.assertNotIn("sandbox_change", rec)
        line = json.loads((P.RUNTIME / "sessions" / "naut" / "runs.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["series"], line["run"], line["cap"]), ("naut", 2, "tokens"))
        # the time cap: outcome timeout, cap "time"; reached: time to the milestone
        rec = P.run_record(d, t0, t0 + 600, {}, "naut-r2", [], "timeout", "time cap used up")
        self.assertEqual(rec["cap"], "time")
        self.assertIn("timeout", [f["kind"] for f in rec["failures"]])
        rec = P.run_record(d, t0, t0 + 600, {"id": "nautiloid_escaped", "t": t0 + 450.0}, "naut-r2", [])
        self.assertEqual((rec["outcome"], rec["cap"], rec["time_to_milestone_s"]), ("reached", None, 450.0))
        r.opt.sandbox_change = "sandbox: container → none"                  # --allow-sandbox-change
        self.assertEqual(r.write_run_record(t0, "timeout")["sandbox_change"], "sandbox: container → none")

    def test_runs_command_prints_the_summary(self):
        d = P.RUNTIME / "sessions" / "naut"
        d.mkdir(parents=True)
        (d / "runs.jsonl").write_text("".join(json.dumps({"series": "naut", **r}) + "\n" for r in Summary.RECS))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(P.main(["runs", "--playthrough", "naut"]), 0)
        self.assertIn("series: n=3, reached 2/3", buf.getvalue())
        self.assertIn("to milestone min", buf.getvalue())


# ======================================================================== persona judge + model time in the record

REAL_CLAUDE_CLI = P.judge.ClaudeCLI   # its output parser; the tests patch judge.ClaudeCLI with FakeJudgeCLI


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
        self.saved = {n: getattr(P, n) for n in ("RUNTIME", "PLAYTHROUGHS", "read_actions_since")}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME, P.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        P.read_actions_since = lambda t, path=None: []
        for name, value in (("PLAYTHROUGHS", P.PLAYTHROUGHS), ("DEFAULT_MODEL", "judge-x")):
            patcher = mock.patch.object(P.judge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = self.tmp / "judge-calls.log"
        (P.PLAYTHROUGHS / "naut" / "memory").mkdir(parents=True)
        (P.PLAYTHROUGHS / "naut" / "memory" / "CHARACTER.md").write_text(TJ.CHARACTER)
        self.dir = P.RUNTIME / "sessions" / "naut"
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
        return mock.patch.object(P.judge, "ClaudeCLI", lambda model: FakeJudgeCLI(model, log=self.calls, **kw))

    def runner(self, **kw) -> P.Runner:
        return P.Runner(P.Options("naut", None, 20, "opus", save_owner="Claude", until="nautiloid_escaped",
                                  series="naut", **{"run_index": 1, **kw}))

    def n_calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    # ---------------------------------------------------------------- runs.py

    def test_update_record_in_place(self):
        path = self.tmp / "runs.jsonl"
        recs = [{"run": 1, "playthrough": "naut", "started": 1.0}, {"run": 2, "playthrough": "naut-r2", "started": 2.0}]
        path.write_text(json.dumps(recs[0]) + "\nnot json\n" + json.dumps(recs[1]) + "\n")
        self.assertTrue(R.update_record(path, recs[1], {"persona": {"score": 5}}))
        lines = path.read_text().splitlines()
        self.assertEqual(lines[:2], [json.dumps(recs[0]), "not json"])                # the rest byte for byte
        self.assertEqual(json.loads(lines[2]), {**recs[1], "persona": {"score": 5}})
        self.assertFalse(R.update_record(path, {"run": 3, "playthrough": "naut-r3", "started": 3.0}, {"x": 1}))
        self.assertFalse(R.update_record(self.tmp / "missing.jsonl", recs[0], {"x": 1}))
        self.assertFalse((self.tmp / "runs.jsonl.tmp").exists())

    def test_series_judge_model(self):
        self.assertEqual(R.series_judge_model(None, None, "d", False), ("d", None, None))      # first run: default
        self.assertEqual(R.series_judge_model(None, "j1", "d", False), ("j1", None, None))
        self.assertEqual(R.series_judge_model("j1", None, "d", False), ("j1", None, None))     # frozen rules
        self.assertEqual(R.series_judge_model("j1", "j1", "d", False), ("j1", None, None))
        model, err, change = R.series_judge_model("j1", "j2", "d", False)
        self.assertIn("--allow-judge-change", err)
        self.assertEqual(change, "judge_model: j1 → j2")
        self.assertEqual(R.series_judge_model("j1", "j2", "d", True), ("j2", None, "judge_model: j1 → j2"))

    def test_table_and_summary_show_persona_and_model_time(self):
        recs = [dict(Summary.RECS[0], persona={"status": "ok", "score": 8.0, "judge_model": "j", "cost_usd": 0.1},
                     model_time={"adjusted_model_s": 300.0, "model_s": 400.0}),
                dict(Summary.RECS[1], persona={"status": "judge_failed", "judge_model": "j", "cost_usd": 0.05},
                     model_time={"adjusted_model_s": 420.0}),
                dict(Summary.RECS[2], persona={"status": "ok", "score": 6.0, "judge_model": "k", "cost_usd": 0.1})]
        lines = R.runs_table(recs).splitlines()
        self.assertIn("persona  adj model min", lines[0])
        cells = [ln.split() for ln in lines[2:5]]
        self.assertEqual((cells[0][13:15], cells[1][13:15]), (["8", "5.0"], ["fail", "7.0"]))
        self.assertIn("6  ", lines[4][lines[0].index("persona"):])
        self.assertIn("-", lines[4][lines[0].index("adj model min"):].split()[0])
        self.assertEqual(R.persona_cell({}), "-")
        out = R.series_summary(recs)
        row = {ln.split("  ")[0]: ln.split() for ln in out.splitlines()[3:11]}
        self.assertEqual(row["persona score"][-5:], ["2", "6.0", "8.0", "7.0", "1.4"])          # failed: not a score
        self.assertEqual(row["adj model min"][-5:], ["2", "5.0", "7.0", "6.0", "1.4"])
        self.assertEqual(row["judge $"][-5:-3], ["3", "0.05"])
        self.assertIn("judge models differ between runs (scores don't compare): j | k", out)

    def test_model_time_field_drops_the_constant_lists(self):
        mt = R.model_time_field({"sessions": 1, "model_s": 3.0, "measured": ["a"], "estimated": ["b"],
                                 "not_separable": ["c"]})
        self.assertEqual(mt, {"sessions": 1, "model_s": 3.0})

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
        with mock.patch.object(P.judge, "judge_run", side_effect=RuntimeError("bug")):
            p = P.judge_record(self.dir, rec, "judge-x")
        self.assertEqual((p["status"], p["error"]), ("judge_failed", "RuntimeError: bug"))

    def test_the_judge_has_an_overall_timeout(self):
        t0 = time.time() - 60
        self.play(t0)
        rec = {"playthrough": "naut", "started": t0, "t": time.time(), "model": "opus"}
        t = time.time()
        with self.fake(sleep=5):
            p = P.judge_record(self.dir, rec, "judge-x", timeout=1.5)
        self.assertLess(time.time() - t, 4)
        self.assertEqual(p["status"], "judge_failed")
        self.assertIn("timed out", p["error"])
        with self.fake():
            p = P.judge_record(self.dir, rec, "judge-x", stop=lambda: True)
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
        self.assertEqual(P.series_meta("naut")["judge_model"], "judge-x")          # frozen from now on
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
        patches = [mock.patch.object(P, "acquire_lock", lambda: None), mock.patch.object(P, "current_pins", dict),
                   mock.patch.object(P, "run_series", lambda *a: calls.append(a) or 0),
                   mock.patch.object(P, "Runner", lambda opt: mock.Mock(run=lambda: calls.append((opt,)) or 0)),
                   mock.patch("bg3ai.skipper.start_background", return_value=None),
                   mock.patch.object(P.ramwatch, "start_background", return_value=None),
                   mock.patch.object(P.signal, "signal"), mock.patch.dict(P.os.environ, {}, clear=False)]
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        P.os.environ.pop("BG3AI_JUDGE_MODEL", None)
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
        P.freeze_series("naut", 3, "nautiloid_escaped", {}, judge_model="judge-x")
        rc, _, err = self.cli(*series, "--judge-model", "j2")
        self.assertEqual(rc, 2)
        self.assertIn("judge_model: judge-x → j2", err)
        with mock.patch.dict(P.os.environ, {"BG3AI_JUDGE_MODEL": "j3"}):
            self.assertIn("judge_model: judge-x → j3", self.cli(*series)[2])
        self.assertEqual(len(calls), n)
        self.assertEqual(self.cli(*series, "--judge-model", "j2", "--allow-judge-change")[0], 0)
        self.assertEqual((calls[-1][0].judge_model, calls[-1][0].judge_change), ("j2", "judge_model: judge-x → j2"))
        P.freeze_series("naut", 3, "nautiloid_escaped", {}, judge_model="other")
        self.assertEqual(P.series_meta("naut")["judge_model"], "judge-x")                  # written once


if __name__ == "__main__":
    unittest.main(verbosity=1)
