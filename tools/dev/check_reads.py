"""Live read-latency probe (read-only: observation tools only, no actions, no hot reload).

Calls the bridge's read tools in-process against the running game, N rounds each, and prints per tool the
median/p90/max round trip plus the evals each call made (ops: "a+b+events.since" ran in one eval).
Target (goal): read median < 30 ms, p90 < 100 ms (server ticks ~25 ms, client ~46 ms on the test machine).

  uv run --project bridge python tools/dev/check_reads.py [--rounds 10] [--queue]

Also prints the debug connections before and after (bridge pools, client sockets to the port, bg3le's own
count from its log) and FAILs if bg3le holds slots with no client socket behind them (a leak).

--queue also keeps a job.poll loop running on the shared connection pool (like a running action queue), to
show reads no longer wait behind it. Safe during setup's live work; don't run it during an agent session
(it would consume the agent's events).
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import statistics
import sys
import shutil
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge" / "src"))
_metrics = Path(tempfile.mkdtemp(prefix="bg3ai-reads-")) / "metrics.jsonl"
atexit.register(shutil.rmtree, _metrics.parent, True)
os.environ.setdefault("BG3AI_METRICS_PATH", str(_metrics))

from bg3ai import metrics as M  # noqa: E402
from server_view import S as server  # noqa: E402, N811

RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", ROOT / "runtime"))
DEBUG_PORT = int(os.environ.get("BG3LE_DEBUG_PORT", "9998"))
_COUNT = re.compile(r"debug: client (?:attached \((\d+) connected\)|detached \((\d+) remaining\))")


def bg3le_clients(text: str) -> int | None:
    """bg3le's own count of attached debug clients: the last "client attached (N connected)" or "client
    detached (N remaining)" line of its log."""
    last = None
    for m in _COUNT.finditer(text):
        last = int(m.group(1) or m.group(2))
    return last


def tcp_clients(port: int = DEBUG_PORT, proc_net: str = "/proc/net/tcp") -> int:
    """Established TCP connections to 127.0.0.1:port seen from the client side (every process here)."""
    n = 0
    try:
        lines = Path(proc_net).read_text().splitlines()[1:]
    except OSError:
        return 0
    for line in lines:
        f = line.split()
        if len(f) > 3 and f[3] == "01" and int(f[2].split(":")[1], 16) == port:  # 01 = ESTABLISHED
            n += 1
    return n


def connections() -> dict:
    """Debug connections now: this process's pools, every client socket on the machine, and bg3le's count.
    bg3le counting more than there are client sockets = slots leaked on the game side."""
    logs = sorted((RUNTIME / "logs").glob("bg3le-*.log*"), key=lambda p: p.stat().st_mtime)
    game_n = None
    if logs:
        try:
            game_n = bg3le_clients(logs[-1].read_text(errors="replace"))
        except OSError:
            pass
    pools = {"agent": server.game}
    if getattr(server, "_watch_game", None) is not None:
        pools["watch"] = server._watch_game
    return {"bridge": {k: (g.open_connections, g.pool_max) for k, g in pools.items()},
            "sockets": tcp_clients(), "bg3le": game_n}


def print_connections(label: str, c: dict) -> None:
    pools = "  ".join(f"{k} {n}/{cap}" for k, (n, cap) in c["bridge"].items())
    print(f"connections {label:6s} bridge pools {pools}  client sockets to :{DEBUG_PORT} {c['sockets']}"
          f"  bg3le says {c['bg3le'] if c['bg3le'] is not None else '?'}")

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
    before = connections()
    print_connections("before", before)
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
    time.sleep(0.5)  # let bg3le log the detaches of anything this run closed
    after = connections()
    print_connections("after", after)
    over = [k for k, (n, cap) in after["bridge"].items() if n > cap]
    leaked = (after["bg3le"] - after["sockets"]) if after["bg3le"] is not None else 0
    conns_ok = not over and leaked <= 1  # 1: a probe can be mid-handshake
    print(f"connections    pools within cap: {not over}  bg3le slots without a client socket: {leaked}"
          f"  -> {'PASS' if conns_ok else 'FAIL'}")
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
        ok = med < 30 and p90 < 100
        print(f"ALL            median {med:.1f} ms  p90 {p90:.1f} ms  -> {'PASS' if ok else 'FAIL'} (<30 / <100)")
        return 0 if ok and conns_ok else 1
    print("no records (is the game running with a save loaded?)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
