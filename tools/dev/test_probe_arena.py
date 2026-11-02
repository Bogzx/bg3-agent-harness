"""Offline tests for tools/dev/probe_arena.py (no game needed): the Lua it builds, the verdicts, and the order of
calls its steps make against a scripted game.

Run: uv run pytest -q tools/dev/test_probe_arena.py
"""

from __future__ import annotations

import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_arena as pa
import probe_arena_judges as pj

A = [f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in range(4)]
B = [f"bbbbbbbb-0000-0000-0000-00000000000{i}" for i in range(4)]
TEAMS = {"A": A, "B": B}


class LuaBuildTest(unittest.TestCase):
    def test_lua_dict_literal(self):
        self.assertEqual(pa.lua({"a-1": "A", "b": 2}), '{["a-1"] = "A", ["b"] = 2}')

    def test_lua_literals(self):
        self.assertEqual(pa.lua(None), "nil")
        self.assertEqual(pa.lua(True), "true")
        self.assertEqual(pa.lua(3), "3")
        self.assertEqual(pa.lua(100.0), "100.0")
        self.assertEqual(pa.lua('Lae"zel'), '"Lae\\"zel"')
        self.assertEqual(pa.lua(["x", 1, [2]]), '{"x", 1, {2}}')

    def test_call_code_tries_each_form(self):
        self.assertEqual(
            pa.call_code("SetHitpoints", ["u", 5, "Guaranteed"], ["u", 5]),
            'return P.js(P.call("SetHitpoints", {{"u", 5, "Guaranteed"}, {"u", 5}}))',
        )


class GeometryTest(unittest.TestCase):
    def test_line_slots_centred_on_the_spot(self):
        self.assertEqual(
            pa.line_slots([10.0, 1.0, 5.0], 4),
            [[7.75, 1.0, 5.0], [9.25, 1.0, 5.0], [10.75, 1.0, 5.0], [12.25, 1.0, 5.0]],
        )

    def test_toward_and_away(self):
        self.assertEqual(pa.toward([0.0, 1.0, 0.0], [10.0, 9.0, 0.0], 3.0), [3.0, 1.0, 0.0])
        self.assertEqual(pa.away([0.0, 1.0, 0.0], [10.0, 9.0, 0.0], 6.0), [-6.0, 1.0, 0.0])

    def test_dist_of_missing_positions_is_infinite(self):
        self.assertEqual(pa.dist(None, [0, 0, 0]), float("inf"))

    def test_spot_arg(self):
        self.assertEqual(pa.spot_arg("1,2.5,-3"), [1.0, 2.5, -3.0])
        with self.assertRaises(pa.argparse.ArgumentTypeError):
            pa.spot_arg("1,2")


# The spike's level (crash site): the companions' Companion<N> factions, their parent "Hero" (used by nobody), and
# multiplayer's Hero Player<N>, two of them held by the party.
SPIKE_FACTIONS = [
    {"name": "Companion4", "osi": "Companion4_1b5b", "used": 1, "to_a": 100},
    {"name": "Companion11", "osi": "Companion11_a364", "used": 0, "to_a": 100},
    {"name": "Hero", "osi": "Hero_a154", "used": 0, "to_a": 100},
    {"name": "Hero Player1", "osi": "Hero Player1_6545", "used": 5, "to_a": 100},
    {"name": "Hero Player2", "osi": "Hero Player2_9fac", "used": 1, "to_a": 100},
    {"name": "Hero Player4", "osi": "Hero Player4_e69d", "used": 0, "to_a": 100},
    {"name": "Hero Player3", "osi": "Hero Player3_2869", "used": 0, "to_a": 100},
    {"name": "Hero Player10", "osi": "Hero Player10_x", "used": 0, "to_a": 100},
    {"name": "Goblin", "osi": "Goblin_2", "used": 9, "to_a": 0},
]


class FactionPickTest(unittest.TestCase):
    def test_two_lowest_spare_player_factions_never_the_bare_hero(self):
        self.assertEqual(
            pj.pick_factions(SPIKE_FACTIONS, {"Companion4_1b5b"}), ("Hero Player3_2869", "Hero Player4_e69d")
        )

    def test_a_faction_a_member_holds_is_not_spare(self):
        self.assertEqual(
            pj.pick_factions(SPIKE_FACTIONS, {"Hero Player3_2869"}), ("Hero Player4_e69d", "Hero Player10_x")
        )

    def test_none_with_fewer_than_two_spare(self):
        self.assertIsNone(
            pj.pick_factions(SPIKE_FACTIONS, {"Hero Player3_2869", "Hero Player4_e69d", "Hero Player10_x"})
        )


def matrix(enemy_across: int = 1, enemy_within: int = 0) -> dict[str, int]:
    """A full IsEnemy matrix of TEAMS: every ordered pair across and within the teams."""
    m = {f"AB:{x}>{y}": enemy_across for x in A for y in B} | {f"BA:{y}>{x}": enemy_across for x in A for y in B}
    return m | {f"{s}{s}:{x}>{y}": enemy_within for s in pa.SIDES for x in TEAMS[s] for y in TEAMS[s] if x != y}


def facts_ok(u: str, slot: list[float], **kw: Any) -> dict[str, Any]:
    return {"uuid": u, "name": u[:4], "in_party_db": True, "pos": slot, "hp": 20, "max_hp": 20, **kw}


