"""The bench's RunHost on the live game (Phase D.3): `bg3bench run --runner session` plays each job through here.

bg3bench.adapters.session_run.SessionRun calls `start(job, sc, run_dir)` before the run's one session and
`record(job, sc, run_dir, outcome)` after it; the harness never imports the bench, so the match is structural (a
HostStart has session_run.Start's fields). `sc` is the v2 scenario dict with its defaults (bg3bench.scenario.load).

start: a SessionRunner for the run directory (make_runner: the entry point's Runner, which brings the LSF reader),
the client's run files (pi's system text, the bg3 tool count, the manifest), the reset (saves from now on only; for a
save start the mod's nav_*.json moved into <run_dir>/reset/, a new game's host is a new uuid that has none), the game
at the scenario's start (its start save loaded, or a new game), the settings check and seed, and the kickoff for the
job's condition. The session's hooks are the runner's
own (RunnerSessionHooks), so crashes, hangs and caps behave as in a playthrough; SessionRun's Recovery(max_resumes=0)
makes a fault end the run invalid. Their stop check is the run's own (BenchHooks): an agent that ends its turn
before the run is decided is resumed (-cN), as the kickoff says the run ends only once decided or at its time cap; a
goal run at most Recovery.max_stop_resumes times, a maximise run (no count) as long as the process that just ended
made an acting call (acted_since).
A job carries the bench's live verdict (job["verdict"]): GoalWatch asks it while the run plays and ends the run once
it says passed or failed (a goal scenario) or wiped (any run: the game's Game Over). SaveWatch ends a run whose game
loads a save that is not its start save or its own (the main menu's Continue, bench B3): the run is invalid.

record: facts.jsonl's run_end, the game stopped (the next run starts a fresh process), the moved nav files put back
(the run's own versions kept in <run_dir>/reset/run/; a failed start puts them back too, and a marker,
runtime/nav-reset.json, lets the next start restore what a run that died left moved), the saves the run wrote moved
out of the shared Story folder into <run_dir>/saves/ (abort and a failed start too; runtime/saves-isolate.json for a
run that died), and the run's fields: the
outcome (the bench grades facts.jsonl; the harness decides no goal: a run the live verdict ended is "reached"
or "failed" by that verdict, a wipe "failed"), cost and tokens of every play process, wall
time, the settings readout, the manifest fields, the reset and the pins (machine_pins: the game build, bg3le, the mod
tree and the repo the run started with; the bench adds the versions).

stop: an operator stop (play.py bench's first SIGINT/SIGTERM, or runtime/STOP, which the runner reads) ends the run's
session at the runner's next check (reason "stopped"); record then writes it as an invalid run (`operator: stop`) with
the game stopped, and no new run starts. abort (a second signal, or an exception out of the matrix) kills the play
process and the game of a run still open and puts its nav files back, without a record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from bg3ai import metrics as bmetrics
from bg3ai import saves as bsaves

import bg3harness
from bg3harness import bg3le_buildinfo, client_setup, game_fingerprint, runs, spend, steam_paths
from bg3harness import pins as hpins
from bg3harness import preflight as hpreflight
from bg3harness import recorder as hrec
from bg3harness.game.io import Save
from bg3harness.runner import agent_group
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import SessionHooks, SessionOutcome

SID = "S001"  # a bench run is one session
SCENARIO_DIFFICULTY = "BG3AI_SCENARIO_DIFFICULTY"  # bg3ai.tools.newgame.SCENARIO_DIFFICULTY_ENV
GOAL_HIT = "goal"  # until_hit's id when the bench's live verdict decided a goal scenario (GoalWatch)
GOAL_POLL_S = 10.0  # how often GoalWatch asks the verdict: the stop comes one poll + the in-flight tool call late
FOREIGN_SAVE = "foreign_save"  # until_hit's id when SaveWatch saw the game load a save that isn't the run's
LOAD_GRACE_S = 60.0  # SaveWatch: a load_game logged this long after its new server boot was seen still explains it
RECORD_ENV = "BG3AI_BENCH_RECORD"  # 0: play.py bench records no run (bench_recording)
RECORD_ENCODER_ENV = "BG3AI_BENCH_RECORD_ENCODER"  # the recording's encoder (default h264_nvenc; libx264 on the CPU)
NO_MEMORY = "memory"  # the bridge's memory tool group (bg3ai.capabilities.GROUPS): off in every bench run
Json = dict[str, Any]


class RunState(Protocol):
    """The runner's runner.json (bg3harness.runner.state.RunnerState) and its run directory."""

    dir: Path

    def __getitem__(self, key: str, /) -> Any: ...
    def __setitem__(self, key: str, value: Any, /) -> None: ...


class FactsMarks(Protocol):
    """The runner's facts.jsonl writer (bg3harness.facts.FactsWriter): its marks."""

    def mark(self, mark: str, detail: Any = None, now: float | None = None) -> Any: ...


class RunRunner(Protocol):
    """What the host drives of a SessionRunner (make_runner's result)."""

    run_started: float | None
    settles_budget: bool  # False here: SessionRun's matrix adds the run's cost to budget.json
    stop_ends_session: bool  # True here: an operator stop ends the run's one session at the next check
    stop_requested: bool
    until_hit: Json | None  # GoalWatch sets it: the agent loop stops on it as on an until milestone
    run_env: dict[str, str] | None  # the job's env (the play process, its bg3 server, the play settings)

    @property
    def opt(self) -> rstate.Options: ...
    @property
    def state(self) -> RunState: ...
    @property
    def cap_hit(self) -> Json | None: ...
    @property
    def mod_rev(self) -> str | None: ...

    def event(self, kind: str, **fields: Any) -> None: ...
    def refresh_mod_snapshot(self) -> list[str]: ...
    def ensure_game(self, first: bool) -> str | None: ...
    def hot_reload_mod(self) -> int: ...
    def apply_play_settings(self) -> None: ...
    def check_settings(self) -> None: ...
    def session_hooks(self, sid: str, minutes: float) -> SessionHooks: ...
    def end_facts(self, outcome: str) -> None: ...
    def facts_writer(self) -> FactsMarks: ...
    def facts_now(self) -> None: ...
    def server_boot(self) -> str | None: ...
    def scenario_undo(self) -> None: ...
    def end_run_game(self, when: str = ..., after: str = ...) -> None: ...
    def kill_agent(self) -> None: ...


