# pyright: basic
"""tools/dev/first_launch.sh's wait for the launch's own load (runner/launch_game.sh --load / --continue: the game's
-loadSaveGame / -continueGame). The game reaches the main menu first and starts that load ~1.5 s later (live D5/D6),
so a READY at the menu let the caller's own load of the same save race it (D6: the save loaded twice). READY now
comes once the load has finished: client state Running and the server context answering.

    PYTHONPATH=packages/harness/src:bridge/src python3 -m bg3harness.game.launch_load [--timeout S] [--pid LAUNCHER]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from . import io as game

POLL_S = 1.0
MENU_GRACE_S = 30.0  # at the main menu this long (no splash) without the load starting: it never will (no such save?)


def wait_loaded(
    alive: Callable[[], bool],
    timeout: float,
    *,
    state: Callable[[], str | None] = game.game_state,
    ui: Callable[[], dict[str, Any] | None] = game.ui_state,
    server: Callable[[], bool] = game.server_ready,
    press: Callable[[], str] = game.press_menu_key,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> str | None:
    """None once the launch's load has finished (Running, server context up), else why not. A "Press any key"
    splash on the way gets a key (as GameController.wait_running)."""
    start = clock()
    menu_since: float | None = None
    st: str | None = None
    while True:
        now = clock()
        if not alive():
            return "the game exited before its launch load finished"
        st = state()
        if st == "Running" and server():
            return None
        splash = st == "Menu" and game.at_splash(ui())
        if splash:
            log(f"  splash screen: pressed {press()}")
        menu_since = (menu_since if menu_since is not None else now) if st == "Menu" and not splash else None
        if menu_since is not None and now - menu_since >= MENU_GRACE_S:
            return f"the game stayed at the main menu for {MENU_GRACE_S:.0f}s: its launch load never started"
        if now - start >= timeout:
            return f"the launch load didn't finish within {timeout:.0f}s (client state {st})"
        sleep(POLL_S)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="launch_load", description="first_launch.sh: wait for the launch's own load")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--pid", type=int, default=None, help="the launcher: its exit fails the wait at once")
    a = ap.parse_args(argv)
    pid = a.pid
    t0 = time.monotonic()
    why = wait_loaded(lambda: pid is None or _pid_alive(pid), a.timeout)
    if why:
        print(f"  FAIL: {why}", file=sys.stderr)
        return 1
    print(f"  ok: the launch's own load finished after {time.monotonic() - t0:.0f}s (Running, server context up)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
