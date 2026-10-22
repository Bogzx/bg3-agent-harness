#!/usr/bin/env python3
"""The benchmark series view, served with the live row: bg3bench.seriesview (the page, the rows from the records)
wired to the runner's analyzers over session files (runner/runs.py, bg3harness.model_time) and the playthrough app's
latency of the running session (bg3play.live), so the page and the records never disagree on a definition.

    python3 runner/dashboard/seriesview.py [--port 8766]     # http://127.0.0.1:8766/series

Read-only: it reads runtime/sessions/<base>/{runs.jsonl,series.json}, runtime/runner.status.json and the running
playthrough's files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))   # runner/: runs.py
sys.path.insert(0, str(REPO / "packages" / "bench" / "src"))
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
sys.path.insert(0, str(REPO / "bridge" / "src"))
import runs as R  # noqa: E402
from bg3bench import seriesview as _core  # noqa: E402
from bg3bench.seriesview import (  # noqa: E402, F401 - re-exports
    LIVE_STALE_S,
    NAME_RE,
    RESULT_TOK_BASELINE,
    TARGETS,
    TOP_TOOLS,
    failure_view,
    latency_view,
    list_series,
    model_time_view,
    result_tokens_view,
    spread,
)
from bg3harness import model_time as MT  # noqa: E402

_RT_CACHE: dict = {}   # (state dir, started, until) → result_tokens_summary of a record without the field


def live_usage(d: Path, started: float) -> dict:
    """The run's cost and tokens so far: its finished processes (runs.claude_totals) plus the streams still
    running (no result event yet), metered as the runner's caps meter them (runs.UsageMeter: live tokens priced at
    the run's cost per token so far)."""
    totals = R.claude_totals(d, started)
    meter = R.UsageMeter(totals.get("cost_usd") or 0, R.token_total(totals.get("tokens")))
    running = 0
    try:
        files = sorted((f for f in d.iterdir() if R.CLAUDE_STREAM_RE.match(f.name) and f.stat().st_mtime >= started),
                       key=lambda f: f.stat().st_mtime)
    except OSError:
        files = []
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        evs = R.stream_events(lines, {}, R.stream_client(lines))
        if any(e.kind == "result" for e in evs) or R.stopped_usage(f.with_name(f.name[:-len(".jsonl")])) is not None:
            continue   # finished (or stopped before its result): in claude_totals
        running += 1
        for e in evs:
            meter.on_event(e)
    return {"cost_usd": round(meter.cost, 2), "tokens_m": round(meter.tokens / 1e6, 2), "running_streams": running,
            "estimated": bool(meter.live)}


class HarnessLive:
    """bg3bench.seriesview.Live over the runner's analyzers."""

    def usage(self, d: Path, started: float) -> dict:
        return live_usage(d, started)

    def story(self, d: Path, started: float) -> dict:
        return R.story_gained(d, started)

    def latency(self, d: Path, started: float) -> dict | None:
        return R.latency_summary(d, started)

    def result_tokens(self, d: Path, started: float, until: float | None) -> dict | None:
        if until is None:   # a run in progress: its finished sessions so far, never cached
            return R.result_tokens_summary(d, started)
        key = (str(d), started, until)
        if key not in _RT_CACHE:
            _RT_CACHE[key] = R.result_tokens_summary(d, started, until)
        return _RT_CACHE[key]

    def model_time(self, d: Path, since: float, until: float) -> dict | None:
        return MT.model_time(d, since=since, until=until)


LIVE = HarnessLive()


def _pid_alive(pid) -> bool:
    return _core._pid_alive(pid)


def record_result_tokens(rec: dict, runtime: Path | None) -> dict | None:
    return _core.record_result_tokens(rec, runtime, LIVE)


def with_result_tokens(recs: list[dict], runtime: Path) -> list[dict]:
    return _core.with_result_tokens(recs, runtime, LIVE)


def row(rec: dict, runtime: Path | None = None) -> dict:
    return _core.row(rec, runtime, LIVE)


def live_row(runtime: Path, base: str, status: dict, records: list[dict], now: float, latency_live=None,
             pid_alive=_pid_alive) -> dict | None:
    return _core.live_row(runtime, base, status, records, now, LIVE, latency_live, pid_alive)


def series_payload(runtime: Path, base: str | None, status: dict, now: float | None = None,
                   latency_live=None, pid_alive=_pid_alive) -> dict:
    """/api/series with the live row: bg3bench.seriesview.series_payload with the runner's analyzers."""
    return _core.series_payload(runtime, base, status, now, latency_live, pid_alive, LIVE)


def session_latency(runtime: Path):
    """The running session's latency (bg3play.live.latency_payload's "live"), or None without the app package."""
    sys.path.insert(0, str(REPO / "packages" / "playthrough" / "src"))
    try:
        from bg3play import live
    except ImportError:
        return None
    return lambda status: live.latency_payload(runtime, status).get("live")


def main(argv: list[str] | None = None) -> int:
    runtime = Path(os.environ.get("BG3AI_RUNTIME") or REPO / "runtime")
    args = list(sys.argv[1:] if argv is None else argv)
    if "--runtime" not in args:
        args = ["--runtime", str(runtime), *args]
        rt = runtime
    else:
        rt = Path(args[args.index("--runtime") + 1])
    return _core.main(args, live=LIVE, latency_live=session_latency(rt))


if __name__ == "__main__":
    sys.exit(main())
