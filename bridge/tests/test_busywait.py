"""bg3le's busy UI is waited out, not failed at once (busywait.py). live5 20261022-034005: trade() right after the
dialogue opened the trade screen, set_reactions(Gale) and ui.hotbar_slot 2.5 s after a load answered "bg3le: UI busy"
and the calls failed at once (the acceptance's se section aborted on the hotbar one).

  cd bridge && uv run python -m unittest tests/test_busywait.py -v
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import busywait, hotbar, rpc_record
from bg3ai.game import Game, GameError
from bg3ai.tools import core, uitools

BUSY = "bg3le: UI busy: the game's UI is busy (a rest/travel/load transition); retry in a moment"


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Call(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def run_call(self, answers: list):
        it = iter(answers)

        def fn():
            a = next(it)
            if isinstance(a, Exception):
                raise a
            return a

        return busywait.call(fn, self.clock, self.clock.sleep, GameError)

    def test_not_busy_answers_at_once(self):
        self.assertEqual(self.run_call(["ok"]), ("ok", 0.0))
        self.assertEqual(self.clock.sleeps, [])

    def test_busy_is_waited_out_and_the_wait_returned(self):
        res, waited = self.run_call([GameError(BUSY), GameError(BUSY), {"r": 1}])
        self.assertEqual(res, {"r": 1})
        self.assertAlmostEqual(waited, 2 * busywait.STEP_S)
        self.assertEqual(self.clock.sleeps, [busywait.STEP_S] * 2)

    def test_a_slow_answer_is_not_a_wait(self):
        def slow():
            self.clock.t += 0.03  # the call itself takes time (test_leveling: "waited 0.0 s" on every result)
            return "ok"

        self.assertEqual(busywait.call(slow, self.clock, self.clock.sleep, GameError), ("ok", 0.0))

    def test_other_errors_raise_at_once(self):
        with self.assertRaisesRegex(GameError, "no trade screen"):
            self.run_call([GameError("no trade screen is open"), "never"])
        self.assertEqual(self.clock.sleeps, [])

    def test_bounded_then_the_busy_answer_says_how_long(self):
        with self.assertRaises(GameError) as cm:
            self.run_call([GameError(BUSY)] * 100)
        self.assertIn(busywait.BUSY_MARK, str(cm.exception))
        self.assertIn("still busy after waiting", str(cm.exception))
        self.assertLessEqual(sum(self.clock.sleeps), busywait.BUDGET_S)
        self.assertGreater(sum(self.clock.sleeps), busywait.BUDGET_S - 2 * busywait.STEP_S)

    def test_note(self):
        self.assertEqual(busywait.note(0.0), "")
        self.assertIn("waited 0.5 s", busywait.note(0.5))


def scripted(answers: dict[str, list]):
    """An eval that answers each op from its list (the last answer repeats); an error string = an error reply."""
    seen: list[tuple[str, str]] = []

    def ev(expr: str, context: str = "server", timeout: float = 12.0) -> str:
        op = rpc_record.decode(expr)[0][0]["op"]
        seen.append((op, context))
        q = answers.get(op)
        a = (q.pop(0) if len(q) > 1 else q[0]) if q else {}
        if isinstance(a, str):
            return json.dumps({"ok": False, "error": a})
        return json.dumps({"ok": True, "result": a})

    return ev, seen


def bare_game(clock: FakeClock) -> Game:
    g = Game.__new__(Game)
    g._tl = threading.local()
    g.metrics = None
    g._busy_clock = clock
    g._busy_sleep = clock.sleep
    return g


class GameRpc(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.g = bare_game(self.clock)

    def test_client_op_waits_out_busy(self):
        self.g.eval, seen = scripted({"ui.hotbar_slot": [BUSY, BUSY, {"index": 3}]})
        self.assertEqual(self.g.rpc("ui.hotbar_slot", {"spell": "x"}, context="client"), {"index": 3})
        self.assertEqual([op for op, _ in seen], ["ui.hotbar_slot"] * 3)
        self.assertAlmostEqual(self.g.busy_waited(), 2 * busywait.STEP_S)
        self.g.begin_tool()
        self.assertEqual(self.g.busy_waited(), 0.0)

    def test_hotbar_slot_read_after_a_load(self):
        """The acceptance's twostep_dd_probe path: hotbar._rpc -> ui.hotbar_slot 2.5 s after load_game."""
        self.g.eval, _ = scripted({"ui.hotbar_slot": [BUSY, {"index": 7, "deck": "spells"}]})
        self.assertEqual(hotbar._rpc(self.g, "ui.hotbar_slot", {"spell": "Dimension Door"})["index"], 7)

    def test_presses_are_not_asked_twice(self):
        for op in ("ui.dialogue_choose", "ui.key", "ui.press", "ui.command", "ui.waypoints"):
            self.g.eval, seen = scripted({op: [BUSY, {"ok": True}]})
            with self.assertRaisesRegex(GameError, "UI busy"):
                self.g.rpc(op, {}, context="client")
            self.assertEqual(len(seen), 1, op)

    def test_server_context_is_not_retried(self):
        self.g.eval, seen = scripted({"state.status": [BUSY, {}]})
        with self.assertRaises(GameError):
            self.g.rpc("state.status")
        self.assertEqual(len(seen), 1)


class SetReactionsTool(unittest.TestCase):
    """live5 se 45.4 s: set_reactions(Gale) right after a load -> FAILED: bg3le: UI busy (0.3 s)."""

    def test_waits_and_says_so(self):
        clock = FakeClock()
        ev, seen = scripted(
            {
                "ui.selected": [{"uuid": "u1"}],
                "ui.reactions": [
                    BUSY,
                    BUSY,
                    {"char": "u1", "reactions": [{"id": "Shield", "name": "Shield", "mode": "ask"}]},
                ],
                "events.since": [{"latest": 0, "events": []}],
            }
        )
        with (
            mock.patch.object(core.game, "eval", ev),
            mock.patch.object(core.game, "_busy_clock", clock),
            mock.patch.object(core.game, "_busy_sleep", clock.sleep),
            mock.patch.object(uitools.plumbing, "_who", lambda who: "u1"),
        ):
            out = uitools.set_reactions(who="Gale")
        self.assertNotIn("FAILED", out)
        self.assertIn("Shield", out)
        self.assertIn("waited 0.5 s for the game's UI", out)
        self.assertEqual([op for op, _ in seen].count("ui.reactions"), 3)


if __name__ == "__main__":
    unittest.main()
