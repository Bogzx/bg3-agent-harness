"""F6 rename through Ext.Loca (live1 §8, L7; the benchmark's name perturbation): the named NPC's display-name handle
set to a test name in both contexts, read back in both and in state.nearby, then restored (the Loca table is
process-wide and survives loads, so a rename left in place leaks into the next run). PASS: renamed everywhere
and restored.

    f6_rename.py [<npc name>=Mattis]
"""

from __future__ import annotations

import json

from probelib import Probe, Verdict, arg, find_char, main

NEW = "Test Name"
HANDLE = "return tostring(Osi.GetDisplayName(%s))"
GET = "return Ext.Loca.GetTranslatedString(%s)"
SET = "return tostring(Ext.Loca.UpdateTranslatedString(%s, %s))"


def judge(orig: str, renamed: dict, restored: dict) -> Verdict:
    shown = f"{orig} -> server {renamed['server']!r}, client {renamed['client']!r}, nearby {renamed['nearby']!r}"
    if any(v != NEW for v in renamed.values()):
        return "FAIL", f"rename not seen everywhere: {shown}"
    if any(v != orig for v in restored.values()):
        return "FAIL", f"renamed ({shown}) but the restore left {restored}"
    return "PASS", f"{shown}; restored in both contexts"


def run(p: Probe) -> Verdict:
    name = arg(p, 0, "Mattis")
    who = find_char(p.nearby(30.0), name)
    if who is None:
        return "FAIL", f"no {name} within 30 m"
    h = json.dumps(p.lua(HANDLE % json.dumps(who["uuid"])))
    orig = p.lua(GET % h)

    def write(text: str) -> None:
        for ctx in ("server", "client"):
            p.log(f"{ctx} set {text!r}: {p.lua(SET % (h, json.dumps(text)), ctx)}")

    def read() -> dict:
        return {"server": p.lua(GET % h), "client": p.lua(GET % h, "client")}

    write(NEW)
    p.sleep(1.0)
    renamed = read()
    now = next((c for c in p.nearby(30.0) if c["uuid"] == who["uuid"]), {})
    renamed["nearby"] = now.get("name")
    write(orig)
    return judge(orig, renamed, read())


if __name__ == "__main__":
    raise SystemExit(main(run))
