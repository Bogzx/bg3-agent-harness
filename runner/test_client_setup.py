#!/usr/bin/env python3
"""bg3harness.client_setup: --profile resolution, the API key, the expected tool count.

python3 -m unittest runner/test_client_setup.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
from bg3harness import client_setup as cs  # noqa: E402
from bg3harness import profiles  # noqa: E402

PI = str(REPO / "profiles" / "deepseek-flash.toml")
CLAUDE = str(REPO / "profiles" / "opus-5.5-claude-code.toml")
TOOLS = REPO / "tools" / "pi" / "conformance" / "tools.json"


class Resolve(unittest.TestCase):
    def test_without_profile(self):
        self.assertEqual(cs.resolve(None, None, None, None), cs.ClientChoice("claude", "opus", None))
        self.assertEqual(cs.resolve("codex", None, "gpt", "low"), cs.ClientChoice("codex", "gpt", "low"))
        with self.assertRaisesRegex(cs.SetupRefused, "needs --profile"):
            cs.resolve("pi", None, None, None)

    def test_pi_profile(self):
        c = cs.resolve(None, PI, None, None)
        self.assertEqual((c.client, c.model, c.effort), ("pi", "deepseek-flash", "high"))
        self.assertEqual(cs.resolve("pi", PI, "deepseek-flash", "low").effort, "low")  # --effort wins
        for args, why in (
            ((PI, "opus", None), "plays deepseek-flash"),
            ((PI, None, None, None, True), "--no-keeper"),
            ((PI, None, None, "other"), "--fix-model"),
            (("/nope.toml", None, None), "nope"),
        ):
            with self.assertRaisesRegex(cs.SetupRefused, why):
                cs.resolve(None, *args)
        with self.assertRaisesRegex(cs.SetupRefused, "pi profile"):
            cs.resolve("claude", PI, None, None)

    def test_claude_profile(self):
        c = cs.resolve(None, CLAUDE, None, None, keeper=True)
        self.assertEqual(c.client, "claude")


class ApiKey(unittest.TestCase):
    def setUp(self):
        self.prof = cs.resolve(None, PI, None, None).profile
        self.dotenv = Path(tempfile.mkdtemp(prefix="bg3ai-env-")) / ".env"
        self.addCleanup(lambda: (self.dotenv.unlink(missing_ok=True), self.dotenv.parent.rmdir()))

    def test_from_env_or_dotenv(self):
        self.assertEqual(cs.api_key_env(self.prof, {"DEEPSEEK_API_KEY": "x"}, self.dotenv), {})
        self.dotenv.write_text('# keys\nexport DEEPSEEK_API_KEY="sk-1"\nOTHER=2\n')
        self.assertEqual(cs.api_key_env(self.prof, {}, self.dotenv), {"DEEPSEEK_API_KEY": "sk-1"})
        self.assertEqual(cs.api_key_env(None, {}, self.dotenv), {})

    def test_missing_is_refused_without_the_value(self):
        self.dotenv.write_text("DEEPSEEK_API_KEY=\n")
        with self.assertRaisesRegex(cs.SetupRefused, "DEEPSEEK_API_KEY"):
            cs.api_key_env(self.prof, {}, self.dotenv)


class CodexLogin(unittest.TestCase):
    def setUp(self):
        raw = {
            "client": "pi",
            "model": "gpt-x",
            "endpoint": {
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api",
                "api": profiles.CODEX_API,
                "api_key_env": "BG3AI_CODEX_ACCESS_TOKEN",
            },
            "model_info": {
                "input": ["text"],
                "reasoning": True,
                "context_window": 1000,
                "max_tokens": 10,
                "cost": {"input": 1, "output": 1, "cache_read": 1},
            },
        }
        self.prof = profiles.parse(raw)
        d = Path(tempfile.mkdtemp(prefix="bg3ai-codex-"))
        self.login, self.dotenv = d / "auth.json", d / ".env"
        self.addCleanup(shutil.rmtree, d, True)

    def jwt(self, exp, account="acct-1"):
        claims = {cs.CODEX_ACCOUNT_CLAIM: {"chatgpt_account_id": account}, "exp": exp}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"e30.{payload}.sig"

    def write(self, exp=4e9, account="acct-1", file_account="acct-1"):
        access = self.jwt(exp, account)
        tokens = {"id_token": "id-1", "access_token": access, "refresh_token": "rt-1", "account_id": file_account}
        self.login.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}))
        return access

    def key(self, environ=None):
        return cs.api_key_env(self.prof, environ or {}, self.dotenv, self.login)

    def test_only_the_access_token_comes_from_the_login(self):
        access = self.write()
        self.assertEqual(self.key(), {"BG3AI_CODEX_ACCESS_TOKEN": access})

    def test_the_file_is_left_as_it_was(self):
        self.write()
        os.utime(self.login, ns=(10**18, 10**18))  # an old stamp: any rewrite moves it, however fast
        before = (self.login.read_bytes(), self.login.stat().st_mtime_ns)
        self.key()
        self.assertEqual((self.login.read_bytes(), self.login.stat().st_mtime_ns), before)

    def test_env_wins_over_the_login(self):
        self.write()
        self.assertEqual(self.key({"BG3AI_CODEX_ACCESS_TOKEN": "x"}), {})

    def test_no_login_is_refused(self):
        with self.assertRaisesRegex(cs.SetupRefused, "no Codex CLI login"):
            self.key()
        self.login.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": None}))
        with self.assertRaisesRegex(cs.SetupRefused, "no Codex CLI login"):
            self.key()

    def test_an_expired_token_says_to_run_the_cli_and_names_no_value(self):
        access = self.write(exp=1)
        with self.assertRaises(cs.SetupRefused) as e:
            self.key()
        self.assertIn("has expired: run the Codex CLI once, it refreshes its own file", str(e.exception))
        for value in (access, "rt-1", "id-1"):
            self.assertNotIn(value, str(e.exception))

    def test_a_token_for_another_account_is_refused(self):
        self.write(account="acct-2")
        with self.assertRaisesRegex(cs.SetupRefused, "not for that file's account_id"):
            self.key()

    def test_another_endpoint_never_reads_the_login(self):
        self.write()
        other = cs.resolve(None, PI, None, None).profile
        with self.assertRaisesRegex(cs.SetupRefused, "DEEPSEEK_API_KEY"):
            cs.api_key_env(other, {}, self.dotenv, self.login)


class ToolCount(unittest.TestCase):
    def test_expected(self):
        names = cs.tool_names(TOOLS)
        self.assertEqual(cs.expected_tools(names, {}), len(names))
        self.assertEqual(cs.expected_tools(names, {"BG3AI_TOOLS_OFF": "wiki"}), len(names) - 2)
        from bg3ai import capabilities

        grouped = sum(len(g) for g in capabilities.GROUPS.values())
        self.assertEqual(cs.expected_tools(names, {"BG3AI_TOOLS": "game,saves"}), len(names) - grouped + 2)
        with self.assertRaises(ValueError):
            cs.expected_tools(names, {"BG3AI_TOOLS": "nope"})

    def test_a_server_without_a_memory_backend_serves_memory_off(self):
        """The dump of a server with no memory backend (the playthrough app not installed) has no memory group: the
        served set is the full registry's with memory off, not a refused registry."""
        from bg3ai import capabilities

        names = cs.tool_names(TOOLS)
        bare = [n for n in names if n not in capabilities.GROUPS["memory"]]
        for off in ("memory", "memory,wiki_search,wiki_page,load_game", ""):
            env = {"BG3AI_TOOLS_OFF": off} if off else {}
            want = cs.served_tools(names, {"BG3AI_TOOLS_OFF": ",".join(filter(None, (off, "memory")))})
            self.assertEqual(cs.served_tools(bare, env), want)
        partial = [n for n in names if n != "recall"]  # a renamed tool is still refused
        with self.assertRaisesRegex(ValueError, "not registered: recall"):
            cs.served_tools(partial, {})

    def test_problem(self):
        self.assertIsNone(cs.tool_count_problem(94, 94, {}))
        self.assertIsNone(cs.tool_count_problem(94, None, {}))
        self.assertEqual(
            cs.tool_count_problem(94, 76, {"BG3AI_TOOLS": "game"}),
            "the bg3 MCP server gave 94 tools, expected 76 (BG3AI_TOOLS=game)",
        )

    def test_claude_init_counts_the_bg3_tools(self):
        # the e2e claude run's init: 95 tools, 94 of them the bg3 server's (mcp__bg3__*) plus claude's ToolSearch;
        # registry: that run's server (bg3-tools.json of the same code)
        import json

        from bg3harness import clients

        fx = json.loads((REPO / "runner" / "fixtures" / "claude" / "e2e-naut-init.json").read_text())
        self.assertEqual(len(fx["init"]["tools"]), 95)
        (init,) = clients.ClaudeClient().parse(json.dumps(fx["init"]))
        self.assertEqual(init.tools, 94)
        self.assertIsNone(cs.tool_count_problem(init.tools, cs.expected_tools(fx["registry"], {}), {}))
        off = {"BG3AI_TOOLS_OFF": "wiki"}
        self.assertEqual(
            cs.tool_count_problem(init.tools, cs.expected_tools(fx["registry"], off), off),
            "the bg3 MCP server gave 94 tools, expected 92 (BG3AI_TOOLS_OFF=wiki)",
        )

    def test_dump_server_leaves_the_tool_selection_out_of_the_list_only(self):
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw["env"]))

        import os

        os.environ["BG3AI_TOOLS"] = "game"
        self.addCleanup(os.environ.pop, "BG3AI_TOOLS", None)
        cs.dump_server(Path("/s.txt"), Path("/t.json"), Path("/i.txt"), REPO, run=run)
        (lst, lst_env), (txt, txt_env) = seen
        self.assertEqual(lst[-2:], ["--tools", "/t.json"])
        self.assertNotIn("BG3AI_TOOLS", lst_env)  # the whole registry, for the count
        self.assertEqual(txt[-4:], ["--system", "/s.txt", "--instructions", "/i.txt"])
        self.assertEqual(txt_env["BG3AI_TOOLS"], "game")  # the texts the session's server serves


