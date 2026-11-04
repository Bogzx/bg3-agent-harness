#!/usr/bin/env python3
# pyright: basic
"""RAM watchdog: keeps the game alive when the 15 GB test machine runs short of memory.

2026-10-10 09:50 the kernel OOM killer killed bg3 (anon-rss 1.8 GB, oom_score_adj 200: the game inherits Steam's
200, so it was the preferred victim) while 4 worker agents ran test suites in parallel. This watchdog:
  - logs free RAM once a minute to runtime/ram.log (every 10 s while low);
  - samples the game's RSS every SAMPLE_S; growth > SPIKE_MB within SPIKE_WINDOW_S is logged with the last tool
    calls (runtime/actions.jsonl) to ram.log, the runner log and runtime/ram_spikes.jsonl (OOM attribution:
    20:48 the game went 5.0 → 13.3 GB in < 40 s after loading the Shadowfell, between two 60 s samples);
  - samples every FAST_SAMPLE_S while the RSS climbs fast, and SIGKILLs bg3 above KILL_GAME_MB RSS (both kill
    thresholds move with the game's cap: its scope's memory.max, else $BG3AI_GAME_MEMORY_MAX: kill_limits) or below
    KILL_AVAIL_MB available (test runs go first), logging the last bg3le log lines and tool calls
    (runtime/ram_kills.jsonl); our own kill is no OOM event, so systemd stops no unit for it;
  - no longer lowers the game's oom_score_adj (it used to set -500): the game runs in its own capped systemd scope
    (runner/launch_game.sh), and when RAM runs out the game must be the victim, never an agent (an OOM kill inside
    the agents' service unit makes systemd stop every agent);
  - below WARN_MB available: marks test runs (unittest/pytest/Lua test suites of this user) as the OOM killer's
    first victims (oom_score_adj 1000, no privilege needed) and logs a warning;
  - below KILL_MB available: SIGTERMs those test runs before the kernel picks the game;
  - every THERMAL_S writes one temperature line to runtime/thermal.log (synced, so the last seconds before a hard
    power-off survive; the format of an operator's thermal_log.sh, which it replaces while it holds
    runtime/thermal_log.lock), and SIGKILLs bg3 when the CPU or the GPU stays above the heat-kill limits
    (heat_limits(); runtime/heat_kills.jsonl, with the last tool calls). The test box cut its power twice under the naut
    series load (2026-10-16 ~20:07, 2026-10-17 ~16:01; no kernel log, no MCE, no OOM). The runner pauses between
    sessions at the lower heat-pause limits (runner/play.py heat_paused, from the same log).

runner/play.py runs it as a thread for the whole run; standalone (e.g. live tests with the runner stopped):
    PYTHONPATH=packages/harness/src python3 -m bg3harness.ramwatch          # until Ctrl-C
    PYTHONPATH=packages/harness/src python3 -m bg3harness.ramwatch --once   # one check (RAM + one thermal line), printed
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from bg3harness.recorder import find_window

RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", Path(__file__).resolve().parents[4] / "runtime"))
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
# Both are the 10G cap's numbers. Under a smaller cap C (MiB, as rss_mb counts) each is min(its 10G number,
# C - KILL_UNDER_CAP_MB) (REVIEW-16 A4, D880: 8G -> 7692/7692; an 8G cgroup OOM-kills the game before 9500, while a
# healthy game's anon+swap reached 7102 on 2026-10-28, so a margin that scales down further kills healthy games).
# C is the game scope's cgroup memory.max when readable, else $BG3AI_GAME_MEMORY_MAX (launch_game.sh's MemoryMax,
# G = 1024 MiB), else 10G.
GAME_CAP_ENV = "BG3AI_GAME_MEMORY_MAX"
KILL_UNDER_CAP_MB = 500
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
THERMAL_LOG = RUNTIME / "thermal.log"
THERMAL_LOCK = RUNTIME / "thermal_log.lock"   # an operator's thermal_log.sh takes the same one (flock -n)
THERMAL_S = 5                  # one thermal line per this many seconds (thermal_log.sh's cadence)
HEAT_KILLS = RUNTIME / "heat_kills.jsonl"
HWMON = Path("/sys/class/hwmon")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
NV_QUERY = "temperature.gpu,power.draw,clocks.gr,utilization.gpu,clocks_throttle_reasons.active"
SAMPLE_GAP_S = 15              # a longer gap between two thermal samples breaks a "sustained" run
# (env var, default): CPU = k10temp Tctl, GPU = nvidia-smi's temperature (the amdgpu hwmon without nvidia-smi)
HEAT_ENV = {"pause_cpu_c": ("BG3AI_HEAT_PAUSE_CPU_C", 92), "pause_gpu_c": ("BG3AI_HEAT_PAUSE_GPU_C", 85),
            "pause_s": ("BG3AI_HEAT_PAUSE_S", 60),
            "kill_cpu_c": ("BG3AI_HEAT_KILL_CPU_C", 97), "kill_gpu_c": ("BG3AI_HEAT_KILL_GPU_C", 92),
            "kill_s": ("BG3AI_HEAT_KILL_S", 20)}
# The game's own scope (runner/launch_game.sh, --collect): its memory.events/memory.stat go into every tick and sample;
# when it ends, its Result from the user journal goes to ram.log and the runner log with the last snapshot (r13 a1:
# the game died with nothing logged about how)
CGROUP_ROOT = Path("/sys/fs/cgroup")
SCOPE_PREFIX = "bg3ai-game-"
STAT_KEYS = ("anon", "file", "kernel", "shmem", "file_mapped")   # memory.stat bytes, logged in MB
SCOPE_END_WAIT_S = 30          # how long a gone scope's end lines may take to reach the journal
MAP_STATES = {"Viewable": "viewable", "UnMapped": "unmapped", "Unviewable": "unviewable"}


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


def game_scope_cgroup(pid: int, proc: Path = Path("/proc"), root: Path = CGROUP_ROOT) -> Path | None:
    """The cgroup dir of the game's own scope (bg3ai-game-<stamp>.scope), or None (no scope, cgroup v1, gone)."""
    try:
        lines = (proc / str(pid) / "cgroup").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        rel = line[3:].strip() if line.startswith("0::") else ""
        if Path(rel).name.startswith(SCOPE_PREFIX) and rel.endswith(".scope"):
            return root / rel.lstrip("/")
    return None


