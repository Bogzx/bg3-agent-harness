"""Imported by every runner test module before play: the game's profile folders become a temp dir for the process.

play.py reads SAVE_DIR and SE_BG3AI_DIR (steam_paths: $BG3AI_SAVE_DIR / $BG3AI_SE_DIR, else the real Larian profile)
once, at import. A test that reached the run reset with the real ones moved the live game's Script Extender
BG3AI/nav_*.json files away (live5 §0.4). Forced, not setdefault: a shell that exports the real folders must not leak
them into a test. test_play_isolation.RealDirs checks every runner test module imports this before play.
"""

import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="bg3ai-test-profile-"))
SAVE_DIR = ROOT / "saves"
SE_DIR = ROOT / "se"
os.environ["BG3AI_SAVE_DIR"] = str(SAVE_DIR)
os.environ["BG3AI_SE_DIR"] = str(SE_DIR)
os.environ["BG3AI_PROFILE_DIR"] = str(ROOT / "profile")
