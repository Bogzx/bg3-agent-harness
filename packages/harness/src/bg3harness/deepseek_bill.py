# pyright: basic
"""What DeepSeek bills for one response, from DeepSeek's own usage fields (no endpoint cost field to read).

DeepSeek's usage has prompt_cache_hit_tokens, prompt_cache_miss_tokens and completion_tokens (reasoning included).
Its price sheet (https://api-docs.deepseek.com/zh-cn/quick_start/pricing, read 2026-10-22; the English page served
no table that day) prices each at a peak rate and halves all three off-peak. Peak is Beijing time (UTC+8) Monday to
Friday 09:00-12:00 and 14:00-18:00; every other hour, weekends and Chinese public holidays, is off-peak. Holidays
are not modelled here: they count as peak, which errs high (the safe side for a spend cap).

The peak USD rates are the profile's cost table (pi 1.3.0's catalog numbers: deepseek-flash input 0.3 = cache miss,
cache_read 0.006 = cache hit, output 1.2; the same 1 : 0.02 : 4 ratio as the CNY sheet). pi's own estimate uses them
at all hours, which is why run 1 (research/ovh/live3.md, off-peak) showed $0.50 for a $0.24 bill.

    uv run pytest packages/harness/tests/test_deepseek_bill.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urlparse

BEIJING = timezone(timedelta(hours=8))
PEAK_HOURS = ((9, 12), (14, 18))  # Beijing, Monday to Friday
OFF_PEAK = 0.5
RATES = ("cache_read", "input", "output")  # the profile cost keys this needs


def is_deepseek(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host == "api.deepseek.com"


def off_peak(when: datetime) -> bool:
    t = when.astimezone(BEIJING)
    return t.weekday() >= 5 or not any(a <= t.hour < b for a, b in PEAK_HOURS)


def parse_ts(ts: object) -> datetime | None:
    """A pi entry timestamp (ISO 8601, 'Z' suffix) as an aware datetime; None when it is not one."""
    if not isinstance(ts, str):
        return None
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def cost(usage: dict, prices: dict, when: datetime | None) -> float | None:
    """USD for one response: usage is DeepSeek's raw usage, prices the profile's peak USD per M tokens (input,
    cache_read, output). None when usage lacks DeepSeek's cache fields; an unknown time counts as peak."""
    fields = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
    n = [usage.get(k) for k in fields]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in n):
        return None
    hit, miss, out = n
    usd = (hit * prices["cache_read"] + miss * prices["input"] + out * prices["output"]) / 1e6
    return usd * OFF_PEAK if when is not None and off_peak(when) else usd
