# pyright: basic
"""bg3bench.submission.validate: good bundles are accepted; each tampered copy is rejected by its own check code.

The good bundles are the committed pilot fixtures repacked as a run of the fixed harness would write them: full
pins, the start save's sha1, deaths from the facts (the pilot's harness wrote 0), a video, render.json and the
contact sheet. A tampered copy is re-hashed like a careful forger would (bundle.json lists the edited files), so
each tamper reaches the check meant to catch it.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from bg3bench import facts
from bg3bench.submission import bundle, codes, consistency, pack, validate

FIXTURES = Path(__file__).parent / "fixtures" / "submission"
GOLD, FIGHT = "act3_gold_max", "fight_act2_balthazar"
PINS = {"game_build": "20981537", "bg3le_sha256": "a" * 64, "mod_sha1": "b" * 40}
# The bg3 tools a rules run was served: the server's list minus the memory, session, reload and wiki tools.
OFF = {
    *("memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append"),
    *("note", "recall", "session_start", "end_session", "load_game", "wiki_search", "wiki_page"),
}
HOME = "/home/" + "someone/.claude/shot.jpg"  # built at run time: tools/ci/scrub.py scans this file


def jsonl(data: str) -> list[dict]:
    return [json.loads(ln) for ln in data.splitlines() if ln.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\0" * 64 + b"\xff\xd9"


def frames_tar(n: int) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for i in range(n):
            info = tarfile.TarInfo(f"frames/{i}.jpg")
            info.size = len(jpeg())
            tar.addfile(info, io.BytesIO(jpeg()))
    return out.getvalue()


def good_run(tmp: Path, name: str) -> Path:
    """A run dir as the fixed harness and the renderer would leave it, from a committed fixture bundle."""
    d = tmp / f"run-{name}"
    d.mkdir()
    with tarfile.open(FIXTURES / f"{name}.bundle.tar.gz") as tar:
        tar.extractall(d, filter="data")
    (d / bundle.ENVELOPE).unlink()
    man, res = json.loads((d / "manifest.json").read_text()), json.loads((d / "result.json").read_text())
    sc = pack.load_scenarios()[man["scenario"]["name"]]
    man["pins"] |= {
        "game_build": PINS["game_build"],
        "bg3le": {"sha256": PINS["bg3le_sha256"]},
        "mod": {"sha1": PINS["mod_sha1"]},
        "repo": {"commit": "c" * 40, "dirty": False, "diff": None},
    }
    man["scenario"]["start_save_sha1"] = sc["start"]["save_sha1"]
    timeline = facts.load(d / "facts.jsonl")
    losses = facts.party_losses(timeline)
    res["deaths"] = {k: int(losses.get(k) or 0) for k in ("fights", "downs", "deaths", "wipes")}
    (d / "manifest.json").write_text(json.dumps(man, indent=1, sort_keys=True))
    (d / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True))
    evs = [(p.name, e) for p in sorted(d.glob("*.events.jsonl")) for e in jsonl(p.read_text())]
    span, t0 = res["wall_s"], evs[0][1]["t"]
    marks = [{"file": n, "seq": e["seq"], "at_s": round(e["t"] - t0, 2)} for n, e in evs[:20]]
    run_hash = bundle.run_hash({p.name: p.read_bytes() for p in d.iterdir() if p.is_file()})
    render = {"schema": 1, "run_hash": run_hash, "video_start": t0, "video_end": t0 + span, "fps": 15, "events": marks}
    (d / "render.json").write_text(json.dumps(render))
    (d / bundle.FRAMES_TAR).write_bytes(frames_tar(round(span / validate.FRAME_EVERY_S)))
    return d


def release_for(d: Path) -> validate.Release:
    man = json.loads((d / "manifest.json").read_text())
    names = sorted({t["name"] for t in json.loads((d / "bg3-tools.json").read_text())} - OFF)
    sc = pack.load_scenarios()[man["scenario"]["name"]]
    return validate.Release(
        suite=man["versions"]["suite"],
        versions={"harness": man["versions"]["harness"], "bench": man["versions"]["bench"]},
        pins=PINS,
        scenarios={
            sc["name"]: {
                "digest": man["scenario"]["digest"],  # the suite the pilot played (files edited since)
                "save_sha1": sc["start"]["save_sha1"],
                "fingerprint": consistency.fingerprint(facts.load(d / "facts.jsonl")),
            }
        },
        conditions={
            "rules": {
                "tools": {"allowlist_sha256": man["tools"]["allowlist_sha256"], "names": names},
                "prompts": {"pi": {"sha256": man["prompt"]["sha256"]}},
            }
        },
    )


VIDEO = {"youtube_id": "dQw4w9WgXcQ", "duration_s": 0, "mp4_sha256": None}


def ctx_for(env: dict, duplicate: str | None = None) -> validate.Context:
    return validate.Context(
        duplicate=lambda sha, run_id: duplicate,
        near_copy=lambda seq: None,
        reference=lambda cell: None,
        oembed=lambda vid: {"title": f"{env['scenario']} · {env['model']['slug']} · {env['run_id']}"},
        duration=lambda vid: env["video"]["duration_s"],
    )


@pytest.fixture(params=[GOLD, FIGHT])
def good(request, tmp_path) -> tuple[Path, validate.Release]:
    d = good_run(tmp_path, request.param)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall})
    return p.bundle_dir, release_for(d)


def env_of(bdir: Path) -> dict:
    return json.loads((bdir / bundle.ENVELOPE).read_text())


def run(bdir: Path, release: validate.Release, duplicate: str | None = None) -> validate.Verdict:
    return validate.validate(bdir, release, where="job", ctx=ctx_for(env_of(bdir), duplicate))


def test_good_bundles_are_accepted(good):
    bdir, release = good
    v = run(bdir, release)
    assert v.accepted, [(r.code, r.detail) for r in v.checks if r.ok is not True]
    not_run = {r.code for r in v.checks if r.ok is None}
    assert not_run == {"X07"}  # no verified cell for the fixtures
    assert [r.code for r in v.checks] == [c.code for c in codes.CHECKS if c.code in {r.code for r in v.checks}]
    assert v.bundle_sha256 == bundle.sha256_bytes((bdir / bundle.ENVELOPE).read_bytes())
    doc = v.as_json()
    assert doc["accepted"] and doc["flags"] == [] and len(doc["checks"]) == len(codes.CHECKS)


def test_panel_runs_its_checks_offline(good):
    bdir, release = good
    v = validate.validate(bdir, release, where="panel")
    assert v.accepted
    codes_run = {r.code for r in v.checks}
    assert codes_run == {c.code for c in codes.CHECKS if "panel" in c.where}
    assert {r.code for r in v.checks if r.ok is None} == {"E01"}  # no YouTube lookup injected


def test_draft_bundle_before_rendering(tmp_path):
    d = good_run(tmp_path, FIGHT)
    (d / "render.json").unlink()
    (d / bundle.FRAMES_TAR).unlink()
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1))
    v = validate.validate(p.bundle_dir, release_for(d), where="panel", submission=False)
    assert v.accepted and {r.code for r in v.checks if r.ok is None} == {"E01", "E03", "E04"}
    v = validate.validate(p.bundle_dir, release_for(d), where="panel")
    assert not v.accepted and {"E01", "E03", "E04"} <= set(v.failed)


def test_render_from_the_packed_bundle_then_added(tmp_path):
    """The order the panel runs: pack, render from the packed bundle with its run_hash, add render.json and the
    frames (tarred by pack), then the whole bundle validates."""
    d = good_run(tmp_path, FIGHT)
    render = json.loads((d / "render.json").read_text())
    (d / "render.json").unlink()
    (d / bundle.FRAMES_TAR).unlink()
    p = pack.pack(
        d,
        tmp_path / "out",
        attempt=pack.Attempt(1),
        video=VIDEO | {"duration_s": render["video_end"] - render["video_start"]},
    )
    rdir = tmp_path / "render"
    (rdir / "frames").mkdir(parents=True)
    (rdir / "render.json").write_text(json.dumps(render | {"run_hash": p.envelope["run_hash"]}))
    n = round((render["video_end"] - render["video_start"]) / validate.FRAME_EVERY_S)
    for i in range(n):
        (rdir / "frames" / f"{i}.jpg").write_bytes(jpeg())
    q = pack.add_render(rdir, tmp_path / "out")
    assert q.envelope["run_hash"] == p.envelope["run_hash"] and q.bundle_sha256 != p.bundle_sha256
    with tarfile.open(q.bundle_dir / bundle.FRAMES_TAR) as tar:
        assert tar.getnames() == [f"frames/{i}.jpg" for i in range(n)]
    with tarfile.open(q.archive) as tar:
        assert sorted(tar.getnames()) == sorted(x.name for x in q.bundle_dir.iterdir())
    v = run(q.bundle_dir, release_for(d))
    assert v.accepted, [(r.code, r.detail) for r in v.checks if r.ok is False]
    with pytest.raises(pack.PackError, match="already holds a render"):
        pack.add_render(rdir, tmp_path / "out")


def test_add_render_refuses_another_runs_render(tmp_path):
    d = good_run(tmp_path, FIGHT)
    render = json.loads((d / "render.json").read_text())
    (d / "render.json").unlink()
    (d / bundle.FRAMES_TAR).unlink()
    pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1))
    rdir = tmp_path / "render"
    (rdir / "frames").mkdir(parents=True)
    (rdir / "render.json").write_text(json.dumps(render | {"run_hash": "0" * 64}))
    with pytest.raises(pack.PackError, match="another run_hash"):
        pack.add_render(rdir, tmp_path / "out")
    (rdir / "render.json").write_text(json.dumps(render))
    (rdir / "frames" / "cover.png").write_bytes(b"x")
    with pytest.raises(pack.PackError, match="not frames"):
        pack.add_render(rdir, tmp_path / "out")


def rehash(bdir: Path, render: bool = True) -> None:
    """bundle.json made to list the files as they are now, with render.json's run_hash to match (a forger's
    re-hash; the video's strip would still show the old run hash)."""
    env = env_of(bdir)
    files = {p.name: p.read_bytes() for p in bdir.iterdir() if p.name != bundle.ENVELOPE}
    if render and "render.json" in files:
        edit_json(bdir, "render.json", lambda r: r.update(run_hash=bundle.run_hash(files)))
    env["files"] = [
        {"name": p.name, "sha256": bundle.sha256_bytes(p.read_bytes()), "bytes": p.stat().st_size}
        for p in sorted(bdir.iterdir())
        if p.name != bundle.ENVELOPE
    ]
    env["run_hash"] = bundle.run_hash({p.name: p.read_bytes() for p in bdir.iterdir() if p.name != bundle.ENVELOPE})
    (bdir / bundle.ENVELOPE).write_bytes(bundle.canonical_bytes(env))


