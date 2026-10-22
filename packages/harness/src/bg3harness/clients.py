# pyright: basic
"""The CLI agent that plays a session, behind one small interface (Goal 3 of docs/AUDIT_2026-10-13.md: "put claude_cmd
behind a small client interface (a command builder + a stream parser)").

A Client builds the command line of one play process (model, effort, prompt or resume, the bg3 MCP server and no
other tools), writes whatever config file it needs into the session dir, adds its env, and parses its stdout (one
JSON object per line) into normalized Events: assistant text, tool call, tool result, the final result with cost,
tokens, is_error and the session id to resume. format_event turns Events into the runner's human log lines, so the
log, latency, cost and stop logic can stop depending on which CLI plays.

play.py still drives `claude` itself; ClaudeClient reproduces its claude_cmd and format_stream_event line for line
(test_clients.py compares both on every line of the real runtime/sessions streams), so wiring it in changes nothing
for Claude. Status per client:
- ClaudeClient: verified offline against play.py and real streams.
- CodexClient (codex-cli 0.157.1): the command and its -c overrides were run offline against a local stub model
  server and a stub MCP server (test_clients.py's fixture lines are from those runs); never run against a real model.
- GeminiClient: gemini isn't installed; built from the documented CLI only. Untested.

    python3 -m unittest runner/test_clients.py
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Protocol

REPO = Path(__file__).resolve().parents[4]
PLAY_DIR = REPO / "play"
MCP_CONFIG = PLAY_DIR / ".mcp.json"
MCP_PREFIX = "mcp__bg3__"
# A claude stream's API and usage-limit failures (the same pattern as runs.API_ERROR_RE; test_clients.py keeps them
# equal), and the assistant text claude writes in their place.
API_ERROR_RE = re.compile(r"API Error|usage limit|rate.?limit|overloaded|credit balance|invalid api key|"
                          r"authentication|\b(?:429|500|502|503|529)\b", re.I)
API_ERROR_TEXT = ("API Error", "Claude AI usage limit")
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")   # runs.TOKEN_FIELDS' names: input excludes cache reads


@dataclass
class Request:
    """One play process. The runner decides everything here; a client only spells it for its CLI."""
    prompt: str
    model: str
    session_dir: Path                  # runtime/sessions/<pt>: where a client writes its config files
    session: str | None = None         # the conversation id: started (claude --session-id) or resumed
    resume: bool = False
    effort: str | None = None          # play.EFFORTS; None = the CLI's default
    fallback_model: str | None = None
    autocompact: int | None = None     # claude --autocompact; the runner leaves it None for the fix pass
    env: dict = field(default_factory=dict)   # the session env (BG3AI_*), also set on the process by the runner
    tag: str = ""                      # the session's file stem (S012, S012-fix): names per-session config files


EVENTS_VERSION = 1   # Sxxx.events.jsonl (packages/harness/schemas/events.v1.schema.json)
CLIENT_KINDS = ("init", "text", "thinking", "tool_call", "tool_result", "usage", "rate_limit", "compact", "error",
                "result")


@dataclass
class Event:
    """One normalized stream event. kind: CLIENT_KINDS from a client's stream (src "client"), or the runner's own
    kinds (src "harness": session_start, session_end, fault, mcp_failed, ...; what in `detail`). tokens use
    TOKEN_KEYS (input without cache reads, as claude reports it). The envelope (v, t, seq, src) is set by the
    events.jsonl writer (bg3harness/events.py), not by the parsers."""
    kind: str
    text: str = ""
    tool: str = ""                     # bare bg3 tool name (other servers' tools keep their prefix)
    args: dict | None = None
    call_id: str = ""
    is_error: bool = False
    api_error: bool = False            # an API, auth or usage-limit failure (not the game's or a tool's)
    session_id: str | None = None
    model: str | None = None
    tools: int | None = None           # init: tools the model was given
    mcp: list[str] | None = None       # init: "name:status" per MCP server
    subtype: str | None = None         # result: success | error_during_execution | ...
    turns: int | None = None
    duration_ms: float | None = None
    cost_usd: float | None = None      # result: the process's cost (None when the client can't know it)
    tokens: dict | None = None
    message_id: str | None = None      # usage: the message a running token count belongs to
    resets_at: float | None = None     # rate_limit: when the limit resets (epoch s), if the client says
    think_ms: float | None = None      # thinking: the CLI's thinking time for this block (claude thinking_duration_ms)
    api_ms: float | None = None        # result: API time of the process (claude duration_api_ms)
    think_tokens: int | None = None    # result: output tokens spent thinking (claude output_tokens_details)
    detail: dict | None = None         # harness kinds: what happened (reason, fault, exit, ...)
    v: int = EVENTS_VERSION
    t: float | None = None             # when the runner read the line (epoch s)
    seq: int | None = None             # position in the process's events.jsonl
    src: str = "client"                # client | harness

    def to_json(self) -> str:
        """One events.jsonl line: the envelope first, then every field that differs from its default."""
        d = {"v": self.v, "seq": self.seq, "t": self.t, "src": self.src, "kind": self.kind}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name not in d and value != f.default:
                d[f.name] = value
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, line: str) -> Event:
        d = json.loads(line)
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


class Client(Protocol):
    name: str
    supports_resume: bool
    assigns_session: bool              # True: the runner picks the conversation id up front; False: read it from
                                       # the stream (Event.session_id of init) and pass it back to resume
    no_tools: str                      # what "no tools but the bg3 MCP server" means for this CLI

    def write_config(self, req: Request) -> list[Path]: ...
    def env(self, req: Request) -> dict: ...
    def command(self, req: Request) -> list[str]: ...
    def parse(self, line: str) -> list[Event]: ...


def tokens(input_=0, output=0, cache_read=0, cache_write=0) -> dict:
    return dict(zip(TOKEN_KEYS, (int(input_ or 0), int(output or 0), int(cache_read or 0), int(cache_write or 0))))


def bare_tool(name: str) -> str:
    return (name or "?").removeprefix(MCP_PREFIX)


def bg3_server(path: Path = MCP_CONFIG) -> dict:
    """The bg3 stdio server of play/.mcp.json (the one claude gets), for clients that take their own config."""
    return json.loads(path.read_text())["mcpServers"]["bg3"]


def http_server(url: str, timeout: int = 110_000) -> dict:
    """The bg3 server over streamable HTTP (`bg3ai-mcp --transport http`), for a client in the container sandbox
    (bg3harness/sandbox.py): the server runs on the host with the session env, so the client passes none."""
    return {"type": "http", "url": url, "timeout": timeout}


def is_http(server: dict) -> bool:
    return bool(server.get("url"))


def session_env_names(env: dict) -> list[str]:
    """The session env the MCP server reads (BG3AI_*, BG3_*): clients that don't pass their whole env to an MCP
    server forward these by name."""
    return sorted(k for k in env if k.startswith(("BG3AI_", "BG3_")))


def _json_text(value) -> str:
    """A tool result body as play.format_stream_event shows it: text blocks joined, other values as JSON."""
    if isinstance(value, list):
        value = " ".join(b.get("text", f"<{b.get('type')}>") for b in value if isinstance(b, dict))
    if not value:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(line: str) -> dict | None:
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return ev if isinstance(ev, dict) else None


def _short(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_event(ev: Event) -> list[str]:
    """The runner's human log lines for one Event (play.format_stream_event's format, for any client)."""
    if ev.kind == "init":
        return [f"[init] session={ev.session_id} model={ev.model} tools={ev.tools or 0} mcp={ev.mcp or []}"]
    if ev.kind == "compact":
        return ["[compact] context compacted"]
    if ev.kind == "text":
        return [f"[say] {_short(ev.text, 2000)}"]
    if ev.kind == "tool_call":
        return [f"[tool] {ev.tool} {_short(ev.args or {}, 300)}"]
    if ev.kind == "tool_result":
        return [f"{'[error]' if ev.is_error else '[result]'} {_short(ev.text, 400)}"]
    if ev.kind == "error":
        return [f"[api] {_short(ev.text, 400)}"]
    if ev.kind == "result":
        cost = f"${ev.cost_usd or 0:.2f}" if ev.cost_usd is not None or not ev.tokens else "$?"
        return [f"[end] {ev.subtype} turns={ev.turns} duration={round((ev.duration_ms or 0) / 60000, 1)}min "
                f"cost={cost}"]
    return []


# ------------------------------------------------------------------------------------------------------- claude

# play.py's CLAUDE_FLAGS (its comment says why each is there); test_clients.py keeps the two equal.
PLAY_TOOLS = "ToolSearch"
CLAUDE_FLAGS = ["--exclude-dynamic-system-prompt-sections", "--setting-sources", "project,local",
                "--disable-slash-commands", "--tools", PLAY_TOOLS]


class ClaudeClient:
    """`claude -p` with stream-json, exactly as play.py runs it today (claude_cmd, format_stream_event)."""
    name = "claude"
    supports_resume = True
    assigns_session = True
    no_tools = ("--tools ToolSearch (it loads the deferred MCP tools' schemas) + --strict-mcp-config with "
                "play/.mcp.json: only mcp__bg3__*; play/.claude/settings.json denies the rest again")

    def __init__(self, mcp_config: Path | str = MCP_CONFIG, binary: str = "claude", settings: dict | None = None):
        # mcp_config: a file or an inline JSON string (the container sandbox's http server). settings: an extra
        # --settings JSON (the memory keeper's MCP pass, the sandbox: allow mcp__bg3__* outside play/)
        self.mcp_config, self.binary, self.settings = mcp_config, binary, settings

    def write_config(self, req: Request) -> list[Path]:
        return []   # play/.mcp.json is the config (it sets BG3AI_SERVE_RULES=0: claude reads play/CLAUDE.md itself)

    def env(self, req: Request) -> dict:
        # The playthrough has its own memory system; Claude Code's auto-memory would add a second one.
        return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

    def command(self, req: Request) -> list[str]:
        cmd = [self.binary, "-p", req.prompt, "--model", req.model, "--output-format", "stream-json",
               "--verbose", "--strict-mcp-config", "--mcp-config", str(self.mcp_config), *CLAUDE_FLAGS]
        if self.settings:
            cmd += ["--settings", json.dumps(self.settings)]
        if req.effort:
            cmd += ["--effort", req.effort]
        if req.fallback_model:
            cmd += ["--fallback-model", req.fallback_model]
        if req.autocompact:
            cmd += ["--autocompact", str(req.autocompact)]
        cmd += ["--resume", req.session] if req.resume else ["--session-id", req.session]
        return cmd

    def parse(self, line: str) -> list[Event]:
        ev = _loads(line)
        if ev is None:
            return []
        t, sub, sid = ev.get("type"), ev.get("subtype"), ev.get("session_id")
        if t == "system" and sub == "init":
            mcp = [f"{m.get('name')}:{m.get('status')}" for m in ev.get("mcp_servers") or [] if isinstance(m, dict)]
            # tools: the bg3 server's (pi's count; claude's list has its own too, e.g. ToolSearch)
            bg3 = [t for t in ev.get("tools") or [] if str(t).startswith(MCP_PREFIX)]
            return [Event("init", session_id=sid, model=ev.get("model"), tools=len(bg3), mcp=mcp)]
        if t == "system" and sub == "compact_boundary":
            return [Event("compact", session_id=sid)]
        if t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            reset = info.get("resetsAt")
            return [Event("rate_limit", text=str(info.get("status") or ""), session_id=sid,
                          api_error=info.get("status") == "rejected",
                          resets_at=float(reset) if isinstance(reset, (int, float)) else None)]
        if t == "assistant":
            msg = ev.get("message") or {}
            out = []
            think_ms = ev.get("thinking_duration_ms")
            for c in msg.get("content") or []:
                if c.get("type") == "thinking" and think_ms:   # the thinking time model_time.py reads
                    out.append(Event("thinking", session_id=sid, message_id=msg.get("id"), think_ms=float(think_ms)))
                    think_ms = None
                elif c.get("type") == "text" and c.get("text", "").strip():
                    text = c["text"].strip()
                    out.append(Event("text", text=text, session_id=sid, api_error=text.startswith(API_ERROR_TEXT)))
                elif c.get("type") == "tool_use":
                    out.append(Event("tool_call", tool=bare_tool(c.get("name", "?")), args=c.get("input") or {},
                                     call_id=c.get("id") or "", session_id=sid))
            if isinstance(msg.get("usage"), dict):
                u = msg["usage"]
                out.append(Event("usage", session_id=sid, message_id=msg.get("id"),
                                 tokens=tokens(u.get("input_tokens"), u.get("output_tokens"),
                                               u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"))))
            return out
        if t == "user":
            content = (ev.get("message") or {}).get("content")
            return [Event("tool_result", text=_json_text(c.get("content")), call_id=c.get("tool_use_id") or "",
                          is_error=bool(c.get("is_error")), session_id=sid)
                    for c in (content if isinstance(content, list) else []) if c.get("type") == "tool_result"]
        if t == "result":
            u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            err = bool(ev.get("is_error"))
            return [Event("result", text=str(ev.get("result") or ""), subtype=sub, session_id=sid, is_error=err,
                          api_error=err and bool(API_ERROR_RE.search(str(ev.get("result") or ""))),
                          turns=ev.get("num_turns"), duration_ms=ev.get("duration_ms"),
                          api_ms=ev.get("duration_api_ms"),
                          think_tokens=(u.get("output_tokens_details") or {}).get("thinking_tokens"),
                          cost_usd=float(ev.get("total_cost_usd") or 0),
                          tokens=tokens(u.get("input_tokens"), u.get("output_tokens"),
                                        u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")))]
        return []


# -------------------------------------------------------------------------------------------------------- codex

# USD per million tokens: (input, cached input, output). Codex reports tokens only, so its cost is computed here. A
# model missing from the table gets cost None (tokens still counted). Prices as of 2026-10-16 from OpenAI's public
# price list as last known to us; recheck before a benchmark series and add the model it plays with
# (gpt-6-astra, ~/.codex/config.toml's default here, isn't listed: no price known to us).
CODEX_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5": (1.25, 0.125, 10.0),
    "gpt-5-codex": (1.25, 0.125, 10.0),
    "gpt-5-mini": (0.25, 0.025, 2.0),
    "gpt-5-nano": (0.05, 0.005, 0.4),
}
CODEX_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
# Codex features that give the model a tool or extra context besides the MCP servers (`codex features list`,
# 0.157.1); unbounded_connection_retries off so a dead network ends the turn (turn.failed) instead of reconnecting
# forever. A name this version doesn't know is an error, so the list is pinned to the installed version.
CODEX_DISABLE = ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "memories", "image_generation",
                 "browser_use", "computer_use", "view_image", "goals", "sleep_tool", "tool_suggest",
                 "unbounded_connection_retries")


def toml_value(v) -> str:
    """A Python value as a TOML literal for `codex -c key=value` (strings, numbers, bools, lists, flat tables)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)   # a TOML basic string: JSON's escapes are valid TOML escapes
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"no TOML literal for {type(v).__name__}")