def cgroup_mem(cg: Path) -> str:
    """'events[low=0 high=0 max=0 oom=0 oom_kill=0 …] stat[anon=5400MB …]' of a cgroup ('' once it is gone)."""
    try:
        events = (cg / "memory.events").read_text().split()
        stat = dict(ln.split(" ", 1) for ln in (cg / "memory.stat").read_text().splitlines() if " " in ln)
    except (OSError, ValueError):
        return ""
    ev = " ".join(f"{k}={v}" for k, v in zip(events[::2], events[1::2], strict=False))
    st = " ".join(f"{k}={int(stat[k]) // 2**20}MB" for k in STAT_KEYS if stat.get(k, "").strip().isdigit())
    return f"events[{ev}] stat[{st}]"


def scope_journal(unit: str, run=subprocess.run) -> list[str]:
    """systemd's lines about the unit from the user journal, unit prefix cut ("Failed with result 'oom-kill'.",
    "Consumed …"); [] when journalctl isn't usable."""
    try:
        r = run(["journalctl", "--user", "--no-pager", "-q", "-o", "cat", "-u", unit],
                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln[len(unit) + 2:] for ln in (r.stdout or "").splitlines() if ln.startswith(unit + ": ")]


def scope_result(lines: list[str]) -> str | None:
    """The ended scope's Result ('success' unless systemd says "Failed with result '…'"), None while not ended."""
    for ln in lines:
        m = re.search(r"Failed with result '([^']+)'", ln)
        if m:
            return m.group(1)
    return "success" if any(ln.startswith(("Consumed ", "Deactivated")) for ln in lines) else None


def game_window(display: str, run=subprocess.run) -> str:
    """'viewable 1408x792' | 'unmapped WxH' | 'unviewable WxH' | 'none': the game window's map state and size
    (xwininfo -id on recorder.find_window's id)."""
    try:
        tree = run(["xwininfo", "-root", "-tree", "-display", display], capture_output=True, text=True, timeout=5,
                   check=False).stdout
        wid = find_window(tree or "")
        if not wid:
            return "none"
        info = run(["xwininfo", "-id", wid, "-display", display], capture_output=True, text=True, timeout=5,
                   check=False).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return "none"
    m = re.search(r"Map State:\s*Is(\w+)", info)
    w, h = re.search(r"Width:\s*(\d+)", info), re.search(r"Height:\s*(\d+)", info)
    if not m:
        return "none"
    return f"{MAP_STATES.get(m.group(1), m.group(1).lower())} {w.group(1) if w else '?'}x{h.group(1) if h else '?'}"


