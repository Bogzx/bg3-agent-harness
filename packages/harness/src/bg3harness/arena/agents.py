# pyright: basic
"""The two sides' agents, live: one client process per side at a time (pi), each with its own bg3 MCP server
playing its side (BG3AI_SIDE), its own tools-off list and its own files in the match dir:

    <side>.events.jsonl       the side's normalized events, every process of it (events.EventLog)
    <side><tag>.jsonl/.log    each process's raw stream and human log (tag: "", -r1, -r2, ...)
    <side><tag>.metrics.jsonl the side's bridge's per-tool records

ModelClock is the per-turn cap's measure: a process's model time from its stream, live. It runs from a trigger (the
process start, the result of the last outstanding tool call) until the model's next tool call or its result, so
time spent in tools (the bridge, the game) and waiting on the other side never counts against a side.
"""

from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bg3harness import client_setup, clients, events, pi_client, spend
from bg3harness.arena.match import AgentExit
from bg3harness.runner import agent_group
from bg3harness.runner import env as renv

STOP_GRACE_S = (10.0, 5.0)  # SIGINT, then SIGTERM, then SIGKILL


class ModelClock:
    def __init__(self, t0: float):
        self.total = 0.0
        self.since: float | None = t0  # running since then; None: stopped (a tool call outstanding, or the result)
        self.open: set[str] = set()
        self.lock = threading.Lock()

    def on_event(self, ev: clients.Event, t: float) -> None:
        with self.lock:
            if ev.kind == "tool_call":
                self._stop(t)
                self.open.add(ev.call_id)
            elif ev.kind == "tool_result":
                self.open.discard(ev.call_id)
                if not self.open and self.since is None:
                    self.since = t
            elif ev.kind == "result":
                self._stop(t)
                self.open.clear()

    def _stop(self, t: float) -> None:
        if self.since is not None:
            self.total += max(0.0, t - self.since)
            self.since = None

    def model_s(self, now: float) -> float:
        with self.lock:
            return self.total + (max(0.0, now - self.since) if self.since is not None else 0.0)


@dataclass(frozen=True)
class SideClient:
    """What one side's processes share: the client (its profile and system text), the session env its bg3 server
    gets (BG3AI_SIDE, BG3AI_TOOLS_OFF, ...) and the model and effort."""

    client: clients.Client
    model: str
    effort: str | None
    env: Mapping[str, str]
    tools_expected: int | None = None  # the bg3 tools its init must list (else mcp_failed)


