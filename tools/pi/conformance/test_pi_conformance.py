#!/usr/bin/env python3
"""Offline conformance of pi 1.3.0 + bg3.ts (research/arch/pi-client.md §3.5, checks 1-21 and 23; check 22, the
runner's tool-count check, is in runner/test_pi_client.py). Each test runs the pinned pi against stub_llm.py (a
scripted OpenAI-compatible model) and stub_mcp.py (the real tool list, no game) with HOME and pi's agent dir in a
temp dir. No key, no network, no game; ~1 min. Skips when tools/pi/node_modules is missing.

    python3 -m unittest tools/pi/conformance/test_pi_conformance.py

BG3AI_PI_CAPTURE=<dir> keeps each run's pi stdout there as <test>.jsonl, scrubbed (scrub()): runner/fixtures/pi/
was made that way.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PI_DIR = HERE.parent
REPO = PI_DIR.parents[1]
BINARY = PI_DIR / "node_modules" / ".bin" / "pi"
EXTENSION = PI_DIR / "bg3.ts"
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
from bg3harness import clients as C
from bg3harness import pi_client as PC
from bg3harness import profiles as PR

sys.path.insert(0, str(HERE))
import stub_mcp

TOOLS = json.loads((HERE / "tools.json").read_text())
PREFIX = "mcp__bg3__"
MODEL = "anthropic/stub-model"
SESSION = "conf-0001"
SYSTEM = "SYSTEM-CANARY-91\n\nRules of play: line two, with trailing spaces   \n"
# What bg3harness/pi_client.py writes (PI_SETTINGS) plus fast retries, so check 20 takes ms, not 14 s.
SETTINGS = {
    "compaction": {"enabled": False},
    "cacheWarming": "off",
    "enableInstallTelemetry": False,
    "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 10},
    "defaultTools": [],
    "quietStartup": True,
}
PROFILE = {
    "client": "pi",
    "model": MODEL,
    "effort": "high",
    "endpoint": {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "BG3_STUB_KEY"},
    "routing": {"only": ["anthropic"]},
    "model_info": {
        "input": ["text", "image"],
        "reasoning": True,
        "context_window": 200000,
        "max_tokens": 16000,
        "cost": {"input": 1.0, "output": 4.0, "cache_read": 0.1},
    },
}
WAIT_S = 30  # upper bound on any one pi process or condition; a pass takes far less


def call(name: str, args: dict | str | None = None) -> dict:
    return {"name": PREFIX + name, "arguments": {} if args is None else args}


def wait_until(cond, what: str, limit: float = WAIT_S) -> None:
    end = time.monotonic() + limit
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def text_of(content) -> str:
    """A request message's content as text (pi sends a block list when it adds cache_control)."""
    return content if isinstance(content, str) else "".join(c.get("text", "") for c in content)


class Reader:
    """pi's stdout read on a thread: its first events carry all the tool schemas (~80 KB each), more than a pipe holds,
    so a test that waits for something else must keep reading or pi blocks."""

    def __init__(self, p: subprocess.Popen):
        self.lines: list[str] = []
        self.thread = threading.Thread(target=lambda: self.lines.extend(iter(p.stdout.readline, "")), daemon=True)
        self.thread.start()

    def has(self, needle: str) -> bool:
        return any(needle in line.replace(" ", "") for line in list(self.lines))

    def text(self, p: subprocess.Popen) -> str:
        self.thread.join(WAIT_S)
        p.stdout.close()
        return "".join(self.lines)


def scrub(out: str) -> str:
    """A captured stream without the bulk the runner never reads: message_update deltas, the tool schemas in
    toolsAdded (names kept, so the count stays), agent_end's copy of the whole transcript."""
    lines = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        ev = json.loads(line)
        if ev.get("type") == "message_update":
            continue
        if ev.get("type") == "agent_end":
            ev["messages"] = []
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if msg.get("toolsAdded"):
            msg["toolsAdded"] = [{"name": t["name"]} for t in msg["toolsAdded"]]
            msg.pop("sections", None)
        lines.append(json.dumps(ev, ensure_ascii=False) + "\n")
    return "".join(lines)


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


