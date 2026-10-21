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

from bg3ai import actq, delta, macros, turn  # noqa: E402,F401  (F401: also the test_turn_macros_*.py parts' imports)
from bg3ai.tools import core, reporting, plumbing, overview, observe, world_tools, travel, dialogue, combat as combat_tools, uitools, batch, queueing, turn_plan, turn_slots, turn_tool, macro_tools, travel_macro  # noqa: E402,F401
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
        self.story_timer: dict | None = None  # ui.story_timer reply (None: op unknown, like a mod not reloaded yet)
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
                if u in self.gone and u not in self.visible:  # like Actions.lua: only for one seen on the bar
                    return {"visible": False, "gone": self.gone[u]} if args.get("on_bar") else {"visible": False}
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
            if op == "ui.story_timer" and self.story_timer is not None:
                return dict(self.story_timer)
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
    for d in (plumbing._async, batch._early, batch._early_seen, macro_tools._looted, turn_tool._bg_ends, turn_plan._turn_sides,
              overview._ability_ids, batch._trips, queueing._queue_ignore, turn_plan._turn_cs, turn_slots._step_note):
        d.clear()


def reset_handles() -> None:
    """Empty core.H in place (modules hold the object): the next uuid registered is e1 again."""
    h = core.H
    h.by_uuid.clear()
    h.by_handle.clear()
    h.names.clear()
    h._n = 0


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.g = FakeGame()
        reporting._listed.clear()  # the last answer listing shown (choose_dialogue numbers): per test
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-mem-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = (core.game, queueing._watch_game, queueing._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
                 core.MEMORY, delta.changes_text)
        core.game = self.g
        queueing._watch_game = self.g
        queueing._queue_mgr = None
        clear_server_state()
        core.MEMORY = self.tmp
        delta.changes_text = lambda game, H: ""
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        # Old tests check the instant-return shape; settle-window tests set it themselves.
        saved_settle, plumbing.SETTLE_S = plumbing.SETTLE_S, 0.0
        self.addCleanup(setattr, plumbing, "SETTLE_S", saved_settle)
        # Old tests check combat_turn's inline end ("end_turn: done"); CombatTurnBackgroundEnd turns it on.
        saved_end, turn_tool.END_ASYNC = turn_tool.END_ASYNC, False
        self.addCleanup(setattr, turn_tool, "END_ASYNC", saved_end)
        reporting._pace.update(issued_at=0.0, calls_since=0, next_shown=0, nudge=False)
        # A17: an empty handle registry, so these are e1 Goblin, e2 Imp, e3 Chest, e4 Claude, e5 Lae'zel whatever ran
        # before ('unittest tests.test_leveling tests.test_turn_macros': 5 FAILs, "e6 Goblin" for "e1 Goblin")
        reset_handles()
        self.gob = core.H.get(GOB, "Goblin")
        self.imp = core.H.get(IMP, "Imp")
        self.chest = core.H.get(CHEST, "Chest")
        core.H.get(P1, "Claude")
        core.H.get(P2, "Lae'zel")

    def _restore(self, saved) -> None:
        if queueing._queue_mgr is not None:
            queueing._queue_mgr.cancel()
            for q in queueing._queue_mgr.queues.values():
                if q.thread:
                    q.thread.join(2)
        clear_server_state()
        (core.game, queueing._watch_game, queueing._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S,
         core.MEMORY, delta.changes_text) = saved

    def casts(self) -> list[str]:
        return [a.get("spell") for _, a in self.g.ops("act.cast")]


# ---------------------------------------------------------------------------
# combat_turn
# ---------------------------------------------------------------------------

class CombatTurnBase(Base):
    """combat_turn tests: P1 Claude's turn in a fight with the goblin; ending it hands the turn to the goblin.
    The test_turn_macros_*.py parts subclass it (test_turn_rpcs plays its shapes on CombatTurn)."""

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


