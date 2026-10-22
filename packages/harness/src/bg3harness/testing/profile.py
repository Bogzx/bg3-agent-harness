"""A temp Larian profile for a test process, set before anything imports bg3harness.runner.env.

bg3harness.runner.env reads SAVE_DIR and SE_BG3AI_DIR (steam_paths: $BG3AI_SAVE_DIR / $BG3AI_SE_DIR, else the real
profile) once, at import: a test process that imports it first with the real ones runs every later test against
the live game's saves and Script Extender files (live5 §0.4). runner/testenv.py and packages/harness/tests/conftest.py
both call temp_profile(), so whichever runs first picks the dir and the other reuses it ($BG3AI_TEST_PROFILE).
Forced, not setdefault: a shell that exports the real folders must not leak them into a test.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path

ENV = "BG3AI_TEST_PROFILE"


def temp_profile(environ: MutableMapping[str, str] = os.environ) -> Path:
    """The process's temp profile root (made on the first call); points the three profile env vars into it."""
    root = Path(environ.get(ENV) or tempfile.mkdtemp(prefix="bg3ai-test-profile-"))
    environ[ENV] = str(root)
    environ["BG3AI_SAVE_DIR"] = str(root / "saves")
    environ["BG3AI_SE_DIR"] = str(root / "se")
    environ["BG3AI_PROFILE_DIR"] = str(root / "profile")
    return root
