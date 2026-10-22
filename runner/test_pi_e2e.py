#!/usr/bin/env python3
"""The pi client end to end, offline, through bg3harness.session.run_session: the real Runner.play_session (the
fake game process of test_crash for the runner's own game checks), the pinned pi with bg3.ts on the scripted stub
model (tools/pi/conformance/stub_llm.py), the real bridge (bg3ai-mcp, started by bg3.ts) answering from the fake mod
(bg3harness.testing). Covers a short scripted play with a stop-check resume of the same conversation, billing from
the stub's usage into the budget, the cost cap stopping the run, a context overflow ending the session as a recorded
failure, the tool-count check, the rules-only vs rules+coaching system text and result nudges, and a text-only
model without the screenshots group. No game, no key, no network; ~2 min.

    uv run pytest -m e2e runner/test_pi_e2e.py      (or BG3AI_E2E=1 uv run python -m unittest runner/test_pi_e2e.py)

Needs tools/pi/node_modules (cd tools/pi && npm ci --ignore-scripts); skipped without it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_crash as TCR  # noqa: N812
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
from bg3harness import events as E  # noqa: N812
from bg3harness import profiles
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.scripted import FakeClock, GameModel

MCP = importlib.util.find_spec("mcp") is not None  # the uv env (bg3harness.testing.mcp_bridge); not system python
if MCP:
    from bg3harness.testing.mcp_bridge import bridge_env

try:
    import pytest

    pytestmark = pytest.mark.e2e
except ImportError:  # plain unittest: BG3AI_E2E=1 runs it (the offline suite leaves e2e out)
    pytest = None

import legacy_playthrough as lpt  # noqa: E402
from bg3harness.runner import env as renv  # noqa: E402
from bg3harness.runner import policy as rpolicy  # noqa: E402
from bg3harness.runner import state as rstate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PI = REPO / "tools" / "pi" / "node_modules" / ".bin" / "pi"
STUB_LLM = REPO / "tools" / "pi" / "conformance" / "stub_llm.py"
MODEL = "anthropic/stub-model"
PREFIX = "mcp__bg3__"
COST = 0.0125  # stub_llm.COST: billed per response
KEY_ENV = "STUB_LLM_KEY"  # not BG3_*: the runner forwards those to the MCP server
SID = "11111111-1111-1111-1111-111111111111"
WALK_JOB = 900
WAIT_S = 60
RUN = pytest is not None or os.environ.get("BG3AI_E2E") == "1"


def call(name: str, args: dict | None = None) -> dict:
    return {"name": PREFIX + name, "arguments": args or {}}


def wait_until(cond, what: str) -> None:
    end = time.monotonic() + WAIT_S
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


class Walk:
    """A fake-mod responder for one walk that never arrives: move_to starts job 900, which stays running (a
    NOTES WINDOW in coaching), and a wait_for right after it is the PACE case."""

    def reply(self, ctx: str, op: str, args: dict) -> dict | None:
        if op == "act.move_to":
            return {"ok": True, "result": {"job": WALK_JOB, "status": "running", "eta_s": 30, "route_m": 40}}
        if op == "job.poll" and int(args.get("id") or 0) == WALK_JOB:
            return {"ok": True, "result": {"id": WALK_JOB, "kind": "act.move_to", "status": "running"}}
        return None

    def eval_raw(self, ctx: str, code: str) -> str | None:
        return None


_DUMPS: dict[tuple[str, str], tuple[str, list[str]]] = {}


def served(condition: str, tools_off: str = "") -> tuple[str, list[str]]:
    """(system text, tool names) the bg3 server serves a pi session in this condition and tool set, dumped offline
    by tools/pi/dump_server.py (pi's --system file; the init's tool count)."""
    key = (condition, tools_off)
    if key not in _DUMPS:
        d = Path(tempfile.mkdtemp(prefix="bg3ai-pi-e2e-dump-"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BG3AI_TOOLS", "BG3AI_PROMPT"))}
        env["BG3AI_PROMPT_CONDITION"] = condition
        if tools_off:
            env["BG3AI_TOOLS_OFF"] = tools_off
        subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(REPO / "bridge"),
                "python",
                str(REPO / "tools/pi/dump_server.py"),
                "--system",
                str(d / "system.txt"),
                "--tools",
                str(d / "tools.json"),
            ],
            check=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
        names = [t["name"] for t in json.loads((d / "tools.json").read_text())]
        _DUMPS[key] = ((d / "system.txt").read_text(), names)
    return _DUMPS[key]


def text_of(content) -> str:
    return content if isinstance(content, str) else "".join(c.get("text", "") for c in content)


@unittest.skipUnless(RUN, "e2e: BG3AI_E2E=1 (or pytest -m e2e)")
@unittest.skipUnless(MCP, "no mcp package: run it in the uv env (uv run ...)")
@unittest.skipUnless(PI.exists(), "tools/pi/node_modules missing: cd tools/pi && npm ci --ignore-scripts")
class PiE2EBase(TCR.FakeRunBase):
    def setUp(self):
        super().setUp()
        self.model = GameModel(clock=FakeClock())
        self.model.open_dialogue("Lae'zel", "Help me get to the helm.", ["We go together.", "Leave."])
        self.fake = FakeMod(Walk(), self.model).start()
        self.addCleanup(self.fake.stop)
        self.stub: subprocess.Popen | None = None
        self.addCleanup(self.stop_stub)
        for k in (KEY_ENV, "BG3AI_PROMPT_CONDITION"):
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ[k])
            else:
                self.addCleanup(os.environ.pop, k, None)
        os.environ[KEY_ENV] = "stub-key"
        self.budget = renv.RUNTIME / "budget.json"
        self.budget.parent.mkdir(parents=True, exist_ok=True)
        self.budget.write_text(json.dumps({"cap": 5.0, "spent": 1.0}))
        self.llm_log = self.tmp / "llm.jsonl"

    def stop_stub(self):
        if self.stub:
            self.stub.kill()
            self.stub.wait()
            self.stub = None

    def start_stub(self, script: list[dict]) -> str:
        (self.tmp / "script.json").write_text(json.dumps(script))
        port = self.tmp / "port"
        self.stub = subprocess.Popen(
            [sys.executable, str(STUB_LLM), str(self.tmp / "script.json"), str(self.llm_log), str(port)]
        )
        wait_until(lambda: port.exists() and port.read_text(), "the stub model's port")
        return f"http://127.0.0.1:{port.read_text()}/v1"

    def play(
        self,
        script: list[dict],
        condition: str = "rules",
        text_only: bool = False,
        expected: int = 0,
        stop_check=None,
        raw: dict | None = None,
        **opt,
    ) -> rstate.SessionResult:
        """One Runner.play_session of pi on the stub model; `expected` adds to the served tool count. `raw`: a
        profile table to play instead of the stub one, its endpoint's base_url pointed at the stub."""
        os.environ["BG3AI_PROMPT_CONDITION"] = condition  # play.py main sets it; the runner forwards it to the server
        url = self.start_stub(script)
        if raw is not None:
            raw = {**raw, "endpoint": {**raw["endpoint"], "base_url": url}}
        else:
            raw = {
                "client": "pi",
                "model": MODEL,
                "effort": "high",
                "endpoint": {"provider": "openrouter", "base_url": url, "api_key_env": KEY_ENV},
                "model_info": {
                    "input": ["text"] if text_only else ["text", "image"],
                    "reasoning": True,
                    "context_window": 200000,
                    "max_tokens": 16000,
                },
            }
        profile = profiles.parse(raw)
        self.system, self.tools = served(condition, ",".join(profiles.tools_off(profile)))
        system_file = self.tmp / "pi-system.txt"
        system_file.write_text(self.system)
        o = rstate.Options(
            "pi-e2e",
            None,
            20,
            profile.model,
            save_owner="Claude",
            client="pi",
            profile=profile,
            system_file=str(system_file),
            tools_expected=len(self.tools) + expected,
        )
        self.wire(replace(o, **opt), [rpolicy.Save("Claude-1__S001-start", "Claude", "S001-start", time.time() - 300)])
        r = self.r
        r.agent_cmd = lambda req: lpt.Runner.agent_cmd(r, req)  # the real pi, not test_crash's fake claude
        home = self.tmp / "bridge-home"
        client: Any = r.client  # the PiClient: its server and cwd point at the real bridge here
        client.server = {
            "command": "uv",
            "args": ["run", "--project", str(REPO / "bridge"), "bg3ai-mcp"],
            "env": bridge_env(self.fake.port, home),
            "timeout": 60_000,
        }
        client.cwd = home / "cwd"
        client.cwd.mkdir(parents=True, exist_ok=True)
        r.stop_check = stop_check or (lambda sid, deadline, minutes: None)
        r.run_t0 = time.time()  # Runner.run's start (the cost meter counts from it)
        return r.play_session(20, first=True)

    def requests(self) -> list[dict]:
        if not self.llm_log.exists():
            return []
        return [json.loads(ln)["body"] for ln in self.llm_log.read_text().splitlines()]

    def logged(self, stem: str = "S001") -> list:
        return E.load_events(self.r.state.dir / stem)

    def results(self, stem: str = "S001") -> dict[str, str]:
        return {e.tool: e.text for e in self.logged(stem) if e.kind == "tool_result"}

    def history(self) -> dict:
        return self.r.state.data["history"][-1]

    def spent(self) -> float:
        return json.loads(self.budget.read_text())["spent"]


