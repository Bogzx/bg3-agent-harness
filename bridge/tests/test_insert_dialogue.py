"""insert_item on an object that answers with a conversation the interact's own text doesn't carry yet (live3 run 1,
2026-10-22: the Nautiloid's console e99 opened "Insert the rune into the socket." a moment after the interact;
insert_item said "no Insert Item panel opened ... not every pedestal takes items" twice)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# isort: off
from test_se_tools_world import InsertFake  # sets BG3AI_RUNTIME before importing the server (via test_se_tools)
from bg3ai.game import JobResult
from bg3ai.tools import core, inventory
# isort: on

CONSOLE = "cccccccc-0000-0000-0000-0000000000e9"
RUNE = "cccccccc-0000-0000-0000-0000000000e3"


class ConsoleFake(InsertFake):
    """The console never opens a panel: using it starts a conversation, on screen from the first ui.state on."""

    def __init__(self):
        super().__init__()
        self.inserts = 0

    def rpc(self, op, args=None, context="server"):
        if op == "ui.state":
            options = [{"text": "Insert the rune into the socket.", "i": 1}, {"text": "Leave.", "i": 2}]
            return {"screen": "dialogue", "dialogue": {"speaker": "Narrator", "options": options}}
        if op == "state.inventory":
            return {"items": [{"uuid": RUNE, "name": "Rune Slate", "owner": self.ME}]}
        return super().rpc(op, args, context)

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, **kw):
        if op == "act.interact":
            self.jobs.append((op, dict(args), context))
            return JobResult(status="done", result={})  # used, no panel
        if op == "ui.insert":
            self.inserts += 1
        return super().run_job(op, args, context, wait_s, **kw)


class InsertOpensALateConversation(unittest.TestCase):
    def test_says_a_conversation_opened_instead(self):
        fake = ConsoleFake()
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        core.H.get(InsertFake.ME, "Corwin Thale")
        into = core.H.get(CONSOLE, "Touch")
        item = core.H.get(RUNE, "Rune Slate")
        out = getattr(inventory.insert_item, "fn", inventory.insert_item)(item=item, into=into)
        self.assertIn("using it opened a conversation instead", out)
        self.assertNotIn("not every pedestal takes items", out)
        # the panel check before the interact, then one after it: a conversation on screen ends the poll at once
        self.assertEqual(fake.inserts, 2)


if __name__ == "__main__":
    unittest.main()
