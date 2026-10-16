"""Offline tests for think-while-acting: async actions, wait_for, the action queue, do_actions conditions,
interrupts and reports. A thread-safe fake game simulates mod jobs in real time.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import actq, server  # noqa: E402
from bg3ai.game import GameError, JobResult  # noqa: E402

P1 = "11111111-2222-3333-4444-555555555555"
P2 = "22222222-2222-3333-4444-555555555555"
GOB = "99999999-2222-3333-4444-555555555555"
DOOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeGame:
    """Jobs finish `dur` seconds after they start; job.cancel interrupts them. Everything is scriptable."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[int, dict] = {}
        self.next_id = 1
        self.calls: list[tuple[str, Any]] = []
        self.events: list[dict] = []
        self.seq = 0
        self.event_seq = {"server": 0, "client": 0}
        self.watch: dict = {"party": [{"uuid": P1, "hp": 20, "max_hp": 20}], "seen": [], "combat": False,
                            "dialogue": False}
        self.screen = "game"
        self.ui: dict = {}  # extra ui.state fields (dialogue, roll, ...)
        self.dur: dict[str, float] = {}  # op -> seconds
        self.refuse: dict[str, str] = {}  # op -> error
        self.visible: set[str] = {DOOR}
        self.combat: dict = {"active": False}
        self.on_start = None  # hook(op, args)

    # -- helpers --
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
            j["result"] = {"dist": 1.0, "after": {"hp": 20, "max_hp": 20, "action": 1, "movement": 5}}
            if j["notify"]:
                self.push("job_finished", {"job": f"j{jid}", "status": "done"})
        return {"id": jid, "status": j["status"], "result": j.get("result"), "error": j.get("error"),
                "elapsed_ms": 10}

    # -- Game API used by the server --
    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
        args = args or {}
        with self.lock:
            self.calls.append((op, args))
            if op.startswith("act.") and op not in ("act.watch", "act.note", "act.target_state"):
                if self.on_start:
                    self.on_start(op, args)
                jid = self.next_id
                self.next_id += 1
                if op in self.refuse:
                    self.jobs[jid] = {"status": "failed", "error": self.refuse[op], "end": 0, "notify": False}
                    return {"job": jid, "status": "failed", "error": self.refuse[op]}
                self.jobs[jid] = {"status": "running", "end": time.monotonic() + self.dur.get(op, 0.05),
                                  "notify": bool(args.get("notify"))}
                return {"job": jid, "eta_s": 2.4, "route_m": 8.0}
            if op == "job.poll":
                return self._view(int(args["id"]))
            if op == "job.notify":  # RPC.lua: announce a waited-on job's end from now on (ACT-10)
                j = self.jobs[int(args["id"])]
                if j["status"] == "running":
                    j["notify"] = True
                return self._view(int(args["id"]))
            if op == "job.cancel":
                j = self.jobs[int(args["id"])]
                if j["status"] == "running":
                    j["status"], j["error"] = "interrupted", "cancelled"
                return {"status": j["status"]}
            if op == "act.watch":
                return dict(self.watch)
            if op == "act.note":
                self.push(args["kind"], args.get("data"))
                return {"ok": True}
            if op == "act.target_state":
                return {"visible": args["uuid"] in self.visible}
            if op == "events.since":
                evs = [e for e in self.events if e["seq"] > int(args.get("seq", 0))][: int(args.get("limit", 200))]
                return {"latest": self.seq, "events": evs if context == "server" else []}
            if op == "ui.state":
                return {"screen": self.screen, **self.ui}
            if op == "state.status":
                return {"active": P1, "party": [{"uuid": P1, "name": "Claude"}, {"uuid": P2, "name": "Lae'zel"}]}
            if op == "state.combat":
                return dict(self.combat)
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


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.g = FakeGame()
        saved = (server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S)
        server.game = self.g
        server._watch_game = self.g
        server._queue_mgr = None
        server._async.clear()
        actq.WATCH_EVERY_S, actq.UI_EVERY_S = 0.01, 0.01
        self.addCleanup(self._restore, saved)
        # Old tests check the instant-return shape; settle-window tests set it themselves.
        saved_settle, server.SETTLE_S = server.SETTLE_S, 0.0
        self.addCleanup(setattr, server, "SETTLE_S", saved_settle)
        server._pace.update(issued_at=0.0, calls_since=0, next_shown=0, nudge=False)
        self.door = server.H.get(DOOR, "Door")
        self.gob = server.H.get(GOB, "Goblin")
        server.H.get(P1, "Claude")
        server.H.get(P2, "Lae'zel")

    def _restore(self, saved) -> None:
        if server._queue_mgr is not None:
            server._queue_mgr.cancel()
            for q in server._queue_mgr.queues.values():
                if q.thread:
                    q.thread.join(2)
        server.game, server._watch_game, server._queue_mgr, actq.WATCH_EVERY_S, actq.UI_EVERY_S = saved

    def wait_queue_idle(self, s: float = 3.0) -> None:
        end = time.monotonic() + s
        while server._queues().busy() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(server._queues().busy(), "queue still busy")


