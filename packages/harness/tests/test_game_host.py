"""bg3harness.game.host: the shared GameHost pieces (save filters, the mod snapshot, the status file, the event log)
and AppHost, alone and under a GameController with a fake launch. Temp dirs only: no game, no git, no real saves."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bg3ai import lsf, lspk
from bg3harness.game import controller as C  # noqa: N812
from bg3harness.game import host as H  # noqa: N812
from bg3harness.game import io as G  # noqa: N812
from bg3harness.game import oom as O  # noqa: N812
from bg3harness.runner import codewatch as rcode

NEW = G.Save("Claude-3__S003-end", "Claude", "S003-end", 300.0, 300.0)
MID = G.Save("Claude-2__S002-end", "Claude", "S002-end", 200.0, 200.0)
OLD = G.Save("Claude-1__S001-end", "Claude", "S001-end", 100.0, 100.0)
START = G.Save("Claude-0__start", "Claude", "start", 50.0, 50.0)
OTHER = G.Save("Karlach-9__S009-end", "Karlach", "S009-end", 400.0, 400.0)
# bench B3: Steam's local cloud restored an earlier run's moved-aside save at the launch (mtime = the launch)
RESTORED = G.Save("Claude-4__S001-all-level3", "Claude", "S001-all-level3", 350.0, 100.0)


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="bg3ai-host-"))


def commit_abc(repo: Path | None = None) -> str:
    return "abc"


def no_sleep(seconds: float) -> None:
    pass


class SaveFilters(unittest.TestCase):
    def test_own_saves_drop_saves_before_the_run_start_but_keep_its_start_save(self) -> None:
        saves = [NEW, MID, OLD, START]
        self.assertEqual(H.own_saves(saves, None, None), saves)
        self.assertEqual(H.own_saves(saves, 150.0, None), [NEW, MID])
        self.assertEqual(H.own_saves(saves, 150.0, START.folder), [NEW, MID, START])

    def test_own_saves_go_by_the_time_the_save_holds_not_its_mtime(self) -> None:
        unread = G.Save("Claude-5__S005-end", "Claude", "S005-end", 360.0)  # meta.lsf unreadable: saved None
        self.assertEqual(H.own_saves([RESTORED, unread, NEW], 150.0, None), [NEW])

    def test_list_saves_reads_each_saves_own_time(self) -> None:
        root = tmp()
        d = root / RESTORED.folder
        d.mkdir()
        meta = lsf.build(
            [("MetaData", -1), ("MetaData", 0)],
            [("LeaderName", 23, 1, b"Claude\0"), ("SaveTime", 24, 1, struct.pack("<Q", 100))],
        )
        (d / "S001-all-level3.lsv").write_bytes(lspk.build([("meta.lsf", meta, "none")]))
        os.utime(d / "S001-all-level3.lsv", (350.0, 350.0))
        (root / "Claude-5__S005-end").mkdir()
        (root / "Claude-5__S005-end" / "S005-end.lsv").write_bytes(b"junk")
        got = {s.folder: s for s in C.list_saves(root, rcode.save_lsv)}
        self.assertEqual(got[RESTORED.folder], RESTORED)
        self.assertIsNone(got["Claude-5__S005-end"].saved)

    def test_list_saves_refuses_a_python_that_cant_read_any_savetime(self) -> None:
        # Night N7: the system python3 3.13.5 has no compression.zstd (BG3's meta.lsf is zstd): every save came back
        # with saved None, save_candidates dropped them all and the bench gave up on all 6 runs, without one word why
        root = tmp()
        (root / RESTORED.folder).mkdir()
        with mock.patch.object(lspk, "zstd", None), self.assertRaises(C.SaveTimeError) as e:
            C.list_saves(root, rcode.save_lsv)
        self.assertIn("needs Python 3.14+", str(e.exception))
        with mock.patch.object(lspk, "zstd", None):
            self.assertIn("compression.zstd", C.savetime_problem() or "")
        with mock.patch.object(lspk, "zstd", object()):
            self.assertIsNone(C.savetime_problem())

    def test_ours(self) -> None:
        st = {"runner_saves": ["a"], "agent_saves": ["b"]}
        self.assertEqual(H.ours(_State(st), "c"), {"a", "b", "c"})
        self.assertEqual(H.ours(_State({}), None), set())

    def test_previous_good_save_is_the_last_loaded_one(self) -> None:
        st = _State({"loaded_save": MID.folder, "save_owner": "Claude"})
        self.assertEqual(H.previous_good_save([NEW, MID, OLD], {NEW.folder}, st, set(), [(0, 1000)]), MID)

    def test_previous_good_save_else_the_newest_other_save(self) -> None:
        st = _State({"loaded_save": MID.folder, "save_owner": "Claude"})
        got = H.previous_good_save([NEW, MID, OLD, OTHER], {NEW.folder, MID.folder}, st, set(), [(0, 1000)])
        self.assertEqual(got, OLD)  # never another owner's save
        self.assertIsNone(H.previous_good_save([NEW], {NEW.folder}, st, set(), [(0, 1000)]))

    def test_setup_env(self) -> None:
        self.assertEqual(H.setup_env(False), {})
        self.assertEqual(list(H.setup_env(True).values()), ["1"])


class _State(dict[str, Any]):
    def __getitem__(self, k: str) -> Any:
        return self.get(k)


class Snapshot(unittest.TestCase):
    def test_copies_again_when_a_merge_lands_during_the_copy(self) -> None:
        d = tmp()
        (d / "mod" / "Mods").mkdir(parents=True)
        (d / "mod" / "Mods" / "a.lua").write_text("x")
        commits = iter(["c1", "c2", "c2", "c2"])

        def commit(repo: Path | None = None) -> str:
            return next(commits)

        logs: list[str] = []
        with mock.patch.object(rcode, "repo_commit", commit):
            changed, rev = H.refresh_snapshot(d / "mod", d / "live", d, logs.append)
        self.assertEqual((changed, rev), (["Mods/a.lua"], "c2"))
        self.assertEqual((d / "live" / "Mods" / "a.lua").read_text(), "x")
        self.assertIn("1 files updated", logs[-1])


class Store(unittest.TestCase):
    def test_a_write_keeps_what_another_writer_added(self) -> None:
        path = tmp() / "runner.json"
        st = H.JsonStore(path)
        self.assertIsNone(st["game"])
        st["game"] = {"stamp": "s1"}
        path.write_text(json.dumps(json.loads(path.read_text()) | {"history": [{"kind": "play"}]}))  # the app
        st["loaded_save"] = NEW.folder
        d = json.loads(path.read_text())
        self.assertEqual(d, {"game": {"stamp": "s1"}, "history": [{"kind": "play"}], "loaded_save": NEW.folder})

    def test_bad_file_reads_empty(self) -> None:
        path = tmp() / "runner.json"
        path.write_text("[1]")
        self.assertIsNone(H.JsonStore(path)["x"])


class App(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime, self.repo = tmp(), tmp()
        self.sdir = self.runtime / "sessions" / "pt"
        self.sdir.mkdir(parents=True)
        self.logs: list[str] = []
        self.saves = [NEW, MID, OLD, OTHER]
        self.host = H.AppHost(self.runtime, self.repo, self.runtime / "saves", log=self.logs.append,
                              saves=lambda: self.saves)  # fmt: skip

    def test_paths(self) -> None:
        self.assertEqual(self.host.launch_sh, self.repo / "runner" / "launch_game.sh")
        self.assertEqual(self.host.mod_live, self.runtime / "mod-live")

    def test_state_needs_a_session(self) -> None:
        with self.assertRaises(RuntimeError):
            self.host.state["game"]
        self.host.begin(self.sdir)
        self.host.state["game"] = {"stamp": "s1"}
        self.assertEqual(json.loads((self.sdir / "runner.json").read_text()), {"game": {"stamp": "s1"}})

    def test_event_status_and_notes(self) -> None:
        self.host.begin(self.sdir)
        self.host.event("game_oom", why="oom")
        self.host.status(game_pgid=7)
        self.host.note("RUNNER NOTE: x")
        ev = json.loads((self.sdir / "runner_events.jsonl").read_text())
        self.assertEqual((ev["kind"], ev["why"]), ("game_oom", "oom"))
        self.assertEqual(json.loads((self.runtime / "runner.status.json").read_text())["game_pgid"], 7)
        self.assertEqual(self.host.take_notes(), ["RUNNER NOTE: x"])
        self.assertEqual(self.host.take_notes(), [])

    def test_saves_from_runner_json(self) -> None:
        self.host.begin(self.sdir)
        (self.sdir / "runner.json").write_text(json.dumps({
            "saves_after": 150.0, "save_owner": "Claude", "loaded_save": NEW.folder,
            "history": [{"kind": "play", "session": "S001", "started": 0, "ended": 1000}]}))  # fmt: skip
        self.assertEqual(self.host.own_saves(), [NEW, MID, OTHER])
        self.assertEqual(self.host.previous_good_save(set()), NEW)
        self.assertEqual(self.host.previous_good_save({NEW.folder}), MID)

    def test_mod_snapshot_and_hashes(self) -> None:
        (self.repo / "mod" / "Mods").mkdir(parents=True)
        (self.repo / "mod" / "Mods" / "a.lua").write_text("x")
        with mock.patch.object(rcode, "repo_commit", commit_abc):
            self.assertEqual(self.host.refresh_mod_snapshot(), ["Mods/a.lua"])
        self.assertEqual(self.host.mod_rev, "abc")
        self.assertEqual(list(self.host.mod_hashes()), ["Mods/a.lua"])

    def test_actions_from_the_runtime(self) -> None:
        (self.runtime / "actions.jsonl").write_text('{"t": 1, "tool": "a"}\n{"t": 5, "tool": "b"}\n')
        self.assertEqual([a["tool"] for a in self.host.read_actions_since(2)], ["b"])

    def test_aborted(self) -> None:
        self.assertFalse(self.host.aborted())
        host = H.AppHost(self.runtime, self.repo, self.runtime / "saves", aborted=lambda: True)
        self.assertTrue(host.aborted())


def oom_journal(since: float) -> str:
    return "1000.0 host systemd[1]: bg3ai-game-s1.scope: A process of this unit has been killed by the OOM killer.\n"


class UnderController(App):
    """A GameController on an AppHost: the launch retries fall back through runner.json, the OOM check writes it."""

    def test_falls_back_to_the_last_loaded_save(self) -> None:
        self.host.begin(self.sdir)
        (self.sdir / "runner.json").write_text(json.dumps({"save_owner": "Claude", "loaded_save": MID.folder}))
        ctl = C.GameController(self.host)
        launched: list[str] = []

        def launch(folder: str | None) -> None:
            launched.append(str(folder))

        def wait_for(what: str, pred: Any, timeout: float, interval: float = 2.0) -> bool:
            if launched[-1] == NEW.folder:
                raise G.GameDied("crash: signal 11")
            return True

        with (
            mock.patch.object(ctl, "launch", launch),
            mock.patch.object(ctl, "wait_for", wait_for),
            mock.patch.object(ctl, "kill", lambda: None),
            mock.patch.object(ctl, "sleep", no_sleep),
        ):
            self.assertEqual(ctl.launch_and_load(NEW), MID)
        self.assertEqual(launched, [NEW.folder, NEW.folder, MID.folder])
        self.assertIn("S002-end", self.host.take_notes()[0])

    def test_oom_check_keeps_the_apps_history(self) -> None:
        self.host.begin(self.sdir)
        history = [{"kind": "play", "session": "S001"}]
        (self.sdir / "runner.json").write_text(json.dumps({
            "history": history, "game": {"stamp": "s1", "launched": 900.0}, "loaded_save": NEW.folder}))  # fmt: skip
        with mock.patch.object(O, "user_journal", oom_journal):
            self.assertIn("bg3ai-game-s1.scope", C.GameController(self.host).check_oom() or "")
        d = json.loads((self.sdir / "runner.json").read_text())
        self.assertEqual((d["history"], d["oom_checked"], d["oom_streak"]["n"]), (history, "s1", 1))


if __name__ == "__main__":
    unittest.main()
