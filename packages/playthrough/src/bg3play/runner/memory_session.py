"""The playthrough memory session's pure parts (moved from runner/legacy_playthrough.py, Phase D.2): the clock env
session.py, the agent and the bg3 MCP server read, session.py start's and stop's answers, the HANDOFF's session.
The legacy Runner (memory_side.MemorySide) and the app's HarnessRunner (adapters/harness_runner.py) both use them."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from bg3harness.runner import policy as rpolicy

from ..memory import memlib


def parse_handoff_session(text: str) -> int | None:
    """The session that wrote HANDOFF.md: N of its '# Handoff SN → SN+1' heading (memlib.HANDOFF_RE), or None."""
    m = memlib.HANDOFF_RE.search(re.sub(r"<!--.*?-->", "", text, flags=re.S))
    return int(m.group(1)) if m else None


def clock_env(deadline: float, minutes: float, keeper: bool) -> dict[str, str]:
    """The session clock session.py, the agent and the bg3 MCP server read: the deadline, WRAP UP's start, the
    session's length and its wrap-up minute, whether a keeper pass follows."""
    return {
        "BG3AI_SESSION_DEADLINE": str(int(deadline)),
        "BG3AI_SESSION_WRAPUP": str(int(deadline - rpolicy.wrapup_minutes(minutes) * 60)),
        "BG3_SESSION_MINUTES": str(minutes),
        "BG3_WRAP_MINUTES": str(rpolicy.wrap_minutes(minutes)),
        "BG3AI_KEEPER": "1" if keeper else "0",
    }


def stop_reason(stdout: str) -> tuple[str | None, str | None]:
    """session.py stop's answer -> (why the session can't end yet or None, its systemMessage). ValueError: not JSON."""
    raw: object = json.loads(stdout) if stdout.strip() else {}
    if not isinstance(raw, dict):
        return None, None
    out = cast(dict[str, Any], raw)
    reason = out.get("reason") if out.get("decision") == "block" else None
    message = out.get("systemMessage")
    return (str(reason) if reason else None), (str(message) if message else None)


def opened_sid(returncode: int, state: dict[str, Any]) -> str | None:
    """The session session.py start opened (memory/.session.json open with a current number), else None."""
    if returncode != 0 or not state.get("open") or not state.get("current"):
        return None
    return rpolicy.session_id(int(state["current"]))
