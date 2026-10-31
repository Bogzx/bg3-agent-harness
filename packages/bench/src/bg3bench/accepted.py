# pyright: basic
"""Which run of a matrix job counts: the one its queue accepted.

A matrix job's run is the valid end record of its LAST attempt in runtime/matrix/<name>/queue.jsonl (matrix.py): a
rerun supersedes every earlier attempt, and an attempt the queue ended invalid, or closed as interrupted (the matrix
runner died: its end record names no run), is no job's run. A job whose last attempt hasn't ended has none yet.

Validity is decided once, when a run ends: the run's result.json and the queue's end record. No tool writes validity
into a recorded result.json afterwards (bg3bench.regrade only prints what today's classifier would say), so a
classifier change never makes a superseded attempt count, and this module reads the queue, not the run dir.

`refusal(run_dir, matrices)` is why a run can't be packed: its run_id is in some queue but isn't its job's accepted
one, or it is a run of a series some queue's runs belong to that the queue never named (an interrupted attempt).
Auto-submit packs only `accepted_run_ids`; the panel's prepare and the pack CLI ask `refusal`. A run outside every
matrix passes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

QUEUE = "queue.jsonl"
RUN_NO = re.compile(r"-r\d{3,}$")  # manifest.run_id: <series>-r<run:03d>

Json = dict[str, Any]


def _records(matrix_dir: Path) -> list[Json]:
    try:
        lines = (matrix_dir / QUEUE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a line being written
        if isinstance(rec, dict):
            out.append(rec)
    return out


def attempts(matrix_dir: Path) -> dict[str, list[Json]]:
    """job id -> its attempts in order, {"attempt", "result" (None until it ended)}, as matrix.Matrix.state folds them."""
    jobs: dict[str, list[Json]] = {}
    for rec in _records(matrix_dir):
        job = rec.get("job")
        if not isinstance(job, str):
            continue
        tried = jobs.setdefault(job, [])
        if rec.get("type") == "start":
            tried.append({"attempt": rec.get("attempt"), "result": None})
        elif rec.get("type") == "end" and tried and tried[-1]["attempt"] == rec.get("attempt"):
            tried[-1]["result"] = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    return jobs


def _accepted(tried: list[Json]) -> str | None:
    res = tried[-1]["result"] if tried else None
    run_id = (res or {}).get("run_id")
    return run_id if (res or {}).get("valid") is True and isinstance(run_id, str) else None


def accepted_run_ids(matrix_dir: Path) -> list[str]:
    """Each job's accepted run_id (the valid end of its last attempt), in queue order of the jobs."""
    return [r for tried in attempts(matrix_dir).values() if (r := _accepted(tried)) is not None]


def refusal(run_dir: Path, matrices: Path) -> str | None:
    """Why the run in run_dir isn't its matrix job's run, or None (accepted, or outside every matrix under
    `matrices`, runtime/matrix)."""
    try:
        man = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # no manifest: pack refuses the run itself
    run_id, series = man.get("run_id"), man.get("series")
    if not isinstance(run_id, str):
        return None
    for queue in sorted(Path(matrices).glob(f"*/{QUEUE}")):
        name = queue.parent.name
        jobs = attempts(queue.parent)
        named: set[str] = set()
        for job, tried in jobs.items():
            ids = [(a["attempt"], (a["result"] or {}).get("run_id")) for a in tried]
            named.update(r for _, r in ids if isinstance(r, str))
            mine = next((n for n, r in ids if r == run_id), None)
            if mine is None:
                continue
            ok = _accepted(tried)
            if ok == run_id:
                return None
            return (
                f"{run_id} is attempt {mine} of matrix {name} job {job}, not the run its queue accepted "
                f"({ok or 'none'}): a superseded attempt never counts"
            )
        if isinstance(series, str) and series in {RUN_NO.sub("", r) for r in named}:
            return f"{run_id} is a run of matrix {name}'s series {series} that its queue never named (interrupted)"
    return None
