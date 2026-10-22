"""What a stress probe (tools/stress/probes/<row>_*.py) drives, and its STRESS line.

tools/stress/run.py runs a probe as `uv run --project bridge python <script> <args>` with this directory on
PYTHONPATH, its out dir (runtime/stress/probe/<check>/) as the working directory and the game started on a COPY of
the row's save (BG3AI_STRESS_SAVE = that folder). The run's verdict is the probe's last `STRESS <VERDICT> <evidence>`
line (tools/stress/stress_core.probe_verdict).

A probe is a module with `run(p: Probe) -> (verdict, evidence)` and `if __name__ == "__main__": main(run)`.
Its checks are pure functions over what it read (unit-tested in test_stress_probes.py); `Probe` is the only I/O:
the mod's RPC (`p.game`, a bg3ai Game), raw Lua (`p.lua`), the bridge's agent tools (`p.S`, the dev scripts' server
view), the referee ring (`p.ref_mark` / `p.ref_after`), a clock and sleep, args and the out dir.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
READY_S = 300.0  # a load (WindowProbe already waits for the save copy; D11 reloads it)
Verdict = tuple[str, str]
# live1 §4: passive Perception checks against Act1_Medium (DC 10), each a RollResult the roll log records
ROLL = (
    "local h = Osi.GetHostCharacter() for i=1,%d do "
    'Osi.RequestPassiveRoll(h, h, "SkillCheck", "%s", "fa621d38-6f83-4e42-a55c-6aa651a75d46", 0, "probe"..i) end '
    'return "sent"'
)


def line(verdict: str, evidence: str) -> str:
    return f"STRESS {verdict} {' '.join(str(evidence).split())}"


class Probe:
    def __init__(
        self,
        game: Any,
        server: Any = None,
        out: Path | None = None,
        args: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ):
        self.game, self.S = game, server
        self.out = out or Path.cwd()
        self.args = list(args or [])
        self.clock, self.sleep, self.log = clock, sleep, log

    def lua(self, code: str, ctx: str = "server", timeout: float = 15.0) -> str:
        return self.game.eval(code, context=ctx, timeout=timeout)

    def screen(self) -> str | None:
        return (self.game.try_rpc("ui.state", context="client") or {}).get("screen")

    def wait_ready(self, timeout: float = READY_S) -> bool:
        """The save is loaded: the server context answers and the game screen is up."""
        end = self.clock() + timeout
        while True:
            try:
                if self.game.rpc("state.status") and self.screen() == "game":
                    return True
            except Exception:
                pass
            if self.clock() >= end:
                return False
            self.sleep(2.0)

    def wait_screen(self, want: str, timeout: float = 5.0, step: float = 0.05) -> str | None:
        end = self.clock() + timeout
        while True:
            s = self.screen()
            if s == want or self.clock() >= end:
                return s
            self.sleep(step)

    # ---- the mod's referee ring (Server/Referee.lua ref.since) ----

    def ref_mark(self) -> int:
        return int(self.game.rpc("ref.since", {"seq": 0, "limit": 1})["latest"])

    def ref_after(self, seq: int) -> list[dict]:
        out: list[dict] = []
        while True:
            page = self.game.rpc("ref.since", {"seq": seq, "limit": 500})["events"] or []
            out += page
            if len(page) < 500:
                return out
            seq = page[-1]["seq"]

    # ---- the mod's roll log (Server/RollLog.lua roll.since) ----

    def roll_batch(self, n: int = 24, skill: str = "Perception", settle_s: float = 3.0) -> str:
        """n passive skill checks by the host against DC 10 (live1 §4), as an S/f string from the roll log's
        Osiris records of the host, oldest first."""
        mark = int(self.game.rpc("roll.since", {"seq": 0, "limit": 1})["latest"])
        host = self.lua("return Osi.GetHostCharacter()")
        self.lua(ROLL % (n, skill))
        self.sleep(settle_s)
        recs = self.game.rpc("roll.since", {"seq": mark, "limit": 500})["events"] or []
        return roll_pattern(recs, host)

    def nearby(self, radius: float = 25.0) -> list[dict]:
        """Living characters near the party (state.nearby), nearest first."""
        nb = self.game.rpc("state.nearby", {"radius": radius}) or {}
        chars = [c for c in nb.get("characters") or [] if isinstance(c, dict) and not c.get("dead")]
        return sorted(chars, key=lambda c: c.get("dist") or 0.0)

    def handle(self, char: dict) -> str:
        """The agent-visible id (e12) of a state.nearby character, as get_nearby would show it."""
        return self.S.H.get(char["uuid"], char.get("name"))


def roll_pattern(records: list[dict], host: str) -> str:
    """S (success) / f per Osiris roll record of the host (host may be the GUIDSTRING `<name>_<uuid>`)."""
    mine = [
        r for r in records if "osi" in (r.get("srcs") or []) and r.get("roller") and str(host).endswith(r["roller"])
    ]
    return "".join("S" if r.get("success") else "f" for r in sorted(mine, key=lambda r: r["seq"]))


def find_char(chars: list[dict], name: str) -> dict | None:
    """The nearest character whose name contains `name` (case-insensitive)."""
    want = name.lower()
    return next((c for c in chars if want in str(c.get("name") or "").lower()), None)


def arg(p: Probe, i: int, default: str) -> str:
    return p.args[i] if len(p.args) > i else default


def connect(out: Path) -> tuple[Any, Any]:
    """The bridge's game connection and agent tools, with the bridge's runtime files under the probe's out dir."""
    os.environ.setdefault("BG3AI_RUNTIME", str(out / "rt"))
    os.environ.setdefault("BG3AI_MEMORY_DIR", str(out / "memory"))
    os.environ.setdefault("BG3AI_PLAYTHROUGH", "stress-probe")
    os.environ.setdefault("BG3AI_SESSION", "S999")
    # load_game(<the save copy>) is allowed: a scenario's start save counts as the playthrough's own
    os.environ.setdefault("BG3AI_START_SAVE", os.environ.get("BG3AI_STRESS_SAVE", ""))
    for p in (REPO / "bridge" / "src", REPO / "tools" / "dev"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from server_view import S  # the bridge's tools as one namespace (tools/dev/server_view.py)

    return S.game, S


def main(run: Callable[[Probe], Verdict], argv: list[str] | None = None, probe: Probe | None = None) -> int:
    """Run one probe and print its STRESS line (an exception is a FAIL with its message; the traceback goes to
    stderr). Exit 0 whenever a verdict was printed."""
    if probe is None:
        out = Path.cwd()
        game, server = connect(out)
        probe = Probe(game, server, out, sys.argv[1:] if argv is None else argv)
    try:
        if not probe.wait_ready():
            verdict, evidence = "FAIL", f"the save never came up (no server context or game screen in {READY_S:.0f} s)"
        else:
            verdict, evidence = run(probe)
    except Exception as e:
        traceback.print_exc()
        verdict, evidence = "FAIL", f"{type(e).__name__}: {e}"
    print(line(verdict, evidence), flush=True)
    return 0
