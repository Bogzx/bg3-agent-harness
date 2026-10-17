"""Live checks for setup's economy tools. Read-only unless an action is named.

  uv run --project bridge python tools/dev/check_setup.py status            # get_status full + ALERTS, inventory filters
  flock -w 1800 runtime/game.lock uv run --project bridge python tools/dev/check_setup.py call <tool> '<json args>'
  flock -w 1800 runtime/game.lock uv run --project bridge python tools/dev/check_setup.py credits
      # game at the splash or the main menu (a launch without --load): splash screen, then the menu's Credits
      # button (a real shim click) -> ui.state screen credits, ending false; ESC back to the menu. PASS/FAIL lines.
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

from bg3ai import server  # noqa: E402


def call(name: str, args: dict) -> str:
    fn = getattr(server, name)
    fn = getattr(fn, "fn", fn)
    t0 = time.monotonic()
    out = fn(**args)
    print(f"=== {name}({args}) {1000 * (time.monotonic() - t0):.0f} ms\n{out}\n")
    return out


# The main menu's Credits button, as a share of the window (live 2026-10-17: (382, 820) in 1920x1080, with the
# Continue button shown; runtime/live/20261017-L4-probes/menu.jpg).
CREDITS_BUTTON = (382 / 1920, 820 / 1080)


def credits_probe() -> int:
    """The credits/splash detection (setup S8) at the main menu, through ui.state only."""
    from bg3ai.input import InputClient

    fails = 0

    def verdict(ok: bool, what: str, st: dict) -> None:
        nonlocal fails
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}: {json.dumps(st, sort_keys=True)}")

    def ui() -> dict:
        return server.game.try_rpc("ui.state", context="client") or {}

    def wait(cond, secs: float) -> dict:
        end = time.monotonic() + secs
        while True:
            st = ui()
            if cond(st) or time.monotonic() > end:
                return st
            time.sleep(0.5)

    with InputClient(timeout=5.0) as inp:
        st = ui()
        if st.get("screen") != "main_menu":
            verdict(st.get("screen") == "splash" and st.get("page") == "SplashScreen", "splash screen", st)
            inp.key("ENTER")
            st = wait(lambda x: x.get("screen") == "main_menu", 20)
        if st.get("screen") != "main_menu":
            verdict(False, "main menu (needed for the credits)", st)
            return 1
        w, h = inp.window_size()
        inp.click(round(CREDITS_BUTTON[0] * w), round(CREDITS_BUTTON[1] * h))
        st = wait(lambda x: x.get("screen") == "credits", 8)
        verdict(st.get("screen") == "credits" and st.get("page") == "Credits" and st.get("blocking") is True,
                "menu Credits -> screen credits", st)
        verdict((st.get("credits") or {}).get("ending") is False, "menu credits: ending false", st)
        verdict("?" not in (st.get("panels_open") or []), "no nameless panel", st)
        inp.key("ESCAPE")
        st = wait(lambda x: x.get("screen") == "main_menu", 10)
        verdict(st.get("screen") == "main_menu", "ESC closes the credits", st)
    return 1 if fails else 0


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
