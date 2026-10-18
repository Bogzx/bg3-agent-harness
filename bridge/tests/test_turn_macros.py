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
from unittest import mock
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
        self.more_party: list[dict] = []  # state.status rows after Claude and Lae'zel
        self.ui: dict = {"screen": "game"}
        self.ui_seq: list[dict] = []  # successive ui.state replies (last one repeats), when set
        self.dur: dict[str, float] = {}
        self.refuse_if = lambda op, args: None
        self.result_for = lambda op, args: {"dist": 1.0, "after": {"hp": 20, "max_hp": 20, "action": 0,
                                                                    "bonus": 1, "movement": 3}}
        self.visible: set[str] = {GOB, IMP, CHEST}
        self.dead: set[str] = set()
        self.gone: dict[str, str] = {}  # act.target_state gone= for an unseen combatant (Actions.lua gone_why)
        self.combat: dict = {"active": False}
        self.reach: Any = None  # nav.reach_toward reply (or callable(args))
        self.approach: Any = None  # nav.approach reply (None: op unknown, like a mod not reloaded yet)
        self.los_spot: Any = None  # nav.los_spot reply (or callable(args)); None: op unknown
        self.actions: list = [{"spell": "Target_MainHandAttack", "name": "Attack"}]
        self.actions_by: dict = {}  # state.actions per uuid (else self.actions for everyone)
        self.path: dict = {"reachable": True, "movement_left": 0}
        self.safe: dict = {"safe": True}  # nav.safe_spot reply
        self.landing: dict | None = None  # nav.landing reply (None: op unknown, like a mod not reloaded yet)
        self.nearby: dict = {"characters": [], "items": []}
        self.inv: dict = {"items": []}
        self.on_start = None  # hook(op, args)
        self.sheets: dict[str, dict] = {}  # state.character replies by uuid
        self.activity: dict | None = None  # nav.activity reply (None: op unknown, like a mod not reloaded yet)
        self.autoskip: dict | None = None  # autoskip.status reply (None: op unknown, an old mod)
        self.active = P1  # state.status active (the selected character); act.select moves it
        self.hud: str | None = None  # ui.selected (the client's ClientControl) when it differs from self.active
        # Actions.lua start(): a new action job for a character interrupts its running one (opt-in: review H1)
        self.supersede = False

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
                if self.supersede and args.get("char"):
                    for j in self.jobs.values():
                        if j["status"] == "running" and j["args"].get("char") == args["char"]:
                            j["status"], j["error"] = "interrupted", "superseded by a new action"
                if op == "act.select":
                    self.active, self.hud = args["char"], None
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
                if u in self.gone and u not in self.visible:
                    return {"visible": False, "gone": self.gone[u]}
                return {"visible": u in self.visible or u in self.dead, "dead": u in self.dead or None}
            if op == "events.since":
                evs = [e for e in self.events if e["seq"] > int(args.get("seq", 0))][: int(args.get("limit", 200))]
                return {"latest": self.seq, "events": evs if context == "server" else []}
            if op == "ui.state":
                if self.ui_seq:
                    return self.ui_seq.pop(0) if len(self.ui_seq) > 1 else self.ui_seq[0]
                return dict(self.ui)
            if op == "ui.selected":
                return {"uuid": self.hud or self.active}
            if op in ("ui.dismiss", "ui.reactions"):
                return {"ok": True}
            if op == "state.status":
                return {"active": self.active, "level": "TUT", "party": [{"uuid": P1, "name": "Claude"},
                                                                 {"uuid": P2, "name": "Lae'zel"}] + self.more_party}
            if op == "state.combat":
                # Like State.lua: unseen combatants are left out, dead ones carry dead=true.
                cs = json.loads(json.dumps(self.combat))
                order = []
                for o in cs.get("order") or []:
                    u = o.get("uuid")
                    if o.get("relation") != "party" and (u not in self.visible and u not in self.dead
                                                         or u in self.gone):
                        continue
                    if u in self.dead:
                        o["dead"] = True
                    order.append(o)
                if "order" in cs:
                    cs["order"] = order
                return cs
            if op == "state.actions":
                return json.loads(json.dumps(self.actions_by.get(args.get("uuid"), self.actions)))
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
            if op == "nav.activity" and self.activity is not None:
                return dict(self.activity)
            if op == "autoskip.status" and self.autoskip is not None:
                return json.loads(json.dumps(self.autoskip))
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


def clear_server_state() -> None:
    """Forget the server's in-flight jobs, queues' leftovers and per-session caches. Base runs it before and after
    each test, so a test that leaves an _async job behind (a walk the queue never polled to its end) can't make a
    later module's tools poll it ('unittest test_turn_based test_objectives': 12 FAILs "unexpected op job.poll")."""
    for d in (server._async, server._early, server._early_seen, server._looted, server._bg_ends, server._turn_sides,
              server._ability_ids, server._trips, server._queue_ignore, server._turn_cs, server._step_note):
        d.clear()