class SessionEnv(unittest.TestCase):
    """session_env / kickoff_pace: coaching (PACE:/NOTES WINDOW, the pace pre-flight) in playthroughs and claude."""

    def test_the_condition_default(self):
        cond = lambda client, scenario, env={}: cs.session_env(client, None, scenario, env).get(  # noqa: E731
            "BG3AI_PROMPT_CONDITION"
        )
        self.assertEqual(cond("pi", False), "rules+coaching")
        self.assertEqual(cond("pi", True), "rules")
        self.assertEqual(cond("claude", True), "rules+coaching")  # its play/CLAUDE.md
        self.assertIsNone(cond("pi", False, {"BG3AI_PROMPT_CONDITION": "rules"}))  # the operator's

    def test_a_text_only_profile_turns_screenshots_off(self):
        from dataclasses import replace

        prof = cs.profiles.load(Path(PI))
        text_only = replace(prof, model_info=replace(prof.model_info, input=("text",)))
        self.assertNotIn("BG3AI_TOOLS_OFF", cs.session_env("pi", prof, True, {}))
        got = cs.session_env("pi", text_only, True, {"BG3AI_TOOLS_OFF": "wiki"})
        self.assertEqual(got["BG3AI_TOOLS_OFF"], "wiki,screenshots")

    def test_kickoff_pace_follows_the_condition(self):
        self.assertEqual(cs.kickoff_pace("pi", {}), "")
        self.assertEqual(cs.kickoff_pace("pi", {"BG3AI_PROMPT_CONDITION": "rules"}), "")
        pi = cs.kickoff_pace("pi", {"BG3AI_PROMPT_CONDITION": "rules+coaching"})
        claude = cs.kickoff_pace("claude", {"BG3AI_PROMPT_CONDITION": "rules+coaching"})
        self.assertIn("PACE PRE-FLIGHT", pi)
        self.assertNotIn("mcp__bg3__", pi)  # claude's tool-name line only for claude
        self.assertIn("mcp__bg3__", claude)