class JudgeTest(unittest.TestCase):
    def test_setup_pass_and_each_failure(self):
        slots = {s: pa.line_slots([0.0, 0.0, 20.0 * i], 4) for i, s in enumerate(pa.SIDES)}
        facts = {u: facts_ok(u, sl) for s in pa.SIDES for u, sl in zip(TEAMS[s], slots[s], strict=True)}
        self.assertEqual(pj.judge_setup(TEAMS, facts, slots)[0], "PASS")
        facts[B[2]] = facts_ok(B[2], [50.0, 0.0, 0.0], in_party_db=False, hp=3)
        v, why = pj.judge_setup(TEAMS, facts, slots)
        self.assertEqual(v, "FAIL")
        self.assertIn("not in DB_PartyMembers", why)
        self.assertIn("from its slot", why)
        self.assertIn("hp 3/20", why)

    def test_hostile(self):
        facts = {u: {"combat": "C1"} for u in A + B}
        self.assertEqual(pj.judge_hostile(TEAMS, facts, matrix())[0], "PASS")
        v, why = pj.judge_hostile(TEAMS, {**facts, B[3]: {"combat": None}}, matrix())
        self.assertEqual(v, "FAIL")
        self.assertIn("combat guids", why)

    def test_hostile_fails_enemies_within_a_team_in_combat(self):
        # the spike: a relation on the companions' parent made Shadowheart her own team's enemy
        inside = matrix() | {f"AA:{A[0]}>{A[1]}": 1, f"AA:{A[1]}>{A[0]}": 1}
        v, why = pj.judge_hostile(TEAMS, {u: {"combat": "C1"} for u in A + B}, inside)
        self.assertEqual(v, "FAIL")
        self.assertIn(f"in combat: IsEnemy within a team: 2/24 read of 24 (AA:{A[0]}>{A[1]}", why)

    def test_hostile_fails_part_of_the_cross_pairs_or_a_missing_read(self):
        facts = {u: {"combat": "C1"} for u in A + B}
        some = matrix(enemy_across=0) | {f"AB:{A[0]}>{y}": 1 for y in B}  # only team A's leader an enemy of B
        v, why = pj.judge_hostile(TEAMS, facts, some)
        self.assertEqual(v, "FAIL")
        self.assertIn("in combat: IsEnemy across the teams: 4/32", why)
        one_way = {k: v for k, v in matrix().items() if not k.startswith("BA:")}
        self.assertIn("IsEnemy across the teams: 16/32", pj.judge_hostile(TEAMS, facts, one_way)[1])
        short = {k: v for k, v in matrix().items() if not k.startswith("BB:")}
        self.assertIn("within a team: 0/12 read of 24", pj.judge_hostile(TEAMS, facts, short)[1])

    def test_stat_changes(self):
        before = {A[0]: {"name": "Minthara", "level": 4, "max_hp": 52}, A[1]: {"name": "Gale", "level": 2, "max_hp": 8}}
        after = {A[0]: {"level": 4, "max_hp": 11}, A[1]: {"level": 2, "max_hp": 8}}
        st = pj.stat_changes(before, after)
        self.assertEqual(st[A[0]], {"name": "Minthara", "level": [4, 4], "max_hp": [52, 11], "changed": True})
        self.assertFalse(st[A[1]]["changed"])

    def test_turns(self):
        turns = [
            {"uuid": A[0], "group": [A[0]], "round": 1, "initiative": 18},
            {"uuid": B[0], "group": [B[0]], "round": 1, "initiative": 12},
            {"uuid": A[1], "group": [A[1], A[2]], "round": 1, "initiative": 9},
        ]
        self.assertEqual(pj.judge_turns(TEAMS, turns, None)[0], "PASS")
        mixed = [*turns, {"uuid": B[1], "name": "Wyll", "group": [B[1], A[3]], "round": 1, "initiative": 5}]
        self.assertIn("held both sides: Wyll", pj.judge_turns(TEAMS, mixed, None)[1])
        disorder = [*turns, {"uuid": B[2], "group": [B[2]], "round": 1, "initiative": 15}]
        self.assertIn("rounds [1] not in initiative order", pj.judge_turns(TEAMS, disorder, None)[1])
        self.assertIn("sides ['A'] only", pj.judge_turns(TEAMS, turns[:1], None)[1])
        down = {"who": "Gale", "downed": True, "combat_after": False, "turns_after": 2}
        self.assertIn("after Gale was downed: combat False", pj.judge_turns(TEAMS, turns, down)[1])
        down = {"who": "Gale", "downed": True, "combat_after": True, "turns_after": 2}
        self.assertEqual(pj.judge_turns(TEAMS, turns, down)[0], "PASS")

    def test_ai(self):
        still = {"moved_m": 0.0, "casts": [], "ended_by_itself": False}
        self.assertEqual(pj.judge_ai([])[0], "OPEN")
        self.assertEqual(pj.judge_ai([{"name": "Tav", "idle": still}])[0], "PASS")
        moved = {"name": "Wyll", "idle": {**still, "moved_m": 3.2}}
        self.assertIn("Wyll", pj.judge_ai([moved])[1])
        self.assertEqual(pj.judge_ai([{"name": "Lae", "idle": {**still, "casts": ["Target_X"]}}])[0], "FAIL")
        # a downed member's turn passes by itself (death saving throw): not the AI
        self.assertEqual(
            pj.judge_ai([{"name": "Gale", "downed_at_start": True, "idle": {**still, "ended_by_itself": True}}])[0],
            "PASS",
        )

    def test_res_amount_reads_both_shapes(self):
        self.assertEqual(pj.res_amount({"ActionPoint": {"0": 1}}, "ActionPoint"), 1.0)
        self.assertEqual(pj.res_amount({"SpellSlot": [2, 1]}, "SpellSlot"), 3.0)
        self.assertIsNone(pj.res_amount({}, "Movement"))

    def test_act(self):
        def run(mv_after: float, ap_after: float, end: str = "done") -> dict[str, Any]:
            return {
                "move": {
                    "status": "done",
                    "res_before": {"Movement": {"0": 9}},
                    "res_after": {"Movement": {"0": mv_after}},
                },
                "cast": {
                    "status": "done",
                    "res_before": {"ActionPoint": {"0": 1}},
                    "res_after": {"ActionPoint": {"0": ap_after}},
                },
                "end": {"status": end},
            }

        self.assertEqual(pj.judge_act({"A": run(4.5, 0), "B": run(3, 0)})[0], "PASS")
        v, why = pj.judge_act({"A": run(9, 0), "B": run(3, 1, end="failed")})
        self.assertEqual(v, "FAIL")
        self.assertIn("side A move: Movement 9.0 -> 9.0 (not charged)", why)
        self.assertIn("side B cast: ActionPoint 1.0 -> 1.0", why)
        self.assertIn("side B end_turn: failed", why)
        self.assertIn("side B: no turn reached", pj.judge_act({"A": run(4, 0)})[1])

    def test_react(self):
        self.assertEqual(pj.judge_react({"error": "x"})[0], "OPEN")
        ok = {"popup_seen": {"open": True}, "answer": {"ok": True}, "reactor_name": "Wyll", "reactor_side": "B"}
        self.assertEqual(pj.judge_react(ok), ("PASS", "prompt for Wyll (side B), answered by ui.react"))
        self.assertEqual(pj.judge_react({**ok, "answer": {"ok": False}})[0], "FAIL")
        self.assertEqual(pj.judge_react({"events": [{"ev": "ReactionInterruptUsed"}]})[0], "OPEN")
        self.assertEqual(pj.judge_react({"events": []})[0], "FAIL")

    def test_end_records_what_the_game_does_after_a_wipe(self):
        base = {"all_down_read": True, "death_save_turns": [{"who": ["Gale"]}, {"who": ["Wyll"]}]}
        v, why = pj.judge_end({**base, "ended_at": "dead", "end_event": "CombatEnded"})
        self.assertEqual(v, "PASS")
        self.assertIn("death-save turns: 2; combat ended at the dead stage (CombatEnded)", why)
        self.assertIn("never ended by itself", pj.judge_end(base)[1])
        self.assertIn("Game Over screen", pj.judge_end({**base, "game_over": "dead"})[1])  # the team's loss, no fault
        self.assertEqual(pj.judge_end({"all_down_read": False})[0], "FAIL")

    def test_gameover(self):
        self.assertEqual(pj.judge_gameover({"dead": True, "game_over": False})[0], "PASS")
        self.assertEqual(pj.judge_gameover({"dead": True, "game_over": True})[0], "FAIL")
        self.assertIn("downed alone", pj.judge_gameover({"game_over_downed": True})[1])
        self.assertEqual(pj.judge_gameover({"dead": False})[0], "OPEN")

    def test_conns(self):
        rows = [{"i": i, "connected": True, "answer": "1"} for i in range(6)]
        self.assertEqual(pj.judge_conns(rows, 6)[0], "PASS")
        rows[5] = {"i": 5, "connected": False, "error": "ConnectionRefusedError"}
        self.assertEqual(pj.judge_conns(rows, 6), ("FAIL", "5/6 answered: #5: ConnectionRefusedError"))

    def test_party_and_pinned_mods(self):
        facts = {u: {"in_party_db": True} for u in A + B}
        mods = [{"name": "GustavDev", "uuid": "g1"}, {"name": "BG3AI", "uuid": "m1"}]
        v, why = pj.judge_party(TEAMS, facts, mods, None)
        self.assertEqual(v, "PASS")
        self.assertIn("2 mods loaded (no pinned list given): GustavDev, BG3AI", why)
        self.assertEqual(pj.judge_party(TEAMS, facts, mods, ["GustavDev", "m1"])[0], "PASS")
        v, why = pj.judge_party(TEAMS, {**facts, B[1]: {"name": "Wyll"}}, [*mods, {"name": "PartyLimit"}], ["g1", "m1"])
        self.assertEqual(v, "FAIL")
        self.assertIn("not in DB_PartyMembers: Wyll", why)
        self.assertTrue(why.endswith("mods beyond the pinned set: PartyLimit"))  # GustavDev pinned by uuid

    def test_tie_info(self):
        order = {
            "groups": [
                [{"uuid": A[0], "initiative": 18}],
                [{"uuid": B[0], "initiative": 15}],
                [{"uuid": A[1], "initiative": 15}, {"uuid": B[1], "name": "Wyll", "initiative": 15}],
            ]
        }
        t = pj.tie_info(order, TEAMS)
        self.assertEqual(t["mixed_groups"], [[A[1], "Wyll"]])
        self.assertEqual(t["equal"], [[B[0], A[1]], [A[1], B[1]]])  # never two of one side
        self.assertEqual(t["adjacent"][0], [A[0], B[0], 18])

    def test_tie(self):
        self.assertEqual(pj.judge_tie({"before": {"equal": []}, "force": None})[0], "OPEN")
        self.assertEqual(pj.judge_tie({"after": {"equal": [[A[0], B[0]]]}})[0], "PASS")
        v, why = pj.judge_tie({"after": {"equal": [[A[0], B[0]]]}, "shared_turns": [["Tav", "Wyll"]]})
        self.assertEqual(v, "FAIL")
        self.assertIn("open together: ['Tav', 'Wyll']", why)
        self.assertEqual(pj.judge_tie({"before": {"mixed_groups": [["Tav", "Wyll"]]}})[0], "FAIL")

    def test_npc(self):
        self.assertEqual(pj.judge_npc({"act": {"refused": True}, "idle": {"acted": False}})[0], "OPEN")
        v, why = pj.judge_npc({"act": {"refused": False}, "idle": {"acted": True}})
        self.assertEqual(v, "FAIL")
        self.assertIn("the game AI acted", why)


