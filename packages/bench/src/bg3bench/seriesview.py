# pyright: basic
# (basic, not strict: it composes records.py and series.py, which are basic and take untyped run records)
"""The benchmark series view (/series, /api/series?playthrough=BASE): one row per runs.jsonl record of a
`play.py run --until M --runs N` series, the series' spread (the lines `play.py runs` prints), its frozen settings
(series.json) and a live row for the run in progress (runner.status.json + that playthrough's session files).

Read-only. The rows and the spread come from the records alone (records.py, series.py: the functions behind
`play.py runs`). What needs the harness's analyzers over session files (the live row's cost, latency, story and model
time, the result tokens of a record written before that field) comes through a `Live` passed in; runner/dashboard/
seriesview.py passes the runner's (runs.py, model_time.py), so the page and the records never disagree on a
definition. Without one the page shows the recorded rows only.

    python -m bg3bench.seriesview --runtime runtime [--port 8766]    # records only
    python3 runner/dashboard/seriesview.py [--port 8766]             # with the live row
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from bg3bench import records as R
from bg3bench import series as S

TOP_TOOLS = 3
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")  # a series name from the query string (no paths)
# The live-feel goals the page colours by (bridge/src/bg3ai/metrics.TARGETS; idle median: the explore goal)
TARGETS = {"idle_median_s": 2.0, "gaps_over_10s": 0, "read_rt_median_ms": 100.0}
RESULT_TOK_BASELINE = {
    "series": "naut",
    "runs": R.RESULT_TOK_BASELINE,
    "total": R.RESULT_TOK_BASELINE_TOTAL,
    "per_result": R.RESULT_TOK_BASELINE_PER_RESULT,
    "better": R.RESULT_TOK_BETTER,
    "worse": R.RESULT_TOK_WORSE,
}
LIVE_STALE_S = 180  # a status file with a dead runner_pid and no heartbeat for this long: no live row
BACKFILL_SLACK_S = 5.0  # runs.BACKFILL_SLACK_S: a run's last files are written a little before its record


class Live(Protocol):
    """The harness's analyzers over a playthrough's session files (runner/runs.py, bg3harness/model_time.py)."""

    def usage(self, d: Path, started: float) -> dict:
        """{cost_usd, tokens_m, running_streams, estimated}: finished processes plus the streams still running."""
        ...

    def story(self, d: Path, started: float) -> dict: ...

    def latency(self, d: Path, started: float) -> dict | None: ...

    def result_tokens(self, d: Path, started: float, until: float | None) -> dict | None: ...

    def model_time(self, d: Path, since: float, until: float) -> dict | None: ...


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def list_series(runtime: Path) -> list[dict]:
    """Every runtime/sessions/<base>/runs.jsonl (a series or a playthrough's --until runs), newest first."""
    out = []
    try:
        dirs = list((runtime / "sessions").iterdir())
    except OSError:
        return []
    for d in dirs:
        f = d / "runs.jsonl"
        try:
            if f.is_file():
                out.append({"name": d.name, "mtime": f.stat().st_mtime, "series": (d / "series.json").exists()})
        except OSError:
            continue
    return sorted(out, key=lambda x: -x["mtime"])


def model_time_view(mt: dict | None) -> dict | None:
    """The harness / model / overlap split of a model_time dict (run record or live) and its top harness tools."""
    if not mt or not mt.get("sessions"):
        return None
    sp = mt.get("split") or {}
    tools = sorted((mt.get("harness_by_tool") or {}).items(), key=lambda kv: -(kv[1].get("s") or 0))
    wall = mt.get("wall_s") or 0
    view = {
        "wall_s": _r(wall),
        "harness_only_s": _r(sp.get("harness_only_s")),
        "model_only_s": _r(sp.get("model_only_s")),
        "overlap_s": _r(sp.get("overlap_s")),
        "other_s": _r(sp.get("other_s")),
        "harness_s": _r(mt.get("harness_s")),
        "model_s": _r(mt.get("model_s")),
        "adjusted_model_s": _r(mt.get("adjusted_model_s")),
        "adj_model_s": _r(mt.get(R.HEADLINE)),
        "api_floor_s": (mt.get("api_floor") or {}).get("p10_s"),
        "top_tools": [{"tool": k, "n": v.get("n"), "s": _r(v.get("s"))} for k, v in tools[:TOP_TOOLS]],
    }
    if wall:
        view["harness_pct"] = round(100 * (sp.get("harness_only_s") or 0) / wall)
        view["model_pct"] = round(100 * (sp.get("model_only_s") or 0) / wall)
        view["overlap_pct"] = round(100 * (sp.get("overlap_s") or 0) / wall)
    return view


def latency_view(lat: dict | None) -> dict | None:
    if not lat:
        return None
    return {
        "idle_median_s": _r(lat.get("idle_median_s"), 2),
        "idle_p90_s": _r(lat.get("idle_p90_s"), 2),
        "gaps_over_10s": lat.get("gaps_over_10s"),
        "read_rt_median_ms": _r(lat.get("read_rt_median_ms")),
    }


def result_tokens_view(rt: dict | None) -> dict | None:
    """A run's result tokens (runs.result_tokens_summary) graded against the naut baseline (fewer is better)."""
    if not rt or rt.get("total") is None:
        return None
    return {
        "total": rt["total"],
        "n": rt.get("n"),
        "per_result": rt.get("per_result"),
        "images": rt.get("images"),
        "source": rt.get("source"),
        "grade_total": R.result_tok_grade(rt["total"], R.RESULT_TOK_BASELINE_TOTAL),
        "grade_per_result": R.result_tok_grade(rt.get("per_result"), R.RESULT_TOK_BASELINE_PER_RESULT),
    }


def record_result_tokens(rec: dict, runtime: Path | None, live: Live | None = None) -> dict | None:
    """The record's result_tokens; a record written before the field: computed read-only by `live` from its
    playthrough's files over the run's window (as `play.py runs --backfill` would write it)."""
    rt = R.record_result_tokens(rec)
    if rt is not None or runtime is None or live is None or "result_tokens" in rec:
        return rt
    pt, started, end = rec.get("playthrough"), rec.get("started"), rec.get("t")
    if not pt or not NAME_RE.match(str(pt)) or not isinstance(started, int | float):
        return None
    until = end + BACKFILL_SLACK_S if isinstance(end, int | float) else None
    return live.result_tokens(runtime / "sessions" / pt, started, until)


def with_result_tokens(recs: list[dict], runtime: Path, live: Live | None = None) -> list[dict]:
    """The records, older ones given their result_tokens (copies; runs.jsonl is not written) for the spread."""
    return [r if "result_tokens" in r else dict(r, result_tokens=record_result_tokens(r, runtime, live)) for r in recs]


def failure_view(failures: list[dict] | None) -> dict:
    c = R.class_counts(failures or [])
    kinds: Counter = Counter()
    for f in failures or []:
        kinds[f"{f.get('kind')}({(f.get('class') or '?')[0]})"] += int(f.get("n") or 1)
    return {
        "harness": c[R.HARNESS],
        "agent": c[R.AGENT],
        "unknown": c[R.UNKNOWN],
        "top": [{"kind": k, "n": n} for k, n in kinds.most_common(4)],
    }


def row(rec: dict, runtime: Path | None = None, live: Live | None = None) -> dict:
    """One run record as the page's row (runtime + live: backfill the result tokens of an older record)."""
    outcome = rec.get("outcome") or ("reached" if rec.get("milestone") else "?")
    ttm = R.record_ttm(rec)
    p = rec.get("persona") or {}
    persona = {
        "score": R.record_persona(rec),
        "cell": R.persona_cell(rec),
        "status": p.get("status"),
        "summary": p.get("summary"),
        "judge_model": p.get("judge_model"),
    }
    changes = [x for x in (rec.get("client_change"), rec.get("sandbox_change"), rec.get("judge_change")) if x]
    return {
        "run": rec.get("run") or 1,
        "playthrough": rec.get("playthrough"),
        "outcome": outcome,
        "cap": rec.get("cap"),
        "why": rec.get("why") or "",
        "clean": bool(rec.get("clean")),
        "started": rec.get("started"),
        "t": rec.get("t"),
        "wall_min": rec.get("wall_min"),
        "to_milestone_min": None if ttm is None else round(ttm / 60, 1),
        "sessions": rec.get("sessions"),
        "cost_usd": _r(rec.get("cost_usd"), 2),
        "tokens_m": round(R.token_total(rec.get("tokens")) / 1e6, 2) if rec.get("tokens") else None,
        "tokens": rec.get("tokens"),
        "failures": failure_view(rec.get("failures")),
        "persona": persona if p else None,
        "latency": latency_view(rec.get("latency")),
        "model_time": model_time_view(rec.get("model_time")),
        "result_tokens": result_tokens_view(record_result_tokens(rec, runtime, live)),
        "sandbox": rec.get("sandbox") or "none",
        "sandbox_unfirewalled": bool(rec.get("sandbox_unfirewalled")),
        "client": rec.get("client") or "claude",
        "model": rec.get("model"),
        "effort": rec.get("effort"),
        "story_points": (rec.get("story") or {}).get("points"),
        "deaths": rec.get("deaths"),
        "changes": changes + list(rec.get("version_drift") or []) + list(rec.get("cap_changes") or []),
    }


def spread(records: list[dict]) -> dict:
    """The series' spread: the streak line of `play.py runs`, min/max/mean/stddev per metric, and both texts."""
    reached = sum(1 for r in records if (r.get("outcome") or ("reached" if r.get("milestone") else "")) == "reached")
    clean = sum(1 for r in records if r.get("clean"))
    metrics = []
    for label, get, fmt in R.SUMMARY_METRICS:
        st = R.stats([get(r) for r in records])
        keys = ("n", "min", "max", "mean", "stddev")
        metrics.append({"metric": label, **({k: (st[k] if k == "n" else float(fmt.format(st[k]))) for k in keys}
                                             if st else {"n": 0})})  # fmt: skip
    table = R.runs_table(records) if records else ""
    floors = [(f"run {r.get('run') or 1}", f) for r in records if (f := R.record_api_floor(r)) is not None]
    return {
        "n": len(records),
        "reached": reached,
        "clean": clean,
        "clean_streak": R.clean_streak(records),
        "best_streak": R.best_streak(records),
        "line": table.splitlines()[-1] if table else "no runs recorded yet",
        "metrics": metrics,
        "summary_text": R.series_summary(records) if records else "",
        "floor_note": R.floor_note(floors),
    }


def _run_started(status: dict, state_dir: Path) -> float | None:
    if status.get("run_started"):
        return float(status["run_started"])
    try:
        run = json.loads((state_dir / "runner.json").read_text()).get("run") or {}
    except (OSError, ValueError):
        return None
    return run.get("started")


def live_row(
    runtime: Path,
    base: str,
    status: dict,
    records: list[dict],
    now: float,
    live: Live,
    latency_live: dict | None = None,
    pid_alive: Callable[[Any], bool] = _pid_alive,
) -> dict | None:
    """The run in progress of series `base` (runner.status.json names it), or None. Cost and tokens are the
    finished play processes' plus the running streams' (live.usage); latency is the run's finished sessions plus,
    from latency_live (bridge metrics.live_view of the running session), the current session's."""
    pt = status.get("playthrough")
    if not pt or (status.get("series") or (pt if pt == base else None)) != base:
        return None
    k = status.get("run_index") or 1
    if any((r.get("run") or 1) == k for r in records):
        return None  # that run is recorded: the runner is between runs (or stopped after it)
    alive = pid_alive(status.get("runner_pid")) if status.get("runner_pid") else False
    beat = status.get("heartbeat") or status.get("updated") or 0
    if not alive and now - beat > LIVE_STALE_S:
        return None
    d = runtime / "sessions" / pt
    started = _run_started(status, d)
    if not started:
        return None
    usage = live.usage(d, started)
    story = live.story(d, started)
    try:
        hist = json.loads((d / "runner.json").read_text()).get("history") or []
    except (OSError, ValueError):
        hist = []
    done = [h for h in hist if (h.get("started") or 0) >= started and h.get("kind", "play") == "play"]
    cur_start, ended = status.get("started") or 0, status.get("session_ended") or 0
    in_session = bool(status.get("session")) and ended < cur_start  # a session is playing now
    in_session = in_session and not any(
        h.get("session") == status.get("session") and abs((h.get("started") or 0) - cur_start) < 30 for h in done
    )
    live_lat = None
    if latency_live:
        gaps = latency_live.get("long_gaps")
        live_lat = {
            "idle_median_s": _r(latency_live.get("idle_median_s"), 2),
            "idle_p90_s": _r(latency_live.get("idle_p90_s"), 2),
            "gaps_over_10s": gaps.get("total") if isinstance(gaps, dict) else gaps,
            "read_rt_median_ms": _r(latency_live.get("rt_read_median_ms")),
            "current_idle_s": latency_live.get("current_idle_s"),
        }
    return {
        "run": k,
        "playthrough": pt,
        "outcome": "running" if alive else "runner gone?",
        "live": True,
        "runner_alive": alive,
        "session": status.get("session"),
        "started": started,
        "elapsed_min": round((now - started) / 60, 1),
        "sessions": len(done) + in_session,
        "in_session": in_session,
        "cost_usd": usage["cost_usd"],
        "tokens_m": usage["tokens_m"],
        "usage_estimated": usage["estimated"],
        "story_points": story["points"],
        "latency": latency_view(live.latency(d, started)),
        "latency_session": live_lat,
        "result_tokens": result_tokens_view(live.result_tokens(d, started, None)),  # its finished sessions
        "model_time": model_time_view(live.model_time(d, started, now)),
        "alert": status.get("alert"),
        "sandbox": status.get("sandbox"),
        "run_end": status.get("run_end"),
    }


def series_payload(
    runtime: Path,
    base: str | None,
    status: dict,
    now: float | None = None,
    latency_live: dict | Callable[[], dict | None] | None = None,
    pid_alive: Callable[[Any], bool] = _pid_alive,
    live: Live | None = None,
) -> dict:
    """/api/series: base None → the running series, else the newest runs.jsonl. latency_live: the running
    session's latency (a dict, or a callable asked only when the running series is the one shown)."""
    now = time.time() if now is None else now
    available = list_series(runtime)
    empty = {"now": now, "series": None, "available": available, "rows": [], "live": None, "spread": spread([]),
             "targets": TARGETS}  # fmt: skip
    if base and (not NAME_RE.match(base) or ".." in base):
        return dict(empty, error=f"not a series name: {base[:80]!r}")
    if not base:
        base = status.get("series") or (available[0]["name"] if available else None)
    if not base:
        return empty
    recs = S.series_records(runtime / "sessions" / base / "runs.jsonl", base)
    try:
        meta = json.loads((runtime / "sessions" / base / "series.json").read_text())
    except (OSError, ValueError):
        meta = {}
    frozen = {k: meta.get(k) for k in ("runs", "until", "client", "judge_model", "caps", "created")}
    frozen["client"] = frozen["client"] or ("claude" if meta else None)  # a series from before --client
    frozen.update(S.sandbox_mode(meta.get("sandbox"), meta.get("sandbox_unfirewalled", False)) if meta else {})
    frozen["scenario"] = (meta.get("scenario") or {}).get("name") if isinstance(meta.get("scenario"), dict) else None
    lat: dict | None
    if callable(latency_live):
        lat = latency_live() if live is not None and status.get("series") == base else None
    else:
        lat = latency_live
    return {
        "now": now,
        "series": base,
        "available": available,
        "frozen": frozen if meta else None,
        "rows": [row(r, runtime, live) for r in recs],
        "spread": spread(with_result_tokens(recs, runtime, live)),
        "live": None if live is None else live_row(runtime, base, status, recs, now, live, lat, pid_alive),
        "targets": TARGETS,
        "result_tok_baseline": RESULT_TOK_BASELINE,
    }


# ======================================================================== the page and its server


def page() -> bytes:
    """The series page (site/series.html): fetches /api/series every few seconds."""
    return resources.files("bg3bench.site").joinpath("series.html").read_bytes()


def _read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def make_server(
    runtime: Path,
    host: str = "127.0.0.1",
    port: int = 8766,
    live: Live | None = None,
    latency_live: Callable[[dict], dict | None] | None = None,
) -> ThreadingHTTPServer:
    """A read-only HTTP server: /series (the page) and /api/series[?playthrough=BASE] (series_payload with the
    runner's runtime/runner.status.json). latency_live(status): the running session's latency."""
    html = page()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/series", "/series.html"):
                return self._send(200, html, "text/html; charset=utf-8")
            if url.path == "/api/series":
                q = parse_qs(url.query)
                status = _read_json(runtime / "runner.status.json")
                base = (q.get("playthrough") or q.get("series") or [None])[0]
                lat = partial(latency_live, status) if latency_live else None
                body = series_payload(runtime, base, status, latency_live=lat, live=live)
                return self._send(200, json.dumps(body).encode("utf-8"), "application/json")
            return self._send(404, b"not found", "text/plain")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: Sequence[str] | None = None, live: Live | None = None, latency_live=None) -> int:
    ap = argparse.ArgumentParser(description="Serve the benchmark series view (/series).")
    ap.add_argument("--runtime", type=Path, default=Path(os.environ.get("BG3AI_RUNTIME") or "runtime"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args(argv)
    srv = make_server(a.runtime, a.host, a.port, live, latency_live)
    print(f"series view: http://{a.host}:{srv.server_address[1]}/series ({a.runtime})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
