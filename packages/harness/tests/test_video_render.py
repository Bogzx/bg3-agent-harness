"""bg3harness.video.render, draw and compose without ffmpeg (a fake records its arguments): a run's events and
recording index become render.json, the frame and column images with their concat lists and the compose command;
the same inputs give the same bytes; a run whose recording has a gap is refused."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from bg3harness.video import compose, draw, render
from bg3harness.video import timeline as T  # noqa: N812

T0 = 1000.0
META = T.Meta(
    "s-r001", "ab" * 32, "The grove gate fight", "Win the fight at the gate.", 1800.0, "billed", 6.0, "x.test"
)


def write_run(d: Path, gap: bool = False) -> Path:
    """A run dir with two play processes' events and facts, and a recording index next to it."""
    d.mkdir(parents=True)
    lines: list[dict[str, Any]] = [
        {"v": 1, "seq": 0, "t": T0 + 0.5, "src": "client", "kind": "tool_call", "tool": "get_status", "args": {}},
        {"v": 1, "seq": 1, "t": T0 + 1.0, "src": "client", "kind": "tool_result", "text": "WHERE grove\nPARTY"},
        {"v": 1, "seq": 2, "t": T0 + 2.0, "src": "client", "kind": "thinking", "think_ms": 900.0},
        {"v": 1, "seq": 3, "t": T0 + 3.0, "src": "client", "kind": "text", "text": "Attacking the gate guard."},
        {"v": 1, "seq": 4, "t": T0 + 4.0, "src": "client", "kind": "result", "cost_usd": 0.4},
    ]
    (d / "S001.events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"half', encoding="utf-8")
    resumed = {"v": 1, "seq": 0, "t": T0 + 6.0, "src": "client", "kind": "text", "text": "Done."}
    (d / "S001-c1.events.jsonl").write_text(json.dumps(resumed) + "\n", encoding="utf-8")
    facts = [
        {"v": 1, "kind": "mark", "t": T0, "mark": "run_start"},
        {"v": 1, "kind": "snapshot", "t": T0 + 0.2, "gold": 10, "party": [{"name": "Tav", "hp": 9, "max_hp": 18}]},
        {"v": 1, "kind": "mark", "t": T0 + 7.5, "mark": "run_end"},
    ]
    (d / "facts.jsonl").write_text("".join(json.dumps(x) + "\n" for x in facts), encoding="utf-8")
    video = d.parent / "video"
    video.mkdir()
    cut = T0 + (1.0 if gap else 4.0)  # a gap: the second segment starts 6 s after the first ends
    segs: list[dict[str, Any]] = [{"file": "rec-1-0000.mp4", "start": T0 - 2.0, "end": cut}]
    segs.append({"file": "rec-1-0001.mp4", "start": cut + (6.0 if gap else 0.0), "end": T0 + 20.0})
    (video / "index.json").write_text(json.dumps({"v": 1, "segments": segs}), encoding="utf-8")
    for s in segs:
        (video / s["file"]).write_bytes(b"mp4")
    return video / "index.json"


class FakeFfmpeg:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> None:
        self.calls.append(list(args))
        Path(args[-1].replace("%04d", "0001")).write_bytes(b"x")


def make_plan(run: Path, index: Path) -> render.Plan:
    logs, facts = render.events_logs(run), render.read_jsonl(run / "facts.jsonl")
    start, end = render.run_window(facts, logs)
    segs: list[dict[str, Any]] = json.loads(index.read_text())["segments"]
    return render.plan(segs, logs, facts, start, end, META.run_sha256, META.t_pass_s)


def test_a_run_becomes_render_json_images_and_the_compose_command(tmp_path: Path):
    index = write_run(tmp_path / "run")
    p = make_plan(tmp_path / "run", index)
    assert (p.start, p.end, p.duration) == (T0, T0 + 7.5, 7.5)
    ff = FakeFfmpeg()
    video = render.render(p, META, index.parent, tmp_path / "out", tmp_path / "work", ff)
    assert video == tmp_path / "out" / "evidence.mp4"
    sync = json.loads((tmp_path / "out" / "render.json").read_text())
    assert sync["events"] == [
        {"file": "S001.events.jsonl", "seq": 0, "at_s": 0.5},
        {"file": "S001.events.jsonl", "seq": 1, "at_s": 1.0},
        {"file": "S001.events.jsonl", "seq": 3, "at_s": 3.0},
        {"file": "S001-c1.events.jsonl", "seq": 0, "at_s": 6.0},
    ]
    assert (sync["video_start"], sync["video_end"], sync["fps"]) == (T0, T0 + 7.5, 15)
    assert (sync["schema"], sync["run_hash"]) == (1, "ab" * 32)
    game = (tmp_path / "work" / "game.ffconcat").read_text()
    assert "inpoint 2.000\noutpoint 6.000" in game and "inpoint 0.000\noutpoint 3.500" in game
    compose_args, sheet = ff.calls
    assert compose_args[compose_args.index("-t") + 1] == "7.500"
    graph = compose_args[compose_args.index("-filter_complex") + 1]
    assert "scale=1408:792:force_original_aspect_ratio=decrease:flags=lanczos" in graph
    assert "[f][g]overlay=24:96[fg]" in graph and "[fg][c]overlay=1456:96,format=yuv420p[v]" in graph
    assert compose_args[-1] == str(video) and sheet[sheet.index("-i") + 1] == str(video)
    assert "fps=1/30,scale=320:180" in sheet
    assert compose_args[compose_args.index("-c:v") + 1 :][:5] == ["libx264", "-preset", "veryfast", "-crf", "24"]


def test_the_encoder_is_libx264_unless_nvenc_is_asked_for(tmp_path: Path):
    index = write_run(tmp_path / "run")
    args = [str(tmp_path / "run"), "--index", str(index), "--out", str(tmp_path / "out"), "--run-id", "s-r001"]
    ff = FakeFfmpeg()
    assert render.main([*args, "--run-hash", "ab" * 32, "--encoder", "h264_nvenc"], ff) == 0
    compose_args = ff.calls[0]
    assert compose_args[compose_args.index("-c:v") + 1 :][:3] == ["h264_nvenc", "-preset", "p5"]
    assert compose_args[compose_args.index("-cq") + 1] == "24"
    with pytest.raises(ValueError, match="unknown encoder 'vp9'"):
        compose.compose_args(tmp_path, tmp_path, tmp_path, tmp_path / "v.mp4", 1.0, encoder="vp9")


def durations(listing: str) -> list[float]:
    return [float(ln.split()[1]) for ln in listing.splitlines() if ln.startswith("duration ")]


def test_the_images_cover_the_whole_run(tmp_path: Path):
    index = write_run(tmp_path / "run")
    p = make_plan(tmp_path / "run", index)
    frame_list, column_list = render.write_images(p, META, tmp_path / "work")
    assert sum(durations(frame_list.read_text())) == pytest.approx(7.5)
    assert sum(durations(column_list.read_text())) == pytest.approx(7.5)
    assert len(durations(frame_list.read_text())) == 8  # one per second of run clock
    assert len(durations(column_list.read_text())) == len(p.changes)


def digests(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir()) if p.suffix == ".png"}


