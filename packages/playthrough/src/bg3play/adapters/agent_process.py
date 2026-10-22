"""One agent process of a play session, for the app's run_session hooks (adapters/harness_runner.py).

A bg3harness Client (pi, claude, codex, gemini) spells the command, its config files and its env; this starts it with
the session's env and pumps its stdout, as runner/play.py does, into <sid>.jsonl (raw), <sid>.events.jsonl (the
normalized events) and <sid>.log (the human log the store reads). Every process of a session (the kickoff, stop
resumes, fault resumes) appends to the same three files and uses the session id as its tag, so a resume finds its
conversation (pi's agent dir is <sid>.pi).

A watchdog ends the process at its time cap (reason "cap") or when the game watch reports a fault ("crash"/"hang",
fault_at set); the pump ends it once the cost its usage events report reaches the call's cap ("cost_cap");
otherwise its exit is reason "exited". Process start, the wait and the clock are passed in, so tests
drive it with a fake process.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from bg3harness import clients, events

WATCH_S = 30.0  # seconds between game checks while the agent plays


class Proc(Protocol):
    @property
    def stdout(self) -> Iterable[str] | None: ...
    def wait(self) -> int: ...
    def kill(self) -> None: ...


def popen(cmd: list[str], cwd: Path, env: dict[str, str]) -> Proc:
    return subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True
    )


@dataclass
class AgentRun:
    """bg3harness.session.AgentResult for one process."""

    reason: str  # exited | cap | cost_cap | crash | hang
    exit_code: int | None
    started: float
    ended: float
    claude_session: str  # the conversation id (the field name is run_session's)
    fault_at: float | None = None
    cost_usd: float | None = None  # the process's cost from its result event, when the client knows it


@dataclass(frozen=True)
class AgentCall:
    sdir: Path  # <runtime>/sessions/<pt>
    sid: str
    prompt: str
    cap_s: float  # the process is stopped after this many seconds
    conversation: str
    resume: bool
    env: dict[str, str]  # the session env (BG3AI_*), on the process and forwarded to the bg3 server
    max_cost_usd: float = 0.0  # the process is stopped once its cost reaches this (0 = no cap)


class ClientAgent:
    def __init__(
        self,
        client: clients.Client,
        model: str,
        effort: str | None,
        cwd: Path,
        clock: Callable[[], float],
        fault: Callable[[], str | None] = lambda: None,
        spawn: Callable[[list[str], Path, dict[str, str]], Proc] = popen,
        watch_s: float = WATCH_S,
    ):
        self.client, self.model, self.effort, self.cwd, self.clock = client, model, effort, cwd, clock
        self.fault, self.spawn, self.watch_s = fault, spawn, watch_s

    @property
    def client_name(self) -> str:
        return self.client.name

    def run(self, call: AgentCall, done: threading.Event | None = None) -> AgentRun:
        """Plays one process to its end. `done` (tests) is the watchdog's wake-up; set when the stream ends."""
        req = clients.Request(
            call.prompt,
            self.model,
            call.sdir,
            session=call.conversation,
            resume=call.resume,
            effort=self.effort,
            env=call.env,
            tag=call.sid,
        )
        self.client.write_config(req)
        base = call.sdir / call.sid
        evlog = events.EventLog(Path(f"{base}.events.jsonl"), self.clock)
        start = self.clock()
        evlog.harness("session_start", session=call.sid, client=self.client.name, model=self.model,  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                      conversation=call.conversation, resume=call.resume)  # fmt: skip
        env: dict[str, str] = clients.process_env(self.client, req)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        proc = self.spawn(self.client.command(req), self.cwd, env)
        done = done or threading.Event()
        stopped: list[tuple[str, float]] = []  # (reason, when) the watchdog stopped it
        dog = threading.Thread(target=self._watch, args=(proc, start + call.cap_s, done, stopped), daemon=True)
        dog.start()
        cost: float | None = None
        with open(f"{base}.log", "a", buffering=1) as human, open(f"{base}.jsonl", "a") as raw:
            how = "resume" if call.resume else "session"
            human.write(f"===== {call.sid} {datetime.now():%Y-%m-%d %H:%M:%S} {self.client.name} {how} "
                        f"{call.conversation}\n")  # fmt: skip
            for line in proc.stdout or ():
                raw.write(line)
                for e in self.client.parse(line):
                    evlog.write(e)
                    if e.cost_usd is not None:  # usage and result events: the process's cost so far
                        cost = max(cost or 0.0, e.cost_usd)
                        if call.max_cost_usd and cost >= call.max_cost_usd and not stopped:
                            stopped.append(("cost_cap", self.clock()))
                            proc.kill()
                    for text in clients.format_event(e):
                        human.write(f"{datetime.now():%H:%M:%S} {text}\n")
            code = proc.wait()
        done.set()
        dog.join()
        reason, at = stopped[0] if stopped else ("exited", None)
        evlog.end(reason, code, start)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return AgentRun(reason, code, start, self.clock(), call.conversation, at, cost)

    def _watch(self, proc: Proc, deadline: float, done: threading.Event, stopped: list[tuple[str, float]]) -> None:
        while not done.wait(max(0.0, min(self.watch_s, deadline - self.clock()))):
            now = self.clock()
            why = "cap" if now >= deadline else self.fault()
            if why:
                stopped.append((why, now))
                proc.kill()
                return