def edit_json(bdir: Path, name: str, fn: Callable[[dict], None]) -> None:
    doc = json.loads((bdir / name).read_text())
    fn(doc)
    (bdir / name).write_text(json.dumps(doc, indent=1, sort_keys=True))


def edit_facts(bdir: Path, fn: Callable[[list[dict]], None]) -> None:
    rows = jsonl((bdir / "facts.jsonl").read_text())
    fn(rows)
    write_jsonl(bdir / "facts.jsonl", rows)


def add_event(bdir: Path, **ev) -> None:
    rows = jsonl((bdir / "S001.events.jsonl").read_text())
    last = rows[-2]  # before session_end
    rows.insert(-1, {"v": 1, "seq": last["seq"], "t": last["t"], "src": "client"} | ev)
    for i, r in enumerate(rows):
        r["seq"] = i
    write_jsonl(bdir / "S001.events.jsonl", rows)


def more_gold(rows: list[dict]) -> None:
    for snap in [r for r in rows if r["kind"] == "snapshot"][1:]:  # the start state stays as published
        snap["gold"] += 5000


def bad_start(rows: list[dict]) -> None:
    snap = next(r for r in rows if r["kind"] == "snapshot")
    snap["party"][0]["level"] += 1


def pins(m: dict) -> None:
    m["pins"]["game_build"] = "1"


