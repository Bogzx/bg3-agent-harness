#!/usr/bin/env python3
"""Offline tests for runner/memory_keeper.py with a fake `claude` (never calls the real CLI or the game).

    python3 runner/test_memory_keeper.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools" / "memory"))
import memory_keeper as K  # noqa: E402
import memlib as M  # noqa: E402
from _testutil import MemoryTestCase  # noqa: E402

# The fake claude: runs in the memory dir (cwd), records its argv, and edits files per $FAKE_MODE.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
mode, resume = os.environ.get("FAKE_MODE", "ok"), "--resume" in args
def app(rel, text):
    with open(rel, "a") as f:
        f.write(text)
if mode == "ok":
    app("quests.md", "\n- Find the tiefling kids [seen] (S003)\n")
    app("objectives.md", "\n- Last updated: S003 (no change)\n")
elif mode == "out_of_bounds":
    app("quests.md", "\n- Rescue Halsin [told] (S003)\n")
    app("HANDOFF.md", "\nkeeper scribble\n")
    app("sessions/S003.md", "\nkeeper scribble\n")
    Path("new-notes.md").write_text("x\n")
elif mode == "handoff":
    p = Path("HANDOFF.md")
    p.write_text(p.read_text().replace("Save to load:", "Save to load: KEEPER") +
                 "\n## From the logs (memory keeper)\n- S003 ended at the Priestess door; next: open it\n")
elif mode == "bad_lint":
    app("npcs.md", "\n- Zevlor untagged bullet with no provenance\n")
elif mode == "fixable":
    if not resume:
        app("npcs.md", "\n- Zevlor untagged bullet\n")
    else:
        p = Path("npcs.md"); p.write_text(p.read_text().replace("- Zevlor untagged bullet", "- Zevlor [seen]"))
elif mode == "hang":
    app("quests.md", "\n- Partial [seen]\n")
    time.sleep(60)
elif mode == "fail":
    sys.exit(3)
print(json.dumps({"type": "result", "subtype": "success"}))
'''


class KeeperTest(MemoryTestCase):
    def setUp(self):
        super().setUp()
        for n in (1, 2, 3):
            self.write(f"sessions/S{n:03d}.md", f"# S{n:03d}\n- 10:00 [note] played\n")
            self.commit(f"S{n:03d}")
        self.fake = self.tmp / "claude"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.calls = self.tmp / "calls.jsonl"
        os.environ["FAKE_CALLS"] = str(self.calls)
        self.base = self.tmp / "S003-keeper"

    def run_keeper(self, mode: str, minutes: float = 1.0) -> K.KeeperResult:
        os.environ["FAKE_MODE"] = mode
        return K.run_keeper(self.mem, 3, self.base, minutes=minutes, claude=str(self.fake))

    def call_args(self) -> list[list[str]]:
        return [json.loads(ln)["args"] for ln in self.calls.read_text().splitlines()]

    def test_ok_commits_with_session_prefix(self):
        r = self.run_keeper("ok")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.files, ["objectives.md", "quests.md"])
        self.assertTrue(r.commit)
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: objectives.md, quests.md")
        # the commit resets staleness: as of S004 they were last touched by S003
        self.assertEqual(M.last_touched_session(self.mem, "quests.md"), 3)
        self.assertNotIn("quests.md", [rel for rel, _ in K.L.stale_files(self.mem, 4)])
        self.assertTrue(Path(f"{self.base}.jsonl").read_text().strip())

    def test_command_is_sandboxed(self):
        self.run_keeper("ok")
        args = self.call_args()[0]
        self.assertIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Edit,Write,Glob,Grep")
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--effort") + 1], "low")
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--mcp-config", args)
        self.assertEqual(json.loads(self.calls.read_text().splitlines()[0])["cwd"], str(self.mem))
        settings = json.loads(args[args.index("--settings") + 1])
        deny = settings["permissions"]["deny"]
        self.assertNotIn(f"Edit(/{self.mem.resolve()}/HANDOFF.md)", deny)  # its last section is the keeper's
        self.assertIn(f"Write(/{self.mem.resolve()}/sessions/**)", deny)
        self.assertIn("memlint", json.dumps(settings["hooks"]))
        prompt = args[args.index("-p") + 1]
        self.assertIn("sessions/S003.md", prompt)
        self.assertIn("nor HANDOFF.md above its last section", prompt)
        self.assertIn("## From the logs (memory keeper)", prompt)
        self.assertIn("HARNESS FIXES", prompt)  # the real harness-news.md: retire fixed quirks
        self.assertIn("## Retired quirks", prompt)

    def test_out_of_bounds_edits_are_reverted(self):
        handoff, log = self.read("HANDOFF.md"), self.read("sessions/S003.md")
        r = self.run_keeper("out_of_bounds")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.files, ["quests.md"])
        self.assertEqual(r.reverted, ["HANDOFF.md (player part)", "new-notes.md", "sessions/S003.md"])
        self.assertEqual(self.read("HANDOFF.md"), handoff)
        self.assertEqual(self.read("sessions/S003.md"), log)
        self.assertFalse((self.mem / "new-notes.md").exists())

    def test_keeper_section_kept_player_part_restored(self):
        handoff = self.read("HANDOFF.md")
        self.assertIn("Save to load:", handoff)
        r = self.run_keeper("handoff")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertIn("HANDOFF.md", r.files)
        now = self.read("HANDOFF.md")
        self.assertTrue(now.startswith(handoff.rstrip("\n")), now)  # player part byte for byte (Save line too)
        self.assertNotIn("KEEPER", now)
        self.assertTrue(now.endswith("## From the logs (memory keeper)\n- S003 ended at the Priestess door; "
                                     "next: open it\n"), now)

    def test_new_lint_errors_roll_back_everything(self):
        npcs = self.read("npcs.md")
        head = self.git_log()[0]
        r = self.run_keeper("bad_lint")
        self.assertEqual(r.status, "lint_failed", r.summary())
        self.assertTrue(r.lint_errors)
        self.assertEqual(self.read("npcs.md"), npcs)
        self.assertEqual(self.git_log()[0], head)
        self.assertEqual(len(self.call_args()), 2)  # one resumed fix attempt
        self.assertIn("--resume", self.call_args()[1])

    def test_lint_fix_retry_succeeds(self):
        r = self.run_keeper("fixable")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertIn("Zevlor [seen]", self.read("npcs.md"))
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: npcs.md")

    def test_timeout_keeps_lint_clean_partial_work(self):
        r = self.run_keeper("hang", minutes=3 / 60)
        self.assertTrue(r.timed_out)
        self.assertEqual(r.status, "ok", r.summary())
        self.assertLess(r.seconds, 30)
        self.assertEqual(r.files, ["quests.md"])

    def test_claude_failure_is_reported(self):
        r = self.run_keeper("fail")
        self.assertEqual(r.status, "error")
        self.assertIn("exit 3", r.summary())

    def test_open_session_changes_are_not_committed(self):
        # the player's uncommitted session log (session left open by failed lint) stays out of the keeper commit
        self.append("sessions/S003.md", "- 10:05 [note] more\n")
        r = self.run_keeper("ok")
        self.assertEqual(r.status, "ok")
        st = M.git(self.mem, "status", "--porcelain").stdout
        self.assertIn("sessions/S003.md", st)

    def test_commit_skips_paths_deleted_and_committed_elsewhere(self):
        # S026: the orchestrator deleted + committed a chapter while the keeper ran; `git add -- <gone path>` died
        # with exit 128 and the keeper's real edits were lost.
        self.write("chapters/C01_old.md", "# C01\n")
        self.commit("chapter")
        (self.mem / "chapters/C01_old.md").unlink()
        self.commit("rm chapter")
        self.append("quests.md", "\n- Keeper edit [seen] (S003)\n")
        h = K.commit(self.mem, ["chapters/C01_old.md", "quests.md"], "S003 memory-keeper: quests.md")
        self.assertTrue(h)
        self.assertIn("quests.md", M.git(self.mem, "show", "--stat", "HEAD").stdout)
        self.assertIsNone(K.commit(self.mem, ["chapters/C01_old.md"], "nothing"))

    def test_compaction_prompt_when_due(self):
        for n in range(4, 13):
            self.write(f"sessions/S{n:03d}.md", f"# S{n:03d}\n")
        self.commit("S012")
        p = K.build_prompt(self.mem, "S012", 12, "", 5)
        self.assertIn("COMPACTION DUE", p)
        self.assertIn("S001-S010", p)
        self.assertNotIn("COMPACTION DUE", K.build_prompt(self.mem, "S003", 3, "", 5))

    def test_logs_to_read_since_stalest_file(self):
        self.assertEqual(K.logs_to_read(self.mem, 3), [1, 2, 3])
        for n in range(4, 15):
            self.write(f"sessions/S{n:03d}.md", "x\n")
        self.assertEqual(len(K.logs_to_read(self.mem, 14)), K.MAX_LOGS)
        self.assertEqual(K.logs_to_read(self.mem, 14)[-1], 14)


if __name__ == "__main__":
    unittest.main()
