#!/usr/bin/env python3
"""Tonight's pilot matrix (packages/bench/matrices/pilot-2026-10-22.toml) as the live worker runs it: the plan, the
DeepSeek spend bounded by construction, the run refused without runtime/budget.json, and each paid job's per-run
cap reaching the runner. No game, no key, no network. The cap stopping a DeepSeek-billed run mid-way is played
through pi on the stub model in runner/test_pi_e2e.py (DeepSeekCap); research/ovh/pilot-runbook.md is the runbook.

    uv run pytest tools/pi/test_pilot.py
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PILOT = REPO / "packages" / "bench" / "matrices" / "pilot-2026-10-22.toml"
DEEPSEEK_USD = 1.5  # the pilot's DeepSeek spend, all paid jobs together (user, 2026-10-22: $5 cap, stop at 2.58)

try:
    from bg3bench import matrix, matrix_cli
    from bg3harness import pi_client, profiles
    from bg3harness.runner import run_host

    UV_ENV = True
except ImportError:  # system python: needs the uv env
    UV_ENV = False


def repo_copy(tmp: Path) -> Path:
    """A repo whose runtime/ is a temp dir: the matrix writes runtime/matrix/<name>/ and reads runtime/budget.json."""
    for d in ("packages", "profiles"):
        (tmp / d).symlink_to(REPO / d)
    (tmp / "runtime").mkdir()
    return tmp


@unittest.skipUnless(UV_ENV, "needs the uv env (uv run pytest)")
class Pilot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-pilot-"))
        self.repo = repo_copy(self.tmp)
        self.m = matrix_cli.load(PILOT, REPO)
        self.paid = [j for j in self.m["jobs"] if j["client"] in matrix.PAID_CLIENTS]

    def budget(self, cap: float, spent: float = 1.0) -> Path:
        path = self.repo / "runtime" / "budget.json"
        path.write_text(json.dumps({"cap": cap, "spent": spent}))
        return path

    def test_play_py_plans_every_job(self):
        out = subprocess.run(
            [sys.executable, "runner/play.py", "bench", "plan", "--matrix", str(PILOT.relative_to(REPO))],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        for j in self.m["jobs"]:
            self.assertRegex(out.stdout, rf"(?m)^{j['id'].replace('+', r'\+')}\s+{j['client']}\s")
        self.assertIn(f"{len(self.m['jobs'])} jobs ({len(self.paid)} paid), reserve ${DEEPSEEK_USD:.2f}", out.stdout)

    def test_the_deepseek_spend_is_bounded_by_the_per_job_caps(self):
        self.assertEqual(len(self.paid), 6)
        self.assertTrue(all(j["max_cost"] is not None for j in self.paid))
        self.assertLessEqual(sum(j["max_cost"] for j in self.paid), DEEPSEEK_USD)
        for j in self.paid:  # each paid job bills DeepSeek's usage fields at the profile's rates
            prof = profiles.load(REPO / j["profile"])
            parser = pi_client.PiClient(prof, self.tmp / "system.txt", server={}).new_parser()
            self.assertIsNotNone(parser.deepseek_prices, j["profile"])

    def test_claude_code_plays_the_same_cells(self):
        def cells(client: str) -> set[tuple[str, str]]:
            return {(j["scenario"], j["condition"]) for j in self.m["jobs"] if j["client"] == client}

        self.assertEqual(cells("claude"), cells("pi"))
        self.assertTrue(all(j["max_cost"] is not None for j in self.m["jobs"]))  # the scenario's reserve

    def run_matrix(self, run_one=None, host=None) -> tuple[int, str]:
        out = io.StringIO()
        rc = matrix_cli.main(
            ["run", "--matrix", str(PILOT)], repo=self.repo, run_one=run_one, host=host, now=lambda: 0.0, out=out
        )
        return rc, out.getvalue()

    def test_the_run_stops_before_its_first_job_without_a_budget_file(self):
        started = []

        class Host:
            def start(self, job, sc, run_dir):
                started.append(job["id"])
                raise AssertionError("no job may start")

            def record(self, job, sc, run_dir, outcome):
                raise AssertionError("no job may start")

        rc, out = self.run_matrix(host=Host())
        self.assertEqual((rc, started), (1, []))
        self.assertIn("stopped: budget", out)

    def billed_in_full(self, ran: list[str]):
        def run_one(job: dict) -> matrix.RunResult:  # every paid run spends its whole reserve
            ran.append(job["id"])
            paid = job["client"] in matrix.PAID_CLIENTS
            return matrix.RunResult(job["id"], True, False, job["max_cost"] if paid else 0.0, None, not paid)

        return run_one

    def test_a_budget_of_the_pilots_total_runs_it_all(self):
        path, ran = self.budget(cap=1.0 + DEEPSEEK_USD), []
        rc, out = self.run_matrix(run_one=self.billed_in_full(ran))
        self.assertEqual(rc, 0, out)
        self.assertEqual(ran, [j["id"] for j in self.m["jobs"]])
        self.assertAlmostEqual(json.loads(path.read_text())["spent"], 1.0 + DEEPSEEK_USD)

    def test_a_smaller_budget_stops_before_the_job_it_cant_cover(self):
        self.budget(cap=1.0 + DEEPSEEK_USD - 0.01)
        ran: list[str] = []
        rc, out = self.run_matrix(run_one=self.billed_in_full(ran))
        self.assertEqual(rc, 1)
        self.assertIn("stopped: budget", out)
        self.assertEqual(sum(1 for j in ran if "deepseek" in j), 5)

    def test_each_paid_job_caps_its_run(self):
        sc = self.m["scenarios"]["nautiloid"]
        for j in self.m["jobs"]:
            opt = run_host.options_for(j, sc, "run", {})
            self.assertEqual(opt.max_cost, j["max_cost"])  # the runner's cost cap (runner/test_pi_e2e.py)
            self.assertEqual((opt.client, opt.scenario), (j["client"], "nautiloid"))


if __name__ == "__main__":
    unittest.main()