def grp(*runs: list[str]) -> list[dict[str, Any]]:
    """Turn groups as P.turn_groups reads them: one group per run of uuids."""
    return [{"team": f"T{i}", "members": [{"uuid": u} for u in run]} for i, run in enumerate(runs)]


# The rerun's rolled order (round 2): Karlach Lae'zel Astarion Shadowheart Gale Wyll Minthara Halsin as B A A A A B B B
ROLLED = [B[0], A[0], A[1], A[2], A[3], B[1], B[2], B[3]]


class GroupsJudgeTest(unittest.TestCase):
    def rec(self, turns: list[list[str]], rnd: int = 3, **kw: Any) -> dict[str, Any]:
        return {
            "before": {"round": 2, "next": grp(ROLLED)},
            "judged_round": 3,
            "turns": [{"round": rnd, "open": t} for t in turns],
            **kw,
        }

    def test_pass_one_side_per_turn_in_the_rolled_order(self):
        v, why = pj.judge_groups(TEAMS, self.rec([[B[0]], [A[0], A[1], A[2], A[3]], [B[1], B[2], B[3]]]))
        self.assertEqual(v, "PASS", why)
        self.assertIn("(B A B)", why)

    def test_fail_the_spike_one_turn_for_all_eight(self):
        v, why = pj.judge_groups(TEAMS, self.rec([ROLLED]))
        self.assertEqual(v, "FAIL")
        self.assertIn("1 turn(s) open for both sides", why)

    def test_fail_out_of_the_rolled_order_or_someone_skipped(self):
        v, why = pj.judge_groups(TEAMS, self.rec([[A[0], A[1], A[2], A[3]], [B[0]], [B[1], B[2], B[3]]]))
        self.assertIn("out of the rolled order", why)
        v, why = pj.judge_groups(TEAMS, self.rec([[B[0]], [A[0], A[1], A[2], A[3]]]))
        self.assertEqual(v, "FAIL")
        self.assertIn("3 of the 8 had no turn", why)

    def test_only_the_judged_round_counts(self):
        r = self.rec([[B[0]], [A[0], A[1], A[2], A[3]], [B[1], B[2], B[3]]])
        r["turns"].insert(0, {"round": 2, "open": ROLLED})  # the round in progress, before the split took
        self.assertEqual(pj.judge_groups(TEAMS, r)[0], "PASS")
        self.assertEqual(pj.judge_groups(TEAMS, self.rec([[B[0]]], rnd=4))[0], "OPEN")

    def test_write_error_or_unread_order(self):
        self.assertEqual(
            pj.judge_groups(TEAMS, self.rec([], write={"error": "x"})), ("FAIL", "the split write failed: x")
        )
        r = self.rec([[B[0]]])
        r["before"] = {"error": "no TurnOrder"}
        self.assertEqual(pj.judge_groups(TEAMS, r)[0], "OPEN")


