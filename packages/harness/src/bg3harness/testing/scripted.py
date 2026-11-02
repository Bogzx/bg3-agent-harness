"""A scripted game state for the fake mod: the ops the bridge's common tools call, answered from a small model.

Reply shapes follow the mod (mod/Mods/BG3AI/ScriptExtender/Lua): state.status / state.inventory / state.combat /
state.journal / state.nearby / state.character (Server/State.lua), events.since and job.poll (Shared/RPC.lua), ui.state
and ui.dialogue_choose (Client/UIState.lua, UIActions.lua), ref.* (Server/Referee.lua), roll.* (Server/RollLog.lua),
scn.* (Server/Scenario.lua). Only the fields the bridge reads are filled; anything else falls through to the next
responder (recorded fixtures) or the mod's "unknown op" refusal.

A test drives the game between tool calls with the scenario methods (add_gold, start_fight, open_dialogue,
change_area, add_item, referee, roll, load_save, ...): each changes the state and pushes the events the mod would push.
Time comes from a FakeClock the test advances; nothing here sleeps or reads the wall clock.

A two-team match (start_arena): calls whose envelope names a side (FakeMod -> reply_side) are answered from that
team's view, as the mod's arena module does with Ctx.side: its own team is the party, the other team are hostile
combatants it sees only while they aren't hidden (hide), the acting combatant is named only when seen, events come in
the side's own view (an unseen source is "Someone", a spell id only for the side's own caster), a job is its side's
("not your job" for the other), and acting out of turn, with the other team's character or on the other side's
reaction prompt is refused with the mod's texts. The reaction prompt is on the one game client's screen: a side's
ui.state shows its own prompt's rows and marks the other side's ({other_side: true}, no rows). Calls without a side
get today's answers.
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


@dataclass
class Arena:
    """A two-team match: the teams (side -> members), the turn order (uuids), whose turn it is, each side's selected
    character, and the characters the other side can't see."""

    teams: dict[str, list[Member]]
    order: list[str]
    selected: dict[str, str]
    turn: int = 0
    round: int = 1
    hidden: set[str] = field(default_factory=set[str])

    def side_of(self, uuid: str) -> str | None:
        return next((s for s, team in self.teams.items() if any(m.uuid == uuid for m in team)), None)

    def member(self, ref: str) -> Member:
        for team in self.teams.values():
            for m in team:
                if ref in (m.name, m.uuid):
                    return m
        raise KeyError(ref)

    @property
    def acting(self) -> Member:
        return self.member(self.order[self.turn])

    def sees(self, side: str, uuid: str) -> bool:
        """Whether `side`'s characters see `uuid`: always their own, the other team unless hidden."""
        return self.side_of(uuid) == side or uuid not in self.hidden

    def refusal(self, side: str, char: str) -> str | None:
        """Why `side` may not act with `char` now (the mod's guard texts), or None."""
        who = self.member(char)
        if self.side_of(who.uuid) != side:
            return f"{who.name} is not on your side"
        now = self.acting
        owner = self.side_of(now.uuid)
        if owner != side:
            return f"not your turn (side {owner} is acting)"
        if now.uuid != who.uuid:
            return "it is not this character's turn"
        return None


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
        self.job_sides: dict[int, str] = {}  # in a match, the side that started a job (RPC.start_job job.side)
        self.next_job = 1
        self.scn_seed: int | None = None
        self.scn_renames: list[dict[str, Any]] = []
        self.scn_applied: dict[str, Any] = {}
        self.scn_reapplied: dict[str, Any] = {}
        self.scn_avatar: dict[str, Any] | None = None  # the record's avatar {uuid, original, name}
        self.scn_karmic_original: str | None = None  # Lua state only (a load forgets it)
        self.roll_mode = "Random"  # RollModeType (WeightedRandom = Karmic Dice on)
        self.difficulty = "Balanced"
        self.names: dict[str, str] = {}
        self.chosen: list[int] = []
        self.arena: Arena | None = None
        self.reaction: dict[str, Any] | None = None  # the reaction prompt on the client's screen
        self.reacted: list[tuple[str, bool]] = []  # (who, use) of each answered prompt
        self.moves: list[tuple[str, str, list[float]]] = []  # arena moves done: (side, uuid, pos)

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

    def push(
        self,
        kind: str,
        data: dict[str, Any] | None = None,
        ctx: str = "server",
        views: dict[str, dict[str, Any]] | None = None,
    ) -> int:
        """An event in the mod's events ring (what the bridge's EVENTS sections and get_events read). views: in a
        match, each side's own data for it (a side without one doesn't see it); `data` is the view without a side."""
        rec: dict[str, Any] = {"t": self.clock.ms, "kind": kind, "data": data or {}}
        if views is not None:
            rec["views"] = views
        ev = self.events[ctx].add(rec)
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
        self.push("turn_started", {**self._ref_of(order[0]["uuid"], order[0]["name"]), "round": 1})
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

    def start_arena(self, team_a: list[str], team_b: list[str], order: list[str] | None = None) -> Arena:
        """A two-team match in turn-based combat: team A ~20 m from team B; the turn order alternates A, B, A, B ...
        unless `order` (names) gives it. Pushes combat_started, round_started and the first turn_started."""
        n = 500
        teams: dict[str, list[Member]] = {}
        for side, names, x in (("A", team_a, 0.0), ("B", team_b, 20.0)):
            teams[side] = []
            for i, name in enumerate(names):
                teams[side].append(Member(_uuid(n), name, pos=(x, 0.0, float(i))))
                n += 1
        by_name = {m.name: m.uuid for team in teams.values() for m in team}
        if order is None:
            pairs = zip(teams["A"], teams["B"], strict=False)
            uuids = [m.uuid for pair in pairs for m in pair]
            uuids += [m.uuid for team in teams.values() for m in team if m.uuid not in uuids]
        else:
            uuids = [by_name[name] for name in order]
        self.arena = Arena(teams, uuids, {s: team[0].uuid for s, team in teams.items()})
        self.push("combat_started", {})
        self.push("round_started", {"round": 1})
        self._push_turn("turn_started")
        return self.arena

    def hide(self, name: str, hidden: bool = True) -> None:
        """A match character the other side can't see (or sees again)."""
        assert self.arena is not None, "no match"
        uuid = self.arena.member(name).uuid
        if hidden:
            self.arena.hidden.add(uuid)
        else:
            self.arena.hidden.discard(uuid)

    def _push_turn(self, kind: str, who: Member | None = None, rnd: int | None = None) -> None:
        """turn_started/turn_ended of `who` (the acting one), tagged with the round its turn belongs to (Events.lua)."""
        a = self.arena
        assert a is not None
        m = who or a.acting
        ref = {**self._ref_of(m.uuid, m.name), "round": a.round if rnd is None else rnd}
        self.push(kind, ref, views={s: ref for s in a.teams if a.sees(s, m.uuid)})

    def arena_cast(self, caster: str, spell: str, name: str) -> None:
        """A cast (Events.lua CastedSpell): seen by the sides that see the caster; the spell id only for its own."""
        a = self.arena
        assert a is not None, "no match"
        m = a.member(caster)
        ref = self._ref_of(m.uuid, m.name)
        views = {
            s: {"caster": ref, "name": name, **({"spell": spell} if a.side_of(m.uuid) == s else {})}
            for s in a.teams
            if a.sees(s, m.uuid)
        }
        self.push("cast", {"caster": ref, "spell": spell, "name": name}, views=views)

    def arena_damage(self, target: str, source: str, amount: int, dtype: str = "Fire") -> None:
        """Damage (Events.lua AttackedBy): seen by the sides that see the target; an unseen source is "Someone"."""
        a = self.arena
        assert a is not None, "no match"
        t, src = a.member(target), a.member(source)
        t.hp = max(0, t.hp - amount)
        tref, sref = self._ref_of(t.uuid, t.name), self._ref_of(src.uuid, src.name)
        views = {
            s: {
                "target": tref,
                "amount": amount,
                "type": dtype,
                "source": sref if a.sees(s, src.uuid) else {"name": "Someone"},
            }
            for s in a.teams
            if a.sees(s, t.uuid)
        }
        self.push("damage", {"target": tref, "amount": amount, "type": dtype, "source": sref}, views=views)

    def open_reaction(self, who: str, reactions: tuple[str, ...] = ("Shield",)) -> None:
        """A reaction prompt for `who` on the game client's screen (UIState.lua S.reaction: rows name the reactor)."""
        rows = [{"i": i + 1, "who": who, "name": r, "level": 1} for i, r in enumerate(reactions)]
        self.reaction = {"prompt": " | ".join([who, *reactions, "Choose how to react."]), "options": rows}

    def arena_end_turn(self) -> None:
        """The acting character's turn ends; the next in the order acts (a new round after the last). At a round
        change the events come in the game's order (phases4 load1): round_started, the new round's turn_started, then
        the old round's turn_ended."""
        a = self.arena
        assert a is not None, "no match"
        ended, old = a.acting, a.round
        if a.turn + 1 < len(a.order):
            self._push_turn("turn_ended")
            a.turn += 1
            self._push_turn("turn_started")
            return
        a.turn, a.round = 0, a.round + 1
        self.push("round_started", {"round": a.round})
        self._push_turn("turn_started")
        self._push_turn("turn_ended", ended, old)

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
        self.scn_applied, self.scn_reapplied, self.scn_karmic_original = {}, {}, None
        if self.scenario_setup:  # Scenario.reapply: the recorded seed, renames (they survive anyway) and avatar
            if self.scn_seed is not None:
                self.scn_applied = {"seed": self.scn_seed, "gt": round(self.clock.game_s, 3), "streams": 4}
                self.scn_reapplied["seed"] = {"seed": self.scn_seed, "base_seed": self.scn_seed, "streams": 4}
            self.scn_reapplied["renames"] = len(self.scn_renames)
            if self.scn_avatar is not None and self.party[0].uuid == self.scn_avatar["uuid"]:
                self.party[0].name = self.scn_reapplied["avatar"] = self.scn_avatar["name"]

    # ---- the responder ----

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        fn = _OPS.get(op)
        if fn is None:
            return None
        try:
            return {"ok": True, "result": copy.deepcopy(fn(self, ctx, args))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reply_side(self, ctx: str, op: str, args: dict[str, Any], side: str) -> Reply | None:
        """A call with a side in its envelope: in a match, answered from that team's view; else as without one."""
        fn = _ARENA_OPS.get(op) if self.arena is not None else None
        if fn is None:
            return self.reply(ctx, op, args)
        try:
            return {"ok": True, "result": copy.deepcopy(fn(self, ctx, args, side))}
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
        if out["party"]:  # the first member is the avatar (the mod's DB_Avatars; scn.rename_avatar renames it)
            out["party"][0]["avatar"] = True
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
        if self.reaction is not None:
            return {
                "screen": "reaction",
                "page": "ReactionDecisionPopup",
                "reaction": copy.deepcopy(self.reaction),
                "region": self.region,
                "blocking": True,
                "ms": 1,
            }
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

    def _failed_job(self, kind: str, error: str, side: str | None = None) -> dict[str, Any]:
        """A guard refusal (Actions.lua guarded): a job that settled failed in the call."""
        jid = self.next_job
        self.next_job += 1
        self.jobs[jid] = {"id": jid, "kind": kind, "status": "failed", "error": error, "elapsed_ms": 0}
        if side:
            self.job_sides[jid] = side
        return {"job": jid, "status": "failed", "error": error}

    # ---- a match, from one side's view ----

    def _match(self) -> Arena:
        assert self.arena is not None
        return self.arena

    def arena_status(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        party = [m.brief(a.selected[side], self.level) for m in a.teams[side]]
        for b in party:
            b["in_combat"] = True
        now = a.acting.uuid
        return {
            "party": party,
            "active": a.selected[side],
            "level": self.level,
            "region": self.region,
            "in_combat": True,
            "turn_based": True,
            "in_dialogue": False,
            "gold": self.gold,
            "camp": [],
            "combat": {"round": a.round, "current": now if a.sees(side, now) else None},
        }

    def arena_combat(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        now = a.acting.uuid
        order: list[dict[str, Any]] = []
        for i, uuid in enumerate(a.order):
            if not a.sees(side, uuid):
                continue
            m = a.member(uuid)
            row: dict[str, Any] = {
                "uuid": uuid,
                "name": m.name,
                "relation": "party" if a.side_of(uuid) == side else "hostile",
                "initiative": 20 - i,
                "hp_pct": round(100 * m.hp / m.max_hp),
                "pos": list(m.pos),
            }
            if uuid == now:
                row["is_current"] = True
            if m.hp <= 0 and not m.dead:  # a player character at 0 HP is down, visibly, to both sides (S.combat)
                row["downed"] = True
            order.append(row)
        seen = a.sees(side, now)
        return {
            "active": True,
            "round": a.round,
            "order": order,
            "current": now if seen else None,
            "turn_group": [now] if seen else [],
        }

    def arena_character(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        uuid = args.get("uuid") or a.selected[side]
        if not a.sees(side, uuid):
            raise RuntimeError(f"no visible character {uuid}")
        saved, self.party = self.party, [a.member(uuid)]
        try:
            return self.op_character(ctx, {"uuid": uuid})
        finally:
            self.party = saved

    def arena_events(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        ring = self.events[ctx]
        evs: list[dict[str, Any]] = []
        for e in ring.since(args.get("seq"), None, len(ring.records) or 1):
            views: dict[str, dict[str, Any]] | None = e.get("views")
            if views is not None and side not in views:
                continue
            ev = {k: v for k, v in e.items() if k != "views"}
            if views is not None:
                ev["data"] = views[side]
            evs.append(ev)
        return {"latest": ring.seq, "events": evs[: int(args.get("limit") or 200)]}

    def arena_ui_state(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        ui = self.op_ui_state(ctx, args)
        a = self._match()
        if self.reaction is not None and a.side_of(a.member(self.reaction["options"][0]["who"]).uuid) != side:
            ui["reaction"] = {"other_side": True}
        return ui

    def arena_job_poll(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        jid = int(args.get("id") or 0)
        if jid in self.job_sides and self.job_sides[jid] != side:
            raise RuntimeError("not your job")
        return self.op_job_poll(ctx, args)

    def arena_react(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        if self.reaction is None:
            raise RuntimeError("no reaction prompt on screen")
        who = self.reaction["options"][0]["who"]
        if a.side_of(a.member(who).uuid) != side:
            return self._failed_job("ui.react", "not your reaction", side)
        use = bool(args.get("use"))
        self.reacted.append((who, use))
        self.reaction = None
        return self._job("ui.react", {"answered": "use" if use else "skip"}, side)

    def arena_end_turn_op(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        why = a.refusal(side, str(args.get("char") or a.selected[side]))
        if why:
            return self._failed_job("act.end_turn", why, side)
        self.arena_end_turn()
        return self._job("act.end_turn", {"ended": True}, side)

    def arena_move_to(self, ctx: str, args: dict[str, Any], side: str) -> dict[str, Any]:
        a = self._match()
        char = str(args.get("char") or a.selected[side])
        why = a.refusal(side, char)
        if why:
            return self._failed_job("act.move_to", why, side)
        m = a.member(char)
        pos = [float(v) for v in args.get("pos") or m.pos]
        m.pos = (pos[0], pos[1], pos[2])
        self.moves.append((side, m.uuid, pos))
        return self._job("act.move_to", {"arrived": True, "pos": pos}, side)

    def _job(self, kind: str, result: dict[str, Any], side: str | None = None) -> dict[str, Any]:
        """A job that settles in the call (RPC.start_job + finish_job in one tick); `side`: the side that started it."""
        jid = self.next_job
        self.next_job += 1
        self.jobs[jid] = {"id": jid, "kind": kind, "status": "done", "result": result, "elapsed_ms": 0}
        if side:
            self.job_sides[jid] = side
        return {"job": jid, "status": "done", "result": result}

    def op_job_poll(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get(int(args.get("id") or 0))
        if job is None:
            raise RuntimeError(f"no such job {args.get('id')}")
        return job

    def op_events(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        ring = self.events[ctx]
        evs = ring.since(args.get("seq"), args.get("limit"), 200)
        return {"latest": ring.seq, "events": [{k: v for k, v in e.items() if k != "views"} for e in evs]}

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
        before, want = self.roll_mode, args.get("set")
        if want is not None:
            if not isinstance(want, bool):
                raise RuntimeError("set must be true or false")
            self.roll_mode = "WeightedRandom" if want else "Random"
            if self.scn_karmic_original is None:
                self.scn_karmic_original = before
        out = {"mode": self.roll_mode, "karmic": self.roll_mode == "WeightedRandom", "component_mode": self.roll_mode}
        return out if want is None else {**out, "before": before}

    def op_new_game(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        """ui.new_game: a new game at the difficulty the bridge asked for (the Options name); character creation."""
        self.difficulty = str(args.get("difficulty") or "Explorer")
        return self._job("ui.new_game", {"screen": "character_creation", "difficulty": self.difficulty})

    def op_scn_difficulty(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        if args.get("set") is not None:
            raise RuntimeError("difficulty is read-only here: the game applies it through its Options flow")
        return {"difficulty": self.difficulty, "rulesets": [{"uuid": _uuid(900), "name": self.difficulty}]}

    def op_scn_rename_avatar(self, ctx: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scn()
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("name must be 1..40 letters, digits, spaces, _ or -")
        host = self.party[0]
        if self.scn_avatar is None or self.scn_avatar["uuid"] != host.uuid:
            self.scn_avatar = {"uuid": host.uuid, "original": host.name}
        self.scn_avatar["name"] = name
        before, host.name = host.name, name
        original = self.scn_avatar["original"]
        return {"uuid": host.uuid, "original": original, "before": before, "name": name, "display": name, "cc": name}

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
        out: dict[str, Any] = {"restored": restored}
        if self.scn_karmic_original is not None:
            self.roll_mode = out["karmic"] = self.scn_karmic_original
            self.scn_karmic_original = None
        a, self.scn_avatar = self.scn_avatar, None
        if a is not None and self.party[0].uuid == a["uuid"] and self.party[0].name == a["name"]:
            self.party[0].name = out["avatar"] = a["original"]
        return out

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


# In a match, the ops whose answer depends on the side asking (GameModel.reply_side); the rest answer as without one.
SideOpFn = Callable[[GameModel, str, dict[str, Any], str], Any]
_ARENA_OPS: dict[str, SideOpFn] = {
    "state.status": GameModel.arena_status,
    "state.combat": GameModel.arena_combat,
    "state.character": GameModel.arena_character,
    "events.since": GameModel.arena_events,
    "act.end_turn": GameModel.arena_end_turn_op,
    "act.move_to": GameModel.arena_move_to,
    "job.poll": GameModel.arena_job_poll,
    "ui.react": GameModel.arena_react,
    "ui.state": GameModel.arena_ui_state,
}

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
    "ui.new_game": GameModel.op_new_game,
    "ref.since": lambda m, c, a: m.ring_reply(m.ref, a),
    "ref.snapshot": GameModel.op_ref_snapshot,
    "roll.since": lambda m, c, a: m.ring_reply(m.rolls, a),
    "roll.snapshot": GameModel.op_roll_snapshot,
    "scn.seed": GameModel.op_scn_seed,
    "scn.karmic": GameModel.op_scn_karmic,
    "scn.rename": GameModel.op_scn_rename,
    "scn.difficulty": GameModel.op_scn_difficulty,
    "scn.rename_avatar": GameModel.op_scn_rename_avatar,
    "scn.undo": GameModel.op_scn_undo,
    "scn.status": GameModel.op_scn_status,
}
