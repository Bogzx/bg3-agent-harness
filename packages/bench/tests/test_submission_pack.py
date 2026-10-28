# pyright: basic
"""bg3bench.submission.pack and .scrub: a run dir -> a scrubbed, size-capped, deterministic bundle; refusals."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import jsonschema
import pytest

from bg3bench.submission import bundle, pack, scrub

FIXTURE = Path(__file__).parent / "fixtures" / "submission" / "fight_act2_balthazar.bundle.tar.gz"
PILOT = Path("/data/Repos/ovh-e2e/runtime/sessions")  # the 2026-10-25 pilot's run dirs, where present
ID = "00000000-0000-4000-8000-000000000001"
# Personal-data shapes, built at run time so this file holds none (tools/ci/scrub.py scans the tree).
WIN, MAIL, STEAM, KEY = "C:" + "\\Users\\bob", "bob" + "@corp.io", "7656119" + "8000000042", "sk-" + "a" * 24


def run_dir(tmp_path: Path) -> Path:
    """The fixture bundle's files as a run dir (bundle.json dropped), plus files a run dir has that a bundle doesn't."""
    d = tmp_path / "run"
    d.mkdir()
    with tarfile.open(FIXTURE) as tar:
        for m in tar.getmembers():
            f = tar.extractfile(m)
            if f is not None and m.name != bundle.ENVELOPE:
                (d / m.name).write_bytes(f.read())
    (d / "S001.jsonl").write_text('{"type":"thinking","text":"hidden"}\n')
    (d / "S001.log").write_text("log /home/someone/x\n")
    (d / "S001.pi").mkdir()
    (d / "saves").mkdir()
    return d


def do_pack(d: Path, out: Path, **kw) -> pack.Packed:
    return pack.pack(d, out, attempt=pack.Attempt(2, 1), new_id=lambda: ID, now=lambda: 1793000000.0, **kw)


def edit_json(path: Path, **changes) -> None:
    doc = json.loads(path.read_text())
    doc.update(changes)
    path.write_text(json.dumps(doc))


def test_pack_fixture_run(tmp_path):
    d = run_dir(tmp_path)
    p = do_pack(d, tmp_path / "out")
    env = p.envelope
    jsonschema.Draft202012Validator(bundle.schema()).validate(env)
    names = [f["name"] for f in env["files"]]
    assert names == sorted(names)
    assert set(names) == {
        "S001.events.jsonl", "bg3-instructions.txt", "bg3-tools.json", "facts.jsonl", "manifest.json",
        "pi-system.txt", "result.json", "rolls.jsonl",
    }  # fmt: skip
    for f in env["files"]:
        data = (p.bundle_dir / f["name"]).read_bytes()
        assert (bundle.sha256_bytes(data), len(data)) == (f["sha256"], f["bytes"])
    raw = (p.bundle_dir / bundle.ENVELOPE).read_bytes()
    assert bundle.is_canonical(raw) and p.bundle_sha256 == bundle.sha256_bytes(raw)
    assert env["attempt"] == {"n": 2, "unsubmitted_valid": 1, "cell": "fight_act2_balthazar/deepseek-flash/rules"}
    assert env["cost"]["kind"] == "billed" and env["scrub"] == {"version": 1, "replacements": 0}
    assert (env["bundle_id"], env["packed"], env["video"], env["versions"]["panel"]) == (ID, 1793000000.0, None, None)
    with tarfile.open(p.archive) as tar:
        assert sorted(tar.getnames()) == sorted([*names, bundle.ENVELOPE])


def test_pack_is_deterministic_and_refuses_a_used_out_dir(tmp_path):
    d = run_dir(tmp_path)
    a, b = do_pack(d, tmp_path / "a"), do_pack(d, tmp_path / "b")
    assert a.archive.read_bytes() == b.archive.read_bytes()
    with pytest.raises(pack.PackError, match="already holds"):
        do_pack(d, tmp_path / "a")


def test_render_and_frames_go_in_when_present(tmp_path):
    d = run_dir(tmp_path)
    (d / "render.json").write_text('{"video_start": 1, "video_end": 2, "fps": 15, "events": []}')
    (d / bundle.FRAMES_TAR).write_bytes(b"\0" * 1024)
    names = [f["name"] for f in do_pack(d, tmp_path / "out").envelope["files"]]
    assert {"render.json", bundle.FRAMES_TAR} <= set(names)


@pytest.mark.parametrize(
    ("file", "changes", "why"),
    [
        ("result.json", {"pilot": ["claude reads play/CLAUDE.md"]}, "a pilot run"),
        ("result.json", {"valid": False, "invalid_reason": "harness_failure"}, "an invalid run"),
        ("manifest.json", {"versions": {"bench": "0.99.0", "harness": "0.1.0", "suite": "bench-v0.99"}}, "released"),
        ("manifest.json", {"scenario": {"name": "nope", "digest": "0" * 16}}, "not in the suite"),
    ],
)
def test_refusals(tmp_path, file, changes, why):
    d = run_dir(tmp_path)
    edit_json(d / file, **changes)
    with pytest.raises(pack.PackError, match=why):
        do_pack(d, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_holdout_refused(tmp_path):
    d = run_dir(tmp_path)
    scenarios = pack.load_scenarios()
    scenarios["fight_act2_balthazar"] = scenarios["fight_act2_balthazar"] | {"holdout": True}
    with pytest.raises(pack.PackError, match="holdout"):
        do_pack(d, tmp_path / "out", scenarios=scenarios)


def test_missing_facts_refused(tmp_path):
    d = run_dir(tmp_path)
    (d / "facts.jsonl").unlink()
    with pytest.raises(pack.PackError, match=r"missing facts\.jsonl"):
        do_pack(d, tmp_path / "out")


def test_size_caps(tmp_path, monkeypatch):
    d = run_dir(tmp_path)
    monkeypatch.setattr(bundle, "MAX_FILE_BYTES", 50_000)
    with pytest.raises(pack.PackError, match="over 50000"):
        do_pack(d, tmp_path / "out")
    assert pack.size_problems({bundle.FRAMES_TAR: b"\0" * (bundle.MAX_FRAMES_BYTES + 1)})
    assert pack.size_problems({f"{i}.json": b"" for i in range(bundle.MAX_FILES + 1)})


def test_scrub_reaches_every_file(tmp_path):
    d = run_dir(tmp_path)
    lines = [
        {
            "v": 1,
            "seq": 0,
            "t": 1.0,
            "kind": "tool_result",
            "text": "[Image: source: /home/someone/.claude/a/shot.jpg]",
        },
        {
            "v": 1,
            "seq": 1,
            "t": 2.0,
            "kind": "text",
            "text": f"saved to {WIN}\\AppData\\x.lsv, mail {MAIL}",
        },
        {"v": 1, "seq": 2, "t": 3.0, "kind": "text", "text": f"steam {STEAM} key {KEY}"},
        {"v": 1, "seq": 3, "t": 4.0, "kind": "text", "text": "my secret is hunter2hunter2"},
    ]
    (d / "S001-c1.events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    edit_json(d / "manifest.json", profile={"name": "/home/someone/bg3/profiles/p.toml", "sha256": "0" * 64})
    p = do_pack(d, tmp_path / "out", literals=["hunter2hunter2", "short"])
    assert p.envelope["scrub"]["replacements"] == 7
    for f in p.envelope["files"]:
        assert scrub.file_hits(f["name"], (p.bundle_dir / f["name"]).read_bytes(), ["hunter2hunter2"]) == []
    ev = (p.bundle_dir / "S001-c1.events.jsonl").read_text()
    assert "[Image: source: <path>/shot.jpg]" in ev and "<path>/x.lsv" in ev and "<email>" in ev
    assert "<steam_id>" in ev and "<key>" in ev and "<env>" in ev
    assert json.loads((p.bundle_dir / "manifest.json").read_text())["profile"]["name"] == "<path>/p.toml"
    # the untouched files keep their bytes
    assert (p.bundle_dir / "facts.jsonl").read_bytes() == (d / "facts.jsonl").read_bytes()


def test_scrub_keeps_noreply_and_references():
    s = scrub.Scrubber()
    text = "noreply@github.com x@users.noreply.github.com API_KEY=OPENROUTER_API_KEY http://example.org/a/b"
    assert s.text(text) == text and s.replacements == 0
    assert scrub.text_hits("/home/" + "bob/x") == ["path"]


def test_cost_kind():
    assert pack.cost_kind({"client": {"name": "claude"}}, []) == "notional"
    assert pack.cost_kind({"client": {"name": "pi"}}, [{"kind": "usage", "cost_usd": 0.1}]) == "billed"
    assert pack.cost_kind({"client": {"name": "pi"}}, [{"kind": "usage", "cost_usd": None}]) == "estimated"


def test_archive_bytes_are_stable():
    files = {"b.json": b"{}", "a.txt": b"x"}
    assert pack.archive_bytes(files) == pack.archive_bytes(dict(reversed(files.items())))
    with tarfile.open(fileobj=io.BytesIO(pack.archive_bytes(files))) as tar:
        assert [(m.name, m.mtime, m.uid) for m in tar.getmembers()] == [("a.txt", 0, 0), ("b.json", 0, 0)]


@pytest.mark.skipif(not PILOT.is_dir(), reason="the pilot's run dirs are on the dev machine only")
def test_pilot_run_dirs(tmp_path):
    runs = sorted(PILOT.glob("diverse-2026-10-25-*"))
    packed = refused = 0
    for i, d in enumerate(runs):
        if not (d / "manifest.json").is_file():
            continue
        try:
            p = pack.pack(d, tmp_path / str(i), attempt=pack.Attempt(1))
        except pack.PackError as e:
            assert "pilot" in str(e) or "invalid" in str(e)
            refused += 1
            continue
        packed += 1
        assert p.archive.stat().st_size <= 2_000_000
        for f in p.envelope["files"]:
            assert scrub.file_hits(f["name"], (p.bundle_dir / f["name"]).read_bytes()) == []
    assert packed >= 5 and refused >= 20


def test_refusal_needs_a_scenario_digest(tmp_path):
    d = run_dir(tmp_path)
    man, res = json.loads((d / "manifest.json").read_text()), json.loads((d / "result.json").read_text())
    man["scenario"]["digest"] = None
    assert (
        pack.refusal(man, res, pack.load_scenarios(), [man["versions"]["suite"]])
        == "the manifest has no scenario digest"
    )


def test_size_problems_count_the_total(monkeypatch):
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 10)
    assert pack.size_problems({"a": b"x" * 11}) == ["11 bytes in all, over 10"]


def render_dir(tmp_path: Path, run_hash: str, frames: int = 0) -> Path:
    r = tmp_path / "render"
    r.mkdir()
    (r / "render.json").write_text(
        json.dumps({"schema": 1, "run_hash": run_hash, "video_start": 0, "video_end": 1, "fps": 15, "events": []})
    )
    if frames:
        (r / "frames").mkdir()
        for i in range(frames):
            (r / "frames" / f"{i}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    return r


def test_add_render_without_frames(tmp_path):
    p = do_pack(run_dir(tmp_path), tmp_path / "out")
    q = pack.add_render(render_dir(tmp_path, p.envelope["run_hash"]), tmp_path / "out")
    names = {f["name"] for f in q.envelope["files"]}
    assert "render.json" in names and bundle.FRAMES_TAR not in names


def test_add_render_over_the_caps(tmp_path, monkeypatch):
    p = do_pack(run_dir(tmp_path), tmp_path / "out")
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", sum(f.stat().st_size for f in p.bundle_dir.iterdir()) + 1)
    with pytest.raises(pack.PackError, match="refused"):
        pack.add_render(render_dir(tmp_path, p.envelope["run_hash"]), tmp_path / "out")
    assert not (p.bundle_dir / "render.json").exists()


def test_cli(tmp_path, capsys):
    d = run_dir(tmp_path)
    assert pack.main([str(d), str(tmp_path / "out"), "--attempt", "1"]) == 0
    env = json.loads((tmp_path / "out" / "bundle" / bundle.ENVELOPE).read_text())
    assert "bundle " in capsys.readouterr().out
    assert pack.main([str(d), str(tmp_path / "out"), "--attempt", "1"]) == 1  # the out dir holds a bundle
    assert "already holds" in capsys.readouterr().err
    r = render_dir(tmp_path, env["run_hash"], frames=2)
    assert pack.main([str(r), str(tmp_path / "out"), "--add-render"]) == 0
    with tarfile.open(tmp_path / "out" / "bundle" / bundle.FRAMES_TAR) as tar:
        assert tar.getnames() == ["frames/0.jpg", "frames/1.jpg"]
    with pytest.raises(SystemExit):
        pack.main([str(d), str(tmp_path / "x")])  # --attempt is required to pack
    assert pack.main([str(tmp_path / "nothing"), str(tmp_path / "y"), "--attempt", "1"]) == 1
    assert "not a run dir" in capsys.readouterr().err


def test_scrub_replaces_a_secret_and_reads_tars():
    s = scrub.Scrubber()
    assert s.text("API_KEY=" + "x" * 20 + " rest") == "API_KEY=<secret> rest"
    assert scrub.file_hits("x.bin", b"API_KEY=" + b"x" * 20) == []  # an unknown binary: not text
    assert scrub.file_hits("x.json", b"{broken API_KEY=" + b"x" * 20 + b" ") == ["secret"]  # scanned as text
    assert scrub.tar_texts(b"not a tar" * 100) == []
    quant = b"\xff\xdb\x00\x04ab"  # a quantisation table: no text in it
    comment = b"\xff\xfe\x00\x07hello"
    assert scrub.jpeg_texts(b"\xff\xd8" + quant + comment + b"\xff\xda\x00\x02") == ["hello"]
