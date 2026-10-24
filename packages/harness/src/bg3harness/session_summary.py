# pyright: basic
"""A play session's summaries at its end, for play.py's Runner and the app (bg3play.adapters.session_files):

    <base>.latency.json  + latency_history.jsonl   bridge records (<base>.metrics.jsonl) + stream timing (.turns.jsonl)
    <base>.progress.json + progress_history.jsonl  the story poll's <base>.progress.jsonl
    <base>.triage.md                               failed calls, long gaps, clicks, stuck stretches

base is the session's path without the extension (runtime/sessions/<pt>/S012); the history files go in hist_dir.
Nothing here raises: a summary that can't be written is logged and returns None.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from bg3ai import metrics as latency
from bg3ai import progress as story

from bg3harness import triage

Log = Callable[[str], None]


def write_latency(
    session: str, base: Path, start: float, end: float, hist_dir: Path, log: Log
) -> dict[str, Any] | None:
    """Sxxx.latency.json + a line in latency_history.jsonl (bridge records + stream timing)."""
    try:
        summary = latency.summarize_files(Path(f"{base}.metrics.jsonl"), Path(f"{base}.turns.jsonl"), start, end)
        latency.write_summary(summary, Path(f"{base}.latency.json"), hist_dir / "latency_history.jsonl", session)
    except Exception as e:
        log(f"{session}: latency summary failed: {e}")
        return None
    t = summary["targets"]
    verdict = " ".join(f"{k}={'PASS' if v['pass'] else 'FAIL' if v['pass'] is False else 'n/a'}" for k, v in t.items())
    log(
        f"{session}: latency idle median {summary['idle_s']['all'].get('median')}s, "
        f"{summary['long_gaps']['total']} gaps >10s, think median {summary['think_s'].get('median')}s "
        f"(API {(summary.get('api') or {}).get('api_s_per_turn')}s/turn, "
        f"{(summary.get('api') or {}).get('ms_per_out_tok')} ms/out-tok), "
        f"apm {summary['apm']}, calls/min {(summary.get('rates') or {}).get('calls_pm')} "
        f"(reads {(summary.get('rates') or {}).get('reads_pm')}, "
        f"actions {(summary.get('rates') or {}).get('actions_pm')}), "
        f"busy {summary['busy_pct']}%, pipelined {summary.get('pipelining_pct')}%, "
        f"think overlap {summary.get('think_overlap_pct')}%, loading {summary.get('loading_s')}s (not idle), "
        f"wait_for noop {(summary.get('wait_for_noop') or {}).get('n')}/"
        f"{(summary.get('wait_for_noop') or {}).get('of')}, "
        + (
            f"wrap-up {summary['wrapup_s']}s (not counted)"
            if summary.get("wrapup_s") is not None
            else "no wrap-up phase"
        )
        + f" | {verdict}"
    )
    return summary


def write_progress(
    session: str, base: Path, start: float, end: float, hist_dir: Path, log: Log
) -> dict[str, Any] | None:
    """Sxxx.progress.json + a line in progress_history.jsonl."""
    try:
        summary = story.summarize(story.load(Path(f"{base}.progress.jsonl")), start, end)
        story.write_summary(summary, Path(f"{base}.progress.json"), hist_dir / "progress_history.jsonl", session)
    except Exception as e:
        log(f"{session}: progress summary failed: {e}")
        return None
    log(
        f"{session}: story progress {summary['points']} pts ({summary['per_hour']}/h), {summary['events']} events, "
        f"longest stretch without progress {summary['longest_no_progress_min']} min, "
        f"{summary['stuck_episodes']} stuck episode(s)"
    )
    return summary


def write_triage(session: str, base: Path, log: Log) -> Path | None:
    """Sxxx.triage.md: failed calls by tool + error, long gaps, ToolSearch, clicks, stuck stretches, summary."""
    try:
        path = triage.write(base)
    except Exception as e:
        log(f"{session}: triage failed: {e}")
        return None
    log(f"{session}: triage {path}")
    return path
