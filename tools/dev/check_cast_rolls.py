"""Live check ($0) of the forced-hit fix through the harness itself: use_ability attacks and Sacred Flame roll.

ops/logs/arena-dice.md "H4 cast path": a cast the harness started (Osi.UseSpell) carried IgnoreSpellRolls, so its
attack roll / saving throw was skipped. The fix (Server/ActionsCast.lua A.roll_cast_state) drops the flag from the
cast's SpellCastState at OnCreate. This makes the casts through bg3ai.tools.combat.use_ability, the agent's own tool,
at a target given +20 AC (attacks) and with no boost (Sacred Flame's DEX save), and counts hits.

One command, a fight loaded with the mod from this branch, during the party's turn (the casters must be able to act;
the caster's action resources are refilled after each cast, and the target healed: probe-only state, nothing saved):
    uv run --project bridge python tools/dev/check_cast_rolls.py --target <uuid> --attackers <who>,<who> \\
        [--ability "Ranged Attack"] [--n 6] [--sf-caster <who>] [--sf-n 6] [--out runtime/probes/cast-check.json]
<who> as use_ability takes it (a name, handle or uuid).

PASS: attacks miss at least once at +20 AC (AC ~35 needs a natural 20; before the fix 30/30 hit) and no cast failed
with "without its roll"; Sacred Flame: at least one save made (0 damage) in --sf-n casts, or say how many.
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

SOURCE = "BG3AI_CastCheck"
REFILL = """local e = Ext.Entity.Get(%s)
for _, list in pairs(e.ActionResources.Resources) do
    for _, r in ipairs(list) do r.Amount = r.MaxAmount end
end
e:Replicate("ActionResources")
return 'ok'"""


def verdict(rows: list[dict[str, Any]]) -> list[str]:
    """Lines per kind (attack / save) and PASS or FAIL. Pure (tests)."""
    lines, ok = [], True
    for kind in ("attack", "save"):
        rs = [r for r in rows if r["kind"] == kind]
        if not rs:
            continue
        cast = [r for r in rs if not r["failed"]]
        unrolled = [r for r in rs if "without its roll" in r["text"]]
        hits = sum(1 for r in cast if r["damage"] > 0)
        lines.append(
            f"{kind:6} {len(cast)}/{len(rs)} cast, {hits} did damage, {len(cast) - hits} did none; "
            f"{len(unrolled)} failed 'without its roll'"
        )
        if unrolled or not cast:
            ok = False
        elif kind == "attack" and hits == len(cast):
            ok = False
            lines.append("attack: every cast hit at +20 AC: still forced")
        elif kind == "save" and hits == len(cast):
            lines.append(f"save: no save made in {len(cast)} (rolled saves can all fail; rerun with more if unsure)")
    lines.append("PASS" if ok else "FAIL")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--target", required=True, help="the target's uuid")
    ap.add_argument("--attackers", default="", help="comma list of who (use_ability's who), used in turn")
    ap.add_argument("--ability", default="Ranged Attack")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--sf-caster", default="")
    ap.add_argument("--sf-n", type=int, default=6)
    ap.add_argument("--ac-boost", type=int, default=20)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)

    from bg3ai.luadebug import LuaDebugClient
    from bg3ai.tools import combat, plumbing

    dbg = LuaDebugClient(connect_timeout=5.0)
    dbg.connect()

    def lua(code: str) -> str:
        return dbg.evaluate(code, context="server", timeout=30.0, statement=True)

    tgt = json.dumps(a.target)

    def hp() -> int:
        return int(float(lua(f"return Osi.GetHitpoints({tgt})")))

    def one(kind: str, who: str, ability: str) -> dict[str, Any]:
        lua(REFILL % json.dumps(plumbing._who(who)))
        before = hp()
        try:
            text = combat.use_ability(ability, target=a.target, who=who)
        except Exception as e:  # a check reports, it doesn't stop the other casts
            text = f"use_ability: FAILED — {e}"
        time.sleep(2.0)  # a projectile's hit can land after the tool's result
        row = {"kind": kind, "who": who, "ability": ability, "damage": before - hp(), "text": str(text)[:400]}
        row["failed"] = "FAILED" in row["text"].split("\n")[0]
        lua(f"Osi.SetHitpointsPercentage({tgt}, 100.0) return 'ok'")
        print(json.dumps(row, ensure_ascii=False))
        return row

    rows: list[dict[str, Any]] = []
    attackers = [x.strip() for x in a.attackers.split(",") if x.strip()]
    boost = json.dumps(f"AC({a.ac_boost})")
    if attackers and a.ac_boost:
        lua(f"Osi.AddBoosts({tgt}, {boost}, {json.dumps(SOURCE)}, {tgt}) return 'ok'")
    try:
        for i in range(a.n if attackers else 0):
            rows.append(one("attack", attackers[i % len(attackers)], a.ability))
    finally:
        if attackers and a.ac_boost:
            lua(f"Osi.RemoveBoosts({tgt}, {boost}, 0, {json.dumps(SOURCE)}, {tgt}) return 'ok'")
    for _ in range(a.sf_n if a.sf_caster else 0):
        rows.append(one("save", a.sf_caster, "Sacred Flame"))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    for line in verdict(rows):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