class AgentProcess:
    """One play process of a side (AgentRun): started at once, read by a pump thread."""

    def __init__(
        self,
        side: str,
        sc: SideClient,
        prompt: str,
        conversation: str,
        resume: bool,
        tag: str,
        out: Path,
        base_env: Mapping[str, str],
        clock: Callable[[], float] = time.time,
        silent_s: float = renv.AGENT_SILENT_MINUTES * 60,
    ):
        self.side, self.clock, self.silent_s = side, clock, silent_s
        self.conversation = conversation
        stem = out / f"{side}{tag}"
        env = {**sc.env, "BG3AI_METRICS_PATH": f"{stem}.metrics.jsonl"}
        self.env, self.tools_expected = env, sc.tools_expected
        req = clients.Request(
            prompt,
            sc.model,
            out,
            session=conversation,
            resume=resume,
            effort=sc.effort,
            env=dict(env),
            tag=f"{side}{tag}",
        )
        client = sc.client
        client.write_config(req)
        cmd = client.command(req)
        self.cost = spend.ProcessCost()
        self.model = ModelClock(clock())
        self.last_output = clock()
        self.mcp: str | None = None
        self.api_texts: list[str] = []
        self.raw: collections.deque[str] = collections.deque(maxlen=40)
        self.log = events.EventLog(out / f"{side}.events.jsonl")
        self.log.harness(
            "session_start",
            session=f"{side}{tag}",
            client=client.name,
            model=sc.model,
            effort=sc.effort,
            conversation=conversation,
            resume=resume,
            side=side,
        )
        self.raw_f = open(f"{stem}.jsonl", "a")  # noqa: SIM115 - open for the whole process
        self.started = clock()
        self.proc = subprocess.Popen(
            cmd,
            cwd=renv.PLAY_DIR,
            env=clients.process_env(client, req, dict(base_env)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            bufsize=1,
        )
        self.exit: AgentExit | None = None
        self.reason: str | None = None  # a fault the runner saw (agent_hang, mcp_failed) before the exit
        self.pump = threading.Thread(target=self._pump, args=(client,), name=f"arena-{side}{tag}", daemon=True)
        self.pump.start()

    def _pump(self, client: clients.Client) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            now = self.last_output = self.clock()
            self.raw_f.write(line)
            self.raw_f.flush()
            try:
                evs = client.parse(line)
            except Exception:  # a line the parser can't read is kept raw
                self.raw.append(line.rstrip())
                continue
            if not evs and line.strip() and not line.lstrip().startswith("{"):
                self.raw.append(line.rstrip())
            for e in evs:
                self.log.write(e)
                self.model.on_event(e, now)
                self.cost.on_event(e)
                if e.kind == "init" and self.mcp is None:
                    self.mcp = (
                        renv.mcp_problem(e.mcp, False)
                        or client_setup.tool_count_problem(e.tools, self.tools_expected, self.env)
                        or ""
                    )
                if e.api_error and e.kind in ("text", "error", "result") and e.text:
                    self.api_texts.append(e.text[:300])

    def model_s(self) -> float:
        return self.model.model_s(self.clock())

    def done(self) -> AgentExit | None:
        if self.exit is not None:
            return self.exit
        if self.proc.poll() is None:
            if self.mcp:
                self.reason = "mcp_failed"
            elif self.clock() - self.last_output >= self.silent_s:
                self.reason = "agent_hang"
            else:
                return None
            self._signal_out()
        return self._finish()

    def stop(self) -> AgentExit:
        if self.exit is None and self.proc.poll() is None:
            self._signal_out()
        return self._finish()

    def _signal_out(self) -> None:
        for sig, grace in ((signal.SIGINT, STOP_GRACE_S[0]), (signal.SIGTERM, STOP_GRACE_S[1])):
            try:
                os.killpg(self.proc.pid, sig)
                self.proc.wait(timeout=grace)
                return
            except (ProcessLookupError, PermissionError):
                return
            except subprocess.TimeoutExpired:
                continue

    def _finish(self) -> AgentExit:
        if self.exit is not None:
            return self.exit
        agent_group.kill_group(self.proc.pid, self.proc)  # its MCP server too
        self.pump.join(timeout=10)
        code = self.proc.returncode
        reason = self.reason or renv.exit_reason("exited", code)
        if reason == "exited" and (problem := self.mcp or pi_client.exit_problem(code, "\n".join(self.raw))):
            reason = "mcp_failed"
            self.mcp = problem
        if reason == "exited" and renv.classify_api_failure(self.api_texts, False):
            reason = "api_error"
        self.raw_f.close()
        self.log.end(reason, code, self.started, side=self.side, conversation=self.conversation)
        self.exit = AgentExit(reason, code, self.conversation, round(self.cost.usd, 6))
        return self.exit


class LiveAgents:
    """Agents for run_arena: each side's processes with its SideClient; a side's conversation id is fixed up front
    (pi takes it on the command line) and a resume continues it."""

    def __init__(
        self,
        sides: Mapping[str, SideClient],
        out: Path,
        base_env: Mapping[str, str],
        wall_cap_min: float,
        clock: Callable[[], float] = time.time,
    ):
        self.sides, self.out, self.base_env, self.wall_cap_min, self.clock = sides, out, base_env, wall_cap_min, clock
        self.deadline: float | None = None  # the wall cap's end, from the first start (the session clock shows it)

    def start(self, side: str, prompt: str, conversation: str | None, tag: str) -> AgentProcess:
        if self.deadline is None:
            self.deadline = self.clock() + self.wall_cap_min * 60
        clock = {"BG3AI_SESSION_DEADLINE": str(int(self.deadline)), "BG3AI_SESSION_WRAPUP": str(int(self.deadline))}
        sc = self.sides[side]
        sc = SideClient(sc.client, sc.model, sc.effort, {**sc.env, **clock}, sc.tools_expected)
        conv = conversation or str(uuid.uuid4())
        return AgentProcess(
            side, sc, prompt, conv, conversation is not None, tag, self.out, self.base_env, clock=self.clock
        )


def summary(exits: Mapping[str, list[AgentExit]]) -> dict[str, list[dict]]:
    """The outcome's exits as JSON (the match's manifest)."""
    return {s: [json.loads(json.dumps(e.__dict__)) for e in xs] for s, xs in exits.items()}
