# pyright: basic
"""PiClient: any model behind an OpenAI-compatible endpoint plays through pi 1.3.0 (tools/pi) and its bg3.ts
extension, behind the runner's Client interface (clients.py). research/arch/pi-client.md §3.4 is the design and
tools/pi/conformance/ the offline evidence; runner/fixtures/pi/*.jsonl are streams captured from those runs.

What a run gets: a profile (runner/profiles.py) for the endpoint, model and effort; a system text file (D2 in
docs/OVERHAUL-PLAN.md: for now the server's instructions + rules, dumped by write_system_text) that bg3.ts sends
verbatim, so the server's own rules copy is off (BG3AI_SERVE_RULES=0); the bg3 tools as sequential
mcp__bg3__* tools and nothing else. Per session, write_config puts settings.json, models.json and bg3.json in
<session_dir>/<tag>.pi (pi's agent dir), so ~/.pi is never read; the API key stays in the env var the profile names.

The parser's guards (pi's exit code is no signal: it is 0 after a final API error and after a context overflow):
- the result is an error when the last assistant message stopped on an error; api_error only for the API's
  transient or account failures, never for a context overflow (a runner that retries an overflow loops forever);
- an overflow-recovery loop (pi #11161: compaction is off, so it should never start) is cut: after
  MAX_RECOVERY compaction or overflow rounds the parser emits an error result, and the runner stops the process;
- cost_usd is the billed cost (OpenRouter's usage.cost, from bg3.ts's bg3.billing entries) when the endpoint
  reports it, else pi's catalog estimate from the profile's prices;
- the init event's mcp status compares the tools the model got with the tools the server listed (bg3.tools).

    python3 -m unittest runner/test_pi_client.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import clients as C
from .profiles import Profile, ProfileError, models_json

PI_DIR = C.REPO / "tools" / "pi"
EXTENSION = PI_DIR / "bg3.ts"
PI_EFFORTS = {e: e for e in ("low", "medium", "high", "xhigh", "max")}  # --thinking; pi clamps per model
# compaction off: no summary replaces the history (it also stops the compact-and-retry on overflow); cache warming
# off: it sends extra billed requests; no built-in tools; pi's own retries for transient API errors.
PI_SETTINGS = {
    "compaction": {"enabled": False},
    "cacheWarming": "off",
    "enableInstallTelemetry": False,
    "retry": {"enabled": True, "maxRetries": 3},
    "defaultTools": [],
    "quietStartup": True,
}
MAX_RECOVERY = 3
# pi-ai's isContextOverflow wording (OpenAI, OpenRouter, Anthropic, DeepSeek)
OVERFLOW_RE = re.compile(
    r"context.?length|maximum context|context window|prompt is too long|too many tokens|"
    r"exceeds the model's|reduce the length",
    re.IGNORECASE,
)
EXTENSION_FAILED_RE = re.compile(r"Failed to load extension .*bg3\.ts", re.DOTALL)


def write_system_text(path: Path, repo: Path = C.REPO) -> Path:
    """The system text pi gets (D2): the bg3 server's instructions with the rules, dumped offline in the bridge's
    env by tools/pi/dump_server.py."""
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(repo / "bridge"),
            "python",
            str(repo / "tools/pi/dump_server.py"),
            "--system",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return path


def exit_problem(returncode: int | None, stderr: str) -> str | None:
    """Why a pi process that ended before any stream event had no game tools (bg3.ts failed to reach the
    server: pi prints the error and exits 1 with nothing on stdout), for the runner's mcp_failed; else None."""
    if returncode and EXTENSION_FAILED_RE.search(stderr or ""):
        line = next((x for x in stderr.splitlines() if "Failed to load extension" in x), "")
        return f"the bg3 MCP server is unreachable: {line.strip()[:300]}"
    return None


