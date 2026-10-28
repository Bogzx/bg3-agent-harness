"""Start and Stop from the panel: a New run plan becomes a matrix file that the repo's own matrix command plays.

The panel adds no second way to run a scenario. Start writes runtime/panel/<name>.toml in matrix_cli's format (the
plan's profiles, ready scenarios, condition, repeats and per-scenario reserve) and hands it to a `Runner`; the Queue
screen reads the matrix's progress.json like any other matrix. `MatrixProcess`, the real runner, starts
`uv run python runner/play.py bench run --matrix <file> --runner session` in the repo (which takes runtime/runner.lock,
so a second matrix or play.py run can't start beside it) with its output in runtime/panel/<name>.log. Stop sends that
process one SIGTERM: play.py bench ends the run playing now, records it (invalid: operator) and starts no new one. A
second signal would exit without recording the run, so Stop refuses to send one.

One matrix at a time, and only the one this panel started: a panel restarted while its matrix runs no longer holds
the process (the matrix itself goes on, its progress still shows in the queue).
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bg3bench.serve.panel import PanelError

PANEL_DIR = "panel"  # runtime/panel/: the matrix files and logs of the runs the panel started


class Runner(Protocol):
    def start(self, name: str, matrix_file: Path, log: Path) -> None:
        """Start playing the matrix file; PanelError when one is running already."""
        ...

    def stop(self) -> None:
        """Ask the running matrix to end the run playing now and start no new one; PanelError when none runs."""
        ...

    def state(self) -> dict[str, Any] | None:
        """{"name", "pid", "started", "log", "running", "returncode", "stopping"} of the last start, or None."""
        ...


def matrix_name(now: float) -> str:
    return "panel-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))


def refusal(plan: Mapping[str, Any]) -> str | None:
    """Why a plan (panel.plan_run) can't start, or None."""
    if not plan["jobs"]:
        return "no job to run: no selected scenario is ready"
    if plan["needs_max_cost"]:
        return f"a paid run needs a cost cap: set max cost for {', '.join(plan['needs_max_cost'])}"
    if plan["budget"]["error"]:
        return f"the budget file: {plan['budget']['error']}"
    if not plan["fits_budget"]:
        return "the reserve doesn't fit the budget's cap"
    if gapped := sorted(p for p, gaps in plan["gaps"].items() if gaps):
        return f"the condition can't be played exactly by {', '.join(gapped)} (a pilot): the panel starts no pilot"
    return None


def _toml_str(s: str) -> str:
    return json.dumps(s)  # a TOML basic string: the names are slugs and paths, json's escapes are TOML's


def matrix_toml(name: str, plan: Mapping[str, Any], *, budget: Path, allow_unverified: bool) -> str:
    """The matrix file (matrix_cli's format) that plays exactly plan["jobs"]."""
    jobs = plan["jobs"]
    profiles = list(dict.fromkeys(f"profiles/{j['profile']}.toml" for j in jobs))
    scenarios = list(dict.fromkeys(j["scenario"] for j in jobs))
    conditions = list(dict.fromkeys(j["condition"] for j in jobs))
    caps = {j["scenario"]: j["max_cost"] for j in jobs if j["max_cost"] is not None}

    def arr(xs: Sequence[str]) -> str:
        return "[" + ", ".join(_toml_str(x) for x in xs) + "]"

    lines = [
        f"name = {_toml_str(name)}",
        f"models = {arr(profiles)}",
        f"scenarios = {arr(scenarios)}",
        f"conditions = {arr(conditions)}",
        f"repeats = {max(j['repeat'] for j in jobs)}",
        "allow_gaps = false",
        f"allow_unverified = {'true' if allow_unverified else 'false'}",
        "",
        "[budget]",
        f"file = {_toml_str(str(budget))}",
        "max_cost = { " + ", ".join(f"{_toml_str(s)} = {float(c)!r}" for s, c in caps.items()) + " }",
    ]
    return "\n".join(lines) + "\n"


def write_matrix(runtime: Path, name: str, text: str) -> tuple[Path, Path]:
    """runtime/panel/<name>.toml (a new file) and its log path."""
    d = runtime / PANEL_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.toml"
    with path.open("x", encoding="utf-8") as f:  # never overwrite: one file per start
        f.write(text)
    return path, d / f"{name}.log"


class Proc(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int, /) -> None: ...


Popen = Callable[..., Proc]


@dataclass
class _Started:
    name: str
    proc: Proc
    started: float
    log: Path
    stopping: bool = False


class MatrixProcess:
    """The real Runner: play.py bench in a process of its own (its own session, so a Ctrl-C of the panel isn't its)."""

    def __init__(
        self,
        repo: Path,
        *,
        popen: Popen = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        now: Callable[[], float] = time.time,
    ):
        self.repo, self.popen, self.which, self.now = repo, popen, which, now
        self.current: _Started | None = None

    def command(self, matrix_file: Path) -> list[str]:
        uv = self.which("uv")
        if uv is None:
            raise PanelError("uv is not on PATH: the panel starts runs with uv run python runner/play.py bench")
        return [
            uv,
            "run",
            "python",
            "runner/play.py",
            "bench",
            "run",
            "--matrix",
            str(matrix_file),
            "--runner",
            "session",
        ]

    def running(self) -> bool:
        return self.current is not None and self.current.proc.poll() is None

    def start(self, name: str, matrix_file: Path, log: Path) -> None:
        if self.running():
            raise PanelError(f"matrix {self.current.name if self.current else ''} is running: stop it first")
        argv = self.command(matrix_file)
        with log.open("ab") as out:
            proc = self.popen(
                argv, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )  # fmt: skip
        self.current = _Started(name, proc, self.now(), log)

    def stop(self) -> None:
        cur = self.current
        if cur is None or cur.proc.poll() is not None:
            raise PanelError("no matrix of this panel is running")
        if cur.stopping:
            raise PanelError("stopping already: the run playing now ends and is recorded")
        cur.proc.send_signal(signal.SIGTERM)
        cur.stopping = True

    def state(self) -> dict[str, Any] | None:
        cur = self.current
        if cur is None:
            return None
        rc = cur.proc.poll()
        return {
            "name": cur.name,
            "pid": cur.proc.pid,
            "started": cur.started,
            "log": str(cur.log),
            "running": rc is None,
            "returncode": rc,
            "stopping": cur.stopping,
        }


def start(
    runner: Runner,
    plan: Mapping[str, Any],
    *,
    runtime: Path,
    budget: Path,
    allow_unverified: bool,
    now: float,
) -> dict[str, Any]:
    """Start a plan's jobs: refuses a plan that can't start or a second matrix; returns the runner's state."""
    st = runner.state()
    if st is not None and st["running"]:
        raise PanelError(f"matrix {st['name']} is running: stop it first")
    if why := refusal(plan):
        raise PanelError(f"can't start: {why}")
    name = matrix_name(now)  # a slug: matrix_cli refuses a name with other characters
    try:
        path, log = write_matrix(
            runtime, name, matrix_toml(name, plan, budget=budget, allow_unverified=allow_unverified)
        )
    except FileExistsError:
        raise PanelError(f"matrix {name} was started this second already") from None
    runner.start(name, path, log)
    return {"started": runner.state(), "matrix_file": str(path)}
