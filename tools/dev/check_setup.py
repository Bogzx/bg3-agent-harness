"""Live checks for setup's economy tools. Read-only unless an action is named.

  uv run --project bridge python tools/dev/check_setup.py status            # get_status full + ALERTS, inventory filters
  flock -w 1800 runtime/live.lock uv run --project bridge python tools/dev/check_setup.py call <tool> '<json args>'
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


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "status":
        call("get_status", {"full": True})
        for f in ("food", "junk", "gear"):
            call("get_inventory", {"filter": f})
    elif mode == "call":
        for i in range(2, len(sys.argv), 2):
            call(sys.argv[i], json.loads(sys.argv[i + 1]) if i + 1 < len(sys.argv) else {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
