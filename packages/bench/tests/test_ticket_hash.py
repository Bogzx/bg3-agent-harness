# pyright: basic
"""bg3bench.submission.ticket_hash, and T04's streams and T05-T07 on small hand-made runs (the whole-bundle path, T01-T04
and failing closed are test_submission_validate.py's). The clocks are plain numbers: nothing here reads the time."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from bg3bench.submission import scrub, ticket_hash, validate

FIXTURES = Path(__file__).parent / "fixtures" / "submission"
NONCE = "ab" * 32
HOME = "/home/" + "someone/saves/x.lsv"  # built at run time: tools/ci/scrub.py scans this file
SECRET = "hunter2-" + "not-a-real-value"
T0 = 1_800_000_000


def fixture_streams(name: str) -> dict[str, bytes]:
    with tarfile.open(FIXTURES / f"{name}.bundle.tar.gz") as tar:
        files = {m.name: (tar.extractfile(m) or b"").read() for m in tar.getmembers()}  # type: ignore[union-attr]
    return {n: d for n, d in files.items() if ticket_hash.is_stream(n)}


def linewise(s: scrub.Scrubber, data: bytes) -> bytes:
    return b"".join(s.line(ln.decode()).encode() + b"\n" for ln in ticket_hash.complete_lines(data))


def test_the_streams_are_the_record_jsonl_files():
    for name in ("facts.jsonl", "rolls.jsonl", "S001.events.jsonl", "S001-c1.events.jsonl", "S012-r2.events.jsonl"):
        assert ticket_hash.is_stream(name)
    for name in ("manifest.json", "S001.fights.jsonl", "A.events.jsonl", "ticket.beats.jsonl", "S001.jsonl"):
        assert not ticket_hash.is_stream(name)


@pytest.mark.parametrize("name", ["act3_gold_max", "fight_act2_balthazar"])
def test_scrubbing_line_by_line_equals_scrubbing_the_file(name):
    """What the client hashes as the run plays is what pack writes, on a real run's lines."""
    for fname, data in fixture_streams(name).items():
        assert data.endswith(b"\n")
        a, b = scrub.Scrubber(), scrub.Scrubber()
        assert linewise(a, data) == b.file(fname, data)
        assert a.replacements == b.replacements


def test_scrubbing_line_by_line_equals_the_file_with_literals_and_replacements():
    data = fixture_streams("fight_act2_balthazar")["facts.jsonl"]
    dirty = {"kind": "mark", "mark": "x", "t": 1.5, "detail": f"{HOME} and {SECRET} é"}
    data += (json.dumps(dirty, ensure_ascii=False) + "\n").encode() + b"\n" + b'{"t":2,"a":"clean"}\n'
    a, b = scrub.Scrubber([SECRET]), scrub.Scrubber([SECRET])
    out = linewise(a, data)
    assert out == b.file("facts.jsonl", data)
    assert a.replacements == b.replacements == 2
    assert SECRET.encode() not in out and HOME.encode() not in out and b"<env>" in out and "é".encode() in out


def test_the_stream_hash_is_the_documented_one():
    lines = [b'{"t":1}', b'{"t":2}']
    want = hashlib.sha256(f"bg3bench.ticket.v1\0{NONCE}\0facts.jsonl\0".encode() + lines[0] + b"\n" + lines[1] + b"\n")
    h = ticket_hash.RunningHash(NONCE, "facts.jsonl")
    h.feed(lines[0])
    h.feed(lines[1])
    assert h.hexdigest() == want.hexdigest() and h.lines == 2
    text = f'[["S001.events.jsonl",0,"{"a" * 64}"],["facts.jsonl",2,"{want.hexdigest()}"]]'
    root = ticket_hash.root([("facts.jsonl", 2, want.hexdigest()), ("S001.events.jsonl", 0, "a" * 64)])
    assert root == hashlib.sha256(text.encode()).hexdigest()


