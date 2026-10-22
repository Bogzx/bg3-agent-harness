#!/usr/bin/env python3
"""Unit tests for runner/pi_client.py: PiParser on pi 1.3.0 streams captured from the conformance suite
(runner/fixtures/pi/*.jsonl, made with BG3AI_PI_CAPTURE; the runner log lines each gives are golden files,
<name>.log, regenerated with BG3AI_UPDATE_GOLDEN=1), the guards on synthetic lines, PiClient's config, env and
command, and the runner's tool-count check (check 22) through play.mcp_problem. Never runs pi.

    python3 -m unittest runner/test_pi_client.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import clients as C
import pi_client as PC

import play as P
import profiles as PR

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"
STREAMS = ("sequential", "overflow", "malformed", "tool_error", "abort", "resume", "billing", "retries", "image")
PROFILE = PR.parse(
    {
        "client": "pi",
        "model": "vendor/model-1",
        "effort": "medium",
        "endpoint": {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "routing": {"only": ["vendor"]},
        "model_info": {"input": ["text", "image"], "reasoning": True, "context_window": 200000, "max_tokens": 16000},
    }
)


def lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text().splitlines()


def parse(stream: list[str]) -> tuple[PC.PiParser, list[C.Event]]:
    p = PC.PiParser("anthropic/stub-model")
    return p, [e for line in stream for e in p.parse(line)]


def result(events: list[C.Event]) -> C.Event:
    res = [e for e in events if e.kind == "result"]
    assert len(res) == 1, res
    return res[0]


def ev(t: str, **kw) -> str:
    return json.dumps({"type": t, **kw})


def assistant(stop="stop", error=None, cost=0.001, content=None) -> str:
    msg = {
        "role": "assistant",
        "content": content or [],
        "model": "m",
        "stopReason": stop,
        "timestamp": 1000,
        "usage": {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 0, "cost": {"total": cost}},
    }
    if error:
        msg["errorMessage"] = error
    return ev("message_end", message=msg)


TOOLS = ev("entry_appended", entry={"customType": "bg3.tools", "data": {"n": 94}})
SYSTEM = ev("message_end", message={"role": "system", "toolsAdded": [{"name": f"t{i}"} for i in range(94)]})


class Streams(unittest.TestCase):
    def test_golden_log_lines(self):
        for name in STREAMS:
            got = "".join(line + "\n" for e in parse(lines(name))[1] for line in C.format_event(e))
            golden = FIXTURES / f"{name}.log"
            if os.environ.get("BG3AI_UPDATE_GOLDEN"):
                golden.write_text(got)
            self.assertEqual(got, golden.read_text(), f"{golden.name}: regenerate with BG3AI_UPDATE_GOLDEN=1")

    def test_sequential(self):
        p, events = parse(lines("sequential"))
        init = events[0]
        self.assertEqual(
            (init.kind, init.session_id, init.tools, init.mcp), ("init", "conf-0001", 94, ["bg3:connected"])
        )
        self.assertEqual(
            [e.tool for e in events if e.kind == "tool_call"], ["press_key", "screenshot", "press_key", "wait"]
        )
        calls = [e.call_id for e in events if e.kind == "tool_call"]
        self.assertEqual([e.call_id for e in events if e.kind == "tool_result"], calls)
        res = result(events)
        self.assertEqual((res.subtype, res.is_error, res.api_error, res.turns), ("success", False, False, 2))
        # two responses of 1000 prompt tokens (800 cached) + 50 output: input excludes cache reads
        self.assertEqual(res.tokens, {"input": 400, "output": 100, "cache_read": 1600, "cache_write": 0})
        self.assertEqual(res.cost_usd, 0.025)  # billed (2 x 0.0125), not pi's catalog estimate
        self.assertEqual(p.generations, ["gen-stub-0", "gen-stub-1"])

    def test_exit_0_after_an_api_error_is_a_failure(self):
        res = result(parse(lines("retries"))[1])
        self.assertEqual((res.subtype, res.is_error, res.api_error), ("error_during_execution", True, True))
        self.assertIn("Overloaded", res.text)

    def test_overflow_is_an_error_but_not_an_api_error(self):
        _, events = parse(lines("overflow"))
        res = result(events)
        self.assertEqual((res.subtype, res.is_error, res.api_error), ("error_context_overflow", True, False))
        self.assertEqual([e.api_error for e in events if e.kind == "error"], [False])

    def test_malformed_arguments_are_counted(self):
        p, events = parse(lines("malformed"))
        self.assertEqual(p.malformed, 1)
        self.assertEqual(list(events[1].args), ["_malformed"])
        self.assertEqual([e.is_error for e in events if e.kind == "tool_result"], [True, True, False, False])

    def test_tool_error(self):
        _, events = parse(lines("tool_error"))
        err = next(e for e in events if e.kind == "tool_result")
        self.assertTrue(err.is_error)
        self.assertIn("boom", err.text)

    def test_abort(self):
        res = result(parse(lines("abort"))[1])
        self.assertEqual((res.subtype, res.is_error, res.api_error), ("aborted", True, False))

    def test_resume_has_no_system_message_and_keeps_its_tools(self):
        _, events = parse(lines("resume"))
        self.assertEqual((events[0].tools, events[0].mcp), (94, ["bg3:connected"]))
        self.assertEqual(result(events).subtype, "success")

    def test_image_result(self):
        _, events = parse(lines("image"))
        self.assertEqual(next(e for e in events if e.kind == "tool_result").text, "<image>")


class Guards(unittest.TestCase):
    def test_tool_count_check(self):
        """Check 22: the model got fewer tools than the server listed -> the runner's mcp_failed."""
        short = ev("message_end", message={"role": "system", "toolsAdded": [{"name": "t"}] * 93})
        init = parse([TOOLS, short])[1][0]
        self.assertEqual(init.mcp, ["bg3:tools 93/94"])
        self.assertEqual(P.mcp_problem(init.mcp, False), "the bg3 MCP server is tools 93/94")
        self.assertIsNone(P.mcp_problem(parse([TOOLS, SYSTEM])[1][0].mcp, False))
        empty = ev("entry_appended", entry={"customType": "bg3.tools", "data": {"n": 0}})
        self.assertEqual(parse([empty, SYSTEM])[1][0].mcp, ["bg3:tools 94/0"])

    def test_no_tools_entry_is_a_failed_server(self):
        _, events = parse([SYSTEM, assistant()])
        self.assertEqual(events[0].mcp, ["bg3:failed"])
        self.assertIsNotNone(P.mcp_problem(events[0].mcp, False))

    def test_init_waits_for_both_and_comes_once(self):
        p = PC.PiParser("m")
        self.assertEqual(p.parse(SYSTEM), [])  # the server's count not known yet
        self.assertEqual([e.kind for e in p.parse(TOOLS)], [])
        self.assertEqual([e.kind for e in p.parse(assistant())], ["init", "usage"])
        self.assertEqual([e.kind for e in p.parse(assistant())], ["usage"])

    def test_overflow_loop_is_cut(self):
        compact = ev("compaction_start")
        _, events = parse([TOOLS, SYSTEM] + [compact] * (PC.MAX_RECOVERY + 3) + [ev("agent_settled", aborted=False)])
        self.assertEqual([e.kind for e in events].count("compact"), PC.MAX_RECOVERY + 3)
        res = result(events)  # one result: the loop's, not a second one at agent_settled
        self.assertEqual((res.subtype, res.is_error, res.api_error), ("error_overflow_loop", True, False))
        overflow = assistant("error", "400: maximum context length is 200000 tokens")
        _, events = parse([TOOLS, SYSTEM] + [overflow] * (PC.MAX_RECOVERY + 1))
        self.assertEqual(result(events).subtype, "error_overflow_loop")

    def test_api_error_classes(self):
        for msg, api in (
            ("429: rate limit exceeded", True),
            ("401: invalid api key", True),
            ("503: service unavailable", True),
            ("400: tool_choice is not supported", False),
            ("context_length_exceeded: prompt is too long", False),
        ):
            res = result(parse([TOOLS, SYSTEM, assistant("error", msg), ev("agent_settled", aborted=False)])[1])
            self.assertEqual((res.is_error, res.api_error), (True, api), msg)

    def test_cost_falls_back_to_the_estimate(self):
        res = result(
            parse([TOOLS, SYSTEM, assistant(cost=0.25), assistant(cost=0.5), ev("agent_settled", aborted=False)])[1]
        )
        self.assertEqual(res.cost_usd, 0.75)
        billing = ev("entry_appended", entry={"customType": "bg3.billing", "data": {"usage": {"cost": 0.1}}})
        res = result(parse([TOOLS, SYSTEM, billing, assistant(cost=0.25), ev("agent_settled", aborted=False)])[1])
        self.assertEqual(res.cost_usd, 0.1)

    def test_deepseek_cost_from_its_usage_fields(self):
        # run 1 (research/ovh/live3.md) was off-peak: pi's catalog estimate said 2x DeepSeek's bill
        def billing(ts, **usage):
            data = {
                "usage": {
                    "prompt_cache_hit_tokens": 1_000_000,
                    "prompt_cache_miss_tokens": 100_000,
                    "completion_tokens": 10_000,
                    **usage,
                }
            }
            return ev("entry_appended", entry={"customType": "bg3.billing", "data": data, "timestamp": ts})

        stream = [
            TOOLS,
            SYSTEM,
            billing("2026-10-21T21:16:07.030Z"),
            billing("2026-10-22T02:00:00Z"),
            assistant(cost=0.25),
            ev("agent_settled", aborted=False),
        ]
        rates = {"input": 0.3, "output": 1.2, "cache_read": 0.006}
        p = PC.PiParser("deepseek-flash", rates)
        res = result([e for line in stream for e in p.parse(line)])
        peak = 0.006 + 0.03 + 0.012
        self.assertAlmostEqual(res.cost_usd, peak / 2 + peak)  # off-peak (05:16 Beijing) + peak (10:00)
        # a billed usage.cost (OpenRouter) still wins; no DeepSeek rates: pi's estimate
        p = PC.PiParser("deepseek-flash", rates)
        res = result(
            [
                e
                for line in [TOOLS, SYSTEM, billing("2026-10-22T02:00:00Z", cost=0.1), *stream[4:]]
                for e in p.parse(line)
            ]
        )
        self.assertEqual(res.cost_usd, 0.1)
        self.assertEqual(result(parse(stream)[1]).cost_usd, 0.25)

    def test_garbage_lines(self):
        p = PC.PiParser()
        for line in ("", "not json", "[1]", "null", ev("message_update")):
            self.assertEqual(p.parse(line), [])

    def test_exit_problem(self):
        err = (
            'Error: Failed to load extension "/x/tools/pi/bg3.ts": Failed to load extension: spawn uv ENOENT\n'
            'Hint: Start without extensions using "pi -ne".\n'
        )
        self.assertIn("spawn uv ENOENT", PC.exit_problem(1, err))
        self.assertIsNone(PC.exit_problem(0, err))
        self.assertIsNone(PC.exit_problem(1, "Error: something else"))