class CombatTurn(CombatTurnBase):
    def test_force_reaches_the_cast(self):
        # force=true on an alternative goes to act.cast (the area-blast guard's override)
        turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob, "force": True}))
        self.assertEqual([a.get("force") for _, a in self.g.ops("act.cast")], [True])

    def test_list_targets_reach_the_cast(self):
        # ACT-8: a plan's list-form targets failed the step "'list' object has no attribute 'split'"
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_Bless", "targets": [self.gob, self.imp]}))
        self.assertNotIn("split", out)
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB, IMP]])

    def test_empty_plan_just_ends_the_turn(self):
        # S031 17:07: {"plan":{}} for a member that had acted through do_actions stopped the chain
        out = turn_tool.combat_turn(self.plan())
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], ["act.end_turn"])
        self.assertIn("end_turn: done", out)

    def test_whole_turn_in_one_call(self):
        out = turn_tool.combat_turn(self.plan(
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
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
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
        out = turn_tool.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"]))
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
        with mock.patch.object(turn_slots, "PASS_END_S", 0.0):
            out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertNotIn("must act first", out)
        self.assertEqual(ended[0], P2)
        self.assertIn("ended " + core.H.get(P2) + " Lae'zel's turn (Stunned: it can't act)", out)
        self.assertIn("TURN " + core.H.get(P1), out)

    def test_s087_a_plan_for_a_stunned_member_ends_its_turn_and_says_so(self):
        for o in self.g.combat["order"]:
            if o["uuid"] == P1:
                o.update(is_current=True, ended=True, passes="Stunned")
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1))
        self.assertIn("Claude is Stunned: nothing to play, its turn passes (ended it)", out)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.end_turn")], [P1])
        self.assertEqual(self.g.ops("act.cast"), [])

    def s088_group_ended(self) -> tuple[str, str]:
        """S088 06:19:31 High Hall Courtyard: Claude's group (Claude + Lae'zel here) has ended, every member shown
        is_current + ended while the game applies the ends, and State.lua names the first of them `current`; the next
        party turn is Cambion + the Emperor's (a shared group after the goblin's). Returns their uuids."""
        p3, p4 = "33333333-2222-3333-4444-555555555555", "44444444-2222-3333-4444-555555555555"
        self.g.more_party = [{"uuid": p3, "name": "Cambion"}, {"uuid": p4, "name": "The Emperor"}]
        core.H.get(p3, "Cambion")
        core.H.get(p4, "The Emperor")
        self.g.combat["order"] += [{"uuid": p3, "name": "Cambion", "relation": "party"},
                                   {"uuid": p4, "name": "The Emperor", "relation": "party"}]
        for o in self.g.combat["order"]:
            if o["uuid"] in (P1, P2):
                o.update(is_current=True, ended=True)
        self.g.combat["current"] = P1
        return p3, p4

    def test_s088_a_chain_sent_while_the_last_group_ends_waits_for_its_turn(self):
        # S088 (9 CHAIN STOPPED 'waited 0.0s', ~205 s with the end_turns they caused): "Cambion: not played — it is
        # not e1 Cambion's turn (now e6 Claude); waited 0.0s for the turn (e6 Claude (yours) must act first …)"
        # right after Claude ended: the ended `current` was read as acting
        p3, p4 = self.s088_group_ended()
        reads = [0]
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.combat":
                reads[0] += 1
                if reads[0] == 3:  # the game hands the turn on: the goblin acted, now Cambion + the Emperor
                    for o in self.g.combat["order"]:
                        o.pop("is_current", None), o.pop("ended", None)
                        if o["uuid"] in (p3, p4):
                            o["is_current"] = True
                    self.g.combat.update(current=p3, turn_group=[p3, p4])
            return real(op, args, context, timeout)

        def advance(op, args):
            if op == "act.end_turn":
                g = self.g.combat["turn_group"]
                g.remove(args["char"])
                self.g.combat["current"] = g[0] if g else GOB
        self.g.on_start = advance
        self.assertEqual(turn.acting(self.g.combat), [])
        with mock.patch.object(self.g, "rpc", rpc):
            out = turn_tool.combat_turn(json.dumps([{"who": "Cambion", "plan": {"action": "Shout_Dodge"}},
                                                 {"who": "The Emperor", "plan": {"action": "Shout_Dodge"}}]),
                                     wait_turn=5)
        self.assertNotIn("must act first", out)
        self.assertNotIn("CHAIN STOPPED", out)
        self.assertIn("TURN " + core.H.get(p3) + " Cambion", out)
        self.assertIn("TURN " + core.H.get(p4) + " The Emperor", out)
        self.assertEqual([a.get("char") for _, a in self.g.ops("act.end_turn")], [p3, p4])

    def test_s088_end_turn_on_a_member_of_an_ended_group_names_no_ended_member_as_acting(self):
        # S088 06:34:50: "end_turn e6 Claude: nothing to end — its turn already ended (combat_turn ends it; the rest of
        # its turn group is still acting); now e6 Claude (yours)" (×3): its whole group had ended
        self.s088_group_ended()
        self.g.on_start = lambda op, a: None
        self.g.refuse_if = lambda op, a: ("this character already ended its turn (the rest of its turn group is "
                                          "still acting)" if op == "act.end_turn" else None)
        out = combat_tools.end_turn(who="Claude", wait=False)
        self.assertIn("nothing to end — its turn already ended", out)
        self.assertNotIn("(yours)", out)
        self.assertNotIn("the rest of its turn group is still acting", out)
        self.assertIn("its whole turn group has ended", out)

    def test_blast_warning_keeps_the_turn_open_for_the_agent(self):
        # S085 05:03:37: Gale's Fireball burst on a ledge on a neutral Guild ally (killed). Guard.blast_risk refuses
        # with where it bursts and who is in the blast; the agent decides: no other alternative, no fallback on
        # another target, no end_turn.
        why = ("Fireball: the way to that point is blocked: it would burst at (-24.5,27.0,742.0), 6.5 m from the "
               "caster and 11.9 m from the point; in its blast (4 m) there: e606 Lady Croup (neutral, 3.4 m): aim "
               "elsewhere or move first, or force=true to cast anyway")
        self.g.refuse_if = lambda op, a: why if a.get("spell") == "Projectile_Fireball" and not a.get("force") else None
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Projectile_Fireball", "x,y,z": [-27, 17, 736]},
                                                   {"ability": "Projectile_FireBolt", "target": self.gob}]))
        self.assertEqual(self.casts(), ["Projectile_Fireball"])
        self.assertNotIn("act.end_turn", [o for o, _ in self.g.ops("act.")])
        self.assertIn("Lady Croup (neutral, 3.4 m)", out)
        self.assertIn("turn NOT ended", out)
        # force=true casts it
        out = turn_tool.combat_turn(self.plan(action={"ability": "Projectile_Fireball", "x,y,z": [-27, 17, 736],
                                                   "force": True}))
        self.assertIn("Projectile_Fireball: done", out)

    def test_move_that_does_not_fit_walks_as_far_as_it_can(self):
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 14.2 m, 5.5 m left)"
                                          if op == "act.move_to" and a.get("target") else None)
        self.g.reach = {"reachable": True, "pos": [3.0, 0.0, 4.0]}
        out = turn_tool.combat_turn(self.plan(move=self.gob, action=["Shout_Dodge"]), end=False)
        moves = self.g.ops("act.move_to")
        self.assertEqual(moves[1][1]["pos"], [3.0, 0.0, 4.0])
        self.assertAlmostEqual(self.g.ops("nav.reach_toward")[0][1]["max_m"], 5.2)
        self.assertIn("→ partial 5.2m toward it: done", out)
        self.assertEqual(self.g.ops("act.end_turn"), [])

    def test_within_range_skips_the_move(self):
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 18},
                                           action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("already within 18m", out)

    def test_within_walks_only_to_range(self):
        self.g.reach = {"reachable": True, "pos": [2.0, 0.0, 0.0]}
        turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 4},
                                     action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertAlmostEqual(self.g.ops("nav.reach_toward")[0][1]["max_m"], 2.3)  # 6 - 4 + 0.3
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [2.0, 0.0, 0.0])

    def test_within_never_walks_into_melee_when_no_ground_leads_there(self):
        out = turn_tool.combat_turn(self.plan(move={"target": self.gob, "within": 4},
                                           action={"ability": "Projectile_RangedAttack", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("no walkable ground toward it", out)
        self.assertIn("action: Projectile_MainHandAttack", out)  # the invented id is aliased (S051)

    def test_dead_target_skips_to_the_next_alternative(self):
        self.g.dead.add(GOB)
        out = turn_tool.combat_turn(self.plan(action=[{"ability": "Target_MainHandAttack", "target": self.gob},
                                                   {"ability": "Target_MainHandAttack", "target": self.imp}]))
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [IMP])
        self.assertIn("is already dead", out)

    def test_not_my_turn_is_refused(self):
        self.g.combat["current"] = GOB
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=0)
        self.assertIn("FAILED: it is not", out)
        self.assertEqual(self.g.ops("act.cast"), [])

    # claude-run-2 S001/S003/S005/S007: combat_turn sent while enemies acted came back "not your turn" and
    # cost a whole think + call cycle. A plan sent ahead now waits for the turn.
    def test_plan_sent_ahead_waits_for_the_turn(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.4, lambda: self.g.combat.update(current=P1)).start()
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertRegex(out, r"TURN .* — waited 0\.\ds for the turn")
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_waiting_for_the_turn_stops_on_a_prompt_and_times_out(self):
        self.g.combat["current"] = GOB
        self.g.ui["screen"] = "reaction"
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertIn("reaction on screen", out)
        self.g.ui["screen"] = "game"
        t = time.monotonic()
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=0.5)
        self.assertIn("(timed out)", out)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(self.g.ops("act.cast"), [])

    # S080 02:31:45: 60 s into a 12-cultist enemy phase the sent-ahead plan came back "FAILED: it is not e4
    # Claude's turn (now ?); waited 60.1s for the turn (timed out)". Still progressing at the bound: said so,
    # nothing played; a list plan stops there too instead of counting it as played.
    def test_wait_bound_hit_while_enemies_still_act_is_not_a_failure(self):
        self.g.combat["current"] = GOB
        still = (self.g.combat, turn_tool.TURN_STILL_ACTING + " (40 turn changes in the wait, the last 1s ago)")
        with mock.patch.object(turn_slots, "_wait_turn", return_value=still):
            out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1))
            self.assertTrue(out.startswith(turn_tool.TURN_NOT_YET), out)
            self.assertIn("still taking their turns", out)
            self.assertIn(core.H.get(GOB), out)
            self.assertNotIn("FAILED", out)
            out = turn_tool.combat_turn(json.dumps([{"who": core.H.get(P1), "plan": {"action": "Shout_Dodge"}},
                                                 {"who": core.H.get(P2), "plan": {"action": "Shout_Dodge"}}]))
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
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge", on_reaction="skip"), who=core.H.get(P1),
                                 wait_turn=5)
        self.assertEqual(self.g.ops("ui.react")[0][1], {"use": False})
        self.assertRegex(out, r"TURN .* — waited \d\.\ds for the turn — reaction prompt: declined")
        self.assertEqual(self.casts(), ["Shout_Dodge"])

    def test_waiting_for_the_turn_names_the_reaction_prompt(self):
        self.g.combat["current"] = GOB
        self.g.ui.update(screen="reaction", reaction={
            "options": [{"i": 1, "decision": "Undecided"}],
            "prompt": "Justiciar Avenger attacked Claude. | Choose how to react. | 72/88 | Shield Blow | Do Not React"})
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertIn("reaction on screen: Justiciar Avenger attacked Claude. | 72/88 | Shield Blow", out)
        self.assertIn('react(use=true|false)', out)
        self.assertIn('"on_reaction"', out)
        self.assertEqual(self.g.ops("ui.react"), [])

    def test_combat_ending_while_waiting(self):
        self.g.combat["current"] = GOB
        threading.Timer(0.2, lambda: self.g.combat.update(active=False)).start()
        out = turn_tool.combat_turn(self.plan(action="Shout_Dodge"), who=core.H.get(P1), wait_turn=5)
        self.assertIn("combat is over", out)

    # S007: Shadowheart's and Gale's casts had no line of sight to the goblin -> action wasted twice.
    def test_no_line_of_sight_retargets_the_nearest_visible_hostile_in_range(self):
        self.g.refuse_if = lambda op, a: "no line of sight to target" if a.get("target") == GOB else None
        self.g.actions = [{"spell": "Target_SacredFlame", "range": 18}]
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
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
        out = turn_tool.combat_turn(self.plan(action={"ability": "Projectile_FireBolt", "target": self.gob}))
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
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_SacredFlame", "target": self.gob}))
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual([a["target"] for _, a in self.g.ops("act.cast")], [GOB, IMP])
        self.assertIn("(auto fallback)", out)

    def test_melee_no_line_of_sight_does_not_seek_a_spot(self):
        self.g.refuse_if = lambda op, a: "no line of sight to target" if a.get("target") == GOB else None
        self.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
        self.g.los_spot = {"found": True, "pos": [2.0, 0.0, 1.5], "walk_m": 2.5}
        turn_tool.combat_turn(self.plan(action={"ability": "Target_MainHandAttack", "target": self.gob}))
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
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_CureWounds", "target": self.gob}))
        self.assertEqual(len(moved), 1)
        self.assertIn("stepped 1.2m to see it", out)
        self.assertIn(f"Target_CureWounds {self.gob}: done", out)

    def test_heal_container_takes_its_biggest_heal(self):
        # S033 18:47: Lay on Hands on Claude at 13/104 took SmallHeal
        self.g.refuse_if = lambda op, a: ("choose a variant: Target_LayOnHands_SmallHeal, Target_LayOnHands_BigHeal, "
                                          "Target_LayOnHands_Cure" if a.get("spell") == "Target_LayOnHands" else None)
        out = turn_tool.combat_turn(self.plan(action={"ability": "Target_LayOnHands", "target": self.gob}))
        self.assertEqual(self.casts(), ["Target_LayOnHands", "Target_LayOnHands_BigHeal"])
        self.assertIn("took the biggest heal variant Target_LayOnHands_BigHeal", out)

    def test_ally_call_quick_pick_takes_the_first_usable_group_at_the_point(self):
        # A27: the mod lists the usable groups first, by name only (S093: the ids were a fairness leak)
        box = "Shout_END_AllyAbilities_MultipleSummon"
        self.g.refuse_if = lambda op, a: (
            "Gather Your Allies casts nothing by itself: use_ability which=<one of The Watch [already called], "
            "Guildmaster Keene's Fixers, Gur Huntwardens [already called]>, x,y,z = the ground where they appear"
            if a.get("spell") == box and not a.get("which") else None)
        out = turn_tool.combat_turn(self.plan(action={"ability": box, "x": 5.0, "y": 0.0, "z": 1.0}))
        self.assertEqual(self.casts(), [box, box])
        last = self.g.ops("act.cast")[-1][1]
        self.assertEqual(last.get("which"), "Guildmaster Keene's Fixers")
        self.assertEqual(last.get("pos"), [5.0, 0.0, 1.0])
        self.assertIn("took the first variant Guildmaster Keene's Fixers", out)
        self.assertNotIn("Target_END", out)


if __name__ == "__main__":
    unittest.main()
