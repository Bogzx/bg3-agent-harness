"""The desktop session's display env for the game, found when a shell lacks it (stdlib only).

A the multiplexer pane or an ssh shell has no DISPLAY/XAUTHORITY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR, so the game can't open a
window. The desktop's Xwayland carries them: `Xwayland :1 -auth /run/user/1000/xauth_XXXX ...` (docs/HANDOFF.md).
Used by runner/play.py (run), tools/dev/live_window.py (its display step) and runner/launch_game.sh (`display_env.py
shell`: fill, or refuse to launch). Without a display the game doesn't fail cleanly: L8 2026-10-18 00:1x, two launches
from a the multiplexer shell fell back to the AMD iGPU (no NVIDIA PRIME without X) and dumped SIGTRAP cores within 10 s.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

DEFAULT_DISPLAY = ":1"   # the test machine's Xwayland
KEYS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")


def xwayland_auth(ps_text: str) -> str | None:
    """XAUTHORITY from `pgrep -a Xwayland` output: the value of its -auth argument."""
    m = re.search(r"-auth\s+(\S+)", ps_text)
    return m.group(1) if m else None


def xwayland_display(ps_text: str) -> str | None:
    """DISPLAY from `pgrep -a Xwayland` output: its `:N` argument."""
    m = re.search(r"Xwayland\S*\s+(?:\S+\s+)*?(:\d+)\b", ps_text)
    return m.group(1) if m else None


def pgrep_xwayland() -> str:
    try:
        return subprocess.run(["pgrep", "-a", "Xwayland"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def wayland_socket(runtime_dir: str) -> str | None:
    """The first wayland-N socket in XDG_RUNTIME_DIR (not its .lock)."""
    try:
        names = sorted(n for n in os.listdir(runtime_dir) if re.fullmatch(r"wayland-\d+", n))
    except OSError:
        return None
    return names[0] if names else None


def fill(env: dict, default_display: str = DEFAULT_DISPLAY, uid: int | None = None, ps_text: str | None = None) -> dict:
    """Set the unset KEYS in env (in place) from the desktop session; returns {key: value} of what was set.
    XAUTHORITY stays unset when no Xwayland runs (the caller decides whether that's fatal). An XAUTHORITY whose
    file is gone counts as unset (and is dropped): Xwayland makes a new one when it restarts (naut3 2026-10-17:
    after the 16:01 freeze xauth_CCLmsa -> xauth_tDSowK, so an exported or earlier-found value went stale)."""
    got: dict[str, str] = {}
    stale = bool(env.get("XAUTHORITY")) and not os.path.exists(env["XAUTHORITY"])
    if not (env.get("DISPLAY") and env.get("XAUTHORITY")) or stale:
        ps = pgrep_xwayland() if ps_text is None else ps_text
        if not env.get("DISPLAY"):
            got["DISPLAY"] = xwayland_display(ps) or default_display
        auth = None if env.get("XAUTHORITY") and not stale else xwayland_auth(ps)
        if auth and auth != env.get("XAUTHORITY"):
            got["XAUTHORITY"] = auth
        elif stale and not auth:
            env.pop("XAUTHORITY")
    if not env.get("XDG_RUNTIME_DIR"):
        d = f"/run/user/{os.getuid() if uid is None else uid}"
        if os.path.isdir(d):
            got["XDG_RUNTIME_DIR"] = d
    rd = env.get("XDG_RUNTIME_DIR") or got.get("XDG_RUNTIME_DIR")
    if not env.get("WAYLAND_DISPLAY") and rd:
        sock = wayland_socket(rd)
        if sock:
            got["WAYLAND_DISPLAY"] = sock
    env.update(got)
    return got


def describe(env: dict) -> str:
    return " ".join(f"{k}={env.get(k) or 'unset'}" for k in KEYS)


def problem(env: dict, home: str | None = None, x11_dir: str = "/tmp/.X11-unix") -> str | None:
    """Why the game can't open a window with env (after fill), or None. DISPLAY must be set and, when local (":N"),
    its X socket must exist; XAUTHORITY must name an existing file, or be unset with a ~/.Xauthority (Xlib's
    default)."""
    disp = env.get("DISPLAY")
    if not disp:
        return "DISPLAY is unset and no Xwayland runs (is the desktop session up?)"
    m = re.fullmatch(r"(?:unix)?:(\d+)(?:\.\d+)?", disp)
    if m and not os.path.exists(os.path.join(x11_dir, f"X{m.group(1)}")):
        return f"DISPLAY={disp} but no X server socket {x11_dir}/X{m.group(1)}"
    auth = env.get("XAUTHORITY")
    if auth:
        return None if os.path.isfile(auth) else f"XAUTHORITY={auth} doesn't exist (Xwayland restarted?)"
    home = home if home is not None else env.get("HOME") or os.path.expanduser("~")
    if os.path.isfile(os.path.join(home, ".Xauthority")):
        return None
    return f"XAUTHORITY is unset, no ~/.Xauthority and no Xwayland -auth found (DISPLAY={disp})"


def main(argv: list[str]) -> int:
    """`display_env.py shell`: `export K=V` lines for what fill() found (eval them); when the result can't open a
    window, also `display_problem=<why>` and exit 1. `display_env.py check`: describe, the problem on stderr."""
    if len(argv) != 2 or argv[1] not in ("shell", "check"):
        print("usage: display_env.py shell|check", file=sys.stderr)
        return 2
    env = dict(os.environ)
    got = fill(env)
    why = problem(env)
    if argv[1] == "shell":
        for k, v in got.items():
            print(f"export {k}={shlex.quote(v)}")
        if "XAUTHORITY" in os.environ and "XAUTHORITY" not in env:
            print("unset XAUTHORITY")   # stale and no Xwayland: fill dropped it
        if why:
            print(f"display_problem={shlex.quote(why)}")
    else:
        print(describe(env))
        if why:
            print(why, file=sys.stderr)
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
