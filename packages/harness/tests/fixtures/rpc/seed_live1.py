"""Rebuild the seed fixtures (ref_live1.jsonl, roll_live1.jsonl, scn_live1.jsonl) from the live1 probe's raw outputs.

    uv run python packages/harness/tests/fixtures/rpc/seed_live1.py [<live1 dir>]

Inputs (local, not in git): runtime/probes/live1/ref_events.jsonl (Osiris events of the referee probe) and l3.json
run 0 (24 passive Perception outcomes and the roll streams before/after). seed_live1.lua plays them through the real
Server/Referee.lua, RollLog.lua and Scenario.lua in Docker Lua 5.4 and prints the RPC replies as fixture lines; this
script scrubs them (bg3ai.rpc_record.scrub) and writes one file per op family. Until the live worker records whole
sessions with BG3AI_RPC_RECORD, these are the fixtures the fake mod replays for ref.*, roll.* and scn.*.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from bg3ai.rpc_record import scrub  # pyright: ignore[reportMissingTypeStubs] - the bridge ships no py.typed

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
DEFAULT_LIVE1 = Path("/data/Repos/bg3-ai/runtime/probes/live1")
IMAGE = "nickblah/lua:5.4"


def lua(v: object) -> str:
    """A Python value as a Lua literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid Lua for this data (ASCII ids and names)
    if isinstance(v, list):
        return "{" + ",".join(lua(x) for x in cast(list[object], v)) + "}"
    if isinstance(v, dict):
        items = cast(dict[object, object], v).items()
        return "{" + ",".join(f"[{json.dumps(str(k))}]={lua(x)}" for k, x in items) + "}"
    raise TypeError(type(v))


def data_chunk(live1: Path) -> str:
    lines = [json.loads(t) for t in (live1 / "ref_events.jsonl").read_text().splitlines() if t.strip()]
    events = [e for _, e in sorted(enumerate(lines), key=lambda p: (p[1]["t"], p[0]))]
    run0 = json.loads((live1 / "l3.json").read_text())[0]
    rolls = [{"res": 1 if c == "S" else 0, "crit": int(k)} for c, k in zip(run0["pattern"], run0["crit"], strict=True)]
    data = {
        "events": [{"ev": e["ev"], "args": e["args"], "t": e["t"], "tag": e["tag"]} for e in events],
        "rolls": rolls,
        "streams0": run0["before"],
        "streams1": run0["after"],
    }
    return "return " + lua(data) + "\n"


def build(live1: Path) -> dict[str, list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "data.lua").write_text(data_chunk(live1))
        out = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{REPO}:/repo:ro",
                "-v",
                f"{tmp}:/data:ro",
                "-w",
                "/repo",
                IMAGE,
                "lua",
                str((HERE / "seed_live1.lua").relative_to(REPO)),
                "mod/Mods/BG3AI/ScriptExtender/Lua",
                "/data/data.lua",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        ).stdout
    files: dict[str, list[dict[str, Any]]] = {"ref": [], "roll": [], "scn": []}
    for text in out.splitlines():
        line: dict[str, Any] = scrub(json.loads(text))
        files[line["calls"][0]["op"].split(".")[0]].append(line)
    return files


def main(argv: list[str]) -> int:
    live1 = Path(argv[0]) if argv else DEFAULT_LIVE1
    for family, lines in build(live1).items():
        path = HERE / f"{family}_live1.jsonl"
        path.write_text("".join(json.dumps(x, separators=(",", ":"), sort_keys=True) + "\n" for x in lines))
        print(f"{path.name}: {len(lines)} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
