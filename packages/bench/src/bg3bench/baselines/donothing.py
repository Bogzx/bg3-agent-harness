"""The do-nothing baseline: start the session, wait for the deadline, end the session. No game action at all.

    python -m bg3bench.baselines.donothing --mcp-config play/.mcp.json [--deadline EPOCH]

It costs wall time only, so it runs to the scenario's real cap (a `survive T` goal depends on it). A scenario it
passes in 5% or more of its runs fails the baseline rule (research/arch/bench.md 2.5).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence

from bg3bench.baselines.agent import Player, Sleep, ToolSession, parser, serve, wall_clock

AGENT = "donothing"
ALLOWED = ("session_start", "end_session")
TICK_S = 30.0


async def play(
    session: ToolSession,
    player: Player,
    deadline: float,
    *,
    session_start: bool = True,
    now: Callable[[], float] = wall_clock,
    sleep: Sleep = asyncio.sleep,
) -> None:
    t0 = now()
    tools = set(await session.list_tools())
    player.emit({"type": "init", "agent": AGENT, "seed": None, "tools": len(tools)})
    if session_start and "session_start" in tools:
        await player.call("session_start")
    while (left := deadline - now()) > 0:
        await sleep(min(TICK_S, left))
    if "end_session" in tools:
        await player.call("end_session")
    player.emit(
        {
            "type": "result",
            "subtype": "success",
            "calls": player.calls,
            "duration_ms": (now() - t0) * 1000,
            "cost_usd": 0.0,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - process entry
    args = parser(__doc__.splitlines()[0]).parse_args(argv)
    return serve(lambda s, p, d: play(s, p, d, session_start=not args.no_session_start, now=time.time), args, ALLOWED)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
