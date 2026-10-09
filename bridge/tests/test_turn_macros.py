"""Offline tests: combat_turn (a whole turn in one call, with fallbacks and guard checks) and the macros
(loot_area, dialogue_advance, travel_to, regroup, read_all, rest_if_needed), plus the clock's stuck signal.
A thread-safe fake game simulates mod jobs in real time.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-"))

from bg3ai import actq, macros, server, turn  # noqa: E402
from bg3ai.game import GameError, JobResult  # noqa: E402

P1 = "11111111-2222-3333-4444-555555555555"
P2 = "22222222-2222-3333-4444-555555555555"
GOB = "99999999-2222-3333-4444-555555555555"
IMP = "88888888-2222-3333-4444-555555555555"
CHEST = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
LOCKED = "abababab-bbbb-cccc-dddd-eeeeeeeeeeee"
OWNED = "acacacac-bbbb-cccc-dddd-eeeeeeeeeeee"
BOOK = "b0000000-bbbb-cccc-dddd-eeeeeeeeeeee"
LETTER = "b1111111-bbbb-cccc-dddd-eeeeeeeeeeee"
POTION = "b2222222-bbbb-cccc-dddd-eeeeeeeeeeee"

UI_JOBS = {"ui.loot", "ui.dialogue_choose", "ui.react", "ui.short_rest", "ui.long_rest"}
NOT_JOBS = {"act.watch", "act.note", "act.target_state"}


class FakeGame:
    """Jobs finish `dur` seconds after they start; job.cancel interrupts them; refuse_if(op, args) -> error."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[int, dict] = {}
        self.next_id = 1
        self.calls: list[tuple[str, Any]] = []
        self.events: list[dict] = []
        self.seq = 0
        self.event_seq = {"server": 0, "client": 0}
        self.watch: dict = {"party": [{"uuid": P1, "hp": 20, "max_hp": 20, "pos": [0, 0, 0]},
                                      {"uuid": P2, "hp": 20, "max_hp": 20, "pos": [1, 0, 0]}],
                            "seen": [], "combat": False, "dialogue": False}
        self.ui: dict = {"screen": "game"}
        self.ui_seq: list[dict] = []  # successive ui.state replies (last one repeats), when set
        self.dur: dict[str, float] = {}
        self.refuse_if = lambda op, args: None
        self.result_for = lambda op, args: {"dist": 1.0, "after": {"hp": 20, "max_hp": 20, "action": 0,
                                                                    "bonus": 1, "movement": 3}}
        self.visible: set[str] = {GOB, IMP, CHEST}
        self.dead: set[str] = set()
        self.combat: dict = {"active": False}
        self.reach: Any = None  # nav.reach_toward reply (or callable(args))
        self.path: dict = {"reachable": True, "movement_left": 0}
        self.nearby: dict = {"characters": [], "items": []}
        self.inv: dict = {"items": []}
        self.on_start = None  # hook(op, args)

    def push(self, kind: str, data: Any = None) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": kind, "data": data})

    def ops(self, prefix: str) -> list[tuple[str, Any]]:
        with self.lock:
            return [c for c in self.calls if c[0].startswith(prefix)]

    def _view(self, jid: int) -> dict:
        j = self.jobs[jid]
        if j["status"] == "running" and time.monotonic() >= j["end"]:
            j["status"] = "done"
            j["result"] = self.result_for(j["op"], j["args"])
        return {"id": jid, "status": j["status"], "result": j.get("result"), "error": j.get("error"),
                "elapsed_ms": 10}

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
        args = args or {}
        with self.lock:
            self.calls.append((op, args))
            if (op.startswith("act.") and op not in NOT_JOBS) or op in UI_JOBS:
                if self.on_start:
                    self.on_start(op, args)
                jid = self.next_id
                self.next_id += 1
                err = self.refuse_if(op, args)
                if err:
                    self.jobs[jid] = {"status": "failed", "error": err, "end": 0, "op": op, "args": args}
                    return {"job": jid, "status": "failed", "error": err}
                self.jobs[jid] = {"status": "running", "end": time.monotonic() + self.dur.get(op, 0.02),
                                  "op": op, "args": args}
                return {"job": jid, "eta_s": 1.0, "route_m": 4.0}
            if op == "job.poll":
                return self._view(int(args["id"]))
            if op == "job.cancel":
                j = self.jobs[int(args["id"])]
                if j["status"] == "running":
                    j["status"], j["error"] = "interrupted", "cancelled"
                return {"status": j["status"]}
            if op == "act.watch":
                return json.loads(json.dumps(self.watch))
            if op == "act.note":
                self.push(args["kind"], args.get("data"))
                return {"ok": True}
            if op == "act.target_state":
                u = args["uuid"]
                return {"visible": u in self.visible or u in self.dead, "dead": u in self.dead or None}
            if op == "events.since":
                evs = [e for e in self.events if e["seq"] > int(args.get("seq", 0))][: int(args.get("limit", 200))]
                return {"latest": self.seq, "events": evs if context == "server" else []}
            if op == "ui.state":
                if self.ui_seq:
                    return self.ui_seq.pop(0) if len(self.ui_seq) > 1 else self.ui_seq[0]
                return dict(self.ui)
            if op in ("ui.dismiss", "ui.reactions"):
                return {"ok": True}
            if op == "state.status":
                return {"active": P1, "level": "TUT", "party": [{"uuid": P1, "name": "Claude"},
                                                                 {"uuid": P2, "name": "Lae'zel"}]}
            if op == "state.combat":
                return json.loads(json.dumps(self.combat))
            if op == "state.actions":
                return [{"spell": "Target_MainHandAttack", "name": "Attack"}]
            if op == "nav.reach_toward":
                r = self.reach(args) if callable(self.reach) else self.reach
                return r if r is not None else {"reachable": False}
            if op == "nav.path":
                return dict(self.path)
            if op == "nav.markers":
                return {"markers": []}
            if op == "state.nearby":
                return json.loads(json.dumps(self.nearby))
            if op == "state.inventory":
                return json.loads(json.dumps(self.inv))
        raise GameError(f"unexpected op {op}")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        try:
            return self.rpc(op, args, context)
        except GameError:
            return default

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0,
                poll_s: float = 0.01, on_start=None) -> JobResult:
        started = self.rpc(op, args, context)
        if started.get("status") not in (None, "running"):
            return JobResult(started["status"], None, started.get("error"))
        if on_start:
            on_start(context, started["job"])
        end = time.monotonic() + wait_s
        while True:
            v = self.rpc("job.poll", {"id": started["job"]}, context)
            if v["status"] != "running":
                return JobResult(v["status"], v.get("result"), v.get("error"))
            if time.monotonic() > end:
                return JobResult("still_running", {"job": started["job"]})
            time.sleep(poll_s)

    def drain_events(self, ctx: str) -> list:
        with self.lock:
            if ctx != "server":
                return []
            evs = [e for e in self.events if e["seq"] > self.event_seq["server"]]
            self.event_seq["server"] = self.seq
            return evs


