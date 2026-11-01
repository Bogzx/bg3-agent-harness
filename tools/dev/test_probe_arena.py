"""Offline tests for tools/dev/probe_arena.py (no game needed): the Lua it builds, the verdicts, and the order of
calls its steps make against a scripted game.

Run: uv run pytest -q tools/dev/test_probe_arena.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_arena as pa

A = [f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in range(4)]
B = [f"bbbbbbbb-0000-0000-0000-00000000000{i}" for i in range(4)]
TEAMS = {"A": A, "B": B}


class LuaBuildTest(unittest.TestCase):
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


class FactionPickTest(unittest.TestCase):
    def test_prefers_an_unused_hero_faction_other_than_team_a(self):
        fs = [
            {"name": "Hero Player1", "osi": "Hero Player1_1", "used": 4, "to_a": 100},
            {"name": "Goblin", "osi": "Goblin_2", "used": 9, "to_a": 0},
            {"name": "Hero Player2", "osi": "Hero Player2_3", "used": 0, "to_a": 100},
            {"name": "Hero Companion", "osi": "Hero Companion_4", "used": 2, "to_a": 100},
        ]
        self.assertEqual(pa.pick_faction_b(fs, "Hero Player1_1"), "Hero Player2_3")

    def test_none_without_another_hero_faction(self):
        fs = [{"name": "Hero Player1", "osi": "F_A", "used": 4}, {"name": "Goblin", "osi": "G", "used": 1}]
        self.assertIsNone(pa.pick_faction_b(fs, "F_A"))


def facts_ok(u: str, slot: list[float], **kw: Any) -> dict[str, Any]:
    return {"uuid": u, "name": u[:4], "in_party_db": True, "pos": slot, "hp": 20, "max_hp": 20, **kw}


class JudgeTest(unittest.TestCase):
    def test_setup_pass_and_each_failure(self):
        slots = {s: pa.line_slots([0.0, 0.0, 20.0 * i], 4) for i, s in enumerate(pa.SIDES)}
        facts = {u: facts_ok(u, sl) for s in pa.SIDES for u, sl in zip(TEAMS[s], slots[s], strict=True)}
        self.assertEqual(pa.judge_setup(TEAMS, facts, slots)[0], "PASS")
        facts[B[2]] = facts_ok(B[2], [50.0, 0.0, 0.0], in_party_db=False, hp=3)
        v, why = pa.judge_setup(TEAMS, facts, slots)
        self.assertEqual(v, "FAIL")
        self.assertIn("not in DB_PartyMembers", why)
        self.assertIn("from its slot", why)
        self.assertIn("hp 3/20", why)

    def test_hostile(self):
        facts = {u: {"combat": "C1"} for u in A + B}
        enemy = {f"AB:{x}>{y}": 1 for x in A for y in B} | {"AA:x>y": 0, "BB:x>y": 0}
        self.assertEqual(pa.judge_hostile(TEAMS, facts, enemy)[0], "PASS")
        v, why = pa.judge_hostile(TEAMS, {**facts, B[3]: {"combat": None}}, {**enemy, "BB:x>y": 1})
        self.assertEqual(v, "FAIL")
        self.assertIn("enemies within a team: BB:x>y", why)
        self.assertIn("combat guids", why)
        v, why = pa.judge_hostile(TEAMS, facts, {**enemy, "AB:x>y": 0})
        self.assertIn("IsEnemy across the teams: 16/17", why)

    def test_turns(self):
        turns = [
            {"uuid": A[0], "group": [A[0]], "round": 1, "initiative": 18},
            {"uuid": B[0], "group": [B[0]], "round": 1, "initiative": 12},
            {"uuid": A[1], "group": [A[1], A[2]], "round": 1, "initiative": 9},
        ]
        self.assertEqual(pa.judge_turns(TEAMS, turns, None)[0], "PASS")
        mixed = [*turns, {"uuid": B[1], "name": "Wyll", "group": [B[1], A[3]], "round": 1, "initiative": 5}]
        self.assertIn("held both sides: Wyll", pa.judge_turns(TEAMS, mixed, None)[1])
        disorder = [*turns, {"uuid": B[2], "group": [B[2]], "round": 1, "initiative": 15}]
        self.assertIn("rounds [1] not in initiative order", pa.judge_turns(TEAMS, disorder, None)[1])
        self.assertIn("sides ['A'] only", pa.judge_turns(TEAMS, turns[:1], None)[1])
        down = {"who": "Gale", "downed": True, "combat_after": False, "turns_after": 2}
        self.assertIn("after Gale was downed: combat False", pa.judge_turns(TEAMS, turns, down)[1])
        down = {"who": "Gale", "downed": True, "combat_after": True, "turns_after": 2}
        self.assertEqual(pa.judge_turns(TEAMS, turns, down)[0], "PASS")

    def test_ai(self):
        still = {"moved_m": 0.0, "casts": [], "ended_by_itself": False}
        self.assertEqual(pa.judge_ai([])[0], "OPEN")
        self.assertEqual(pa.judge_ai([{"name": "Tav", "idle": still}])[0], "PASS")
        moved = {"name": "Wyll", "idle": {**still, "moved_m": 3.2}}
        self.assertIn("Wyll", pa.judge_ai([moved])[1])
        self.assertEqual(pa.judge_ai([{"name": "Lae", "idle": {**still, "casts": ["Target_X"]}}])[0], "FAIL")
        # a downed member's turn passes by itself (death saving throw): not the AI
        self.assertEqual(
            pa.judge_ai([{"name": "Gale", "downed_at_start": True, "idle": {**still, "ended_by_itself": True}}])[0],
            "PASS",
        )

    def test_res_amount_reads_both_shapes(self):
        self.assertEqual(pa.res_amount({"ActionPoint": {"0": 1}}, "ActionPoint"), 1.0)
        self.assertEqual(pa.res_amount({"SpellSlot": [2, 1]}, "SpellSlot"), 3.0)
        self.assertIsNone(pa.res_amount({}, "Movement"))

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

        self.assertEqual(pa.judge_act({"A": run(4.5, 0), "B": run(3, 0)})[0], "PASS")
        v, why = pa.judge_act({"A": run(9, 0), "B": run(3, 1, end="failed")})
        self.assertEqual(v, "FAIL")
        self.assertIn("side A move: Movement 9.0 -> 9.0 (not charged)", why)
        self.assertIn("side B cast: ActionPoint 1.0 -> 1.0", why)
        self.assertIn("side B end_turn: failed", why)
        self.assertIn("side B: no turn reached", pa.judge_act({"A": run(4, 0)})[1])

    def test_react(self):
        self.assertEqual(pa.judge_react({"error": "x"})[0], "OPEN")
        ok = {"popup_seen": {"open": True}, "answer": {"ok": True}, "reactor_name": "Wyll", "reactor_side": "B"}
        self.assertEqual(pa.judge_react(ok), ("PASS", "prompt for Wyll (side B), answered by ui.react"))
        self.assertEqual(pa.judge_react({**ok, "answer": {"ok": False}})[0], "FAIL")
        self.assertEqual(pa.judge_react({"events": [{"ev": "ReactionInterruptUsed"}]})[0], "OPEN")
        self.assertEqual(pa.judge_react({"events": []})[0], "FAIL")

    def test_end_and_gameover(self):
        self.assertEqual(pa.judge_end({"ended_at": "downed", "end_event": "CombatEnded"})[0], "PASS")
        self.assertEqual(pa.judge_end({"ended_at": "dead", "game_over": True})[0], "FAIL")
        self.assertEqual(pa.judge_end({})[0], "FAIL")
        self.assertEqual(pa.judge_gameover({"dead": True, "game_over": False})[0], "PASS")
        self.assertEqual(pa.judge_gameover({"dead": True, "game_over": True})[0], "FAIL")
        self.assertEqual(pa.judge_gameover({"dead": False})[0], "OPEN")

    def test_npc(self):
        self.assertEqual(pa.judge_npc({"act": {"refused": True}, "idle": {"acted": False}})[0], "OPEN")
        v, why = pa.judge_npc({"act": {"refused": False}, "idle": {"acted": True}})
        self.assertEqual(v, "FAIL")
        self.assertIn("the game AI acted", why)


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
