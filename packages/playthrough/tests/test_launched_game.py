"""adapters/launched_game.py: the recovering game over a fake GameController, fake bridge reads and a save list."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from bg3ai import settings
from bg3ai.tools import core, saving
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

    def launch(self, load_folder: str | None) -> None:
        self.calls.append(f"launch {load_folder}")

    def wait_menu(self) -> bool:
        self.calls.append("main menu")
        return True

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


def save(owner: str, n: int, label: str, mtime: float, saved: float | None = None) -> Save:
    """A save as controller.list_saves gives it: its .lsv mtime and its meta.lsf SaveTime (default the same)."""
    return Save(f"{owner}-{n}__{label}", owner, label, mtime, mtime if saved is None else saved)


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


def test_a_series_earlier_run_save_steam_restored_is_never_the_runs(tmp_path: Path):
    # bench B3 (PR #14): Steam's local cloud restores a moved-aside save at a launch with mtime = the launch; its
    # meta.lsf SaveTime still says the series' previous run wrote it (before saves_after)
    restored = save(OWNER, 5, "S001-end", 2995, saved=500)
    state = PLAYED | {"saves_after": 900}
    ctl = FakeCtl()
    g, *_ = setup(tmp_path, state, [restored], handoff='Save to load: "S001-end"\n', ctl=ctl)
    with pytest.raises(RuntimeError, match="no save to load: no save of this playthrough"):
        g.recover("crashed")
    assert ctl.calls == []


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


def test_no_owner_is_learned_from_the_playthroughs_own_saves(tmp_path: Path):
    # a new playthrough's first session saved as its new character; another playthrough's newer save (outside this
    # one's play windows) is never the owner
    hist = {"history": [{"kind": "play", "session": "S001", "started": 1000, "ended": 3000}]}
    saves = [save("Vessa", 1, "S001-start", 1500), save("Karlach", 2, "S004-end", 5000)]
    ctl = FakeCtl()
    g, sdir, logs = setup(tmp_path, hist, saves, ctl=ctl)
    assert not g.is_new()
    assert g.recover("crashed") == "S001-start" and ctl.calls == ["kill", "launch Vessa-1__S001-start"]
    assert json.loads((sdir / "runner.json").read_text())["save_owner"] == "Vessa"
    assert "save owner Vessa: learned from this playthrough's own saves" in logs


def test_a_crash_mid_session_recovers_to_the_save_that_session_made(tmp_path: Path):
    # live9 S002: the running session has no history entry yet, so its S002-passage sat outside every play window
    # and the recovery loaded S001's save. The running session's window (begin..now) counts; a save of the same name
    # written between the sessions (another playthrough had the game) or another owner's save in the session never
    now = [5000.0]
    sdir, mem = tmp_path / "sessions" / "ilse", tmp_path / "ilse" / "memory"
    sdir.mkdir(parents=True)
    mem.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps(PLAYED))
    (mem / "HANDOFF.md").write_text('# Handoff S001 → S002\n\nSave to load: "S001-end"\n')
    saves = [save(OWNER, 1, "S001-end", 2900), save(OWNER, 2, "S027-end", 4000)]
    ctl = FakeCtl()
    g = lg.LaunchedGame(FakeReads(), ctl, FakeHost(), lambda: saves, lambda: None, clock=lambda: now[0])
    g.begin(sdir, mem)  # S002 starts at 5000
    saves += [save(OWNER, 3, "S002-passage", 5600), save("Karlach", 4, "S009-end", 5700)]
    now[0] = 6000.0  # the crash
    assert g.recover("crashed") == "S002-passage"
    assert ctl.calls == ["kill", f"launch {OWNER}-3__S002-passage"]
    saves.pop(2)  # without the session's own save: the previous session's, never the foreign ones
    assert g.recover("crashed") == "S001-end"


def test_a_new_playthroughs_crash_after_its_first_save_learns_the_owner_from_it(tmp_path: Path):
    now = [5000.0]
    sdir, mem = tmp_path / "sessions" / "ilse", tmp_path / "ilse" / "memory"
    sdir.mkdir(parents=True)
    mem.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps({"history": []}))
    saves = [save("Karlach", 1, "S004-end", 4000)]
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: saves, lambda: None, clock=lambda: now[0])
    g.begin(sdir, mem)
    saves.append(save("Vessa", 2, "S001-start", 5500))
    now[0] = 6000.0
    assert g.recover("crashed") == "S001-start"
    assert json.loads((sdir / "runner.json").read_text())["save_owner"] == "Vessa"


def test_a_save_steam_restored_at_the_launch_names_no_owner(tmp_path: Path):
    # bench B3: Steam restored another playthrough's save at this session's launch (5000): mtime = the launch, inside
    # the running window; its SaveTime (4000) is from before this playthrough began
    now = [6000.0]
    sdir, mem = tmp_path / "sessions" / "ilse", tmp_path / "ilse" / "memory"
    sdir.mkdir(parents=True)
    mem.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps({"history": []}))
    saves = [save("Karlach", 1, "S004-end", 5010, saved=4000)]
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: saves, lambda: None, clock=lambda: now[0])
    now[0] = 5000.0
    g.begin(sdir, mem)
    now[0] = 6000.0
    assert g.is_new()
    with pytest.raises(lg.NoSaveYetError):
        g.recover("crashed")


def test_a_playthrough_without_saves_is_new_and_launches_to_the_main_menu(tmp_path: Path):
    ctl = FakeCtl(alive=False)
    g, sdir, _ = setup(tmp_path, {"history": []}, [save("Karlach", 2, "S004-end", 5000)], ctl=ctl)
    assert g.is_new()  # another playthrough's save makes it no less new
    with pytest.raises(lg.NoSaveYetError):
        g.recover("wasn't answering at the session start")
    g.new_game()
    assert ctl.calls == ["kill", "launch None", "main menu"]
    assert json.loads((sdir / "runner.json").read_text())["loaded_save"] is None


class FakeSaveOps:
    """The game's quicksave: adds `write` (the folder the game makes; None: none appears) to the save list."""

    def __init__(self, saves: list[Save], clock: list[float], write: Save | None, ready: bool = True):
        self.saves, self.clock, self.write, self.is_ready = saves, clock, write, ready
        self.calls = 0

    def ready(self) -> bool:
        return self.is_ready

    def in_combat(self) -> bool | None:
        return True

    def quicksave(self) -> None:
        self.calls += 1
        if self.write is not None:
            self.saves.append(self.write)


