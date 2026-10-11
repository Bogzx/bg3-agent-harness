#!/usr/bin/env python3
"""Unit tests for runner/play.py's pure logic. Never launches claude or the game.

    python3 runner/test_play.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from unittest import mock
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
        self.assertEqual(P.wrap_minutes(60), 57)
        self.assertEqual(P.wrap_minutes(30), 27)
        self.assertEqual(P.wrap_minutes(20), 17)
        self.assertEqual(P.wrap_minutes(6), 4)  # tiny harness sessions: wrap-up is a third of the session
        self.assertEqual(P.wrapup_minutes(90), 3)

    def test_early_exit_minutes(self):
        t0 = 1_000_000.0
        # claude-run-2 S002: exited cleanly after 12.6 of 20 min -> early exit with 7.4 min left
        self.assertEqual(P.early_exit_minutes("exited", t0, t0 + 12.6 * 60, 20), 7.4)
        self.assertIsNone(P.early_exit_minutes("exited", t0, t0 + 17.5 * 60, 20))  # normal wrap-up end
        self.assertIsNone(P.early_exit_minutes("exited", t0, t0 + 25 * 60, 20))    # overtime
        for hard in ("crash", "hang", "hard_stop", "aborted"):
            self.assertIsNone(P.early_exit_minutes(hard, t0, t0 + 60, 20))

    def test_short_sessions_refused(self):
        a = mock.Mock(session_minutes=15, allow_short_sessions=False)
        with mock.patch.object(P, "acquire_lock") as lock, mock.patch("sys.stderr"):
            self.assertEqual(P.cmd_run(a), 2)
        lock.assert_not_called()

    def test_budget_unbounded(self):
        self.assertEqual(P.session_budget(0, None, 60), 60)

    def test_budget_clipped_and_exhausted(self):
        now = 10_000.0
        self.assertEqual(P.session_budget(now, now + 3 * 3600, 60), 60)
        self.assertEqual(P.session_budget(now, now + 40 * 60, 60), 40)
        self.assertIsNone(P.session_budget(now, now + 14 * 60, 60))
        self.assertIsNone(P.session_budget(now, now - 5, 60))

    def test_kickoff_has_pace_preflight(self):
        k = P.build_kickoff("S004", "pt1", time.time() + 3600, None, [])
        self.assertIn("PACE PRE-FLIGHT", k)
        self.assertIn("Never pass wait=true", k)
        self.assertIn("stuck.md", k)
        self.assertNotIn(time.strftime("%Y-%m-%d"), P.PACE_PREFLIGHT)  # static: cache-friendly

    def test_kickoff_mentions_deadline_and_notes(self):
        t = time.mktime((2026, 10, 9, 21, 30, 0, 0, 0, -1))
        k = P.build_kickoff("S004", "pt1", t, "S003-end", ["NOTE A", "NOTE B"])
        self.assertIn("S004", k)
        self.assertIn("21:30", k)
        self.assertIn('"S003-end"', k)
        self.assertTrue(k.index("NOTE A") < k.index("NOTE B"))
        self.assertNotIn("already loaded", P.build_kickoff("S001", "pt1", t, None, []))


class HandoffAndSaves(unittest.TestCase):
    def test_load_menu_args(self):
        t = time.mktime((2026, 10, 9, 22, 39, 30, 0, 0, -1))
        saves = [P.Save("Claude-1__S001-start", "Claude", "S001-start", t - 400),
                 P.Save("Claude-2__S001-imps-done", "Claude", "S001-imps-done", t),
                 P.Save("Claude-3__S001-end", "Claude", "S001-end", t + 360),
                 P.Save("Tav-4__S002-end", "Tav", "S002-end", t + 900)]
        a = P.load_menu_args(saves[1], saves)
        self.assertEqual((a["action"], a["name"], a["owner"], a["index"]), ("load", "Claude-2__S001-imps-done", "Claude", 2))
        self.assertEqual(a["date"], {"y": 2026, "m": 10, "d": 9, "H": 22, "M": 39})

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

    def test_named_saves_with_a_folder_prefix(self):
        # BG3AI_SAVE_PREFIX=Claude- (docs/HANDOFF.md, the MCP server) names the folder, and the agent's
        # save_game labels are plain "S001-end": the session's named save must count (S001 quicksaved over it).
        saves = [P.parse_save_folder("Claude-10__S001-end", 200), P.parse_save_folder("Claude-11__QuickSave_0", 300),
                 P.parse_save_folder("Tav-12__S001-end", 250)]
        self.assertEqual([s.folder for s in P.saves_since(saves, 100, "Claude", "Claude-")], ["Claude-10__S001-end"])
        self.assertEqual(P.pick_save(saves, None, "Claude", set(), "Claude-").folder, "Claude-10__S001-end")
        # a label prefix still works, and a foreign character never matches
        self.assertTrue(P.is_ai_save(P.parse_save_folder("Ilse-1__pt1-S010-end"), "pt1-"))
        self.assertFalse(P.is_ai_save(P.parse_save_folder("Ilse-1__S010-end"), "pt1-"))
        self.assertFalse(P.is_ai_save(P.parse_save_folder("Tav-12__S001-end"), "Claude-"))

    def test_pick_skips_worker_saves(self):
        # 2026-10-10 live-test break: workers saved LIVETEST-* after S027-start; the playthrough's save stays the pick
        saves = [P.parse_save_folder("Claude-48__S027-start", 100), P.parse_save_folder("Claude-14__LIVETEST-act", 200),
                 P.parse_save_folder("Claude-12__LIVETEST-vis-open", 300), P.parse_save_folder("Claude-13__AutoSave_42", 250)]
        self.assertEqual(P.pick_save(saves, "S027-start", "Claude", set(), "Claude-").folder, "Claude-48__S027-start")

    def test_foreign_use_since(self):
        best = P.parse_save_folder("Claude-48__S027-start", 100)
        mine = [best, P.parse_save_folder("Claude-13__AutoSave_42", 150), P.parse_save_folder("Claude-9__S026-end", 90)]
        f = lambda saves=mine, acts=(), started=None, rs=set(): P.foreign_use_since(best, saves, list(acts), started, rs, "Claude-")
        self.assertIsNone(f())
        self.assertIn("LIVETEST-setup", f(mine + [P.parse_save_folder("Claude-21__LIVETEST-setup", 120)]))
        self.assertIsNone(f(mine + [P.parse_save_folder("Claude-21__LIVETEST-setup", 80)]))   # older: not a sign
        self.assertIsNone(f(mine + [P.parse_save_folder("Claude-5__QuickSave_1", 130)], rs={"Claude-5__QuickSave_1"}))
        load = {"t": 130, "tool": "load_game", "args": {"save": "Claude-9__S026-end"}, "result": "load_game: done — loaded=S026-end"}
        self.assertIn("S026-end", f(acts=[load]))                  # a worker loaded an older playthrough save
        self.assertIsNone(f(acts=[dict(load, t=90)]))              # before our save: irrelevant
        self.assertIsNone(f(acts=[dict(load, result="load_game: done — loaded=S027-start")]))
        self.assertIn("relaunched", f(started=110))
        self.assertIsNone(f(started=95))

    def test_read_actions_since(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "actions.jsonl"
            p.write_text('{"t": 5, "tool": "a"}\nnot json\n{"t": 15, "tool": "load_game"}\n')
            self.assertEqual([r["tool"] for r in P.read_actions_since(10, p)], ["load_game"])
            self.assertEqual(P.read_actions_since(10, Path(d) / "missing"), [])

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

    def test_session_progress_counts_points_and_far_moves(self):
        a = P.progress_signature(self.status, self.journal)
        # S040: islands descent + Balthazar (32 pts), wipe, reload: same save signature, still progress
        self.assertEqual(P.session_progress(a, dict(a), 32), (True, "32 story points"))
        self.assertEqual(P.session_progress(a, dict(a), 2)[0], False)            # S039: 2 explore points
        here = dict(self.status, party=[{"level": 6, "xp": 100, "pos": [0, 0, 0]}, {"level": 6, "xp": 90}])
        near = dict(here, party=[{"level": 6, "xp": 100, "pos": [20, 0, 0]}, {"level": 6, "xp": 90}])
        far = dict(here, party=[{"level": 6, "xp": 100, "pos": [0, -30, 70]}, {"level": 6, "xp": 90}])
        s0 = P.progress_signature(here, self.journal)
        self.assertEqual(s0["pos"], [0.0, 0.0, 0.0])
        self.assertFalse(P.made_progress(s0, P.progress_signature(near, self.journal)))   # a step isn't progress
        self.assertEqual(P.session_progress(s0, P.progress_signature(near, self.journal), 0)[0], False)
        ok, why = P.session_progress(s0, P.progress_signature(far, self.journal), 0)
        self.assertTrue(ok)
        self.assertIn("moved 76 m", why)
        ok, why = P.session_progress(a, dict(a, subregion="Reithwin Town"), 0)
        self.assertEqual((ok, why), (True, "changed subregion"))
        self.assertEqual(P.session_progress(None, a, 12), (True, "12 story points"))   # points alone tell it
        self.assertEqual(P.session_progress(None, a, 3), (None, "unknown"))
        self.assertEqual(P.session_progress(a, a, None)[0], False)

    def test_boundary_s(self):
        h = [{"kind": "play", "ended": 1000.0}, {"kind": "fix", "ended": 1100.0}]
        self.assertEqual(P.boundary_s(h, 1045.0), 45.0)          # fix entries are no play boundary
        self.assertIsNone(P.boundary_s(h, 1000.0 + P.BOUNDARY_MAX_S + 1))   # the runner was stopped
        self.assertIsNone(P.boundary_s([], 5.0))

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
        self.assertNotIn("--effort", cmd)  # Claude Code's default unless asked
        for flag in P.CLAUDE_FLAGS:
            self.assertIn(flag, cmd)

    def test_model_and_effort(self):
        import tempfile
        from types import SimpleNamespace
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 60, "opus", effort="medium", fix_model="sonnet", fallback_model="sonnet")
        with tempfile.TemporaryDirectory() as d:
            r.state = SimpleNamespace(dir=Path(d))
            cmd = r.claude_cmd("go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
            self.assertEqual(cmd[cmd.index("--fallback-model") + 1], "sonnet")
            fix = r.claude_cmd("fix", "1", resume=True, tag="-fix")
            self.assertEqual((fix[fix.index("--model") + 1], fix[fix.index("--effort") + 1]), ("sonnet", "low"))
            # session-config.json overrides the CLI for play sessions, re-read every time; bad values ignored
            (Path(d) / "session-config.json").write_text('{"model": "fable", "effort": "high"}')
            self.assertEqual(r.model_for(), ("fable", "high"))
            (Path(d) / "session-config.json").write_text('{"effort": "turbo", "model": 3}')
            self.assertEqual(r.model_for(), ("opus", "medium"))
            (Path(d) / "session-config.json").write_text("not json")
            self.assertEqual(r.model_for(), ("opus", "medium"))
            self.assertEqual(r.model_for("-fix"), ("sonnet", "low"))

    def test_autocompact(self):
        import tempfile
        from types import SimpleNamespace
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus")
        with tempfile.TemporaryDirectory() as d:
            r.state = SimpleNamespace(dir=Path(d))
            self.assertNotIn("--autocompact", r.claude_cmd("go", "1", resume=False))  # Claude Code's default
            r.opt = P.Options("pt", None, 30, "opus", autocompact=350_000)
            cmd = r.claude_cmd("go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--autocompact") + 1], "350000")
            self.assertNotIn("--autocompact", r.claude_cmd("fix", "1", resume=True, tag="-fix"))
            # session-config.json switches it live (A/B); out-of-range / non-int values are ignored
            (Path(d) / "session-config.json").write_text('{"autocompact": 200000}')
            cmd = r.claude_cmd("go", "1", resume=False)
            self.assertEqual(cmd[cmd.index("--autocompact") + 1], "200000")
            for bad in ('{"autocompact": 5000}', '{"autocompact": "200k"}', '{"autocompact": true}'):
                (Path(d) / "session-config.json").write_text(bad)
                cmd = r.claude_cmd("go", "1", resume=False)
                self.assertEqual(cmd[cmd.index("--autocompact") + 1], "350000")

    def test_cli_effort(self):
        with self.assertRaises(SystemExit):
            P.main(["run", "--playthrough", "x", "--effort", "turbo"])


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


class Relaunch(unittest.TestCase):
    """se's discriminator (2026-10-10 18:07-18:18): party ready checks break in a long-lived game process; the
    runner relaunches it between sessions (age ≥ GAME_MAX_AGE_H, or the last session logged a ready-check failure)."""

    def test_reason(self):
        now = 100_000.0
        self.assertIsNone(P.relaunch_reason(now - 3600, now, "fine", 2))
        self.assertIn("2.0 h old", P.relaunch_reason(now - 7200, now, "", 2))
        self.assertIsNone(P.relaunch_reason(now - 9 * 3600, now, "", 0))   # 0 = never by age
        why = P.relaunch_reason(now - 60, now, "EVENTS\n  rest_flow step=ready_failed:ReadyCheckContent_GoToNight\n", 2)
        self.assertIn("ready_failed:ReadyCheckContent_GoToNight", why)
        self.assertIn("end_day_failed", P.relaunch_reason(None, now, "rest_flow step=end_day_failed", 2))
        self.assertIsNone(P.relaunch_reason(None, now, None, 2))

    def test_pick_save_never_an_autosave(self):
        f = P.parse_save_folder
        saves = [f("Claude-1__S032-end", 10.0), f("Claude-2__AutoSave_59", 20.0), f("Claude-3__QuickSave_4", 30.0)]
        self.assertEqual(P.pick_save(saves, None, "Claude", set()).label, "S032-end")
        self.assertEqual(P.pick_save(saves, "AutoSave_59", "Claude", set()).label, "S032-end")   # not even by HANDOFF
        self.assertEqual(P.pick_save(saves, None, "Claude", {"Claude-3__QuickSave_4"}).label, "QuickSave_4")  # runner's own

    def _runner(self, d: Path, *, launched, log_text, save_mtime, last_started=50.0, steam=(True, "offline"),
                owns=True, first=False):
        self.saved = (P.port_open, P.steam_ready, P.list_saves, P.game_started_at, P.log)
        P.port_open = lambda *a, **k: True
        P.steam_ready = lambda: steam
        P.list_saves = lambda: [P.Save("Claude-1__S032-end", "Claude", "S032-end", save_mtime)]
        P.game_started_at = lambda: launched
        P.log = lambda m: self.logs.append(m)
        self.logs, self.killed, self.events = [], [], []
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus", save_owner="Claude", take_over=True)
        r.mem = d
        r.state = type("S", (), {"dir": d, "data": {"game": None, "save_owner": "Claude", "runner_saves": [],
                                                       "loaded_save": "x",
                                                       "history": [{"kind": "play", "session": "S032",
                                                                    "started": last_started}]},
                                  "__getitem__": lambda s, k: s.data.get(k),
                                  "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        (d / "S032.log").write_text(log_text)
        r.owns_game = lambda: owns
        r.kill_game = lambda: self.killed.append(1)
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (P.port_open, P.steam_ready, P.list_saves, P.game_started_at, P.log) = self.saved

    def test_relaunches_after_a_ready_check_failure(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="step=ready_failed:X", save_mtime=100.0)
            self.assertIn("ready_failed", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])
            self.assertIsNone(r.state["loaded_save"])
            self.assertIn("game_relaunch", self.events)

    def test_relaunches_an_old_process(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 5 * 3600, log_text="ok", save_mtime=100.0)
            self.assertIn("h old", r.maybe_relaunch(False))
            self.assertEqual(self.killed, [1])

    def test_young_healthy_process_kept(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])

    def test_kept_when_steam_not_offline(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="end_day_failed", save_mtime=100.0,
                             steam=(False, "Steam is not running"))
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertIn("game_relaunch_skipped", self.events)

    def test_kept_when_the_newest_save_predates_the_last_session(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ready_failed", save_mtime=10.0,
                             last_started=50.0)
            self.assertIsNone(r.maybe_relaunch(False))
            self.assertEqual(self.killed, [])
            self.assertTrue(any("lose play" in m for m in self.logs))


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




class Latency(unittest.TestCase):
    """Runner metrics: stream timing from a fake stream-json transcript + the end-of-session summary."""

    def test_stream_timing_and_summary(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "S001"
            timing = P.latency.StreamTiming(Path(f"{base}.turns.jsonl"))
            t0 = 1_760_000_000.0
            events = [
                (1.0, {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "a", "name": "mcp__bg3__move_to", "input": {"target": "e1"}}]}}),
                (4.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "done"}]}}),
                (5.5, {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "b", "name": "mcp__bg3__interact", "input": {"target": "e2"}}]}}),
                (7.0, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": "done"}]}}),
            ]
            for t, ev in events:
                timing.on_event(ev, t0 + t)
            Path(f"{base}.metrics.jsonl").write_text(json.dumps(
                {"type": "tool", "tool": "get_status", "t0": t0, "t1": t0 + 0.04, "ms": 40, "rpc_ms": 30, "rpc_n": 1}) + "\n")

            class FakeRunner:
                state = type("S", (), {"dir": d})()

            s = P.Runner.write_latency(FakeRunner(), "S001", base, t0, t0 + 60)
            self.assertEqual(s["think_s"]["median"], 1.5)
            self.assertEqual(s["source"], "stream")       # no action records from the bridge: stream fallback
            self.assertEqual(s["idle_s"]["all"]["median"], 1.5)
            self.assertTrue(s["targets"]["median_idle_explore_s"]["pass"])
            self.assertTrue(s["targets"]["bridge_rt_median_ms"]["pass"])
            self.assertTrue(Path(f"{base}.latency.json").exists())
            hist = (d / "latency_history.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(hist[0])["session"], "S001")

    def test_metrics_path_in_env(self):
        src = Path(P.__file__).read_text()
        self.assertIn('env["BG3AI_METRICS_PATH"] = f"{base}.metrics.jsonl"', src)

class Progress(unittest.TestCase):
    """Story progress: the session poller writes Sxxx.progress.jsonl, the end writes the summary + history."""

    def test_poller_and_summary(self):
        import json
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "S004"
            world = {"status": {"region": "Nautiloid", "party": [{"name": "Claude", "level": 1}]},
                     "journal": [{"title": "Escape", "steps": ["a"]}], "map": {"waypoints": []},
                     "markers": {"markers": []}, "ui": {"screen": "game", "region": "Hold"}}
            polls = []
            stop = threading.Event()

            def fake_read(_eval):
                polls.append(1)
                if len(polls) == 2:
                    world["journal"][0]["steps"].append("b")
                    world["status"]["region"] = "Wilderness"
                if len(polls) == 3:
                    raise OSError("loading screen")
                if len(polls) >= 4:
                    stop.set()
                return json.loads(json.dumps(world))

            class FakeRunner:
                state = type("S", (), {"dir": d})()

            orig_read, orig_poll = P.story.read_game, P.story.POLL_S
            P.story.read_game, P.story.POLL_S = fake_read, 0.001
            try:
                log = []
                human = type("H", (), {"write": lambda self, x: log.append(x)})()
                P.Runner.progress_loop(FakeRunner(), "S004", base, 0.0, stop, human)
            finally:
                P.story.read_game, P.story.POLL_S = orig_read, orig_poll
            self.assertTrue(any("+15 region: Wilderness" in x for x in log), log)
            self.assertTrue(any("+2 quest_step: Escape: b" in x for x in log), log)
            s = P.Runner.write_progress(FakeRunner(), "S004", base, 0.0, 1800.0)
            self.assertEqual(s["points"], 15 + 2 + 3)  # region, quest_step, area (Hold in a new region)
            self.assertEqual(s["per_hour"], 40.0)
            self.assertEqual(P.story_points(Path(f"{base}.progress.json")), 20)
            row = json.loads((d / "progress_history.jsonl").read_text())
            self.assertEqual(row["session"], "S004")
            self.assertTrue((d / "progress_state.json").exists())

    def test_wired_into_run_claude(self):
        src = Path(P.__file__).read_text()
        self.assertIn('progress=f"{base}.progress.jsonl"', src)
        self.assertIn('self.write_progress(f"{sid}{tag}", base, start, end)', src)


class AvatarName(unittest.TestCase):
    def test_reads_the_avatar_not_the_selection(self):
        seen = []
        saved = P.lua_eval
        try:
            P.lua_eval = lambda code, context="client", timeout=0: seen.append((code, context)) or " Claude \n"
            self.assertEqual(P.host_character_name(), "Claude")
        finally:
            P.lua_eval = saved
        code, ctx = seen[0]
        self.assertEqual(ctx, "server")
        self.assertIn("DB_Avatars", code)
        self.assertLess(code.index("DB_Avatars"), code.index("GetHostCharacter"))  # selection only as fallback

    def test_unreadable_is_none(self):
        saved = P.lua_eval
        try:
            P.lua_eval = lambda *a, **k: (_ for _ in ()).throw(OSError("no game"))
            self.assertIsNone(P.host_character_name())
        finally:
            P.lua_eval = saved


class HotReload(unittest.TestCase):
    def setUp(self):
        self.saved = (P.port_open, P.game_state, P.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes)
        self.logs, self.evals = [], []
        self.back_after = 2
        P.port_open = lambda *a, **k: True
        P.game_state = lambda: "Running"
        P.log = self.logs.append
        P.time.sleep = lambda s: None

        def ev(code, context="client", timeout=0):
            self.evals.append((code, context))
            if "Reset" in code:
                raise ConnectionError("dropped")
            self.back_after -= 1
            return "true" if self.back_after <= 0 else "false"
        P.lua_eval = ev
        self.r = P.Runner.__new__(P.Runner)
        self.r.game_proc, self.r.abort = None, False
        self.r.state = type("S", (), {"data": {}, "__getitem__": lambda s, k: s.data.get(k),
                                      "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()

    def tearDown(self):
        (P.port_open, P.game_state, P.lua_eval, P.log, P.time.sleep, P.mod_tree_hashes) = self.saved

    def test_hashes_and_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "x.lua").write_text("one")
            (root / "y.lua").write_text("two")
            h1 = P.mod_tree_hashes(root)
            self.assertEqual(set(h1), {"a/x.lua", "y.lua"})
            (root / "y.lua").write_text("2")
            (root / "z.lua").write_text("new")
            h2 = P.mod_tree_hashes(root)
            self.assertEqual(P.changed_files(h1, h2), ["y.lua", "z.lua"])
            self.assertEqual(P.changed_files(h2, h2), [])
            self.assertEqual(P.changed_files(None, h2), sorted(h2))
            (root / "a" / "x.lua").unlink()
            self.assertEqual(P.changed_files(h2, P.mod_tree_hashes(root)), ["a/x.lua"])

    def test_unchanged_does_nothing(self):
        P.mod_tree_hashes = lambda: {"a.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_changed_resets_and_waits_for_rpc(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2", "b.lua": "1"}
        self.r.state["mod_hashes"] = {"a.lua": "1", "b.lua": "1"}
        self.assertEqual(self.r.hot_reload_mod(), 1)
        self.assertEqual(self.evals[0], ("Ext.Debug.Reset() return 'reset'", "server"))
        self.assertTrue(any("Mods.BG3AI.BG3AI.RPC" in c for c, _ in self.evals[1:]))
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "2", "b.lua": "1"})
        self.assertTrue(any(m.startswith("mod hot-reloaded (1 files changed") for m in self.logs), self.logs)

    def test_mod_never_back_raises_and_keeps_old_hashes(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        self.r.state["mod_hashes"] = {"a.lua": "1"}
        self.back_after = 10 ** 9
        saved = P.RELOAD_TIMEOUT
        P.RELOAD_TIMEOUT = 0.05
        try:
            with self.assertRaises(RuntimeError):
                self.r.hot_reload_mod()
        finally:
            P.RELOAD_TIMEOUT = saved
        self.assertEqual(self.r.state["mod_hashes"], {"a.lua": "1"})  # retried next time

    def test_skipped_while_loading(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        P.game_state = lambda: "LoadLevel"
        self.assertEqual(self.r.hot_reload_mod(), 0)
        self.assertEqual(self.evals, [])

    def test_menu_uses_client_context(self):
        P.mod_tree_hashes = lambda: {"a.lua": "2"}
        P.game_state = lambda: "Menu"
        self.r.hot_reload_mod()
        self.assertEqual({c for _, c in self.evals}, {"client"})


class WaitWatchdog(unittest.TestCase):
    def test_reports_after_90s_then_every_30s(self):
        out = []
        w = P.WaitReporter("game state Running", 600, start=0.0, logf=out.append)
        fired = [t for t in range(0, 200, 5) if w.tick(float(t))]
        self.assertEqual(fired, [90, 120, 150, 180])
        self.assertIn("game state Running", out[0])
        self.assertIn("90 s so far, 510 s left", out[0])

    def test_short_wait_silent(self):
        out = []
        w = P.WaitReporter("x", 60, start=0.0, logf=out.append)
        self.assertFalse(any(w.tick(float(t)) for t in range(0, 61)))


class Triage(unittest.TestCase):
    def _session(self, d: Path) -> Path:
        base = d / "S009"
        use = lambda i, name, inp: {"type": "tool_use", "id": f"t{i}", "name": f"mcp__bg3__{name}", "input": inp}
        res = lambda i, text, err=False: {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err,
                                          "content": [{"type": "text", "text": json.dumps({"result": text})}]}
        calls = [
            (use(1, "move_to", {"x": 1}), res(1, "move_to: done")),
            (use(2, "use_ability", {"target": "e4"}), res(2, "use_ability: FAILED — target is out of range (2.2 m, range 0.0 m)")),
            (use(3, "do_actions", {"actions": "[]"}), res(3, "1. move_to: done\n2. use_ability: FAILED — target is out of range (1.9 m, range 0.0 m)\nSTOPPED: step 2 failed")),
            ({"type": "tool_use", "id": "t4", "name": "ToolSearch", "input": {"query": "select:mcp__bg3__click"}}, res(4, "ok")),
            (use(5, "click", {"x": 1, "y": 2}), res(5, "clicked")),
            (use(6, "click", {"x": 1, "y": 2}), res(6, "clicked")),
            (use(7, "click", {"x": 1, "y": 2}), res(7, "clicked")),
            (use(8, "screenshot", {}), res(8, "img")),
            (use(9, "move_to", {"x": 2}), res(9, "move_to: done")),
            (use(10, "travel_to", {"objective": "o1"}), {"type": "tool_result", "tool_use_id": "t10", "is_error": True,
                                                       "content": "<tool_use_error>Error: No such tool available: travel_to</tool_use_error>"}),
        ]
        stream, turns, t = [], [], 1000.0
        for i, (u, r) in enumerate(calls, 1):
            stream.append({"type": "assistant", "message": {"content": [u]}})
            stream.append({"type": "user", "message": {"content": [r]}})
            turns += [{"type": "use", "t": t, "id": f"t{i}", "name": u["name"]}, {"type": "result", "t": t + 1, "id": f"t{i}"}]
            t += 2 if i != 8 else 30  # a 30 s gap before the 2nd move_to
        stream.append({"type": "result", "result": "Reached the Helm. Saved as S009-end."})
        Path(f"{base}.jsonl").write_text("\n".join(map(json.dumps, stream)) + "\n")
        Path(f"{base}.turns.jsonl").write_text("\n".join(map(json.dumps, turns)) + "\n")
        metrics = [{"type": "tool", "tool": "move_to", "t0": 1000.0, "t1": 1001.0},
                   {"type": "tool", "tool": "move_to", "t0": 1044.0, "t1": 1045.0}]
        Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
        Path(f"{base}.progress.jsonl").write_text(json.dumps({"type": "stuck", "t": 1500, "since": 1000,
                                                              "idle_min": 8, "actions": 40}) + "\n")
        return base

    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._session(Path(d))
            md = P.triage.write(base).read_text()
        self.assertIn("**use_ability** ×2 (via do_actions)", md)              # top-level + batch step, one group
        self.assertIn("range N m", md)
        self.assertIn("**travel_to** ×1", md)                                 # is_error
        self.assertIn("43.0 s", md)                                           # the long gap
        self.assertIn("during: ", md)
        self.assertIn("select:mcp__bg3__click", md)
        self.assertIn("screenshots: 1; clicks: 3", md)
        self.assertIn("`click` repeated 3×", md)
        self.assertIn("no story progress for 8 min", md)
        self.assertIn("Reached the Helm", md)

    def test_combat_gaps_split_own_turn(self):
        # combat: end_turn at 1001, enemies act 1002-1020 (pturn false), next action 1035: 34 s raw, 15 s own;
        # then a 25 s gap with a 19 s enemy turn: not long once enemies are excluded.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "S011"
            metrics = [{"type": "tool", "tool": "end_turn", "t0": 1000.0, "t1": 1001.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1035.0, "t1": 1036.0},
                       {"type": "tool", "tool": "use_ability", "t0": 1061.0, "t1": 1062.0}]
            metrics += [{"type": "sample", "t": float(t), "moving": False, "job": 0, "dlg": False, "combat": True,
                         "pturn": not (1002 <= t <= 1020 or 1037 <= t <= 1055)} for t in range(1000, 1063)]
            Path(f"{base}.metrics.jsonl").write_text("\n".join(map(json.dumps, metrics)) + "\n")
            md = P.triage.write(base).read_text()
        self.assertIn("Idle gaps > 10 s between visible actions (1)", md)
        self.assertIn("**15.0 s** idle (combat; own turn 15.0 s of 34.0 s, enemy turns 19.0 s)", md)
        self.assertIn("not counted: 1 gap(s)", md)
        self.assertIn("25 s = 19 s enemy + 6 s ours", md)  # the parts, not "25 s with 19 s enemy"

    def test_bare_failed_header_takes_the_next_line(self):
        # S034/S037: "equip_best: FAILED\nFAILED: Claude: …" grouped as a bare "FAILED:" plus the real reason
        text = "equip_best: FAILED\nFAILED: Claude: e599 Boots (rare vs common): in a conversation\nMAIN HANDS x"
        self.assertEqual(P.triage.failures("equip_best", {}, text),
                         [("equip_best", "FAILED: Claude: e599 Boots (rare vs common): in a conversation")])

    def test_missing_files_still_write(self):
        with tempfile.TemporaryDirectory() as d:
            md = P.triage.write(Path(d) / "S001").read_text()
        self.assertIn("# Triage S001", md)
        self.assertIn("(none)", md)

    def test_wired_into_run_claude(self):
        src = Path(P.__file__).read_text()
        self.assertIn('self.write_triage(f"{sid}{tag}", base)', src)



class CheckAllBetweenSessions(unittest.TestCase):
    """check_all after the hot reload, before the next kickoff: spacing, verdict, triage append, status."""

    REPORT = {"exit": 1, "counts": {"PASS": 7, "WARN": 1, "FAIL": 1, "SKIP": 0},
              "rows": [{"name": "latency", "verdict": "PASS", "secs": 6.1, "detail": "p50 40 ms", "lines": []},
                       {"name": "vis", "verdict": "FAIL", "secs": 3.0, "detail": "markers | unknown op",
                        "lines": ["[FAIL] markers: unknown op", "[FAIL] second: x"]}]}

    def test_due(self):
        self.assertTrue(P.check_all_due(None, 1000.0))
        self.assertFalse(P.check_all_due(1000.0, 1000.0 + 29 * 60))
        self.assertTrue(P.check_all_due(1000.0, 1000.0 + 30 * 60))

    def test_result(self):
        res = P.check_all_result(1, "", self.REPORT)
        self.assertTrue(res["ok"])
        self.assertEqual(res["line"], "check_all: 7 pass, 1 warn, 1 fail")
        for rc, want in ((2, "not reachable"), (3, "refused"), (None, "timed out")):
            res = P.check_all_result(rc, "", None)
            self.assertFalse(res["ok"])
            self.assertIn(want, res["line"])
        self.assertIn("Traceback boom", P.check_all_result(1, "x\nTraceback boom\n", None)["line"])

    def test_markdown(self):
        md = P.check_all_markdown(P.check_all_result(1, "", self.REPORT), time.time(), 25.4, "after the session ")
        self.assertIn("## check_all after the session (", md)
        self.assertIn("25 s): 7 pass, 1 warn, 1 fail", md)
        self.assertIn("| vis | **FAIL** | 3.0 | markers \\| unknown op |", md)
        self.assertIn("[FAIL] second: x", md)
        self.assertEqual(md.count("\n|"), 5)  # header, rule, 2 rows, 1 extra line

    def test_cmd_uses_bridge_env(self):
        with mock.patch.object(P.shutil, "which", return_value="/u/uv"):
            cmd = P.check_all_cmd(Path("/r/x.json"))
        self.assertEqual(cmd[:2], ["/u/uv", "run"])
        self.assertEqual(cmd[-3:], [str(P.CHECK_ALL), "--json", "/r/x.json"])

    def _runner(self, d: Path, last=None):
        r = P.Runner.__new__(P.Runner)
        r.stop_requested = False
        r.state = P.RunnerState.__new__(P.RunnerState)
        r.state.dir, r.state.path, r.state.data = d, d / "runner.json", {"check_all_at": last}
        triage_md = d / "S004.triage.md"
        triage_md.write_text("# S004 triage\n")
        r.last_triage = ("S004", triage_md)
        return r, triage_md

    def _run(self, d: Path, r, report, rc=1):
        logs, calls = [], []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            if report is not None:
                Path(cmd[-1]).write_text(json.dumps(report))
            return mock.Mock(returncode=rc, stdout="", stderr="")

        with mock.patch.object(P, "RUNTIME", d), mock.patch.object(P, "log", logs.append), \
                mock.patch.object(P.subprocess, "run", fake_run):
            res = r.between_sessions_check()
        return res, logs, calls

    def test_runs_appends_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d)
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertTrue(res["ok"])
            self.assertEqual(calls[0][1]["env"]["BG3AI_CHECK_ALL_RUNNER_PID"], str(P.os.getpid()))
            self.assertTrue(logs[0].startswith("check_all: 7 pass, 1 warn, 1 fail (after S004"))
            self.assertTrue(any(line.startswith("check_all FAIL vis") for line in logs))
            md = triage_md.read_text()
            self.assertTrue(md.startswith("# S004 triage\n"))
            self.assertIn("| vis | **FAIL** |", md)
            st = json.loads((d / "runner.status.json").read_text())["check_all"]
            self.assertEqual((st["session"], st["counts"]["FAIL"], st["ok"]), ("S004", 1, True))
            self.assertIsNotNone(r.state["check_all_at"])
            # within 30 min: skipped, nothing run
            res, logs, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_no_verdict_retries_next_time(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, triage_md = self._runner(d, last=time.time() - 3600)
            res, logs, _ = self._run(d, r, None, rc=2)
            self.assertFalse(res["ok"])
            self.assertIn("not reachable", logs[0])
            self.assertLess(r.state["check_all_at"], time.time() - 3000)  # not recorded: retried next session
            self.assertIn("no verdict", triage_md.read_text())

    def test_stop_requested_skips(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r, _ = self._runner(d)
            r.stop_requested = True
            res, _, calls = self._run(d, r, self.REPORT)
            self.assertIsNone(res)
            self.assertEqual(calls, [])

    def test_wired_after_reload_before_kickoff(self):
        src = Path(P.__file__).read_text()
        body = src[src.index("    def play_session("):]
        self.assertLess(body.index("self.hot_reload_mod()"), body.index("self.between_sessions_check()"))
        self.assertLess(body.index("self.between_sessions_check()"), body.index("build_kickoff("))



class SelfRestart(unittest.TestCase):
    """Between sessions the runner execs itself when its own code changed on disk."""

    def test_code_watch(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.py"), Path(d, "b.py")
            a.write_text("x = 1\n")
            b.write_text("y = 1\n")
            w = P.CodeWatch([a])
            self.assertEqual(w.changed([a]), [])
            self.assertEqual(w.changed([a, b]), [])          # a lazily imported module: its hash when first seen
            b.write_text("y = 2\n")
            self.assertEqual(w.changed([a, b]), [b])
            a.unlink()
            self.assertEqual(sorted(w.changed([])), sorted([a, b]))

    def test_loaded_code_files(self):
        repo = Path("/r")
        mods = {"play": mock.Mock(__file__="/r/runner/play.py"),
                "metrics": mock.Mock(__file__="/r/bridge/src/bg3ai/metrics.py"),
                "dep": mock.Mock(__file__="/r/bridge/.venv/lib/site-packages/x.py"),
                "json": mock.Mock(__file__="/usr/lib/python3/json/__init__.py"),
                "ext": mock.Mock(__file__="/r/x.so"), "builtin": object()}
        self.assertEqual(P.loaded_code_files(repo, mods),
                         [Path("/r/bridge/src/bg3ai/metrics.py"), Path("/r/runner/play.py")])
        self.assertIn(Path(P.__file__).resolve(), P.loaded_code_files())

    def test_handover(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d, "handover.json")
            P.write_handover({"run_end": 5.0}, h)
            self.assertEqual(P.read_handover(h)["run_end"], 5.0)
            self.assertFalse(h.exists())                     # consumed
            self.assertIsNone(P.read_handover(h))
            P.write_handover({}, h)
            self.assertIsNone(P.read_handover(h, now=time.time() + P.HANDOVER_MAX_AGE + 1))  # stale
            h.write_text(json.dumps({"pid": -1, "t": time.time()}))
            self.assertIsNone(P.read_handover(h))            # another process's

    def _runner(self, stale):
        r = object.__new__(P.Runner)
        r.stop_requested = False
        r.bridge_hashes = None
        r.code_watch = mock.Mock(changed=mock.Mock(return_value=stale), hashes={})
        r.event = mock.Mock()
        return r

    def test_between_sessions(self):
        f = Path(P.__file__).resolve()
        logs = []
        with mock.patch.object(P, "log", logs.append), mock.patch.object(P, "tree_hashes", return_value={"a": "1"}):
            r = self._runner([])
            r.code_update_check()                             # nothing changed
            with mock.patch.object(P, "tree_hashes", return_value={"a": "2", "b": "1"}):
                r.code_update_check()
            self.assertIn("bridge code changed since last session: a, b", logs)
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=ok) as run:
                with self.assertRaises(P.RestartRunner):
                    r.code_update_check()
            self.assertEqual(run.call_args.args[0][-2:], ["run", "--help"])
            self.assertTrue(any(m.startswith("runner code changed: restarting") for m in logs))
            r = self._runner([f])
            r.stop_requested = True                          # stopping anyway: no restart
            r.code_update_check()
            bad = mock.Mock(returncode=1, stdout="", stderr="Traceback\nSyntaxError: invalid syntax")
            r = self._runner([f])
            with mock.patch.object(P.subprocess, "run", return_value=bad):
                r.code_update_check()                         # broken new code: keep running the old one
            self.assertIn(f, r.code_watch.hashes)            # and don't retry until it changes again
            r.event.assert_called_once()
            self.assertIn("SyntaxError", r.event.call_args.kwargs["error"])

    def test_handover_data_roundtrip(self):
        r = self._runner([])
        r.pending_notes, r.quick_failures, r.new_game = ["note"], 1, True
        r.last_triage = ("S007", Path("/x/S007.triage.md"))
        d = json.loads(json.dumps(r.handover_data(123.0, 100.0)))
        self.assertEqual((d["run_end"], d["run_started"], d["pending_notes"]), (123.0, 100.0, ["note"]))
        self.assertEqual(d["last_triage"], ["S007", "/x/S007.triage.md"])

    def test_fake_runner_execs_itself(self):
        """A real execv: same pid, same argv, the lock taken again, the handover read by the successor."""
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(Path(P.__file__).resolve().parent)!r})\n"
            "import play as P\n"
            "lock = P.acquire_lock()\n"
            "h = P.read_handover()\n"
            "if h is None:\n"
            "    P.write_handover({'n': 1})\n"
            "    print('first', os.getpid(), sys.argv[1:], flush=True)\n"
            "    P.exec_new_runner(lock, None)\n"
            "print('second', os.getpid(), sys.argv[1:], h['n'], flush=True)\n")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_runner.py").write_text(script)
            env = {**__import__("os").environ, "BG3AI_RUNTIME": d}
            p = __import__("subprocess").run([sys.executable, "fake_runner.py", "run", "--x"], cwd=d, env=env,
                                             capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            first, second = p.stdout.split("\n")[:2]
            self.assertEqual(first.split()[1], second.split()[1])     # same pid
            self.assertIn("['run', '--x']", second)
            self.assertTrue(second.endswith(" 1"))
            self.assertFalse(Path(d, "runner-handover.json").exists())

class MemoryKeeperHook(unittest.TestCase):
    """Runner.start_keeper/join_keeper: threaded pass, logged to events + keeper_history, joined before play."""

    def test_start_and_join(self):
        import memory_keeper as K
        with tempfile.TemporaryDirectory() as d:
            calls, events = [], []

            class FakeState:
                dir = Path(d)

            class FakeRunner:
                opt = P.Options("pt", None, 20, "opus")
                state, mem, keeper_thread = FakeState(), Path(d) / "memory", None
                def event(self, kind, **f): events.append((kind, f))
                def memory_state(self): return {"open": False}
                start_keeper = P.Runner.start_keeper
                join_keeper = P.Runner.join_keeper

            def fake_run(mem, n, base, model, effort, minutes, refused=None):
                calls.append((n, Path(base).name, model, effort, minutes))
                got_refused.append(refused)
                time.sleep(0.2)
                return K.KeeperResult("S007", status="ok", seconds=0.2, files=["quests.md"], commit="abc")

            r = FakeRunner()
            res = P.SessionResult("S007", "c", 0, "exited", 0.0, 1200.0)
            got_refused = []
            wyll = {"tool": "level_up", "result": "level_up: FAILED — passive Thirsting Blade: not offered; can pick: "
                    "Devil's Sight\n(plan: Wyll L5 from memory/party-build.md)"}
            with mock.patch.object(K, "run_keeper", fake_run), \
                    mock.patch.object(P, "read_actions_since", lambda t, *a, **k: [wyll]):
                r.start_keeper("S007", res, [{"name": "Gale", "level": 8, "problems": "check_plans …: no plan line"},
                                             {"name": "wyll", "level": 5, "problems": "dup of the refused line"}])
                self.assertTrue(r.keeper_thread.is_alive())
                r.join_keeper()
            self.assertIsNone(r.keeper_thread)
            self.assertEqual(calls, [(7, "S007-keeper", "sonnet", "low", 5.0)])
            self.assertEqual([(x["name"], x["level"]) for x in got_refused[0]], [("Wyll", 5), ("Gale", 8)])
            self.assertEqual(events[0][0], "memory_keeper")
            self.assertEqual(events[0][1]["status"], "ok")
            hist = json.loads((Path(d) / "keeper_history.jsonl").read_text())
            self.assertEqual(hist["files"], ["quests.md"])
            # quick exits and --no-memory-keeper skip the pass
            r.start_keeper("S008", P.SessionResult("S008", "c", 0, "exited", 0.0, 30.0))
            self.assertIsNone(r.keeper_thread)
            r.opt = P.Options("pt", None, 20, "opus", keeper=False)
            r.pending_notes = []
            r.start_keeper("S009", res, [{"name": "Gale", "level": 8, "problems": "no plan line"}])
            self.assertIsNone(r.keeper_thread)
            self.assertIn("Gale L8: no plan line", r.pending_notes[0])  # no keeper: the player gets the note


class PlanCheck(unittest.TestCase):
    ROWS = [{"name": "Gale", "level": 6, "plan": {"spells": ["Haste", "Slow"]}, "problems": [], "offers": "pick 2"},
            {"name": "Wyll", "level": 5, "plan": {"passives": ["Thirsting Blade"]},
             "problems": ["passive Thirsting Blade: not offered", "spells: 0 of 1 picks"],
             "offers": "pick 1 of Warlock invocations L5: Devil's Sight, Fiendish Vigour"}]

    def test_all_valid(self):
        text, bad = P.plan_check_result(self.ROWS[:1])
        self.assertEqual(bad, [])
        self.assertIn("1 checked, all valid", text)

    def test_invalid_line_goes_to_keeper_format(self):
        text, bad = P.plan_check_result(self.ROWS)
        self.assertIn("1 of 2 INVALID: Wyll L5: passive Thirsting Blade: not offered", text)
        self.assertEqual((bad[0]["name"], bad[0]["level"]), ("Wyll", 5))
        self.assertIn("Fiendish Vigour", bad[0]["problems"])
        self.assertIn('"Thirsting Blade"', bad[0]["problems"])
        self.assertIn("Wyll L5:", P.plan_note(bad))

    def test_cmd_points_at_memory(self):
        cmd = P.check_plans_cmd(Path("/tmp/x.json"), Path("/m"))
        self.assertEqual(cmd[-4:], ["--json", "/tmp/x.json", "--memory", "/m"])
        self.assertTrue(cmd[-5].endswith("check_plans.py"))


class OomGuard(unittest.TestCase):
    """20:48 S038: the Shadowfell load blew the game up to 13.3 GB; the kernel OOM-killed it (and systemd every
    agent). The runner must spot a game OOM, and refuse a save that OOM-killed the game twice in a row."""
    J = ("1791654500.100000 the test machine systemd[1740]: bg3ai-game-20261010-2000.scope: A process of this unit has been "
         "killed by the OOM killer.\n")
    HERDR = ("1791654510.000000 the test machine systemd[1740]: app-the multiplexer@b676.service: A process of this unit has been killed "
             "by the OOM killer.\n")

    def test_find_game_oom(self):
        self.assertIn("its scope bg3ai-game-20261010-2000.scope", P.find_game_oom(self.J, "20261010-2000", []))
        self.assertIsNone(P.find_game_oom(self.J, "20261010-2100", []))             # another launch's scope
        self.assertIsNone(P.find_game_oom(self.HERDR, "x", []))                       # a test OOM'd in the multiplexer's unit
        spike = {"t": 1791654480.0, "from_mb": 5000, "to_mb": 13300, "seconds": 35}
        why = P.find_game_oom(self.HERDR, "x", [spike])                                # legacy: no scope, but a spike
        self.assertIn("app-the multiplexer@b676.service", why)
        self.assertIn("5000 → 13300 MB", why)
        self.assertIsNone(P.find_game_oom("garbage killed by the OOM killer\n", "x", [spike]))

    def test_streak(self):
        a = P.oom_streak_update(None, "S038", "oom")
        self.assertEqual(a["n"], 1)
        self.assertEqual(P.oom_streak_update(a, "S038", "oom")["n"], 2)
        self.assertEqual(P.oom_streak_update(a, "S037", "oom")["n"], 1)
        self.assertIsNone(P.oom_streak_update(a, "S038", None))                     # a non-OOM death resets

    def _runner(self, d: Path, saves):
        self.saved = (P.list_saves, P.log, P.user_journal, P.read_spikes, P.write_status, P.read_actions_since)
        self.logs, self.events = [], []
        P.list_saves = lambda: saves
        P.log = self.logs.append
        P.user_journal = lambda since: self.J
        P.read_spikes = lambda since, path=None: [{"t": 1.0, "tools": ["20:47:41 jump_to {} 12118ms → NOT READY"]}]
        P.write_status = lambda **f: None
        P.read_actions_since = lambda t, path=None: []
        r = P.Runner.__new__(P.Runner)
        r.opt = P.Options("pt", None, 30, "opus", save_owner="Claude")
        r.mem = d
        r.pending_notes = []
        r.state = type("S", (), {"dir": d, "data": {"save_owner": "Claude", "runner_saves": [],
                                                       "loaded_save": "Claude-2__S038-shadowfell"},
                                  "__getitem__": lambda s, k: s.data.get(k),
                                  "__setitem__": lambda s, k, v: s.data.__setitem__(k, v)})()
        r.event = lambda kind, **f: self.events.append(kind)
        return r

    def tearDown(self):
        if hasattr(self, "saved"):
            (P.list_saves, P.log, P.user_journal, P.read_spikes, P.write_status, P.read_actions_since) = self.saved

    def test_second_oom_from_the_same_save_refuses_it(self):
        saves = [P.Save("Claude-1__S037-end", "Claude", "S037-end", 10.0),
                 P.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0)]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["game"] = {"stamp": "20261010-2000", "launched": 1.0}
            self.assertIn("OOM-killed", r.check_game_oom())
            self.assertIsNone(r.check_game_oom())                                   # once per launch
            self.assertEqual(r.choose_save().label, "S038-shadowfell")               # 1st OOM: retried
            r.state["game"] = {"stamp": "20261010-2000", "launched": 2.0}
            r.state["oom_checked"] = None                                            # (2nd launch, same journal)
            r.check_game_oom()
            self.assertEqual(r.state["oom_streak"]["n"], 2)
            self.assertEqual(r.choose_save().label, "S037-end")                      # 2nd: refused
            self.assertIn("oom_save_refused", self.events)
            self.assertTrue(any("REFUSING TO RESUME" in m for m in self.logs))
            self.assertIn("jump_to", r.pending_notes[-1])
            self.assertIn("S037-end", r.pending_notes[-1])

    def test_refused_with_no_other_save_stops(self):
        saves = [P.Save("Claude-2__S038-shadowfell", "Claude", "S038-shadowfell", 20.0)]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), saves)
            r.state["oom_blocked"] = ["Claude-2__S038-shadowfell"]
            with self.assertRaises(P.LaunchGaveUp):
                r.choose_save()

    def test_ramwatch_kill_counts_as_oom(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.user_journal = lambda since: ""
            P.read_spikes = lambda since, path=None: (
                [{"kind": "kill", "t": 5.0, "why": "game RSS 9600 MB >= 9500 MB"}] if path else [])
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIn("ramwatch killed the game", r.check_game_oom())

    def test_crash_note_names_the_last_three_calls(self):
        acts = [{"t": 10.0, "tool": "get_status", "args": {}, "result": "x"},
                {"t": 1791654449.2, "tool": "do_actions", "args": {"n": 3}, "result": "1. jump_to: FAILED\nSTOPPED"},
                {"t": 1791654481.5, "tool": "move_to", "args": {"pos": [-415.4, -0.2, -1436.6]}, "result": "done"},
                {"t": 1791654493.7, "tool": "jump_to", "args": {"x": -408.5, "y": 1, "z": -1437},
                 "result": "NOT READY: timed out"},
                {"t": 1791654600.0, "tool": "get_status", "args": {}, "result": "after"}]
        note = P.crash_tools_note("S038: game gone", acts, 1791654514.7)
        self.assertIn("jump_to {\"x\":-408.5,\"y\":1,\"z\":-1437}", note)
        self.assertIn("do_actions", note)
        self.assertNotIn("after", note)
        self.assertNotIn("get_status {}", note)
        self.assertIn("route/WAYS", note)

    def test_game_gone_at_start_warns_once(self):
        acts = [{"t": 99.0, "tool": "jump_to", "args": {"x": -408.5}, "result": "NOT READY"}]
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.read_actions_since = lambda t, path=None: acts
            r.state["history"] = [{"kind": "play", "session": "S038", "ended": 100.0}]
            r.note_game_gone(None)
            r.note_game_gone(None)
            self.assertEqual(len(r.pending_notes), 1)
            self.assertIn("S038", r.pending_notes[0])
            self.assertIn("jump_to", r.pending_notes[0])
            r.pending_notes.clear()
            r.state["crash_noted"] = None
            r.state["game_killed_at"] = 200.0          # the runner killed it after S038 (age relaunch): no warning
            r.note_game_gone(None)
            self.assertEqual(r.pending_notes, [])

    def test_no_oom_resets(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), [])
            P.user_journal = lambda since: ""
            r.state["oom_streak"] = {"save": "Claude-2__S038-shadowfell", "n": 1}
            r.state["game"] = {"stamp": "s", "launched": 1.0}
            self.assertIsNone(r.check_game_oom())
            self.assertIsNone(r.state["oom_streak"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
