"""E2E (fight-flow research 2026-11-02, D291): a scripted fight played one action per call through the real bridge
(bg3ai.server over stdio) against the scripted fake mod. Each result shows what its action changed before the next
call: the walk's arrival (no job id), the attack's kill in CHANGES, end_turn's hand-over (with the next member's
actions) after the enemy's hit; a batch in combat runs nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e

TIMEOUT_S = 180


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


async def fight(home: Path) -> tuple[dict[str, str], list[str]]:
    m = GameModel(clock=FakeClock())
    out: dict[str, str] = {}
    with FakeMod(m) as fake:
        async with bridge_session(fake.port, home) as s:
            await call(s, "get_status")  # the first call syncs the event cursor and serves the rules once
            m.start_fight(["Goblin", "Goblin Archer"], nearby=True)
            out["state"] = await call(s, "get_combat_state")
            gob = re.search(r"\b(e\d+) Goblin enemy\b", out["state"])
            assert gob, out["state"]
            k0 = len(fake.calls)
            out["batch"] = await call(
                s,
                "do_actions",
                {"actions": [{"tool": "use_ability", "ability": "Target_MainHandAttack", "target": gob.group(1)}]},
            )
            batch_ops = [op for _, op, _ in fake.calls[k0:] if op.startswith("act.") and op != "act.watch"]
            out["move"] = await call(s, "move_to", {"target": gob.group(1), "wait": False})
            out["attack"] = await call(s, "use_ability", {"ability": "Target_MainHandAttack", "target": gob.group(1)})
            out["end"] = await call(s, "end_turn", {})
            out["end2"] = await call(s, "end_turn", {"who": "Shadowheart"})
    return out, batch_ops


def test_a_fight_one_action_per_call(tmp_path: Path) -> None:
    out, batch_ops = run(fight(tmp_path / "home"))
    assert "REFUSED: in combat do_actions runs no steps" in out["batch"], out["batch"]
    assert batch_ops == []
    assert out["move"].startswith("move_to: done"), out["move"]
    assert not re.search(r"\bj\d+\b", out["move"].split("\n", 1)[0]), out["move"]  # the arrival, not a job id
    assert "movement=" in out["move"], out["move"]
    attack, _, changes = out["attack"].partition("\nCHANGES\n")
    assert attack.startswith("use_ability: done") and "action=0" in attack, out["attack"]
    assert re.search(r"e\d+ Goblin DEAD", changes), out["attack"]
    assert out["end"].startswith("end_turn e1 Tav: done") and "\nnow e2's turn" in out["end"], out["end"]
    assert "\nYOUR ACTIONS (e2)" in out["end"] and "Healing Word" in out["end"], out["end"]  # her hotbar at once
    end2, _, events = out["end2"].partition("\nEVENTS\n")
    assert end2.startswith("end_turn e2 Shadowheart: done") and "\nnow e1's turn" in end2, out["end2"]
    assert "Goblin Archer" in events and "3 Piercing" in events, out["end2"]  # the enemy's turn, before ours
