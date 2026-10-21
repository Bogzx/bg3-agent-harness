"""TEMPORARY (remove in Phase D, when the harness has a run API): the matrix's run_one through the legacy CLI.

One job = one more run of a series: `python3 runner/play.py run --scenario S --playthrough <series> --client C
--model M [--effort E] --runs <records + 1> <condition flags>`. play.py's `--runs N` is the series' total (a
re-run resumes it), so the adapter counts the series' records first and reads the new one after. The series is per
(scenario, model, client, effort, condition): every repeat and rerun of a job appends to it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from bg3bench import results
from bg3bench.manifest import run_id
from bg3bench.matrix import RunResult

REPO = Path(__file__).resolve().parents[5]


def series_name(matrix: str, job: Mapping) -> str:
    parts = [matrix, job["scenario"], job["model"], job["client"], job["effort"] or "", job["condition"]]
    return re.sub(r"[^a-z0-9_-]+", "-", "-".join(p for p in parts if p).lower()).strip("-")[:100]


def _records(sessions: Path, series: str) -> list[dict]:
    path = sessions / series / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _execute(cmd: Sequence[str]) -> int:  # pragma: no cover - runs the game
    return subprocess.run(list(cmd), cwd=REPO, check=False).returncode


class LegacyCli:
    """run_one for matrix.Matrix. condition_flags maps each condition id the legacy runner can play to its
    play.py flags; any other condition is refused (today's play.py has no condition switch)."""

    def __init__(
        self,
        matrix: str,
        condition_flags: Mapping[str, Sequence[str]],
        *,
        repo: Path = REPO,
        execute: Callable[[Sequence[str]], int] = _execute,
        python: str = "python3",
    ):
        self.matrix, self.condition_flags, self.repo = matrix, condition_flags, Path(repo)
        self.execute, self.python = execute, python
        self.sessions = self.repo / "runtime" / "sessions"

    def command(self, job: Mapping, series: str, runs: int) -> list[str]:
        if job["condition"] not in self.condition_flags:
            raise ValueError(f"the legacy runner can't play condition {job['condition']!r}")
        cmd = [self.python, str(self.repo / "runner" / "play.py"), "run", "--scenario", job["scenario"]]
        cmd += ["--playthrough", series, "--client", job["client"], "--model", job["model"], "--runs", str(runs)]
        if job["effort"]:
            cmd += ["--effort", job["effort"]]
        return cmd + list(self.condition_flags[job["condition"]])

    def __call__(self, job: dict) -> RunResult:
        series = series_name(self.matrix, job)
        before = _records(self.sessions, series)
        code = self.execute(self.command(job, series, len(before) + 1))
        after = _records(self.sessions, series)
        if len(after) <= len(before):
            return RunResult(None, False, False, None, f"no run record (play.py exit {code})")
        rec = after[len(before)]
        res = results.from_record(rec, run_id(series, rec["run"]))
        return RunResult(res["run_id"], res["valid"], res["passed"], res["cost_usd"], res["invalid_reason"])