def test_the_same_run_renders_the_same_bytes(tmp_path: Path):
    index = write_run(tmp_path / "run")
    for k in (1, 2):
        render.render(
            make_plan(tmp_path / "run", index),
            META,
            index.parent,
            tmp_path / f"out{k}",
            tmp_path / f"w{k}",
            FakeFfmpeg(),
        )
    assert digests(tmp_path / "w1") == digests(tmp_path / "w2")
    assert digests(tmp_path / "w1")
    assert (tmp_path / "out1" / "render.json").read_bytes() == (tmp_path / "out2" / "render.json").read_bytes()


def test_the_id_line_names_the_run_and_its_run_hash():
    assert draw.id_line(META) == "run s-r001 · hash abababababab · x.test"


def test_the_run_hash_must_be_a_sha256_hex_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    index = write_run(tmp_path / "run")
    args = [str(tmp_path / "run"), "--index", str(index), "--out", str(tmp_path / "out"), "--run-id", "s-r001"]
    for bad in ("AB" * 32, "ab" * 31, "6f1c-uuid"):
        with pytest.raises(SystemExit):
            render.main([*args, "--run-hash", bad], FakeFfmpeg())
        assert "not a sha256 hex digest" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_the_images_have_their_fixed_sizes():
    s = T.Strip(5, 0.42, 410, 133, (T.Member("Tav", 9, 18),), 6.0, 2.0)
    frame = draw.frame_image(s, META, draw.lanes_image(T.Lanes(), 10.0), 10.0)
    column = draw.column_image([T.Item(1.0, "S001", 0, "call", "", "get_status")])
    assert (frame.size, frame.mode, column.size, column.mode) == ((1920, 1080), "RGB", (440, 960), "RGBA")
    assert draw.lanes_image(T.Lanes(), 10.0).size == (draw.TL_W, draw.TL_H)
    assert draw.clock(65) == "01:05" and draw.clock(3725) == "1:02:05" and draw.short_clock(65) == "1:05"


