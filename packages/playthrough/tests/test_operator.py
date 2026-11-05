# pyright: basic
"""The app's side of the operator messages (bg3play.operator): the POST's codes and ids, the rows with their status
(a redeliver row queues one again), a session's counts, the redeliver check against a client's stream log, and the
playthrough option ([operator] messages)."""

import json
from pathlib import Path

import pytest
from bg3ai import operator as op
from bg3play import config
from bg3play import operator as pop

T = 1_762_340_531.0


def write(path: Path, *rows: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def on(pt_dir: Path, messages: bool = True) -> None:
    pt_dir.mkdir(parents=True, exist_ok=True)
    cfg = config.validate({"v": 1, "name": pt_dir.name, "operator": {"messages": messages}})
    config.save(pt_dir, cfg)


def test_the_option_is_off_by_default_and_round_trips():
    cfg = config.validate({"v": 1, "name": "naut"})
    assert cfg.operator.messages is False
    text = config.dumps(cfg)
    assert "[operator]\nmessages = false" in text
    guided = config.validate({"v": 1, "name": "naut", "operator": {"messages": True}})
    assert config.loads(config.dumps(guided)) == guided
    with pytest.raises(config.ConfigError, match=r"operator\.messages"):
        config.validate({"v": 1, "name": "naut", "operator": {"messages": "yes"}})


def test_enabled_needs_the_option_and_no_bench_manifest(tmp_path: Path):
    pt_dir, sdir = tmp_path / "pt" / "ilse", tmp_path / "sessions" / "ilse"
    sdir.mkdir(parents=True)
    assert not pop.enabled(pt_dir, sdir)  # no config: the defaults
    on(pt_dir, messages=False)
    assert not pop.enabled(pt_dir, sdir)
    on(pt_dir)
    assert pop.enabled(pt_dir, sdir)
    (sdir / "manifest.json").write_text("{}")
    assert not pop.enabled(pt_dir, sdir)
    (pt_dir / config.FILENAME).write_text("v = 1\nname = 'ilse'\n[operator]\nmessages = 1\n")
    (sdir / "manifest.json").unlink()
    assert not pop.enabled(pt_dir, sdir)  # an unusable config: off


def test_post_codes_and_ids(tmp_path: Path):
    sdir = tmp_path / "ilse"
    assert pop.post(sdir, False, {"text": "hi"}, T)[0] == 409
    assert not sdir.exists()  # nothing created when the option is off
    assert pop.post(sdir, True, {"text": 3}, T)[0] == 400
    assert pop.post(sdir, True, {"text": " \t\x01 "}, T)[0] == 400
    code, body = pop.post(sdir, True, {"text": "x" * 1001}, T)
    assert (code, body) == (400, {"error": "text: 1001 characters, at most 1000"})
    assert pop.post(sdir, True, {"text": "x" * 1000}, T) == (201, {"id": "m1", "t": T, "status": "queued"})
    assert pop.post(sdir, True, {"text": "too soon"}, T + 4.9)[0] == 429
    code, body = pop.post(sdir, True, {"text": "  go north\r\nthen\x07 rest  "}, T + 5)
    assert (code, body) == (201, {"id": "m2", "t": T + 5, "status": "queued"})
    assert op.read_rows(sdir / op.INBOX)[1] == {"id": "m2", "t": T + 5, "text": "go north\nthen rest"}
    (sdir / "manifest.json").write_text("{}")
    assert pop.post(sdir, True, {"text": "bench"}, T + 60)[0] == 409  # a bench run's dir, whatever the option says


def test_ten_queued_refuse_an_eleventh_until_one_is_delivered(tmp_path: Path):
    sdir = tmp_path / "ilse"
    for i in range(10):
        assert pop.post(sdir, True, {"text": f"m{i}"}, T + 10 * i)[0] == 201
    code, body = pop.post(sdir, True, {"text": "one more"}, T + 200)
    assert (code, body) == (429, {"error": "10 messages are still queued"})
    write(sdir / op.DELIVERED, {"id": "m1", "t": T + 201, "session": "S001", "tool": "look"})
    assert pop.post(sdir, True, {"text": "one more"}, T + 202) == (201, {"id": "m11", "t": T + 202, "status": "queued"})


def test_rows_fold_delivered_and_redeliver(tmp_path: Path):
    sdir = tmp_path
    write(sdir / op.INBOX, {"id": "m1", "t": T, "text": "a"}, {"id": "m2", "t": T + 6, "text": "b"}, {"id": "bad"})
    write(sdir / op.DELIVERED, {"id": "m1", "t": T + 9, "session": "S004", "tool": "travel_to"})
    assert pop.rows(sdir) == [
        {"id": "m1", "t": T, "text": "a", "status": "delivered",
         "delivered": {"t": T + 9, "session": "S004", "tool": "travel_to"}},
        {"id": "m2", "t": T + 6, "text": "b", "status": "queued"},
    ]  # fmt: skip
    write(sdir / op.DELIVERED, {"id": "m1", "t": T + 20, "redeliver": True})
    assert [r["status"] for r in pop.rows(sdir)] == ["queued", "queued"]
    assert pop.rows(tmp_path / "none") == []


def test_session_counts(tmp_path: Path):
    write(tmp_path / op.INBOX, *({"id": f"m{i}", "t": T + 10 * i, "text": "x"} for i in range(1, 5)))
    write(
        tmp_path / op.DELIVERED,
        {"id": "m1", "t": T + 11, "session": "S001", "tool": "look"},  # sent before S002, delivered in its first call
        {"id": "m2", "t": T + 21, "session": "S002", "tool": "look"},
        {"id": "m2", "t": T + 30, "redeliver": True},
        {"id": "m2", "t": T + 31, "session": "S002", "tool": "look"},  # counted once
        {"id": "m3", "t": T + 31, "session": "S002", "tool": "look"},
    )
    assert pop.session_counts(tmp_path, "S002", T + 15, T + 35) == {"enabled": True, "sent": 2, "delivered": 2}
    assert pop.session_counts(tmp_path, "S003", None, None) == {"enabled": True, "sent": 0, "delivered": 0}
    assert pop.session_counts(tmp_path, "S003", T + 35, None)["sent"] == 1  # still running: sent since its start


def test_redeliver_queues_a_message_the_dead_clients_stream_never_showed(tmp_path: Path):
    write(tmp_path / op.INBOX, {"id": "m1", "t": T, "text": "a"}, {"id": "m2", "t": T + 6, "text": "b"},
          {"id": "m3", "t": T + 12, "text": "c"})  # fmt: skip
    write(
        tmp_path / op.DELIVERED,
        {"id": "m1", "t": T + 1, "session": "S004", "tool": "look"},
        {"id": "m2", "t": T + 7, "session": "S004", "tool": "travel_to"},
        {"id": "m3", "t": T + 13, "session": "S003", "tool": "look"},  # another session's: not checked
    )
    shown = {"type": "user", "message": {"content": [{"type": "tool_result", "content": [
        {"type": "text", "text": "You are at X.\n" + op.line(T, "a")}]}]}}  # fmt: skip
    (tmp_path / "S004.jsonl").write_text(json.dumps(shown) + "\nnot json\n", encoding="utf-8")
    assert pop.redeliver(tmp_path, "S004", T + 60) == ["m2"]
    assert op.read_rows(tmp_path / op.DELIVERED)[-1] == {"id": "m2", "t": T + 60, "redeliver": True}
    assert [r["status"] for r in pop.rows(tmp_path)] == ["delivered", "queued", "delivered"]
    assert pop.redeliver(tmp_path, "S004", T + 61) == []  # m2 is pending now: nothing to check again
    assert pop.redeliver(tmp_path / "none", "S004", T) == []
