#!/usr/bin/env python3
"""Offline tests for bg3play.memory.keeper (was runner/memory_keeper.py) with a fake `claude` (never calls the real CLI
or the game)."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

from bg3harness import profiles
from bg3play.memory import keeper, memlib

from tests.memory._testutil import MemoryTestCase

# The fake claude: runs in the memory dir (cwd), records its argv, and edits files per $FAKE_MODE.
FAKE = r"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
mode, resume = os.environ.get("FAKE_MODE", "ok"), "--resume" in args or args[:2] == ["exec", "resume"]
if args[:1] == ["exec"]:   # codex: the thread id comes first in its stream
    print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}), flush=True)
def pi_usage(n, usd):   # pi: an assistant message whose usage carries its cost (the running cost adds up)
    msg = {"role": "assistant", "responseId": f"r{n}", "usage": {"input": 10, "output": 5, "cost": {"total": usd}},
           "content": [], "stopReason": "toolUse"}
    print(json.dumps({"type": "message_end", "message": msg}), flush=True)
if "--mode" in args:
    pi_usage(0, 0.01)
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
elif mode == "character":
    p = Path("CHARACTER.md")
    t = p.read_text().replace("Mercy first.", "Profit first.")
    p.write_text(t + "- S003: swore to free the tieflings [seen]\n")
elif mode == "character_append":
    app("CHARACTER.md", "- S003: swore to free the tieflings [seen]\n")
elif mode == "character_new":
    Path("CHARACTER.md").write_text("# Character: Keeper\n")
elif mode == "git_meta":
    app("quests.md", "\n- Rescue Halsin [told] (S003)\n")
    Path(".gitattributes").write_text("* filter=evil\n")
    Path("chapters/.gitattributes").write_text("* filter=evil\n")
elif mode == "hallucinate":   # MEM-1: a name no note has, next to a real one
    app("quests.md", "\n- Bring the idol to Qxzarnath the Undying [seen] (S003)\n"
        "- Find the tiefling kids [seen] (S003)\n")
elif mode == "spend":   # keeps spending until it is killed at the cap
    app("quests.md", "\n- Find the tiefling kids [seen] (S003)\n")
    for n in range(1, 20):
        pi_usage(n, 0.02)
        time.sleep(0.05)
    time.sleep(60)
elif mode == "fail":
    sys.exit(3)
print(json.dumps({"type": "result", "subtype": "success"}))
"""


