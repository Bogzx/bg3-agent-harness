"""Offline tests for think-while-acting: async actions, wait_for, the action queue, do_actions conditions,
interrupts and reports. A thread-safe fake game simulates mod jobs in real time.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from typing import Any

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-"))

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
                return {"screen": self.screen}
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

    def test_async_refusal_is_reported_immediately(self):
        self.g.refuse["act.move_to"] = "no walkable path there"
        out = server.move_to(target=self.door)
        self.assertIn("FAILED — no walkable path there", out)
        self.assertEqual(server._async, {})

    def test_wait_for_job(self):
        server.move_to(target=self.door)
        out = server.wait_for("j1", timeout_s=2)
        self.assertIn("move_to: done", out)
        self.assertIn("job_finished", out)  # the mod's notify event is reported once

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
        self.assertIn("queue_done", out)

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

    def test_damage_in_combat_is_not_an_interrupt_above_the_threshold(self):
        self.g.watch["combat"] = True
        self.w.baseline()
        self.party(hp=15)
        self.assertIsNone(self.w.check("move_to", None, ui=False))

    def test_hp_threshold_wins_over_damage(self):
        self.w.baseline()
        self.party(hp=3)
        self.assertEqual(self.w.check("move_to", None, ui=False)[0], "hp")

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

    def test_ignore_damage_and_surface(self):
        self.w = actq.Watcher(self.g, ignore={"damage", "surface"})
        self.w.baseline()
        self.party(hp=19, surface="Fire")
        self.assertIsNone(self.w.check("move_to", None, ui=False))