def combat(current: str = P1, gob_dist: float = 6.0) -> dict:
    return {"active": True, "round": 2, "current": current, "order": [
        {"uuid": P1, "name": "Claude", "relation": "party"},
        {"uuid": GOB, "name": "Goblin", "relation": "hostile", "from": {P1: [gob_dist, 1], P2: [9.0, 1]}},
        {"uuid": IMP, "name": "Imp", "relation": "hostile", "from": {P1: [12.0, 1]}},
        {"uuid": P2, "name": "Lae'zel", "relation": "party"}]}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.g = FakeGame()
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-mem-"))
        saved = (server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
                 server.MEMORY, server.delta.changes_text)
        server.game = self.g
        server._watch_game = self.g
        server._queue_mgr = None
        server._async.clear()
        server._looted.clear()
        server.MEMORY = self.tmp
        server.delta.changes_text = lambda game, H: ""
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        self.gob = server.H.get(GOB, "Goblin")
        self.imp = server.H.get(IMP, "Imp")
        self.chest = server.H.get(CHEST, "Chest")
        server.H.get(P1, "Claude")
        server.H.get(P2, "Lae'zel")

    def _restore(self, saved) -> None:
        if server._queue_mgr is not None:
            server._queue_mgr.cancel()
            for q in server._queue_mgr.queues.values():
                if q.thread:
                    q.thread.join(2)
        (server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
         server.MEMORY, server.delta.changes_text) = saved

    def casts(self) -> list[str]:
        return [a.get("spell") for _, a in self.g.ops("act.cast")]


# ---------------------------------------------------------------------------
# combat_turn
# ---------------------------------------------------------------------------