@dataclass(frozen=True)
class HostStart:
    """bg3bench.adapters.session_run.Start's fields."""

    prompt: str
    hooks: SessionHooks
    client_session: str | None = None
    prompt_sha: str | None = None
    prompt_parts: Sequence[Json] = field(default_factory=tuple[Json, ...])
    harness_version: str | None = None
    tools: Json | None = None
    client_version: str | None = None


def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """The reset's nav_state "archive": the mod's machine-global nav_*.json (kill lists, breadcrumb mirrors) moved
    into dest, so the run starts with none from an earlier game (bg3bench.resets.archive_nav_state's rule)."""
    moved: list[str] = []
    try:
        files = sorted(p for p in se_bg3ai.glob("nav_*.json") if p.is_file())
    except OSError:
        return moved
    for p in files:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except OSError:
            continue
    return moved


SE_RUN_FILES = ("nav_*.json", "scenario.json")  # what the mod writes in the shared Script Extender/BG3AI folder


def se_files(se_bg3ai: Path) -> list[str]:
    """The names of the mod's files (SE_RUN_FILES) in the shared folder now."""
    try:
        return sorted({p.name for pat in SE_RUN_FILES for p in se_bg3ai.glob(pat) if p.is_file()})
    except OSError:
        return []


def isolate_nav(se_bg3ai: Path, dest: Path, marker: Path, archive: bool) -> list[str]:
    """A run's nav reset. The marker, written first, names the mod's files the shared folder held before the run, so
    restore_nav can move the run's own out after it (live9: a new game left its nav files and scenario.json), even
    after a crash. A save start (archive) also moves every nav_*.json into dest (archive_nav_state). The decision of
    2026-10-22 03:30 is to move only the start host's two (bg3bench.resets.isolate_nav); that needs the save's host
    uuid, which tools/saves/inspect.py doesn't report yet, so all of them go (and all come back). A new game moves
    none: its host is a new uuid. Returns the moved names."""
    before = se_files(se_bg3ai)
    names = [n for n in before if n.startswith("nav_")] if archive else []
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"se": str(se_bg3ai), "dest": str(dest), "files": names, "before": before}))
    return archive_nav_state(se_bg3ai, dest) if names else []


def restore_nav(marker: Path) -> list[str]:
    """After the run (or a failed start, or at the next start after a crash): the marker's files go back from dest;
    a file of the same name the run wrote meanwhile is kept in <dest>/run/, and so is every file of the mod's that
    wasn't there before the run. Idempotent; the marker goes last. No marker: nothing was moved, []."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    se, dest = Path(m["se"]), Path(m["dest"])
    back: list[str] = []
    for n in m["files"]:
        if not (dest / n).is_file():
            continue
        if (se / n).exists():
            (dest / "run").mkdir(parents=True, exist_ok=True)
            shutil.move(str(se / n), str(dest / "run" / n))
        shutil.move(str(dest / n), str(se / n))
        back.append(n)
    before: list[str] | None = m.get("before")  # an older marker has no list: it can't tell the run's files
    for n in [] if before is None else [n for n in se_files(se) if n not in before]:
        (dest / "run").mkdir(parents=True, exist_ok=True)
        shutil.move(str(se / n), str(dest / "run" / n))
    marker.unlink(missing_ok=True)
    return back


def nav_marker() -> Path:
    return renv.RUNTIME / "nav-reset.json"


def saves_marker() -> Path:
    return renv.RUNTIME / "saves-isolate.json"


def mark_run_saves(marker: Path, story: Path, dest: Path, after: float, start_save: str | None) -> None:
    """At a run's start: which saves will be the run's (isolate_saves), written before the game loads anything, so
    a run that dies without a record is isolated by the next start."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"story": str(story), "dest": str(dest), "after": after, "start": start_save}))


def isolate_saves(marker: Path) -> list[str]:
    """At a run's end (record, abort, a failed start, or the next start after a run that died): every save folder the
    run wrote into the shared Story folder (saved since its start, `after`, by the time its meta.lsf holds; its start
    save never) moves into its run dir (`dest`), so no later run reaches it (bench B3: the main menu's Continue loaded
    act1_level_3's S001 save in a Balthazar run). Moved, never deleted. A folder's mtime says nothing: Steam's local
    cloud restores the moved saves at each launch with mtime = the launch (B3 job 9 moved 33 such copies, 523 MB);
    a restored copy keeps its own SaveTime and stays where it is. Idempotent; the marker goes last. No marker: []
    (nothing to do)."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    story, dest = Path(m["story"]), Path(m["dest"])
    moved: list[str] = []
    try:
        folders = sorted(d for d in story.iterdir() if d.is_dir() and d.name != m.get("start"))
    except OSError:
        folders = []
    for d in folders:
        try:
            saved = bsaves.saved_at(d)
            if saved is None or saved < m["after"]:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest / d.name))
            moved.append(d.name)
        except OSError as e:
            renv.log(f"save isolation: {d.name} not moved: {e}")
    marker.unlink(missing_ok=True)
    return moved


def own_save(name: str, saves: Sequence[Save], after: float, start_save: str | None) -> bool:
    """A save (folder or label) is the run's own: its start save, or one saved since the run started (`after`, by the
    time the save holds, Save.saved; bg3ai.tools.saving's SAVES_AFTER rule). Never by mtime: a restored copy of an
    earlier run's save (isolate_saves) has mtime = the launch."""
    return any(
        name in (s.folder, s.label) and (s.folder == start_save or (s.saved is not None and s.saved >= after))
        for s in saves
    )


LOADED_RE = re.compile(r"load_game: done — loaded=(\S+)")


