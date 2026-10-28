#!/usr/bin/env python3
"""A scripted stand-in for `claude -p` that makes one in-game reload through the run's own bg3 MCP server.

Live probe N14 (the bench's reload count, SaveWatch): a real bench run with a stand-in agent, no model and no text
added to any real agent's prompt. Linked as `claude` into a directory put first on PATH, it takes the command line
the runner gives claude (clients.ClaudeClient.command): it starts the bg3 server of --mcp-config as claude would
(stdio, cwd play/, the runner's env plus the server's), calls save_game(label=<BG3AI_SESSION>-reload), list_saves
until that save is on disk, then load_game(save=<label>), and ends its turn. It prints claude's stream-json (init,
tool_use, tool_result, result with cost 0), so the runner reads it as a claude session. A --resume reloads nothing
(one reload per run), and with load_game off (a condition without reloads) it saves nothing and ends with an error.

Standard library only: it runs under the system python3 from the shebang.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSION = "0.0.0 (bg3harness.testing.reload_agent: scripted, no model)"
SAVE_WAIT_S = 120.0  # save_game returns at once; the save is on disk (list_saves) seconds later
SAVE_POLL_S = 2.0
PREFIX = "mcp__bg3__"


def emit(ev: dict[str, Any]) -> None:
    print(json.dumps(ev), flush=True)


def arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def server_config(spec: str) -> dict[str, Any]:
    """The bg3 server of --mcp-config: a file, or inline JSON (the container sandbox's)."""
    text = spec if spec.lstrip().startswith("{") else Path(spec).read_text(encoding="utf-8")
    server = json.loads(text)["mcpServers"]["bg3"]
    if server.get("type", "stdio") != "stdio":
        raise SystemExit(f"reload_agent: only a stdio bg3 server is supported, not {server.get('type')!r}")
    return server


class Mcp:
    """A minimal MCP client over the server's stdio (newline-delimited JSON-RPC)."""

    def __init__(self, server: dict[str, Any]):
        extra: dict[str, str] = server.get("env") or {}
        env = {**os.environ, **extra}
        self.proc = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        self.next_id = 0

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
            if "method" in msg and "id" in msg:  # a request from the server (ping, roots): answer it empty
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        raise RuntimeError(f"{method}: the bg3 server closed its output")

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def start(self) -> list[str]:
        """The handshake; the server's tool names."""
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "reload_agent", "version": VERSION},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return [t["name"] for t in self.request("tools/list", {}).get("tools", [])]

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=10)


class Turn:
    """One turn's stream-json: each tool call as claude prints it (assistant tool_use, user tool_result)."""

    def __init__(self, mcp: Mcp, sid: str):
        self.mcp, self.sid, self.calls = mcp, sid, 0

    def call(self, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls += 1
        cid = f"toolu_reload_{self.calls}"
        emit(
            {
                "type": "assistant",
                "session_id": self.sid,
                "message": {
                    "id": f"msg_reload_{self.calls}",
                    "content": [{"type": "tool_use", "id": cid, "name": PREFIX + tool, "input": args}],
                },
            }
        )
        res = self.mcp.request("tools/call", {"name": tool, "arguments": args})
        content: list[dict[str, Any]] = res.get("content") or []
        text = "\n".join(str(c.get("text", "")) for c in content if c.get("type") == "text")
        err = bool(res.get("isError"))
        emit(
            {
                "type": "user",
                "session_id": self.sid,
                "message": {"content": [{"type": "tool_result", "tool_use_id": cid, "content": text, "is_error": err}]},
            }
        )
        return text, err


def reload_once(
    turn: Turn,
    tools: list[str],
    label: str,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """save_game, the save on disk, load_game: None when the load is done, else why not."""
    if "load_game" not in tools or "save_game" not in tools:
        return "load_game or save_game is not in the bg3 server's tools (the condition removes it)"
    text, err = turn.call("save_game", {"label": label})
    if err or "REFUSED" in text:
        return f"save_game failed: {text}"
    end = clock() + SAVE_WAIT_S
    while True:
        text, err = turn.call("list_saves", {})
        if f"__{label}]" in text:
            break
        if clock() >= end:
            return f"the save {label} wasn't on disk {SAVE_WAIT_S:g} s after save_game"
        sleep(SAVE_POLL_S)
    text, err = turn.call("load_game", {"save": label})
    if err or not text.startswith("load_game: done"):
        return f"load_game failed: {text}"
    return None


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    resume = "--resume" in argv
    sid = arg(argv, "--resume" if resume else "--session-id") or "reload-agent"
    spec = arg(argv, "--mcp-config")
    if spec is None:
        print("reload_agent: no --mcp-config", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    mcp = Mcp(server_config(spec))
    why: str | None = "interrupted"
    turn = Turn(mcp, sid)
    label = f"{os.environ.get('BG3AI_SESSION') or 'S000'}-reload"
    try:
        tools = mcp.start()
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": sid,
                "model": arg(argv, "--model"),
                "tools": [PREFIX + t for t in tools],
                "mcp_servers": [{"name": "bg3", "status": "connected"}],
            }
        )
        why = None if resume else reload_once(turn, tools, label)
    except KeyboardInterrupt:  # the runner's hard stop
        pass
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    finally:
        mcp.close()
    done = "resumed: nothing to do (one reload per run)" if resume else f"reloaded {label}"
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution" if why else "success",
            "is_error": bool(why),
            "result": why or done,
            "session_id": sid,
            "num_turns": turn.calls,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "duration_api_ms": 0,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
