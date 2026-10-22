#!/usr/bin/env python3
"""The pinned pi install in tools/pi: package.json, the lockfile and node_modules all say pi 1.3.0. The version
check runs the installed binary; it skips with the install command when node_modules is missing. CiTest: the CI job
that installs pi is the one that selects the pi conformance (pytest only).

    python3 -m unittest tools/pi/test_pi_install.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parent
REPO = PI_DIR.parents[1]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
CONFORMANCE = "tools/pi/conformance/test_pi_conformance.py"
PI_VERSION = "1.3.0"
PACKAGES = ("@earendil-works/pi-coding-agent", "@earendil-works/pi-mcp")
BINARY = PI_DIR / "node_modules" / ".bin" / "pi"


class PinnedTest(unittest.TestCase):
    def test_package_json_pins_exact_versions(self):
        deps = json.loads((PI_DIR / "package.json").read_text())["dependencies"]
        self.assertEqual({p: deps[p] for p in PACKAGES}, dict.fromkeys(PACKAGES, PI_VERSION))

    def test_lockfile_resolves_the_pinned_version(self):
        lock = json.loads((PI_DIR / "package-lock.json").read_text())["packages"]
        for p in PACKAGES:
            self.assertEqual(lock[f"node_modules/{p}"]["version"], PI_VERSION, p)

    @unittest.skipUnless(BINARY.exists(), "tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts")
    def test_installed_binary_version(self):
        out = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), PI_VERSION)


def ci_jobs() -> dict[str, str]:
    """ci.yml's jobs by name -> the job's text (a job starts at a two-space-indented `name:` line)."""
    jobs: dict[str, list[str]] = {}
    current = None
    for line in CI_YML.read_text().splitlines():
        if m := re.fullmatch(r"  ([\w-]+):", line):
            current = jobs.setdefault(m[1], [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines) for name, lines in jobs.items()}


def selected(expr: str) -> list[str]:
    """The conformance tests `pytest -m expr` collects."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", "-m", expr, CONFORMANCE],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return [line for line in out.stdout.splitlines() if line.startswith(CONFORMANCE + "::")]


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest only: uv run pytest tools/pi/test_pi_install.py")
class CiTest(unittest.TestCase):
    def test_the_job_that_installs_pi_runs_the_conformance_and_no_other_job_does(self):
        runs = {}
        for name, text in ci_jobs().items():
            exprs = re.findall(r'run: uv run pytest -m "([^"]+)"', text)
            if exprs:
                runs[name] = (bool(re.search(r"working-directory: tools/pi\n\s+run: npm ci", text)), exprs)
        self.assertTrue(any(installs for installs, _ in runs.values()), runs)
        for name, (installs, exprs) in runs.items():
            for expr in exprs:
                with self.subTest(job=name, expr=expr):
                    tests = selected(expr)
                    if installs:
                        self.assertGreater(len(tests), 10)
                    else:
                        self.assertEqual(tests, [])


if __name__ == "__main__":
    unittest.main()
