#!/usr/bin/env python3
"""bg3harness.deepseek_bill: DeepSeek's bill from its usage fields, peak and off-peak.

uv run pytest packages/harness/tests/test_deepseek_bill.py
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from bg3harness import deepseek_bill as D  # noqa: N812

FLASH = {"input": 0.3, "output": 1.2, "cache_read": 0.006}  # profiles/deepseek-flash.toml
# run 1's totals (research/ovh/live3.md, S001 bg3.billing entries): 53,296,640 hit, 165,340 miss, 109,003 out
RUN1 = {"prompt_cache_hit_tokens": 53296640, "prompt_cache_miss_tokens": 165340, "completion_tokens": 109003}
RUN1_START = datetime(2026, 10, 21, 21, 16, 7, tzinfo=UTC)  # Thursday 05:16 in Beijing


class Bill(unittest.TestCase):
    def test_off_peak_hours(self):
        # Beijing Monday to Friday 09-12 and 14-18 are peak; UTC 01:00 Thu is 09:00 Thu in Beijing
        self.assertFalse(D.off_peak(datetime(2026, 10, 22, 1, 0, tzinfo=UTC)))
        self.assertTrue(D.off_peak(datetime(2026, 10, 22, 0, 59, tzinfo=UTC)))
        self.assertTrue(D.off_peak(datetime(2026, 10, 22, 4, 0, tzinfo=UTC)))  # 12:00 lunch
        self.assertFalse(D.off_peak(datetime(2026, 10, 22, 9, 59, tzinfo=UTC)))  # 17:59
        self.assertTrue(D.off_peak(datetime(2026, 10, 22, 10, 0, tzinfo=UTC)))  # 18:00
        self.assertTrue(D.off_peak(datetime(2026, 10, 24, 2, 0, tzinfo=UTC)))  # Saturday 10:00
        self.assertTrue(D.off_peak(RUN1_START))

    def test_run1_lands_on_the_balance_delta(self):
        # DeepSeek balance 7.58 -> 7.34; pi's catalog estimate (peak at all hours) said 0.5002
        self.assertAlmostEqual(D.cost(RUN1, FLASH, RUN1_START), 0.2501, places=4)
        self.assertAlmostEqual(D.cost(RUN1, FLASH, datetime(2026, 10, 22, 2, 0, tzinfo=UTC)), 0.5002, places=4)

    def test_unknown_time_counts_as_peak(self):
        self.assertAlmostEqual(D.cost(RUN1, FLASH, None), 0.5002, places=4)

    def test_not_deepseek_usage(self):
        self.assertIsNone(D.cost({"prompt_tokens": 10, "completion_tokens": 5}, FLASH, RUN1_START))
        self.assertIsNone(D.cost({**RUN1, "completion_tokens": True}, FLASH, RUN1_START))

    def test_parse_ts_and_host(self):
        self.assertEqual(D.parse_ts("2026-10-21T21:16:07.030Z"), RUN1_START.replace(microsecond=30000))
        self.assertIsNone(D.parse_ts(1000))
        self.assertIsNone(D.parse_ts("yesterday"))
        self.assertTrue(D.is_deepseek("https://api.deepseek.com"))
        self.assertTrue(D.is_deepseek("https://api.deepseek.com/v1"))
        self.assertFalse(D.is_deepseek("https://openrouter.ai/api/v1"))


if __name__ == "__main__":
    unittest.main()
