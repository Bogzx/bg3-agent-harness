"""Offline: combat_turn's serial round trips (impl-act "combat_turn serial RPCs", ~6-8 s/run live).

Each fight shape of test_turn_macros is played twice on a fake game that can read() / multi() like game.Game:
once with actq.BATCH_CHECKS off (the old path: one RPC per read) and once on (the watcher's check after a step
reads events + act.watch + the next step check's state.combat in one round trip, a read nobody can have changed
since is reused). Decisions (every act.* order and its args) and the tool's text must be identical; the round
trips counted on the tool's own thread (not the interrupt monitor's, which overlaps the job waits) must drop.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_turn_rpcs.py'
"""

from __future__ import annotations

import json
import threading
import time
import unittest

import test_turn_macros as T
from bg3ai import actq, metrics, server
from bg3ai.game import GameError

P1, P2, GOB, IMP = T.P1, T.P2, T.GOB, T.IMP


class TripGame(T.FakeGame):
    """FakeGame plus Game's read()/multi(): one round trip each, whatever they hold. trips counts the round
    trips made on the tool's own (main) thread: the serial ones a combat_turn waits for (the actq monitor's polls
    overlap the job waits; a background end's queue worker runs after the return)."""

    def __init__(self) -> None:
        super().__init__()
        self.trips: list[str] = []

    def _trip(self, what: str) -> None:
        if threading.current_thread() is threading.main_thread():
            with self.lock:
                self.trips.append(what)

    def rpc(self, op, args=None, context="server", timeout=12.0):
        if not getattr(self._in, "batch", False):
            self._trip(op)
        return super().rpc(op, args, context, timeout)

    _in = threading.local()

    def _ops(self, calls, context):
        self._in.batch = True
        try:
            return [self.try_rpc(op, a, context) for op, a in calls]
        finally:
            self._in.batch = False

    def multi(self, calls, context="server", events=False, timeout=12.0):
        self._trip("+".join(op for op, _ in calls))
        return self._ops(calls, context)

    def read(self, server=(), client=(), events=True, soft=("client",), strict=False):
        self._trip("+".join(op for op, _ in list(server) + [(o + "@c", a) for o, a in client]))
        return self._ops(server, "server"), self._ops(client, "client")


def _decisions(g: TripGame) -> list:
    """Every order the turn gave the game (job ops), with its args."""
    return [(op, json.dumps(a, sort_keys=True)) for op, a in g.calls
            if (op.startswith("act.") and op not in T.NOT_JOBS) or op in T.UI_JOBS]


def checks(trips: list) -> list:
    """The round trips that are not an order or its job poll: the reads between steps."""
    return [t for t in trips if t != "job.poll" and not (t.startswith("act.") and t not in T.NOT_JOBS)]


def play(batch: bool, shape) -> tuple[str, list, list]:
    """(combat_turn text, decisions, round trips) of one fight shape with BATCH_CHECKS on/off."""
    saved = actq.BATCH_CHECKS
    actq.BATCH_CHECKS = batch
    class Run(T.CombatTurn):  # a harness, not a test (in here: the loader would collect CombatTurn's tests)
        def runTest(self) -> None:
            pass
    r = Run()
    r.setUp()
    try:
        g = TripGame()
        g.combat, g.watch["combat"], g.on_start = r.g.combat, True, r.g.on_start
        r.g = g
        server.game = server._watch_game = g
        r.g.on_start = lambda op, a: g.combat.update(current=GOB) if op == "act.end_turn" else None
        out = shape(r)
        time.sleep(0.05)  # a background end's worker
        return out, _decisions(g), list(g.trips)
    finally:
        r.doCleanups()
        actq.BATCH_CHECKS = saved


def whole_turn(r) -> str:
    return server.combat_turn(r.plan(move=r.gob, action={"ability": "Target_MainHandAttack", "target": r.gob},
                                     bonus={"ability": "Shout_SecondWind"}))


