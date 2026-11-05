"""Offline tests: the CHANGES read after a job carries the result's EVENTS drain in the same evals (latency survey
2026-11-04 §5.3: the events.since round trip ran after the CHANGES read, one more game tick per action result).
Against a fake bg3le with the real tick semantics (fake_bg3le.py) and the real Game.

Run: cd bridge && uv run python -m unittest tests.test_changes_one_eval
"""

from __future__ import annotations

import json
import time
import unittest

from fake_bg3le import FakeBg3le
from test_turn_macros import Base  # sets BG3AI_RUNTIME before importing the server

# isort: split
from bg3ai import delta
from bg3ai.game import Game
from bg3ai.tools import batch, core, macro_tools, plumbing

P1 = "11111111-2222-3333-4444-555555555555"
GOB = "22222222-2222-3333-4444-555555555555"
REAL_CHANGES_TEXT = delta.changes_text


class OneEval(Base):
    def setUp(self) -> None:
        super().setUp()
        self.pos = [0.0, 0.0, 0.0]
        f = FakeBg3le(server_tick_s=0.005, client_tick_s=0.005)
        f.op(
            "state.status",
            lambda a, c: {
                "party": [{"uuid": P1, "name": "Claude", "hp": 10, "max_hp": 10, "pos": list(self.pos)}],
                "active": P1,
                "in_combat": True,
            },
        )
        f.op(
            "state.nearby",
            lambda a, c: {
                "characters": [
                    {"uuid": GOB, "name": "Goblin", "pos": [5, 0, 5], "relation": "hostile", "hp": 7, "max_hp": 7}
                ],
                "items": [],
            },
        )
        f.op("nav.markers", lambda a, c: {"markers": []})
        f.op("nav.hazards", lambda a, c: {"hazards": []})
        f.op("nav.frontier", lambda a, c: {"leads": []})
        f.op("ui.state", lambda a, c: {"screen": "game"})
        f.op("act.watch", lambda a, c: {"combat": True, "party": []})
        self.codes: list[str] = []
        run = f._run

        def logged(code, ctx):
            self.codes.append(code)
            return run(code, ctx)

        f._run = logged
        self.fake = f.start()
        self.addCleanup(f.stop)
        self.game = Game(port=f.port)
        self.addCleanup(self.game.close)
        self.game.sync_events()
        saved = core.game
        core.game = self.game
        self.addCleanup(setattr, core, "game", saved)
        delta.changes_text = REAL_CHANGES_TEXT  # Base stubs it out (no_changes) and restores it

    def emit(self):
        self.fake.emit("server", "died", uuid=GOB, name="Goblin")
        self.fake.emit("client", "dialogue_started", speaker="Goblin")

    def drains(self) -> int:
        """Evals that only drained events (a separate events.since round trip)."""
        return sum(1 for c in self.codes if '"op":"events.since"' in c and c.count('{"op":') == 1)

    def one_action(self, events: bool) -> tuple[str, str, int]:
        """A CHANGES read then the result's EVENTS drain, as _act -> _finish do: (changes, events text, evals)."""
        parts = (*delta.ACTION_PARTS, "hazards")
        tr = delta.Tracker()
        tr.bind(core.H)
        tr.observe(delta.snapshot_of(core.H, delta.fetch(core.game, parts)))  # the agent's last look
        self.codes.clear()
        self.game.begin_tool()
        self.pos[0] += 4.0  # the party moved: a CHANGES line
        self.emit()
        changes = delta.changes_text(core.game, core.H, parts, tracker=tr, events=events)
        ev = core._events_text()
        return changes, ev, len(self.codes)

    def test_same_changes_and_events_in_fewer_evals(self):
        old = self.one_action(events=False)
        new = self.one_action(events=True)
        self.assertEqual(new[:2], old[:2])
        self.assertEqual(old[1], "EVENTS\n  died\nEVENTS\n  dialogue_started")
        self.assertTrue(old[0].startswith("CHANGES\n"), old[0])
        self.assertEqual((old[2], new[2]), (4, 2))  # server + client reads, then server + client drains

    def test_combat_changes_drain_rides_in_the_read(self):
        self.emit()
        self.game.begin_tool()
        self.codes.clear()
        plumbing._combat_changes("use_ability")
        ev = core._events_text()
        self.assertEqual(self.drains(), 0, self.codes)
        self.assertEqual(ev, "EVENTS\n  died\nEVENTS\n  dialogue_started")  # both contexts, once

    def test_walk_end_drain_rides_in_the_read(self):
        self.emit()
        self.game.begin_tool()
        self.codes.clear()
        batch.walk_end_tail()
        ev = core._events_text()
        self.assertEqual(self.drains(), 0, json.dumps(self.codes)[:400])
        self.assertEqual(ev, "EVENTS\n  died\nEVENTS\n  dialogue_started")  # both contexts, once

    def test_a_macro_result_drain_rides_in_the_read(self):
        self.emit()
        self.game.begin_tool()
        self.codes.clear()
        out = macro_tools._macro_finish("regroup", {}, ["regroup: done"], None, [], time.monotonic())
        self.assertEqual(self.drains(), 0, json.dumps(self.codes)[:400])
        self.assertIn("EVENTS\n  died\nEVENTS\n  dialogue_started", out)


if __name__ == "__main__":
    unittest.main()
