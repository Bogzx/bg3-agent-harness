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

The package: core.py (saves, plan, result log, S, shared helpers), one module per section (reads.py ... vis.py;
hotbar.py holds the hotbar probes se and act share) and runner.py (FUNCS, run_section, main).
tools/dev/acceptance.py is the entry point the commands above run.
"""

from __future__ import annotations

from .core import BASE, EST_S, LOCK_HELD_ENV, SAVE_NEEDS, SECTION_OWNERS, SECTIONS
from .runner import FUNCS, main, run_section
from .setup import cc_verdict

__all__ = [
    "BASE",
    "EST_S",
    "FUNCS",
    "LOCK_HELD_ENV",
    "SAVE_NEEDS",
    "SECTIONS",
    "SECTION_OWNERS",
    "cc_verdict",
    "main",
    "run_section",
]
