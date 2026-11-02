"""Hand calls for the bench-v0.10 live window (the v09d driver's shape): bridge tools, RPC ops, Lua, sleeps.

    uv run --project bridge python tools/dev/live_v010/call.py <label> <tool> ['<json kwargs>'] [<tool> ...]
    ... call.py <label> rpc:<op> ['<json params>']      (crpc:<op> = client context)
    ... call.py <label> lua:<server|client> '<lua>'
    ... call.py <label> key:<SDL name>                   (a raw key press through the input shim, e.g. key:ESCAPE)
    ... call.py <label> shot:<name>                      (a screenshot saved to runtime/probes/v010/<name>.jpg)
    ... call.py <label> sleep <s>

Each result is printed and appended to runtime/probes/v010/calls.jsonl (common.rec)."""

from __future__ import annotations

import json
import sys
import time

from common import call, lua, rec, save_image, server


def run(label: str, tool: str, raw: str | None) -> None:
    t0 = time.time()
    if tool == "sleep":
        time.sleep(float(raw or 1))
        rec(label, "sleep", raw, f"slept {raw} s", t0)
    elif tool.startswith(("rpc:", "crpc:")):
        op = tool.split(":", 1)[1]
        srv = server()
        try:
            out = srv.game.rpc(op, json.loads(raw) if raw else {}, context="client" if tool[0] == "c" else "server")
        except Exception as e:
            out = f"EXCEPTION {type(e).__name__}: {e}"
        rec(label, tool, raw, out, t0)
    elif tool.startswith("lua:"):
        lua(raw or "return 1", tool[4:], label)
    elif tool.startswith("key:"):
        inp = server()._inp()  # world_tools._inp: the shim client in agent mode, as the tools use it
        if inp is None:
            rec(label, tool, None, "no input shim", t0)
        else:
            inp.key(tool[4:])
            rec(label, tool, None, "pressed", t0)
    elif tool.startswith("shot:"):
        rec(label, tool, None, save_image(server().screenshot(scale=0.5), f"{label}-{tool[5:]}"), t0)
    else:
        call(label, tool, json.loads(raw) if raw else None)


def main(argv: list[str]) -> int:
    label, rest = argv[0], argv[1:]
    while rest:
        tool = rest.pop(0)
        takes = tool == "sleep" or tool.startswith(("lua:", "rpc:", "crpc:")) or not tool.startswith(("key:", "shot:"))
        raw = (
            rest.pop(0)
            if takes and rest and (tool == "sleep" or tool.startswith("lua:") or rest[0][:1] in "{[")
            else None
        )
        run(label, tool, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
