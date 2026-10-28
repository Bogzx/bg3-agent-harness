"""Start and Stop from the panel: a New run plan becomes a matrix file that the repo's own matrix command plays.

The panel adds no second way to run a scenario. Start writes runtime/panel/<name>.toml in matrix_cli's format (the
plan's profiles, ready scenarios, condition, repeats and per-scenario reserve) and hands it to a `Runner`; the Queue
screen reads the matrix's progress.json like any other matrix. `MatrixProcess`, the real runner, starts
`uv run python runner/play.py bench run --matrix <file> --runner session` in the repo (which takes runtime/runner.lock,
so a second matrix or play.py run can't start beside it) with its output in runtime/panel/<name>.log. Stop sends that
process one SIGTERM: play.py bench ends the run playing now, records it (invalid: operator) and starts no new one. A
second signal would exit without recording the run, so Stop refuses to send one.

Record and Submit when done: Start passes Record to play.py bench as BG3AI_BENCH_RECORD (1 or 0; the run host records
the game window into <run_dir>/recording/ when it is on) and keeps both choices in runtime/panel/<name>.json, which
auto.py reads to pack and validate the matrix's valid runs (never to upload them). Submit when done needs Record: a
run without a recording has no evidence video.

One matrix at a time. A matrix this panel didn't start (another panel, a terminal, or this panel before a restart) is
found from runtime/runner.lock: play.py writes its pid there and holds the lock while it runs, and that process's
command line names the matrix file (`found`). Stop for such a matrix writes runtime/STOP, the operator's stop file the
run host reads: the run playing now ends, recorded, and no new run starts, as with the signal.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from bg3bench.serve.panel import PanelError

PANEL_DIR = "panel"  # runtime/panel/: the matrix files, logs and choices of the runs the panel started
RECORD_ENV = "BG3AI_BENCH_RECORD"  # play.py bench's run host records the game window unless it is "0"


class Runner(Protocol):
    def start(self, name: str, matrix_file: Path, log: Path, *, record: bool) -> None:
        """Start playing the matrix file, recording each run or not; PanelError when one is running already."""
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
        environ: Mapping[str, str] = os.environ,
    ):
        self.repo, self.popen, self.which, self.now, self.environ = repo, popen, which, now, environ
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

    def start(self, name: str, matrix_file: Path, log: Path, *, record: bool) -> None:
        if self.running():
            raise PanelError(f"matrix {self.current.name if self.current else ''} is running: stop it first")
        argv = self.command(matrix_file)
        with log.open("ab") as out:
            proc = self.popen(
                argv, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True, env={**self.environ, RECORD_ENV: "1" if record else "0"},
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


def choices_path(runtime: Path, name: str) -> Path:
    return runtime / PANEL_DIR / f"{name}.json"


def read_choices(runtime: Path, name: str) -> dict[str, Any] | None:
    """runtime/panel/<name>.json: {"name", "record", "submit_when_done", "auto": {run dir: outcome}}, or None."""
    try:
        doc: Any = json.loads(choices_path(runtime, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], doc) if isinstance(doc, dict) else None


def write_choices(runtime: Path, doc: Mapping[str, Any]) -> None:
    path = choices_path(runtime, str(doc["name"]))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def flag(body: Mapping[str, Any], key: str, default: bool) -> bool:
    v = body.get(key, default)
    if not isinstance(v, bool):
        raise PanelError(f"{key} must be true or false")
    return v


# (argv, cwd) of a live process by pid, or None when there is none
ProcReader = Callable[[int], tuple[list[str], Path] | None]


def read_proc(pid: int) -> tuple[list[str], Path] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a], cwd


def bench_matrix_arg(argv: Sequence[str]) -> str | None:
    """The --matrix of a `... play.py bench run|resume --matrix F ...` command line, else None."""
    i = next((k for k, a in enumerate(argv) if Path(a).name == "play.py"), None)
    if i is None or list(argv[i + 1 : i + 2]) != ["bench"] or argv[i + 2 : i + 3] not in (["run"], ["resume"]):
        return None
    rest = list(argv[i + 3 :])
    for k, a in enumerate(rest):
        if a == "--matrix" and k + 1 < len(rest):
            return rest[k + 1]
        if a.startswith("--matrix="):
            return a.split("=", 1)[1]
    return None


def stop_file(runtime: Path) -> Path:
    return runtime / "STOP"


def found(runtime: Path, proc: ProcReader = read_proc) -> dict[str, Any] | None:
    """The matrix play.py bench is playing on this machine now, whoever started it, or None.

    runtime/runner.lock holds the runner's pid; only a live process whose command line is `play.py bench run|resume
    --matrix F` counts (the lock of a playthrough run, or a stale pid, is no matrix). The name is the matrix file's."""
    try:
        pid = int((runtime / "runner.lock").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    got = proc(pid)
    arg = bench_matrix_arg(got[0]) if got else None
    if got is None or arg is None:
        return None
    matrix_file = got[1] / arg
    try:
        name = str(tomllib.loads(matrix_file.read_text(encoding="utf-8"))["name"])
    except (OSError, ValueError, KeyError):
        name = matrix_file.stem
    return {
        "name": name,
        "pid": pid,
        "matrix_file": str(matrix_file),
        "running": True,
        "stopping": stop_file(runtime).exists(),
        "choices": read_choices(runtime, name),
    }


def request_stop(runtime: Path, now: float) -> None:
    """runtime/STOP: the run host ends the run playing now (recorded) and the matrix starts no new one."""
    stop_file(runtime).write_text(str(now), encoding="utf-8")


def start(
    runner: Runner,
    plan: Mapping[str, Any],
    *,
    runtime: Path,
    budget: Path,
    allow_unverified: bool,
    now: float,
    record: bool = True,
    submit_when_done: bool = False,
    proc: ProcReader | None = None,
) -> dict[str, Any]:
    """Start a plan's jobs: refuses a plan that can't start or a second matrix; returns the runner's state."""
    st = runner.state()
    if st is not None and st["running"]:
        raise PanelError(f"matrix {st['name']} is running: stop it first")
    if other := found(runtime, proc or read_proc):
        raise PanelError(f"matrix {other['name']} is running (pid {other['pid']}): stop it first")
    if submit_when_done and not record:
        raise PanelError("Submit when done needs Record: a run without a recording has no evidence video")
    if why := refusal(plan):
        raise PanelError(f"can't start: {why}")
    name = matrix_name(now)  # a slug: matrix_cli refuses a name with other characters
    try:
        path, log = write_matrix(
            runtime, name, matrix_toml(name, plan, budget=budget, allow_unverified=allow_unverified)
        )
    except FileExistsError:
        raise PanelError(f"matrix {name} was started this second already") from None
    write_choices(runtime, {"name": name, "record": record, "submit_when_done": submit_when_done, "auto": {}})
    runner.start(name, path, log, record=record)
    return {"started": runner.state(), "matrix_file": str(path)}
