"""C30 referee events in a fight: scripted damage on the nearest enemy, then the mod's referee ring (ref.since).

live1 §6 hit Sneem (S_GOB_RaiderFunny, an Act 1 goblin at the grove path) on the S007 grove load; live5 ran the same
uuid on S025-end (Act 2), where that goblin isn't, so ApplyDamage did nothing and no event fired. The probe now picks
its target on the save it runs on: the nearest living hostile (or the nearest character whose name holds the arg).
Two hits from the host as live1 did (1 piercing, then 300 fire). PASS: the ring has a `kill` of the target; the
other records (combat_started, died, crime, ...) are listed in the evidence.

    c30_fight_events.py [<name>]
"""

from __future__ import annotations

import json
from collections import Counter

from probelib import Probe, Verdict, arg, find_char, main

HIT = 'Osi.ApplyDamage(%s, %d, "%s", Osi.GetHostCharacter()) return "ok"'
RADIUS_M = 40.0  # live1: the grove-path goblins stood 26-39 m from the party


def judge(target: dict, events: list[dict]) -> Verdict:
    kinds = Counter(str(e.get("ev")) for e in events)
    seen = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) or "no referee records"
    who = target.get("name")
    kill = next((e for e in events if e.get("ev") == "kill" and e.get("victim_uuid") == target["uuid"]), None)
    if kill:
        return "PASS", f"kill of {who} (killer {kill.get('killer')}, by_party {kill.get('by_party')}); ring: {seen}"
    return "FAIL", f"no kill of {who} in the referee ring after 301 damage; ring: {seen}"


def run(p: Probe) -> Verdict:
    chars = p.nearby(RADIUS_M)
    want = arg(p, 0, "")
    t = find_char(chars, want) if want else next((c for c in chars if c.get("relation") == "hostile"), None)
    if t is None:
        return "FAIL", f"no {want or 'hostile'} within {RADIUS_M:.0f} m (nearby: " + ", ".join(
            str(c.get("name")) for c in chars[:8]
        ) + ")"
    p.log(f"target {t['name']} {t['uuid']} at {t.get('dist')} m")
    mark = p.ref_mark()
    p.lua(HIT % (json.dumps(t["uuid"]), 1, "Piercing"))
    p.sleep(6.0)
    p.lua(HIT % (json.dumps(t["uuid"]), 300, "Fire"))
    p.sleep(5.0)
    events = p.ref_after(mark)
    for e in events:
        p.log(json.dumps(e))
    return judge(t, events)


if __name__ == "__main__":
    raise SystemExit(main(run))
