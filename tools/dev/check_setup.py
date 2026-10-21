"""Live checks for setup's economy tools. Read-only unless an action is named.

  uv run --project bridge python tools/dev/check_setup.py status            # get_status full + ALERTS, inventory filters
  flock -w 1800 runtime/game.lock uv run --project bridge python tools/dev/check_setup.py call <tool> '<json args>'
  flock -w 1800 runtime/game.lock uv run --project bridge python tools/dev/check_setup.py credits
      # a launch without --load (it waits out the loading): splash screen, Enter (again while the splash stays, up
      # to 4 presses), the main menu settled, then its Credits button (a real shim click, once more if the menu
      # stays up) -> ui.state screen credits, ending false; ESC back to the menu. PASS/FAIL lines.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge" / "src"))
_tmp = Path(tempfile.mkdtemp(prefix="bg3ai-setup-"))
atexit.register(shutil.rmtree, _tmp, True)
os.environ.setdefault("BG3AI_METRICS_PATH", str(_tmp / "metrics.jsonl"))
# Live tests load their own LIVETEST-<worker> save first (game.lock rule); save/load is gated on these (11:14: a
# load_game here FAILED "BG3AI_PLAYTHROUGH unset" and the script went on in whatever state the game was in).
os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")
os.environ.setdefault("BG3AI_SAVE_PREFIX", "Claude-")

from server_view import S as server  # noqa: E402, N811


def call(name: str, args: dict) -> str:
    fn = getattr(server, name)
    fn = getattr(fn, "fn", fn)
    t0 = time.monotonic()
    out = fn(**args)
    print(f"=== {name}({args}) {1000 * (time.monotonic() - t0):.0f} ms\n{out}\n")
    return out


# The main menu's Credits button, as a share of the window (live 2026-10-17: (382, 820) in 1920x1080, with the
# Continue button shown; runtime/live/20261017-L4-probes/menu.jpg). Without Continue the buttons sit elsewhere.
CREDITS_BUTTON = (382 / 1920, 820 / 1080)
# Live L6 (runtime/live/20261017-L6-probes): run straight after a launch the game was still loading (screen
# loading, LoadModule), and a click as soon as the main menu read did nothing (it was still fading in); the same
# click on the shown menu worked. So: wait for the splash/menu, then for the menu to read the same for
# MENU_SETTLE_S, and click once more if the first click left the menu up.
MENU_WAIT_S = 180
MENU_SETTLE_S = 2.0
CREDITS_WAIT_S = 6.0
# Live L7: on a fresh launch the first Enter on the splash didn't take (still SplashScreen 20 s later); the runner
# presses again every few seconds while the splash shows (play.py wait_menu). So: Enter, wait, Enter again, bounded.
SPLASH_PRESSES = 4
SPLASH_PRESS_WAIT_S = 8.0
POLL_S = 0.5


def menu_buttons(st: dict) -> list | None:
    """The main menu's button labels as ui.state reads them (`menu`; `popup` before setup S9), None if unread."""
    return (st.get("menu") or st.get("popup") or {}).get("buttons")


def credits_steps(ui, key, click, clock=time.monotonic, sleep=time.sleep, out=print) -> int:
    """The probe against ui() (ui.state), key(name) and click() (the Credits button). Exit code: 1 on any FAIL."""
    fails = 0

    def verdict(ok: bool, what: str, st: dict) -> None:
        nonlocal fails
        fails += not ok
        out(f"{'PASS' if ok else 'FAIL'} {what}: {json.dumps(st, sort_keys=True)}")

    def wait(cond, secs: float) -> dict:
        end = clock() + secs
        while True:
            st = ui()
            if cond(st) or clock() > end:
                return st
            sleep(POLL_S)

    def settled(secs: float) -> dict:
        """The main menu once it has read the same for `secs` (its fade-in done), or whatever shows after 20 s."""
        end, since, last = clock() + 20, None, None
        while True:
            st = ui()
            now = clock()
            seen = (st.get("screen"), st.get("page"), st.get("panels_open"), menu_buttons(st))
            if st.get("screen") != "main_menu" or seen != last:
                since, last = now, seen
            elif now - since >= secs:
                return st
            if now > end:
                return st
            sleep(POLL_S)

    st = wait(lambda x: x.get("screen") in ("splash", "main_menu"), MENU_WAIT_S)
    if st.get("screen") != "main_menu":
        verdict(st.get("screen") == "splash" and st.get("page") == "SplashScreen", "splash screen", st)
        for press in range(1, SPLASH_PRESSES + 1):
            if st.get("screen") != "splash":
                break
            if press > 1:
                out(f"RETRY Enter on the splash (still up {SPLASH_PRESS_WAIT_S:.0f} s after press {press - 1})")
            key("ENTER")  # only on the splash: on the main menu Enter would press Continue
            st = wait(lambda x: x.get("screen") == "main_menu", SPLASH_PRESS_WAIT_S)
    if st.get("screen") == "main_menu":
        st = settled(MENU_SETTLE_S)
    if st.get("screen") != "main_menu":
        verdict(False, "main menu (needed for the credits)", st)
        return 1
    buttons = menu_buttons(st)
    if buttons is not None and "Continue" not in buttons:
        verdict(False, "menu shows Continue (the Credits button position assumes it)", st)
        return 1
    for attempt in (1, 2):
        click()
        st = wait(lambda x: x.get("screen") == "credits", CREDITS_WAIT_S)
        if st.get("screen") != "main_menu" or attempt == 2:
            break
        out(f"RETRY the Credits click once (the menu was still up {CREDITS_WAIT_S:.0f} s after it)")
        st = settled(MENU_SETTLE_S)
    verdict(st.get("screen") == "credits" and st.get("page") == "Credits" and st.get("blocking") is True,
            "menu Credits -> screen credits", st)
    verdict((st.get("credits") or {}).get("ending") is False, "menu credits: ending false", st)
    verdict("?" not in (st.get("panels_open") or []), "no nameless panel", st)
    key("ESCAPE")
    st = wait(lambda x: x.get("screen") == "main_menu", 10)
    verdict(st.get("screen") == "main_menu", "ESC closes the credits", st)
    return 1 if fails else 0


def credits_probe() -> int:
    """The credits/splash detection (setup S8) at the main menu, through ui.state only."""
    from bg3ai.input import InputClient

    def ui() -> dict:
        return server.game.try_rpc("ui.state", context="client") or {}

    with InputClient(timeout=5.0) as inp:
        def click() -> None:
            w, h = inp.window_size()
            inp.click(round(CREDITS_BUTTON[0] * w), round(CREDITS_BUTTON[1] * h))

        return credits_steps(ui, inp.key, click)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "status":
        call("get_status", {"full": True})
        for f in ("food", "junk", "gear"):
            call("get_inventory", {"filter": f})
    elif mode == "call":
        for i in range(2, len(sys.argv), 2):
            call(sys.argv[i], json.loads(sys.argv[i + 1]) if i + 1 < len(sys.argv) else {})
    elif mode == "credits":
        return credits_probe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
