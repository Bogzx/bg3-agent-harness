# pyright: basic
"""A benchmark series' runs and the settings it freezes (moved from runner/runs.py; runs.py re-exports them).

A series is `play.py run --until M --runs N` (or `--scenario`): runs named run_name(base, k), one runs.jsonl record per
finished run, and series.json freezing the caps, the persona judge's model, the client and the sandbox from its
first run. A later run with another value is refused unless the operator passes the matching --allow-*-change flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_name(base: str, k: int) -> str:
    """Run k of the series `base`: run 1 is the playthrough itself, run k > 1 is `<base>-r<k>`."""
    return base if k <= 1 else f"{base}-r{k}"


def series_records(path: Path, base: str) -> list[dict[str, Any]]:
    """The series' runs.jsonl records (runtime/sessions/<base>/runs.jsonl), in file order."""
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("series", r.get("playthrough")) == base:
            out.append(r)
    return out


def same_record(a: dict, b: dict) -> bool:
    """The same run: same playthrough, run number and start time (a record's identity in runs.jsonl)."""
    return all(a.get(k) == b.get(k) for k in ("playthrough", "run", "started"))


def update_record(path: Path, record: dict, fields: dict) -> bool:
    """Merge fields into the runs.jsonl line of `record` (same_record), in place: the other lines stay byte for
    byte, the file is replaced atomically. False when the record isn't there or the file can't be written."""
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError:
        return False
    hit = False
    for i, ln in enumerate(lines):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and not hit and same_record(r, record):
            lines[i] = json.dumps({**r, **fields}) + "\n"
            hit = True
    if not hit:
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text("".join(lines))
        tmp.replace(path)
    except OSError:
        return False
    return True


def next_run_index(records: list[dict[str, Any]]) -> int:
    """The run to play next: one after the highest recorded run (a record means that run is over, any outcome)."""
    return max([int(r.get("run") or 1) for r in records], default=0) + 1


CAP_KEYS = ("run_hours", "max_cost", "max_tokens")  # --run-hours, --max-cost USD, --max-tokens N


def series_caps(frozen: dict | None, cli: dict, allow_change: bool) -> tuple[dict, str | None, list[str]]:
    """(caps for this run, error, changes): series.json's caps rule a series; a CLI cap left out takes the frozen
    one, a different CLI value is an error unless allow_change (then it is used and listed). No frozen caps (the
    first run, or a series from before caps): the CLI's."""
    if frozen is None:
        return {k: cli.get(k) for k in CAP_KEYS}, None, []
    caps, changes = {}, []
    for k in CAP_KEYS:
        want, have = cli.get(k), frozen.get(k)
        if want is None or want == have:
            caps[k] = have
        else:
            changes.append(f"{k}: {have} → {want}")
            caps[k] = want
    if changes and not allow_change:
        return (
            caps,
            (
                "the series' caps are fixed in series.json (" + "; ".join(changes) + "): drop the flag, or "
                "pass --allow-cap-change to record the change"
            ),
            changes,
        )
    return caps, None, changes


def series_judge_model(
    frozen: str | None, wanted: str | None, default: str, allow_change: bool
) -> tuple[str, str | None, str | None]:
    """(judge model for this run, error, change): like series_caps for the persona judge. wanted: --judge-model or
    $BG3AI_JUDGE_MODEL (None: neither set, the frozen one or the default). Scores by another judge don't compare."""
    if frozen is None or wanted is None or wanted == frozen:
        return frozen or wanted or default, None, None
    change = f"judge_model: {frozen} → {wanted}"
    if not allow_change:
        return (
            wanted,
            (
                f"the series' judge model is fixed in series.json ({change}): drop --judge-model / "
                "BG3AI_JUDGE_MODEL, or pass --allow-judge-change to record the change"
            ),
            change,
        )
    return wanted, None, change


def series_client(
    frozen: str | None, wanted: str | None, default: str, allow_change: bool
) -> tuple[str, str | None, str | None]:
    """(client for this run, error, change): like series_judge_model for the CLI that plays (--client). wanted:
    --client (None: not given, the frozen one or the default). Runs by another CLI don't compare."""
    if frozen is None or wanted is None or wanted == frozen:
        return frozen or wanted or default, None, None
    change = f"client: {frozen} → {wanted}"
    if not allow_change:
        return (
            wanted,
            (
                f"the series' client is fixed in series.json ({change}): drop --client, or pass "
                "--allow-client-change to record the change"
            ),
            change,
        )
    return wanted, None, change


def sandbox_mode(mode: str | None, unfirewalled: bool = False) -> dict:
    """The sandbox setting a series freezes (series.json "sandbox", "sandbox_unfirewalled"): --sandbox and
    --sandbox-unfirewalled (only meaningful with container)."""
    mode = mode or "none"
    return {"sandbox": mode, "sandbox_unfirewalled": bool(unfirewalled) and mode == "container"}


def series_sandbox(meta: dict, wanted: dict, allow_change: bool) -> tuple[str | None, str | None]:
    """(error, change) for the sandbox like series_client, except that a left-out --sandbox is "none" (never the
    frozen container: that needs the firewall and Docker, so the operator says it). meta: series.json ({} = a new
    series: nothing frozen yet); a series from before the sandbox key played without one ("none")."""
    if not meta:
        return None, None
    frozen = sandbox_mode(meta.get("sandbox"), meta.get("sandbox_unfirewalled", False))
    if frozen == wanted:
        return None, None
    fmt = lambda m: m["sandbox"] + (" (unfirewalled)" if m["sandbox_unfirewalled"] else "")
    change = f"sandbox: {fmt(frozen)} → {fmt(wanted)}"
    if not allow_change:
        flags = "--sandbox " + frozen["sandbox"] + (" --sandbox-unfirewalled" if frozen["sandbox_unfirewalled"] else "")
        return (
            f"the series' sandbox is fixed in series.json ({change}): run it with {flags}, or pass "
            "--allow-sandbox-change to record the change"
        ), change
    return None, change