def agent_loads(since: float) -> list[str]:
    """What the agent's load_game calls loaded since `since` (actions.jsonl), oldest first."""
    actions: list[Json] = rpolicy.read_actions_since(since)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return [
        m.group(1)
        for a in actions
        if a.get("tool") == "load_game" and (m := LOADED_RE.match(str(a.get("result") or "")))
    ]


def stop_file() -> Path:
    """runtime/STOP: the operator's stop file (a playthrough stops after its session, a bench run at once)."""
    return renv.RUNTIME / "STOP"


def stop_on_signals(host: LiveRunHost, fd: int = 2) -> None:
    """SIGINT/SIGTERM for play.py bench: the first one is host.stop() (the run playing now ends and is recorded, no new
    run starts), a second one raises KeyboardInterrupt (the caller's finally calls host.abort()).

    The handler only sets flags and says so with one os.write to `fd` (stderr): a print or a logging call there
    re-enters the buffered stream the main thread may be writing ("reentrant call inside BufferedWriter", the merge
    suite under load). The runner's agent loop logs the stop when it acts on it."""
    seen: list[int] = []

    def on_signal(signum: int, _frame: object) -> None:
        seen.append(signum)
        if len(seen) > 1:
            os.write(fd, f"signal {signum} again: exiting now; the run is not recorded\n".encode())
            raise KeyboardInterrupt
        host.stop()
        os.write(
            fd, f"signal {signum}: the run playing now ends and is recorded (send again to exit at once)\n".encode()
        )

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)


class GoalWatch:
    """A run's live verdict (SessionRun's job["verdict"]: "passed", "failed", "wiped" or None) asked every poll_s
    while the run plays. Once it decides, the runner's until_hit is set to {"id": GOAL_HIT, "verdict": ...}: the
    agent loop stops the agent on it as on an until milestone (a goal run never plays on past its grade, and no run
    plays on past a party wipe, the game's Game Over: bench B3 resumed a wiped Balthazar run 6 times). check() is one
    poll (the tests call it directly)."""

    def __init__(self, r: RunRunner, verdict: Callable[[], str | None], poll_s: float = GOAL_POLL_S):
        self.r, self.verdict, self.poll_s = r, verdict, poll_s
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="goal-watch", daemon=True)

    def check(self) -> bool:
        """True once the run is decided (by this verdict or an earlier stop): nothing left to watch."""
        if self.r.until_hit:
            return True
        try:
            v = self.verdict()
        except Exception as e:  # a verdict that can't be read doesn't end the run
            renv.log(f"goal verdict failed: {type(e).__name__}: {e}")
            return False
        if v not in ("passed", "failed", "wiped"):
            return False
        label = "the party was wiped (Game Over)" if v == "wiped" else f"the goal {v}"
        self.r.until_hit = {"id": GOAL_HIT, "label": label, "verdict": v, "session": SID, "t": time.time()}
        renv.log(f"{SID}: {label}: stopping the run")
        self.r.event("goal_verdict", verdict=v, session=SID)
        return True

    def _loop(self) -> None:
        while not self.done.wait(self.poll_s):
            if self.check():
                return

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.done.set()


class SaveWatch:
    """The save a run plays, polled every poll_s: a run plays only from its start save and its own saves (own_save).
    A new server boot (the runner's server_boot: only a load builds one) is a load: it is the run's when the agent's
    load_game (`loads`, actions.jsonl) loaded one of its own saves, logged up to grace_s after the new boot was seen
    (the tool answers once the load is done). Any other load (the main menu's Continue or Load page, a new game)
    can't be told to be the run's: the run leaves its scenario (bench B3: Game Over -> Main Menu -> Continue loaded
    another run's save and the record stayed valid). That sets `foreign`, stops the agent (until_hit FOREIGN_SAVE,
    unless a verdict stopped it first) and record makes the run invalid. check(final=True), at record, decides a
    pending load at once."""

    def __init__(
        self,
        r: RunRunner,
        own: Callable[[str], bool],
        boot: Callable[[], str | None] | None = None,
        loads: Callable[[float], list[str]] = agent_loads,
        clock: Callable[[], float] = time.time,
        poll_s: float = GOAL_POLL_S,
        grace_s: float = LOAD_GRACE_S,
    ):
        self.r, self.own, self.boot, self.loads, self.clock = r, own, boot or r.server_boot, loads, clock
        self.poll_s, self.grace_s = poll_s, grace_s
        self.known: str | None = None  # the boot of the run's game (its start, or its own last load)
        self.since = clock()  # when it was known: a later load_game explains a new one
        self.new: str | None = None  # a new boot seen, not explained yet
        self.seen_at: float | None = None
        self.foreign: str | None = None  # why the run left its saves
        self.reloads = 0  # the loads seen after the start (the agent's own and a foreign one): the record's reloads
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="save-watch", daemon=True)

    def check(self, final: bool = False) -> bool:
        """One poll; True once the run left its saves."""
        if self.foreign is not None:
            return True
        b, now = self.boot(), self.clock()
        if self.known is None:
            self.known = b
        elif b is not None and b != self.known and b != self.new:
            self.new, self.seen_at = b, self.seen_at or now
        if self.seen_at is None:
            return False
        loaded = self.loads(self.since)
        if loaded and self.own(loaded[-1]):
            self.known, self.since, self.new, self.seen_at = self.new, now, None, None
            self.reloads += 1
            self.r.event("load", save=loaded[-1], session=SID)
            self.r.facts_writer().mark("load", loaded[-1])
            return False
        if loaded:
            why = f"the agent's load_game loaded {loaded[-1]}, which is not the run's start save or its own save"
        elif final or now - self.seen_at >= self.grace_s:
            why = "the game loaded a save the harness didn't (the main menu's Continue or Load page, or a new game)"
        else:
            return False
        self.foreign = why
        self.reloads += 1
        renv.log(f"{SID}: {why}: stopping the run (invalid: {FOREIGN_SAVE})")
        self.r.event(FOREIGN_SAVE, why=why, session=SID)
        self.r.facts_writer().mark("load", {"save": loaded[-1] if loaded else None, FOREIGN_SAVE: why})
        if not self.r.until_hit:
            self.r.until_hit = {"id": FOREIGN_SAVE, "label": why, "session": SID, "t": now}
        return True

    def _loop(self) -> None:
        while not self.done.wait(self.poll_s):
            if self.check():
                return

    def start(self) -> None:
        self.check()  # the start's boot, before the agent plays
        self.thread.start()

    def stop(self) -> None:
        """Stops polling and waits for a poll in flight, so record's check(final=True) never decides (and counts,
        and marks) the load that poll is deciding."""
        self.done.set()
        if self.thread.is_alive():
            self.thread.join()