def saving_game(
    tmp_path: Path,
    state: dict[str, Any],
    saves: list[Save],
    ops: FakeSaveOps,
    clock: list[float],
    actions: list[dict[str, Any]] | None = None,
):
    sdir, mem = tmp_path / "sessions" / "ilse", tmp_path / "ilse" / "memory"
    sdir.mkdir(parents=True)
    mem.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps(state))
    logs: list[str] = []

    def sleep(s: float) -> None:
        clock[0] += s

    acts = actions or []
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: saves, lambda: None, logs.append, ops,
                        lambda: clock[0], sleep, lambda since: [a for a in acts if a["t"] > since])  # fmt: skip
    g.begin(sdir, mem)
    return g, sdir, logs


def test_ensure_save_takes_the_sessions_own_save_and_learns_a_new_owner(tmp_path: Path):
    clock = [5000.0]
    saves = [save("Vessa", 1, "S000-old", 2000), save("Vessa", 2, "S001-start", 3100)]
    ops = FakeSaveOps(saves, clock, None)
    g, sdir, _ = saving_game(tmp_path, {}, saves, ops, clock)
    assert g.ensure_save(3000) == "Vessa-2__S001-start" and ops.calls == 0
    assert json.loads((sdir / "runner.json").read_text())["save_owner"] == "Vessa"


