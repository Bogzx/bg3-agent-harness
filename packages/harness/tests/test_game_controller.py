"""bg3harness.game.controller with a fake host and a fake game: launch retries and the fallback save, the stale-bg3le
refusal, the abort and launcher-exit checks of wait_for, the OOM streak and the save block. No processes, no port."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bg3harness.game import controller as C  # noqa: N812
from bg3harness.game import io as G  # noqa: N812
from bg3harness.game import oom as O  # noqa: N812


class State(dict[str, Any]):
    def __getitem__(self, k: str) -> Any:
        return self.get(k)


class Host:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.launch_sh = runtime / "launch_game.sh"
        self.mod_live = runtime / "mod-live"
        self.stale_exit = 4
        self.rebuild_hint = "tools/build_bg3le.sh"
        self.state = State()
        self.logs: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.saves: list[G.Save] = []
        self.fallback: G.Save | None = None
        self.abort = False
        self.actions: list[dict[str, Any]] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def status(self, **fields: Any) -> None:
        self.statuses.append(fields)

    def steam_ready(self) -> tuple[bool, str]:
        return True, "test"

    def bg3le_problems(self) -> list[str]:
        return []

    def setup_env(self) -> dict[str, str]:
        return {}

    def refresh_mod_snapshot(self) -> list[str]:
        return []

    def mod_hashes(self) -> dict[str, str]:
        return {}

    def aborted(self) -> bool:
        return self.abort

    def own_saves(self) -> list[G.Save]:
        return self.saves

    def previous_good_save(self, failed: set[str]) -> G.Save | None:
        return self.fallback

    def read_actions_since(self, t: float) -> list[dict[str, Any]]:
        return self.actions

    def note(self, text: str) -> None:
        self.notes.append(text)


def _no_sleep(seconds: float) -> None:
    pass


NEW = G.Save("Claude-2__S002-end", "Claude", "S002-end", 200.0)
OLD = G.Save("Claude-1__S001-end", "Claude", "S001-end", 100.0)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.host = Host(Path(tempfile.mkdtemp(prefix="bg3ai-gc-")))
        self.ctl = C.GameController(self.host)


class LaunchAndLoad(Base):
    def run_launches(self, dies: set[str]) -> tuple[list[str], G.Save | Exception]:
        """Each launch of a folder in `dies` dies before the save plays; the rest load."""
        launched: list[str] = []

        def launch(folder: str | None) -> None:
            launched.append(str(folder))

        def wait_for(what: str, pred: Any, timeout: float, interval: float = 2.0) -> bool:
            if launched[-1] in dies:
                raise G.GameDied("crash: signal 11")
            return True

        with (
            mock.patch.object(self.ctl, "launch", launch),
            mock.patch.object(self.ctl, "wait_for", wait_for),
            mock.patch.object(self.ctl, "kill", lambda: None),
            mock.patch.object(self.ctl, "sleep", _no_sleep),
        ):
            try:
                return launched, self.ctl.launch_and_load(NEW)
            except Exception as e:  # the test inspects it
                return launched, e

    def test_first_launch_loads(self) -> None:
        launched, got = self.run_launches(set())
        self.assertEqual((launched, got), ([NEW.folder], NEW))
        self.assertEqual(self.host.notes, [])

    def test_third_try_loads_the_previous_good_save_with_a_note(self) -> None:
        self.host.fallback = OLD
        launched, got = self.run_launches({NEW.folder})
        self.assertEqual((launched, got), ([NEW.folder, NEW.folder, OLD.folder], OLD))
        self.assertEqual([k for k, _ in self.host.events], ["launch_failed", "launch_failed"])
        self.assertIn('the older save "S001-end" instead', self.host.notes[0])

    def test_gives_up_after_the_attempts(self) -> None:
        self.host.fallback = OLD
        launched, got = self.run_launches({NEW.folder, OLD.folder})
        self.assertEqual(len(launched), C.LAUNCH_ATTEMPTS)
        self.assertIsInstance(got, G.LaunchGaveUp)

    def test_abort_between_attempts(self) -> None:
        self.host.abort = True
        launched, got = self.run_launches({NEW.folder})
        self.assertEqual(launched, [NEW.folder])
        self.assertIsInstance(got, RuntimeError)


class Waits(Base):
    def test_abort_stops_a_wait(self) -> None:
        self.host.abort = True
        with self.assertRaisesRegex(RuntimeError, "aborted while waiting for x"):
            self.ctl.wait_for("x", lambda: False, 60, 0)

    def test_launcher_stale_exit_is_the_bg3le_refusal(self) -> None:
        proc = mock.Mock(returncode=4, poll=lambda: 4)
        self.ctl.proc = proc
        self.host.state["game"] = {"launched": 1e12}  # just launched
        with self.assertRaises(G.Bg3leStale):
            self.ctl.wait_for("debug port", lambda: False, 60, 0)
        self.assertEqual(self.host.events[0][0], "bg3le_stale")

    def test_other_launcher_exit_is_a_death(self) -> None:
        self.ctl.proc = mock.Mock(returncode=1, poll=lambda: 1)
        with self.assertRaisesRegex(G.GameDied, "status 1"):
            self.ctl.wait_for("debug port", lambda: False, 60, 0)

    def test_pred_true_returns(self) -> None:
        self.assertTrue(self.ctl.wait_for("x", lambda: True, 60, 0))


class Refusals(Base):
    def test_stale_bg3le_refused_loudly(self) -> None:
        with self.assertRaisesRegex(G.Bg3leStale, "missing 0009"):
            self.ctl.refuse_stale_bg3le(["missing 0009"])
        self.assertEqual(self.host.statuses[0], {"bg3le_stale": ["missing 0009"]})
        self.assertEqual(self.host.statuses[1]["alert"]["kind"], "bg3le_stale")
        self.assertIn("tools/build_bg3le.sh", self.host.logs[0])

    def test_current_bg3le_clears_the_status(self) -> None:
        self.ctl.refuse_stale_bg3le([])
        self.assertEqual(self.host.statuses, [{"bg3le_stale": None}])

    def test_steam_not_ready_never_launches(self) -> None:
        with (
            mock.patch.object(self.host, "steam_ready", lambda: (False, "online")),
            mock.patch.object(C.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(G.SteamNotOffline):
                self.ctl.launch(None)
            popen.assert_not_called()
        self.assertEqual(self.host.events, [("steam_refused", {"why": "online"})])


class Oom(Base):
    JOURNAL = "1000.0 host systemd[1]: bg3ai-game-s1.scope: A process of this unit has been killed by the OOM killer.\n"

    def check(self, journal: str) -> str | None:
        self.host.state["game"] = {"stamp": "s1", "launched": 900.0}
        self.host.state["loaded_save"] = NEW.folder

        def user_journal(since: float) -> str:
            return journal

        with mock.patch.object(O, "user_journal", user_journal):
            return self.ctl.check_oom()

    def test_no_game_record_no_check(self) -> None:
        self.assertIsNone(self.ctl.check_oom())
        self.assertIsNone(self.host.state["oom_checked"])

    def test_oom_starts_a_streak_once_per_launch(self) -> None:
        self.assertIn("its scope bg3ai-game-s1.scope", self.check(self.JOURNAL) or "")
        self.assertEqual(self.host.state["oom_streak"]["n"], 1)
        self.assertEqual(self.host.events[0][0], "game_oom")
        self.assertIsNone(self.check(self.JOURNAL))  # the same launch is checked once
        self.assertEqual(self.host.state["oom_streak"]["n"], 1)

    def test_second_oom_on_the_same_save_blocks_it(self) -> None:
        self.host.state["oom_streak"] = {"save": NEW.folder, "n": O.OOM_SAVE_LIMIT - 1}
        self.check(self.JOURNAL)
        self.assertEqual(self.host.state["oom_blocked"], [NEW.folder])

    def test_no_oom_resets_the_streak(self) -> None:
        self.host.state["oom_streak"] = {"save": NEW.folder, "n": 1}
        self.assertIsNone(self.check(""))
        self.assertIsNone(self.host.state["oom_streak"])

    def test_a_ramwatch_kill_counts(self) -> None:
        (self.host.runtime / "ram_kills.jsonl").write_text('{"t": 950.0, "kind": "kill", "why": "rss 9000 MB"}\n')
        self.assertIn("ramwatch killed the game", self.check("") or "")


class Watch(Base):
    def test_resume_watch_from_the_record(self) -> None:
        self.ctl.resume_watch()
        self.assertIsNone(self.ctl.watch)
        self.host.state["game"] = {"stamp": "s1", "launched": 5.0}
        self.ctl.resume_watch()
        self.assertIsNotNone(self.ctl.watch)


if __name__ == "__main__":
    unittest.main()
