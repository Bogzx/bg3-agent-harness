"""C21 fleeing: the bridge's flee tool on a fight in progress (S049-end: sewers, mid-fight vs Aelis + mephits).

The game lets a character flee only with every enemy 27 m away (research/ovh/live2.md §2); the save starts at 5.5 m,
so the probe first teleports the fleeing member GAP_M away from the enemies (a probe-only cheat on the save copy,
as live2 did by hand), then calls flee() as the agent would. PASS: flee answered "fled:" and the member is out of
the fight (state.combat); the referee's combat_ended is only reported (it fires only when the last member leaves).

    flee.py [<member name>]   (default: the first party member in the turn order)
"""

from __future__ import annotations

import json
import math

from probelib import Probe, Verdict, arg, main

GAP_M = 50.0  # live2 §2: 47 m worked; the tool refuses under 27 m

POS = "local p=Ext.Entity.Get(%s).Transform.Transform.Translate return Ext.Json.Stringify({p[1],p[2],p[3]})"
TELEPORT = 'Osi.TeleportToPosition(%s, %f, %f, %f, "", 0, 0, 0, 0, 1) return "ok"'


def fighters(cs: dict) -> tuple[list[dict], list[dict]]:
    order = [o for o in cs.get("order") or [] if isinstance(o, dict) and o.get("uuid") and not o.get("dead")]
    return [o for o in order if o.get("relation") == "party"], [o for o in order if o.get("relation") == "hostile"]


def away(me: list[float], foes: list[list[float]], gap: float) -> list[float]:
    """A point `gap` m from the foes' centre, on the far side of `me` (x/z plane; y kept)."""
    cx = sum(f[0] for f in foes) / len(foes)
    cz = sum(f[2] for f in foes) / len(foes)
    dx, dz = me[0] - cx, me[2] - cz
    n = math.hypot(dx, dz)
    if n < 1e-6:  # standing on the centre: any direction
        dx, dz, n = 1.0, 0.0, 1.0
    return [cx + dx / n * gap, me[1], cz + dz / n * gap]


def judge(result: str, still_in: bool, events: list[dict]) -> Verdict:
    ended = any(e.get("ev") == "combat_ended" for e in events)
    tail = f"; referee combat_ended {'fired' if ended else 'not fired (others still fight)'}"
    if not result.startswith("fled:"):
        return "FAIL", f"flee answered: {result[:300]}"
    if still_in:
        return "FAIL", f"flee said '{result[:200]}' but state.combat still lists the member"
    return "PASS", result[:300] + tail


def run(p: Probe) -> Verdict:
    cs = p.game.rpc("state.combat") or {}
    if not cs.get("active"):
        return "FAIL", "no fight in progress on the save (state.combat inactive)"
    party, foes = fighters(cs)
    want = arg(p, 0, "")
    me = (
        next((m for m in party if want.lower() in str(m.get("name") or "").lower()), None)
        if want
        else (party[0] if party else None)
    )
    if me is None or not foes:
        return "FAIL", f"no {'member ' + want if want else 'party member'} or no enemy in the turn order"
    pos = json.loads(p.lua(POS % json.dumps(me["uuid"])))
    foe_pos = [json.loads(p.lua(POS % json.dumps(f["uuid"]))) for f in foes]
    to = away(pos, foe_pos, GAP_M)
    p.log(f"teleport {me['name']} {pos} -> {to} (enemies {len(foes)})")
    p.lua(TELEPORT % (json.dumps(me["uuid"]), *to))
    p.sleep(2.0)
    mark = p.ref_mark()
    result = p.S.flee(who=me["name"])
    p.log(result)
    still_in = any(o.get("uuid") == me["uuid"] for o in fighters(p.game.rpc("state.combat") or {})[0])
    return judge(result, still_in, p.ref_after(mark))


if __name__ == "__main__":
    raise SystemExit(main(run))
