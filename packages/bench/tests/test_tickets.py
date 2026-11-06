# pyright: basic
"""bg3bench.tickets: a fake site behind the injected urlopen, a fake clock, and no thread sleeps: the tick is called
directly from inside the fake run."""

from __future__ import annotations

import io
import json
import os
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror

import pytest
from bg3harness import diskguard
from test_matrix_cli import fake_repo, matrix_file

from bg3bench import matrix, matrix_cli, tickets
from bg3bench.matrix import RunResult
from bg3bench.submission import ticket_hash

T0 = 1_800_000_000
TICKET_ID = "6f1c2a3b-0000-4000-8000-123456789abc"
NONCE = "ab" * 32
TOKEN = "bgb_" + "t" * 40
JOB = {
    "id": "j1",
    "scenario": "nautiloid",
    "model": "deepseek-flash",
    "client": "pi",
    "condition": "rules",
    "max_cost": 1.0,
    "effort": None,
    "profile": None,
    "repeat": 1,
}


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(diskguard, "free_mb", lambda path: 10**9)


def clock() -> float:
    return T0


def nap(_s: float) -> None:
    pass


class Answer:
    def __init__(self, status: int, body: dict | None = None):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode() if self.body is not None else b""


class FakeSite:
    """urlopen for the ticket routes: keeps what each route was sent. `beat` and `end`: what they answer next (a status,
    or an exception to raise); `issued_at`: the site's clock."""

    def __init__(self, issued_at: int = T0):
        self.issued_at = issued_at
        self.issue_answer: int | Exception = 201
        self.beat_answer: int | Exception = 204
        self.end_answer: int | Exception = 200
        self.issued: list[dict] = []
        self.beats: list[dict] = []
        self.ends: list[dict] = []
        self.auth: set[str] = set()
        self.timeouts: set[float | None] = set()
        self.ids = 0

    def __call__(self, req, timeout=None):
        self.auth.add(req.get_header("Authorization"))
        self.timeouts.add(timeout)
        doc = json.loads(req.data)
        if req.full_url.endswith("/api/tickets"):
            return self._answer(req, self.issue_answer, self.issued, doc, self._ticket())
        if req.full_url.endswith("/beat"):
            return self._answer(req, self.beat_answer, self.beats, doc)
        return self._answer(req, self.end_answer, self.ends, doc, {"status": "ended"})

    def _ticket(self) -> dict:
        self.ids += 1
        return {
            "id": f"6f1c2a3b-0000-4000-8000-{self.ids:012x}",
            "nonce": NONCE,
            "issued_at": self.issued_at,
            "expires_at": self.issued_at + 6 * 3600,
            "beat_s": 60,
        }

    def _answer(self, req, answer, into, doc, body=None):
        if isinstance(answer, Exception):
            raise answer
        into.append(doc)
        if answer >= 400:
            raise urlerror.HTTPError(req.full_url, answer, "no", Message(), io.BytesIO(b'{"error":{"code":"nope"}}'))
        return Answer(answer, body)


class FakeRun:
    """run_one: writes lines into its run dir and ticks while it plays, as `play(run_dir, tick)` says."""

    def __init__(self, root: Path, result: RunResult | Exception | None = None, play=None):
        self.root, self.result, self.play = root, result, play
        self.calls = 0
        self.tick = None
        self.every = None
        self.dirs: list[Path] = []

    def run_dir(self, job) -> Path:
        return self.root / f"{job['id']}-r{self.calls + 1}"

    def __call__(self, job):
        d = self.run_dir(job)
        self.calls += 1
        self.dirs.append(d)
        d.mkdir(parents=True, exist_ok=True)
        if self.play:
            self.play(d, self.tick)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or RunResult(f"run-{self.calls}", True, True, 0.5)


def ticketed(site: FakeSite, run: FakeRun, now: float = T0, token: str | None = TOKEN, values=()) -> tickets.Ticketed:
    def start(tick, every):
        run.tick = tick
        run.every = every
        return lambda: None

    return tickets.Ticketed(
        run,
        tickets.Site("https://site.test", site),
        token=lambda: token,
        values=lambda t: values,
        suite_tag="bench-v0.12",
        now=lambda: now,
        start_beats=start,
    )


def append(path: Path, *rows: dict) -> None:
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def ticket_of(run_dir: Path) -> dict:
    return json.loads((run_dir / tickets.TICKET_FILE).read_text())


