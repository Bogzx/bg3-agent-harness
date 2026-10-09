import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import memlib as M
import session as S
from _testutil import MemoryTestCase

SESSION_PY = str(Path(__file__).resolve().parent / "session.py")


def run_quiet(fn, *args, stdin="{}"):
    """Call fn with stdout captured and a fake stdin (hook entry points read their JSON from stdin)."""
    buf = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(buf):
            rc = fn(*args)
    finally:
        sys.stdin = old_stdin
    return rc, buf.getvalue()


class StartTest(MemoryTestCase):
    def test_first_start_opens_s001_and_prints_context(self):
        rc, out = run_quiet(S.cmd_start, self.mem)
        self.assertEqual(rc, 0)
        self.assertTrue((self.mem / "sessions" / "S001.md").exists())
        st = self.state()
        self.assertEqual((st["current"], st["open"]), (1, True))
        self.assertIn("session S001", out)
        self.assertIn("----- PLAYTHROUGH.md -----", out)
        self.assertIn("----- HANDOFF.md -----", out)
        self.assertIn("Playthrough: test-run", out)
        self.assertNotIn("<!--", out)  # template guidance stripped
        self.assertIn("session-start playbook", out)
        self.assertLessEqual(len(out), 10000)

    def test_numbering_continues_from_existing_logs(self):
        self.write("sessions/S007.md", "# Session S007\n")
        run_quiet(S.cmd_start, self.mem)
        self.assertEqual(self.state()["current"], 8)
        self.assertTrue((self.mem / "sessions" / "S008.md").exists())

    def test_unclosed_previous_session_is_flagged(self):
        run_quiet(S.cmd_start, self.mem)
        _, out = run_quiet(S.cmd_start, self.mem)
        self.assertEqual(self.state()["current"], 2)
        self.assertIn("S001 ended without wrap-up", out)
        self.assertIn("WITHOUT wrap-up", self.read("sessions/S001.md"))

    def test_session_state_is_gitignored(self):
        run_quiet(S.cmd_start, self.mem)
        st = M.git(self.mem, "status", "--porcelain").stdout
        self.assertNotIn(".session.json", st)

    def test_missing_memory_dir(self):
        rc, out = run_quiet(S.cmd_start, self.tmp / "nope")
        self.assertEqual(rc, 1)
        self.assertIn("new_playthrough.py", out)


class ContextTest(MemoryTestCase):
    def test_context_is_capped_and_truncated(self):
        self.write("PLAYTHROUGH.md", "# P\n" + ("p" * 99 + "\n") * 200)
        self.write("HANDOFF.md", "# Handoff S000 → S001\n" + ("h" * 99 + "\n") * 200)
        run_quiet(S.cmd_start, self.mem)
        out = S.build_context(self.mem)
        self.assertLessEqual(len(out), S.HOOK_CHAR_LIMIT)
        self.assertIn("truncated; Read memory/PLAYTHROUGH.md", out)
        self.assertIn("truncated; Read memory/HANDOFF.md", out)

    def test_compact_source_message(self):
        run_quiet(S.cmd_start, self.mem)
        out = S.build_context(self.mem, "compact")
        self.assertIn("CONTEXT WAS COMPACTED", out)
        self.assertIn("sessions/S001.md", out)

    def test_compaction_due_flag(self):
        self.write("sessions/S010.md", "# Session S010\n")
        run_quiet(S.cmd_start, self.mem)  # opens S011
        self.assertIn("COMPACTION DUE", S.build_context(self.mem))

    def test_lint_errors_surface_in_context(self):
        self.write("markers.json", "{nope")
        run_quiet(S.cmd_start, self.mem)
        self.assertIn("markers:", S.build_context(self.mem))