class FakeArena(pa.Arena):
    """Arena over scripted reads: facts per uuid, a turn queue the end_turn jobs advance, every call recorded."""

    def __init__(self, out: Path, facts: dict[str, dict[str, Any]], queue: list[list[str]]) -> None:
        super().__init__(game=None, out=out, sleep=lambda s: None, clock=self._tick)
        self.t, self.f, self.queue, self.calls, self.jobs, self.n = 0.0, facts, queue, [], [], 0

    def _tick(self) -> float:
        self.t += 1.0
        return self.t

    def facts(self, us: list[str]) -> dict[str, dict[str, Any]]:
        open_ = set(self.queue[0]) if self.queue else set()
        out = {}
        for u in us:
            f = json.loads(json.dumps(self.f[u]))
            f["tb"] = {"active": u in open_, "requested_end": False}
            out[u] = f
        return out

    def call(self, fn: str, *forms: list[Any]) -> dict[str, Any]:
        self.calls.append((fn, forms[0]))
        return {"fn": fn, "ok": True, "form": 1}

    def ev(self, code: str, timeout: float = 30.0) -> Any:
        if code.startswith("return P.who("):
            return json.loads(code[len("return P.who(") : -1])
        if "GetHostCharacter" in code:
            return A[0]
        if "P.order" in code:
            return {"round": 1, "groups": [[{"uuid": u, "initiative": 20 - i}] for i, u in enumerate(A + B)]}
        return {}

    def mark(self) -> int:
        return self.n

    def since(self, i: int) -> list[dict[str, Any]]:
        return [{"ev": "TurnStarted"}] if self.n > i else []

    def mod_open(self, teams: dict[str, Any]) -> list[dict[str, Any]]:  # one side per turn: the mod's view = open
        return self.active(teams)

    def raw(self, code: str, context: str) -> Any:
        self.raws = [*getattr(self, "raws", []), code]
        return {"ok": True}

    def job(self, op: str, args: dict[str, Any], wait_s: float = 20.0) -> dict[str, Any]:
        self.jobs.append((op, args))
        u = args["char"]
        if op == "act.end_turn" and self.queue and u in self.queue[0]:
            self.queue[0].remove(u)
            if not self.queue[0]:
                self.queue.pop(0)
                self.n += 1
        if op == "act.move_to":
            self.f[u]["res"]["Movement"]["0"] -= 4
        if op == "act.cast":
            self.f[u]["res"]["ActionPoint"]["0"] -= 1
        return {"op": op, "args": args, "status": "done"}

    def write(self, name: str, rec: dict[str, Any]) -> Path:
        self.written = getattr(self, "written", {})
        self.written[name] = rec
        return self.out / name


class GroupsArena(FakeArena):
    """A combat from round 2: each round a list of turns (the uuids open together); Osi EndTurn closes a member, the
    turn passes when all of it ended, the round when all its turns did."""

    def __init__(self, out: Path, facts: dict[str, dict[str, Any]], rounds: list[list[list[str]]]) -> None:
        super().__init__(out, facts, [])
        self.rounds, self.rnd, self.split_code = [[list(t) for t in r] for r in rounds], 0, ""

    def turn_groups(self, u: str) -> dict[str, Any]:
        if self.rnd >= len(self.rounds):
            return {"round": 2 + self.rnd}
        return {"round": 2 + self.rnd, "next": grp(ROLLED), "current": grp(*self.rounds[self.rnd])}

    def facts(self, us: list[str]) -> dict[str, dict[str, Any]]:
        open_ = set(self.rounds[self.rnd][0]) if self.rnd < len(self.rounds) and self.rounds[self.rnd] else set()
        return {
            u: {**json.loads(json.dumps(self.f[u])), "tb": {"active": u in open_, "requested_end": False}} for u in us
        }

    def ev(self, code: str, timeout: float = 30.0) -> Any:
        if "P.split_groups(" in code:
            self.split_code = code[code.index("P.split_groups(") : code.rindex(")")]
            return {"groups_written": 3}
        return super().ev(code, timeout)

    def end(self, u: str) -> None:
        if self.rnd < len(self.rounds):
            turn = self.rounds[self.rnd][0]
            turn.remove(u)
            if not turn:
                self.rounds[self.rnd].pop(0)
                self.n += 1
                if not self.rounds[self.rnd]:
                    self.rnd += 1

    def call(self, fn: str, *forms: list[Any]) -> dict[str, Any]:
        r = super().call(fn, *forms)
        if fn == "EndTurn":
            self.end(forms[0][0])
        return r

    def job(self, op: str, args: dict[str, Any], wait_s: float = 20.0) -> dict[str, Any]:
        if op != "act.end_turn":
            return super().job(op, args, wait_s)
        self.jobs.append((op, args))
        self.end(args["char"])
        return {"op": op, "args": args, "status": "done"}

    def mod_open(self, teams: dict[str, Any]) -> list[dict[str, Any]]:
        """The mod's acting side in a shared group: the leading same-side run of the turn's members left, in the
        rolled order."""
        if self.rnd >= len(self.rounds) or not self.rounds[self.rnd]:
            return []
        left = [u for u in ROLLED if u in self.rounds[self.rnd][0]]
        side = pj.side_of(teams, left[0])
        run = list(itertools.takewhile(lambda u: pj.side_of(teams, u) == side, left))
        return [{**self.f[u], "uuid": u} for u in run]

    def raw(self, code: str, context: str) -> Any:
        self.raws = [*getattr(self, "raws", []), code]
        return {"ok": True}


