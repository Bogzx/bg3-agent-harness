"""create_character types the name and finishes (RUN-1), save identity and load_game("latest") (RUN-5),
the runner's save-gate decision (UI-2): the MCP tools against a fake game, a fake naming page and save folders.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_cc_saves.py'
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from test_naming import FakeInput, FakePage
from test_turn_macros import Base, FakeGame  # sets BG3AI_RUNTIME before importing server

from bg3ai import naming, server
from bg3ai.game import GameError, JobResult


class CCGame(FakeGame):
    """Character creation: ui.cc_apply (a job), the naming page (FakePage), then the world's party names."""

    def __init__(self, page: FakePage, world_name: str | None = "Ilse") -> None:
        super().__init__()
        self.page, self.world_name = page, world_name
        self.applied: list[dict] = []
        self.config: list[dict] = []
        self.waits: list[float] = []
        self.owner_seen: list[str] = []
        self.cc_open = True

    def run_job(self, op, args, context="server", wait_s=25.0, poll_s=0.01, on_start=None):
        if op == "ui.cc_apply":
            self.applied.append(json.loads(json.dumps(args)))
            self.waits.append(wait_s)
            if not self.cc_open or self.page.finished:
                return JobResult("failed", None, "character creation is not open (pages: )")
            return JobResult("done", {"set": {"race": "Elf"}, "complete": True})
        return super().run_job(op, args, context, wait_s, poll_s, on_start)

    def game_state(self):
        self.owner_seen.append(server.SAVE_OWNER)
        return "Running"

    def rpc(self, op, args=None, context="server", timeout=12.0):
        if op in ("ui.cc_name", "ui.cc_finish"):
            return self.page.rpc(op, args or {})
        if op == "ui.config":
            self.config.append(dict(args or {}))
            return {"gates": {}}
        if op == "ui.state":
            return {"screen": "game" if self.page.finished or not self.cc_open else "character_creation"}
        if op == "state.status":
            if not self.page.finished or self.world_name is None:
                raise GameError("server not ready")
            return {"party": [{"uuid": "p1", "name": self.world_name}]}
        return super().rpc(op, args, context, timeout)


