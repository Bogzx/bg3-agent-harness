"""adapters/launched_game.py: the recovering game over a fake GameController, fake bridge reads and a save list."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bg3harness.game.io import Save
from bg3play.adapters import launched_game as lg

OWNER = "Ilse"


class FakeCtl:
    def __init__(self, alive: bool = True, oom: str | None = None):
        self.is_alive, self.oom_why = alive, oom
        self.calls: list[str] = []

    def alive(self) -> bool:
        return self.is_alive

    def kill(self) -> None:
        self.calls.append("kill")

    def launch_and_load(self, save: Save) -> Save:
        self.calls.append(f"launch {save.folder}")
        return save

    def check_oom(self) -> str | None:
        self.calls.append("check_oom")
        return self.oom_why


class FakeHost:
    def __init__(self):
        self.began: list[Path] = []
        self.pending: list[str] = []

    def begin(self, sdir: Path) -> None:
        self.began.append(sdir)

    def take_notes(self) -> list[str]:
        out, self.pending = self.pending, []
        return out


class FakeReads:
    def __init__(self, ok: bool = True):
        self.is_ok = ok

    def ok(self) -> bool:
        return self.is_ok

    def progress(self) -> dict[str, Any] | None:
        return {"level": "TUT"}


def save(owner: str, n: int, label: str, mtime: float) -> Save:
    return Save(f"{owner}-{n}__{label}", owner, label, mtime)


def setup(
    tmp_path: Path,
    state: dict[str, Any],
    saves: list[Save],
    handoff: str = "",
    ctl: FakeCtl | None = None,
    reads: FakeReads | None = None,
    leader: Callable[[], str | None] | None = None,
) -> tuple[lg.LaunchedGame, Path, list[str]]:
    sdir, mem = tmp_path / "sessions" / "ilse", tmp_path / "ilse" / "memory"
    sdir.mkdir(parents=True)
    mem.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps(state))
    if handoff:
        (mem / "HANDOFF.md").write_text(handoff)
    logs: list[str] = []
    g = lg.LaunchedGame(
        reads or FakeReads(), ctl or FakeCtl(), FakeHost(), lambda: saves, leader or (lambda: None), logs.append
    )
    g.begin(sdir, mem)
    return g, sdir, logs


PLAYED = {"save_owner": OWNER, "history": [{"kind": "play", "session": "S001", "started": 1000, "ended": 3000}]}


def test_recover_kills_and_loads_the_playthroughs_newest_save(tmp_path: Path):
    saves = [save(OWNER, 1, "S001-start", 1100), save(OWNER, 2, "S001-end", 2900), save("Karlach", 3, "S001-x", 2950)]
    ctl = FakeCtl()
    g, _, logs = setup(tmp_path, PLAYED, saves, ctl=ctl)
    assert g.recover("crashed") == "S001-end"  # never the other owner's newer save
    assert ctl.calls == ["kill", f"launch {OWNER}-2__S001-end"] and "S001-end" in logs[0]


def test_begin_names_the_hosts_session_and_recover_records_the_loaded_save(tmp_path: Path):
    # the controller's state is the session's runner.json; check_oom and previous_good_save read loaded_save
    saves = [save(OWNER, 1, "S001-start", 1100), save(OWNER, 2, "S001-end", 2900)]
    g, sdir, _ = setup(tmp_path, PLAYED, saves)
    assert isinstance(g.host, FakeHost) and g.host.began == [sdir]
    g.recover("crashed")
    rj = json.loads((sdir / "runner.json").read_text())
    assert rj["loaded_save"] == f"{OWNER}-2__S001-end" and rj["history"] == PLAYED["history"]
    g.host.pending = ["RUNNER NOTE: loaded the older save"]
    assert g.notes() == ["RUNNER NOTE: loaded the older save"] and g.notes() == []


def test_recover_refuses_a_save_from_elsewhere_or_stale_or_oom_blocked(tmp_path: Path):
    elsewhere = save(OWNER, 4, "S027-end", 9000)  # same name, written while another playthrough played
    blocked = save(OWNER, 3, "S001-late", 2990)
    old_run = save(OWNER, 1, "S001-end", 500)  # a series' previous run (before saves_after)
    good = save(OWNER, 2, "S001-mid", 2000)
    state = PLAYED | {"oom_blocked": [blocked.folder], "saves_after": 900}
    ctl = FakeCtl()
    g, *_ = setup(tmp_path, state, [elsewhere, blocked, old_run, good], ctl=ctl)
    assert g.recover("crashed") == "S001-mid"
    ctl.calls.clear()
    g2, *_ = setup(tmp_path / "b", state, [elsewhere, blocked, old_run], ctl=ctl)
    with pytest.raises(RuntimeError, match="no save to load: no save of this playthrough"):
        g2.recover("crashed")
    assert ctl.calls == []  # nothing killed when there is nothing to load


def test_recover_follows_handoffs_save_to_load(tmp_path: Path):
    saves = [save(OWNER, 1, "S001-end", 2000), save(OWNER, 2, "S001-fight", 2500)]
    handoff = '# Handoff S001 → S002\n\nSave to load: "S001-end" — before the fight\n'
    g, *_ = setup(tmp_path, PLAYED, saves, handoff=handoff)
    assert g.recover("crashed") == "S001-end"  # S001 wrote HANDOFF knowing S001-fight


def test_no_owner_known_refuses_any_save(tmp_path: Path):
    g, *_ = setup(tmp_path, {}, [save(OWNER, 1, "S001-end", 2000)])
    with pytest.raises(RuntimeError, match="no save owner known"):
        g.recover("crashed")


def test_the_first_game_that_answers_names_the_owner(tmp_path: Path):
    g, sdir, _ = setup(tmp_path, {"history": []}, [save(OWNER, 1, "S001-end", 2000)], leader=lambda: OWNER)
    assert g.ok()
    rj = json.loads((sdir / "runner.json").read_text())
    assert rj["save_owner"] == OWNER and rj["history"] == []
    assert g.recover("crashed") == "S001-end"


def test_fault_and_oom(tmp_path: Path):
    ctl, reads = FakeCtl(alive=True, oom="the kernel OOM-killed the game"), FakeReads(ok=True)
    g, *_ = setup(tmp_path, PLAYED, [], ctl=ctl, reads=reads)
    assert g.fault() is None and g.oom() is None and ctl.calls == []  # alive: no OOM check
    reads.is_ok = False
    assert g.fault() == "hang"
    ctl.is_alive = False
    assert g.fault() == "crash" and g.oom() == "the kernel OOM-killed the game"


def test_recover_before_a_session_began_refuses(tmp_path: Path):
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: [], lambda: None)
    with pytest.raises(RuntimeError, match="no session began"):
        g.recover("crashed")
