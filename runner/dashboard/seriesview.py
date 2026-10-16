"""The benchmark series view (/series, /api/series?playthrough=BASE): one row per runs.jsonl record of a
`play.py run --until M --runs N` series, the series' spread (the lines `play.py runs` prints), its frozen settings
(series.json) and a live row for the run in progress (runner.status.json + that playthrough's session files).

Read-only: it reads runtime/sessions/<base>/{runs.jsonl,series.json} and the running playthrough's files, through
runner/runs.py and runner/model_time.py (the same functions that write the records), so the page and the records
never disagree on a definition.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402

sys.path.insert(0, str(HERE.parent))   # runner/: runs.py, model_time.py
import model_time as MT  # noqa: E402
import runs as R  # noqa: E402

TOP_TOOLS = 3
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")   # a series name from the query string (no paths)
# The live-feel goals the page colours by (bridge/src/bg3ai/metrics.TARGETS; idle median: the explore goal)
TARGETS = {"idle_median_s": 2.0, "gaps_over_10s": 0, "read_rt_median_ms": 100.0}
RESULT_TOK_BASELINE = {"series": "naut", "runs": R.RESULT_TOK_BASELINE, "total": R.RESULT_TOK_BASELINE_TOTAL,
                       "per_result": R.RESULT_TOK_BASELINE_PER_RESULT,
                       "better": R.RESULT_TOK_BETTER, "worse": R.RESULT_TOK_WORSE}
LIVE_STALE_S = 180   # a status file with a dead runner_pid and no heartbeat for this long: no live row


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
    view = {"wall_s": _r(wall), "harness_only_s": _r(sp.get("harness_only_s")),
            "model_only_s": _r(sp.get("model_only_s")), "overlap_s": _r(sp.get("overlap_s")),
            "other_s": _r(sp.get("other_s")), "harness_s": _r(mt.get("harness_s")), "model_s": _r(mt.get("model_s")),
            "adjusted_model_s": _r(mt.get("adjusted_model_s")),
            "top_tools": [{"tool": k, "n": v.get("n"), "s": _r(v.get("s"))} for k, v in tools[:TOP_TOOLS]]}
    if wall:
        view["harness_pct"] = round(100 * (sp.get("harness_only_s") or 0) / wall)
        view["model_pct"] = round(100 * (sp.get("model_only_s") or 0) / wall)
        view["overlap_pct"] = round(100 * (sp.get("overlap_s") or 0) / wall)
    return view


def latency_view(lat: dict | None) -> dict | None:
    if not lat:
        return None
    return {"idle_median_s": _r(lat.get("idle_median_s"), 2), "idle_p90_s": _r(lat.get("idle_p90_s"), 2),
            "gaps_over_10s": lat.get("gaps_over_10s"), "read_rt_median_ms": _r(lat.get("read_rt_median_ms"))}


_RT_CACHE: dict = {}   # (state dir, started, t) → result_tokens_summary of a record without the field


def result_tokens_view(rt: dict | None) -> dict | None:
    """A run's result tokens (runs.result_tokens_summary) graded against the naut baseline (fewer is better)."""
    if not rt or rt.get("total") is None:
        return None
    return {"total": rt["total"], "n": rt.get("n"), "per_result": rt.get("per_result"), "images": rt.get("images"),
            "source": rt.get("source"),
            "grade_total": R.result_tok_grade(rt["total"], R.RESULT_TOK_BASELINE_TOTAL),
            "grade_per_result": R.result_tok_grade(rt.get("per_result"), R.RESULT_TOK_BASELINE_PER_RESULT)}


def record_result_tokens(rec: dict, runtime: Path | None) -> dict | None:
    """The record's result_tokens; a record written before the field: computed read-only from its playthrough's
    files over the run's window (as `play.py runs --backfill` would write it), cached per record."""
    rt = R.record_result_tokens(rec)
    if rt is not None or runtime is None or "result_tokens" in rec:
        return rt
    pt, started, end = rec.get("playthrough"), rec.get("started"), rec.get("t")
    if not pt or not NAME_RE.match(str(pt)) or not isinstance(started, (int, float)):
        return None
    until = end + R.BACKFILL_SLACK_S if isinstance(end, (int, float)) else None
    key = (str(runtime), pt, started, until)
    if key not in _RT_CACHE:
        _RT_CACHE[key] = R.result_tokens_summary(runtime / "sessions" / pt, started, until)
    return _RT_CACHE[key]


def with_result_tokens(recs: list[dict], runtime: Path) -> list[dict]:
    """The records, older ones given their result_tokens (copies; runs.jsonl is not written) for the spread."""
    return [r if "result_tokens" in r else dict(r, result_tokens=record_result_tokens(r, runtime)) for r in recs]