def prompt(m: dict) -> None:
    m["prompt"]["sha256"] = "0" * 64


def schema(r: dict) -> None:
    del r["valid"]


def oversize(bdir: Path) -> None:
    with (bdir / "rolls.jsonl").open("a") as f:
        f.write(" " * (bundle.MAX_FILE_BYTES + 1))


TAMPERS: dict[str, tuple[str, Callable[[Path], None]]] = {
    "edited gold": ("G01", lambda b: edit_facts(b, more_gold)),
    "missing file": ("F01", lambda b: (b / "facts.jsonl").unlink()),
    "home path": ("S01", lambda b: add_event(b, kind="text", text=f"[Image: source: {HOME}]")),
    "wrong pins": ("P02", lambda b: edit_json(b, "manifest.json", pins)),
    "wrong prompt hash": ("C01", lambda b: edit_json(b, "manifest.json", prompt)),
    "duplicate run id": ("D01", lambda b: None),
    "bad start snapshot": ("X01", lambda b: edit_facts(b, bad_start)),
    "extra tool": ("A02", lambda b: add_event(b, kind="tool_call", tool="teleport", args={}, call_id="x")),
    "reload without the condition": ("A02", lambda b: add_event(b, kind="tool_call", tool="load_game", args={})),
    "hidden reasoning": ("S02", lambda b: add_event(b, kind="thinking", text="secret plan")),
    "oversize": ("F01", oversize),
    "bad schema": ("F03", lambda b: edit_json(b, "result.json", schema)),
}


