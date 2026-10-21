#!/usr/bin/env python3
"""runner/spend.py and the live cost meter on pi's events.

python3 -m unittest runner/test_spend.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clients as C
import pi_client
import runs
import spend as S

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"


class Budget(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-spend-"))
        self.path = self.d / "budget.json"

    def tearDown(self):
        __import__("shutil").rmtree(self.d, True)

    def test_read(self):
        self.assertIsNone(S.read_budget(self.path))
        self.path.write_text(json.dumps({"cap": 5}))
        self.assertEqual(S.read_budget(self.path), {"cap": 5.0, "spent": 0.0})
        for bad in ("{", "[]", '{"spent": 1}', '{"cap": -1}', '{"cap": 5, "spent": "x"}', '{"cap": true}'):
            self.path.write_text(bad)
            with self.assertRaises(S.BudgetError, msg=bad):  # a broken budget stops paid runs
                S.read_budget(self.path)

    def test_run_cap(self):
        self.assertEqual(S.run_cap("claude", None, None), (None, None))  # subscription clients: as given
        self.assertIn("needs --max-cost", S.run_cap("pi", None, {"cap": 5.0, "spent": 0.0})[1])
        cap, why = S.run_cap("pi", 2.0, None)  # live3: no global budget
        self.assertIsNone(cap)
        self.assertIn("needs a global budget", why)
        self.assertIn("--budget USD", why)
        self.assertEqual(S.run_cap("pi", 2.0, {"cap": 5.0, "spent": 4.0}), (1.0, None))
        self.assertEqual(S.run_cap("pi", 0.5, {"cap": 5.0, "spent": 4.0}), (0.5, None))
        cap, why = S.run_cap("pi", 2.0, {"cap": 5.0, "spent": 5.0})
        self.assertIsNone(cap)
        self.assertIn("used up", why)

    def test_ensure_budget(self):
        self.assertIsNone(S.ensure_budget(self.path, None))  # no flag: what is there
        self.assertEqual(S.ensure_budget(self.path, 3.0, write=False), {"cap": 3.0, "spent": 0.0})
        self.assertFalse(self.path.exists())  # --plan writes nothing
        self.assertEqual(S.ensure_budget(self.path, 3.0)["cap"], 3.0)
        self.assertEqual(S.read_budget(self.path)["spent"], 0.0)
        S.add_spend(self.path, 1.0)
        self.assertEqual(S.ensure_budget(self.path, 3.0)["spent"], 1.0)  # the same cap again: kept as is
        with self.assertRaises(S.BudgetError) as e:  # another cap: edit the file
            S.ensure_budget(self.path, 10.0)
        self.assertIn("edit the file", str(e.exception))
        self.assertEqual(S.read_budget(self.path)["cap"], 3.0)
        for bad in (0.0, -1.0):
            with self.assertRaises(S.BudgetError):
                S.ensure_budget(self.d / "other.json", bad)

    def test_add_spend(self):
        self.assertIsNone(S.add_spend(self.path, 0.5))  # no budget file: nothing to add to
        self.assertFalse(self.path.exists())
        self.path.write_text(json.dumps({"cap": 5, "spent": 1.25, "note": "keep"}))
        b = S.add_spend(self.path, 0.5, now=100.0, session="S001")
        self.assertEqual((b["spent"], b["updated"], b["note"]), (1.75, 100.0, "keep"))
        self.assertEqual(b["last"], {"usd": 0.5, "session": "S001"})
        self.assertEqual(json.loads(self.path.read_text())["spent"], 1.75)
        self.assertEqual(S.add_spend(self.path, 0.0)["spent"], 1.75)


def pi_events(name: str) -> list[C.Event]:
    p = pi_client.PiParser()
    return [e for ln in (FIXTURES / name).read_text().splitlines() for e in p.parse(ln)]


class Meters(unittest.TestCase):
    def test_process_cost_follows_the_billed_cost(self):
        c = S.ProcessCost()
        seen = []
        for e in pi_events("billing.jsonl"):
            c.on_event(e)
            seen.append(c.usd)
        self.assertEqual(sorted(set(seen)), [0.0, 0.0125, 0.025])
        self.assertEqual(c.usd, 0.025)

    def test_usage_meter_uses_the_running_cost(self):
        m = runs.UsageMeter(1.0, 1000)
        evs = pi_events("billing.jsonl")
        usage = [e for e in evs if e.kind == "usage"]
        m.on_event(usage[0])
        self.assertAlmostEqual(m.cost, 1.0125)  # before any result: not 0 (no rate guess)
        for e in evs:
            m.on_event(e)
        self.assertAlmostEqual(m.cost, 1.025)  # the result's total, counted once

    def test_claude_meter_unchanged(self):
        m = runs.UsageMeter(1.0, 1000)
        m.on_event(C.Event("usage", message_id="a", tokens={"input": 500}))
        self.assertAlmostEqual(m.cost, 1.5)


if __name__ == "__main__":
    unittest.main()