def test_the_hash_at_each_cut_equals_a_fresh_hash_over_that_prefix():
    data = fixture_streams("fight_act2_balthazar")["S001.events.jsonl"]
    lines = ticket_hash.complete_lines(data)
    cuts = {0, 1, 17, len(lines) // 2, len(lines)}
    at = ticket_hash.digests_at(NONCE, "S001.events.jsonl", data, cuts)
    assert set(at) == cuts
    for n in cuts:
        fresh = ticket_hash.RunningHash(NONCE, "S001.events.jsonl")
        for line in lines[:n]:
            fresh.feed(line)
        assert at[n] == fresh.hexdigest()


def test_the_nonce_and_the_name_seed_the_hash():
    data = b'{"t":1}\n'
    base = ticket_hash.digests_at(NONCE, "facts.jsonl", data, {1})[1]
    assert ticket_hash.digests_at("cd" * 32, "facts.jsonl", data, {1})[1] != base
    assert ticket_hash.digests_at(NONCE, "rolls.jsonl", data, {1})[1] != base


def test_only_complete_lines_count():
    assert ticket_hash.complete_lines(b"") == []
    assert ticket_hash.complete_lines(b'{"t":1}\n{"t":') == [b'{"t":1}']
    assert ticket_hash.counts({"facts.jsonl": b'{"t":1}\n{"t":', "rolls.jsonl": b"", "manifest.json": b"{}\n"}) == {
        "facts.jsonl": 1,
        "rolls.jsonl": 0,
    }


def test_a_count_the_file_has_not_got_is_refused():
    with pytest.raises(ValueError, match="facts.jsonl: 2 lines counted, the file has 1"):
        ticket_hash.digests_at(NONCE, "facts.jsonl", b'{"t":1}\n', {2})
    with pytest.raises(ValueError, match="S001.events.jsonl: 1 lines counted, the file has 0"):
        ticket_hash.root_at(NONCE, {"facts.jsonl": b""}, {"S001.events.jsonl": 1})


def test_a_stream_with_no_file_is_empty_at_count_zero():
    assert ticket_hash.root_at(NONCE, {}, {"rolls.jsonl": 0}) == ticket_hash.root(
        [("rolls.jsonl", 0, ticket_hash.RunningHash(NONCE, "rolls.jsonl").hexdigest())]
    )


# ---------------------------------------------------------------------------------------- T05, T06, T07 on small runs


def run_of(ts: list[float], wall: float | None = None) -> validate.Loaded:
    """A run with run_start at T0, a facts line at each T0 + x and run_end at the last x; wall_s defaults to that."""
    last = ts[-1]
    rows = [
        {"kind": "mark", "mark": "run_start", "t": T0},
        *({"kind": "event", "t": T0 + x} for x in ts),
        {"kind": "mark", "mark": "run_end", "t": T0 + last},
    ]
    raw = {"facts.jsonl": "".join(json.dumps(r) + "\n" for r in rows).encode()}
    return validate.Loaded(
        Path("."), {}, raw, {}, {"wall_s": wall if wall is not None else last}, rows, None, {}, {}, {}, {}
    )


def lines_at(run: validate.Loaded, machine_t: float) -> int:
    """How many facts lines the machine had written by machine_t: a beat cuts there."""
    return sum(1 for r in run.facts if r["t"] <= machine_t)


def beat(run: validate.Loaded, s: int, a: float, off: float = 0, **extra) -> dict:
    """The beat the server stores at its time `a`, from a machine whose clock is `off` behind the server's."""
    return {"s": s, "a": a, "l": {"facts.jsonl": lines_at(run, a - off)}, "r": "0" * 64, **extra}


def beats(run: validate.Loaded, every: int = 60, off: float = 0, upto: float | None = None) -> list[dict]:
    end = upto if upto is not None else run.facts[-1]["t"] - T0
    return [beat(run, i, T0 + off + x, off) for i, x in enumerate(range(every, int(end) + 1, every))]


def ticket_of(bs: list[dict], issued: float = T0 - 10, expires: float = T0 + 6 * 3600) -> dict:
    return {"issued_at": issued, "expires_at": expires, "beats": bs}


def every_10s(until: int = 1200) -> list[float]:
    return list(map(float, range(10, until + 1, 10)))


def test_a_run_with_regular_beats_passes():
    run = run_of(every_10s())
    t = ticket_of(beats(run))
    assert validate.t05_times(run, t) == (True, "")
    assert validate.t06_coverage(run, t) == (True, "")
    assert validate.t07_gaps(run, t) == (True, "")


def test_a_run_started_before_its_ticket_fails_t05():
    run = run_of(every_10s())
    assert validate.t05_times(run, ticket_of(beats(run), issued=T0 + 5))[0] is True
    ok, why = validate.t05_times(run, ticket_of(beats(run), issued=T0 + 6))
    assert ok is False and "6 s before its ticket" in why


@pytest.mark.parametrize("off,ok", [(300, True), (301, False), (-300, True), (-301, False), (200, True), (-200, True)])
def test_the_clock_offset_limit(off, ok):
    """A clock 200 s off passes, and a log with yesterday's times can't be replayed untouched."""
    run = run_of(every_10s())
    t = ticket_of(beats(run, off=off), issued=T0 + off - 10, expires=T0 + off + 6 * 3600)
    assert validate.t05_times(run, t)[0] is ok


def test_lines_falling_behind_the_servers_clock_fail_t05():
    """Silence of 240 s at a beat passes, 241 s fails: an all-streams silence means the lines stopped being live."""
    ts = [*every_10s(600), 1200.0]
    run = run_of(ts)
    base = [beat(run, 0, T0 + 600)]
    assert validate.t05_times(run, ticket_of([*base, beat(run, 1, T0 + 840), beat(run, 2, T0 + 1200)]))[0] is True
    ok, why = validate.t05_times(run, ticket_of([*base, beat(run, 1, T0 + 841), beat(run, 2, T0 + 1200)]))
    assert ok is False and "241 s behind" in why


def test_ten_minutes_of_host_record_after_run_end_passes():
    """host.record stops the game and moves saves with no line written: those beats don't count as stale."""
    run = run_of(every_10s())
    t = ticket_of(beats(run, upto=1800))
    assert t["beats"][-1]["a"] == T0 + 1800 and t["beats"][-1]["l"]["facts.jsonl"] == len(run.facts)
    assert validate.t05_times(run, t) == (True, "")
    assert validate.t06_coverage(run, t) == (True, "")
    assert validate.t07_gaps(run, t) == (True, "")


def test_a_beat_that_has_not_reached_run_end_stays_held_to_the_staleness_limit():
    """run_end is the line that ends the exemption: one line short of it, the beat still counts."""
    run = run_of(every_10s())
    short = len(run.facts) - 1
    stuck = {"s": 99, "a": T0 + 1500, "l": {"facts.jsonl": short}, "r": "0" * 64}
    ok, why = validate.t05_times(run, ticket_of([*beats(run), stuck]))
    assert ok is False and "beat 99" in why


def test_a_wall_s_longer_than_the_server_watched_fails_t05():
    def two_beats(run: validate.Loaded) -> dict:
        return ticket_of([beat(run, 0, T0 + 60), beat(run, 1, T0 + 120)])

    fine, over = run_of(every_10s(120), wall=430), run_of(every_10s(120), wall=431)  # 120 + 10 + 300
    assert validate.t05_times(fine, two_beats(fine))[0] is True
    ok, why = validate.t05_times(over, two_beats(over))
    assert ok is False and "wall_s 431" in why


def test_a_line_past_the_tickets_expiry_fails_t05():
    run = run_of(every_10s())
    assert validate.t05_times(run, ticket_of(beats(run), expires=T0 + 1200))[0] is True
    ok, why = validate.t05_times(run, ticket_of(beats(run), expires=T0 + 1199))
    assert ok is False and "past the ticket's expiry" in why


def test_an_end_delivered_days_late_passes():
    run = run_of(every_10s())
    late = beat(run, 99, T0 + 3 * 86400, e=1)
    t = ticket_of([*beats(run), late])
    assert validate.t05_times(run, t) == (True, "")
    assert validate.t06_coverage(run, t) == (True, "")
    assert validate.t07_gaps(run, t) == (True, "")


def test_a_late_end_does_not_cover_a_run_nobody_watched():
    """The end's time is its arrival, so it counts for no coverage: 20 minutes with one regular beat at the start."""
    run = run_of(every_10s(1200))
    t = ticket_of([beat(run, 0, T0 + 10), beat(run, 1, T0 + 86400, e=1)])
    assert validate.t06_coverage(run, t)[0] is False
    assert validate.t07_gaps(run, t)[0] is False


def test_a_short_run_with_only_a_timely_end_passes():
    run = run_of(every_10s(40))
    t = ticket_of([beat(run, 0, T0 + 45, e=1)])
    assert validate.t05_times(run, t) == (True, "")
    assert validate.t06_coverage(run, t) == (True, "")
    assert validate.t07_gaps(run, t) == (True, "")


def test_a_short_run_whose_only_end_is_days_late_fails_t05():
    """With no regular beat over its lines the end is the only clock, so a late one reads as a far-off clock."""
    run = run_of(every_10s(40))
    assert validate.t05_times(run, ticket_of([beat(run, 0, T0 + 86400, e=1)]))[0] is False


def test_beats_that_cover_no_line_give_no_offset():
    run = run_of(every_10s())
    empty = {"s": 0, "a": T0 + 60, "l": {"facts.jsonl": 0}, "r": "0" * 64}
    ok, why = validate.t05_times(run, ticket_of([empty]))
    assert ok is False and "no beat or end covers a line" in why
    with pytest.raises(ValueError, match="no beat or end"):
        validate.t06_coverage(run, ticket_of([empty]))
    assert validate._guarded(validate.t06_coverage, run, ticket_of([empty]))[0] is False


def test_a_run_with_no_run_end_mark_fails_closed():
    run = run_of(every_10s())
    raw = run.raw["facts.jsonl"].rsplit(b"\n", 2)[0] + b"\n"
    t = ticket_of(beats(run, upto=1100))
    no_end = validate.Loaded(Path("."), {}, {"facts.jsonl": raw}, {}, run.result, run.facts[:-1], None, {}, {}, {}, {})
    for check in (validate.t05_times, validate.t06_coverage, validate.t07_gaps):
        ok, why = validate._guarded(check, no_end, t)
        assert ok is False and "run_end" in why


@pytest.mark.parametrize("wall,ok", [(600, True), (601, False)])
def test_coverage_of_exactly_half_passes_and_more_fails(wall, ok):
    """One beat at the start, then nothing: the gap is the wall time and everything past 300 s of it is uncovered,
    300 of 600 s (exactly half) against 301 of 601 s."""
    run = run_of([0.0, float(wall)])
    t = ticket_of([{"s": 0, "a": T0, "l": {"facts.jsonl": 2}, "r": "0" * 64}])
    assert validate.t06_coverage(run, t)[0] is ok


@pytest.mark.parametrize("gap,ok", [(300, True), (301, False)])
def test_a_gap_over_300_s_flags(gap, ok):
    run = run_of(every_10s(1200))
    later = [beat(run, i + 2, T0 + x) for i, x in enumerate(range(120 + gap, 1201, 60))]
    t = ticket_of([beat(run, 0, T0 + 60), beat(run, 1, T0 + 60 + gap), *later])
    assert validate.t07_gaps(run, t)[0] is ok
    assert validate.t06_coverage(run, t)[0] is True  # a long gap alone is no rejection


def test_the_run_start_and_end_gaps_count():
    """The first beat comes 301 s into the run: the stretch from run_start to it is a gap too."""
    run = run_of(every_10s(600))
    t = ticket_of([beat(run, 0, T0 + 301), beat(run, 1, T0 + 600)])
    assert validate.t07_gaps(run, t)[0] is False


# ------------------------------------------------------------- T04: a beat must reach every stream's old lines


def with_rolls(run: validate.Loaded, xs: list[float]) -> validate.Loaded:
    """The run with a rolls.jsonl of a line at each T0 + x, and the bundle's ticket over its files."""
    raw = {**run.raw, ticket_hash.ROLLS: "".join(json.dumps({"t": T0 + x}) + "\n" for x in xs).encode()}
    env = {"ticket": {"nonce": NONCE, "root": ticket_hash.root_at(NONCE, raw, ticket_hash.counts(raw))}}
    return dataclasses.replace(run, raw=raw, envelope=env)


def named(run: validate.Loaded, s: int, a: float, off: float = 0, only: tuple[str, ...] | None = None) -> dict:
    """The beat stored at the server's time `a`: each stream as far as the machine had got, `only` those streams."""
    lines = {
        n: sum(1 for ln in ticket_hash.complete_lines(d) if json.loads(ln)["t"] <= a - off)
        for n, d in run.raw.items()
        if only is None or n in only
    }
    return {"s": s, "a": a, "l": lines, "r": ticket_hash.root_at(NONCE, run.raw, lines)}


def rooted(bs: list[dict]) -> dict:
    return {**ticket_of(bs), "nonce": NONCE}


FACTS_ONLY = (ticket_hash.FACTS,)


def test_a_stream_silent_for_491_s_passes():
    """Nothing to commit in a quiet stream: the count stays where it was and no line is older than it."""
    run = with_rolls(run_of(every_10s(1200)), [10, 501, 1100])
    t = rooted([named(run, i, T0 + x) for i, x in enumerate(range(60, 1201, 60))])
    assert validate.t04_beats(run, t) == (True, "")


def test_a_stream_left_out_of_the_beats_fails_t04():
    run = with_rolls(run_of(every_10s(1200)), [10, 501, 1100])
    t = rooted([named(run, i, T0 + x, only=FACTS_ONLY) for i, x in enumerate(range(60, 1201, 60))])
    ok, why = validate.t04_beats(run, t)
    assert ok is False and "beat 4" in why and "rolls.jsonl" in why  # T0 + 300: the first with a line 240 s old
    assert validate.t05_times(run, t) == (True, "")  # the newest line of any stream looks fresh


@pytest.mark.parametrize("off", [0, 50, -50, 300])  # 300: late beats look like a clock off, the honest limit: 540 s
@pytest.mark.parametrize("delay,ok", [(250, True), (251, False)])
def test_a_line_may_wait_240_s_on_the_servers_clock(off, delay, ok):
    run = with_rolls(run_of(every_10s(600)), [10])
    t = rooted([named(run, 0, T0 + 60 + off, off, FACTS_ONLY), named(run, 1, T0 + delay + off, off, FACTS_ONLY)])
    assert validate.t04_beats(run, t)[0] is ok


@pytest.mark.parametrize("xs", [[590, 20, 30], [590, 590, 590, 590, 20]])
def test_a_late_time_on_early_lines_does_not_excuse_the_lines_after_them(xs):
    """It would under a running maximum: moving one `t` forward would leave the whole stream uncommitted."""
    run = with_rolls(run_of(every_10s(600)), xs)
    t = rooted([named(run, 0, T0 + 60, only=FACTS_ONLY), named(run, 1, T0 + 300, only=FACTS_ONLY)])
    ok, why = validate.t04_beats(run, t)
    assert ok is False and "rolls.jsonl" in why


def test_a_beat_past_run_end_is_not_held_to_it():
    run = with_rolls(run_of(every_10s(600)), [590])
    t = rooted([named(run, 0, T0 + 300, only=FACTS_ONLY), named(run, 1, T0 + 900, only=FACTS_ONLY)])
    assert validate.t04_beats(run, t) == (True, "")


def test_beats_that_cover_no_line_and_no_end_fail_t04():
    run = with_rolls(run_of(every_10s(600)), [10])
    ok, why = validate.t04_beats(run, rooted([named(run, 0, T0 + 60, only=())]))
    assert ok is False and "no beat or end covers a line" in why
