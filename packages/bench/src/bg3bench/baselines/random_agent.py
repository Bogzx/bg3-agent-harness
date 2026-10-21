# pyright: basic
"""The random-action baseline: look, pick a random action on a random entity in view, repeat until the deadline.

    python -m bg3bench.baselines.random_agent --mcp-config play/.mcp.json --seed 7 [--deadline EPOCH]

No model and no network beyond the MCP server. Given the same tool results, the same seed gives the same calls.
It calls only the game-action tools of ACTIONS (plus look, session_start, end_session): never load or save a
game, memory, notes, the wiki, raw clicks or keys. "Legal" is whatever the tool accepts; refusals go in the log.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Callable, Sequence

from bg3bench.baselines.agent import Player, Sleep, ToolSession, parser, serve, wall_clock

AGENT = "random"
OBSERVE = "look"
# Each action and the handle argument it takes (None: no handle).
ACTIONS: dict[str, str | None] = {
    "move_to": "target",
    "interact": "target",
    "talk_to": "npc",
    "pick_up": "item",
    "loot": None,
    "walk_toward": None,
    "choose_dialogue": None,
    "dialogue_advance": None,
    "combat_turn": "plan",
    "end_turn": None,
}
ALLOWED = (*ACTIONS, OBSERVE, "session_start", "end_session")
DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
HANDLE_RE = re.compile(r"\be(\d+)\b")  # the entity ids results show (runner/fairness.py _HANDLE)
PERIOD_S = 1.0


def handles(text: str) -> list[str]:
    return [f"e{n}" for n in sorted({int(m) for m in HANDLE_RE.findall(text)})]


def choose(rng: random.Random, actions: Sequence[str], seen: Sequence[str]) -> tuple[str, dict]:
    """One random action and its arguments. A handle action with nothing in view becomes a walk."""
    tool = rng.choice(list(actions))
    arg = ACTIONS[tool]
    if arg is not None and not seen:
        tool, arg = "walk_toward", None
    if tool == "walk_toward":
        return tool, {"direction": rng.choice(DIRECTIONS)}
    if tool == "choose_dialogue":
        return tool, {"option": rng.randint(1, 4)}
    if arg == "plan":
        h = rng.choice(list(seen))
        plan = {"move": h, "action": [{"ability": "Target_MainHandAttack", "target": h}]}
        return tool, {"plan": json.dumps(plan, separators=(",", ":"))}
    if arg is not None:
        return tool, {arg: rng.choice(list(seen))}
    return tool, {}


async def play(
    session: ToolSession,
    player: Player,
    deadline: float,
    *,
    seed: int,
    session_start: bool = True,
    now: Callable[[], float] = wall_clock,
    sleep: Sleep = asyncio.sleep,
    period_s: float = PERIOD_S,
    max_steps: int | None = None,
) -> None:
    t0 = now()
    rng = random.Random(seed)
    tools = set(await session.list_tools())
    actions = [a for a in ACTIONS if a in tools]
    if "walk_toward" not in tools:
        raise RuntimeError("the server has no walk_toward: not the bg3 server?")
    player.emit({"type": "init", "agent": AGENT, "seed": seed, "tools": len(tools)})
    if session_start and "session_start" in tools:
        await player.call("session_start")
    steps = 0
    while now() < deadline and (max_steps is None or steps < max_steps):
        seen = handles((await player.call(OBSERVE))[0]) if OBSERVE in tools else []
        await player.call(*choose(rng, actions, seen))
        steps += 1
        await sleep(period_s)
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
    ap = parser((__doc__ or "").splitlines()[0])
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--max-steps", type=int, help="stop after this many actions (smoke tests)")
    ap.add_argument("--period-s", type=float, default=PERIOD_S, help="seconds between actions")
    args = ap.parse_args(argv)
    return serve(
        lambda s, p, d: play(
            s,
            p,
            d,
            seed=args.seed,
            session_start=not args.no_session_start,
            now=time.time,
            max_steps=args.max_steps,
            period_s=args.period_s,
        ),
        args,
        ALLOWED,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