def standing(u: str, x: float) -> dict[str, Any]:
    return {
        "uuid": u,
        "name": u[:4],
        "pos": [x, 0.0, 0.0],
        "hp": 20,
        "max_hp": 20,
        "in_party_db": True,
        "res": {"Movement": {"0": 9}, "ActionPoint": {"0": 1}},
    }


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "teams.json").write_text(json.dumps({**TEAMS, "host": A[0]}))
        self.facts = {u: standing(u, 0.0 if u in A else 6.0) for u in A + B}

    def test_act_drives_to_each_side_and_charges_the_ops(self):
        ar = FakeArena(self.tmp, self.facts, [[B[1]], [A[2]], [B[0]]])
        rec = ar.act("Target_MainHandAttack")
        # side A's turn came after ending B[1]'s; side B's after A[2]'s own end
        self.assertEqual(
            [(op, a["char"]) for op, a in ar.jobs],
            [
                ("act.end_turn", B[1]),
                ("act.move_to", A[2]),
                ("act.cast", A[2]),
                ("act.end_turn", A[2]),
                ("act.move_to", B[0]),
                ("act.cast", B[0]),
                ("act.end_turn", B[0]),
            ],
        )
        self.assertEqual(rec["verdict"], "PASS", rec["why"])
        self.assertEqual(rec["runs"]["A"]["cast"]["args"]["target"], B[0])  # the nearest standing enemy

    def test_setup_calls_in_order(self):
        facts = {u: {**standing(u, 0.0), "in_party_db": u not in (B[2], B[3])} for u in A + B}
        ar = FakeArena(self.tmp, facts, [])
        ar.game = type("G", (), {"rpc": lambda self, op, args: {"seed": args["seed"]}})()
        rec = ar.setup(A, B, [0.0, 0.0, 0.0], [0.0, 0.0, 20.0], seed=7)
        fns = [c[0] for c in ar.calls]
        self.assertEqual(fns[0], "SetMaxPartySizeOverride")
        self.assertEqual([c[1] for c in ar.calls if c[0] == "PROC_GLO_PartyMembers_Add"], [[B[2], A[0]], [B[3], A[0]]])
        self.assertEqual(fns.count("DetachFromPartyGroup"), 8)
        self.assertEqual(
            [c[1] for c in ar.calls if c[0] == "AttachToPartyGroup"],
            [[u, A[0]] for u in A[1:]] + [[u, B[0]] for u in B[1:]],
        )
        self.assertEqual(fns.count("TeleportToPosition"), 8)
        self.assertLess(fns.index("TeleportToPosition"), fns.index("RestoreParty"))
        self.assertIn({"scn.seed": {"seed": 7}}, rec["calls"])
        saved = json.loads((self.tmp / "teams.json").read_text())
        self.assertEqual((saved["A"], saved["B"], saved["host"]), (A, B, A[0]))
        self.assertEqual(set(saved["before"]), set(A + B))
        self.assertEqual(saved["before"][B[1]]["max_hp"], 20)
        self.assertEqual(set(saved["before"][B[1]]), {"name", "faction", "base_faction", "level", "max_hp"})
        self.assertEqual(rec["stats"][B[1]]["max_hp"], [20, 20])

    def test_hostile_gives_each_team_its_own_spare_faction_both_ways(self):
        facts = {u: {**standing(u, 0.0), "faction": f"Companion{i}_c", "combat": "C1"} for i, u in enumerate(A + B)}
        ar = FakeArena(self.tmp, facts, [])
        evs: list[str] = []
        base_ev = ar.ev

        def ev(code: str, timeout: float = 30.0) -> Any:
            evs.append(code)
            if "P.factions(" in code:
                return SPIKE_FACTIONS
            if "IsEnemy" in code:
                return matrix()
            return base_ev(code, timeout)

        ar.ev = ev  # type: ignore[method-assign]
        rec = ar.hostile("auto")
        fa, fb = "Hero Player3_2869", "Hero Player4_e69d"
        self.assertEqual([c[1] for c in ar.calls if c[0] == "SetFaction"], [[u, fa] for u in A] + [[u, fb] for u in B])
        self.assertEqual([c[1] for c in ar.calls if c[0] == "SetRelation"], [[fa, fb, 0], [fb, fa, 0]])
        self.assertIn(["SetHostileAndEnterCombat", [fa, fb, A[0], B[0]]], [list(c) for c in ar.calls])
        self.assertEqual(sum("IsEnemy" in c for c in evs), 2)  # read before the combat call and once after (right)
        self.assertEqual((rec["enemy_in_combat_reads"], rec["before_combat_info"]), (1, []))
        self.assertEqual(rec["verdict"], "PASS", rec["why"])
        self.assertEqual(rec["factions_before"][B[0]], "Companion4_c")
        saved = json.loads((self.tmp / "teams.json").read_text())
        self.assertEqual((saved["faction_a"], saved["faction_b"]), (fa, fb))

    def hostile_over(self, reads: list[dict[str, int]]) -> dict[str, Any]:
        """q3 with the IsEnemy matrix read in this order (the last read repeats)."""
        facts = {u: {**standing(u, 0.0), "faction": f"Companion{i}_c", "combat": "C1"} for i, u in enumerate(A + B)}
        ar = FakeArena(self.tmp, facts, [])
        base_ev, left = ar.ev, list(reads)

        def ev(code: str, timeout: float = 30.0) -> Any:
            if "P.factions(" in code:
                return SPIKE_FACTIONS
            if "IsEnemy" in code:
                return left.pop(0) if len(left) > 1 else left[0]
            return base_ev(code, timeout)

        ar.ev = ev  # type: ignore[method-assign]
        return ar.hostile("auto")

    def test_hostile_before_combat_is_informational_live_load1(self):
        # live 2026-11-02 load1: B->A 0/16 before the combat call, 16/16 in combat
        one_way = matrix() | {f"BA:{y}>{x}": 0 for x in A for y in B}
        rec = self.hostile_over([one_way, matrix()])
        self.assertEqual(rec["verdict"], "PASS", rec["why"])
        self.assertEqual(rec["before_combat_info"], ["IsEnemy across the teams: 16/32"])

    def test_hostile_reads_the_matrix_again_until_right_within_3_s(self):
        one_way = matrix() | {f"BA:{y}>{x}": 0 for x in A for y in B}
        rec = self.hostile_over([matrix(), one_way, one_way, matrix()])
        self.assertEqual(rec["verdict"], "PASS", rec["why"])
        self.assertEqual(rec["enemy_in_combat_reads"], 3)

    def test_hostile_fails_when_the_matrix_is_still_wrong_after_3_s(self):
        one_way = matrix() | {f"BA:{y}>{x}": 0 for x in A for y in B}
        rec = self.hostile_over([matrix(), one_way])
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertIn("in combat: IsEnemy across the teams: 16/32", rec["why"])
        self.assertGreaterEqual(rec["enemy_in_combat_wait_s"], pa.ENEMY_WAIT_S)

    def test_enemy_matrix_reads_both_directions_across_and_within(self):
        sent: list[str] = []
        ar = FakeArena(self.tmp, self.facts, [])
        ar.ev = lambda code, timeout=30.0: sent.append(code) or {}  # type: ignore
        ar.enemy_matrix(TEAMS)
        code = sent[0]
        for k, n in (("AB:", 16), ("BA:", 16), ("AA:", 12), ("BB:", 12)):
            self.assertEqual(code.count(f'o["{k}'), n, k)
        self.assertIn(f'o["BA:{B[0]}>{A[0]}"] = P.q("IsEnemy", "{B[0]}", "{A[0]}")', code)

    def test_groups_splits_then_watches_the_next_round_whole(self):
        ar = GroupsArena(self.tmp, self.facts, rounds=[[ROLLED], [[B[0]], A, B[1:]], [[B[0]]]])
        rec = ar.groups("write", "next", None)
        self.assertIn(f'P.split_groups("{A[0]}", {{["{A[0]}"] = "A"', ar.split_code)
        self.assertTrue(ar.split_code.endswith(', "next")'))
        self.assertEqual(rec["judged_round"], 3)
        self.assertEqual([t["round"] for t in rec["turns"]], [2, 3, 3, 3])
        self.assertEqual(sorted(c[1][0] for c in ar.calls if c[0] == "EndTurn"), sorted([*ROLLED, B[0], *A, *B[1:]]))
        self.assertEqual(ar.written["q10-groups"]["verdict"], "PASS", ar.written["q10-groups"]["why"])

    def test_groups_baseline_fails_on_the_shared_group(self):
        ar = GroupsArena(self.tmp, self.facts, rounds=[[ROLLED], [ROLLED], [ROLLED]])
        rec = ar.groups("none", "next", None)
        self.assertNotIn("write", rec)
        self.assertEqual(ar.written["q10-groups"]["verdict"], "FAIL")

    def test_groups_user_split_assigns_team_b(self):
        ar = GroupsArena(self.tmp, self.facts, rounds=[[ROLLED], [ROLLED]])
        ar.groups("user", "next", 131073)
        self.assertEqual([c[1] for c in ar.calls if c[0] == "AssignToUser"], [[131073, u] for u in B])

    def test_groups_phases_judges_the_mods_view_of_a_shared_group(self):
        # the engine keeps all 8 in one group each round; the mod lets one side's run act at a time
        ar = GroupsArena(self.tmp, self.facts, rounds=[[ROLLED], [ROLLED], [ROLLED]])
        rec = ar.groups("phases", "next", None)
        self.assertIn("Mods.BG3AI.BG3AI.Arena.begin(", ar.raws[0])
        self.assertIn("Mods.BG3AI.BG3AI.Arena.reset()", ar.raws[-1])
        self.assertFalse([c for c in ar.calls if c[0] == "EndTurn"])  # ended through the mod's act.end_turn
        self.assertEqual([t["open"] for t in rec["turns"] if t["round"] == 3], [[B[0]], A, B[1:]])
        self.assertEqual(ar.written["q10-groups"]["verdict"], "PASS", ar.written["q10-groups"]["why"])

    def test_mod_open_asks_the_guard(self):
        sent: list[str] = []
        ar = FakeArena(self.tmp, self.facts, [])
        ar.ev = lambda code, timeout=30.0: sent.append(code) or [B[1]]  # type: ignore
        self.assertEqual([f["uuid"] for f in pa.Arena.mod_open(ar, TEAMS)], [B[1]])
        self.assertIn("local G = Mods.BG3AI.BG3AI.Guard", sent[0])
        self.assertIn("G.is_my_turn(u)", sent[0])

    def test_act_in_a_shared_group_ends_through_the_mods_view_one_at_a_time(self):
        # the rerun: all 8 in one group (B | AAAA | BBB); no end goes to a held member, and ends are SETTLE_S apart
        ar = GroupsArena(self.tmp, self.facts, rounds=[[ROLLED]])
        sleeps: list[float] = []
        ar.sleep = sleeps.append  # type: ignore[method-assign]
        rec = ar.act("Target_MainHandAttack")
        ends = [a["char"] for op, a in ar.jobs if op == "act.end_turn"]
        self.assertEqual(ends, [B[0], *A, *B[1:]])
        self.assertEqual(
            [(op, a["char"]) for op, a in ar.jobs if op != "act.end_turn"],
            [("act.move_to", A[0]), ("act.cast", A[0]), ("act.move_to", B[1]), ("act.cast", B[1])],
        )
        self.assertEqual(sleeps.count(pa.SETTLE_S) >= len(ends), True)
        self.assertEqual([j["args"]["char"] for j in rec["runs"]["A"]["ends"]], A)
        self.assertIn("Mods.BG3AI.BG3AI.Arena.begin(", ar.raws[1])  # not active yet: the probe begins it
        self.assertIn("Mods.BG3AI.BG3AI.Arena.reset()", ar.raws[-1])
        self.assertEqual(rec["verdict"], "PASS", rec["why"])

    def test_main_groups_user_needs_user_b(self):
        with self.assertRaises(SystemExit):
            pa.main(["groups", "--split", "user"])

    def test_hostile_open_without_two_spare_factions(self):
        ar = FakeArena(self.tmp, {u: standing(u, 0.0) for u in A + B}, [])
        ar.ev = lambda code, timeout=30.0: SPIKE_FACTIONS[:5] if "P.factions(" in code else {}  # type: ignore
        rec = ar.hostile("auto")
        self.assertEqual(rec["verdict"], "OPEN")
        self.assertFalse([c for c in ar.calls if c[0] == "SetFaction"])

    def test_conns_opens_all_then_evaluates_on_each(self):
        opened: list[Any] = []

        class Client:
            def __init__(self, i: int) -> None:
                self.i, self.closed = i, False

            def evaluate(self, code: str, context: str, timeout: float) -> str:
                assert len(opened) == 6, "every connection is open before the first eval"
                return str(self.i)

            def close(self) -> None:
                self.closed = True

        def connect() -> Client:
            if len(opened) == 6:
                raise ConnectionRefusedError("cap")
            opened.append(Client(len(opened)))
            return opened[-1]

        ar = FakeArena(self.tmp, self.facts, [])
        ar.connect = connect
        self.assertEqual(ar.conns(6)["verdict"], "PASS")
        self.assertTrue(all(c.closed for c in opened))
        self.assertEqual(ar.conns(1)["verdict"], "FAIL")  # the 7th is refused

    def test_tie_forces_a_b_member_to_its_a_neighbour_and_records_a_shared_turn(self):
        ar = FakeArena(self.tmp, self.facts, [[A[0]], [B[0], A[1]], [B[1]]])
        evs: list[str] = []
        base_ev = ar.ev

        def ev(code: str, timeout: float = 30.0) -> Any:
            evs.append(code)
            if "P.order" in code:
                return {
                    "round": 1,
                    "groups": [
                        [{"uuid": A[0], "initiative": 18}],
                        [{"uuid": B[0], "initiative": 12}],
                        [{"uuid": A[1], "initiative": 9}],
                    ],
                }
            if "set_initiative" in code:
                return {"writes": [{"list": "CurrentGroups", "member": True}], "replicated": True}
            return base_ev(code, timeout)

        ar.ev = ev  # type: ignore[method-assign]
        r = ar.tie(force=True)
        self.assertEqual(r["force"]["who"], B[0])
        self.assertEqual(r["force"]["initiative"], 18)
        self.assertIn(f'P.set_initiative("{B[0]}", 18)', "".join(evs))
        self.assertEqual([sorted(t) for t in r["shared_turns"]], [sorted([B[0][:4], A[1][:4]])])
        self.assertEqual(ar.written["q5-tie"]["verdict"], "FAIL")

    def test_end_records_death_save_turns_of_the_downed_team(self):
        ar = FakeArena(self.tmp, self.facts, [[A[0]], [B[0]], [A[1]]])
        ar.client = lambda op, args=None: {"uuid": B[0]} if op == "ui.selected" else {"screen": "game"}  # type: ignore
        turns = ar.downed_turns(TEAMS, "B", max_turns=3)
        self.assertEqual([t["who"] for t in turns], [[B[0][:4]]])
        self.assertFalse(turns[0]["ended_by_itself"])  # the fake game never ends a turn by itself
        self.assertEqual(turns[0]["hud"], {"uuid": B[0]})
        self.assertEqual([a["char"] for op, a in ar.jobs], [A[0], B[0], A[1]])

    def test_main_dry_run_needs_no_game(self):
        self.assertEqual(pa.main(["all", "--dry-run"]), 0)