def test_a_run_takes_a_ticket_beats_and_ends(tmp_path):
    site = FakeSite()

    def play(d, tick):
        tick()  # no stream yet
        append(d / "facts.jsonl", {"t": 1}, {"t": 2})
        tick()
        append(d / "facts.jsonl", {"t": 3})
        append(d / "S001.events.jsonl", {"t": 4})
        tick()

    run = FakeRun(tmp_path, play=play)
    t = ticketed(site, run)
    assert t(JOB).valid
    assert site.issued == [
        {
            "suite": "bench-v0.12",
            "scenario": "nautiloid",
            "model_slug": "deepseek-flash",
            "client": "pi",
            "condition": "rules",
        }
    ]
    assert [b["seq"] for b in site.beats] == [0, 1]
    assert [b["lines"] for b in site.beats] == [
        {"facts.jsonl": 2},
        {"S001.events.jsonl": 1, "facts.jsonl": 3},
    ]
    assert site.ends[0]["seq"] == 2 and site.ends[0]["outcome"] == "valid" and site.ends[0]["reason"] is None
    assert run.every == 60  # the server's beat_s
    assert site.auth == {f"Bearer {TOKEN}"} and site.timeouts == {tickets.TIMEOUT_S}
    d = run.root / "j1-r1"
    assert ticket_of(d)["end"]["delivered"] is True and ticket_of(d)["nonce"] == NONCE


def test_the_hash_matches_ticket_hash_over_the_finished_files(tmp_path):
    """What the beats committed is what the validator recomputes, each beat at its counts, with the scrub applied and
    a stream that appears midway."""
    home = "/home/" + "someone/x.lsv"
    secret = "hunter2-" + "not-a-real-value"
    site = FakeSite()

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1, "detail": home}, {"t": 2})
        tick()
        append(d / "facts.jsonl", {"t": 3, "detail": secret})
        append(d / "S001.events.jsonl", {"t": 4, "text": "é"})
        tick()
        with (d / "rolls.jsonl").open("w"):
            pass  # exists, no line yet
        tick()

    run = FakeRun(tmp_path, play=play)
    assert ticketed(site, run, values=[secret])(JOB).valid
    d = run.root / "j1-r1"
    scrubber = tickets.scrub.Scrubber([secret])
    files = {p.name: scrubber.file(p.name, p.read_bytes()) for p in d.iterdir() if ticket_hash.is_stream(p.name)}
    assert secret.encode() not in files["facts.jsonl"] and home.encode() not in files["facts.jsonl"]
    for b in [*site.beats, *site.ends]:
        assert ticket_hash.root_at(NONCE, files, b["lines"]) == b["root"]
    assert site.ends[0]["lines"] == ticket_hash.counts(files)
    assert site.ends[0]["root"] == ticket_hash.root_at(NONCE, files, ticket_hash.counts(files))


def test_tail_takes_complete_lines_and_new_files(tmp_path):
    t = tickets.Tail(tmp_path, NONCE, ())
    assert t.read()[0] == {}
    (tmp_path / "manifest.json").write_text("{}\n")
    (tmp_path / "S001.jsonl").write_text('{"raw":1}\n')  # the raw stream: not a record file
    with (tmp_path / "facts.jsonl").open("wb") as f:
        f.write(b'{"t":1}\n{"t":')
    assert t.read()[0] == {"facts.jsonl": 1}
    with (tmp_path / "facts.jsonl").open("ab") as f:
        f.write(b"2}\n")
    (tmp_path / "S001-c1.events.jsonl").write_text('{"t":3}\n')
    lines, root = t.read()
    assert lines == {"S001-c1.events.jsonl": 1, "facts.jsonl": 2}
    files = {"facts.jsonl": b'{"t":1}\n{"t":2}\n', "S001-c1.events.jsonl": b'{"t":3}\n'}
    assert root == ticket_hash.root_at(NONCE, files, lines)


def test_a_line_the_scrub_can_not_read_holds_its_stream_only(tmp_path):
    (tmp_path / "facts.jsonl").write_text('{"t":1}\nnot json\n{"t":3}\n')
    (tmp_path / "rolls.jsonl").write_text('{"t":2}\n')
    t = tickets.Tail(tmp_path, NONCE, ())
    assert t.read()[0] == {"facts.jsonl": 1, "rolls.jsonl": 1}
    assert t.read()[0] == {"facts.jsonl": 1, "rolls.jsonl": 1}