class Play(PiE2EBase):
    def test_a_scripted_session_plays_bills_and_resumes_its_conversation(self):
        script = [
            {"tool_calls": [call("get_status")]},
            {"tool_calls": [call("choose_dialogue", {"option": 1})]},
            {"text": "Done for now."},
            {"text": "Wrapping up."},  # the stop-check resume: the same conversation, 3 assistant messages in
        ]
        checks = []

        def stop_check(sid, deadline, minutes):
            checks.append(sid)
            return "Keep playing: 10 minutes left in this session." if len(checks) == 1 else None

        res = self.play(script, stop_check=stop_check)
        self.assertEqual((res.reason, res.exit_code), ("exited", 0))
        h = self.history()
        self.assertEqual((h["reason"], h["stop_resumes"], h["resumes"]), ("exited", 1, 0))
        self.assertEqual(self.model.chosen, [1])  # the real bridge drove the fake mod
        out = self.results()
        self.assertIn("gold 100", out["get_status"])
        self.assertIn("conversation ended", out["choose_dialogue"])
        init = next(e for e in self.logged() if e.kind == "init")
        self.assertEqual((init.tools, init.mcp), (len(self.tools), ["bg3:connected"]))
        reqs = self.requests()
        self.assertEqual(len(reqs), 4)
        self.assertEqual(text_of(reqs[0]["messages"][0]["content"]), self.system)  # the dumped text, verbatim
        self.assertEqual(
            sorted(t["function"]["name"] for t in reqs[0]["tools"]), sorted(PREFIX + n for n in self.tools)
        )
        resumed = reqs[3]["messages"]  # pi's -c1 sent the whole conversation, the stop check's reason last
        self.assertIn("gold 100", json.dumps(resumed))
        self.assertIn("Keep playing", text_of(resumed[-1]["content"]))
        billed = [round(e.cost_usd, 6) for s in ("S001", "S001-c1") for e in self.logged(s) if e.kind == "result"]
        self.assertEqual(billed, [round(3 * COST, 6), COST])  # the stub's usage.cost, not pi's catalog estimate
        self.assertAlmostEqual(self.spent(), 1.0 + 4 * COST)

    def test_the_cost_cap_stops_the_run(self):
        res = self.play([{"tool_calls": [call("get_status")], "delay": 0.2}], max_cost=2.5 * COST)
        self.assertEqual(res.reason, "cap")
        cap = self.r.cap_hit
        assert cap is not None
        self.assertEqual(cap["cap"], "cost")
        self.assertGreaterEqual(cap["cost"], 2.5 * COST)
        self.assertEqual(self.history()["stop_resumes"], 0)
        billed = round((self.spent() - 1.0) / COST)  # the budget got what the stream billed
        self.assertGreaterEqual(billed, 3)
        self.assertAlmostEqual(self.spent(), 1.0 + billed * COST)
        self.assertIn(len(self.requests()) - billed, (0, 1))  # at most one request in flight at the stop

    def test_a_context_overflow_ends_the_session_as_a_recorded_failure(self):
        body = {
            "error": {
                "message": "This model's maximum context length is 200000 tokens.",
                "code": "context_length_exceeded",
            }
        }
        res = self.play([{"error": 400, "body": body}], stop_check=lambda *a: "Keep playing.")
        self.assertEqual(res.reason, "context_overflow")
        self.assertTrue(renv.quick_failure(res))  # at the kickoff: every new session would overflow the same way
        h = self.history()
        self.assertEqual((h["reason"], h["stop_resumes"], h["resumes"]), ("context_overflow", 0, 0))
        self.assertEqual(len(self.requests()), 1)  # no retry, no resume of a full conversation
        end = self.logged()[-1]
        self.assertEqual((end.kind, end.detail["reason"]), ("session_end", "context_overflow"))

    def test_a_tool_count_other_than_expected_is_mcp_failed(self):
        t0 = time.time()
        res = self.play([{"tool_calls": [call("get_status")], "delay": 0.5}], expected=1)
        self.assertEqual(res.reason, "mcp_failed")
        self.assertIn(f"gave {len(self.tools)} tools, expected {len(self.tools) + 1}", res.detail)
        self.assertLess(time.time() - t0, 60)
        self.assertIn("mcp_failed", [e["kind"] for e in self.events()])


