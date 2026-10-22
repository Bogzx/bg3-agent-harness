"""Autoskip (user 2026-10-22: optional, on by default) end to end through the real bridge (bg3ai.server over stdio)
against the fake mod: with the default the server's movie skipper polls the mod's autoskip.status; with the run's
autoskip off ($BG3AI_AUTOSKIP=0, set by play.py --no-autoskip, a scenario's game_settings.autoskip = false or
playthrough.toml [game] autoskip = false) no skipper runs, so no ESCAPE can be sent. The fake mod's status has no
`want`, so the skipper never presses a key here either way."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from bg3harness.testing.fakemod import FakeMod, Ops
from bg3harness.testing.mcp_bridge import bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

pytestmark = pytest.mark.e2e

TIMEOUT_S = 120


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))


async def session(home: Path, env: dict[str, str], polled: threading.Event) -> list[str]:
    def status(ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        polled.set()
        return {"on": True, "now": 0}

    with FakeMod(Ops({"autoskip.status": status}), GameModel(clock=FakeClock())) as fake:
        async with bridge_session(fake.port, home, {"BG3AI_MOVIE_SKIP": "1", **env}) as s:
            assert "gold 100" in await call(s, "get_status")
            if env.get("BG3AI_AUTOSKIP") != "0":
                # the skipper's first poll comes ~0.15 s after the server starts; wait for it, not for a time
                assert await asyncio.to_thread(polled.wait, 60), "the skipper never polled autoskip.status"
            else:
                # a negative: a skipper polls every 0.15 s, so none in 2 s means none runs (never flaky when correct)
                assert not await asyncio.to_thread(polled.wait, 2.0), "a skipper polled with autoskip off"
                await call(s, "get_status")
        return fake.ops("client")


def test_the_skipper_runs_by_default(tmp_path: Path) -> None:
    polled = threading.Event()
    ops = run(session(tmp_path, {}, polled))
    assert polled.is_set() and "autoskip.status" in ops


def test_no_skipper_when_the_run_turns_autoskip_off(tmp_path: Path) -> None:
    polled = threading.Event()
    ops = run(session(tmp_path, {"BG3AI_AUTOSKIP": "0"}, polled))
    assert not polled.is_set() and "autoskip.status" not in ops
    assert "ui.state" in ops  # the reads did reach the client context: an idle skipper would have polled there too