def test_the_column_is_transparent_where_the_party_card_shows_through():
    column = draw.column_image([T.Item(1.0, "S001", 0, "text", "hello")])
    hole = draw.PARTY_Y - draw.COL_Y + draw.PARTY_H // 2
    assert column.getpixel((200, hole))[3] == 0  # pyright: ignore[reportIndexIssue, reportOptionalSubscript]
    assert column.getpixel((200, 100))[3] == 255  # pyright: ignore[reportIndexIssue, reportOptionalSubscript]


def test_a_character_the_font_lacks_is_drawn_with_dejavu():
    runs = draw._runs("a✕b", "mono", 18)  # pyright: ignore[reportPrivateUsage]
    assert [r for r, _ in runs] == ["a", "✕", "b"]
    assert runs[1][1] is draw._fallback("mono", 18) and runs[0][1] is draw.font("mono", 18)  # pyright: ignore[reportPrivateUsage]


def test_a_result_goes_under_its_call_and_a_failure_is_marked():
    items = [
        T.Item(1.0, "S001", 0, "call", "plan", "combat_turn", "c1"),
        T.Item(2.0, "S001", 1, "call", "", "look", "c2"),
        T.Item(3.0, "S001", 2, "error", "FAILED: no path", ref="c1"),
        T.Item(4.0, "S001", 3, "result", "orphan"),
    ]
    groups = draw._groups(items)  # pyright: ignore[reportPrivateUsage]
    assert [(g.seq, r and r.seq) for g, r in groups] == [(0, 2), (1, None), (3, None)]


def test_the_verdict_chip_says_not_yet_then_passed_or_the_ending():
    meta = dataclasses.replace(META, ending="wiped")
    assert draw.verdict(T.Strip(3), meta, 100)[0] == "GOAL: NOT YET"
    assert draw.verdict(T.Strip(96), meta, 100)[:2] == ("WIPED", draw.RUST)
    assert draw.verdict(T.Strip(70, passed_s=65.0), meta, 100)[:2] == ("PASSED 1:05", draw.TEAL)


def test_wrap_keeps_lines_in_the_width_and_marks_a_cut():
    lines = draw.wrap("word " * 200 + "x" * 400, "sans", 18, 300, 4)
    assert len(lines) == 4 and lines[-1].endswith(" …")
    assert all(draw.length(ln, "sans", 18) <= 300 for ln in lines)
    assert draw.wrap("short", "sans", 18, 300, 4) == ["short"]
    assert draw.clip("a " * 100, "mono", 18, 100).endswith("…")


def test_a_run_whose_recording_has_a_gap_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    index = write_run(tmp_path / "run", gap=True)
    ff = FakeFfmpeg()
    args = [str(tmp_path / "run"), "--index", str(index), "--out", str(tmp_path / "out")]
    assert render.main([*args, "--run-id", "s-r001", "--run-hash", "ab" * 32], ff) == 2
    assert "refused: the recording misses 6.0 s at run second 1.0" in capsys.readouterr().err
    assert not (tmp_path / "out").exists() and not ff.calls


def test_a_sample_leaves_out_the_events_after_it(tmp_path: Path):
    index = write_run(tmp_path / "run")
    args = [str(tmp_path / "run"), "--index", str(index), "--out", str(tmp_path / "out"), "--sample-s", "2"]
    assert render.main([*args, "--run-id", "s-r001", "--run-hash", "ab" * 32], FakeFfmpeg()) == 0
    sync = json.loads((tmp_path / "out" / "render.json").read_text())
    assert (sync["video_end"], [e["seq"] for e in sync["events"]]) == (T0 + 2.0, [0, 1])


def test_a_deleted_segment_is_refused_before_anything_is_written(tmp_path: Path):
    index = write_run(tmp_path / "run")
    (index.parent / "rec-1-0001.mp4").unlink()
    ff = FakeFfmpeg()
    with pytest.raises(T.RenderError, match=r"missing in .*: rec-1-0001\.mp4"):
        render.render(make_plan(tmp_path / "run", index), META, index.parent, tmp_path / "out", tmp_path / "w", ff)
    assert not ff.calls and not (tmp_path / "out").exists()