def machine_pins(env: Mapping[str, str]) -> Json:
    """The game build, bg3le, mod and repo pins of a run starting now (bg3harness.pins.run_pins), read as the legacy
    series path reads them: the Steam buildid, the bg3le build the launcher loads ($BG3AI_BG3LE_LIB), the mod tree
    the runner's game loads (codewatch.MOD_LIVE) with the repo mod's version, and the repo. A game dir whose files match
    the pinned build's fingerprint (a download_depot copy) plays the pinned build whatever its appmanifest says;
    game_build_by says which of the two told the build."""
    lib = Path(env.get("BG3AI_BG3LE_LIB") or bg3le_buildinfo.DIST / "libbg3le.so")
    meta = rcode.MOD_DIR / "Mods" / "BG3AI" / "meta.lsx"
    fingerprints = renv.REPO / "tools" / "dev" / "game_fingerprints.json"
    pinned = hpreflight.pinned_build(renv.REPO)
    build, how = game_fingerprint.installed_build(
        pinned, steam_paths.game_dir(env), steam_paths.buildid(env), fingerprints
    )
    pins = hpins.run_pins(renv.REPO, build, lib.parent, meta, rcode.mod_tree_hashes())
    return {**pins, "game_build_by": how}


def bench_recording(env: Mapping[str, str]) -> Callable[[Path], hrec.RunRecording] | None:
    """play.py bench records every run (recorder.run_recording, the game's window on env's DISPLAY);
    BG3AI_BENCH_RECORD=0 turns it off. BG3AI_BENCH_RECORD_ENCODER picks the encoder (a machine without NVENC sets
    libx264; bg3harness.doctor checks the one set); an unknown one is refused here, before any run."""
    if env.get(RECORD_ENV, "1") == "0":
        return None
    encoder = bench_encoder(env)
    return lambda out: hrec.run_recording(out, env, renv.RUNTIME, encoder)


def bench_encoder(env: Mapping[str, str]) -> str:
    encoder = env.get(RECORD_ENCODER_ENV) or "h264_nvenc"
    if encoder not in hrec.ENCODER_ARGS:
        raise ValueError(f"{RECORD_ENCODER_ENV}={encoder!r}: one of {sorted(hrec.ENCODER_ARGS)}")
    return encoder


def owner_of(sc: Json) -> str:
    """The save owner: the scenario's character (a save start fixes it), else the new game's name."""
    return (sc["profile"]["character"].get("name") or "Tav").strip()


def options_for(job: Json, sc: Json, run_name: str, files: Json) -> rstate.Options:
    """One bench run's Options: one session of the scenario's run_hours, no keeper, no judge, the start save (none
    for a new game), the dice seed and the settings the check compares. An `until` scenario's run ends at its
    milestone (the runner stops the agent once the progress poller sees it: until_hit). A save start's avatar is
    renamed to the scenario's character at the run's start (scenario_settings avatar_name). `files`:
    client_setup.run_files' fields. The job's max_cost (the matrix's [budget]: a paid run's cap) caps a paid client
    only, as a subscription run is never charged (fixlive: a Claude Code nautiloid run ended at its first end_turn on
    the matrix's $0.25 DeepSeek cap, notional $2.12); the scenario's own caps.max_cost caps every client."""
    caps, char, gs = sc["caps"], sc["profile"]["character"], sc["game_settings"]
    paid_cap = job.get("max_cost") if spend.is_paid(job["client"]) else None
    max_cost = paid_cap if paid_cap is not None else caps.get("max_cost")
    return rstate.Options(
        run_name,
        None,
        caps["run_hours"] * 60,
        job["model"],
        save_owner=owner_of(sc),
        effort=job.get("effort"),
        allow_short=True,
        keeper=False,
        until=sc.get("until"),
        character_brief=char.get("brief"),
        roleplay=char.get("roleplay"),
        difficulty=gs.get("difficulty"),
        run_hours=caps["run_hours"],
        max_cost=max_cost,
        max_tokens=caps.get("max_tokens"),
        scenario=sc["name"],
        start_save=sc["start"].get("save"),
        client=job["client"],
        seed=gs.get("seed"),
        scenario_settings={
            "difficulty": gs.get("difficulty"),
            "karmic_dice": gs.get("karmic_dice"),
            "timing": sc.get("timing"),
            "avatar_name": owner_of(sc) if sc["start"]["kind"] == "save" else None,
        },
        **files,
    )


def goal_of(sc: Json) -> Json:
    """The scenario's goal predicate: an `until` milestone (nautiloid) is {"milestone": it} (as bg3bench.scenario's
    goal_of; the harness never imports the bench). A maximise scenario has none (maximised)."""
    return {"milestone": sc["until"]} if "until" in sc else sc["goal"]


def maximised(sc: Json) -> str | None:
    """The fact a maximise scenario scores the gain of (bg3bench.scenario's maximised), None for a goal scenario:
    such a run has no pass and plays its whole time cap."""
    return sc["maximise"]["fact"] if "maximise" in sc else None


