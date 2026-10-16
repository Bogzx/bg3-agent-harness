#!/usr/bin/env python3
"""RAM watchdog: keeps the game alive when the 15 GB laptop runs short of memory.

2026-10-10 09:50 the kernel OOM killer killed bg3 (anon-rss 1.8 GB, oom_score_adj 200: the game inherits Steam's
200, so it was the preferred victim) while 4 worker agents ran test suites in parallel. This watchdog:
  - logs free RAM once a minute to runtime/ram.log (every 10 s while low);
  - samples the game's RSS every SAMPLE_S; growth > SPIKE_MB within SPIKE_WINDOW_S is logged with the last tool
    calls (runtime/actions.jsonl) to ram.log, the runner log and runtime/ram_spikes.jsonl (OOM attribution:
    20:48 the game went 5.0 → 13.3 GB in < 40 s after loading the Shadowfell, between two 60 s samples);
  - samples every FAST_SAMPLE_S while the RSS climbs fast, and SIGKILLs bg3 above KILL_GAME_MB RSS or below
    KILL_AVAIL_MB available (test runs go first), logging the last bg3le log lines and tool calls
    (runtime/ram_kills.jsonl); our own kill is no OOM event, so systemd stops no unit for it;
  - no longer lowers the game's oom_score_adj (it used to set -500): the game runs in its own capped systemd scope
    (runner/launch_game.sh), and when RAM runs out the game must be the victim, never an agent (an OOM kill inside
    the multiplexer's unit makes systemd stop every agent);
  - below WARN_MB available: marks test runs (unittest/pytest/Lua test suites of this user) as the OOM killer's
    first victims (oom_score_adj 1000, no privilege needed) and logs a warning;
  - below KILL_MB available: SIGTERMs those test runs before the kernel picks the game.

runner/play.py runs it as a thread for the whole run; standalone (e.g. live tests with the runner stopped):
    python3 runner/ramwatch.py            # until Ctrl-C
    python3 runner/ramwatch.py --once     # one check, printed
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import json
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
SAMPLE_S = 5                   # game RSS sampling (cheap: /proc/<pid>/status)
FAST_SAMPLE_S = 1              # … while the RSS grows by more than FAST_GROWTH_MB per sample
FAST_GROWTH_MB = 300
# Last resort: ramwatch SIGKILLs bg3 itself (a kill from us is no kernel OOM, so systemd never stops a unit for it).
# 9.5 GB: above the 6.3 GB normal peak and just under launch_game.sh's MemoryMax=10G, so a runaway dies here, with
# the bg3le crumbs and last tool calls logged, rather than at the cgroup cap or the global OOM killer.
KILL_GAME_MB = 9500
# … or above KILL_ANON_SWAP_MB of anonymous memory incl. what is swapped out (RssAnon + VmSwap of all bg3 processes).
# S039 21:53: RSS 4.8 → 7.9 GB in 18 s, then the kernel OOM-killed bg3 at the scope's 10G cap: the scope swapped the
# runaway's pages out (MemorySwapMax=2G), so the RSS never reached 9.5 GB. Normal play: anon ~5.4 GB + swap ~0.2 GB;
# the scope's memory.current (~9.2 GB) is mostly reclaimable file cache, so it is no kill signal.
KILL_ANON_SWAP_MB = 9000
KILL_AVAIL_MB = 700            # … or when RAM available drops below this and no test run is left to kill
KILLS = RUNTIME / "ram_kills.jsonl"   # the runner's OOM crash-loop guard counts these like kernel OOM kills
SPIKE_MB = 2048                # RSS growth within SPIKE_WINDOW_S that gets logged with the last tool calls
SPIKE_WINDOW_S = 60
LOUD_MB = 7000                 # spikes ending above this (normal play peaks at ~6.3 GB) also go to the runner log;
                               # smaller ones are usually a save loading (menu ~2 GB → 5 GB) and stay in ram.log
SPIKES = RUNTIME / "ram_spikes.jsonl"
ACTIONS = RUNTIME / "actions.jsonl"   # the MCP server's tool log
WARN_MB = 1500
KILL_MB = 800
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


def anon_swap_mb(pid: int) -> int:
    """Anonymous memory incl. its swapped-out part (RssAnon + VmSwap): what the game really holds."""
    total = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("RssAnon:", "VmSwap:")):
                total += int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return total // 1024


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


def last_tools(t: float, path: Path = ACTIONS, window_s: float = 120, n: int = 8,
               tail_bytes: int = 400_000) -> list[str]:
    """The tool calls of the last `window_s` before t, oldest first: 'HH:MM:SS tool {args} 1234ms → result…'."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            data = f.read()
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not (t - window_s <= r.get("t", 0) <= t + 1):
            continue
        args = json.dumps(r.get("args") or {}, separators=(",", ":"))[:160]
        res = str(r.get("result") or "").split("\n")[0][:100]
        out.append(f"{datetime.fromtimestamp(r['t']):%H:%M:%S} {r.get('tool')} {args} {r.get('ms', '?')}ms → {res}")
    return out[-n:]


