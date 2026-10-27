"""Live probe, variant C (night round 2026-10-28, after N2 did not reproduce): replay bench B3 job 13's own tool calls
through the MCP tools, at their recorded times, and read who the game lets act after every call.

N2 (research/fix/live.md "Night N2") replayed the fall, the Scroll of Revivify, Cure Wounds and the short rest by hand:
the revived Tav walked. What the hand replay lacked, from the job (runtime/sessions/diverse-2026-10-25-crash_site_grove-
claude-haiku-5-5-claude-medium-rules/S001.events.jsonl, hotbar_cast.jsonl, the bg3le log):
  - the agent's exact calls and overlap: Cure Wounds and short_rest sent 22 ms apart (16:47:38.217/.239), walk_toward
    and wait_for("j63") together (16:47:44.775/.914), wait_for("j66") with get_layout (16:47:47.184/.257);
  - the revive's input: the job's ground pick took TWO clicks (hotbar_cast row t=1792939651.6: ground_clicks 2,
    elapsed 1.86 s): click 1 at (264.2,0.43,205.57), 0.5 m from the reader Shadowheart's feet (264,0.2,205.1), not taken
    in LAND_WAIT_S 1.2 s; click 2 at (265.19,0.43,205.72) took it, and Tav stood up there (265,0.2,205.8). N2's two
    revives took ONE click each (its first look hovered Shadowheart and was skipped). --two-click-revive forces the
    job's pattern: a first click 0.2/0.47 m (x/z) from the reader's feet, clicked whatever the cursor shows;
  - j63 (16:47:45 "j63: done — selected") is the act.select of the reader before the scroll (bg3le 96820.07-.14, job
    63, before act.interact job 64): the agent waited on a stale id; nothing selected anything then;
  - ~8 min of the session before (scenario start, travels, fights with nothing): --from session replays them all.

What it reads after every call (and once before the first): the client HUD's selected member (ui.selected) and per
party member the server's ClientControl / CharacterControl / RegisteredForAutomatedDialog / ResurrectComponent /
IsResurrected / falling, Osiris tasks and the compact state (probe_revive_stuck CONTROL).

Run it like night-live/lib/t (in-process bridge, from a dir whose srv.py sets the env and S), on a fresh load of the
job's start save (Tav-34221263934__SCN-crash_site_grove or its PROBE- copy):
    cd /data/Repos/ovh-e2e/runtime/probes/night-live/lib && uv run -q --project /data/Repos/ovh-e2e/bridge python \
        /data/Repos/fix-act/tools/dev/probe_revive_replay.py --two-click-revive --out ../revive/C1.jsonl
  C1 (default window 16:46:29-16:48:10, with a setup walk of e2 to the job's 16:46:29 spot (255.8,0.9,200.3) first)
  C2 --two-click-revive off (the same calls, the revive as it clicks today)
  C3 --from session --to 16:48:10 (the whole job up to the stuck walks, ~9.5 min; no setup walk)
`--dry-run` prints the plan only (no game). PASS for the probe: the replay's walk_toward at +16:47:44 reads travelled=0
(reproduced), and the control lines show what changed on Tav that the N2 runs did not have.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import probe_revive_stuck as probe  # noqa: E402  (tools/dev on the path first)

JOB = Path(
    "/data/Repos/ovh-e2e/runtime/sessions/diverse-2026-10-25-crash_site_grove-claude-haiku-5-5-claude-medium-rules"
)
DAY = "2026-10-25"
SETUP_SPOT = (255.8, 0.9, 200.3)  # where e2 Tav stood at 16:46:29 (walk_toward j54 done)
SKIP = ("memory_", "session_")  # the harness's memory and session tools: no game state
FIRST_CLICK_OFF = (0.2, 0.23, 0.47)  # job's click 1 (264.2,0.43,205.57) minus the reader's feet (264,0.2,205.1)


def clock(hms: str) -> float:
    return time.mktime(time.strptime(f"{DAY} {hms}", "%Y-%m-%d %H:%M:%S"))


def load_calls(events: Path, t_from: float | None, t_to: float) -> list[dict[str, Any]]:
    """The agent's tool calls in [t_from, t_to] (t_from None = the session start), each with the job's result text."""
    calls: dict[str, dict[str, Any]] = {}
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if e.get("kind") == "tool_call" and (t_from is None or e["t"] >= t_from) and e["t"] <= t_to:
            calls[e["call_id"]] = {"t": e["t"], "tool": e["tool"], "args": e.get("args") or {}, "job": None}
        elif e.get("kind") == "tool_result" and e.get("call_id") in calls:
            calls[e["call_id"]]["job"] = e.get("text")
    out = sorted(calls.values(), key=lambda c: c["t"])
    return [c for c in out if not c["tool"].startswith(SKIP)]


STARTED = re.compile(r"\bstarted (j\d+)\b")


class JobMap:
    """The job's job ids -> the replay's: learned from 'started jN' in the same call's two results; an id never
    printed (j63, the reader's select) maps by its offset from the nearest known one below it."""

    def __init__(self, calls: Sequence[dict[str, Any]] = (), wait_s: float = 5.0) -> None:
        self.known: dict[int, int] = {}
        self.cv = threading.Condition()
        self.wait_s = wait_s
        # ids some call printed as started: a wait_for on one (often sent before that call returned) waits for it
        self.printed = {int(m.group(1)[1:]) for c in calls for m in [STARTED.search(c.get("job") or "")] if m}

    def learn(self, job_text: str | None, replay_text: str | None) -> None:
        a, b = STARTED.search(job_text or ""), STARTED.search(replay_text or "")
        if a and b:
            with self.cv:
                self.known[int(a.group(1)[1:])] = int(b.group(1)[1:])
                self.cv.notify_all()

    def map(self, target: str) -> str:
        if not re.fullmatch(r"j\d+", target):
            return target
        n = int(target[1:])
        if n in self.printed:
            with self.cv:
                self.cv.wait_for(lambda: n in self.known, timeout=self.wait_s)
        below = [k for k in self.known if k <= n]
        if not below:
            return target
        k = max(below)
        return f"j{self.known[k] + n - k}"


def mapped_args(call: dict[str, Any], jobs: JobMap) -> dict[str, Any]:
    args = dict(call["args"])
    if call["tool"] == "wait_for" and isinstance(args.get("target"), str):
        args["target"] = jobs.map(args["target"])
    return args


def force_two_click_revive(twostep: Any, rpc: Any) -> None:
    """The job's revive input: the first ground pick 0.5 m from the reader's feet, clicked whatever the cursor
    reads (the job's look there showed no character); the normal spots follow."""
    spots0, ok0 = twostep._Run.spots, twostep.ground_ok
    forced: dict[str, Any] = {}

    def spots(self: Any) -> list[list[float]]:
        out = spots0(self)
        if self.landing is None:
            org = rpc(self.game, "ui.screen_pos", {"uuid": self.caster, "aim": "feet"}).get("world")
            if isinstance(org, list) and len(org) >= 3:
                p = [round(org[i] + FIRST_CLICK_OFF[i], 2) for i in range(3)]
                forced["spot"] = p
                out.insert(0, p)
        return out

    def ground_ok(gc: dict) -> tuple[bool, str | None]:
        if forced.pop("spot", None) is not None:
            return True, None
        return ok0(gc)

    twostep._Run.spots, twostep.ground_ok = spots, ground_ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--events", type=Path, default=JOB / "S001.events.jsonl")
    ap.add_argument("--from", dest="t_from", default="16:46:29", help="HH:MM:SS on the job's day, or 'session'")
    ap.add_argument("--to", dest="t_to", default="16:48:10")
    ap.add_argument("--two-click-revive", action="store_true")
    ap.add_argument("--no-setup", action="store_true", help="skip the setup walk to the job's 16:46:29 spot")
    ap.add_argument("--out", type=Path, default=Path("revive-replay.jsonl"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    t_from = None if a.t_from == "session" else clock(a.t_from)
    calls = load_calls(a.events, t_from, clock(a.t_to))
    t0 = calls[0]["t"]
    if a.dry_run:
        for c in calls:
            print(f"+{c['t'] - t0:7.2f}s {c['tool']} {json.dumps(c['args'])}")
        return 0

    sys.path.insert(0, os.getcwd())
    from bg3ai import twostep
    from bg3ai.luadebug import LuaDebugClient
    from bg3ai.tools import core

    tools = importlib.import_module("srv").S  # the caller's in-process bridge setup (night-live/lib/srv.py)
    dbg = LuaDebugClient(connect_timeout=5.0)
    dbg.connect()
    lock = threading.Lock()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    log = a.out.open("a")

    def call(name: str, **kw: Any) -> Any:
        fn = getattr(tools, name)
        return getattr(fn, "fn", fn)(**kw)  # an MCP-wrapped tool keeps the function in .fn

    def control() -> str:
        with lock:
            rows = json.loads(dbg.evaluate(probe.INSTALL + "\n" + probe.CONTROL, context="server", timeout=30.0))
        hud = (core.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
        return probe.control_line(rows, hud)

    def record(rec: dict[str, Any]) -> None:
        with lock:
            log.write(json.dumps(rec, default=str) + "\n")
            log.flush()
        print(json.dumps(rec, default=str)[:1500], flush=True)

    if a.two_click_revive:
        force_two_click_revive(twostep, twostep._rpc)
    if t_from is not None and not a.no_setup:
        x, y, z = SETUP_SPOT
        # N4's first C1: travel_to(who="e2") as the process's first call was "unknown target 'e2'": the eN ids
        # exist once a read has named the party
        status = call("get_status")
        record(
            {
                "status": status,
                "setup": call("travel_to", x=x, y=y, z=z, who="e2"),
                "wait": call("wait_for", target="queue"),
            }
        )
    record({"step": "before", "control": control()})

    jobs = JobMap(calls)
    threads = []
    start = time.monotonic()

    def run(i: int, c: dict[str, Any]) -> None:
        args = mapped_args(c, jobs)
        began = time.monotonic() - start
        try:
            out = call(c["tool"], **args)
        except Exception as e:  # a call that raises in the replay is a result too
            out = f"EXC {type(e).__name__}: {e}"
        text = out if isinstance(out, str) else json.dumps(out, default=str)[:4000]
        jobs.learn(c["job"], text)
        record(
            {
                "i": i,
                "job_at": round(c["t"] - t0, 2),
                "at": round(began, 2),
                "took": round(time.monotonic() - start - began, 2),
                "tool": c["tool"],
                "args": args,
                "result": text,
                "job_result": c["job"],
                "control": control(),
            }
        )

    for i, c in enumerate(calls):
        wait = (c["t"] - t0) - (time.monotonic() - start)
        if wait > 0:
            time.sleep(wait)
        th = threading.Thread(target=run, args=(i, c), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=120)
    record({"step": "after", "control": control()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