@pytest.mark.parametrize(
    "answer,why",
    [
        (503, "HTTP 503 (nope)"),
        (422, "HTTP 422 (nope)"),
        (429, "HTTP 429 (nope)"),
        (403, "HTTP 403 (nope)"),
        (urlerror.URLError("offline"), "didn't answer: URLError"),
        (TimeoutError(), "didn't answer: TimeoutError"),
    ],
)
def test_a_refused_ticket_stops_the_matrix_with_no_attempt(tmp_path, answer, why):
    site = FakeSite()
    site.issue_answer = answer
    run = FakeRun(tmp_path)
    (tmp_path / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": 0.0}))
    mx = matrix.Matrix(tmp_path / "mx", ticketed(site, run), tmp_path / "budget.json", now=clock, sleep=nap)
    mx.create("m", [JOB, {**JOB, "id": "j2"}])
    stop = mx.run()
    assert stop.startswith(f"{matrix.REFUSED}: ticket refused") and why in stop
    assert run.calls == 0  # no game, no agent
    state = mx.state()
    assert state["j1"]["status"] == "pending" and state["j2"]["attempts"] == []
    assert state["j1"]["attempts"][0]["result"]["refused"] is True
    assert state["j1"]["attempts"][0]["result"]["cost_usd"] == 0.0
    assert not (tmp_path / "j1-r1").exists()
    site.issue_answer = 201  # fixed: a later run resumes the same job
    assert mx.run() == matrix.FINISHED and run.calls == 2


def test_a_malformed_answer_is_a_refusal(tmp_path):
    class Odd(FakeSite):
        def _ticket(self):
            return {**super()._ticket(), "nonce": "short"}

    run = FakeRun(tmp_path)
    r = ticketed(Odd(), run)(JOB)
    assert r.refused and "isn't a ticket" in str(r.invalid_reason) and run.calls == 0


def test_no_token_refuses_before_any_request(tmp_path):
    site, run = FakeSite(), FakeRun(tmp_path)
    r = ticketed(site, run, token=None)(JOB)
    assert r.refused and "no site token" in str(r.invalid_reason) and run.calls == 0
    assert site.issued == []


@pytest.mark.parametrize("off,refused", [(60, False), (-60, False), (61, True), (-61, True)])
def test_a_clock_over_60_s_off_refuses_and_ends_not_played(tmp_path, off, refused):
    site, run = FakeSite(issued_at=T0 + off), FakeRun(tmp_path)
    r = ticketed(site, run)(JOB)
    assert r.refused is refused
    if refused:
        assert "61 s off" in str(r.invalid_reason) and run.calls == 0
        assert site.ends == [
            {"seq": 0, "lines": {}, "root": ticket_hash.root([]), "outcome": "not_played", "reason": "refused"}
        ]
        assert not (tmp_path / "j1-r1").exists()
    else:
        assert r.valid and run.calls == 1


def test_a_clock_refusal_survives_a_site_that_fails_the_end(tmp_path):
    site, run = FakeSite(issued_at=T0 + 61), FakeRun(tmp_path)
    site.end_answer = TimeoutError()
    assert ticketed(site, run)(JOB).refused


def test_a_ticket_json_that_can_not_be_written_ends_not_played(tmp_path):
    (tmp_path / "file").write_text("")
    site, run = FakeSite(), FakeRun(tmp_path / "file")
    r = ticketed(site, run)(JOB)
    assert r.refused and "can't write ticket.json" in str(r.invalid_reason)
    assert site.ends[0]["outcome"] == "not_played"


def test_lost_beats_change_nothing_and_the_next_covers_them(tmp_path):
    site = FakeSite()

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1})
        site.beat_answer = urlerror.URLError("offline")
        tick()
        append(d / "facts.jsonl", {"t": 2})
        tick()
        site.beat_answer = 204
        append(d / "facts.jsonl", {"t": 3})
        tick()

    run = FakeRun(tmp_path, play=play)
    r = ticketed(site, run)(JOB)
    assert r == RunResult("run-1", True, True, 0.5)
    assert [(b["seq"], b["lines"]) for b in site.beats] == [(2, {"facts.jsonl": 3})]
    log = [json.loads(ln) for ln in (run.root / "j1-r1" / tickets.BEATS_FILE).read_text().splitlines()]
    assert [x.get("error") or x.get("status") for x in log] == ["URLError", "URLError", 204, 200]


@pytest.mark.parametrize("status", [410, 401])
def test_a_410_or_401_stops_the_beats_and_leaves_the_result_alone(tmp_path, status):
    site = FakeSite()
    want = RunResult("run-1", True, False, 0.7, score=3.0)

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1})
        site.beat_answer = status
        tick()
        tick()
        tick()
        site.beat_answer = 204

    run = FakeRun(tmp_path, result=want, play=play)
    assert ticketed(site, run)(JOB) == want
    assert len(site.beats) == 1
    assert ticket_of(run.root / "j1-r1")["beats_stopped"] == status
    assert len(site.ends) == 1  # the site still hears how it ended


