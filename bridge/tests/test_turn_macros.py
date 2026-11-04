"""Offline test fixtures shared by the test_turn_macros_*.py parts and other tests: a thread-safe fake game that
simulates mod jobs in real time, Base (server state reset per test) and CombatTurnBase (P1's turn in a fight).

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

from bg3ai import actq, delta, macros, turn  # noqa: E402,F401  (F401: also the test_turn_macros_*.py parts' imports)
from bg3ai.tools import core, reporting, plumbing, overview, observe, world_tools, travel, dialogue, combat as combat_tools, uitools, batch, queueing, turn_plan, turn_slots, macro_tools, travel_macro  # noqa: E402,F401
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
        self.route: Any = None  # nav.route reply (or callable(args)); None: op unknown
        self.approach: Any = None  # nav.approach reply (None: op unknown, like a mod not reloaded yet)
        self.los_spot: Any = None  # nav.los_spot reply (or callable(args)); None: op unknown
        self.actions: list = [{"spell": "Target_MainHandAttack", "name": "Attack"}]
        self.actions_by: dict = {}  # state.actions per uuid (else self.actions for everyone)
        self.path: dict = {"reachable": True, "movement_left": 0}
        self.safe: dict = {"safe": True}  # nav.safe_spot reply
        self.landing: dict | None = None  # nav.landing reply (None: op unknown, like a mod not reloaded yet)
        self.nearby: dict = {"characters": [], "items": []}
        self.hazards: list | None = None  # nav.hazards patches (None: op unknown, like a mod not reloaded yet)
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
            if op == "state.actions":  # StateOps.lua: {uuid, resources, actions}
                acts = json.loads(json.dumps(self.actions_by.get(args.get("uuid"), self.actions)))
                return acts if isinstance(acts, dict) else {"uuid": args.get("uuid"), "actions": acts}
            if op == "nav.approach" and self.approach is not None:
                return self.approach(args) if callable(self.approach) else dict(self.approach)
            if op == "nav.los_spot" and self.los_spot is not None:
                return self.los_spot(args) if callable(self.los_spot) else dict(self.los_spot)
            if op == "nav.reach_toward":
                r = self.reach(args) if callable(self.reach) else self.reach
                return r if r is not None else {"reachable": False}
            if op == "nav.route" and self.route is not None:
                return self.route(args) if callable(self.route) else json.loads(json.dumps(self.route))
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
            if op == "nav.hazards" and self.hazards is not None:
                return {"hazards": json.loads(json.dumps(self.hazards))}
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
    for d in (plumbing._async, batch._early, batch._early_seen, macro_tools._looted, overview._ability_ids, batch._trips,
              queueing._queue_ignore, overview._chain_refused, overview._objective_ids):
        d.clear()  # _objective_ids: walk results read the markers now (minimap block), so o1 is per test too


def reset_handles() -> None:
    """Empty core.H in place (modules hold the object): the next uuid registered is e1 again."""
    h = core.H
    h.by_uuid.clear()
    h.by_handle.clear()
    h.names.clear()
    h._n = 0


def no_changes(game, H, parts=delta.ACTION_PARTS, tracker=None, got=None, events=False) -> str:
    """Base's delta.changes_text: no CHANGES lines (the fake world would fill them), but the read itself still
    happens for a caller that takes what it fetched (got=: the minimap block after a walk)."""
    if got is not None:
        got.update(delta.fetch(game, parts, events=events))
    return ""


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
        delta.changes_text = no_changes
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        # Old tests check the instant-return shape; settle-window tests set it themselves.
        saved_settle, plumbing.SETTLE_S = plumbing.SETTLE_S, 0.0
        self.addCleanup(setattr, plumbing, "SETTLE_S", saved_settle)
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
# a fight
# ---------------------------------------------------------------------------

class CombatTurnBase(Base):
    """P1 Claude's turn in a fight with the goblin; ending it hands the turn to the goblin. The test_turn_macros_*.py
    parts subclass it."""

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


if __name__ == "__main__":
    unittest.main()
