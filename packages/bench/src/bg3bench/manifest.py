# pyright: basic
"""Run manifests (schema/manifest.v1.schema.json): what a run was set up with.

`from_series` builds one from a series' `series.json` and one `runs.jsonl` record, the files the runner writes today
(runner/runs.py). What those files don't record yet (prompt hash and parts, tool allowlist, price sheet, provider,
client version, condition, the dice seed, the scenario's timing and the harness version) is passed in, or stays null.
`judge` names the LLM judge that scored the run (the persona judge's model), which must not be a contestant.
Every manifest cites the harness version, this package's version and the suite version (`bench-vX.Y`,
bg3bench.suite; CHANGELOG.md: results cite them). The record's pins carry the versions the run played with
(pins.version_pins); the arguments override them, and the installed ones fill in what neither gives.
"""

from __future__ import annotations

import json
from pathlib import Path

from bg3bench import __version__, suite

SCHEMA = 1
FACTS_SCHEMA = 1
GRADER_VERSION = "1"


def installed_version() -> str:
    """bg3bench's version (pyproject.toml: bg3bench.__version__)."""
    return __version__


def run_id(series: str, run: int) -> str:
    return f"{series}-r{run:03d}"


def from_series(
    series: dict,
    record: dict,
    *,
    bench_tag: str | None = None,
    condition: str | None = None,
    seed: int | None = None,
    timing: str | None = None,
    prompt: dict | None = None,
    tools: dict | None = None,
    price_sheet: dict | None = None,
    provider: str | None = None,
    client_version: str | None = None,
    grader_version: str = GRADER_VERSION,
    harness_version: str | None = None,
    bench_version: str | None = None,
) -> dict:
    """series.json + one runs.jsonl record -> manifest v1. The versions: the arguments, else the record's pins
    (`versions`), else (bench and suite only) the installed bg3bench's; the harness stays null when neither says."""
    if record.get("series") != series["series"]:
        raise ValueError(f"run record of series {record.get('series')!r}, not {series['series']!r}")
    scenario = series.get("scenario") or {}
    if scenario.get("name") is None:
        raise ValueError(f"series {series['series']!r} has no scenario")
    caps = record.get("caps") or series.get("caps") or {}
    return {
        "schema": SCHEMA,
        "run_id": run_id(series["series"], record["run"]),
        "created": record["started"],
        "bench_tag": bench_tag,
        "versions": versions(record, harness_version, bench_version),
        "series": series["series"],
        "run": record["run"],
        "scenario": {
            "name": scenario["name"],
            "digest": scenario.get("digest"),
            "start_save_sha1": scenario.get("start_save_sha1"),
            "timing": timing,
        },
        "seed": seed,
        "condition": condition,
        "client": {"name": record.get("client") or series["client"], "version": client_version},
        "model": {"slug": record["model"], "provider_requested": provider, "reasoning_effort": record.get("effort")},
        "caps": {k: caps.get(k) for k in ("run_hours", "max_cost", "max_tokens")},
        "pins": record.get("pins") or series.get("pins") or {},
        "prompt": prompt,
        "tools": tools,
        "grader": {"version": grader_version},
        "facts_schema": FACTS_SCHEMA,
        "price_sheet": price_sheet,
        "sandbox": series.get("sandbox"),
        "judge": {"model": judge} if (judge := judge_model(series, record)) else None,
    }


def versions(record: dict, harness_version: str | None = None, bench_version: str | None = None) -> dict:
    """The manifest's versions: {harness, bench, suite (bench's bench-vX.Y)}."""
    pinned = (record.get("pins") or {}).get("versions") or {}
    bench = bench_version or pinned.get("bench") or installed_version()
    return {"harness": harness_version or pinned.get("harness"), "bench": bench, "suite": suite.suite_version(bench)}


def judge_model(series: dict, record: dict) -> str | None:
    """The judge that scored the run: its persona's judge_model, else the series' frozen one."""
    return (record.get("persona") or {}).get("judge_model") or series.get("judge_model")


def write(manifest: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a manifest v{SCHEMA} (schema={data.get('schema')!r})")
    return data