@pytest.mark.parametrize("what", ["connection", "garbage", "http error", "a bug in the site client"])
def test_a_raising_site_never_reaches_run_one(tmp_path, what):
    site = FakeSite()
    want = RunResult("run-1", True, True, 0.5)

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1})
        site.beat_answer = site.end_answer = {
            "connection": ConnectionResetError(),
            "garbage": ValueError("not json"),
            "http error": 500,
            "a bug in the site client": KeyError("x"),
        }[what]
        tick()

    run = FakeRun(tmp_path, result=want, play=play)
    assert ticketed(site, run)(JOB) == want
    assert ticket_of(run.root / "j1-r1")["end"]["delivered"] is False


def test_no_beat_goes_before_a_line_so_a_refused_start_stays_not_played(tmp_path):
    """The site answers 409 to not_played on a ticket that holds a beat, and a launch can take longer than a beat_s."""
    site = FakeSite()

    def play(d, tick):
        tick()
        (d / "facts.jsonl").touch()  # a stream with no line yet
        tick()

    run = FakeRun(tmp_path, RunResult(None, False, False, 0.0, "refused: the game build", True, refused=True), play)
    assert ticketed(site, run)(JOB).refused
    assert site.beats == []
    assert (site.ends[0]["seq"], site.ends[0]["outcome"]) == (0, "not_played")
    assert not (run.root / "j1-r1" / tickets.BEATS_FILE).read_text().count('"seq": 1')


def test_a_tick_survives_a_run_dir_that_went_away(tmp_path):
    site = FakeSite()

    def play(d, tick):
        (d / tickets.TICKET_FILE).unlink()
        d.rmdir()
        tick()  # the read fails and goes nowhere: its log can't be written either

    run = FakeRun(tmp_path, play=play)
    assert ticketed(site, run)(JOB).valid
    assert site.beats == [] and site.ends == []


@pytest.mark.parametrize(
    "result,outcome,reason",
    [
        (RunResult("r", True, True, 1.0), "valid", None),
        (RunResult("r", True, False, 1.0), "valid", None),
        (RunResult("r", True, None, 1.0, score=-3.0), "valid", None),
        (RunResult("r", False, False, 1.0, "harness_failure"), "invalid", "harness_failure"),
        (
            RunResult("r", False, False, 1.0, "harness_failure: /home/me/x crashed (no resume)"),
            "invalid",
            "harness_failure",
        ),
        (RunResult("r", False, False, 1.0, "runner_error"), "invalid", "runner_error"),
        (RunResult("r", False, False, 1.0, "operator"), "invalid", "operator"),
        (RunResult("r", False, False, 1.0, "off_route"), "invalid", "off_route"),
        (RunResult("r", False, False, 1.0, "unseeded"), "invalid", "unseeded"),
        (RunResult("r", False, False, 1.0, "foreign_save"), "invalid", "foreign_save"),
        (
            RunResult(None, False, False, 1.0, "artifacts failed: ValueError: /home/me/secret"),
            "invalid",
            "artifacts_failed",
        ),
        (RunResult(None, False, False, 0.0, "start failed: ValueError: /home/me/x", True), "invalid", "runner_error"),
        (RunResult(None, False, False, None, "error: KeyError: k"), "invalid", "error"),
        (RunResult(None, False, False, 1.0, "interrupted (cost from /home/me/x)"), "invalid", "interrupted"),
        (RunResult(None, False, False, 1.0, "something new: /home/me/x"), "invalid", "error"),
        (RunResult(None, False, False, 1.0, None), "invalid", "error"),
        (
            RunResult(None, False, False, 0.0, "start refused: Steam: /home/me/x", True, refused=True),
            "not_played",
            "refused",
        ),
    ],
)
def test_each_result_maps_to_its_reason_and_free_text_never_leaves(tmp_path, result, outcome, reason):
    site = FakeSite()
    run = FakeRun(tmp_path, result=result)
    assert ticketed(site, run)(JOB) == result
    end = site.ends[0]
    assert (end["outcome"], end["reason"]) == (outcome, reason)
    assert end["reason"] is None or end["reason"] in tickets.REASONS
    sent = json.dumps([site.issued, site.beats, site.ends])
    assert "/home" not in sent and "secret" not in sent and "cost" not in sent and "score" not in sent


def test_a_raising_run_one_ends_as_error_and_still_raises(tmp_path):
    site = FakeSite()
    run = FakeRun(tmp_path, result=RuntimeError("/home/me/x"))
    with pytest.raises(RuntimeError):
        ticketed(site, run)(JOB)
    assert (site.ends[0]["outcome"], site.ends[0]["reason"]) == ("invalid", "error")


