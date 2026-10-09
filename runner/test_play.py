#!/usr/bin/env python3
"""Unit tests for runner/play.py's pure logic. Never launches claude or the game.

    python3 runner/test_play.py
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import play as P  # noqa: E402


class SessionNumbering(unittest.TestCase):
    def test_first_session(self):
        self.assertEqual(P.predict_session_number([], None), 1)

    def test_after_logs(self):
        self.assertEqual(P.predict_session_number([1, 2, 3], 3), 4)

    def test_state_ahead_of_logs(self):
        # a session whose log was never written still advances the counter (session.py does the same)
        self.assertEqual(P.predict_session_number([1, 2], 5), 6)

    def test_gaps(self):
        self.assertEqual(P.predict_session_number([1, 7, 3], None), 8)

    def test_ids(self):
        self.assertEqual(P.session_id(7), "S007")
        self.assertEqual(P.session_id(1234), "S1234")


class DeadlineMath(unittest.TestCase):
    def test_deadlines(self):
        d, hard = P.deadlines(1000.0, 60)
        self.assertEqual(d, 1000 + 3600)
        self.assertEqual(hard, d + 600)
        self.assertEqual(P.deadlines(0, 10, grace=30), (600, 630))

    def test_wrap_minutes(self):
        self.assertEqual(P.wrap_minutes(60), 50)
        self.assertEqual(P.wrap_minutes(10), 5)   # never before half-time
        self.assertEqual(P.wrap_minutes(30), 20)

    def test_budget_unbounded(self):
        self.assertEqual(P.session_budget(0, None, 60), 60)

    def test_budget_clipped_and_exhausted(self):
        now = 10_000.0
        self.assertEqual(P.session_budget(now, now + 3 * 3600, 60), 60)
        self.assertEqual(P.session_budget(now, now + 40 * 60, 60), 40)
        self.assertIsNone(P.session_budget(now, now + 14 * 60, 60))
        self.assertIsNone(P.session_budget(now, now - 5, 60))

    def test_kickoff_mentions_deadline_and_notes(self):
        t = time.mktime((2026, 10, 9, 21, 30, 0, 0, 0, -1))
        k = P.build_kickoff("S004", "pt1", t, "S003-end", ["NOTE A", "NOTE B"])
        self.assertIn("S004", k)
        self.assertIn("21:30", k)
        self.assertIn('"S003-end"', k)
        self.assertTrue(k.index("NOTE A") < k.index("NOTE B"))
        self.assertNotIn("already loaded", P.build_kickoff("S001", "pt1", t, None, []))


class HandoffAndSaves(unittest.TestCase):
    def test_parse_handoff(self):
        self.assertEqual(P.parse_handoff_save('# Handoff\nSave to load: "S042-end" — party at the well\n'), "S042-end")
        self.assertEqual(P.parse_handoff_save("Save to load: S042-end — x"), "S042-end")
        self.assertEqual(P.parse_handoff_save("Save to load: `S007-pre-boss` (out of combat)"), "S007-pre-boss")
        self.assertIsNone(P.parse_handoff_save("Save to load: (none — new game; start from the main menu)"))
        self.assertIsNone(P.parse_handoff_save("no such line"))
        # the template's example inside an HTML comment must be ignored
        self.assertIsNone(P.parse_handoff_save('<!--\nSave to load: "S042-end"\n-->\nSave to load: (none)\n'))

    def test_parse_folder(self):
        s = P.parse_save_folder("pibooool-10812619937__QuickSave_42", 5.0)
        self.assertEqual((s.owner, s.label, s.mtime), ("pibooool", "QuickSave_42", 5.0))
        s = P.parse_save_folder("Tav-Name-52012618851__S003-pre-boss")
        self.assertEqual((s.owner, s.label), ("Tav-Name", "S003-pre-boss"))
        self.assertIsNone(P.parse_save_folder("garbage"))

    def test_is_ai_label(self):
        self.assertTrue(P.is_ai_label("S001-start", ""))
        self.assertFalse(P.is_ai_label("QuickSave_3", ""))
        self.assertFalse(P.is_ai_label("Dank Crypt - 3h 15m", ""))
        self.assertTrue(P.is_ai_label("pt1-S010-end", "pt1-"))
        self.assertFalse(P.is_ai_label("S010-end", "pt1-"))

    def _saves(self):
        f = P.parse_save_folder
        return [
            f("pibooool-1__Dank Crypt - 3h 15m", 900),      # the user's own playthrough
            f("pibooool-2__QuickSave_42", 950),
            f("Ilse-3__S001-start", 100),
            f("Ilse-4__S001-end", 200),
            f("Ilse-5__QuickSave_1", 300),                 # a runner quicksave
            f("Other-6__S001-end", 400),                   # another playthrough, same label
        ]

    def test_pick_never_takes_foreign_saves(self):
        saves = self._saves()
        self.assertIsNone(P.pick_save(saves, None, None, set(), ""))
        best = P.pick_save(saves, None, "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")
        self.assertNotIn("pibooool", best.folder)

    def test_pick_includes_runner_quicksave(self):
        best = P.pick_save(self._saves(), "S001-end", "Ilse", {"Ilse-5__QuickSave_1"}, "")
        self.assertEqual(best.folder, "Ilse-5__QuickSave_1")

    def test_pick_by_handoff_before_owner_known(self):
        best = P.pick_save(self._saves(), "S001-start", None, set(), "")
        self.assertEqual(best.folder, "Ilse-3__S001-start")
        # with an owner, the other playthrough's identical label is excluded
        best = P.pick_save(self._saves(), "S001-end", "Ilse", set(), "")
        self.assertEqual(best.folder, "Ilse-4__S001-end")

    def test_saves_since(self):
        new = P.saves_since(self._saves(), 150, "Ilse", "")
        self.assertEqual([s.folder for s in new], ["Ilse-4__S001-end"])
        self.assertEqual(P.saves_since(self._saves(), 1000, None, ""), [])


class StuckDetection(unittest.TestCase):
    status = {"level": "SCL_Main_A", "region": "Shadow-Cursed Lands", "subregion": "Last Light Inn",
              "party": [{"level": 6, "xp": 100}, {"level": 6, "xp": 90}]}
    journal = [{"title": "Find the Nightsong", "steps": ["a", "b"]}, {"title": "Lift the Curse", "steps": ["x"]}]

    def test_signature(self):
        sig = P.progress_signature(self.status, self.journal)
        self.assertEqual(sig["level"], "SCL_Main_A")
        self.assertEqual(sig["party_levels"], ["6", "6"])
        self.assertEqual(sig["xp"], 190)
        self.assertEqual(sig["journal_steps"], 3)
        self.assertIsNone(P.progress_signature(None, None))
        self.assertEqual(P.progress_signature(None, {"quests": self.journal})["journal_steps"], 3)

    def test_changes_count_as_progress(self):
        a = P.progress_signature(self.status, self.journal)
        self.assertFalse(P.made_progress(a, P.progress_signature(self.status, self.journal)))
        moved = dict(self.status, subregion="Reithwin Town")
        self.assertTrue(P.made_progress(a, P.progress_signature(moved, self.journal)))
        more = self.journal + [{"title": "New", "steps": ["s"]}]
        self.assertTrue(P.made_progress(a, P.progress_signature(self.status, more)))
        xp = dict(self.status, party=[{"level": 6, "xp": 150}, {"level": 6, "xp": 90}])
        self.assertTrue(P.made_progress(a, P.progress_signature(xp, self.journal)))
        self.assertIsNone(P.made_progress(None, a))

    def test_is_stuck(self):
        h = lambda p, kind="play": {"progress": p, "kind": kind}  # noqa: E731
        self.assertFalse(P.is_stuck([]))
        self.assertFalse(P.is_stuck([h(False)]))
        self.assertTrue(P.is_stuck([h(True), h(False), h(False)]))
        self.assertFalse(P.is_stuck([h(False), h(True)]))
        self.assertFalse(P.is_stuck([h(False), h(None)]))          # unknown (crash) isn't "stuck"
        self.assertTrue(P.is_stuck([h(False), h(False, "fix"), h(False)]))  # fix sessions don't count

    def test_directive_mentions_location(self):
        sig = P.progress_signature(self.status, self.journal)
        d = P.stuck_directive([{"progress": False, "end_sig": sig}] * 2)
        self.assertIn("CHANGE APPROACH", d)
        self.assertIn("Last Light Inn", d)


class Misc(unittest.TestCase):
    def test_health_tracker(self):
        t = P.HealthTracker(3)
        for ok in (False, False):
            t.record(ok)
        self.assertFalse(t.hung)
        t.record(True)
        t.record(False)
        t.record(False)
        self.assertFalse(t.hung)
        t.record(False)
        self.assertTrue(t.hung)

    def test_stream_formatting(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Heading to the windmill."},
            {"type": "tool_use", "name": "mcp__bg3__move_to", "input": {"target": "e12"}}]}}
        self.assertEqual(P.format_stream_event(ev), ["[say] Heading to the windmill.",
                                                     '[tool] move_to {"target":"e12"}'])
        res = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "status: arrived"}]}]}}
        self.assertEqual(P.format_stream_event(res), ["[result] status: arrived"])
        end = {"type": "result", "subtype": "success", "num_turns": 12, "duration_ms": 120000,
               "total_cost_usd": 1.5}
        self.assertEqual(P.format_stream_event(end), ["[end] success turns=12 duration=2.0min cost=$1.50"])
        init = {"type": "system", "subtype": "init", "session_id": "abc", "model": "opus", "tools": [1, 2],
                "mcp_servers": [{"name": "bg3", "status": "connected"}]}
        self.assertIn("bg3:connected", P.format_stream_event(init)[0])
        self.assertEqual(P.format_stream_event({"type": "weird"}), [])

    def test_claude_command(self):
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus")
        cmd = r.claude_cmd("go", "1234", resume=False)
        self.assertEqual(cmd[:3], ["claude", "-p", "go"])
        self.assertIn("--session-id", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertIn("--resume", r.claude_cmd("fix", "1234", resume=True))


class KillGame(unittest.TestCase):
    """kill_game with every OS call mocked: nothing is signalled for real."""

    def setUp(self):
        self.saved = (P.pids_of_game, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM)
        self.sent = []
        self.alive = {4242}
        self.ignores_term = True

        def kill(pid, sig):
            self.sent.append((pid, sig))
            if sig == P.signal.SIGKILL or not self.ignores_term:
                self.alive.discard(pid)

        P.pids_of_game = lambda: sorted(self.alive)
        P.port_open = lambda *a, **k: bool(self.alive)
        P.os.kill = kill
        P.os.killpg = lambda pg, sig: self.sent.append(("pg", pg, sig))
        P.time.sleep = lambda s: None
        P.KILL_GRACE = 0.01
        P.KILL_CONFIRM = 0.01
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc = None
        self.r.state = type("S", (), {"data": {"game": {"pgid": 777}}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (P.pids_of_game, P.port_open, P.os.kill, P.os.killpg, P.time.sleep, P.KILL_GRACE, P.KILL_CONFIRM) = self.saved

    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        self.r.kill_game()
        sigs = [x[-1] for x in self.sent]
        self.assertIn(P.signal.SIGTERM, sigs)
        self.assertIn(P.signal.SIGKILL, sigs)
        self.assertIn(("pg", 777, P.signal.SIGTERM), self.sent)   # the launcher's group too
        self.assertIn((4242, P.signal.SIGKILL), self.sent)        # and the bg3 process itself
        self.assertIsNone(self.r.state["game"])

    def test_no_sigkill_when_sigterm_works(self):
        self.ignores_term = False
        self.r.kill_game()
        self.assertNotIn(P.signal.SIGKILL, [x[-1] for x in self.sent])

    def test_survivor_raises(self):
        P.os.kill = lambda pid, sig: self.sent.append((pid, sig))   # nothing ever dies
        with self.assertRaises(RuntimeError):
            self.r.kill_game()


class Gates(unittest.TestCase):
    def setUp(self):
        self.saved = (P.rpc, P.lua_eval, P.log)
        self.calls = []

    def test_decision(self):
        self.assertTrue(P.gates_decision(None, None, True))               # new game
        self.assertTrue(P.gates_decision("Claude", "Claude", False))
        self.assertTrue(P.gates_decision(" claude ", "Claude", False))
        self.assertFalse(P.gates_decision("pibooool", "Claude", False))   # the user's own save
        self.assertIsNone(P.gates_decision(None, "Claude", False))        # name unreadable
        self.assertIsNone(P.gates_decision("Claude", None, False))        # owner unknown

    def _runner(self, new_game=False, name="Claude"):
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus", save_owner="Claude")
        r.state = type("S", (), {"__getitem__": lambda s, k: None})()
        r.new_game = new_game
        self.calls = []
        P.log = lambda m: None
        P.lua_eval = lambda *a, **k: name
        return r

    def tearDown(self):
        P.rpc, P.lua_eval, P.log = self.saved

    def test_settings_errors_never_raise(self):
        r = self._runner()

        def boom(op, args=None, context="server", timeout=0):
            self.calls.append((op, args))
            raise RuntimeError("game not answering")
        P.rpc = boom
        r.apply_play_settings()                                            # must not raise
        self.assertEqual([c[0] for c in self.calls[:3]], ["ui.config", "ui.settings", "ui.dismiss"])
        self.assertEqual(self.calls[0][1], {"autoskip": True})
        self.assertEqual(self.calls[1][1], {"show_tutorials": False})

    def test_gates_follow_character(self):
        for name, new_game, expected in (("Claude", False, True), ("pibooool", False, False),
                                         ("", True, True), ("", False, None)):
            r = self._runner(new_game, name)
            P.rpc = lambda op, args=None, context="server", timeout=0: self.calls.append((op, args))
            r.apply_play_settings()
            gates = [a for op, a in self.calls if op == "ui.config" and "save_enabled" in a]
            if expected is None:
                self.assertEqual(gates, [], name)
            else:
                self.assertEqual(gates, [{"save_enabled": expected, "load_enabled": expected}], name)
            P.rpc, P.lua_eval, P.log = self.saved


class ClaudeSaves(unittest.TestCase):
    def test_claude_owner(self):
        f = P.parse_save_folder
        saves = [f("Claude-11__S001-start", 10), f("Claude-12__S002-end", 20), f("pibooool-9__S002-end", 30),
                 f("Claude-13__QuickSave_4", 40)]
        best = P.pick_save(saves, "S002-end", "Claude", set(), "")
        self.assertEqual(best.folder, "Claude-12__S002-end")
        self.assertEqual(saves[0].owner, "Claude")


if __name__ == "__main__":
    unittest.main(verbosity=1)
