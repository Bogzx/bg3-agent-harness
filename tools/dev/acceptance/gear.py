"""Section gear: equip_best and the lantern plan."""

from __future__ import annotations

import re
import time

from .core import BASE, S, call, first_line, item, load_verified, took


def sec_gear() -> None:
    if not load_verified(BASE):
        return
    hands = (
        'local o={} for _,r in pairs(Osi.DB_Players:Get(nil)) do local c=r[1]; local it=Osi.GetEquippedItem(c,"Melee Main Weapon") '
        'o[#o+1]=Osi.ResolveTranslatedString(Osi.GetDisplayName(c)).."="..(it and Osi.ResolveTranslatedString(Osi.GetDisplayName(it)) or "-") end '
        'return table.concat(o,"; ")'
    )
    before = S.game.eval(hands, context="server")
    gear = call("get_inventory", filter="gear")
    # Job push: equip_best runs several short jobs; with the push each needs ~1 job.poll (was ~10 per 1 s job).
    from bg3ai import jobpush

    counts: dict[str, int] = {}
    orig = S.game.rpc

    def counting(op, *a, **k):
        counts[op] = counts.get(op, 0) + 1
        return orig(op, *a, **k)

    jp = jobpush.shared(S.game.host, S.game.port)
    jp.ensure()
    S.game.rpc = counting
    try:
        out, dt = took("equip_best")
    finally:
        S.game.rpc = orig
    jobs = sum(v for k, v in counts.items() if k.startswith("act."))
    polls = counts.get("job.poll", 0)
    ok = jp.seen and jobs and polls <= jobs + 3
    item(
        "job push latency",
        "PASS" if ok else "FAIL",
        f"equip_best {dt:.2f}s: {jobs} jobs, {polls} job.polls, {sum(counts.values())} RPCs; listener heard marks={jp.seen}",
    )
    time.sleep(1.5)
    after = S.game.eval(hands, context="server")
    holder = re.search(r"(\w+)=Moonlantern", after or "")
    caster_ok = holder and holder.group(1) in ("Gale", "Shadowheart", "Wyll")
    ok = "FAILED" not in out and dt < 5 and (caster_ok or "Moonlantern" not in (before or ""))
    item(
        "equip_best + lantern plan",
        "PASS" if ok else "FAIL",
        f"{dt:.1f}s; before [{before}] after [{after}]; {first_line(out)}",
    )
    load_verified(BASE)