def codex_cost(model: str | None, toks: dict) -> float | None:
    price = CODEX_PRICES.get(model or "")
    if price is None:
        return None
    return round((toks["input"] * price[0] + toks["cache_read"] * price[1] + toks["output"] * price[2]) / 1e6, 6)


class CodexClient:
    """`codex exec --json` (codex-cli 0.157.1). The user's ~/.codex/config.toml is ignored (--ignore-user-config;
    auth still comes from ~/.codex) and everything is set with -c overrides, so nothing is written for it. Sessions
    are persisted by codex in ~/.codex/sessions (needed for `codex exec resume`). The rules: the bg3 server serves
    play/CLAUDE.md itself (BG3AI_SERVE_RULES=1: MCP instructions + the first briefing, bridge/src/bg3ai/rules.py),
    and no AGENTS.md is loaded (project_doc_max_bytes=0)."""
    name = "codex"
    supports_resume = True
    assigns_session = False   # the thread id comes in thread.started; `codex exec resume <id>` continues it
    no_tools = ("shell/exec/file/web/agent/app features disabled (CODEX_DISABLE), web_search disabled, read-only "
                "sandbox with approval_policy=never as a second fence; only mcp_servers.bg3 is configured")

    def __init__(self, server: dict | None = None, binary: str = "codex", extra_config: dict | None = None,
                 cwd: Path = PLAY_DIR):
        self.server = server if server is not None else bg3_server()
        self.binary, self.extra_config, self.cwd = binary, dict(extra_config or {}), cwd
        self.model: str | None = None   # parse: the model of the stream, for the cost
        self.thread: str | None = None
        # turn.completed's usage is the thread's running total: a resumed thread's counts include the earlier
        # processes (seen with 0.157.1: 2000 input tokens, then 3000 after a resume that used 1000). Per thread,
        # the last total seen, so each result reports only its own process; a runner restarted between a run and
        # its resume seeds it with seed_usage(thread, the earlier stream's last turn.completed usage).
        self.thread_usage: dict[str, dict] = {}

    def seed_usage(self, thread: str, usage: dict) -> None:
        self.thread_usage[thread] = self._usage_tokens(usage)

    @staticmethod
    def _usage_tokens(u: dict) -> dict:
        cached = int(u.get("cached_input_tokens") or 0)
        # input_tokens includes the cached ones and output_tokens the reasoning ones (OpenAI's usage)
        return tokens(int(u.get("input_tokens") or 0) - cached, u.get("output_tokens"), cached,
                      u.get("cache_write_input_tokens"))

    def write_config(self, req: Request) -> list[Path]:
        return []

    def env(self, req: Request) -> dict:
        return {}

    def config(self, req: Request) -> dict:
        """The -c overrides (dotted key → value)."""
        s = self.server
        env = {**(s.get("env") or {}), "BG3AI_SERVE_RULES": "1"}   # .mcp.json's 0 is for claude only
        cfg = {
            "approval_policy": "never",
            "sandbox_mode": "read-only",
            "web_search": "disabled",
            "project_doc_max_bytes": 0,
            "skills.include_instructions": False,
        }
        if is_http(s):   # the container sandbox: the host runs the server with the session env
            cfg["mcp_servers.bg3.url"] = s["url"]
        else:
            cfg.update({
                "mcp_servers.bg3.command": s["command"],
                "mcp_servers.bg3.args": list(s.get("args") or []),
                "mcp_servers.bg3.cwd": str(self.cwd),
                "mcp_servers.bg3.env": env,
                # codex starts MCP servers with a filtered env: the session's BG3AI_* go by name
                "mcp_servers.bg3.env_vars": session_env_names(req.env),
            })
        cfg.update({
            # approval_policy=never refuses every MCP call that needs approval ("MCP tool call requires approval,
            # but approval policy is never"): the bg3 tools are pre-approved, they are the agent's only tools
            "mcp_servers.bg3.default_tools_approval_mode": "approve",
            "mcp_servers.bg3.startup_timeout_sec": 60,
            "mcp_servers.bg3.tool_timeout_sec": (s.get("timeout") or 110_000) / 1000,
        })
        if req.effort:
            cfg["model_reasoning_effort"] = CODEX_EFFORTS.get(req.effort, req.effort)
        cfg.update(self.extra_config)
        return cfg

    def command(self, req: Request) -> list[str]:
        if req.resume and not req.session:
            raise ValueError("codex resume needs the thread id from the first run's thread.started")
        self.model = req.model
        cmd = [self.binary, "exec"] + (["resume"] if req.resume else [])
        cmd += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "-m", req.model]
        for feature in CODEX_DISABLE:
            cmd += ["--disable", feature]
        for k, v in self.config(req).items():
            cmd += ["-c", f"{k}={toml_value(v)}"]
        return cmd + ([req.session, req.prompt] if req.resume else [req.prompt])

    def parse(self, line: str) -> list[Event]:
        ev = _loads(line)
        if ev is None:
            return []
        t = ev.get("type")
        if t == "thread.started":
            self.thread = ev.get("thread_id")
            return [Event("init", session_id=self.thread, model=self.model)]
        if t == "error":
            msg = str(ev.get("message") or "")
            return [Event("error", text=msg, api_error=True)]
        if t == "turn.failed":
            msg = str((ev.get("error") or {}).get("message") or "")
            return [Event("result", text=msg, subtype="error", session_id=self.thread, is_error=True, api_error=True)]
        if t == "turn.completed":
            total = self._usage_tokens(ev.get("usage") or {})
            prev = self.thread_usage.get(self.thread or "", tokens())
            if self.thread:
                self.thread_usage[self.thread] = total
            toks = {k: max(0, total[k] - prev[k]) for k in TOKEN_KEYS}
            return [Event("result", subtype="success", session_id=self.thread, tokens=toks,
                          cost_usd=codex_cost(self.model, toks))]
        if t not in ("item.started", "item.completed"):
            return []
        item = ev.get("item") or {}
        it, done = item.get("type"), t == "item.completed"
        if it == "agent_message" and done and str(item.get("text") or "").strip():
            return [Event("text", text=item["text"].strip())]
        if it == "mcp_tool_call":
            server, tool = item.get("server"), item.get("tool") or "?"
            name = tool if server == "bg3" else f"{server}.{tool}"
            if not done:
                args = item.get("arguments")
                return [Event("tool_call", tool=name, args=args if isinstance(args, dict) else {"arguments": args},
                              call_id=item.get("id") or "")]
            err = item.get("error") or {}
            body = _json_text((item.get("result") or {}).get("content")) or err.get("message") or ""
            return [Event("tool_result", tool=name, text=body, call_id=item.get("id") or "",
                          is_error=bool(err) or item.get("status") == "failed")]
        if it == "command_execution":   # disabled; shown if a build of codex still runs one
            if not done:
                return [Event("tool_call", tool="shell", args={"command": item.get("command")},
                              call_id=item.get("id") or "")]
            return [Event("tool_result", tool="shell", text=str(item.get("aggregated_output") or ""),
                          call_id=item.get("id") or "", is_error=item.get("exit_code") not in (0, None))]
        if it in ("file_change", "web_search") and done:
            return [Event("tool_call", tool=it, args={k: v for k, v in item.items() if k not in ("id", "type")},
                          call_id=item.get("id") or "")]
        if it == "error" and done:   # a warning item (e.g. no model metadata), not a failed turn
            return [Event("error", text=str(item.get("message") or ""))]
        return []


