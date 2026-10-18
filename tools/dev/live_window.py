#!/usr/bin/env python3
"""The orchestrator's live window in one command (se's runbook, research/impl-se.md "live-window runbook").

    python3 tools/dev/live_window.py                          # every acceptance section, then stop the game
    python3 tools/dev/live_window.py --only se,act --keep-game
    python3 tools/dev/live_window.py --attach                 # a harness game already runs: reuse it (hot reload if mod/ is newer)
    python3 tools/dev/live_window.py --dry-run                # print every step, run nothing

Steps (each one a row of the final PASS/FAIL/SKIP table; the first FAIL before the acceptance stops the window):
  lock       runtime/game.lock, taken with a non-blocking flock and held to the end; refused while the continuous
             runner holds runtime/runner.lock or a worker holds game.lock (the holders' pids are printed). The fd is
             close-on-exec, and first_launch.sh closes fds > 2 before the launcher, so the game never inherits it
             (the 10:51 deadlock, docs/ORCHESTRATOR.md "flock -o"). The acceptance runs with BG3AI_GAME_LOCK_HELD=1,
             so its sections don't flock the file a second time.
  display    DISPLAY/XAUTHORITY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR from Xwayland when unset (runner/display_env.py)
  steam      tools/steam_paths.py launch-ok: Steam up and OFFLINE (exit 3 = not offline: stop)
  bg3le      tools/bg3le_buildinfo.py: the dist has every patch
  check      acceptance.py --check (offline: tools, RPC ops, Lua snippets, saves): must be 0 FAIL
  ramwatch   runner/ramwatch.py, started detached unless one holds runtime/ramwatch.lock (setup's ramwatch item needs it)
  launch     tools/dev/first_launch.sh --skip-shim -- --load <save> (runner/launch_game.sh: its own systemd scope);
             it waits for bg3le, the mod's sys.info, health and one frame. Refused beside a user's own BG3; a harness
             game already running needs --attach
  reload     only with --attach and mod/ newer than the game (or --reload): Ext.Debug.Reset() + two module checks
  acceptance acceptance.py [--only ...], output tee'd; per section its PASS/FAIL/SKIP counts, and the game's memory
             at the start and after each section (acceptance's memory.json: RSS, swap, its scope's current/peak/
             swap peak, anon vs file cache)
  collect    on a game death: the last bg3le/game log lines, crash lines, ram.log / ram_kills / ram_spikes tails,
             journalctl OOM lines, the launcher log
  stop       SIGTERM then SIGKILL this harness's bg3 + crash reporters only (runner/play.py is_harness_pid, RUN-4);
             a user's own BG3 is never signalled. Skipped with --keep-game

Everything goes to runtime/live/<stamp>/: window.log (all output), launch.log, acceptance.log, acceptance/ (the
section logs, results.json, table.md), death/ (on a crash), summary.md (the table + failures per owner, owner from
acceptance.SECTION_OWNERS). Exit code = number of FAILs (capped at 100).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runner"))
import display_env  # noqa: E402  (runner/display_env.py: shared with play.py run)
from display_env import xwayland_auth  # noqa: E402,F401

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SAVE = "Claude-4810126690__S027-start"   # acceptance.BASE's folder (acceptance --check proves it is the newest)
STOP_GRACE_S = 10      # bg3 ignores SIGTERM (docs/SETUP.md §6): SIGKILL after this
STOP_CONFIRM_S = 30
RELOAD_SETTLE_S = 20
UV_ARGS = ["run", "--project", "bridge"]
RELOAD_PY = ("from bg3ai import luadebug as L\nc=L.LuaDebugClient(); c.connect()\n"
             "try: c.evaluate('Ext.Debug.Reset() return 1', context='server')\nexcept Exception: pass\n")
# After a hot reload: (context flag, Lua) that must print "true" (the runbook's step 4).
RELOAD_CHECKS = [([], "return tostring(Mods.BG3AI.BG3AI.Hotbar ~= nil)"),
                 (["--server"], "return tostring(Mods.BG3AI.BG3AI.Actions.gone_why ~= nil)")]


def load_acceptance():
    """tools/dev/acceptance.py's module (stdlib-only at import: the server is imported inside a section)."""
    sys.path.insert(0, str(REPO / "tools" / "dev"))
    import acceptance
    return acceptance