def test_without_a_session_save_the_runner_quicksaves_and_records_it(tmp_path: Path):
    # play.py ensure_save: a session that ended without the agent's save would send the next one back to an older save
    clock = [5000.0]
    saves = [save(OWNER, 1, "S001-end", 3100)]
    ops = FakeSaveOps(saves, clock, save(OWNER, 2, "QuickSave_1", 5000))
    g, sdir, logs = saving_game(tmp_path, {"save_owner": OWNER, "runner_saves": ["Ilse-0__QuickSave_0"]}, saves, ops,
                                clock)  # fmt: skip
    assert g.ensure_save(4000) == "Ilse-2__QuickSave_1" and ops.calls == 1
    rj = json.loads((sdir / "runner.json").read_text())
    assert rj["runner_saves"] == ["Ilse-0__QuickSave_0", "Ilse-2__QuickSave_1"]
    assert rj["runner_save_combat"] == {"Ilse-2__QuickSave_1": True}
    assert "quicksave written: Ilse-2__QuickSave_1 (mid-fight)" in logs


def test_the_runners_quicksave_is_the_new_folder_by_its_own_time_never_a_steam_restored_copy(tmp_path: Path):
    # a copy Steam restores while the runner waits is a new folder too, with mtime = when it appeared
    clock = [5000.0]
    saves = [save(OWNER, 1, "S001-end", 3100)]
    restored = save(OWNER, 9, "QuickSave_0", 5001, saved=900)

    class RestoringOps(FakeSaveOps):
        def quicksave(self) -> None:
            super().quicksave()
            self.saves.append(restored)

    ops = RestoringOps(saves, clock, save(OWNER, 2, "QuickSave_1", 5000))
    g, sdir, _ = saving_game(tmp_path, {"save_owner": OWNER}, saves, ops, clock)
    assert g.ensure_save(4000) == "Ilse-2__QuickSave_1"
    assert json.loads((sdir / "runner.json").read_text())["runner_saves"] == ["Ilse-2__QuickSave_1"]


def test_a_quicksave_that_never_appears_or_a_game_not_ready_records_nothing(tmp_path: Path):
    clock = [5000.0]
    ops = FakeSaveOps([], clock, None)
    g, sdir, logs = saving_game(tmp_path, {"save_owner": OWNER}, [], ops, clock)
    assert g.ensure_save(4000) is None and clock[0] >= 5000 + lg.QUICKSAVE_WAIT_S
    assert "quicksave didn't appear within 60 s" in logs and "runner_saves" not in json.loads(
        (sdir / "runner.json").read_text()
    )
    ops.is_ready = False
    assert g.ensure_save(4000) is None and ops.calls == 1  # not asked again


def test_the_agents_quicksaves_are_recorded_and_count_as_the_sessions_save(tmp_path: Path):
    # play.py record_agent_saves: choose_save takes a quicksave only when it is the runner's or the agent's
    clock = [5000.0]
    saves = [save(OWNER, 1, "S001-end", 3100), save(OWNER, 7, "QuickSave_3", 4500)]
    acts = [
        {"t": 3500, "tool": "save_game.bg", "ok": True, "folder": "Ilse-6__QuickSave_2"},  # before the session
        {"t": 4500, "tool": "save_game.bg", "ok": True, "folder": "Ilse-7__QuickSave_3"},
        {"t": 4600, "tool": "save_game.bg", "ok": True, "folder": "Karlach-8__QuickSave_1"},  # not this owner's
        {"t": 4700, "tool": "save_game.bg", "ok": False, "folder": "Ilse-9__QuickSave_4"},
    ]
    ops = FakeSaveOps(saves, clock, None)
    state = {"save_owner": OWNER, "agent_saves": ["Ilse-7__QuickSave_3", "Ilse-5__QuickSave_1"]}
    g, sdir, logs = saving_game(tmp_path, state, saves, ops, clock, acts)
    assert g.ensure_save(4000) == "Ilse-7__QuickSave_3" and ops.calls == 0  # no runner quicksave on top
    assert json.loads((sdir / "runner.json").read_text())["agent_saves"] == [
        "Ilse-5__QuickSave_1",
        "Ilse-7__QuickSave_3",
    ]
    assert "the agent's quicksave(s) Ilse-7__QuickSave_3 are this playthrough's saves too" in logs