class CombatTurn(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat()
        self.g.watch["combat"] = True

        def advance(op, args):  # ending the turn hands it to the goblin
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = advance

    def plan(self, **kw) -> str:
        return json.dumps(kw)

    def test_whole_turn_in_one_call(self):
        out = server.combat_turn(self.plan(
            move=self.gob, action={"ability": "Target_MainHandAttack", "target": self.gob},
            bonus={"ability": "Shout_SecondWind"}))
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS],
                         ["act.move_to", "act.cast", "act.cast", "act.end_turn"])
        self.assertEqual(self.casts(), ["Target_MainHandAttack", "Shout_SecondWind"])
        self.assertIn("TURN ", out)
        self.assertIn("round 2", out)
        self.assertIn(f"action: Target_MainHandAttack {self.gob}: done", out)
        self.assertIn("end_turn: done", out)
        self.assertNotIn("after=", out)
        self.assertIn("NOW ", out)
        self.assertIn(f"NEXT {self.gob} Goblin, {self.imp} Imp, then ", out)
        self.assertIn("Lae'zel (yours). Plan it while they act", out)

    def test_out_of_range_falls_back_to_ranged(self):
        self.g.refuse_if = lambda op, a: ("target is out of range (6.0 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_MainHandAttack" else None)
        out = server.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_ThrowWeapon", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Target_MainHandAttack", "Projectile_ThrowWeapon"])
        self.assertIn("FAILED — target is out of range", out)
        self.assertIn(f"→ Projectile_ThrowWeapon {self.gob}: done", out)
        self.assertNotIn("STOPPED", out)
        self.assertIn("end_turn: done", out)

    def test_move_that_does_not_fit_walks_as_far_as_it_can(self):
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 14.2 m, 5.5 m left)"
                                          if op == "act.move_to" and a.get("target") else None)
        self.g.reach = {"reachable": True, "pos": [3.0, 0.0, 4.0]}
        out = server.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"]), end=False)
        moves = self.g.ops("act.move_to")
        self.assertEqual(moves[1][1]["pos"], [3.0, 0.0, 4.0])
        self.assertAlmostEqual(self.g.ops("nav.reach_toward")[0][1]["max_m"], 5.2)
        self.assertIn("→ partial 5.2m toward it: done", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_within_range_skips_the_move(self):
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 18},
                                           action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("already within 18m", out)

    def test_within_walks_only_to_range(self):
        self.g.reach = {"reachable": True, "pos": [2.0, 0.0, 0.0]}
        server.combat_turn(self.plan(move={"target": self.gob, "within": 4},
                                     action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertAlmostEqual(self.g.ops("nav.reach_toward")[0][1]["max_m"], 2.3)  # 6 - 4 + 0.3
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [2.0, 0.0, 0.0])

    def test_within_never_walks_into_melee_when_no_ground_leads_there(self):
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 4},
                                           action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("no walkable ground toward it", out)
        self.assertIn("action: Projectile_RangedAttack", out)

    def test_dead_target_skips_to_the_next_alternative(self):
        self.g.dead.add(GOB)
        out = server.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Target_MainHandAttack", "target": self.imp}]))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])
        self.assertIn("is already dead", out)

    def test_not_my_turn_is_refused(self):
        self.g.combat["current"] = GOB
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
        self.assertIn("FAILED: it is not", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_not_in_combat(self):
        self.g.combat = {"active": False}
        self.assertIn("FAILED: not in combat", server.combat_turn(self.plan(action="Shout_Dodge")))

    def test_hp_drop_stops_the_turn_without_ending_it(self):
        def hurt(op, args):
            if op == "act.move_to":
                self.g.watch["party"][0]["hp"] = 4
        self.g.on_start = hurt
        out = server.combat_turn(self.plan(move=self.gob, action={"ability": "Target_MainHandAttack",
                                                                  "target": self.gob}))
        self.assertIn("STOPPED: ", out)
        self.assertIn("HP 4/20", out)
        self.assertIn("turn NOT ended", out)
        self.assertEqual(self.g.ops("act.cast"), [])
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_turn_taken_away_mid_plan(self):
        def lose(op, args):
            if op == "act.cast":
                self.g.combat["current"] = GOB
        self.g.on_start = lose
        out = server.combat_turn(self.plan(action="Shout_Dash", bonus="Shout_SecondWind"))
        self.assertEqual(self.casts(), ["Shout_Dash"])
        self.assertIn("no longer", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_dash_then_move_again(self):
        n = {"moves": 0}

        def refuse(op, a):
            if op == "act.move_to":
                n["moves"] += 1
                return "no movement left" if n["moves"] == 1 else None
            return None
        self.g.refuse_if = refuse
        out = server.combat_turn(self.plan(move=self.gob, action=[{"ability": "Shout_Dash", "then_move": True}]))
        self.assertEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn(f"move {self.gob}: FAILED — no movement left", out)
        self.assertIn("action: Shout_Dash: done", out)
        self.assertTrue(out.index("action:") < out.rindex(f"move {self.gob}: done"))

    def test_reaction_prompt_answered_by_policy(self):
        self.g.dur["act.cast"] = 0.4

        def prompt(op, args):
            if op == "act.cast":
                threading.Timer(0.05, lambda: self.g.ui.update(screen="reaction")).start()
            if op == "ui.react":
                self.g.ui["screen"] = "game"
        self.g.on_start = prompt
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           on_reaction="use"), end=False)
        self.assertEqual(self.g.ops("ui.react")[0][1], {"use": True})
        self.assertIn("reaction prompt: used", out)
        self.assertIn(f"action: Target_MainHandAttack {self.gob}: done", out)
        self.assertNotIn("STOPPED", out)

    def test_reaction_prompt_stops_by_default(self):
        self.g.dur["act.cast"] = 0.4
        self.g.on_start = lambda op, a: op == "act.cast" and threading.Timer(
            0.05, lambda: self.g.ui.update(screen="reaction")).start()
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("STOPPED: reaction on screen", out)
        self.assertEqual(self.g.ops("ui.react"), [])

    def test_reactions_prep(self):
        server.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(self.g.ops("ui.reactions")[0][1], {"char": P1, "set": {"Divine Smite": "always"}})

    def test_next_party_member_already_up_gets_actions(self):
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("'s turn: plan it", out)
        self.assertIn("YOUR ACTIONS", out)

    def test_extra_attack_slot_and_order(self):
        server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                     bonus="Shout_SecondWind",
                                     extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]],
                                     order="bonus,action,extra"))
        self.assertEqual(self.casts(), ["Shout_SecondWind", "Target_MainHandAttack", "Target_MainHandAttack"])

    def test_plan_errors(self):
        for bad, msg in (({"actoin": "x"}, "unknown plan key"), ({}, "does nothing"),
                         ({"action": {"tool": "end_turn"}}, "can't be a turn step"),
                         ({"action": {"tool": "save_game"}}, "can't be batched"),
                         ({"action": "Shout_Dodge", "on_reaction": "maybe"}, "on_reaction"),
                         ({"action": "Shout_Dodge", "order": "action,fly"}, "order")):
            self.assertIn(msg, server.combat_turn(json.dumps(bad)), bad)


