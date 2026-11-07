# pyright: basic
"""A side's scripted client: plays a match through its side's real bg3 MCP server with no model, at $0.

`play.py arena --a scripted --b scripted` gives a side this client instead of a pi profile (SCRIPTED, the reserved
profile name). The process (`python -m bg3harness.arena.scripted_client --mcp-config <file> --session-id <id>`)
starts the bg3 server of the config as a client would (stdio, its env on top of the process's: the side's
BG3AI_SIDE, BG3AI_TOOLS_OFF, ...), and prints claude's stream-json (init, each tool call and result, a result with
cost 0), so the runner reads it like any client: the side's events file, its metrics, its model time (the gaps
between a result and the next call: next to none).

It plays one own member at a time, the same way every time:

1. `wait_for(target="my_turn")`, then `get_combat_state`.
2. The first standing party member in the read's turn group (the side's acting phase) acts: the nearest standing
   enemy in the read (by the rows' positions) is attacked with ATTACK (`use_ability`, UNARMED when the member has
   no ATTACK) when within REACH_M; else the member walks to it (`move_to target`, within melee reach) and attacks
   when the next read puts it within REACH_M. A body in the way can stop that walk short with metres left, and the
   game refuses a swing at the edge of reach (live s5-rx-2 capped on both). So a member that walked and stands within
   SHOOT_M, or whose swing was refused as too far, uses RANGED instead.
3. `end_turn(who=<member>, wait=false)`; back to 1.

Not in combat, or no own member to act: it waits POLL_S and reads again. It never ends on its own: the runner stops
it when the match ends (SIGINT).
"""

from __future__ import annotations

import math
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bg3harness import clients
from bg3harness.testing.reload_agent import Mcp, Turn, arg, emit, server_config

SCRIPTED = "scripted"  # the profile name play.py arena reserves for this client
PLAYER = "scripted/none"  # the player name a scripted side is recorded under (bg3bench never rates it)
VERSION = "0.0.0 (bg3harness.arena.scripted_client: scripted, no model)"
ATTACK, UNARMED, RANGED = "Main Hand Attack", "Unarmed Strike", "Ranged Attack"
REACH_M = 2.0  # centre to centre: within this the member attacks without walking (GuardCast.lua: 1.5 m + radii, 1.998)
SHOOT_M = 15.0  # the shortest Ranged Attack in the builds
TOO_FAR = ("target is out of range", "isn't a valid target")  # the guard's and the game's words at the edge of reach
WITHIN_M = 1.5  # move_to's `within` for a target: stop inside melee reach
POLL_S = 1.0
TURN_WAIT_S = 60  # wait_for's longest wait
ROW = re.compile(r"^  (?P<h>e\d+) (?P<name>.+?) (?P<rel>party|hostile|enemy|ally|neutral)\b(?P<rest>.*)$")
POS = re.compile(r"@\((-?[\d.]+),(-?[\d.]+),(-?[\d.]+)\)")
GROUP = re.compile(r"\bturn_group=\[([^\]]*)\]")


@dataclass(frozen=True)
class Row:
    handle: str
    name: str
    relation: str
    pos: tuple[float, float, float] | None
    standing: bool  # not downed, not dead, HP above 0


@dataclass(frozen=True)
class View:
    rows: list[Row]
    turn_group: list[str]  # handles


