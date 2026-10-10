#!/usr/bin/env python3
"""RAM watchdog: keeps the game alive when the 15 GB laptop runs short of memory.

2026-10-10 09:50 the kernel OOM killer killed bg3 (anon-rss 1.8 GB, oom_score_adj 200: the game inherits Steam's
200, so it was the preferred victim) while 4 worker agents ran test suites in parallel. This watchdog:
  - logs free RAM once a minute to runtime/ram.log (every 10 s while low);
  - lowers the game's oom_score_adj to GAME_OOM_ADJ (needs root to go below the inherited value: tries a direct
    write, then `sudo -n choom`; logs once if neither is allowed);
  - below WARN_MB available: marks test runs (unittest/pytest/Lua test suites of this user) as the OOM killer's
    first victims (oom_score_adj 1000, no privilege needed) and logs a warning;
  - below KILL_MB available: SIGTERMs those test runs before the kernel picks the game.

runner/play.py runs it as a thread for the whole run; standalone (e.g. live tests with the runner stopped):
    python3 runner/ramwatch.py            # until Ctrl-C
    python3 runner/ramwatch.py --once     # one check, printed
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", Path(__file__).resolve().parents[1] / "runtime"))
LOG = RUNTIME / "ram.log"
INTERVAL_S = 60
LOW_INTERVAL_S = 10
WARN_MB = 1500
KILL_MB = 800
GAME_OOM_ADJ = -500
TEST_OOM_ADJ = 1000
GAME_COMM = "bg3"
# Test runs that may be sacrificed for the game: python unittest/pytest, the Lua test scripts and their Docker
# client. Never claude, the runner, the bridge or the game.
TEST_RX = re.compile(r"(-m\s+(unittest|pytest)\b|\bpytest\b|/test_[\w-]+\.(sh|py|lua)\b|nickblah/lua)")


def meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """/proc/meminfo in MB."""
    out = {}
    for line in path.read_text().splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k] = int(parts[0]) // 1024
    return out


def _procs(proc: Path = Path("/proc")):
    me = os.getuid()
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != me:
                continue
            comm = (d / "comm").read_text().strip()
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        yield int(d.name), comm, cmd


def game_pids(procs=None) -> list[int]:
    return [pid for pid, comm, _ in (procs if procs is not None else _procs()) if comm == GAME_COMM]


def test_pids(procs=None, exclude: set[int] | None = None) -> list[int]:
    exclude = (exclude or set()) | {os.getpid()}
    return [pid for pid, comm, cmd in (procs if procs is not None else _procs())
            if pid not in exclude and comm != GAME_COMM and "claude" not in comm and TEST_RX.search(cmd)]


def rss_mb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def get_adj(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/oom_score_adj").read_text())
    except (OSError, ValueError):
        return None


def set_adj(pid: int, adj: int, sudo: bool = True) -> str:
    """'ok' | 'sudo' | the reason it failed. Raising is always allowed; lowering below the value the process
    inherited needs CAP_SYS_RESOURCE (root)."""
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text(str(adj))
        return "ok"
    except PermissionError:
        pass
    except OSError as e:
        return str(e)
    if not sudo:
        return "not permitted"
    try:
        r = subprocess.run(["sudo", "-n", "choom", "-p", str(pid), "-n", str(adj)], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"sudo choom: {e}"
    return "sudo" if r.returncode == 0 else f"not permitted (sudo -n choom: {(r.stderr or r.stdout).strip()[:120]})"


class RamWatch:
    def __init__(self, log=None, logfile: Path = LOG, warn_mb: int = WARN_MB, kill_mb: int = KILL_MB,
                 exclude: set[int] | None = None):
        self.log = log or (lambda m: None)
        self.logfile = logfile
        self.warn_mb, self.kill_mb = warn_mb, kill_mb
        self.exclude = exclude or set()
        self.protected: dict[int, str] = {}   # game pid → result of lowering its oom_score_adj (tried once)
        self.low = False

    def _write(self, line: str) -> None:
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with self.logfile.open("a") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        except OSError:
            pass

    def tick(self, mem: dict[str, int] | None = None, procs=None) -> dict:
        mem = mem if mem is not None else meminfo()
        procs = list(procs if procs is not None else _procs())
        avail = mem.get("MemAvailable", 0)
        games = game_pids(procs)
        for pid in games:
            if pid not in self.protected:
                adj = get_adj(pid)
                res = "ok" if adj is not None and adj <= GAME_OOM_ADJ else set_adj(pid, GAME_OOM_ADJ)
                self.protected[pid] = res
                if res not in ("ok", "sudo"):
                    self.log(f"ramwatch: can't lower bg3 {pid} oom_score_adj from {adj} to {GAME_OOM_ADJ}: {res}")
        out = {"avail_mb": avail, "swap_free_mb": mem.get("SwapFree", 0),
               "game_rss_mb": sum(rss_mb(p) for p in games), "game_adj": [get_adj(p) for p in games],
               "action": ""}
        tests = test_pids(procs, self.exclude) if avail < self.warn_mb else []
        if avail < self.kill_mb and tests:
            for pid in tests:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            out["action"] = f"killed {len(tests)} test run(s) {tests[:8]}"
            self.log(f"ramwatch: only {avail} MB RAM available: SIGTERM to test runs {tests[:8]} to save the game")
        elif avail < self.warn_mb:
            for pid in tests:
                set_adj(pid, TEST_OOM_ADJ, sudo=False)
            out["action"] = f"low RAM: {len(tests)} test run(s) marked oom_score_adj {TEST_OOM_ADJ}"
            if not self.low:
                self.log(f"ramwatch: WARNING only {avail} MB RAM available (game {out['game_rss_mb']} MB); "
                         "run fewer tests in parallel")
        self.low = avail < self.warn_mb
        self._write(f"avail={avail}MB swap_free={out['swap_free_mb']}MB game_rss={out['game_rss_mb']}MB "
                    f"game_adj={out['game_adj']} {out['action']}".rstrip())
        return out

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the watchdog never breaks the runner
                self._write(f"error: {e}")
            stop.wait(LOW_INTERVAL_S if self.low else INTERVAL_S)


def take_lock(path: Path = RUNTIME / "ramwatch.lock"):
    """One watchdog per machine (the runner's thread or a standalone one). Returns the held file, or None."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


def start_background(log=None) -> threading.Event | None:
    """Starts the watchdog thread unless another one holds runtime/ramwatch.lock (then None)."""
    lock = take_lock()
    if lock is None:
        return None
    stop = threading.Event()

    def run():
        try:
            RamWatch(log).loop(stop)
        finally:
            lock.close()
    threading.Thread(target=run, name="ramwatch", daemon=True).start()
    return stop


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one check, printed")
    a = ap.parse_args()
    w = RamWatch(lambda m: print(m, flush=True))
    if a.once:
        print(w.tick())
        return 0
    lock = take_lock()
    if lock is None:
        print("another RAM watchdog holds runtime/ramwatch.lock")
        return 1
    try:
        w.loop(threading.Event())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
