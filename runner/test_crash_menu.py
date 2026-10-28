#!/usr/bin/env python3
"""runner/play.py's game menu and load handling on a fake game (no BG3): the splash and LoadMenu states, a load that
never starts, and the restart drift check (moved from test_crash.py).

    python3 -m unittest runner/test_crash_menu.py
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # first: the game's profile folders are a temp dir before play

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import legacy_playthrough as lpt
from bg3ai import lsf, lspk
from bg3harness.game import controller as C  # noqa: N812  (the GameController and its timings)
from bg3harness.game import io as G  # noqa: N812
from bg3harness.game import oom as O  # noqa: N812  (the OOM check's journal and ramwatch reads)
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import game_side as rgame
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate

G_FUNCS = {
    n
    for n, v in vars(G).items()
    if callable(v) and getattr(v, "__module__", None) == G.__name__ and not isinstance(v, type)
}


CTL_NAMES = ("KILL_GRACE", "KILL_CONFIRM", "LAUNCH_BACKOFF", "MENU_POLL", "SPLASH_LOADMENU_S")
MOVED: dict[str, ModuleType] = {**dict.fromkeys(CTL_NAMES, C), **dict.fromkeys(("user_journal", "read_spikes"), O)}


def home(name: str):
    """The module a patched name lives in: the game's I/O functions moved to bg3harness.game.io, the GameController's
    timings to bg3harness.game.controller and the OOM check's reads to bg3harness.game.oom."""
    return MOVED.get(name) or (G if name in G_FUNCS else testenv.home(name))


def make_save(root: Path, folder: str, leader: str, saved: float, session: str) -> None:
    """A save folder whose .lsv holds a meta.lsf as BG3 writes it (LeaderName, SaveTime, GameSessionID)."""
    meta = lsf.build(
        [("MetaData", -1), ("MetaData", 0)],
        [
            ("GameSessionID", 22, 1, session.encode() + b"\0"),
            ("LeaderName", 23, 1, leader.encode() + b"\0"),
            ("SaveTime", 24, 1, struct.pack("<Q", int(saved))),
        ],
    )
    (root / folder).mkdir(parents=True)
    (root / folder / f"{folder.split('__', 1)[1]}.lsv").write_bytes(lspk.build([("meta.lsf", meta, "none")]))


class FakeMenuGame:
    """A game at the main menu's splash ("Press any key"): the client state flips LoadMenu/Menu (or stays LoadMenu),
    ui.state shows SplashScreen as a panel, ui.menu fails until a key gets past it; a load takes 3 polls."""

    def __init__(self, splash: bool = True, flip: bool = True, load_starts: bool = True):
        self.splash, self.flip, self.load_starts = splash, flip, load_starts
        self.polls, self.presses, self.loads, self.loading, self.running = 0, 0, [], 0, False
        self.load_gate = True  # the mod's persisted load gate (ui.config load_enabled)
        self.refuse: str | None = None  # ui.menu load raises this RpcError text

    def game_state(self, timeout: float = 0):
        self.polls += 1
        if self.running:
            return "Running"
        if self.loading:
            self.loading -= 1
            self.running = self.loading == 0
            return "Running" if self.running else "LoadSession"
        if self.splash:
            return ("LoadMenu", "Menu")[self.polls % 2] if self.flip else "LoadMenu"
        return "Menu"

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 0):
        if op == "ui.state":
            if self.running:
                return {"screen": "game"}
            return {"screen": "game", "panels_open": ["SplashScreen"]} if self.splash else {"screen": "main_menu"}
        if op == "ui.config" and args and "load_enabled" in args:
            self.load_gate = args["load_enabled"]
        if op == "ui.menu":
            if self.splash:
                raise rgame.RpcError("ui.menu: not at the main menu")
            if self.refuse:
                raise rgame.RpcError(self.refuse)
            if not self.load_gate:
                raise rgame.RpcError("ui.menu: loading is disabled")
            self.loads.append(args)
            self.loading = 3 if self.load_starts else 0
            return {}
        return {}

    def press(self, log=None) -> str:
        self.presses += 1
        self.splash = False
        return "fake Enter"


