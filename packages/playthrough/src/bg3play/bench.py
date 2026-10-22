"""A benchmark run's grade: the manifest.json and result.json bg3bench writes into the run's directory.

bg3bench.run_artifacts.write_run_artifacts writes both next to the run's facts.jsonl, in runtime/sessions/<run name>/
(the store's session dir). The app reads the files, never bg3bench: they are versioned contracts with JSON Schemas
(packages/bench/schema/{manifest,result}.v1.schema.json). A file with another schema version, or without a field the
screens read, is reported as an error rather than shown half-read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import read_json

MANIFEST = "manifest.json"
RESULT = "result.json"
SCHEMA = 1
# the fields the screens read (both schemas require them)
MANIFEST_KEYS = ("run_id", "created", "series", "run", "scenario", "client", "model", "caps", "sandbox")
RESULT_KEYS = (
    "run_id",
    "outcome",
    "valid",
    "invalid_reason",
    "passed",
    "t_pass_s",
    "grade",
    "methods",
    "cost_usd",
    "tokens",
    "wall_s",
    "play_s",
    "think_s",
    "luck",
    "reloads",
    "deaths",
    "fairness",
)


def _check(doc: dict[str, Any], name: str, keys: tuple[str, ...]) -> str | None:
    if not doc:
        return f"{name} is missing or not a JSON object"
    if doc.get("schema") != SCHEMA:
        return f"{name} has schema {doc.get('schema')!r}; this app reads schema {SCHEMA}"
    missing = [k for k in keys if k not in doc]
    return f"{name} lacks {', '.join(missing)}" if missing else None


def grade(run_dir: Path) -> dict[str, Any] | None:
    """{"manifest", "result"} as written (result.json's optional `choices` included), {"error"} for a file the app
    can't read, None when the run has neither file (not a benchmark run, or not graded yet)."""
    if not (run_dir / MANIFEST).exists() and not (run_dir / RESULT).exists():
        return None
    man, res = read_json(run_dir / MANIFEST), read_json(run_dir / RESULT)
    error = _check(man, MANIFEST, MANIFEST_KEYS) or _check(res, RESULT, RESULT_KEYS)
    if error is None and man["run_id"] != res["run_id"]:
        error = f"{MANIFEST} is run {man['run_id']!r} but {RESULT} is run {res['run_id']!r}"
    if error is not None:
        return {"error": error}
    return {"manifest": {k: man[k] for k in MANIFEST_KEYS}, "result": res}