# The probe library run in Lua 5.4 (docker, as the repo's Lua suites run) over stubbed Ext/Osi: who-by-name, the
# argument-form fallback, story DB reads, the event log and its marks.
LUA_STUBS = r"""
local listeners = {}
local names = { ["S_Player_Wyll_aaaaaaaa-1111-2222-3333-444444444444"] = "h1",
    ["S_Player_Laezel_bbbbbbbb-1111-2222-3333-444444444444"] = "h2" }
local text = { h1 = "Wyll", h2 = "Lae'zel" }
local DB = function(rows) return { Get = function(self, a) return rows end } end
Osi = setmetatable({
    DB_PartyMembers = DB({ { "S_Player_Wyll_aaaaaaaa-1111-2222-3333-444444444444" } }),
    DB_Players = DB({}), DB_Avatars = DB({}),
    DB_Origins = DB({ { "S_Player_Laezel_bbbbbbbb-1111-2222-3333-444444444444" } }),
    GetDisplayName = function(u)
        for g, h in pairs(names) do if g:sub(-36) == u:sub(-36) then return h end end
        return nil
    end,
    TwoArgs = function(a, b) if b == nil then error("want 2 args") end return a .. b end,
}, { __index = function(_, k) error("No function named " .. k) end })
Ext = {
    Utils = { MonotonicTime = function() return 1000 end },
    Loca = { GetTranslatedString = function(h) return text[h] end },
    Json = { Stringify = function(x) return "json" end },
    Osiris = { RegisterListener = function(name, arity, when, fn) listeners[name] = fn end },
}
Mods = { BG3AI = { BG3AI = {} } }
"""

