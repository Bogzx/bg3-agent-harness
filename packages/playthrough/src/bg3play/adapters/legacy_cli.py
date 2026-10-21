"""TEMPORARY SessionRunner over today's runner CLI (remove in Phase D: the harness's run_session replaces it).

One session = one `python3 runner/play.py run --playthrough <pt> --hours <minutes/60> --session-minutes <minutes>`
with the config's client/model/effort/character/difficulty as flags. A client profile (profiles/<name>.toml) is
mapped to --client/--model/--effort; pi profiles need an endpoint the CLI can't take yet, so they are refused.
A soft stop writes <runtime>/STOP (`play.py stop`: the session finishes, the runner exits). The result comes from the
newest play entry of <runtime>/sessions/<pt>/runner.json.

What play.py does itself here, so the supervisor must not do it again: the memory keeper (use NoKeeper), the stuck
directive and the crash resume (request.notes are not passed on), the save to load (HANDOFF.md).
"""

from __future__ import annotations

import json
import subprocess
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Protocol

from .. import config as C
from ..supervisor import SessionRequest, SessionResult, SupervisorError

CRASH_REASONS = ("crash", "hang", "aborted")


class Proc(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def poll(self) -> int | None: ...


def spawn(args: list[str], cwd: Path, log: IO[bytes]) -> Proc:
    return subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def profile_agent(path: Path) -> C.Agent:
    """client/model/effort of a client profile; pi (an endpoint) isn't something the legacy CLI can take."""
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SupervisorError(f"client profile {path.name}: {e}") from e
    if d.get("client") == "pi" or "endpoint" in d:
        raise SupervisorError(f"client profile {path.name}: pi profiles need the harness (not the legacy CLI)")
    return C.Agent(profile=path.stem, client=str(d.get("client") or "claude"), model=str(d.get("model") or "opus"),
                   effort=str(d.get("effort") or ""))


def play_args(python: str, repo: Path, req: SessionRequest, agent: C.Agent) -> list[str]:
    """The play.py command line for one session (pure)."""
    cfg = req.config
    m = cfg.session.minutes
    args = [python, str(repo / "runner" / "play.py"), "run", "--playthrough", req.playthrough,
            "--hours", f"{m / 60:.4g}", "--session-minutes", f"{m:g}", "--client", agent.client, "--model", agent.model]
    if agent.effort:
        args += ["--effort", agent.effort]
    if not cfg.keeper.enabled:
        args.append("--no-keeper")
    else:
        args += ["--keeper-effort", cfg.keeper.effort, "--keeper-minutes", f"{cfg.keeper.minutes:g}"]
        if cfg.keeper.model:
            args += ["--keeper-model", cfg.keeper.model]
    if cfg.difficulty:
        args += ["--difficulty", cfg.difficulty]
    if cfg.character.brief:
        args += ["--character-brief", cfg.character.brief, "--roleplay", cfg.character.roleplay]
    return args


def last_play(runner_json: Path) -> dict | None:
    try:
        d = json.loads(runner_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plays = [h for h in d.get("history") or [] if isinstance(h, dict) and h.get("kind", "play") == "play"]
    return plays[-1] if plays else None


def result_from(entry: dict | None, before: dict | None, exit_code: int, stopped: bool) -> SessionResult:
    if entry is None or entry == before:
        return SessionResult(session="?", outcome="error", detail=f"play.py exited {exit_code} without a session")
    reason = str(entry.get("reason") or "")
    outcome = ("crash" if reason in CRASH_REASONS or exit_code != 0 else "stopped" if stopped else "ok")
    prog = entry.get("progress")
    return SessionResult(session=str(entry.get("session") or "?"), outcome=outcome,
                         progress=prog if isinstance(prog, bool) else None, started=entry.get("started"),
                         ended=entry.get("ended"), detail=f"reason={reason} exit={exit_code}")


class LegacyCli:
    def __init__(self, repo: Path, runtime: Path, profiles: Path | None = None, python: str = "python3",
                 spawn_fn: Callable[[list[str], Path, IO[bytes]], Proc] = spawn, poll_s: float = 1.0):
        self.repo, self.runtime, self.python, self.spawn, self.poll_s = repo, runtime, python, spawn_fn, poll_s
        self.profiles = profiles if profiles is not None else repo / "profiles"

    def agent(self, cfg: C.PlaythroughConfig) -> C.Agent:
        if cfg.agent.profile:
            return profile_agent(self.profiles / f"{cfg.agent.profile}.toml")
        return cfg.agent

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        runner_json = self.runtime / "sessions" / req.playthrough / "runner.json"
        before = last_play(runner_json)
        args = play_args(self.python, self.repo, req, self.agent(req.config))
        log_path = self.runtime / "sessions" / req.playthrough / "app-runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stopped = False
        with log_path.open("ab") as log:
            proc = self.spawn(args, self.repo, log)
            code: Any = None
            while code is None:
                if stop.is_set() and not stopped:
                    (self.runtime / "STOP").write_text("bg3play supervisor stop\n")
                    stopped = True
                try:
                    code = proc.wait(self.poll_s)
                except subprocess.TimeoutExpired:
                    code = None
        return result_from(last_play(runner_json), before, int(code), stopped)
