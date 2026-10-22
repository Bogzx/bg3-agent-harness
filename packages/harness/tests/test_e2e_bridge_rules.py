"""E2E plan 1 (research/ovh/pkg-harness-analysis.md H): the rules the real bridge serves per condition, and the
wrap-up clock line. The bridge is a subprocess (bg3ai.server over stdio) against the fake mod.

B1: a missing rules file used to serve "" and the run went on, scored as valid; now the server doesn't start.
B3: rules-only sessions got "follow knowledge/playbooks/session-wrapup.md" in every result during wrap-up.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3ai import rules  # pyright: ignore[reportMissingTypeStubs] - the bridge ships no py.typed
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.e2e

TIMEOUT_S = 120


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def wrapping_up() -> dict[str, str]:
    """A session 4 min from its deadline with a 10 min wrap-up: every result is in wrap-up."""
    return {"BG3AI_SESSION_DEADLINE": str(time.time() + 4 * 60 + 30), "BG3AI_SESSION_WRAPUP": "600"}


async def served(tmp: Path, env: dict[str, str]) -> dict[str, str]:
    with FakeMod(GameModel(clock=FakeClock())) as fake:
        async with bridge_session(fake.port, tmp, env) as s:
            res = await s.read_resource(rules.URI)  # pyright: ignore[reportArgumentType]
            return {
                "rules": "\n".join(getattr(c, "text", "") for c in res.contents),
                "status": await call(s, "get_status"),
            }


@pytest.mark.parametrize("condition", ["rules", "rules+coaching"])
def test_each_condition_serves_its_assembled_rules(tmp_path: Path, condition: str) -> None:
    out = run(served(tmp_path, {"BG3AI_PROMPT_CONDITION": condition, **wrapping_up()}))
    want = rules.prompts().assemble(condition).text.strip()
    assert len(want) > 1000
    assert out["rules"] == want
    clock = [ln for ln in out["status"].splitlines() if ln.startswith("⏱")]
    assert len(clock) == 1, out["status"]
    assert re.fullmatch(r"⏱ \d\d:\d\d · 4m left: WRAP UP NOW\..*", clock[0]), clock[0]
    if condition == "rules":
        # B3: the ranked condition names no playbook and no tool its caps may have switched off
        assert clock[0].endswith("WRAP UP NOW."), clock[0]
        assert "playbook" not in out["status"]
    else:
        assert "knowledge/playbooks/session-wrapup.md" in clock[0]


def test_missing_rules_file_fails_the_start(tmp_path: Path) -> None:
    """B1: the server exits before the client's initialize completes; a runner records the run as invalid."""
    with pytest.RaisesGroup(pytest.RaisesExc(MCPError, match="Connection closed"), flatten_subgroups=True):
        run(served(tmp_path, {"BG3AI_RULES_PATH": str(tmp_path / "no-such-rules.md")}))


def test_missing_rules_file_is_fine_when_the_server_does_not_serve_rules(tmp_path: Path) -> None:
    """Claude Code in play/ ($BG3AI_SERVE_RULES=0) reads play/CLAUDE.md: the server starts and says so."""
    env = {"BG3AI_RULES_PATH": str(tmp_path / "no-such-rules.md"), "BG3AI_SERVE_RULES": "0"}
    out = run(served(tmp_path, env))
    assert out["rules"].startswith("No rules file found")
    assert "gold 100" in out["status"]
