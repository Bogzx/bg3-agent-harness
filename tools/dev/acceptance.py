"""Acceptance run before play resumes: every scriptable DONE item of docs/BACKLOG.md, live, PASS/FAIL/SKIP per item.

    uv run --project bridge python tools/dev/acceptance.py                  # every section, one game.lock hold each
    uv run --project bridge python tools/dev/acceptance.py --only rest,trade
    uv run --project bridge python tools/dev/acceptance.py --list             # sections, order, estimated time
    uv run --project bridge --with lz4 --with zstandard python tools/dev/acceptance.py --check   # offline, no game

Each section runs in its own process under `flock -w 1800 runtime/game.lock` (other workers test between
sections), loads its own save first and verifies the load (a server Lua marker set before the load must be gone
after it, the party must be readable and the screen the game view), then reloads that save at the end, so story
state never changes. Saves are only loaded (S027-start, S026-dammon-heat, S050-end), never written; the bridge runs
with a scratch runtime and a scratch memory dir (note() writes there). Results: runtime/acceptance/<section>.json
+ .log, and the table printed at the end (also runtime/acceptance/table.md). Exit code = number of FAILs.

The code is the acceptance package next to this file (tools/dev/acceptance/); this is its entry point.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import main  # the package beside this file

if __name__ == "__main__":
    raise SystemExit(main())