class PiParser:
    """pi --mode json lines -> Events (one parser per process)."""

    def __init__(self, model: str | None = None):
        self.model = model
        self.session: str | None = None
        self.server_tools: int | None = None  # bg3.tools: what the server listed
        self.model_tools: int | None = None  # the system message's toolsAdded: what the model got (None: no
        # system message, a resumed session keeps the declared tools)
        self.init_sent = False
        self.turns = 0
        self.tokens = dict.fromkeys(C.TOKEN_KEYS, 0)
        self.estimate = 0.0
        self.billed: float | None = None
        self.providers: list[str] = []  # served providers, as the endpoint named them
        self.generations: list[str] = []  # response ids (OpenRouter: /api/v1/generation?id=)
        self.malformed = 0  # tool calls whose arguments pi could not parse
        self.recovery = 0  # compaction or overflow rounds
        self.last_stop: str | None = None
        self.last_error = ""
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.done = False

    def mcp_status(self) -> str:
        if self.server_tools is None:
            return "bg3:failed"
        if not self.server_tools or self.model_tools not in (None, self.server_tools):
            return f"bg3:tools {self.model_tools or 0}/{self.server_tools}"
        return "bg3:connected"

    def _init(self) -> list[C.Event]:
        if self.init_sent:
            return []
        self.init_sent = True
        n = self.model_tools if self.model_tools is not None else self.server_tools
        return [C.Event("init", session_id=self.session, model=self.model, tools=n or 0, mcp=[self.mcp_status()])]

    def _stamp(self, msg: dict) -> None:
        ts = msg.get("timestamp")
        if isinstance(ts, (int, float)):
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def _cost(self) -> float:
        return round(self.billed if self.billed is not None else self.estimate, 6)

    def _result(self, subtype: str, text: str, api_error: bool) -> C.Event:
        self.done = True
        duration = None if self.first_ts is None else float(self.last_ts - self.first_ts)
        return C.Event(
            "result",
            subtype=subtype,
            text=text,
            session_id=self.session,
            is_error=subtype != "success",
            api_error=api_error,
            turns=self.turns,
            duration_ms=duration,
            cost_usd=self._cost(),
            tokens=dict(self.tokens),
            model=self.model,
        )

    def _recover(self, what: str) -> list[C.Event]:
        self.recovery += 1
        if self.recovery <= MAX_RECOVERY or self.done:
            return []
        return [
            self._result(
                "error_overflow_loop",
                f"pi started {what} {self.recovery} times (pi #11161): stop the process",
                api_error=False,
            )
        ]

    def _assistant(self, msg: dict) -> list[C.Event]:
        out = self._init()
        self._stamp(msg)
        self.turns += 1
        self.model = msg.get("model") or self.model
        sid = self.session
        for c in msg.get("content") or []:
            if c.get("type") == "text" and str(c.get("text") or "").strip():
                out.append(C.Event("text", text=c["text"].strip(), session_id=sid))
            elif c.get("type") == "toolCall":
                args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
                if c.get("argumentsError"):
                    self.malformed += 1
                    args = {"_malformed": str(c["argumentsError"])}
                out.append(
                    C.Event(
                        "tool_call",
                        tool=C.bare_tool(c.get("name") or "?"),
                        args=args,
                        call_id=c.get("id") or "",
                        session_id=sid,
                    )
                )
        u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        toks = C.tokens(u.get("input"), u.get("output"), u.get("cacheRead"), u.get("cacheWrite"))
        self.tokens = {k: self.tokens[k] + toks[k] for k in C.TOKEN_KEYS}
        self.estimate += float((u.get("cost") or {}).get("total") or 0)
        out.append(
            C.Event("usage", session_id=sid, message_id=msg.get("responseId"), tokens=toks, cost_usd=self._cost())
        )  # the process's running cost (spend caps act on it)
        self.last_stop = msg.get("stopReason")
        if self.last_stop == "error":
            self.last_error = str(msg.get("errorMessage") or "")
            overflow = bool(OVERFLOW_RE.search(self.last_error))
            out.append(
                C.Event(
                    "error",
                    text=self.last_error,
                    session_id=sid,
                    api_error=not overflow and bool(C.API_ERROR_RE.search(self.last_error)),
                )
            )
            if overflow:
                out += self._recover("overflow recovery")
        return out

    def parse(self, line: str) -> list[C.Event]:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(ev, dict):
            return []
        t = ev.get("type")
        if t == "session":
            self.session = ev.get("id")
            return []
        if t == "entry_appended":
            entry = ev.get("entry") or {}
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            if entry.get("customType") == "bg3.tools":
                self.server_tools = int(data.get("n") or 0)
            elif entry.get("customType") == "bg3.billing":
                cost = (data.get("usage") or {}).get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    self.billed = (self.billed or 0.0) + float(cost)
                if data.get("provider"):
                    self.providers.append(str(data["provider"]))
                if data.get("id"):
                    self.generations.append(str(data["id"]))
            return []
        if t == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "system":
                self.model_tools = len(msg.get("toolsAdded") or [])
                return self._init() if self.server_tools is not None else []
            if msg.get("role") == "user":
                self._stamp(msg)
                return []
            if msg.get("role") == "assistant":
                return self._assistant(msg)
            return []
        if t == "tool_execution_end" and not ev.get("parentToolCallId"):
            res = ev.get("result") or {}
            return [
                C.Event(
                    "tool_result",
                    tool=C.bare_tool(ev.get("toolName") or "?"),
                    text=C._json_text(res.get("content")),
                    call_id=ev.get("toolCallId") or "",
                    is_error=bool(ev.get("isError")),
                    duration_ms=ev.get("durationMs"),
                    session_id=self.session,
                )
            ]
        if t == "compaction_start":
            return [C.Event("compact", session_id=self.session), *self._recover("compaction")]
        if t == "auto_retry_start":
            return [
                C.Event(
                    "error",
                    text=f"retry {ev.get('attempt')}/{ev.get('maxAttempts')}: {ev.get('errorMessage') or ''}",
                    session_id=self.session,
                )
            ]
        if t == "agent_settled":
            if self.done:
                return []
            out = self._init()
            if ev.get("aborted"):
                return [*out, self._result("aborted", "", api_error=False)]
            if self.last_stop == "error":
                overflow = bool(OVERFLOW_RE.search(self.last_error))
                return [
                    *out,
                    self._result(
                        "error_context_overflow" if overflow else "error_during_execution",
                        self.last_error,
                        api_error=not overflow and bool(C.API_ERROR_RE.search(self.last_error)),
                    ),
                ]
            return [*out, self._result("success", "", api_error=False)]
        return []