def bg3le_crumbs(logs: Path | None = None, n: int = 12, tail_bytes: int = 65536) -> list[str]:
    """The last n non-empty lines of the newest bg3le log (runtime/logs/bg3le-<stamp>.log.<pid>; binary-safe)."""
    logs = logs or RUNTIME / "logs"
    try:
        f = max(logs.glob("bg3le-*.log*"), key=lambda p: p.stat().st_mtime)
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            data = fh.read().decode(errors="replace")
    except (OSError, ValueError):
        return []
    return [ln.rstrip()[:200] for ln in data.splitlines() if ln.strip()][-n:]


def last_tick(path: Path = LOG, tail_bytes: int = 16384) -> tuple[float, str] | None:
    """The newest periodic tick line of ram.log ("… anon+swap=…MB …") and its time (epoch s), or None. Spike, kill
    and test-kill lines are written between ticks, so the file's last line (and its mtime) is not always a tick."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if "anon+swap=" in ln and " avail=" in ln:
            try:
                return datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").timestamp(), ln.strip()
            except ValueError:
                continue
    return None


class RamWatch:
    def __init__(self, log=None, logfile: Path = LOG, warn_mb: int = WARN_MB, kill_mb: int = KILL_MB,
                 exclude: set[int] | None = None):
        self.log = log or (lambda m: None)
        self.logfile = logfile
        self.warn_mb, self.kill_mb = warn_mb, kill_mb
        self.exclude = exclude or set()
        self.low = False
        self.samples: collections.deque[tuple[float, int]] = collections.deque()   # (t, game RSS MB)
        self.spike_at = 0.0
        self.actions, self.spikes, self.kills, self.logs = ACTIONS, SPIKES, KILLS, RUNTIME / "logs"
        self.last_anon_swap = 0
        self.fast = False            # the RSS grew fast at the last sample: sample every FAST_SAMPLE_S
        self.tests_killed_at = 0.0

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
        out = {"avail_mb": avail, "swap_free_mb": mem.get("SwapFree", 0),
               "game_rss_mb": sum(rss_mb(p) for p in games),
               "game_anon_swap_mb": sum(anon_swap_mb(p) for p in games), "game_adj": [get_adj(p) for p in games],
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
                    f"anon+swap={out['game_anon_swap_mb']}MB "
                    f"game_adj={out['game_adj']} {out['action']}".rstrip())
        return out

    def _append(self, path: Path, rec: dict) -> None:
        try:
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def sample(self, now: float | None = None, rss: int | None = None, avail: int | None = None,
               pids: list[int] | None = None, anon_swap: int | None = None) -> dict | None:
        """One game RSS sample. Kills bg3 (SIGKILL) above KILL_GAME_MB RSS or KILL_ANON_SWAP_MB anon+swap, or below
        KILL_AVAIL_MB available once no test run is left to kill; logs growth > SPIKE_MB within SPIKE_WINDOW_S (once per window). Returns the kill
        record ({"kind": "kill", …}), the spike record ({"kind": "spike", …}) or None."""
        now = time.time() if now is None else now
        if pids is None:
            pids = game_pids()
        if rss is None:
            rss = sum(rss_mb(p) for p in pids)
        if anon_swap is None:
            anon_swap = sum(anon_swap_mb(p) for p in pids)
        q = self.samples
        if rss <= 0:          # no game (or it just died): a new game starts a new baseline
            q.clear()
            self.fast = False
            self.last_anon_swap = 0
            return None
        if avail is None:
            try:
                avail = meminfo().get("MemAvailable", 0)
            except OSError:
                avail = 1 << 30
        self.fast = bool(q) and (rss - q[-1][1] > FAST_GROWTH_MB or anon_swap - self.last_anon_swap > FAST_GROWTH_MB)
        self.last_anon_swap = anon_swap
        q.append((now, rss))
        while q and q[0][0] < now - SPIKE_WINDOW_S:
            q.popleft()
        t0, low = min(q, key=lambda s: s[1])
        if rss >= KILL_GAME_MB or avail < KILL_AVAIL_MB:
            if rss < KILL_GAME_MB and now - self.tests_killed_at > 10:   # still short soon after: the game
                tests = test_pids(exclude=self.exclude)
                if tests:      # RAM is short but the game is in bounds: test runs go first
                    for pid in tests:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
                    self.tests_killed_at = now
                    self.fast = True
                    self._write(f"only {avail} MB available (game {rss} MB): SIGTERM to test runs {tests[:8]}")
                    return None
            return self._kill_game(now, pids, rss, avail, low, t0)
        if anon_swap >= KILL_ANON_SWAP_MB:
            return self._kill_game(now, pids, rss, avail, low, t0, anon_swap)
        if rss - low <= SPIKE_MB or now - self.spike_at < SPIKE_WINDOW_S:
            return None
        self.spike_at = now
        rec = {"kind": "spike", "t": now, "from_mb": low, "to_mb": rss, "seconds": round(now - t0),
               "avail_mb": avail, "tools": last_tools(now, self.actions)}
        msg = (f"RSS SPIKE: game {low} → {rss} MB in {rec['seconds']} s (RAM available {avail} MB); last tool calls: "
               + (" | ".join(rec["tools"]) or "none in the last 2 min"))
        self._write(msg)
        if rss >= LOUD_MB:
            self.log(f"ramwatch: {msg}")
        self._append(self.spikes, rec)
        return rec

    def _kill_game(self, now: float, pids: list[int], rss: int, avail: int, low: int, t0: float,
                   anon_swap: int | None = None) -> dict:
        why = (f"game RSS {rss} MB >= {KILL_GAME_MB} MB" if rss >= KILL_GAME_MB
               else f"game anon+swap {anon_swap} MB >= {KILL_ANON_SWAP_MB} MB (RSS {rss} MB; the rest is swapped out)"
               if anon_swap is not None
               else f"only {avail} MB RAM available (< {KILL_AVAIL_MB} MB, game {rss} MB)")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        rec = {"kind": "kill", "t": now, "why": why, "rss_mb": rss, "to_mb": rss, "avail_mb": avail, "from_mb": low,
               "seconds": round(now - t0), "anon_swap_mb": anon_swap, "pids": pids, "tools": last_tools(now, self.actions),
               "bg3le": bg3le_crumbs(self.logs)}
        msg = (f"KILLED bg3 {pids} (SIGKILL): {why}; RSS {low} → {rss} MB in {rec['seconds']} s. Last tool calls: "
               + (" | ".join(rec["tools"]) or "none in the last 2 min")
               + ". Last bg3le lines: " + (" ⏎ ".join(rec["bg3le"][-6:]) or "none"))
        self._write(msg)
        self.log(f"ramwatch: !!!!! {msg}")
        self._append(self.kills, rec)
        self._append(self.spikes, rec)
        self.samples.clear()
        self.fast = False
        return rec

    def loop(self, stop: threading.Event) -> None:
        next_tick = 0.0
        while not stop.is_set():
            try:
                if time.time() >= next_tick:
                    self.tick()
                    next_tick = time.time() + (LOW_INTERVAL_S if self.low else INTERVAL_S)
                self.sample()
            except Exception as e:  # noqa: BLE001 - the watchdog never breaks the runner
                self._write(f"error: {e}")
            stop.wait(FAST_SAMPLE_S if self.fast else SAMPLE_S)


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