class SplashMenu(unittest.TestCase):
    """Live bug: a fresh game at the splash (state LoadMenu/Menu) made ensure_game wait 5+ min for Running.
    Now LoadMenu counts as the menu, the splash gets a key, and Running is awaited only after a load started."""

    NAMES = (
        "RUNTIME",
        "port_open",
        "game_state",
        "rpc",
        "available_ops",
        "server_ready",
        "list_saves",
        "press_menu_key",
        "host_character_name",
        "MENU_POLL",
        "SPLASH_LOADMENU_S",
        "LOAD_START_TIMEOUT",
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-splash-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {n: getattr(home(n), n) for n in self.NAMES}
        renv.RUNTIME = self.tmp / "runtime"
        C.MENU_POLL, C.SPLASH_LOADMENU_S, renv.LOAD_START_TIMEOUT = 0.01, 0.2, 1

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(home(n), n, v)

    def run_ensure(self, g: FakeMenuGame, saves: list[rpolicy.Save] | None = None):
        G.port_open = lambda *a, **k: True
        G.game_state, G.rpc, G.press_menu_key = g.game_state, g.rpc, g.press
        G.available_ops = lambda ctx: {"ui.state", "ui.menu"}
        G.server_ready = lambda: g.running
        G.host_character_name = lambda: "Claude"
        t = time.time() - 60
        rcode.list_saves = lambda: saves or [rpolicy.Save("Claude-1__S014-start", "Claude", "S014-start", t, t)]
        r = lpt.Runner(rstate.Options("splash-test", None, 20, "opus", save_owner="Claude"))
        r.mem = self.tmp / "memory"
        r.owns_game = lambda: True
        t0 = time.time()
        label = r.ensure_game(first=True)
        return r, label, time.time() - t0, (renv.RUNTIME / "runner.log").read_text()

    def test_splash_flipping_loadmenu_menu_gets_a_key_then_the_save_loads(self):
        g = FakeMenuGame()
        r, label, secs, log = self.run_ensure(g)
        self.assertEqual(label, "S014-start")
        self.assertEqual(g.presses, 1)
        self.assertEqual(len(g.loads), 1)
        self.assertTrue(g.running)
        self.assertEqual(r.state["loaded_save"], "Claude-1__S014-start")
        self.assertLess(secs, 5)
        self.assertNotIn("game is busy", log)
        self.assertIn("splash screen (state Menu): pressed fake Enter", log)

    def test_splash_stuck_in_loadmenu_gets_a_key_after_a_while(self):
        g = FakeMenuGame(flip=False)
        _r, label, _secs, log = self.run_ensure(g)
        self.assertEqual((label, g.presses), ("S014-start", 1))
        self.assertIn("splash screen (state LoadMenu)", log)

    def test_main_menu_gets_no_key(self):
        # a key on the main menu would hit Continue
        g = FakeMenuGame(splash=False)
        _r, label, _secs, _log = self.run_ensure(g)
        self.assertEqual((label, g.presses, len(g.loads)), ("S014-start", 0, 1))

    def test_the_row_hint_counts_the_playthrough_as_the_page_groups_it(self):
        # live N7 4b: PROBE-* copies of Vessa's saves keep her GameSessionID, so the Load page lists them in her
        # group; counted by folder prefix the hint for S003-1908 was row 4 for page row 5 (sazza-escape 22 for 24)
        t = time.mktime((2026, 10, 24, 19, 8, 29, 0, 0, -1))
        root = self.tmp / "saves"
        for folder, saved in (
            ("Claude-3__S004-end", t + 1253),
            ("PROBE-3__S004-end", t + 1253),
            ("Claude-2__S003-1908", t),
        ):
            make_save(root, folder, "Claude", saved, "c56ea03d-d05f-9a05-f248-607db4d436d3")
        make_save(root, "Claude-9__S001-other", "Claude", t + 9000, "another-playthrough")
        pick = rpolicy.Save("Claude-2__S003-1908", "Claude", "S003-1908", t, t)
        g = FakeMenuGame(splash=False)
        with mock.patch.object(renv, "SAVE_DIR", root):
            _r, label, _secs, _log = self.run_ensure(g, [pick])
        self.assertEqual(label, "S003-1908")
        (args,) = g.loads
        self.assertEqual((args["name"], args["owner"], args["index"]), ("Claude-2__S003-1908", "Claude", 3))
        self.assertEqual(args["date"], {"y": 2026, "m": 10, "d": 24, "H": 19, "M": 8})

    def test_load_gates_open_before_the_menu_load(self):
        # RUN-3: the mod refuses ui.menu load while its persisted load gate is off (a fresh profile's default)
        g = FakeMenuGame(splash=False)
        g.load_gate = False
        _r, label, _secs, _log = self.run_ensure(g)
        self.assertEqual((label, len(g.loads)), ("S014-start", 1))
        self.assertTrue(g.load_gate)

    def test_a_refused_menu_load_relaunches_the_game_with_the_save(self):
        # RUN-3: any refusal (gate, playthrough not on the page, eval failure) → kill + launch_and_load
        # (-loadSaveGame needs no gate), not a dead runner.
        g = FakeMenuGame(splash=False)
        g.refuse = "ui.menu: loading is disabled (bg3ai/config.json load_enabled=false)"
        relaunched = []
        real_runner = lpt.Runner

        class R(real_runner):
            def kill_game(self):
                relaunched.append("kill")

            def launch_and_load(self, save):
                relaunched.append(save.folder)
                return save

        lpt.Runner = R
        try:
            r, label, _secs, log = self.run_ensure(g)
        finally:
            lpt.Runner = real_runner
        self.assertEqual(label, "S014-start")
        self.assertEqual(relaunched, ["kill", "Claude-1__S014-start"])
        self.assertEqual(r.state["loaded_save"], "Claude-1__S014-start")
        self.assertIn("refused (ui.menu: loading is disabled", log)
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual(ev[-1]["kind"], "load_refused")

    def test_a_save_folder_gone_while_the_row_hint_is_read_relaunches_with_the_save(self):
        # app review of #33: load_args walks the save dir; an autosave rotated away between its listing and its stat
        # raised FileNotFoundError past the refusal handler and the runner died
        g = FakeMenuGame(splash=False)
        relaunched = []
        real_runner = lpt.Runner

        class R(real_runner):
            def kill_game(self):
                relaunched.append("kill")

            def launch_and_load(self, save):
                relaunched.append(save.folder)
                return save

        gone = FileNotFoundError(2, "No such file or directory", "Story/Claude-1__AutoSave_3")
        lpt.Runner = R
        try:
            with mock.patch.object(rgame.bsaves, "load_args", side_effect=gone):
                r, label, _secs, log = self.run_ensure(g)
        finally:
            lpt.Runner = real_runner
        self.assertEqual((label, g.loads), ("S014-start", []))
        self.assertEqual(relaunched, ["kill", "Claude-1__S014-start"])
        self.assertIn("Claude-1__AutoSave_3", log)
        ev = [json.loads(ln) for ln in (r.state.dir / "runner_events.jsonl").read_text().splitlines()]
        self.assertEqual((ev[-1]["kind"], ev[-1]["save"]), ("load_refused", "Claude-1__S014-start"))

    def test_a_load_that_never_starts_fails_fast_instead_of_waiting_for_running(self):
        g = FakeMenuGame(load_starts=False)
        t0 = time.time()
        with self.assertRaisesRegex(RuntimeError, "never started"):
            self.run_ensure(g)
        self.assertLess(time.time() - t0, 5)
        self.assertNotIn("waiting for game state Running", (renv.RUNTIME / "runner.log").read_text())


class RestartDrift(unittest.TestCase):
    """S059 08:45 / S063 10:51: a runner restart (test cycle) kept the running game, but acceptance had loaded its own
    saves in between; claude started on an Act-2 fixture. Now a restart compares the game with the last session's
    end_sig and loads the newest save when it moved; an unchanged game is kept (no reload)."""

    END = {  # noqa: RUF012  (read only)
        "level": "BGO_Main_A",
        "region": "R",
        "party_levels": ["9"],
        "pos": [15.3, 46.4, 224.3],
        "journal": "921b6e07b9fa",
        "journal_steps": 413,
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-drift-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        names = (*SplashMenu.NAMES, "read_progress")
        self.saved = {n: getattr(home(n), n) for n in names}
        self.addCleanup(lambda: [setattr(home(n), n, v) for n, v in self.saved.items()])
        renv.RUNTIME = self.tmp / "runtime"
        C.MENU_POLL, C.SPLASH_LOADMENU_S, renv.LOAD_START_TIMEOUT = 0.01, 0.2, 1

    def run_restart(
        self,
        now_sig: dict | None,
        take_over: bool = True,
        owned: bool = True,
        saves: list | None = None,
        agent_saves: list | None = None,
    ):
        g = FakeMenuGame(splash=False)
        g.running = True
        orig_rpc = g.rpc

        def rpc(op, args=None, context="server", timeout=0):
            if op == "ui.menu":
                g.running = False  # an in-game load: Running → loading → Running
            return orig_rpc(op, args, context, timeout)

        G.port_open = lambda *a, **k: True
        G.game_state, G.rpc, G.press_menu_key = g.game_state, rpc, g.press
        G.available_ops = lambda ctx: {"ui.state", "ui.menu"}
        G.server_ready = lambda: g.running
        G.host_character_name = lambda: "Claude"
        G.read_progress = lambda log=None: now_sig
        t = time.time() - 60
        rcode.list_saves = lambda: saves or [rpolicy.Save("Claude-1__S062-end", "Claude", "S062-end", t, t)]
        r = lpt.Runner(rstate.Options("drift-test", None, 20, "opus", save_owner="Claude", take_over=take_over))
        r.state["agent_saves"] = agent_saves
        r.mem = self.tmp / "memory"
        r.owns_game = lambda: owned
        r.state["loaded_save"] = "Claude-1__S062-end" if owned else None
        r.state["history"] = [{"kind": "play", "session": "S062", "ended": time.time() - 600, "end_sig": self.END}]
        label = r.ensure_game(first=True)
        logf = renv.RUNTIME / "runner.log"
        return g, label, logf.read_text() if logf.exists() else ""

    def test_fixture_loaded_in_between_reloads_the_newest_save(self):
        act2 = dict(self.END, level="SCL_Main_A", journal="79e1e111a061", journal_steps=219, pos=[635.3, 18.8, -1499.8])
        g, label, log = self.run_restart(act2)
        self.assertEqual((label, len(g.loads)), ("S062-end", 1))
        self.assertIn("the running game moved since the last session (level SCL_Main_A", log)

    def test_take_over_of_a_foreign_game_also_reloads(self):
        g, label, _log = self.run_restart(dict(self.END, pos=[115.3, 46.4, 224.3]), owned=False)
        self.assertEqual((label, len(g.loads)), ("S062-end", 1))

    def test_same_place_is_kept(self):
        g, label, log = self.run_restart(dict(self.END, pos=[16.0, 46.4, 226.0], party_levels=["1", "9"]))
        self.assertEqual((label, len(g.loads)), (None, 0))
        self.assertNotIn("moved since", log)

    def test_unreadable_state_fails_closed(self):
        # RUN-6: the progress read failed: the game isn't known to be where the session ended → reload (was: kept)
        g, label, log = self.run_restart(None)
        self.assertEqual((label, len(g.loads)), ("S062-end", 1))
        self.assertIn("progress can't be read", log)

    def test_agent_quicksave_is_the_reload_target(self):
        # RUN-6 (S059): the newest good state was the agent's own quicksave; pick_save used to skip every QuickSave_N
        now = time.time()
        saves = [
            rpolicy.Save("Claude-1__S062-end", "Claude", "S062-end", now - 600, now - 600),
            rpolicy.Save("Claude-1__QuickSave_7", "Claude", "QuickSave_7", now - 60, now - 60),  # the agent's, recorded
            rpolicy.Save("Claude-1__QuickSave_8", "Claude", "QuickSave_8", now - 30, now - 30),
        ]  # acceptance's (not)
        act2 = dict(self.END, level="SCL_Main_A")
        g, label, _log = self.run_restart(act2, saves=saves, agent_saves=["Claude-1__QuickSave_7"])
        self.assertEqual((label, len(g.loads)), ("QuickSave_7", 1))
        g, label, _log = self.run_restart(act2, saves=saves, agent_saves=None)
        self.assertEqual(label, "S062-end")  # unrecorded: never

    def test_party_order_change_is_not_a_move(self):
        # RUN-6: party[0] changed (a swap/selection) but Claude stands where the session ended → kept
        end = dict(self.END, pos_name="Claude", party_pos={"Claude": [15.3, 46.4, 224.3], "Gale": [17.0, 46.4, 226.0]})
        self.END = end
        now = dict(
            end,
            pos=[300.0, 46.4, 224.3],
            pos_name="Karlach",
            party_pos={"Karlach": [300.0, 46.4, 224.3], "Claude": [16.0, 46.4, 225.0]},
        )
        g, label, _log = self.run_restart(now)
        self.assertEqual((label, len(g.loads)), (None, 0))
        moved = dict(now, party_pos={"Karlach": [300.0, 46.4, 224.3], "Claude": [90.0, 46.4, 224.3]})
        self.assertIn("Claude stands 75 m from", rpolicy.state_drift(end, moved) or "")

    def test_state_drift(self):
        self.assertIsNone(rpolicy.state_drift(None, self.END))
        self.assertIn("journal_steps 414", rpolicy.state_drift(self.END, dict(self.END, journal_steps=414)) or "")
        self.assertIn("40 m from", rpolicy.state_drift(self.END, dict(self.END, pos=[55.3, 46.4, 224.3])) or "")


if __name__ == "__main__":
    unittest.main()
