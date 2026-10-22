"""bg3 MCP server: the agent's eyes and hands in Baldur's Gate 3.

Design rules (docs/ARCHITECTURE.md):
* Engine first. State is compact text from the mod's visibility-filtered ops, and actions are engine
  calls or UI commands. Screenshots are an explicit opt-in (`screenshot`, `zoom`) for ambiguity only.
* Fair. Everything comes through the mod's player-visible projections. GUIDs never reach the agent
  (short handles like e12 instead).
* Every result ends with what happened since the last call (events) and the session clock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import memtools, metrics, skipper
from .game import Game
from .tools import core, session

mcp = core.mcp  # the server every tools/ module registers its tools on (tools/__init__.py has the order)
INSTRUCTIONS = session.INSTRUCTIONS  # for the served tool set; tools/pi/dump_server.py reads these two
RULES = core.RULES


# ---------------------------------------------------------------------------
# keeper mode (BG3AI_KEEPER_MODE=1): the runner's memory keeper pass for any client
# ---------------------------------------------------------------------------

KEEPER_TOOLS = ("memory_list", "memory_read", "memory_search", "memory_write", "memory_edit", "memory_append")
KEEPER_DEFAULT_TASK = (
    "Memory keeper mode: you maintain the memory files of an AI's Baldur's Gate 3 playthrough "
    "between sessions. Your task is in your prompt. No game tools."
)


def keeper_mode() -> bool:
    return os.environ.get("BG3AI_KEEPER_MODE", "0") not in ("", "0")


def keeper_task(env=None) -> str:
    """The keeper's task text for the server instructions: the file $BG3AI_KEEPER_TASK (runner/memory_keeper.py
    writes its prompt there), so a client that shows MCP instructions gets it too."""
    path = (os.environ if env is None else env).get("BG3AI_KEEPER_TASK")
    if not path:
        return KEEPER_DEFAULT_TASK
    try:
        return Path(path).read_text(encoding="utf-8").strip() or KEEPER_DEFAULT_TASK
    except OSError:
        return KEEPER_DEFAULT_TASK


def keeper_server(task: str, sandbox: memtools.KeeperSandbox) -> MCPServer:
    """A separate server with only the memory tools over the keeper's sandbox: no game tools, no game connection or
    background threads, no session_start/end_session (the memory session stays as the runner left it), no events
    or clock in results, nothing in runtime/actions.jsonl or the metrics (the client's stream log is the record)."""
    srv = MCPServer("bg3", instructions=task)

    def guarded(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw).rstrip()
            except memtools.Refused as e:
                return str(e)

        return wrapper

    def memory_list(path: str = "memory") -> str:
        """List a folder: memory/ (the playthrough's notes), memory/sessions, memory/chapters, knowledge/ (read-only reference notes). Shows each file's line count."""
        return memtools.list_files(sandbox, path)

    def memory_read(path: str, start_line: int = 1, max_lines: int = 400) -> str:
        """Read a file: "memory/<file>" (e.g. memory/npcs.md, memory/sessions/S012.md) or "knowledge/<file>". A bare name means memory/. Long files come in pages: start_line for the next one."""
        return memtools.read_file(sandbox, path, start_line, max_lines)

    def memory_search(query: str, path: str = "memory", regex: bool = False) -> str:
        """Find a name or word (case-insensitive): every matching line as file:line: text. path narrows it (memory/npcs.md, memory/sessions, knowledge). regex=true for a regular expression."""
        return memtools.search(sandbox, query, path, regex)

    def memory_write(path: str, content: str) -> str:
        """Create or rewrite a whole memory file (Markdown only; only the files your task names are writable). The result shows memory lint errors about the file: fix them."""
        return memtools.write_file(sandbox, path, content or "")

    def memory_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in a memory file (old_text must match once, whitespace included; replace_all=true for every match). The result shows memory lint errors about the file: fix them."""
        return memtools.edit_file(sandbox, path, old_text or "", new_text or "", replace_all)

    def memory_append(path: str, text: str) -> str:
        """Append lines at the end of a memory file. The result shows memory lint errors about the file: fix them."""
        return memtools.append_file(sandbox, path, text or "")

    for fn in (memory_list, memory_read, memory_search, memory_write, memory_edit, memory_append):
        srv.add_tool(guarded(fn), meta=dict(session.ALWAYS_LOAD_META))
    return srv


def main(argv: list[str] | None = None) -> None:
    import argparse
    import threading

    ap = argparse.ArgumentParser(prog="bg3ai-mcp", description="bg3 MCP server")
    ap.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (one client, the default) or http (MCP streamable HTTP at /mcp, e.g. for a client in "
        "a container: tools/container/README.md)",
    )
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="http: address to bind (the docker network's gateway address for a container client)",
    )
    ap.add_argument("--port", type=int, default=8770, help="http: port (the dashboard has 8765)")
    a = ap.parse_args(argv)
    if keeper_mode():
        srv = keeper_server(keeper_task(), memtools.KeeperSandbox.from_env(lambda: core.MEMORY, lambda: core.KNOWLEDGE))
        if a.transport == "http":
            srv.run("streamable-http", **http_options(a.host, a.port))
        else:
            srv.run("stdio")
        return
    # Serving a client: without the runner the first tool call opens the memory session (never on a plain import).
    core.LIFE.auto = os.environ.get("BG3AI_AUTO_SESSION", "1") != "0"
    stop = threading.Event()
    if skipper.enabled():
        threading.Thread(target=session._movie_skipper, args=(stop,), name="movie-skipper", daemon=True).start()
    sample_s = float(os.environ.get("BG3AI_METRICS_SAMPLE_S", "1.0") or 0)
    if sample_s > 0:  # busy/idle samples for the latency metrics, on their own connection
        threading.Thread(
            target=metrics.sampler_loop,
            args=(core.METRICS, Game(port=core.game.port), stop, sample_s),
            name="activity-sampler",
            daemon=True,
        ).start()
    try:
        if a.transport == "http":
            core.mcp.run("streamable-http", **http_options(a.host, a.port))
        else:
            core.mcp.run("stdio")
    finally:
        stop.set()


def http_options(host: str, port: int) -> dict:
    """streamable-http settings. The SDK's DNS-rebinding guard accepts only localhost Host headers by default; a
    container reaches the server by the bind address, so that host:port is allowed too."""
    opts: dict[str, Any] = {"host": host, "port": port}
    if host not in ("127.0.0.1", "localhost", "::1"):
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = [f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"]
        opts["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[f"http://{h}" for h in hosts]
        )
    return opts


if __name__ == "__main__":
    main()
