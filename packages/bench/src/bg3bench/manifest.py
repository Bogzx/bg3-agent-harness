"""Run manifests (schema/manifest.v1.schema.json): what a run was set up with.

`from_series` builds one from a series' `series.json` and one `runs.jsonl` record, the files the runner writes today
(runner/runs.py). What those files don't record yet (prompt hash and parts, tool allowlist, price sheet, provider,
client version, condition, the dice seed, the scenario's timing and the harness version) is passed in, or stays null.
Every manifest cites the harness version and this package's version (CHANGELOG.md: results cite both).
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

SCHEMA = 1
FACTS_SCHEMA = 1
GRADER_VERSION = "1"


def installed_version() -> str:
    """bg3bench's installed version (pyproject.toml); raises PackageNotFoundError when it is not installed."""
    return metadata.version("bg3bench")


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
    """series.json + one runs.jsonl record -> manifest v1. `bench_version` defaults to the installed bg3bench's."""
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
        "versions": {"harness": harness_version, "bench": bench_version or installed_version()},
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
    }


def write(manifest: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a manifest v{SCHEMA} (schema={data.get('schema')!r})")
    return data
