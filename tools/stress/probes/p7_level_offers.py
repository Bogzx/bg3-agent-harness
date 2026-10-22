"""P7 level up, every class: what the game offers at each party/camp member's next levels (tools/dev/plan_offers.lua
through check_plans.offers) and whether the plan checker the keeper and level_up rely on accepts a legal pick.

The old check (tools/dev/check_plans.py) read the plans in a live playthrough's memory, which belong to a later
party than the row's save, so its exit 1 meant nothing here. This probe needs no memory: for every member and level
it builds the plan line a player could write from the offers (the first options of each choice, "Ability
Improvement" for a feat) and checks it with check_plans.check_member. PASS: every member has offers for the next
level, no choice offers fewer options than it asks for, and every built line is valid. Offers in offers.json.

    p7_level_offers.py [<levels ahead>=2]
"""

from __future__ import annotations

import json
import os
import sys

from probelib import REPO, Probe, Verdict, arg, main

if str(REPO / "tools" / "dev") not in sys.path:
    sys.path.insert(0, str(REPO / "tools" / "dev"))
_had = os.environ.get("BG3AI_PLAYTHROUGH")
import check_plans as cp  # noqa: E402

if _had is None:  # check_plans defaults it to a live playthrough's name; this probe reads no playthrough
    os.environ.pop("BG3AI_PLAYTHROUGH", None)

SKILLS = ("Athletics", "Acrobatics", "Stealth", "Perception", "Insight", "Persuasion", "Arcana", "History")


def plan_for(lv: dict, known: set[str]) -> dict:
    """A legal plan line for one level's offers: the first unknown options of each choice."""
    k = {cp._norm(x) for x in known}
    plan: dict = {"cantrips": [], "spells": [], "passives": []}
    for s in lv["spells"]:
        opts = [o for o in s["options"] if cp._norm(o["name"]) not in k]
        for o in opts[: s["amount"]]:
            plan["cantrips" if o.get("level") == 0 else "spells"].append(o["name"])
            k.add(cp._norm(o["name"]))
    for ps in lv["passives"]:
        opts = [o for o in ps["options"] if cp._norm(o) not in k]
        plan["passives"] += opts[: ps["amount"]]
        k |= {cp._norm(o) for o in opts[: ps["amount"]]}
    if lv.get("feat"):
        plan["feat"] = "Ability Improvement"
    if lv.get("skills"):
        plan["skills"] = list(SKILLS[: lv["skills"]])
    return plan


def short(lv: dict) -> list[str]:
    """Choices that offer fewer options than they ask for."""
    out = [
        f"{s['list'] or 'spells'} {len(s['options'])} of {s['amount']}"
        for s in lv["spells"]
        if len(s["options"]) < s["amount"]
    ]
    return out + [
        f"{ps['list'] or 'passives'} {len(ps['options'])} of {ps['amount']}"
        for ps in lv["passives"]
        if len(ps["options"]) < ps["amount"]
    ]


def judge(members: list[dict]) -> Verdict:
    probs, classes = [], []
    for m in members:
        cls = "/".join(
            f"{c.get('class')}{'-' + c['sub'] if c.get('sub') else ''} {c.get('level')}" for c in m.get("classes") or []
        )
        classes.append(f"{m['name']} {cls}")
        if not m.get("levels"):
            probs.append(f"{m['name']}: no level-up offers")
            continue
        known = set(m.get("known") or []) | set(m.get("known_passives") or [])
        plans = {}
        for lv in m["levels"]:
            plans[(m["name"].lower(), lv["level"])] = plan_for(lv, known)
            known |= set(plans[(m["name"].lower(), lv["level"])]["spells"])
            probs += [f"{m['name']} L{lv['level']}: {s}" for s in short(lv)]
        probs += [
            f"{m['name']} L{r['level']}: {'; '.join(r['problems'])}" for r in cp.check_member(m, plans) if r["problems"]
        ]
    what = f"{len(members)} members ({'; '.join(classes)})"
    if not members:
        return "FAIL", "no party or camp member read"
    if probs:
        return "FAIL", f"{what}: " + " | ".join(probs[:8])
    return "PASS", f"{what}: every next level offered and a legal plan line checks out"


def run(p: Probe) -> Verdict:
    st = p.game.rpc("state.status") or {}
    uuids = [
        r["uuid"] for r in (st.get("party") or []) + (st.get("camp") or []) if isinstance(r, dict) and r.get("uuid")
    ]
    members = cp.offers(p.game, uuids, int(arg(p, 0, "2")))
    (p.out / "offers.json").write_text(json.dumps(members, indent=1))
    return judge(members)


if __name__ == "__main__":
    raise SystemExit(main(run))
