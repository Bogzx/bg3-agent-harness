#!/usr/bin/env python3
"""Moved to packages/harness: bg3harness.preflight (its docstring lists the checks). This is still the CLI:

    python3 tools/bench_preflight.py [--series NAME]

It passes the package the harness's Steam and bg3le helpers (tools/steam_paths.py, tools/bg3le_buildinfo.py) and
this checkout. `import bench_preflight` gets the package's module, so mock.patch on it patches the code that runs."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "packages" / "harness" / "src"))
import bg3le_buildinfo  # noqa: E402
import steam_paths  # noqa: E402
from bg3harness import preflight as _m  # noqa: E402

if __name__ == "__main__":
    sys.exit(_m.main(sys.argv[1:], _m.Ctx(repo=TOOLS.parent, steam=steam_paths, bg3le=bg3le_buildinfo)))
sys.modules[__name__] = _m