@pytest.mark.parametrize("what", sorted(TAMPERS))
def test_tampered_copies_are_rejected_by_their_code(tmp_path, what):
    d = good_run(tmp_path, GOLD)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    code, tamper = TAMPERS[what]
    tamper(bdir)
    if what != "missing file":
        rehash(bdir)
    v = run(bdir, release_for(d), duplicate="an accepted submission has this run id" if code == "D01" else None)
    assert not v.accepted
    assert code in v.failed, v.failed
    if what in ("edited gold", "wrong pins", "duplicate run id", "bad start snapshot", "extra tool"):
        assert v.failed == [code]


def test_render_for_another_run_fails_e03(good):
    bdir, release = good
    edit_json(bdir, "render.json", lambda r: r.update(run_hash="0" * 64))
    rehash(bdir, render=False)
    assert run(bdir, release).failed == ["E03"]


def test_render_event_names_its_log(good):
    bdir, release = good
    edit_json(bdir, "render.json", lambda r: r["events"][0].update(file="S001-c9.events.jsonl"))
    rehash(bdir)
    assert run(bdir, release).failed == ["E03"]


def test_stale_run_hash_fails_f02(good):
    bdir, release = good
    env = env_of(bdir)
    env["run_hash"] = "0" * 64
    (bdir / bundle.ENVELOPE).write_bytes(bundle.canonical_bytes(env))
    assert run(bdir, release).failed == ["F02"]


def test_unhashed_edit_fails_f02(good):
    bdir, release = good
    (bdir / "result.json").write_text((bdir / "result.json").read_text().replace('"valid": true', '"valid": true '))
    env = env_of(bdir)
    for f in env["files"]:
        if f["name"] == "result.json":
            f["bytes"] = (bdir / "result.json").stat().st_size
    (bdir / bundle.ENVELOPE).write_bytes(bundle.canonical_bytes(env))
    assert run(bdir, release).failed == ["F02"]


def test_non_canonical_envelope_fails_f02(good):
    bdir, release = good
    (bdir / bundle.ENVELOPE).write_text(json.dumps(env_of(bdir), indent=1))
    assert run(bdir, release).failed == ["F02"]


def test_extra_file_fails_f01(good):
    bdir, release = good
    (bdir / "S001.jsonl").write_text("{}\n")
    v = run(bdir, release)
    assert v.failed == ["F01"] and all(r.ok is None for r in v.checks if r.code != "F01")


def test_frames_tar_with_other_paths_fails_e04(good):
    bdir, release = good
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        info = tarfile.TarInfo("../evil.sh")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (bdir / bundle.FRAMES_TAR).write_bytes(out.getvalue())
    rehash(bdir)
    assert run(bdir, release).failed == ["E04"]


def test_flags_accept_with_a_flag(good):
    bdir, release = good
    edit_json(bdir, "result.json", lambda r: r.update(cost_usd=r["cost_usd"] * 2))
    rehash(bdir)
    v = run(bdir, release)
    assert "X05" in v.failed and v.flags == ["cost_mismatch"]


def test_pilot_bundle_reports_missing_pins(tmp_path):
    """A bundle of the pilot as written (no pins, no start save sha1, deaths 0) fails P01/P02 and G02, not F*."""
    d = tmp_path / "b"
    d.mkdir()
    with tarfile.open(FIXTURES / f"{FIGHT}.bundle.tar.gz") as tar:
        tar.extractall(d, filter="data")
    v = validate.validate(d, release_for(d), where="panel", submission=False)
    failed = set(v.failed)
    assert {"P01", "P02", "G02"} <= failed and not any(c.startswith("F") for c in failed)
    assert "missing pins" in next(r.detail for r in v.checks if r.code == "P02")


def first(rows: list[dict], kind: str, **match) -> dict:
    return next(r for r in rows if r["kind"] == kind and all(r.get(k) == v for k, v in match.items()))


def edit_events(bdir: Path, fn: Callable[[list[dict]], None]) -> None:
    rows = jsonl((bdir / "S001.events.jsonl").read_text())
    fn(rows)
    write_jsonl(bdir / "S001.events.jsonl", rows)


def insert_fact(rows: list[dict], at: int, **rec) -> None:
    rows.insert(at + 1, {"v": 1, "kind": "event", "t": rows[at]["t"]} | rec)