@mock.patch.object(naming.time, "sleep", lambda s: None)
class CreateCharacter(Base):
    def setUp(self) -> None:
        super().setUp()
        for k, v in (("PLAYTHROUGH", "pt-new"), ("SAVE_OWNER", ""), ("SAVE_PREFIX", ""), ("CC_BUDGET_S", 0.3),
                     ("CC_NAMING_S", 0.1)):
            p = mock.patch.object(server, k, v)
            p.start()
            self.addCleanup(p.stop)

    def use(self, page: FakePage, world_name: str | None = "Ilse") -> tuple[CCGame, FakeInput]:
        g, inp = CCGame(page, world_name), FakeInput(page)
        server.game = g  # Base restores it
        p = mock.patch.object(server, "_inp", lambda: inp)
        p.start()
        self.addCleanup(p.stop)
        return g, inp

    def test_name_typed_finished_checked_and_owner_set(self):
        g, inp = self.use(FakePage())
        out = server.create_character(json.dumps({"class": "Wizard", "name": "Ilse"}))
        self.assertIn("character created and finished: 'Ilse' (name checked in game)", out)
        self.assertEqual(g.applied[0]["finish"], False)  # cc_apply never presses "Venture forth" itself
        self.assertEqual(g.applied[0]["plan"]["name"], "Ilse")
        self.assertIn("Ilse", inp.typed)
        self.assertTrue(g.page.finished)
        self.assertEqual([op for op, _ in g.page.calls][-1], "ui.cc_finish")
        self.assertEqual((server.SAVE_OWNER, server.SAVE_PREFIX), ("Ilse", "Ilse-"))  # I3

    def test_owner_is_the_default_name(self):
        server.SAVE_OWNER, server.SAVE_PREFIX = "Claude", "Claude-"
        g, inp = self.use(FakePage(), world_name="Claude")
        out = server.create_character(json.dumps({"class": "Paladin"}))
        self.assertIn("'Claude' (name checked in game)", out)
        self.assertIn("Claude", inp.typed)
        self.assertEqual(server.SAVE_PREFIX, "Claude-")

    def test_wrong_name_in_the_world_is_an_error(self):
        g, _ = self.use(FakePage(), world_name="Tav")
        out = server.create_character(json.dumps({"class": "Wizard", "name": "Ilse"}))
        self.assertTrue(out.startswith("FAILED: character creation finished, but the character in the world is "
                                       "called 'Tav', not 'Ilse'"), out)
        self.assertEqual(server.SAVE_PREFIX, "")  # not adopted

    def test_no_name_and_no_owner_is_refused(self):
        g, _ = self.use(FakePage())
        out = server.create_character(json.dumps({"class": "Wizard"}))
        self.assertIn("put the character's name in the plan", out)
        self.assertEqual(g.applied, [])
        self.assertFalse(g.page.finished)

    def test_name_other_than_the_owner_is_refused(self):
        server.SAVE_OWNER = "Claude"
        g, _ = self.use(FakePage())
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertIn("saves belong to 'Claude'", out)
        self.assertEqual(g.applied, [])

    def test_naming_failure_never_finishes(self):
        g, _ = self.use(FakePage(focus_on_click=False))
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertIn("didn't focus", out)
        self.assertIn("NOT finished", out)
        self.assertFalse(g.page.finished)
        self.assertEqual(server.SAVE_OWNER, "")

    def test_bad_name_refused_before_applying(self):
        g, _ = self.use(FakePage())
        out = server.create_character(json.dumps({"name": "x" * 21}))
        self.assertIn("longer than the page allows", out)
        self.assertEqual(g.applied, [])

    def test_unreadable_world_still_sets_the_owner_and_says_so(self):
        g, _ = self.use(FakePage(), world_name=None)
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertIn("hadn't loaded yet", out)
        self.assertEqual(server.SAVE_OWNER, "Ilse")

    def test_slow_world_returns_within_the_budget_with_the_owner_already_set(self):
        server.CC_BUDGET_S, server.CC_NAMING_S = 1.0, 0.4
        g, _ = self.use(FakePage(), world_name=None)  # the load never ends
        t0 = time.monotonic()
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertLessEqual(g.waits[0], 0.6)  # cc_apply's wait leaves room for naming
        self.assertIn("hadn't loaded yet", out)
        self.assertIn("Ilse", g.owner_seen)  # adopted before the read-back wait
        self.assertEqual(server.SAVE_PREFIX, "Ilse-")

    def test_second_call_after_a_finished_cc_says_so(self):
        g, _ = self.use(FakePage())
        server.create_character(json.dumps({"name": "Ilse"}))
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertIn("REFUSED: character creation isn't open: the character is already created ('Ilse'", out)

    def test_no_cc_and_no_world_points_to_new_game(self):
        g, _ = self.use(FakePage(), world_name=None)
        g.cc_open = False
        out = server.create_character(json.dumps({"name": "Ilse"}))
        self.assertIn("new_game first", out)

    def test_origin_without_name_takes_the_world_name(self):
        g, inp = self.use(FakePage(can_edit=False), world_name="Lae'zel")
        out = server.create_character(json.dumps({"origin": "Lae'zel"}))
        self.assertIn("'Lae'zel' (name checked in game)", out)
        self.assertEqual(inp.typed, [])
        self.assertEqual(server.SAVE_PREFIX, "Lae'zel-")

    def test_only_in_a_playthrough(self):
        server.PLAYTHROUGH = ""
        self.use(FakePage())
        self.assertIn("only available in an AI playthrough", server.create_character(json.dumps({"name": "Ilse"})))


