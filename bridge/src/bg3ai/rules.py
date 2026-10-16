"""The play rules for any MCP client, and the server-side session stop.

Claude Code reads play/CLAUDE.md by itself (`claude -p` runs in play/). Any other client (Codex, Gemini CLI, a plain
MCP client) gets the same file from the server instead: in the initialize result's `instructions`, prepended to the
first briefing of a session (session_start, or the first tool result in manual mode; a client that ignores
`instructions` still sees them), and as the `rules` prompt and the bg3://rules resource. play/CLAUDE.md stays the
source of truth; the few Claude-Code-only passages are rewritten at serve time (ADAPT; docs/BENCHMARK-BIAS.md).
$BG3AI_SERVE_RULES=0 turns the instructions and the briefing copy off (play/.mcp.json, for Claude Code).

The stop: the runner stops `claude` at deadline + 10 min. Any client gets the same from the server: after the
deadline + STOP_GRACE_S every tool outside the allowed set (reads, memory, note, save, session tools) is refused
with a pointer to end_session.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RULES_PATH = Path(os.environ.get("BG3AI_RULES_PATH") or REPO / "play" / "CLAUDE.md")
URI = "bg3://rules"

# (pattern, replacement): passages of play/CLAUDE.md that describe Claude Code, not the game or the server. A
# pattern that stops matching (CLAUDE.md was edited) is skipped; test_rules.py fails so it gets updated.
ADAPT: list[tuple[str, str]] = [
    # Claude Code's mcp__bg3__ prefix, its ToolSearch and the alwaysLoad hint.
    (r"Game tools come from the `bg3` MCP server\. \*\*Their real names.*?only when you need one\.",
     "Game tools come from the `bg3` MCP server. This text names them by their bare name (`get_status`, "
     "`travel_to`); your client may show them with a prefix. Every tool is available: call them directly."),
]


def adapt(text: str) -> str:
    for pat, rep in ADAPT:
        text = re.sub(pat, rep, text, count=1, flags=re.S)
    return text


def load(path: Path | None = None) -> str:
    """play/CLAUDE.md as served to a client ("" when unreadable: the server still runs, without rules)."""
    try:
        return adapt((path or RULES_PATH).read_text(encoding="utf-8")).strip()
    except OSError:
        return ""


def serve_enabled() -> bool:
    """Rules in `instructions` and the briefing: off for Claude Code, which loads play/CLAUDE.md itself."""
    return os.environ.get("BG3AI_SERVE_RULES", "1").strip().lower() not in ("0", "false", "no", "off")


def instructions(base: str, rules: str) -> str:
    return f"{base}\n\n=== RULES (play/CLAUDE.md) ===\n{rules}" if rules and serve_enabled() else base


class Once:
    """The rules ride on the first briefing of each session (key: the memory session id), not on every repeat."""

    def __init__(self, rules: str):
        self.rules = rules
        self.sent_for: str | None = None

    def header(self) -> str:
        return (f"=== RULES (play/CLAUDE.md, once per session; session_start(rules=true), the `rules` prompt or "
                f"{URI} give them again) ===\n{self.rules}\n=== end of rules ===")

    def take(self, key: str, force: bool = False) -> str:
        if not self.rules or not (force or serve_enabled()):
            return ""
        if not force and self.sent_for == key:
            return ""
        self.sent_for = key
        return self.header()

    def wrap(self, briefing: str, key: str, force: bool = False) -> str:
        r = self.take(key, force)
        return f"{r}\n\n{briefing}" if r else briefing


# Server-side stop. The runner's hard stop is at deadline + 10 min: the server closes the game earlier, so the
# agent reads the refusal and ends the session itself.
try:
    STOP_GRACE_S = float(os.environ.get("BG3AI_STOP_GRACE_S") or 300)
except ValueError:
    STOP_GRACE_S = 300.0

# Besides the read tools: what the wrap-up needs.
AFTER_STOP = frozenset({"note", "save_game", "list_saves", "session_start", "end_session", "session_clock",
                        "memory_list", "memory_read", "memory_search", "memory_write", "memory_edit",
                        "memory_append"})


def stop_refusal(tool: str, deadline: float, reads: frozenset[str] | set[str] = frozenset(),
                 now: float | None = None, grace: float | None = None) -> str:
    """The refusal for `tool` once the session is past deadline + grace ("" = allowed, or no deadline)."""
    if not deadline or tool in AFTER_STOP or tool in reads:
        return ""
    now = time.time() if now is None else now
    grace = STOP_GRACE_S if grace is None else grace
    if now <= deadline + grace:
        return ""
    over = int((now - deadline) // 60)
    return (f"REFUSED: the session ended {over} min ago ({time.strftime('%H:%M', time.localtime(deadline))}); "
            f"game actions are closed. Reads, memory tools, note and save_game still work. Finish the wrap-up "
            f"(save_game Sxxx-end if not saved, HANDOFF.md) and call end_session now.")
