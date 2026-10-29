# pyright: basic
"""bg3bench.submission.catalog: the website's <suite>/catalog.json from the release, the scenario files and the
model names."""

import json
from pathlib import Path

import pytest

from bg3bench.submission import catalog, pack, release

SCS = pack.load_scenarios()
# the scenario fields the website's board reads (any other field is dropped there)
SITE_FIELDS = ["id", "title", "family", "mode", "cap_min", "seed", "difficulty", "holdout", "task"]


def test_scenario_entries_have_the_sites_fields():
    windmill = catalog.scenario_entry(SCS["fight_act1_windmill"])
    assert list(windmill) == SITE_FIELDS
    assert windmill == {
        "id": "fight_act1_windmill",
        "title": SCS["fight_act1_windmill"]["title"],
        "family": "fight",
        "mode": "pass",
        "cap_min": 60,
        "seed": 1003,
        "difficulty": "Balanced",
        "holdout": False,
        "task": "Win the fight: defeat the goblins at the windmill.",
    }
    assert catalog.scenario_entry(SCS["act1_gold_max"])["mode"] == "maximise"
    karlach = catalog.scenario_entry(SCS["choice_act1_karlach"])
    assert (karlach["family"], karlach["mode"], karlach["cap_min"]) == ("choice", "pass", 30)
    odd = {**SCS["fight_act1_windmill"], "caps": {"run_hours": 0.3}}
    assert catalog.scenario_entry(odd)["cap_min"] == 18


def test_the_catalog_lists_the_release_scenarios_and_a_row_per_client_and_model():
    rel = json.loads(release.default_path().read_text())
    rows = catalog.model_rows()
    doc = catalog.build(rel, SCS, rows, catalog.load_names())
    assert [s["id"] for s in doc["scenarios"]] == list(rel["scenarios"])
    assert not any(s["holdout"] for s in doc["scenarios"])
    assert ("gemini", "gemini-2.5-pro") not in rows  # its profile is unsupported
    assert not any(c == "claude" for c, _ in rows)  # Claude Code runs never enter a release
    assert {("pi", "deepseek-flash"), ("codex", "gpt-5")} <= set(rows)
    assert all(list(m) == ["client", "slug", "name"] for m in doc["models"])
    assert len({(m["client"], m["slug"]) for m in doc["models"]}) == len(doc["models"])
    names = [m["name"] for m in doc["models"]]
    assert len(set(names)) == len(names)  # one model through two endpoints: two names


def test_build_refuses_a_missing_scenario_or_an_unnamed_model():
    rel = {"suite": "bench-v0.8", "scenarios": {"fight_act1_windmill": {}, "gone": {}}}
    with pytest.raises(catalog.CatalogError, match="no scenario file for gone"):
        catalog.build(rel, SCS, [], {})
    with pytest.raises(catalog.CatalogError, match="no display name in models.json for new-model"):
        catalog.build({"suite": "s", "scenarios": {}}, SCS, [("pi", "a"), ("pi", "new-model")], {"a": "x"})


def test_names_file_version(tmp_path: Path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"schema": 2, "models": {}}))
    with pytest.raises(catalog.CatalogError, match="not a v1"):
        catalog.load_names(p)


def test_cli_writes_the_catalog_and_reports_a_missing_release(tmp_path: Path, capsys):
    out = tmp_path / "catalog.json"
    assert catalog.main(["--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert set(doc) == {"scenarios", "models"} and doc["scenarios"] and doc["models"]
    assert all(list(s) == SITE_FIELDS for s in doc["scenarios"])
    assert all(set(m) == {"client", "slug", "name"} for m in doc["models"])
    assert catalog.main(["--suite", "bench-v0.0"]) == 2
    assert "catalog:" in capsys.readouterr().err