class Conditions(PiE2EBase):
    WALK: ClassVar[list[dict]] = [
        {
            "tool_calls": [
                call("move_to", {"x": 10, "y": 0, "z": 0}),
                call("wait_for", {"target": f"j{WALK_JOB}", "timeout_s": 0.5}),
            ]
        },
        {"text": "Done."},
    ]

    def test_rules_only_gets_the_rules_text_and_no_nudges(self):
        res = self.play(self.WALK, condition="rules")
        self.assertEqual(res.reason, "exited")
        self.assertEqual(text_of(self.requests()[0]["messages"][0]["content"]), self.system)
        out = self.results()
        self.assertIn(f"move_to: started j{WALK_JOB}", out["move_to"])
        self.assertIn(f"still waiting for j{WALK_JOB}", out["wait_for"])
        self.assertNotIn("NOTES WINDOW", out["move_to"])
        self.assertNotIn("PACE:", out["wait_for"])

    def test_rules_and_coaching_gets_the_coaching_text_and_the_nudges(self):
        res = self.play(self.WALK, condition="rules+coaching")
        self.assertEqual(res.reason, "exited")
        rules_only, _ = served("rules")
        self.assertEqual(text_of(self.requests()[0]["messages"][0]["content"]), self.system)
        self.assertNotEqual(self.system, rules_only)
        coaching = "## Play like a human clicking ahead (pipelining)"  # coaching/pace.md
        self.assertIn(coaching, self.system)
        self.assertNotIn(coaching, rules_only)
        out = self.results()
        self.assertIn("NOTES WINDOW: ~30s of walking left", out["move_to"])
        self.assertIn("PACE: you waited right after issuing", out["wait_for"])


class TextOnly(PiE2EBase):
    def test_a_text_only_model_plays_without_the_screenshots_group(self):
        res = self.play([{"tool_calls": [call("get_status")]}, {"text": "Done."}], text_only=True)
        self.assertEqual(res.reason, "exited")  # the init's count is the text-only tool set's
        _, every = served("rules")
        self.assertIn("screenshot", every)
        self.assertNotIn("screenshot", self.tools)
        self.assertNotIn("zoom", self.tools)
        self.assertLess(len(self.tools), len(every))
        req = self.requests()[0]
        names = {t["function"]["name"] for t in req["tools"]}
        self.assertEqual(names, {PREFIX + n for n in self.tools})
        system = text_of(req["messages"][0]["content"])
        self.assertEqual(system, self.system)
        self.assertNotIn("screenshot(", system)
        self.assertIn("gold 100", self.results()["get_status"])


if __name__ == "__main__":
    unittest.main()
