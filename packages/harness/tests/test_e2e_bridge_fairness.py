"""E2E plan 5 (research/ovh/pkg-harness-analysis.md H): a per-tool fairness sweep through the real bridge (bg3ai.server
over stdio) against the scripted fake mod. What the mod hands the bridge carries engine facts a player never sees:
UUIDs, level ids (WLD_Main_A), and the harness-only rings (ref.*, roll.*, scn.*: bg3ai/referee.py). Every read tool
the server lists (a `get_*` tool callable with no arguments) is called in three game states (exploring, a fight, a
dialogue after an area change) and its text must show none of them. The mod-side filter (what is visible at all) is
tools/dev/test_fairness_lua; this pins the bridge's rendering of each tool.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e

TIMEOUT_S = 240
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
LEVEL_ID = re.compile(r"\b(?:WLD|TUT|SCL|CTY|BGO|CRE|INT|IRN|UND|GOB)_[A-Za-z]+_[A-Z]\b")
HIDDEN_OPS = ("ref.", "roll.", "scn.")
# get_* tools that need an argument or act outside the game state (a doc page, a file of the run)
SKIP = frozenset({"get_character_sheet_page"})


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def leaks(text: str) -> list[str]:
    return [m.group(0) for m in UUID.finditer(text)] + [m.group(0) for m in LEVEL_ID.finditer(text)]


async def sweep(home: Path) -> tuple[dict[str, list[str]], list[str], list[str]]:
    m = GameModel(clock=FakeClock())
    m.add_nearby("Arron", can_trade=True)
    m.add_nearby("Iron Chest", item=True, dist=3.0)
    m.add_quest("Find the Nightsong", "Look for her in the Shadow-Cursed Lands.")
    m.referee("item_added", item="Ring of Protection")  # a referee record: the harness's, never the agent's
    found: dict[str, list[str]] = {}
    called: list[str] = []
    with FakeMod(m) as fake:
        async with bridge_session(fake.port, home) as s:
            tools = sorted(
                t.name
                for t in (await s.list_tools()).tools
                if t.name.startswith("get_") and t.name not in SKIP and not (t.input_schema or {}).get("required")
            )
            states = [
                lambda: None,
                lambda: m.start_fight(["Goblin", "Goblin Archer"]),
                lambda: (m.end_fight(), m.change_area("Blighted Village", "WLD_Main_A"),
                         m.open_dialogue("Arron", "What do you want?", ["Trade.", "No."])),
            ]  # fmt: skip
            for change in states:
                change()
                for tool in tools:
                    res = await s.call_tool(tool, {})
                    text = "\n".join(getattr(c, "text", "") for c in res.content)
                    called.append(tool)
                    if bad := leaks(text):
                        found.setdefault(tool, []).extend(bad)
        hidden = [op for op in fake.ops() if op.startswith(HIDDEN_OPS)]
        raw = json.dumps(m.op_status("server", {}))  # what the mod handed the bridge
    assert UUID.search(raw) and "WLD_Main_A" in raw, "the control: the mod's replies do carry engine ids"
    return found, called, hidden


def test_no_read_tool_shows_engine_ids_or_reaches_a_hidden_ring(tmp_path: Path) -> None:
    found, called, hidden = run(sweep(tmp_path))
    assert len(set(called)) >= 10, sorted(set(called))  # the sweep really covers the read tools
    assert not hidden, f"an agent tool reached a harness-only ring: {sorted(set(hidden))}"
    assert not found, f"engine ids in tool text: {found}"
