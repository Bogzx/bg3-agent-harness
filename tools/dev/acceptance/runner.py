"""Runs the sections: each in its own process under runtime/game.lock (--section), the plan, the table."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import core
from .act import sec_act
from .combat import sec_combat
from .core import EST_S, LOCK, LOCK_HELD_ENV, OUT, REPO, RESULTS, SCRIPT, SECTIONS, alive, game_memory, item, log
from .gear import sec_gear
from .loads import sec_loads
from .nav import sec_nav
from .reads import sec_reads
from .rest import sec_rest
from .se import sec_se
from .setup import sec_setup
from .strict import sec_strict
from .swap import sec_swap
from .trade import sec_trade
from .vis import sec_vis

FUNCS = {"reads": sec_reads, "rest": sec_rest, "trade": sec_trade, "gear": sec_gear, "swap": sec_swap,
         "nav": sec_nav, "loads": sec_loads, "combat": sec_combat, "se": sec_se, "setup": sec_setup,
         "strict": sec_strict, "act": sec_act, "vis": sec_vis}


def run_section(name: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")
    os.environ.setdefault("BG3AI_SAVE_PREFIX", "Claude-")
    # the game's capture layer writes into the launcher's runtime/shots, not into our scratch runtime
    os.environ.setdefault("BG3AI_SHOTS_DIR", str(Path(os.environ.get("BG3AI_RUNTIME") or REPO / "runtime") / "shots"))
    os.environ["BG3AI_RUNTIME"] = str(OUT / "rt")
    os.environ["BG3AI_MEMORY_DIR"] = str(OUT / "memory")
    os.environ["BG3AI_SESSION"] = "S999"
    (OUT / "memory" / "sessions").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    from server_view import S as server  # noqa: E402, N811
    core.S.bind(server)
    core.LOGF = open(OUT / f"{name}.log", "w")
    log(f"section {name} start {time.strftime('%H:%M:%S')}")
    try:
        if not alive():
            item(f"section {name}", "FAIL", "the game is not running")
        else:
            FUNCS[name]()
    except Exception as e:  # noqa: BLE001
        import traceback
        log(traceback.format_exc())
        item(f"section {name}", "FAIL", f"script error {type(e).__name__}: {e}")
    (OUT / f"{name}.json").write_text(json.dumps(RESULTS, indent=1))
    log(f"section {name} end {time.strftime('%H:%M:%S')}")
    return sum(r["verdict"] == "FAIL" for r in RESULTS)


def plan(names: list[str]) -> str:
    t = 0
    rows = []
    for n in names:
        t += EST_S.get(n, 60)
        rows.append(f"  {n:<7} ~{EST_S.get(n, 60) // 60}m{EST_S.get(n, 60) % 60:02d}s  (done by ~{t // 60}m{t % 60:02d}s)")
    return "\n".join(rows) + f"\n  total ~{t // 60} min (+ waits for {LOCK.name})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--section", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true", help="offline: validate tools, RPC ops, Lua snippets, saves")
    a = ap.parse_args()
    if a.list:
        print(" ".join(SECTIONS))
        print(plan(SECTIONS))
        return 0
    if a.check:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import acceptance_check
        return acceptance_check.main_check(sys.modules[__name__.rpartition(".")[0]])  # the acceptance package
    if a.section:
        return run_section(a.section)
    names = [s for s in (a.only.split(",") if a.only else SECTIONS) if s]
    unknown = [n for n in names if n not in FUNCS]
    if unknown:
        print(f"unknown section(s) {unknown}; sections: {' '.join(SECTIONS)}")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    print("plan:\n" + plan(names), flush=True)
    rows = []
    died = ""
    memory = [{"after": "start", "t": round(time.time()), **game_memory()}]   # memory.json: live_window's table
    (OUT / "memory.json").write_text(json.dumps(memory, indent=1))
    for name in names:
        if died:  # the game is gone: the rest can't run; say so instead of N "the game is not running" FAILs
            rows.append({"section": name, "item": f"section {name}", "verdict": "SKIP",
                         "evidence": f"not run: the game died during section {died} (relaunch, then --only {name},…)"})
            continue
        print(f"--- section {name} (~{EST_S.get(name, 60)} s): waiting for {LOCK.name} ({time.strftime('%H:%M:%S')})",
              flush=True)
        t = time.monotonic()
        started = time.time() - 1   # a result file older than this is a previous run's
        lock = [] if os.environ.get(LOCK_HELD_ENV) == "1" else ["flock", "-w", "1800", str(LOCK)]
        subprocess.run(lock + ["timeout", "900", sys.executable, str(SCRIPT), "--section", name])
        print(f"--- section {name} took {time.monotonic() - t:.0f} s (est. {EST_S.get(name, 60)} s)", flush=True)
        f = OUT / f"{name}.json"
        if f.exists() and f.stat().st_mtime >= started:
            rows += [dict(r, section=name) for r in json.loads(f.read_text())]
        else:   # killed by the timeout, or crashed before writing: an older file is a previous run's
            rows.append({"section": name, "item": f"section {name}", "verdict": "FAIL", "evidence": "no result file"})
        if not alive():
            died = name
            rows.append({"section": name, "item": f"game alive after section {name}", "verdict": "FAIL",
                         "evidence": "bg3 is gone: collect runtime/logs/bg3le-*.log (crash: lines) + "
                                     f"runtime/acceptance/{name}.log"})
        memory.append({"after": name, "t": round(time.time()), **game_memory()})
        (OUT / "memory.json").write_text(json.dumps(memory, indent=1))
        print(f"--- memory after {name}: {memory[-1]}", flush=True)
    table = ["| item | verdict | evidence |", "|---|---|---|"] + [
        f"| {r['item']} | {r['verdict']} | {r['evidence'].replace('|', '/')} |" for r in rows]
    (OUT / "table.md").write_text("\n".join(table) + "\n")
    (OUT / "results.json").write_text(json.dumps(rows, indent=1))   # with each row's section (live_window.py)
    print("\n".join(table))
    return sum(r["verdict"] == "FAIL" for r in rows)
