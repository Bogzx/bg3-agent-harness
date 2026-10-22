"""The play agent's process group: the client (claude, pi, codex, gemini) runs in its own session, so its group holds
the client and the MCP server it spawned (bg3ai-mcp). live7: a SIGINT to `play.py bench run` left `claude -p` and its
bg3ai-mcp alive (reparented to init); the agent played on and from 16 min later drove the next game.

Every end of a play process takes its whole group down and waits until no member is left (end_group): the loop's
end (the client exited, or the hard stop's SIGINT/TERM/KILL ended it, a cap, a fault, an operator stop), and
kill_agent (an operator's hard exit, an exception out of the bench's matrix). runtime/agent.json names the live group
(the leader's pid and start time); a process that died without cleaning up (SIGKILL, a power cut) leaves it, and the
next game or agent start ends that leftover group first (reap_leftover), so a new session is never driven by it.

A group id is never reused while any member is alive (the kernel keeps the pid), so a live group whose leader is gone
is still the one we started; a live leader with another start time is an unrelated process that got the pid after
our whole group ended: not ours, left alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

FILE = "agent.json"  # under the runtime dir
WAIT_S = 10.0  # SIGKILL to the group, then up to this long for its members to be gone


def start_time(pid: int) -> int | None:
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22), None when it doesn't exist."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat.rsplit(")", 1)[1].split()[19])


def alive(pgid: int) -> bool:
    """Any process left in the group (a zombie member counts until its parent reaps it)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group(
    pgid: int,
    proc: subprocess.Popen[str] | None = None,
    timeout: float = WAIT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """SIGKILL every process of the group and wait (proc, the leader we started, is reaped) until none is left or
    `timeout` passed. True: the group is gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    end = clock() + timeout
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
    while alive(pgid):
        if clock() >= end:
            return False
        sleep(0.05)
    return True


def record(path: Path, proc: subprocess.Popen[str]) -> None:
    """runtime/agent.json: the live play process's group (its leader's pid and start time)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pgid": proc.pid, "start": start_time(proc.pid)}) + "\n")


def end_group(path: Path, proc: subprocess.Popen[str]) -> bool:
    """The play process ended or must end: its whole group goes (the MCP server outlives a client that exited) and
    runtime/agent.json with it once nothing is left. False: a member survived the wait (the file stays)."""
    gone = kill_group(proc.pid, proc)
    if gone:
        path.unlink(missing_ok=True)
    return gone


def reap_leftover(path: Path, log: Callable[[str], None]) -> None:
    """Before a game or an agent starts: end the group runtime/agent.json names if a dead runner left it alive.
    RuntimeError when it survives the wait (the start must not go on with an agent that can still act)."""
    try:
        rec = json.loads(path.read_text())
        pgid, start = int(rec["pgid"]), rec.get("start")
    except (OSError, ValueError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return
    leader = start_time(pgid)
    if (leader is not None and leader != start) or not alive(pgid):
        path.unlink(missing_ok=True)  # gone, or the pid is another process's now
        return
    log(f"a play agent left by an earlier runner (process group {pgid}) is still alive: killing it before the start")
    if not kill_group(pgid):
        raise RuntimeError(f"the leftover play agent (process group {pgid}) survived SIGKILL; not starting")
    path.unlink(missing_ok=True)