class AsyncActions(Base):
    def test_move_to_returns_at_once_with_eta(self):
        self.g.dur["act.move_to"] = 5
        t0 = time.monotonic()
        out = server.move_to(target=self.door)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(out.startswith("move_to: started j1, ETA 2.4s (8m)"), out)
        self.assertTrue(self.g.ops("act.move_to")[0][1]["notify"])

    def test_a_waited_job_that_outlives_its_wait_is_followed(self):
        # ACT-10: "still running (use wait to keep watching)" with no id and no job_finished: the outcome was lost
        self.g.dur["act.interact"] = 0.4
        out = server._act("interact", "act.interact", {"char": P1, "target": DOOR}, wait_s=0.05)
        self.assertIn("still running", out)
        self.assertIn("goes on as j1", out)
        self.assertIn("j1", server._async)
        self.assertIn("RUNNING j1 interact", server.get_events())
        out = server.wait_for("j1", timeout_s=2)
        self.assertIn("interact: done", out)

    def test_a_waited_job_that_ends_before_it_is_followed_reports_its_end(self):
        orig = self.g.rpc

        def ended(op, args=None, context="server", timeout=12.0):
            if op == "job.notify":
                return {"id": args["id"], "status": "failed", "error": "could not use that", "elapsed_ms": 9}
            return orig(op, args, context, timeout)
        self.g.rpc = ended
        self.g.dur["act.interact"] = 5
        out = server._act("interact", "act.interact", {"char": P1, "target": DOOR}, wait_s=0.05)
        self.assertIn("interact: FAILED — could not use that", out)
        self.assertNotIn("j1", server._async)

    def test_async_refusal_is_reported_immediately(self):
        self.g.refuse["act.move_to"] = "no walkable path there"
        out = server.move_to(target=self.door)
        self.assertIn("FAILED — no walkable path there", out)
        self.assertEqual(server._async, {})

    def test_wait_for_job(self):
        server.move_to(target=self.door)
        out = server.wait_for("j1", timeout_s=2)
        self.assertIn("move_to: done", out)
        self.assertNotIn("job_finished", out)  # the result already says it: no duplicate EVENTS line

    def test_wait_for_times_out_and_lists_running(self):
        self.g.dur["act.move_to"] = 5
        server.move_to(target=self.door)
        out = server.wait_for("j1", timeout_s=0.2)
        self.assertIn("still waiting for j1", out)
        self.assertIn("RUNNING j1 move_to", out)

    def test_wait_for_event_kind_does_not_eat_events(self):
        self.g.push("combat_started", {})
        out = server.wait_for("combat_started,dialogue_started", timeout_s=1)
        self.assertIn("combat_started happened", out)
        self.assertIn("EVENTS", out)

    def test_wait_for_returns_early_on_a_prompt(self):
        def later():
            time.sleep(0.15)
            self.g.screen = "reaction"
        threading.Thread(target=later).start()
        out = server.wait_for("combat_started", timeout_s=3)
        self.assertIn("stopped waiting: reaction on screen", out)

    def test_wait_for_stops_when_the_rest_screen_opens(self):
        """S026 09:04: popup -> rest screen during a wait for dialogue_started (25 s, then 20 s more)."""
        self.g.screen = "popup"

        def later():
            time.sleep(0.15)
            self.g.screen = "rest"
        threading.Thread(target=later).start()
        t0 = time.monotonic()
        out = server.wait_for("dialogue_started,combat_started", timeout_s=5)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("stopped waiting: the rest screen is open and waits on you: long_rest finishes it", out)

    def test_wait_for_an_event_on_an_input_screen_answers_at_once(self):
        self.g.screen = "rest"
        t0 = time.monotonic()
        out = server.wait_for("dialogue_started,combat_started", timeout_s=5)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("not waiting: the rest screen is open and waits on you", out)

    def test_wait_for_an_event_with_nothing_running_gives_up_early(self):
        old = server.IDLE_EVENT_WAIT_S
        server.IDLE_EVENT_WAIT_S = 0.2
        try:
            t0 = time.monotonic()
            out = server.wait_for("dialogue_started", timeout_s=5)
            self.assertLess(time.monotonic() - t0, 1.5)
            self.assertIn("nothing is running that would cause dialogue_started", out)
            self.g.dur["act.move_to"] = 0.6  # a walk is running: it could start one, so keep waiting
            server.move_to(target=self.door)
            out = server.wait_for("dialogue_started", timeout_s=0.5)
            self.assertIn("still waiting for dialogue_started", out)
        finally:
            server.IDLE_EVENT_WAIT_S = old

    def test_wait_for_my_turn(self):
        self.g.combat = {"active": True, "current": P1}
        self.assertIn("'s turn", server.wait_for("my_turn", timeout_s=1))

    def test_get_events_never_blocks_and_shows_running(self):
        self.g.dur["act.move_to"] = 5
        server.move_to(target=self.door)
        t0 = time.monotonic()
        out = server.get_events()
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertIn("RUNNING j1 move_to ~", out)

    def test_blocking_tools_still_block_by_default(self):
        out = server.interact(self.door)
        self.assertIn("interact: done", out)


