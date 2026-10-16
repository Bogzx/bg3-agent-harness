#!/usr/bin/env python3
"""Unit tests for runner/clients.py: ClaudeClient is a drop-in for play.py's claude_cmd and format_stream_event
(compared on every line of the real runtime/sessions streams), CodexClient's command and its parsing of real codex
0.157.1 event lines (recorded offline against a local stub model server and a stub MCP server, no model call), and
GeminiClient's documented shapes (untested CLI). Never launches claude, codex, gemini or the game.

    python3 -m unittest runner/test_clients.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clients as C  # noqa: E402
import play as P  # noqa: E402
import runs as R  # noqa: E402

SESSIONS = P.REPO / "runtime" / "sessions"
NAUT = SESSIONS / "naut"
STREAM_RE = re.compile(r"^S\d{3,}(?:-[\w-]+)?\.jsonl$")   # runs.CLAUDE_STREAM_RE: play, resumes, -fix, -keeper

# codex exec --json, 0.157.1, against the stub model server (one get_status call, then a message; 1000 input /
# 800 cached / 50 output tokens per request): the first run, its `codex exec resume`, and a run with the model
# endpoint down (--disable unbounded_connection_retries).
CODEX_RUN = """\
{"type":"thread.started","thread_id":"01a1442f-b623-7451-bcbc-a50b4fce7438"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-5` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."}}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_1","type":"mcp_tool_call","server":"bg3","tool":"get_status","arguments":{"verbose":true},"result":null,"error":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","server":"bg3","tool":"get_status","arguments":{"verbose":true},"result":{"content":[{"type":"text","text":"status: ok, hp 12/12"}],"structured_content":null},"error":null,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Status read. Done."}}
{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":1600,"cache_write_input_tokens":0,"output_tokens":100,"reasoning_output_tokens":40}}
""".splitlines()
CODEX_RESUME = """\
{"type":"thread.started","thread_id":"01a1442f-b623-7451-bcbc-a50b4fce7438"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"Status read. Done."}}
{"type":"turn.completed","usage":{"input_tokens":3000,"cached_input_tokens":2400,"cache_write_input_tokens":0,"output_tokens":150,"reasoning_output_tokens":60}}
""".splitlines()
CODEX_DOWN = """\
{"type":"thread.started","thread_id":"01a1442c-e27e-7241-8dba-0e3ee1cf4c23"}
{"type":"turn.started"}
{"type":"error","message":"Connection failed: error sending request"}
{"type":"turn.failed","error":{"message":"Connection failed: error sending request"}}
""".splitlines()
# The bg3 tool refused when the server isn't pre-approved (approval_policy=never), recorded before the fix.
CODEX_REFUSED = ('{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","server":"bg3","tool":'
                 '"get_status","arguments":{"verbose":true},"result":null,"error":{"message":"MCP tool call requires '
                 'approval, but approval policy is never"},"status":"failed"}}')


def _streams(root: Path):
    return sorted(f for f in root.glob("*/S*.jsonl") if STREAM_RE.match(f.name)) if root.is_dir() else []


def _runner(**opt) -> P.Runner:
    r = P.Runner.__new__(P.Runner)
    r.opt = P.Options("pt", None, 60, "opus", **opt)
    return r


def _req(r: P.Runner, prompt: str, sid: str, resume: bool, tag: str = "") -> C.Request:
    """The Request the runner would build for play.py's claude_cmd(prompt, sid, resume, tag)."""
    model, effort = r.model_for(tag)
    return C.Request(prompt, model, Path("/tmp"), session=sid, resume=resume, effort=effort,
                     fallback_model=r.opt.fallback_model, autocompact=None if tag == "-fix" else r.opt.autocompact)


def _events(client, lines) -> list[C.Event]:
    return [e for ln in lines for e in client.parse(ln)]


class ClaudeDropIn(unittest.TestCase):
    def test_constants_match_play_and_runs(self):
        self.assertEqual(C.CLAUDE_FLAGS, P.CLAUDE_FLAGS)
        self.assertEqual(C.PLAY_TOOLS, P.PLAY_TOOLS)
        self.assertEqual(C.API_ERROR_RE.pattern, R.API_ERROR_RE.pattern)
        self.assertEqual(C.MCP_CONFIG, P.PLAY_DIR / ".mcp.json")
        self.assertEqual(C.TOKEN_KEYS, tuple(k for k, _ in R.TOKEN_FIELDS))

    def test_same_command_as_play(self):
        cases = [{}, {"effort": "high"}, {"fallback_model": "sonnet"}, {"autocompact": 350_000},
                 {"effort": "max", "fallback_model": "sonnet", "autocompact": 200_000, "fix_model": "haiku"}]
        client = C.ClaudeClient()
        for opt in cases:
            r = _runner(**opt)
            for prompt, sid, resume, tag in (("go", "1234", False, ""), ("keep playing", "1234", True, ""),
                                             ("fix", "5678", False, "-fix"), ("fix", "5678", True, "-fix")):
                with self.subTest(opt=opt, resume=resume, tag=tag):
                    self.assertEqual(client.command(_req(r, prompt, sid, resume, tag)),
                                     r.claude_cmd(prompt, sid, resume, tag))

    def test_env_and_config(self):
        c, req = C.ClaudeClient(), C.Request("go", "opus", Path("/tmp"), session="1")
        self.assertEqual(c.write_config(req), [])
        self.assertEqual(c.env(req), {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        self.assertTrue(c.supports_resume and c.assigns_session)
        env = C.process_env(c, C.Request("go", "opus", Path("/tmp"), env={"BG3AI_SESSION": "S1"}), base={"PATH": "/x"})
        self.assertEqual(env, {"PATH": "/x", "BG3AI_SESSION": "S1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    @unittest.skipUnless(_streams(SESSIONS), "no runtime/sessions streams on this machine")
    def test_same_log_lines_as_play_on_real_streams(self):
        # Every line of every claude stream (naut and the others): the client's events, formatted, are exactly
        # play.format_stream_event's lines.
        client, n = C.ClaudeClient(), 0
        for f in _streams(SESSIONS):
            for i, ln in enumerate(f.read_text(errors="replace").splitlines()):
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                got = [s for e in client.parse(ln) for s in C.format_event(e)]
                self.assertEqual(got, P.format_stream_event(ev), f"{f.name}:{i + 1}")
                n += 1
        self.assertGreater(n, 100)

    @unittest.skipUnless((NAUT / "S001.jsonl").exists(), "no naut streams")
    def test_naut_kinds_tokens_and_cost(self):
        client = C.ClaudeClient()
        lines = (NAUT / "S001.jsonl").read_text().splitlines()
        evs = _events(client, lines)
        kinds = {e.kind for e in evs}
        self.assertTrue({"init", "text", "tool_call", "tool_result", "usage", "rate_limit", "result"} <= kinds)
        init = next(e for e in evs if e.kind == "init")
        self.assertEqual(init.session_id, "b0e3fc50-fd17-425e-8123-0aff3b5ba89d")
        self.assertIn("bg3:connected", init.mcp)
        self.assertFalse(any(e.tool.startswith("mcp__") for e in evs if e.kind == "tool_call"))
        # every result: the same cost, turns and tokens runs.py reads from the raw line
        raw = [json.loads(ln) for ln in lines if '"type":"result"' in ln.replace(" ", "")]
        res = [e for e in evs if e.kind == "result"]
        self.assertEqual(len(res), len(raw))
        for e, ev in zip(res, raw):
            self.assertEqual(e.tokens, R.usage_tokens(ev.get("usage")))
            self.assertEqual(e.cost_usd, float(ev.get("total_cost_usd") or 0))
            self.assertEqual(e.turns, ev.get("num_turns"))
            self.assertEqual(e.session_id, init.session_id)
        # the first result of S001 is the interrupted one (error_during_execution, no API error)
        self.assertEqual((res[0].subtype, res[0].is_error, res[0].api_error), ("error_during_execution", True, False))
        # tool results pair with their calls
        calls = {e.call_id for e in evs if e.kind == "tool_call"}
        self.assertTrue(all(e.call_id in calls for e in evs if e.kind == "tool_result"))

    def test_api_error_shapes(self):
        c = C.ClaudeClient()
        text = c.parse(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "API Error: 529 overloaded"}]}}))
        self.assertEqual([(e.kind, e.api_error) for e in text], [("text", True)])
        limit = c.parse(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                    "result": "Claude AI usage limit reached|1760000000"}))[0]
        self.assertTrue(limit.is_error and limit.api_error)
        self.assertEqual(limit.text, "Claude AI usage limit reached|1760000000")
        ok = c.parse(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "rate limit"}))[0]
        self.assertFalse(ok.api_error)   # only failed results count
        rl = c.parse(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}))[0]
        self.assertEqual((rl.kind, rl.api_error), ("rate_limit", True))
        self.assertEqual(c.parse("not json"), [])
        self.assertEqual(c.parse("[1, 2]"), [])

    def test_usage_per_message(self):
        # assistant messages carry running usage (runs.UsageMeter's input), also when the message is only thinking
        line = json.dumps({"type": "assistant", "message": {"id": "msg_1", "content": [{"type": "thinking"}],
                           "usage": {"input_tokens": 2, "output_tokens": 28, "cache_read_input_tokens": 5,
                                     "cache_creation_input_tokens": 100}}})
        (u,) = C.ClaudeClient().parse(line)
        self.assertEqual((u.kind, u.message_id), ("usage", "msg_1"))
        self.assertEqual(u.tokens, {"input": 2, "output": 28, "cache_read": 5, "cache_write": 100})
        self.assertEqual(C.format_event(u), [])


class Codex(unittest.TestCase):
    def setUp(self):
        self.req = C.Request("play", "gpt-5", Path("/tmp"), effort="max",
                             env={"BG3AI_SESSION": "S012", "BG3AI_SESSION_DEADLINE": "1792150000", "HOME": "/h",
                                  "BG3_SESSION_MINUTES": "60"})

    def config(self, cmd: list[str]) -> dict:
        """The -c overrides, each value parsed as TOML (as codex does)."""
        out = {}
        for i, a in enumerate(cmd):
            if a == "-c":
                k, v = cmd[i + 1].split("=", 1)
                out[k] = tomllib.loads(f"v = {v}")["v"]
        return out

    def test_command(self):
        cmd = C.CodexClient().command(self.req)
        self.assertEqual(cmd[:3], ["codex", "exec", "--json"])
        self.assertEqual(cmd[-1], "play")
        self.assertIn("--ignore-user-config", cmd)    # ~/.codex/config.toml is never read or written
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5")
        disabled = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec", "multi_agent", "unbounded_connection_retries"} <= disabled)
        cfg = self.config(cmd)
        server = C.bg3_server()
        self.assertEqual(cfg["mcp_servers.bg3.command"], server["command"])
        self.assertEqual(cfg["mcp_servers.bg3.args"], server["args"])
        self.assertEqual(cfg["mcp_servers.bg3.cwd"], str(P.PLAY_DIR))   # .mcp.json's args are relative to play/
        self.assertEqual(cfg["mcp_servers.bg3.env"]["BG3AI_SERVE_RULES"], "1")   # the server sends the rules
        self.assertEqual(cfg["mcp_servers.bg3.env_vars"],
                         ["BG3AI_SESSION", "BG3AI_SESSION_DEADLINE", "BG3_SESSION_MINUTES"])
        self.assertEqual(cfg["mcp_servers.bg3.tool_timeout_sec"], server["timeout"] / 1000)
        self.assertEqual(cfg["mcp_servers.bg3.default_tools_approval_mode"], "approve")
        self.assertEqual((cfg["approval_policy"], cfg["sandbox_mode"], cfg["web_search"]),
                         ("never", "read-only", "disabled"))
        self.assertEqual(cfg["project_doc_max_bytes"], 0)
        self.assertEqual(cfg["model_reasoning_effort"], "xhigh")   # codex has no "max"

    def test_resume_command(self):
        c = C.CodexClient(extra_config={"model_provider": "stub"})
        cmd = c.command(C.Request("keep playing", "gpt-5", Path("/tmp"), session="01a1-thread", resume=True))
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "--json"])
        self.assertEqual(cmd[-2:], ["01a1-thread", "keep playing"])
        self.assertNotIn("-C", cmd)          # `exec resume` has no -C/--sandbox: cwd and -c only
        self.assertNotIn("--sandbox", cmd)
        self.assertEqual(self.config(cmd)["model_provider"], "stub")
        with self.assertRaises(ValueError):
            c.command(C.Request("keep playing", "gpt-5", Path("/tmp"), resume=True))
        self.assertFalse(c.assigns_session)

    def test_toml_values(self):
        self.assertEqual(C.toml_value({"A": "x\"y", "B": ["1", 2, True]}), '{"A" = "x\\"y", "B" = ["1", 2, true]}')
        self.assertEqual(tomllib.loads("v = " + C.toml_value("a\nb\\c"))["v"], "a\nb\\c")
        with self.assertRaises(TypeError):
            C.toml_value(object())

    def test_parse_run(self):
        c = C.CodexClient()
        c.command(self.req)
        evs = _events(c, CODEX_RUN)
        self.assertEqual([e.kind for e in evs], ["init", "error", "tool_call", "tool_result", "text", "result"])
        init, warn, call, res, text, end = evs
        self.assertEqual((init.session_id, init.model), ("01a1442f-b623-7451-bcbc-a50b4fce7438", "gpt-5"))
        self.assertFalse(warn.api_error)   # an error *item* is a warning, not a failed turn
        self.assertEqual((call.tool, call.args, call.call_id), ("get_status", {"verbose": True}, "item_1"))
        self.assertEqual((res.text, res.is_error, res.call_id), ("status: ok, hp 12/12", False, "item_1"))
        self.assertEqual(text.text, "Status read. Done.")
        self.assertEqual(end.tokens, {"input": 400, "output": 100, "cache_read": 1600, "cache_write": 0})
        self.assertAlmostEqual(end.cost_usd, (400 * 1.25 + 1600 * 0.125 + 100 * 10.0) / 1e6)
        self.assertEqual((end.is_error, end.session_id), (False, init.session_id))
        self.assertEqual([s for e in evs for s in C.format_event(e)][2:5], [
            '[tool] get_status {"verbose":true}', "[result] status: ok, hp 12/12", "[say] Status read. Done."])

    def test_resume_usage_is_per_process(self):
        # turn.completed counts the whole thread: the resume reports 3000 input, of which 1000 are its own
        c = C.CodexClient()
        c.command(self.req)
        _events(c, CODEX_RUN)
        end = _events(c, CODEX_RESUME)[-1]
        self.assertEqual(end.tokens, {"input": 200, "output": 50, "cache_read": 800, "cache_write": 0})
        fresh = C.CodexClient()   # a restarted runner: seeded from the earlier stream's last usage
        fresh.seed_usage("01a1442f-b623-7451-bcbc-a50b4fce7438", json.loads(CODEX_RUN[-1])["usage"])
        self.assertEqual(_events(fresh, CODEX_RESUME)[-1].tokens, end.tokens)

    def test_failures(self):
        c = C.CodexClient()
        evs = _events(c, CODEX_DOWN)
        self.assertEqual([(e.kind, e.api_error) for e in evs], [("init", False), ("error", True), ("result", True)])
        self.assertTrue(evs[-1].is_error)
        self.assertEqual(evs[-1].text, "Connection failed: error sending request")
        (refused,) = c.parse(CODEX_REFUSED)
        self.assertEqual((refused.kind, refused.is_error), ("tool_result", True))
        self.assertIn("requires approval", refused.text)

    def test_cost_unknown_model(self):
        c = C.CodexClient()
        c.command(C.Request("play", "gpt-6-astra", Path("/tmp")))
        end = _events(c, CODEX_RUN)[-1]
        self.assertIsNone(end.cost_usd)
        self.assertEqual(end.tokens["input"], 400)
        self.assertTrue(C.format_event(end)[0].endswith("cost=$?"))

    def test_other_items(self):
        c = C.CodexClient()
        cmd = c.parse('{"type":"item.started","item":{"id":"i","type":"command_execution","command":"ls"}}')
        out = c.parse('{"type":"item.completed","item":{"id":"i","type":"command_execution","command":"ls",'
                      '"aggregated_output":"x","exit_code":1}}')
        self.assertEqual((cmd[0].tool, out[0].is_error), ("shell", True))
        other = c.parse('{"type":"item.started","item":{"id":"j","type":"mcp_tool_call","server":"web","tool":"get",'
                        '"arguments":{}}}')
        self.assertEqual(other[0].tool, "web.get")
        self.assertEqual(c.parse('{"type":"item.completed","item":{"type":"reasoning","text":"hm"}}'), [])

    @unittest.skipUnless(shutil.which("codex"), "codex not installed")
    def test_disabled_features_exist(self):
        # a feature name the installed codex doesn't know would make every run fail
        out = subprocess.run(["codex", "features", "list"], capture_output=True, text=True, timeout=60, check=False).stdout
        known = {ln.split()[0] for ln in out.splitlines() if ln.strip()}
        self.assertEqual(set(C.CODEX_DISABLE) - known, set())


class Gemini(unittest.TestCase):
    def test_config_env_and_command(self):
        with tempfile.TemporaryDirectory() as d:
            c = C.GeminiClient()
            req = C.Request("play", "gemini-2.5-pro", Path(d), tag="S012",
                            env={"BG3AI_SESSION": "S012", "BG3AI_SESSION_DEADLINE": "1792150000", "HOME": "/h"})
            (path,) = c.write_config(req)
            self.assertEqual(path, Path(d) / "S012.gemini-settings.json")
            s = json.loads(path.read_text())
            bg3 = s["mcpServers"]["bg3"]
            self.assertEqual(bg3["command"], C.bg3_server()["command"])
            self.assertEqual(bg3["env"], {"BG3AI_SERVE_RULES": "1", "BG3AI_SESSION": "S012",
                                          "BG3AI_SESSION_DEADLINE": "1792150000"})
            self.assertIn("run_shell_command", s["tools"]["exclude"])
            self.assertEqual(c.env(req), {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(path)})
        cmd = c.command(req)
        self.assertEqual(cmd[:3], ["gemini", "-p", "play"])
        self.assertIn("stream-json", cmd)
        self.assertNotIn("--resume", cmd)
        resumed = c.command(C.Request("go on", "gemini-2.5-pro", Path("/tmp"), session="abc", resume=True))
        self.assertEqual(resumed[-2:], ["--resume", "abc"])

    def test_parse_documented_events(self):
        c = C.GeminiClient()
        c.command(C.Request("play", "gemini-2.5-pro", Path("/tmp")))
        lines = [
            {"type": "init", "session_id": "g1", "model": "gemini-2.5-pro"},
            {"type": "message", "role": "user", "content": "play"},
            {"type": "message", "role": "assistant", "content": "Checking.", "delta": True},
            {"type": "tool_use", "tool_name": "get_status", "tool_id": "t1", "parameters": {}},
            {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "status: ok"},
            {"type": "tool_result", "tool_id": "t2", "status": "error", "error": {"message": "boom"}},
            {"type": "result", "status": "success",
             "stats": {"input_tokens": 1000, "output_tokens": 10, "cached": 600, "duration_ms": 1200}},
        ]
        evs = _events(c, [json.dumps(x) for x in lines])
        self.assertEqual([e.kind for e in evs], ["init", "text", "tool_call", "tool_result", "tool_result", "result"])
        self.assertEqual(evs[0].session_id, "g1")
        self.assertEqual((evs[4].text, evs[4].is_error), ("boom", True))
        self.assertEqual(evs[-1].tokens, {"input": 400, "output": 10, "cache_read": 600, "cache_write": 0})
        self.assertAlmostEqual(evs[-1].cost_usd, (400 * 1.25 + 600 * 0.31 + 10 * 10.0) / 1e6)


class Registry(unittest.TestCase):
    def test_make(self):
        self.assertIsInstance(C.make("claude"), C.ClaudeClient)
        self.assertIsInstance(C.make("codex", server={"command": "x"}), C.CodexClient)
        with self.assertRaises(ValueError):
            C.make("gpt")


if __name__ == "__main__":
    unittest.main()