# An app session's save identity reaches the bg3 server: save_env (policy.save_env, play.py's builder) read by the
# bridge (bg3ai.settings.save_identity) decides which folders its list_saves / load_game offer (_my_saves). live9
# S002: the app passed none, so list_saves was empty and load_game refused S002-passage-won.
VESSA = "Vessa Thornquill"
VESSA_HISTORY = [{"kind": "play", "session": "S001", "started": 1000, "ended": 3000}]


def bridge_saves(env: dict[str, str], save_dir: Path, runtime: Path) -> list[str]:
    """The folders the bridge's list_saves / load_game take under `env` (the server reads it once at start)."""
    ident = settings.save_identity(env)
    with (
        mock.patch.multiple(saving, SAVE_OWNER=ident.owner, SAVE_PREFIX=ident.prefix, PLAYTHROUGH=ident.playthrough,
                            SAVES_AFTER=ident.after, START_SAVE=ident.start_save, SAVES_EXCLUDE=ident.exclude),
        mock.patch.multiple(core, SAVE_DIR=save_dir, RUNTIME=runtime),
    ):  # fmt: skip
        return [d.name for d in saving._my_saves()] if saving.SAVE_PREFIX else []  # pyright: ignore[reportPrivateUsage]


def test_an_app_sessions_bridge_offers_the_playthroughs_own_saves_and_never_anothers(tmp_path: Path):
    save_dir, sdir, mem = tmp_path / "saves", tmp_path / "sessions" / "vessa", tmp_path / "vessa" / "memory"
    for d in (save_dir, sdir, mem):
        d.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps({"save_owner": VESSA, "history": VESSA_HISTORY}))
    saves = [
        Save(f"{VESSA}-1__S001-sazza-escape", VESSA, "S001-sazza-escape", 2000, 2000),  # S001's
        Save(f"{VESSA}-2__S027-end", VESSA, "S027-end", 4000, 4000),  # same name, another playthrough between sessions
        Save("Karlach-3__S009-end", "Karlach", "S009-end", 5200, 5200),  # another character
    ]
    for s in saves:
        (save_dir / s.folder).mkdir()
        os.utime(save_dir / s.folder, (s.mtime, s.mtime))
    now = [5000.0]
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: saves, lambda: None, clock=lambda: now[0])
    g.begin(sdir, mem)
    env = {"BG3AI_PLAYTHROUGH": "vessa", **g.save_env()}  # the session env's playthrough and save identity
    assert (env["BG3AI_SAVE_OWNER"], env["BG3AI_SAVE_PREFIX"]) == (VESSA, f"{VESSA}-")
    assert env["BG3AI_SAVES_EXCLUDE"] == f"{VESSA}-2__S027-end"
    # the session saves after the passage fight; its bridge offers that save and S001's, never the others
    (save_dir / f"{VESSA}-4__S002-passage-won").mkdir()
    os.utime(save_dir / f"{VESSA}-4__S002-passage-won", (5600, 5600))
    assert bridge_saves(env, save_dir, tmp_path / "runtime") == [
        f"{VESSA}-4__S002-passage-won",
        f"{VESSA}-1__S001-sazza-escape",
    ]
    # live9 S002's env (no identity): the bridge offered nothing
    assert bridge_saves({"BG3AI_PLAYTHROUGH": "vessa"}, save_dir, tmp_path / "runtime") == []


def test_a_new_playthrough_has_no_identity_yet_and_before_a_session_none(tmp_path: Path):
    sdir, mem = tmp_path / "sessions" / "new", tmp_path / "new" / "memory"
    g = lg.LaunchedGame(FakeReads(), FakeCtl(), FakeHost(), lambda: [], lambda: None)
    assert g.save_env() == {}
    sdir.mkdir(parents=True)
    (sdir / "runner.json").write_text(json.dumps({"history": []}))
    g.begin(sdir, mem)
    # the server adopts the name create_character types (as under play.py)
    assert g.save_env() == {"BG3AI_SAVE_OWNER": "", "BG3AI_SAVE_PREFIX": ""}