class DoActions(Base):
    def test_conditions_skip_and_run(self):
        self.g.watch["party"] = [{"uuid": P1, "hp": 8, "max_hp": 20}]
        out = server.do_actions('[{"tool":"interact","target":"%s","unless":"in_combat"},'
                                '{"tool":"use_ability","ability":"Shout_SecondWind","if":"in_combat"},'
                                '{"tool":"use_ability","ability":"Target_Heal","target":"Claude","if":"hp_below:50"}]'
                                % self.door)
        self.assertIn("1. interact: done", out)
        self.assertIn("2. use_ability Shout_SecondWind: skipped (if in_combat)", out)
        self.assertIn("3. use_ability: done", out)
        self.assertIn("NOW ", out)
        self.assertNotIn("after", out)

    def test_failed_step_stops_and_lists_what_was_left(self):
        self.g.refuse["act.interact"] = "it is locked"
        out = server.do_actions('[{"tool":"interact","target":"%s"},{"tool":"pick_up","item":"%s"},'
                                '{"tool":"move_to","target":"%s"}]' % (self.door, self.door, self.door))
        self.assertIn("1. interact: FAILED — it is locked", out)
        self.assertIn("STOPPED: step 1 interact %s failed; not run: pick_up %s, move_to %s"
                      % (self.door, self.door, self.door), out)

    def test_new_hostile_interrupts_the_running_step(self):
        self.g.dur["act.move_to"] = 3

        def hostile_appears(op, args):
            if op == "act.move_to":
                threading.Timer(0.1, lambda: self.g.watch.update(
                    seen=[{"uuid": GOB, "name": "Goblin", "hostile": True}])).start()
        self.g.on_start = hostile_appears
        t0 = time.monotonic()
        out = server.do_actions('[{"tool":"move_to","target":"%s"},{"tool":"interact","target":"%s"}]'
                                % (self.door, self.door))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("move_to: INTERRUPTED — cancelled", out)
        self.assertIn(f"STOPPED: hostile in view: {self.gob} Goblin; not run: interact {self.door}", out)
        self.assertEqual(len(self.g.ops("job.cancel")), 1)

    def test_ignore_turns_an_interrupt_off(self):
        self.g.watch["seen"] = []

        def hostile(op, args):
            self.g.watch["seen"] = [{"uuid": GOB, "hostile": True}]
        self.g.on_start = hostile
        out = server.do_actions('[{"tool":"interact","target":"%s"},{"tool":"interact","target":"%s"}]'
                                % (self.door, self.door), ignore="hostile")
        self.assertNotIn("STOPPED", out)

    def test_hp_drop_interrupts(self):
        def hurt(op, args):
            self.g.watch["party"] = [{"uuid": P1, "hp": 5, "max_hp": 20}]
        self.g.on_start = hurt
        out = server.do_actions('[{"tool":"interact","target":"%s"},{"tool":"interact","target":"%s"}]'
                                % (self.door, self.door))
        self.assertIn("STOPPED: ", out)
        self.assertIn("HP 5/20", out)

    def test_damage_outside_combat_interrupts(self):
        def burn(op, args):
            self.g.watch["party"] = [{"uuid": P1, "hp": 18, "max_hp": 20, "surface": "Fire"}]
        self.g.on_start = burn
        out = server.do_actions('[{"tool":"interact","target":"%s"},{"tool":"interact","target":"%s"}]'
                                % (self.door, self.door))
        self.assertIn("STOPPED: ", out)
        self.assertIn("standing in Fire", out)

    def test_list_targets_become_a_comma_list(self):
        # ACT-8: {"targets": ["e1","e2"]} in a plan reached targets.split(",") (AttributeError)
        st = actq.parse_steps([{"tool": "use_ability", "ability": "Target_Bless", "targets": ["e1", "e2", " "]}],
                              {"use_ability"})[0]
        self.assertEqual(st.args["targets"], "e1,e2")
        st = actq.parse_steps([{"tool": "use_ability", "ability": "Target_Bless", "targets": "e1,e2"}],
                              {"use_ability"})[0]
        self.assertEqual(st.args["targets"], "e1,e2")

    def test_bad_steps(self):
        self.assertIn("FAILED: step 1: 'save_game' can't be batched", server.do_actions('[{"tool":"save_game"}]'))
        self.assertIn("unknown condition 'drunk'",
                      server.do_actions('[{"tool":"wait","if":"drunk"}]'))

    def test_combat_start_mid_batch(self):
        def fight(op, args):
            self.g.push("combat_started", {})
        self.g.on_start = fight
        out = server.do_actions('[{"tool":"interact","target":"%s"},{"tool":"interact","target":"%s"}]'
                                % (self.door, self.door))
        self.assertIn("STOPPED: combat started", out)


