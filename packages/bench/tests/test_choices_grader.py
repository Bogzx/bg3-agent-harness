# pyright: basic
# (basic, not strict: it drives results, run_artifacts and site.build, which are basic and take untyped dicts)
"""The choices grader: picks from the facts' choice events, the persona fit through bg3harness.judge (a fake judge),
the cross-model table, and the path into result.json, the release and the leaderboard page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.judge import FakeJudge

from bg3bench import choices, facts, manifest, results, run_artifacts
from bg3bench.site import build

HERE = Path(__file__).resolve().parent
FACTS = HERE / "fixtures" / "choices" / "karlach.facts.jsonl"
SCENARIO = HERE.parent / "scenarios" / "choice_act1_karlach.json"
SCHEMAS = HERE.parent / "schema"
ANDERS = "Anders: We're hunting a devil. Will you help us?"
KARLACH = "Karlach: Wait! I'm not what they told you."


def timeline() -> list[dict[str, Any]]:
    return facts.load(FACTS)


def verdict(per: dict[int, float]) -> str:
    return json.dumps(
        {
            "score": 8,
            "summary": "a careful paladin",
            "decisions": [{"id": i, "score": s, "why": "w"} for i, s in per.items()],
        }
    )


def assert_valid(name: str, data: Any) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text()))
    errors = list(v.iter_errors(data))
    assert not errors, f"{name}: {errors[0].json_path}: {errors[0].message}"


def test_picks_from_the_choice_events() -> None:
    ps = choices.picks(timeline())
    assert [(p["scene"], p["pick"], p["picked"]) for p in ps] == [
        (ANDERS, "What did it do?", 2),
        (KARLACH, "Talk. Quickly.", 2),  # no picked_text: the option list's
        (KARLACH, "Talk. Quickly.", 2),
    ]
    assert ps[0]["t"] == 1_800_000_028.0  # `at`: when it happened, before the record's `t`
    assert ps[0]["options"] == ["1. Of course.", "2. What did it do?", "3. Not interested."]
    assert (ps[1]["reason"], ps[2]["reason"]) == ("Hear her side.", None)
    unknown = {"kind": "event", "ev": "choice", "scene": "", "options": [], "picked": 4}
    assert choices.picks([unknown, {"kind": "event", "ev": "joined"}])[0]["pick"] == "option 4"


def test_decisions_for_the_judge() -> None:
    ds = choices.decisions(choices.picks(timeline()))
    assert ds[0] == {
        "id": 1,
        "kind": "dialogue",
        "speaker": "Anders",
        "line": "We're hunting a devil. Will you help us?",
        "offered": ["1. Of course.", "2. What did it do?", "3. Not interested."],
        "chosen": "What did it do?",
        "reason": "Ask first; a paladin does not kill on hearsay.",
    }
    no_speaker = choices.decisions([{"scene": "*A door creaks.*", "options": [], "pick": "x", "reason": None}])[0]
    assert (no_speaker["speaker"], no_speaker["line"]) == ("", "*A door creaks.*")


def test_character_of_the_scenario() -> None:
    sc = json.loads(SCENARIO.read_text())
    text = choices.character_of(sc)
    assert text is not None and text.startswith("Character: Tav\nBrief: A half-elf paladin")
    assert text.endswith("Roleplay: light")
    assert choices.character_of({"profile": {}}) is None


def test_fit_through_the_judge() -> None:
    fake = FakeJudge([verdict({1: 9, 2: 7, 3: 7})], model="deepseek-flash")
    g = choices.grade(timeline(), character="# Tav", client=fake, model_under_test="claude-opus-5-5")
    fit = g["fit"]
    assert (fit["status"], fit["score"], fit["judge_model"], fit["call_costs"]) == ("ok", 8.0, "deepseek-flash", [0.01])
    assert [i["score"] for i in fit["items"]] == [9, 7, 7]
    assert "What did it do?" in fake.prompts[0] and "# Tav" in fake.prompts[0]
    # no judge or no character: picks only
    assert choices.grade(timeline())["fit"] is None
    assert choices.grade(timeline(), client=fake)["fit"] is None


def test_fit_refuses_the_model_under_test_and_reports_failures() -> None:
    fake = FakeJudge([], model="claude-opus-5-5")
    fit = choices.fit(choices.picks(timeline()), "# Tav", fake, "opus")
    assert fit["status"] == "judge_failed" and "model under test" in fit["error"] and fake.prompts == []
    assert choices.fit([], "# Tav", FakeJudge([]), "opus")["status"] == "no_decisions"
    bad = choices.fit(choices.picks(timeline()), "# Tav", FakeJudge(["junk", "junk"]), "opus")
    assert bad["status"] == "judge_failed" and "items" not in bad and bad["cost_usd"] == 0.02


def run(model: str, picks: list[tuple[str, str]], *, valid: bool = True, fit: float | None = None) -> dict[str, Any]:
    return {
        "scenario": "choice_act1_karlach",
        "model": model,
        "condition": None,
        "valid": valid,
        "choices": {"picks": [{"scene": s, "pick": p} for s, p in picks], "fit": fit},
    }


def test_cross_model_table() -> None:
    runs = [
        run("opus", [(ANDERS, "What did it do?"), (KARLACH, "Talk."), (KARLACH, "Talk.")], fit=8.0),
        run("opus", [(ANDERS, "Of course.")], fit=6.0),
        run("deepseek", [(ANDERS, "Of course."), (KARLACH, "[Attack]")]),
        run("deepseek", [(ANDERS, "What did it do?")], valid=False),
        {**run("gpt", [(ANDERS, "x")]), "choices": None},
        {**run("opus", [(ANDERS, "Not interested.")]), "condition": "persona"},
    ]
    t = choices.table(runs)
    assert [(e["scenario"], e["scene"]) for e in t] == [
        ("choice_act1_karlach", ANDERS),
        ("choice_act1_karlach", KARLACH),
    ]
    anders, karlach = t
    assert anders["columns"] == ["deepseek", "opus", "opus · persona"]
    assert anders["runs"] == {"opus": 2, "deepseek": 1, "opus · persona": 1}
    assert anders["rows"] == [
        {"pick": "Of course.", "counts": {"opus": 1, "deepseek": 1}},
        {"pick": "Not interested.", "counts": {"opus · persona": 1}},
        {"pick": "What did it do?", "counts": {"opus": 1}},  # the invalid deepseek run is not counted
    ]
    assert anders["fit"] == {"deepseek": None, "opus": 7.0, "opus · persona": None}
    assert karlach["runs"] == {"opus": 1, "deepseek": 1}  # the reload's repeat counts once
    assert karlach["rows"] == [
        {"pick": "Talk.", "counts": {"opus": 1}},
        {"pick": "[Attack]", "counts": {"deepseek": 1}},
    ]
    assert karlach["fit"] == {"deepseek": None, "opus": 7.0}
    assert choices.table([]) == []


def test_release_entry() -> None:
    assert choices.release_entry(None) is None
    ok = {"picks": [{"scene": "s", "pick": "p", "t": 1}], "fit": {"status": "ok", "score": 7.5}}
    assert choices.release_entry(ok) == {"picks": [{"scene": "s", "pick": "p"}], "fit": 7.5}
    failed = {"picks": [], "fit": {"status": "judge_failed", "score": None}}
    assert choices.release_entry(failed) == {"picks": [], "fit": None}


def series_and_record() -> tuple[dict[str, Any], dict[str, Any]]:
    sc = json.loads(SCENARIO.read_text())
    series = {"series": "karlach", "client": "claude", "scenario": {"name": sc["name"], "spec": sc}, "sandbox": "none"}
    record = {"series": "karlach", "run": 1, "started": 1_800_000_000.0, "outcome": "reached", "model": "opus"}
    return series, record


def test_choice_run_artifacts_to_the_leaderboard(tmp_path: Path) -> None:
    series, record = series_and_record()
    (tmp_path / "facts.jsonl").write_text(FACTS.read_text())
    fake = FakeJudge([verdict({1: 9, 2: 7, 3: 7})], model="claude-sonnet-5-5")
    man, res = run_artifacts.write_run_artifacts(tmp_path, series, record, None, None, judge=fake)
    assert res["passed"] is True  # Karlach joined: the goal only ends the run
    assert [p["pick"] for p in res["choices"]["picks"]] == ["What did it do?", "Talk. Quickly.", "Talk. Quickly."]
    assert res["choices"]["fit"]["score"] == 8.0
    assert "Brief: A half-elf paladin" in fake.prompts[0]
    assert_valid("result.v1.schema.json", res)
    assert_valid("manifest.v1.schema.json", man)
    rel = results.build_results([(man, res)], seed=0, created=0.0, b=10)
    assert rel["runs"][0]["choices"]["fit"] == 8.0
    assert_valid("results.v1.schema.json", rel)
    page = build.render(rel)
    assert '<section id="choices">' in page and "Talk. Quickly." in page and "<td>1/1</td>" in page
    assert "persona fit" in page


def test_without_a_judge_or_outside_choice_scenarios(tmp_path: Path) -> None:
    series, record = series_and_record()
    (tmp_path / "facts.jsonl").write_text(FACTS.read_text())
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, record, None, None)
    assert res["choices"]["fit"] is None and len(res["choices"]["picks"]) == 3
    rel = results.build_results([(manifest.from_series(series, record), res)], seed=0, created=0.0, b=10)
    page = build.render(rel)
    assert '<section id="choices">' in page and "persona fit" not in page
    other = {**series["scenario"]["spec"], "family": "exploration"}
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, record, None, None, scenario_spec=other)
    assert "choices" not in res
    assert '<section id="choices">' not in build.render(results.build_results([], seed=0, created=0.0, b=10))
    assert build.choices_html([{"scenario": "s", "scene": "", "columns": [], "runs": {}, "rows": [], "fit": {}}])
