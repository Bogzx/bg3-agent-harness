# pyright: basic
"""T01-T07 on whole bundles: a ticketed copy of test_submission_validate's good runs, with the beats the server would
have stored for them, against each way of tampering; failing closed; and a release without tickets staying as it was.
T05-T07's limits on small hand-made runs are test_ticket_hash.py's."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from test_submission_pack import mp4
from test_submission_validate import (
    FIGHT,
    GOLD,
    NOT_TICKET_CODES,
    TICKET_CODES,
    VIDEO,
    ctx_for,
    edit_json,
    env_of,
    good_run,
    jpeg,
    jsonl,
    rehash,
    release_for,
)

from bg3bench.submission import bundle, codes, pack, ticket_hash, validate
from bg3bench.submission.release import RELEASES

NONCE = "ab" * 32
TICKET_ID = "6f1c2a3b-0000-4000-8000-123456789abc"


def streams_of(bdir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(bdir.iterdir()) if ticket_hash.is_stream(p.name)}


def sign(bdir: Path, nonce: str = NONCE) -> None:
    """bundle.json names the ticket at the files as they are now: what pack writes, and a forger's re-sign."""
    raw, env = streams_of(bdir), env_of(bdir)
    lines = ticket_hash.counts(raw)
    env["ticket"] = {"id": TICKET_ID, "nonce": nonce, "root": ticket_hash.root_at(nonce, raw, lines), "lines": lines}
    (bdir / bundle.ENVELOPE).write_bytes(bundle.canonical_bytes(env))


def cut(data: bytes, at: float) -> int:
    """How many first lines of a stream the machine had written by its time `at`."""
    n = 0
    for line in ticket_hash.complete_lines(data):
        if json.loads(line)["t"] > at:
            break
        n += 1
    return n


def stored(raw: dict[str, bytes], a: int, s: int, machine_t: float, nonce: str = NONCE, **extra) -> dict:
    """A beat as the server stores it."""
    lines = {n: cut(d, machine_t) for n, d in raw.items()}
    return {"s": s, "a": a, "l": lines, "r": ticket_hash.root_at(nonce, raw, lines), **extra}


def server_ticket(bdir: Path, nonce: str = NONCE, end: bool = True) -> dict:
    """What the site stores for the honest run in bdir: a beat a minute over its whole length, then its end."""
    raw = streams_of(bdir)
    rows = jsonl(raw["facts.jsonl"].decode())
    t0 = next(r["t"] for r in rows if r.get("mark") == "run_start")
    t1 = next(r["t"] for r in rows if r.get("mark") == "run_end")
    beats = [stored(raw, int(t0) + x, i, int(t0) + x, nonce) for i, x in enumerate(range(60, int(t1 - t0), 60))]
    if end:
        lines = ticket_hash.counts(raw)
        beats.append(
            {"s": len(beats), "a": int(t1) + 30, "l": lines, "r": ticket_hash.root_at(nonce, raw, lines), "e": 1}
        )
    return {
        "id": TICKET_ID,
        "nonce": nonce,
        "issued_at": int(t0) - 30,
        "expires_at": int(t0) + 6 * 3600,
        "status": "ended" if end else "open",
        "beats": beats,
    }


@pytest.fixture(params=[GOLD, FIGHT])
def ticketed(request, tmp_path) -> tuple[Path, validate.Release, dict]:
    d = good_run(tmp_path, request.param)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    ticket = server_ticket(bdir)
    sign(bdir)
    return bdir, dataclasses.replace(release_for(d), tickets={"version": 1}), ticket


def job(bdir: Path, release: validate.Release, ticket: dict | None) -> validate.Verdict:
    return validate.validate(bdir, release, where="job", ctx=ctx_for(env_of(bdir)), ticket=ticket)


def ok_of(v: validate.Verdict, code: str) -> bool | None:
    return next(r.ok for r in v.checks if r.code == code)


def detail_of(v: validate.Verdict, code: str) -> str:
    return next(r.detail for r in v.checks if r.code == code)