def reset_handles() -> None:
    """Empty server.H in place (modules hold the object): the next uuid registered is e1 again."""
    h = server.H
    h.by_uuid.clear()
    h.by_handle.clear()
    h.names.clear()
    h._n = 0


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.g = FakeGame()
        server._listed.clear()  # the last answer listing shown (choose_dialogue numbers): per test
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-mem-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
                 server.MEMORY, server.delta.changes_text)
        server.game = self.g
        server._watch_game = self.g
        server._queue_mgr = None
        clear_server_state()
        server.MEMORY = self.tmp
        server.delta.changes_text = lambda game, H: ""
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        # Old tests check the instant-return shape; settle-window tests set it themselves.
        saved_settle, server.SETTLE_S = server.SETTLE_S, 0.0
        self.addCleanup(setattr, server, "SETTLE_S", saved_settle)
        # Old tests check combat_turn's inline end ("end_turn: done"); CombatTurnBackgroundEnd turns it on.
        saved_end, server.END_ASYNC = server.END_ASYNC, False
        self.addCleanup(setattr, server, "END_ASYNC", saved_end)
        server._pace.update(issued_at=0.0, calls_since=0, next_shown=0, nudge=False)
        # A17: an empty handle registry, so these are e1 Goblin, e2 Imp, e3 Chest, e4 Claude, e5 Lae'zel whatever ran
        # before ('unittest tests.test_leveling tests.test_turn_macros': 5 FAILs, "e6 Goblin" for "e1 Goblin")
        reset_handles()
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
        clear_server_state()
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

    def test_list_targets_reach_the_cast(self):
        # ACT-8: a plan's list-form targets failed the step "'list' object has no attribute 'split'"
        out = server.combat_turn(self.plan(action={"ability": "Target_Bless", "targets": [self.gob, self.imp]}))
        self.assertNotIn("split", out)
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB, IMP]])

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

    def test_unknown_ability_keeps_the_turn_open(self):
        # S035 19:33: action ["Shout_Dodge"] → "unknown action" (nothing spent), then end_turn threw the action away.
        self.g.refuse_if = lambda op, a: ("unknown action Shout_Dodge (not an ability id; get_actions lists them)"
                                          if a.get("spell") == "Shout_Dodge" else None)
        out = server.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"]))
        self.assertNotIn("act.end_turn", [o for o, _ in self.g.ops("act.")])
        self.assertIn("BG3 has no Dodge action", out)
        self.assertIn("turn NOT ended", out)

    def test_s087_a_stunned_member_does_not_block_a_sent_ahead_turn_and_its_open_turn_is_ended(self):
        # S087 06:03:03-06:03:26: Claude's combat_turn sent ahead was refused after 23 s "e3 Shadowheart (yours) must
        # act first" while Shadowheart was Stunned (Mind Blast): her open turn passes, it never blocks another member
        self.g.combat["current"] = P2
        for o in self.g.combat["order"]:
            if o["uuid"] == P2:
                o.update(is_current=True, ended=True, passes="Stunned")
        ended = []

        def advance(op, args):
            if op == "act.end_turn":
                ended.append(args.get("char"))
                for o in self.g.combat["order"]:
                    o.pop("is_current", None), o.pop("passes", None), o.pop("ended", None)
                self.g.combat["current"] = P1
        self.g.on_start = advance
        self.assertEqual(turn.acting(self.g.combat), [])
        self.assertEqual(turn.passing(self.g.combat), [P2])
        with mock.patch.object(server, "PASS_END_S", 0.0):
            out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertNotIn("must act first", out)
        self.assertEqual(ended[0], P2)
        self.assertIn("ended " + server.H.get(P2) + " Lae'zel's turn (Stunned: it can't act)", out)
        self.assertIn("TURN " + server.H.get(P1), out)

    def test_s087_a_plan_for_a_stunned_member_ends_its_turn_and_says_so(self):
        for o in self.g.combat["order"]:
            if o["uuid"] == P1:
                o.update(is_current=True, ended=True, passes="Stunned")
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
        self.assertIn("Claude is Stunned: nothing to play, its turn passes (ended it)", out)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.end_turn")], [P1])
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_blast_warning_keeps_the_turn_open_for_the_agent(self):
        # S085 05:03:37: Gale's Fireball burst on a ledge on a neutral Guild ally (killed). Guard.blast_risk refuses
        # with where it bursts and who is in the blast; the agent decides: no other alternative, no fallback on
        # another target, no end_turn.
        why = ("Fireball: the way to that point is blocked: it would burst at (-24.5,27.0,742.0), 6.5 m from the "
               "caster and 11.9 m from the point; in its blast (4 m) there: e606 Lady Croup (neutral, 3.4 m): aim "
               "elsewhere or move first, or force=true to cast anyway")
        self.g.refuse_if = lambda op, a: why if a.get("spell") == "Projectile_Fireball" and not a.get("force") else None
        out = server.combat_turn(self.plan(action=[{"ability": "Projectile_Fireball", "x,y,z": [-27, 17, 736]},
                                                   {"ability": "Projectile_FireBolt", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Projectile_Fireball"])
        self.assertNotIn("act.end_turn", [o for o, _ in self.g.ops("act.")])
        self.assertIn("Lady Croup (neutral, 3.4 m)", out)
        self.assertIn("turn NOT ended", out)
        # force=true casts it
        out = server.combat_turn(self.plan(action={"ability": "Projectile_Fireball", "x,y,z": [-27, 17, 736],
                                                   "force": True}))
        self.assertIn("Projectile_Fireball: done", out)

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
        self.assertIn("action: Projectile_MainHandAttack", out)  # the invented id is aliased (S051)

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

    # S080 02:31:45: 60 s into a 12-cultist enemy phase the sent-ahead plan came back "FAILED: it is not e4
    # Claude's turn (now ?); waited 60.1s for the turn (timed out)". Still progressing at the bound: said so,
    # nothing played; a list plan stops there too instead of counting it as played.
    def test_wait_bound_hit_while_enemies_still_act_is_not_a_failure(self):
        self.g.combat["current"] = GOB
        still = (self.g.combat, server.TURN_STILL_ACTING + " (40 turn changes in the wait, the last 1s ago)")
        with mock.patch.object(server, "_wait_turn", return_value=still):
            out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
            self.assertTrue(out.startswith(server.TURN_NOT_YET), out)
            self.assertIn("still taking their turns", out)
            self.assertIn(server.H.get(GOB), out)
            self.assertNotIn("FAILED", out)
            out = server.combat_turn(json.dumps([{"who": server.H.get(P1), "plan": {"action": "Shout_Dodge"}},
                                                 {"who": server.H.get(P2), "plan": {"action": "Shout_Dodge"}}]))
            self.assertIn("not played — the enemies are still taking their turns", out)
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

    def test_touch_cast_in_reach_without_sight_seeks_a_spot(self):
        # S033 18:49: Cure Wounds / Help on downed Shadowheart at 1.5 m: "no line of sight" twice, turn lost
        self.g.combat = combat(gob_dist=1.5)
        moved = []
        self.g.on_start = lambda op, a: moved.append(a) if op == "act.move_to" else None
        self.g.refuse_if = lambda op, a: ("no line of sight to target"
                                          if op == "act.cast" and a.get("target") == GOB and not moved else None)
        self.g.actions = [{"spell": "Target_CureWounds", "range": 1.5}]
        self.g.los_spot = {"found": True, "pos": [1.0, 0.0, 1.0], "walk_m": 1.2}
        out = server.combat_turn(self.plan(action={"ability": "Target_CureWounds", "target": self.gob}))
        self.assertEqual(len(moved), 1)
        self.assertIn("stepped 1.2m to see it", out)
        self.assertIn(f"Target_CureWounds {self.gob}: done", out)

    def test_heal_container_takes_its_biggest_heal(self):
        # S033 18:47: Lay on Hands on Claude at 13/104 took SmallHeal
        self.g.refuse_if = lambda op, a: ("choose a variant: Target_LayOnHands_SmallHeal, Target_LayOnHands_BigHeal, "
                                          "Target_LayOnHands_Cure" if a.get("spell") == "Target_LayOnHands" else None)
        out = server.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": self.gob}))
        self.assertEqual(self.casts(), ["Target_LayOnHands", "Target_LayOnHands_BigHeal"])
        self.assertIn("took the biggest heal variant Target_LayOnHands_BigHeal", out)

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

    # S035: three chains stopped 'not e6's turn (now e510 Spiritual Weapon)': up to 21.7 s waited + a re-send each.
    def test_own_summon_first_is_played_then_the_plan(self):
        p2 = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        p2.update(name="Spiritual Weapon", summon=True)
        self.g.combat["current"] = P2

        def advance(op, args):  # the summon's end hands the turn to Claude, Claude's to the goblin
            if op == "act.end_turn":
                self.g.combat["current"] = P1 if self.g.combat["current"] == P2 else GOB
        self.g.on_start = advance
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 who=server.H.get(P1), wait_turn=5)
        self.assertIn("(your summon) had to act first", out)
        self.assertNotIn("FAILED: it is not", out)
        casts = [(a.get("spell"), a.get("caster") or a.get("who")) for _, a in self.g.ops("act.cast")]
        self.assertEqual(len(casts), 2, casts)               # the summon's attack on the goblin, then Claude's
        self.assertEqual(len(self.g.ops("act.end_turn")), 2)
        self.assertEqual(self.g.ops("act.cast")[0][1].get("target"), GOB)
        # in a list plan too; a heal on an ally is never the summon's target
        self.g.combat["current"] = P2
        n = len(self.g.ops("act.cast"))
        out = server.combat_turn(json.dumps([{"who": server.H.get(P1), "plan": {
            "action": {"ability": "Target_CureWounds", "target": server.H.get(P1)}}}]))
        # no hostile named, none with a position: the summon's turn just ends; the one new cast is Claude's heal
        self.assertEqual([a.get("spell") for _, a in self.g.ops("act.cast")[n:]], ["Target_CureWounds"])
        self.assertIn("(your summon) had to act first", out)
        self.assertNotIn("CHAIN STOPPED", out)

    def test_other_member_first_still_hands_back(self):
        self.g.combat["current"] = P2   # Lae'zel, not a summon: the player plans her turn
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertIn("(yours) must act first", out)
        self.assertNotIn("your summon", out)

    # S018: end_turn on downed Claude / Shadowheart -> "FAILED: character is downed" (x2); combat_turn too.
    def test_downed_character_turn_is_skipped(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
        self.assertIn("is downed: nothing to play", out)
        out = server.end_turn(who=server.H.get(P1), wait=False)
        self.assertIn("skipped", out)
        self.assertNotIn("FAILED", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])

    # S076 00:29-00:32 (Cazador try 6 r6): Karlach at 1/218 HP read hp_pct 0 (State.lua rounded it) and end_turn /
    # combat_turn refused her as downed while set_party showed hp=1/218 action=1. Not downed: she plays.
    def test_one_hp_member_rounded_to_zero_pct_is_not_downed(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1["hp_pct"] = 0                       # round(100 * 1 / 218) from an older mod; no downed flag
        self.assertEqual(turn.downed(self.g.combat), set())
        out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1))
        self.assertNotIn("is downed", out)
        self.assertIn("Shout_Dodge", self.casts())
        self.assertIn("end_turn: done", out)

    def test_stale_conversation_flag_retried_for_a_single_tool(self):
        # S055 06:44: pickpocket ×3 "character is in a conversation" right after the conversation ended
        saved, server.CONVO_WAIT_S = server.CONVO_WAIT_S, 2.0
        self.addCleanup(setattr, server, "CONVO_WAIT_S", saved)
        n = {"left": 2}

        def refuse(op, a):
            if op == "act.pickpocket" and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (dialog 1995725312 res=e411)"
            return None
        self.g.refuse_if = refuse
        self.g.ui = {"screen": "game"}
        out = server._act("pickpocket", "act.pickpocket", {"char": P1, "npc": GOB})
        self.assertIn("pickpocket: done", out)
        self.assertEqual(len(self.g.ops("act.pickpocket")), 3)

    def test_invented_ranged_attack_id_is_aliased(self):
        # S051 ×2: "Projectile_RangedAttack" → "unknown action", a failed alternative each time
        self.assertEqual(server._ability_id(P1, "Projectile_RangedAttack"), "Projectile_MainHandAttack")
        self.assertEqual(server._ability_id(P1, "Target_MeleeAttack"), "Target_MainHandAttack")

    def test_mid_fight_lines_are_waited_out(self):
        # S045 Ketheric: 6 steps "character is in a conversation (InDialog)" while his lines played
        n = {"left": 2}

        def refuse(op, a):
            if op == "act.cast" and n["left"] > 0:
                n["left"] -= 1
                return "character is in a conversation (InDialog)"
            return None
        self.g.refuse_if = refuse
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "Ketheric: ...", "listening": True}}
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("Shout_Dodge: done", out)
        self.assertIn("for the mid-fight lines)", out)
        # an answer on screen: handed back at once
        n["left"] = 99
        self.g.combat = combat()
        self.g.ui = {"screen": "dialogue", "dialogue": {"text": "?", "options": [{"i": 1, "text": "Never."}]}}
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("needs your answer", out)

    def test_hud_end_is_not_interleaved_with_input_from_another_thread(self):
        """Review M7: the background end's HUD fallback (select A, press End Turn) ran while the MCP thread clicked /
        selected for B: the press could land on B. Both take server._INPUT_LOCK now: B's press_key waits."""
        self.g.combat["current"] = P1
        self.g.active = P2                       # someone else is selected: the fallback selects P1 first
        self.g.dur["act.select"] = 0.3
        real = self.g.rpc
        order = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op in ("ui.command", "ui.key") or op == "act.select":
                order.append(op if op != "act.select" else f"select {args['char'][-1]}")
            if op == "ui.command":
                self.g.combat["current"] = GOB
                return {"done": True}
            if op == "ui.key":  # a finished job
                self.g.jobs[999] = {"status": "done", "end": 0, "op": op, "args": args, "result": {"key": "I"}}
                return {"job": 999}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        self.g.ui = {"screen": "inventory", "panels_open": ["Inventory"]}
        t = threading.Thread(target=server._end_turn_by_hud,
                             args=(P1, server.JobResult("failed", None, server.END_IGNORED)))
        t.start()
        deadline = time.monotonic() + 2
        while not order and time.monotonic() < deadline:
            time.sleep(0.01)
        server._act("press_key", "ui.key", {"key": "I"}, context="client", wait_s=2)
        t.join(5)
        self.assertEqual(order[:3], [f"select {P1[-1]}", "ui.command", "ui.key"], order)

    def test_ignored_end_turn_presses_the_hud_end_turn(self):
        # S044/S045: Osiris EndTurn ignored for our Spiritual Weapon / during Ketheric's lines: 10 s TIMEOUT ×9
        self.g.refuse_if = lambda op, a: server.END_IGNORED if op == "act.end_turn" else None
        self.g.on_start = None   # (the class's hook hands the turn on at the order: here the game ignores it)
        real = self.g.rpc
        pressed = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.command":
                pressed.append(args)
                self.g.combat["current"] = GOB
                return {"done": True}
            return real(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.end_turn(who=server.H.get(P1), wait=False)
        self.assertEqual(pressed, [{"widget": "*", "command": "EndTurn"}])
        self.assertIn("the HUD's End Turn", out)
        self.assertNotIn("FAILED", out)
        # the HUD doesn't end it either: says so
        self.g.combat["current"] = P1
        saved, server.END_HUD_WAIT_S = server.END_HUD_WAIT_S, 0.3
        self.addCleanup(setattr, server, "END_HUD_WAIT_S", saved)
        def stuck(op, args=None, context="server", timeout=12.0):
            if op == "ui.command":
                return {"done": True}
            if op == "state.combat":
                self.g.combat["current"] = P1   # the turn never moves on
            return real(op, args, context, timeout)
        self.g.rpc = stuck
        out = server.end_turn(who=server.H.get(P1), wait=False)
        self.assertIn("didn't end it either", out, (out, self.g.combat))

    def test_a_slow_end_after_the_hud_press_is_done_not_failed(self):
        # S066 19:21 #68 / S067 19:46 #65: a shared group's last end came 7-10.4 s after the order, past the Lua 3 s
        # + the old 3 s HUD watch: "didn't end it either", the next combat_turn refused, end_turn found nothing
        clock = [0.0]
        reads = []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.combat":
                reads.append(clock[0])
                return combat(current=GOB) if len(reads) > 1 and clock[0] >= 7.0 else combat()
            return {"done": True}

        def sleep(s):
            clock[0] += s
        g = type("G", (), {"rpc": staticmethod(rpc), "try_rpc": staticmethod(rpc)})()
        with mock.patch.object(server, "game", g), mock.patch.object(server, "_hud_has", lambda u: u == P1), \
                mock.patch.object(server.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(server.time, "sleep", sleep):
            r = server._end_turn_by_hud(P1, server.JobResult("failed", None, server.END_IGNORED))
        self.assertEqual(r.status, "done", r.error)
        self.assertIn("the game, slowly (10 s after the order", r.result["via"])

    def test_downed_member_is_not_who_acts(self):
        p1 = next(o for o in self.g.combat["order"] if o["uuid"] == P1)
        p1.update(hp_pct=0, downed=True)
        self.g.combat["turn_group"] = [P1, P2]
        self.assertEqual(turn.party_acting(self.g.combat, {P1, P2}), [P2])

    # S018 04:12: Wyll's and Shadowheart's turns (the heal for Claude) stopped on "e4 Claude is downed",
    # though Claude was down before the turn started.
    def test_ally_already_downed_does_not_stop_the_turn(self):
        self.g.watch["party"][1]["downed"] = True
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertNotIn("STOPPED", out)
        self.assertIn("end_turn: done", out)

    # S033 18:41: Wyll's AoO mid-turn dropped him below the HP threshold and the watcher cancelled his cast.
    def test_hp_drop_mid_turn_does_not_cancel_the_queued_cast(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4   # 20 % < 35 %
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.2
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = server.combat_turn(self.plan(action=hit, extra=[[dict(hit)]]))
        self.assertIn("action: Target_MainHandAttack e1: done", out)   # the running cast finished
        self.assertNotIn("INTERRUPTED", out)
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertIn("turn NOT ended", out)
        self.assertEqual(len(self.g.ops("act.cast")), 1)                # the extra attack waits for the player
        self.assertEqual(self.g.ops("act.end_turn"), [])

    # review M6: the hold was checked at slot boundaries only; a slot's next alternative still spent after a drop
    def test_hp_drop_during_an_alternative_stops_before_the_next_one(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4   # 20 % < 35 %
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.4
        view = self.g._view

        def failing(jid):  # the first cast runs (the AoO lands meanwhile), then fails late
            j = self.g.jobs[jid]
            if (j["status"] == "running" and j["args"].get("spell") == "Target_MainHandAttack"
                    and time.monotonic() >= j["end"]):
                j["status"], j["error"] = "failed", "the attack was cancelled"
            return view(jid)
        self.g._view = failing
        out = server.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_FireBolt", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Target_MainHandAttack"], out)
        self.assertIn("action: Target_MainHandAttack e1: FAILED", out)
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_hp_drop_during_the_action_skips_then_move(self):
        def aoo(op, a):
            if op == "act.cast":
                self.g.watch["party"][0]["hp"] = 4
        self.g.on_start = aoo
        self.g.dur["act.cast"] = 0.2
        out = server.combat_turn(self.plan(move=self.gob, action=[{"ability": "Shout_Dash", "then_move": True}]))
        self.assertIn("STOPPED: e4 Claude HP 4/20 (HP dropped mid-turn", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1, out)   # the move before the Dash only
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_hp_below_condition_still_decides_inside_the_turn(self):
        self.g.watch["party"][0]["hp"] = 4
        self.g.actions = [{"spell": "Shout_SecondWind"}, {"spell": "Target_MainHandAttack"}]
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           bonus={"ability": "Shout_SecondWind", "if": "hp_below:50"}))
        self.assertIn("Shout_SecondWind", self.casts())

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
        self.assertIn("already dead → aimed at", out)  # re-aimed at once (no skip, no fallback pass)

    # review L4: the re-aim dropped every other pick (a 3-dart Magic Missile became one dart on the new target)
    def test_dead_first_pick_is_replaced_in_targets_only(self):
        self.g.dead.add(GOB)
        self.g.actions = [{"spell": "Projectile_MagicMissile", "range": 18}]
        server.combat_turn(self.plan(action={"ability": "Projectile_MagicMissile", "target": self.gob,
                                             "targets": [self.gob, self.imp, self.gob]}))
        # target + targets = 4 picks; the hotbar route is off, so the 2-target cast takes target + the 1st pick
        # (before: targets popped, one dart). A9: that pair is the imp twice, UseSpell(imp, imp), which the game never
        # started (S066 #59): the cast goes at the imp alone, which takes every dart
        self.assertEqual([(a.get("target"), a.get("targets")) for _, a in self.g.ops("act.cast")], [(IMP, None)])

    def test_dead_picks_are_dropped_from_targets(self):
        self.g.dead.add(IMP)
        self.g.actions = [{"spell": "Projectile_MagicMissile", "range": 18}]
        out = server.combat_turn(self.plan(action={"ability": "Projectile_MagicMissile",
                                                   "targets": [self.gob, self.imp]}))
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB]])
        self.assertIn("dropped dead e2 Imp from the picks", out)

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

    # S033 18:39-18:41: Umbral Tremors left the fight; act.target_state said only visible=false, 4 turns of
    # skipped attacks. Now "is gone (…)" and the attack is re-aimed at once (the Dodge after it is not used).
    def test_gone_target_says_gone_and_reaims_at_once(self):
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "off_stage"
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}, {"spell": "Shout_Dodge"}]
        hit = {"ability": "Target_MainHandAttack", "target": self.gob}
        out = server.combat_turn(self.plan(action=[hit, "Shout_Dodge"], extra=[[dict(hit)]]))
        self.assertIn(f"{self.gob} Goblin is gone (it left the battlefield: dissipated or despawned) → aimed at "
                      f"{self.imp} Imp", out)
        self.assertNotIn("not visible", out)
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Target_MainHandAttack", IMP), ("Target_MainHandAttack", IMP)])

    def test_gone_target_nobody_in_reach_skips_and_says_gone(self):
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "left_fight"
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}, {"spell": "Shout_Dodge"}]
        out = server.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   "Shout_Dodge"]))
        self.assertIn("is gone (it is no longer in the turn order)", out)  # imp 12 m: out of melee reach
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_hidden_target_is_not_reaimed_at_once(self):
        # only out of sight (still in the fight): the old skip + auto fallback, not "gone"
        self.g.visible.discard(GOB)
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("not visible", out)
        self.assertNotIn("is gone", out)
        self.assertIn("(auto fallback)", out)

    def test_gone_move_target_says_gone(self):
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "dead"
        out = server.combat_turn(self.plan(move=self.gob,
                                           action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn(f"target: {self.gob} Goblin is gone (it died) → the plan now aims at the nearest visible "
                      f"hostile {self.imp} Imp", out)

    def test_gone_target_retarget_false_keeps_the_skip(self):
        self.g.visible.discard(GOB)
        self.g.gone[GOB] = "off_stage"
        next(o for o in self.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.9, 1]
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           retarget=False))
        self.assertIn("is gone", out)
        self.assertEqual(self.g.ops("act.cast"), [])

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
        self.assertIn("extra 1: e1 Goblin is already dead → aimed at e2 Imp", out)

    def test_unplanned_bonus_heals_a_downed_ally(self):
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(hp_pct=0, downed=True, **{"from": {P1: [7.0, 1]}})
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
        lae.update(hp_pct=0, downed=True, **{"from": {P1: [7.0, 1]}})
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

    def test_walk_that_never_starts_runs_the_rest_from_here(self):
        # S074 23:21:13: Claude's move stood still (Lethargic) 5.1 s, "no path"; the mod now gives up after 2.5 s
        self.g.combat = combat(gob_dist=8.0)
        self.g.refuse_if = lambda op, a: ("never started moving (2.5 s, the route was there); the character is "
                                          "Lethargic" if op == "act.move_to" else None)
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn("never started moving", out)
        self.assertIn("melee is out of reach this turn", out)
        self.assertEqual(self.casts(), ["Projectile_MainHandAttack"])

    def test_turn_passing_during_the_walk_stops_the_plan(self):
        # S074 23:21:50: "action: it is no longer e5 Claude's turn" came only from the next step; the walk says it now
        self.g.combat = combat(gob_dist=8.0)
        self.g.refuse_if = lambda op, a: ("not this character's turn any more: the game passed the turn on during "
                                          "the walk (travelled 0.0 m)" if op == "act.move_to" else None)
        out = server.combat_turn(self.plan(move=self.gob,
                                           action=[{"ability": "Projectile_MainHandAttack", "target": self.gob}]))
        self.assertIn("the game passed the turn on during the walk", out)
        self.assertEqual(self.casts(), [])

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
        # naut3 15:40:52: the range asked for was reached, so the move line says done, not "FAILED — could not …"
        self.assertIn(f"move {server.H.get(GOB)}: done", out)
        self.assertIn("now 1.2m from", out)
        self.assertIn("(along the route; the walk to the spot beside it stopped 5.7m away)", out)
        self.assertNotIn("could not get closer", out)
        self.assertNotIn("melee is out of reach", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack"])

    def test_route_walk_that_stays_out_of_range_keeps_the_first_failure(self):
        self.g.combat = combat(gob_dist=5.7)
        self.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 4.5, "need_m": 4.5,
                           "limit": "in_range", "movement_left": 9.0}
        self.g.refuse_if = lambda op, a: ("could not get closer to the destination — dist=5.7"
                                          if op == "act.move_to" and a.get("target") else None)
        self._walk_to(3.5)
        out = server.combat_turn(self.plan(move={"target": self.gob, "within": 1.5}))
        self.assertIn("FAILED — could not get closer to the destination", out)
        self.assertIn("→ along the route: done", out)

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

    def test_plan_level_avoid_folds_into_move(self):
        # S061: {"move": ..., "avoid": false} → "unknown plan key(s) avoid" (our refusal says "move avoid:false")
        p = turn.parse_plan(json.dumps({"move": "e1", "avoid": False, "action": ["Dash"]}), server._BATCHABLE)
        self.assertFalse(p.move.avoid)
        p = turn.parse_plan(json.dumps({"move": {"target": "e1", "avoid": True}, "avoid": False}), server._BATCHABLE)
        self.assertTrue(p.move.avoid)  # the move's own key wins
        p = turn.parse_plan(json.dumps({"avoid": False, "action": ["Dash"]}), server._BATCHABLE)
        self.assertIsNone(p.move)

    def test_point_target_becomes_xyz(self):
        # S034 Balthazar: Fireball {"target": "-828,8.5,-783"} → "unknown target" ×N, the agent's lesson said
        # combat_turn can't aim point spells
        p = turn.parse_plan(json.dumps({"action": [{"ability": "Fireball", "target": "-828,8.5,-783"},
                                                   {"ability": "Fireball", "target": [1, 2, 3]},
                                                   {"ability": "Fireball", "pos": [4, 5, 6]},
                                                   {"ability": "Fireball", "x,y,z": "7,8,9"}]}),
                            server._BATCHABLE)
        args = [a.step.args for a in p.slots["action"]]
        self.assertEqual((args[0].get("x"), args[0].get("y"), args[0].get("z")), (-828.0, 8.5, -783.0))
        self.assertNotIn("target", args[0])
        self.assertEqual((args[1]["x"], args[2]["z"], args[3]["y"]), (1.0, 6.0, 8.0))
        server.combat_turn(self.plan(action={"ability": "Shout_Dodge", "target": "1,0,1"}))
        self.assertEqual([a.get("pos") for _, a in self.g.ops("act.cast")][:1], [[1.0, 0.0, 1.0]])

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

    def test_use_ability_takes_a_list_of_targets(self):
        # ACT-8: a client sending targets as a JSON list reached targets.split(",")
        out = server.use_ability("Target_Bless", targets=[self.gob, " ", self.imp])
        self.assertNotIn("split", out)
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB, IMP]])

    def test_reactions_prep(self):
        server.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(self.g.ops("ui.reactions")[0][1], {"char": P1, "set": {"Divine Smite": "always"}})

    # S041 22:41:22: the server said P1 is active (host fallback) but the client's ClientControl was elsewhere.
    def test_reactions_prep_selects_and_retries_on_client_refusal(self):
        from bg3ai.game import GameError
        orig, refused = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions" and not refused:
                refused.append(1)
                self.g.calls.append((op, args))
                raise GameError('[string "BG3AI/Client/UISettings.lua"]:192: char is not the selected character; '
                                'select it first (act.select)')
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(len(self.g.ops("ui.reactions")), 2)
        self.assertIn(("act.select", {"char": P1}), [(o, a) for o, a in self.g.calls if o == "act.select"])
        self.assertIn("reactions set: Divine Smite=always", out)
        self.assertNotIn("reactions: FAILED", out)

    # S069 20:45:31: act.select now waits for ClientControl; when the game keeps another member selected its reason
    # is the prep's failure (no 5 futile retries of the client)
    def test_reactions_prep_names_who_the_game_kept_selected(self):
        from bg3ai.game import GameError
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions":
                self.g.calls.append((op, args))
                raise GameError('[string "BG3AI/Client/UISettings.lua"]:192: char is not the selected character; '
                                'select it first (act.select)')
            if op == "act.select":
                self.g.calls.append((op, args))
                return {"status": "failed", "error": "the game kept Karlach selected (MakePlayerActive twice, once"
                        " via another member)"}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        self.assertEqual(len(self.g.ops("ui.reactions")), 1)
        self.assertIn("could not be selected: the game kept Karlach selected", out)
        self.assertIn("action: Shout_Dodge: done", out)

    # A16 (S074 23:06:52): the server said Claude was selected, the HUD had another member: select before the prep
    def test_reactions_prep_selects_first_when_the_hud_shows_another_member(self):
        self.g.hud = P2
        out = server.combat_turn(self.plan(action="Shout_Dodge", reactions={"Divine Smite": "always"}))
        sel = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.select"]
        reac = [i for i, (o, _) in enumerate(self.g.calls) if o == "ui.reactions"]
        self.assertEqual(len(reac), 1)
        self.assertTrue(sel and sel[0] < reac[0], self.g.calls)
        self.assertIn("reactions set: Divine Smite=always", out)

    # S081 #8 02:43:22: set_reactions(Wyll) under an open Counterspell prompt → "Wyll could not be selected: the HUD
    # kept another member selected". The prompt is named and nothing is selected.
    def test_set_reactions_under_a_reaction_prompt_names_it(self):
        self.g.hud = P2
        self.g.ui = {"screen": "reaction", "reaction": {
            "prompt": "Sharran Fidelian Lamona cast Bone Chill on Claude. | Choose how to react. | 108/160 | Counterspell"}}
        out = server.set_reactions(who=server.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("a reaction prompt is on screen (Sharran Fidelian Lamona cast Bone Chill on Claude.)", out)
        self.assertIn("react(use=true|false)", out)
        self.assertEqual(self.g.ops("act.select"), [])
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S087 06:10:18: set_reactions(Wyll) in the enemies' turns: the select fails; the reason and when it works are named
    def test_set_reactions_in_an_enemy_turn_says_why_the_select_failed(self):
        self.g.hud = P2
        self.g.combat["current"] = GOB      # an enemy acts: no party member's turn
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "act.select":
                self.g.calls.append((op, args))
                return {"status": "failed", "error": "the HUD kept another member selected"}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.set_reactions(who=server.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("could not be selected: the HUD kept another member selected", out)
        self.assertIn("It is the enemies' turn", out)
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S081 #258: a mode change answers with a job (the commands apply a frame later): the result printed `job=1`
    def test_set_reactions_waits_for_the_change(self):
        orig, polls = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions":
                self.g.calls.append((op, args))
                return {"job": 1, "status": "running"}
            if op == "job.poll" and context == "client":
                polls.append(1)
                return {"status": "done", "result": {"char": P1, "reactions": [{"name": "Counterspell", "mode": "never"}]}}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.set_reactions(who=server.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertTrue(polls)
        self.assertIn("Counterspell", out)
        self.assertIn("never", out)
        self.assertNotIn("job=1", out)

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

    def test_melee_step_closer_failure_is_named(self):
        # S042 r4: Karlach's melee "out of range (2.7 m, range 1.5 m)" ×3 with movement left, no word why
        self.g.refuse_if = lambda op, a: (
            "target is out of range (2.7 m, range 1.5 m): move closer first" if op == "act.cast"
            else "no walkable path there (may need a jump or another route)" if op == "act.move_to" else None)
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("(no step closer: ", out)
        self.assertIn("no walkable path there", out)

    def test_extra_attack_used_by_an_earlier_slot_skips_the_extra(self):
        # S042 r4: "bonus (auto fallback): Projectile_MainHandAttack … done … extra_attack", then "extra 1: … not
        # enough action" ×3
        real = self.g.result_for
        self.g.result_for = lambda op, a: ({"cast": a.get("spell"), "extra_attack": True}
                                           if op == "act.cast" and a.get("spell") == "Projectile_MainHandAttack"
                                           else real(op, a))
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                           bonus={"ability": "Projectile_MainHandAttack", "target": self.gob},
                                           extra=[[{"ability": "Target_MainHandAttack", "target": self.gob}]]))
        self.assertIn("extra 1: skipped (the Extra Attack already went to the bonus attack)", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack", "Projectile_MainHandAttack"])

    # ---- ACT-1: fallbacks keep to the planned target's side (S063 11:00 / 11:13) ----

    def _ally_at(self, dist: float, **kw) -> dict:
        lae = next(o for o in self.g.combat["order"] if o["uuid"] == P2)
        lae.update(**{"from": {P1: [dist, 1]}}, **kw)
        self.g.visible.add(P2)
        return lae

    def test_heal_on_an_ally_out_of_range_never_becomes_an_attack_on_it(self):
        # (a) Lay on Hands on ally e3 stopped short → "auto fallback: Projectile_MainHandAttack e3 … damage"
        self._ally_at(9.0)
        self.g.actions = [{"spell": "Target_LayOnHands", "range": 1.5},
                          {"spell": "Projectile_MainHandAttack", "range": 18}]
        self.g.refuse_if = lambda op, a: ("target is out of range (9.0 m, range 1.5 m): move closer first"
                                          if a.get("spell") == "Target_LayOnHands" else None)
        p2 = server.H.get(P2)
        out = server.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": p2}))
        self.assertEqual(self.casts(), ["Target_LayOnHands"])
        self.assertNotIn("auto fallback", out)

    def test_heal_on_an_ally_out_of_range_is_never_retargeted_at_a_hostile(self):
        # (b) Healing Word (L3 slot) planned on an ally went to ENEMY Devastator Stropes
        self._ally_at(20.0)
        self.g.actions = [{"spell": "Target_HealingWord", "range": 18}]
        self.g.refuse_if = lambda op, a: ("target is out of range (20.0 m, range 18.0 m): move closer first"
                                          if a.get("spell") == "Target_HealingWord" and a.get("target") == P2 else None)
        out = server.combat_turn(self.plan(action={"ability": "Target_HealingWord", "target": server.H.get(P2)}))
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")], [P2])
        self.assertNotIn(GOB, [a.get("target") for _, a in self.g.ops("act.cast")])
        self.assertNotIn("auto fallback", out)
        # a heal may re-aim at another (downed) party member in reach
        p3 = "33333333-2222-3333-4444-555555555555"
        server.H.get(p3, "Gale")
        self.g.combat["order"].append({"uuid": p3, "name": "Gale", "relation": "party", "hp_pct": 0,
                                       "downed": True, "from": {P1: [5.0, 1]}})
        self.g.combat["current"] = P1
        n = len(self.g.ops("act.cast"))
        out = server.combat_turn(self.plan(action={"ability": "Target_HealingWord", "target": server.H.get(P2)},
                                           auto_bonus=False))
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.cast")][n:], [P2, p3])
        self.assertIn("(auto fallback)", out)

    def test_attack_on_a_hostile_still_retargets_hostiles(self):
        self.g.dead.add(GOB)
        self._ally_at(3.0)
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        server.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])

    def test_revive_on_a_dead_ally_is_cast_and_never_reaimed(self):
        # (c) _turn_check skipped every dead target, then the retarget aimed Revivify at the nearest hostile
        self._ally_at(1.2)
        self.g.dead.add(P2)
        next(o for o in self.g.combat["order"] if o["uuid"] == GOB)["from"][P1] = [1.8, 1]
        self.g.actions = [{"spell": "Teleportation_Revivify", "range": 1.5}]
        out = server.combat_turn(self.plan(action={"ability": "Teleportation_Revivify", "target": server.H.get(P2)}))
        self.assertEqual([(a.get("spell"), a.get("target")) for _, a in self.g.ops("act.cast")],
                         [("Teleportation_Revivify", P2)])
        self.assertNotIn("already dead", out)
        self.g.refuse_if = lambda op, a: ("no line of sight to target" if op == "act.cast" else None)
        self.g.combat["current"] = P1
        server.combat_turn(self.plan(action={"ability": "Teleportation_Revivify", "target": server.H.get(P2)}))
        self.assertEqual({a.get("target") for _, a in self.g.ops("act.cast")}, {P2})

    def test_move_to_a_dead_ally_is_not_reaimed_at_a_hostile(self):
        # (d) "move to dead Shadowheart, Revivify her" walked toward the nearest enemy and cast Revivify on it
        self._ally_at(4.0)
        self.g.dead.add(P2)
        self.g.actions = [{"spell": "Teleportation_Revivify", "range": 1.5}]
        p2 = server.H.get(P2)
        out = server.combat_turn(self.plan(move=p2, action={"ability": "Teleportation_Revivify", "target": p2}))
        self.assertNotIn("now aims at", out)
        self.assertEqual([a.get("target") for _, a in self.g.ops("act.move_to")], [P2])
        self.assertEqual({a.get("target") for _, a in self.g.ops("act.cast")}, {P2})

    def test_side(self):
        cs = self.g.rpc("state.combat")
        party = {P1, P2}
        self.assertEqual(turn.side(cs, P2, party), "party")
        self.assertEqual(turn.side(cs, GOB, party), "hostile")
        self.assertIsNone(turn.side(cs, CHEST, party))
        self.assertEqual(turn.side(cs, CHEST, party, {CHEST: "neutral"}), "neutral")
        self.g.visible.discard(GOB)  # out of sight now: what an earlier read saw
        self.assertEqual(turn.side(self.g.rpc("state.combat"), GOB, party, turn.sides(cs)), "hostile")

    # ---- ACT-2: who left out (S065: Claude's turn "skipped" twice) ----

    def _shared_group(self) -> None:
        self.g.combat["turn_group"] = [P1, P2]
        attack = {"spell": "Target_MainHandAttack", "name": "Attack"}
        self.g.actions_by = {P1: [attack], P2: [attack, {"spell": "Shout_ActionSurge", "name": "Action Surge"}]}

    def test_shared_turn_without_who_plays_the_member_the_plan_fits(self):
        self._shared_group()  # Claude (P1) is selected; only Lae'zel has Action Surge
        out = server.combat_turn(self.plan(action="Action Surge"))
        self.assertEqual([(a.get("spell"), a.get("char")) for _, a in self.g.ops("act.cast")],
                         [("Shout_ActionSurge", P2)])
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.end_turn")], [P2])
        self.assertIn("who: ", out)
        self.assertIn("Lae'zel (the only one acting", out)

    def test_shared_turn_without_who_refuses_a_plan_both_could_play(self):
        self._shared_group()
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertIn("REFUSED: several of your characters can act now", out)
        self.assertIn("give who=", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])
        out = server.combat_turn(self.plan())  # an empty plan would end an unknown member's turn
        self.assertIn("REFUSED", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_sent_ahead_without_who_is_not_taken_by_another_member(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P2)).start()
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 wait_turn=3)
        self.assertEqual(self.g.ops("act.cast"), [])  # Lae'zel's turn came first: not hers to take
        self.assertIn("(yours) must act first", out)
        # the selected member's own turn plays it
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P1)).start()
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}),
                                 wait_turn=3)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.cast")], [P1])
        self.assertIn("sent ahead: the character selected when it was sent", out)

    def test_sent_ahead_without_who_infers_the_only_member_with_the_ability(self):
        self.g.actions_by = {P1: [{"spell": "Target_MainHandAttack", "name": "Attack"}],
                             P2: [{"spell": "Shout_ActionSurge", "name": "Action Surge"}]}
        self.g.combat["current"] = GOB
        threading.Timer(0.3, lambda: self.g.combat.update(current=P2)).start()
        out = server.combat_turn(self.plan(action="Shout_ActionSurge"), wait_turn=3)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.cast")], [P2])
        self.assertIn("the only one of yours whose actions the plan fits", out)

    # ---- ACT-3: a melee swing refused without a reason steps closer once (S065 ×9) ----

    NO_REASON = ("the game refused the cast (no reason given): a melee swing from 2.1 m centre to centre "
                 "(our reach 2.5 m: range 1.5 m + both bounding radii), probably just outside the engine's reach")

    def test_melee_refused_without_a_reason_steps_in_and_swings_again(self):
        n = {"cast": 0}

        def refuse(op, a):
            if op == "act.cast":
                n["cast"] += 1
                return self.NO_REASON if n["cast"] == 1 else None
            return None
        self.g.refuse_if = refuse
        out = server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS],
                         ["act.cast", "act.move_to", "act.cast", "act.end_turn"])
        mv = self.g.ops("act.move_to")[0][1]
        self.assertEqual((mv.get("target"), mv.get("within")), (GOB, server.MELEE_STEP_IN_M))
        self.assertIn("stepped closer for the swing", out)
        self.assertIn(f"Target_MainHandAttack {self.gob}: done", out)

    def test_melee_refused_steps_in_only_once_and_not_for_other_reasons(self):
        self.g.refuse_if = lambda op, a: self.NO_REASON if op == "act.cast" else None
        server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                     retarget=False))
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertEqual(len(self.g.ops("act.cast")), 2)
        for why in ("the game refused the cast (no reason given): the target is out of sight now",
                    "the game refused the cast (no reason given): no line of sight to the target now"):
            self.g.combat["current"] = P1
            self.g.refuse_if = lambda op, a, why=why: why if op == "act.cast" else None
            n = len(self.g.ops("act.move_to"))
            server.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob},
                                         retarget=False))
            self.assertEqual(len(self.g.ops("act.move_to")), n, why)
        # a ranged ability refused without a reason: no step in
        self.g.combat["current"] = P1
        self.g.actions = [{"spell": "Projectile_FireBolt", "range": 18}]
        self.g.refuse_if = lambda op, a: "the game refused the cast (no reason given): usually the shot's path" \
            if op == "act.cast" else None
        n = len(self.g.ops("act.move_to"))
        server.combat_turn(self.plan(action={"ability": "Projectile_FireBolt", "target": self.gob}, retarget=False))
        self.assertEqual(len(self.g.ops("act.move_to")), n)

    def test_plan_errors(self):
        for bad, msg in (({"actoin": "x"}, "unknown plan key"),
                         ({"action": {"tool": "end_turn"}}, "can't be a turn step"),
                         ({"action": {"tool": "save_game"}}, "can't be batched"),
                         ({"action": "Shout_Dodge", "on_reaction": "maybe"}, "on_reaction"),
                         ({"action": "Shout_Dodge", "order": "action,fly"}, "order")):
            self.assertIn(msg, server.combat_turn(json.dumps(bad)), bad)