class NextUp(unittest.TestCase):
    def test_wraps_round_and_skips_the_ended_character(self):
        cs = combat(current=P1)
        text, mine = turn.next_up(cs, {P1, P2}, lambda u: u[:2], ended=P1)
        self.assertEqual(mine, P2)
        self.assertEqual(text, "NEXT 99, 88, then 22 (yours)")
        cs["order"] = cs["order"][:3]  # only Claude and enemies: back to Claude after them
        text, mine = turn.next_up(cs, {P1}, lambda u: u[:2], ended=P1)
        self.assertEqual((text, mine), ("NEXT 99, 88, then 11 (yours)", P1))

    def test_movement_left(self):
        self.assertEqual(turn.movement_left("destination is beyond remaining movement (path 14.2 m, 5.5 m left)"), 5.5)
        self.assertTrue(turn.is_fatal("FAILED — it is not this character's turn"))


# ---------------------------------------------------------------------------
# macros
# ---------------------------------------------------------------------------

class LootArea(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.dead.add(GOB)
        self.g.nearby = {
            "characters": [{"uuid": GOB, "name": "Goblin", "relation": "hostile", "dead": True, "pos": [8, 0, 0],
                            "dist": 8.0}],
            "items": [{"uuid": CHEST, "name": "Chest", "kind": "container", "pos": [2, 0, 0], "dist": 2.0},
                      {"uuid": LOCKED, "name": "Iron Chest", "kind": "container", "locked": True, "pos": [3, 0, 0],
                       "dist": 3.0},
                      {"uuid": OWNED, "name": "Merchant's Crate", "kind": "container", "owned": True,
                       "pos": [4, 0, 0], "dist": 4.0}]}
        self.corpses: list[str] = []

        def fake_loot_corpse(corpse: str, who: str | None = None) -> str:
            self.corpses.append(corpse)
            server._batch["last"] = "done"
            return "loot_corpse: done"
        orig = server.loot_corpse
        server.loot_corpse = fake_loot_corpse
        self.addCleanup(setattr, server, "loot_corpse", orig)

    def test_loots_visible_in_walking_order_and_skips_the_rest(self):
        out = server.loot_area()
        self.assertEqual(self.g.ops("act.interact")[0][1]["target"], CHEST)
        self.assertEqual(self.corpses, [self.gob])
        self.assertEqual(len(self.g.ops("ui.loot")), 2)
        self.assertLess(out.index(self.chest), out.index(f"{self.gob} Goblin:"))  # nearest first
        self.assertIn("Iron Chest (locked)", out)
        self.assertIn("Merchant's Crate (owned: taking is stealing)", out)
        self.assertIn("nothing to loot", server.loot_area())  # remembered

    def test_stops_at_the_first_interrupt(self):
        self.g.on_start = lambda op, a: op == "ui.loot" and self.g.push("combat_started", {})
        out = server.loot_area()
        self.assertIn("STOPPED: combat started; not reached: ", out)
        self.assertEqual(self.corpses, [])

    def test_refused_in_combat(self):
        self.g.watch["combat"] = True
        self.assertIn("FAILED: loot_area is for outside combat", server.loot_area())


class DialogueAdvance(Base):
    def test_advances_filler_and_stops_at_a_choice(self):
        choice = {"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}},
                         {"screen": "dialogue", "dialogue": {"text": "Listen.", "options": [{"i": 1, "text": "..."}]}},
                         choice]
        out = server.dialogue_advance()
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [0, 1])
        self.assertIn("2 filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertIn("2. No", out)

    def test_never_picks_a_tagged_or_committing_option(self):
        for opt in ({"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]}, {"i": 1, "text": "Attack them."}):
            self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"options": [opt]}}]
            out = server.dialogue_advance()
            self.assertIn("0 filler line(s)", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])

    def test_conversation_over(self):
        self.assertIn("STOPPED: conversation ended", server.dialogue_advance())

    def test_filler_rules(self):
        self.assertEqual(macros.dialogue_filler({"continue": True}), (True, 0, ""))
        self.assertFalse(macros.dialogue_filler({"listening": True})[0])
        self.assertFalse(macros.dialogue_filler({"options": [{"i": 1, "text": "Give him the gold."}]})[0])
        self.assertEqual(macros.dialogue_filler({"options": [{"i": 1, "text": "Hm."}]})[:2], (True, 1))
        self.assertEqual(macros.dialogue_filler({"options": [{"i": 1, "text": "x"}, {"i": 2, "text": "y", "off": True}]})[:2],
                         (True, 1))