LUA_CHECKS = r"""
local P = Mods.BG3AI.BG3AI.ArenaProbe
assert(P.who("wyll") == "aaaaaaaa-1111-2222-3333-444444444444", "who by name prefix")
assert(P.who("laez") == "bbbbbbbb-1111-2222-3333-444444444444", "who ignores punctuation, reads DB_Origins")
assert(not pcall(P.who, "Karlach"), "unknown name is an error")
local r = P.call("TwoArgs", { { "x" }, { "x", "y" } })
assert(r.ok and r.form == 2 and r.ret == "xy", "second form taken")
r = P.call("Missing", { { 1 } })
assert(not r.ok and r.err:find("No function named Missing"), "missing function reported")
assert(P.hooks.TurnStarted == true, "listener registered")
local m = P.n
listeners.TurnStarted("S_Player_Wyll_aaaaaaaa-1111-2222-3333-444444444444")
listeners.CombatRoundStarted("C1", 2)
local s = P.since(m)
assert(#s == 2 and s[1].ev == "TurnStarted" and s[1].names[1] == "Wyll" and s[2].args[2] == "2", "log since mark")
assert(#P.since(P.n) == 0, "nothing after the latest mark")
-- a second install keeps the same library and log (no second listener)
"""

LUA_REINSTALL = r"""
assert(Mods.BG3AI.BG3AI.ArenaProbe.n == 2, "reinstall kept the log")
print("LUA OK")
"""


