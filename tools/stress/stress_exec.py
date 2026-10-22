"""Runs the stress plan's steps one at a time: guards (RAM, heat, a STOP file, the --hours deadline) before each,
the save COPY rule for probes and agent sessions, runtime/game.lock around everything that drives the game, and the
results written after every check (so a crash loses at most the check in flight, which then runs again).

Every outside effect is a small Protocol passed in; stress_io.py has the real ones, the tests fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

import stress_core as core
from stress_matrix import Check, Matrix, Row

EXIT_DONE, EXIT_STOPPED = 0, 3


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class Host(Protocol):
    def mem_available_gb(self) -> float: ...
    def cpu_temp_c(self) -> float | None: ...
    def stop_requested(self) -> bool: ...


class SaveError(Exception):
    pass


class Saves(Protocol):
    def copy(self, label: str, new_label: str) -> tuple[str, str]:
        """Copy the newest `<owner>-<id>__<label>` folder to `<owner>-<id>__<new_label>` (replacing an older copy),
        checked equal to the original. -> (copy folder, owner). SaveError when there's no such save."""
        ...


class LockBusyError(Exception):
    pass


class GameLock(Protocol):
    def hold(self) -> AbstractContextManager[None]:
        """runtime/game.lock for the block (waits its own limit, then LockBusyError)."""
        ...


@dataclass
class AccOutcome:
    items: list[dict] | None  # acceptance results.json rows; None = the window gave none
    where: str  # the live window's folder (evidence)
    error: str = ""


@dataclass
class ProbeOutcome:
    rc: int | None  # None = timed out
    output: str
    log: str
    error: str = ""  # could not start (missing script, the game would not come up)


@dataclass
class SessionOutcome:
    record: dict | None  # the run's runs.jsonl line
    facts: list[dict]
    where: str
    error: str = ""


LOAD_WAIT_S, LOAD_POLL_S = 300.0, 3.0
NOT_LOADED_SCREENS = (None, "main_menu", "loading")  # the mod's ui.state screen before a save is in play


def wait_loaded(
    server_ready: Callable[[], bool],
    screen: Callable[[], str | None],
    clock: Clock,
    timeout_s: float = LOAD_WAIT_S,
    poll_s: float = LOAD_POLL_S,
) -> tuple[bool, str]:
    """Until the launch's save is in play: the server context answers and the screen is no longer the main menu
    or the loading one (first_launch's READY is the menu: the -loadSaveGame load is still running). -> (loaded,
    what was seen and after how long)."""
    t0 = clock.now()
    while True:
        ready, scr = server_ready(), screen()
        waited = clock.now() - t0
        if ready and scr not in NOT_LOADED_SCREENS:
            return True, f"save loaded after {waited:.0f} s (server context up, screen {scr})"
        if waited >= timeout_s:
            seen = f"server context {'up' if ready else 'down'}, screen {scr}"
            return False, f"no save loaded after {timeout_s:.0f} s ({seen})"
        clock.sleep(poll_s)


class AccRunner(Protocol):
    def run(self, sections: tuple[str, ...]) -> AccOutcome: ...


class ProbeRunner(Protocol):
    def run(
        self, script: str, args: list[str], save_folder: str | None, out_dir: str, timeout_s: float
    ) -> ProbeOutcome: ...


class SessionRunner(Protocol):
    def run(self, scenario: dict, profile: str, max_cost: float | None, playthrough: str) -> SessionOutcome: ...


class Store(Protocol):
    def load(self) -> dict: ...
    def save(self, results: dict) -> None: ...


@dataclass
class Deps:
    clock: Clock
    host: Host
    saves: Saves
    lock: GameLock
    acc: AccRunner
    probe: ProbeRunner
    session: SessionRunner
    store: Store
    read_profile: Callable[[str], dict]
    render: Callable[[dict], None]  # writes docs/STRESS-TEST.md from the results
    say: Callable[[str], None] = print


@dataclass
class Opts:
    profile: str | None = None  # every agent check on this profile (else the check's, else the default)
    max_cost: float | None = None  # per agent session on a paid profile
    hours: float | None = None  # stop starting steps after this long
    probe_timeout_s: float = 1800
    stopped: str = field(default="", init=False)


