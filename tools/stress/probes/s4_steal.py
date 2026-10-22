"""S4 pickpocket success (live2 §1): steals succeed only while unseen. The probe makes the thief Invisible with
+20 Sleight of Hand (probe-only cheats on the save copy, as live2), then takes the best-odds item from the named
NPC. PASS: the referee recorded a successful pickpocket and no crime.

    s4_steal.py [<npc name>=Mattis] [<thief name>]   (default thief: the host)
"""

from __future__ import annotations

import json

from probe_pickpocket import pp_records, steal
from probelib import Probe, Verdict, arg, main

CHEAT = """local t = %s
Osi.AddBoosts(t, "Skill(SleightOfHand,20)", "", t)
Osi.ApplyStatus(t, "INVISIBILITY", 60, 1)
return tostring(Osi.HasActiveStatus(t, "INVISIBILITY"))"""
HOST = "Osi.GetHostCharacter()"


def judge(why: str, events: list[dict]) -> Verdict:
    if why:
        return "FAIL", why
    took, failed = pp_records(events, True), pp_records(events, False)
    crimes = sorted({str(e.get("type")) for e in events if e.get("ev") == "crime"})
    if took and not crimes:
        return "PASS", f"stole {took[0].get('item')} x{took[0].get('count')} from {took[0].get('npc')}, no crime"
    return "FAIL", f"invisible steal: success {len(took)}, failed {len(failed)}, crimes {crimes}"


def run(p: Probe) -> Verdict:
    thief = arg(p, 1, "")
    if thief:
        m = next(
            (c for c in p.game.rpc("state.status").get("party") or [] if thief.lower() in str(c.get("name")).lower()),
            None,
        )
        if m is None:
            return "FAIL", f"no party member {thief}"
        who = json.dumps(m["uuid"])
    else:
        who = HOST
    p.log(f"invisible: {p.lua(CHEAT % who)}")
    p.sleep(1.5)
    return judge(*steal(p, arg(p, 0, "Mattis"), thief or None))


if __name__ == "__main__":
    raise SystemExit(main(run))
