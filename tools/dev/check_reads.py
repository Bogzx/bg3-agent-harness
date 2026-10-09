"""Live read-latency probe (read-only: observation tools only, no actions, no hot reload).

Calls the bridge's read tools in-process against the running game, N rounds each, and prints per tool the
median/p90/max round trip plus the evals each call made (ops: "a+b+events.since" ran in one eval).
Target (goal): read median < 60 ms, p90 < 200 ms.

  uv run --project bridge python tools/dev/check_reads.py [--rounds 10] [--queue]

--queue also keeps a job.poll loop running on the shared connection pool (like a running action queue), to
show reads no longer wait behind it. Safe during setup's live work; don't run it during an agent session
(it would consume the agent's events).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge" / "src"))
_metrics = Path(tempfile.mkdtemp(prefix="bg3ai-reads-")) / "metrics.jsonl"
os.environ.setdefault("BG3AI_METRICS_PATH", str(_metrics))

from bg3ai import metrics as M  # noqa: E402
from bg3ai import server  # noqa: E402

TOOLS = {
    "get_status": lambda: server.get_status(),
    "look": lambda: server.look(),
    "get_nearby": lambda: server.get_nearby(),
    "get_map": lambda: server.get_map(),
    "get_frontier": lambda: server.get_frontier(),
    "get_party": lambda: server.get_party(),
    "get_dialogue": lambda: server.get_dialogue(),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--queue", action="store_true", help="poll a job id in the background meanwhile")
    a = ap.parse_args()
    stop = threading.Event()
    if a.queue:
        def poll():
            while not stop.is_set():
                server.game.try_rpc("job.poll", {"id": 1})
                time.sleep(0.1)
        threading.Thread(target=poll, daemon=True).start()
    try:
        for name, fn in TOOLS.items():
            fn()  # warm-up (first look/get_status is a full view)
            for _ in range(a.rounds):
                fn()
                time.sleep(0.05)
    finally:
        stop.set()
    recs = [r for r in M.read_jsonl(_metrics) if r.get("type") == "tool" and r.get("tool") in TOOLS and r.get("ok", True)]
    allms = []
    for name in TOOLS:
        rs = [r for r in recs if r["tool"] == name][1:]
        if not rs:
            continue
        ms = [r["ms"] for r in rs]
        allms += ms
        ops = max(rs, key=lambda r: r["ms"]).get("ops")
        print(f"{name:14s} n={len(ms):3d} median {statistics.median(ms):6.1f} ms  p90 {M.percentile(ms, 90):6.1f}"
              f"  max {max(ms):6.1f}  evals {statistics.median([r['rpc_n'] for r in rs]):.0f}  slowest ops {ops}")
    if allms:
        med, p90 = statistics.median(allms), M.percentile(allms, 90)
        ok = med < 60 and p90 < 200
        print(f"ALL            median {med:.1f} ms  p90 {p90:.1f} ms  -> {'PASS' if ok else 'FAIL'} (<60 / <200)")
        return 0 if ok else 1
    print("no records (is the game running with a save loaded?)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
