"""The runner's names for the modules that moved into packages/harness (bg3harness, overhaul step 5): runner/<name>.py
is a shim that makes `import <name>` the package module itself (so mock.patch on it reaches the real code), and
`python3 runner/<name>.py ...` runs the module's own __main__. The shims go when runner/ imports bg3harness directly.
"""

import importlib
import runpy
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = (_REPO / "bridge" / "src", _REPO / "packages" / "harness" / "src", _REPO / "packages" / "bench" / "src")
for _p in _SRC:  # no-ops under the uv workspace
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def alias(name: str, module: str) -> None:
    if name == "__main__":
        runpy.run_module(f"bg3harness.{module}", run_name="__main__", alter_sys=True)
    else:
        sys.modules[name] = importlib.import_module(f"bg3harness.{module}")
