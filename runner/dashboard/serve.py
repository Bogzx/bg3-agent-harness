#!/usr/bin/env python3
"""Retired: the live dashboard is now the playthrough app (packages/playthrough, bg3play-serve). This shim starts it
with the old flags and port, so docs and scripts keep working:

    python3 runner/dashboard/serve.py --port 8765                      # screen as raw PNG (no Pillow)
    uv run --project bridge python runner/dashboard/serve.py --port 8765   # screen as small JPEGs

The old endpoints (/api/state, /api/feed, ...) are served by bg3play/dashboard.py; the page is the new web app
(build it once: cd packages/playthrough/web && npm ci && npm run build). /series moves to the bench package.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))  # bg3play's dependency
sys.path.insert(0, str(REPO / "packages" / "playthrough" / "src"))
from bg3play import server  # noqa: E402


def translate(argv: list[str] | None = None) -> list[str]:
    """The old serve.py flags -> bg3play-serve's (game polling on, screen capture unless --no-screen)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to watch from the LAN)")
    ap.add_argument("--no-screen", action="store_true", help="don't capture game frames")
    ap.add_argument("--screen-interval", type=float, default=5.0)
    ap.add_argument("--screen-width", type=int, default=1280)
    ap.add_argument("--status-interval", type=float, default=3.0)
    ap.add_argument("--memory-dir", help="default: playthroughs/<runner playthrough>/memory, else play/memory")
    ap.add_argument("--debug-port", type=int, default=int(os.environ.get("BG3LE_DEBUG_PORT", "9998")))
    ap.add_argument("--verbose", action="store_true")
    opt = ap.parse_args(argv)
    out = ["--runtime", os.environ.get("BG3AI_RUNTIME") or str(REPO / "runtime"),
           "--playthroughs", str(REPO / "playthroughs"), "--repo", str(REPO),
           "--host", opt.host, "--port", str(opt.port), "--debug-port", str(opt.debug_port),
           "--game", "--status-interval", str(opt.status_interval),
           "--screen-interval", str(opt.screen_interval), "--screen-width", str(opt.screen_width)]
    out += [] if opt.no_screen else ["--screen"]
    out += ["--memory-dir", opt.memory_dir] if opt.memory_dir else []
    out += ["--verbose"] if opt.verbose else []
    return out


def main(argv: list[str] | None = None) -> int:
    args = translate(argv)
    if not (server.DEFAULT_WEB / "index.html").is_file():
        print(f"serve.py: no built web app at {server.DEFAULT_WEB} (the API still works); build it with "
              "cd packages/playthrough/web && npm ci && npm run build", file=sys.stderr)
    return server.main(args)


if __name__ == "__main__":
    sys.exit(main())
