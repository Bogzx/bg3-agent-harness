#!/usr/bin/env python3
"""The documented commands, run as written, so the docs can't rot. No game, no key, no network.

- docs/QUICKSTART.md "Test your own model": the profile parses, and the `play.py run` line with `--plan` (as the
  doc tells you to try first) plans a pi series on it; the e2e class plays that profile through pi on the scripted
  stub model, the real bridge and the fake mod (runner/test_pi_e2e.py's harness).
- docs/MCP-SERVER.md: the group table is capabilities.GROUPS; the client config and the Python client start the
  server and read the game state from the fake mod; the memory command, the typo refusal, BG3AI_TOOLS_OFF, the
  conditions, BG3AI_SERVE_RULES=0 and the HTTP transport behave as the page says.

    uv run pytest tools/pi/test_docs.py               (the e2e class: uv run pytest -m e2e tools/pi/test_docs.py)

The e2e class needs tools/pi/node_modules (cd tools/pi && npm ci --ignore-scripts); skipped without it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))  # a no-op under the uv workspace
from bg3harness import profiles  # noqa: E402

QUICKSTART = REPO / "docs" / "QUICKSTART.md"
MCP_DOC = REPO / "docs" / "MCP-SERVER.md"
PI = REPO / "tools" / "pi" / "node_modules" / ".bin" / "pi"
KEY = "not-a-real-key"
WAIT_S = 60

try:
    from bg3harness.testing.fakemod import FakeMod
    from bg3harness.testing.mcp_bridge import bridge_env
    from bg3harness.testing.scripted import FakeClock, GameModel
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    UV_ENV = True
except ImportError:  # system python: the MCP-SERVER.md checks need the uv env (mcp, bg3harness.testing)
    UV_ENV = False


def section(text: str, heading: str) -> str:
    """The doc text under `heading` (a whole line, e.g. "## Tools") up to the next heading of its level or above."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading) + 1
    for i in range(start, len(lines)):
        m = re.match(r"(#+) ", lines[i])
        if m and len(m.group(1)) <= level and not _in_fence(lines[:i]):
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:])


def _in_fence(lines: list[str]) -> bool:
    return sum(ln.lstrip().startswith("```") for ln in lines) % 2 == 1


def blocks(text: str, lang: str) -> list[str]:
    return re.findall(r"^ *```" + lang + r"\n(.*?)^ *```", text, re.M | re.S)


def quickstart() -> str:
    return section(QUICKSTART.read_text(), "## Test your own model")


def doc_profile() -> dict:
    return tomllib.loads(blocks(quickstart(), "toml")[0])


def doc_commands() -> list[list[str]]:
    """The "### 2. One command" block, one argv per command (backslash continuations joined)."""
    text = blocks(section(quickstart(), "### 2. One command"), "bash")[0]
    return [shlex.split(c) for c in text.replace("\\\n", " ").splitlines() if c.strip()]


class QuickstartCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-doc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_documented_profile_is_a_valid_pi_profile(self):
        p = profiles.parse(doc_profile(), "QUICKSTART.md")
        self.assertEqual((p.client, p.model, p.effort), ("pi", "vendor/model-id", "high"))
        assert p.routing is not None and p.endpoint is not None
        self.assertEqual(p.routing["only"], ["vendor"])
        self.assertEqual(p.endpoint.api_key_env, "OPENROUTER_API_KEY")

    def test_the_documented_run_line_plans_a_pi_series(self):
        cmds = doc_commands()
        run = next(c for c in cmds if c[1:3] == ["runner/play.py", "run"])
        self.assertEqual(run[0], "python3")
        profile = self.tmp / "my-model.toml"
        profile.write_text(blocks(quickstart(), "toml")[0])
        argv = [str(profile) if a == "profiles/my-model.toml" else a for a in run]
        self.assertIn(str(profile), argv)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BG3AI_", "OPENROUTER"))}
        rt = self.tmp / "runtime"
        env.update(OPENROUTER_API_KEY=KEY, BG3AI_RUNTIME=str(rt), BG3AI_PLAYTHROUGHS=str(self.tmp / "pt"))
        out = subprocess.run([*argv, "--plan"], cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing launched or written", out.stdout)
        self.assertIn("client pi, model vendor/model-id, effort high", out.stdout)
        self.assertIn("'max_cost': None", out.stdout)  # nautiloid's caps: --max-cost 5 is the spend cap
        self.assertIn("spend cap $5", out.stdout)
        self.assertIn('"openrouter/vendor/model-id"', out.stdout)  # the pi command's --model
        self.assertNotIn(KEY, out.stdout + out.stderr)
        self.assertFalse(rt.exists())  # no series.json, no budget.json

        runs = next(c for c in cmds if c[1:3] == ["runner/play.py", "runs"])
        out = subprocess.run(runs, cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("no run records for naut-my-model", out.stdout)


if UV_ENV and PI.exists():
    import pytest

    sys.path.insert(0, str(REPO / "runner"))  # test_pi_e2e and its test_crash, testenv
    import test_pi_e2e as T  # noqa: N812  runner/: the real Runner.play_session of pi on the stub model and the fake mod

    @pytest.mark.e2e
    class QuickstartPlay(T.PiE2EBase):
        def test_the_documented_profile_plays_through_pi(self):
            self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
            os.environ["OPENROUTER_API_KEY"] = KEY
            raw = doc_profile()
            del raw["routing"]  # OpenRouter's provider pin: refused for a base_url that isn't openrouter.ai
            res = self.play([{"tool_calls": [T.call("get_status")]}, {"text": "Done."}], raw=raw)
            self.assertEqual(res.reason, "exited")
            self.assertIn("gold 100", self.results()["get_status"])
            req = self.requests()[0]
            self.assertEqual(req["model"], "vendor/model-id")
            self.assertEqual(T.text_of(req["messages"][0]["content"]), self.system)  # the rules condition
            self.assertAlmostEqual(self.spent(), 1.0 + 2 * T.COST)


async def _serve(env: dict[str, str], tool: str | None = "get_status") -> dict:
    """Start `uv run --project bridge bg3ai-mcp` (stdio) with env; what a client sees."""
    params = StdioServerParameters(
        command="uv", args=["run", "--project", str(REPO / "bridge"), "bg3ai-mcp"], env=env, cwd=str(REPO)
    )
    from mcp.types import TextContent, TextResourceContents

    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        rules = (await s.read_resource("bg3://rules")).contents[0]
        assert isinstance(rules, TextResourceContents)
        out = {
            "instructions": init.instructions or "",
            "tools": {t.name for t in (await s.list_tools()).tools},
            "prompts": [p.name for p in (await s.list_prompts()).prompts],
            "resource": rules.text,
        }
        if tool:
            result = (await s.call_tool(tool, {})).content[0]
            assert isinstance(result, TextContent)
            out["result"] = result.text
        return out


def _listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


async def _http(url: str) -> set[str]:
    async with AsyncExitStack() as st:
        r, w = await st.enter_async_context(streamable_http_client(url))
        s = await st.enter_async_context(ClientSession(r, w))
        await s.initialize()
        return {t.name for t in (await s.list_tools()).tools}


@unittest.skipUnless(UV_ENV, "needs the uv env (mcp, bg3harness.testing): uv run pytest tools/pi/test_docs.py")
class McpServerDoc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = MCP_DOC.read_text()
        cls.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-mcpdoc-"))
        cls.fake = FakeMod(GameModel(clock=FakeClock())).start()
        # the doc's memory command, into the scratch dir
        cmd = next(
            shlex.split(ln.split("#")[0])
            for ln in blocks(section(cls.text, "## A memory"), "bash")[0].splitlines()
            if "--memory-dir" in ln
        )
        cls.memory = cls.tmp / "memory"
        cmd = [str(cls.memory) if a == "DIR" else a for a in cmd]
        subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        shutil.rmtree(cls.tmp, True)

    def env(self, **extra: str) -> dict[str, str]:
        home = Path(tempfile.mkdtemp(dir=self.tmp))
        return bridge_env(self.fake.port, home, {"BG3AI_MEMORY_DIR": str(self.memory), **extra})

    def test_the_group_table_is_the_servers(self):
        from bg3ai import capabilities

        rows = re.findall(r"^\| `(\w+)` \| (.+) \|$", section(self.text, "## Tools"), re.M)
        self.assertEqual({g: tuple(re.findall(r"`(\w+)`", t)) for g, t in rows}, capabilities.GROUPS)

    def test_the_config_and_the_python_client_read_the_game(self):
        config = json.loads(blocks(section(self.text, "## A minimal client config"), "json")[0])
        bg3 = config["mcpServers"]["bg3"]
        bg3["args"] = [a.replace("/path/to/bg3-ai", str(REPO)) for a in bg3["args"]]
        bg3["env"] = self.env(BG3AI_AUTO_SESSION="1")  # the doc's memory var, plus the fake mod and scratch dirs
        (self.tmp / "mcp.json").write_text(json.dumps(config))
        (self.tmp / "client.py").write_text(blocks(self.text, "python")[0])
        m = re.search(r"`(uv run --project bridge python client\.py mcp\.json)`", self.text)
        assert m, "MCP-SERVER.md names the client command"
        run = shlex.split(m.group(1))
        run = [str(self.tmp / a) if a in ("client.py", "mcp.json") else a for a in run]
        out = subprocess.run(run, cwd=REPO, capture_output=True, text=True, timeout=180)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"\d{4,} characters of instructions")
        self.assertRegex(out.stdout, r"\n\d+ tools\n")
        self.assertIn("=== RULES (once per session", out.stdout)  # with a memory: the first result opens S001
        self.assertIn("gold 100", out.stdout)
        self.assertTrue((self.memory / "sessions" / "S001.md").exists())

    def test_the_rules_ways_and_the_tool_switches(self):
        rules = asyncio.run(_serve(self.env(), tool=None))
        self.assertIn("=== RULES ===", rules["instructions"])
        self.assertEqual(rules["prompts"], ["rules"])
        self.assertIn(rules["resource"], rules["instructions"])
        self.assertIn("get_status", rules["tools"])

        off = asyncio.run(_serve(self.env(BG3AI_TOOLS_OFF="wiki,reload"), tool=None))
        self.assertEqual(rules["tools"] - off["tools"], {"wiki_search", "wiki_page", "load_game"})
        self.assertLess(len(off["resource"]), len(rules["resource"]))  # the off groups' rules are left out

        keep = asyncio.run(_serve(self.env(BG3AI_TOOLS="wiki,saves"), tool=None))
        self.assertTrue({"wiki_page", "save_game", "get_status"} <= keep["tools"])
        self.assertFalse({"memory_read", "load_game", "screenshot", "press_key"} & keep["tools"])

        coach = asyncio.run(_serve(self.env(BG3AI_PROMPT_CONDITION="rules+coaching"), tool=None))
        self.assertGreater(len(coach["resource"]), 2.5 * len(rules["resource"]))  # "about three times as long"

        quiet = asyncio.run(_serve(self.env(BG3AI_SERVE_RULES="0", BG3AI_AUTO_SESSION="1")))
        self.assertNotIn("=== RULES", quiet["instructions"] + quiet["result"])
        self.assertEqual(quiet["resource"], coach["resource"])  # still served, and rules+coaching

    def test_a_typo_stops_the_server(self):
        m = re.search(r"^ *(ValueError: BG3AI_TOOLS: .+)$", section(self.text, "## Tools"), re.M)
        assert m, "MCP-SERVER.md shows the typo's error"
        want = m.group(1)
        out = subprocess.run(
            ["uv", "run", "--project", "bridge", "bg3ai-mcp"],
            cwd=REPO, env=self.env(BG3AI_TOOLS="memroy"), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(want, out.stderr)

    def test_the_http_transport(self):
        cmd = next(
            shlex.split(ln.split("#")[0])
            for ln in blocks(section(self.text, "## Start it"), "bash")[0].splitlines()
            if "--transport http" in ln
        )
        with socket.socket() as s:  # a free port instead of the doc's 8770, which may be in use
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cmd = [str(port) if a == "8770" else a for a in cmd]
        p = subprocess.Popen(cmd, cwd=REPO, env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)
        end = time.monotonic() + WAIT_S
        while not _listening(port):
            self.assertIsNone(p.poll(), "the http server exited")
            self.assertLess(time.monotonic(), end, "the http server never listened")
            time.sleep(0.1)
        self.assertIn("get_status", asyncio.run(_http(f"http://127.0.0.1:{port}/mcp")))


if __name__ == "__main__":
    unittest.main()