class NearestNamesake(Base):
    """ACT-7: disarm/lockpick/loot_corpse by a name two things share take the one in sight nearest the actor."""

    T1, T2 = "71717171-2222-3333-4444-555555555555", "72727272-2222-3333-4444-555555555555"

    def setUp(self) -> None:
        super().setUp()
        server.H.get(self.T1, "Fire Trap")
        server.H.get(self.T2, "Fire Trap")
        self.g.nearby = {"characters": [], "items": [
            {"uuid": self.T1, "name": "Fire Trap", "pos": [9, 0, 0], "dist": 9.0},
            {"uuid": self.T2, "name": "Fire Trap", "pos": [3, 0, 0], "dist": 3.0}]}
        rpc = self.g.rpc

        def with_pos(op, args=None, context="server"):  # state.status party rows carry pos (State.lua member rows)
            r = rpc(op, args, context)
            if op == "state.status":
                for row in r["party"]:
                    row["pos"] = next(m["pos"] for m in self.g.watch["party"] if m["uuid"] == row["uuid"])
            return r
        self.g.rpc = with_pos

    def test_the_nearest_in_sight_is_meant(self):
        self.assertEqual(server._nearest_uuid("Fire Trap", P1), self.T2)
        # from Lae'zel's side the other one is nearer: distances are the actor's, not the active member's
        self.g.watch["party"][1]["pos"] = [10, 0, 0]
        self.assertEqual(server._nearest_uuid("Fire Trap", P2), self.T1)

    def test_a_tie_or_none_in_sight_stays_ambiguous(self):
        self.g.nearby["items"][0]["pos"] = [-3.2, 0, 0]
        with self.assertRaisesRegex(ValueError, "is ambiguous"):
            server._nearest_uuid("Fire Trap", P1)
        self.g.nearby = {"characters": [], "items": []}
        with self.assertRaisesRegex(ValueError, "is ambiguous"):
            server._nearest_uuid("Fire Trap", P1)

    def test_an_id_is_taken_as_is(self):
        self.assertEqual(server._nearest_uuid(server.H.get(self.T1), P1), self.T1)


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

    def test_an_open_loot_window_is_taken_before_stopping(self):
        # S042 22:51:18: a damage stop right after opening a corpse left its loot window unlooted
        def opened(op, a):
            if op == "act.interact":
                self.g.watch["party"] = [{"uuid": P1, "hp": 50, "max_hp": 104, "pos": [0, 0, 0]}]
        self.g.watch["party"] = [{"uuid": P1, "hp": 104, "max_hp": 104, "pos": [0, 0, 0]}]
        self.g.dur["act.interact"] = 0.0   # opened at once; the damage shows at the check after the step
        self.g.on_start = opened
        out = server.loot_area()
        self.assertIn("STOPPED: ", out)
        self.assertIn("took damage outside combat", out)
        self.assertEqual(self.g.ops("ui.loot")[:1], [("ui.loot", {"all": True})], (self.g.ops("ui.loot"), out))  # Take All ran
        self.assertIn(CHEST, server._looted, out)
        self.assertEqual(self.corpses, [])                       # the next target was not started

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