def _pi_profile():
    return profiles.parse(
        {
            "client": "pi",
            "model": "anthropic/stub-model",
            "effort": "low",
            "endpoint": {"provider": "openrouter", "base_url": "http://127.0.0.1:9/v1", "api_key_env": "BG3_STUB_KEY"},
            "model_info": {
                "input": ["text"],
                "reasoning": True,
                "context_window": 200000,
                "max_tokens": 16000,
                "cost": {"input": 1.0, "output": 4.0, "cache_read": 0.1},
            },
        }
    )


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
        self.budget = self.tmp / "budget.json"

    def run_keeper(self, mode: str, minutes: float = 1.0) -> keeper.KeeperResult:
        os.environ["FAKE_MODE"] = mode
        return keeper.run_keeper(self.mem, 3, self.base, minutes=minutes, claude=str(self.fake))

    def call_args(self) -> list[list[str]]:
        return [json.loads(ln)["args"] for ln in self.calls.read_text().splitlines()]

    def test_ok_commits_with_session_prefix(self):
        r = self.run_keeper("ok")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.files, ["objectives.md", "quests.md"])
        self.assertTrue(r.commit)
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: objectives.md, quests.md")
        # the commit resets staleness: as of S004 they were last touched by S003
        self.assertEqual(memlib.last_touched_session(self.mem, "quests.md"), 3)
        self.assertNotIn("quests.md", [rel for rel, _ in keeper.lint.stale_files(self.mem, 4)])
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

    def test_claude_via_mcp_keeper_server(self):
        os.environ["FAKE_MODE"] = "ok"
        r = keeper.run_keeper(self.mem, 3, self.base, minutes=1.0, claude=str(self.fake), mcp=True)
        self.assertEqual(r.status, "ok", r.summary())
        args = self.call_args()[0]
        self.assertNotIn("--restricted", args)
        self.assertEqual(args[args.index("--tools") + 1], "ToolSearch")  # + the bg3 server's tools only
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"permissions": {"allow": ["mcp__bg3__*"]}})
        self.assertEqual(json.loads(self.calls.read_text().splitlines()[0])["cwd"], str(self.mem))
        self.assertIn("--session-id", args)
        cfg = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
        env = cfg["mcpServers"]["bg3"]["env"]
        self.assertEqual(env["BG3AI_KEEPER_MODE"], "1")
        self.assertEqual(env["BG3AI_MEMORY_DIR"], str(self.mem.resolve()))
        self.assertEqual(env["BG3AI_KEEPER_HANDOFF_SECTION"], keeper.HANDOFF_SECTION)
        writable = env["BG3AI_KEEPER_WRITABLE"].split(",")
        self.assertIn("objectives.md", writable)
        self.assertIn("chapters/", writable)
        self.assertNotIn("sessions/", writable)
        prompt = args[args.index("-p") + 1]
        self.assertEqual(Path(env["BG3AI_KEEPER_TASK"]).read_text().strip(), prompt.strip())
        self.assertIn("memory_edit", prompt)
        self.assertIn("lint check", prompt)
        self.assertNotIn("Read/Edit/Write/Glob/Grep", prompt)

    def test_keeper_server_runs_the_bridge_from_anywhere(self):
        s = keeper.keeper_server(self.mem)
        proj = s["args"][s["args"].index("--project") + 1]
        self.assertEqual(Path(proj), keeper.REPO / "bridge")
        self.assertEqual(s["args"][-1], "bg3ai-mcp")
        self.assertNotIn("BG3AI_KEEPER_TASK", s["env"])

    def run_codex(self, mode: str) -> keeper.KeeperResult:
        os.environ["FAKE_MODE"] = mode
        return keeper.run_keeper(
            self.mem, 3, self.base, model="gpt-5", minutes=1.0, client="codex", binary=str(self.fake)
        )

    @staticmethod
    def codex_config(args: list[str]) -> dict:
        return dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-c")

    def test_codex_via_mcp_keeper_server(self):
        r = self.run_codex("ok")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: objectives.md, quests.md")
        args = self.call_args()[0]
        self.assertEqual(args[:2], ["exec", "--json"])
        self.assertEqual(args[args.index("-m") + 1], "gpt-5")
        self.assertIn("shell_tool", args)  # --disable shell_tool
        cfg = self.codex_config(args)
        self.assertEqual(cfg["model_reasoning_effort"], '"low"')
        self.assertEqual(json.loads(cfg["mcp_servers.bg3.cwd"]), str(self.mem))
        self.assertIn('"BG3AI_KEEPER_MODE" = "1"', cfg["mcp_servers.bg3.env"])
        self.assertIn(keeper.HANDOFF_SECTION, cfg["mcp_servers.bg3.env"])
        self.assertIn("memory_write", args[-1])  # the prompt is codex's last argument

    def test_codex_fix_attempt_resumes_its_thread(self):
        r = self.run_codex("fixable")
        self.assertEqual(r.status, "ok", r.summary())
        calls = self.call_args()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][:3], ["exec", "resume", "--json"])
        self.assertEqual(calls[1][-2], "thread-1")
        self.assertIn("NEW errors", calls[1][-1])

    def test_codex_lint_failure_rolls_back(self):
        r = self.run_codex("bad_lint")
        self.assertEqual(r.status, "lint_failed")
        self.assertNotIn("Zevlor untagged", (self.mem / "npcs.md").read_text())

    def test_codex_out_of_bounds_reverted(self):
        r = self.run_codex("out_of_bounds")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertNotIn("keeper scribble", (self.mem / "sessions" / "S003.md").read_text())
        self.assertFalse((self.mem / "new-notes.md").exists())

    def test_codex_failure_is_reported(self):
        r = self.run_codex("fail")
        self.assertEqual(r.status, "error")
        self.assertIn("codex exit 3", r.detail)

    def run_pi(self, mode: str, max_cost: float | None = 0.05, budget: dict | None = None) -> keeper.KeeperResult:
        os.environ["FAKE_MODE"] = mode
        self.budget.write_text(json.dumps(budget or {"cap": 1.0, "spent": 0.0}))
        return keeper.run_keeper(
            self.mem,
            3,
            self.base,
            minutes=1.0,
            client="pi",
            binary=str(self.fake),
            profile=_pi_profile(),
            max_cost=max_cost,
            budget=self.budget,
        )

    def spent(self) -> float:
        return json.loads(self.budget.read_text())["spent"]

    def test_pi_via_mcp_keeper_server(self):
        r = self.run_pi("ok")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: objectives.md, quests.md")
        args = self.call_args()[0]
        self.assertEqual(args[args.index("--model") + 1], "openrouter/anthropic/stub-model")  # the profile's
        self.assertEqual(args[args.index("--tools") + 1], "mcp__bg3__*")
        self.assertIn("memory_write", args[-1])  # the prompt is pi's last argument
        bg3 = json.loads((self.tmp / "S003-keeper.pi" / "bg3.json").read_text())
        self.assertEqual(bg3["server"]["env"]["BG3AI_KEEPER_MODE"], "1")
        self.assertEqual(bg3["server"]["cwd"], str(self.mem))
        self.assertEqual(Path(bg3["systemPromptFile"]), Path(f"{self.base}.task.md"))  # the task as system text
        self.assertEqual(r.cost_usd, 0.01)
        self.assertAlmostEqual(self.spent(), 0.01)  # added to runtime/budget.json
        self.assertIn("keeper cost $0.0100; budget $0.01 of $1", r.detail)

    def test_pi_killed_at_its_cost_cap(self):
        t0 = time.monotonic()
        r = self.run_pi("spend", max_cost=0.05)
        self.assertLess(time.monotonic() - t0, 30)  # killed at the cap, not at the 1-minute timeout
        self.assertFalse(r.timed_out)
        self.assertIn("cost cap $0.05 hit", r.detail)
        self.assertGreaterEqual(r.cost_usd, 0.05)
        self.assertLess(r.cost_usd, 0.09)  # stopped within a message of the cap
        self.assertAlmostEqual(self.spent(), r.cost_usd)
        self.assertEqual(r.status, "ok", r.summary())  # its edits so far are checked like a timed-out pass

    def test_pi_cap_cut_to_the_budget_left(self):
        r = self.run_pi("spend", max_cost=1.0, budget={"cap": 1.0, "spent": 0.97})
        self.assertIn("cost cap $0.03 hit", r.detail)

    def test_pi_refused_without_cap_or_budget(self):
        for max_cost, budget, why in (
            (None, None, "a keeper cost cap ([keeper] max_cost_usd)"),
            (0.0, None, "a keeper cost cap"),
            (0.05, {"cap": 1.0, "spent": 1.0}, "the global budget is used up"),
        ):
            r = self.run_pi("ok", max_cost=max_cost, budget=budget)
            self.assertEqual(r.status, "refused")
            self.assertIn(why, r.detail)
        os.environ["FAKE_MODE"] = "ok"
        r = keeper.run_keeper(
            self.mem, 3, self.base, minutes=1.0, client="pi", binary=str(self.fake), profile=_pi_profile(), max_cost=1
        )
        self.assertEqual(r.status, "refused")
        self.assertIn("runtime/budget.json", r.detail)
        self.assertFalse(self.calls.exists())  # no pi process ran

    def test_pi_without_profile_is_an_error(self):
        os.environ["FAKE_MODE"] = "ok"
        self.budget.write_text('{"cap": 1, "spent": 0}')
        r = keeper.run_keeper(
            self.mem, 3, self.base, minutes=1.0, client="pi", binary=str(self.fake), max_cost=1, budget=self.budget
        )
        self.assertEqual(r.status, "error")
        self.assertIn("needs its client profile", r.detail)

    def test_deaths_always_become_lessons(self):
        # S043 Jaheira / S045 Aylin: an ally's death in a fight must reach lessons.md (and the party notes)
        p = keeper.build_prompt(self.mem, "S012", 12, "", 5)
        self.assertIn("A death always gets a lesson", p)
        self.assertIn("`ally DIED:`", p)
        self.assertIn("Scroll of Revivify", p)

    def test_lessons_md_is_the_keepers(self):
        # fight debriefs → lessons.md (docs/ARCHITECTURE.md "Learning from fights"): writable, a new file survives
        self.assertTrue(keeper.writable("lessons.md"))
        p = keeper.build_prompt(self.mem, "S012", 12, "", 5)
        self.assertIn("lessons.md", p)
        self.assertIn("[fight]", p)
        self.assertIn("- WHEN <trigger>", p)

    def test_out_of_bounds_edits_are_reverted(self):
        handoff, log = self.read("HANDOFF.md"), self.read("sessions/S003.md")
        r = self.run_keeper("out_of_bounds")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.files, ["quests.md"])
        self.assertEqual(r.reverted, ["HANDOFF.md (player part)", "new-notes.md", "sessions/S003.md"])
        self.assertEqual(self.read("HANDOFF.md"), handoff)
        self.assertEqual(self.read("sessions/S003.md"), log)
        self.assertFalse((self.mem / "new-notes.md").exists())

    CHARACTER = (
        "# Character: Mira\n\nName: Mira\nRace: Dwarf / Gold Dwarf\nClass: Cleric / Life Domain\n"
        "Background: Acolyte\nDeity: Moradin\nRoleplay: strict\n\n## Look\nShort, scarred.\n\n## Voice\nBlunt.\n\n"
        "## Backstory\nTemple orphan.\n\n## Values\nMercy first.\n\n## Flaws\nTemper.\n\n## Goals\nGo home.\n\n"
        "## Won't do\nAbandon the weak.\n\n## How they decide\nThe weak first, then the odds.\n\n"
        "## Character development\n- S002: distrusts mind flayers more after the nautiloid [seen]\n"
    )

    def test_character_core_reverted_append_kept(self):
        self.write("CHARACTER.md", self.CHARACTER)
        self.commit("S002")
        r = self.run_keeper("character")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertIn("CHARACTER.md (core)", r.reverted)
        now = self.read("CHARACTER.md")
        self.assertTrue(now.startswith(self.CHARACTER), now)  # core and older development lines as they were
        self.assertNotIn("Profit first", now)
        self.assertTrue(now.endswith("- S003: swore to free the tieflings [seen]\n"), now)
        self.assertIn("CHARACTER.md", r.files)
        self.assertEqual(keeper.lint.lint(self.mem, "any", 4).errors, [])

    def test_character_pure_append_kept(self):
        self.write("CHARACTER.md", self.CHARACTER)
        self.commit("S002")
        r = self.run_keeper("character_append")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.reverted, [])
        self.assertEqual(self.read("CHARACTER.md"), self.CHARACTER + "- S003: swore to free the tieflings [seen]\n")
        self.assertEqual(self.git_log()[0], "S003 memory-keeper: CHARACTER.md")

    def test_keeper_never_creates_character(self):
        r = self.run_keeper("character_new")
        self.assertIn("CHARACTER.md (core)", r.reverted)
        self.assertFalse((self.mem / "CHARACTER.md").exists())

    def test_git_control_files_reverted_and_denied(self):
        r = self.run_keeper("git_meta")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertEqual(r.files, ["quests.md"])
        self.assertIn(".gitattributes", r.reverted)
        self.assertIn("chapters/.gitattributes", r.reverted)
        self.assertFalse((self.mem / ".gitattributes").exists())
        self.assertFalse((self.mem / "chapters" / ".gitattributes").exists())
        self.assertTrue((self.mem / ".gitignore").exists())  # the template's own, untouched
        deny = keeper.keeper_settings(self.mem)["permissions"]["deny"]
        m = self.mem.resolve()
        for tool in ("Read", "Edit", "Write"):
            self.assertIn(f"{tool}(/{m}/.git/**)", deny)
            self.assertIn(f"{tool}(/{m}/**/.git*)", deny)

    def test_keeper_section_kept_player_part_restored(self):
        handoff = self.read("HANDOFF.md")
        self.assertIn("Save to load:", handoff)
        r = self.run_keeper("handoff")
        self.assertEqual(r.status, "ok", r.summary())
        self.assertIn("HANDOFF.md", r.files)
        now = self.read("HANDOFF.md")
        self.assertTrue(now.startswith(handoff.rstrip("\n")), now)  # player part byte for byte (Save line too)
        self.assertNotIn("KEEPER", now)
        self.assertTrue(
            now.endswith("## From the logs (memory keeper)\n- S003 ended at the Priestess door; next: open it\n"), now
        )

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
        st = memlib.git(self.mem, "status", "--porcelain").stdout
        self.assertIn("sessions/S003.md", st)

    def test_commit_skips_paths_deleted_and_committed_elsewhere(self):
        # S026: the orchestrator deleted + committed a chapter while the keeper ran; `git add -- <gone path>` died
        # with exit 128 and the keeper's real edits were lost.
        self.write("chapters/C01_old.md", "# C01\n")
        self.commit("chapter")
        (self.mem / "chapters/C01_old.md").unlink()
        self.commit("rm chapter")
        self.append("quests.md", "\n- Keeper edit [seen] (S003)\n")
        h = keeper.commit(self.mem, ["chapters/C01_old.md", "quests.md"], "S003 memory-keeper: quests.md")
        self.assertTrue(h)
        self.assertIn("quests.md", memlib.git(self.mem, "show", "--stat", "HEAD").stdout)
        self.assertIsNone(keeper.commit(self.mem, ["chapters/C01_old.md"], "nothing"))

    def test_compaction_prompt_when_due(self):
        for n in range(4, 13):
            self.write(f"sessions/S{n:03d}.md", f"# S{n:03d}\n")
        self.commit("S012")
        p = keeper.build_prompt(self.mem, "S012", 12, "", 5)
        self.assertIn("COMPACTION DUE", p)
        self.assertIn("S001-S010", p)
        self.assertNotIn("COMPACTION DUE", keeper.build_prompt(self.mem, "S003", 3, "", 5))

    # claude-run-2 S060.md 09:20 (a v1 record: "Radiant did little to Gortash" was a rider, vis V6 2026-10-17)
    FIGHT = (
        "- 09:20 [fight] reloaded @Rivington & Wyrm's Crossing vs Gortash (Human) | rounds 4 | downed: Shadowheart "
        "by Black Gauntlet Numia | dealt 91 / taken 666 | resisted: Gortash:Radiant | unused: none | Radiant did "
        "little to Gortash (2 hits, avg 2.0)\n"
    )

    def fights(self, sid: str, *versions: int | None, name: str | None = None) -> None:
        recs = [{"session": sid, "grade": "reloaded", **({"v": v} if v else {})} for v in versions]
        (self.tmp / f"{name or sid}.fights.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_old_debrief_logs_by_record_version(self):
        self.append("sessions/S002.md", self.FIGHT)
        self.append("sessions/S003.md", self.FIGHT)
        self.fights("S002", None)  # v1 (no "v"): before the fix
        self.fights("S003", 3, 3)
        self.assertEqual(keeper.old_debrief_logs(self.mem, self.tmp, [1, 2, 3]), [2])  # S001 has no [fight] line
        self.fights("S003", 3, name="S003-r1")  # a restarted session's second records file
        self.fights("S003", 3)
        self.assertEqual(keeper.old_debrief_logs(self.mem, self.tmp, [3]), [])
        # vis V18b: v2 (S066-S077) judged "did little" from 2 hits / killing blows on 1-HP bats / other attacks
        self.fights("S003", 2, name="S003-r1")
        self.assertEqual(keeper.old_debrief_logs(self.mem, self.tmp, [3]), [3])
        self.fights("S003", 1, name="S003-r1")
        self.assertEqual(keeper.old_debrief_logs(self.mem, self.tmp, [3]), [3])
        # fight lines without any record (a hand run, no sessions dir) can't be shown to be new
        self.assertEqual(keeper.old_debrief_logs(self.mem, None, [2, 3]), [2, 3])

    def test_prompt_flags_old_debrief_resistances(self):
        # S8 (vis found ~80 false "resisted / did little" facts in S035-S065): the keeper must not distill them
        self.append("sessions/S003.md", self.FIGHT)
        self.commit("S003 fight")
        self.fights("S003", None)
        p = keeper.build_prompt(self.mem, "S003", 3, "", 5, sessions_dir=self.tmp)
        self.assertIn("OLD FIGHT DEBRIEFS", p)
        self.assertIn("in sessions/S003.md the `[fight]` lines' `resisted: …` field", p)
        self.fights("S003", 2)  # a v2 record (S066-S077): its 'did little' facts are wrong too
        p = keeper.build_prompt(self.mem, "S003", 3, "", 5, sessions_dir=self.tmp)
        self.assertIn("killing blows capped by a 1-HP enemy's HP", p)
        self.fights("S003", 3)
        self.assertNotIn("OLD FIGHT DEBRIEFS", keeper.build_prompt(self.mem, "S003", 3, "", 5, sessions_dir=self.tmp))

    def test_done_items_listed_for_the_next_steps_check(self):
        # S078 01:18: HANDOFF's side list (S077) and objectives.md's Next still named Gale's Sorcerous Sundries vault,
        # done in S069 ("DONE S069: Annals of Karsus taken" in the same objectives block); the map kept its Tolna's
        # Office marker and the party travelled there (~20 s) before the player saw it was stale
        self.write(
            "objectives.md",
            "# Objectives\n\n## Main\n4. NOW: prepare for the elder brain.\n"
            "   - DONE S069: Annals of Karsus taken; Gale met Mystra [seen S069].\n"
            "   - Next: side list (Blue Jay, Sorcerous Sundries vault), then the Morphic Pool.\n",
        )
        self.write(
            "quests.md",
            "# Quests\n\n## Investigate Cazador's Palace / The Pale Elf (DONE S077)\n- killed\n"
            "## The Wizard of Waterdeep (active)\n- vault\n## Done\n- Roveer's Storehouse: rewarded (S073) "
            "[seen]\n- Last updated: S076\n",
        )
        self.commit("S003 memory")
        self.assertEqual(
            keeper.done_records(self.mem),
            [
                "objectives.md: DONE S069: Annals of Karsus taken; Gale met Mystra [seen S069].",
                "quests.md: Investigate Cazador's Palace / The Pale Elf (DONE S077)",
                "quests.md Done: Roveer's Storehouse: rewarded (S073) [seen]",
            ],
        )
        p = keeper.build_prompt(self.mem, "S003", 3, "", 5)
        block = p.split("DONE IN MEMORY (finished per the memory files", 1)[1]
        self.assertIn("- objectives.md: DONE S069: Annals of Karsus taken", block)
        self.assertNotIn("Wizard of Waterdeep", block)  # active quests aren't done
        self.assertNotIn("Last updated", block)
        self.assertIn("Check EVERY next step and side-list item (also ones you copy from HANDOFF.md)", p)
        self.assertIn("even when the game's journal or map still shows its marker", p)
        self.assertIn("`- <item> is done (Sxxx: <what the record says>): skip it.`", p)

    def test_bosses_done_from_milestones_and_fight_records(self):
        # S086 review: the S085 player wrote "Cazador still pending (7 tries lost)" into HANDOFF, copied from
        # PLAYTHROUGH.md's "next Cazador" (stale since S076); Cazador died in S077 (milestone), though his own fight
        # record left him at 34% (the sarcophagus) and the journal kept a Call of the Blood objective
        def jl(name: str, *recs: dict) -> None:
            (self.tmp / name).write_text("".join(json.dumps(r) + "\n" for r in recs))

        def ev(kind: str, what: str) -> dict:
            return {"type": "progress", "t": 1.0, "kind": kind, "what": what}

        jl("S066.progress.jsonl", ev("story_kill", "Orin"))  # her first death: a try that was reloaded
        jl("S066.fights.jsonl", {"session": "S066", "reloaded": True, "kills": [{"name": "Orin", "round": 3}]})
        jl(
            "S067.fights.jsonl",
            {
                "session": "S067",
                "reloaded": False,
                "kills": [{"name": "Bat", "round": 1}, {"name": "Orin", "round": 5}],
            },
        )
        jl(
            "S077.fights.jsonl",
            {
                "session": "S077",
                "reloaded": False,
                "kills": [{"name": "Bat", "round": 1}],
                "left": [{"name": "Cazador Szarr", "pct": 34}],
            },
        )
        jl("S077.progress.jsonl", ev("quest_step", "Call of the Blood: Cazador's been defeated"))
        jl("S077-r1.progress.jsonl", ev("quest_done", "Investigate Cazador's Palace"))
        jl("S078-keeper.progress.jsonl", ev("story_kill", "Bat"))  # not a session file
        (self.tmp / "progress_state.json").write_text(
            json.dumps(
                {"quests": {"Investigate Cazador's Palace": {"done": True}, "Call of the Blood": {"done": False}}}
            )
        )
        self.assertEqual(
            keeper.game_done_records(self.tmp),
            ["milestone: Cazador defeated (S077)", "fight record: Orin killed in a fight not reloaded (S067)"],
        )
        self.assertEqual(keeper.game_done_records(None), [])
        self.write(
            "PLAYTHROUGH.md",
            "# Playthrough\nMain thread: Cazador fight (S074-S076, 7 tries lost); next "
            "Cazador, then the Morphic Pool.\n",
        )
        self.commit("S003 memory")
        p = keeper.build_prompt(self.mem, "S003", 3, "", 5, sessions_dir=self.tmp)
        block = p.split("DONE IN MEMORY (finished per the memory files and the runner's milestones", 1)[1]
        self.assertIn("\n- milestone: Cazador defeated (S077)\n", block)
        self.assertIn("\n- fight record: Orin killed in a fight not reloaded (S067)\n", block)
        self.assertIn("The Main thread's `next …` never names a boss or goal that DONE IN MEMORY", p)
        self.assertIn("'still pending / not yet / try again' claim (a boss, a quest) in HANDOFF's text above", p)
        self.assertIn("not the game's journal: it can keep an objective open after the boss is dead", p)

    def test_no_done_block_without_records(self):
        self.write("objectives.md", "# Objectives\n\n## Main\n1. NOW: find the grove.\n")
        self.commit("S003 memory")
        self.assertEqual(keeper.done_records(self.mem), [])
        self.assertNotIn("DONE IN MEMORY (finished", keeper.build_prompt(self.mem, "S003", 3, "", 5))

    def test_done_records_cap_keeps_objectives_first(self):
        # claude-run-2 at S078: quests.md's Done list alone is > 40 lines; the objectives' DONE line must stay
        self.write("objectives.md", "# Objectives\n\n## Main\n   - DONE S069: Annals of Karsus taken [seen S069].\n")
        self.write(
            "quests.md",
            "# Quests\n\n## Done\n" + "".join(f"- old quest {n} (S0{n:02d}) [seen]\n" for n in range(60, 0, -1)),
        )
        self.commit("S003 memory")
        got = keeper.done_records(self.mem)
        self.assertEqual(len(got), keeper.DONE_MAX_LINES)
        self.assertEqual(got[0], "objectives.md: DONE S069: Annals of Karsus taken [seen S069].")
        self.assertEqual(got[1], "quests.md Done: old quest 60 (S060) [seen]")  # newest first, as the file has it

    def test_old_debriefs_in_the_compaction_range(self):
        # compaction reads S001-S010 into a chapter: an old fight line there is flagged too, though it isn't a log
        # the keeper reads for the curated files
        self.append("sessions/S002.md", self.FIGHT)
        for n in range(4, 13):
            self.write(f"sessions/S{n:03d}.md", f"# S{n:03d}\n")
        self.commit("S012")
        self.assertNotIn(2, keeper.logs_to_read(self.mem, 12))
        p = keeper.build_prompt(self.mem, "S012", 12, "", 5, sessions_dir=self.tmp)
        self.assertIn("COMPACTION DUE", p)
        self.assertIn("OLD FIGHT DEBRIEFS (a harness bug, fixed 2026-10-17 and 2026-10-18): in sessions/S002.md the", p)

    def test_run_keeper_reads_the_records_beside_its_log(self):
        self.append("sessions/S003.md", self.FIGHT)
        self.commit("S003 fight")
        self.fights("S003", None)  # self.base's directory: runtime/sessions/<playthrough>
        self.run_keeper("ok")
        self.assertIn("OLD FIGHT DEBRIEFS", " ".join(self.call_args()[0]))

    # Live 12:2x (runtime/actions.jsonl): Wyll's stored L5 line refused, then the camp path's two-level result.
    WYLL_FAIL = (
        "level_up: FAILED \u2014 finish: level up is not complete, not finishing: passive Thirsting Blade: not "
        "offered; passive choice 'Warlock invocations L5' needs 1 more in plan.passives; can pick: Mire the Mind, "
        "Devil's Sight, Fiendish Vigour; spell choice 'spells' needs 1 more spell in plan.spells; can pick: "
        "Counterspell, Fireball, Hypnotic Pattern \u2014 options={Warlock invocations L5=[Mire the Mind,Devil's "
        "Sight,Fiendish Vigour]}\n(plan: Wyll L5 from memory/party-build.md)"
    )
    CAMP_OK = (
        "level_up: done \u2014 Wyll (camp member): swapped in\nL4: level_up: done \u2014 complete\n(plan: Wyll L4 "
        "from memory/party-build.md) (clicked spells: Booming Blade)\nL5: level_up: done \u2014 finished complete\n"
        "(plan: Wyll L5 from memory/party-build.md)"
    )

    def test_refused_plans_from_level_up_results(self):
        acts = [
            {"tool": "get_status", "result": "x"},
            {"tool": "level_up", "result": self.WYLL_FAIL},
            {
                "tool": "level_up",
                "result": "level_up: done \u2014 complete\n(plan: Gale L5 from memory/party-build.md)",
            },
        ]
        r = keeper.refused_plans(acts)
        self.assertEqual([(x["name"], x["level"]) for x in r], [("Wyll", 5)])
        self.assertIn("Thirsting Blade: not offered", r[0]["problems"])
        self.assertIn("can pick: Mire the Mind", r[0]["problems"])
        p = keeper.build_prompt(self.mem, "S012", 12, "", 5, r)
        self.assertIn("REFUSED LEVEL-UP PLAN LINES", p)
        self.assertIn("- Wyll L5: level_up: FAILED", p)
        self.assertNotIn("REFUSED LEVEL-UP", keeper.build_prompt(self.mem, "S012", 12, "", 5))

    def test_refused_line_completed_later_is_dropped(self):
        acts = [{"tool": "level_up", "result": self.WYLL_FAIL}, {"tool": "level_up", "result": self.CAMP_OK}]
        self.assertEqual(keeper.refused_plans(acts), [])
        # each chunk of a camp result is its own level: a refused L5 after a done L4 is still reported
        mixed = self.CAMP_OK.split("\nL5:")[0] + "\nL5: " + self.WYLL_FAIL
        self.assertEqual(
            [(x["name"], x["level"]) for x in keeper.refused_plans([{"tool": "level_up", "result": mixed}])],
            [("Wyll", 5)],
        )

    def test_harness_click_failure_is_not_a_refused_plan(self):
        # S032 17:28: Wyll L6's valid pick was lost by the tool ("spell picks pending"); the keeper was told to
        # rewrite it
        lost = (
            "level_up: FAILED \u2014 finish: spell picks pending (Hunger of Hadar): the level_up tool clicks these "
            "itself; nothing to click by hand \u2014 waited_fade closed_page=a page stuck in its intro\n"
            "(plan: Wyll L6 from memory/party-build.md) (clicked spells: Hunger of Hadar)"
        )
        self.assertEqual(keeper.refused_plans([{"tool": "level_up", "result": lost}]), [])
        self.assertEqual(
            [(x["name"], x["level"]) for x in keeper.refused_plans([{"tool": "level_up", "result": self.WYLL_FAIL}])],
            [("Wyll", 5)],
        )

    def test_logs_to_read_since_stalest_file(self):
        self.assertEqual(keeper.logs_to_read(self.mem, 3), [1, 2, 3])
        for n in range(4, 15):
            self.write(f"sessions/S{n:03d}.md", "x\n")
        self.assertEqual(len(keeper.logs_to_read(self.mem, 14)), keeper.MAX_LOGS)
        self.assertEqual(keeper.logs_to_read(self.mem, 14)[-1], 14)

    # ---- MEM-1 (audit 2026-10-13)

    def test_ungrounded_names_are_dropped(self):
        r = self.run_keeper("hallucinate")
        self.assertEqual(r.status, "ok", r.summary())
        q = self.read("quests.md")
        self.assertNotIn("Qxzarnath", q)
        self.assertIn("Find the tiefling kids [seen]", q)
        self.assertEqual(len(r.ungrounded), 1)
        self.assertIn("Qxzarnath", r.summary())

    def test_grounding_rules(self):
        text = "met zevlor at the gate. nightsong"
        self.assertEqual(keeper.ungrounded_names("- Talk to Zevlor at the Gate [seen]", text), [])
        self.assertEqual(keeper.ungrounded_names("- Talk to Ketheric [seen]", text), ["Ketheric"])
        self.assertEqual(keeper.ungrounded_names("- WHEN Nightsong, fall: S012 L5 [seen]", text), [])
        self.assertEqual(keeper.ungrounded_names("- Done. Ketheric fell [seen]", text), [])  # a sentence start

    def test_a_possessive_name_is_grounded_by_the_name(self):  # S071: the true Raging Vortex lesson was dropped
        text = "raging vortex (air myrmidon) silences everyone in it. ketheric"
        line = "- Air Myrmidon's Raging Vortex silences everyone inside: keep casters out (S071) [seen]"
        self.assertEqual(keeper.ungrounded_names(line, text), [])
        self.assertEqual(keeper.ungrounded_names("- Avoid Ketheric\u2019s aura [seen]", text), [])
        self.assertEqual(keeper.ungrounded_names("- Avoid Qxzarnath's lair [seen]", text), ["Qxzarnath"])

    def test_no_pending_snapshot_after_a_pass(self):
        self.run_keeper("ok")
        self.assertFalse((self.base.parent / keeper.PENDING).exists())

    def test_recover_after_a_runner_kill_mid_pass(self):
        quests = self.read("quests.md")
        snap = keeper.snapshot(self.mem)
        keeper.save_pending(self.base.parent, self.mem, "S003", snap)  # what run_keeper wrote before the call
        self.append("quests.md", "\n- Unvalidated [seen] (S003)\n")  # the keeper's edit, never checked
        msg = keeper.recover(self.mem, self.base.parent)
        self.assertIn("never finished", msg)
        self.assertIn("quests.md", msg)
        self.assertEqual(self.read("quests.md"), quests)
        self.assertFalse((self.base.parent / keeper.PENDING).exists())
        self.assertIsNone(keeper.recover(self.mem, self.base.parent))

    def test_recover_keeps_a_committed_pass(self):
        r = self.run_keeper("ok")
        keeper.save_pending(self.base.parent, self.mem, "S003", {})  # died between the commit and the cleanup
        self.assertIn("committed", keeper.recover(self.mem, self.base.parent))
        self.assertIn("Find the tiefling kids", self.read("quests.md"))
        self.assertTrue(r.commit)

    def test_recover_kills_a_leftover_keeper(self):
        import subprocess

        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        pend = keeper.save_pending(self.base.parent, self.mem, "S003", keeper.snapshot(self.mem))
        keeper.note_pgid(pend, p.pid)
        keeper.recover(self.mem, self.base.parent)
        p.wait(timeout=5)
        self.assertIsNotNone(p.returncode)

    def test_logs_to_read_starts_after_the_last_keeper_pass(self):
        for n in range(4, 15):
            self.write(f"sessions/S{n:03d}.md", "x\n")
        self.commit("S014")
        self.append("quests.md", "\n- Kept [seen] (S012)\n")
        self.commit("S012 memory-keeper: quests.md")
        self.assertEqual(keeper.logs_to_read(self.mem, 14), [13, 14])  # a never-touched file no longer pulls S001 in
        self.write("chapters/C01_S001-S014_x.md", "# C01\n")
        self.commit("S014 chapter")
        self.assertEqual(keeper.logs_to_read(self.mem, 14), [14])  # always the session that just ended


if __name__ == "__main__":
    unittest.main()


def test_a_set_stop_ends_the_client_call_like_a_timeout(tmp_path):
    # Ctrl-C on bg3play-serve during the keeper pass: the client (its own process group) played on after the app
    import threading

    stop = threading.Event()
    stop.set()
    t0 = time.monotonic()
    rc, timed_out = keeper._run_claude(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, tmp_path / "k.jsonl", 60, stop=stop
    )
    assert timed_out and rc is not None and time.monotonic() - t0 < 30
