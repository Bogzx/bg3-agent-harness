# pyright: basic
"""A UI that never came back (bg3le refusing every UI call "bg3le: UI busy" for 26 min after a camp sleep press, the
HUD frozen on screen) is a harness failure, ui_hang, once per streak. A short busy burst (a load, a rest) is not."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from bg3harness import runs

BUSY = (
    "FAILED: bg3le: UI busy: the game's UI is busy (a rest/travel/load transition); refused before its first UI call, "
    "nothing was done; retry in a moment (still busy after waiting 4.8 s)"
)
T0 = 1793401449.1
# the refused calls' times (s after the first) in that run's S001: 36 refusals over 26 min, at most 129 s apart
STREAK = [
    0,
    60,
    150,
    186,
    257,
    277,
    363,
    439,
    445,
    542,
    671,
    678,
    704,
    741,
    780,
    780,
    796,
    833,
    869,
    964,
    970,
    1007,
    1043,
    1117,
    1159,
    1166,
    1202,
    1275,
    1282,
    1338,
    1344,
    1380,
    1418,
    1492,
    1528,
    1565,
]


def _session(d: Path, offsets: Sequence[float], text: str = BUSY) -> Path:
    """S001 with one get_dialogue call per offset, each answered `text` (events.jsonl v1); returns the stream path."""
    evs = []
    for i, off in enumerate(offsets):
        cid = f"c{i}"
        evs.append(
            {
                "v": 1,
                "seq": 2 * i + 1,
                "t": T0 + off,
                "src": "client",
                "kind": "tool_call",
                "tool": "get_dialogue",
                "args": {},
                "call_id": cid,
            }
        )
        evs.append(
            {
                "v": 1,
                "seq": 2 * i + 2,
                "t": T0 + off + 5,
                "src": "client",
                "kind": "tool_result",
                "text": text,
                "call_id": cid,
                "is_error": True,
            }
        )
    (d / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    stream = d / "S001.jsonl"
    stream.write_text("")
    return stream


def _hangs(fails: list[dict]) -> list[dict]:
    return [f for f in fails if f["kind"] == "ui_hang"]


class UiHang(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-uihang-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_the_runs_streak_is_one_harness_failure(self) -> None:
        fails = runs.stream_failures(_session(self.d, STREAK))
        hangs = _hangs(fails)
        self.assertEqual(len(hangs), 1)
        self.assertEqual(hangs[0]["class"], runs.HARNESS)
        self.assertEqual(hangs[0]["t"], round(T0, 1))
        self.assertNotIn("n", hangs[0])  # one hang, one harness failure
        self.assertIn("26 min (36 refused calls)", hangs[0]["detail"])
        self.assertFalse(runs.is_clean("reached", fails, []))
        # each refusal is still listed as the tool's own failure (unknown class)
        self.assertEqual([(f["class"], f.get("n")) for f in fails if f["kind"] == "tool_failed"], [(runs.UNKNOWN, 36)])

    def test_one_refusal_is_no_hang(self) -> None:
        fails = runs.stream_failures(_session(self.d, [0]))
        self.assertEqual(_hangs(fails), [])
        self.assertTrue(runs.is_clean("reached", fails, []))

    def test_a_short_burst_or_spread_out_refusals_are_no_hang(self) -> None:
        self.assertEqual(_hangs(runs.ui_hangs("S001", [T0 + s for s in range(0, 60, 5)])), [])  # 12 in 1 min
        self.assertEqual(_hangs(runs.ui_hangs("S001", [T0 + s for s in range(0, 2000, 200)])), [])  # gaps > 3 min
        self.assertEqual(_hangs(runs.ui_hangs("S001", [T0, T0 + 100, T0 + 200, T0 + 300])), [])  # 4 refusals

    def test_the_thresholds_are_inclusive(self) -> None:
        ts = [T0 + s for s in (0, 75, 150, 225, 300)]  # 5 refusals over exactly 5 min
        self.assertEqual(len(runs.ui_hangs("S001", ts)), 1)
        self.assertEqual(len(runs.ui_hangs("S001", [T0 + s for s in (0, 180, 360, 540, 720)])), 1)  # gaps of 3 min


if __name__ == "__main__":
    unittest.main()