def edit_line(bdir: Path, name: str, i: int) -> None:
    """Move one line's time by a millisecond: an edit nothing else notices."""
    lines = (bdir / name).read_bytes().split(b"\n")
    row = json.loads(lines[i])
    row["t"] += 0.001
    lines[i] = json.dumps(row, separators=(",", ":")).encode()
    (bdir / name).write_bytes(b"\n".join(lines))


def test_a_ticketed_run_is_accepted(ticketed):
    bdir, release, ticket = ticketed
    v = job(bdir, release, ticket)
    assert v.accepted, [(r.code, r.detail) for r in v.checks if r.ok is not True]
    assert [ok_of(v, c) for c in TICKET_CODES] == [True, None, True, True, True, True, True]  # T02 is the Worker's
    assert [r.code for r in v.checks] == [c.code for c in codes.CHECKS]
    assert v.flags == []


def test_the_panel_reports_what_it_can_and_null_for_the_servers_data(ticketed):
    bdir, release, _ = ticketed
    v = validate.validate(bdir, release, where="panel")
    assert v.accepted
    assert {c: ok_of(v, c) for c in ("T01", "T03", "T04", "T05", "T06", "T07")} == {
        "T01": True,
        "T03": True,
        "T04": None,
        "T05": None,
        "T06": None,
        "T07": None,
    }
    assert "T02" not in {r.code for r in v.checks}


def test_a_run_without_a_ticket_fails_t01_and_t03(tmp_path):
    d = good_run(tmp_path, FIGHT)
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": 432.6}).bundle_dir
    release = dataclasses.replace(release_for(d), tickets={"version": 1})
    panel = validate.validate(bdir, release, where="panel")
    assert not panel.accepted and {"T01", "T03"} <= set(panel.failed)
    v = job(bdir, release, None)
    assert {"T01", "T03", "T04", "T05", "T06"} <= set(v.failed)


def test_a_malformed_ticket_is_rejected_by_the_schema(ticketed):
    bdir, release, ticket = ticketed
    edit_json(bdir, bundle.ENVELOPE, lambda e: e["ticket"].update(nonce="short"))
    (bdir / bundle.ENVELOPE).write_bytes(bundle.canonical_bytes(env_of(bdir)))
    v = job(bdir, release, ticket)
    assert "F03" in v.failed and "ticket/nonce" in detail_of(v, "F03")


def test_a_line_edited_before_a_beat_fails_t04(ticketed):
    bdir, release, ticket = ticketed
    edit_line(bdir, "S001.events.jsonl", 3)
    rehash(bdir)
    sign(bdir)  # T03 is happy: only the server's beats still hold the old line
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False
    assert "doesn't recompute" in detail_of(v, "T04")


def test_a_line_edited_after_the_last_beat_fails_t03(ticketed):
    bdir, release, ticket = ticketed
    edit_line(bdir, "S001.events.jsonl", -2)  # session_end; the file's last entry after the split is empty
    rehash(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is False and "root doesn't recompute" in detail_of(v, "T03")


def test_a_line_edited_after_the_last_beat_and_signed_again_fails_on_the_end(ticketed):
    bdir, release, ticket = ticketed
    edit_line(bdir, "S001.events.jsonl", -2)
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False


def test_a_ticket_with_no_end_fails_t04_so_skipping_it_leaves_no_line_editable(tmp_path):
    """With no end stored, nothing on the server holds the lines after the last beat: this edit passes every other check."""
    d = good_run(tmp_path, FIGHT)
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": 432.6}).bundle_dir
    ticket = server_ticket(bdir, end=False)
    edit_line(bdir, "S001.events.jsonl", -2)
    rehash(bdir)
    sign(bdir)
    v = job(bdir, dataclasses.replace(release_for(d), tickets={"version": 1}), ticket)
    assert ok_of(v, "T04") is False and "no end" in detail_of(v, "T04") and not v.accepted
    assert v.failed == ["T04"]


def left_out(bdir: Path, ticket: dict, *names: str) -> dict:
    """A modified client's beats: on time, over the honest lines, without these streams and with no end."""
    raw = streams_of(bdir)
    beats = []
    for b in (x for x in ticket["beats"] if not x.get("e")):
        lines = {n: c for n, c in b["l"].items() if n not in names}
        beats.append({**b, "l": lines, "r": ticket_hash.root_at(NONCE, raw, lines)})
    return {**ticket, "beats": beats, "status": "open"}


def end_after(bdir: Path, ticket: dict) -> dict:
    """The end sent once the files were edited: it commits the edit."""
    raw, last = streams_of(bdir), ticket["beats"][-1]
    lines = ticket_hash.counts(raw)
    end = {"s": last["s"] + 1, "a": last["a"] + 30, "l": lines, "r": ticket_hash.root_at(NONCE, raw, lines), "e": 1}
    return {**ticket, "beats": [*ticket["beats"], end], "status": "ended"}


def test_facts_left_out_of_the_beats_and_edited_fail_t04(ticketed):
    bdir, release, ticket = ticketed
    forged = left_out(bdir, ticket, ticket_hash.FACTS)
    edit_line(bdir, ticket_hash.FACTS, 3)
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, forged)  # sent no end: the next test sends one
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False and not v.accepted


