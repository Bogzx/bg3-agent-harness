"""Unit tests for runner/events.py and the clients.Event envelope: to_json/from_json round trip, the schema file
naming every field, EventLog's seq/t, load_events on an events log and on old raw streams (parity on the recorded
client fixtures of test_clients.py), and run_claude writing Sxxx.events.jsonl (the fake claude of test_crash.py).

    python3 -m unittest runner/test_events.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
import clients as C
import events as E
import test_clients as TC  # (the recorded codex lines)
import test_crash as TCR  # (the fake claude and fake game)

SCHEMA = Path(__file__).resolve().parents[1] / "packages" / "harness" / "schemas" / "events.v1.schema.json"

# claude stream-json shapes (as in runtime/sessions streams): init, a thinking-only line with its time, a call with
# its result (the bridge's {"result": ...} envelope), a screenshot result, text, the final result.
CLAUDE = [
    json.dumps(x)
    for x in (
        {
            "type": "system",
            "subtype": "init",
            "session_id": "c1",
            "model": "claude-opus-5-5",
            "tools": ["a", "b"],
            "mcp_servers": [{"name": "bg3", "status": "connected"}],
        },
        {
            "type": "assistant",
            "thinking_duration_ms": 624,
            "session_id": "c1",
            "message": {
                "id": "msg_1",
                "content": [{"type": "thinking", "thinking": ""}],
                "usage": {"input_tokens": 2, "output_tokens": 28},
            },
        },
        {
            "type": "assistant",
            "session_id": "c1",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "text", "text": "Looking first."},
                    {"type": "tool_use", "id": "toolu_1", "name": "mcp__bg3__look", "input": {"radius": 10}},
                ],
            },
        },
        {
            "type": "user",
            "session_id": "c1",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"result": "a door e1"}'}]
            },
        },
        {
            "type": "assistant",
            "session_id": "c1",
            "message": {
                "id": "msg_2",
                "content": [{"type": "tool_use", "id": "toolu_2", "name": "mcp__bg3__screenshot", "input": {}}],
            },
        },
        {
            "type": "user",
            "session_id": "c1",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "is_error": True,
                        "content": [{"type": "text", "text": "FAILED: no window"}, {"type": "image", "source": {}}],
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "c1",
            "result": "done",
            "num_turns": 2,
            "duration_ms": 5000,
            "duration_api_ms": 4000,
            "total_cost_usd": 0.5,
            "usage": {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"thinking_tokens": 3}},
        },
    )
]
GEMINI = [
    json.dumps(x)
    for x in (
        {"type": "init", "session_id": "g1", "model": "gemini-2.5-pro"},
        {"type": "message", "role": "assistant", "content": "Checking.", "delta": True},
        {"type": "tool_use", "tool_name": "get_status", "tool_id": "t1", "parameters": {}},
        {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "status: ok"},
        {
            "type": "result",
            "status": "success",
            "stats": {"input_tokens": 1000, "output_tokens": 10, "cached": 600, "duration_ms": 1200},
        },
    )
]


def _strip(evs: list[C.Event]) -> list[dict]:
    """The events without the time (the raw fallback has times only for calls and results)."""
    return [{k: v for k, v in json.loads(e.to_json()).items() if k != "t"} for e in evs]


class Envelope(unittest.TestCase):
    def test_round_trip(self):
        ev = C.Event(
            "result",
            text="done",
            subtype="success",
            turns=2,
            cost_usd=0.5,
            api_ms=4000.0,
            think_tokens=3,
            tokens=C.tokens(10, 5),
            session_id="c1",
            t=1761050000.123,
            seq=7,
        )
        line = ev.to_json()
        self.assertEqual(list(json.loads(line))[:5], ["v", "seq", "t", "src", "kind"])
        self.assertNotIn('"tool"', line)  # defaults left out
        self.assertEqual(C.Event.from_json(line), ev)
        h = C.Event("fault", src="harness", detail={"fault": "crash", "path": Path("/x")}, seq=0, t=1.0)
        self.assertEqual(C.Event.from_json(h.to_json()).detail, {"fault": "crash", "path": "/x"})

    def test_from_json_ignores_unknown_fields(self):
        ev = C.Event.from_json('{"v":1,"seq":0,"t":1,"src":"client","kind":"text","text":"x","later":1}')
        self.assertEqual((ev.kind, ev.text), ("text", "x"))

    def test_schema_names_every_field(self):
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(set(schema["properties"]), {f.name for f in fields(C.Event)})
        self.assertEqual(schema["required"], ["v", "seq", "t", "src", "kind"])
        self.assertEqual(schema["properties"]["v"]["const"], C.EVENTS_VERSION)
        for k in C.CLIENT_KINDS:
            self.assertIn(k, schema["properties"]["kind"]["description"])

    def test_claude_thinking_and_result_fields(self):
        evs = [e for ln in CLAUDE for e in C.ClaudeClient().parse(ln)]
        think = [e for e in evs if e.kind == "thinking"]
        self.assertEqual([(e.message_id, e.think_ms) for e in think], [("msg_1", 624.0)])
        self.assertEqual(C.format_event(think[0]), [])  # the human log is unchanged
        res = evs[-1]
        self.assertEqual((res.api_ms, res.think_tokens, res.duration_ms), (4000, 3, 5000))


class Log(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-events-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_seq_time_and_reopen(self):
        clock = iter([10.0, 11.0, 12.0])
        log = E.EventLog(self.d / "S001.events.jsonl", clock=lambda: next(clock))
        log.harness("session_start", client="claude")
        log.write(C.Event("text", text="hi"))
        log.write(C.Event("text", text="given", t=5.0))
        log.close()
        log.write(C.Event("text", text="after close"))  # dropped, no error
        log = E.EventLog(self.d / "S001.events.jsonl", clock=lambda: 20.0)
        log.end("exited", 0, 20.0, detail="", fault_at=None)
        evs = E.load_events(self.d / "S001")
        self.assertEqual(
            [(e.seq, e.t, e.src, e.kind) for e in evs],
            [
                (0, 10.0, "harness", "session_start"),
                (1, 11.0, "client", "text"),
                (2, 5.0, "client", "text"),
                (3, 20.0, "harness", "session_end"),
            ],
        )
        self.assertEqual(evs[0].detail, {"client": "claude"})
        self.assertEqual(evs[3].detail, {"reason": "exited", "exit": 0, "minutes": 0.0})

    def test_cut_line_skipped_and_kinds(self):
        log = E.EventLog(self.d / "S002.events.jsonl", clock=lambda: 1.0)
        for ev in (C.Event("text", text='"kind":"result" in a text'), C.Event("result", turns=1)):
            log.write(ev)
        log.close()
        with open(self.d / "S002.events.jsonl", "a") as f:
            f.write('{"v":1,"seq":2,"t":1,"src":"cli')
        self.assertEqual(len(E.load_events(self.d / "S002")), 2)
        self.assertEqual([e.kind for e in E.load_events(self.d / "S002", kinds={"result"})], ["result"])

    def write_both(self, client, lines: list[str], stem: str) -> None:
        """The raw stream as the client wrote it, and the events log as run_claude's pump writes it."""
        (self.d / f"{stem}.jsonl").write_text("\n".join(lines) + "\n")
        log = E.EventLog(self.d / f"{stem}.log-copy.events.jsonl", clock=lambda: 1.0)
        for ln in lines:
            for ev in client.parse(ln):
                log.write(ev)
        log.close()

    def test_parity_raw_fallback_and_log(self):
        for i, (name, client, lines) in enumerate(
            (
                ("claude", C.ClaudeClient(), CLAUDE),
                ("codex", C.CodexClient(server={}), TC.CODEX_RUN),
                ("codex", C.CodexClient(server={}), TC.CODEX_DOWN),
                ("gemini", C.GeminiClient(server={}), GEMINI),
            )
        ):
            stem = f"S00{i + 1}"
            with self.subTest(name=name, stem=stem):
                self.write_both(client, lines, stem)
                logged = E.load_events(self.d / f"{stem}.log-copy")
                raw = E.load_events(self.d / stem)
                self.assertTrue(logged)
                self.assertEqual(_strip(raw), _strip(logged))
                self.assertEqual(
                    [e.kind for e in E.load_events(self.d / stem, kinds={"result"})],
                    [e.kind for e in logged if e.kind == "result"],
                )

    def test_codex_resume_totals_shared_parser(self):
        (self.d / "S001.jsonl").write_text("\n".join(TC.CODEX_RUN) + "\n")
        (self.d / "S001-r1.jsonl").write_text("\n".join(TC.CODEX_RESUME) + "\n")
        parsers: dict = {}
        first = E.load_events(self.d / "S001", kinds={"result"}, parsers=parsers)
        resumed = E.load_events(self.d / "S001-r1", kinds={"result"}, parsers=parsers)
        self.assertEqual(first[-1].tokens["output"], 100)
        self.assertEqual(resumed[-1].tokens["output"], 50)  # this process's share of the running total

    def test_raw_fallback_times_calls_from_turns(self):
        (self.d / "S001.jsonl").write_text("\n".join(CLAUDE) + "\n")
        (self.d / "S001.turns.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in (
                    {"type": "use", "t": 100.0, "id": "toolu_1", "name": "look"},
                    {"type": "result", "t": 101.5, "id": "toolu_1"},
                    {"type": "result", "t": 109.0, "id": "toolu_1"},
                )
            )
            + "\n"
        )
        evs = {(e.kind, e.call_id): e.t for e in E.load_events(self.d / "S001") if e.call_id}
        self.assertEqual(
            evs,
            {
                ("tool_call", "toolu_1"): 100.0,
                ("tool_result", "toolu_1"): 101.5,
                ("tool_call", "toolu_2"): None,
                ("tool_result", "toolu_2"): None,
            },
        )

    def test_missing_session(self):
        self.assertEqual(E.load_events(self.d / "S404"), [])

    def test_result_text(self):
        self.assertEqual(E.result_text(C.Event("tool_result", text=' {"result": "a door"} ')), "a door")
        self.assertEqual(E.result_text(C.Event("tool_result", text="{not json")), "{not json")


