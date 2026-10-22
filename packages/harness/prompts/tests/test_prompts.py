"""The prompt files and their assembly (assemble.py): lint of the rules (research/arch/bench.md §2.6 tests a-e,
docs/BENCHMARK-BIAS.md items 2, 3 and 9), the byte-stable assembly, and play/CLAUDE.md as its generated copy.

    python3 -m unittest discover -s packages/harness/prompts/tests
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import bg3harness

HERE = Path(__file__).resolve().parent
PROMPTS = HERE.parent
REPO = PROMPTS.parents[2]
A = bg3harness.prompts()

# The server's tool list (tools/list wire form), as tools/pi/dump_server.py --tools writes it.
TOOLS = {t["name"] for t in json.loads((REPO / "tools/pi/conformance/tools.json").read_text(encoding="utf-8"))}
ANECDOTE_RE = re.compile(r"\bS\d{3}\b|claude-run|naut|runtime/|playthroughs/")
CLIENT_RE = re.compile(r"\bclaude\b|codex|gemini|openai|anthropic|deepseek|openrouter|\bpi\b|CLAUDE\.md", re.IGNORECASE)
CLIENT_ONLY_RE = re.compile(r"mcp__|ToolSearch|alwaysLoad|your turn|context compaction|slash command", re.IGNORECASE)
# Backticked words in the rules that are not tools: events, result tokens, arguments, ids, paths.
TERMS = {
    "bg3",
    "e12",
    "j12",
    "full",
    "objective",
    "target",
    "started",
    "job_finished",
    "queue_done",
    "queue",
    "queue_interrupted",
    "my_turn",
    "idle",
    "in_combat",
    "hp_below",
    "item_present",
    "target_visible",
    "in_dialogue",
    "stopped",
    "done",
    "dialogue_line",
    "memory",
    "knowledge",
    "path",
    "lint",
}
# What a rules text must not say when a group is off (bench.md §2.6 test d): its tools and its words.
GROUP_WORDS = {
    "memory": r"memory|knowledge/|notes? file",
    "wiki": r"wiki",
    "reload": r"reload",
    "saves": r"SAVED",
    "screenshots": r"screenshot",
    "raw_input": r"\bclick\b|press_key",
}
# The results' coaching nudges (bridge reporting.py, batch.py): served, and explained, only in rules+coaching.
COACHING_MARKERS = ("NEXT:", "PACE:", "NOTES WINDOW")
MAX_PART = 6000
MAX_RULES = 10000
MAX_COACHED = 30000


def parts_in(sub: str) -> list[Path]:
    return sorted((PROMPTS / sub).rglob("*.md"))


def tools_named(text: str) -> set[str]:
    return {t for t in TOOLS if re.search(rf"(?<![\w/.-]){re.escape(t)}(?![\w-])", text)}


class Lint(unittest.TestCase):
    def test_no_session_anecdotes_anywhere(self):
        for p in parts_in("rules") + parts_in("coaching") + parts_in("clients"):
            self.assertIsNone(ANECDOTE_RE.search(p.read_text(encoding="utf-8")), p)

    def test_rules_and_coaching_are_client_neutral(self):
        for p in parts_in("rules") + parts_in("coaching"):
            self.assertIsNone(CLIENT_RE.search(p.read_text(encoding="utf-8")), p)
        for p in parts_in("rules"):
            self.assertIsNone(CLIENT_ONLY_RE.search(p.read_text(encoding="utf-8")), p)

    def test_backticked_words_in_the_rules_are_tools_or_known_terms(self):
        text = A.assemble("rules").text
        for span in re.findall(r"`([^`\n]+)`", text):
            m = re.match(r"[a-z][a-z0-9_]*", span)
            if m and not span.startswith(("memory/", "knowledge/")):
                self.assertIn(m.group(0), TOOLS | TERMS, f"`{span}`: not a server tool nor a known term")

    def test_capability_tools_exist_on_the_server(self):
        for group, names in A.CAPABILITIES.items():
            self.assertLessEqual(set(names), TOOLS, group)

    def test_capabilities_match_the_bridge_groups(self):
        path = REPO / "bridge/src/bg3ai/capabilities.py"
        if not path.exists():
            self.skipTest("bridge capabilities.py not merged yet")
        spec = importlib.util.spec_from_file_location("bg3caps", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual({k: set(v) for k, v in mod.GROUPS.items()}, {k: set(v) for k, v in A.CAPABILITIES.items()})

    def test_every_condition_names_only_tools_it_exposes(self):
        for cond in A.CONDITIONS:
            self.assertLessEqual(tools_named(A.assemble(cond).text), A.allowlist(TOOLS), cond)

    def test_a_group_that_is_off_is_not_mentioned(self):
        for group in A.CAPABILITIES:
            caps = A.ALL_CAPS - {group}
            text = A.assemble("rules", caps=caps).text
            self.assertLessEqual(tools_named(text), A.allowlist(TOOLS, caps), group)
            self.assertIsNone(re.search(GROUP_WORDS[group], text, re.IGNORECASE), group)
        bare = A.assemble("rules", caps=frozenset()).text
        self.assertEqual(tools_named(bare) & {t for names in A.CAPABILITIES.values() for t in names}, set())
        for group, words in GROUP_WORDS.items():
            self.assertIsNone(re.search(words, bare, re.IGNORECASE), group)

    def test_lengths(self):
        for p in parts_in("rules") + parts_in("coaching") + parts_in("clients"):
            self.assertLessEqual(len(p.read_text(encoding="utf-8")), MAX_PART, p)
        self.assertLessEqual(len(A.assemble("rules").text), MAX_RULES)
        self.assertLessEqual(len(A.assemble("rules+coaching", "claude").text), MAX_COACHED)

    def test_every_file_is_used(self):
        used = {f for c in A.CONDITIONS for f in A.part_files(c, "claude")} | {
            A.PACE_PREFLIGHT,
            "clients/claude-kickoff.md",
        }
        on_disk = {str(p.relative_to(PROMPTS)) for p in parts_in("rules") + parts_in("coaching") + parts_in("clients")}
        self.assertEqual(on_disk, used)


class Assembly(unittest.TestCase):
    def test_byte_identical_across_assemblies(self):
        for cond in A.CONDITIONS:
            a, b = A.assemble(cond), A.assemble(cond)
            self.assertEqual(a, b)
            self.assertEqual(a.sha256, hashlib.sha256(a.text.encode()).hexdigest())

    def test_parts_hash_their_files_in_order(self):
        got = A.assemble("rules+coaching")
        self.assertEqual([p["file"] for p in got.parts], A.part_files("rules+coaching"))
        for p in got.parts:
            self.assertEqual(p["sha256"], hashlib.sha256((PROMPTS / p["file"]).read_bytes()).hexdigest())
        self.assertTrue(got.text.startswith("# You are playing Baldur's Gate 3\n"))
        self.assertTrue(got.text.endswith("\n") and not got.text.endswith("\n\n"))

    def test_rules_is_a_prefix_of_rules_plus_coaching(self):
        # the shared prompt-cache prefix, and the coaching only adds
        self.assertTrue(A.assemble("rules+coaching").text.startswith(A.assemble("rules").text.rstrip("\n") + "\n\n"))

    def test_rules_only_has_no_coaching_markers(self):
        # the server's coaching nudges (bridge rules.coaching) come only in rules+coaching, and so does their text
        for client in A.CLIENTS:
            text = A.assemble("rules", client).text
            for marker in COACHING_MARKERS:
                self.assertNotIn(marker, text, client)
        coached = A.assemble("rules+coaching").text
        for marker in COACHING_MARKERS:
            self.assertIn(marker, coached)

    def test_a_coaching_edit_changes_the_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for f in set(A.part_files("rules+coaching")):
                (root / f).parent.mkdir(parents=True, exist_ok=True)
                (root / f).write_bytes((PROMPTS / f).read_bytes())
            before = A.assemble("rules+coaching", root=root)
            (root / "coaching/style.md").write_text("## Style\n- Be quick.\n", encoding="utf-8")
            after = A.assemble("rules+coaching", root=root)
            self.assertNotEqual(before.sha256, after.sha256)
            self.assertEqual(A.assemble("rules", root=root), A.assemble("rules"))

    def test_only_claude_gets_an_appendix(self):
        neutral = A.assemble("rules").text
        for client in ("mcp", "codex", "gemini", "pi"):
            self.assertEqual(A.assemble("rules", client).text, neutral)
        claude = A.assemble("rules", "claude").text
        self.assertIn("mcp__bg3__<name>", claude)
        self.assertNotIn("mcp__", neutral)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            A.assemble("coaching")
        with self.assertRaises(ValueError):
            A.assemble("rules", "cursor")
        with self.assertRaises(ValueError):
            A.assemble("rules", caps={"memory", "telepathy"})
        with self.assertRaises(ValueError):  # the coaching names memory files and the wiki throughout
            A.assemble("rules+coaching", caps=A.ALL_CAPS - {"wiki"})

    def test_pace_preflight(self):
        mcp, claude = A.pace_preflight(), A.pace_preflight("claude")
        self.assertTrue(mcp.startswith("PACE PRE-FLIGHT (read before your first game action):\n- The character"))
        self.assertNotIn("mcp__", mcp)
        self.assertEqual(
            claude.splitlines()[1],
            "- Game tools are named mcp__bg3__<name> (e.g. mcp__bg3__get_status); the short names below are shorthand.",
        )
        self.assertEqual(claude.splitlines()[2:], mcp.splitlines()[1:])


class ClaudeMd(unittest.TestCase):
    def test_play_claude_md_is_the_generated_text(self):
        # A failure here: a prompt file changed. Run:
        #   python3 packages/harness/prompts/assemble.py rules+coaching --client claude --out play/CLAUDE.md
        self.assertEqual((REPO / "play/CLAUDE.md").read_text(encoding="utf-8"), A.claude_md())

    def test_check_flag(self):
        r = subprocess.run(
            [
                sys.executable,
                str(PROMPTS / "assemble.py"),
                "rules+coaching",
                "--client",
                "claude",
                "--out",
                str(REPO / "play/CLAUDE.md"),
                "--check",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "rules.md"
            out.write_text("old\n", encoding="utf-8")
            argv = [sys.executable, str(PROMPTS / "assemble.py"), "rules", "--out", str(out)]
            self.assertEqual(subprocess.run([*argv, "--check"], capture_output=True, check=False).returncode, 1)
            self.assertEqual(subprocess.run(argv, capture_output=True, check=False).returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), A.assemble("rules").text)


if __name__ == "__main__":
    unittest.main()
