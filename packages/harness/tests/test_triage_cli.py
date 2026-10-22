"""bg3harness.triage's command line: --help prints usage and writes nothing (it once wrote a file named
--help.triage.md), a session path writes <session>.triage.md."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from bg3harness import triage


class TriageCli(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-triage-"))
        cwd = Path.cwd()
        os.chdir(self.d)
        self.addCleanup(os.chdir, cwd)

    def test_help_prints_usage_and_writes_nothing(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            triage.main(["--help"])
        self.assertEqual(e.exception.code, 0)
        self.assertIn("usage: python -m bg3harness.triage", out.getvalue())
        self.assertEqual(list(self.d.iterdir()), [])

    def test_no_session_is_an_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            triage.main([])
        self.assertEqual(e.exception.code, 2)

    def test_a_session_file_writes_its_triage(self) -> None:
        (self.d / "S002.jsonl").write_text("")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(triage.main([str(self.d / "S002.jsonl")]), 0)
        report = self.d / "S002.triage.md"
        self.assertEqual(out.getvalue().strip(), str(report))
        self.assertTrue(report.read_text().startswith("# Triage S002"))
