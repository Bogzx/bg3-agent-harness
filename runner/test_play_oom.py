#!/usr/bin/env python3
"""runner/play.py's OOM guard: the game's OOM kills (journal, ramwatch spikes and kills), the consecutive-OOM streak
per save, the blocked save and the kickoff notes. Moved from test_play.py. Never launches claude or the game.

    python3 runner/test_play_oom.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))  # RUN-9: never the real one
import legacy_playthrough as lpt
from bg3harness.game import oom as O  # noqa: N812  (the OOM check's journal and ramwatch reads)
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate


class OomGuard(unittest.TestCase):
    """20:48 S038: the Shadowfell load blew the game up to 13.3 GB; the kernel OOM-killed it (and systemd every
    agent). The runner must spot a game OOM, and refuse a save that OOM-killed the game twice in a row."""

    J = (
        "1791654500.100000 host systemd[1740]: bg3ai-game-20261010-2000.scope: A process of this unit has been "
        "killed by the OOM killer.\n"
    )
    OTHER = (
        "1791654510.000000 host systemd[1740]: app-term@b676.service: A process of this unit has been killed "
        "by the OOM killer.\n"
    )

    def test_find_game_oom(self):
        self.assertIn("its scope bg3ai-game-20261010-2000.scope", O.find_game_oom(self.J, "20261010-2000", []))
        self.assertIsNone(O.find_game_oom(self.J, "20261010-2100", []))  # another launch's scope
        self.assertIsNone(O.find_game_oom(self.OTHER, "x", []))  # a test OOM'd in another unit
        spike = {"t": 1791654480.0, "from_mb": 5000, "to_mb": 13300, "seconds": 35}
        why = O.find_game_oom(self.OTHER, "x", [spike])  # legacy: no scope, but a spike
        self.assertIn("app-term@b676.service", why)
        self.assertIn("5000 → 13300 MB", why)
        self.assertIsNone(O.find_game_oom("garbage killed by the OOM killer\n", "x", [spike]))

    def test_streak(self):
        a = O.oom_streak_update(None, "S038", "oom")
        self.assertEqual(a["n"], 1)
        self.assertEqual(O.oom_streak_update(a, "S038", "oom")["n"], 2)
        self.assertEqual(O.oom_streak_update(a, "S037", "oom")["n"], 1)
        self.assertIsNone(O.oom_streak_update(a, "S038", None))  # a non-OOM death resets

    def _runner(self, d: Path, saves):
        self.saved = (
            rcode.list_saves,
            renv.log,
            O.user_journal,
            O.read_spikes,
            rstate.write_status,
            rpolicy.read_actions_since,
        )
        self.logs, self.events = [], []
        rcode.list_saves = lambda: saves
        renv.log = self.logs.append
        O.user_journal = lambda since: self.J
        O.read_spikes = lambda since, path=None: [{"t": 1.0, "tools": ["20:47:41 jump_to {} 12118ms → NOT READY"]}]
        rstate.write_status = lambda **f: None
        rpolicy.read_actions_since = lambda t, path=None: []
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("pt", None, 30, "opus", save_owner="Claude")
        r.mem = d
        r.pending_notes = []
        r.state = type(
            "S",
            (),
            {
                "dir": d,
                "data": {"save_owner": "Claude", "runner_saves": [], "loaded_save": "Claude-2__S038-shadowfell"},
                "__getitem__": lambda s, k: s.data.get(k),
                "__setitem__": lambda s, k, v: s.data.__setitem__(k, v),
            },
        )()
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (
                rcode.list_saves,
                renv.log,
                O.user_journal,
                O.read_spikes,
                rstate.write_status,
                rpolicy.read_actions_since,
            ) = self.saved

    def test_second_oom_from_the_same_save_refuses_it(self):
        saves = [
            rpolicy.Save("Claude-1__S037-end", "Claude", "S037-end", 10.0, 10.0),
            rpolicy.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0, 20.0),
        ]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["game"] = {"stamp": "20261010-2000", "launched": 1.0}
            self.assertIn("OOM-killed", r.check_game_oom())
            self.assertIsNone(r.check_game_oom())  # once per launch
            self.assertEqual(r.choose_save().label, "S038-shadowfell")  # 1st OOM: retried
            r.state["game"] = {"stamp": "20261010-2000", "launched": 2.0}
            r.state["oom_checked"] = None  # (2nd launch, same journal)
            r.check_game_oom()
            self.assertEqual(r.state["oom_streak"]["n"], 2)
            self.assertEqual(r.choose_save().label, "S037-end")  # 2nd: refused
            self.assertIn("oom_save_refused", self.events)
            self.assertTrue(any("REFUSING TO RESUME" in m for m in self.logs))
            self.assertIn("jump_to", r.pending_notes[-1])
            self.assertIn("S037-end", r.pending_notes[-1])

    def test_oom_line_names_the_save_loaded_last(self):
        # S082: the line named S078-start (the runner's load at the launch), not the agent's later S081-end reload
        saves = [
            rpolicy.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0, 20.0),
            rpolicy.Save("Claude-3__S039-end", "Claude", "S039-end", 30.0, 30.0),
        ]
        acts = [
            {
                "t": 40.0,
                "tool": "load_game",
                "args": {"save": "Claude-3__S039-end"},
                "result": "load_game: done — loaded=S039-end",
            },
            {"t": 50.0, "tool": "load_game", "args": {"save": "latest"}, "result": "NOT READY: game not reachable"},
        ]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            rpolicy.read_actions_since = lambda t, path=None: acts
            r.state["game"] = {"stamp": "20261010-2000", "launched": 1.0}
            r.check_game_oom()
            self.assertTrue(any("save loaded before it: Claude-3__S039-end" in m for m in self.logs))
            self.assertEqual(r.state["oom_streak"]["save"], "Claude-3__S039-end")

    def test_last_loaded_save(self):
        saves = [rpolicy.Save("Claude-1__S081-end", "Claude", "S081-end", 10.0, 10.0)]
        by_label = [{"t": 20.0, "tool": "load_game", "args": {"save": "S081-end"}, "result": "done loaded=S081-end"}]
        self.assertEqual(O.last_loaded_save(by_label, saves, "x"), "Claude-1__S081-end")
        self.assertEqual(O.last_loaded_save([], saves, "Claude-0__S078-start"), "Claude-0__S078-start")
        failed = [{"t": 20.0, "tool": "load_game", "args": {"save": "S081-end"}, "result": "FAILED: no such save"}]
        self.assertEqual(O.last_loaded_save(failed, saves, "fallback"), "fallback")
        # an earlier playthrough's S081-end that Steam's local cloud restored at the launch: mtime 15, SaveTime 5
        restored = rpolicy.Save("Claude-0__S081-end", "Claude", "S081-end", 15.0, 5.0)
        self.assertEqual(O.last_loaded_save(by_label, [*saves, restored], "x"), "Claude-1__S081-end")

    def test_refused_with_no_other_save_stops(self):
        saves = [rpolicy.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0, 20.0)]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["oom_blocked"] = ["Claude-2__S038-shadowfell"]
            with self.assertRaises(rstate.LaunchGaveUp):
                r.choose_save()

    def test_ramwatch_kill_counts_as_oom(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            O.user_journal = lambda since: ""
            O.read_spikes = lambda since, path=None: (
                [{"kind": "kill", "t": 5.0, "why": "game RSS 9600 MB >= 9500 MB"}] if path else []
            )
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIn("ramwatch killed the game", r.check_game_oom())

    def test_crash_note_names_the_last_three_calls(self):
        acts = [
            {"t": 10.0, "tool": "get_status", "args": {}, "result": "x"},
            {"t": 1791654449.2, "tool": "do_actions", "args": {"n": 3}, "result": "1. jump_to: FAILED\nSTOPPED"},
            {"t": 1791654481.5, "tool": "move_to", "args": {"pos": [-415.4, -0.2, -1436.6]}, "result": "done"},
            {
                "t": 1791654493.7,
                "tool": "jump_to",
                "args": {"x": -408.5, "y": 1, "z": -1437},
                "result": "NOT READY: timed out",
            },
            {"t": 1791654600.0, "tool": "get_status", "args": {}, "result": "after"},
        ]
        note = rpolicy.crash_tools_note("S038: game gone", acts, 1791654514.7)
        self.assertIn('jump_to {"x":-408.5,"y":1,"z":-1437}', note)
        self.assertIn("do_actions", note)
        self.assertNotIn("after", note)
        self.assertNotIn("get_status {}", note)
        self.assertIn("route/WAYS", note)

    def test_game_gone_at_start_warns_once(self):
        acts = [{"t": 99.0, "tool": "jump_to", "args": {"x": -408.5}, "result": "NOT READY"}]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            rpolicy.read_actions_since = lambda t, path=None: acts
            r.state["history"] = [{"kind": "play", "session": "S038", "ended": 100.0}]
            r.note_game_gone(None)
            r.note_game_gone(None)
            self.assertEqual(len(r.pending_notes), 1)
            self.assertIn("S038", r.pending_notes[0])
            self.assertIn("jump_to", r.pending_notes[0])
            r.pending_notes.clear()
            r.state["crash_noted"] = None
            r.state["game_killed_at"] = 200.0  # the runner killed it after S038 (age relaunch): no warning
            r.note_game_gone(None)
            self.assertEqual(r.pending_notes, [])

    def test_no_oom_resets(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            O.user_journal = lambda since: ""
            r.state["oom_streak"] = {"save": "Claude-2__S038-shadowfell", "n": 1}
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIsNone(r.check_game_oom())
            self.assertIsNone(r.state["oom_streak"])


if __name__ == "__main__":
    unittest.main()
