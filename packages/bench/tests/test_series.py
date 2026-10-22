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
        self.assertEqual(R.series_caps(None, cli, False), (cli, None, None, []))  # the first run sets them
        frozen = {"run_hours": 4, "max_cost": 30.0, "max_tokens": 9_000_000}
        self.assertEqual(
            R.series_caps(frozen, {"run_hours": None, "max_cost": None, "max_tokens": None}, False),
            (frozen, None, None, []),
        )  # later runs take series.json's
        self.assertEqual(R.series_caps(frozen, cli, False)[2], None)  # same values, one left out
        caps, _, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, False)
        self.assertIn("--allow-cap-change", err)
        self.assertEqual(changes, ["max_cost: 30.0 → 50.0"])
        caps, spend, err, changes = R.series_caps(frozen, {**cli, "max_cost": 50.0}, True)
        self.assertEqual((caps["max_cost"], spend, err, changes), (50.0, None, None, ["max_cost: 30.0 → 50.0"]))

    def test_a_stricter_max_cost_is_the_operators_spend_cap_not_a_cap_change(self):
        frozen = {"run_hours": 1, "max_cost": None, "max_tokens": None}  # nautiloid.json: no cost cap
        cli = {"run_hours": None, "max_cost": 5.0, "max_tokens": None}
        self.assertEqual(R.series_caps(frozen, cli, False), (frozen, 5.0, None, []))
        frozen = {**frozen, "max_cost": 10.0}
        self.assertEqual(R.series_caps(frozen, cli, False), (frozen, 5.0, None, []))
        caps, spend, err, changes = R.series_caps(frozen, {**cli, "max_cost": 12.0}, False)  # looser: refused
        self.assertEqual((spend, changes), (None, ["max_cost: 10.0 → 12.0"]))
        self.assertIn("--allow-cap-change", err)
        caps, spend, err, _ = R.series_caps(frozen, {**cli, "run_hours": 2, "max_cost": 5.0}, True)
        self.assertEqual((caps, spend, err), ({**frozen, "run_hours": 2}, 5.0, None))

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

    def test_series_records(self):
        path = self.tmp / "runs.jsonl"
        recs = [{"series": "naut", "run": 1}, {"playthrough": "naut", "run": 2}, {"series": "other", "run": 1}]
        path.write_text("\n".join([json.dumps(recs[0]), "not json", "[1]", json.dumps(recs[1]), json.dumps(recs[2])]))
        self.assertEqual(R.series_records(path, "naut"), recs[:2])  # a record before "series" keys its playthrough
        self.assertEqual(R.series_records(self.tmp / "missing.jsonl", "naut"), [])

    def test_update_record_that_cant_be_written(self):
        path = self.tmp / "runs.jsonl"
        rec = {"run": 1, "playthrough": "naut", "started": 1.0}
        path.write_text(json.dumps(rec) + "\n")
        (self.tmp / "runs.jsonl.tmp").mkdir()  # the temp file can't be written
        self.assertFalse(R.update_record(path, rec, {"x": 1}))
        self.assertEqual(path.read_text(), json.dumps(rec) + "\n")

    def test_series_client(self):
        self.assertEqual(R.series_client(None, None, "claude", False), ("claude", None, None))  # first run: default
        self.assertEqual(R.series_client(None, "pi", "claude", False), ("pi", None, None))
        self.assertEqual(R.series_client("pi", None, "claude", False), ("pi", None, None))  # frozen rules
        client, err, change = R.series_client("claude", "pi", "claude", False)
        self.assertEqual((client, change), ("pi", "client: claude → pi"))
        self.assertIn("--allow-client-change", err)
        self.assertEqual(R.series_client("claude", "pi", "claude", True), ("pi", None, "client: claude → pi"))

    def test_series_sandbox(self):
        none, box = R.sandbox_mode(None), R.sandbox_mode("container")
        self.assertEqual(none, {"sandbox": "none", "sandbox_unfirewalled": False})
        self.assertFalse(R.sandbox_mode("none", True)["sandbox_unfirewalled"])  # only meaningful with container
        open_box = R.sandbox_mode("container", True)
        self.assertEqual(R.series_sandbox({}, box, False), (None, None))  # a new series: nothing frozen
        self.assertEqual(R.series_sandbox({"client": "pi"}, none, False), (None, None))  # before the sandbox key
        self.assertEqual(R.series_sandbox({"sandbox": "container"}, box, False), (None, None))
        err, change = R.series_sandbox({"sandbox": "container", "sandbox_unfirewalled": True}, none, False)
        self.assertEqual(change, "sandbox: container (unfirewalled) → none")
        self.assertIn("run it with --sandbox container --sandbox-unfirewalled", err)
        err, change = R.series_sandbox({"sandbox": "container"}, open_box, False)
        self.assertIn("run it with --sandbox container, or pass --allow-sandbox-change", err)
        self.assertEqual(R.series_sandbox({"sandbox": "none"}, box, True), (None, "sandbox: none → container"))

    def test_run_names_and_the_next_run(self):
        self.assertEqual([R.run_name("naut", k) for k in (1, 2, 3)], ["naut", "naut-r2", "naut-r3"])
        self.assertEqual(R.next_run_index([]), 1)
        self.assertEqual(R.next_run_index([{"run": 2}, {}, {"run": 1}]), 3)  # a record without a run is run 1
