"""The desktop session's display env for the game, found when a shell lacks it (stdlib only).

A the multiplexer pane or an ssh shell has no DISPLAY/XAUTHORITY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR, so the game can't open a
window. The desktop's Xwayland carries them: `Xwayland :1 -auth /run/user/1000/xauth_XXXX ...` (docs/HANDOFF.md).
Used by runner/play.py (run) and tools/dev/live_window.py (its display step).
"""

from __future__ import annotations

import os
import re
import subprocess

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
    XAUTHORITY stays unset when no Xwayland runs (the caller decides whether that's fatal)."""
    got: dict[str, str] = {}
    if not (env.get("DISPLAY") and env.get("XAUTHORITY")):
        ps = pgrep_xwayland() if ps_text is None else ps_text
        if not env.get("DISPLAY"):
            got["DISPLAY"] = xwayland_display(ps) or default_display
        auth = None if env.get("XAUTHORITY") else xwayland_auth(ps)
        if auth:
            got["XAUTHORITY"] = auth
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