def test_a_second_signal_sends_no_end_and_a_restart_sends_it(tmp_path):
    site = FakeSite()

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1}, {"t": 2})
        tick()
        append(d / "facts.jsonl", {"t": 3})
        raise SystemExit(1)

    run = FakeRun(tmp_path, play=play)
    t = ticketed(site, run)
    with pytest.raises(SystemExit):
        t(JOB)
    assert site.ends == []
    d = run.root / "j1-r1"
    t.close_interrupted({"j1": {"status": "interrupted", "attempts": [{"run_dir": str(d)}]}})
    assert (site.ends[0]["outcome"], site.ends[0]["reason"]) == ("invalid", "interrupted")
    assert site.ends[0]["lines"] == {"facts.jsonl": 3}
    assert site.ends[0]["seq"] > site.beats[-1]["seq"]
    assert site.ends[0]["root"] == ticket_hash.root_at(
        NONCE, {"facts.jsonl": (d / "facts.jsonl").read_bytes()}, {"facts.jsonl": 3}
    )
    t.close_interrupted({"j1": {"status": "interrupted", "attempts": [{"run_dir": str(d)}]}})
    assert len(site.ends) == 1  # delivered once


def test_close_interrupted_leaves_other_jobs_and_runs_without_a_ticket(tmp_path):
    site, run = FakeSite(), FakeRun(tmp_path)
    t = ticketed(site, run)
    t.close_interrupted({
        "a": {"status": "pending", "attempts": []},
        "b": {"status": "interrupted", "attempts": [{"run_dir": None}]},
        "c": {"status": "interrupted", "attempts": [{"run_dir": str(tmp_path)}]},
    })  # fmt: skip
    assert site.ends == []


def test_deliver_end_retries_the_end_kept_in_ticket_json(tmp_path):
    site = FakeSite()
    site.end_answer = TimeoutError()

    def play(d, tick):
        append(d / "facts.jsonl", {"t": 1})

    run = FakeRun(tmp_path, play=play)
    client = tickets.Site("https://site.test", site)
    assert ticketed(site, run)(JOB).valid
    d = run.root / "j1-r1"
    kept = ticket_of(d)["end"]
    assert kept["delivered"] is False and kept["outcome"] == "valid"
    assert tickets.deliver_end(d, client, None, (), lambda: T0) is False  # no token: nothing sent
    assert tickets.deliver_end(d, client, TOKEN, (), lambda: T0) is False  # still down
    site.end_answer = 200
    assert tickets.deliver_end(d, client, TOKEN, (), lambda: T0) is True
    assert site.ends == [{k: kept[k] for k in ("seq", "lines", "root", "outcome", "reason")}]
    assert ticket_of(d)["end"]["delivered"] is True
    assert tickets.deliver_end(d, client, TOKEN, (), lambda: T0) is True and len(site.ends) == 1
    assert tickets.deliver_end(tmp_path, client, TOKEN, (), lambda: T0) is None


def test_deliver_end_with_unreadable_files_stays_undelivered(tmp_path):
    (tmp_path / tickets.TICKET_FILE).write_text(json.dumps({"id": TICKET_ID}))  # no nonce: can't hash
    site = FakeSite()
    client = tickets.Site("https://site.test", site)
    assert tickets.deliver_end(tmp_path, client, TOKEN, (), lambda: T0, close=True) is False
    assert site.ends == []


def test_deliver_end_makes_up_no_end_unless_told_to_close(tmp_path):
    (tmp_path / tickets.TICKET_FILE).write_text(json.dumps({"id": TICKET_ID, "nonce": NONCE}))
    site = FakeSite()
    client = tickets.Site("https://site.test", site)
    assert tickets.deliver_end(tmp_path, client, TOKEN, (), lambda: T0) is False
    assert site.ends == []
    assert tickets.deliver_end(tmp_path, client, TOKEN, (), lambda: T0, close=True) is True
    assert site.ends[0]["reason"] == "interrupted"


def test_last_seq_reads_past_a_torn_log(tmp_path):
    assert tickets._last_seq(tmp_path) == -1
    (tmp_path / tickets.BEATS_FILE).write_text('{"seq":3}\nnot json\n[1]\n{"kind":"x"}\n{"seq":7}\n')
    assert tickets._last_seq(tmp_path) == 7


