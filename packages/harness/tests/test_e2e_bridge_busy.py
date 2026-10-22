"""E2E plan 4 (research/ovh/pkg-harness-analysis.md H): the mod's busy-UI answers (Shared/RPC.lua BUSY_BEFORE /
BUSY_AFTER) through the real bridge (bg3ai.server over stdio) against the fake mod. One contract across both sides:
an op refused before its first UI call did nothing, so the bridge asks it again (a press too); a busy answer after a
UI call ran is asked again only for reads and target-state ops (bg3ai.busywait.RETRY_OPS), never for a press, which
may already have landed. Reads folded into one tick (Game.read: get_status) follow the same rule. The busy wait is
real (0.25 s steps), bounded by busywait.BUDGET_S.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod, Reply
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e

TIMEOUT_S = 120
# Shared/RPC.lua's answer, word for word but for the marker
BUSY = "bg3le: UI busy: the game's UI is busy (a rest/travel/load transition); {}; retry in a moment"
BEFORE = BUSY.format("refused before its first UI call, nothing was done")
AFTER = BUSY.format("met it after a UI call ran")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


class Busy:
    """The first `times` calls of `op` get the busy answer `error`; then the next responder answers."""

    def __init__(self, op: str, error: str, times: int = 1):
        self.op, self.error, self.left, self.seen = op, error, times, 0

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        if op != self.op:
            return None
        self.seen += 1
        if self.left <= 0:
            return None
        self.left -= 1
        return {"ok": False, "error": self.error}

    def eval_raw(self, ctx: str, code: str) -> str | None:
        return None


async def choose(home: Path, busy: Busy) -> tuple[str, GameModel]:
    m = GameModel(clock=FakeClock())
    m.add_nearby("Arron")
    with FakeMod(busy, m) as fake:
        async with bridge_session(fake.port, home) as s:
            await call(s, "get_status")
            m.open_dialogue("Arron", "What do you want?", ["Trade.", "No."])
            await call(s, "get_dialogue")
            out = await call(s, "choose_dialogue", {"option": 2})
    return out, m


def test_a_press_refused_before_its_first_ui_call_is_asked_again(tmp_path: Path) -> None:
    busy = Busy("ui.dialogue_choose", BEFORE)
    out, m = run(choose(tmp_path, busy))
    assert busy.seen == 2 and m.chosen == [2]
    assert "conversation ended" in out and "waited 0." in out and "UI busy" not in out


def test_a_press_met_by_busy_after_a_ui_call_is_never_pressed_again(tmp_path: Path) -> None:
    busy = Busy("ui.dialogue_choose", AFTER)
    out, m = run(choose(tmp_path, busy))
    assert busy.seen == 1 and m.chosen == []  # one ask, the busy answer stands
    assert "busy" in out.lower(), out


def test_a_read_met_by_busy_after_a_ui_call_is_asked_again(tmp_path: Path) -> None:
    async def read() -> tuple[str, Busy]:
        busy = Busy("ui.state", AFTER, times=2)
        with FakeMod(busy, GameModel(clock=FakeClock())) as fake:
            async with bridge_session(fake.port, tmp_path) as s:
                await call(s, "get_status")
                busy.left, busy.seen = 2, 0
                return await call(s, "get_status", {"full": True}), busy

    out, busy = run(read())
    # two busy answers, then the read (get_status reads ui.state in its one-tick read(): that path dropped a busy
    # answer as "no UI" without asking again before 2026-10-22)
    assert busy.seen == 3
    assert "gold 100" in out and "UI busy" not in out and "waited 0." in out
