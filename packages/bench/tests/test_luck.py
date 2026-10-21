# pyright: basic
import json
from pathlib import Path

import pytest

from bg3bench import luck, stats

HERE = Path(__file__).resolve().parent
SCHEMA = HERE.parent / "schema" / "rolls.v1.schema.json"


def test_summary_mirrors_enemy_rolls_and_skips_what_is_not_a_fair_d20():
    rolls = [
        {"natural": 20, "party": True, "dc": 15, "success": True, "stream": "Generic"},
        {"natural": 1, "party": False},  # an enemy's 1 is a 20 for the party
        {"natural": 7},  # no party field: counted as the party's
        {"natural": 18, "party": True, "advantage": True},  # max of two: skipped
        {"natural": 3, "disadvantage": True},
        {"natural": None, "party": True, "success": False},  # the osi source saw no face
    ]
    got = luck.summary(rolls)
    assert got == {
        "n": 3,
        "z": round(stats.luck_z([20, 20, 7]), 4),
        "party": 2,
        "other": 1,
        "skipped": 3,
        "nat20": 2,
        "nat1": 0,
    }
    assert got["z"] > 0
    assert luck.summary([]) == {"n": 0, "z": 0.0, "party": 0, "other": 0, "skipped": 0, "nat20": 0, "nat1": 0}
    assert luck.summary([{"natural": 20, "party": False}])["nat1"] == 1


def test_load_checks_every_record(tmp_path):
    good = tmp_path / "rolls.jsonl"
    recs = [{"natural": 12, "result": 17, "dc": 15, "stream": "Generic", "seq": 4, "roller": "x"}, {"natural": None}]
    good.write_text("\n".join(json.dumps(r) for r in recs) + "\n\n")
    assert luck.load(good) == recs
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"natural": 0}\n{"dc": 10}\nnot json\n[1]\n{"natural": 5, "party": "yes", "dc": "x"}\n')
    with pytest.raises(luck.RollLogError) as e:
        luck.load(bad)
    msg = str(e.value)
    for part in ("line 1: natural must be 1..20", "line 2: natural is required", "line 3:", "line 4: not a JSON",
                 "line 5: party must be", "line 5: dc must be"):  # fmt: skip
        assert part in msg
    assert luck.check({"natural": 5, "stream": 3}) == ["roll: stream must be a string or null"]
    assert luck.check({"natural": True}) == ["roll: natural must be 1..20 or null"]


def test_schema_agrees_with_check():
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    for rec in ({"natural": 20, "party": False, "dc": 12, "streams": {"Generic": 2}}, {"natural": None, "gt": 3.5}):
        assert v.is_valid(rec) and luck.check(rec) == []
    for rec in ({"natural": 21}, {}, {"natural": 4, "success": 1}, {"natural": 4, "stream": 9}):
        assert not v.is_valid(rec) and luck.check(rec)
