"""The supervisor: starts, stops and resumes a multi-session playthrough (research/arch/app.md §3.5).

    for each session until stopped or the hours budget is spent:
        request = the playthrough's config + nudges (stuck after STUCK_SESSIONS sessions without progress; resume
                  after a crash)
        result  = runner.run_session(request, stop)       # SessionRunner: the harness's run_session later; until
                                                          # then adapters/legacy_cli.py (runner/play.py run)
        keeper.run_keeper(pt, result)                     # between sessions, joined before the next start
        history += result                                 # <runtime>/sessions/<pt>/supervisor.json

Stop is soft: the running session is asked to finish (stop event), the loop starts no other. One playthrough runs
at a time (one game). `Supervisor.run` is synchronous; `start`/`resume` run it on a thread. Clock and runner are
passed in, so tests drive whole playthroughs with a fake runner and a fake clock.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from . import config as C

HISTORY_VERSION = 1
STUCK_SESSIONS = 2  # runner/play.py STUCK_SESSIONS
MAX_CRASHES = 3  # crashed sessions in a row before the loop gives up
# Coaching text (the app's, not the harness's: app.md §5.7); moves to kit/prompts.py with A1.
STUCK_NUDGE = (
    "CHANGE APPROACH: the last {n} sessions ended with no measurable progress. What you've been doing "
    "isn't working. Run the stuck playbook, write three genuinely different approaches in hypotheses.md "
    "and pick one you haven't tried; consider parking the blocked objective for another quest or region."
)
CRASH_NOTE = "The last session ended early ({why}). Load the save from HANDOFF.md and continue from there."


@dataclass(frozen=True)
class SessionRequest:
    playthrough: str
    session: int  # 1-based play session number
    config: C.PlaythroughConfig
    resume: bool  # continue an existing playthrough (load its save) rather than a new game
    notes: tuple[str, ...] = ()
    record: bool = True


@dataclass(frozen=True)
class SessionResult:
    session: str  # "S012"
    outcome: str  # "ok" | "stopped" | "crash" | "error" | "finished" (the game's story ended)
    progress: bool | None = None  # None = unknown (doesn't count as stuck)
    started: float | None = None
    ended: float | None = None
    cost_usd: float | None = None
    detail: str = ""


class SessionRunner(Protocol):
    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        """Plays one session; returns when it ends or soon after `stop` is set."""
        ...


class Keeper(Protocol):
    def run_keeper(self, pt: str, result: SessionResult) -> None: ...


class NoKeeper:
    """For a runner that keeps the memory itself (the legacy CLI runs runner/memory_keeper.py on its own)."""

    def run_keeper(self, pt: str, result: SessionResult) -> None:
        return None


class SupervisorError(RuntimeError):
    """A control request that can't be done now (already running, nothing to resume ...)."""


# ------------------------------------------------------------ pure policy


def is_stuck(history: list[dict], n: int = STUCK_SESSIONS) -> bool:
    """The last n sessions each ended with progress False (runner/play.py is_stuck)."""
    recent = history[-n:]
    return len(recent) == n and all(h.get("progress") is False for h in recent)


def notes_for(history: list[dict]) -> tuple[str, ...]:
    out = []
    if history and history[-1].get("outcome") in ("crash", "error"):
        out.append(CRASH_NOTE.format(why=history[-1].get("detail") or history[-1]["outcome"]))
    if is_stuck(history):
        out.append(STUCK_NUDGE.format(n=STUCK_SESSIONS))
    return tuple(out)


def budget_left(cfg: C.PlaythroughConfig, started: float, now: float) -> bool:
    """Hours budget (config [session] hours; 0 = no budget) counted over this run of the loop."""
    return cfg.session.hours <= 0 or now - started < cfg.session.hours * 3600


# The Launcher's fields -> config keys (section, key); section None = top level.
LAUNCH_KEYS: dict[str, tuple[str | None, str]] = {
    "profile": ("agent", "profile"),
    "client": ("agent", "client"),
    "model": ("agent", "model"),
    "effort": ("agent", "effort"),
    "brief": ("character", "brief"),
    "roleplay": ("character", "roleplay"),
    "difficulty": (None, "difficulty"),
    "session_minutes": ("session", "minutes"),
    "hours": ("session", "hours"),
    "max_cost": ("session", "max_cost_usd"),
    "keeper": ("keeper", "enabled"),
    "recording": ("recording", "enabled"),
}


def launch_config(cfg: C.PlaythroughConfig, body: dict) -> C.PlaythroughConfig:
    """The config with the Launcher's choices applied and validated (unknown fields are a ConfigError)."""
    unknown = sorted(set(body) - set(LAUNCH_KEYS))
    if unknown:
        raise C.ConfigError(f"unknown launch field(s) {', '.join(unknown)}")
    d = C.to_dict(cfg)
    for k, v in body.items():
        section, key = LAUNCH_KEYS[k]
        (d if section is None else d[section])[key] = v
    return C.validate(d)


