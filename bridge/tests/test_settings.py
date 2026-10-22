"""Offline tests: settings.from_env reads the bridge's settings from a mapping, and the server's Game gets them.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_settings.py
"""

from __future__ import annotations

import unittest
from pathlib import Path

from bg3ai import settings
from bg3ai.game import Game
from bg3ai.tools import core

REPO, HOME = Path("/r"), Path("/h")


def make(**env: str) -> settings.Settings:
    return settings.from_env(env, repo=REPO, home=HOME)


class FromEnv(unittest.TestCase):
    def test_defaults_under_the_repo_and_home(self):
        s = make()
        self.assertEqual(s.runtime, REPO / "runtime")
        self.assertEqual(
            (s.play_dir, s.memory, s.knowledge), (REPO / "play", REPO / "play/memory", REPO / "play/knowledge")
        )
        self.assertEqual(
            s.save_dir, HOME / ".local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"
        )
        self.assertEqual((s.deadline, s.wrapup_at, s.progress_path, s.hold_path), (0.0, 0.0, "", ""))
        self.assertEqual((s.port, s.rpc_conns, s.job_push), (9998, 4, True))

    def test_env_overrides(self):
        s = make(
            BG3AI_RUNTIME="/rt",
            BG3AI_PLAY_DIR="/p",
            BG3AI_PROFILE_DIR="/prof",
            BG3AI_SESSION_DEADLINE="100.5",
            BG3AI_SESSION_WRAPUP="90",
            BG3AI_FIGHT_HOLD_PATH="/rt/hold.json",
            BG3LE_DEBUG_PORT="5000",
            BG3AI_RPC_CONNS="1",
            BG3AI_JOB_PUSH="0",
        )
        self.assertEqual((s.runtime, s.memory, s.knowledge), (Path("/rt"), Path("/p/memory"), Path("/p/knowledge")))
        self.assertEqual(s.save_dir, Path("/prof/PlayerProfiles/Public/Savegames/Story"))
        self.assertEqual((s.deadline, s.wrapup_at, s.hold_path), (100.5, 90.0, "/rt/hold.json"))
        self.assertEqual((s.port, s.rpc_conns, s.job_push), (5000, 1, False))
        s = make(BG3AI_MEMORY_DIR="/m", BG3_KNOWLEDGE_DIR="/k", BG3AI_SAVE_DIR="/s")
        self.assertEqual((s.memory, s.knowledge, s.save_dir), (Path("/m"), Path("/k"), Path("/s")))

    def test_progress_path_next_to_a_session_metrics_file(self):
        self.assertEqual(make(BG3AI_METRICS_PATH="/l/S003.metrics.jsonl").progress_path, "/l/S003.progress.jsonl")
        self.assertEqual(make(BG3AI_METRICS_PATH="/l/metrics.json").progress_path, "")
        self.assertEqual(make(BG3AI_METRICS_PATH="/l/S003.metrics.jsonl", BG3AI_PROGRESS_PATH="/x").progress_path, "/x")

    def test_empty_and_bad_values(self):
        s = make(BG3AI_RUNTIME="", BG3AI_SESSION_WRAPUP="soon", BG3AI_RPC_CONNS="0")
        self.assertEqual((s.runtime, s.wrapup_at, s.rpc_conns), (REPO / "runtime", 0.0, 1))

    def test_save_identity(self):
        s = make().saves
        self.assertEqual(
            (s.owner, s.prefix, s.playthrough, s.gates, s.after, s.start_save), ("", "", "", None, 0.0, "")
        )
        self.assertEqual(s.exclude, frozenset())
        s = make(
            BG3AI_SAVE_OWNER="Ilse",
            BG3AI_PLAYTHROUGH="run-2",
            BG3AI_SAVE_GATES="0",
            BG3AI_SAVES_AFTER="1760000000.5",
            BG3AI_START_SAVE="Ilse-1__SCN-grove",
            BG3AI_SAVES_EXCLUDE="Ilse-1__S004-end\n\nIlse-2__x",
        ).saves
        self.assertEqual((s.owner, s.prefix, s.playthrough, s.gates), ("Ilse", "Ilse-", "run-2", "0"))
        self.assertEqual((s.after, s.start_save), (1760000000.5, "Ilse-1__SCN-grove"))
        self.assertEqual(s.exclude, frozenset({"Ilse-1__S004-end", "Ilse-2__x"}))
        self.assertEqual(make(BG3AI_SAVES_AFTER="soon").saves.after, 0.0)

    def test_repo_default_is_the_checkout(self):
        self.assertTrue((settings.REPO / "bridge" / "src" / "bg3ai" / "settings.py").is_file())


class ServerGame(unittest.TestCase):
    def test_core_game_uses_the_settings(self):
        s = core.SETTINGS
        self.assertEqual((core.game.port, core.game.pool_cap, core.game.job_push), (s.port, s.rpc_conns, s.job_push))
        self.assertEqual((core.RUNTIME, core.DEADLINE, core.WRAPUP_AT), (s.runtime, s.deadline, s.wrapup_at))
        from bg3ai.tools import saving

        self.assertEqual((saving.SAVE_PREFIX, saving.SAVES_EXCLUDE), (s.saves.prefix, s.saves.exclude))

    def test_game_defaults_read_no_env(self):
        g = Game(pool_max=2, job_push=False)
        self.assertEqual((g.pool_max, g.pool_cap, g.job_push), (2, 2, False))
        self.assertEqual((Game().pool_cap, Game().job_push), (4, True))


if __name__ == "__main__":
    unittest.main()