def ranged_fallback(r) -> str:
    r.g.refuse_if = lambda op, a: ("target is out of range (6.0 m, range 1.5 m): move closer first"
                                   if a.get("spell") == "Target_MainHandAttack" else None)
    return server.combat_turn(r.plan(action=[{"ability": "Target_MainHandAttack", "target": r.gob},
                                             {"ability": "Projectile_ThrowWeapon", "target": r.gob}]))


def dies_mid_turn(r) -> str:
    # the action kills the goblin: the extra attack's check must see it dead (the read after the step)
    next(o for o in r.g.combat["order"] if o["uuid"] == IMP)["from"][P1] = [1.2, 1]
    r.g.actions = [{"spell": "Target_MainHandAttack", "range": 1.5}]
    base = r.g.result_for

    def result_for(op, a):
        if op == "act.cast":
            r.g.dead.add(GOB)
        return base(op, a)
    r.g.result_for = result_for
    hit = {"ability": "Target_MainHandAttack", "target": r.gob}
    return server.combat_turn(r.plan(action=hit, extra=[[dict(hit)]]))


def dead_alternative(r) -> str:
    r.g.dead.add(GOB)
    return server.combat_turn(r.plan(action=[{"ability": "Target_MainHandAttack", "target": r.gob},
                                             {"ability": "Target_MainHandAttack", "target": r.imp}]))


def background_end(r) -> str:
    server.END_ASYNC = True
    return server.combat_turn(r.plan(action={"ability": "Target_MainHandAttack", "target": r.gob}))


def approach_short(r) -> str:
    # live window 2026-10-16 (Shadowheart, Gale): a move {target, within 1.5} along the route stops short, out of
    # movement; the "now 9.9m from" distance before and after the walk read state.combat on their own (+2 per turn)
    r.g.combat = T.combat(gob_dist=16.0)
    r.g.approach = {"reachable": True, "pos": [-4.0, 0.0, 5.0], "walk_m": 8.8, "need_m": 18.5,
                    "route_m": 20.0, "limit": "movement", "movement_left": 9.0, "end_m": 9.9}

    def advance(op, a):
        if op == "act.move_to":
            next(o for o in r.g.combat["order"] if o["uuid"] == GOB)["from"][P1] = [9.9, 1]
        if op == "act.end_turn":
            r.g.combat["current"] = GOB
    r.g.on_start = advance
    r.g.refuse_if = lambda op, a: ("target is out of range (9.9 m, range 1.5 m): move closer first"
                                   if a.get("spell") == "Target_MainHandAttack" else None)
    r.g.actions = [{"spell": "Target_MainHandAttack"}, {"spell": "Projectile_MainHandAttack", "range": 18}]
    return server.combat_turn(r.plan(move={"target": r.gob, "within": 1.5},
                                     action={"ability": "Target_MainHandAttack", "target": r.gob}))


SHAPES = {"whole_turn": whole_turn, "ranged_fallback": ranged_fallback, "dies_mid_turn": dies_mid_turn,
          "dead_alternative": dead_alternative, "background_end": background_end, "approach_short": approach_short}