# ------------------------------------------------------------ history file


def history_path(runtime: Path, pt: str) -> Path:
    return runtime / "sessions" / pt / "supervisor.json"


def read_history(path: Path) -> list[dict]:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    h = v.get("history") if isinstance(v, dict) else None
    return [x for x in h if isinstance(x, dict)] if isinstance(h, list) else []


def write_history(path: Path, history: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"v": HISTORY_VERSION, "history": history}, indent=1), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------ the supervisor


@dataclass
class Supervisor:
    runner: SessionRunner
    keeper: Keeper
    runtime: Path
    playthroughs: Path
    clock: Callable[[], float]
    on_change: Callable[[dict], object] = lambda state: None  # the SSE hub ("supervisor" events)
    # sessions played before the supervisor kept a history (the runner's own logs); None = only its history
    played: Callable[[str], int] | None = None
    pt: str | None = None
    state: str = "idle"  # idle | running | stopping
    detail: str = ""
    session: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def history(self, pt: str) -> list[dict]:
        return read_history(history_path(self.runtime, pt))

    def sessions_played(self, pt: str, hist: list[dict] | None = None) -> int:
        n = len(self.history(pt) if hist is None else hist)
        return max(n, self.played(pt)) if self.played else n

    def view(self) -> dict:
        return {"playthrough": self.pt, "state": self.state, "detail": self.detail, "session": self.session}

    def _set(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.on_change(self.view())

    def _claim(self, pt: str, resume: bool, cfg: C.PlaythroughConfig | None) -> C.PlaythroughConfig:
        with self.lock:
            if self.state != "idle":
                raise SupervisorError(f"{self.pt} is {self.state}")
            n = self.sessions_played(pt)
            if resume and not n:
                raise SupervisorError(f"{pt} has no sessions to resume; start it")
            if not resume and n:
                raise SupervisorError(f"{pt} already has {n} sessions; resume it")
            if cfg is None:
                cfg = C.load(self.playthroughs / pt)
            else:  # the Launcher's choices: written only once the start is accepted
                (self.playthroughs / pt).mkdir(parents=True, exist_ok=True)
                C.save(self.playthroughs / pt, cfg)
            self.stop_event = threading.Event()
            self._set(pt=pt, state="running", detail="", session=None)
            return cfg

    def start(self, pt: str, resume: bool = False, wait: bool = False, cfg: C.PlaythroughConfig | None = None) -> None:
        """Start (resume=False: a playthrough without sessions) or resume (the next session of one with sessions)
        on a thread; wait=True runs it here (tests). `cfg` (the Launcher's) is saved as playthrough.toml first."""
        cfg = self._claim(pt, resume, cfg)
        if wait:
            self._run(pt, cfg, resume)
            return
        self.thread = threading.Thread(target=self._run, args=(pt, cfg, resume), daemon=True, name=f"sup-{pt}")
        self.thread.start()

    def resume(self, pt: str, wait: bool = False, cfg: C.PlaythroughConfig | None = None) -> None:
        self.start(pt, resume=True, wait=wait, cfg=cfg)

    def stop(self) -> None:
        """Soft stop: the running session finishes (the runner sees the event), no new one starts."""
        with self.lock:
            if self.state != "running":
                raise SupervisorError("nothing is running")
            self.stop_event.set()
            self._set(state="stopping", detail="finishing the current session")

    def _run(self, pt: str, cfg: C.PlaythroughConfig, resume: bool) -> None:
        started = self.clock()
        path = history_path(self.runtime, pt)
        end = "stopped"
        try:
            while not self.stop_event.is_set():
                hist = read_history(path)
                if not budget_left(cfg, started, self.clock()):
                    end = "hours budget spent"
                    break
                n = self.sessions_played(pt, hist) + 1
                req = SessionRequest(
                    pt, n, cfg, resume=resume or n > 1, notes=notes_for(hist), record=cfg.recording.enabled
                )
                self._set(session=f"S{n:03d}", detail="playing" + (" (stuck nudge)" if is_stuck(hist) else ""))
                res = self.runner.run_session(req, self.stop_event)
                hist.append(asdict(res))
                write_history(path, hist)
                if cfg.keeper.enabled and res.outcome != "error":
                    self._set(detail="memory keeper")
                    self.keeper.run_keeper(pt, res)
                if res.outcome == "finished":
                    end = "the story is finished"
                    break
                if res.outcome == "stopped":  # also a stop from outside (play.py stop / runtime/STOP)
                    break
                if len(hist) >= MAX_CRASHES and all(h["outcome"] == "crash" for h in hist[-MAX_CRASHES:]):
                    end = f"{MAX_CRASHES} sessions crashed in a row"
                    break
                if res.outcome == "error":
                    end = f"session error: {res.detail}"
                    break
        except Exception as e:  # noqa: BLE001 - shown in the UI; the app keeps serving
            end = f"{type(e).__name__}: {e}"[:300]
        finally:
            with self.lock:
                self._set(state="idle", detail=end)
