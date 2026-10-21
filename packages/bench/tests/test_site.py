import re

import pytest

from bg3bench import results
from bg3bench.site import build

FAMILIES = {"nautiloid": "story", "goblin_fight": "fights", "owlbear": "fights", "gold_1000": "sandbox"}
TOK = {"input": 1000, "output": 20000, "cache_read": 900000, "cache_write": 79000}  # 1.0 M per run


def run(model, scenario, i, passed, cost, *, condition="rules", valid=True, wall=600.0):
    rid = f"{model.split('/')[-1]}-{scenario}-{i}"
    man = {"run_id": rid, "model": {"slug": model}, "condition": condition, "scenario": {"name": scenario}}
    res = {
        "run_id": rid,
        "valid": valid,
        "invalid_reason": None if valid else "runner_error",
        "passed": passed,
        "t_pass_s": 300.0 if passed else None,
        "cost_usd": cost,
        "wall_s": wall,
        "tokens": TOK,
    }
    return man, res


def fixture_release():
    pairs = []
    for i, p in enumerate([1, 1, 0]):
        pairs.append(run("opus", "nautiloid", i, bool(p), 3.0))
        pairs.append(run("openrouter/deepseek/v4", "nautiloid", i, bool(p), 0.5))
    for i, p in enumerate([1, 0, 0, 1]):
        pairs.append(run("opus", "goblin_fight", i, bool(p), 2.0))
        pairs.append(run("openrouter/deepseek/v4", "goblin_fight", i, False, 0.4))
    for i, p in enumerate([1, 1, 1]):
        pairs.append(run("opus", "owlbear", i, bool(p), 2.0))
        pairs.append(run("openrouter/deepseek/v4", "owlbear", i, i == 0, None))
    pairs.append(run("opus", "owlbear", 9, False, 9.0, valid=False))
    for i in range(5):
        pairs.append(run("baseline:donothing", "gold_1000", i, False, 0.0, condition=None))
        pairs.append(run("opus", "gold_1000", i, i < 4, 1.5))
    return results.build_results(pairs, seed=0, created=1792300000.0, bench_tag="bench-v0.1", b=100)


def test_pareto():
    assert build.pareto([]) == []
    assert build.pareto([(1.0, 0.5)]) == [0]
    # (2, .5) is dominated by (1, .5); (3, .9) is not (more successful); the duplicate (1, .5) points both stay
    assert build.pareto([(2.0, 0.5), (1.0, 0.5), (3.0, 0.9), (1.0, 0.5), (0.5, 0.1)]) == [4, 1, 3, 2]
    assert build.pareto([(0.0, 0.0), (0.0, 0.0)]) == [0, 1]
    assert build.pareto([(1.0, 1.0), (2.0, 0.0), (1.0, 0.0)]) == [0]


def test_rows_snapshot():
    got = build.rows(fixture_release(), FAMILIES)
    assert list(got) == ["fights", "sandbox", "story"]
    snap = {
        fam: [
            (
                r["model"],
                r["condition"],
                f"{r['k']}/{r['n']}",
                round(r["rate"], 3),
                [round(x, 3) for x in r["wilson"]],
                r["pass_k"],
                round(r["pass_hat"], 3),
                r["cost_per_run"],
                r["cost_per_success"] and round(r["cost_per_success"], 3),
                r["tokens"],
                r["wall_s"],
                r["t_pass_s"],
                r["n_invalid"],
            )
            for r in fam_rows
        ]
        for fam, fam_rows in got.items()
    }
    assert snap == {
        "fights": [
            ("opus", "rules", "5/7", 0.714, [0.359, 0.918], 3, 0.5, 2.0, 2.8, 1000000, 600.0, 300.0, 1),
            (
                "openrouter/deepseek/v4",
                "rules",
                "1/7",
                0.143,
                [0.026, 0.513],
                3,
                0.0,
                None,
                None,
                1000000,
                600.0,
                300.0,
                0,
            ),
        ],
        "sandbox": [
            ("opus", "rules", "4/5", 0.8, [0.376, 0.964], 5, 0.0, 1.5, 1.875, 1000000, 600.0, 300.0, 0),
            ("baseline:donothing", None, "0/5", 0.0, [0.0, 0.434], 5, 0.0, 0.0, None, 1000000, 600.0, None, 0),
        ],
        "story": [
            (
                "openrouter/deepseek/v4",
                "rules",
                "2/3",
                0.667,
                [0.208, 0.939],
                3,
                0.0,
                0.5,
                0.75,
                1000000,
                600.0,
                300.0,
                0,
            ),
            ("opus", "rules", "2/3", 0.667, [0.208, 0.939], 3, 0.0, 3.0, 4.5, 1000000, 600.0, 300.0, 0),
        ],
    }
    opus_fights = got["fights"][0]
    assert opus_fights["scenarios"] == ["goblin_fight", "owlbear"]
    assert "opus-owlbear-9" in opus_fights["run_ids"]  # invalid runs are listed, not scored


