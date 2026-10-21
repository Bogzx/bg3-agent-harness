# pyright: basic
# (basic, not strict: seriesview takes and returns untyped run records)
"""bg3bench.seriesview: the rows and spread from the records alone, the live row and the result-token backfill
through a fake Live, and the served page. The runner's analyzers behind it: runner/dashboard/test_seriesview.py."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bg3bench import seriesview as V

T0 = 1_800_000_000.0


class FakeLive:
    def __init__(self):
        self.calls = []

    def usage(self, d, started):
        return {"cost_usd": 1.5, "tokens_m": 0.2, "running_streams": 1, "estimated": True}

    def story(self, d, started):
        return {"points": 4}

    def latency(self, d, started):
        return {"idle_median_s": 1.234, "idle_p90_s": 3.0, "gaps_over_10s": 1, "read_rt_median_ms": 80.04}

    def result_tokens(self, d, started, until):
        self.calls.append((d.name, started, until))
        return {"total": 30000, "n": 100, "per_result": 300.0, "images": 0, "source": "stream"}

    def model_time(self, d, since, until):
        return {"sessions": 1, "wall_s": 100.0, "split": {"harness_only_s": 40.0, "model_only_s": 50.0},
                "harness_by_tool": {"move": {"n": 3, "s": 20.0}}}  # fmt: skip


def write_series(rt: Path, name: str = "sb", recs: list[dict] | None = None, meta: dict | None = None) -> Path:
    d = rt / "sessions" / name
    d.mkdir(parents=True)
    recs = recs if recs is not None else [
        {"series": name, "run": 1, "playthrough": name, "outcome": "reached", "clean": True, "started": T0,
         "t": T0 + 600, "cost_usd": 2.0, "tokens": {"input": 1000}},
    ]  # fmt: skip
    (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    if meta is not None:
        (d / "series.json").write_text(json.dumps(meta))
    return d


def test_records_only_rows_and_no_live_row(tmp_path):
    write_series(tmp_path, meta={"series": "sb", "runs": 2, "until": "m", "judge_model": "j"})
    status = {"playthrough": "sb-r2", "series": "sb", "run_index": 2, "runner_pid": 1, "heartbeat": T0}
    never = lambda: pytest.fail("no live row, so no session latency")
    p = V.series_payload(tmp_path, "sb", status, now=T0, latency_live=never, pid_alive=lambda pid: True)
    assert (p["series"], [r["run"] for r in p["rows"]], p["live"]) == ("sb", [1], None)
    assert p["rows"][0]["result_tokens"] is None  # no field and no Live to backfill it
    assert (p["rows"][0]["cost_usd"], p["rows"][0]["tokens_m"]) == (2.0, 0.0)
    assert p["frozen"]["judge_model"] == "j" and p["frozen"]["client"] == "claude"
    assert p["spread"]["line"].startswith("1 run(s), 1 clean")


def test_live_row_and_backfill_through_live(tmp_path):
    d = write_series(tmp_path)
    (tmp_path / "sessions" / "sb-r2").mkdir()
    live = FakeLive()
    status = {"playthrough": "sb-r2", "series": "sb", "run_index": 2, "runner_pid": 1, "heartbeat": T0 + 700,
              "run_started": T0 + 650, "session": "S002", "started": T0 + 660, "session_ended": 0}  # fmt: skip
    asked = []

    def lat():
        asked.append(1)
        return {"idle_median_s": 0.5, "long_gaps": 2, "rt_read_median_ms": 70.0}

    p = V.series_payload(tmp_path, "sb", status, now=T0 + 710, latency_live=lat, pid_alive=lambda pid: True, live=live)
    lv = p["live"]
    assert (lv["run"], lv["outcome"], lv["sessions"], lv["in_session"]) == (2, "running", 1, True)
    assert (lv["cost_usd"], lv["story_points"], lv["elapsed_min"]) == (1.5, 4, 1.0)
    assert lv["latency"] == {"idle_median_s": 1.23, "idle_p90_s": 3.0, "gaps_over_10s": 1, "read_rt_median_ms": 80.0}
    assert lv["latency_session"]["gaps_over_10s"] == 2 and asked == [1]
    as_dict = {"long_gaps": {"total": 3}}  # a dict is used as it is
    p2 = V.series_payload(tmp_path, "sb", status, now=T0 + 710, latency_live=as_dict, pid_alive=bool, live=live)
    assert p2["live"]["latency_session"]["gaps_over_10s"] == 3
    assert lv["model_time"]["top_tools"] == [{"tool": "move", "n": 3, "s": 20.0}]
    assert lv["model_time"]["harness_pct"] == 40
    assert lv["result_tokens"]["grade_total"] == "better"
    # the recorded run's tokens were backfilled over its window (+ the slack), the live run's over its sessions
    assert ("sb", T0, T0 + 600 + V.BACKFILL_SLACK_S) in live.calls and ("sb-r2", T0 + 650, None) in live.calls
    assert p["rows"][0]["result_tokens"]["total"] == 30000
    # another series shown: the running session's latency is not asked for
    write_series(tmp_path, "other")
    assert V.series_payload(tmp_path, "other", status, latency_live=lat, live=live)["live"] is None
    assert asked == [1]
    # a run without a start time has no live row; the runner.json one is used when the status has none
    (d.parent / "sb-r2" / "runner.json").write_text(json.dumps({"run": {"started": T0 + 650}}))
    st = dict(status, run_started=None)
    got = V.live_row(tmp_path, "sb", st, [], T0 + 710, live, pid_alive=lambda pid: True)
    assert got is not None and got["started"] == T0 + 650
    (d.parent / "sb-r2" / "runner.json").write_text("{bad")
    assert V.live_row(tmp_path, "sb", st, [], T0 + 710, live, pid_alive=lambda pid: True) is None


def test_backfill_guards():
    live = FakeLive()
    assert V.record_result_tokens({"result_tokens": None}, Path("."), live) is None
    assert V.record_result_tokens({"playthrough": "../x", "started": T0}, Path("."), live) is None
    assert V.record_result_tokens({"playthrough": "x"}, Path("."), live) is None
    got = V.record_result_tokens({"playthrough": "x", "started": T0}, Path("."), live)
    assert got is not None and got["total"] == 30000
    assert live.calls == [("x", T0, None)]


def test_bad_names_and_empty_runtime(tmp_path):
    for bad in ("../naut", "a/b", ".hidden", "x" * 200):
        p = V.series_payload(tmp_path, bad, {})
        assert p["series"] is None and "not a series name" in p["error"]
    p = V.series_payload(tmp_path, None, {})
    assert (p["series"], p["rows"], p["spread"]["line"]) == (None, [], "no runs recorded yet")
    (tmp_path / "sessions" / "junk").mkdir(parents=True)
    assert V.list_series(tmp_path) == []
    assert V.list_series(tmp_path / "missing") == []
    assert V.model_time_view({"sessions": 0}) is None and V.latency_view(None) is None
    assert V.result_tokens_view({"total": None}) is None


def test_pid_alive():
    import os

    assert V._pid_alive(os.getpid()) and not V._pid_alive("x")


@pytest.fixture
def server(tmp_path):
    write_series(tmp_path, meta={"series": "sb"})
    (tmp_path / "runner.status.json").write_text(json.dumps({"series": "sb"}))
    seen = []
    srv = V.make_server(tmp_path, port=0, live=FakeLive(), latency_live=lambda st: seen.append(st) or None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


def test_served_page_and_api(server):
    base, seen = server
    with urllib.request.urlopen(base + "/series", timeout=10) as r:
        assert r.headers["Content-Type"].startswith("text/html") and b"/api/series" in r.read()
    assert V.page().startswith(b"<!doctype html>")
    with urllib.request.urlopen(base + "/api/series", timeout=10) as r:
        p = json.loads(r.read())
    assert (p["series"], len(p["rows"]), p["live"]) == ("sb", 1, None)
    assert seen == [{"series": "sb"}]  # the running series is shown: its session latency was asked for
    with urllib.request.urlopen(base + "/api/series?playthrough=nope", timeout=10) as r:
        assert json.loads(r.read())["rows"] == []
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/elsewhere", timeout=10)
    assert e.value.code == 404


def test_main_serves_until_interrupted(tmp_path, monkeypatch, capsys):
    class Srv:
        server_address = ("127.0.0.1", 1234)
        closed = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            Srv.closed = True

    monkeypatch.setattr(V, "make_server", lambda *a: Srv())
    assert V.main(["--runtime", str(tmp_path), "--port", "0"]) == 0
    assert Srv.closed and "/series" in capsys.readouterr().out
    assert V._read_json(tmp_path / "missing.json") == {}
    (tmp_path / "list.json").write_text("[1]")
    assert V._read_json(tmp_path / "list.json") == {}


def test_failures_and_when_there_is_no_live_row(tmp_path):
    fails = [{"kind": "crash", "class": "harness", "n": 2}, {"kind": "timeout", "class": "unknown"}]
    assert V.failure_view(fails) == {"harness": 2, "agent": 0, "unknown": 1,
                                     "top": [{"kind": "crash(h)", "n": 2}, {"kind": "timeout(u)", "n": 1}]}  # fmt: skip
    live, rec = FakeLive(), [{"run": 2}]
    st = {"playthrough": "sb-r2", "series": "sb", "run_index": 2, "runner_pid": 1, "heartbeat": T0}
    assert V.live_row(tmp_path, "sb", st, rec, T0, live, pid_alive=lambda pid: True) is None  # recorded
    assert V.live_row(tmp_path, "sb", st, [], T0 + 3600, live, pid_alive=lambda pid: False) is None  # runner gone
    gone = V.live_row(tmp_path, "sb", dict(st, run_started=T0), [], T0 + 10, live, pid_alive=lambda pid: False)
    assert gone is not None and gone["outcome"] == "runner gone?"


def test_listing_skips_unreadable_entries(tmp_path, monkeypatch):
    write_series(tmp_path)
    real = Path.is_file

    def is_file(self):
        if self.name == "runs.jsonl":
            raise OSError("gone")
        return real(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert V.list_series(tmp_path) == []