class Saves(Base):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.tmp / "Story"
        self.dir.mkdir()
        for k, v in (("SAVE_DIR", self.dir), ("SAVE_PREFIX", "Claude-"), ("PLAYTHROUGH", "claude-run-3"),
                     ("RUNTIME", self.tmp / "rt"), ("SAVE_GATES", None)):
            p = mock.patch.object(server, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.loaded: list[str] = []
        for k, v in (("_enable_save_gates", lambda: None), ("_game_process_alive", lambda: True),
                     ("_server_boot", lambda: "b"),
                     ("_load_once", lambda save, *a: (self.loaded.append(save) or JobResult("done", {}), False))):
            p = mock.patch.object(server, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server.SAVER, "settle", lambda: "")
        p.start()
        self.addCleanup(p.stop)
        self.t = time.time() - 1000

    def save(self, name: str, age: int) -> None:
        d = self.dir / name
        d.mkdir()
        os.utime(d, (self.t + age, self.t + age))

    def test_latest_skips_test_saves_autosaves_and_other_characters(self):
        self.save("Claude-1__S003-pre-boss", 10)
        self.save("Claude-2__S004-end", 20)                   # this playthrough's newest Sxxx- save
        self.save("Claude-3__LIVETEST-dimension-door", 30)    # a worker's live test
        self.save("Claude-4__AutoSave_7", 40)                 # the game's autosave
        self.save("Claude-5__QuickSave_3", 50)                # nobody recorded it
        self.save("Claude-Two-6__S009-end", 60)               # another character whose name starts the same
        self.save("Tav-7__S010-end", 70)                      # another playthrough
        server.load_game("latest")
        self.assertEqual(self.loaded, ["Claude-2__S004-end"])
        listed = server.list_saves()
        self.assertIn("[Claude-2__S004-end]  <- latest", listed)
        self.assertNotIn("Claude-Two", listed)
        self.assertNotIn("Tav-7", listed)
        self.assertIn("LIVETEST", listed)  # listed (loadable by name), just never "latest"

    def test_our_own_quicksave_counts(self):
        self.save("Claude-1__S004-end", 10)
        self.save("Claude-2__QuickSave_3", 20)
        server._save_log({"save": "quicksave", "ok": True, "folder": "Claude-2__QuickSave_3"})
        server.load_game("latest")
        self.assertEqual(self.loaded, ["Claude-2__QuickSave_3"])

    def test_named_load_still_takes_any_of_ours(self):
        self.save("Claude-1__S004-end", 10)
        self.save("Claude-3__LIVETEST-x", 30)
        server.load_game("LIVETEST-x")
        self.assertEqual(self.loaded, ["Claude-3__LIVETEST-x"])
        self.assertIn("not one of this playthrough's saves", server.load_game("S010-end"))

    def test_a_series_run_never_sees_an_earlier_runs_saves(self):
        # run --runs: every run's character may be "Claude"; BG3AI_SAVES_AFTER (the run's start) keeps run 1's out
        self.save("Claude-1__S004-end", 10)                   # run 1
        with mock.patch.object(server, "SAVES_AFTER", self.t + 50):
            self.assertIn("no Sxxx- save", server.load_game("latest"))
            self.assertNotIn("S004-end", server.list_saves())
            self.save("Claude-2__S001-start", 60)             # run 2's own
            server.load_game("latest")
        self.assertEqual(self.loaded, ["Claude-2__S001-start"])

    def test_prefix_from_the_owner(self):
        self.assertEqual(server._env_prefix({"BG3AI_SAVE_OWNER": "Ilse"}), "Ilse-")
        self.assertEqual(server._env_prefix({"BG3AI_SAVE_OWNER": "Ilse", "BG3AI_SAVE_PREFIX": "Ilse-4"}), "Ilse-4")
        self.assertEqual(server._env_prefix({}), "")
        server.SAVE_PREFIX = ""
        self.assertIn("isn't known", server.load_game("latest"))
        self.assertIn("isn't known yet", server.list_saves())


class Gates(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sent: list[dict] = []
        orig = self.g.rpc
        self.g.rpc = lambda op, args=None, context="server", timeout=12.0: (
            self.sent.append(dict(args)) or {} if op == "ui.config" else orig(op, args, context, timeout))
        p = mock.patch.object(server, "PLAYTHROUGH", "pt")
        p.start()
        self.addCleanup(p.stop)

    def test_runner_closed_gates_refuse_and_never_open(self):
        with mock.patch.object(server, "SAVE_GATES", "0"):
            with self.assertRaisesRegex(GameError, "runner closed the gates"):
                server._enable_save_gates()
        self.assertNotIn(True, [a.get("save_enabled") for a in self.sent])

    def test_runner_opened_or_hand_run_open(self):
        for v in ("1", None):
            self.sent.clear()
            with mock.patch.object(server, "SAVE_GATES", v):
                server._enable_save_gates()
            self.assertEqual(self.sent, [{"save_enabled": True, "load_enabled": True}])


if __name__ == "__main__":
    unittest.main()
