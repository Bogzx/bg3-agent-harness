# pyright: basic
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""bg3harness.runner.run_host: a run ends at a party wipe, plays only from its own saves, and leaves none behind.

Bench B3 (research/fix/live.md, Job 6, Opus x fight_act2_balthazar): the party wiped and the runner resumed the agent 6
times ("the grader hasn't seen its goal yet"); then the agent took the Game Over box's Main Menu and the main menu's
Continue loaded the newest Story save, act1_level_3's own S001 save from an earlier run, with no load mark, and the
record stayed valid. The fake runner's server_boot is the mod's sys.ping boot (a load makes a new one); the rig's
SaveWatch never polls on its own and its clock is rig.now."""

from __future__ import annotations

import contextlib
import json
import os
import struct
import threading
from pathlib import Path
from typing import Any

from bg3ai import lsf, lspk
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from test_run_host import JOB, MAX_SC, SAVE_SC, FakeRunner, Rig, outcome, rig, run_dir  # noqa: F401  (rig: fixture)

START = SAVE_SC["start"]["save"]


def write_lsv(lsv: Path, saved: float) -> None:
    """A v18 .lsv whose meta.lsf holds SaveTime `saved` (u64 epoch, as BG3 writes it)."""
    meta = lsf.build(
        [("MetaData", -1), ("MetaData", 0)],
        [("LeaderName", 23, 1, b"Lae'zel\0"), ("SaveTime", 24, 1, struct.pack("<Q", int(saved)))],
    )
    lsv.write_bytes(lspk.build([("StorySave.bin", b"\0" * 64, "none"), ("meta.lsf", meta, "none")]))


def save(folder: str, mtime: float, saved: float | None = None) -> Path:
    """A save folder in the shared Story folder, saved at `saved` (default: mtime) and written at mtime (the rig's run
    starts at 1000.0). A restored copy is saved before the run and written in it."""
    d = renv.SAVE_DIR / folder
    d.mkdir()
    lsv = d / f"{folder.split('__', 1)[1]}.lsv"
    write_lsv(lsv, mtime if saved is None else saved)
    for p in (lsv, d):
        os.utime(p, (mtime, mtime))
    return d


def story() -> list[str]:
    return sorted(p.name for p in renv.SAVE_DIR.iterdir())


def agent_loaded(label: str, t: float = 1100.0) -> None:
    """The agent's load_game in the MCP server's tool log (runtime/actions.jsonl)."""
    renv.RUNTIME.mkdir(parents=True, exist_ok=True)
    rec = {"t": t, "tool": "load_game", "args": {"save": label}, "result": f"load_game: done — loaded={label} (3.1 s)"}
    with open(renv.RUNTIME / "actions.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_a_wipe_ends_a_goal_run_and_a_maximise_run_as_failed(rig: Rig):
    for sc, name in ((SAVE_SC, "g"), (MAX_SC, "m")):
        said: list[str | None] = [None]
        rig.h.start({**JOB, "verdict": lambda s=said: s[0]}, sc, run_dir(name))
        watch, r = rig.h.watches[run_dir(name)], rig.runners[-1]
        said[0] = "wiped"  # bg3bench graders.verdict: every party member dead or downed (the Game Over box)
        assert watch.check() and r.until_hit is not None
        assert (r.until_hit["verdict"], r.until_hit["label"]) == ("wiped", "the party was wiped (Game Over)")
        assert H.undecided_why(r, sc, 1e12, 1000.0) is None  # no "the grader hasn't seen its goal" resume
        rec = rig.h.record(JOB, sc, run_dir(name), outcome())
        assert (rec["outcome"], rec["verdict"], rec["harness_failures"], rec["reloads"]) == ("failed", "wiped", 0, 0)
        assert "foreign_save" not in rec


def test_a_load_the_harness_didnt_make_ends_the_run_invalid(rig: Rig):
    save("Tav-502512612935__S001-all-level3", 500.0)  # another run's save, the newest before this run
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    sw, r = rig.h.saves[run_dir("b")], rig.runners[-1]
    assert not sw.check() and sw.known == "boot-start"
    r.boot = None  # the Game Over box's Main Menu: no server Lua state
    assert not sw.check()
    r.boot = "boot-continue"  # the main menu's Continue loaded a save
    rig.now[0] = 1010.0
    assert not sw.check() and r.until_hit is None  # a load_game may still explain it (grace)
    rig.now[0] = 1010.0 + H.LOAD_GRACE_S
    assert sw.check() and r.until_hit is not None and r.until_hit["id"] == H.FOREIGN_SAVE
    assert r.marks == [("load", {"save": None, "foreign_save": sw.foreign})]  # the facts' load mark
    assert ("foreign_save", {"why": sw.foreign, "session": "S001"}) in r.events
    rec = rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert rec["outcome"] == "gave_up" and rec["foreign_save"] == sw.foreign and rec["reloads"] == 1
    assert "the main menu's Continue or Load page" in rec["foreign_save"]
    assert any(f["kind"] == "gave_up" and f["detail"] == sw.foreign for f in rec["failures"])


def test_record_decides_a_load_seen_since_the_last_poll(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    rig.runners[-1].boot = "boot-continue"  # loaded after the last poll, the run ends before the grace is up
    assert rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())["foreign_save"]


def test_a_pass_on_a_foreign_save_is_still_invalid(rig: Rig):
    said: list[str | None] = [None]
    rig.h.start({**JOB, "verdict": lambda: said[0]}, SAVE_SC, run_dir("b"))
    r = rig.runners[-1]
    r.boot, said[0] = "boot-continue", "passed"  # the other save already holds the goal
    assert rig.h.watches[run_dir("b")].check() and r.until_hit is not None and r.until_hit["verdict"] == "passed"
    rec = rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert rec["outcome"] == "gave_up" and rec["foreign_save"]  # bg3bench results.validity: invalid, foreign_save


def test_the_agents_load_game_of_its_own_save_is_the_runs(rig: Rig):
    save(START, 400.0)  # older than the run, still its save
    save("Lae'zel-77__S001-before-gate", 1050.0)  # written in this run
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    sw, r = rig.h.saves[run_dir("b")], rig.runners[-1]
    r.boot, rig.now[0] = "boot-own", 1100.0
    assert not sw.check()  # seen before load_game answered
    agent_loaded("S001-before-gate")
    rig.now[0] = 1120.0
    assert not sw.check() and sw.known == "boot-own" and r.until_hit is None
    assert r.marks == [("load", "S001-before-gate")]
    r.boot, rig.now[0] = "boot-own-2", 1200.0  # the start save again
    agent_loaded(START, 1201.0)
    assert not sw.check() and sw.known == "boot-own-2"
    rec = rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert rec["outcome"] == "timeout" and "foreign_save" not in rec
    assert rec["reloads"] == 2  # bg3bench result.json's reloads (the record had none: always 0)


def test_record_waits_for_the_poll_in_flight_and_counts_its_load_once(rig: Rig):
    # the watch thread is deciding a new boot (its load_game lookup in flight) when the run ends: record's final
    # check must not decide the same load again (reloads 2, two load marks)
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    r = rig.runners[-1]
    in_poll, release, final_looked = threading.Event(), threading.Event(), threading.Event()

    def loads(since: float) -> list[str]:
        if threading.current_thread() is sw.thread:
            in_poll.set()
            release.wait()
        else:
            final_looked.set()
        return ["S001-before-gate"]

    sw = H.SaveWatch(r, lambda label: True, loads=loads, clock=lambda: rig.now[0], poll_s=0)
    rig.h.saves[run_dir("b")] = sw
    sw.start()  # the start's boot
    r.boot = "boot-own"  # the agent's load_game
    assert in_poll.wait(10)
    ender = threading.Thread(target=rig.h.record, args=(JOB, SAVE_SC, run_dir("b"), outcome()))
    ender.start()
    assert not final_looked.wait(0.2)  # record waits for the poll instead of deciding the load itself
    release.set()
    ender.join()
    assert sw.reloads == 1 and r.marks == [("load", "S001-before-gate")]


def test_the_agents_load_game_of_a_save_from_before_the_run_is_foreign(rig: Rig):
    save("Lae'zel-12__S001-old", 500.0)  # same owner, an earlier run's (the bridge's gate should refuse it)
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    sw, r = rig.h.saves[run_dir("b")], rig.runners[-1]
    r.boot = "boot-old"
    agent_loaded("S001-old")
    assert sw.check() and "loaded S001-old, which is not the run's" in str(sw.foreign)
    assert r.marks == [("load", {"save": "S001-old", "foreign_save": sw.foreign})]


def test_record_moves_the_runs_saves_out_of_the_story_folder(rig: Rig):
    save(START, 400.0)
    save("Tav-5__S001-all-level3", 500.0)  # older than the run: another run's or the user's
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    save("Lae'zel-77__S001-before-gate", 1050.0)
    save("Lae'zel-78__AutoSave_17", 1060.0)
    rec = rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert story() == [START, "Tav-5__S001-all-level3"]
    mine = ["Lae'zel-77__S001-before-gate", "Lae'zel-78__AutoSave_17"]
    assert rec["reset"]["saves"] == mine and sorted(p.name for p in (run_dir("b") / "saves").iterdir()) == mine
    assert (run_dir("b") / "saves" / mine[0] / "S001-before-gate.lsv").stat().st_size > 0  # moved, not copied
    assert not H.saves_marker().exists()


def test_an_autosave_slot_the_run_rewrote_is_the_runs(rig: Rig):
    slot = save("Lae'zel-78__AutoSave_17", 500.0)
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    write_lsv(slot / "AutoSave_17.lsv", 1070.0)  # the game rewrote the slot in this run
    rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert story() == [] and (run_dir("b") / "saves" / "Lae'zel-78__AutoSave_17").is_dir()


def test_abort_and_a_failed_start_move_the_runs_saves_too(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("a"))
    save("Lae'zel-77__S001-a", 1050.0)
    rig.h.abort()
    assert story() == [] and (run_dir("a") / "saves" / "Lae'zel-77__S001-a").is_dir()

    def prepare(r: FakeRunner) -> None:
        def settings() -> None:
            save("Lae'zel-79__AutoSave_3", 1040.0)  # the game saved while it came up
            raise RuntimeError("difficulty Explorer, not Balanced")

        r.check_settings = settings  # pyright: ignore[reportAttributeAccessIssue]

    rig.prepare = prepare
    with contextlib.suppress(RuntimeError):
        rig.h.start(JOB, SAVE_SC, run_dir("f"))
    assert story() == [] and (run_dir("f") / "saves" / "Lae'zel-79__AutoSave_3").is_dir()
    assert not H.saves_marker().exists() and not rig.h.saves


def test_the_next_start_moves_the_saves_a_dead_run_left(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("dead"))  # the process dies here: no record, no abort
    save("Lae'zel-77__S001-dead", 1050.0)

    def no_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        return {}

    host = H.LiveRunHost(lambda opt: FakeRunner(opt, []), run_files=no_files, env={}, clock=lambda: 2000.0)
    host.save_watch = lambda r, own: H.SaveWatch(r, own, poll_s=1e9)  # pyright: ignore[reportAttributeAccessIssue]
    host.start(JOB, SAVE_SC, run_dir("next"))
    assert story() == [] and (run_dir("dead") / "saves" / "Lae'zel-77__S001-dead").is_dir()
    assert json.loads(H.saves_marker().read_text())["dest"] == str(run_dir("next") / "saves")
    host.abort()


def test_a_restored_copy_of_an_earlier_runs_save_is_not_the_runs(rig: Rig):
    """B3 job 9 (16:02:37): Steam's local cloud put the 33 saves earlier runs had moved aside back into Story at the
    launch, new folders with mtime = the launch and the save inside unchanged. run_host.own_save counted them as the
    run's by mtime: the record moved all 33 (523 MB) into the run dir, and a load_game of one passed as an own save."""
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    save("Tav-502512612935__S001-all-level3", 1005.0, saved=500.0)  # restored at the game's launch
    save("Lae'zel-77__S001-before-gate", 1050.0)  # the run's own
    sw, r = rig.h.saves[run_dir("b")], rig.runners[-1]
    r.boot = "boot-restored"
    agent_loaded("S001-all-level3")
    assert sw.check() and "loaded S001-all-level3, which is not the run's" in str(sw.foreign)
    rec = rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())
    assert rec["reset"]["saves"] == ["Lae'zel-77__S001-before-gate"]
    assert story() == ["Tav-502512612935__S001-all-level3"]  # left where Steam put it


def test_a_save_whose_meta_cant_be_read_is_not_the_runs(rig: Rig):
    rig.h.start(JOB, SAVE_SC, run_dir("b"))
    d = save("Lae'zel-77__S001-x", 1050.0)
    (d / "S001-x.lsv").write_bytes(b"junk")  # no SaveTime: nothing says this run made it
    assert not H.own_save("S001-x", H.rcode.list_saves(), 1000.0, None)
    assert rig.h.record(JOB, SAVE_SC, run_dir("b"), outcome())["reset"]["saves"] == []
    assert story() == ["Lae'zel-77__S001-x"]
