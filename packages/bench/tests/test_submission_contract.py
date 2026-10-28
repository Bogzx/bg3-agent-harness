# pyright: basic
"""The submission contract: schema/bundle.v1.schema.json, the bundle hash and the check codes (schema/checks.v1.json)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from bg3bench.submission import bundle, codes

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"


def envelope(**over) -> dict:
    env = {
        "schema": 1,
        "bundle_id": "6f1c2a3b-0000-4000-8000-123456789abc",
        "run_id": "diverse-act1-r001",
        "suite": "bench-v0.8",
        "scenario": "act1_gold_max",
        "scenario_digest": "0b264d08717b21da",
        "run_hash": "1" * 64,
        "condition": "rules",
        "model": {"slug": "deepseek-flash", "provider_requested": None, "reasoning_effort": "medium"},
        "client": {"name": "pi", "version": "1.3.0"},
        "versions": {"harness": "0.1.0", "bench": "0.8.0", "panel": None},
        "attempt": {"n": 1, "unsubmitted_valid": 0, "cell": "act1_gold_max/deepseek-flash/rules"},
        "cost": {"usd": 0.29, "kind": "billed"},
        "files": [{"name": "manifest.json", "sha256": "0" * 64, "bytes": 2546}],
        "video": None,
        "recording": None,
        "scrub": {"version": 1, "replacements": 0},
        "packed": 1792931000.0,
    }
    env.update(over)
    return env


def errors(env: dict) -> list[str]:
    v = jsonschema.Draft202012Validator(bundle.schema())
    return [e.message for e in v.iter_errors(env)]


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(bundle.schema())


def test_example_envelope_validates():
    assert errors(envelope()) == []
    video = {"youtube_id": "dQw4w9WgXcQ", "duration_s": 846, "mp4_sha256": "a" * 64}
    recording = {"started": 1792929552.1, "ended": 1792930398.4, "fps": 15}
    assert errors(envelope(video=video, recording=recording)) == []


@pytest.mark.parametrize(
    "name", ["../manifest.json", "/etc/passwd", "a/b.json", "frames/0001.jpg", ".env", "x..y", "x" * 200]
)
def test_file_names_are_bare(name):
    assert errors(envelope(files=[{"name": name, "sha256": "0" * 64, "bytes": 1}]))


def test_frames_tar_allowed_and_file_count_capped():
    assert errors(envelope(files=[{"name": bundle.FRAMES_TAR, "sha256": "0" * 64, "bytes": 1}])) == []
    many = [{"name": f"S001-c{i}.events.jsonl", "sha256": "0" * 64, "bytes": 1} for i in range(bundle.MAX_FILES + 1)]
    assert errors(envelope(files=many))
    assert bundle.FRAMES_MEMBER.fullmatch("frames/12.jpg")
    assert not bundle.FRAMES_MEMBER.fullmatch("frames/../x.jpg")


@pytest.mark.parametrize(
    "over",
    [
        {"cost": {"usd": 1.0, "kind": "free"}},
        {"attempt": {"n": 0, "unsubmitted_valid": 0, "cell": "a/b/c"}},
        {"video": {"youtube_id": "https://youtu.be/x", "duration_s": 1, "mp4_sha256": None}},
        {"files": [{"name": "a.json", "sha256": "0" * 64, "bytes": bundle.MAX_FILE_BYTES + 1}]},
        {"suite": "v1"},
        {"extra": 1},
    ],
)
def test_bad_envelopes_fail(over):
    assert errors(envelope(**over))


def test_bundle_hash_is_canonical():
    a = envelope()
    b = dict(reversed(list(a.items())))
    assert bundle.canonical_bytes(a) == bundle.canonical_bytes(b)
    assert b" " not in bundle.canonical_bytes(a)
    assert bundle.bundle_hash(a) == bundle.bundle_hash(b)
    assert bundle.bundle_hash(a) != bundle.bundle_hash(envelope(run_id="other-r001"))
    # non-ASCII is hashed as UTF-8, not as \u escapes (the site's JSON.stringify keeps it as is)
    assert "é".encode() in bundle.canonical_bytes(envelope(condition="é"))


def test_run_hash_covers_the_record_files_only():
    files = {"manifest.json": b"m", "facts.jsonl": b"f", "S001.events.jsonl": b"e", "S001-c1.events.jsonl": b"c"}
    sha = {n: bundle.sha256_bytes(d) for n, d in files.items()}
    # the canonical JSON of [{name, sha256, bytes}] sorted by name: what the harness's renderer computes too
    text = ",".join(f'{{"bytes":1,"name":"{n}","sha256":"{sha[n]}"}}' for n in sorted(files))
    want = bundle.sha256_bytes(f"[{text}]".encode())
    assert bundle.run_hash(files) == want
    assert bundle.run_hash(files | {"render.json": b"r", "frames.tar": b"t", "bg3-tools.json": b"x"}) == want
    assert bundle.run_hash(files | {"rolls.jsonl": b"r"}) != want
    assert bundle.run_hash(files | {"S001-c1.events.jsonl": b"C"}) != want


def test_render_schema():
    v = jsonschema.Draft202012Validator(json.loads((SCHEMA_DIR / "render.v1.schema.json").read_text()))
    v.check_schema(v.schema)
    good = {"schema": 1, "run_hash": "a" * 64, "video_start": 1.0, "video_end": 2.0, "fps": 15,
            "events": [{"file": "S001-c1.events.jsonl", "seq": 3, "at_s": 0.5}]}  # fmt: skip
    assert list(v.iter_errors(good)) == []
    assert list(v.iter_errors(good | {"events": [{"seq": 3, "at_s": 0.5}]}))
    assert list(v.iter_errors(good | {"events": [{"file": "../x", "seq": 3, "at_s": 0.5}]}))


def test_is_canonical():
    env = envelope()
    assert bundle.is_canonical(bundle.canonical_bytes(env))
    assert not bundle.is_canonical(json.dumps(env, indent=1).encode())
    assert not bundle.is_canonical(b"{not json")


def test_check_codes_are_the_plan_table():
    assert [c.code for c in codes.CHECKS] == [
        *("F01", "F02", "F03", "F04", "P01", "P02", "C01", "C02", "C03", "D01", "D02"),
        *("X01", "X02", "X03", "X04", "X05", "X06", "X07", "G01", "G02", "A01", "A02"),
        *("E01", "E02", "E03", "E04", "S01", "S02"),
    ]
    for c in codes.CHECKS:
        assert c.where and set(c.where) <= {"panel", "worker", "job"}
        assert (c.severity == "flag") <= (c.flag is not None)
    # the panel runs everything but the cross-submission checks and the video duration
    assert {c.code for c in codes.CHECKS if "panel" not in c.where} == {"D01", "D02", "X07", "E02"}
    # the Worker scans the small files, the job's validate every file (final)
    assert codes.BY_CODE["S01"].where == ("panel", "worker", "job")


def test_checks_json_is_in_sync():
    assert json.loads((SCHEMA_DIR / "checks.v1.json").read_text()) == codes.as_json()