class Queue(Base):
    def test_queue_runs_in_background_and_reports_by_event(self):
        self.g.dur["act.interact"] = 0.2
        t0 = time.monotonic()
        out = server.queue_actions('[{"tool":"interact","target":"%s"},{"tool":"pick_up","item":"%s"}]'
                                   % (self.door, self.door))
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertIn("queued 2 step(s) for", out)
        self.assertFalse(server._batch["active"])  # the worker's batch flag is its own
        out = server.wait_for("queue", timeout_s=3)
        self.assertIn("done (2 steps)", out)
        self.assertNotIn("queue_done", out)  # the wait_for result is the report: no duplicate EVENTS line

    def test_queue_steps_act_for_the_queue_owner(self):
        server.queue_actions('[{"tool":"interact","target":"%s"}]' % self.door, who="Lae'zel")
        self.wait_queue_idle()
        self.assertEqual(self.g.ops("act.interact")[0][1]["char"], P2)

    def test_dialogue_start_flushes_queue_with_reason_and_left(self):
        self.g.dur["act.move_to"] = 2

        def talk(op, args):
            if op == "act.move_to":
                threading.Timer(0.1, lambda: self.g.push("dialogue_started", {})).start()
        self.g.on_start = talk
        server.queue_actions('[{"tool":"move_to","target":"%s"},{"tool":"interact","target":"%s"},'
                             '{"tool":"pick_up","item":"%s"}]' % (self.door, self.door, self.door))
        self.wait_queue_idle()
        note = [a for op, a in self.g.ops("act.note")][-1]
        self.assertEqual(note["kind"], "queue_interrupted")
        self.assertEqual(note["data"]["why"], "a conversation started")
        self.assertEqual(note["data"]["left"], [f"interact {self.door}", f"pick_up {self.door}"])

    def test_party_banter_does_not_interrupt(self):
        # An automated dialogue (companion banter while walking) is not a conversation the player must answer.
        self.g.dur["act.move_to"] = 0.5

        def banter(op, args):
            if op == "act.move_to":
                self.g.push("dialogue_started", {"automated": True})
        self.g.on_start = banter
        server.queue_actions('[{"tool":"move_to","target":"%s"},{"tool":"wait","seconds":0.2}]' % self.door)
        self.wait_queue_idle()
        self.assertEqual(self.g.ops("act.note")[-1][1]["kind"], "queue_done")

    def test_talk_step_does_not_interrupt_itself(self):
        def talk(op, args):
            if op == "act.talk":
                self.g.push("dialogue_started", {})
                self.g.screen = "dialogue"
        self.g.on_start = talk
        server.queue_actions('[{"tool":"talk_to","npc":"%s"},{"tool":"wait","seconds":0.2}]' % self.gob)
        self.wait_queue_idle()
        self.assertEqual(self.g.ops("act.note")[-1][1]["kind"], "queue_done")

    def test_cancel_queue(self):
        self.g.dur["act.move_to"] = 5
        server.queue_actions('[{"tool":"move_to","target":"%s"},{"tool":"interact","target":"%s"}]'
                             % (self.door, self.door))
        time.sleep(0.1)
        out = server.cancel_queue()
        self.assertIn("stopped move_to", out)
        self.assertIn("dropped 1", out)
        self.wait_queue_idle()
        self.assertEqual(self.g.ops("act.note")[-1][1]["data"]["why"], "cancelled")

    def test_appending_while_running_extends_the_same_run(self):
        self.g.dur["act.interact"] = 0.2
        server.queue_actions('[{"tool":"interact","target":"%s"}]' % self.door)
        server.queue_actions('[{"tool":"pick_up","item":"%s"}]' % self.door)
        self.wait_queue_idle()
        self.assertEqual(len(self.g.ops("act.pickup")), 1)

    def test_two_characters_in_parallel(self):
        self.g.dur["act.move_to"] = 0.4
        t0 = time.monotonic()
        server.queue_actions('[{"tool":"move_to","target":"%s"}]' % self.door, who="Claude")
        server.queue_actions('[{"tool":"move_to","target":"%s"}]' % self.door, who="Lae'zel")
        self.wait_queue_idle()
        self.assertLess(time.monotonic() - t0, 0.75)
        self.assertEqual({a["char"] for _, a in self.g.ops("act.move_to")}, {P1, P2})

    def test_do_actions_wait_false_queues(self):
        out = server.do_actions('[{"tool":"interact","target":"%s"}]' % self.door, wait=False)
        self.assertIn("queued 1 step(s)", out)
        self.wait_queue_idle()

    def test_running_text_shows_queue(self):
        self.g.dur["act.move_to"] = 2
        server.queue_actions('[{"tool":"move_to","target":"%s"},{"tool":"interact","target":"%s"}]'
                             % (self.door, self.door))
        time.sleep(0.1)
        out = server.get_events()
        self.assertIn(f"queue {server.H.get(P1)} Claude: move_to {self.door} then interact {self.door}", out)