class SerialRoundTrips(unittest.TestCase):
    def test_same_decisions_and_text_fewer_round_trips(self):
        report = []
        for name, shape in SHAPES.items():
            with self.subTest(shape=name):
                out0, dec0, trips0 = play(False, shape)
                out1, dec1, trips1 = play(True, shape)
                self.assertEqual(dec1, dec0)
                self.assertEqual(out1, out0)
                self.assertTrue(dec0, "the shape gave no order")
                c0, c1 = checks(trips0), checks(trips1)
                self.assertLess(len(c1), len(c0) * 0.5, f"{name}: {c0} → {c1}")
                self.assertLess(len(trips1), len(trips0))
                report.append(f"{name} {len(c0)}→{len(c1)} ({len(trips0)}→{len(trips1)} with the orders + polls)")
        print("\ncombat_turn serial reads between steps (off→on): " + ", ".join(report))

    def test_dies_mid_turn_reaims_from_the_read_after_the_step(self):
        out, dec, _ = play(True, dies_mid_turn)
        self.assertIn("already dead → aimed at", out)
        self.assertEqual([json.loads(a)["target"] for op, a in dec if op == "act.cast"], [GOB, IMP])

    def test_step_checks_ride_on_the_watcher_read(self):
        _, _, trips = play(True, whole_turn)
        # the check after each of the 3 steps carries the next step check's state.combat; none on its own
        self.assertEqual(sum("events.since+act.watch+state.combat" in t for t in trips), 3, trips)
        self.assertEqual([t for t in trips if t == "state.combat"], ["state.combat", "state.combat"], trips)
        # (left: end_turn's own read, the NEXT line's read after the end — both after the game moved on)

    def test_move_distance_lines_reuse_the_kept_read(self):
        """Live window 2026-10-16: single state.combat 4 > 3 (Shadowheart) and 6 > 5 (Gale): dist_now() read it
        before and after each move with a target. Now the start read / the read after the move step carry it."""
        out, _, trips = play(True, approach_short)
        self.assertIn("now 9.9m from", out)
        self.assertIn("melee is out of reach this turn", out)
        self.assertEqual([t for t in trips if t == "state.combat"], ["state.combat", "state.combat"], trips)

    def test_a_stale_kept_read_is_read_again(self):
        server._cs_keep({"active": True, "order": []})
        server._turn_cs["t"] -= server.TURN_CS_MAX_S + 0.1
        saved = actq.BATCH_CHECKS
        actq.BATCH_CHECKS = True
        try:
            self.assertIsNone(server._cs_cached())
        finally:
            actq.BATCH_CHECKS = saved
            server._turn_cs.clear()

    def test_a_failed_read_falls_back_like_try_rpc(self):
        class Broken(TripGame):
            def read(self, *a, **kw):
                raise GameError("tick lost")
        w = actq.Watcher(Broken())
        saved = actq.BATCH_CHECKS
        actq.BATCH_CHECKS = True
        try:
            w.baseline()
            self.assertEqual(w.start, {})
            self.assertIsNone(w.check("move_to", None, ui=False))
        finally:
            actq.BATCH_CHECKS = saved


class RoundTripMetric(unittest.TestCase):
    def test_round_trips_merge_parallel_evals(self):
        self.assertEqual(metrics.round_trips([(0, 1), (0.1, 0.9), (1, 2), (3, 4), (3.5, 5)]), 3)
        self.assertEqual(metrics.round_trips([]), 0)

    def test_tool_record_counts_both_connections(self):
        rec = metrics.Recorder(None)
        now = time.time()
        rec.rpc_done("state.combat", now - 0.30, now - 0.28)
        w = rec.watch_view()
        w.rpc_done("events.since+act.watch", now - 0.20, now - 0.18)
        w.rpc_done("events.since", now - 0.20, now - 0.185)  # the client half of the same read
        out = rec.tool_done("combat_turn", 400.0, t1=now)
        self.assertEqual((out["rpc_n"], out["rt_n"], out["watch_n"]), (1, 2, 2))
        other = threading.Thread(target=lambda: w.rpc_done("act.watch", now - 0.1, now - 0.09))
        other.start()
        other.join()  # the monitor thread's polls are not the tool's
        self.assertEqual(rec.tool_done("combat_turn", 400.0, t1=now)["watch_n"], 2)

    def test_session_summary_has_combat_turn_round_trips(self):
        t = 1000.0
        tools = [{"type": "tool", "tool": "combat_turn", "t0": t, "t1": t + 5, "ms": 5000, "rt_n": n}
                 for n in (10, 20, 30)] + [{"type": "tool", "tool": "combat_turn", "t0": t, "t1": t + 1, "ms": 900},
                                           {"type": "tool", "tool": "look", "t0": t, "t1": t + 1, "ms": 90, "rt_n": 2}]
        s = metrics.summarize(tools, [], [], start=t, end=t + 60)
        self.assertEqual((s["combat_turn_rt"]["n"], s["combat_turn_rt"]["median"]), (3, 20))


if __name__ == "__main__":
    unittest.main()