class Client(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3-pi-client-"))
        self.server = {
            "command": "uv",
            "args": ["run", "bg3ai-mcp"],
            "env": {"BG3AI_SERVE_RULES": "1"},
            "timeout": 110_000,
        }
        self.client = PC.PiClient(
            PROFILE, self.tmp / "system.txt", server=self.server, cwd=self.tmp / "play", binary="/opt/pi"
        )
        self.req = C.Request(
            prompt="go",
            model="vendor/model-1",
            session_dir=self.tmp,
            session="S007-ab",
            env={"BG3AI_SESSION_DEADLINE": "99", "OTHER": "x"},
            tag="S007",
        )

    def test_registered(self):
        c = C.make("pi", profile=PROFILE, system_file=self.tmp / "s.txt", server=self.server)
        self.assertIsInstance(c, PC.PiClient)
        self.assertEqual((c.name, c.supports_resume, c.assigns_session), ("pi", True, True))

    def test_command(self):
        self.assertEqual(
            self.client.command(self.req),
            [
                "/opt/pi",
                "--mode",
                "json",
                "-ne",
                "-na",
                "-nc",
                "-ns",
                "-np",
                "--offline",
                "-e",
                str(PC.EXTENSION),
                "--tools",
                "mcp__bg3__*",
                "--session-dir",
                str(self.tmp / "pi-sessions"),
                "--session-id",
                "S007-ab",
                "--model",
                "openrouter/vendor/model-1",
                "--thinking",
                "medium",
                "go",
            ],
        )
        self.req.effort = "max"
        self.assertEqual(self.client.command(self.req)[-3:], ["--thinking", "max", "go"])

    def test_a_resume_finds_the_conversation_of_the_first_process(self):
        # S001-c1 (a stop-check resume) has its own agent dir; with its own session dir pi started a new conversation
        first = self.client.command(self.req)
        resume = self.client.command(dataclasses.replace(self.req, tag="S007-c1", resume=True))
        at = first.index("--session-dir") + 1
        self.assertEqual(resume[at], first[at])
        self.assertNotEqual(self.client.agent_dir(dataclasses.replace(self.req, tag="S007-c1")),
                            self.client.agent_dir(self.req))

    def test_command_guards(self):
        self.req.session = None
        with self.assertRaisesRegex(ValueError, "session id up front"):
            self.client.command(self.req)
        self.req.session, self.req.model = "S", "other/model"
        with self.assertRaisesRegex(ValueError, "not the profile's"):
            self.client.command(self.req)
        with self.assertRaisesRegex(PR.ProfileError, "needs a pi profile"):
            PC.PiClient(PR.parse({"client": "codex", "model": "m"}), self.tmp / "s.txt", server=self.server)

    def test_deepseek_rates_only_for_deepseeks_api(self):
        self.assertIsNone(self.client.new_parser().deepseek_prices)  # OpenRouter bills usage.cost itself
        prof = PR.load(C.REPO / "profiles" / "deepseek-flash.toml")
        c = PC.PiClient(prof, self.tmp / "s.txt", server=self.server)
        self.assertEqual(c.parser.deepseek_prices, {"input": 0.3, "output": 1.2, "cache_read": 0.006})

    def test_config_files(self):
        paths = self.client.write_config(self.req)
        d = self.tmp / "S007.pi"
        self.assertEqual(paths, [d / "settings.json", d / "models.json", d / "bg3.json"])
        self.assertEqual(json.loads(paths[0].read_text()), PC.PI_SETTINGS)
        self.assertEqual(json.loads(paths[1].read_text()), PR.models_json(PROFILE))
        self.assertEqual(
            json.loads(paths[2].read_text()),
            {
                "server": {
                    "command": "uv",
                    "args": ["run", "bg3ai-mcp"],
                    "cwd": str(self.tmp / "play"),
                    "env": {"BG3AI_SERVE_RULES": "0", "BG3AI_SESSION_DEADLINE": "99"},
                    "timeout": 110.0,
                },
                "systemPromptFile": str(self.tmp / "system.txt"),
            },
        )
        self.assertNotIn("OTHER", paths[2].read_text())

    def test_a_text_only_model_gets_a_server_without_screenshots(self):
        raw = PR.parse(
            {
                "client": "pi",
                "model": "vendor/model-1",
                "endpoint": {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
                "model_info": {"input": ["text"], "reasoning": False, "context_window": 9000, "max_tokens": 1000},
            }
        )
        c = PC.PiClient(raw, self.tmp / "s.txt", server=self.server, cwd=self.tmp / "play")
        req = C.Request(
            prompt="go",
            model="vendor/model-1",
            session_dir=self.tmp,
            session="S007-ab",
            env={"BG3AI_TOOLS_OFF": "wiki"},
            tag="S007",
        )
        env = c.bg3_config(req)["server"]["env"]
        self.assertEqual(env["BG3AI_TOOLS_OFF"], "wiki,screenshots")

    def test_config_http(self):
        c = PC.PiClient(PROFILE, self.tmp / "s.txt", server=C.http_server("http://h:1/mcp", 60_000))
        self.assertEqual(c.bg3_config(self.req)["server"], {"url": "http://h:1/mcp", "timeout": 60.0})

    def test_env(self):
        d = self.tmp / "S007.pi"
        self.assertEqual(
            self.client.env(self.req),
            {"PI_CODING_AGENT_DIR": str(d), "BG3_PI_CONFIG": str(d / "bg3.json"), "PI_OFFLINE": "1"},
        )

    def test_settings_keep_compaction_off(self):
        self.assertEqual(PC.PI_SETTINGS["compaction"], {"enabled": False})
        self.assertEqual(PC.PI_SETTINGS["cacheWarming"], "off")


if __name__ == "__main__":
    unittest.main()