def test_a_rerun_gets_a_new_ticket(tmp_path):
    site = FakeSite()
    run = FakeRun(tmp_path, result=RunResult("r", False, False, 0.1, "harness_failure"))
    (tmp_path / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": 0.0}))
    mx = matrix.Matrix(tmp_path / "mx", ticketed(site, run), tmp_path / "budget.json", now=clock, sleep=nap)
    mx.create("m", [JOB])
    assert mx.run() == matrix.FINISHED
    assert run.calls == 3 and len(site.issued) == 3 and len(site.ends) == 3  # one run, max_reruns 2
    ids = {ticket_of(tmp_path / f"j1-r{i}")["id"] for i in (1, 2, 3)}
    assert len(ids) == 3


def test_the_queues_start_record_keeps_run_dir(tmp_path):
    run = FakeRun(tmp_path)
    (tmp_path / "budget.json").write_text(json.dumps({"cap": 20.0, "spent": 0.0}))
    t = ticketed(FakeSite(), run)
    assert t.run_dir(JOB) == run.run_dir(JOB)
    mx = matrix.Matrix(tmp_path / "mx", t, tmp_path / "budget.json", now=clock, sleep=nap)
    mx.create("m", [JOB])
    assert mx.run() == matrix.FINISHED
    assert mx.state()["j1"]["attempts"][0]["run_dir"] == str(tmp_path / "j1-r1")


def test_thread_beats_ticks_until_halted():
    seen = threading.Event()
    calls = []

    def tick():
        calls.append(1)
        seen.set()

    halt = tickets.thread_beats(tick, 0.0)
    assert seen.wait(30)
    halt()
    count = len(calls)
    assert count >= 1
    halt()
    assert len(calls) == count


class StuckBeat(FakeSite):
    """A beat that doesn't return until told to, then answers `late`."""

    def __init__(self, late: int = 204):
        super().__init__()
        self.sending, self.let_go, self.late = threading.Event(), threading.Event(), late

    def __call__(self, req, timeout=None):
        if req.full_url.endswith("/beat"):
            self.sending.set()
            self.let_go.wait()
            self.beat_answer = self.late
        return super().__call__(req, timeout)


def test_a_beat_that_never_returns_holds_up_neither_the_end_nor_the_process(tmp_path, monkeypatch):
    monkeypatch.setattr(tickets, "JOIN_S", 0.01)
    site = StuckBeat(410)  # by the time it lands the ticket has ended

    def play(d, _tick):
        append(d / "facts.jsonl", {"t": 1})
        assert site.sending.wait(30)

    run = FakeRun(tmp_path, play=play)
    t = tickets.Ticketed(
        run,
        tickets.Site("https://site.test", site),
        token=lambda: TOKEN,
        values=lambda _t: (),
        suite_tag="bench-v0.12",
        now=lambda: T0,
        start_beats=lambda tick, _every: tickets.thread_beats(tick, 0.0),
    )
    results: list[RunResult] = []
    caller = threading.Thread(target=lambda: results.append(t(JOB)), daemon=True)
    caller.start()
    try:
        caller.join(30)
        assert results and results[0].valid and [e["outcome"] for e in site.ends] == ["valid"]
        beat_thread = next(x for x in threading.enumerate() if x.name == "ticket-beats")
        assert beat_thread.daemon
    finally:
        site.let_go.set()
    beat_thread.join(30)
    kept = ticket_of(run.root / "j1-r1")  # its 410 changes nothing: the end stays kept and delivered
    assert kept["end"]["delivered"] is True and "beats_stopped" not in kept


# ------------------------------------------------------------------------------------------------- the site URL, env


@pytest.mark.parametrize(
    "url,want",
    [
        ("https://site.test", "https://site.test"),
        ("https://site.test/", "https://site.test"),
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
        ("http://localhost:8787", "http://localhost:8787"),
    ],
)
def test_base_url(url, want):
    assert tickets.base_url(url) == want


@pytest.mark.parametrize(
    "url", [None, "", "http://site.test", "https://site.test/api", "https://s.test?x=1", "ftp://x"]
)
def test_a_bad_site_url_is_refused(url):
    with pytest.raises(ValueError, match="site URL"):
        tickets.base_url(url)


def test_literals_are_the_env_values_and_the_token(tmp_path):
    (tmp_path / ".env").write_text("A_KEY=value-of-a\nEMPTY=\n")
    assert tickets.literals(tmp_path / ".env", TOKEN) == ["value-of-a", TOKEN]
    assert tickets.literals(tmp_path / ".env", None) == ["value-of-a"]


def test_read_token(tmp_path):
    assert tickets.read_token(tmp_path / "none") is None
    (tmp_path / "t").write_text("\n")
    assert tickets.read_token(tmp_path / "t") is None
    (tmp_path / "t").write_text(TOKEN + "\n")
    assert tickets.read_token(tmp_path / "t") == TOKEN


def test_the_site_reads_a_body_that_is_not_json_or_an_object(tmp_path):
    class Raw:
        def __init__(self, data):
            self.data = data

        def __call__(self, req, timeout=None):
            answer = Answer(201)
            answer.read = lambda: self.data
            return answer

    assert tickets.Site("https://s.test", Raw(b"<html>")).issue(TOKEN, {}) == (201, {})
    assert tickets.Site("https://s.test", Raw(b"[1]")).issue(TOKEN, {}) == (201, {})


class Local:
    """A real HTTP server on this machine for the length of a `with`: `answer` is what it says to any request."""

    def __init__(self, answer: int, location: str = "", trickle: bool = False):
        self.seen: list[dict[str, str]] = []
        self.stop = stop = threading.Event()
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def respond(self):
                seen.append(dict(self.headers.items()))
                self.send_response(answer)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "1000000" if trickle else "0")
                self.end_headers()
                while trickle and not stop.wait(0.01):  # a byte at a time: no socket read ever times out
                    self.wfile.write(b"x")
                    self.wfile.flush()

            do_GET = do_POST = respond

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_site_follows_no_redirect_so_the_token_and_the_access_secret_stay_there(status):
    """Staging behind Access without a working service token answers with a redirect to its login page."""
    env = {tickets.ACCESS_ID_ENV: "id", tickets.ACCESS_SECRET_ENV: "secret"}
    with Local(200) as other, Local(status, other.url + "/login") as site:
        client = tickets.http_site(site.url, env)
        assert client.end(TOKEN, TICKET_ID, {"seq": 0}) == status  # read as an answer, not a delivered end
        assert client.beat(TOKEN, TICKET_ID, {"seq": 0}) == status
        assert tickets.Site(site.url).issue(TOKEN, {})[0] == status
    assert len(site.seen) == 3 and other.seen == []