def heat_limits(env=None) -> dict[str, float]:
    """The heat guard's limits (HEAT_ENV): the env var's number, else the default (a bad value = the default)."""
    env = os.environ if env is None else env
    out = {}
    for k, (var, default) in HEAT_ENV.items():
        try:
            out[k] = float(env.get(var) or default)
        except ValueError:
            out[k] = float(default)
    return out


def hwmon_temp(name: str, root: Path = HWMON) -> int | None:
    """temp1_input of the first hwmon called `name`, in whole C (thermal_log.sh's hw()), or None."""
    try:
        dirs = sorted(root.glob("hwmon*"))   # bash's glob order
    except OSError:
        return None
    for d in dirs:
        try:
            if (d / "name").read_text().strip() != name:
                continue
            return int((d / "temp1_input").read_text().strip()) // 1000
        except (OSError, ValueError):
            return None
    return None


def nvidia_query(run=subprocess.run) -> str:
    """nvidia-smi's NV_QUERY as 'temp,power,clock,util,throttle' (spaces removed), '' without nvidia-smi."""
    if run is subprocess.run and not shutil.which("nvidia-smi"):
        return ""
    try:
        r = run(["nvidia-smi", f"--query-gpu={NV_QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout.splitlines() or [""])[0].replace(" ", "")


def _num(v: str) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def thermal_sample(root: Path = HWMON, profile: Path = PLATFORM_PROFILE, nv: str | None = None,
                   loadavg: Path = Path("/proc/loadavg"), game_up: bool | None = None) -> dict:
    """One reading: hwmon temps by name (None when missing), the nvidia-smi fields, platform_profile, load."""
    nv = nvidia_query() if nv is None else nv
    try:
        prof = profile.read_text().strip()
    except OSError:
        prof = ""
    try:
        load = loadavg.read_text().split()[0]
    except (OSError, IndexError):
        load = ""
    s = {name: hwmon_temp(name, root) for name in ("k10temp", "acpitz", "amdgpu", "nvme")}
    s.update(nv=nv, nv_temp=_num(nv.split(",")[0]) if nv else None, profile=prof, load=load,
             bg3=bool(game_pids()) if game_up is None else game_up)
    return s


def sample_gpu(s: dict) -> int | None:
    """The GPU temperature the heat guard watches: NVIDIA's when nvidia-smi answers, else the amdgpu hwmon."""
    return s.get("nv_temp") if s.get("nv_temp") is not None else s.get("amdgpu")


def thermal_line(s: dict, now: datetime | None = None) -> str:
    """thermal_log.sh's line: '2026-10-17 16:25:39 cpu=51 acpi=51 amdgpu=49 nvme=36 nv=[47,8.09,210,0,0x…1]
    profile=balanced load=0.21 bg3=down' ('-' for a missing sensor, nv=[] without nvidia-smi)."""
    def v(x):
        return "-" if x is None else x
    return (f"{(now or datetime.now()):%Y-%m-%d %H:%M:%S} cpu={v(s.get('k10temp'))} acpi={v(s.get('acpitz'))} "
            f"amdgpu={v(s.get('amdgpu'))} nvme={v(s.get('nvme'))} nv=[{s.get('nv') or ''}] "
            f"profile={s.get('profile') or ''} load={s.get('load') or ''} bg3={'up' if s.get('bg3') else 'down'}")


THERMAL_RX = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) cpu=(\S+) acpi=\S+ amdgpu=(\S+) nvme=\S+ nv=\[([^\]]*)\]")


def parse_thermal(line: str) -> tuple[float, int | None, int | None] | None:
    """(epoch s, CPU C, GPU C) of a thermal.log line (either writer), or None for any other line."""
    m = THERMAL_RX.match(line)
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None
    nv = m.group(4)
    gpu = _num(nv.split(",")[0]) if nv else None
    return t, _num(m.group(2)), gpu if gpu is not None else _num(m.group(3))


