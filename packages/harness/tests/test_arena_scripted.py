# pyright: basic
"""The scripted arena client: its read of get_combat_state (the recorded golden text and the bridge's rendering of a
mod-shaped state.combat), its one pass per own member, and a whole match offline: run_arena with LiveAgents, each
side's scripted client process starting that side's real bridge against one fake mod playing the two-team match."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from bg3harness.arena import live as alive
from bg3harness.arena import match as am
from bg3harness.arena import scripted_client as sc
from bg3harness.arena.agents import LiveAgents, SideClient
from bg3harness.arena.spec import ArenaSpec, Side
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.mcp_bridge import bridge_env, bridge_session, call
from bg3harness.testing.scripted import FakeClock, GameModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_arena_match import ARENA, ArenaModel, arena_events, grade, phases

REPO = Path(__file__).resolve().parents[3]
GOLDEN = REPO / "packages" / "harness" / "tests" / "fixtures" / "golden" / "bridge" / "combat.txt"


def test_the_recorded_combat_text_reads_as_rows_and_the_turn_group():
    text = GOLDEN.read_text().split("=== ")[1].split("\n", 1)[1].split("EVENTS")[0]
    view = sc.parse_combat(text)
    assert view is not None and view.turn_group == ["e1"]
    assert [(r.handle, r.name, r.relation, r.pos, r.standing) for r in view.rows] == [
        ("e1", "Tav", "party", (10.0, 0.0, 10.0), True),
        ("e2", "Shadowheart", "party", (11.0, 0.0, 10.0), True),
        ("e5", "Goblin", "enemy", (15.0, 0.0, 12.0), True),
        ("e6", "Goblin Archer (Goblin)", "enemy", (16.0, 0.0, 12.0), True),
    ]
    me = sc.actor(view)
    assert me is not None and me.name == "Tav"
    near = sc.nearest(view, me)
    assert near is not None and near[0].name == "Goblin"


MOD_COMBAT = {
    "active": True,
    "round": 2,
    "current": "c7c13742-bacd-460a-8f65-f864fe41f255",
    "turn_group": ["c7c13742-bacd-460a-8f65-f864fe41f255", "ad9af97d-75da-406a-ae13-7071c563f604"],
    "order": [
        {
            "uuid": "25721313-0c15-4935-8176-9f134385451b",
            "name": "Nightwarden Minthara",
            "relation": "hostile",
            "initiative": 18,
            "hp_pct": 40,
            "pos": [3.0, 0.0, 0.0],
        },
        {
            "uuid": "58a69333-40bf-8358-1d17-fff240d7fb12",
            "name": "Lae'zel",
            "relation": "party",
            "initiative": 15,
            "hp_pct": 0,
            "downed": True,
            "pos": [0.0, 0.0, 0.0],
        },
        {
            "uuid": "c7c13742-bacd-460a-8f65-f864fe41f255",
            "name": "Astarion",
            "relation": "party",
            "initiative": 14,
            "hp_pct": 80,
            "is_current": True,
            "can_act": True,
            "pos": [1.0, 0.0, 0.0],
        },
        {
            "uuid": "ad9af97d-75da-406a-ae13-7071c563f604",
            "name": "Gale",
            "relation": "party",
            "initiative": 12,
            "hp_pct": 100,
            "can_act": True,
            "pos": [0.0, 0.0, 1.0],
        },
        {
            "uuid": "0de603c5-42e2-4811-9dad-f652de080eba",
            "name": "Minsc",
            "relation": "hostile",
            "initiative": 9,
            "hp_pct": 0,
            "dead": True,
            "pos": [1.5, 0.0, 0.0],
        },
    ],
}  # fmt: skip  (the mod's state.combat keys, StateWorld.lua)


class ModCombat(GameModel):
    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Any:
        if op == "state.combat":
            return {"ok": True, "result": MOD_COMBAT}
        return super().reply(ctx, op, args)


@pytest.mark.integration
def test_a_mod_shaped_phase_reads_through_the_bridge_with_its_members_and_who_is_down(tmp_path: Path) -> None:
    async def read() -> str:
        with FakeMod(ModCombat(clock=FakeClock())) as fake:
            async with bridge_session(fake.port, tmp_path) as s:
                return await call(s, "get_combat_state")

    text = asyncio.run(asyncio.wait_for(read(), 120))
    view = sc.parse_combat(text)
    assert view is not None and len(view.turn_group) == 2, text
    rows = {r.name: r for r in view.rows}
    assert set(rows) == {"Nightwarden Minthara", "Lae'zel", "Astarion", "Gale", "Minsc"}, text
    assert [n for n, r in rows.items() if not r.standing] == ["Lae'zel", "Minsc"], text
    me = sc.actor(view)
    assert me is not None and me.name == "Astarion"
    near = sc.nearest(view, me)  # Minsc is nearer but dead
    assert near is not None and (near[0].name, near[1]) == ("Nightwarden Minthara", 2.0)
    assert sc.parse_combat("Not in combat.") is None


class Calls:
    """A step's tool calls, answered from scripted texts (each read of get_combat_state takes the next one)."""

    def __init__(self, reads: list[str], answers: dict[str, tuple[str, bool]] | None = None) -> None:
        self.reads, self.answers, self.log = list(reads), answers or {}, []
        self.slept: list[float] = []

    def __call__(self, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.log.append((tool, args))
        if tool == "get_combat_state":
            return self.reads.pop(0), False
        if tool == "use_ability":
            return self.answers.get(args["ability"], ("done", False))
        return self.answers.get(tool, ("done", False))


def fight(me: tuple[float, float, float], foe: tuple[float, float, float]) -> str:
    return (
        "COMBAT round=1 current=e1 turn_group=[e1]\n"
        f"  e1 Karlach party init 20 hp 100% current @({me[0]:g},{me[1]:g},{me[2]:g})\n"
        f"  e2 Gale hostile init 19 hp 100% @({foe[0]:g},{foe[1]:g},{foe[2]:g})\n"
    )


def test_a_member_in_reach_attacks_then_ends_its_turn():
    calls = Calls([fight((0, 0, 0), (2, 0, 0))])
    assert sc.step(calls, calls.slept.append) == "attacked"
    assert [t for t, _a in calls.log] == ["wait_for", "get_combat_state", "use_ability", "end_turn"]
    assert calls.log[2][1] == {"ability": "Main Hand Attack", "target": "e2", "who": "e1"}
    assert calls.log[3][1] == {"who": "e1", "wait": False}


def test_a_member_out_of_reach_walks_to_the_nearest_enemy_and_attacks_when_it_got_there():
    calls = Calls([fight((0, 0, 0), (9, 0, 0)), fight((7.5, 0, 0), (9, 0, 0))])
    assert sc.step(calls, calls.slept.append) == "moved+attacked"
    tools = [t for t, _a in calls.log]
    assert tools == ["wait_for", "get_combat_state", "move_to", "get_combat_state", "use_ability", "end_turn"]
    assert calls.log[2][1] == {"target": "e2", "who": "e1", "within": 1.5, "wait": True}


def test_a_member_that_didnt_get_there_ends_its_turn_without_attacking():
    calls = Calls([fight((0, 0, 0), (30, 0, 0)), fight((9, 0, 0), (30, 0, 0))])
    assert sc.step(calls, calls.slept.append) == "moved"
    assert "use_ability" not in [t for t, _a in calls.log]


def test_a_member_without_the_weapon_attack_uses_its_unarmed_strike():
    calls = Calls([fight((0, 0, 0), (1, 0, 0))], {"Main Hand Attack": ("Karlach has no action called 'x'", True)})
    sc.step(calls, calls.slept.append)
    assert [a["ability"] for t, a in calls.log if t == "use_ability"] == ["Main Hand Attack", "Unarmed Strike"]


def test_no_fight_or_no_own_member_to_act_waits_and_reads_again():
    calls = Calls(["Not in combat."])
    assert sc.step(calls, calls.slept.append) == "waited" and calls.slept == [sc.POLL_S]
    other = "COMBAT round=1 current=e2 turn_group=[e2]\n  e1 Karlach party init 20 hp 100% @(0,0,0)\n"
    calls = Calls([other])
    assert sc.step(calls, calls.slept.append) == "waited"
    assert [t for t, _a in calls.log] == ["wait_for", "get_combat_state"]


def test_an_end_that_fails_waits_before_the_next_read():
    calls = Calls([fight((0, 0, 0), (1, 0, 0))], {"end_turn": ("FAILED: not your turn (side B is acting)", True)})
    sc.step(calls, calls.slept.append)
    assert calls.slept == [sc.POLL_S]


def test_the_client_command_starts_the_scripted_process_and_reads_its_stream_as_claudes(tmp_path):
    from bg3harness import clients

    c = sc.ScriptedClient(tmp_path / "mcp.json")
    req = clients.Request("go", "none", tmp_path, session="s-1")
    assert c.command(req)[1:] == ["-m", "bg3harness.arena.scripted_client", "--mcp-config", str(tmp_path / "mcp.json"),
                                  "--session-id", "s-1"]  # fmt: skip
    assert c.command(clients.Request("go", "none", tmp_path, session="s-1", resume=True))[-2:] == ["--resume", "s-1"]
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": ["mcp__bg3__end_turn"],
            "mcp_servers": [{"name": "bg3", "status": "connected"}]}  # fmt: skip
    assert [e.kind for e in c.parse(json.dumps(init))] == ["init"]


