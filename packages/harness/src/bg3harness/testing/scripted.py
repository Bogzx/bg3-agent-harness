"""A scripted game state for the fake mod: the ops the bridge's common tools call, answered from a small model.

Reply shapes follow the mod (mod/Mods/BG3AI/ScriptExtender/Lua): state.status / state.inventory / state.combat /
state.journal / state.nearby / state.character (Server/State.lua), events.since and job.poll (Shared/RPC.lua), ui.state
and ui.dialogue_choose (Client/UIState.lua, UIActions.lua), ref.* (Server/Referee.lua), roll.* (Server/RollLog.lua),
scn.* (Server/Scenario.lua). Only the fields the bridge reads are filled; anything else falls through to the next
responder (recorded fixtures) or the mod's "unknown op" refusal.

A test drives the game between tool calls with the scenario methods (add_gold, start_fight, open_dialogue,
change_area, add_item, referee, roll, load_save, ...): each changes the state and pushes the events the mod would push.
Time comes from a FakeClock the test advances; nothing here sleeps or reads the wall clock.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .fakemod import Reply

SCENARIO_SWITCH = "BG3AI_SCENARIO_SETUP"


@dataclass
class FakeClock:
    """The mod's monotonic clock (ms) and the game clock (s), both moved only by the test."""

    ms: int = 1_000_000
    game_s: float = 100.0

    def advance(self, ms: int) -> None:
        self.ms += ms
        self.game_s += ms / 1000.0


@dataclass
class Member:
    uuid: str
    name: str
    hp: int = 12
    max_hp: int = 12
    level: int = 1
    cls: str = "Fighter"
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dead: bool = False

    def brief(self, active: str, region: str) -> dict[str, Any]:
        o: dict[str, Any] = {
            "uuid": self.uuid,
            "name": self.name,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "level": self.level,
            "pos": list(self.pos),
            "statuses": [],
            "classes": [{"class": self.cls, "level": self.level}],
            "region": region,
        }
        if self.dead:
            o["dead"] = True
        if self.uuid == active:
            o["active"] = True
        else:
            o["chained"] = True
        return o


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


