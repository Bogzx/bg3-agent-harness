"""Dry check of party-build.md level-up plan lines against what the game will offer (read-only, no lock needed).

  uv run --project bridge python tools/dev/check_plans.py [--offers] [--ahead 2]

Reads each party/camp member's next levels from the game's Progression data (class AND subclass rows by Name +
Level: the spell picks sit in the subclass rows, research/impl-se.md handoff 5) via tools/dev/plan_offers.lua,
then checks the stored line `- <Name> L<n>: {json}` for that level: every spell/cantrip/passive named must be
offered and not already known, counts must fill each choice, a feat only where the level allows one and not one
already taken (unless the game lets it repeat, like Ability Improvement).
Exit 1 when a line is invalid or missing a needed pick.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge" / "src"))
os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")

from bg3ai import leveling  # noqa: E402

LUA = Path(__file__).with_name("plan_offers.lua")


def offers(game, uuids: list[str], ahead: int) -> list[dict]:
    code = f"local MEMBERS = {{{', '.join(json.dumps(u) for u in uuids)}}}\nlocal AHEAD = {ahead}\n" + LUA.read_text()
    return json.loads(game.eval(code, context="server", timeout=60))


def _norm(s: str) -> str:
    return s.strip().lower().replace("armor", "armour").replace("agonizing", "agonising")


def check_level(lv: dict, plan: dict | None, known: set[str], taken_feats: set[str] = frozenset()) -> list[str]:
    """Problems of one plan line against one level's offers ([] = valid). taken_feats: the non-repeatable feats
    the character already has."""
    if plan is None:
        need = [f"{s['amount']} from {s['list'] or 'spells'}" for s in lv["spells"]] + \
               [f"{p['amount']} from {p['list'] or 'passives'}" for p in lv["passives"]] + \
               (["a feat"] if lv.get("feat") else []) + ([f"{lv['skills']} skill(s)"] if lv.get("skills") else [])
        return [f"no plan line (picks needed: {'; '.join(need)})"] if need else []
    probs = []
    k = {_norm(x) for x in known}
    cantrip_opts = {_norm(o["name"]) for s in lv["spells"] for o in s["options"] if o.get("level") == 0}
    spell_opts = {_norm(o["name"]) for s in lv["spells"] for o in s["options"] if o.get("level") != 0}
    for key, opts in (("cantrips", cantrip_opts), ("spells", spell_opts)):
        for name in plan.get(key) or []:
            n = _norm(name)
            if n in k:
                probs.append(f"{key[:-1]} {name}: already known")
            elif n not in opts:
                probs.append(f"{key[:-1]} {name}: not offered")
    for key, want in (("cantrips", lambda o: o.get("level") == 0), ("spells", lambda o: o.get("level") != 0)):
        need = sum(s["amount"] for s in lv["spells"] if s["options"] and all(want(o) for o in s["options"]))
        have = len(plan.get(key) or [])
        if have < need:
            probs.append(f"{key}: {have} of {need} picks")
    pass_opts = {_norm(o) for p in lv["passives"] for o in p["options"]}
    for name in plan.get("passives") or []:
        if _norm(name) in k:
            probs.append(f"passive {name}: already known")
        elif _norm(name) not in pass_opts:
            probs.append(f"passive {name}: not offered")
    need = sum(p["amount"] for p in lv["passives"])
    if len(plan.get("passives") or []) < need:
        probs.append(f"passives: {len(plan.get('passives') or [])} of {need} picks")
    if plan.get("feat") and not lv["feat"]:
        probs.append(f"feat {plan['feat']}: this level has no feat")
    elif plan.get("feat") and _norm(plan["feat"]) in {_norm(f) for f in taken_feats}:
        # S085: Claude L12 Tough (taken at L4) passed and level_up failed on it
        probs.append(f"feat {plan['feat']}: already taken (taken: {', '.join(sorted(taken_feats))})")
    # S053: placeholder lines ({} "assuming no picks") passed; a feat level or skill picks left out of the line are
    # decisions the game would take by default (Ability Improvement +2 on the primary ability, the first skills).
    if lv.get("feat") and not plan.get("feat"):
        probs.append('feat: this level picks a feat (plan.feat: a feat name, or "Ability Improvement" + abilities)')
    if lv.get("skills") and len(plan.get("skills") or []) < lv["skills"]:
        probs.append(f"skills: {len(plan.get('skills') or [])} of {lv['skills']} picks")
    return probs


def offer_text(lv: dict, known: set[str], cap: int = 24, taken_feats: set[str] = frozenset()) -> str:
    k = {_norm(x) for x in known}
    bits = []
    for s in lv["spells"]:
        names = [o["name"] for o in s["options"] if _norm(o["name"]) not in k]
        bits.append(f"pick {s['amount']} of {s['list']}: " + ", ".join(names[:cap]) + (" …" if len(names) > cap else ""))
    for p in lv["passives"]:
        names = [o for o in p["options"] if _norm(o) not in k]  # never offer what they already have (keeper input)
        bits.append(f"pick {p['amount']} of {p['list']}: " + ", ".join(names[:cap]) + (" …" if len(names) > cap else ""))
    if lv["skills"]:
        bits.append(f"{lv['skills']} skill(s)")
    if lv["feat"]:
        bits.append(f"feat (already taken: {', '.join(sorted(taken_feats))})" if taken_feats else "feat")
    return " | ".join(bits) or "no choices"


def check_member(m: dict, plans: dict) -> list[dict]:
    """One member's offers (plan_offers.lua) vs the stored plans → [{name, level, plan, problems, offers}]."""
    rows = []
    known = set(m["known"]) | set(m.get("known_passives") or [])
    taken = set(m.get("known_feats") or [])
    repeatable = {_norm(f) for f in m.get("repeatable_feats") or []}
    for i, lv in enumerate(m["levels"]):
        plan = plans.get((m["name"].lower(), lv["level"]))
        # a later level ahead can't see the earlier ones' picks: count them as known
        if i > 0:
            prev = plans.get((m["name"].lower(), lv["level"] - 1)) or {}
            known |= set(prev.get("spells") or []) | set(prev.get("cantrips") or []) | set(prev.get("passives") or [])
            if prev.get("feat") and _norm(prev["feat"]) not in repeatable:
                taken.add(prev["feat"])
        rows.append({"name": m["name"], "level": lv["level"], "plan": plan,
                     "problems": check_level(lv, plan, known, taken),
                     "offers": offer_text(lv, known, cap=40, taken_feats=taken)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offers", action="store_true", help="print each level's offers")
    ap.add_argument("--ahead", type=int, default=2)
    ap.add_argument("--json", help="also write [{name, level, plan, problems, offers}] for every checked line here")
    ap.add_argument("--memory", help="memory dir (default: playthroughs/$BG3AI_PLAYTHROUGH/memory)")
    a = ap.parse_args()
    from server_view import S as server  # noqa: N811
    st = server.game.rpc("state.status")
    uuids = [r["uuid"] for r in (st.get("party") or []) + (st.get("camp") or []) if isinstance(r, dict) and r.get("uuid")]
    mem = Path(a.memory) if a.memory else ROOT / "playthroughs" / os.environ["BG3AI_PLAYTHROUGH"] / "memory"
    rows = []
    plans = leveling.load_plans((mem / leveling.PLAN_FILE).read_text())
    for m in offers(server.game, uuids, a.ahead):
        cls = ", ".join(f"{c.get('class')}/{c.get('sub') or '-'} {c.get('level')}" for c in m["classes"])
        for r in check_member(m, plans):
            probs = r["problems"]
            mark = "OK " if not probs else "BAD"
            line = json.dumps(r["plan"]) if r["plan"] is not None else "(no line)"
            print(f"{mark} {m['name']} L{r['level']} ({cls}): {line}"
                  + (f"\n    problems: {'; '.join(probs)}" if probs else ""))
            if a.offers or probs:
                print(f"    offers: {r['offers']}")
            rows.append(r)
    if a.json:
        Path(a.json).write_text(json.dumps(rows))
    return 1 if any(r["problems"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