class SidesTest(unittest.TestCase):
    """q9: the mod's arena begun on the server, the client's BG3AI.Arena.state read before and after reset."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "teams.json").write_text(json.dumps({**TEAMS, "host": A[0]}))

    def run_sides(self, answers: dict[str, list[Any]]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        sent: list[tuple[str, str]] = []
        ar = FakeArena(self.tmp, {}, [])

        def raw(code: str, context: str) -> Any:
            sent.append((context, code))
            return answers[context].pop(0)

        ar.raw = raw  # type: ignore[method-assign]
        rec = ar.sides()
        self.assertIs(ar.written["q9-sides"], rec)
        return rec, sent

    def test_pass_when_the_client_gets_the_teams_and_drops_them(self):
        names = {"A": ["Tav"], "B": ["Karlach"]}
        rec, sent = self.run_sides(
            {
                "server": [{"ok": True}, {"ok": True}],
                "client": [{"state": True, "active": True, "names": names}, {"state": False, "active": False}],
            }
        )
        self.assertEqual(rec["verdict"], "PASS", rec["why"])
        self.assertEqual([c for c, _ in sent], ["server", "client", "server", "client"])
        self.assertIn(f"Mods.BG3AI.BG3AI.Arena.begin({{A = {pa.lua(A)}, B = {pa.lua(B)}}})", sent[0][1])
        self.assertIn("Mods.BG3AI.BG3AI.Arena.reset()", sent[2][1])
        for _, code in sent:  # the debugger's eval env has no global BG3AI: every use goes through Mods
            self.assertNotRegex(code, r"(?<!Mods\.)(?<!Mods\.BG3AI\.)\bBG3AI\.Arena")

    def test_fail_when_the_client_is_side_blind(self):
        rec, _ = self.run_sides(
            {"server": [{"ok": True}, {"ok": True}], "client": [{"state": False}, {"state": False}]}
        )
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertIn("side-blind", rec["why"])

    def test_open_without_the_switch(self):
        err = "arena ops need BG3AI_ARENA=1 in the game's environment"
        rec, _ = self.run_sides({"server": [{"ok": False, "error": err}, {"ok": True}], "client": [{}, {}]})
        self.assertEqual(rec["verdict"], "OPEN")
        self.assertIn("BG3AI_ARENA=1", rec["why"])

    def test_open_states_the_refusal_only(self):
        err = "attempt to index a nil value (global 'BG3AI')"
        rec, _ = self.run_sides({"server": [{"ok": False, "error": err}, {"ok": True}], "client": [{}, {}]})
        self.assertEqual(rec["why"], f"BG3AI.Arena.begin refused: {err}")

    def test_judge_fails_teams_kept_after_reset_or_switch_unseen(self):
        ok = {"ok": True}
        after = {"state": True, "active": True}
        kept = {"begin": ok, "client_after_begin": after, "client_after_reset": {"state": True}}
        self.assertEqual(pj.judge_sides(kept)[0], "FAIL")
        unseen = {"begin": ok, "client_after_begin": {"state": True, "active": False}, "client_after_reset": {}}
        self.assertEqual(pj.judge_sides(unseen)[0], "FAIL")


class LuaLibraryTest(unittest.TestCase):
    def test_install_in_lua(self):
        import shutil
        import subprocess

        if not shutil.which("docker"):
            self.skipTest("no docker")
        src = LUA_STUBS + pa.INSTALL + LUA_CHECKS + pa.INSTALL + LUA_REINSTALL
        r = subprocess.run(
            ["docker", "run", "--rm", "-i", "nickblah/lua:5.4", "lua", "-"],
            input=src,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertIn("LUA OK", r.stdout, r.stderr)

    def test_sides_snippets_run_without_a_global_bg3ai(self):
        import shutil
        import subprocess

        if not shutil.which("docker"):
            self.skipTest("no docker")
        stubs = (
            "Ext = { Json = { Stringify = function(t) return tostring(t.ok) .. ',' .. tostring(t.state) end } }\n"
            "Mods = { BG3AI = { BG3AI = { Arena = { state = { names = {} }, active = function() return true end,\n"
            "    begin = function(t) return t end } } } }\n"
            "assert(BG3AI == nil)\n"
        )
        server = pa.SERVER_TRY.format(call=f"{pa.MOD}.Arena.begin({{A = {{}}, B = {{}}}})")
        src = stubs + f"print((function() {server} end)())\nprint((function() {pa.CLIENT_ARENA} end)())\n"
        r = subprocess.run(
            ["docker", "run", "--rm", "-i", "nickblah/lua:5.4", "lua", "-"],
            input=src,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.stdout.split(), ["true,nil", "nil,true"], r.stderr)

    def test_split_groups_in_lua(self):
        import shutil
        import subprocess

        if not shutil.which("docker"):
            self.skipTest("no docker")
        stubs = r"""
local ents = {}
local function ent(u, team) ents[u] = { Uuid = { EntityUuid = u }, TurnBased = { CombatTeam = team },
    Replicate = function(self, c) self.replicated = c end } return ents[u] end
local m = function(u, ini, team) return { Entity = ent(u, team), Initiative = ini } end
local next_ = { { Team = "G1", Round = 2, Initiative = 5, IsPlayer = true,
    Members = { m("b0", 5, "G1"), m("a0", 5, "tA0"), m("a1", 4, "G1"), m("b1", 2, "tB1"), m("b2", 2, "G1") } },
    { Team = "N1", Round = 2, Initiative = -20, IsPlayer = false, Members = { m("npc", -20, "N1") } } }
local written
local combat = { TurnOrder = { Round = 2, NextRoundGroups = next_, CurrentGroups = {} },
    Replicate = function(self, c) self.replicated = c end }
Ext = { Entity = { Get = function(u) if u == "a0" then return { CombatParticipant = { CombatHandle = combat } } end
        return ents[u] end },
    Types = { Unserialize = function(arr, t) written = t; for k in pairs(arr) do arr[k] = nil end
        for i, v in ipairs(t) do arr[i] = v end end } }
Mods = { BG3AI = { BG3AI = { ArenaProbe = { uuid = function(g) return g end, name = function() return nil end,
    q = function() return nil end } } } }
"""
        checks = r"""
local P = Mods.BG3AI.BG3AI.ArenaProbe
local r = P.split_groups("a0", { a0 = "A", a1 = "A", b0 = "B", b1 = "B", b2 = "B" }, "next")
assert(r.error == nil, tostring(r.error))
assert(r.groups_before == 2 and r.groups_written == 4 and r.groups_after == 4, "B | A A | B B | npc")
local w = written
assert(w[1].Team == "G1" and #w[1].Members == 1, "first run keeps G1")
assert(w[1].Members[1].Entity.Uuid.EntityUuid == "b0", "first run is b0")
assert(w[2].Team == "tA0" and #w[2].Members == 2 and w[2].Initiative == 5, "a0's own team")
assert(w[3].Team == "tB1" and #w[3].Members == 2 and w[3].Initiative == 2, "b1's own team")
assert(w[4].Team == "N1" and #w[4].Members == 1, "the npc group untouched")
assert(#r.team_writes == 2 and r.team_writes[1].uuid == "a1" and r.team_writes[2].uuid == "b2", "a1, b2 rejoin")
assert(ents.a1.TurnBased.CombatTeam == "tA0" and ents.b2.TurnBased.CombatTeam == "tB1", "CombatTeam written")
assert(ents.a1.replicated == "TurnBased" and r.replicated, "replicated")
local g = P.turn_groups("a0")
assert(g.error == nil and g.round == 2 and #g.next == 4, tostring(g.error))
print("GROUPS OK")
"""
        r = subprocess.run(
            ["docker", "run", "--rm", "-i", "nickblah/lua:5.4", "lua", "-"],
            input=stubs + pa.GROUPS + checks,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertIn("GROUPS OK", r.stdout, r.stderr)

    def test_popup_parses(self):
        import shutil
        import subprocess

        if not shutil.which("docker"):
            self.skipTest("no docker")
        r = subprocess.run(
            ["docker", "run", "--rm", "-i", "nickblah/lua:5.4", "sh", "-c", "cat > /tmp/p.lua && luac -p /tmp/p.lua"],
            input=pa.POPUP,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
