"""Offline test: the environment variables the bridge knows are a fixed list.

A bench run's result depends on every switch it ran with, and a run records the ones the harness sets. An
experiment toggle read from the environment that nobody sets or records lets two runs differ silently (15 such
switches were removed 2026-10-22). A new one fails here: make it a module constant (tests patch it), or add it to
the list and have whoever sets it record it.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_env_surface.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "bg3ai"

KNOWN = frozenset(
    [
        "BG3AI_ALWAYS_LOAD",
        "BG3AI_AUTO_SESSION",
        "BG3AI_AUTOSKIP",
        "BG3AI_CC_PACED",
        "BG3AI_COMBAT_WHO_ACTING",
        "BG3AI_COMPACT",
        "BG3AI_COMPACT_OFF",
        "BG3AI_END_HUD_FIRST",
        "BG3AI_FIGHT_HOLD_PATH",
        "BG3AI_FIX_NOTES",
        "BG3AI_JOB_PUSH",
        "BG3AI_KEEPER_HANDOFF_SECTION",
        "BG3AI_KEEPER_MODE",
        "BG3AI_KEEPER_TASK",
        "BG3AI_KEEPER_WRITABLE",
        "BG3AI_MEMORY_DIR",
        "BG3AI_METRICS_PATH",
        "BG3AI_METRICS_SAMPLE_S",
        "BG3AI_MOVIE_SKIP",
        "BG3AI_NEW_GAME_ASYNC",
        "BG3AI_PLAY_DIR",
        "BG3AI_PLAYTHROUGH",
        "BG3AI_PROFILE_DIR",
        "BG3AI_PROGRESS_PATH",
        "BG3AI_PROGRESS_POLL_S",
        "BG3AI_PROMPT_CONDITION",
        "BG3AI_RPC_CONNS",
        "BG3AI_RPC_RECORD",
        "BG3AI_RULES_PATH",
        "BG3AI_RUNTIME",
        "BG3AI_SAVE_DIR",
        "BG3AI_SAVE_GATES",
        "BG3AI_SAVE_OWNER",
        "BG3AI_SAVE_PREFIX",
        "BG3AI_SAVES_AFTER",
        "BG3AI_SAVES_EXCLUDE",
        "BG3AI_SCENARIO_DIFFICULTY",  # a new-game bench run's difficulty (run_host sets and records it)
        "BG3AI_SERVE_RULES",
        "BG3AI_SESSION",
        "BG3AI_SESSION_DEADLINE",
        "BG3AI_SESSION_WRAPUP",
        "BG3AI_SHOTS_DIR",
        "BG3AI_START_SAVE",
        "BG3AI_STOP_GRACE_S",
        "BG3AI_STUCK_MINUTES",
        "BG3AI_TOOLS",
        "BG3AI_TOOLS_OFF",
        "BG3AI_WIKI_CACHE",
        "BG3AI_WIKI_CONTACT",
        "BG3_KNOWLEDGE_DIR",
        "BG3LE_DEBUG_PORT",
    ]
)

NAME = re.compile(r"""["'](BG3AI_[A-Z0-9_]+|BG3LE_[A-Z0-9_]+|BG3_[A-Z0-9_]+)["']""")


def names_in_source() -> set[str]:
    return {m for p in SRC.rglob("*.py") for m in NAME.findall(p.read_text(encoding="utf-8"))}


class EnvSurface(unittest.TestCase):
    def test_the_bridge_knows_only_the_listed_variables(self):
        found = names_in_source()
        self.assertEqual(sorted(found - KNOWN), [], "new env variable(s): see this file's docstring")
        self.assertEqual(sorted(KNOWN - found), [], "listed but no longer in the source: drop them from KNOWN")

    def test_the_scan_sees_reads(self):
        self.assertIn("BG3AI_RUNTIME", names_in_source())
        self.assertEqual(NAME.findall('os.environ.get("BG3AI_X_Y", "1")'), ["BG3AI_X_Y"])


if __name__ == "__main__":
    unittest.main()
