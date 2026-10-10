"""Offline tests: combat_turn (a whole turn in one call, with fallbacks and guard checks) and the macros
(loot_area, dialogue_advance, travel_to, regroup, read_all, rest_if_needed), plus the clock's stuck signal.
A thread-safe fake game simulates mod jobs in real time.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

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
NOT_JOBS = {"act.watch", "act.note", "act.target_state", "act.mode"}


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
        self.approach: Any = None  # nav.approach reply (None: op unknown, like a mod not reloaded yet)
        self.los_spot: Any = None  # nav.los_spot reply (or callable(args)); None: op unknown
        self.actions: list = [{"spell": "Target_MainHandAttack", "name": "Attack"}]
        self.path: dict = {"reachable": True, "movement_left": 0}
        self.safe: dict = {"safe": True}  # nav.safe_spot reply
        self.landing: dict | None = None  # nav.landing reply (None: op unknown, like a mod not reloaded yet)
        self.nearby: dict = {"characters": [], "items": []}
        self.inv: dict = {"items": []}
        self.on_start = None  # hook(op, args)
        self.sheets: dict[str, dict] = {}  # state.character replies by uuid

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
                if u in (CHEST, LOCKED, OWNED, BOOK, LETTER, POTION):  # items: like Actions.lua, no dead
                    return {"visible": u in self.visible, "locked": u == LOCKED, "opened": False}
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
                # Like State.lua: unseen combatants are left out, dead ones carry dead=true.
                cs = json.loads(json.dumps(self.combat))
                order = []
                for o in cs.get("order") or []:
                    u = o.get("uuid")
                    if o.get("relation") != "party" and u not in self.visible and u not in self.dead:
                        continue
                    if u in self.dead:
                        o["dead"] = True
                    order.append(o)
                if "order" in cs:
                    cs["order"] = order
                return cs
            if op == "state.actions":
                return json.loads(json.dumps(self.actions))
            if op == "nav.approach" and self.approach is not None:
                return self.approach(args) if callable(self.approach) else dict(self.approach)
            if op == "nav.los_spot" and self.los_spot is not None:
                return self.los_spot(args) if callable(self.los_spot) else dict(self.los_spot)
            if op == "nav.reach_toward":
                r = self.reach(args) if callable(self.reach) else self.reach
                return r if r is not None else {"reachable": False}
            if op == "nav.path":
                return dict(self.path)
            if op == "nav.safe_spot":
                return dict(self.safe)
            if op == "nav.landing" and self.landing is not None:
                return json.loads(json.dumps(self.landing))
            if op == "nav.markers":
                return {"markers": []}
            if op == "state.nearby":
                return json.loads(json.dumps(self.nearby))
            if op == "state.character" and args.get("uuid") in self.sheets:
                return json.loads(json.dumps(self.sheets[args["uuid"]]))
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
                 server.MEMORY, server.delta.changes_text)
        server.game = self.g
        server._watch_game = self.g
        server._queue_mgr = None
        server._async.clear()
        server._early.clear()
        server._early_seen.clear()
        server._looted.clear()
        server.MEMORY = self.tmp
        server.delta.changes_text = lambda game, H: ""
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        # Old tests check the instant-return shape; settle-window tests set it themselves.
        saved_settle, server.SETTLE_S = server.SETTLE_S, 0.0
        self.addCleanup(setattr, server, "SETTLE_S", saved_settle)
        server._pace.update(issued_at=0.0, calls_since=0, next_shown=0, nudge=False)
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

    def test_force_reaches_the_cast(self):
        # Oath of Devotion guard (S026/S028): force=true on an alternative goes to act.cast
        server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob, "force": True}))
        self.assertEqual([a.get("force") for _, a in self.g.ops("act.cast")], [True])

    def test_empty_plan_just_ends_the_turn(self):
        # S031 17:07: {"plan":{}} for a member that had acted through do_actions stopped the chain
        out = server.combat_turn(self.plan())
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], ["act.end_turn"])
        self.assertIn("end_turn: done", out)

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
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=0)
        self.assertIn("FAILED: it is not", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    # claude-run-2 S001/S003/S005/S007: combat_turn sent while enemies acted came back "not your turn" and
    # cost a whole think + call cycle. A plan sent ahead now waits for the turn.
    def test_plan_sent_ahead_waits_for_the_turn(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.4, lambda: self.g.combat.update(current=P1)).start()
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertRegex(out, r"TURN .* — waited 0\.\ds for the turn")
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_waiting_for_the_turn_stops_on_a_prompt_and_times_out(self):
        self.g.combat["current"] = GOB
        self.g.ui["screen"] = "reaction"
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertIn("reaction on screen", out)
        self.g.ui["screen"] = "game"
        t = time.monotonic()
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=0.5)
        self.assertIn("(timed out)", out)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(self.g.ops("act.cast"), [])

    # S029: 5 sent-ahead turns stopped on an enemy attack's Shield Blow prompt; each cost get_dialogue + react
    # + the same plan again. The plan's on_reaction now answers it while waiting; without one the prompt is named.
    def test_waiting_for_the_turn_answers_a_reaction_by_policy(self):
        self.g.combat["current"] = GOB
        self.g.ui["screen"] = "reaction"

        def answer(op, args):
            if op == "ui.react":
                self.g.ui["screen"] = "game"
                threading.Timer(0.2, lambda: self.g.combat.update(current=P1)).start()
        self.g.on_start = answer
        out = server.combat_turn(self.plan(action="Shout_Dodge", on_reaction="skip"), who=server.H.get(P1),
                                 wait_turn=5)
        self.assertEqual(self.g.ops("ui.react")[0][1], {"use": False})
        self.assertRegex(out, r"TURN .* — waited \d\.\ds for the turn — reaction prompt: declined")
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_waiting_for_the_turn_names_the_reaction_prompt(self):
        self.g.combat["current"] = GOB
        self.g.ui.update(screen="reaction", reaction={
            "options": [{"i": 1, "decision": "Undecided"}],
            "prompt": "Justiciar Avenger attacked Claude. | Choose how to react. | 72/88 | Shield Blow | Do Not React"})
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertIn("reaction on screen: Justiciar Avenger attacked Claude. | 72/88 | Shield Blow", out)
        self.assertIn('react(use=true|false)', out)
        self.assertIn('"on_reaction"', out)
        self.assertEqual(self.g.ops("ui.react"), [])

    def test_combat_ending_while_waiting(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.2, lambda: self.g.combat.update(active=False)).start()
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertIn("combat is over", out)

    # S007: Shadowheart's and Gale's casts had no line of sight to the goblin -> action wasted twice.
    def test_no_line_of_sight_retargets_the_nearest_visible_hostile_in_range(self):
        self.g.refuse_if = lambda op, a: "no line of sight to target" if a.get("target") == GOB else None
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        out = server.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, IMP])
        self.assertIn(f"action (auto fallback): Target_SacredFlame {self.imp}: done", out)

    # S018: 14 casts refused "no line of sight" (pillars, a downed ally behind vines): step to a spot that sees it.
    def test_no_line_of_sight_steps_to_a_spot_in_sight_then_casts(self):
        moved = []
        self.g.on_start = lambda op, a: moved.append(a) if op == "act.move_to" else None
        self.g.refuse_if = lambda op, a: ("no line of sight to target"
                                          if op == "act.cast" and a.get("target") == GOB and not moved else None)
        self.g.actions = [{"spell": "Projectile_FireBolt", "range": 18}]
        self.g.los_spot = {"found": True, "pos": [2.0, 0.0, 1.5], "walk_m": 2.5, "dist_m": 7.0, "via": "ring"}
        out = server.combat_turn(self.plan(action={"ability": "Projectile_FireBolt", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, GOB])
        self.assertEqual(len(moved), 1)
        spot = self.g.ops("nav.los_spot")[0][1]
        self.assertEqual((spot["target"], spot["range"]), (GOB, 17.5))
        self.assertIn("no line of sight to target → stepped 2.5m to see it", out)
        self.assertIn(f"Projectile_FireBolt {self.gob}: done", out)
        self.assertNotIn("auto fallback", out)

    def test_no_spot_in_sight_falls_back_to_retarget(self):
        self.g.refuse_if = lambda op, a: "no line of sight to target" if a.get("target") == GOB else None
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        self.g.los_spot = {"found": False, "reason": "no spot in sight"}
        out = server.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, IMP])
        self.assertIn("(auto fallback)", out)

    def test_melee_no_line_of_sight_does_not_seek_a_spot(self):
        self.g.refuse_if = lambda op, a: "no line of sight to target" if a.get("target") == GOB else None
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        self.g.los_spot = {"found": True, "pos": [2.0, 0.0, 1.5], "walk_m": 2.5}
        server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("nav.los_spot"), [])

    # S018 04:06: three combat_turns sent ahead waited 40+30+30 s while our own Spiritual Weapon had its turn.
    def test_sent_ahead_returns_at_once_when_another_party_member_must_act_first(self):
        self.g.combat["current"] = P2
        t = time.monotonic()
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertIn("FAILED: it is not", out)
        self.assertIn("(yours) must act first", out)
        self.assertIn(f"end_turn who={server.H.get(P2)}", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    # S018: end_turn on downed Claude / Shadowheart -> "FAILED: character is downed" (x2); combat_turn too.
    def test_downed_character_turn_is_skipped(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1["hp_pct"] = 0
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
        self.assertIn("is downed: nothing to play", out)
        out = server.end_turn(who=server.H.get(P1), wait=False)
        self.assertIn("skipped", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])

    def test_downed_member_is_not_who_acts(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1["hp_pct"] = 0
        self.g.combat["turn_group"] = [P1, P2]
        self.assertEqual(turn.party_acting(self.g.combat, {P1, P2}), [P2])

    # S018 04:12: Wyll's and Shadowheart's turns (the heal for Claude) stopped on "e4 Claude is downed",
    # though Claude was down before the turn started.
    def test_ally_already_downed_does_not_stop_the_turn(self):
        self.g.watch["party"][1]["downed"] = True
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertNotIn("STOPPED", out)
        self.assertIn("end_turn: done", out)

    def test_ally_going_down_mid_turn_still_stops_it(self):
        def down(op, a):
            if op == "act.cast":
                self.g.watch["party"][1]["downed"] = True
        self.g.on_start = down
        self.g.dur["act.cast"] = 0.2
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("is downed", out)
        self.assertIn("turn NOT ended", out)

    # S002: the planned imp died before Shadowheart's cast -> "skipped (already dead)", action unused.
    def test_dead_target_retargets(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        out = server.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])
        self.assertIn("already dead", out)
        self.assertIn("(auto fallback)", out)

    def test_retarget_respects_reach_sight_and_the_off_switch(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        plan = {"action": {"ability": "Target_MainHandAttack", "target": self.gob}}
        server.combat_turn(self.plan(**plan))  # the imp is 12 m away: out of melee reach
        self.assertEqual(self.g.ops("act.cast"), [])
        imp = next(o for o in self.g.combat["order"] if o["uuid"] == IMP)
        imp["from"][P1] = [2.2, 0]  # in reach (edge to edge) but out of sight
        self.g.combat["current"] = P1
        server.combat_turn(self.plan(**plan))
        self.assertEqual(self.g.ops("act.cast"), [])
        imp["from"][P1] = [2.2, 1]
        self.g.combat["current"] = P1
        server.combat_turn(self.plan(**plan, retarget=False))
        self.assertEqual(self.g.ops("act.cast"), [])
        self.g.combat["current"] = P1
        server.combat_turn(self.plan(**plan))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])

    def test_extra_attack_retargets_after_the_first_kill(self):
        def kill(op, a):
            if op == "act.cast" and a.get("target") == GOB:
                self.g.dead.add(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = kill
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = server.combat_turn(self.plan(action=hit, extra=[[hit]]))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, IMP])
        self.assertIn("extra 1 (auto fallback)", out)

    def test_unplanned_bonus_heals_a_downed_ally(self):
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(hp_pct=0, **{"from": {P1: [7.0, 1]}})
        self.g.visible.add(P2)  # G.visible: party members always
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}, {"spell": "Shout_Dodge"}]
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertEqual(self.casts(), ["Shout_Dodge", "Target_HealingWord"])
        self.assertEqual(self.g.ops("act.cast")[1][1]["target"], P2)
        self.assertIn("bonus (auto: ally downed): Target_HealingWord", out)
        self.assertTrue(out.index("bonus (auto") < out.index("end_turn:"))

    def test_no_auto_bonus_without_a_downed_ally_or_when_planned_or_off(self):
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}]
        self.g.visible.add(P2)
        server.combat_turn(self.plan(action="Shout_Dodge"))
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(hp_pct=0, **{"from": {P1: [7.0, 1]}})
        for kw in ({"auto_bonus": False}, {"bonus": "Shout_SecondWind"}):
            self.g.combat["current"] = P1
            server.combat_turn(self.plan(action="Shout_Dodge", **kw))
        self.assertNotIn("Target_HealingWord", self.casts())
        lae["from"][P1] = [25.0, 1]  # out of range
        self.g.combat["current"] = P1
        server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertNotIn("Target_HealingWord", self.casts())

    def test_not_in_combat(self):
        self.g.combat = {"active": False}
        self.assertIn("nothing to play — not in combat", server.combat_turn(self.plan(action="Shout_Dodge")))

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

    # S002: "move within 1.5 m of Commander Zhalk" said done 6-16 m away, then the melee swing failed.
    def _walk_to(self, dist_after: float) -> None:
        def advance(op, args):
            if op == "act.move_to":
                for o in self.g.combat["order"]:
                    if o["uuid"] == GOB:
                        o["from"][P1] = [dist_after, 1]
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = advance

    def test_within_follows_the_route_and_says_why_it_stopped_short(self):
        self.g.combat = combat(gob_dist=16.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.8, "need_m": 18.5,
                           "route_m": 20.0, "limit": "movement", "movement_left": 9.0, "end_m": 9.9}
        self._walk_to(9.9)
        self.g.refuse_if = lambda op, a: ("target is out of range (9.9 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_MainHandAttack" else None)
        self.g.actions = [{"spell": "Target_MainHandAttack"}, {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("nav.approach")[0][1]["within"], 1.5)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [-4.0, 0.0, 5.0])
        self.assertIn("now 9.9m from", out)
        self.assertIn("stopped short of ≤1.5m: out of movement (9m left, the route needs 18.5m)"
                      " — melee is out of reach this turn", out)
        # acceptance 13:3x: no melee attack sent into a sure range miss; skipped, then the ranged fallback
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])
        self.assertIn(f"skipped ({self.gob} Goblin is out of range for melee after the move (9.9m))", out)
        self.assertIn(f"action (auto fallback): Projectile_MainHandAttack {self.gob}: done", out)

    def test_tight_route_short_by_a_metre_skips_melee_for_the_planned_ranged(self):
        # acceptance 13:3x: "12m left, the route needs 12m" ended 3.1 m away; the plan's own ranged alt runs
        self.g.combat = combat(gob_dist=13.9)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 12.0, "need_m": 12.0,
                           "limit": "movement", "movement_left": 12.0}
        self._walk_to(3.1)
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])
        self.assertIn("melee is out of reach this turn", out)
        self.assertNotIn("FAILED", out)

    def test_route_ends_short_skips_a_useless_dash(self):
        # live 14:5x: "no walkable route gets closer than 2.5m", in_reach=false; Dash + walk on stood still
        self.g.combat = combat(gob_dist=8.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 5.0, "limit": "route_end",
                           "end_m": 2.5}
        self.g.result_for = lambda op, a: ({"dist": 2.9, "target": GOB, "in_reach": False} if op == "act.move_to"
                                           else {"dist": 1.0})
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("melee is out of reach this turn", out)
        self.assertIn("Shout_Dash: skipped (more movement gets no closer", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_blocked_melee_walk_skips_melee_and_the_dash(self):
        # live 15:1x: "move e74: FAILED — could not get closer to the destination — dist=3.5"
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.0, "need_m": 4.0,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=3.5"
                                          if op == "act.move_to" else None)
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("melee is out of reach this turn", out)
        self.assertIn("is out of range for melee after the move (3.5m)", out)
        self.assertIn("Shout_Dash: skipped (more movement gets no closer", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_blocked_spot_beside_target_walks_the_route_instead(self):
        # S032 17:44: post-Dash walk to the spot beside e743 ended where it started (5.7 m, 9 m unspent)
        self.g.combat = combat(gob_dist=5.7)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.5, "need_m": 4.5,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=5.7"
                                          if op == "act.move_to" and a.get("target") else None)
        self._walk_to(1.2)
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        moves = self.g.ops("act.move_to")
        self.assertEqual(moves[-1][1]["pos"], [-4.0, 0.0, 5.0])
        self.assertIn("→ along the route: done", out)
        self.assertIn("now 1.2m from", out)
        self.assertNotIn("melee is out of reach", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack"])

    def test_melee_short_but_within_reach_pad_still_attacks(self):
        # 1.5 m wanted, 2.2 m centre to centre: inside the edge-to-edge reach, the game decides
        self.g.combat = combat(gob_dist=10.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.0, "need_m": 8.7,
                           "limit": "movement", "movement_left": 8.0}
        self._walk_to(2.2)
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.casts(), ["Target_MainHandAttack"])
        self.assertNotIn("melee is out of reach", out)

    def test_move_target_vanished_retargets_the_plan(self):
        # acceptance 13:3x: the Shadow went into Shadow Blend during Claude's walk: both attacks skipped
        # 'not visible', the Dash walked nowhere, 9 m unused
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.discard(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.8, 1]
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash"]))
        self.assertIn(f"target: {self.gob} Goblin is out of sight → the plan now aims at the nearest visible "
                      f"hostile {self.imp} Imp (1.8m)", out)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [IMP])
        self.assertEqual(len(self.g.ops("act.move_to")), 2)  # walked on toward the imp (movement left)

    def test_retarget_without_movement_skips_melee_on_the_far_new_target(self):
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.discard(GOB)
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        self.g.result_for = lambda op, a: {"dist": 1.0, "after": {"movement": 0, "action": 1}}
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertEqual(len(self.g.ops("act.move_to")), 1)  # no movement left: no walk on
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Projectile_MainHandAttack", IMP)])
        self.assertIn(f"{self.imp} Imp is out of range for melee after the move (12m)", out)

    def test_move_target_dead_before_the_turn_aims_at_the_nearest(self):
        self.g.dead.add(GOB)
        out = server.combat_turn(self.plan(move=self.gob,
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("is dead → the plan now aims at the nearest visible hostile", out)
        self.assertEqual(self.g.ops("act.move_to")[0][1].get("target"), IMP)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [IMP])
        server.H.get(GOB, "Goblin")

    def test_move_target_gone_and_nobody_in_sight_skips_the_dash(self):
        def vanish(op, a):
            if op == "act.move_to":
                self.g.visible.clear()
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = vanish
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dash", "Shout_Dodge"]))
        self.assertIn("no other hostile is in sight", out)
        self.assertIn("Shout_Dash: skipped (the move's target", out)
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_target_gone_before_the_turn_no_walk_one_line(self):
        # S028: "target: e742 Shadow is out of sight ... skipped" x3, and the move still ran (FAILED not visible)
        self.g.visible.clear()
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dodge"]))
        self.assertEqual(out.count("no other hostile is in sight"), 1, out)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_retarget_false_keeps_a_vanished_move_target(self):
        self.g.dead.add(GOB)
        out = server.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"], retarget=False))
        self.assertNotIn("the plan now aims", out)

    def test_short_move_without_ranged_dashes_and_walks_on(self):
        self.g.combat = combat(gob_dist=16.0)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.8, "need_m": 14.0,
                           "limit": "movement", "movement_left": 9.0}
        self._walk_to(6.0)
        self.g.refuse_if = lambda op, a: ("target is out of range (6 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_MainHandAttack" else None)
        self.g.actions = [{"spell": "Target_MainHandAttack"}, {"spell": "Shout_Dash"}]
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5},
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual(self.casts(), ["Shout_Dash"])  # melee skipped: out of reach after the move
        self.assertEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn("action (auto fallback): Shout_Dash: done", out)

    def test_no_route_closer_stays_put_and_says_so(self):
        self.g.combat = combat(gob_dist=8.0)
        self.g.approach = {"reachable": True, "pos": [0.0, 0.0, 0.0], "walk_m": 0.2, "limit": "route_end",
                           "end_m": 7.8}
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}, action=["Shout_Dodge"]))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("stays put, 8m away — no walkable route gets closer than 7.8m", out)

    def test_fire_on_the_route_is_the_reason_and_avoid_false_is_passed(self):
        self.g.combat = combat(gob_dist=10.0)
        self.g.approach = {"reachable": True, "pos": [-3.0, 0.0, 0.0], "walk_m": 3.0, "limit": "hazard",
                           "hazard": {"surface": "Fire", "at_m": 4.2}}
        self._walk_to(7.0)
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}, action=["Shout_Dodge"]),
                                 end=False)
        self.assertIn("stopped short of ≤1.5m: kept out of Fire on the route 4.2m ahead", out)
        server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5, "avoid": False},
                                     action=["Shout_Dodge"]), end=False)
        self.assertIs(self.g.ops("nav.approach")[-1][1]["avoid"], False)

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

    def test_plain_dash_walks_the_move_on(self):
        # acceptance 12:5x: "Dash" (no then_move) after a move out of movement dashed and stood still
        n = {"moves": 0}

        def refuse(op, a):
            if op == "act.move_to":
                n["moves"] += 1
                return "no movement left" if n["moves"] == 1 else None
            return None
        self.g.refuse_if = refuse
        server.combat_turn(self.plan(move=self.gob, action=["Shout_Dash"]))
        self.assertEqual(len(self.g.ops("act.move_to")), 2)

    def test_dash_then_move_false_stays(self):
        p = turn.parse_plan(json.dumps({"move": "e1", "action": [{"ability": "Dash", "then_move": False}, "Dash",
                                                                  "Shout_Dash_CunningAction", "Shout_Dodge"]}),
                            server._BATCHABLE)
        self.assertEqual([a.then_move for a in p.slots["action"]], [False, True, True, False])

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

    # S018/S025/S029: Bless on 3 targets was refused ("more than two targets"): the action went to the fallback.
    def test_three_targets_cast_on_the_first_two(self):
        out = server.combat_turn(self.plan(action={"ability": "Target_Bless", "targets": f"{self.gob},{self.imp},{server.H.get(P1)}"}))
        casts = [a for _, a in self.g.ops("act.cast")]
        self.assertEqual(casts[0]["targets"], [GOB, IMP])
        self.assertIn("targets: cast on the first 2 only", out)
        self.assertIn("left out: ", out)
        self.assertIn("action: Target_Bless", out)
        self.assertNotIn("FAILED", out)

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

    def test_extra_attack_skipped_after_a_dash(self):
        # S032 17:44: "extra 1: Target_MainHandAttack e743: FAILED — not enough action" after the action went to Dash
        out = server.combat_turn(self.plan(action="Shout_Dash",
                                           extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]]))
        self.assertEqual(self.casts(), ["Shout_Dash"])
        self.assertIn("extra: skipped (the action went to Shout_Dash", out)
        self.assertEqual(turn.action_without_attack(
            "action: use_ability e743: skipped (out of range) → Shout_Dash: done — statuses=[]"), "Shout_Dash")
        self.assertIsNone(turn.action_without_attack("action: Target_MainHandAttack e743: done — cast=x"))
        self.assertIsNone(turn.action_without_attack("action: Shout_Dash: FAILED — no"))

    def test_plan_errors(self):
        for bad, msg in (({"actoin": "x"}, "unknown plan key"),
                         ({"action": {"tool": "end_turn"}}, "can't be a turn step"),
                         ({"action": {"tool": "save_game"}}, "can't be batched"),
                         ({"action": "Shout_Dodge", "on_reaction": "maybe"}, "on_reaction"),
                         ({"action": "Shout_Dodge", "order": "action,fly"}, "order")):
            self.assertIn(msg, server.combat_turn(json.dumps(bad)), bad)


class OpenerCombat(Base):
    """acceptance 13:3x: a Ranged Attack on a dormant Shadow did damage, no combat began, nothing said so."""

    def setUp(self) -> None:
        super().setUp()
        saved, server.OPENER_WAIT_S = server.OPENER_WAIT_S, 0.05
        self.addCleanup(setattr, server, "OPENER_WAIT_S", saved)
        self.g.result_for = lambda op, a: ({"total_damage": 3, "target": GOB} if op == "act.cast" else {})

    def test_damage_without_a_fight_says_so(self):
        out = server.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertIn(f"combat: NOT started — {self.gob} Goblin took 3 damage but no fight began", out)

    def test_fight_started_says_so(self):
        def start(op, a):
            if op == "act.cast":
                self.g.combat = combat()
        self.g.on_start = start
        out = server.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertIn("COMBAT: the attack started a fight (round 2)", out)

    def test_items_party_and_in_combat_add_nothing(self):
        out = server.use_ability("Projectile_MainHandAttack", target=self.chest)
        self.assertNotIn("combat:", out.lower())
        self.g.combat = combat()
        out = server.use_ability("Projectile_MainHandAttack", target=self.gob)
        self.assertNotIn("combat: NOT", out)
        self.assertNotIn("started a fight", out)


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

    def _in_convo(self, refusals: int) -> None:
        """The Guard refuses the first `refusals` act steps (live S021: InDialog after a fight)."""
        n = {"left": refusals}

        def refuse(op, args):
            if op.startswith("act.") and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (InDialog)"
            return None
        self.g.refuse_if = refuse

    def test_conversation_on_screen_stops_at_once(self):
        # S021: 4 corpses each "FAILED — character is in a conversation"; now one try, then hand it back.
        self._in_convo(99)
        self.g.ui = {"screen": "dialogue"}
        out = server.loot_area()
        self.assertIn("STOPPED: a conversation is open: answer it (choose_dialogue)", out)
        self.assertIn(f"not reached: {self.chest} Chest, {self.gob} Goblin", out)
        self.assertEqual(len(self.g.ops("act.interact")), 1)
        self.assertEqual(self.corpses, [])

    def test_stale_conversation_flag_retried(self):
        saved, server.CONVO_WAIT_S = server.CONVO_WAIT_S, 2.0
        self.addCleanup(setattr, server, "CONVO_WAIT_S", saved)
        self._in_convo(2)
        out = server.loot_area()
        self.assertNotIn("STOPPED", out)
        self.assertNotIn("conversation", out)
        self.assertEqual(len(self.g.ops("act.interact")), 3)  # 2 refused, then opened
        self.assertEqual(self.corpses, [self.gob])

    def test_conversation_flag_that_never_clears(self):
        saved, server.CONVO_WAIT_S = server.CONVO_WAIT_S, 0.5
        self.addCleanup(setattr, server, "CONVO_WAIT_S", saved)
        self._in_convo(99)
        out = server.loot_area()
        self.assertIn("STOPPED: still in a conversation after 0.5s with no dialogue on screen", out)
        self.assertEqual(self.corpses, [])


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


class AutoDialogue(Base):
    """Fast reflexes: the bridge clicks through opening/follow-up filler itself (never a real answer)."""
    CHOICE = {"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
        {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}

    def test_talk_to_shows_the_first_real_choice(self):
        self.g.ui_seq = [{"screen": "game"},
                         {"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}},
                         self.CHOICE]
        out = server.talk_to(self.gob)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [0])
        self.assertIn("auto-advanced 1 opening filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertIn("2. No", out)

    def test_choose_dialogue_clicks_through_following_filler(self):
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hm.", "options": [{"i": 1, "text": "..."}]}},
                         self.CHOICE]
        out = server.choose_dialogue(2)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [2, 1])
        self.assertIn("then auto-advanced 1 filler line(s)", out)
        self.assertIn("STOPPED: a real choice (2 options)", out)

    def test_choose_dialogue_never_picks_a_committing_or_tagged_answer(self):
        for opt in ({"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]}, {"i": 1, "text": "Attack them."}):
            self.g.calls.clear()
            self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "So?", "options": [opt]}}]
            out = server.choose_dialogue(1)
            self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [1])
            self.assertNotIn("auto-advanced", out)
            self.assertIn(opt["text"], out)

    def test_choose_dialogue_end(self):
        self.g.ui = {"screen": "game"}
        out = server.choose_dialogue(1)
        self.assertIn("STOPPED: conversation ended", out)

    def test_plain_interact_adds_nothing_and_stays_quick(self):
        t = time.monotonic()
        out = server.interact(self.chest)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertNotIn("DIALOGUE", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])

    def test_dialogue_started_event_after_a_walk(self):
        self.g.push("dialogue_started", {"speakers": [{"name": "Shadowheart"}]})
        self.g.ui_seq = [self.CHOICE]
        out = server.wait_for("idle", timeout_s=0.1)
        self.assertIn("1. Yes", out)
        self.assertEqual(out.count("1. Yes"), 1)
        # A result that already shows the dialogue (get_status) is left alone.
        self.g.push("dialogue_started", {"speakers": [{"name": "Shadowheart"}]})
        self.assertNotIn("STOPPED", server.get_status())

    def test_ambient_dialogue_event_adds_nothing(self):
        self.g.push("dialogue_started", {"speakers": [{"name": "Lae'zel"}]})
        out = server.wait_for("idle", timeout_s=0.1)
        self.assertNotIn("STOPPED", out)

    def test_autopilot_never_says_a_lone_spoken_answer(self):
        # dialogue_advance (asked for) may take "Hm."; the bridge's own autopilot leaves any spoken line to the agent.
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Well?", "options": [{"i": 1, "text": "Hm."}]}}]
        out = server.choose_dialogue(1)
        self.assertEqual([a["i"] for _, a in self.g.ops("ui.dialogue_choose")], [1])
        self.assertIn("the only answer is yours to give: 'Hm.'", out)

    def test_skill_check_options_show_the_speakers_modifier_not_the_dc(self):
        self.g.sheets = {P1: {"skills": {"Persuasion": 3, "Intimidation": 5, "SleightOfHand": 0},
                              "abilities": {"str": 17, "wis": 10}},
                         P2: {"skills": {"Persuasion": 6, "Intimidation": 2}, "abilities": {"str": 16}}}
        server._sheet_cache.clear()
        self.g.push("dialogue_started", {"party": [{"uuid": P1, "name": "Claude"}], "speakers": []})
        server.get_events()
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "Halt.", "options": [
            {"i": 1, "text": "Let me through.", "tags": ["PERSUASION"]},
            {"i": 2, "text": "Move.", "tags": ["INTIMIDATION"]},
            {"i": 3, "text": "Shove him.", "tags": ["STRENGTH"]},
            {"i": 4, "text": "Pray.", "tags": ["CLERIC"]},
            {"i": 5, "text": "Use the tadpole.", "tags": ["ILLITHID"]},
            {"i": 6, "text": "Leave."}], "dc": 15}}
        out = server.get_dialogue()
        claude, laezel = server.H.get(P1), server.H.get(P2)
        self.assertIn(f"1. [PERSUASION] Let me through.  ← Persuasion check: {claude} +3 (party best {laezel} +6", out)
        self.assertIn(f"2. [INTIMIDATION] Move.  ← Intimidation check: {claude} +5", out)
        self.assertNotIn("party best", out.split("\n")[[i for i, l in enumerate(out.split("\n")) if "2. [" in l][0]])
        self.assertIn(f"Strength check: {claude} +3", out)
        self.assertIn("4. [CLERIC] Pray.  ← class/race/background option", out)
        self.assertIn("tadpole's power", out)
        self.assertIn("6. Leave.", out)
        self.assertNotIn("15", out.split("EVENTS")[0].replace("+15", ""))

    def test_off_switch(self):
        saved, server.AUTO_DIALOGUE = server.AUTO_DIALOGUE, False
        self.addCleanup(setattr, server, "AUTO_DIALOGUE", saved)
        self.g.ui_seq = [{"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}}]
        out = server.talk_to(self.gob)
        self.assertNotIn("STOPPED", out)
        self.assertEqual(self.g.ops("ui.dialogue_choose"), [])


class TravelTo(Base):
    def setUp(self) -> None:
        super().setUp()
        server.SETTLE_S = 10.0  # the trips end within the settle window: their whole report comes back
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0],
                                                             "level": "TUT"}]))
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_chained_walks_to_a_marker(self):
        out = server.travel_to(marker="well", wait=True)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 50.0])
        self.assertIn("travel_to marker m1 Old well: 50m", out)
        self.assertIn("arrived", out)

    def test_no_way_further(self):
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0]}
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_interrupt_stops_the_trip(self):
        self.g.on_start = lambda op, a: op == "act.move_to" and self.g.push("dialogue_started", {})
        out = server.travel_to(marker="m1", wait=True)
        self.assertIn("STOPPED: a conversation started", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_stalled_legs_end_the_trip(self):
        # S001: after the landing, every leg came back "stopped_short ... path blocked" at the same spot.
        self.g.reach = {"reachable": True, "pos": [20.0, 0.0, 0.0]}
        self.g.result_for = lambda op, a: ({"stopped_short": True, "reason": "path blocked",
                                            "after": {"pos": [5.0, 0.0, 0.0]}} if op == "act.move_to" else {})
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        # leg 1 gets 5 m closer (progress), legs 2 and 3 end at the same spot: stop after 2 stalls
        self.assertEqual(len(self.g.ops("act.move_to")), 3)
        self.assertIn("STOPPED: stopped short 2 legs in a row", out)

    def test_fire_on_the_only_way_stops_before_walking_into_it(self):
        self.g.reach = {"reachable": False, "hazard": {"surface": "Fire", "pos": [-18, 18, -424], "at_m": 6}}
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: the way on crosses Fire 6m ahead", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_legs_report_the_surface_they_kept_clear_of(self):
        def reach(a):
            return {"reachable": True, "pos": [50.0, 0.0, 0.0], "avoided": 1,
                    "hazard_beyond": {"surface": "Acid", "at_m": 31}}
        self.g.reach = reach
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("kept clear of Acid 31m ahead", out)
        self.assertIn("arrived", out)

    def test_stepping_into_fire_interrupts_and_steps_out(self):
        def burn(op, a):
            if op == "act.move_to" and len(self.g.ops("act.move_to")) == 1:
                self.g.watch["party"][0]["surface"] = "Fire"
        self.g.on_start = burn
        self.g.dur["act.move_to"] = 0.2
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [-2.0, 0.0, 0.0],
                       "path_m": 2.0, "dir": "W"}
        out = server.travel_to(marker="m1", wait=True)
        # it steps out (toward the destination), then goes on
        self.assertIn("left Fire W 2m", out)
        self.assertEqual(self.g.ops("nav.safe_spot")[-1][1]["pos"], [50, 0, 0])
        self.assertIn("arrived", out)

    def test_stepping_into_fire_strict_steps_out_and_stops(self):
        def burn(op, a):
            if op == "act.move_to" and len(self.g.ops("act.move_to")) == 1:
                self.g.watch["party"][0]["surface"] = "Fire"
        self.g.on_start = burn
        self.g.dur["act.move_to"] = 0.2
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [-2.0, 0.0, 0.0],
                       "path_m": 2.0, "dir": "W"}
        out = server.travel_to(marker="m1", wait=True, avoid_hazards="strict")
        self.assertIn("standing in Fire", out)
        self.assertIn("left Fire W 2m", out)
        self.assertEqual(self.g.ops("act.move_to")[-1][1]["pos"], [-2.0, 0.0, 0.0])

    def test_starting_in_fire_steps_out_first(self):
        self.g.watch["party"][0]["surface"] = "Fire"
        self.g.safe = {"safe": False, "standing_in": "Fire", "found": True, "pos": [3.0, 0.0, 0.0],
                       "path_m": 3.0, "dir": "E"}
        out = server.travel_to(marker="m1", wait=True)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [3.0, 0.0, 0.0])
        self.assertIn("left Fire E 3m", out)
        self.assertIn("arrived", out)

    def test_marker_on_another_level(self):
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "x", "pos": [1, 0, 0],
                                                             "level": "WLD", "region": "Wilderness"}]))
        self.assertIn("fast_travel there first", server.travel_to(marker="m1", wait=True))


class Regroup(Base):
    def test_far_members_walk_back(self):
        # Walks back in travel_to legs (S005: one 77 m move_to was refused as too far).
        self.g.watch["party"][1]["pos"] = [20, 0, 0]
        self.g.reach = {"reachable": True, "pos": [0.0, 0.0, 0.0]}

        def arrive(op, a):
            if op == "act.move_to" and a.get("char") == P2:
                self.g.watch["party"][1]["pos"] = list(a["pos"])
        self.g.on_start = arrive
        out = server.regroup()
        mv = self.g.ops("act.move_to")
        self.assertEqual((mv[0][1]["char"], mv[0][1]["pos"]), (P2, [0.0, 0.0, 0.0]))
        self.assertIn("1 walking", out)
        self.assertIn("(20m → 0m): back", out)

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
        self.addCleanup(shutil.rmtree, self.path.parent, True)
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
        self.assertNotIn("keep playing", server._clock_line())  # shown recently, not stuck: terse at most
        now = time.time()
        self.write({"type": "stuck", "t": now, "since": now - 17 * 60, "idle_min": 17, "actions": 40})
        line = server._clock_line()
        self.assertIn("m left · no story progress for 17 min: change approach", line)
        self.assertNotIn("no story progress", server._clock_line())  # announced once, then with the clock
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
