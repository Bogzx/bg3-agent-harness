"""bg3bench.preflight is bg3harness.preflight itself (the machine checks moved to the harness for bg3harness.doctor),
so the bench's imports and a mock.patch on either name reach the same code."""

from __future__ import annotations

import bg3harness.preflight

import bg3bench.preflight
from bg3bench import preflight


def test_the_bench_name_is_the_harness_module():
    assert bg3bench.preflight is bg3harness.preflight
    assert preflight.check_build is bg3harness.preflight.check_build