# Records that are valid JSON and pass the old F03 (version, kind, time order) but break what later checks read:
# each is rejected with a code, never a crash.
MALFORMED: dict[str, tuple[str, Callable[[Path], None]]] = {
    "party not a list": ("F03", lambda b: edit_facts(b, lambda rows: first(rows, "snapshot").update(party="x"))),
    "level a string": (
        "F03",
        lambda b: edit_facts(b, lambda rows: first(rows, "snapshot")["party"][0].update(level="9")),
    ),
    "gold a string": ("F03", lambda b: edit_facts(b, lambda rows: first(rows, "snapshot").update(gold="x"))),
    "snapshot without party": ("F03", lambda b: edit_facts(b, lambda rows: first(rows, "snapshot").pop("party"))),
    "event without ev": ("F03", lambda b: edit_facts(b, lambda rows: first(rows, "event").pop("ev"))),
    "mark name a list": ("F03", lambda b: edit_facts(b, lambda rows: first(rows, "mark").update(mark=["run_start"]))),
    "tokens a string": ("F03", lambda b: edit_events(b, lambda rows: first(rows, "usage").update(tokens="x"))),
    "token count a string": (
        "F03",
        lambda b: edit_events(b, lambda rows: first(rows, "usage").update(tokens={"input": "1"})),
    ),
    "cost a dict": ("F03", lambda b: edit_events(b, lambda rows: first(rows, "usage").update(cost_usd={}))),
    "tool a list": ("F03", lambda b: edit_events(b, lambda rows: first(rows, "tool_call").update(tool=["a"]))),
    "session detail a list": (
        "F03",
        lambda b: edit_events(b, lambda rows: first(rows, "session_start").update(detail=[1])),
    ),
    "NaN args": ("F03", lambda b: edit_events(b, lambda rows: first(rows, "tool_call").update(args=float("nan")))),
    "session id a list": (
        "X06",
        lambda b: edit_events(b, lambda rows: first(rows, "session_start")["detail"].update(session=["S001"])),
    ),
}


@pytest.mark.parametrize("what", sorted(MALFORMED))
def test_malformed_records_are_rejected_by_a_code(tmp_path, what):
    d = good_run(tmp_path, FIGHT)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    code, tamper = MALFORMED[what]
    tamper(bdir)
    rehash(bdir)
    v = run(bdir, release_for(d))
    assert not v.accepted and v.failed == [code], [(r.code, r.detail) for r in v.checks if r.ok is False]


def test_a_check_that_cannot_read_its_input_fails_its_code():
    def broken(b):
        return b["missing"]

    ok, detail = validate._guarded(broken, {})
    assert ok is False and detail.startswith("malformed input: KeyError")


def loaded(facts_rows: list[dict], events: list[dict]) -> validate.Loaded:
    return validate.Loaded(Path("."), {}, {}, {}, {}, facts_rows, None, {"S001.events.jsonl": events}, {}, {}, {})


def call(t: float, tool: str) -> dict:
    return {"v": 1, "kind": "tool_call", "t": t, "tool": tool, "args": {}}


def ev(t: float, name: str, **kw) -> dict:
    return {"v": 1, "kind": "event", "t": t, "ev": name} | kw


def test_x03_trades_follow_a_trade_or_dialogue_call():
    assert consistency.x03_events_vs_facts(loaded([ev(110, "trade_open")], [call(100, "trade")])) == (True, "")
    assert consistency.x03_events_vs_facts(loaded([ev(150, "trade_open")], [call(100, "choose_dialogue")]))[0]
    for calls in ([call(100, "get_status")], [call(30, "trade")], [call(120, "trade")]):
        ok, detail = consistency.x03_events_vs_facts(loaded([ev(110, "trade_open")], calls))
        assert ok is False and "trades opened" in detail
    assert consistency.x03_events_vs_facts(loaded([], [call(100, "trade")])) == (True, "")  # a failed trade call


def test_x03_party_kills_follow_a_tool_call():
    kill = ev(200, "kill", victim="Goblin", by_party=True)
    assert consistency.x03_events_vs_facts(loaded([kill], [call(100, "combat_turn")])) == (True, "")
    ok, detail = consistency.x03_events_vs_facts(loaded([kill], [call(50, "combat_turn")]))
    assert ok is False and "kills by the party" in detail
    other = ev(200, "kill", victim="Goblin", by_party=False)  # an ally's or a trap's: no call needed
    assert consistency.x03_events_vs_facts(loaded([other], [])) == (True, "")


def test_inserted_party_kill_fails_x03(tmp_path):
    d = good_run(tmp_path, GOLD)
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    bdir = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall}).bundle_dir
    edit_facts(bdir, lambda rows: insert_fact(rows, 0, ev="kill", victim="Someone", by_party=True))  # before any call
    rehash(bdir)
    v = run(bdir, release_for(d))  # G01 fails too: the scenario's goal forbids kills
    assert "X03" in v.failed and "kills by the party" in next(r.detail for r in v.checks if r.code == "X03")