class InjectionExtrasTest(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.kdir = self.tmp / "knowledge"
        self.kdir.mkdir()
        os.environ["BG3_KNOWLEDGE_DIR"] = str(self.kdir)

    def tearDown(self):
        os.environ.pop("BG3_KNOWLEDGE_DIR", None)
        super().tearDown()

    def test_harness_news_since_previous_start(self):
        (self.kdir / "harness-news.md").write_text(
            "# news\n<!--\n- 2026-01-01 00:00 example in a comment\n-->\n"
            "- 2026-10-09 10:00 old fix\n- 2026-10-10 01:30 dismiss_popups is not the fix\n")
        self.assertEqual(S.harness_news("2026-10-10T01:18:20+03:00"),
                         ["- 2026-10-10 01:30 dismiss_popups is not the fix"])
        self.assertEqual(len(S.harness_news(None)), 2)
        M.save_state(self.mem, {"current": 4, "open": False, "started": "2026-10-10T01:00:00+03:00"})
        _, out = run_quiet(S.cmd_start, self.mem)
        self.assertIn("HARNESS CHANGES", out)
        self.assertIn("dismiss_popups is not the fix", out)
        self.assertNotIn("old fix", out)
        self.assertNotIn("HARNESS CHANGES", S.build_context(self.mem, "compact"))

    def test_no_news_file(self):
        self.assertEqual(S.harness_news("2026-10-10T01:00"), [])

    def test_status_snapshot_and_stale_notes_injected(self):
        self.write("snapshots/status.md", "<!-- generated -->\n# Snapshot: status\n\nPARTY * Claude Paladin 3 HP 9/9\n")
        for n in (1, 2, 3, 4):
            self.write(f"sessions/S{n:03d}.md", "# s\n\n## Log\n")
            M.git_commit_all(self.mem, f"S{n:03d}")
        run_quiet(S.cmd_start, self.mem)
        out = S.build_context(self.mem)
        self.assertIn("PARTY * Claude Paladin 3", out)
        self.assertIn("STALE NOTES: objectives.md", out)
        self.assertLessEqual(len(out), S.HOOK_CHAR_LIMIT)


class HookDispatchTest(MemoryTestCase):
    def _hook(self, payload):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, SESSION_PY, "hook", "--memory-dir", str(self.mem)],
                           input=json.dumps(payload), capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_startup_opens_session_compact_does_not(self):
        out = self._hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertIn("session S001", out)
        out = self._hook({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertIn("CONTEXT WAS COMPACTED", out)
        self.assertEqual(self.state()["current"], 1)

    def test_missing_memory_never_fails_the_hook(self):
        p = subprocess.run([sys.executable, SESSION_PY, "hook", "--memory-dir", str(self.tmp / "x")],
                           input="{}", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("no memory directory", p.stdout)


class EndTest(MemoryTestCase):
    def test_end_refuses_without_fresh_handoff(self):
        run_quiet(S.cmd_start, self.mem)
        rc, out = run_quiet(S.cmd_end, self.mem)
        self.assertEqual(rc, 1)
        self.assertIn("NOT committed", out)
        self.assertTrue(self.state()["open"])
        self.assertEqual(self.git_log(), ["S000 init test-run"])

    def test_end_commits(self):
        run_quiet(S.cmd_start, self.mem)
        self.write("HANDOFF.md", self.good_handoff(1))
        rc, out = run_quiet(S.cmd_end, self.mem)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git_log()[0], "S001")
        self.assertFalse(self.state()["open"])
        self.assertIn("session ended after", self.read("sessions/S001.md"))
        self.assertEqual(M.git(self.mem, "status", "--porcelain").stdout, "")

    def test_human_override(self):
        run_quiet(S.cmd_start, self.mem)
        rc, _ = run_quiet(S.cmd_end, self.mem, True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.git_log()[0], "S001 (lint override)")

    def test_end_without_open_session(self):
        rc, _ = run_quiet(S.cmd_end, self.mem)
        self.assertEqual(rc, 1)


class ClockTest(MemoryTestCase):
    def test_reminders(self):
        now = time.time()
        st = {"current": 4, "open": True, "started_epoch": now - 10 * 60}
        self.assertIsNone(S.clock_message(st, now))
        st["started_epoch"] = now - 51 * 60
        msg = S.clock_message(st, now)
        self.assertIn("SESSION CLOCK", msg)
        self.assertIn("S004", msg)
        self.assertIsNone(S.clock_message(st, now + 60))  # throttled
        self.assertIsNotNone(S.clock_message(st, now + S.WARN_REPEAT_SECONDS + 1))
        st["started_epoch"] = now - 61 * 60
        st["warned_epoch"] = None
        self.assertIn("OVERTIME", S.clock_message(st, now))

    def test_env_overrides(self):
        os.environ["BG3_WRAP_MINUTES"] = "5"
        try:
            now = time.time()
            self.assertIsNotNone(S.clock_message({"current": 1, "open": True, "started_epoch": now - 360}, now))
        finally:
            del os.environ["BG3_WRAP_MINUTES"]

    def test_autonomous_clock_hook_is_silent_and_fast_path(self):
        self.open_session(1, minutes_ago=59)
        env = dict(os.environ, BG3AI_SESSION_DEADLINE=str(time.time() + 60))
        p = subprocess.run([sys.executable, "-X", "importtime", SESSION_PY, "clock", "--memory-dir", str(self.mem)],
                           input="{}", capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertNotIn("| lint", p.stderr)  # exited before importing lint/memlib

    def test_closed_session_silent(self):
        self.assertIsNone(S.clock_message({"current": 1, "open": False, "started_epoch": 0}, time.time()))

    def test_clock_cli_outputs_hook_json(self):
        self.open_session(1, minutes_ago=55)
        p = subprocess.run([sys.executable, SESSION_PY, "clock", "--memory-dir", str(self.mem)],
                           input="{}", capture_output=True, text=True)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("SESSION CLOCK", out["hookSpecificOutput"]["additionalContext"])


class StopHookTest(MemoryTestCase):
    def test_ordinary_stop_is_allowed(self):
        self.open_session(1, minutes_ago=10)
        self.assertIsNone(S.stop_decision(self.mem))

    def test_no_session_allowed(self):
        self.assertIsNone(S.stop_decision(self.mem))

    def test_due_without_handoff_blocks(self):
        self.open_session(1, minutes_ago=52)
        out = S.stop_decision(self.mem)
        self.assertEqual(out["decision"], "block")
        self.assertIn("session-wrapup", out["reason"])
        self.assertIn("# Handoff S001 → S002", out["reason"])

    def test_handoff_and_clean_lint_commits(self):
        self.open_session(1, minutes_ago=20)  # early stop requested by operator: handoff written
        self.write("HANDOFF.md", self.good_handoff(1))
        out = S.stop_decision(self.mem)
        self.assertNotIn("decision", out)
        self.assertIn("committed", out["systemMessage"])
        self.assertEqual(self.git_log()[0], "S001")
        self.assertFalse(self.state()["open"])

    def test_lint_failure_blocks_with_report(self):
        self.open_session(1, minutes_ago=55)
        self.write("HANDOFF.md", self.good_handoff(1))
        self.append("npcs.md", "- untagged fact\n")
        out = S.stop_decision(self.mem)
        self.assertEqual(out["decision"], "block")
        self.assertIn("provenance", out["reason"])
        self.assertTrue(self.state()["open"])
        self.assertEqual(self.git_log(), ["S000 init test-run"])

    def test_gives_up_after_max_blocks(self):
        self.open_session(1, minutes_ago=55)
        for _ in range(S.MAX_STOP_BLOCKS):
            self.assertEqual(S.stop_decision(self.mem)["decision"], "block")
        out = S.stop_decision(self.mem)
        self.assertNotIn("decision", out)
        self.assertIn("left open", out["systemMessage"])
        self.assertTrue(self.state()["open"])

    def test_stop_cli(self):
        self.open_session(1, minutes_ago=55)
        p = subprocess.run([sys.executable, SESSION_PY, "stop", "--memory-dir", str(self.mem)],
                           input=json.dumps({"stop_hook_active": False}), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["decision"], "block")


class AutonomousStopTest(MemoryTestCase):
    """BG3AI_SESSION_DEADLINE set (runner): early stops are refused; 'due' follows the deadline."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, "BG3AI_SESSION_DEADLINE", None)
        self.addCleanup(os.environ.pop, "BG3AI_SESSION_WRAPUP", None)

    def set_deadline(self, minutes_from_now):
        os.environ["BG3AI_SESSION_DEADLINE"] = str(time.time() + minutes_from_now * 60)

    def test_early_stop_blocked_with_minutes_left(self):
        self.open_session(1, minutes_ago=5)
        self.set_deadline(40)
        out = S.stop_decision(self.mem)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("Keep playing: 39 minutes left") or
                        out["reason"].startswith("Keep playing: 40 minutes left"), out["reason"])
        self.assertIn("SESSION CLOCK", out["reason"])
        st = self.state()
        self.assertEqual(st["keep_playing_blocks"], 1)
        self.assertNotIn("stop_blocks", st)  # counted separately from wrap-up blocks

    def test_keep_playing_cap(self):
        self.open_session(1, minutes_ago=5)
        self.set_deadline(40)
        st = self.state()
        st["keep_playing_blocks"] = S.MAX_KEEP_PLAYING_BLOCKS
        M.save_state(self.mem, st)
        out = S.stop_decision(self.mem)
        self.assertNotIn("decision", out)
        self.assertIn("early stop allowed", out["systemMessage"])

    def test_deadline_decides_due_not_elapsed(self):
        self.open_session(1, minutes_ago=2)  # elapsed is tiny...
        self.set_deadline(2)                 # ...but the deadline is within the wrap-up window
        out = S.stop_decision(self.mem)
        self.assertEqual(out["decision"], "block")
        self.assertIn("session-wrapup", out["reason"])
        self.open_session(1, minutes_ago=55)  # long elapsed, but the deadline is far away
        self.set_deadline(30)
        self.assertTrue(S.stop_decision(self.mem)["reason"].startswith("Keep playing"))

    def test_handoff_written_early_keeps_playing(self):
        # claude-run-2 S001: stuck on a ladder, wrote HANDOFF.md at 16 min left and stopped. Not allowed.
        self.open_session(1, minutes_ago=5)
        self.set_deadline(40)
        self.write("HANDOFF.md", self.good_handoff(1))
        out = S.stop_decision(self.mem)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("Keep playing"), out["reason"])
        self.assertIn("stuck.md", out["reason"])
        self.assertEqual(self.state()["keep_playing_blocks"], 1)

    def test_handoff_written_commits_once_wrap_due(self):
        self.open_session(1, minutes_ago=25)
        self.set_deadline(2)  # inside the 3-minute wrap-up window
        self.write("HANDOFF.md", self.good_handoff(1))
        out = S.stop_decision(self.mem)
        self.assertIn("committed", out["systemMessage"])

    def test_clock_follows_deadline(self):
        now = time.time()
        st = {"current": 1, "open": True, "started_epoch": now - 60}
        self.set_deadline(2)
        self.assertIn("SESSION CLOCK", S.clock_message(st, now))
        st["warned_epoch"] = None
        self.set_deadline(-1)
        self.assertIn("OVERTIME", S.clock_message(st, now))
        st["warned_epoch"] = None
        self.set_deadline(30)
        self.assertIsNone(S.clock_message(st, now))

    def test_wrapup_follows_runner_epoch(self):
        # claude-run-2 S002: the runner's WRAP UP was 6.7 min before the deadline but the hook used a fixed
        # 10-min window, so the agent stopped at 7.4 min left while the clock line still said "1m of play".
        self.open_session(1, minutes_ago=12)
        self.write("HANDOFF.md", self.good_handoff(1))
        self.set_deadline(7.4)
        os.environ["BG3AI_SESSION_WRAPUP"] = str(time.time() + 4.4 * 60)  # wrap-up 3 min before the deadline
        self.assertTrue(S.stop_decision(self.mem)["reason"].startswith("Keep playing"))
        os.environ["BG3AI_SESSION_WRAPUP"] = str(time.time() - 1)
        self.assertIn("committed", S.stop_decision(self.mem)["systemMessage"])

    def test_wrapup_epoch_out_of_range_falls_back(self):
        self.set_deadline(10)
        dl = float(os.environ["BG3AI_SESSION_DEADLINE"])
        for bad in ("", "garbage", "0", str(dl + 60)):
            os.environ["BG3AI_SESSION_WRAPUP"] = bad
            self.assertAlmostEqual(S.wrap_at(), dl - S.WRAP_WINDOW_S)

    def test_context_mentions_deadline(self):
        self.set_deadline(45)
        run_quiet(S.cmd_start, self.mem)
        os.environ["BG3AI_SESSION_WRAPUP"] = str(float(os.environ["BG3AI_SESSION_DEADLINE"]) - 180)
        ctx = S.build_context(self.mem)
        self.assertIn("Autonomous session: deadline", ctx)
        self.assertIn("WRAP UP (3 min before)", ctx)

    def test_manual_session_unchanged(self):
        self.open_session(1, minutes_ago=5)
        self.assertIsNone(S.stop_decision(self.mem))


class SessionEndHookTest(MemoryTestCase):
    def test_commits_when_ready(self):
        self.open_session(1, minutes_ago=55)
        self.write("HANDOFF.md", self.good_handoff(1))
        run_quiet(S.cmd_session_end, self.mem)
        self.assertEqual(self.git_log()[0], "S001")

    def test_leaves_open_when_not_ready(self):
        self.open_session(1, minutes_ago=55)
        run_quiet(S.cmd_session_end, self.mem)
        self.assertTrue(self.state()["open"])
        self.assertIn("before wrap-up finished", self.read("sessions/S001.md"))


class MemlintTest(MemoryTestCase):
    def test_reports_errors_for_edited_file_only(self):
        self.open_session(1)
        self.append("npcs.md", "- untagged\n")
        self.assertIn("npcs.md", S.memlint_message(self.mem, str(self.mem / "npcs.md")))
        self.assertIsNone(S.memlint_message(self.mem, str(self.mem / "strategy.md")))
        self.assertIsNone(S.memlint_message(self.mem, "/etc/passwd"))

    def test_works_through_symlink(self):
        link = self.tmp / "play-memory"
        link.symlink_to(self.mem, target_is_directory=True)
        self.append("npcs.md", "- untagged\n")
        self.assertIsNotNone(S.memlint_message(link, str(link / "npcs.md")))


if __name__ == "__main__":
    unittest.main()
