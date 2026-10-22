"""runner/agent_group with real processes: a fake play agent in its own session spawns a child (its "MCP server"),
both deaf to SIGINT/SIGTERM. live7: a SIGINT to `play.py bench run` left `claude -p` and its bg3ai-mcp alive; the
agent played on and drove the next game. After every end (the agent still running, the agent exited with its child
left, a runner that died without cleaning up) no process of the group is left. The tests wait on the processes' own
lines and on the group's end, never on a fixed sleep."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from bg3harness.runner import agent_group as A  # noqa: N812
from bg3harness.runner import env as renv
from bg3harness.runner.agent_side import AgentSide

AGENT = r"""
import os, signal, subprocess, sys, time
for s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(s, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c",
    "import signal, time\nfor s in (signal.SIGINT, signal.SIGTERM): signal.signal(s, signal.SIG_IGN)\n"
    "print('CHILD', flush=True)\nwhile True: time.sleep(60)"], stdout=sys.stdout)
print("AGENT", child.pid, flush=True)
if sys.argv[1] == "exits":
    sys.exit(0)
while True:
    time.sleep(60)
"""


def agent(tmp_path: Path, mode: str = "plays") -> tuple[subprocess.Popen[str], int]:
    """The fake agent (a session leader, as agent_side starts it) and its child's pid, once both run."""
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    p = subprocess.Popen([sys.executable, str(script), mode], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert p.stdout is not None
    seen: dict[str, str] = {}
    for line in p.stdout:
        word, _, rest = line.strip().partition(" ")
        seen[word] = rest
        if {"AGENT", "CHILD"} <= seen.keys():
            return p, int(seen["AGENT"])
    raise AssertionError(f"the fake agent ended early: {p.wait()}")


def gone(pid: int) -> bool:
    """No such process any more (a zombie still waiting for its parent counts as gone: it can't act)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state == "Z"


def test_end_group_takes_the_agent_and_its_child_down(tmp_path: Path):
    p, child = agent(tmp_path)
    path = tmp_path / "agent.json"
    A.record(path, p)
    assert json.loads(path.read_text()) == {"pgid": p.pid, "start": A.start_time(p.pid)}
    p.send_signal(signal.SIGINT)  # deaf to the hard stop's first signals: only the group's SIGKILL ends it
    assert A.end_group(path, p)
    assert p.returncode == -signal.SIGKILL and gone(child) and not A.alive(p.pid)
    assert not path.exists()


def test_an_agent_that_exited_leaves_no_child_behind(tmp_path: Path):
    # the client exited (a cap, a stop, the hard stop's SIGINT) but its MCP server would play on
    p, child = agent(tmp_path, "exits")
    assert p.wait(timeout=60) == 0 and not gone(child) and A.alive(p.pid)
    assert A.end_group(tmp_path / "agent.json", p)
    assert gone(child) and not A.alive(p.pid)


def test_kill_agent_kills_the_whole_group_even_when_the_client_already_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(renv, "RUNTIME", tmp_path / "runtime")
    for mode in ("plays", "exits"):
        p, child = agent(tmp_path, mode)
        A.record(renv.RUNTIME / A.FILE, p)
        side = SimpleNamespace(agent_proc=p, sandbox_cleanup=lambda: None)
        AgentSide.kill_agent(side)  # pyright: ignore[reportArgumentType]
        assert side.agent_proc is None and gone(child) and not A.alive(p.pid), mode
        assert not (renv.RUNTIME / A.FILE).exists()


RUNNER = r"""
import signal, subprocess, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from bg3harness.runner import agent_group as A
p = subprocess.Popen([sys.executable, sys.argv[2], "plays"], stdout=subprocess.PIPE, text=True,
                     start_new_session=True)
A.record(Path(sys.argv[3]), p)
lines = [p.stdout.readline(), p.stdout.readline()]
print(*[ln.strip() for ln in lines], flush=True)
while True:
    signal.pause()
"""


def dead_runner_leftover(tmp_path: Path, path: Path) -> tuple[int, int]:
    """A runner that started an agent and was SIGKILLed: the agent group lives on (reparented), agent.json names it."""
    (tmp_path / "agent.py").write_text(AGENT)
    (tmp_path / "runner.py").write_text(RUNNER)
    tests = str(Path(__file__).resolve().parent)
    runner = subprocess.Popen(
        [sys.executable, str(tmp_path / "runner.py"), tests, str(tmp_path / "agent.py"), str(path)],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert runner.stdout is not None
    words = runner.stdout.readline().split()
    runner.kill()
    runner.wait()
    leader = json.loads(path.read_text())["pgid"]
    child = int(words[words.index("AGENT") + 1])
    assert A.alive(leader) and not gone(child)
    return leader, child


def test_a_leftover_group_of_a_dead_runner_is_killed_before_the_next_start(tmp_path: Path):
    path = tmp_path / "agent.json"
    leader, child = dead_runner_leftover(tmp_path, path)
    said: list[str] = []
    A.reap_leftover(path, said.append)
    assert not A.alive(leader) and gone(child) and not path.exists()
    assert said and f"process group {leader}" in said[0]


def test_a_pid_now_used_by_another_process_is_left_alone(tmp_path: Path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"pgid": os.getpid(), "start": -1}))  # our own pid, another start time
    A.reap_leftover(path, lambda m: pytest.fail(m))
    assert not path.exists()
    path.write_text("not json")
    A.reap_leftover(path, lambda m: pytest.fail(m))
    assert not path.exists()
    A.reap_leftover(path, lambda m: pytest.fail(m))  # no file: nothing to do
