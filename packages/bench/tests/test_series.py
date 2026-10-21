"""bg3bench.series: the frozen caps and judge model, runs.jsonl records updated in place (moved from
runner/test_runs.py)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bg3bench import series as R


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3bench-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class Series(TmpDir):
    def test_series_caps(self):
        cli = {"run_hours": 4, "max_cost": 30.0, "max_tokens": None}
        self.assertEqual(R.series_caps(None, cli, False), (cli, None, []))  # the first run sets them
        frozen = {"run_hours": 4, "max_cost": 30.0, "max_tokens": 9_000_000}
        self.assertEqual(
            R.series_caps(frozen, {"run_hours": None, "max_cost": None, "max_tokens": None}, False), (frozen, None, [])
        )  # later runs take series.json's
        self.assertEqual(R.series_caps(frozen, cli, False)[1], None)  # same values, one left out
        caps, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, False)
        self.assertIn("--allow-cap-change", err)
        self.assertEqual(changes, ["max_cost: 30.0 → 50.0"])
        caps, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, True)
        self.assertEqual((caps["max_cost"], err, changes), (50.0, None, ["max_cost: 30.0 → 50.0"]))

    def test_update_record_in_place(self):
        path = self.tmp / "runs.jsonl"
        recs = [{"run": 1, "playthrough": "naut", "started": 1.0}, {"run": 2, "playthrough": "naut-r2", "started": 2.0}]
        path.write_text(json.dumps(recs[0]) + "\nnot json\n" + json.dumps(recs[1]) + "\n")
        self.assertTrue(R.update_record(path, recs[1], {"persona": {"score": 5}}))
        lines = path.read_text().splitlines()
        self.assertEqual(lines[:2], [json.dumps(recs[0]), "not json"])  # the rest byte for byte
        self.assertEqual(json.loads(lines[2]), {**recs[1], "persona": {"score": 5}})
        self.assertFalse(R.update_record(path, {"run": 3, "playthrough": "naut-r3", "started": 3.0}, {"x": 1}))
        self.assertFalse(R.update_record(self.tmp / "missing.jsonl", recs[0], {"x": 1}))
        self.assertFalse((self.tmp / "runs.jsonl.tmp").exists())

    def test_series_judge_model(self):
        self.assertEqual(R.series_judge_model(None, None, "d", False), ("d", None, None))  # first run: default
        self.assertEqual(R.series_judge_model(None, "j1", "d", False), ("j1", None, None))
        self.assertEqual(R.series_judge_model("j1", None, "d", False), ("j1", None, None))  # frozen rules
        self.assertEqual(R.series_judge_model("j1", "j1", "d", False), ("j1", None, None))
        _model, err, change = R.series_judge_model("j1", "j2", "d", False)
        self.assertIn("--allow-judge-change", err)
        self.assertEqual(change, "judge_model: j1 → j2")
        self.assertEqual(R.series_judge_model("j1", "j2", "d", True), ("j2", None, "judge_model: j1 → j2"))