class TravelTo(Base):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0],
                                                             "level": "TUT"}]))
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_chained_walks_to_a_marker(self):
        out = server.travel_to(marker="well")
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 50.0])
        self.assertIn("travel_to marker m1 Old well: 50m", out)
        self.assertIn("arrived", out)

    def test_no_way_further(self):
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0]}
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_interrupt_stops_the_trip(self):
        self.g.on_start = lambda op, a: op == "act.move_to" and self.g.push("dialogue_started", {})
        out = server.travel_to(marker="m1")
        self.assertIn("STOPPED: a conversation started", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_marker_on_another_level(self):
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "x", "pos": [1, 0, 0],
                                                             "level": "WLD", "region": "Wilderness"}]))
        self.assertIn("fast_travel there first", server.travel_to(marker="m1"))


class Regroup(Base):
    def test_far_members_walk_back(self):
        self.g.watch["party"][1]["pos"] = [20, 0, 0]
        out = server.regroup()
        mv = self.g.ops("act.move_to")
        self.assertEqual((mv[0][1]["char"], mv[0][1]["target"]), (P2, P1))
        self.assertIn("1 walking", out)
        self.assertIn("(20m): move_to: done", out)

    def test_nobody_far(self):
        self.assertIn("everyone is within", server.regroup())