def test_facts_left_out_with_an_end_sent_after_the_edit_fail_t04(ticketed):
    bdir, release, ticket = ticketed
    forged = left_out(bdir, ticket, ticket_hash.FACTS)
    edit_line(bdir, ticket_hash.FACTS, 3)
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, end_after(bdir, forged))
    assert ok_of(v, "T04") is False and not v.accepted
    assert ticket_hash.FACTS in detail_of(v, "T04")


def test_empty_beats_and_one_end_after_the_edits_fail_t04(ticketed):
    bdir, release, ticket = ticketed
    forged = left_out(bdir, ticket, *streams_of(bdir))
    assert {tuple(b["l"]) for b in forged["beats"]} == {()}
    edit_line(bdir, ticket_hash.FACTS, 3)
    edit_line(bdir, "S001.events.jsonl", 3)
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, end_after(bdir, forged))
    assert ok_of(v, "T04") is False and not v.accepted
    assert ticket_hash.FACTS in detail_of(v, "T04") and "S001.events.jsonl" in detail_of(v, "T04")


def test_facts_held_back_and_packed_before_run_end_fail_t04(ticketed):
    """pQ's N1: each moved line looks too new to be owed, so only what the server holds can show it."""
    bdir, release, ticket = ticketed
    raw = streams_of(bdir)
    beats = []
    for b in (x for x in ticket["beats"] if not x.get("e")):
        lines = {**b["l"], ticket_hash.FACTS: min(b["l"][ticket_hash.FACTS], 2)}
        beats.append({**b, "l": lines, "r": ticket_hash.root_at(NONCE, raw, lines)})
    rows = jsonl(raw[ticket_hash.FACTS].decode())
    last = next(r["t"] for r in rows if r.get("mark") == "run_end") - 1
    moved = [json.dumps({**r, "t": max(r["t"], last)}, separators=(",", ":")).encode() for r in rows[2:]]
    (bdir / ticket_hash.FACTS).write_bytes(b"\n".join([*raw[ticket_hash.FACTS].split(b"\n")[:2], *moved, b""]))
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, end_after(bdir, {**ticket, "beats": beats, "status": "open"}))
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False and not v.accepted
    assert "committed facts" in detail_of(v, "T04")


def test_a_line_removed_fails_t04(ticketed):
    bdir, release, ticket = ticketed
    lines = (bdir / "S001.events.jsonl").read_bytes().split(b"\n")
    del lines[10]
    (bdir / "S001.events.jsonl").write_bytes(b"\n".join(lines))
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False


