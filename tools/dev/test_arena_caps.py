# pyright: basic
"""arena_caps over synthetic run folders: the sample, list price, the thin rule, the over-budget count.

uv run -q pytest tools/dev/test_arena_caps.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arena_caps as caps

X, Y = "pi/x", "pi/y"


def run(root: Path, name: str, costs: dict, a: str = X, b: str = Y, end: str = "wipe", **fields) -> Path:
    d = root / name
    d.mkdir(parents=True)
    rec = {
        "arena": "arena-v0",
        "sides": {"A": {"player": a, "model": a[3:]}, "B": {"player": b, "model": b[3:]}},
        "end": {"reason": end, **fields},
        "invalid": None if end == "wipe" else f"{end}: why",
        "costs": costs,
    }
    (d / "arena.json").write_text(json.dumps(rec))
    return d


def response(total: float) -> str:
    msg = {"role": "assistant", "usage": {"cost": {"total": total}}}
    return json.dumps({"type": "message_end", "message": msg}) + "\n"


def by_player(root: Path) -> dict:
    return {r["player"]: r for r in caps.caps(caps.run_dirs([root]))}


def test_a_cap_is_three_times_the_p95_rounded_up_to_a_cent(tmp_path):
    for i in range(1, 21):
        run(tmp_path, f"m{i}", {"A": i / 1000, "B": 0.0}, b="pi/y")
    x = by_player(tmp_path)[X]
    assert (x["sides"], x["matches"], x["list_p95"], x["max"], x["cap"], x["thin"]) == (
        20,
        20,
        0.019,
        0.02,
        0.06,
        False,
    )


def test_below_twenty_sides_there_is_no_cap(tmp_path):
    for i in range(19):
        run(tmp_path, f"m{i}", {"A": 0.5, "B": 0.1})
    x = by_player(tmp_path)[X]
    assert (x["sides"], x["cap"], x["thin"]) == (19, None, True)
    assert "too thin for a cap (19 of 20 sides)" in caps.show(x)


def test_list_price_is_the_figure_a_cap_is_set_from_and_a_resumes_stream_counts(tmp_path):
    """DeepSeek bills half off-peak; the raw stream has the list price. Other jsonl files are no streams."""
    d = run(tmp_path, "m", {"A": 0.01, "B": 0.5})
    (d / "A.jsonl").write_text("Warning: not json\n" + response(0.015))
    (d / "A-r1.jsonl").write_text(response(0.005))
    (d / "A.events.jsonl").write_text(response(9.0))
    (d / "A-r1.metrics.jsonl").write_text(response(9.0))
    rows = by_player(tmp_path)
    assert (rows[X]["billed_p95"], rows[X]["list_p95"]) == (0.01, 0.02)
    assert rows[Y]["list_p95"] == 0.5  # no stream: what it was billed


def test_an_over_budget_match_stays_in_the_sample_and_a_cut_one_does_not(tmp_path):
    run(tmp_path, "over", {"A": 0.9, "B": 0.1}, end="over_budget", capped=["A"])
    run(tmp_path, "cut", {"A": 5.0, "B": 0.1}, end="fault")
    run(tmp_path, "ok", {"A": 0.2, "B": 0.1})
    rows = by_player(tmp_path)
    assert (rows[X]["sides"], rows[X]["max"], rows[X]["cut"], rows[X]["attempts"]) == (2, 0.9, 1, 3)
    assert (rows[X]["over_budget"], rows[X]["over_budget_flag"]) == (1, True)
    assert rows[Y]["over_budget"] == 0


def test_a_model_against_itself_is_one_match_and_a_scripted_side_is_no_model(tmp_path):
    run(tmp_path, "self", {"A": 0.1, "B": 0.2}, b=X)
    run(tmp_path, "scripted", {"A": 0.3, "B": 0.0}, b="pi/none")
    x = by_player(tmp_path)[X]
    assert (x["attempts"], x["sides"]) == (2, 3)
    assert set(by_player(tmp_path)) == {X}