def scenario_kickoff(sc: Json, sid: str, loaded: str | None, pace: str = "") -> str:
    """A bench run's first message: the task and how it is scored (the scenario's `task` and `scored_by`, first),
    when the run ends (once graded, or at the time cap), its start (`loaded`: the label of the save the harness
    loaded), the grader's exact goal (graders.grade: passed at the first instant it holds, failed by a "never" that
    happened before; a maximise scenario's score instead: the fact's gain at the time cap), the character. No memory
    or playbook steps (a bench run has no playthrough memory) and no "play until WRAP UP NOW": the run ends on its
    verdict (an until milestone, or GoalWatch), a maximise run at its time cap. `pace`: the coaching condition's
    pre-flight ("" under rules)."""
    char, start = sc["profile"]["character"], sc["start"]
    note: Json = start.get("note") or {}
    task = sc.get("task") or note.get("objective") or "Make the goal below hold."
    cap = round(sc["caps"]["run_hours"] * 60)
    lines = [
        f"This is a benchmark run of the scenario '{sc.get('title') or sc['name']}' ({sc['name']}), session "
        f"{sid}, started by the harness.",
        f"YOUR TASK: {task}",
    ]
    if sc.get("scored_by"):
        lines.append(f"How it is scored: {sc['scored_by']}")
    fact = maximised(sc)
    if fact is None:
        lines.append(
            f"The run ends as soon as the task is decided (passed, or it can no longer pass) or after {cap} minutes "
            "of play (the time cap), whichever comes first."
        )
    else:
        lines.append(
            f"The run has no pass: it plays all {cap} minutes of its time cap, then it is scored. A party wipe (the "
            "Game Over screen) ends it sooner."
        )
    if start["kind"] == "save":
        lines.append(
            f'The harness has loaded save "{loaded}": the character, party, gear and '
            "journal are the save's. Don't reload it; verify state with get_status. Never start a new game."
        )
        lines += [f"{k.capitalize()}: {note[k]}" for k in ("where", "objective", "party", "journal") if note.get(k)]
    else:
        lines.append(rpolicy.new_game_note(owner_of(sc)))
    since = "the start save" if start["kind"] == "save" else "the new game"
    if fact is None:
        lines.append(
            f"The grader's exact goal: the run passes the first moment it holds, from {since} on; anything under "
            f'"never" fails it if it happens first, and so does a party wipe (the Game Over screen): '
            f"{json.dumps(goal_of(sc), sort_keys=True)}."
        )
    else:
        lines.append(
            f"The grader's exact score: the party's {fact} when the run ends minus its {fact} at {since}; "
            "higher is better."
        )
    if char.get("brief"):
        lines.append("Character brief:\n> " + char["brief"].strip().replace("\n", "\n> "))
    lines.append(
        rpolicy.ROLEPLAY_TEXT[char.get("roleplay") if char.get("roleplay") in rpolicy.ROLEPLAY_TEXT else "strict"]
    )
    lines.append("Nobody will answer questions: decide yourself.")
    if pace:
        lines.append(pace)
    return "\n\n".join(lines)


class BenchHooks:
    """The runner's session hooks for a bench run, except that a play process that can't start (the client's binary
    missing, a leftover agent that survived SIGKILL: OSError, RuntimeError) ends the session as a `setup_failed`
    result instead of raising out of the matrix (H3: the run then went unrecorded with its game up). record lists it
    as a harness failure, so the run is invalid and the matrix reruns it. A playthrough's run loop keeps the raise
    (it counts setup failures and recovers).

    The stop check is `undecided` (the run's: undecided_why), not the runner's, which is the playthrough memory's
    wrap-up and has nothing to say in a bench run: fixlive D4's agent ended its turn at the Nautiloid's loading
    screen, 7.9 of 240 min, before the milestone, and the run ended there. It gets the session end and when the play
    process that just ended started (`since`: what it did is what the agent did since its last resume)."""

    def __init__(
        self,
        hooks: SessionHooks,
        event: Callable[..., None],
        undecided: Callable[[float, float], str | None] = lambda _end, _since: None,
    ):
        self.hooks, self.event, self.undecided = hooks, event, undecided
        self.since = 0.0  # when the latest play process started

    def stop_check(self, session_end: float) -> str | None:
        return self.undecided(session_end, self.since)

    def run_agent(self, prompt: str, minutes: float, client_session: str | None, resume: bool, tag: str) -> Any:
        self.since = self.hooks.now()
        try:
            return self.hooks.run_agent(prompt, minutes, client_session, resume, tag)
        except (OSError, RuntimeError) as e:
            detail = f"{type(e).__name__}: {e}"[:300]
            self.event("setup_failed", session=SID + tag, error=detail)
            now = self.hooks.now()
            return rstate.SessionResult(SID, client_session or "", None, "setup_failed", now, now, detail=detail)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.hooks, name)


def acted_since(since: float) -> bool:
    """Whether the agent made an acting call since `since`: a tool call (actions.jsonl) that issues a party action,
    as the session metrics count them (bg3ai.metrics.ISSUE_TOOLS: action tools, batches, queue_actions; not wait,
    reads, saves or loads)."""
    actions: list[Json] = rpolicy.read_actions_since(since)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return any(a.get("tool") in bmetrics.ISSUE_TOOLS for a in actions)


def undecided_why(r: RunRunner, sc: Json, session_end: float, now: float, acted: bool = True) -> str | None:
    """Why the run's agent can't end its turn yet (run_session's stop check): its goal isn't decided (no until
    milestone or live verdict yet; a maximise scenario's never is) with time left, and no operator stop or cap; a
    maximise run's agent also made an acting call since its last resume (`acted`): a stretch without one ends the
    run (its session has no count on stop resumes). None: it may end."""
    if r.until_hit or r.cap_hit or r.stop_requested or stop_file().exists() or now >= session_end:
        return None
    if (fact := maximised(sc)) is not None:
        if not acted:
            return None
        return (
            f"The run isn't over: it has no pass, it scores the party's {fact} gained by its time cap, and goes on "
            "until then."
        )
    return (
        "The run isn't decided: the grader hasn't seen its goal "
        f"{json.dumps(goal_of(sc), sort_keys=True)} yet, and the run goes on until it is decided or its time cap."
    )