def test_a_site_that_never_stops_sending_costs_the_end_one_timeout_and_keeps_it_undelivered(tmp_path, monkeypatch):
    """TIMEOUT_S bounds each socket read, not the request: without the deadline this holds the matrix for good."""
    monkeypatch.setattr(tickets, "TIMEOUT_S", 0.3)
    ticket = {"id": TICKET_ID, "nonce": NONCE}
    results: list[bool] = []
    with Local(200, trickle=True) as site:
        doc = {"seq": 0, "lines": {}, "root": "0" * 64, "outcome": "valid", "reason": None}
        args = (tickets.Site(site.url), TOKEN, tmp_path, ticket, doc, T0)
        caller = threading.Thread(target=lambda: results.append(tickets._send_end(*args)), daemon=True)
        caller.start()
        caller.join(30)
        assert results == [False]
        assert all(x.daemon for x in threading.enumerate() if x.name == "ticket-request")
    assert json.loads((tmp_path / tickets.TICKET_FILE).read_text())["end"]["delivered"] is False


# ----------------------------------------------------------------------------------------------------- matrix_cli


TICKETED_TOML = """
name = "night1"
models = ["profiles/claude.toml"]
scenarios = ["nautiloid"]
conditions = ["rules"]
repeats = 1
allow_gaps = true
ticket = "required"
"""


def cli(repo, tmp_path, *argv, body=TICKETED_TOML, run_one=None, site=None, env=None, now=T0):
    out = io.StringIO()
    environ = {tickets.SITE_ENV: "https://site.test", tickets.CONFIG_ENV: str(tmp_path / "config")}
    code = matrix_cli.main(
        [*argv, "--matrix", str(matrix_file(tmp_path, body))],
        repo=repo,
        run_one=run_one,
        now=lambda: now,
        sleep=lambda s: None,
        out=out,
        environ={**environ, **(env or {})},
        urlopen=site,
    )
    return code, out.getvalue()


def save_token(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / tickets.SITE_TOKEN).write_text(TOKEN + "\n")


def test_a_matrix_with_the_key_plays_every_attempt_under_a_ticket(tmp_path):
    repo = fake_repo(tmp_path)
    save_token(tmp_path)
    site = FakeSite()
    (repo / ".env").write_text("A_KEY=a-value-of-the-env\n")
    run = FakeRun(
        tmp_path / "runs", play=lambda d, tick: append(d / "facts.jsonl", {"t": 1, "x": "a-value-of-the-env"})
    )
    code, out = cli(repo, tmp_path, "run", run_one=run, site=site)
    assert code == 0 and "stopped: finished" in out
    assert site.auth == {f"Bearer {TOKEN}"}
    assert (
        site.issued[0]["model_slug"] == "claude-opus-5-5" and site.issued[0]["suite"] == tickets.suite.suite_version()
    )
    end = site.ends[0]
    # the .env value and the token from the file are what the line was scrubbed with
    scrubber = tickets.scrub.Scrubber(["a-value-of-the-env", TOKEN])
    files = {"facts.jsonl": scrubber.file("facts.jsonl", (run.dirs[0] / "facts.jsonl").read_bytes())}
    assert b"a-value-of-the-env" not in files["facts.jsonl"]
    assert ticket_hash.root_at(NONCE, files, end["lines"]) == end["root"]


