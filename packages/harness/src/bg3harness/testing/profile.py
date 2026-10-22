"""A temp Larian profile for a test process, set before anything imports bg3harness.runner.env.

bg3harness.runner.env reads SAVE_DIR and SE_BG3AI_DIR (steam_paths: $BG3AI_SAVE_DIR / $BG3AI_SE_DIR, else the real
profile) once, at import: a test process that imports it first with the real ones runs every later test against
the live game's saves and Script Extender files (live5 §0.4, live6 §3). Every pytest run in a venv with bg3harness
loads bg3harness.testing.pytest_profile (a pytest11 entry point) first; runner/testenv.py does it for unittest. All
call temp_profile(), so whichever runs first picks the dir and the others reuse it ($BG3AI_TEST_PROFILE). A child
process inherits the env vars. Forced, not setdefault: a shell that exports the real folders must not leak them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path

ENV = "BG3AI_TEST_PROFILE"
DIR_VARS = ("BG3AI_SAVE_DIR", "BG3AI_SE_DIR", "BG3AI_PROFILE_DIR")


def temp_profile(environ: MutableMapping[str, str] = os.environ) -> Path:
    """The process's temp profile root (made on the first call); points the three profile env vars into it."""
    root = Path(environ.get(ENV) or tempfile.mkdtemp(prefix="bg3ai-test-profile-"))
    environ[ENV] = str(root)
    environ["BG3AI_SAVE_DIR"] = str(root / "saves")
    environ["BG3AI_SE_DIR"] = str(root / "se")
    environ["BG3AI_PROFILE_DIR"] = str(root / "profile")
    return root


def leaks(environ: Mapping[str, str] = os.environ, modules: Mapping[str, object] = sys.modules) -> list[str]:
    """The profile folders this test process would use that are not under the temp dir: the three env vars, and
    bg3harness.runner.env's SAVE_DIR and SE_BG3AI_DIR once it is imported (a test may point them at its own tmp)."""
    tmp = Path(tempfile.gettempdir()).resolve()
    dirs = {var: environ.get(var, "") for var in DIR_VARS}
    renv = modules.get("bg3harness.runner.env")
    if renv is not None:
        dirs |= {f"renv.{n}": str(getattr(renv, n)) for n in ("SAVE_DIR", "SE_BG3AI_DIR")}
    return [f"{k}={v or '(unset)'}" for k, v in dirs.items() if not v or not Path(v).resolve().is_relative_to(tmp)]
