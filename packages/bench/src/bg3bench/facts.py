# pyright: basic
"""Reading facts.jsonl v1 (packages/harness/schemas/facts.v1.schema.json) and folding it into game state.

The harness writes the file; the benchmark only reads it. `State` is what the graders test: snapshots replace the
current party, inventory, gold, region and combat flag; events update them between snapshots; places and quests seen
accumulate. `from_progress` turns an old `Sxxx.progress.jsonl` (bg3ai.progress) into v1 events so archived runs can
be graded with the same leaves.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 1
KINDS = ("snapshot", "event", "mark")


class FactsError(ValueError):
    """A facts record is malformed or out of order."""


def check(records: Iterable[dict]) -> list[dict]:
    """The records as a list, after checking version, kind and time order (the schema has the full contract)."""
    out, last = [], None
    for i, r in enumerate(records):
        if not isinstance(r, dict) or r.get("v") != VERSION:
            raise FactsError(f"record {i}: not a facts v{VERSION} record: {r!r}")
        if r.get("kind") not in KINDS:
            raise FactsError(f"record {i}: unknown kind {r.get('kind')!r}")
        t = r.get("t")
        if isinstance(t, bool) or not isinstance(t, int | float):
            raise FactsError(f"record {i}: t must be a number")
        if last is not None and t < last:
            raise FactsError(f"record {i}: t={t} goes back in time (previous {last})")
        last = t
        out.append(r)
    return out


def load(path: str | Path) -> list[dict]:
    """Read and check a facts.jsonl file."""
    with open(path, encoding="utf-8") as f:
        return check(json.loads(line) for line in f if line.strip())


@dataclass
class Member:
    level: int | None = None
    dead: bool = False
    downed: bool = False
    avatar: bool = False
    summon: bool = False


@dataclass
class State:
    gold: int = 0
    party: dict[str, Member] = field(default_factory=dict)
    inv: list[dict] = field(default_factory=list)
    region: str | None = None
    regions: set[str] = field(default_factory=set)
    areas: set[str] = field(default_factory=set)
    quests_done: set[str] = field(default_factory=set)
    combat: bool = False
    combats_started: int = 0
    combats_won: int = 0
    wiped_this_combat: bool = False
    game_end: bool = False

    def real_party(self) -> list[Member]:
        return [m for m in self.party.values() if not m.summon]

    def companions(self) -> int:
        return sum(1 for m in self.real_party() if not m.avatar)

    def party_dead(self) -> int:
        return sum(1 for m in self.real_party() if m.dead)

    def wiped(self) -> bool:
        real = self.real_party()
        return bool(real) and all(m.dead or m.downed for m in real)

    def party_level(self, agg: str = "min") -> int:
        levels = [m.level for m in self.real_party() if m.level is not None]
        if not levels:
            return 0
        return min(levels) if agg == "min" else max(levels)

    def item_count(self, name: str | None = None, tpl: str | None = None) -> int:
        if name is not None:
            return sum(int(i.get("count", 0)) for i in self.inv if str(i.get("name", "")).lower() == name.lower())
        return sum(int(i.get("count", 0)) for i in self.inv if i.get("tpl") == tpl)

    def _set_combat(self, on: bool) -> None:
        if on and not self.combat:
            self.combats_started += 1
            self.wiped_this_combat = False
        elif not on and self.combat and not (self.wiped_this_combat or self.wiped()):
            self.combats_won += 1
        self.combat = on

    def apply(self, r: dict) -> None:
        """Fold one record into the state."""
        if r["kind"] == "snapshot":
            self._snapshot(r)
        elif r["kind"] == "event":
            self._event(r)
        if self.combat and self.wiped():
            self.wiped_this_combat = True

    def _snapshot(self, r: dict) -> None:
        self.gold = int(r["gold"])
        self.party = {
            p["name"]: Member(
                p.get("level"), bool(p.get("dead")), bool(p.get("downed")), bool(p.get("avatar")), bool(p.get("summon"))
            )
            for p in r["party"]
        }
        self.inv = [dict(i) for i in r["inv"]]
        self.region = r["region"]
        if r["region"]:
            self.regions.add(r["region"])
        self.areas.update(r["areas"])
        if r.get("area"):
            self.areas.add(r["area"])
        self.quests_done.update(q for q, done in r["quests"].items() if done)
        self._set_combat(bool(r["combat"]))

    def _event(self, r: dict) -> None:
        ev = r["ev"]
        if ev == "combat_started":
            self._set_combat(True)
        elif ev == "combat_ended":
            self._set_combat(False)
        elif ev in ("died", "downed", "revived"):
            if r["party"]:
                m = self.party.setdefault(r["name"], Member())
                if ev == "died":
                    m.dead = True
                else:
                    m.downed = ev == "downed"
        elif ev == "level_up":
            self.party.setdefault(r["name"], Member()).level = int(r["level"])
        elif ev == "joined":
            self.party.setdefault(r["name"], Member())
        elif ev == "gold":
            self.gold = int(r["gold"])
        elif ev == "item_added":
            self.inv.append({k: r[k] for k in ("name", "tpl", "count") if k in r})
        elif ev == "quest_done":
            self.quests_done.add(r["title"])
        elif ev == "region":
            self.region = r["name"]
            self.regions.add(r["name"])
        elif ev == "area":
            self.areas.add(r["name"])
        elif ev == "game_end":
            self.game_end = True


# bg3ai.progress record kinds -> v1 events. Kinds with no fact behind them (explore, talk, objective, ...) are
# dropped; "kill" is kept as a kill event without a killer.
_PROGRESS_KINDS = {
    "region": ("region", "name"),
    "area": ("area", "name"),
    "companion": ("joined", "name"),
    "quest_new": ("quest_added", "title"),
    "quest_done": ("quest_done", "title"),
    "kill": ("kill", "victim"),
    "story_kill": ("kill", "victim"),
}


def from_progress(records: Iterable[dict], started: float | None = None) -> Iterator[dict]:
    """Old progress records -> facts v1 records, starting with a run_start mark at `started` (default: the first
    record's time). Wall-clock only: these files have no game time."""
    first = True
    for r in records:
        t = r.get("t")
        if first:
            first = False
            yield {
                "v": VERSION,
                "kind": "mark",
                "t": t if started is None else started,
                "mark": "run_start",
                "detail": "from_progress",
            }
        kind, what = r.get("kind"), r.get("what")
        if r.get("type") != "progress" or not what:
            continue
        if kind in _PROGRESS_KINDS:
            ev, key = _PROGRESS_KINDS[kind]
            yield {"v": VERSION, "kind": "event", "t": t, "ev": ev, key: what}
        elif kind == "level_up" and " -> level " in what:
            name, level = what.rsplit(" -> level ", 1)
            yield {"v": VERSION, "kind": "event", "t": t, "ev": "level_up", "name": name, "level": int(level)}
        elif kind == "game_end":
            yield {"v": VERSION, "kind": "event", "t": t, "ev": "game_end"}
