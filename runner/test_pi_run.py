#!/usr/bin/env python3
"""play.py with --client pi, offline: the real Runner.run_claude on a fake pi (replays runner/fixtures/pi streams),
the fake game of test_crash. Covers research/ovh/pi.md's wiring points (profile, system text, key passthrough,
cost meter and budget, mcp_failed from pi's stderr, overflow loop, no retry on overflow) and the tool-count check.

    python3 -m unittest runner/test_pi_run.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events as E
import test_crash as TCR

import profiles

P = TCR.P
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"
PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "deepseek-flash.toml"

FAKE_PI = r"""
import json, os, signal, sys, time
with open(os.environ["FAKE_PI_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "key": os.environ.get("DEEPSEEK_API_KEY"),
                        "agent_dir": os.environ.get("PI_CODING_AGENT_DIR")}) + "\n")
if os.environ.get("FAKE_PI_EXT_FAIL"):
    print('Error: Failed to load extension "/repo/tools/pi/bg3.ts": connect ECONNREFUSED 127.0.0.1:9998',
          file=sys.stderr, flush=True)
    sys.exit(1)
lines = open(os.environ["FAKE_PI_STREAM"]).read().splitlines()
hold = int(os.environ.get("FAKE_PI_HOLD_AT", "-1"))   # after this many lines: wait (a long session) for a signal
for i, ln in enumerate(lines):
    if i == hold:
        signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
        time.sleep(60)
    print(ln, flush=True)
"""


def overflow_loop_stream() -> list[str]:
    """pi #11161: overflow errors over and over in one process (compaction off, so it should never start)."""
    lines = [
        ln for ln in (FIXTURES / "overflow.jsonl").read_text().splitlines() if json.loads(ln)["type"] != "agent_settled"
    ]
    err = next(ln for ln in lines if '"stopReason": "error"' in ln and '"message_end"' in ln)
    return lines + [err] * 4


class PiRunBase(TCR.FakeRunBase):
    def setUp(self):
        super().setUp()
        self.pi = self.tmp / "fake_pi.py"
        self.pi.write_text(FAKE_PI)
        self.pi_log = self.tmp / "pi.jsonl"
        system = self.tmp / "system.txt"
        system.write_text("rules\n")
        for k in ("FAKE_PI_EXT_FAIL", "FAKE_PI_HOLD_AT", "FAKE_PI_STREAM", "DEEPSEEK_API_KEY"):
            self.addCleanup(os.environ.pop, k, None)
        os.environ.update(FAKE_PI_LOG=str(self.pi_log), DEEPSEEK_API_KEY="sk-test")
        self.budget = P.RUNTIME / "budget.json"
        self.budget.parent.mkdir(parents=True, exist_ok=True)
        self.budget.write_text(json.dumps({"cap": 5.0, "spent": 1.0}))
        self.opt = P.Options(
            "pi-test",
            None,
            20,
            "deepseek-flash",
            save_owner="Claude",
            client="pi",
            profile=profiles.load(PROFILE),
            system_file=str(system),
            tools_expected=94,
        )

    def run_pi(self, stream: Path | list[str], **opt) -> P.SessionResult:
        if isinstance(stream, list):
            path = self.tmp / "stream.jsonl"
            path.write_text("\n".join(stream) + "\n")
            stream = path
        os.environ["FAKE_PI_STREAM"] = str(stream)
        self.wire(replace(self.opt, **opt), [])
        real_cmd = self.r.agent_cmd
        self.r.agent_cmd = lambda req: [sys.executable, str(self.pi)] + real_cmd(req)[1:]
        self.t0 = self.r.run_t0 = time.time()  # Runner.run's start (the cost meter counts from it)
        return self.r.run_claude("S001", "go", 20, "11111111-1111-1111-1111-111111111111", needs_game=False)

    def calls(self) -> list[dict]:
        return [json.loads(ln) for ln in self.pi_log.read_text().splitlines()]

    def logged(self) -> list:
        return E.load_events(self.r.state.dir / "S001")


