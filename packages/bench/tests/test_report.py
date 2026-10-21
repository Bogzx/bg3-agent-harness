# pyright: basic
import json
import re
from pathlib import Path

import pytest

from bg3bench import facts, graders, manifest, methods, report, results

HERE = Path(__file__).resolve().parent
NAUT2 = HERE / "fixtures" / "naut2"
TIMELINES = HERE / "fixtures" / "timelines"
LUCK = {"n": 40, "z": 1.5, "party": 30, "other": 10, "skipped": 3, "nat20": 4, "nat1": 1}


def series_pairs():
    """naut2 as one realtime series: run 1 graded from its facts, run 2 from the gold timeline with method
    attribution and a roll log, run 3 invalid (an operator acted)."""
    series = json.loads((NAUT2 / "series.json").read_text())
    records = [json.loads(line) for line in (NAUT2 / "runs.jsonl").read_text().splitlines()]
    naut = facts.load(TIMELINES / "naut2_S001.facts.jsonl")
    gold = facts.load(TIMELINES / "gold_spend.facts.jsonl")
    extra = [
        {"grade": graders.grade({"milestone": "nautiloid_escaped"}, naut, clock="wall")},
        {
            "grade": graders.grade({"gold": {"gte": 1000}}, gold, clock="wall"),
            "methods": methods.attribute(gold),
            "luck": LUCK,
        },
        {},
    ]
    pairs = []
    for i, (rec, kw) in enumerate(zip(records, extra, strict=True)):
        rec = {**rec, "model_time": {"model_s": 150.0 + 30 * i}}
        if i == 2:
            rec["operator"] = [{"t": 1, "what": "reloaded by hand"}]
        man = manifest.from_series(series, rec, condition="rules", seed=1001, timing="realtime")
        pairs.append((man, results.from_record(rec, man["run_id"], **kw)))
    return pairs


def test_build():
    rep = report.build(reversed(series_pairs()))
    assert (rep["series"], rep["scenario"], rep["timing"], rep["seed"]) == ("naut2", "nautiloid", "realtime", 1001)
    assert [r["run"] for r in rep["runs"]] == [1, 2, 3]
    assert [r["verdict"] for r in rep["runs"]] == ["pass", "pass", "invalid (operator)"]
    r1, r2, _ = rep["runs"]
    assert r1["methods"] is None and r1["luck"] is None and r1["curve"]["fact"] == "gold"
    assert r2["methods"]["gold"] == {"sold": 0, "stolen": 0, "looted": 0, "quest": 0, "other": 1450}
    assert r2["luck"] == LUCK and r2["think_s"] == 180.0
    assert r1["think_share"] == pytest.approx(150.0 / 600.0)
    assert r1["links"] == {
        "manifest": "naut2-r001/manifest.json",
        "events": "naut2-r001/events.jsonl",
        "facts": "naut2-r001/facts.jsonl",
    }
    s = rep["summary"]
    assert (s["n_runs"], s["n_valid"], s["k"], s["rate"]) == (3, 2, 2, 1.0)  # the invalid run is not counted
    assert s["cost_total"] == pytest.approx(3.349 + 3.3406)
    assert (s["think_median"], s["luck_mean_z"]) == (165.0, 1.5)


def test_build_edges():
    pairs = series_pairs()
    with pytest.raises(ValueError, match="no runs"):
        report.build([])
    with pytest.raises(ValueError, match="paired"):
        report.build([(pairs[0][0], pairs[1][1])])
    other = {**pairs[1][0], "series": "other"}
    with pytest.raises(ValueError, match="one series"):
        report.build([pairs[0], (other, pairs[1][1])])
    (m, r) = pairs[0]
    failed = {**r, "passed": False, "t_pass_s": None, "outcome": "timeout", "cost_usd": None, "grade": None}
    rep = report.build([(m, failed)])
    assert rep["runs"][0]["verdict"] == "fail (timeout)" and rep["runs"][0]["curve"] is None
    assert (rep["summary"]["cost_total"], rep["summary"]["luck_mean_z"]) == (None, None)
    assert report.curves_svg(rep) == ""
    no_wall = report.build([(m, {**r, "wall_s": None, "think_s": None})])
    assert no_wall["runs"][0]["think_share"] is None
    assert "Thinking" in report.markdown(no_wall)
    only_invalid = report.build([pairs[2]])
    assert only_invalid["summary"]["rate"] is None and only_invalid["summary"]["wall_median"] is None
    assert only_invalid["runs"][0]["passed"] is False  # its record said "reached"; invalid runs never pass
    assert "0/0 valid runs, – " in report.markdown(only_invalid)


def test_markdown():
    md = report.markdown(report.build(series_pairs()))
    assert md.startswith("# Series naut2\n")
    assert "- **Success:** 2/2 valid runs, 100% [95% Wilson 34%, 100%]" in md
    assert "- **Dice seed:** 1001" in md and "- **Invalid runs:** 1" in md
    assert "Real-time scenario" in md
    rows = [line for line in md.splitlines() if re.match(r"\| \d ", line)]
    assert len(rows) == 3
    assert "| 2 | opus · rules | pass | 45.0 min |" in rows[1]
    assert "z +1.50 (n=40)" in rows[1] and "gold: 10m 120, 20m 400, 30m 300" in rows[1]
    assert "other 1450" in rows[1] and "3.0 min (29%)" in rows[1]
    assert "[facts](naut2-r002/facts.jsonl)" in rows[1] and "[events](naut2-r002/events.jsonl)" in rows[1]
    assert "invalid (operator)" in rows[2]
    turn = report.build([(dict(m, scenario={**m["scenario"], "timing": "turn_based"}), r) for m, r in series_pairs()])
    assert "Real-time" not in report.markdown(turn) and "Real-time" not in report.html(turn)


def test_html():
    page = report.html(report.build(series_pairs()))
    assert page.startswith("<!doctype html>") and "<title>Series naut2</title>" in page
    assert page.count('<tr class="ok">') == 2 and page.count('<tr class="no">') == 1
    assert '<a href="naut2-r001/facts.jsonl">facts</a>' in page
    assert page.count("<polyline") == 2  # run 1's curve (one known point) and run 2's
    assert "<script" not in page and "Real-time scenario" in page


def test_methods_text_lists_counters():
    m = {"gold": {"sold": 300, "stolen": 0, "looted": 0, "quest": 0, "other": 0}, "spent": 10, "crimes": 2,
         "kills": 0, "traders_killed": 1, "pickpockets": 0}  # fmt: skip
    assert report._methods_text(m) == "sold 300; crimes 2, traders_killed 1"
    assert report._methods_text({**m, "gold": dict.fromkeys(m["gold"], 0)}).startswith("no gold made")


def test_cli(tmp_path):
    runs = tmp_path / "runs"
    for man, res in series_pairs():
        d = runs / man["run_id"]
        d.mkdir(parents=True)
        manifest.write(man, d / "manifest.json")
        results.write(res, d / "result.json")
    (runs / "stray").mkdir()
    out = tmp_path / "out"
    assert report.main([str(runs), str(out), "--series", "naut2"]) == 0
    assert "| 3 |" in (out / "report.md").read_text()
    assert '<a href="naut2-r003/manifest.json">' in (out / "report.html").read_text()
    links = '{"facts": "https://example.org/{run_id}/facts.jsonl"}'
    assert report.main([str(runs), str(out), "--links", links]) == 0
    assert "(https://example.org/naut2-r001/facts.jsonl)" in (out / "report.md").read_text()
    with pytest.raises(ValueError, match="no runs"):
        report.main([str(runs), str(out), "--series", "nope"])