def test_a_stream_dropped_fails_t03_and_signed_again_t04(ticketed):
    bdir, release, ticket = ticketed
    (bdir / "rolls.jsonl").unlink()
    rehash(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is False and "rolls.jsonl" in detail_of(v, "T03")
    sign(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False
    assert "rolls.jsonl" in detail_of(v, "T04") and "the file has 0" in detail_of(v, "T04")


def test_a_valid_line_with_no_newline_fails_t03(ticketed):
    """The graders would read it and no hash would count it."""
    bdir, release, ticket = ticketed
    facts_rows = (bdir / "facts.jsonl").read_bytes().split(b"\n")
    with (bdir / "facts.jsonl").open("ab") as f:
        f.write(facts_rows[-2])
    rehash(bdir)
    sign(bdir)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is False and "after the last complete line of facts.jsonl" in detail_of(v, "T03")


def test_a_beats_root_changed_fails_t04(ticketed):
    bdir, release, ticket = ticketed
    ticket["beats"][2]["r"] = "0" * 64
    v = job(bdir, release, ticket)
    assert ok_of(v, "T04") is False and "beat 2" in detail_of(v, "T04")


def test_counts_falling_fail_t04(ticketed):
    bdir, release, ticket = ticketed
    ticket["beats"][4] |= {"l": ticket["beats"][1]["l"], "r": ticket["beats"][1]["r"]}
    v = job(bdir, release, ticket)
    assert ok_of(v, "T04") is False and "fell" in detail_of(v, "T04")


def test_a_stream_gone_from_a_later_beat_is_a_count_falling(ticketed):
    bdir, release, ticket = ticketed
    raw = streams_of(bdir)
    b = ticket["beats"][4]
    lines = {n: c for n, c in b["l"].items() if n != "facts.jsonl"}
    b |= {"l": lines, "r": ticket_hash.root_at(NONCE, raw, lines)}
    v = job(bdir, release, ticket)
    assert ok_of(v, "T04") is False and "fell in facts.jsonl" in detail_of(v, "T04")


def test_a_count_past_the_file_fails_t04(ticketed):
    bdir, release, ticket = ticketed
    ticket["beats"][-2]["l"] = ticket["beats"][-2]["l"] | {"facts.jsonl": 100_000}
    v = job(bdir, release, ticket)
    assert ok_of(v, "T04") is False and "100000 lines counted" in detail_of(v, "T04")


def test_the_wrong_nonce_fails_t04(ticketed):
    """A bundle hashed under a nonce of its own is consistent with itself (T03) and not with the server."""
    bdir, release, ticket = ticketed
    sign(bdir, nonce="cd" * 32)
    v = job(bdir, release, ticket)
    assert ok_of(v, "T03") is True and ok_of(v, "T04") is False
    assert "isn't the server's" in detail_of(v, "T04")


def test_beats_made_before_the_ticket_existed_fail_t04(ticketed):
    """Beats hashed under another nonce: nothing can be committed before the server gives the nonce out."""
    bdir, release, _ = ticketed
    v = job(bdir, release, server_ticket(bdir, nonce="cd" * 32) | {"nonce": NONCE})
    assert ok_of(v, "T04") is False and "doesn't recompute" in detail_of(v, "T04")


def test_an_end_that_is_not_the_bundles_fails_t04(ticketed):
    bdir, release, ticket = ticketed
    raw = streams_of(bdir)
    end = ticket["beats"][-1]
    lines = {**end["l"], "facts.jsonl": end["l"]["facts.jsonl"] - 1}
    end |= {"l": lines, "r": ticket_hash.root_at(NONCE, raw, lines)}
    v = job(bdir, release, ticket)
    assert ok_of(v, "T04") is False and "end's root" in detail_of(v, "T04")


def test_pruned_beats_fail_t04(ticketed):
    bdir, release, ticket = ticketed
    v = job(bdir, release, ticket | {"beats": []})
    assert ok_of(v, "T04") is False and "no beats" in detail_of(v, "T04")
    assert ok_of(v, "T05") is False and ok_of(v, "T06") is False


def test_times_fail_t05_on_a_whole_bundle(ticketed):
    bdir, release, ticket = ticketed
    v = job(bdir, release, ticket | {"issued_at": ticket["issued_at"] + 3600})
    assert not v.accepted and v.failed == ["T05"]


def test_a_gap_flags_and_the_run_stays_accepted(tmp_path):
    d = good_run(tmp_path, GOLD)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    ticket = server_ticket(bdir)
    sign(bdir)
    ticket["beats"] = [b for b in ticket["beats"] if not 3 <= b["s"] <= 9]  # 8 minutes lost
    v = job(bdir, dataclasses.replace(release_for(d), tickets={"version": 1}), ticket)
    assert v.accepted and v.failed == ["T07"] and v.flags == ["beat_gaps"]


@pytest.mark.parametrize("what", ["none", "found false", "missing file"])
def test_the_job_fails_closed_without_the_servers_ticket(ticketed, tmp_path, what):
    bdir, release, _ = ticketed
    path = tmp_path / "ticket.json"
    if what == "found false":
        path.write_text('{"found": false}')
    ticket = None if what == "none" else validate.read_ticket(path)
    assert ticket is None
    v = job(bdir, release, ticket)
    assert not v.accepted
    assert [ok_of(v, c) for c in ("T04", "T05", "T06")] == [False, False, False]
    assert ok_of(v, "T07") is None and ok_of(v, "T02") is None


def test_the_validator_cli_reads_the_ticket_file(ticketed, tmp_path, monkeypatch, capsys):
    bdir, release, ticket = ticketed
    monkeypatch.setattr(validate.Release, "from_json", classmethod(lambda cls, doc: release))
    (tmp_path / "release.json").write_text("{}")
    path = tmp_path / "ticket.json"
    path.write_text(json.dumps(ticket))
    argv = [str(bdir), "--release", str(tmp_path / "release.json"), "--where", "job", "--ticket", str(path)]
    assert validate.main(argv) == 0
    assert {c["code"]: c["ok"] for c in json.loads(capsys.readouterr().out)["checks"]}["T04"] is True
    path.write_text('{"found": false}')
    assert validate.main(argv) == 1
    assert {c["code"]: c["ok"] for c in json.loads(capsys.readouterr().out)["checks"]}["T04"] is False
    assert validate.main(argv[:-2]) == 1  # the job's flag left out: it fails closed all the same
    capsys.readouterr()


def test_a_release_without_tickets_runs_no_t_checks(tmp_path):
    """A released suite's verdict is what it was: no T code, whatever the bundle holds."""
    d = good_run(tmp_path, GOLD)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    release = release_for(d)
    before = job(bdir, release, None)
    assert [r.code for r in before.checks] == NOT_TICKET_CODES and before.accepted
    panel = validate.validate(bdir, release, where="panel")
    assert not {r.code for r in panel.checks} & set(TICKET_CODES)
    sign(bdir)
    after = job(bdir, release, None)
    assert [(r.code, r.ok) for r in after.checks] == [(r.code, r.ok) for r in before.checks]


@pytest.mark.parametrize("where", ["panel", "job"])
@pytest.mark.parametrize("how", ["a file gone", "a file changed"])
def test_an_early_rejection_on_a_release_without_tickets_names_no_t_code(tmp_path, where, how):
    """_skip_rest writes a null row per check still to run: a released suite's verdict has no T row there either."""
    d = good_run(tmp_path, GOLD)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    if how == "a file gone":
        (bdir / ticket_hash.ROLLS).unlink()
    else:
        (bdir / ticket_hash.ROLLS).write_bytes(b"{}\n")
    v = validate.validate(bdir, release_for(d), where=where)
    assert not v.accepted and not {r.code for r in v.checks} & set(TICKET_CODES)


def test_the_shipped_bench_v011_release_has_no_tickets_and_no_t_checks(tmp_path):
    doc = json.loads((RELEASES / "bench-v0.11.json").read_text(encoding="utf-8"))
    assert "tickets" not in doc
    release = validate.Release.from_json(doc)
    assert release.tickets is None
    d = good_run(tmp_path, FIGHT)
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1)).bundle_dir
    v = validate.validate(bdir, release, where="job")
    assert not {r.code for r in v.checks} & set(TICKET_CODES)
    assert [r.code for r in v.checks] == NOT_TICKET_CODES