def failure_view(failures: list[dict] | None) -> dict:
    c = R.class_counts(failures)
    kinds = Counter()
    for f in failures or []:
        kinds[f"{f.get('kind')}({(f.get('class') or '?')[0]})"] += int(f.get("n") or 1)
    return {"harness": c[R.HARNESS], "agent": c[R.AGENT], "unknown": c[R.UNKNOWN],
            "top": [{"kind": k, "n": n} for k, n in kinds.most_common(4)]}


def row(rec: dict, runtime: Path | None = None) -> dict:
    """One run record as the page's row (runtime: where to backfill the result tokens of an older record)."""
    outcome = rec.get("outcome") or ("reached" if rec.get("milestone") else "?")
    ttm = R.record_ttm(rec)
    p = rec.get("persona") or {}
    return {
        "run": rec.get("run") or 1, "playthrough": rec.get("playthrough"), "outcome": outcome, "cap": rec.get("cap"),
        "why": rec.get("why") or "", "clean": bool(rec.get("clean")), "started": rec.get("started"), "t": rec.get("t"),
        "wall_min": rec.get("wall_min"), "to_milestone_min": None if ttm is None else round(ttm / 60, 1),
        "sessions": rec.get("sessions"), "cost_usd": _r(rec.get("cost_usd"), 2),
        "tokens_m": round(R.token_total(rec.get("tokens")) / 1e6, 2) if rec.get("tokens") else None,
        "tokens": rec.get("tokens"), "failures": failure_view(rec.get("failures")),
        "persona": {"score": R.record_persona(rec), "cell": R.persona_cell(rec), "status": p.get("status"),
                    "summary": p.get("summary"), "judge_model": p.get("judge_model")} if p else None,
        "latency": latency_view(rec.get("latency")), "model_time": model_time_view(rec.get("model_time")),
        "result_tokens": result_tokens_view(record_result_tokens(rec, runtime)),
        "sandbox": rec.get("sandbox") or "none", "sandbox_unfirewalled": bool(rec.get("sandbox_unfirewalled")),
        "client": rec.get("client") or "claude", "model": rec.get("model"), "effort": rec.get("effort"),
        "story_points": (rec.get("story") or {}).get("points"), "deaths": rec.get("deaths"),
        "changes": [x for x in (rec.get("client_change"), rec.get("sandbox_change"), rec.get("judge_change"))
                    if x] + list(rec.get("version_drift") or []) + list(rec.get("cap_changes") or []),
    }


def spread(records: list[dict]) -> dict:
    """The series' spread: the streak line of `play.py runs`, min/max/mean/stddev per metric, and both texts."""
    reached = sum(1 for r in records if (r.get("outcome") or ("reached" if r.get("milestone") else "")) == "reached")
    clean = sum(1 for r in records if r.get("clean"))
    metrics = []
    for label, get, fmt in R.SUMMARY_METRICS:
        st = R.stats([get(r) for r in records])
        metrics.append({"metric": label, **({k: (st[k] if k == "n" else float(fmt.format(st[k])))
                                             for k in ("n", "min", "max", "mean", "stddev")} if st else {"n": 0})})
    table = R.runs_table(records) if records else ""
    return {"n": len(records), "reached": reached, "clean": clean, "clean_streak": R.clean_streak(records),
            "best_streak": R.best_streak(records),
            "line": table.splitlines()[-1] if table else "no runs recorded yet",
            "metrics": metrics, "summary_text": R.series_summary(records) if records else ""}


def _run_started(status: dict, state_dir: Path) -> float | None:
    if status.get("run_started"):
        return float(status["run_started"])
    try:
        run = json.loads((state_dir / "runner.json").read_text()).get("run") or {}
    except (OSError, ValueError):
        return None
    return run.get("started")


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
        if any(e.kind == "result" for e in evs):
            continue   # finished: in claude_totals
        running += 1
        for e in evs:
            meter.on_event(e)
    return {"cost_usd": round(meter.cost, 2), "tokens_m": round(meter.tokens / 1e6, 2), "running_streams": running,
            "estimated": bool(meter.live)}


