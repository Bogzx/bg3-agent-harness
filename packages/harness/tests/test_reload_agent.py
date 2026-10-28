"""The scripted reload agent of live probe N14 (bg3harness.testing.reload_agent) against a fake bg3 MCP server.

The agent's main gets the argv the runner gives claude (ClaudeClient.command), in its cwd and session env; the
server is a small stdio JSON-RPC script that logs each tool call; the agent's stdout goes through
ClaudeClient.parse, so the runner reads it as a claude session. The fake's load_game answers as the bridge does,
so SaveWatch's LOADED_RE takes it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from bg3harness import clients
from bg3harness.runner.run_host import LOADED_RE
from bg3harness.testing import reload_agent

AGENT = Path(reload_agent.__file__)

SERVER = r"""
import json, sys
tools, log = sys.argv[1].split(","), open(sys.argv[2], "a")
def text(name, args):
    if name == "save_game":
        return "save_game: saving in the background"
    if name == "list_saves":
        return "10-28 03:00  S001-reload  [Tav-1__S001-reload]\n10-28 02:00  SCN-start  [Tav-1__SCN-start]"
    if name == "load_game":
        return "load_game: done — loaded=Tav-1__" + args["save"]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "bg3"}}
    elif m == "tools/list":
        res = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in tools]}
    else:
        p = msg["params"]
        log.write(json.dumps([p["name"], p["arguments"]]) + "\n"); log.flush()
        res = {"content": [{"type": "text", "text": text(p["name"], p["arguments"])}], "isError": False}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
"""


def run(
    tmp_path: Path, mp: pytest.MonkeyPatch, cap: pytest.CaptureFixture[str], tools: str, resume: bool = False
) -> tuple[list[clients.Event], list[Any], int]:
    """The agent's main on the argv the runner gives claude, in play/'s place (tmp_path), with the session env."""
    server, calls = tmp_path / "server.py", tmp_path / "calls.jsonl"
    server.write_text(SERVER)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bg3": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server), tools, str(calls)],
                        "env": {},
                    }
                }
            }
        )
    )
    client = clients.ClaudeClient(mcp_config=cfg, binary=str(AGENT))
    req = clients.Request("play", "haiku", tmp_path, session="sess-1", resume=resume)
    mp.setenv("BG3AI_SESSION", "S001")
    mp.chdir(tmp_path)
    code = reload_agent.main(client.command(req)[1:])
    events = [e for line in cap.readouterr().out.splitlines() for e in client.parse(line)]
    done = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return events, done, code


def test_it_saves_then_loads_its_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    events, calls, code = run(tmp_path, monkeypatch, capsys, "get_status,save_game,list_saves,load_game")
    assert code == 0
    assert calls == [
        ["save_game", {"label": "S001-reload"}],
        ["list_saves", {}],
        ["load_game", {"save": "S001-reload"}],
    ]
    kinds = [e.kind for e in events]
    assert kinds[0] == "init" and events[0].session_id == "sess-1" and events[0].tools == 4
    assert [e.tool for e in events if e.kind == "tool_call"] == ["save_game", "list_saves", "load_game"]
    load = [e for e in events if e.kind == "tool_result"][-1].text
    m = LOADED_RE.match(load)
    assert m and m.group(1) == "Tav-1__S001-reload"  # what SaveWatch reads from actions.jsonl
    res = events[-1]
    assert res.kind == "result" and not res.is_error and res.cost_usd == 0 and res.session_id == "sess-1"


def test_a_resume_reloads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    events, calls, code = run(tmp_path, monkeypatch, capsys, "save_game,list_saves,load_game", resume=True)
    assert code == 0 and calls == []
    assert events[-1].kind == "result" and not events[-1].is_error and events[-1].session_id == "sess-1"


def test_without_load_game_it_saves_nothing_and_ends_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    events, calls, code = run(tmp_path, monkeypatch, capsys, "get_status,save_game,list_saves")
    assert code == 1 and calls == []
    assert events[-1].kind == "result" and events[-1].is_error and "load_game" in events[-1].text


def test_it_answers_the_manifests_version_call():
    p = subprocess.run([str(AGENT), "--version"], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and "reload_agent" in p.stdout


class FakeTurn(reload_agent.Turn):
    """reload_once's turn: list_saves shows the save from list number `on_disk_from` on (0: never)."""

    def __init__(self, on_disk_from: int):
        self.calls: list[str] = []
        self.on_disk_from = on_disk_from

    def call(self, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append(tool)
        lists = self.calls.count("list_saves")
        if tool == "list_saves" and self.on_disk_from and lists >= self.on_disk_from:
            return "10-28 03:00  S001-reload  [Tav-1__S001-reload]", False
        if tool == "load_game":
            return "load_game: done — loaded=Tav-1__S001-reload", False
        return "10-28 02:00  SCN-start  [Tav-1__SCN-start]", False


def reload_on_a_fake_clock(t: FakeTurn) -> tuple[str | None, float]:
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    tools = ["save_game", "list_saves", "load_game"]
    why = reload_agent.reload_once(t, tools, "S001-reload", clock=lambda: now[0], sleep=sleep)
    return why, now[0]


def test_it_loads_once_the_save_is_on_disk():
    t = FakeTurn(on_disk_from=3)
    why, waited = reload_on_a_fake_clock(t)
    assert why is None and waited == 2 * reload_agent.SAVE_POLL_S
    assert t.calls == ["save_game", "list_saves", "list_saves", "list_saves", "load_game"]


def test_a_save_that_never_reaches_the_disk_ends_without_a_load():
    t = FakeTurn(on_disk_from=0)
    why, waited = reload_on_a_fake_clock(t)
    assert why is not None and "wasn't on disk" in why and waited >= reload_agent.SAVE_WAIT_S
    assert "load_game" not in t.calls and t.calls[0] == "save_game"
