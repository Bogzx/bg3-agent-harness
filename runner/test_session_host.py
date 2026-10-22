"""`play.py bench run --matrix <file>` offline: bg3bench's matrix CLI with the harness's LiveRunHost, one job played
as one run_session by a SessionRunner whose game and agent process are stubs (no game, no client, no uv dump).

The job's run dir gets the runner's files (runner.json) and the bench's (runs.jsonl,
manifest.json, result.json); `bench plan` needs no lock.

    python3 -m unittest runner/test_session_host.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
import legacy_cli as lcli
import legacy_playthrough as lpt
import play as P  # noqa: N812

from bg3harness import client_setup, ramwatch, spend
from bg3harness import events as E  # noqa: N812
from bg3harness.runner import core as rcore
from bg3harness.runner import env as renv
from bg3harness.runner import state as rstate

SCENARIO = {
    "schema": 2,
    "name": "host_idol",
    "title": "Sell the idol",
    "family": "sandbox",
    "start": {"kind": "new_game"},
    "goal": {"gold": {"gte": 200}},
    "runs": 1,
    "caps": {"run_hours": 0.25, "max_cost": 1.0, "max_tokens": None},
    "game_settings": {"seed": 7},
    "timing": "realtime",
    "conditions": ["rules"],
    "profile": {"character": {"brief": "A careful trader."}},
}
PAID_USD = 0.3
PI = (Path(__file__).resolve().parents[1] / "profiles" / "deepseek-flash.toml").read_text()  # DeepSeek's rates


class StubRunner(lpt.Runner):
    """The real Runner (SessionRunner + the legacy parts) with the game and the agent process stubbed."""

    played: ClassVar[list[dict]] = []
    refusals: ClassVar[list[str]] = []  # ensure_game's SystemExits, one per start until empty
    stopped: ClassVar[list[str]] = []

    def refresh_mod_snapshot(self) -> list[str]:
        return []

    def ensure_game(self, first: bool) -> str | None:
        if StubRunner.refusals:
            raise SystemExit(StubRunner.refusals.pop(0))
        return None  # a new game: nothing loaded

    def hot_reload_mod(self) -> int:
        return 0

    def apply_play_settings(self) -> None:
        pass

    def check_settings(self) -> None:
        pass

    def game_alive(self) -> bool:
        return True

    def end_run_game(self, when: str = "", after: str = "") -> None:
        StubRunner.stopped.append(when)

    def run_claude(self, sid, prompt, minutes, claude_session, resume=False, tag="", needs_game=True):
        StubRunner.played.append({"sid": sid, "prompt": prompt, "minutes": minutes, "dir": self.state.dir})
        if spend.is_paid(self.opt.client):  # the process's result, and the runner's spend step as run_claude ends
            base = self.state.dir / f"{sid}{tag}"
            stream = base.with_name(base.name + ".jsonl")
            stream.write_text("")
            os.utime(stream, (self.run_started + 1, self.run_started + 1))  # after the run's start (coarse mtimes)
            result = E.Event("result", subtype="success", turns=3, cost_usd=PAID_USD, tokens={"output": 10})
            base.with_name(base.name + ".events.jsonl").write_text(result.to_json() + "\n")
            self.settle_cost(sid + tag, PAID_USD)
        now = time.time()  # a session that played (runs.history_failures: an exit within 60 s is a harness failure)
        return rstate.SessionResult(sid, claude_session or "", 0, "exited", now, now + 600)


class Bench(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-bench-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        repo = self.tmp / "repo"
        (repo / "packages" / "bench" / "scenarios").mkdir(parents=True)
        (repo / "packages" / "bench" / "scenarios" / "host_idol.json").write_text(json.dumps(SCENARIO))
        (repo / "profiles").mkdir()
        (repo / "profiles" / "claude.toml").write_text('client = "claude"\nmodel = "opus"\nstatus = "verified"\n')
        (repo / "runtime").mkdir()
        (repo / "runtime" / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": 0.0}))  # the matrix's
        self.matrix = self.tmp / "m.toml"
        self.matrix.write_text(
            "name = 'm'\nscenarios = ['host_idol']\nmodels = ['profiles/claude.toml']\nconditions = ['rules']\n"
            "allow_gaps = true\n"  # claude plays play/CLAUDE.md (coached) under any condition: a pilot
        )
        se = self.tmp / "se" / "BG3AI"
        se.mkdir(parents=True)
        (se / "nav_guard_x.json").write_text("{}")
        StubRunner.played, StubRunner.refusals, StubRunner.stopped = [], [], []
        for p in (
            mock.patch.multiple(
                renv,
                REPO=repo,
                RUNTIME=repo / "runtime",
                PLAYTHROUGHS=self.tmp / "pt",
                SE_BG3AI_DIR=se,
                log=lambda m: None,
            ),
            mock.patch.object(lpt, "Runner", StubRunner),
            mock.patch.object(client_setup, "run_files", lambda *a: {}),  # no uv dump of the bg3 server
            mock.patch.object(lcli, "acquire_lock", lambda: None),
            mock.patch("bg3ai.skipper.start_background", return_value=None),
            mock.patch.object(ramwatch, "start_background", return_value=None),
            mock.patch.dict(os.environ),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.repo, self.se = repo, se

    def bench(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = P.main(["bench", *argv, "--matrix", str(self.matrix)])
        return rc, out.getvalue()

    def test_plan_lists_the_job_and_runs_nothing(self):
        rc, out = self.bench("plan")
        self.assertEqual(rc, 0, out)
        self.assertIn("host_idol", out)
        self.assertEqual(StubRunner.played, [])

    def test_run_plays_one_session_per_job_and_writes_the_run(self):
        rc, out = self.bench("run")
        self.assertEqual(rc, 0, out)
        [play] = StubRunner.played
        run_dir = play["dir"]
        self.assertEqual(run_dir.parent, self.repo / "runtime" / "sessions")
        self.assertEqual((play["sid"], play["minutes"]), ("S001", 15.0))
        self.assertIn("benchmark run of the scenario 'Sell the idol'", play["prompt"])
        self.assertIn('{"gold": {"gte": 200}}', play["prompt"])
        self.assertFalse((run_dir / "reset").exists())  # a new game's host is a new uuid: no nav file moved
        self.assertEqual([p.name for p in self.se.iterdir()], ["nav_guard_x.json"])
        rec = json.loads((run_dir / "runs.jsonl").read_text())
        self.assertEqual((rec["outcome"], rec["client"], rec["sessions"]), ("timeout", "claude", 1))
        self.assertTrue((run_dir / "manifest.json").is_file() and (run_dir / "result.json").is_file())
        self.assertEqual(os.environ["BG3AI_PROMPT_CONDITION"], "rules")

    def test_a_paid_run_is_charged_once_by_the_matrix(self):
        (self.repo / "profiles" / "pi.toml").write_text(PI)
        self.matrix.write_text(
            "name = 'm'\nscenarios = ['host_idol']\nmodels = ['profiles/pi.toml']\nconditions = ['rules']\n"
            "[budget]\nmax_cost = { host_idol = 1.0 }\n"
        )
        os.environ["DEEPSEEK_API_KEY"] = "test-key"  # api_key_env: no .env read
        dumped = {"system_file": str(self.tmp / "system.txt")}  # pi's run file (no uv dump)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(client_setup, "run_files", lambda *a: dumped))
        rc, out = self.bench("run")
        self.assertEqual(rc, 0, out)
        [play] = StubRunner.played
        rec = json.loads((play["dir"] / "runs.jsonl").read_text())
        self.assertEqual((rec["client"], rec["cost_usd"]), ("pi", PAID_USD))
        budget = json.loads((self.repo / "runtime" / "budget.json").read_text())
        self.assertAlmostEqual(budget["spent"], PAID_USD)  # the matrix's add, not the runner's too
        self.assertEqual(budget["last"]["job"], "deepseek-flash~host_idol~rules~r1")

    def test_a_runner_outside_the_bench_still_charges(self):
        r = StubRunner(rstate.Options("pt", None, 15.0, "opus"))  # play.py run's runner
        self.assertTrue(r.settles_budget)
        r.settle_cost("S001", PAID_USD)
        budget = json.loads((self.repo / "runtime" / "budget.json").read_text())
        self.assertAlmostEqual(budget["spent"], PAID_USD)
        self.assertEqual(budget["last"]["session"], "S001")

    def test_a_start_the_runner_refuses_is_one_invalid_attempt_and_the_job_reruns(self):
        StubRunner.refusals = ["the game is running with a save loaded that the runner didn't load"]
        rc, out = self.bench("run")
        self.assertEqual(rc, 0, out)
        [play] = StubRunner.played
        first = self.repo / "runtime" / "sessions" / "m-host_idol-opus-claude-rules"
        recs = [json.loads(line) for line in (first / "runs.jsonl").read_text().splitlines()]
        self.assertEqual([r["outcome"] for r in recs], ["runner_error", "timeout"])
        self.assertIn("start failed: the game is running with a save loaded", recs[0]["reason"])
        self.assertEqual(play["dir"], first.with_name(first.name + "-r2"))  # the rerun's fresh dir
        self.assertEqual(StubRunner.stopped, ["start failed", "run end"])

    def test_the_composed_runner_is_a_session_runner(self):
        self.assertTrue(issubclass(StubRunner, rcore.SessionRunner))


if __name__ == "__main__":
    unittest.main()