class RunClaudeWritesEvents(TCR.FakeRunBase):
    """run_claude's pump writes Sxxx.events.jsonl: the client's events between the runner's start and end."""

    def run_one(self) -> list[C.Event]:
        os.environ["FAKE_CLAUDE_FIRST_S"] = "0"
        self.r.run_claude("S001", "go", 20, "11111111-1111-1111-1111-111111111111", needs_game=False)
        self.assertIsNone(self.r.events_log)
        base = self.r.state.dir / "S001"
        self.assertTrue(base.with_name("S001.events.jsonl").exists())
        return E.load_events(base)

    def test_exited(self):
        evs = self.run_one()
        self.assertEqual(
            [(e.src, e.kind) for e in evs],
            [
                ("harness", "session_start"),
                ("client", "init"),
                ("client", "text"),
                ("client", "result"),
                ("harness", "session_end"),
            ],
        )
        self.assertEqual([e.seq for e in evs], list(range(5)))
        self.assertTrue(all(e.t for e in evs))
        self.assertEqual((evs[0].detail["client"], evs[0].detail["session"]), ("claude", "S001"))
        self.assertEqual((evs[-1].detail["reason"], evs[-1].detail["exit"]), ("exited", 0))
        self.assertEqual(evs[3].cost_usd, 0.5)

    def test_runner_events_inside(self):
        os.environ["FAKE_CLAUDE_MCP"] = "failed"
        os.environ["FAKE_CLAUDE_FIRST_S"] = "60"
        self.r.run_claude("S001", "go", 20, "11111111-1111-1111-1111-111111111111", needs_game=False)
        evs = E.load_events(self.r.state.dir / "S001")
        kinds = [(e.src, e.kind) for e in evs]
        self.assertIn(("harness", "mcp_failed"), kinds)
        self.assertLess(kinds.index(("client", "init")), kinds.index(("harness", "mcp_failed")))
        self.assertEqual(kinds[-1], ("harness", "session_end"))
        self.assertEqual(evs[-1].detail["reason"], "mcp_failed")
        self.r.event("after", x=1)  # no log open: runner_events.jsonl only
        self.assertEqual(len(E.load_events(self.r.state.dir / "S001")), len(evs))


if __name__ == "__main__":
    unittest.main()
