"""Harness-side readers for the mod's hidden rings: the referee (Server/Referee.lua: ref.since, ref.snapshot) and the
roll log (Server/RollLog.lua: roll.since).

These rings hold what the agent must never see (crimes nobody witnessed, kills out of sight, every die rolled). Only
the harness reads them; no MCP tool may call these ops or import this module (tests/test_referee.py checks the
bridge sources). Records come back with the mod's monotonic clock (`t`, ms); `RingReader` turns that into wall-clock
epoch seconds so they line up with facts.jsonl (packages/harness/schemas/facts.v1.schema.json).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

FACTS_V = 1
ROLLS_V = 1
# Fields of a ring record that are not part of the facts event.
_RING_FIELDS = ("seq", "t")


class Rpc(Protocol):
    def __call__(self, op: str, args: dict) -> Any: ...


@dataclass
class RingReader:
    """Drains one ring op (`ref.since` or `roll.since`) from a cursor.

    A save load rebuilds the mod's Lua state: its `boot` id changes and seq restarts at 1, so the cursor restarts too.
    Records dropped by the ring cap before they were read are counted in `lost`."""

    rpc: Rpc
    op: str
    wall: Callable[[], float] = time.time
    limit: int = 500
    boot: str | None = None
    seq: int = 0
    lost: int = 0

    def sync(self) -> None:
        """Start from now: skip what the ring already holds (a reader started mid-run)."""
        res = self.rpc(self.op, {"seq": 10**12, "limit": 1})
        self.boot, self.seq = res.get("boot"), int(res.get("latest") or 0)

    def poll(self) -> list[dict]:
        """Records since the last poll, oldest first, `t` in wall-clock epoch seconds."""
        out: list[dict] = []
        while True:
            res = self.rpc(self.op, {"seq": self.seq, "limit": self.limit})
            wall = self.wall()
            if res.get("boot") != self.boot:
                restarted = self.boot is not None
                self.boot, self.seq = res.get("boot"), 0
                if restarted:
                    continue
            first = int(res.get("first") or 1)
            if first > self.seq + 1:
                self.lost += first - self.seq - 1
            now = float(res.get("now") or 0)
            events = res.get("events") or []
            for rec in events:
                rec = dict(rec)
                rec["t"] = wall - (now - float(rec.get("t") or now)) / 1000.0
                self.seq = max(self.seq, int(rec.get("seq") or 0))
                out.append(rec)
            if len(events) < self.limit:
                return out


def to_fact(rec: dict) -> dict:
    """One referee record (already through RingReader) as a facts v1 event. `game_t` is the harness's to fill
    (time since run_start); the mod's own game clock stays in `gt`."""
    fact = {"v": FACTS_V, "kind": "event", "t": rec["t"], "game_t": None}
    fact.update({k: v for k, v in rec.items() if k not in _RING_FIELDS and v is not None})
    return fact


def referee(rpc: Rpc, wall: Callable[[], float] = time.time) -> RingReader:
    return RingReader(rpc, "ref.since", wall)


def rolls(rpc: Rpc, wall: Callable[[], float] = time.time) -> RingReader:
    return RingReader(rpc, "roll.since", wall)


def to_roll(rec: dict) -> dict:
    """One roll log record (already through RingReader) as a rolls.jsonl row: {v, t, srcs: [...], roller, party,
    success, active, crit, natural, dc, streams, ...}; fields a source did not give are left out."""
    row = {"v": ROLLS_V, "t": rec["t"]}
    row.update({k: v for k, v in rec.items() if k not in _RING_FIELDS and v is not None})
    return row


def selfcheck(snapshot: dict) -> dict:
    """{fired: {listener: count}, never: [listener]} from ref.snapshot: a listener that registered but never fired
    may have the wrong name or arity on this build (registration proves nothing, research/ovh/live1.md §6)."""
    fired = {k: int(n) for k, n in (snapshot.get("fired") or {}).items() if int(n) > 0}
    never = sorted(k for k, n in (snapshot.get("fired") or {}).items() if int(n) == 0)
    return {"fired": dict(sorted(fired.items())), "never": never}
