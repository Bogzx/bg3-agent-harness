"""bg3harness.runs (moved from the runner): the usage meter and caps, the run's totals over its streams, the run
metrics (deaths, fairness flags, latency, result tokens, story points) and the model_time backfill. Pure functions over
files in a temp dir; never launches a client or the game. Runner-side wiring: runner/test_runs.py."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from bg3harness import runs

REPO = Path(__file__).resolve().parents[3]


def _stream(path: Path, *events) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _use(i: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _res(i: str, text: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": i,
                    "is_error": is_error,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class Caps(TmpDir):
    def test_usage_meter(self):
        def msg(mid, out):
            return {
                "type": "assistant",
                "message": {
                    "id": mid,
                    "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 100},
                },
            }

        claude = runs.clients.ClaudeClient()  # the meter takes clients.Events, as play.py feeds it

        class Meter(runs.UsageMeter):
            def on_event(self, ev):
                for e in claude.parse(json.dumps(ev)):
                    super().on_event(e)

        m = Meter()
        m.on_event(msg("m1", 5))
        m.on_event(msg("m1", 20))  # the same message again (a later content block): counted once
        m.on_event(msg("m2", 0))
        self.assertEqual(m.tokens, 130 + 110)
        self.assertEqual(m.cost, 0.0)  # no result yet: no cost per token known
        m.on_event(
            {
                "type": "result",
                "total_cost_usd": 2.0,
                "usage": {"input_tokens": 20, "output_tokens": 30, "cache_read_input_tokens": 950},
            }
        )
        self.assertEqual((m.tokens, m.cost), (1000, 2.0))  # the result's exact totals replace the live sum
        m.on_event(msg("m3", 390))
        self.assertEqual(m.tokens, 1500)
        self.assertAlmostEqual(m.cost, 3.0)  # 500 live tokens at the run's $0.002 per token
        base = Meter(10.0, 5000)  # earlier processes of the run
        base.on_event(msg("x", 890))
        self.assertEqual((base.tokens, base.cost), (6000, 12.0))
        self.assertIsNone(runs.cap_exceeded({"max_cost": None, "max_tokens": None}, 99, 10**9))
        self.assertEqual(runs.cap_exceeded({"max_cost": 12.0}, 12.0, 0), "cost")
        self.assertEqual(runs.cap_exceeded({"max_cost": 50.0, "max_tokens": 6000}, 12.0, 6000), "tokens")
        self.assertIsNone(runs.cap_exceeded(None, 1, 1))

    def test_claude_totals(self):
        t0 = time.time() - 10
        _stream(
            self.tmp / "S001.jsonl",
            {"type": "result", "total_cost_usd": 1.5, "num_turns": 4, "usage": {"input_tokens": 1, "output_tokens": 2}},
        )
        _stream(
            self.tmp / "S001-keeper.jsonl",
            {"type": "result", "total_cost_usd": 0.5, "num_turns": 1, "usage": {"cache_creation_input_tokens": 7}},
        )
        _stream(self.tmp / "notes.jsonl", {"type": "result", "total_cost_usd": 100})
        tot = runs.claude_totals(self.tmp, t0)
        self.assertEqual((tot["cost_usd"], tot["turns"]), (2.0, 5))
        self.assertEqual(tot["tokens"], {"input": 1, "output": 2, "cache_read": 0, "cache_write": 7})
        self.assertEqual(runs.token_total(tot["tokens"]), 10)
        self.assertEqual(runs.claude_totals(self.tmp, time.time() + 60)["cost_usd"], 0.0)  # streams before the run

    def test_claude_totals_counts_a_process_stopped_before_its_result(self):
        # the e2e pi run (e2e-naut-pi S001, trimmed to 5 of its 431 usage events): the cost cap stopped pi, no result
        # event, and the run record said cost_usd 0, turns 0, tokens 0
        src = REPO / "runner" / "fixtures" / "pi" / "e2e-naut-pi"
        for name in ("S001.jsonl", "S001.events.jsonl"):
            shutil.copy(src / name, self.tmp / name)
        tot = runs.claude_totals(self.tmp, 0)
        self.assertEqual((tot["cost_usd"], tot["turns"]), (0.500185, 5))  # pi's last running (billed) cost
        self.assertEqual(tot["tokens"], {"input": 27990, "output": 616, "cache_read": 512896, "cache_write": 0})
        # claude stopped mid-stream (its events log ends in session_end): its usage per message id at the run's
        # cost per token so far; while it runs (no session_end yet) it is no finished process: not counted
        _stream(
            self.tmp / "S002.jsonl",
            {"type": "result", "total_cost_usd": 1.0, "num_turns": 1, "usage": {"input_tokens": 1000}},
        )
        later = time.time() + 5
        os.utime(self.tmp / "S002.jsonl", (later, later))
        _stream(self.tmp / "S003.jsonl", {"type": "system", "subtype": "init"})
        os.utime(self.tmp / "S003.jsonl", (later + 1, later + 1))
        log = runs.events.EventLog(self.tmp / "S003.events.jsonl")
        for mid, i, o in (("m1", 10, 5), ("m1", 10, 90), ("m2", 100, 0)):
            log.write(runs.clients.Event("usage", message_id=mid, tokens=runs.clients.tokens(i, o, 0, 0)))
        self.assertEqual(runs.claude_totals(self.tmp, 0)["turns"], 5 + 1)
        log.end("cap", -2, time.time())
        rate = 1.0 / (541502 + 1000)  # the run's dollars per token before S003
        tot = runs.claude_totals(self.tmp, 0)
        self.assertEqual(tot["turns"], 5 + 1 + 2)
        self.assertEqual(tot["tokens"]["input"], 27990 + 1000 + 110)
        self.assertAlmostEqual(tot["cost_usd"], 0.500185 + 1.0 + 200 * rate * (0.500185 + 1.0))

    def test_a_first_claude_process_stopped_before_its_result_has_an_unknown_cost(self):
        # H4: claude reports cost only in its result; a bench run (one process) stopped at the hard stop, a cap or a
        # crash read cost 0 with 208k tokens
        _stream(self.tmp / "S001.jsonl", {"type": "system", "subtype": "init"})
        log = runs.events.EventLog(self.tmp / "S001.events.jsonl")
        log.write(runs.clients.Event("usage", message_id="m1", tokens=runs.clients.tokens(208000, 900, 0, 0)))
        log.end("hard_stop", -2, time.time())
        tot = runs.claude_totals(self.tmp, 0)
        self.assertEqual((tot["cost_usd"], tot["cost_unknown"], tot["turns"]), (0.0, True, 1))
        self.assertEqual(runs.token_total(tot["tokens"]), 208900)
        ev = runs.unfinished_result(runs.stopped_usage(self.tmp / "S001") or [], 0.0, 0)
        self.assertIsNone(ev.cost_usd)  # nothing priced a token yet: no rate to price it at

    def test_a_result_without_a_price_has_an_unknown_cost(self):
        # a codex model with no known price: its result carries tokens and no cost
        _stream(
            self.tmp / "S001.jsonl",
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.completed", "usage": {"input_tokens": 5000, "cached_input_tokens": 0, "output_tokens": 50}},
        )
        tot = runs.claude_totals(self.tmp, 0)
        self.assertEqual((tot["cost_usd"], tot["cost_unknown"]), (0.0, True))
        self.assertGreater(runs.token_total(tot["tokens"]), 0)
        _stream(self.tmp / "S001.jsonl", {"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 10}})
        self.assertIs(runs.claude_totals(self.tmp, 0)["cost_unknown"], False)

    def test_a_cap_is_the_agents_failure_like_a_timeout(self):
        f = runs.outcome_failure("over_budget", "", 9, [], "tokens")
        self.assertEqual([(x["kind"], x["class"]) for x in f], [("tokens_cap", "agent")])
        f = runs.outcome_failure("over_budget", "", 9, [{"class": "harness"}], "cost")
        self.assertEqual([(x["kind"], x["class"]) for x in f], [("cost_cap", "unknown")])
        self.assertIn("over_budget", runs.OUTCOMES)
        self.assertFalse(runs.is_clean("over_budget", [], []))


# ======================================================================== run metrics


class Metrics(TmpDir):
    def test_deaths(self):
        t0 = time.time() - 10
        _stream(
            self.tmp / "S001.fights.jsonl",
            {"party": [{"name": "Lae'zel", "downed": 1, "died": False}, {"name": "Tav", "downed": 2, "died": True}]},
            {"party": [{"name": "Tav", "downed": 0, "died": False}]},
        )
        _stream(self.tmp / "S002.fights.jsonl", {"party": [{"name": "Gale", "downed": 1, "died": True}]})
        fails = [{"kind": "party_wipe", "class": "agent"}, {"kind": "reload", "class": "agent"}]
        self.assertEqual(runs.death_counts(self.tmp, t0, fails), {"fights": 3, "downs": 4, "deaths": 2, "wipes": 1})
        self.assertEqual(
            runs.death_counts(self.tmp, time.time() + 60, []), {"fights": 0, "downs": 0, "deaths": 0, "wipes": 0}
        )

    def test_fairness_flags(self):
        s = _stream(
            self.tmp / "S001.jsonl",
            _use("1", "mcp__bg3__look", {}),
            _res("1", "e3 goblin 7f0c6a1e-1111-2222-3333-444455556666 hp 7"),
            _use("2", "mcp__bg3__look", {}),
            _res("2", "door DC 15 locked"),
            _use("3", "Read", {"file_path": "../runner/play.py"}),
            _res("3", "Blocked by the sandbox/fairness rule: Read outside memory/", True),
            _use("4", "mcp__bg3__memory_read", {}),
            _res("4", "id 7f0c6a1e-1111-2222-3333-444455556666"),
        )
        f = runs.fairness_flags([s])
        self.assertEqual((f["leak"], f["blocks"]), (2, 1))
        self.assertEqual(f["kinds"], {"guid": 1, "dc_outside_roll": 1})
        self.assertEqual(f["count"], f["leak"] + f["check"])
        self.assertEqual(runs.fairness_flags([]), {"count": 0, "leak": 0, "check": 0, "blocks": 0, "kinds": {}})

    def test_latency_summary(self):
        t0 = time.time() - 10
        self.assertIsNone(runs.latency_summary(self.tmp, t0))

        def lat(name, med, p90, n, gaps, rt):
            (self.tmp / name).write_text(
                json.dumps(
                    {
                        "idle_s": {"all": {"n": n, "median": med, "p90": p90}},
                        "long_gaps": {"total": gaps},
                        "tool_rt_ms": {"read": {"n": n, "median": rt}},
                    }
                )
            )

        lat("S001.latency.json", 0.5, 3.0, 100, 2, 60.0)
        self.assertEqual(
            runs.latency_summary(self.tmp, t0),
            {"sessions": 1, "idle_median_s": 0.5, "idle_p90_s": 3.0, "gaps_over_10s": 2, "read_rt_median_ms": 60.0},
        )
        lat("S001-r1.latency.json", 2.0, 9.0, 10, 1, 300.0)  # a short resume: weighs little
        lat("S002.latency.json", 1.0, 4.0, 120, 0, 80.0)
        lat("S002-fix.latency.json", 50.0, 90.0, 1000, 9, 1.0)  # not play
        got = runs.latency_summary(self.tmp, t0)
        self.assertEqual(
            got, {"sessions": 3, "idle_median_s": 1.0, "idle_p90_s": 4.0, "gaps_over_10s": 3, "read_rt_median_ms": 80.0}
        )

    def test_result_tokens_summary(self):
        t0 = time.time() - 10
        self.assertIsNone(runs.result_tokens_summary(self.tmp, t0))
        (self.tmp / "S001.latency.json").write_text(
            json.dumps({"result_tokens": {"total": 3000, "n": 10, "per_result": 300.0, "images": 1}})
        )
        self.assertEqual(
            runs.result_tokens_summary(self.tmp, t0),
            {"sessions": 1, "total": 3000, "n": 10, "per_result": 300.0, "images": 1, "source": "turns"},
        )
        # a session from before the field: backfilled from its raw stream (S002.jsonl next to it)
        (self.tmp / "S002.latency.json").write_text(json.dumps({"idle_s": {}}))
        _stream(
            self.tmp / "S002.jsonl",
            _use("1", "mcp__bg3__look", {}),
            _res("1", "a goblin " * 40),
            _use("2", "mcp__bg3__look", {}),
            _res("2", "ok"),
        )
        got = runs.result_tokens_summary(self.tmp, t0)
        self.assertEqual((got["sessions"], got["n"], got["images"], got["source"]), (2, 12, 1, "stream+turns"))
        self.assertGreater(got["total"], 3000)
        self.assertEqual(got["per_result"], round(got["total"] / 12, 1))
        (self.tmp / "S002-fix.latency.json").write_text(json.dumps({"result_tokens": {"total": 9999, "n": 1}}))
        (self.tmp / "S003.latency.json").write_text(json.dumps({}))  # no field and no stream: skipped
        self.assertEqual(runs.result_tokens_summary(self.tmp, t0), got)  # -fix is not a play session

    def test_story_gained(self):
        _stream(
            self.tmp / "progress_history.jsonl",
            {"session": "S000", "t": 50, "points": 999, "events": 9},
            {"session": "S001", "t": 150, "points": 119, "events": 43, "by_kind": {"kill": 10, "area": 1}},
            {"session": "S001-fix", "t": 160, "points": 0, "events": 0, "by_kind": {}},
            {"session": "S002", "t": 300, "points": 20, "events": 3, "by_kind": {"kill": 2}},
        )
        self.assertEqual(
            runs.story_gained(self.tmp, 100),
            {"points": 139, "events": 46, "sessions": 3, "by_kind": {"kill": 12, "area": 1}},
        )
        self.assertEqual(runs.story_gained(self.tmp / "none", 0)["points"], 0)


# ======================================================================== the runs command


class Backfill(unittest.TestCase):
    def test_backfill_model_time_adds_missing_keys_only(self):
        fresh = {
            "sessions": 1,
            "model_s": 999.0,
            "adjusted_model_s_ref_ttft": 160.0,
            "api_floor": {"p10_s": 1.5},
            "measured": ["x"],
        }
        calls = []
        orig = runs.model_time.model_time
        runs.model_time.model_time = lambda d, since=None, until=None: calls.append((since, until)) or dict(fresh)
        try:
            rec = {"started": 100.0, "t": 200.0, "model_time": {"sessions": 1, "model_s": 210.0}}
            out = runs.backfill_model_time(Path("/nowhere"), rec)["model_time"]
            self.assertEqual(out["model_s"], 210.0)  # a present value never changes
            self.assertEqual((out["api_floor"], out["adjusted_model_s_ref_ttft"]), ({"p10_s": 1.5}, 160.0))
            self.assertNotIn("measured", out)
            self.assertEqual(calls, [(100.0, 200.0 + runs.BACKFILL_SLACK_S)])
            self.assertEqual(runs.backfill_model_time(Path("/nowhere"), dict(rec, model_time=out)), {})  # complete
            self.assertEqual(runs.backfill_model_time(Path("/nowhere"), {"t": 1.0}), {})  # no start
            self.assertIn("model_time", runs.backfill_model_time(Path("/nowhere"), {"started": 1.0}))  # none yet
            runs.model_time.model_time = lambda d, since=None, until=None: {"sessions": 0}
            self.assertEqual(runs.backfill_model_time(Path("/nowhere"), rec), {})  # no timing
        finally:
            runs.model_time.model_time = orig

    def test_model_time_field_drops_the_constant_lists(self):
        mt = runs.model_time_field(
            {"sessions": 1, "model_s": 3.0, "measured": ["a"], "estimated": ["b"], "not_separable": ["c"]}
        )
        self.assertEqual(mt, {"sessions": 1, "model_s": 3.0})

    # ---------------------------------------------------------------- the runner


if __name__ == "__main__":
    unittest.main()
