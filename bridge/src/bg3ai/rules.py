"""The play rules for any MCP client, and the server-side session stop.

The rules are data files in packages/harness/prompts (D3 in docs/OVERHAUL-PLAN.md), assembled per condition by its
assemble.py: "rules" (how the game, the tools, fairness and the clock line work; client-neutral) or
"rules+coaching" (plus how to play well and the playthrough's memory protocol). Claude Code reads play/CLAUDE.md by
itself (`claude -p` runs in play/), the generated rules+coaching text for the claude client. Any other client gets
assemble(default_condition()) from the server: in the initialize result's `instructions`,
prepended to the first briefing of a session (session_start, or the first tool result in manual mode; a client that
ignores `instructions` still sees them), and as the `rules` prompt and the bg3://rules resource.
$BG3AI_RULES_PATH serves that file verbatim instead. $BG3AI_SERVE_RULES=0 turns the instructions and the briefing
copy off (play/.mcp.json for Claude Code; pi, whose system text carries the rules: D2).

The stop: the runner stops `claude` at deadline + 10 min. Any client gets the same from the server: after the
deadline + STOP_GRACE_S every tool outside the allowed set (reads, memory, note, save, session tools) is refused
with a pointer to end_session.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
ASSEMBLE = REPO / "packages" / "harness" / "prompts" / "assemble.py"
URI = "bg3://rules"


def prompts(path: Path = ASSEMBLE) -> ModuleType:
    """packages/harness/prompts/assemble.py (standard library only), loaded by path: the bridge doesn't depend on
    the harness package."""
    spec = importlib.util.spec_from_file_location("bg3_prompts_assemble", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load(path: Path | None = None, condition: str | None = None, off: frozenset[str] | set[str] = frozenset()) -> str:
    """The rules a client is served: `path` (or $BG3AI_RULES_PATH) verbatim, else the assembled text of `condition`
    (or default_condition()). "" when unreadable: the server still runs, without rules.
    `off`: the tools the session's condition removed (capabilities.py); a rules-only text leaves out the rules of
    a group with a tool off. The coaching conditions are the playthrough's text with every group (assemble.py)."""
    path = path or (Path(p) if (p := os.environ.get("BG3AI_RULES_PATH")) else None)
    try:
        if path is not None:
            return path.read_text(encoding="utf-8").strip()
        mod = prompts()
        cond = condition or default_condition()
        caps = mod.ALL_CAPS
        if not mod.CONDITIONS.get(cond):
            caps = frozenset(c for c, names in mod.CAPABILITIES.items() if not set(names) & set(off))
        return mod.assemble(cond, caps=caps).text.strip()
    except OSError:
        return ""


def default_condition(env: Mapping[str, str] | None = None) -> str:
    """$BG3AI_PROMPT_CONDITION, which every runner session sets. Unset: "rules", except with $BG3AI_SERVE_RULES=0
    (play/.mcp.json): that is Claude Code in play/ without the runner, a manual playthrough, which reads
    play/CLAUDE.md (rules+coaching), so the server's nudges come with it."""
    env = os.environ if env is None else env
    if cond := env.get("BG3AI_PROMPT_CONDITION"):
        return cond
    return "rules" if serve_enabled(env) else "rules+coaching"


def coaching(env: Mapping[str, str] | None = None) -> bool:
    """The session's condition has coaching (default_condition: rules+coaching): only then do results carry the
    coaching nudges (NEXT:, PACE:, NOTES WINDOW). Rules-only (benchmark ranked runs) gets none."""
    return "coaching" in default_condition(env).strip().lower().split("+")


def serve_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Rules in `instructions` and the briefing: off for Claude Code, which loads play/CLAUDE.md itself."""
    val = (os.environ if env is None else env).get("BG3AI_SERVE_RULES", "1")
    return val.strip().lower() not in ("0", "false", "no", "off")


def instructions(base: str, rules: str) -> str:
    return f"{base}\n\n=== RULES ===\n{rules}" if rules and serve_enabled() else base


class Once:
    """The rules ride on the first briefing of each session (key: the memory session id), not on every repeat."""

    def __init__(self, rules: str):
        self.rules = rules
        self.sent_for: str | None = None

    def header(self) -> str:
        return (f"=== RULES (once per session; session_start(rules=true), the `rules` prompt or "
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
                 now: float | None = None, grace: float | None = None,
                 off: frozenset[str] | set[str] = frozenset()) -> str:
    """The refusal for `tool` once the session is past deadline + grace ("" = allowed, or no deadline). It names
    only the tools the session has: `off` are the ones its condition switched off (capabilities.py)."""
    if not deadline or tool in AFTER_STOP or tool in reads:
        return ""
    now = time.time() if now is None else now
    grace = STOP_GRACE_S if grace is None else grace
    if now <= deadline + grace:
        return ""
    over = int((now - deadline) // 60)
    memory, saves = "end_session" not in off, "save_game" not in off
    works = ", ".join(["Reads"] + ["memory tools", "note"] * memory + ["save_game"] * saves)
    if memory:
        finish = (f"Finish the wrap-up ({'save_game Sxxx-end if not saved, ' * saves}HANDOFF.md) and call "
                  f"end_session now.")
    else:
        finish = f"{'Save with save_game if you want, then stop' if saves else 'Stop'} now: no more tool calls."
    return (f"REFUSED: the session ended {over} min ago ({time.strftime('%H:%M', time.localtime(deadline))}); "
            f"game actions are closed. {works} still work. {finish}")