@dataclass
class Ring:
    """A seq-numbered record ring like the mod's (RPC.events, Referee R.ring, RollLog L.ring)."""

    records: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    seq: int = 0

    def add(self, rec: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        rec["seq"] = self.seq
        self.records.append(rec)
        return rec

    def since(self, seq: Any, limit: Any, default_limit: int) -> list[dict[str, Any]]:
        since = int(seq or 0)
        n = int(limit or default_limit)
        return [r for r in self.records if r["seq"] > since][:n]

    def first(self) -> int:
        return self.records[0]["seq"] if self.records else self.seq + 1


class GameModel:
    """The scripted game. Answers ops as a fake mod responder (`reply`, `eval_raw`)."""

    def __init__(
        self,
        clock: FakeClock | None = None,
        scenario_setup: bool = True,
        region: str = "Act 1",
        level: str = "WLD_Main_A",
        gold: int = 100,
        game_state: str = "Running",
    ):
        self.clock = clock or FakeClock()
        self.scenario_setup = scenario_setup
        self.game_state = game_state
        self.boots = 1
        self.region, self.level = region, level
        self.party = [
            Member(_uuid(1), "Tav", cls="Fighter", pos=(10.0, 0.0, 10.0)),
            Member(_uuid(2), "Shadowheart", hp=10, max_hp=10, cls="Cleric", pos=(11.0, 0.0, 10.0)),
        ]
        self.active = self.party[0].uuid
        self.gold = gold
        self.items: list[dict[str, Any]] = [
            {
                "uuid": _uuid(101),
                "name": "Longsword",
                "owner": self.party[0].uuid,
                "value": 15,
                "weight": 1.35,
                "equipped_slot": "MeleeMainHand",
                "slot_type": "MeleeMainHand",
            },
            {
                "uuid": _uuid(102),
                "name": "Potion of Healing",
                "owner": self.party[0].uuid,
                "count": 2,
                "value": 50,
                "weight": 0.1,
                "usable": True,
            },
        ]
        self.nearby: list[dict[str, Any]] = []
        self.journal: list[dict[str, Any]] = []
        self.combat: dict[str, Any] | None = None
        self.dialogue: dict[str, Any] | None = None
        self.events = {"server": Ring(), "client": Ring()}
        self.ref = Ring()
        self.rolls = Ring()
        self.ref_fired: dict[str, int] = {}
        self.jobs: dict[int, dict[str, Any]] = {}
        self.next_job = 1
        self.scn_seed: int | None = None
        self.scn_renames: list[dict[str, Any]] = []
        self.scn_applied: dict[str, Any] = {}
        self.scn_reapplied: dict[str, Any] = {}
        self.names: dict[str, str] = {}
        self.chosen: list[int] = []

    # ---- helpers ----

    @property
    def boot(self) -> str:
        return f"{1_000_000 + self.boots}-{self.boots}"

    def member(self, name_or_uuid: str) -> Member:
        for m in self.party:
            if name_or_uuid in (m.name, m.uuid) or m.uuid.endswith(name_or_uuid):
                return m
        raise KeyError(name_or_uuid)

    def name(self, uuid: str, default: str) -> str:
        return self.names.get(uuid, default)

    def push(self, kind: str, data: dict[str, Any] | None = None, ctx: str = "server") -> int:
        """An event in the mod's events ring (what the bridge's EVENTS sections and get_events read)."""
        ev = self.events[ctx].add({"t": self.clock.ms, "kind": kind, "data": data or {}})
        return int(ev["seq"])

    def ref_record(self, ev: str, **data: Any) -> dict[str, Any]:
        """A referee ring record (Referee.lua R.record): what the harness reads, never the agent."""
        self.ref_fired[ev] = self.ref_fired.get(ev, 0) + 1
        return self.ref.add({**data, "t": self.clock.ms, "gt": round(self.clock.game_s, 3), "ev": ev})

    def _ref_of(self, uuid: str, name: str) -> dict[str, Any]:
        return {"uuid": uuid, "name": name}

    # ---- scenario steps ----

    def add_gold(self, delta: int) -> None:
        self.gold += delta
        self.push("gold_changed", {"gold": self.gold, "delta": delta})
        self.ref_record("gold", gold=self.gold, delta=delta, holder=self.party[0].name)

    def add_item(self, name: str, owner: str | None = None, count: int = 1, value: int = 1, **extra: Any) -> str:
        m = self.member(owner) if owner else self.party[0]
        uuid = _uuid(200 + len(self.items))
        item: dict[str, Any] = {"uuid": uuid, "name": name, "owner": m.uuid, "value": value, "weight": 0.5, **extra}
        if count > 1:
            item["count"] = count
        self.items.append(item)
        data: dict[str, Any] = {"owner": self._ref_of(m.uuid, m.name), "item": self._ref_of(uuid, name)}
        if count > 1:
            data["count"] = count
        self.push("item_added", data)
        self.ref_record("item_added", name=name, uuid=uuid, count=count, add_type="Regular", owner=m.name)
        return uuid

    def add_nearby(self, name: str, item: bool = False, dist: float = 5.0, **extra: Any) -> str:
        """A character (relation "neutral" unless given) or an item (kind "object" unless given) near the party."""
        uuid = _uuid(300 + len(self.nearby))
        row: dict[str, Any] = {"uuid": uuid, "name": name, "dist": dist, "pos": [12.0, 0.0, 12.0], "item": item}
        row.update({"kind": "object"} if item else {"relation": "neutral", "can_talk": True})
        row.update(extra)
        self.nearby.append(row)
        return uuid

    def start_fight(self, enemies: list[str], hp: int = 7) -> None:
        """A combat with the party first in the turn order; pushes combat_started and round_started."""
        order: list[dict[str, Any]] = []
        for i, m in enumerate(self.party):
            order.append(
                {
                    "uuid": m.uuid,
                    "name": m.name,
                    "relation": "party",
                    "initiative": 20 - i,
                    "hp_pct": round(100 * m.hp / m.max_hp),
                    "pos": list(m.pos),
                }
            )
        for i, name in enumerate(enemies):
            uuid = _uuid(400 + i)
            order.append(
                {
                    "uuid": uuid,
                    "name": name,
                    "relation": "enemy",
                    "initiative": 5 - i,
                    "hp_pct": 100,
                    "race": "Goblin",
                    "pos": [15.0 + i, 0.0, 12.0],
                    "hp": hp,
                }
            )
        order[0]["is_current"] = True
        self.combat = {
            "active": True,
            "round": 1,
            "order": order,
            "current": order[0]["uuid"],
            "turn_group": [order[0]["uuid"]],
        }
        self.push("combat_started", {})
        self.push("round_started", {"round": 1})
        self.push("turn_started", self._ref_of(order[0]["uuid"], order[0]["name"]))
        self.ref_record("combat_started", combat=f"combat-{self.boots}-{len(self.ref.records)}")

    def kill(self, name: str) -> None:
        """An enemy dies (by the party): died event, referee kill + died."""
        assert self.combat is not None, "no fight"
        row = next(r for r in self.combat["order"] if r["name"] == name)
        self.combat["order"].remove(row)
        self.push("died", self._ref_of(row["uuid"], name))
        self.ref_record(
            "kill",
            victim=name,
            victim_uuid=row["uuid"],
            killer=self.party[0].name,
            owner=self.party[0].name,
            by_party=True,
            trader=False,
        )
        self.ref_record("died", name=name, party=False, uuid=row["uuid"])

    def end_fight(self) -> None:
        self.combat = None
        self.push("combat_ended", {})
        self.ref_record("combat_ended", combat="combat")

    def open_dialogue(self, speaker: str, text: str, options: list[str]) -> None:
        self.dialogue = {
            "speaker": speaker,
            "text": text,
            "options": [{"i": i + 1, "text": t} for i, t in enumerate(options)],
        }
        self.push(
            "dialogue_started", {"speakers": [{"name": speaker}], "party": [self.party[0].name], "level": self.level}
        )
        self.push("dialogue_line", {"speaker": speaker, "text": text, "options": len(options)}, "client")

    def close_dialogue(self) -> None:
        self.dialogue = None
        self.push("dialogue_ended", {})

    def change_area(self, region: str, level: str) -> None:
        self.region, self.level = region, level
        self.push("level_changed", {"region": region})

    def add_quest(self, title: str, text: str) -> None:
        self.journal.append({"title": title, "steps": [text], "id": title.replace(" ", "_")})
        self.push("quest_added", {"title": title})

    def referee(self, ev: str, **data: Any) -> dict[str, Any]:
        """Any referee record (trade_open, trade_end, pickpocket, stole, crime, loot, ...)."""
        return self.ref_record(ev, **data)

    def roll(
        self, success: bool, natural: int | None = None, dc: int | None = None, roller: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        """A roll log record (RollLog.lua): the merged view of Osiris RollResult and the roll-finished component."""
        who = self.member(roller).uuid if roller else self.party[0].uuid
        rec: dict[str, Any] = {
            "t": self.clock.ms,
            "gt": round(self.clock.game_s, 3),
            "srcs": ["finished", "osi"],
            "roller": who,
            "subject": who,
            "party": True,
            "success": success,
            "active": False,
            "streams": {"Generic": 2},
            **extra,
        }
        if natural is not None:
            rec["natural"] = natural
            rec["crit"] = "nat20" if natural == 20 else "nat1" if natural == 1 else None
        if dc is not None:
            rec["dc"] = dc
        return self.rolls.add({k: v for k, v in rec.items() if v is not None})

    def load_save(self) -> None:
        """A save load: the mod's Lua state is rebuilt (new boot, rings start over); Scenario re-applies its record."""
        self.boots += 1
        self.events = {"server": Ring(), "client": Ring()}
        self.ref, self.rolls, self.ref_fired, self.jobs = Ring(), Ring(), {}, {}
        self.combat = self.dialogue = None
        self.scn_applied, self.scn_reapplied = {}, {}
        if self.scenario_setup:  # Scenario.reapply: the recorded seed and renames (the renames survive anyway)
            if self.scn_seed is not None:
                self.scn_applied = {"seed": self.scn_seed, "gt": round(self.clock.game_s, 3), "streams": 4}
                self.scn_reapplied["seed"] = {"seed": self.scn_seed, "base_seed": self.scn_seed, "streams": 4}
            self.scn_reapplied["renames"] = len(self.scn_renames)

    # ---- the responder ----

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        fn = _OPS.get(op)
        if fn is None:
            return None
        try:
            return {"ok": True, "result": copy.deepcopy(fn(self, ctx, args))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def eval_raw(self, ctx: str, code: str) -> str | None:
        if "GetGameState" in code:
            return self.game_state
        return None

    # ---- ops ----

    def op_status(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "party": [m.brief(self.active, self.level) for m in self.party],
            "active": self.active,
            "level": self.level,
            "region": self.region,
            "in_combat": self.combat is not None,
            "turn_based": self.combat is not None,
            "in_dialogue": self.dialogue is not None,
            "gold": self.gold,
            "camp": [],
        }
        if self.combat:
            for b in out["party"]:
                b["in_combat"] = True
            out["combat"] = {"round": self.combat["round"], "current": self.combat["current"]}
        return out

    def op_inventory(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        items = [dict(i) for i in self.items if not args.get("uuid") or i["owner"] == args["uuid"]]
        members = {
            m.uuid: {"name": m.name, "encumbrance": 0, "weight": 10.0, "limit": 90.0}
            for m in self.party
            if not args.get("uuid") or m.uuid == args["uuid"]
        }
        return {"items": items, "members": members, "supplies": 0, "camp_supplies": 0, "gold": self.gold, "rev": 1}

    def op_combat(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.combat is None:
            return {"active": False}
        return dict(self.combat)

    def op_character(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        m = self.member(args.get("uuid") or self.active)
        return {
            "uuid": m.uuid,
            "name": m.name,
            "level": m.level,
            "hp": m.hp,
            "max_hp": m.max_hp,
            "classes": [{"class": m.cls, "level": m.level}],
            "race": "Human",
            "abilities": {
                "Strength": 16,
                "Dexterity": 12,
                "Constitution": 14,
                "Intelligence": 10,
                "Wisdom": 12,
                "Charisma": 8,
            },
            "ac": 16,
            "statuses": [],
            "equipment": {},
            "xp": {"current": 0, "next_level": 300, "total": 0},
        }

    def op_ui_state(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.dialogue is not None:
            return {
                "screen": "dialogue",
                "page": "Dialogue",
                "dialogue": copy.deepcopy(self.dialogue),
                "region": self.region,
                "blocking": True,
                "ms": 1,
            }
        return {"screen": "game", "region": self.region, "ms": 1}

    def op_choose(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.dialogue is None:
            raise RuntimeError("no dialogue on screen")
        i = int(args.get("i") or 0)
        n = len(self.dialogue.get("options") or [])
        if not 1 <= i <= n:
            raise RuntimeError(f"no option {i} ({n} shown)")
        if args.get("dry"):
            return {"can": True}
        self.chosen.append(i)
        self.close_dialogue()
        return self._job("ui.dialogue_choose", {"screen": "game", "ended": True, "lines": []})

    def _job(self, kind: str, result: dict[str, Any]) -> dict[str, Any]:
        """A job that settles in the call (RPC.start_job + finish_job in one tick)."""
        jid = self.next_job
        self.next_job += 1
        self.jobs[jid] = {"id": jid, "kind": kind, "status": "done", "result": result, "elapsed_ms": 0}
        return {"job": jid, "status": "done", "result": result}

    def op_job_poll(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get(int(args.get("id") or 0))
        if job is None:
            raise RuntimeError(f"no such job {args.get('id')}")
        return job

    def op_events(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        ring = self.events[ctx]
        return {"latest": ring.seq, "events": ring.since(args.get("seq"), args.get("limit"), 200)}

    def ring_reply(self, ring: Ring, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "boot": self.boot,
            "now": self.clock.ms,
            "gt": round(self.clock.game_s, 3),
            "latest": ring.seq,
            "first": ring.first(),
            "events": ring.since(args.get("seq"), args.get("limit"), 500),
        }

    def op_ref_snapshot(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "boot": self.boot,
            "now": self.clock.ms,
            "gt": round(self.clock.game_s, 3),
            "latest": self.ref.seq,
            "fired": dict(self.ref_fired),
            "never": [],
            "registered": sorted(self.ref_fired),
            "combats": {},
        }

    def op_roll_snapshot(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        seed = self.scn_seed if self.scn_seed is not None else 1792610828
        return {
            "boot": self.boot,
            "now": self.clock.ms,
            "latest": self.rolls.seq,
            "fired": {"osi": self.rolls.seq, "finished": self.rolls.seq},
            "finished_subscribed": True,
            "base_seed": seed,
            "mode": "Random",
            "streams": [
                {
                    "key": "Generic",
                    "type": "Generic",
                    "num_rolls": 2 * self.rolls.seq,
                    "seed": seed + 1,
                    "frame": 1,
                    "fail_debt": 0,
                    "success_debt": 0,
                }
            ],
        }

    def _scn(self) -> None:
        if not self.scenario_setup:
            raise RuntimeError(f"scenario setup is off: the game was not started with {SCENARIO_SWITCH}=1")

    def op_scn_seed(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        seed = args.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2_000_000_000:
            raise RuntimeError("seed must be an integer in 0..2000000000")
        self.scn_seed = seed
        self.scn_applied = {"seed": seed, "gt": round(self.clock.game_s, 3), "streams": 4}
        return {"seed": seed, "base_seed": seed, "streams": 4}

    def op_scn_karmic(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        return {"mode": "Random", "karmic": False, "component_mode": "Random"}

    def op_scn_rename(self, ctx: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        self._scn()
        out: list[dict[str, Any]] = []
        renames: list[dict[str, Any]] = args.get("renames") or []
        for r in renames:
            uuid, name = r.get("uuid"), r.get("name")
            if not isinstance(uuid, str) or not isinstance(name, str) or not name:
                raise RuntimeError("each rename needs a uuid and a non-empty name")
            original = self.names.get(uuid) or next((m.name for m in self.party if m.uuid == uuid), uuid[:8])
            rec = {"uuid": uuid, "handle": f"h{len(self.scn_renames) + 1:04x}", "original": original, "name": name}
            self.names[uuid] = name
            self.scn_renames.append(rec)
            out.append(rec)
        return out

    def op_scn_undo(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        restored: list[dict[str, Any]] = []
        for r in reversed(self.scn_renames):
            self.names.pop(r["uuid"], None)
            restored.append({"handle": r["handle"], "original": r["original"]})
        self.scn_renames = []
        return {"restored": restored}

    def op_scn_status(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        out = {
            "seed": self.scn_seed,
            "applied": self.scn_applied or None,
            "reapplied": self.scn_reapplied or None,
            "renames": copy.deepcopy(self.scn_renames),
        }
        return {k: v for k, v in out.items() if v is not None}  # Lua drops nil fields


def _nearby(m: GameModel, kind_items: bool) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "item"} for r in m.nearby if bool(r["item"]) == kind_items]


OpFn = Callable[[GameModel, str, dict[str, Any]], Any]
_OPS: dict[str, OpFn] = {
    "sys.ping": lambda m, c, a: "pong",
    "events.since": GameModel.op_events,
    "job.poll": GameModel.op_job_poll,
    "state.status": GameModel.op_status,
    "state.inventory": GameModel.op_inventory,
    "state.combat": GameModel.op_combat,
    "state.character": GameModel.op_character,
    "state.journal": lambda m, c, a: copy.deepcopy(m.journal),
    "state.nearby": lambda m, c, a: {"characters": _nearby(m, False), "items": _nearby(m, True)},
    "act.prepared": lambda m, c, a: {"casters": []},
    "nav.markers": lambda m, c, a: {"markers": [], "ms": 0},
    "ui.state": GameModel.op_ui_state,
    "ui.rest_info": lambda m, c, a: {"short_rests_left": 2, "short_rests_max": 2},
    "ui.story_timer": lambda m, c, a: {"none": True},
    "ui.dialogue_choose": GameModel.op_choose,
    "ref.since": lambda m, c, a: m.ring_reply(m.ref, a),
    "ref.snapshot": GameModel.op_ref_snapshot,
    "roll.since": lambda m, c, a: m.ring_reply(m.rolls, a),
    "roll.snapshot": GameModel.op_roll_snapshot,
    "scn.seed": GameModel.op_scn_seed,
    "scn.karmic": GameModel.op_scn_karmic,
    "scn.rename": GameModel.op_scn_rename,
    "scn.undo": GameModel.op_scn_undo,
    "scn.status": GameModel.op_scn_status,
}
