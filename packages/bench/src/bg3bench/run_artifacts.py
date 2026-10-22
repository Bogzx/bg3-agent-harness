# pyright: basic
"""A finished run's manifest.json and result.json, written next to its facts.jsonl (the harness calls this at run end).

The run directory is the run's playthrough directory (runtime/sessions/<run name>/, runner/runs.py run_name), where
the harness writes facts.jsonl (and rolls.jsonl when the mod logs rolls). `write_run_artifacts` grades facts.jsonl
with the scenario's goal (graders.grade, on the scenario's clock), adds the method attribution when the scenario asks
for it, the choices grader's picks for a choice scenario (and their persona fit, given a judge client) and the
luck summary when there is a roll log, and writes:

- manifest.json: manifest.from_series over series.json and the run's runs.jsonl record, with the harness version,
  the prompt hash, the condition and the scenario's dice seed and timing.
- result.json: results.from_record with that grade. Without facts.jsonl the runner's own milestone verdict is used
  (results.py); a malformed facts.jsonl raises (a harness bug, never a silent fallback to the milestone verdict).

report.load_runs(<runtime/sessions>) then finds every run that has both files.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bg3bench import choices, facts, graders, luck, manifest, methods, results, scenario

FACTS = "facts.jsonl"
ROLLS = "rolls.jsonl"
MANIFEST = "manifest.json"
RESULT = "result.json"


def series_scenario(series: dict) -> dict:
    """The scenario a series froze in series.json (`scenario.spec`, v1 or v2), validated, as v2 with defaults."""
    spec = (series.get("scenario") or {}).get("spec")
    if not isinstance(spec, dict):
        raise ValueError(f"series {series.get('series')!r} has no scenario spec")  # noqa: TRY004 - bad file content
    return _v2(spec)


def write_run_artifacts(
    run_dir: str | Path,
    series: dict[str, Any],
    run_record: dict[str, Any],
    harness_version: str | None,
    prompt_sha: str | None,
    *,
    prompt_parts: Sequence[dict[str, Any]] = (),
    scenario_spec: dict[str, Any] | None = None,
    condition: str | None = None,
    bench_tag: str | None = None,
    tools: dict[str, Any] | None = None,
    provider: str | None = None,
    client_version: str | None = None,
    price_sheet: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    judge: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Grade the run in run_dir and write its manifest.json and result.json there; returns (manifest, result).

    `prompt_sha` is the sha256 of the assembled prompt (`prompt_parts`: [{"file", "sha256"}]); `scenario_spec`
    overrides series.json's frozen spec (a v2 file from packages/bench/scenarios). `judge` (a bg3harness.judge
    client, never the model under test) scores a choice scenario's picks against its character brief."""
    run_dir = Path(run_dir)
    sc = series_scenario(series) if scenario_spec is None else _v2(scenario_spec)
    man = manifest.from_series(
        series,
        run_record,
        bench_tag=bench_tag,
        condition=condition,
        seed=sc["game_settings"].get("seed"),
        timing=sc["timing"],
        prompt=None if prompt_sha is None else {"sha256": prompt_sha, "parts": [dict(p) for p in prompt_parts]},
        tools=tools,
        price_sheet=price_sheet,
        provider=provider,
        client_version=client_version,
        profile=profile,
        harness_version=harness_version,
    )
    grade = attribution = luck_summary = picked = None
    if (run_dir / FACTS).is_file():
        timeline = facts.load(run_dir / FACTS)
        grade = graders.grade(scenario.goal_of(sc), timeline)
        if sc["methods"]:
            attribution = methods.attribute(timeline)
        if sc["family"] == "choice":
            picked = choices.grade(
                timeline, character=choices.character_of(sc), client=judge, model_under_test=run_record.get("model")
            )
    if (run_dir / ROLLS).is_file():
        luck_summary = luck.summary(luck.load(run_dir / ROLLS))
    res = results.from_record(run_record, man["run_id"], grade, attribution, luck_summary, picked)
    manifest.write(man, run_dir / MANIFEST)
    results.write(res, run_dir / RESULT)
    return man, res


def _v2(spec: dict) -> dict:
    sc = spec if "schema" in spec else scenario.from_v1(spec)
    if errs := scenario.validate(sc, spec.get("name", "?")):
        raise scenario.ScenarioError("; ".join(errs))
    return scenario.with_defaults(sc)