def test_a_matrix_without_the_key_takes_no_ticket(tmp_path):
    repo = fake_repo(tmp_path)
    site = FakeSite()
    run = FakeRun(tmp_path / "runs")
    code, _ = cli(
        repo, tmp_path, "run", body=TICKETED_TOML.replace('ticket = "required"\n', ""), run_one=run, site=site
    )
    assert code == 0 and run.calls == 1 and site.issued == []


def test_no_token_file_refuses_the_run_and_stops_the_matrix(tmp_path):
    repo = fake_repo(tmp_path)
    site, run = FakeSite(), FakeRun(tmp_path / "runs")
    code, out = cli(repo, tmp_path, "run", run_one=run, site=site)
    assert code == 1 and "refused: ticket refused: no site token" in out
    assert run.calls == 0 and site.issued == []


def test_no_site_url_refuses_the_matrix(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    code, _ = cli(repo, tmp_path, "run", run_one=FakeRun(tmp_path / "runs"), env={tickets.SITE_ENV: ""})
    assert code == 2 and "no site URL" in capsys.readouterr().err


def test_a_runner_that_names_no_run_dir_is_refused(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    code, _ = cli(repo, tmp_path, "run", run_one=lambda job: None)
    assert code == 2 and "needs --runner session" in capsys.readouterr().err


def test_plan_says_so_and_a_bad_key_is_refused(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    code, out = cli(repo, tmp_path, "plan")
    assert code == 0 and "each run takes a ticket from the site" in out
    code, _ = cli(repo, tmp_path, "plan", body=TICKETED_TOML.replace('"required"', '"maybe"'))
    assert code == 2 and "ticket must be 'required'" in capsys.readouterr().err


def test_a_matrix_restart_closes_the_interrupted_attempt(tmp_path):
    """The queue holds a started attempt with no end: the restart sends its late end, then plays the job again."""
    repo = fake_repo(tmp_path)
    save_token(tmp_path)
    site = FakeSite()
    run = FakeRun(tmp_path / "runs", play=lambda d, tick: append(d / "facts.jsonl", {"t": 1}))

    class Dies(FakeRun):
        def __call__(self, job):
            super().__call__(job)
            raise SystemExit(1)

    dies = Dies(tmp_path / "runs", play=lambda d, tick: append(d / "facts.jsonl", {"t": 1}))
    with pytest.raises(SystemExit):
        cli(repo, tmp_path, "run", run_one=dies, site=site)
    assert site.ends == []
    run.calls = 1  # the next run gets a dir of its own
    code, _ = cli(repo, tmp_path, "resume", run_one=run, site=site)
    assert code == 0
    assert [(e["outcome"], e["reason"]) for e in site.ends] == [("invalid", "interrupted"), ("valid", None)]
    assert len(site.issued) == 2


def test_the_token_is_never_in_the_runs_environment(tmp_path):
    """It is read from its file by this process and goes in the Authorization header only: nothing sets it in
    os.environ, so the game and the agent can't inherit it, and it stays out of ticket.json and the beat log."""
    repo = fake_repo(tmp_path)
    save_token(tmp_path)
    seen = {}

    def play(d, tick):
        seen["env"] = dict(os.environ)
        tick()

    run = FakeRun(tmp_path / "runs", play=play)
    code, _ = cli(repo, tmp_path, "run", run_one=run, site=FakeSite())
    assert code == 0
    assert TOKEN not in json.dumps(seen["env"])
    for p in (tmp_path / "runs").rglob("*"):
        if p.is_file():
            assert TOKEN not in p.read_text()


def test_the_matrix_process_sends_the_access_headers_of_a_site_behind_access(tmp_path):
    repo = fake_repo(tmp_path)
    save_token(tmp_path)
    seen = []

    class Site(FakeSite):
        def __call__(self, req, timeout=None):
            seen.append(req.get_header("Cf-access-client-id"))
            return super().__call__(req, timeout)

    env = {tickets.ACCESS_ID_ENV: "id.access", tickets.ACCESS_SECRET_ENV: "secret"}
    code, _ = cli(repo, tmp_path, "run", run_one=FakeRun(tmp_path / "runs"), site=Site(), env=env)
    assert code == 0 and seen and set(seen) == {"id.access"}


def test_view_of_a_run_with_no_log_or_ticket(tmp_path):
    assert tickets.view(tmp_path) is None
    (tmp_path / tickets.TICKET_FILE).write_text(json.dumps({"id": "i", "beats_stopped": 410}))
    assert tickets.view(tmp_path) == {
        "id": "i",
        "issued_at": None,
        "expires_at": None,
        "beats_sent": 0,
        "beats_landed": 0,
        "beats_stopped": 410,
        "end": None,
    }