# ------------------------------------------------------------------------------------------------------- gemini

# USD per million tokens: (input, cached input, output), prompts up to 200k tokens. As of 2026-10-16, from Google's
# public price list as last known to us; recheck before a benchmark series.
GEMINI_PRICES: dict[str, tuple[float, float, float]] = {
    "gemini-2.5-pro": (1.25, 0.31, 10.0),
    "gemini-2.5-flash": (0.30, 0.075, 2.50),
}
# Gemini CLI's built-in tools, all excluded (tools.exclude): only MCP tools are left.
GEMINI_BUILTIN_TOOLS = ("run_shell_command", "read_file", "read_many_files", "write_file", "replace",
                        "list_directory", "glob", "search_file_content", "google_web_search", "web_fetch",
                        "save_memory", "write_todos")


class GeminiClient:
    """`gemini -p ... --output-format stream-json`. UNTESTED: no gemini on the test machine; built from the
    documented CLI (settings.json mcpServers, tools.exclude, GEMINI_CLI_SYSTEM_SETTINGS_PATH, --resume, the
    stream-json events init/message/tool_use/tool_result/error/result). The settings file goes into the session
    dir and is set as the system settings (highest precedence), so ~/.gemini is left alone. Check every flag and
    event shape against the installed version before a live run."""
    name = "gemini"
    supports_resume = True    # --resume <session id>; assumed, check the installed version
    assigns_session = False   # the session id comes in the init event
    no_tools = "every built-in tool in tools.exclude; only mcpServers.bg3 (--allowed-mcp-server-names bg3)"

    def __init__(self, server: dict | None = None, binary: str = "gemini", cwd: Path = PLAY_DIR):
        self.server = server if server is not None else bg3_server()
        self.binary, self.cwd = binary, cwd
        self.model: str | None = None

    def settings_path(self, req: Request) -> Path:
        return Path(req.session_dir) / f"{req.tag or 'session'}.gemini-settings.json"

    def write_config(self, req: Request) -> list[Path]:
        s = self.server
        if is_http(s):   # the container sandbox: the host runs the server with the session env
            bg3 = {"httpUrl": s["url"], "timeout": s.get("timeout") or 110_000, "trust": True}
        else:
            env = {**(s.get("env") or {}), "BG3AI_SERVE_RULES": "1",
                   **{k: str(req.env[k]) for k in session_env_names(req.env)}}
            bg3 = {"command": s["command"], "args": list(s.get("args") or []), "cwd": str(self.cwd), "env": env,
                   "timeout": s.get("timeout") or 110_000, "trust": True}
        settings = {"mcpServers": {"bg3": bg3}, "tools": {"exclude": list(GEMINI_BUILTIN_TOOLS)}}
        path = self.settings_path(req)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=1) + "\n")
        return [path]

    def env(self, req: Request) -> dict:
        return {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(self.settings_path(req))}

    def command(self, req: Request) -> list[str]:
        self.model = req.model
        cmd = [self.binary, "-p", req.prompt, "-m", req.model, "--output-format", "stream-json",
               "--approval-mode", "yolo", "--allowed-mcp-server-names", "bg3"]
        if req.resume:
            if not req.session:
                raise ValueError("gemini resume needs the session id from the first run's init event")
            cmd += ["--resume", req.session]
        return cmd

    def parse(self, line: str) -> list[Event]:
        ev = _loads(line)
        if ev is None:
            return []
        t = ev.get("type")
        if t == "init":
            self.model = ev.get("model") or self.model
            return [Event("init", session_id=ev.get("session_id"), model=self.model)]
        if t == "message" and ev.get("role") == "assistant" and str(ev.get("content") or "").strip():
            return [Event("text", text=str(ev["content"]).strip())]
        if t == "tool_use":
            return [Event("tool_call", tool=bare_tool(ev.get("tool_name") or "?"), args=ev.get("parameters") or {},
                          call_id=ev.get("tool_id") or "")]
        if t == "tool_result":
            err = ev.get("error") or {}
            return [Event("tool_result", text=_json_text(ev.get("output")) or str(err.get("message") or ""),
                          call_id=ev.get("tool_id") or "", is_error=ev.get("status") == "error")]
        if t == "error":
            return [Event("error", text=str(ev.get("message") or ""), api_error=True)]
        if t == "result":
            st = ev.get("stats") or {}
            cached = int(st.get("cached") or 0)
            toks = tokens(int(st.get("input_tokens") or 0) - cached, st.get("output_tokens"), cached)
            price = GEMINI_PRICES.get(self.model or "")
            cost = None if price is None else round(
                (toks["input"] * price[0] + toks["cache_read"] * price[1] + toks["output"] * price[2]) / 1e6, 6)
            err = ev.get("status") == "error"
            return [Event("result", subtype="error" if err else "success", is_error=err,
                          text=str((ev.get("error") or {}).get("message") or ""), duration_ms=st.get("duration_ms"),
                          tokens=toks, cost_usd=cost)]
        return []


