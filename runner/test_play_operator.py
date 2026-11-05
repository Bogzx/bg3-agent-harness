#!/usr/bin/env python3
"""The kickoff of a session with operator messages on, through the real Runner on test_crash.py's fake game and fake
claude (moved from test_crash.py's NewRuns): it states the operator channel (bg3ai.operator.CHANNEL) once, and says
nothing of operator messages without them.

    python3 runner/test_play_operator.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
from bg3ai import operator as bg3op
from bg3harness.runner import state as rstate
from test_crash import NautiloidBase


class OperatorKickoff(NautiloidBase):
    def kickoff_with(self, env: dict[str, str]) -> str:
        """The session's kickoff from play.py run with `env`'s BG3AI_OPERATOR_DIR (the app's legacy_cli sets it for
        a playthrough with [operator] messages; the runner passes it on to the bg3 server)."""
        os.environ["FAKE_CLAUDE_FIRST_S"] = "2"
        self.wire(rstate.Options("op-test", None, 20, "opus", save_owner="Claude"), [])
        with mock.patch.dict(os.environ, env):
            if not env:
                os.environ.pop("BG3AI_OPERATOR_DIR", None)
            self.r.play_session(20, first=True)
        (run,) = self.claude_runs()
        return run[run.index("-p") + 1]

    def test_the_kickoff_states_the_operator_channel_when_the_bg3_server_takes_messages(self):
        # live chat-on S003: without it the agent took the block for a prompt injection
        prompt = self.kickoff_with({"BG3AI_OPERATOR_DIR": str(self.tmp / "sessions" / "op-test")})
        self.assertEqual(prompt.count(bg3op.CHANNEL), 1)

    def test_the_kickoff_says_nothing_of_operator_messages_without_them(self):
        self.assertNotIn("operator messages", self.kickoff_with({}))


if __name__ == "__main__":
    unittest.main()
