"""Offline tests: the memory and session harness served by the MCP server (memtools.py), which replaced Claude Code's
file tools and hooks (2026-10-13): the path sandbox, lint in write results, the session lifecycle, the clock in
results and the pace guard.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import memtools, server  # noqa: E402
from bg3ai.memtools import Refused  # noqa: E402

import _testutil as TU  # noqa: E402  (tools/memory, on sys.path via memtools)
import memlib as M  # noqa: E402
import new_playthrough as NP  # noqa: E402

RUNNER_ENV = ("BG3AI_SESSION", "BG3AI_FIX_NOTES", "BG3AI_SESSION_DEADLINE", "BG3AI_SESSION_WRAPUP", "BG3AI_KEEPER")


def make_tree(root: Path) -> tuple[Path, Path, Path]:
    """root/play/{memory -> ../playthroughs/run1/memory, knowledge/}; returns (play, memory, knowledge)."""
    TU.isolate_git()
    real = root / "playthroughs" / "run1" / "memory"
    NP.create_memory("run1", real)
    play = root / "play"
    play.mkdir()
    (play / "memory").symlink_to("../playthroughs/run1/memory", target_is_directory=True)
    k = play / "knowledge"
    (k / "playbooks").mkdir(parents=True)
    (k / "playbooks" / "combat.md").write_text("# Combat\nFocus fire.\n")
    (k / "tools.md").write_text("# Tools\nmove_to walks.\n")
    (root / "bridge").mkdir()
    (root / "bridge" / "secret.py").write_text("SECRET = 1\n")
    return play, play / "memory", k


class SandboxPaths(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bg3memtools-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.play, self.mem, self.k = make_tree(self.root)
        self.sb = memtools.Sandbox(lambda: self.mem, lambda: self.k)

    def refused(self, fn, *a, **kw):
        with self.assertRaises(Refused) as cm:
            fn(self.sb, *a, **kw)
        return str(cm.exception)

    def test_reads_inside_both_roots(self):
        self.assertIn("# Combat", memtools.read_file(self.sb, "knowledge/playbooks/combat.md"))
        self.assertIn("PLAYTHROUGH", memtools.read_file(self.sb, "memory/PLAYTHROUGH.md"))
        self.assertIn("PLAYTHROUGH", memtools.read_file(self.sb, "PLAYTHROUGH.md"))   # a bare path is memory/
        self.assertIn("sessions/", memtools.list_files(self.sb, "memory"))
        self.assertIn("(read-only)", memtools.list_files(self.sb, "knowledge"))

    def test_escapes_refused(self):
        for p in ("../bridge/secret.py", "memory/../../bridge/secret.py", "knowledge/../../bridge/secret.py",
                  "/etc/passwd", "~/.bashrc", str(self.root / "bridge" / "secret.py"), "memory/sessions/../../x.md",
                  "C:/Windows/x"):
            self.assertIn("REFUSED", self.refused(memtools.read_file, p), p)
            self.assertIn("REFUSED", self.refused(memtools.write_file, p, "x"), p)

    def test_git_internals_refused(self):
        # FAIR-1: host-side git acts on these when it commits the memory
        for p in ("memory/.git/config", "memory/.git/hooks/pre-commit", "memory/.gitattributes", "memory/.gitignore",
                  ".git/HEAD", "memory/chapters/.gitattributes", "memory/.GIT/config", ".session.json"):
            self.assertIn("hidden and git files", self.refused(memtools.read_file, p), p)
            self.assertIn("hidden and git files", self.refused(memtools.write_file, p, "x"), p)
            self.assertIn("hidden and git files", self.refused(memtools.append_file, p, "x"), p)
        self.assertNotIn(".git", memtools.list_files(self.sb, "memory"))
        self.assertIn("No match", memtools.search(self.sb, "repositoryformatversion", "memory"))

    def test_knowledge_and_playbooks_read_only(self):
        for p in ("knowledge/playbooks/combat.md", "knowledge/tools.md", "knowledge/new.md"):
            self.assertIn("read-only", self.refused(memtools.write_file, p, "x"))
            self.assertIn("read-only", self.refused(memtools.append_file, p, "x"))
        self.assertIn("read-only", self.refused(memtools.edit_file, "knowledge/playbooks/combat.md", "Focus", "No"))
        self.assertEqual((self.k / "playbooks" / "combat.md").read_text(), "# Combat\nFocus fire.\n")

    def test_symlinks_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "x.md").write_text("outside secret\n")
        (self.mem / "evil.md").symlink_to(self.root / "bridge" / "secret.py")
        (self.mem / "chapters" / "link").symlink_to(outside, target_is_directory=True)
        (self.mem / "inner.md").symlink_to(self.mem / "npcs.md")   # even one that stays inside
        for p in ("memory/evil.md", "memory/chapters/link/x.md", "memory/inner.md"):
            self.assertIn("symlinks", self.refused(memtools.read_file, p), p)
            self.assertIn("symlinks", self.refused(memtools.write_file, p, "x\n"), p)
        self.assertIn("No match", memtools.search(self.sb, "outside secret", "memory"))
        self.assertIn("No match", memtools.search(self.sb, "SECRET", "memory"))
        self.assertEqual((outside / "x.md").read_text(), "outside secret\n")

    def test_generated_and_special_files(self):
        self.assertIn("generated", self.refused(memtools.write_file, "memory/snapshots/status.md", "x"))
        self.assertIn("place_marker", self.refused(memtools.write_file, "memory/markers.json", "[]"))
        self.assertIn("Markdown", self.refused(memtools.write_file, "memory/notes.txt", "x"))
        self.assertIn("Markdown", self.refused(memtools.write_file, "memory/x.py", "x"))

    def test_session_log_append_only(self):
        (self.mem / "sessions" / "S001.md").write_text("# Session S001\n\nGoal: (write one line)\n\n## Log\n")
        self.assertIn("append-only", self.refused(memtools.write_file, "memory/sessions/S001.md", "gone\n"))
        out = memtools.edit_file(self.sb, "memory/sessions/S001.md", "Goal: (write one line)", "Goal: reach camp")
        self.assertIn("Edited", out)
        memtools.append_file(self.sb, "memory/sessions/S001.md", "- 10:00 [note] x")
        self.assertTrue((self.mem / "sessions" / "S001.md").read_text().endswith("## Log\n- 10:00 [note] x\n"))

    def test_character_md_protected_after_commit(self):
        text = TU.character_text()
        self.assertIn("Wrote", memtools.write_file(self.sb, "memory/CHARACTER.md", text))
        # the creation session may still correct it (header lines to the game's names)
        self.assertIn("Edited", memtools.edit_file(self.sb, "memory/CHARACTER.md", "Acolyte", "Sage"))
        M.git_commit_all(self.mem, "S001")
        self.assertIn("who you play", self.refused(memtools.write_file, "memory/CHARACTER.md", "# Me\n"))
        self.assertIn("who you play", self.refused(memtools.edit_file, "memory/CHARACTER.md", "Sage", "Noble"))
        old_dev = "- S002: distrusts mind flayers more after the nautiloid [seen]"
        self.assertIn("who you play", self.refused(memtools.edit_file, "memory/CHARACTER.md", old_dev, "- gone"))
        self.assertIn("Appended", memtools.append_file(self.sb, "memory/CHARACTER.md", "- S003: spared the goblin"))
        out = memtools.edit_file(self.sb, "memory/CHARACTER.md", "- S003: spared the goblin",
                                 "- S003: spared the goblin\n- S003: freed the gnome")
        self.assertIn("Edited", out)
        self.assertTrue((self.mem / "CHARACTER.md").read_text().endswith("freed the gnome\n"))

    def test_lint_in_write_results(self):
        out = memtools.append_file(self.sb, "memory/npcs.md", "- Kagha leads the druids")
        self.assertIn("LINT (1 error(s) in npcs.md", out)
        self.assertIn("- provenance: npcs.md:", out)
        self.assertIn("'Kagha leads the druids'", out)
        out = memtools.write_file(self.sb, "memory/HANDOFF.md", "x" * 5000)
        self.assertIn("- budget: HANDOFF.md", out)   # written anyway; the agent fixes it
        self.assertNotIn("npcs.md", out)             # only the errors about the written file
        out = memtools.edit_file(self.sb, "memory/npcs.md", "- Kagha leads the druids",
                                 "- Kagha leads the druids [seen S001]")
        self.assertIn("lint: ok", out)

    def test_edit_needs_a_unique_match(self):
        memtools.write_file(self.sb, "memory/strategy.md", "# Strategy\n- a\n- a\n")
        self.assertIn("occurs 2 times", self.refused(memtools.edit_file, "memory/strategy.md", "- a", "- b"))
        self.assertIn("not found", self.refused(memtools.edit_file, "memory/strategy.md", "- z", "- b"))
        self.assertIn("2 replacement", memtools.edit_file(self.sb, "memory/strategy.md", "- a", "- b", True))

    def test_search_and_paging(self):
        memtools.write_file(self.sb, "memory/npcs.md", "# NPCs\n- Kagha, druid [seen S001]\n")
        out = memtools.search(self.sb, "kagha")
        self.assertIn("memory/npcs.md:2: - Kagha, druid", out)
        self.assertIn("knowledge/tools.md:2:", memtools.search(self.sb, "walks", "knowledge"))
        memtools.write_file(self.sb, "memory/strategy.md", "".join(f"- line {i}\n" for i in range(50)))
        page = memtools.read_file(self.sb, "memory/strategy.md", 1, 10)
        self.assertIn("lines 1-10 of 50", page)
        self.assertIn("start_line=11", page)


class ServerMemoryTools(Base):
    """The tools as the agent calls them: refusals are text, results carry lint and the clock."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)   # restored after the test
        env.start()
        self.addCleanup(env.stop)
        for k in RUNNER_ENV:
            os.environ.pop(k, None)
        os.environ["BG3_SNAPSHOTS"] = "0"
        self.root = Path(tempfile.mkdtemp(prefix="bg3memtools-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.play, self.mem, self.k = make_tree(self.root)
        saved = (server.MEMORY, server.KNOWLEDGE, server.LIFE, server.DEADLINE)
        self.addCleanup(self._restore_mem, saved)
        server.MEMORY, server.KNOWLEDGE, server.DEADLINE = self.mem, self.k, 0.0
        server.LIFE = memtools.Lifecycle(lambda: server.MEMORY, auto=True)   # as server.main sets it
        server._clock_shown.update(at=0.0, stuck=None)

    def _restore_mem(self, saved) -> None:
        server.MEMORY, server.KNOWLEDGE, server.LIFE, server.DEADLINE = saved

    def test_refusals_are_results_not_errors(self):
        os.environ["BG3AI_SESSION"] = "S001"   # runner mode: nothing opens a session
        self.assertTrue(server.memory_read(".git/config").startswith("REFUSED"))
        self.assertTrue(server.memory_write("knowledge/playbooks/combat.md", "x").startswith("REFUSED"))
        out = server.memory_append("memory/npcs.md", "- untagged bullet")
        self.assertIn("LINT", out)

    def test_clock_in_tool_results_runner_mode(self):
        os.environ["BG3AI_SESSION"] = "S001"
        server.DEADLINE = time.time() + 30 * 60
        with mock.patch.object(server, "_stuck", lambda: None):
            out = server.memory_list()
        self.assertIn("⏱", out)
        self.assertIn("keep playing: you'll be told when to wrap up", out)

    def test_manual_mode_first_call_opens_the_session(self):
        out = server.memory_list("knowledge")
        self.assertIn("BG3 PLAYTHROUGH MEMORY — session S001", out)
        self.assertIn("=== the result of your call ===\nknowledge/", out)
        self.assertTrue((self.mem / "sessions" / "S001.md").is_file())
        out2 = server.memory_list("knowledge")
        self.assertNotIn("PLAYTHROUGH MEMORY", out2)
        self.assertIn("Session already open", server.session_start())
        self.assertEqual(M.load_state(self.mem)["current"], 1)   # nothing opened a second session

    def test_manual_mode_session_start_first(self):
        out = server.session_start()
        self.assertIn("session S001", out)
        self.assertNotIn("=== the result of your call ===", out)   # the briefing once, not twice

    def test_manual_clock_reminder(self):
        server.session_start()
        st = M.load_state(self.mem)
        st["started_epoch"] = time.time() - 55 * 60   # past BG3_WRAP_MINUTES (50)
        M.save_state(self.mem, st)
        self.assertIn("SESSION CLOCK", server.memory_list())
        self.assertNotIn("SESSION CLOCK", server.memory_list())   # repeats only every few minutes

    def test_a_library_import_never_opens(self):
        # tools/dev/check_*.py import the server and call tools on the live play/memory between sessions
        server.LIFE = memtools.Lifecycle(lambda: server.MEMORY)
        out = server.memory_list()
        self.assertNotIn("PLAYTHROUGH MEMORY", out)
        self.assertFalse((self.mem / "sessions" / "S001.md").exists())

    def test_runner_mode_never_opens(self):
        os.environ["BG3AI_SESSION"] = "S001"
        out = server.memory_list()
        self.assertNotIn("PLAYTHROUGH MEMORY", out)
        self.assertFalse((self.mem / "sessions" / "S001.md").exists())

    def test_end_session_keep_playing_then_commit(self):
        os.environ["BG3AI_SESSION"] = "S001"
        TU.MemoryTestCase.open_session(self, 1)   # as the runner's session.py start would
        deadline = time.time() + 30 * 60
        os.environ["BG3AI_SESSION_DEADLINE"] = str(deadline)
        os.environ["BG3AI_SESSION_WRAPUP"] = str(deadline - 180)
        out = server.end_session()
        self.assertTrue(out.startswith("NOT ENDED. Keep playing"), out)
        self.assertTrue(M.load_state(self.mem)["open"])
        os.environ["BG3AI_SESSION_WRAPUP"] = str(time.time() - 1)   # wrap-up time reached
        out = server.end_session()
        self.assertIn("HANDOFF.md hasn't been rewritten", out)
        self.assertIn("then call end_session again", out)
        server.memory_write("memory/HANDOFF.md", TU.MemoryTestCase.good_handoff(self, 1))
        server.memory_write("memory/objectives.md", "# Objectives\n## Main\n- reach camp [seen S001]\n")
        out = server.end_session()
        self.assertIn("committed", out)
        self.assertIn("end your turn now", out)
        self.assertFalse(M.load_state(self.mem)["open"])
        self.assertEqual(M.git(self.mem, "log", "-1", "--format=%s").stdout.strip(), "S001")
        self.assertIn("No open session", server.end_session())

    def test_end_session_mid_fight(self):
        # S074: refused mid-fight with "2 min of play before WRAP UP at 23:27": in a fight it says finish it.
        os.environ["BG3AI_SESSION"] = "S001"
        TU.MemoryTestCase.open_session(self, 1)
        deadline = time.time() + 30 * 60
        os.environ["BG3AI_SESSION_DEADLINE"] = str(deadline)
        os.environ["BG3AI_SESSION_WRAPUP"] = str(deadline - 180)
        self.g.combat = {"active": True}
        out = server.end_session()
        self.assertTrue(out.startswith("NOT ENDED. Finish the fight first"), out)
        self.assertNotIn("min of play", out)
        self.g.combat = {"active": False}
        self.assertTrue(server.end_session().startswith("NOT ENDED. Keep playing"))

    # helpers borrowed by the test above (TU.MemoryTestCase's, bound to this case)
    def write(self, rel: str, text: str) -> None:
        p = self.mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class HttpTransport(unittest.TestCase):
    """--transport http: a container client reaches the server by the bind address, which the SDK's DNS-rebinding
    guard must accept (it accepts only localhost by default)."""

    def test_options(self):
        self.assertEqual(server.http_options("127.0.0.1", 8770), {"host": "127.0.0.1", "port": 8770})
        opts = server.http_options("172.30.99.1", 8770)
        sec = opts["transport_security"]
        self.assertTrue(sec.enable_dns_rebinding_protection)
        self.assertIn("172.30.99.1:8770", sec.allowed_hosts)
        self.assertIn("http://172.30.99.1:8770", sec.allowed_origins)

    def test_cli(self):
        with mock.patch.object(server.mcp, "run") as run, mock.patch.object(server.LIFE, "auto", False), \
                mock.patch.dict(os.environ, {"BG3AI_MOVIE_SKIP": "0", "BG3AI_METRICS_SAMPLE_S": "0"}):
            server.main(["--transport", "http", "--host", "172.30.99.1", "--port", "9001"])
            self.assertEqual(run.call_args.args, ("streamable-http",))
            self.assertEqual(run.call_args.kwargs["port"], 9001)
            server.main([])
            self.assertEqual(run.call_args.args, ("stdio",))
            self.assertTrue(server.LIFE.auto)   # serving a client: the first call may open the session


class PaceGuard(Base):
    """The pace guard is the server's (it was also a client PreToolUse hook): walks don't block outside combat."""

    def test_wait_true_dropped_out_of_combat(self):
        self.g.watch["combat"] = False
        wait, note = server._walk_wait(True)
        self.assertFalse(wait)
        self.assertIn("pace guard", note)

    def test_wait_true_kept_in_combat_and_when_unknown(self):
        self.g.watch["combat"] = True
        self.assertEqual(server._walk_wait(True), (True, ""))
        with mock.patch.object(server.game, "try_rpc", return_value=None):
            self.assertEqual(server._walk_wait(True), (True, ""))
        self.assertEqual(server._walk_wait(False), (False, ""))


if __name__ == "__main__":
    unittest.main()