class PiClient:
    """`pi --mode json` with bg3.ts as its only extension (research/arch/pi-client.md §3.4)."""

    name = "pi"
    supports_resume = True  # --session-id opens the session file in the agent dir's sessions/ again
    assigns_session = True  # the runner picks the id; pi also sends it as OpenRouter's sticky-routing key
    no_tools = (
        "-ne (no built-in or discovered extensions: no read/bash/edit/write, MCP, codemode, tool_search) + "
        "-e bg3.ts + --tools 'mcp__bg3__*' + -nc/-ns/-np/-na (no context files, skills, prompts, project "
        "config): only bg3.ts's tools exist"
    )

    def __init__(
        self,
        profile: Profile,
        system_file: Path | str,
        server: dict | None = None,
        cwd: Path = C.PLAY_DIR,
        binary: Path | str | None = None,
        settings: dict | None = None,
        extension: Path | str | None = None,
    ):
        if profile.client != "pi":
            raise ProfileError(f"{profile.source}: PiClient needs a pi profile (client = {profile.client!r})")
        self.p, self.system_file = profile, Path(system_file)
        self.server = server if server is not None else C.bg3_server()
        self.cwd, self.settings = cwd, settings if settings is not None else PI_SETTINGS
        self.binary = Path(binary) if binary else PI_DIR / "node_modules" / ".bin" / "pi"
        self.extension = Path(extension) if extension else EXTENSION  # the container sandbox: the image's copy
        self.parser = PiParser(profile.model)

    def agent_dir(self, req: C.Request) -> Path:
        return Path(req.session_dir) / f"{req.tag or 'session'}.pi"

    def bg3_config(self, req: C.Request) -> dict:
        s = self.server
        if C.is_http(s):  # the container sandbox: the host runs the server with the session env
            server = {"url": s["url"]}
        else:
            env = {
                **(s.get("env") or {}),
                "BG3AI_SERVE_RULES": "0",  # the system text carries the rules (D2)
                **{k: str(req.env[k]) for k in C.session_env_names(req.env)},
            }
            server = {"command": s["command"], "args": list(s.get("args") or []), "cwd": str(self.cwd), "env": env}
        server["timeout"] = (s.get("timeout") or 110_000) / 1000  # bg3.ts takes seconds
        return {"server": server, "systemPromptFile": str(self.system_file)}

    def write_config(self, req: C.Request) -> list[Path]:
        d = self.agent_dir(req)
        d.mkdir(parents=True, exist_ok=True)
        files = {"settings.json": self.settings, "models.json": models_json(self.p), "bg3.json": self.bg3_config(req)}
        for name, obj in files.items():
            (d / name).write_text(json.dumps(obj, indent=1) + "\n")
        return [d / n for n in files]

    def env(self, req: C.Request) -> dict:
        d = self.agent_dir(req)
        return {"PI_CODING_AGENT_DIR": str(d), "BG3_PI_CONFIG": str(d / "bg3.json"), "PI_OFFLINE": "1"}

    def command(self, req: C.Request) -> list[str]:
        if not req.session:
            raise ValueError("pi needs the session id up front (--session-id)")
        if req.model and req.model != self.p.model:
            raise ValueError(f"the request's model {req.model!r} is not the profile's {self.p.model!r}")
        self.parser = PiParser(self.p.model)
        cmd = [
            str(self.binary),
            "--mode",
            "json",
            "-ne",
            "-na",
            "-nc",
            "-ns",
            "-np",
            "--offline",
            "-e",
            str(self.extension),
            "--tools",
            "mcp__bg3__*",
            "--session-dir",
            str(self.agent_dir(req) / "sessions"),
            "--session-id",
            req.session,
            "--model",
            self.p.pi_model,
        ]
        effort = req.effort or self.p.effort
        if effort:
            cmd += ["--thinking", PI_EFFORTS[effort]]
        return [*cmd, req.prompt]

    def parse(self, line: str) -> list[C.Event]:
        return self.parser.parse(line)