def _pi(**kw) -> Client:
    from . import pi_client  # pi_client imports this module
    return pi_client.PiClient(**kw)


CLIENTS = {"claude": ClaudeClient, "codex": CodexClient, "gemini": GeminiClient, "pi": _pi}


def make(name: str, **kw) -> Client:
    try:
        return CLIENTS[name](**kw)
    except KeyError:
        raise ValueError(f"unknown client {name!r} (one of {', '.join(CLIENTS)})") from None


# The container's working dir and HOME (tools/container/Dockerfile.agent).
SANDBOX_CWD = Path("/home/agent")


def play_settings(path: Path = PLAY_DIR / ".claude" / "settings.json") -> dict:
    """play/.claude/settings.json (allow mcp__bg3__*, deny every built-in tool) as a --settings JSON, for a claude
    that runs where play/ isn't its project (the container sandbox)."""
    raw = json.loads(path.read_text())
    raw.pop("$schema", None)
    return raw


def make_http(name: str, url: str, server_timeout: int = 110_000, claude_settings: dict | None = None,
              **kw) -> Client:
    """A client whose only MCP server is the bg3 server at `url` (the container sandbox, bg3harness/sandbox.py). The
    rules of play come from the server (BG3AI_SERVE_RULES=1 on the host side): the container has no play/CLAUDE.md."""
    server = http_server(url, server_timeout)
    if name == "claude":
        return ClaudeClient(mcp_config=json.dumps({"mcpServers": {"bg3": server}}),
                            settings=play_settings() if claude_settings is None else claude_settings, **kw)
    return make(name, server=server, cwd=SANDBOX_CWD, **kw)


def process_env(client: Client, req: Request, base: dict | None = None) -> dict:
    """The play process's env: the runner's (`base`, default os.environ) + the session env + the client's own."""
    return {**(os.environ if base is None else base), **req.env, **client.env(req)}