def run_failures(
    state_dir: Path,
    t0: float,
    outcome: SessionOutcome,
    name: str,
    why: str,
    cap: str | None = None,
    session_minutes: float | None = None,
) -> list[Json]:
    """Every failure of a bench run, tagged harness/agent/unknown as the legacy series tags them (runs.classify_run
    over the run's runner_events.jsonl, its play streams and the agent's reloads; its one session as the history),
    plus the session's ending when it was ours (runs.reason_failures: mcp_failed, setup_failed, aborted, api_limit).
    A harness failure makes the run invalid (bg3bench results.validity): it doesn't score the model. name: the
    outcome to classify ("stopped" for an operator stop: listed apart, not a failure). A `timeout` whose agent ended
    its turn with more than EARLY_EXIT_MINUTES of the session_minutes left (no stop resume left) says so, not that the
    time ran out (fixlive D4: "the run's time ran out" at 7.9 of 240 min)."""
    res = outcome.result
    logged: list[Json] = runs.read_jsonl(state_dir / "runner_events.jsonl")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    events = [e for e in logged if e.get("t", 0) >= t0 - 1]
    session = {"kind": "play", "session": SID, "started": outcome.started, "ended": res.ended}
    tools: list[Json] = rpolicy.read_actions_since(t0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    actions = [a for a in tools if a.get("t", res.ended) <= res.ended + 1]
    # the whole session's (every stop resume's) game calls that worked: a quick exit has none
    session |= {"reason": res.reason, "exit": res.exit_code, "game_calls_ok": runs.game_calls_ok(actions)}
    ending = (res.reason, why)
    ending_why = why or res.reason if name == "gave_up" else ""  # a time-out or a cap says its own why
    if name == "timeout" and session_minutes is not None:
        left = rpolicy.early_exit_minutes(res.reason, outcome.started, res.ended, session_minutes)
        if left is not None:
            ending_why = f"{SID}: the agent ended its turn with {left} min of the run left, before the goal"
    return runs.classify_run(state_dir, t0, res.ended, [session], events, actions, name, ending_why, cap, ending)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


class LiveRunHost:
    """bg3bench's RunHost (structural) on the live game, one run at a time.

    make_runner(opt) builds the run's runner (runner/play.py passes its Runner: SessionRunner with the
    playthrough parts, whose LSF reader the settings readout needs); run_files(run_dir, client, seed, timing)
    dumps the client's files (client_setup.run_files by default); env is the base env (this process's, read once):
    each job's env is a fresh copy with the job's vars (condition, API key, tools, memory dir), handed to its runner
    (run_env: the play process, its bg3 server and the play settings read it), so nothing of one job reaches the next
    and os.environ is never changed (H1). read_pins(env) reads a run's pins at its start, after the mod snapshot
    refresh (machine_pins by default). recording(out_dir) makes a run's recording (recorder.run_recording; None: off):
    it starts once the game is up, into <run_dir>/recording/, and a run whose recorder isn't recording then or at
    the kickoff fails its start (recorder.RecordingError); record stops it after run_end, before the game stops."""

    def __init__(
        self,
        make_runner: Callable[[rstate.Options], RunRunner],
        *,
        run_files: Callable[[Path, str, int | None, str | None], Json] | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        read_pins: Callable[[Mapping[str, str]], Json] | None = None,
        recording: Callable[[Path], hrec.RunRecording] | None = None,
    ):
        self.make_runner, self.clock = make_runner, clock
        self.read_pins = read_pins or machine_pins
        self.base = dict(os.environ if env is None else env)
        self.env: dict[str, str] = dict(self.base)  # the current job's (_setup)
        self.run_files = run_files or self._run_files
        self.runs: dict[Path, tuple[RunRunner, float, list[str]]] = {}
        self.watches: dict[Path, GoalWatch] = {}  # the run's live verdict (a goal, a wipe) while it plays
        self.saves: dict[Path, SaveWatch] = {}  # the save each run plays
        self.pins: dict[Path, Json] = {}  # what each run plays with (read_pins at its start)
        self.recording = recording
        self.recordings: dict[Path, hrec.RunRecording] = {}  # each run's, while it plays
        self.stopping = False  # stop(): an operator signal

    @property
    def stopped(self) -> bool:
        """An operator stop (a signal, or runtime/STOP): no new run starts."""
        return self.stopping or stop_file().exists()

    def stop(self) -> None:
        """An operator stop (play.py bench's first SIGINT/SIGTERM): the run playing now ends its session at the
        runner's next check and is recorded (operator: invalid); no new run starts."""
        self.stopping = True
        for r, _, _ in self.runs.values():
            r.stop_requested = True

    def abort(self, when: str = "operator exit") -> None:
        """A hard exit (a second signal, or an exception out of the matrix): every run still open loses its play
        process and the game it brought up, and its moved nav files go back. Its record is not written."""
        while self.runs:
            run_dir, (r, _, _) = self.runs.popitem()
            self.pins.pop(run_dir, None)
            watches = (
                self.watches.pop(run_dir, None),
                self.saves.pop(run_dir, None),
                self.recordings.pop(run_dir, None),
            )
            for w in watches:
                if w is not None:
                    w.stop()
            try:
                r.kill_agent()
                r.scenario_undo()
                r.end_run_game(when, "the run is not recorded")
            finally:
                restore_nav(nav_marker())
                isolate_saves(saves_marker())

    def save_watch(self, r: RunRunner, own: Callable[[str], bool]) -> SaveWatch:
        return SaveWatch(r, own)

    def _run_files(self, d: Path, client: str, seed: int | None, timing: str | None) -> Json:
        return client_setup.run_files(d, client, seed, timing, renv.REPO, renv.log, environ=self.env)

    def _setup(self, job: Json, sc: Json, run_dir: Path) -> tuple[Json, str, str | None]:
        """The job's client env (condition, API key, tools) and run files; the kickoff's pace pre-flight.

        A bench run sees no playthrough memory (knowledge contamination, live7: session_start served play/memory's
        briefing): the memory tool group is off and the server's memory dir is the run's own `memory/`, which
        nothing fills, so no tool reads another playthrough's notes. job["tools_off"]: the condition's removed
        tools (the bench's conditions.removed_tools: wiki, load_game); job["prompt_condition"]: the prompt condition
        the bench's id maps to (conditions.Condition.prompt_condition)."""
        choice = client_setup.resolve(job["client"], job.get("profile"), job["model"], job.get("effort"), None, False)
        self.env = dict(self.base)
        self.env.update(client_setup.api_key_env(choice.profile, self.env, renv.REPO / ".env"))
        self.env["BG3AI_PROMPT_CONDITION"] = job["prompt_condition"]  # rules | rules+coaching, not the bench's id
        self.env["BG3AI_MEMORY_DIR"] = str(run_dir / "memory")
        self.env["BG3AI_TOOLS_OFF"] = ",".join([NO_MEMORY, *job.get("tools_off", ())])  # the profile's go on top
        # the scenario's autoskip (default on): the runner's ui.config each session, and the play server's skipper
        self.env["BG3AI_AUTOSKIP"] = "0" if sc["game_settings"].get("autoskip", True) is False else "1"
        difficulty = sc["game_settings"].get("difficulty") if sc["start"]["kind"] == "new_game" else None
        if difficulty:  # the bridge's new_game starts it, whatever the agent picks (live7: Explorer, not Balanced)
            self.env[SCENARIO_DIFFICULTY] = difficulty
        self.env.update(client_setup.session_env(choice.client, choice.profile, True, self.env))
        files: Json = {"profile": choice.profile, "pace": client_setup.kickoff_pace(choice.client, self.env)}
        if choice.client == "claude" or (choice.profile is not None and choice.client == "pi"):
            files |= self.run_files(run_dir, choice.client, sc["game_settings"].get("seed"), sc.get("timing"))
        return files, choice.model, choice.effort

    def start(self, job: Json, sc: Json, run_dir: Path) -> HostStart:
        """The run's runner, reset, game at the scenario's start and kickoff. A start that fails forgets the run,
        stops the game it brought up and raises RuntimeError (the runner's SystemExits too: an operator refusal
        such as ensure_game's foreign game would otherwise end the process), so SessionRun records one invalid
        attempt and the matrix reruns. A save start whose save didn't load (missing, or --take-over playing on
        from another) fails before the kickoff. The nav files a failed start moved are put back."""
        want = renv.RUNTIME / "sessions" / run_dir.name
        if Path(run_dir).resolve() != want.resolve():  # facts.jsonl and the streams must land in the run dir
            raise ValueError(f"run dir {run_dir}: the runner writes runtime/sessions/<run> under {renv.RUNTIME}")
        if self.stopped:
            raise RuntimeError("operator stop: no new run starts")
        r: RunRunner | None = None
        try:
            agent_group.reap_leftover(renv.RUNTIME / agent_group.FILE, renv.log)  # live7: it drove the next game
            files, model, effort = self._setup(job, sc, run_dir)
            opt = options_for(dict(job, model=model, effort=effort), sc, run_dir.name, files)
            r = self.make_runner(opt)
            r.run_env = self.env
            st = self._begin(r, opt, sc, run_dir)
            t0, start_save = self.runs[run_dir][1], opt.start_save

            def own(name: str) -> bool:
                return own_save(name, rcode.list_saves(), t0, start_save)

            self.saves[run_dir] = self.save_watch(r, own)
            self.saves[run_dir].start()
            if job.get("verdict") is not None:
                self.watches[run_dir] = GoalWatch(r, job["verdict"])
                self.watches[run_dir].start()
            return st
        except (Exception, SystemExit) as e:
            for w in (self.saves.pop(run_dir, None), self.recordings.pop(run_dir, None)):
                if w is not None:
                    w.stop()
            try:
                if r is not None:
                    self.runs.pop(run_dir, None)
                    self.pins.pop(run_dir, None)
                    r.scenario_undo()
                    r.end_run_game("start failed", "the rerun launches a new one")
            finally:
                restore_nav(nav_marker())
                isolate_saves(saves_marker())
            if isinstance(e, SystemExit):
                raise RuntimeError(f"start failed: {e.code}") from e
            raise

    def _begin(self, r: RunRunner, opt: rstate.Options, sc: Json, run_dir: Path) -> HostStart:
        t0 = self.clock()
        r.run_started = t0
        r.settles_budget = False  # the matrix settles a paid run's cost (once): the runner must not add it too
        r.stop_ends_session = True
        r.stop_requested = self.stopping
        r.state["saves_after"] = t0  # reset.saves: only saves from now on (and the start save) are the run's
        restore_nav(nav_marker())  # what a run that died between start and record left moved
        isolate_saves(saves_marker())  # and the saves it wrote
        mark_run_saves(saves_marker(), renv.SAVE_DIR, run_dir / "saves", t0, opt.start_save)
        archive = sc["reset"]["nav_state"] == "archive" and sc["start"]["kind"] == "save"
        moved = isolate_nav(renv.SE_BG3AI_DIR, run_dir / "reset", nav_marker(), archive)
        r.event("run_reset", nav_files=moved)
        self.runs[run_dir] = (r, t0, moved)
        try:
            r.refresh_mod_snapshot()
        except OSError as e:
            renv.log(f"mod snapshot refresh failed: {e}; the game keeps {r.mod_rev}'s mod")
        self.pins[run_dir] = self.read_pins(self.base)  # the mod tree the game loads from now on
        loaded = r.ensure_game(first=True)
        if self.recording is not None:  # with the game up (the recorder records its window)
            self.recordings[run_dir] = self.recording(run_dir / "recording")
            self.recordings[run_dir].start()
        if sc["start"]["kind"] == "save":
            start_save = sc["start"].get("save")
            if not start_save or r.state["loaded_save"] != start_save:
                raise RuntimeError(
                    f"the start save {start_save} didn't load (loaded: {r.state['loaded_save']}): "
                    "the run would play from elsewhere"
                )
            loaded = loaded or start_save
        r.hot_reload_mod()
        r.check_settings()  # the scenario's difficulty / karmic dice against the readout; the seed; the avatar's name
        r.apply_play_settings()  # after the rename: the save/load gates compare the avatar's name with the owner (B2)
        if self.stopped:  # stopped while the game came up: no agent starts (start's handler stops the game)
            raise RuntimeError("operator stop before the kickoff")
        if (rec := self.recordings.get(run_dir)) is not None:
            rec.check()  # ffmpeg still up after the settings: the run plays recorded
        prompt = scenario_kickoff(sc, SID, loaded, opt.pace or "")
        minutes = opt.session_minutes
        # the manifest's prompt is what the client is served (run_manifest.manifest_fields; none without run files),
        # its parts that text's and the kickoff's (live9: it was the kickoff's hash alone, no tools, no version)
        man: Json = opt.manifest or {}
        served: Json | None = man.get("prompt")
        kickoff = {"file": "kickoff", "sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        hooks = r.session_hooks(SID, minutes)
        return HostStart(
            prompt,
            cast(
                SessionHooks,
                BenchHooks(hooks, r.event, lambda end, since: self.undecided(r, sc, run_dir, end, hooks.now(), since)),
            ),
            str(uuid.uuid4()),
            served["sha256"] if served else None,
            (*served["parts"], kickoff) if served else (kickoff,),
            bg3harness.__version__,
            man.get("tools"),
            cast(Json, man.get("client") or {}).get("version"),
        )

    def undecided(
        self, r: RunRunner, sc: Json, run_dir: Path, session_end: float, now: float, since: float
    ) -> str | None:
        """undecided_why on the game as it is now: a run with a live verdict first reads the facts once
        (r.facts_now) and asks the verdict (GoalWatch.check), so a wipe or a pass since the last polls (facts every
        story.POLL_S, the verdict every GOAL_POLL_S) ends the run instead of resuming its agent (B3 job 10: the
        agent's wait_for saw the Game Over box at 16:23:40, the runner resumed it at 16:23:45 as undecided, the wipe
        stop came at 16:24:07). A failed read leaves the verdict to the facts already written. A maximise run's
        agent is resumed only if it made an acting call since `since` (its play process's start: acted_since)."""
        if (w := self.watches.get(run_dir)) is not None and not r.until_hit:
            try:
                r.facts_now()
            except Exception as e:  # the game not answering: the stop check still decides on what it has
                renv.log(f"{SID}: stop check: facts read failed: {type(e).__name__}: {e}")
            w.check()
        acted = maximised(sc) is None or acted_since(since)
        if not acted:
            renv.log(f"{SID}: no acting call since the agent's last (re)start: the maximise run ends at its turn's end")
        return undecided_why(r, sc, session_end, now, acted)

    def record(self, job: Json, sc: Json, run_dir: Path, outcome: SessionOutcome) -> Json:
        r, t0, moved = self.runs.pop(run_dir)
        pins = self.pins.pop(run_dir, None)
        if (w := self.watches.pop(run_dir, None)) is not None:
            w.stop()
        foreign: str | None = None
        reloads = 0
        if (sw := self.saves.pop(run_dir, None)) is not None:
            sw.stop()
            sw.check(final=True)  # with the game up: a load seen since the last poll is decided now
            foreign, reloads = sw.foreign, sw.reloads
        res = outcome.result
        hit = r.until_hit if r.until_hit and r.until_hit.get("id") == sc.get("until") else None
        goal = r.until_hit if r.until_hit and r.until_hit.get("id") == GOAL_HIT else None
        stopped = res.reason == "stopped"  # an operator stop ended the session: invalid (bench results.validity)
        invalid = outcome.invalid or outcome.gave_up or stopped or res.reason in runs.HARNESS_REASONS or foreign
        if invalid:
            name = "gave_up"
        elif goal:
            name = "reached" if goal["verdict"] == "passed" else "failed"  # the live verdict ended the run
        else:
            name = "reached" if hit else "over_budget" if r.cap_hit else "timeout"
        try:
            r.end_facts(name)
        finally:
            try:
                if (rec := self.recordings.pop(run_dir, None)) is not None:
                    rec.stop()  # after run_end, with the game still up: the recording covers the whole run
                # with the game up: scn.undo takes the seed out of BG3AI/scenario.json, which a record from before
                # the run keeps (restore_nav moves only one the run added), else it seeds every later load
                r.scenario_undo()
                r.end_run_game("run end", "the next run launches a new one")
            finally:
                back = restore_nav(nav_marker())  # with the game stopped: it writes no nav file any more
                own_saves = isolate_saves(saves_marker())  # nor a save
        totals: Json = runs.claude_totals(r.state.dir, t0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        why, cap = foreign or getattr(res, "detail", ""), (r.cap_hit or {}).get("cap")
        failures = run_failures(
            r.state.dir, t0, outcome, "stopped" if stopped else name, why, cap, r.opt.session_minutes
        )
        counts = {cls: sum(int(f.get("n") or 1) for f in failures if f["class"] == cls) for cls in runs.CLASSES}
        return (
            {
                "outcome": name,
                "reason": res.reason,
                "cost_usd": None if totals["cost_unknown"] else totals["cost_usd"],  # unknown is not $0
                "tokens": totals["tokens"],
                "served": totals["served"],  # what the endpoint served (pi: thinking levels, providers)
                "turns": totals["turns"],
                "wall_min": round((res.ended - outcome.started) / 60, 2),
                # the agent's first end of turn (resumed or not), wall s from the run's start; None: never
                "first_stop_s": None if outcome.first_stop is None else round(outcome.first_stop - outcome.started, 1),
                "sessions": 1,
                "cap_hit": r.cap_hit,
                "milestone": hit and hit["id"],
                "game_settings": getattr(r, "game_settings", None),
                "manifest": r.opt.manifest,
                "pins": pins,  # the bench adds the versions (bg3bench.pins.versions)
                "reset": {"nav_files": moved, "restored": back, "saves": own_saves},
                "loaded_save": r.state["loaded_save"],
                "reloads": reloads,  # bg3bench result.json's (diverse-2026-10-25: 0, the record had none)
                "harness_failures": counts[runs.HARNESS],
                "agent_failures": counts[runs.AGENT],
                "unknown_failures": counts[runs.UNKNOWN],
                "failures": failures,
            }
            | ({"verdict": goal["verdict"]} if goal else {})
            | ({FOREIGN_SAVE: foreign} if foreign else {})
            | ({"operator": "stop"} if stopped else {})
            | ({"cost_unknown": True} if totals["cost_unknown"] else {})
        )