# ---------------------------------------------------------------------------------------------------------- pack


def run_with_ticket(tmp_path: Path, name: str = FIGHT) -> Path:
    d = good_run(tmp_path, name)
    (d / pack.TICKET).write_text(json.dumps({"id": TICKET_ID, "nonce": NONCE, "issued_at": 1, "expires_at": 2}))
    return d


def test_pack_names_the_ticket_and_recomputes_it_from_the_scrubbed_files(tmp_path):
    d = run_with_ticket(tmp_path)
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), ticket_required=True)
    raw = streams_of(p.bundle_dir)
    lines = ticket_hash.counts(raw)
    assert p.envelope["ticket"] == {
        "id": TICKET_ID,
        "nonce": NONCE,
        "root": ticket_hash.root_at(NONCE, raw, lines),
        "lines": lines,
    }
    assert pack.TICKET not in {f["name"] for f in p.envelope["files"]}
    assert not (p.bundle_dir / pack.TICKET).exists()
    assert (tmp_path / "out" / pack.TICKET).is_file()  # beside the bundle dir: the panel's T03 reads the run's end
    release = dataclasses.replace(release_for(d), tickets={"version": 1})
    v = validate.validate(p.bundle_dir, release, where="panel", submission=False)
    assert ok_of(v, "F03") is True and ok_of(v, "T01") is True and ok_of(v, "T03") is True