# ------------------------------------------------------------------------------------------------- a whole match


class FightModel(ArenaModel):
    """ArenaModel plus what a scripted client's attack needs: each character's actions (one weapon attack), a cast
    that hits the target for HIT when it stands within REACH of the caster, and a walk to a target. A downed
    character's turn passes by itself (the game's death save), as the mod's phase never holds it open."""

    HIT = 6
    REACH = 2.0

    def arena_end_turn(self) -> None:
        super().arena_end_turn()
        a = self._match()
        while self.ended is None and a.acting.hp == 0:
            super().arena_end_turn()

    def reply_side(self, ctx: str, op: str, args: dict[str, Any], side: str) -> Any:
        if self.arena is None:
            return super().reply_side(ctx, op, args, side)
        a = self._match()
        if op == "state.actions":
            acts = [{"name": "Main Hand Attack", "spell": "Target_MainHandAttack", "cost": {"action": 1}}]
            return {"ok": True, "result": {"actions": acts, "resources": {"ActionPoint": [1, 1], "Movement": [9, 9]}}}
        if op == "act.cast":
            me, foe = a.member(args["char"]), a.member(args["target"])
            if (why := a.refusal(side, me.uuid)) or a.side_of(foe.uuid) == side:
                return {"ok": True, "result": self._failed_job("act.cast", why or "a teammate", side)}
            dist = sum((p - q) ** 2 for p, q in zip(me.pos, foe.pos, strict=True)) ** 0.5
            if dist > self.REACH:
                return {"ok": True, "result": self._failed_job("act.cast", "out of reach", side)}
            self.arena_damage(foe.name, me.name, self.HIT)
            return {"ok": True, "result": self._job("act.cast", {"cast": True}, side)}
        if op == "act.move_to" and args.get("target"):
            me, foe = a.member(args["char"]), a.member(args["target"])
            if not a.refusal(side, me.uuid):
                me.pos = (foe.pos[0] - 1.0, foe.pos[1], foe.pos[2])
        return super().reply_side(ctx, op, args, side)