class PiRun(PiRunBase):
    def test_a_session_runs_and_its_cost_goes_to_the_budget(self):
        res = self.run_pi(FIXTURES / "sequential.jsonl")
        self.assertEqual((res.reason, res.exit_code), ("exited", 0))
        call = self.calls()[0]
        self.assertEqual(call["key"], "sk-test")  # the key reaches pi through the env only
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek/deepseek-flash")
        self.assertEqual(argv[argv.index("--session-id") + 1], "11111111-1111-1111-1111-111111111111")
        agent_dir = Path(call["agent_dir"])
        self.assertEqual(json.loads((agent_dir / "bg3.json").read_text())["systemPromptFile"], self.opt.system_file)
        self.assertNotIn("sk-test", "".join(p.read_text() for p in agent_dir.glob("*.json")))
        kinds = [e.kind for e in self.logged()]
        self.assertEqual((kinds[0], kinds[-1]), ("session_start", "session_end"))
        self.assertIn("init", kinds)
        self.assertEqual(json.loads(self.budget.read_text())["spent"], 1.025)

    def test_an_extension_load_failure_is_mcp_failed(self):
        os.environ["FAKE_PI_EXT_FAIL"] = "1"
        res = self.run_pi(FIXTURES / "sequential.jsonl")
        self.assertEqual(res.reason, "mcp_failed")
        self.assertIn("unreachable", res.detail)
        self.assertIn("ECONNREFUSED", res.detail)

    def test_a_tool_count_other_than_expected_is_mcp_failed(self):
        os.environ["FAKE_PI_HOLD_AT"] = "30"  # the init event, then a long session: stopped at once
        res = self.run_pi(FIXTURES / "sequential.jsonl", tools_expected=60)
        self.assertEqual(res.reason, "mcp_failed")
        self.assertIn("gave 94 tools, expected 60", res.detail)
        self.assertLess(time.time() - self.t0, 30)

    def test_context_overflow_ends_the_session_without_retry_or_resume(self):
        res = self.run_pi(FIXTURES / "overflow.jsonl")
        self.assertEqual(res.reason, "context_overflow")
        self.assertNotIn(res.reason, P.FAULTS)  # play_session: no resume of a full context
        self.assertEqual(res.tool_calls, 0)  # at the kickoff: a quick failure
        self.assertTrue(P.quick_failure(res))
        self.assertEqual(len(self.calls()), 1)

    def test_an_overflow_loop_is_stopped(self):
        os.environ["FAKE_PI_HOLD_AT"] = str(len(overflow_loop_stream()))  # pi would go on: the runner stops it
        res = self.run_pi(overflow_loop_stream())
        self.assertEqual(res.reason, "context_overflow")
        self.assertIn("looped", res.detail)
        self.assertLess(time.time() - self.t0, 30)

    def test_the_cost_cap_stops_the_process_live(self):
        lines = (FIXTURES / "billing.jsonl").read_text().splitlines()
        first_usage = next(i for i, ln in enumerate(lines) if '"role": "assistant"' in ln and "message_end" in ln)
        os.environ["FAKE_PI_HOLD_AT"] = str(first_usage + 1)  # $0.0125 billed, then a long session
        res = self.run_pi(FIXTURES / "billing.jsonl", max_cost=0.01)
        self.assertEqual(res.reason, "cap")
        self.assertEqual(self.r.cap_hit["cap"], "cost")
        self.assertLess(time.time() - self.t0, 30)
        self.assertAlmostEqual(json.loads(self.budget.read_text())["spent"], 1.0125)

    def test_a_used_up_budget_refuses_the_run(self):
        self.budget.write_text(json.dumps({"cap": 5.0, "spent": 5.0}))
        self.wire(replace(self.opt, max_cost=1.0), [])
        self.assertIn("used up", self.r.budget_refusal())
        self.budget.write_text(json.dumps({"cap": 5.0, "spent": 4.5}))
        self.assertIsNone(self.r.budget_refusal())
        self.assertEqual(self.r.opt.max_cost, 0.5)
        self.assertTrue(self.r.has_usage_caps())  # also without --until


class RawStream(unittest.TestCase):
    def test_a_pi_stream_without_an_events_log_is_parsed_as_pi(self):
        import runs

        lines = (FIXTURES / "billing.jsonl").read_text().splitlines()
        self.assertEqual(runs.stream_client(lines), "pi")
        d = Path(self.id().replace(".", "_"))
        tmp = Path(os.environ["BG3AI_RUNTIME"]) / d
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "S001.jsonl").write_text("\n".join(lines) + "\n")
        res = E.load_events(tmp / "S001", kinds={"result"})
        self.assertEqual([(e.subtype, e.cost_usd) for e in res], [("success", 0.025)])


class Cli(unittest.TestCase):
    def test_refusals(self):
        import contextlib
        import io

        for argv, why in (
            (["--client", "pi"], "needs --profile"),
            (["--profile", str(PROFILE), "--no-keeper", "--model", "opus"], "plays deepseek-flash"),
            (["--profile", str(PROFILE)], "--no-keeper"),
            (["--profile", str(PROFILE), "--no-keeper", "--client", "claude"], "pi profile"),
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(P.main(["run", "--playthrough", "x", *argv]), 2)
            self.assertIn(why, err.getvalue())

    def test_a_paid_client_needs_a_cap(self):
        import contextlib
        import io

        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        self.addCleanup(os.environ.pop, "DEEPSEEK_API_KEY", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(P.main(["run", "--playthrough", "x", "--profile", str(PROFILE), "--no-keeper"]), 2)
        self.assertIn("needs --max-cost", err.getvalue())


if __name__ == "__main__":
    unittest.main()
