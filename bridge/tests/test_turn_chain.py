"""combat_turn list form: several party members' turns in one call (round-trip fold, 2026-10-10)."""
import json
import unittest
from unittest import mock

from bg3ai import server
from bg3ai.game import GameError

TC = getattr(server.combat_turn, "fn", server.combat_turn)


class Chain(unittest.TestCase):
    def run_chain(self, plans, one):
        calls = []

        def fake(plan, who, end, wait_next, ignore, wait_turn, **kw):
            calls.append((who, json.loads(plan), wait_turn))
            server._batch["last"] = "done"
            return one(who) + "\n⏱ 12:00 · 40m left"
        with mock.patch.object(server, "_combat_turn_one", fake):
            out = TC(json.dumps(plans))
        return out, calls

    PLANS = [{"who": "Karlach", "plan": {"action": [{"ability": "Attack", "target": "e7"}]}},
             {"who": "Gale", "plan": {"action": [{"ability": "Fire Bolt", "target": "e7"}]}},
             {"who": "Claude", "plan": {}}]

    def test_all_played_one_clock_line(self):
        out, calls = self.run_chain(self.PLANS, lambda w: f"TURN {w}\nend_turn: done")
        self.assertEqual([c[0] for c in calls], ["Karlach", "Gale", "Claude"])
        self.assertEqual(calls[0][2], 30.0)
        self.assertTrue(all(0 < c[2] <= 30.0 for c in calls[1:]))   # later members wait too, within the budget
        self.assertEqual(out.count("⏱"), 1)
        self.assertNotIn("CHAIN STOPPED", out)

    def test_budget_spent_no_more_waiting(self):
        with mock.patch.object(server, "CHAIN_BUDGET_S", 0.0):
            _, calls = self.run_chain(self.PLANS, lambda w: f"TURN {w}")
        self.assertEqual([c[2] for c in calls], [30.0, 0.0, 0.0])

    def test_stops_when_a_member_cant_act_now(self):
        def one(w):
            if w == "Gale":
                raise GameError("it is not e2 Gale's turn (now e9 Goblin)")
            return f"TURN {w}"
        out, calls = self.run_chain(self.PLANS, one)
        self.assertEqual(len(calls), 2)
        self.assertIn("CHAIN STOPPED after 1 of 3: Gale: not played — it is not e2 Gale's turn", out)
        self.assertIn("NOT PLAYED: Gale, Claude", out)

    def test_member_that_already_ended_is_skipped(self):
        # S028: [Karlach, Claude, ...] with Karlach done → "CHAIN STOPPED after 0 of 4"; now the rest play
        def one(w):
            if w == "Karlach":
                raise GameError("e2 Karlach already ended its turn (now e4 Claude); waited 0.0s for the turn")
            return f"TURN {w}"
        out, calls = self.run_chain(self.PLANS, one)
        self.assertEqual([c[0] for c in calls], ["Karlach", "Gale", "Claude"])
        self.assertIn("Karlach: skipped — e2 Karlach already ended its turn", out)
        self.assertNotIn("CHAIN STOPPED", out)
        self.assertIn("TURN Gale", out)

    def test_later_member_that_must_act_first_plays_first(self):
        order = []

        def one(w):
            if w == "Karlach" and "Claude" not in order:
                raise GameError("it is not e1 Karlach's turn (now e4 Claude); waited 0.0s for the turn (e4 Claude "
                                "(yours) must act first: play that turn (combat_turn who=e4) or end it (end_turn who=e4))")
            order.append(w)
            return f"TURN {w}"
        uu = {"e4": "U-claude", "Claude": "U-claude", "Gale": "U-gale", "Karlach": "U-karlach"}
        with mock.patch.object(server, "_uuid", lambda h: uu[h]):
            out, calls = self.run_chain(self.PLANS, one)
        self.assertEqual(order, ["Claude", "Karlach", "Gale"])
        self.assertNotIn("CHAIN STOPPED", out)

    def test_stops_on_interrupt(self):
        def fake(plan, who, end, wait_next, ignore, wait_turn, **kw):
            server._batch["last"] = "interrupted" if who == "Karlach" else "done"
            return f"TURN {who}\nturn NOT ended"
        with mock.patch.object(server, "_combat_turn_one", fake):
            out = TC(json.dumps(self.PLANS))
        self.assertIn("Karlach's turn was interrupted", out)
        self.assertIn("NOT PLAYED: Gale, Claude", out)

    def test_s080_an_earlier_calls_interrupt_does_not_stop_the_chain(self):
        # S080 23:23:12: [Wyll, Gale], Wyll downed ("nothing to play", no step ran): "CHAIN STOPPED after 1 of 2: Wyll's
        # turn was interrupted (above)" on the flag an earlier call had left; Gale not played
        def fake(plan, who, end, wait_next, ignore, wait_turn, **kw):
            return f"{who} is downed: nothing to play" if who == "Karlach" else f"TURN {who}"
        server._batch["last"] = "interrupted"
        with mock.patch.object(server, "_combat_turn_one", fake):
            out = TC(json.dumps(self.PLANS))
        self.assertNotIn("CHAIN STOPPED", out)
        self.assertIn("TURN Gale", out)

    def test_single_plan_unchanged(self):
        with mock.patch.object(server, "_combat_turn_one", return_value="TURN x") as m:
            self.assertEqual(TC('{"action":[]}', who="Gale"), "TURN x")
        m.assert_called_once()

    def test_bad_list(self):
        self.assertIn("a list plan is", TC('[{"plan": {}}]'))
        self.assertIn("ends each turn", TC(json.dumps(self.PLANS), end=False))