def thermal_tail(path: Path = THERMAL_LOG, tail_bytes: int = 16384) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            return fh.read().decode(errors="replace").splitlines()
    except OSError:
        return []


def last_thermal(path: Path = THERMAL_LOG) -> tuple[float, str] | None:
    """The newest temperature line of thermal.log and its time (epoch s), or None."""
    for ln in reversed(thermal_tail(path)):
        p = parse_thermal(ln)
        if p:
            return p[0], ln.strip()
    return None


def held_for(samples, hot) -> float:
    """How long `hot(cpu, gpu)` has held without a break up to the newest sample (s; 0 when it doesn't hold now).
    samples: (t, cpu, gpu) oldest first; a gap > SAMPLE_GAP_S ends the run."""
    start = end = None
    for t, cpu, gpu in reversed(list(samples)):
        if not hot(cpu, gpu) or (start is not None and start - t > SAMPLE_GAP_S):
            break
        end = t if end is None else end
        start = t
    return 0.0 if end is None else end - start


def over(cpu_c: float, gpu_c: float):
    return lambda cpu, gpu: (cpu is not None and cpu >= cpu_c) or (gpu is not None and gpu >= gpu_c)


def heat_state(samples, now: float, limits: dict | None = None, stale_s: float = 30) -> dict:
    """The runner's heat pause from thermal samples (t, cpu, gpu): {"pause": hot for pause_s, "cool": under the
    pause limits for pause_s, "hot_s", "cool_s", "cpu", "gpu", "fresh"}. Stale data (newest sample older than
    stale_s, or none) pauses nothing."""
    lim = limits or heat_limits()
    samples = list(samples)
    fresh = bool(samples) and now - samples[-1][0] <= stale_s
    hot = over(lim["pause_cpu_c"], lim["pause_gpu_c"])
    hot_s = held_for(samples, hot) if fresh else 0.0
    cool_s = held_for(samples, lambda c, g: not hot(c, g)) if fresh else 0.0
    cpu, gpu = (samples[-1][1], samples[-1][2]) if samples else (None, None)
    return {"pause": fresh and hot_s >= lim["pause_s"], "cool": not fresh or cool_s >= lim["pause_s"],
            "hot_s": hot_s, "cool_s": cool_s, "cpu": cpu, "gpu": gpu, "fresh": fresh}


def read_heat(path: Path = THERMAL_LOG, now: float | None = None, limits: dict | None = None) -> dict:
    """heat_state() of thermal.log's tail."""
    samples = [p for p in map(parse_thermal, thermal_tail(path)) if p]
    return heat_state(samples, time.time() if now is None else now, limits)


def cap_mb(cap: str | None) -> int | None:
    """MiB of a MemoryMax as systemd reads it ('8G', '8192M'); None when unset or another form (bytes, %, infinity)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GM])\s*", cap or "", re.IGNORECASE)
    if not m:
        return None
    return int(float(m.group(1)) * (1024 if m.group(2).upper() == "G" else 1))


def scope_cap_mb(cg: Path) -> int | None:
    """MiB of a cgroup's memory.max; None when it is unreadable or 'max' (no cap)."""
    try:
        v = (cg / "memory.max").read_text().strip()
    except OSError:
        return None
    return int(v) // 2**20 if v.isdigit() else None


def kill_limits(cap: int | None) -> tuple[int, int]:
    """(RSS, anon+swap) kill thresholds in MiB under a game cap of `cap` MiB; None keeps the 10G numbers."""
    if cap is None:
        return KILL_GAME_MB, KILL_ANON_SWAP_MB
    return min(KILL_GAME_MB, cap - KILL_UNDER_CAP_MB), min(KILL_ANON_SWAP_MB, cap - KILL_UNDER_CAP_MB)