class RunFiles(unittest.TestCase):
    """run_files: what a pi or claude run's play processes share (tool count, manifest; pi: the system text)."""

    def run_files(self, client: str, env: dict[str, str]) -> tuple[dict, list[str]]:
        import json
        import os
        from unittest import mock

        d = Path(tempfile.mkdtemp(prefix="bg3ai-runfiles-")) / "pt"
        logs: list[str] = []

        def dump(system: Path, tools: Path, instructions: Path, repo: Path) -> None:
            system.write_text("SYSTEM")
            tools.write_text(TOOLS.read_text())
            instructions.write_text("INSTRUCTIONS\n")

        with mock.patch.dict(os.environ, env):
            got = cs.run_files(
                d,
                client,
                1001,
                "realtime",
                REPO,
                logs.append,
                dump=dump,
                version=lambda c: "9.9",
            )
        json.dumps(got)  # the run record takes it as is
        return got, logs

    def test_claude_gets_the_tool_count_and_the_manifest(self):
        got, logs = self.run_files("claude", {})
        n = len(cs.tool_names(TOOLS))
        self.assertEqual(set(got), {"tools_expected", "tools_served", "manifest"})  # no system file: play/CLAUDE.md
        self.assertEqual(got["tools_expected"], n)
        self.assertEqual(got["tools_served"], tuple(sorted(cs.tool_names(TOOLS))))
        m = got["manifest"]
        self.assertEqual(
            (m["seed"], m["timing"], m["client"], m["condition"], m["tools"]["count"]),
            (1001, "realtime", {"name": "claude", "version": "9.9"}, "rules+coaching", n),
        )
        self.assertIn(f"claude: {n} bg3 tools expected, prompt rules+coaching sha256 ", logs[0])
        self.assertEqual(m["prompt"]["parts"][0]["file"], "server:instructions")  # hashed with the rules

    def test_pi_gets_its_system_text_and_the_served_condition(self):
        got, _ = self.run_files("pi", {"BG3AI_TOOLS_OFF": "wiki"})
        self.assertTrue(got["system_file"].endswith("pi-system.txt"))
        self.assertEqual(got["tools_expected"], len(cs.tool_names(TOOLS)) - 2)
        self.assertEqual(len(got["tools_served"]), got["tools_expected"])
        self.assertNotIn("wiki_search", got["tools_served"])
        self.assertEqual(
            (got["manifest"]["condition"], got["manifest"]["tools"]["count"]), ("rules", got["tools_expected"])
        )

    def test_coaching_with_a_group_off_records_the_served_prompt(self):  # live9 #12: it recorded an error
        got, logs = self.run_files("claude", {"BG3AI_TOOLS_OFF": "memory,reload,wiki"})
        full, _ = self.run_files("claude", {})
        self.assertEqual(got["manifest"]["condition"], "rules+coaching")
        self.assertNotIn("error", got["manifest"])
        self.assertEqual(got["manifest"]["prompt"], full["manifest"]["prompt"])  # play/CLAUDE.md is served whole
        self.assertIn("prompt rules+coaching sha256 ", logs[0])


if __name__ == "__main__":
    unittest.main()
