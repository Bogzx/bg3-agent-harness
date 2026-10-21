"""The real bridge (bg3ai-mcp, a subprocess over stdio) against the fake mod: tool calls end to end through an MCP
client while the scripted game changes between them (gold, an item, a fight, a dialogue, an area change). The same
session is recorded with BG3AI_RPC_RECORD and a second bridge replays it from the file alone."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.fixtures import FixtureStore
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.integration

TIMEOUT_S = 180
# Ops no agent tool may reach (bg3ai/referee.py: the harness reads them, never the agent).
HIDDEN = ("ref.", "roll.", "scn.")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


def game() -> GameModel:
    m = GameModel(clock=FakeClock())
    m.add_nearby("Arron", can_trade=True)
    m.add_quest("Find the Nightsong", "Look for her in the Shadow-Cursed Lands.")
    return m


def test_tools_end_to_end(tmp_path: Path) -> None:
    model = game()
    record = tmp_path / "session.jsonl"
    out: dict[str, str] = {}

    async def session() -> None:
        with FakeMod(model) as fake:
            async with bridge_session(fake.port, tmp_path / "home", {"BG3AI_RPC_RECORD": str(record)}) as s:
                tools = {t.name for t in (await s.list_tools()).tools}
                assert {"get_status", "get_events", "choose_dialogue", "get_combat_state"} <= tools

                out["status"] = await call(s, "get_status")
                out["party"] = await call(s, "get_party")
                out["character"] = await call(s, "get_character")
                out["inventory"] = await call(s, "get_inventory")
                out["nearby"] = await call(s, "get_nearby")
                out["journal"] = await call(s, "get_journal")

                model.add_gold(25)
                model.add_item("Ring of Protection", value=300)
                out["events_loot"] = await call(s, "get_events")
                out["inventory2"] = await call(s, "get_inventory", {"filter": "all"})

                model.start_fight(["Goblin", "Goblin Archer"])
                out["combat"] = await call(s, "get_combat_state")
                out["status_fight"] = await call(s, "get_status")
                model.kill("Goblin")
                model.end_fight()
                out["events_fight"] = await call(s, "get_events")

                model.open_dialogue("Arron", "What do you want?", ["Trade.", "Nothing."])
                out["dialogue"] = await call(s, "get_dialogue")
                out["choose"] = await call(s, "choose_dialogue", {"option": 2})

                model.change_area("Blighted Village", "WLD_Main_A")
                out["events_area"] = await call(s, "get_events")
                out["status_area"] = await call(s, "get_status")
                out["clock"] = await call(s, "session_clock")
            assert not [op for op in fake.ops() if op.startswith(HIDDEN)], "an agent tool reached a hidden ring"

    run(session())

    assert "gold 100" in out["status"] and "Tav Fighter1 HP 12/12" in out["status"]
    assert "Shadowheart Cleric1 HP 10/10" in out["party"]
    assert "ac=16" in out["character"]
    assert "Longsword" in out["inventory"] and "Potion of Healing x2" in out["inventory"]
    assert "Arron" in out["nearby"] and "can_talk" in out["nearby"]
    assert "Find the Nightsong" in out["journal"]
    assert "Ring of Protection" in out["events_loot"]
    assert "GOLD 125" in out["inventory2"] and "Ring of Protection" in out["inventory2"]
    assert "COMBAT round=1" in out["combat"] and "Goblin Archer" in out["combat"]
    # a later get_status reports what changed since the last one
    assert "MODE exploring -> COMBAT turn-based (round 1" in out["status_fight"]
    assert "GOLD 100->125" in out["status_fight"]
    assert "died" in out["events_fight"] and "Goblin" in out["events_fight"]
    assert "combat_ended" in out["events_fight"]
    assert "DIALOGUE Arron: What do you want?" in out["dialogue"] and "2. Nothing." in out["dialogue"]
    assert "conversation ended" in out["choose"] and model.chosen == [2]
    assert "Blighted Village" in out["events_area"]
    assert "Blighted Village" in out["status_area"] and "exploring" in out["status_area"], out["status_area"]
    assert "No session deadline set." in out["clock"]

    # The recorder: one line per eval, the scripted replies in it, no absolute path of this machine.
    text = record.read_text()
    lines = [json.loads(t) for t in text.splitlines()]
    ops = {c["op"] for line in lines for c in line.get("calls", [])}
    assert {"state.status", "state.inventory", "state.combat", "ui.state", "ui.dialogue_choose"} <= ops
    assert str(tmp_path) not in text and "/home/" not in text

    # Replay: a second bridge against the recorded file alone answers the first reads the same way.
    async def replay() -> dict[str, str]:
        with FakeMod(FixtureStore.load(record)) as fake:
            async with bridge_session(fake.port, tmp_path / "home2") as s:
                return {
                    "status": await call(s, "get_status"),
                    "inventory": await call(s, "get_inventory"),
                    "journal": await call(s, "get_journal"),
                }

    again = run(replay())
    assert again == {"status": out["status"], "inventory": out["inventory"], "journal": out["journal"]}
