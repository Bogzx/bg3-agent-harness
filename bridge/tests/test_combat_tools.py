"""Offline tests: end_turn waits through enemy turns; manage_inventory use (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)

from bg3ai import server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

PARTY = "11111111-2222-3333-4444-555555555555"
ENEMY = "99999999-2222-3333-4444-555555555555"
POTION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeGame:
    def __init__(self, combat: list[dict], ui: list[dict] | None = None, job: str = "done",
                 error: str = "character is stunned"):
        self.combat = list(combat)  # successive state.combat replies (last one repeats)
        self.ui = list(ui or [{"screen": "game"}])
        self.job = job
        self.error = error
        self.jobs: list[tuple[str, dict]] = []

    def rpc(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        if op == "state.status":
            return {"active": PARTY, "party": [{"uuid": PARTY, "name": "Claude"}]}
        if op == "state.combat":
            return self.combat.pop(0) if len(self.combat) > 1 else self.combat[0]
        if op == "ui.state":
            return self.ui.pop(0) if len(self.ui) > 1 else self.ui[0]
        raise AssertionError(f"unexpected op {op}")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        return self.rpc(op, args, context)

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0) -> JobResult:
        self.jobs.append((op, args))
        if self.job != "done":
            return JobResult(status=self.job, error=self.error)
        return JobResult(status="done", result={"ended": True})

    def drain_events(self, ctx: str) -> list:
        return []


class EndTurn(unittest.TestCase):
    def use(self, fake: FakeGame) -> None:
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        real_sleep = server.time.sleep
        server.time.sleep = lambda s: None  # type: ignore[assignment]
        self.addCleanup(setattr, server.time, "sleep", real_sleep)

    def test_waits_until_party_turn(self):
        self.use(FakeGame([{"active": True, "current": ENEMY}, {"active": True, "current": ENEMY},
                           {"active": True, "current": PARTY}]))
        out = server.end_turn()
        self.assertRegex(out, r"^end_turn \S+ \S+: done")
        self.assertIn("'s turn", out)
        self.assertEqual(server._batch["last"], "done")

    def test_combat_over(self):
        self.use(FakeGame([{"active": True, "current": ENEMY}, {"active": False}]))
        self.assertIn("combat is over", server.end_turn())

    def test_reaction_prompt_stops_waiting(self):
        self.use(FakeGame([{"active": True, "current": ENEMY}],
                          ui=[{"screen": "game"}, {"screen": "reaction", "reaction": {}}]))
        out = server.end_turn()
        self.assertIn("waiting stopped: reaction", out)
        self.assertEqual(server._batch["last"], "interrupted")

    def test_refused_end_turn_does_not_wait(self):
        fake = FakeGame([{"active": True, "current": ENEMY}], job="failed")
        self.use(fake)
        out = server.end_turn()
        self.assertIn("FAILED", out)
        self.assertNotIn("turn\n", out)

    def test_already_ended_is_not_a_failure(self):
        # S024: end_turn on Gale after combat_turn had ended its turn (shared group) -> FAILED x12.
        fake = FakeGame([{"active": True, "current": PARTY, "turn_group": [PARTY]},
                         {"active": True, "current": ENEMY}, {"active": True, "current": PARTY}],
                        job="failed", error="this character already ended its turn (the rest ...)")
        self.use(fake)
        out = server.end_turn()
        self.assertRegex(out, r"^end_turn \S+( \S+)?: nothing to end — its turn already ended")
        self.assertNotIn("FAILED", out)
        self.assertIn("(yours)", out)
        self.assertEqual(server._batch["last"], "done")

    def test_not_its_turn_names_who_acts_and_waits(self):
        fake = FakeGame([{"active": True, "current": ENEMY}, {"active": True, "current": ENEMY},
                         {"active": True, "current": PARTY}], job="failed", error="it is not this character's turn")
        self.use(fake)
        out = server.end_turn()
        self.assertIn("nothing to end — it is not its turn; now", out)
        self.assertIn("'s turn", out.splitlines()[1])
        out = server.end_turn(wait=False)
        self.assertEqual(len(out.split("\n")[0].split("now")), 2)


class ManageInventory(unittest.TestCase):
    def test_use_goes_to_interact(self):
        fake = FakeGame([{"active": False}])
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        out = server.manage_inventory("use", server.H.get(POTION, "Potion of Healing"))
        self.assertIn("manage_inventory: done", out)
        self.assertEqual(fake.jobs, [("act.interact", {"char": PARTY, "target": POTION})])


if __name__ == "__main__":
    unittest.main()