if __name__ == "__main__":
    unittest.main()


class AutoCombat(unittest.TestCase):
    def test_fight_start_carries_the_combat_state(self):
        with mock.patch.object(server, "_combat_state_text", return_value="ROUND 1 order: e1 Karlach, e9 Goblin\nYOUR ACTIONS (e1)"):
            out = server._auto_combat("move_to", "move_to: done", "EVENTS\n  combat_started {}")
            self.assertTrue(out.startswith("COMBAT STARTED (no get_combat_state needed):"))
            self.assertIn("YOUR ACTIONS (e1)", out)
            self.assertEqual(server._auto_combat("combat_turn", "TURN e1", "EVENTS\n  combat_started {}"), "")
            self.assertEqual(server._auto_combat("move_to", "move_to: done", "EVENTS\n  died {}"), "")
            self.assertEqual(server._auto_combat("move_to", "x\nYOUR ACTIONS (e1)", "  combat_started"), "")


class WaitTurn(unittest.TestCase):
    """S031 17:04: a sent-ahead plan timed out after 30 s in the middle of a long enemy phase and was re-sent."""

    def run_wait(self, states, max_s, extend_s, step=1.0):
        clock = [0.0]
        seq = iter(states)
        last = [None]

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                if op == "state.combat":
                    last[0] = next(seq, last[0])
                    return last[0]
                return {}

        with mock.patch.object(server, "game", G()), \
                mock.patch.object(server.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(server.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + step)):
            cs, why = server._wait_turn(lambda c: c.get("current") == "me", max_s, None, None, extend_s)
        return cs, why, clock[0]

    def enemy(self, n):
        return {"active": True, "round": 3, "current": f"enemy{n}"}

    def test_progressing_enemies_extend_the_wait(self):
        # a new enemy every 5 s for 40 s, then our turn: max 30 + extend 30 → played, no time out
        states = [self.enemy(i // 5) for i in range(40)] + [{"active": True, "round": 3, "current": "me"}]
        cs, why, t = self.run_wait(states, 30.0, 30.0)
        self.assertEqual(why, "")
        self.assertEqual(cs["current"], "me")

    def test_stalled_enemy_times_out_at_max(self):
        states = [self.enemy(0)] * 100
        _, why, t = self.run_wait(states, 30.0, 30.0)
        self.assertEqual(why, "timed out")
        self.assertLess(t, 32.0)

    def test_no_extension_without_extend_s(self):
        states = [self.enemy(i // 5) for i in range(100)]
        _, why, t = self.run_wait(states, 30.0, 0.0)
        self.assertEqual(why, "timed out")
        self.assertLess(t, 32.0)

    def test_extension_is_capped(self):
        # S080 02:31:45: still progressing at the cap = "enemies still acting", not a bare "timed out"
        states = [self.enemy(i // 5) for i in range(200)]
        _, why, t = self.run_wait(states, 30.0, 30.0)
        self.assertTrue(why.startswith(server.TURN_STILL_ACTING), why)
        self.assertIn("turn changes in the wait", why)
        self.assertLess(t, 62.0)

    def test_hidden_enemy_hits_count_as_progress(self):
        # S080: an enemy acting in Darkness is off the visible order (no current): only the party's HP moves.
        # 80 s of that, then our turn: the default bound (30 + TURN_EXTEND_S) waits it out.
        def hidden(i):
            return {"active": True, "round": 3,
                    "order": [{"uuid": "me", "hp": 100 - i // 6, "relation": "party"}]}
        states = [hidden(i) for i in range(80)] + [{"active": True, "round": 3, "current": "me"}]
        cs, why, t = self.run_wait(states, 30.0, server.TURN_EXTEND_S)
        self.assertEqual(why, "")
        self.assertEqual(cs.get("current"), "me")
        self.assertGreater(t, 60.0)

