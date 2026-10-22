# pyright: basic
"""bg3harness.run_manifest's fields reach the manifest through write_run_artifacts (schema-valid)."""

import json
from pathlib import Path

from bg3harness import run_manifest
from test_run_artifacts import assert_valid, load_series, readout

from bg3bench import run_artifacts

REPO = Path(__file__).resolve().parents[3]
REGISTRY = json.loads((REPO / "runner" / "fixtures" / "claude" / "e2e-naut-init.json").read_text())["registry"]
NAUT = json.loads((REPO / "packages" / "bench" / "scenarios" / "nautiloid.json").read_text())


def test_manifest_records_the_harness_fields(tmp_path):
    series, records = load_series()
    fields = run_manifest.manifest_fields("claude", "2.1.301", "rules+coaching", REGISTRY, NAUT, "instructions")
    man, _ = run_artifacts.write_run_artifacts(
        tmp_path,
        series,
        records[0] | readout(NAUT["game_settings"]["seed"]),
        "0.1.0",
        **run_manifest.artifact_kwargs(fields),
        scenario_spec=NAUT,
    )
    assert (
        (man["seed"], man["scenario"]["timing"])
        == (fields["seed"], fields["timing"])
        == (NAUT["game_settings"]["seed"], "realtime")
    )
    assert man["client"] == {"name": "claude", "version": "2.1.301"} and man["condition"] == "rules+coaching"
    assert man["prompt"] == fields["prompt"] and man["tools"] == fields["tools"]
    assert_valid("manifest.v1.schema.json", man)