@pytest.mark.parametrize("ticket", [None, "not json", "[]", '{"id": "only an id"}'])
def test_pack_refuses_a_run_without_a_ticket_when_the_suite_requires_one(tmp_path, ticket):
    d = good_run(tmp_path, FIGHT)
    if ticket:
        (d / pack.TICKET).write_text(ticket)
    with pytest.raises(pack.PackError, match="takes only runs played with a ticket"):
        pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), ticket_required=True)
    assert not (tmp_path / "out").exists()


def test_pack_leaves_the_ticket_out_for_a_suite_that_has_none(tmp_path):
    """A validator from before tickets, at that suite's own commit, would reject the key (F03)."""
    d = run_with_ticket(tmp_path)
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), ticket_required=False)
    assert "ticket" not in p.envelope and not (tmp_path / "out" / pack.TICKET).exists()


def test_requires_ticket_reads_the_suites_shipped_release_file(tmp_path, monkeypatch):
    assert pack.requires_ticket("bench-v0.11") is False
    assert pack.requires_ticket("bench-v99.0") is False
    (tmp_path / "bench-v9.9.json").write_text('{"tickets": {"version": 1}}')
    (tmp_path / "bench-v9.8.json").write_text("not json")
    assert pack.requires_ticket("bench-v9.9", tmp_path) is True
    assert pack.requires_ticket("bench-v9.8", tmp_path) is False
    d = run_with_ticket(tmp_path)  # a v0.8 fixture run: the default asks its suite's file
    monkeypatch.setattr(pack, "RELEASES", tmp_path)
    assert "ticket" not in pack.pack(d, tmp_path / "out1", attempt=pack.Attempt(1)).envelope
    (tmp_path / "bench-v0.8.json").write_text('{"tickets": {"version": 1}}')
    assert "ticket" in pack.pack(d, tmp_path / "out2", attempt=pack.Attempt(1)).envelope


def test_the_ticket_stays_through_the_render_and_the_video(tmp_path):
    d = run_with_ticket(tmp_path)
    render = json.loads((d / "render.json").read_text())
    (d / "render.json").unlink()
    (d / bundle.FRAMES_TAR).unlink()
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), ticket_required=True)
    rdir = tmp_path / "render"
    (rdir / "frames").mkdir(parents=True)
    (rdir / "render.json").write_text(json.dumps(render | {"run_hash": p.envelope["run_hash"]}))
    (rdir / "frames" / "0.jpg").write_bytes(jpeg())
    q = pack.add_render(rdir, tmp_path / "out")
    assert q.envelope["ticket"] == p.envelope["ticket"]
    (rdir / pack.VIDEO).write_bytes(mp4(render["video_end"] - render["video_start"]))
    assert pack.add_video(rdir, tmp_path / "out", "dQw4w9WgXcQ").envelope["ticket"] == p.envelope["ticket"]


def test_t03_compares_the_end_the_run_reported(ticketed):
    bdir, release, _ = ticketed
    root = env_of(bdir)["ticket"]["root"]
    same = validate.validate(bdir, release, where="panel", local_end={"root": root})
    assert ok_of(same, "T03") is True
    other = validate.validate(bdir, release, where="panel", local_end={"root": "0" * 64})
    assert ok_of(other, "T03") is False and ".env value" in detail_of(other, "T03")
