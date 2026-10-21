"""Live check for the hidden roll log (mod Server/RollLog.lua), against the running game.

    uv run --project bridge python tools/dev/check_rolllog.py [--n 24] [--dc 10] [--wait 6] [--out FILE]

Needs a loaded save out of combat and dialogue (any party, any place). It rolls --n passive Perception checks for
the host (Osi.RequestPassiveRoll with the Act1_Medium DC, research/ovh/live1.md §4: the probe's event names are
"rollcheck<i>"), waits --wait s, reads the records with roll.since and roll.snapshot, and prints one PASS / WARN /
FAIL line per check (exit 1 on any FAIL):

  hooks      the ServerRoll post-update hook (or the Tick fallback) and the RequestedRoll OnCreate are in place
  records    one record per roll: --n records named rollcheck1..n, each with srcs including osi
  natural    every record has natural 1..20 and dc == --dc (FAIL: none has a natural; WARN: some lack it)
  result     every record has result (the total; only the requested source gives it: WARN when missing)
  agree      natural 20 / 1 match crit nat20 / nat1, and success == (result >= dc) where both are known
  streams    every record has streams and a stream, and the batch's draws add up to >= 2 per roll

Also printed: which sources fired (fired.finished / requested / osi) and the raw hook hits (seen), so a FAIL says
which source is dead on this build. --out writes the raw records and snapshot as JSON. Rolls are passive checks
on the host only: nothing in the playthrough changes besides dice drawn from the Generic stream.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bridge" / "src"))

DC_ACT1_MEDIUM = "fa621d38-6f83-4e42-a55c-6aa651a75d46"
ROLL = """
local h = Osi.GetHostCharacter()
for i = 1, %d do Osi.RequestPassiveRoll(h, h, "SkillCheck", "Perception", "%s", 0, "rollcheck" .. i) end
return "sent"
"""

Check = tuple[str, str, str]  # (verdict, name, detail)


def grade(snap: dict[str, Any], records: list[dict[str, Any]], n: int, dc: int) -> list[Check]:
    """The verdicts for one batch: the records roll.since returned after the rolls and the snapshot after them."""
    out: list[Check] = []
    hooks = snap.get("hooks") or {}
    frame = hooks.get("post_update") or hooks.get("tick")
    ok = frame and hooks.get("requested_create")
    out.append(("PASS" if ok else "FAIL", "hooks", json.dumps(hooks, sort_keys=True)))

    mine = sorted(
        (r for r in records if str(r.get("event", "")).startswith("rollcheck")), key=lambda r: int(str(r["event"])[9:])
    )
    names = [r["event"] for r in mine]
    want = [f"rollcheck{i}" for i in range(1, n + 1)]
    osi = all("osi" in (r.get("srcs") or []) for r in mine)
    out.append(
        (
            "PASS" if names == want and osi else "FAIL",
            "records",
            f"{len(mine)} of {n} ({len(records)} new records in all); srcs "
            + ",".join(sorted({s for r in mine for s in r.get("srcs") or []})),
        )
    )

    nat = [r for r in mine if isinstance(r.get("natural"), int) and 1 <= r["natural"] <= 20]
    dcs = sorted({r["dc"] for r in mine if r.get("dc") is not None})
    v = "FAIL" if not nat else "PASS" if len(nat) == len(mine) and dcs == [dc] else "WARN"
    out.append((v, "natural", f"{len(nat)} of {len(mine)} with natural; dc values {dcs} (want [{dc}])"))

    res = [r for r in mine if isinstance(r.get("result"), int | float)]
    out.append(("PASS" if mine and len(res) == len(mine) else "WARN", "result", f"{len(res)} of {len(mine)}"))

    bad = []
    for r in mine:
        crit = {20: "nat20", 1: "nat1"}.get(r.get("natural") or 0)
        if r.get("natural") is not None and r.get("crit") != crit:
            bad.append(f"{r['event']}: natural {r['natural']} crit {r.get('crit')}")
        if (
            r.get("result") is not None
            and r.get("dc") is not None
            and r.get("success") is not None
            and r["success"] != (r["result"] >= r["dc"])
        ):
            bad.append(f"{r['event']}: result {r['result']} dc {r['dc']} success {r['success']}")
    out.append(("PASS" if not bad else "FAIL", "agree", "; ".join(bad[:5]) or "consistent"))

    drawn = sum(sum((r.get("streams") or {}).values()) for r in mine)
    each = all(r.get("streams") and r.get("stream") for r in mine)
    out.append(
        (
            "PASS" if mine and each and drawn >= 2 * len(mine) else "FAIL",
            "streams",
            f"{drawn} draws over {len(mine)} records; every record has streams+stream: {each}",
        )
    )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--dc", type=int, default=10)
    ap.add_argument("--wait", type=float, default=6.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient  # the game's debugger port; imported here so tests need no bridge

    c = LuaDebugClient(connect_timeout=5.0)
    c.connect()

    def rpc(op: str, args: dict[str, Any] | None = None) -> Any:
        payload = json.dumps({"op": op, "args": args or {}})
        reply = json.loads(
            c.evaluate(f"return Mods.BG3AI.BG3AI.rpc([==[{payload}]==])", context="server", timeout=15.0)
        )
        if not reply.get("ok"):
            raise SystemExit(f"{op}: {reply.get('error')}")
        return reply["result"]

    start = rpc("roll.snapshot")["latest"]
    c.evaluate(ROLL % (a.n, DC_ACT1_MEDIUM), context="server", timeout=15.0)
    time.sleep(a.wait)
    records = rpc("roll.since", {"seq": start, "limit": 500})["events"]
    snap = rpc("roll.snapshot")
    if a.out:
        a.out.write_text(json.dumps({"records": records, "snapshot": snap}, indent=1, sort_keys=True) + "\n")
    print(
        f"fired {json.dumps(snap.get('fired'), sort_keys=True)} seen {json.dumps(snap.get('seen'), sort_keys=True)}"
        f" pending {snap.get('pending')}"
    )
    checks = grade(snap, records, a.n, a.dc)
    for v, name, detail in checks:
        print(f"{v:4} {name:8} {detail}")
    return 1 if any(v == "FAIL" for v, _, _ in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