def test_rows_without_families_or_valid_runs():
    release = fixture_release()
    (only,) = build.rows(release).values()
    assert len(only) == 3 and only[0]["model"] == "opus"
    release["runs"] = [{**r, "valid": False} for r in release["runs"]]
    for row in build.rows(release)["all"]:
        assert (row["n"], row["rate"], row["pass_hat"], row["cost_per_run"], row["tokens"]) == (
            0,
            None,
            None,
            None,
            None,
        )


def test_page_html(tmp_path):
    html = build.render(fixture_release(), FAMILIES, manifest_href="runs/{run_id}/m.json")
    assert html.startswith("<!doctype html>") and "Release bench-v0.1" in html
    assert [m for m in re.findall(r'<section id="family-(\w+)"', html)] == ["fights", "sandbox", "story"]
    assert html.count("<svg") == 3
    assert '<a href="runs/opus-owlbear-9/m.json">opus-owlbear-9</a>' in html
    # one table row per (model, condition) per family
    assert html.count('<tr><th scope="row">') == 6
    first = re.search(r"<tr><th scope=\"row\">(.*?)</tr>", html).group(1)
    assert re.sub(r"<[^>]+>", "|", first).split("|")[:4] == ["opus", "rules", "", ""]
    # pass^3: goblin_fight 2/4 -> 0, owlbear 3/3 -> 1, mean 50%
    assert "<td>5/7</td><td>71% <small>[36%, 92%]</small></td><td>50% <small>k=3</small></td>" in first
    assert "<td>$2.00</td><td>$2.80</td><td>1.00 M</td><td>10.0 min</td><td>5.0 min</td><td>1</td>" in first
    # the frontier in the sandbox family: donothing ($0, 0%) and opus ($1.50, 80%) are both on it
    sandbox = html.split('id="family-sandbox"')[1].split("</section>")[0]
    assert sandbox.count('class="pt on"') == 2 and "<polyline" in sandbox
    # deepseek has no known cost in fights, so the plot has one point and no frontier line
    fights = html.split('id="family-fights"')[1].split("</section>")[0]
    assert fights.count("<circle") == 1 and "<polyline" not in fights
    assert "&lt;" not in build.render(fixture_release(), {"nautiloid": "<x>"}).split("<main>")[0]


def test_plot_and_formatting_edges():
    assert "No run" in build.plot_svg([{"cost_per_run": None, "rate": 0.5, "model": "m", "condition": None}])
    svg = build.plot_svg([{"cost_per_run": 0.0, "rate": 0.0, "model": "m", "condition": None}])
    assert "<circle" in svg and "$0.00" in svg
    assert build._tokens(5000) == "5 k" and build._tokens(None) == "–"


def test_cli(tmp_path):
    release = fixture_release()
    results.write(release, tmp_path / "results.json")
    (tmp_path / "fam.json").write_text('{"nautiloid": "story"}')
    out = tmp_path / "site" / "index.html"
    assert build.main([str(tmp_path / "results.json"), str(out), "--families", str(tmp_path / "fam.json")]) == 0
    html = out.read_text()
    assert 'id="family-story"' in html and 'id="family-all"' in html
    assert build.main([str(tmp_path / "results.json"), str(tmp_path / "b.html")]) == 0
    with pytest.raises(ValueError):
        (tmp_path / "bad.json").write_text('{"schema": 3}')
        build.main([str(tmp_path / "bad.json"), str(tmp_path / "c.html")])