def parse_combat(text: str) -> View | None:
    """get_combat_state's text as rows and the turn group; None when it isn't a fight (Not in combat)."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("COMBAT"):
        return None
    group = GROUP.search(lines[0])
    rows = []
    for line in lines[1:]:
        m = ROW.match(line)
        if m is None:
            continue
        rest = m["rest"]
        p = POS.search(rest)
        words = set(rest.split())
        hp0 = re.search(r"\bhp 0%", rest) is not None
        rows.append(
            Row(
                m["h"],
                m["name"],
                m["rel"],
                (float(p[1]), float(p[2]), float(p[3])) if p else None,
                not ({"downed", "dead"} & words) and not hp0,
            )
        )
    handles = [h.strip() for h in group[1].split(",") if h.strip()] if group else []
    return View(rows, handles)


def actor(view: View) -> Row | None:
    """The first standing party member in the turn group (the side's acting phase), in the group's order."""
    party = {r.handle: r for r in view.rows if r.relation == "party" and r.standing}
    return next((party[h] for h in view.turn_group if h in party), None)


def nearest(view: View, me: Row) -> tuple[Row, float] | None:
    """The nearest standing enemy in the read and its distance (rows without a position are left out)."""
    if me.pos is None:
        return None
    foes = [
        (r, math.dist(me.pos, r.pos))
        for r in view.rows
        if r.relation in ("hostile", "enemy") and r.standing and r.pos is not None
    ]
    return min(foes, key=lambda x: (x[1], x[0].handle)) if foes else None


Call = Callable[[str, dict[str, Any]], tuple[str, bool]]


def attack(call: Call, me: Row, foe: Row) -> None:
    text, err = call("use_ability", {"ability": ATTACK, "target": foe.handle, "who": me.handle})
    if err and "has no action called" in text:
        text, err = call("use_ability", {"ability": UNARMED, "target": foe.handle, "who": me.handle})
    if err and any(words in text for words in TOO_FAR):
        shoot(call, me, foe)


def shoot(call: Call, me: Row, foe: Row) -> None:
    call("use_ability", {"ability": RANGED, "target": foe.handle, "who": me.handle})


def step(call: Call, sleep: Callable[[float], None] = time.sleep) -> str:
    """One pass of the loop (the module doc); what it did, for the tests."""
    call("wait_for", {"target": "my_turn", "timeout_s": TURN_WAIT_S})
    view = parse_combat(call("get_combat_state", {})[0])
    me = actor(view) if view else None
    if view is None or me is None:
        sleep(POLL_S)
        return "waited"
    near = nearest(view, me)
    moved = near is not None and near[1] > REACH_M
    if near is not None and moved:
        call("move_to", {"target": near[0].handle, "who": me.handle, "within": WITHIN_M, "wait": True})
        view2 = parse_combat(call("get_combat_state", {})[0])
        me2 = next((r for r in view2.rows if r.handle == me.handle), None) if view2 else None
        near = nearest(view2, me2) if view2 and me2 else None
    attacked = near is not None and near[1] <= REACH_M
    shot = near is not None and moved and not attacked and near[1] <= SHOOT_M
    if near is not None and attacked:
        attack(call, me, near[0])
    elif near is not None and shot:
        shoot(call, me, near[0])
    _text, err = call("end_turn", {"who": me.handle, "wait": False})
    if err:
        sleep(POLL_S)  # the end didn't go through: read again, don't press it in a loop
    return "+".join(w for w, on in (("moved", moved), ("attacked", attacked), ("shot", shot)) if on) or "ended"


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    sid = arg(argv, "--resume") or arg(argv, "--session-id") or str(uuid.uuid4())
    spec = arg(argv, "--mcp-config")
    if spec is None:
        print("scripted_client: no --mcp-config", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    mcp = Mcp(server_config(spec))
    turn = Turn(mcp, sid)
    why: str | None = None
    try:
        tools = mcp.start()
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": sid,
                "model": "none",
                "tools": [f"mcp__bg3__{t}" for t in tools],
                "mcp_servers": [{"name": "bg3", "status": "connected"}],
            }
        )
        while True:
            step(turn.call)
    except KeyboardInterrupt:  # the runner's stop at the match's end
        pass
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    finally:
        mcp.close()
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution" if why else "success",
            "is_error": bool(why),
            "result": why or "stopped",
            "session_id": sid,
            "num_turns": turn.calls,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "duration_api_ms": 0,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    return 1 if why else 0


class ScriptedClient:
    """clients.Client for the scripted process: claude's stream-json, read with ClaudeClient's parser."""

    name = SCRIPTED
    supports_resume = True
    assigns_session = True
    no_tools = "no model and no tools: it calls only its side's bg3 server"

    def __init__(self, mcp_config: Path | str = clients.MCP_CONFIG):
        self.mcp_config = str(mcp_config)
        self._parser = clients.ClaudeClient()

    def write_config(self, req: clients.Request) -> list[Path]:
        return []

    def env(self, req: clients.Request) -> dict:
        return {}

    def command(self, req: clients.Request) -> list[str]:
        flag = "--resume" if req.resume else "--session-id"
        return [sys.executable, "-m", "bg3harness.arena.scripted_client", "--mcp-config", self.mcp_config, flag,
                str(req.session)]  # fmt: skip

    def parse(self, line: str) -> list[clients.Event]:
        return self._parser.parse(line)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
