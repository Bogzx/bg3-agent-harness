#!/usr/bin/env python3
"""Offline conformance of the installed codex CLI with runner/clients.py's CodexClient: CodexClient's own command
(features off, -c overrides, the bg3 MCP server) runs codex against stub_responses.py (a scripted Responses-API
model, as a custom model_provider) and tools/pi/conformance/stub_mcp.py (the real tool list, no game), with
CODEX_HOME in a temp dir (~/.codex is never read or written). No key, no network, no game; ~10 s. Skips when
codex is not on PATH.

    python3 -m unittest tools/codex/conformance/test_codex_conformance.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PI_CONF = REPO / "tools" / "pi" / "conformance"
sys.path.insert(0, str(REPO / "runner"))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import clients as C  # noqa: E402, N812

sys.path.insert(0, str(PI_CONF))
import stub_mcp  # noqa: E402

BINARY = shutil.which("codex")
VERSION = "codex-cli 0.157.1"  # the version CodexClient was written against
TOOLS = json.loads((PI_CONF / "tools.json").read_text())
NAMESPACE = "mcp__bg3"  # codex 0.157.1 offers MCP tools as one namespace tool per server
# what else the model gets with CODEX_DISABLE: codex's MCP resource tools and request_user_input (exec mode answers
# it with nothing); no shell, file, web or agent tool
EXTRA_TOOLS = {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource", "request_user_input"}


def wait_until(cond, what: str, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


@unittest.skipUnless(BINARY, "codex is not on PATH")
class Conformance(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="bg3-codex-conf-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        (self.ws / "home").mkdir()
        self.stub: subprocess.Popen | None = None

    def tearDown(self):
        if self.stub:
            self.stub.kill()
            self.stub.wait()

    def client(self, script: list[dict]) -> C.CodexClient:
        if self.stub:
            self.stub.kill()
            self.stub.wait()
        (self.ws / "script.json").write_text(json.dumps(script))
        port = self.ws / "port"
        port.unlink(missing_ok=True)
        args = [str(self.ws / "script.json"), str(self.ws / "llm.jsonl"), str(port)]
        self.stub = subprocess.Popen([sys.executable, str(HERE / "stub_responses.py"), *args])
        wait_until(lambda: port.exists() and port.read_text(), "the stub model's port")
        env = {"STUB_MCP_LOG": str(self.ws / "mcp.jsonl"), "STUB_MCP_DELAY": "0"}
        server = {"command": sys.executable, "args": [str(PI_CONF / "stub_mcp.py")], "env": env}
        stub = {"name": "stub", "base_url": f"http://127.0.0.1:{port.read_text()}/v1", "wire_api": "responses"}
        extra = {"model_provider": "stub", "model_providers.stub": stub}
        return C.CodexClient(server=server, extra_config=extra, cwd=self.ws)

    def run_codex(self, client: C.CodexClient, req: C.Request) -> list[C.Event]:
        p = subprocess.run(
            client.command(req),
            cwd=self.ws,
            env={**os.environ, "CODEX_HOME": str(self.ws / "home")},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return [e for line in p.stdout.splitlines() for e in client.parse(line)]

    def requests(self) -> list[dict]:
        return [json.loads(x)["body"] for x in (self.ws / "llm.jsonl").read_text().splitlines()]

    def test_version(self):
        out = subprocess.run([BINARY, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertEqual(out, VERSION)

    def test_a_bg3_tool_call_round_trip(self):
        call = {"name": "get_status", "namespace": NAMESPACE, "arguments": {"who": "Lae'zel"}}
        client = self.client([{"tool_calls": [call]}, {"text": "done"}])
        events = self.run_codex(client, C.Request(prompt="play", model="stub", session_dir=self.ws, effort="high"))
        kinds = [e.kind for e in events if e.kind != "error"]  # error: the 'no model metadata' warning item
        self.assertEqual(kinds, ["init", "tool_call", "tool_result", "text", "result"])
        tool_call, tool_result, res = events[-4], events[-3], events[-1]
        self.assertEqual((tool_call.tool, tool_call.args), ("get_status", {"who": "Lae'zel"}))
        self.assertEqual(tool_result.text, "ok get_status " + json.dumps(call["arguments"]) + "\n" + stub_mcp.CLOCK)
        self.assertFalse(tool_result.is_error)
        self.assertEqual((res.subtype, res.is_error, res.session_id), ("success", False, events[0].session_id))
        # two requests of 1000 input (400 cached) and 50 output each
        self.assertEqual(res.tokens, {"input": 1200, "output": 100, "cache_read": 800, "cache_write": 0})
        mcp = [json.loads(x) for x in (self.ws / "mcp.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(r["tool"], r["args"]) for r in mcp if r["ev"] == "start"], [("get_status", call["arguments"])]
        )

        first = self.requests()[0]
        tools = {t.get("name"): t for t in first["tools"]}
        self.assertEqual(set(tools), EXTRA_TOOLS | {NAMESPACE})
        names = [t["name"] for t in tools[NAMESPACE]["tools"]]  # codex sorts them by name
        self.assertEqual(names, sorted(t["name"] for t in TOOLS))
        self.assertEqual(tools[NAMESPACE]["description"], stub_mcp.INSTRUCTIONS)  # the server's instructions
        self.assertEqual(first["reasoning"]["effort"], "high")
        self.assertIn("play", json.dumps(first["input"]))

    def test_resume_reports_only_its_own_tokens(self):
        client = self.client([{"text": "first"}])
        req = C.Request(prompt="play", model="stub", session_dir=self.ws)
        first = self.run_codex(client, req)
        thread = first[0].session_id
        self.assertTrue(thread)
        again = C.Request(prompt="go on", model="stub", session_dir=self.ws, session=thread, resume=True)
        events = self.run_codex(client, again)
        res = events[-1]
        self.assertEqual((res.kind, res.subtype, res.session_id), ("result", "success", thread))
        self.assertEqual(res.tokens, {"input": 600, "output": 50, "cache_read": 400, "cache_write": 0})
        self.assertEqual([e.text for e in events if e.kind == "text"], ["first"])
        self.assertIn("go on", json.dumps(self.requests()[-1]["input"]))


if __name__ == "__main__":
    unittest.main()
