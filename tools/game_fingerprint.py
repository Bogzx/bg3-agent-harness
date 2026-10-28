"""Shim: packages/harness/src/bg3harness/game_fingerprint.py (the harness's game environment).

`import game_fingerprint` (tools/ on sys.path) is the package module itself, so mock.patch on it reaches the real code;
`python3 tools/game_fingerprint.py ...` runs its CLI."""

import importlib
import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the type checker sees the package module's names
    from bg3harness.game_fingerprint import *  # noqa: F403

_SRC = str(Path(__file__).resolve().parents[1] / "packages" / "harness" / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if __name__ == "__main__":
    runpy.run_module("bg3harness.game_fingerprint", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("bg3harness.game_fingerprint")