class ReadAll(Base):
    def test_reads_new_books_once(self):
        self.g.inv = {"items": [{"uuid": BOOK, "name": "Dusty Book", "owner": P1},
                                {"uuid": LETTER, "name": "Letter to Edowin", "owner": P2},
                                {"uuid": POTION, "name": "Potion of Healing", "owner": P1},
                                {"uuid": "c" * 8 + CHEST[8:], "name": "Scroll of Revivify", "owner": P1}]}
        self.g.ui = {"screen": "book", "popup": {"title": "Dusty Book", "text": "The tadpoles must be removed."}}
        self.g.on_start = lambda op, a: op == "act.interact" and self.g.ui.update(screen="book")
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.dismiss":
                self.g.ui["screen"] = "game"
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.read_all()
        self.assertEqual([a["target"] for _, a in self.g.ops("act.interact")], [BOOK, LETTER])
        self.assertEqual(self.g.ops("act.interact")[1][1]["char"], P2)  # the owner reads it
        self.assertIn("The tadpoles must be removed.", out)
        self.assertEqual(len(self.g.ops("ui.dismiss")), 2)
        self.assertEqual(set(json.loads((self.tmp / "read_items.json").read_text())), {BOOK, LETTER})
        self.assertIn("nothing new to read", server.read_all())

    def test_readable_names(self):
        self.assertTrue(macros.readable({"name": "Necromancy of Thay"}) is False)  # needs a book word
        self.assertTrue(macros.readable({"name": "Gnoll's Diary"}))
        self.assertFalse(macros.readable({"name": "Scroll of Fireball"}))


class RestIfNeeded(Base):
    def test_no_rest_needed(self):
        self.assertIn("no rest needed (lowest HP 100%)", server.rest_if_needed())
        self.assertEqual(self.g.ops("ui.short_rest"), [])

    def test_short_rest_when_low(self):
        self.g.watch["party"][1]["hp"] = 6
        out = server.rest_if_needed()
        self.assertEqual(len(self.g.ops("ui.short_rest")), 1)
        self.assertIn("low: ", out)
        self.assertIn("6/20", out)

    def test_long_rest_only_when_allowed(self):
        self.g.watch["party"][0]["hp"] = 2
        self.g.refuse_if = lambda op, a: "no short rests left" if op == "ui.short_rest" else None
        out = server.rest_if_needed()
        self.assertIn("long rest at camp", out)
        self.assertEqual(self.g.ops("ui.long_rest"), [])
        server.rest_if_needed(allow_long=True)
        self.assertEqual(len(self.g.ops("act.go_to_camp")), 1)
        self.assertEqual(len(self.g.ops("ui.long_rest")), 1)


class ClockStuck(unittest.TestCase):
    def setUp(self) -> None:
        saved = (server.DEADLINE, server.PROGRESS_PATH, dict(server._clock_shown), dict(server._stuck_cache))
        self.addCleanup(self._restore, saved)
        self.path = Path(tempfile.mkdtemp()) / "S005.progress.jsonl"
        server.PROGRESS_PATH = str(self.path)
        server.DEADLINE = time.time() + 40 * 60
        server._clock_shown.update(at=time.time(), stuck=None)

    def _restore(self, saved) -> None:
        server.DEADLINE, server.PROGRESS_PATH = saved[0], saved[1]
        server._clock_shown.clear()
        server._clock_shown.update(saved[2])
        server._stuck_cache.clear()
        server._stuck_cache.update(saved[3])

    def write(self, *recs: dict) -> None:
        with self.path.open("a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        os.utime(self.path, (time.time(), time.time() + len(self.path.read_text())))  # new mtime each write

    def test_stuck_signal_shows_at_once_then_clears(self):
        self.assertEqual(server._clock_line(), "")  # shown recently, not stuck
        now = time.time()
        self.write({"type": "stuck", "t": now, "since": now - 17 * 60, "idle_min": 17, "actions": 40})
        line = server._clock_line()
        self.assertIn("m left · no story progress for 17 min: change approach", line)
        self.assertEqual(server._clock_line(), "")  # announced once, then with the clock
        self.write({"type": "progress", "t": now + 1, "kind": "area", "what": "x", "points": 3})
        self.assertNotIn("no story progress", server._clock())

    def test_wrapup_points_to_the_playbook(self):
        server.DEADLINE = time.time() + 5 * 60
        line = server._clock_line()
        self.assertIn("WRAP UP NOW", line)
        self.assertIn("knowledge/playbooks/session-wrapup.md", line)
        self.assertNotIn("skill", line)


if __name__ == "__main__":
    unittest.main()
