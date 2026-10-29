"""The evidence video renderer (bg3harness.video) and the submission pipeline together: a fixture run is packed, the
real renderer renders it with the packed bundle's run hash (ffmpeg faked: no video, the contact sheet's JPEGs
written), its render.json holds to render.v1, and pack --add-render's bundle passes the validator, E03 included."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import jsonschema
from bg3harness.video import render
from test_submission_validate import FIGHT, good_run, jpeg, release_for, run

from bg3bench.submission import bundle, pack, validate

SCHEMA = Path(__file__).parents[1] / "schema" / "render.v1.schema.json"
VIDEO: dict[str, object] = {"youtube_id": "dQw4w9WgXcQ", "mp4_sha256": None}


class SheetOnly:
    """ffmpeg faked: the compose writes nothing, the contact sheet writes one JPEG per FRAME_EVERY_S of video."""

    def __init__(self, n: int):
        self.n = n

    def __call__(self, args: Sequence[str]) -> None:
        out = Path(args[-1])
        if out.name == "%04d.jpg":
            for i in range(1, self.n + 1):
                (out.parent / f"{i:04d}.jpg").write_bytes(jpeg())


def test_a_real_render_of_a_packed_run_is_added_and_validates(tmp_path: Path):
    d = good_run(tmp_path, FIGHT)
    (d / "render.json").unlink()
    (d / bundle.FRAMES_TAR).unlink()
    wall = json.loads((d / "result.json").read_text())["wall_s"]
    p = pack.pack(d, tmp_path / "out", attempt=pack.Attempt(1), video=VIDEO | {"duration_s": wall})
    logs = render.events_logs(p.bundle_dir)
    start, end = render.run_window(render.read_jsonl(p.bundle_dir / "facts.jsonl"), logs)
    rec = tmp_path / "recording"
    rec.mkdir()
    (rec / "rec-1-0000.mp4").write_bytes(b"mp4")
    segment = {"file": "rec-1-0000.mp4", "run": "rec-1", "start": start - 1, "end": end + 1, "bytes": 3}
    (rec / "index.json").write_text(json.dumps({"segments": [segment]}))
    rdir = tmp_path / "render"
    args = [str(p.bundle_dir), "--index", str(rec / "index.json"), "--out", str(rdir), "--run-id", "r1"]
    sheet = SheetOnly(round((end - start) / validate.FRAME_EVERY_S))
    assert render.main([*args, "--run-hash", p.envelope["run_hash"]], sheet) == 0
    doc = json.loads((rdir / "render.json").read_text())
    jsonschema.validate(doc, json.loads(SCHEMA.read_text()))
    assert doc["events"] and doc["run_hash"] == p.envelope["run_hash"]
    q = pack.add_render(rdir, tmp_path / "out")
    v = run(q.bundle_dir, release_for(d))
    assert v.accepted, [(r.code, r.detail) for r in v.checks if r.ok is False]
    assert next(r.ok for r in v.checks if r.code == "E03") is True