class Runner:
    def __init__(self, m: Matrix, deps: Deps, opts: Opts):
        self.m, self.d, self.o = m, deps, opts
        self.results = deps.store.load()
        self.t0 = deps.clock.now()

    # ---------------------------------------------------------------- bookkeeping
    def start(self, checks: tuple[Check, ...]) -> None:
        for c in checks:
            prev = self.results["checks"].get(c.key) or {}
            self.results["checks"][c.key] = {
                "kind": c.kind,
                "verdict": "",
                "evidence": "",
                "state": "running",
                "attempts": prev.get("attempts", 0) + 1,
                "started": round(self.d.clock.now(), 1),
            }
        self.d.store.save(self.results)

    def record(self, c: Check, verdict: str, evidence: str) -> None:
        r = self.results["checks"].setdefault(c.key, {"kind": c.kind, "attempts": 1})
        if r.get("attempts", 1) > 1:
            evidence = f"{evidence} [attempt {r['attempts']}]"
        r.update(verdict=verdict, evidence=core.clip(evidence), state="done", ended=round(self.d.clock.now(), 1))
        self.d.store.save(self.results)
        self.d.say(f"  {c.key} {verdict}: {r['evidence']}")

    # ---------------------------------------------------------------- guards
    def why_stop(self) -> str:
        if self.d.host.stop_requested():
            return "STOP file"
        if self.o.hours is not None and self.d.clock.now() - self.t0 >= self.o.hours * 3600:
            return f"--hours {self.o.hours:g} used up"
        return ""

    def guards(self) -> str:
        """Wait (guard_poll_s at a time, up to guard_wait_s) until RAM and heat allow the next step; the reason
        when they never did."""
        df = self.m.defaults
        waited = 0.0
        while True:
            free, temp = self.d.host.mem_available_gb(), self.d.host.cpu_temp_c()
            bad = []
            if free < df["min_free_gb"]:
                bad.append(f"RAM {free:.1f} GB free < {df['min_free_gb']:g}")
            if temp is not None and temp > df["max_cpu_c"]:
                bad.append(f"CPU {temp:.0f} C > {df['max_cpu_c']:g}")
            if not bad:
                return ""
            if waited >= df["guard_wait_s"]:
                return "guard: " + ", ".join(bad) + f" after {waited:.0f} s"
            self.d.say(f"guard: {', '.join(bad)}: waiting {df['guard_poll_s']:g} s")
            self.d.clock.sleep(df["guard_poll_s"])
            waited += df["guard_poll_s"]

    # ---------------------------------------------------------------- steps
    def row(self, c: Check) -> Row:
        return self.m.by_id[c.row]

    def step_acc(self, step: core.Step) -> None:
        out = self.d.acc.run(step.sections)
        if out.items is None:
            for c in step.checks:
                self.record(c, "ERROR", f"acceptance gave no results: {out.error} ({out.where})")
            return
        for c in step.checks:
            self.record(c, *core.acc_verdicts((c,), out.items, out.where)[c.key])

    def start_save(self, row: Row) -> tuple[str, str] | None:
        """The copy of the row's save the check starts from; None for a new game. SaveError passes up."""
        return self.d.saves.copy(row.save, core.copy_label(row)) if row.save else None

    def step_probe(self, c: Check) -> None:
        row = self.row(c)
        try:
            with self.d.lock.hold():
                start = self.start_save(row)
                out = self.d.probe.run(
                    c.spec["script"],
                    list(c.spec.get("args") or []),
                    start[0] if start else None,
                    f"stress/probe/{c.key.replace('#', '-')}",
                    float(c.spec.get("timeout_s") or self.o.probe_timeout_s),
                )
        except (SaveError, LockBusyError) as e:
            self.record(c, "ERROR", f"{type(e).__name__}: {e}")
            return
        if out.error:
            self.record(c, "ERROR", f"{out.error} ({out.log})")
            return
        self.record(c, *core.probe_verdict(out.rc, out.output, out.log))

    def step_agent(self, c: Check) -> None:
        row = self.row(c)
        profile_path = self.o.profile or c.spec.get("profile") or self.m.defaults["profile"]
        try:
            profile = self.d.read_profile(profile_path)
        except (OSError, ValueError) as e:
            self.record(c, "ERROR", f"profile {profile_path}: {e}")
            return
        cap = core.cost_cap(c, self.m.defaults, profile, self.o.max_cost)
        attempt = self.results["checks"][c.key]["attempts"]
        try:
            with self.d.lock.hold():
                start = self.start_save(row)
                sc = core.resolve_scenario(row, c, self.m.defaults, start, cap)
                out = self.d.session.run(sc, profile_path, cap, f"{sc['name'].replace('_', '-')}-a{attempt}")
        except (SaveError, LockBusyError) as e:
            self.record(c, "ERROR", f"{type(e).__name__}: {e}")
            return
        if out.error:
            self.record(c, "ERROR", f"{out.error} ({out.where})")
            return
        self.record(c, *core.session_verdict(out.record, out.facts, c.spec.get("goal"), out.where))

    def run(self, steps: list[core.Step]) -> int:
        """Every step that can run; manual ones are only listed. EXIT_STOPPED when a guard, the STOP file or the
        deadline ended it early (re-run the same command to resume)."""
        for step in steps:
            if step.kind == "manual":
                c = step.checks[0]
                self.d.say(f"manual {c.key}: {c.spec['what']} (record: run.py --record {c.key} VERDICT EVIDENCE)")
                continue
            if step.kind == "skip":
                c = step.checks[0]
                self.record(c, "SKIP", f"probe not ready: {c.spec['why']}")
                continue
            why = self.why_stop() or self.guards()
            if why:
                self.o.stopped = why
                self.d.say(f"stopped before {step.kind} {', '.join(c.key for c in step.checks)}: {why}")
                self.d.render(self.results)
                return EXIT_STOPPED
            self.d.say(f"{step.kind}: {', '.join(c.key for c in step.checks)}")
            self.start(step.checks)
            if step.kind == "acc":
                self.step_acc(step)
            elif step.kind == "probe":
                self.step_probe(step.checks[0])
            else:
                self.step_agent(step.checks[0])
            self.d.render(self.results)
        self.d.render(self.results)
        return EXIT_DONE