class Run:
    def __init__(self, ws: Path, out: str, err: str, rc: int | None):
        self.ws, self.out, self.err, self.rc = ws, out, err, rc
        self.events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        self.requests = [r["body"] for r in jsonl(ws / "llm.jsonl")]
        self.headers = [r["headers"] for r in jsonl(ws / "llm.jsonl")]
        self.mcp = jsonl(ws / "mcp.jsonl")

    def of(self, t: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == t]

    def messages(self, role: str) -> list[dict]:
        return [e["message"] for e in self.of("message_end") if e["message"].get("role") == role]

    def calls(self) -> list[tuple[str, str]]:
        return [(r["ev"], r["tool"]) for r in self.mcp if r["ev"] in ("start", "end")]


@unittest.skipUnless(BINARY.exists(), "tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts")
class Conformance(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="bg3-pi-conf-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        for d in ("home", "agent", "cwd"):
            (self.ws / d).mkdir()
        (self.ws / "system.txt").write_text(SYSTEM)
        self.stub: subprocess.Popen | None = None

    def tearDown(self):
        self.stop_stub()

    # ---------------------------------------------------------------------------------------------- plumbing

    def stop_stub(self):
        if self.stub:
            self.stub.kill()
            self.stub.wait()
            self.stub = None

    def start_stub(self, script: list[dict]) -> int:
        self.stop_stub()
        (self.ws / "script.json").write_text(json.dumps(script))
        port = self.ws / "port"
        port.unlink(missing_ok=True)
        self.stub = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "stub_llm.py"),
                str(self.ws / "script.json"),
                str(self.ws / "llm.jsonl"),
                str(port),
            ]
        )
        wait_until(lambda: port.exists() and port.read_text(), "the stub model's port")
        return int(port.read_text())

    def configure(self, script, model_info=None, settings=None, mcp_env=None, timeout_s=110):
        port = self.start_stub(script)
        raw = json.loads(json.dumps(PROFILE))
        raw["model_info"].update(model_info or {})
        models = PR.models_json(PR.parse(raw))
        models["providers"]["openrouter"]["baseUrl"] = f"http://127.0.0.1:{port}/v1"  # the stub, as OpenRouter
        (self.ws / "agent" / "models.json").write_text(json.dumps(models))
        (self.ws / "agent" / "settings.json").write_text(json.dumps(settings or SETTINGS))
        env = {"STUB_MCP_LOG": str(self.ws / "mcp.jsonl"), **(mcp_env or {})}
        server = {"command": sys.executable, "args": [str(HERE / "stub_mcp.py")], "env": env, "timeout": timeout_s}
        (self.ws / "bg3.json").write_text(
            json.dumps({"server": server, "systemPromptFile": str(self.ws / "system.txt")})
        )

    def command(self, prompt: str | None, mode: str = "json", session: str = SESSION) -> list[str]:
        cmd = [
            str(BINARY),
            "--mode",
            mode,
            "-ne",
            "-na",
            "-nc",
            "-ns",
            "-np",
            "--offline",
            "-e",
            str(EXTENSION),
            "--tools",
            PREFIX + "*",
            "--session-dir",
            str(self.ws / "sessions"),
            "--session-id",
            session,
            "--model",
            f"openrouter/{MODEL}",
            "--thinking",
            "high",
        ]
        return cmd + ([prompt] if prompt is not None else [])

    def env(self) -> dict:
        keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TERM")}
        return {
            **keep,
            "HOME": str(self.ws / "home"),
            "PI_CODING_AGENT_DIR": str(self.ws / "agent"),
            "BG3_PI_CONFIG": str(self.ws / "bg3.json"),
            "PI_OFFLINE": "1",
            "BG3_STUB_KEY": "stub-key",
        }

    def popen(
        self, cmd: list[str], cwd: Path | None = None, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE
    ) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            cwd=cwd or self.ws / "cwd",
            env=self.env(),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )

    def capture(self, out: str) -> None:
        d = os.environ.get("BG3AI_PI_CAPTURE")
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
            n = len(list(Path(d).glob(f"{self._testMethodName}*.jsonl")))
            (Path(d) / f"{self._testMethodName}{'-' + str(n) if n else ''}.jsonl").write_text(scrub(out))

    def run_pi(self, script=None, prompt: str = "play", cwd: Path | None = None, **cfg) -> Run:
        if script is not None:
            self.configure(script, **cfg)
        p = self.popen(self.command(prompt), cwd)
        out, err = p.communicate(timeout=WAIT_S)
        self.capture(out)
        return Run(self.ws, out, err, p.returncode)

    def assert_sequential(self, run: Run, tools: list[str]):
        expected = [x for t in tools for x in (("start", t), ("end", t))]
        self.assertEqual(run.calls(), expected)
        starts = [r["t"] for r in run.mcp if r["ev"] == "start"]
        ends = [r["t"] for r in run.mcp if r["ev"] == "end"]
        for i in range(1, len(starts)):
            self.assertGreaterEqual(starts[i], ends[i - 1], f"call {i} started before call {i - 1} ended")

    # ------------------------------------------------------------------------------------------------ checks

    def test_01_02_tools_are_the_servers_and_only_those(self):
        run = self.run_pi([{"text": "hello"}])
        self.assertEqual(run.rc, 0, run.err)
        declared = {t["function"]["name"]: t["function"]["parameters"] for t in run.requests[0]["tools"]}
        self.assertEqual(sorted(declared), sorted(PREFIX + t["name"] for t in TOOLS))
        for t in TOOLS:  # 1: the server's schemas, unchanged
            self.assertEqual(declared[PREFIX + t["name"]], t["inputSchema"], t["name"])
        system = run.messages("system")[0]  # 2: pi's own view of what it declared
        self.assertEqual(sorted(t["name"] for t in system["toolsAdded"]), sorted(declared))
        tools_entry = [
            e["entry"]["data"] for e in run.of("entry_appended") if e["entry"].get("customType") == "bg3.tools"
        ]
        self.assertEqual([d["n"] for d in tools_entry], [len(TOOLS)])

    def test_03_04_calls_run_one_at_a_time_in_source_order(self):
        names = ["press_key", "screenshot", "press_key", "wait"]
        script = [
            {"tool_calls": [call(n, {"key": str(i)} if n == "press_key" else None) for i, n in enumerate(names)]},
            {"text": "done"},
        ]
        run = self.run_pi(script)
        self.assert_sequential(run, names)
        assistant = run.requests[1]["messages"]
        ids = [c["id"] for m in assistant if m["role"] == "assistant" for c in m.get("tool_calls") or []]
        self.assertEqual([m["tool_call_id"] for m in assistant if m["role"] == "tool"], ids)  # 4
        self.assertEqual(ids, [f"call_0_{i}" for i in range(4)])

    def test_05_system_text_is_the_file_byte_for_byte(self):
        run = self.run_pi([{"text": "hello"}])
        first = run.requests[0]["messages"][0]
        self.assertIn(first["role"], ("system", "developer"))
        self.assertEqual(text_of(first["content"]), SYSTEM)

    def test_06_no_compaction_past_the_threshold(self):
        big = {"prompt_tokens": 195000, "prompt_tokens_details": {"cached_tokens": 0}}  # window 200k
        script = [
            {"tool_calls": [call("get_status")], "usage": big},
            {"tool_calls": [call("look")], "usage": big},
            {"text": "done"},
        ]
        run = self.run_pi(script)
        self.assertEqual(run.of("compaction_start"), [])
        self.assertEqual(len(run.requests), 3)
        roles = [m["role"] for m in run.requests[2]["messages"]]
        self.assertEqual(roles, ["developer", "user", "assistant", "tool", "assistant", "tool"])  # full history

    def test_07_overflow_error_ends_the_run_without_compaction(self):
        body = {
            "error": {
                "message": "This model's maximum context length is 200000 tokens.",
                "code": "context_length_exceeded",
            }
        }
        run = self.run_pi([{"error": 400, "body": body}])
        self.assertEqual(run.rc, 0)  # pi exits 0: the client must read stopReason
        self.assertEqual(len(run.requests), 1)
        self.assertEqual(run.of("compaction_start"), [])
        self.assertEqual(run.messages("assistant")[-1]["stopReason"], "error")
        self.assertEqual(run.of("agent_settled")[-1]["aborted"], False)

    def test_08_image_reaches_the_model(self):
        run = self.run_pi([{"tool_calls": [call("screenshot")]}, {"text": "a red square"}])
        msgs = run.requests[1]["messages"]
        tool = next(m for m in msgs if m["role"] == "tool")
        self.assertEqual(tool["content"], "(see attached image)")
        urls = [
            c["image_url"]["url"]
            for m in msgs
            if m["role"] == "user" and isinstance(m["content"], list)
            for c in m["content"]
            if c.get("type") == "image_url"
        ]
        self.assertEqual(urls, [f"data:image/png;base64,{stub_mcp.CANARY_B64}"])
        self.assertEqual(msgs.index(tool) + 1, len(msgs) - 1)  # one user message right after the tool results

    def test_09_image_dropped_for_a_text_only_model(self):
        run = self.run_pi([{"tool_calls": [call("screenshot")]}, {"text": "?"}], model_info={"input": ["text"]})
        self.assertNotIn(stub_mcp.CANARY_B64, json.dumps(run.requests[1]))
        self.assertNotIn("image_url", json.dumps(run.requests[1]))

    def test_10_11_clock_line_and_stop_refusal_pass_through(self):
        refusal = "REFUSED: the session is over; call end_session"
        run = self.run_pi(
            [{"tool_calls": [call("get_status"), call("travel_to")]}, {"text": "ok"}],
            mcp_env={"STUB_MCP_REFUSE": "travel_to", "STUB_MCP_REFUSAL": refusal},
        )
        tools = [text_of(m["content"]) for m in run.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(tools, ["ok get_status {}\n" + stub_mcp.CLOCK, f"{refusal}\n{stub_mcp.CLOCK}"])

    def test_12_13_bad_arguments_never_reach_the_server(self):
        script = [
            {
                "tool_calls": [
                    call("press_key", '{"key": "e"'),
                    call("press_key", {}),
                    call("press_key", {"key": 5}),
                    call("press_key", {"key": "ok"}),
                ]
            },
            {"text": "ok"},
        ]
        run = self.run_pi(script)
        # pi coerces a wrong primitive type to the schema's (5 -> "5") before validating: such a call is sent
        self.assertEqual([r["args"] for r in run.mcp if r["ev"] == "start"], [{"key": "5"}, {"key": "ok"}])
        ends = run.of("tool_execution_end")
        self.assertEqual([e["isError"] for e in ends], [True, True, False, False])
        texts = [e["result"]["content"][0]["text"] for e in ends[:2]]
        self.assertRegex(texts[0], "Invalid arguments")
        self.assertRegex(texts[1], "Validation failed")
        calls = run.messages("assistant")[0]["content"]
        self.assertIn("argumentsError", calls[0])  # the parser reports these as malformed

    def test_14_mcp_is_error_is_an_error_result(self):
        run = self.run_pi([{"tool_calls": [call("press_key", {"key": "boom"})]}, {"text": "ok"}])
        end = run.of("tool_execution_end")[0]
        self.assertTrue(end["isError"])
        self.assertIn("press_key failed: boom", end["result"]["content"][0]["text"])

    def test_15_abort_cancels_the_running_call_and_the_rest_of_the_batch(self):
        self.configure(
            [{"tool_calls": [call("wait", {"seconds": 30}), call("press_key", {"key": "never"})]}, {"text": "ok"}]
        )
        p = self.popen(self.command(None, mode="rpc"), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader = Reader(p)
        p.stdin.write(json.dumps({"id": "p1", "type": "prompt", "message": "play"}) + "\n")
        p.stdin.flush()
        wait_until(lambda: any(r["ev"] == "start" for r in jsonl(self.ws / "mcp.jsonl")), "the first call")
        p.stdin.write(json.dumps({"id": "a1", "type": "abort"}) + "\n")
        p.stdin.flush()
        wait_until(lambda: reader.has('"command":"abort"'), "pi's answer to abort")
        p.stdin.close()
        p.wait(WAIT_S)
        out = reader.text(p)
        self.capture(out)
        run = Run(self.ws, out, "", p.returncode)
        cancelled = [r for r in run.mcp if r["ev"] == "recv" and r["method"] == "notifications/cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual([r["tool"] for r in run.mcp if r["ev"] == "start"], ["wait"])  # press_key never sent
        self.assertTrue(run.of("agent_settled")[-1]["aborted"])
        self.assertTrue(run.of("tool_execution_end")[0]["isError"])
        self.assertEqual(len(run.requests), 1)

    def signal_mid_call(self, sig: int) -> Run:
        self.configure(
            [{"tool_calls": [call("wait", {"seconds": 30}), call("press_key", {"key": "x"})]}, {"text": "resumed"}]
        )
        p = self.popen(self.command("play"), stderr=subprocess.DEVNULL)
        reader = Reader(p)
        wait_until(lambda: any(r["ev"] == "start" for r in jsonl(self.ws / "mcp.jsonl")), "the first call")
        p.send_signal(sig)
        p.wait(WAIT_S)
        run = Run(self.ws, reader.text(p), "", p.returncode)
        stub_pid = next(r["pid"] for r in run.mcp if r["ev"] == "boot")
        wait_until(lambda: not pid_alive(stub_pid), "the stub MCP server to exit")
        return run

    def test_16_17_sigint_ends_pi_and_the_server_and_resume_continues(self):
        run = self.signal_mid_call(signal.SIGINT)
        self.assertNotEqual(run.rc, 0)
        self.assertEqual(run.of("agent_settled"), [])
        resumed = self.run_pi(prompt="continue")  # same session id, same agent dir
        self.assertEqual(resumed.rc, 0, resumed.err)
        msgs = resumed.requests[-1]["messages"]
        first_prompt = next(text_of(m["content"]) for m in msgs if m["role"] == "user")
        self.assertEqual(first_prompt, "play")  # the first exchange is in the request
        orphans = [text_of(m["content"]) for m in msgs if m["role"] == "tool"]
        self.assertEqual(len(orphans), 2)
        self.assertTrue(all("No result provided" in c for c in orphans), orphans)

    def test_16_sigterm_ends_pi_and_the_server(self):
        run = self.signal_mid_call(signal.SIGTERM)
        self.assertNotEqual(run.rc, 0)

    def test_18_routing_session_cache_and_effort_in_the_request(self):
        run = self.run_pi([{"tool_calls": [call("get_status")]}, {"text": "ok"}])
        body, headers = run.requests[0], {k.lower(): v for k, v in run.headers[0].items()}
        self.assertEqual(
            body["provider"], {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True}
        )
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(headers["x-session-id"], SESSION)
        self.assertEqual(headers["authorization"], "Bearer stub-key")
        self.assertEqual(body["max_completion_tokens"], 16000)
        self.assertEqual(body["messages"][0]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("cache_control", body["tools"][-1])
        last = run.requests[1]["messages"][-1]
        self.assertIn("cache_control", json.dumps(last))

    def test_19_billed_cost_and_provider_per_response(self):
        run = self.run_pi([{"tool_calls": [call("get_status")]}, {"text": "ok"}])
        billing = [
            e["entry"]["data"] for e in run.of("entry_appended") if e["entry"].get("customType") == "bg3.billing"
        ]
        self.assertEqual([b["id"] for b in billing], ["gen-stub-0", "gen-stub-1"])
        self.assertEqual({b["provider"] for b in billing}, {"StubProvider"})
        self.assertEqual([b["usage"]["cost"] for b in billing], [0.0125, 0.0125])

    def test_20_retries_then_a_final_api_error(self):
        # pi retries by the error text, not the status (pi-ai utils/retry.js): Anthropic's 529 says "Overloaded"
        run = self.run_pi([{"error": 529, "body": {"error": {"message": "Overloaded", "type": "overloaded_error"}}}])
        self.assertEqual(run.rc, 0)
        self.assertEqual(len(run.of("auto_retry_start")), 3)
        self.assertEqual(len(run.requests), 4)
        self.assertEqual([e["success"] for e in run.of("auto_retry_end")], [False])
        self.assertEqual(run.messages("assistant")[-1]["stopReason"], "error")

    def test_21_mcp_timeout_is_an_error_result_and_the_next_call_runs(self):
        run = self.run_pi(
            [{"tool_calls": [call("wait", {"seconds": 5}), call("press_key", {"key": "x"})]}, {"text": "ok"}],
            timeout_s=1,
        )
        ends = run.of("tool_execution_end")
        self.assertEqual([e["isError"] for e in ends], [True, False])
        self.assertLess(ends[0]["durationMs"], 4000)
        self.assertEqual([r["tool"] for r in run.mcp if r["ev"] == "start"], ["wait", "press_key"])

    def test_23_nothing_from_home_or_the_project(self):
        play = REPO / "play"
        before = sorted(str(p.relative_to(play)) for p in play.rglob("*"))
        run = self.run_pi([{"text": "hello"}], cwd=play)
        self.assertEqual(run.rc, 0, run.err)
        self.assertEqual(sorted(str(p.relative_to(play)) for p in play.rglob("*")), before)
        claude_md = (play / "CLAUDE.md").read_text()[:200]
        self.assertNotIn(claude_md[:80], json.dumps(run.requests))
        self.assertEqual(sorted(p.name for p in (self.ws / "home").iterdir()), [])  # no ~/.pi
        self.assertEqual(len(run.requests[0]["messages"]), 2)  # system + prompt, no context files

    def test_30_pi_client_end_to_end(self):
        """bg3harness/pi_client.py's own config files, env and command against the stubs, its parser on the stream."""
        port = self.start_stub([{"tool_calls": [call("get_status"), call("press_key", '{"key"')]}, {"text": "done"}])
        server = {
            "command": sys.executable,
            "args": [str(HERE / "stub_mcp.py")],
            "timeout": 110_000,
            "env": {"STUB_MCP_LOG": str(self.ws / "mcp.jsonl")},
        }
        client = PC.PiClient(
            PR.parse(PROFILE), self.ws / "system.txt", server=server, cwd=self.ws / "cwd", settings=SETTINGS
        )
        req = C.Request(
            prompt="play",
            model=MODEL,
            session_dir=self.ws / "sessions-root",
            session="S001-pi",
            env={"BG3AI_SESSION_DEADLINE": "123"},
            tag="S001",
        )
        client.write_config(req)
        models = client.agent_dir(req) / "models.json"
        m = json.loads(models.read_text())
        m["providers"]["openrouter"]["baseUrl"] = f"http://127.0.0.1:{port}/v1"  # the stub, as OpenRouter
        models.write_text(json.dumps(m))
        env = {**self.env(), **client.env(req)}
        p = subprocess.run(
            client.command(req),
            cwd=self.ws / "cwd",
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            timeout=WAIT_S,
        )
        self.capture(p.stdout)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [e for line in p.stdout.splitlines() for e in client.parse(line)]
        self.assertEqual(
            [e.kind for e in events],
            ["init", "tool_call", "tool_call", "usage", "tool_result", "tool_result", "text", "usage", "result"],
        )
        init = (events[0].tools, events[0].mcp, events[0].session_id)
        self.assertEqual(init, (len(TOOLS), ["bg3:connected"], "S001-pi"))
        self.assertEqual(events[2].args.keys(), {"_malformed"})
        res = events[-1]
        self.assertEqual((res.subtype, res.is_error, res.turns, res.cost_usd), ("success", False, 2, 0.025))
        self.assertEqual(client.parser.providers, ["StubProvider", "StubProvider"])
        self.assertEqual(client.parser.malformed, 1)
        sent = json.loads((self.ws / "llm.jsonl").read_text().splitlines()[0])["body"]
        self.assertEqual(text_of(sent["messages"][0]["content"]), SYSTEM)
        self.assertEqual([r["tool"] for r in jsonl(self.ws / "mcp.jsonl") if r["ev"] == "start"], ["get_status"])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