def load_play():
    """runner/play.py's module, for its harness-only process logic (RUN-4)."""
    sys.path.insert(0, str(REPO / "runner"))
    import play
    return play


class Procs:
    """The process side, swapped for fakes in the tests: harness game pids, a user's own BG3, crash reporters."""

    def __init__(self):
        self._p = None

    @property
    def p(self):
        if self._p is None:
            self._p = load_play()
        return self._p

    def game(self) -> list[int]:
        return self.p.pids_of_game()

    def foreign(self) -> list[int]:
        return self.p.foreign_bg3_pids()

    def reporters(self) -> list[int]:
        return self.p.crash_reporter_pids()

    def steam_running(self) -> bool:
        """A Steam client process (launch_game.sh only warns without one; the game's libsteam_api then fails)."""
        return any(subprocess.run(["pgrep"] + a, capture_output=True).returncode == 0
                   for a in (["-x", "steam"], ["-f", "ubuntu12_32/steam"]))

    @staticmethod
    def _tools(name: str):
        sys.path.insert(0, str(REPO / "tools"))
        return __import__(name)

    def save_dir(self, save: str) -> Path:
        return self._tools("steam_paths").saves_dir() / save

    def steam_offline(self) -> tuple[bool, bool, str]:
        """(offline required?, offline?, why): tools/steam_paths.py's own reads (the dry run's probe)."""
        sp = self._tools("steam_paths")
        req, _ = sp.offline_required()
        off, why = sp.steam_offline()
        return req, off, why

    def bg3le_problems(self) -> list[str]:
        return self._tools("bg3le_buildinfo").problems()

    def kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def lock_holders(path: Path) -> list[int]:
    """Pids with an open fd on path (the flock's possible holders; only processes we may read)."""
    out = []
    try:
        target = path.resolve()
    except OSError:
        return out
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            for fd in (d / "fd").iterdir():
                try:
                    if Path(os.readlink(fd)) == target:
                        out.append(int(d.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(out)


def held_by_other(path: Path) -> bool:
    """Someone holds an flock on path (a missing file is free). Probes with a separate open: never keeps it."""
    if not path.exists():
        return False
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def find_uv(env: dict) -> str:
    """uv's path: on PATH, else ~/.local/bin/uv (a multiplexer/ssh shell may lack ~/.local/bin on PATH)."""
    found = shutil.which("uv", path=env.get("PATH"))
    if found:
        return found
    local = Path(env.get("HOME") or Path.home()) / ".local" / "bin" / "uv"
    return str(local) if local.exists() else "uv"


class Window:
    def __init__(self, a: argparse.Namespace, *, repo: Path = REPO, runtime: Path | None = None,
                 procs: Procs | None = None, env: dict | None = None, out: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, acceptance=None):
        self.a = a
        self.repo = repo
        self.runtime = runtime or repo / "runtime"
        self.procs = procs or Procs()
        self.env = dict(os.environ if env is None else env)
        self.sleep = sleep
        self.acc = acceptance or load_acceptance()
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = self.runtime / "live" / self.stamp
        self.rows: list[dict] = []          # harness steps: {step, verdict, evidence}
        self.items: list[dict] = []         # acceptance rows: {section, item, verdict, evidence[, owner]}
        self.memory: list[dict] = []        # acceptance's memory.json: the game's memory at the start, after each section
        self.lock_f = None
        self.logf = None
        self._out = out or (lambda s: print(s, flush=True))
        self.launched = False
        self.died = ""

    # ------------------------------------------------------------------ output
    def say(self, s: str) -> None:
        self._out(s)
        if self.logf:
            self.logf.write(s + "\n")
            self.logf.flush()

    def row(self, step: str, verdict: str, evidence: str) -> str:
        self.rows.append({"step": step, "verdict": verdict, "evidence": evidence})
        self.say(f"  {verdict}: {step}: {evidence}")
        return verdict

    def cmd(self, argv: list[str], log: Path | None = None, timeout: float | None = None,
            env: dict | None = None) -> tuple[int, str]:
        """Run argv in the repo, streaming its output to the window log (and log, a tee); (exit code, output).
        Exit 124 = timeout. The child never inherits the lock fd (close_fds, and the fd is close-on-exec)."""
        self.say("  $ " + shlex.join(argv))
        lines = []
        lf = open(log, "a") if log else None
        try:
            p = subprocess.Popen(argv, cwd=self.repo, env=env or self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace",
                                 close_fds=True, start_new_session=True)
        except OSError as e:
            if lf:
                lf.close()
            return 127, f"{argv[0]}: {e}"
        end = None if timeout is None else time.monotonic() + timeout
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        rc = None
        while True:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                rc = 124
                break
            if sel.select(timeout=min(1.0, left) if left is not None else 1.0):
                ln = p.stdout.readline()
                if ln == "":
                    rc = p.wait()
                    break
                ln = ln.rstrip("\n")
                lines.append(ln)
                self.say("    " + ln)
                if lf:
                    lf.write(ln + "\n")
                    lf.flush()
            elif p.poll() is not None and not sel.select(timeout=0):
                rc = p.returncode
                break
        sel.close()
        p.stdout.close()
        if lf:
            lf.close()
        return rc, "\n".join(lines)

    # ------------------------------------------------------------------ plan
    def sections(self) -> list[str]:
        names = [s for s in self.a.only.split(",") if s] if self.a.only else list(self.acc.SECTIONS)
        bad = [n for n in names if n not in self.acc.SECTIONS]
        if bad:
            raise SystemExit(f"unknown section(s) {bad}; sections: {' '.join(self.acc.SECTIONS)}")
        return names

    def commands(self) -> dict[str, list[str]]:
        UV = [find_uv(self.env)] + UV_ARGS
        acc = str(self.repo / "tools" / "dev" / "acceptance.py")
        launch = [str(self.repo / "tools" / "dev" / "first_launch.sh")]
        if self.a.attach:
            launch.append("--attach")
        if not self.a.shim:
            launch.append("--skip-shim")
        if not self.a.attach:
            launch += ["--", "--load", self.a.save] + [x for e in self.a.env for x in ("--env", e)]
        only = ["--only", ",".join(self.sections())] if self.a.only else []
        return {
            "steam": ["python3", str(self.repo / "tools" / "steam_paths.py"), "launch-ok"],
            "bg3le": ["python3", str(self.repo / "tools" / "bg3le_buildinfo.py")],
            "check": UV + ["--with", "lz4", "--with", "zstandard", "python", acc, "--check"],
            "ramwatch": ["python3", str(self.repo / "runner" / "ramwatch.py")],
            "launch": launch,
            "reload": UV + ["python", "-c", RELOAD_PY],
            "acceptance": UV + ["python", "-u", acc] + only,
        }

    def reload_plan(self, c: dict) -> str:
        if not (self.a.reload or self.a.attach):
            return "SKIP (a fresh launch runs the tree's Lua)"
        checks = "; ".join("luadebug " + shlex.join(f + [lua]) for f, lua in RELOAD_CHECKS)
        when = "" if self.a.reload else "only if mod/ is newer than the game: "
        return f"{when}{shlex.join(c['reload'])}; sleep {RELOAD_SETTLE_S}; {checks}   (each must print true)"

    def dry_run(self) -> int:
        c = self.commands()
        rl = self.runtime
        steps = [
            ("lock", f"flock -n {rl / 'game.lock'} (held to the end, close-on-exec); refuse if "
                     f"{rl / 'runner.lock'} is held (the continuous runner) or game.lock is held (a worker)"),
            ("display", "DISPLAY=:1 + XAUTHORITY=<Xwayland -auth> when unset (now: DISPLAY="
                        f"{self.env.get('DISPLAY') or 'unset'}, XAUTHORITY={self.env.get('XAUTHORITY') or 'unset'})"),
            ("steam", shlex.join(c["steam"]) + "   (exit 3 = Steam not offline: stop)"),
            ("bg3le", shlex.join(c["bg3le"]) + "   (stale dist: stop)"),
            ("check", "SKIP (--skip-check)" if self.a.skip_check else shlex.join(c["check"]) + "   (any FAIL: stop)"),
            ("ramwatch", f"unless {rl / 'ramwatch.lock'} is held: setsid {shlex.join(c['ramwatch'])} >> "
                         f"{rl / 'ramwatch.out'}"),
            ("launch", "refuse beside a user's own BG3 (foreign bg3 pids); "
                       + ("" if self.a.attach else "a harness game already up needs --attach; ")
                       + shlex.join(c["launch"]) + f"   (tee {self.dir / 'launch.log'})"),
            ("reload", self.reload_plan(c)),
            ("acceptance", f"{self.acc.LOCK_HELD_ENV}=1 " + shlex.join(c["acceptance"])
             + f"   (tee {self.dir / 'acceptance.log'}; sections {','.join(self.sections())})"),
            ("collect", f"on a game death: bg3le/game log tails, ram.log/ram_kills/ram_spikes tails, journalctl OOM "
                        f"lines -> {self.dir / 'death'}"),
            ("stop", "SKIP (--keep-game)" if self.a.keep_game else
             f"SIGTERM harness bg3 + crash reporters (is_harness_pid), SIGKILL after {STOP_GRACE_S} s; never a "
             "user's own BG3"),
            ("summary", f"PASS/FAIL/SKIP table + failures per owner -> {self.dir / 'summary.md'}"),
        ]
        now = self.state()
        self.say(f"live window (dry run): nothing is run. Output dir would be {self.dir}")
        for i, (name, what) in enumerate(steps, 1):
            self.say(f"{i:>2}. {name:<10} {what}")
            if name in now:
                self.say(f"    {'':<10} now: {now[name]}")
        blockers = [f"{k}: {v}" for k, v in now.items() if "WOULD REFUSE" in v]
        self.say("would refuse at: " + "; ".join(blockers) if blockers else "no blocker seen (read-only probes)")
        return 0

    def state(self) -> dict[str, str]:
        """Read-only probes for the dry run (no lock taken, nothing started): what the real run would meet."""
        now: dict[str, str] = {}

        def probe(name, fn):
            try:
                now[name] = fn()
            except Exception as e:  # noqa: BLE001 (a probe never breaks the dry run)
                now[name] = f"probe error {type(e).__name__}: {e}"

        def lock():
            out = []
            for f, who in (("runner.lock", "the runner"), ("game.lock", "a worker")):
                pids = lock_holders(self.runtime / f)
                if pids:
                    out.append(f"WOULD REFUSE if held: {f} open in pid(s) {pids} ({who})")
            return "; ".join(out) or "no process has runner.lock or game.lock open"

        def display():
            if self.env.get("DISPLAY") and self.env.get("XAUTHORITY"):
                return f"inherited DISPLAY={self.env['DISPLAY']} XAUTHORITY={self.env['XAUTHORITY']}"
            env = dict(self.env)
            display_env.fill(env, self.a.display)
            return (f"would use {display_env.describe(env)}" if env.get("XAUTHORITY")
                    else "WOULD REFUSE: no XAUTHORITY and no Xwayland -auth")

        def steam():
            req, off, owhy = self.procs.steam_offline()
            run = self.procs.steam_running()
            bad = (req and not off) or not run
            return (("WOULD REFUSE: " if bad else "") + ("Steam client running" if run else "no Steam client process")
                    + f"; offline={off} ({owhy[-80:]}); offline required={req}")

        def bg3le():
            bad = self.procs.bg3le_problems()
            return "WOULD REFUSE: stale: " + "; ".join(bad) if bad else "dist has every patch"

        def ramwatch():
            pids = lock_holders(self.runtime / "ramwatch.lock")
            return f"running (pid(s) {pids}): left alone" if pids else "not running: would start one"

        def launch():
            foreign, ours = self.procs.foreign(), self.procs.game()
            if foreign:
                return f"WOULD REFUSE: the user's own bg3 runs (pid(s) {foreign})"
            if ours and not self.a.attach:
                return f"WOULD REFUSE: a harness game runs (pid(s) {ours}): pass --attach"
            if self.a.attach and not ours:
                return "WOULD REFUSE: --attach, but no harness game runs"
            if self.a.attach:
                return f"would attach to pid(s) {ours}"
            save = self.procs.save_dir(self.a.save)
            uv = find_uv(self.env)
            return (("" if save.is_dir() else "WOULD REFUSE: no save folder ") + f"{save}; uv={uv}"
                    + ("" if Path(uv).is_absolute() else " (WARN: not found on PATH or ~/.local/bin)"))

        for name, fn in (("lock", lock), ("display", display), ("steam", steam), ("bg3le", bg3le),
                         ("ramwatch", ramwatch), ("launch", launch)):
            probe(name, fn)
        return now

    # ------------------------------------------------------------------ steps
    def take_lock(self) -> bool:
        self.runtime.mkdir(parents=True, exist_ok=True)
        runner_lock = self.runtime / "runner.lock"
        if held_by_other(runner_lock):
            return self.row("lock", "FAIL", f"the continuous runner holds {runner_lock} (pids "
                            f"{lock_holders(runner_lock) or '?'}): stop it first (tools/orchestrator/runner_lib.sh "
                            "stop_runner)") == "PASS"
        path = self.runtime / "game.lock"
        f = open(path, "a")   # Python fds are close-on-exec: no child (the game included) inherits the hold
        deadline = time.monotonic() + self.a.lock_wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    f.close()
                    return self.row("lock", "FAIL", f"{path} is held by pid(s) {lock_holders(path) or '?'} (a worker's "
                                    "live sequence?): retry later or pass --lock-wait S") == "PASS"
                self.sleep(2)
        self.lock_f = f
        return self.row("lock", "PASS", f"holding {path}") == "PASS"

    def display(self) -> bool:
        inherited = bool(self.env.get("DISPLAY") and self.env.get("XAUTHORITY"))
        display_env.fill(self.env, self.a.display)
        if inherited:
            return self.row("display", "PASS", f"DISPLAY={self.env['DISPLAY']} XAUTHORITY={self.env['XAUTHORITY']} "
                            "(inherited)") == "PASS"
        if not self.env.get("XAUTHORITY"):
            return self.row("display", "FAIL", "no XAUTHORITY and no Xwayland -auth found (is the desktop session "
                            "up? export DISPLAY/XAUTHORITY yourself)") == "PASS"
        return self.row("display", "PASS", f"DISPLAY={self.env['DISPLAY']} XAUTHORITY={self.env['XAUTHORITY']}") \
            == "PASS"

    def simple(self, step: str, argv: list[str], ok_text: str, fail_text: str, timeout: float) -> bool:
        rc, out = self.cmd(argv, timeout=timeout)
        last = out.strip().splitlines()[-1] if out.strip() else ""
        if rc == 0:
            return self.row(step, "PASS", last or ok_text) == "PASS"
        return self.row(step, "FAIL", f"exit {rc}: {fail_text}; {last[:300]}") == "PASS"

    def ramwatch(self) -> None:
        if held_by_other(self.runtime / "ramwatch.lock"):
            self.row("ramwatch", "PASS", "already running (runtime/ramwatch.lock held)")
            return
        argv = self.commands()["ramwatch"]
        self.say("  $ setsid " + shlex.join(argv) + f" >> {self.runtime / 'ramwatch.out'}")
        with open(self.runtime / "ramwatch.out", "a") as o:
            p = subprocess.Popen(argv, cwd=self.repo, env=self.env, stdout=o, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        self.sleep(2)
        if p.poll() is None:
            self.row("ramwatch", "PASS", f"started, pid {p.pid} (left running: it guards the next game too)")
        else:
            self.row("ramwatch", "FAIL", f"exited at once ({p.returncode}): see {self.runtime / 'ramwatch.out'}")

    def launch(self) -> bool:
        foreign = self.procs.foreign()
        if foreign:
            return self.row("launch", "FAIL", f"a bg3 this harness didn't start runs (pid(s) {foreign}: the user's "
                            "own BG3?): never launched beside it, never stopped") == "PASS"
        ours = self.procs.game()
        if ours and not self.a.attach:
            return self.row("launch", "FAIL", f"a harness game already runs (pid(s) {ours}): pass --attach to use it, "
                            "or stop it (kill -9 $(cat runtime/game.pid))") == "PASS"
        if self.a.attach and not ours:
            return self.row("launch", "FAIL", "--attach, but no harness game runs") == "PASS"
        if not self.a.attach:
            try:
                save = self.procs.save_dir(self.a.save)
            except Exception as e:  # noqa: BLE001 (no Larian profile found)
                return self.row("launch", "FAIL", f"can't find the saves folder: {e}") == "PASS"
            if not save.is_dir():
                return self.row("launch", "FAIL", f"no save folder {save} (--save takes the folder name)") == "PASS"
        rc, out = self.cmd(self.commands()["launch"], log=self.dir / "launch.log", timeout=self.a.launch_timeout)
        self.launched = not self.a.attach
        if rc == 0:
            return self.row("launch", "PASS", "attached" if self.a.attach else
                            f"game up on {self.a.save} (first_launch.sh: READY)") == "PASS"
        fail = [ln.strip() for ln in out.splitlines() if "FAIL" in ln]
        self.row("launch", "FAIL", f"first_launch.sh exit {rc}: {(fail[-1] if fail else out[-300:]).strip()[:300]}")
        if not self.procs.game():
            self.died = self.died or "launch"
        return False

    def mod_newer_than_game(self) -> bool:
        pidf = self.runtime / "game.pid"
        try:
            since = pidf.stat().st_mtime
        except OSError:
            return True
        return any(p.stat().st_mtime > since for p in (self.repo / "mod").rglob("*.lua"))

    def reload(self) -> bool:
        if not (self.a.reload or (self.a.attach and self.mod_newer_than_game())):
            self.row("reload", "SKIP", "fresh launch (the tree's Lua is loaded)" if not self.a.attach else
                     "mod/ is not newer than the running game")
            return True
        c = self.commands()
        self.cmd(c["reload"], timeout=120)
        self.sleep(RELOAD_SETTLE_S)
        bad = []
        for flags, lua in RELOAD_CHECKS:
            env = dict(self.env, PYTHONPATH=str(self.repo / "bridge" / "src"))
            rc, out = self.cmd(["python3", "-m", "bg3ai.luadebug"] + flags + [lua], timeout=60, env=env)
            if rc != 0 or out.strip().splitlines()[-1:] != ["true"]:
                bad.append(f"{lua!r} -> {out.strip()[-120:] or rc}")
        if bad:
            return self.row("reload", "FAIL", "after Ext.Debug.Reset(): " + "; ".join(bad)) == "PASS"
        return self.row("reload", "PASS", "Ext.Debug.Reset() + module checks true") == "PASS"

    def acceptance(self) -> None:
        out_dir = self.repo / "runtime" / "acceptance"
        results = out_dir / "results.json"
        start = time.time()
        env = dict(self.env, **{self.acc.LOCK_HELD_ENV: "1"})
        rc, out = self.cmd(self.commands()["acceptance"], log=self.dir / "acceptance.log",
                           timeout=self.a.acceptance_timeout, env=env)
        try:
            fresh = results.stat().st_mtime >= start - 1
            rows = json.loads(results.read_text()) if fresh else None
        except (OSError, ValueError):
            rows = None
        if rows is None:
            self.row("acceptance", "FAIL", f"exit {rc}: no fresh {results} (timeout {self.a.acceptance_timeout} s?): "
                     f"see {self.dir / 'acceptance.log'}")
            rows = []
        else:
            self.row("acceptance", "PASS" if rc == 0 else "FAIL", f"exit {rc} = FAILs; {len(rows)} items")
        self.items = rows
        for r in rows:
            if r.get("item", "").startswith("game alive after section") and r.get("verdict") == "FAIL":
                self.died = self.died or r.get("section", "?")
        if (self.launched or self.a.attach) and not self.procs.game():
            self.died = self.died or "acceptance"
        dst = self.dir / "acceptance"
        dst.mkdir(parents=True, exist_ok=True)
        for n in self.sections():
            for ext in ("log", "json"):
                f = out_dir / f"{n}.{ext}"
                if f.exists() and f.stat().st_mtime >= start - 1:
                    shutil.copy2(f, dst / f.name)
        for f in [results, out_dir / "table.md", out_dir / "memory.json"] + sorted(out_dir.glob("se-jump-probe-*.jpg")):
            if f.exists() and f.stat().st_mtime >= start - 1:
                shutil.copy2(f, dst / f.name)
        try:
            mem = out_dir / "memory.json"
            self.memory = json.loads(mem.read_text()) if mem.stat().st_mtime >= start - 1 else []
        except (OSError, ValueError):
            self.memory = []

    def collect(self) -> None:
        """A game death: the evidence se's runbook lists, into death/ (best effort, every source optional)."""
        d = self.dir / "death"
        d.mkdir(parents=True, exist_ok=True)
        logs = self.runtime / "logs"
        got = []

        def tail(src: Path, n: int, name: str | None = None) -> None:
            try:
                lines = src.read_bytes().decode(errors="replace").splitlines()[-n:]
            except OSError:
                return
            (d / (name or src.name)).write_text("\n".join(lines) + "\n")
            got.append(name or src.name)

        newest = lambda pat: max(logs.glob(pat), key=lambda p: p.stat().st_mtime, default=None)
        blog = newest("bg3le-*.log*")
        if blog:
            tail(blog, 300, "bg3le-tail.log")
            crash = [ln for ln in blog.read_bytes().decode(errors="replace").splitlines()
                     if re.search(r"crash: signal|WARNING: resolve|strict|guarded", ln)][-40:]
            (d / "bg3le-crash-lines.txt").write_text(f"{blog}\n" + "\n".join(crash) + "\n")
            got.append("bg3le-crash-lines.txt")
        for pat, n in (("game-*.log*", 200), ("first_launch-*.log", 100)):
            f = newest(pat)
            if f:
                tail(f, n)
        for f, n in ((self.runtime / "ram.log", 100), (self.runtime / "ram_kills.jsonl", 10),
                     (self.runtime / "ram_spikes.jsonl", 10), (self.runtime / "ramwatch.out", 50)):
            if f.exists():
                tail(f, n)
        try:
            r = subprocess.run(["journalctl", "--user", "--since", "-60min", "--no-pager"], capture_output=True,
                               text=True, timeout=30)
            j = [ln for ln in r.stdout.splitlines() if re.search(r"bg3ai-game|oom", ln, re.I)]
            (d / "journal-oom.txt").write_text("\n".join(j[-80:]) + "\n")
            got.append("journal-oom.txt")
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.row("collect", "PASS", f"the game died during {self.died}: {len(got)} files in {d} "
                 f"({', '.join(got) or 'no logs found'})")

    def stop(self) -> None:
        if self.a.keep_game:
            self.row("stop", "SKIP", "--keep-game: the game is left running")
            return
        foreign = self.procs.foreign()
        pids = self.procs.game() + self.procs.reporters()
        if not pids:
            self.row("stop", "PASS", "no harness game running" + (f"; left the user's bg3 {foreign} alone"
                                                                  if foreign else ""))
            return
        for sig, wait in ((signal.SIGTERM, STOP_GRACE_S), (signal.SIGKILL, STOP_CONFIRM_S)):
            for pid in self.procs.game() + self.procs.reporters():
                self.procs.kill(pid, sig)
            t = 0.0
            while t < wait and (self.procs.game() or self.procs.reporters()):
                self.sleep(1)
                t += 1
            if not (self.procs.game() or self.procs.reporters()):
                break
        left = self.procs.game() + self.procs.reporters()
        if left:
            self.row("stop", "FAIL", f"harness pid(s) {left} survive SIGKILL")
        else:
            self.row("stop", "PASS", f"stopped harness pid(s) {pids}" + (f"; left the user's bg3 {foreign} alone"
                                                                         if foreign else ""))

    # ------------------------------------------------------------------ summary
    def owner(self, r: dict) -> str:
        return r.get("owner") or self.acc.SECTION_OWNERS.get(r.get("section", ""), "orchestrator")

    def summary(self) -> str:
        out = [f"# Live window {self.stamp}", "", "| step | verdict | evidence |", "|---|---|---|"]
        out += [f"| {r['step']} | {r['verdict']} | {r['evidence'].replace('|', '/')[:300]} |" for r in self.rows]
        if self.items:
            out += ["", "| section | owner | PASS | FAIL | SKIP |", "|---|---|---|---|---|"]
            for n in self.sections():
                rs = [r for r in self.items if r.get("section") == n]
                c = {v: sum(r.get("verdict") == v for r in rs) for v in ("PASS", "FAIL", "SKIP")}
                out.append(f"| {n} | {self.acc.SECTION_OWNERS.get(n, '?')} | {c['PASS']} | {c['FAIL']} | {c['SKIP']} |")
        if self.memory:
            out += ["", ("Game memory (MB; scope = its bg3ai-game cgroup, MemoryMax 10G, page cache included: anon "
                         "growing section after section is a leak, file is cache)"), "",
                    "| after | RSS | swap | scope | scope peak | scope swap peak | anon | file |",
                    "|---|---|---|---|---|---|---|---|"]
            keys = ("rss", "swap", "cg_current", "cg_peak", "cg_swap_peak", "anon", "file")
            for m in self.memory:
                out.append(f"| {m.get('after', '?')} | " + " | ".join(str(m.get(k, "-")) for k in keys) + " |"
                           if "rss" in m else f"| {m.get('after', '?')} | no bg3 |" + " |" * (len(keys) - 1))
        fails = [r for r in self.items if r.get("verdict") == "FAIL"]
        harness_fails = [r for r in self.rows if r["verdict"] == "FAIL"]
        out += ["", "## Failures by owner", ""]
        if not fails and not harness_fails:
            out.append("none")
        by: dict[str, list[str]] = {}
        for r in fails:
            by.setdefault(self.owner(r), []).append(
                f"- [{r.get('section', '?')}] {r.get('item', '?')}: {str(r.get('evidence', ''))[:300]}")
        for r in harness_fails:
            by.setdefault("orchestrator", []).append(f"- [window] {r['step']}: {r['evidence'][:300]}")
        for o in sorted(by):
            out += [f"### {o} ({len(by[o])})"] + by[o] + [""]
        total = sum(r["verdict"] == "FAIL" for r in self.rows) + len(fails)
        out.append(f"\n{total} FAIL; output in {self.dir}")
        return "\n".join(out)

    def fails(self) -> int:
        return sum(r["verdict"] == "FAIL" for r in self.rows) + sum(r.get("verdict") == "FAIL" for r in self.items)

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if self.a.dry_run:
            return self.dry_run()
        self.sections()   # unknown names: refuse before anything
        self.dir.mkdir(parents=True, exist_ok=True)
        self.logf = open(self.dir / "window.log", "a")
        self.say(f"live window {self.stamp}: output in {self.dir}")
        try:
            if not self.take_lock():
                return self.finish()
            ok = self.display()
            c = self.commands()
            ok = ok and self.simple("steam", c["steam"], "Steam offline", "Steam is not offline (or not running): "
                                    "never launch online where Steam must stay offline", 60)
            if ok and not self.procs.steam_running():
                ok = self.row("steam", "FAIL", "no Steam client process: start Steam (offline) first; the game's "
                              "libsteam_api needs it (launch_game.sh would only warn)") == "PASS"
            ok = ok and self.simple("bg3le", c["bg3le"], "dist complete", "stale bg3le dist: rebuild "
                                    "(tools/build_bg3le.sh, game down)", 60)
            if ok and self.a.skip_check:
                self.row("check", "SKIP", "--skip-check")
            elif ok:
                ok = self.simple("check", c["check"], "0 FAIL", "acceptance.py --check FAILs: fix the script "
                                 "before spending a live window", 600)
            if not ok:
                return self.finish()
            self.ramwatch()
            if self.launch() and self.reload():
                self.acceptance()
            if self.died:
                self.collect()
            if self.launched or self.a.attach:
                self.stop()
            else:   # refused before launching (a user's BG3, a harness game without --attach): not ours to stop
                self.row("stop", "SKIP", "nothing was launched by this window")
            return self.finish()
        except KeyboardInterrupt:
            self.row("window", "FAIL", "interrupted (Ctrl-C)")
            if self.launched and not self.a.keep_game:
                self.stop()
            return self.finish()

    def finish(self) -> int:
        s = self.summary()
        self.say("\n" + s)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "summary.md").write_text(s + "\n")
        except OSError:
            pass
        if self.lock_f:
            self.lock_f.close()   # releases game.lock
            self.lock_f = None
        if self.logf:
            self.logf.close()
            self.logf = None
        return min(self.fails(), 100)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="acceptance sections, comma-separated (default: all)")
    ap.add_argument("--save", default=DEFAULT_SAVE, help="the save folder the game starts on")
    ap.add_argument("--attach", action="store_true", help="use the harness game that already runs")
    ap.add_argument("--reload", action="store_true", help="hot-reload the mod before the acceptance")
    ap.add_argument("--keep-game", action="store_true", help="leave the game running at the end")
    ap.add_argument("--shim", action="store_true", help="rebuild + test the input shim (default --skip-shim)")
    ap.add_argument("--skip-check", action="store_true", help="skip acceptance.py --check")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="game environment (launch_game.sh --env), e.g. BG3LE_UI_STRICT=1")
    ap.add_argument("--display", default=":1", help="DISPLAY when unset (the desktop's Xwayland)")
    ap.add_argument("--lock-wait", type=float, default=0, help="wait up to S s for game.lock (default: refuse)")
    ap.add_argument("--launch-timeout", type=float, default=900)
    ap.add_argument("--acceptance-timeout", type=float, default=5400)
    ap.add_argument("--dry-run", action="store_true", help="print every step, run nothing")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return Window(parse(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
