"""Stands in for `runner/play.py run` in the offline playthrough test: one session, no game.

    python fake_play.py run --playthrough <pt> ... (the legacy adapter's command line)

play.py run launches and drives the real game, so it can't run offline until the harness's run_session takes a fake
game. This keeps everything else real: the real play.py's argparse parses the command line (its `cmd_run` is swapped
for `session` below), the real pi plays from the --profile against the scripted stub model, the real bridge
(bg3ai.server) answers its tools from the fake mod on $E2E_FAKE_MOD_PORT, and the files the app reads are written the
way play.py writes them: play.write_status (runner.status.json), play.RunnerState (runner.json's play entry),
events.EventLog (Sxxx.events.jsonl) and clients.format_event (the Sxxx.log lines). The bridge writes
<runtime>/actions.jsonl itself. The player's session log (memory/sessions/Sxxx.md) is a one-line stand-in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "runner"))
from bg3harness import clients, events, profiles  # noqa: E402
from bg3harness.pi_client import PiClient  # noqa: E402
from bg3harness.testing.mcp_bridge import bridge_env  # noqa: E402

import play  # noqa: E402  (BG3AI_RUNTIME / BG3AI_PLAYTHROUGHS from the env)

SYSTEM = "Rules of play: you are the party on the nautiloid. Use the bg3 tools.\n"


def session(a) -> int:
    assert a.profile and a.max_cost > 0 and a.no_memory_keeper, "pi: --profile, --max-cost and --no-keeper"
    profile = profiles.load(Path(a.profile))
    state = play.RunnerState(a.playthrough)
    hist = state["history"]
    sid = f"S{sum(1 for h in hist if h.get('kind', 'play') == 'play') + 1:03d}"
    base = state.dir / sid
    home = Path(os.environ["E2E_HOME"])
    start = time.time()
    play.write_status(
        playthrough=a.playthrough,
        session=sid,
        model=profile.model,
        effort=profile.effort,
        started=start,
        log=f"{base}.log",
        session_ended=None,
    )
    system = state.dir / "system.txt"
    system.write_text(SYSTEM)
    server = {
        "command": sys.executable,
        "args": ["-m", "bg3ai.server"],
        "env": bridge_env(int(os.environ["E2E_FAKE_MOD_PORT"]), home, {"BG3AI_RUNTIME": str(play.RUNTIME)}),
        "timeout": 60_000,
    }
    (home / "cwd").mkdir(parents=True, exist_ok=True)
    client = PiClient(profile, system, server=server, cwd=home / "cwd")
    conversation = str(uuid.uuid4())
    req = clients.Request(
        "Play.",
        profile.model,
        state.dir,
        session=conversation,
        tag=sid,
        env={"BG3AI_SESSION": sid, "BG3AI_PLAYTHROUGH": a.playthrough},
    )
    client.write_config(req)
    evlog = events.EventLog(Path(f"{base}.events.jsonl"))
    evlog.harness("session_start", session=sid, client="pi", model=profile.model, conversation=conversation)
    with open(f"{base}.log", "a", buffering=1) as human, open(f"{base}.jsonl", "a") as raw:
        human.write(f"===== {sid} {datetime.now():%Y-%m-%d %H:%M:%S} pi session {conversation}\n")
        proc = subprocess.Popen(
            client.command(req),
            cwd=home / "cwd",
            env=clients.process_env(client, req),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:  # play.py's pump: the raw stream, the events log, the human log
            raw.write(line)
            for e in client.parse(line):
                evlog.write(e)
                for text in clients.format_event(e):
                    human.write(f"{datetime.now():%H:%M:%S} {text}\n")
        code = proc.wait()
    evlog.end("exited", code, start)
    (mem := play.PLAYTHROUGHS / a.playthrough / "memory" / "sessions").mkdir(parents=True, exist_ok=True)
    (mem / f"{sid}.md").write_text(f"# {sid}\n- {datetime.now():%H:%M} [note] got off the ship's first deck\n")
    ended = time.time()
    entry = {
        "kind": "play",
        "session": sid,
        "claude_session": conversation,
        "reason": "exited",
        "exit": code,
        "started": start,
        "ended": ended,
        "model": profile.model,
        "effort": profile.effort,
        "minutes": round((ended - start) / 60, 1),
        "progress": True,
    }
    state["history"] = [*hist, entry]
    play.write_status(session_ended=ended)
    return code


if __name__ == "__main__":
    play.cmd_run = session
    raise SystemExit(play.main(sys.argv[1:]))
