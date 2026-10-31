"""Submit when done: pack and validate each valid run of a matrix the panel started with that box ticked.

`tick` is one pass, called every few seconds by the panel's ticker thread (server.py) and directly by the tests. For
every runtime/panel/<name>.json whose submit_when_done is true it reads runtime/matrix/<name>/queue.jsonl's
accepted runs (accepted.accepted_run_ids: each job's last attempt, when it ended valid; a superseded attempt never):
one (its run_id) that is finished (a run dir with manifest.json and result.json) and has neither a prepared bundle
nor an outcome in the file's "auto" yet is prepared, the same `prepare` the Validate button calls. The
outcome (the bundle hash and the verdict, or the error) goes into "auto", so a run is tried once: a failed one waits
for the user's Validate. Nothing is uploaded: upload stays the user's click on the Results screen.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bg3bench import accepted
from bg3bench.serve import launch
from bg3bench.serve.panel import PanelError


def tick(
    runtime: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    prepared: Callable[[str], bool],
    prepare: Callable[[str], dict[str, Any]],
    now: float,
) -> list[dict[str, Any]]:
    """One pass: prepare every due run (rows: panel.results; prepared(run dir); prepare(run dir) -> its verdict).
    Returns what it did, one {"matrix", "run", ...outcome} per run."""
    by_id = {r["run_id"]: r["dir"] for r in rows if r.get("run_id")}
    done: list[dict[str, Any]] = []
    for f in sorted((runtime / launch.PANEL_DIR).glob("*.json")):
        choices = launch.read_choices(runtime, f.stem)
        if not choices or choices.get("submit_when_done") is not True:
            continue
        auto: dict[str, Any] = choices.setdefault("auto", {})
        for run_id in accepted.accepted_run_ids(runtime / "matrix" / f.stem):
            run = by_id.get(run_id)
            if run is None or run in auto or prepared(run):
                continue
            try:
                p = prepare(run)
                verdict: dict[str, Any] = p.get("verdict") or {}
                outcome: dict[str, Any] = {
                    "t": now,
                    "bundle_sha256": p.get("bundle_sha256"),
                    "accepted": verdict.get("accepted"),
                }
            except (PanelError, OSError, ValueError) as e:
                outcome = {"t": now, "error": str(e)}
            auto[run] = outcome
            launch.write_choices(runtime, choices)
            done.append({"matrix": f.stem, "run": run, **outcome})
    return done