class Parsing(unittest.TestCase):
    def test_conditions(self):
        st = actq.parse_steps('[{"tool":"wait","if":["in_combat","hp_below:40"],"unless":"target_visible:e3"}]',
                              {"wait"})
        self.assertEqual(st[0].cond_if, [("in_combat", ""), ("hp_below", "40")])
        self.assertEqual(st[0].cond_unless, [("target_visible", "e3")])
        with self.assertRaises(actq.StepError):
            actq.parse_steps('[{"tool":"wait","if":"hp_below"}]', {"wait"})
        with self.assertRaises(actq.StepError):
            actq.parse_steps("[" + ",".join(['{"tool":"wait"}'] * 21) + "]", {"wait"})


class WaitForNoop(Base):
    """wait_for answers at once when the state already answers it or nothing running could cause it (S013:
    3x 4 s on 'roll'/'dialogue_started' after a failed lockpick), and flags those calls 'noop' in metrics."""

    def setUp(self) -> None:
        super().setUp()
        self.recs: list[dict] = []
        orig = server.METRICS.write
        server.METRICS.write = lambda rec: self.recs.append(rec)
        self.addCleanup(setattr, server.METRICS, "write", orig)
        server._noop_waits["n"] = 0

    def timed(self, target: str, timeout_s: float = 5.0) -> tuple[str, float]:
        t0 = time.monotonic()
        out = server.wait_for(target, timeout_s=timeout_s)
        return out, time.monotonic() - t0

    def last_noop(self):
        return [r for r in self.recs if r.get("tool") == "wait_for"][-1].get("noop")

    def test_event_nothing_could_cause_returns_at_once(self):
        for tgt in ("roll", "dialogue_started", "dialogue_options", "job_finished,queue_done"):
            out, dt = self.timed(tgt)
            self.assertLess(dt, 0.4, tgt)
            self.assertIn(f"nothing is running that would cause {tgt}", out)
            self.assertEqual(self.last_noop(), "cannot_happen")

    def test_trivial_targets_return_at_once(self):
        self.g.combat = {"active": False}
        for tgt, want in (("queue", "queue: empty"), ("idle", "idle: no action running"),
                          ("my_turn", "not in combat"), ("dialogue_ended", "no dialogue open"),
                          ("combat_ended", "not in combat"), ("dialogue_started,idle", "idle: no action running")):
            out, dt = self.timed(tgt)
            self.assertLess(dt, 0.4, tgt)
            self.assertIn(want, out, tgt)
            self.assertEqual(self.last_noop(), "already", tgt)

    def test_my_turn_when_it_already_is(self):
        self.g.combat = {"active": True, "current": P1}
        out, dt = self.timed("my_turn")
        self.assertLess(dt, 0.4)
        self.assertIn("'s turn", out)
        self.assertEqual(self.last_noop(), "already")
        out, dt = self.timed("combat_started")
        self.assertIn("already in combat", out)

    def test_finished_job_returns_at_once(self):
        server.move_to(target=self.door)
        time.sleep(0.05)
        out, dt = self.timed("j1")
        self.assertLess(dt, 0.4)
        self.assertIn("move_to: done", out)
        self.assertEqual(self.last_noop(), "job_done")

    def test_prompt_already_on_screen(self):
        self.g.screen = "roll"
        self.g.ui = {"roll": {"skill": "Sleight of Hand", "dc": 15}}
        out, dt = self.timed("roll")
        self.assertLess(dt, 0.4)
        self.assertIn("roll on screen", out)
        self.g.screen = "dialogue"
        self.g.ui = {"dialogue": {"speaker": "Kagha", "text": "Well?", "options": [{"i": 1, "text": "Yes."}]}}
        out, dt = self.timed("dialogue_options")
        self.assertLess(dt, 0.4)
        self.assertIn("dialogue open", out)
        self.assertIn("1. Yes.", out)

    def test_waits_while_a_job_runs_then_a_short_grace(self):
        self.g.dur["act.move_to"] = 0.4
        server.move_to(target=self.door)
        out, dt = self.timed("dialogue_started", timeout_s=5)
        self.assertGreater(dt, 0.4 + server.EVENT_GRACE_S - 0.15)
        self.assertLess(dt, 0.4 + server.EVENT_GRACE_S + 0.6)
        self.assertIn("nothing is running that would cause dialogue_started", out)
        self.assertIsNone(self.last_noop())  # it really waited for the walk

    def test_world_events_keep_the_idle_fallback(self):
        old, server.IDLE_EVENT_WAIT_S = server.IDLE_EVENT_WAIT_S, 0.4
        try:
            out, dt = self.timed("combat_started,dialogue_started", timeout_s=5)  # an ambush needs no job of ours
            self.assertGreater(dt, 0.35)
            self.assertIn("after 0.", out)
        finally:
            server.IDLE_EVENT_WAIT_S = old

    def test_repeated_world_wait_with_only_reads_between_answers_at_once(self):
        old, server.IDLE_EVENT_WAIT_S = server.IDLE_EVENT_WAIT_S, 0.4
        try:
            out, dt = self.timed("combat_started", timeout_s=5)
            self.assertGreater(dt, 0.35)  # the first one still waits for an ambush
            server.get_status()  # a read: nothing could have changed
            out, dt = self.timed("combat_started", timeout_s=5)
            self.assertLess(dt, 0.3)
            self.assertIn("again: only reads ran since", out)
            self.assertEqual(self.last_noop(), "repeat")
            server.move_to(target=self.door)  # an action resets it
            time.sleep(0.05)
            out, dt = self.timed("combat_started", timeout_s=5)
            self.assertNotIn("again", out)
        finally:
            server.IDLE_EVENT_WAIT_S = old

    def test_enemy_turn_keeps_waiting(self):
        self.g.combat = {"active": True, "current": GOB}
        out, dt = self.timed("dialogue_started", timeout_s=0.4)
        self.assertIn("still waiting for dialogue_started", out)

    def test_summary_counts_noops(self):
        from bg3ai import metrics
        self.timed("roll")
        self.timed("idle")
        s = metrics.summarize([r for r in self.recs if r.get("type") == "tool"], [], [])
        self.assertEqual(s["wait_for_noop"]["n"], 2)
        self.assertEqual(s["wait_for_noop"]["by"], {"already": 1, "cannot_happen": 1})