class CombatTurnBackgroundEnd(Base):
    """Latency (naut series): the party's last end before enemy turns runs in the background (END_ASYNC); the
    agent plans while the game ends the turn (1.6-5.3 s a time, ~35 s a run blocked)."""

    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat()
        self.g.watch["combat"] = True

        def advance(op, args):  # ending the turn hands it to the goblin
            if op == "act.end_turn":
                self.g.combat["current"] = GOB
        self.g.on_start = advance
        server.END_ASYNC = True
        self.g.dur["act.end_turn"] = 0.6

    def plan(self, **kw) -> str:
        return json.dumps(kw)

    def gate_end(self) -> None:
        """The background end runs until a hold waits on it (released at the hold's first busy check) or the test
        releases it: nothing here may depend on a 0.6 s end outlasting the tool calls in between (flaky under
        full-suite load, 2026-10-17: no 'waited' in the next combat_turn, 1 of 5 full runs)."""
        self.g.dur["act.end_turn"] = 3600.0
        mgr = server._queues()
        busy, hold = mgr.busy, server._hold_for_bg_end
        holding = threading.local()

        def held_busy(who=None):
            b = busy(who)
            if b and getattr(holding, "on", False):
                self.release_end()  # the hold saw the end running: it waits, then sees it done
            return b

        def gated_hold(char=None):
            holding.on = True
            try:
                return hold(char)
            finally:
                holding.on = False
        for p in (mock.patch.object(mgr, "busy", held_busy), mock.patch.object(server, "_hold_for_bg_end", gated_hold)):
            p.start()
            self.addCleanup(p.stop)

    def release_end(self) -> None:
        """The end finishes at its next poll (one not started yet finishes at once)."""
        with self.g.lock:
            self.g.dur["act.end_turn"] = 0.0
            for j in self.g.jobs.values():
                if j["op"] == "act.end_turn" and j["status"] == "running":
                    j["end"] = 0.0

    def test_returns_before_the_end_finishes_and_reports_it_later(self):
        # The end is the character's queue step: its worker thread sends act.end_turn after combat_turn returned (or
        # just before). Flaky under full-suite load when this asserted the op at once (se, 2026-10-16: 0 != 1 in 1 of
        # 4 full runs, 10/10 alone): wait for it, bounded. The end runs until released, so "returned first" is no race.
        self.gate_end()
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: ending in the background", out)
        self.assertIn(f"NEXT {self.gob} Goblin", out)
        self.assertIn("RUNNING queue", out)
        until = time.monotonic() + 3.0
        while not self.g.ops("act.end_turn") and time.monotonic() < until:
            time.sleep(0.01)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)
        self.assertNotIn("queue_done", server.get_events())  # still running
        self.release_end()
        evs = ""
        until = time.monotonic() + 5.0
        while "queue_done" not in evs and time.monotonic() < until:
            time.sleep(0.1)
            evs += server.get_events()
        self.assertIn("queue_done", evs)
        self.assertEqual(len(self.g.ops("act.end_turn")), 1)

    def test_next_combat_turn_waits_for_the_background_end(self):
        self.gate_end()
        server.combat_turn(self.plan(action="Shout_Dodge"))
        self.g.combat["current"] = P1  # the enemies acted; Claude again
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertRegex(out, r"waited [\d.]+s for e\d+ Claude's turn end to finish first")
        ends = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.end_turn"]
        casts = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.cast"]
        self.assertLess(ends[0], casts[1])

    def test_a_direct_action_for_that_character_waits_too(self):
        self.gate_end()
        server.combat_turn(self.plan(action="Shout_Dodge"))
        out = server.use_ability("Shout_Dodge")
        self.assertIn("turn end to finish first", out)

    def test_review_m4_queued_steps_wait_and_get_their_own_interrupts(self):
        self.gate_end()
        # the end's executor ignores every interrupt: steps queued for that character during it must not run in it
        server.combat_turn(self.plan(action="Shout_Dodge"))
        out = server.queue_actions(json.dumps([{"tool": "use_ability", "ability": "Shout_Dodge"}]),
                                   who=server.H.get(P1))
        self.assertIn("turn end to finish first", out)
        q = server._queues().queues[P1]
        ex = q.executor
        self.assertEqual(ex.watcher.ignore, set())               # not the end's all-kinds set
        # queue_actions returns before its worker sends the cast (flaky under full-suite load, 2026-10-17: the cast
        # list still ended with combat_turn's own cast, before the end): wait for that worker, bounded
        q.thread.join(5.0)
        self.assertFalse(q.thread.is_alive())
        ends = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.end_turn"]
        casts = [i for i, (o, _) in enumerate(self.g.calls) if o == "act.cast"]
        self.assertLess(ends[0], casts[-1])

    def test_review_m5_cancel_queue_never_cancels_the_end_half_way(self):
        self.gate_end()
        server.combat_turn(self.plan(action="Shout_Dodge"))
        until = time.monotonic() + 3.0
        while not self.g.ops("act.end_turn") and time.monotonic() < until:
            time.sleep(0.01)
        out = server.cancel_queue()
        self.assertIn("turn end to finish first", out)
        self.assertEqual(self.g.ops("job.cancel"), [])
        end = next(j for j in self.g.jobs.values() if j["op"] == "act.end_turn")
        server.game.rpc("job.poll", {"id": next(k for k, j in self.g.jobs.items() if j is end)})
        self.assertEqual(end["status"], "done")

    def test_review_m5_replace_waits_for_the_end_too(self):
        self.gate_end()
        server.combat_turn(self.plan(action="Shout_Dodge"))
        out = server.queue_actions(json.dumps([{"tool": "use_ability", "ability": "Shout_Dodge"}]),
                                   who=server.H.get(P1), replace=True)
        self.assertIn("turn end to finish first", out)
        self.assertFalse([j for j in self.g.jobs.values() if j["op"] == "act.end_turn"
                          and j["status"] == "interrupted"])

    def test_s084_a_member_shown_open_while_another_end_runs_names_the_running_end(self):
        # S084 (7x in one fight): Gale's background end (the group's last) still running, the game showed Shadowheart
        # (ended earlier) as open: Claude's plan was refused "e3 Shadowheart (yours) must act first … end it", and
        # the end_turn on her found nothing to end
        p3 = "33333333-2222-3333-4444-555555555555"
        self.g.more_party = [{"uuid": p3, "name": "Shadowheart"}]
        self.g.combat["order"].append({"uuid": p3, "name": "Shadowheart", "relation": "party"})
        self.g.combat["current"] = p3
        self.g.on_start = lambda op, a: None                 # the end never hands the turn on here
        self.g.dur["act.end_turn"] = 3600.0
        self.addCleanup(self.release_end)
        self.assertTrue(server._end_in_background(P2))
        with mock.patch.object(server, "END_BG_HOLD_S", 0.2):
            out = server.combat_turn(self.plan(action="Shout_Dodge"), who=server.H.get(P1), wait_turn=5)
        self.assertIn("Lae'zel's turn end is still running in the background", out)
        self.assertIn("Shadowheart's turn open meanwhile", out)
        self.assertNotIn("must act first", out)
        self.assertIn("waited 0.2s for", out)                # the hold's wait is reported, not only "waited 0.0s"
        self.assertEqual(self.g.ops("act.cast"), [])

    def test_a_shared_turn_group_member_left_ends_inline(self):
        self.g.combat["turn_group"] = [P1, P2]
        self.g.combat["current"] = P1

        def advance(op, args):
            if op == "act.end_turn":
                self.g.combat["turn_group"] = [P2]
        self.g.on_start = advance
        out = server.combat_turn(json.dumps({}), who=server.H.get(P1))
        self.assertIn("end_turn: done", out)
        self.assertNotIn("background", out)

    def test_next_turn_ours_ends_inline_and_shows_its_actions(self):
        self.g.combat["order"] = [o for o in self.g.combat["order"] if o["uuid"] in (P1, P2)] + \
            [o for o in self.g.combat["order"] if o["uuid"] not in (P1, P2)]
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: done", out)
        self.assertIn("'s turn: plan it", out)

    def test_wait_next_blocks_as_before(self):
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=P2)
        out = server.combat_turn(self.plan(action="Shout_Dodge"), wait_next=True)
        self.assertNotIn("background", out)

    def test_off_switch(self):
        server.END_ASYNC = False
        out = server.combat_turn(self.plan(action="Shout_Dodge"))
        self.assertIn("end_turn: done", out)


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

    def test_nobody_in_a_conversation_returns_at_once(self):
        # naut series (latency): "0 filler line(s), STOPPED: conversation ended" took 2.7 s each (the whole 2.5 s
        # open window), 2-3 calls a run. The game says no party member is in one: wait DIALOGUE_IDLE_OPEN_S only.
        self.g.activity = {"in_dialogue": False}
        t = time.monotonic()
        out = server.dialogue_advance()
        self.assertLess(time.monotonic() - t, server.DIALOGUE_IDLE_OPEN_S + 0.6)
        self.assertIn("STOPPED: conversation ended", out)
        self.assertEqual(len(self.g.ops("nav.activity")), 1)

    def test_a_conversation_still_opening_gets_the_whole_window(self):
        self.g.activity = {"in_dialogue": True}  # started on the server, not drawn yet
        self.g.ui_seq = [{"screen": "game"}] * 4 + [{"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}]
        out = server.dialogue_advance()
        self.assertIn("STOPPED: a real choice (2 options)", out)

    # naut series (helm): "*The helm's alien transponder - you've made it in time.*" in the party's combat turn.
    HELM = {"screen": "dialogue", "dialogue": {"speaker": "Narrator", "listening": True,
                                               "text": "*The helm's alien transponder - you've made it in time.*"}}

    def test_a_line_autoskip_cannot_skip_hands_back_early(self):
        self.g.ui = self.HELM
        for blocked, says in (("our turn", "AutoSkip doesn't skip lines in your combat turn"),
                              ("tries", "still up after 8 skips")):
            self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 8, "age": 4000, "blocked": blocked}}
            t = time.monotonic()
            out = server.dialogue_advance()
            self.assertLess(time.monotonic() - t, 2.5)  # not DIALOGUE_LINE_WAIT_S (15 s)
            self.assertIn("STOPPED: line still playing (a line playing 4s, " + says, out)
            self.assertIn("wait_for dialogue_ended or level_changed", out)
        self.g.autoskip = {"on": True, "line": {"id": 7, "notext": True, "tries": 60, "age": 61000, "blocked": "tries"}}
        self.assertIn("a cinematic shot playing 61s", server.dialogue_advance())
        self.g.autoskip = {"on": False}
        self.assertIn("AutoSkip is off", server.dialogue_advance())

    def test_a_line_autoskip_is_skipping_is_waited_for(self):
        # Skipping (no blocked) or too new: the usual wait, and the next prompt is clicked through.
        self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 2, "age": 1500, "turn": True}}
        self.g.ui_seq = [self.HELM] * 8 + [{"screen": "dialogue", "dialogue": {"text": "Choose.", "options": [
            {"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]}}]
        out = server.dialogue_advance()
        self.assertIn("STOPPED: a real choice (2 options)", out)
        self.assertGreaterEqual(len(self.g.ops("autoskip.status")), 1)

    def test_stuck_exit_off_switch_and_old_mod_wait_the_full_window(self):
        self.g.ui = self.HELM
        for exit_s, status in ((0.0, {"on": True, "line": {"tries": 8, "age": 9000, "blocked": "our turn"}}),
                               (2.0, None)):
            self.g.calls.clear()
            self.g.autoskip = status
            with mock.patch.object(server, "DIALOGUE_STUCK_EXIT_S", exit_s), \
                    mock.patch.object(server, "DIALOGUE_LINE_WAIT_S", 1.2):
                t = time.monotonic()
                out = server.dialogue_advance()
            self.assertGreaterEqual(time.monotonic() - t, 1.2)
            self.assertIn("STOPPED: line still playing", out)
            self.assertNotIn("line still playing (", out)
            if exit_s == 0:
                self.assertEqual(self.g.ops("autoskip.status"), [])

    def test_a_read_tool_waits_briefly_on_a_playing_line(self):
        # naut-r2: get_combat_state (dialogue_started in its events) took 15.4 s on the helm line.
        self.g.ui = self.HELM
        self.g.autoskip = {"on": True, "line": {"id": 7, "tries": 1, "age": 300, "turn": True}}
        with mock.patch.object(server, "AUTO_DIALOGUE_READ_LINE_S", 0.6):
            t = time.monotonic()
            out = server._auto_dialogue("get_combat_state", "COMBAT round=6", "dialogue_started []")
            took = time.monotonic() - t
        self.assertLess(took, 1.5)
        self.assertIn("STOPPED: line still playing", out)
        self.assertEqual(server.AUTO_DIALOGUE_READ_LINE_S, 3.0)  # the default; a talk_to/wait_for keeps 15 s

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

    def test_objective_that_moved_on_is_named_on_arrival(self):
        # naut2 run 1: o1 "arrived (0.7m from it)", then the marker stood 66.8m W; 3 calls to find out
        seen = [[{"label": "Escape Route", "pos": [50.0, 0.0, 0.0], "dist": 50.0, "dir": "E", "here": True}],
                [{"label": "Escape Route", "pos": [50.0, 0.0, 66.0], "dist": 66.8, "dir": "N", "here": True}]]
        calls = []
        server._objectives["last"] = []
        server._objective_ids.clear()  # o1 = this marker, whatever ids earlier tests handed out

        def markers(max_age=0.0):
            calls.append(max_age)
            arrived = any(a["pos"][0] >= 50.0 for _, a in self.g.ops("act.move_to"))
            ms = [dict(m) for m in seen[1 if arrived else 0]]
            server._set_objectives(ms)
            return ms
        orig = server._objective_markers
        server._objective_markers = markers
        self.addCleanup(setattr, server, "_objective_markers", orig)
        out = server.travel_to(objective="o1", wait=True)
        self.assertIn("arrived", out)
        self.assertIn('o1 Escape Route moved on: now 66.8m N: travel_to(objective="o1") again', out)
        self.assertEqual(calls[-1], 0.0)  # a fresh read on arrival, not the cached markers

    def test_no_way_further(self):
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0]}
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_no_way_further_says_why_the_games_route_was_not_taken(self):
        # live window #2: inside the Last Light Inn the straight legs dead-ended 106 m short, the reason unsaid
        self.g.reach = {"reachable": True, "pos": [0.2, 0, 0], "direct_why": "partial"}
        out = server.travel_to(x=50.0, y=0.0, z=0.0, wait=True)
        self.assertIn("; the game's path ends short of it", out)
        out = server.travel_to(x=20.0, y=0.0, z=0.0, wait=True)  # one sampled leg: no route asked about
        self.assertIn("STOPPED: no walkable ground leads further", out)
        self.assertNotIn("the game's path", out)

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
        p = mock.patch.object(server, "_fight_now", lambda: False)  # this module's fake game may be mid-fight
        p.start()
        self.addCleanup(p.stop)

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
        self.assertNotIn("no story progress", server._clock_line())  # shown recently, not stuck: terse at most
        now = time.time()
        self.write({"type": "stuck", "t": now, "since": now - 17 * 60, "idle_min": 17, "actions": 40})
        line = server._clock_line()
        self.assertIn(" · no story progress for 17 min: change approach", line)
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
