"""TEMPORARY SessionRunner over today's runner CLI (remove in Phase D: the harness's run_session replaces it).

One session = one `uv run python runner/play.py run --playthrough <pt> --hours <minutes/60> --session-minutes <minutes>`
with the config's client/model/effort/character/difficulty as flags. A client profile (profiles/<name>.toml, parsed
by bg3harness/profiles.py) is mapped to --client/--model/--effort; a pi profile is passed whole as --profile with the
config's cost cap as --max-cost and no keeper (what play.py --profile requires for pi).
A soft stop writes <runtime>/STOP (`play.py stop`: the session finishes, the runner exits). The result comes from the
newest play entry of <runtime>/sessions/<pt>/runner.json. The config's prompt condition goes to play.py as
$BG3AI_PROMPT_CONDITION.

What play.py does itself here, so the supervisor must not do it again: the memory keeper (but not for pi, which
play.py runs with --no-keeper: keeper_client gives the app's keeper_pass.MemoryKeeper pi's profile), the stuck
directive and the crash resume (request.notes are not passed on), the save to load (HANDOFF.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Protocol

from .. import client_profiles, config
from ..jsontypes import is_obj
from ..supervisor import SessionRequest, SessionResult, SupervisorError

CRASH_REASONS = ("crash", "hang", "aborted")


class Proc(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def poll(self) -> int | None: ...


def spawn(args: list[str], cwd: Path, log: IO[bytes], env: dict[str, str]) -> Proc:
    return subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def profile_agent(
    directory: Path, name: str, load: Callable[[Path], Any] = client_profiles.runner_load
) -> config.Agent:
    """client/model/effort of a client profile (bg3harness/profiles.py checks it)."""
    try:
        p = client_profiles.load_one(directory, name, load)
    except (OSError, ValueError) as e:
        raise SupervisorError(f"client profile {name}: {e}") from e
    return config.Agent(profile=name, client=p.client, model=p.model, effort=p.effort or "")


def play_args(
    python: str, repo: Path, req: SessionRequest, agent: config.Agent, profiles: Path | None = None
) -> list[str]:
    """The play.py command line for one session (pure). A pi agent runs from its profile file under `profiles`."""
    cfg = req.config
    m = cfg.session.minutes
    args = [
        python,
        str(repo / "runner" / "play.py"),
        "run",
        "--playthrough",
        req.playthrough,
        "--hours",
        f"{m / 60:.4g}",
        "--session-minutes",
        f"{m:g}",
    ]
    if agent.client == "pi":  # play.py --profile: pi needs --max-cost and --no-keeper
        if not agent.profile or profiles is None:
            raise SupervisorError("client pi: pick a pi client profile (profiles/*.toml)")
        if cfg.session.max_cost_usd <= 0:
            raise SupervisorError(f"client profile {agent.profile}: pi needs a cost cap (session.max_cost_usd > 0)")
        args += [
            "--profile",
            str(profiles / f"{agent.profile}.toml"),
            "--max-cost",
            f"{cfg.session.max_cost_usd:g}",
            "--no-keeper",
        ]
    else:
        args += ["--client", agent.client, "--model", agent.model]
        if agent.effort:
            args += ["--effort", agent.effort]
        if cfg.session.max_cost_usd > 0:
            args += ["--max-cost", f"{cfg.session.max_cost_usd:g}"]
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


def condition_env(cfg: config.PlaythroughConfig) -> dict[str, str]:
    """play.py's env: the config's prompt condition for the bg3 server and pi's system text (claude's play/CLAUDE.md
    is rules+coaching whatever it says), and [game] autoskip (the runner's ui.config, its and the server's skipper)."""
    return {**os.environ, "BG3AI_PROMPT_CONDITION": cfg.agent.condition,
            "BG3AI_AUTOSKIP": "1" if cfg.game.autoskip else "0"}  # fmt: skip


def last_play(runner_json: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(runner_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    history: list[Any] = d.get("history") or []
    plays = [h for h in history if is_obj(h) and h.get("kind", "play") == "play"]

    return plays[-1] if plays else None


def result_from(
    entry: dict[str, Any] | None, before: dict[str, Any] | None, exit_code: int, stopped: bool
) -> SessionResult:
    if entry is None or entry == before:
        return SessionResult(session="?", outcome="error", detail=f"play.py exited {exit_code} without a session")
    reason = str(entry.get("reason") or "")
    outcome = "crash" if reason in CRASH_REASONS or exit_code != 0 else "stopped" if stopped else "ok"
    prog = entry.get("progress")
    return SessionResult(
        session=str(entry.get("session") or "?"),
        outcome=outcome,
        progress=prog if isinstance(prog, bool) else None,
        started=entry.get("started"),
        ended=entry.get("ended"),
        detail=f"reason={reason} exit={exit_code}",
    )


class LegacyCli:
    def __init__(
        self,
        repo: Path,
        runtime: Path,
        profiles: Path | None = None,
        python: str = "python3",
        spawn_fn: Callable[[list[str], Path, IO[bytes], dict[str, str]], Proc] = spawn,
        poll_s: float = 1.0,
        load: Callable[[Path], Any] = client_profiles.runner_load,
    ):
        self.repo, self.runtime, self.python, self.spawn, self.poll_s = repo, runtime, python, spawn_fn, poll_s
        self.load = load
        self.profiles = profiles if profiles is not None else repo / "profiles"

    def agent(self, cfg: config.PlaythroughConfig) -> config.Agent:
        if cfg.agent.profile:
            return profile_agent(self.profiles, cfg.agent.profile, self.load)
        return cfg.agent

    def keeper_client(self, cfg: config.PlaythroughConfig) -> tuple[str, Any] | None:
        """(client, profile) for the app's keeper pass (keeper_pass.MemoryKeeper): only pi's, since play.py keeps
        the memory itself for every other client and runs pi with --no-keeper. None = play.py's keeper or none."""
        agent = self.agent(cfg)
        if agent.client != "pi" or not cfg.keeper.enabled:
            return None
        try:
            return "pi", client_profiles.load_one(self.profiles, agent.profile, self.load)
        except (OSError, ValueError) as e:
            raise SupervisorError(f"client profile {agent.profile}: {e}") from e

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        runner_json = self.runtime / "sessions" / req.playthrough / "runner.json"
        before = last_play(runner_json)
        args = play_args(self.python, self.repo, req, self.agent(req.config), self.profiles)
        log_path = self.runtime / "sessions" / req.playthrough / "app-runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stopped = False
        with log_path.open("ab") as log:
            proc = self.spawn(args, self.repo, log, condition_env(req.config))
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