class RamWatch:
    def __init__(self, log=None, logfile: Path = LOG, warn_mb: int = WARN_MB, kill_mb: int = KILL_MB,
                 exclude: set[int] | None = None):
        self.log = log or (lambda m: None)
        self.logfile = logfile
        self.warn_mb, self.kill_mb = warn_mb, kill_mb
        self.env_cap_mb = cap_mb(os.environ.get(GAME_CAP_ENV))   # the fallback when no scope's memory.max reads
        self.kill_game_mb, self.kill_anon_swap_mb = kill_limits(self.env_cap_mb)
        self.exclude = exclude or set()
        self.low = False
        self.samples: collections.deque[tuple[float, int]] = collections.deque()   # (t, game RSS MB)
        self.spike_at = 0.0
        self.actions, self.spikes, self.kills, self.logs = ACTIONS, SPIKES, KILLS, RUNTIME / "logs"
        self.last_anon_swap = 0
        self.fast = False            # the RSS grew fast at the last sample: sample every FAST_SAMPLE_S
        self.tests_killed_at = 0.0
        self.thermal_log, self.thermal_lock_path, self.heat_kills = THERMAL_LOG, THERMAL_LOCK, HEAT_KILLS
        self.thermal_lock = None     # held runtime/thermal_log.lock: this watchdog writes thermal.log
        self.heat: collections.deque[tuple[float, int | None, int | None]] = collections.deque(maxlen=64)
        self.limits = heat_limits()
        self.next_thermal = 0.0
        self.proc, self.cgroup_root, self.run = Path("/proc"), CGROUP_ROOT, subprocess.run
        self.display = os.environ.get("DISPLAY") or ":1"
        self.scope: dict | None = None   # the game scope's last snapshot: {"unit", "t", "mem"}, + "gone" once ended

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
        snap = self.scope_snapshot(games)
        win = f" win={game_window(self.display, self.run)}" if games else ""
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
                    f"game_adj={out['game_adj']}{win}{f' scope={snap[0]} {snap[1]}' if snap else ''} "
                    f"{out['action']}".rstrip())
        return out

    def scope_snapshot(self, pids: list[int]) -> tuple[str, str] | None:
        """(unit, cgroup_mem) of the game's scope, or None. Sets the kill thresholds from the scope's memory.max (else
        the env's cap): a ramwatch started without $BG3AI_GAME_MEMORY_MAX still follows the real cap."""
        cap = None
        for pid in pids:
            cg = game_scope_cgroup(pid, self.proc, self.cgroup_root)
            cap = cap or (scope_cap_mb(cg) if cg else None)
            mem = cgroup_mem(cg) if cg else ""
            if cg and mem:
                break
        else:
            cg = None
        self.kill_game_mb, self.kill_anon_swap_mb = kill_limits(cap if cap is not None else self.env_cap_mb)
        return (cg.name, mem) if cg else None

    def watch_scope(self, now: float, pids: list[int]) -> None:
        """Keeps the game scope's newest snapshot; once it is gone (no game, or another scope), logs its end."""
        snap = self.scope_snapshot(pids) if pids else None
        if self.scope is not None and ((pids and snap is None) or (snap and snap[0] != self.scope["unit"])):
            self.scope_ended(now, wait=False)    # a new game already: the old one's end with what the journal has
        if snap:
            self.scope = {"unit": snap[0], "t": now, "mem": snap[1]}
        elif self.scope is not None and not pids:
            self.scope_ended(now)

    def scope_ended(self, now: float, wait: bool = True) -> None:
        """Logs the ended scope's Result (user journal) and its last memory.events/memory.stat, once; waits up to
        SCOPE_END_WAIT_S for systemd's end lines."""
        sc = self.scope
        if sc is None:
            return
        lines = scope_journal(sc["unit"], self.run)
        result = scope_result(lines)
        if result is None and wait and now - sc.setdefault("gone", now) < SCOPE_END_WAIT_S:
            return
        self.scope = None
        msg = (f"game scope {sc['unit']} ended: Result={result or 'unknown (no end line in the user journal)'}; "
               f"last memory {sc.get('gone', now) - sc['t']:.0f} s before it was gone: {sc['mem']}; journal: {' | '.join(lines[-4:]) or 'none'}")
        self._write(msg)
        self.log(f"ramwatch: {msg}")

    def _append(self, path: Path, rec: dict) -> None:
        try:
            with path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def sample(self, now: float | None = None, rss: int | None = None, avail: int | None = None,
               pids: list[int] | None = None, anon_swap: int | None = None) -> dict | None:
        """One game RSS sample. Kills bg3 (SIGKILL) above kill_game_mb RSS or kill_anon_swap_mb anon+swap, or below
        KILL_AVAIL_MB available once no test run is left to kill; logs growth > SPIKE_MB within SPIKE_WINDOW_S (once per window). Returns the kill
        record ({"kind": "kill", …}), the spike record ({"kind": "spike", …}) or None."""
        now = time.time() if now is None else now
        if pids is None:
            pids = game_pids()
        if rss is None:
            rss = sum(rss_mb(p) for p in pids)
        if anon_swap is None:
            anon_swap = sum(anon_swap_mb(p) for p in pids)
        self.watch_scope(now, pids if rss > 0 else [])
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
        if rss >= self.kill_game_mb or avail < KILL_AVAIL_MB:
            if rss < self.kill_game_mb and now - self.tests_killed_at > 10:   # still short soon after: the game
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
        if anon_swap >= self.kill_anon_swap_mb:
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
        why = (f"game RSS {rss} MB >= {self.kill_game_mb} MB" if rss >= self.kill_game_mb
               else f"game anon+swap {anon_swap} MB >= {self.kill_anon_swap_mb} MB (RSS {rss} MB; the rest is swapped out)"
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

    def _thermal_writer(self) -> bool:
        """Holds runtime/thermal_log.lock (tried again each time while thermal_log.sh has it): one writer only."""
        if self.thermal_lock is None:
            self.thermal_lock = take_lock(self.thermal_lock_path)
        return self.thermal_lock is not None

    def thermal(self, now: float | None = None, s: dict | None = None, pids: list[int] | None = None) -> dict | None:
        """One thermal sample: the thermal.log line (when this watchdog is the writer, synced), then the heat kill:
        bg3 is SIGKILLed when the CPU >= kill_cpu_c or the GPU >= kill_gpu_c for kill_s. Returns the kill record."""
        now = time.time() if now is None else now
        pids = game_pids() if pids is None else pids
        s = thermal_sample(game_up=bool(pids)) if s is None else s
        if self._thermal_writer():
            try:
                self.thermal_log.parent.mkdir(parents=True, exist_ok=True)
                with self.thermal_log.open("a") as f:
                    f.write(thermal_line(s, datetime.fromtimestamp(now)) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass
        cpu, gpu = s.get("k10temp"), sample_gpu(s)
        self.heat.append((now, cpu, gpu))
        lim = self.limits
        hot_s = held_for(self.heat, over(lim["kill_cpu_c"], lim["kill_gpu_c"]))
        if not pids or hot_s < lim["kill_s"]:
            return None
        why = (f"CPU {cpu} C / GPU {gpu} C for {hot_s:.0f} s (heat kill at CPU >= {lim['kill_cpu_c']:g} C or "
               f"GPU >= {lim['kill_gpu_c']:g} C for {lim['kill_s']:g} s)")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        rec = {"kind": "heat_kill", "t": now, "why": why, "cpu_c": cpu, "gpu_c": gpu, "seconds": round(hot_s),
               "line": thermal_line(s, datetime.fromtimestamp(now)), "pids": pids,
               "tools": last_tools(now, self.actions), "bg3le": bg3le_crumbs(self.logs)}
        msg = (f"KILLED bg3 {pids} (SIGKILL): too hot: {why}. Last tool calls: "
               + (" | ".join(rec["tools"]) or "none in the last 2 min")
               + ". Last bg3le lines: " + (" ⏎ ".join(rec["bg3le"][-6:]) or "none"))
        self._write(msg)
        self.log(f"ramwatch: !!!!! {msg}")
        self._append(self.heat_kills, rec)
        self.heat.clear()
        return rec

    def loop(self, stop: threading.Event) -> None:
        next_tick = 0.0
        while not stop.is_set():
            try:
                if time.time() >= next_tick:
                    self.tick()
                    next_tick = time.time() + (LOW_INTERVAL_S if self.low else INTERVAL_S)
                if time.time() >= self.next_thermal:
                    self.next_thermal = time.time() + THERMAL_S
                    self.thermal()
                self.sample()
            except Exception as e:  # noqa: BLE001 - the watchdog never breaks the runner
                self._write(f"error: {e}")
            stop.wait(FAST_SAMPLE_S if self.fast else SAMPLE_S)


def take_lock(path: Path = RUNTIME / "ramwatch.lock"):
    """One watchdog per machine (the runner's thread or a standalone one). Returns the held file, or None."""
    f = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if f is not None:   # the thermal lock is tried every THERMAL_S while thermal_log.sh holds it
            f.close()
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
        print(thermal_line(thermal_sample()))
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