if __name__ == "__main__":
    unittest.main()


class WatcherParty(unittest.TestCase):
    """The S001 interrupts: damage taken outside combat, stepping into a harmful surface."""

    def setUp(self) -> None:
        self.g = FakeGame()
        self.w = actq.Watcher(self.g)

    def party(self, **row) -> None:
        self.g.watch["party"] = [{"uuid": P1, "hp": 20, "max_hp": 20, **row}]

    def test_damage_outside_combat(self):
        self.w.baseline()
        self.party(hp=19)
        kind, why = self.w.check("move_to", None, ui=False)
        self.assertEqual(kind, "damage")
        self.assertIn("HP 19/20", why)

    def test_trivial_damage_outside_combat_does_not_stop_until_it_adds_up(self):
        # S042 22:51:18: loot_area stopped on 2 HP of 104 (Holy Fire) and skipped 8 corpses
        self.g.watch["party"] = [{"uuid": P1, "hp": 104, "max_hp": 104}]
        self.w.baseline()
        for hp in (102, 100, 98, 96):  # 2 HP chips: 1.9 % each
            self.g.watch["party"] = [{"uuid": P1, "hp": hp, "max_hp": 104}]
            self.assertIsNone(self.w.check("loot_corpse", None, ui=False), hp)
        self.g.watch["party"] = [{"uuid": P1, "hp": 94, "max_hp": 104}]  # 9.6 % in all: a burn that keeps ticking
        kind, why = self.w.check("loot_corpse", None, ui=False)
        self.assertEqual(kind, "damage")
        self.assertIn("HP 94/104", why)

    def test_damage_in_combat_is_not_an_interrupt_above_the_threshold(self):
        self.g.watch["combat"] = True
        self.w.baseline()
        self.party(hp=15)
        self.assertIsNone(self.w.check("move_to", None, ui=False))

    def test_hp_threshold_wins_over_damage(self):
        self.w.baseline()
        self.party(hp=3)
        self.assertEqual(self.w.check("move_to", None, ui=False)[0], "hp")

    def test_l7_one_drop_below_the_threshold_still_moves_every_row_on(self):
        """Review L7: _party_check returned at the first row below the threshold, so the other members' baselines were
        not updated that tick (a second, stale hit on the next check)."""
        self.g.watch["party"] = [{"uuid": P1, "hp": 20, "max_hp": 20}, {"uuid": P2, "hp": 20, "max_hp": 20}]
        self.w.baseline()
        self.g.watch["party"] = [{"uuid": P1, "hp": 3, "max_hp": 20}, {"uuid": P2, "hp": 4, "max_hp": 20}]
        kind, why = self.w.check("move_to", None, ui=False)
        self.assertEqual(kind, "hp")
        self.assertIn(P1, why)
        self.assertIsNone(self.w.check("move_to", None, ui=False))  # P2's drop was taken in the same tick

    def test_l7_an_hp_drop_does_not_hide_a_new_hostile_in_the_same_check(self):
        self.w.baseline()
        self.party(hp=3)
        self.g.watch["seen"] = [{"uuid": "99999999-2222-3333-4444-555555555555", "hostile": True}]
        self.assertEqual(self.w.check("move_to", None, ui=False)[0], "hostile")

    def test_stepping_into_a_surface(self):
        self.w.baseline()
        self.party(surface="Acid")
        kind, why = self.w.check("move_to", None, ui=False)
        self.assertEqual(kind, "surface")
        self.assertIn("Acid", why)

    def test_already_in_a_surface_at_the_start_is_not_an_interrupt(self):
        self.party(surface="Fire")
        self.w.baseline()
        self.assertIsNone(self.w.check("move_to", None, ui=False))
        self.party()  # out of it
        self.assertIsNone(self.w.check("move_to", None, ui=False))
        self.party(surface="Fire")  # back in
        self.assertEqual(self.w.check("move_to", None, ui=False)[0], "surface")

    TRAP = "11111111-2222-3333-4444-555555555555"

    def walk(self, *xs):
        for x in xs:
            self.party(pos=[x, 0, 0])
            hit = self.w.check("travel_to", None, ui=False)
            if hit:
                return hit
        return None

    def spot(self, pos):
        self.g.push("spotted", {"kind": "trap", "item": {"uuid": self.TRAP, "name": "Vent"}, "pos": pos})

    def test_trap_spotted_on_the_way_stops_the_walk(self):
        """S021/S025: 'spotted kind=trap' fired during walks and only showed in EVENTS; a player stops."""
        self.party(pos=[0, 0, 0])
        self.w.baseline()
        self.spot([8, 0.2, 1.0])  # 8 m ahead, 1 m to the side
        kind, why = self.walk(1, 2)
        self.assertEqual(kind, "trap")
        self.assertIn("TRAP spotted on the way", why)
        self.assertIn(self.TRAP, why)

    def test_trap_off_the_way_behind_or_on_another_floor_does_not_stop(self):
        self.party(pos=[0, 0, 0])
        self.w.baseline()
        self.spot([-6, 0, 0])  # behind
        self.assertIsNone(self.walk(1, 2, 3))
        self.g.push("spotted", {"kind": "trap", "item": {"uuid": "aaaaaaaa-2222-3333-4444-555555555555"},
                                "pos": [9, 0, 6]})  # 6 m to the side
        self.g.push("spotted", {"kind": "trap", "item": {"uuid": "bbbbbbbb-2222-3333-4444-555555555555"},
                                "pos": [8, 9, 0]})  # a floor above
        self.assertIsNone(self.walk(4, 5))

    def test_trap_close_by_stops_even_when_standing(self):
        self.party(pos=[0, 0, 0])
        self.w.baseline()
        self.spot([1.5, 0, 0])
        self.assertEqual(self.w.check("interact", None, ui=False)[0], "trap")

    def test_trap_stop_once_and_not_for_disarm_or_ignore(self):
        self.party(pos=[0, 0, 0])
        self.w.baseline()
        self.spot([1.0, 0, 0])
        self.assertIsNone(self.w.check("disarm", None, ui=False))
        self.assertEqual(self.w.check("move_to", None, ui=False)[0], "trap")
        self.assertIsNone(self.w.check("move_to", None, ui=False))  # reported once
        self.w = actq.Watcher(self.g, ignore={"trap"})
        self.w.baseline()
        self.spot([1.0, 0, 0])
        self.assertIsNone(self.w.check("move_to", None, ui=False))

    def test_ignore_damage_and_surface(self):
        self.w = actq.Watcher(self.g, ignore={"damage", "surface"})
        self.w.baseline()
        self.party(hp=19, surface="Fire")
        self.assertIsNone(self.w.check("move_to", None, ui=False))

    def test_passive_rolls_and_noise_events_do_not_stop_a_walk(self):
        """Roadside Cliffs 11:5x: passive Perception rolls (Claude, Gale) fired during a travel_to; only an
        active roll on screen (screen=roll) is a decision. The walk's stop there was 'no walkable ground'."""
        self.w.baseline()
        self.g.push("roll_result", {"roller": {"name": "Claude"}, "success": True, "active": False})
        self.g.push("roll_result", {"roller": {"name": "Gale"}, "success": False, "active": False})
        self.g.push("status_applied", {"name": "Moonshield"})
        self.g.push("spotted", {"kind": "trap"})
        self.assertIsNone(self.w.check("travel_to", None, ui=True))
