# pyright: basic
"""The arena a match plays and the two sides playing it, as the runner reads them.

The harness never imports the bench: a caller (runner/arena_cli.py) loads the arena file with bg3bench.arena.load
(validated, defaults filled in) and hands the dict to ArenaSpec.from_dict, which reads only the fields the runner
uses and refuses a dict that lacks them. The arena dict itself goes on to the mod's arena.setup unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SIDES: tuple[str, ...] = ("A", "B")


class ArenaSpecError(ValueError):
    pass


def other(side: str) -> str:
    return "B" if side == "A" else "A"


@dataclass(frozen=True)
class ArenaSpec:
    name: str
    save: str  # the start save's name
    save_sha1: str
    teams: dict[str, tuple[str, ...]]  # side -> character names, in the arena file's order
    uuids: dict[str, tuple[str | None, ...]]  # side -> each character's uuid when the file gives one
    seed: int
    karmic: bool
    round_cap: int
    wall_cap_min: float
    turn_cap_s: float  # model time per turn
    tools_off: str  # the bridge's named tools-off list (BG3AI_TOOLS_OFF)
    mirror: bool = False  # a mirror arena: arena.setup makes the twins, their uuids come from its result
    turn_order: str = "alternate"  # one character a phase, sides alternating: "alternate" | "snake" (the mod's rule)
    arena: dict[str, Any] = field(default_factory=dict[str, Any], compare=False)  # what arena.setup gets

    @classmethod
    def from_dict(cls, a: Mapping[str, Any]) -> ArenaSpec:
        """An arena.v1 dict with its defaults (bg3bench.arena.load). A draft arena (its spots are placeholders) or a
        missing field is refused, naming every problem."""
        errs: list[str] = []
        name = a.get("name")
        if a.get("draft"):
            errs.append(f"a draft arena can't be played: {a['draft']}")
        save: Mapping[str, Any] = a["save"] if isinstance(a.get("save"), Mapping) else {}
        if not isinstance(save.get("name"), str) or not isinstance(save.get("sha1"), str):
            errs.append("save needs a name and a sha1")
        teams_raw: Mapping[str, Any] = a["teams"] if isinstance(a.get("teams"), Mapping) else {}
        teams: dict[str, tuple[str, ...]] = {}
        uuids: dict[str, tuple[str | None, ...]] = {}
        for s in SIDES:
            members = teams_raw.get(s)
            rows = [m for m in members if isinstance(m, Mapping)] if isinstance(members, list) else []
            names = [m.get("name") for m in rows]
            if not names or not all(isinstance(n, str) and n for n in names):
                errs.append(f"team {s} needs its characters' names")
            teams[s] = tuple(n for n in names if isinstance(n, str))
            uuids[s] = tuple(str(m["uuid"]).lower() if m.get("uuid") else None for m in rows)
        for key, kind in (("seed", int), ("round_cap", int), ("wall_cap_min", (int, float)),
                          ("turn_cap_s", (int, float)), ("tools_off", str), ("name", str)):  # fmt: skip
            v = a.get(key)
            if not isinstance(v, kind) or isinstance(v, bool):
                errs.append(f"{key} is missing or not a {getattr(kind, '__name__', 'number')}")
        if a.get("karmic") is not False:
            errs.append("karmic must be false (Karmic Dice off)")
        turn_order = a.get("turn_order", "alternate")
        if turn_order not in ("alternate", "snake"):
            errs.append("turn_order must be alternate or snake")
        if errs:
            raise ArenaSpecError(f"arena {name or '?'}: " + "; ".join(errs))
        return cls(
            name=a["name"],
            save=save["name"],
            save_sha1=save["sha1"],
            teams=teams,
            uuids=uuids,
            seed=a["seed"],
            karmic=False,
            round_cap=a["round_cap"],
            wall_cap_min=float(a["wall_cap_min"]),
            turn_cap_s=float(a["turn_cap_s"]),
            tools_off=a["tools_off"],
            mirror="mirror" in a,
            turn_order=str(turn_order),
            arena=dict(a),
        )

    def names(self) -> dict[str, list[str]]:
        return {s: list(self.teams[s]) for s in SIDES}


@dataclass(frozen=True)
class Side:
    """One side's player: the client profile it plays and the name it is rated under ("<client>/<model slug>")."""

    side: str
    player: str
    profile: str = ""  # the profile file
    client: str = ""
    model: str = ""