class FakeModGame:
    """ArenaGame over the fake mod, sent the way bg3harness.game.io.rpc sends a mod op."""

    def __init__(self, fake: FakeMod) -> None:
        self.fake = fake

    def rpc(self, op: str, args: dict[str, Any], side: str | None = None, context: str = "server") -> Any:
        payload = json.dumps({"op": op, "args": args or {}, **({"side": side} if side else {})})
        reply = json.loads(self.fake.answer(context, f"return Mods.BG3AI.BG3AI.rpc([==[{payload}]==])"))
        if not reply.get("ok"):
            raise RuntimeError(f"{op}: {reply.get('error')}")
        return reply.get("result")

    def alive(self) -> bool:
        return True


@pytest.mark.integration
def test_two_scripted_clients_play_a_match_to_its_end_through_their_bridges(tmp_path: Path) -> None:
    arena = dict(ARENA, round_cap=6)
    spec = ArenaSpec.from_dict(arena)
    model = FightModel(FakeClock())
    sides = {s: Side(s, sc.PLAYER, sc.SCRIPTED, sc.SCRIPTED, "none") for s in ("A", "B")}
    out = tmp_path / "match"
    with FakeMod(model) as fake:
        side_clients = {}
        for s in ("A", "B"):
            home = {k: v for k, v in bridge_env(fake.port, tmp_path / s).items() if k.startswith("BG3")}
            server = {"type": "stdio", "command": sys.executable, "args": ["-m", "bg3ai.server"], "env": home}
            cfg = tmp_path / f"mcp-{s}.json"
            cfg.write_text(json.dumps({"mcpServers": {"bg3": server}}))
            env = alive.side_env(spec, s, None, out)
            side_clients[s] = SideClient(sc.ScriptedClient(cfg), "none", None, env)
        agents = LiveAgents(side_clients, out, dict(os.environ), spec.wall_cap_min)
        outcome = am.run_arena(spec, sides, FakeModGame(fake), agents, out, alive.WallClock())
    assert outcome.invalid == "scripted: side A and B played the scripted client (a live check, never graded)"
    assert outcome.end is not None and (outcome.end["reason"], outcome.end["winner"]) == ("wipe", "A")  # A first
    assert not [e for e in arena_events(out) if e["ev"] == "arena_turn_forced"]
    assert outcome.cost("A") == 0 and outcome.cost("B") == 0
    for s in ("A", "B"):  # each side's events file, written from its client's stream
        evs = [json.loads(x) for x in (out / f"{s}.events.jsonl").read_text().splitlines()]
        calls = [e for e in evs if e.get("kind") == "tool_call"]
        assert {c["tool"] for c in calls} >= {"wait_for", "get_combat_state", "end_turn"}
        assert any(e.get("kind") == "init" for e in evs)
    hits = [r for r in model.events["server"].records if r["kind"] == "damage"]
    assert hits, "no attack landed"
    assert phases(out)  # the match's turns are in facts
    starts = [e for e in arena_events(out) if e["ev"] == "arena_start"]
    assert starts[0]["sides"] == {"A": "scripted/none", "B": "scripted/none"}
    g = grade(out, arena)
    assert (g["valid"], g["invalid_reason"]) == (False, "scripted")  # graded, never counted