def live_row(runtime: Path, base: str, status: dict, records: list[dict], now: float, latency_live=None,
             pid_alive=_pid_alive) -> dict | None:
    """The run in progress of series `base` (runner.status.json names it), or None. Cost and tokens are the
    finished play processes' (stream result events); latency is the run's finished sessions (Sxxx.latency.json)
    plus, from latency_live (bridge metrics.live_view of the running session), the current session's."""
    pt = status.get("playthrough")
    if not pt or (status.get("series") or (pt if pt == base else None)) != base:
        return None
    k = status.get("run_index") or 1
    if any((r.get("run") or 1) == k for r in records):
        return None   # that run is recorded: the runner is between runs (or stopped after it)
    alive = pid_alive(status.get("runner_pid")) if status.get("runner_pid") else False
    beat = status.get("heartbeat") or status.get("updated") or 0
    if not alive and now - beat > LIVE_STALE_S:
        return None
    d = runtime / "sessions" / pt
    started = _run_started(status, d)
    if not started:
        return None
    usage = live_usage(d, started)
    story = R.story_gained(d, started)
    try:
        hist = json.loads((d / "runner.json").read_text()).get("history") or []
    except (OSError, ValueError):
        hist = []
    done = [h for h in hist if (h.get("started") or 0) >= started and h.get("kind", "play") == "play"]
    cur_start, ended = status.get("started") or 0, status.get("session_ended") or 0
    in_session = bool(status.get("session")) and ended < cur_start   # a session is playing now
    in_session = in_session and not any(h.get("session") == status.get("session") and
                                        abs((h.get("started") or 0) - cur_start) < 30 for h in done)
    live_lat = None
    if latency_live:
        live_lat = {"idle_median_s": _r(latency_live.get("idle_median_s"), 2),
                    "idle_p90_s": _r(latency_live.get("idle_p90_s"), 2),
                    "gaps_over_10s": (latency_live.get("long_gaps") or {}).get("total")
                    if isinstance(latency_live.get("long_gaps"), dict) else latency_live.get("long_gaps"),
                    "read_rt_median_ms": _r(latency_live.get("rt_read_median_ms")),
                    "current_idle_s": latency_live.get("current_idle_s")}
    return {
        "run": k, "playthrough": pt, "outcome": "running" if alive else "runner gone?", "live": True,
        "runner_alive": alive, "session": status.get("session"), "started": started,
        "elapsed_min": round((now - started) / 60, 1), "sessions": len(done) + in_session, "in_session": in_session,
        "cost_usd": usage["cost_usd"], "tokens_m": usage["tokens_m"], "usage_estimated": usage["estimated"],
        "story_points": story["points"],
        "latency": latency_view(R.latency_summary(d, started)), "latency_session": live_lat,
        "result_tokens": result_tokens_view(R.result_tokens_summary(d, started)),   # its finished sessions
        "model_time": model_time_view(MT.model_time(d, since=started, until=now)),
        "alert": status.get("alert"), "sandbox": status.get("sandbox"),
        "run_end": status.get("run_end"),
    }


def series_payload(runtime: Path, base: str | None, status: dict, now: float | None = None,
                   latency_live=None, pid_alive=_pid_alive) -> dict:
    """/api/series: base None → the running series, else the newest runs.jsonl."""
    now = time.time() if now is None else now
    available = list_series(runtime)
    if base and (not NAME_RE.match(base) or ".." in base):
        return {"now": now, "series": None, "error": f"not a series name: {base[:80]!r}", "available": available,
                "rows": [], "live": None, "spread": spread([]), "targets": TARGETS}
    if not base:
        base = status.get("series") or (available[0]["name"] if available else None)
    if not base:
        return {"now": now, "series": None, "available": available, "rows": [], "live": None,
                "spread": spread([]), "targets": TARGETS}
    recs = R.series_records(runtime / "sessions" / base / "runs.jsonl", base)
    try:
        meta = json.loads((runtime / "sessions" / base / "series.json").read_text())
    except (OSError, ValueError):
        meta = {}
    frozen = {k: meta.get(k) for k in ("runs", "until", "client", "judge_model", "caps", "created")}
    frozen["client"] = frozen["client"] or ("claude" if meta else None)   # a series from before --client
    frozen.update(R.sandbox_mode(meta.get("sandbox"), meta.get("sandbox_unfirewalled", False)) if meta else {})
    frozen["scenario"] = (meta.get("scenario") or {}).get("name") if isinstance(meta.get("scenario"), dict) else None
    lat = latency_live() if callable(latency_live) and status.get("series") == base else \
        (None if callable(latency_live) else latency_live)
    return {"now": now, "series": base, "available": available, "frozen": frozen if meta else None,
            "rows": [row(r, runtime) for r in recs], "spread": spread(with_result_tokens(recs, runtime)),
            "live": live_row(runtime, base, status, recs, now, lat, pid_alive), "targets": TARGETS,
            "result_tok_baseline": RESULT_TOK_BASELINE}
