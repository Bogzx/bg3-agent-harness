"""Live probe ($0, REVIEW-5 F1/F3/F4): once a harness cast loses one check flag, does the engine refuse what a human
player can't do?

A cast the harness orders (Osi.UseSpell) carries IgnoreHasSpell, IgnoreCastChecks, IgnoreSpellRolls,
IgnoreTargetChecks; the mod drops IgnoreSpellRolls at the cast state's creation (Server/ActionsRolls.lua). The probe
switch `A.CAST_DROP_EXTRA` also drops check flags from the awaited cast. Window 6: dropping all three at once refused
every cast, the control too; so each mode drops ONE flag (--modes off,IgnoreTargetChecks,IgnoreCastChecks,
IgnoreHasSpell; "all" = the three). The guard's own rule checks (Server/GuardRules.lua) are switched off for the probe
(G.RULES_OFF), so the engine's verdict is what is measured.

Each case goes through bg3ai.tools.combat.use_ability (the agent's tool), once per mode:
    control       a normal attack at --target: a mode is usable only if this still casts
    hold_person   Hold Person at --target (an undead or non-humanoid: TargetConditions refuse it)   F1
    heavy_shove   Shove at --heavy (Strength x 12 < its weight: CanShoveWeight refuses it)            F1
    prone_attack  the attack while the caster has PRONE (applied by the probe), in its turn            F4
    jump_webbed   Jump 3 m while the caster has --immobile (ENSNARED, live: blocks Movement)           F3
    rage_ooc      Rage out of combat (Requirements Combat)                                             F3
    surge_ooc     Action Surge out of combat (Requirements Combat)                                     F3
--plan live (window 7 follow-up): the default the mod now ships (IgnoreCastChecks + IgnoreTargetChecks dropped,
mode "default"), the guard's rules off, on the casts a bench run needs; each case has its expected verdict and the
summary says PASS/FAIL per case and whether the engine's own reason (SpellCastOutcome) was read on a refusal:
    rage_ooc      Rage out of combat: refused by the engine (first, before the fight)
    control       a normal attack at --target (out of combat it starts the fight): cast
    shout         Dash: cast            area      --area at --target (Thunderwave): cast
    plain         Sacred Flame (a save, no attack roll: no reaction prompt): cast
    turn_undead   Turn Undead (a Shout whose TargetConditions filter who it reaches): cast
    hide          Hide: cast            two_step  --two-step at --two-step-target, landing 1.5 m beside the caster
                                                  (A.TASK_POS2 on for it; not run without a target): cast
    heavy_shove   Shove at --heavy: refused by the engine, or cast = only the guard stops it (not run without --heavy)
    uv run --project bridge python tools/dev/probe_cast_checks.py --plan live --target <undead enemy> \\
        [--heavy <heavy enemy>] [--two-step Revivify --two-step-who <cleric> --two-step-target <a body>]
Verdict per case and mode: cast (the engine let it through), refused (the game refused the cast: CastSpellFailed),
guard (the harness refused before any order), not run (precondition not met: not its turn, in/out of combat, a
status that didn't apply). Per usable mode: a case refused only with that flag gone = the engine re-checks it.

Probe-only state (save nothing): action resources refilled and the target healed before each order, statuses applied
and removed; the switch and RULES_OFF restored at the end.

One command per scene, a fight loaded with this branch's mod (combat cases; the caster's turn: the probe ends other
party members' turns to reach it), then a scene out of combat for the ooc cases:
    uv run --project bridge python tools/dev/probe_cast_checks.py --plan combat --target <undead uuid> \\
        --heavy <heavy uuid> [--caster Karlach] [--hold-caster <who has Hold Person prepared>] [--immobile ENSNARED]
    uv run --project bridge python tools/dev/probe_cast_checks.py --plan ooc [--rager Karlach] [--surger <fighter>]
    [--modes off,IgnoreTargetChecks,...] [--only case,case] [--out runtime/probes/cast-checks.jsonl]
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

A_LUA = "Mods.BG3AI.BG3AI.Actions"
G_LUA = "Mods.BG3AI.BG3AI.Guard"
FLAGS = ("IgnoreTargetChecks", "IgnoreCastChecks", "IgnoreHasSpell")
REFILL = """local e = Ext.Entity.Get(%s)
for _, list in pairs(e.ActionResources.Resources) do
    for _, r in ipairs(list) do r.Amount = r.MaxAmount end
end
e:Replicate("ActionResources")
return 'ok'"""
# The awaited cast's flags after the write (nil when no cast state came)
OPTIONS = (
    "local w = "
    + A_LUA
    + """.roll_wait[Mods.BG3AI.BG3AI.Guard.guid(%s)] local o = w and w.job and w.job.roll and w.job.roll.options
return o and table.concat(o, ",") or "none\""""
)


def plan(name: str, a: argparse.Namespace) -> list[dict[str, Any]]:
    """The cases of a plan, from the CLI args. Pure (tests)."""
    if name == "combat":
        return [
            {"case": "control", "who": a.caster, "ability": a.attack, "target": a.target, "need": "combat"},
            {
                "case": "hold_person",
                "who": a.hold_caster,
                "ability": "Hold Person",
                "target": a.target,
                "need": "combat",
            },
            {"case": "heavy_shove", "who": a.caster, "ability": "Shove", "target": a.heavy, "need": "combat"},
            {
                "case": "prone_attack",
                "who": a.caster,
                "ability": a.attack,
                "target": a.target,
                "status": "PRONE",
                "need": "combat",
            },
            {
                "case": "jump_webbed",
                "who": a.caster,
                "ability": "Jump",
                "jump_m": 3.0,
                "status": a.immobile,
                "need": "combat",
            },
        ]
    if name == "live":
        cases = [
            {"case": "rage_ooc", "who": a.rager, "ability": "Rage", "need": "no_combat", "expect": "refused"},
            {
                "case": "control",
                "who": a.caster,
                "ability": a.attack,
                "target": a.target,
                "need": "any",
                "expect": "cast",
            },
            {"case": "shout", "who": a.caster, "ability": "Dash", "need": "combat", "expect": "cast"},
            {
                "case": "area",
                "who": a.area_caster,
                "ability": a.area,
                "target": a.target,
                "need": "combat",
                "expect": "cast",
            },
            {
                "case": "plain",
                "who": a.hold_caster,
                "ability": "Sacred Flame",
                "target": a.target,
                "need": "combat",
                "expect": "cast",
            },
            {"case": "turn_undead", "who": a.hold_caster, "ability": "Turn Undead", "need": "combat", "expect": "cast"},
            {"case": "hide", "who": a.caster, "ability": "Hide", "need": "combat", "expect": "cast"},
        ]
        if a.two_step_target:
            cases.append(
                {
                    "case": "two_step",
                    "who": a.two_step_who,
                    "ability": a.two_step,
                    "target": a.two_step_target,
                    "landing_m": 1.5,
                    "pos2": True,
                    "need": "any",
                    "expect": "cast",
                }
            )
        if a.heavy:
            cases.append(
                {
                    "case": "heavy_shove",
                    "who": a.caster,
                    "ability": "Shove",
                    "target": a.heavy,
                    "need": "combat",
                    "expect": "refused",
                }
            )
        return cases
    if name == "ooc":
        return [
            {"case": "rage_ooc", "who": a.rager, "ability": "Rage", "need": "no_combat"},
            {"case": "surge_ooc", "who": a.surger, "ability": "Action Surge", "need": "no_combat"},
        ]
    raise ValueError(f"no plan {name!r}")


def verdict(text: str) -> str:
    """The tool result as cast / refused / guard. Pure (tests)."""
    first = text.split("\n", 1)[0]
    if first.startswith("REFUSED") or "REFUSED" in first.split("—", 1)[0]:
        return "guard"
    if "FAILED" not in first:
        return "cast"
    if "the game refused the cast" in first or "the game would not" in first:
        return "refused"
    return "guard"


def switch(mode: str) -> str:
    """The Lua value of A.CAST_DROP_EXTRA for a mode: off (no check flag dropped), default (the shipped
    CAST_DROP_CHECKS), all, or one flag name. Pure (tests)."""
    if mode == "off":
        return "false"
    if mode == "default":
        return A_LUA + ".CAST_DROP_CHECKS"
    if mode == "all":
        return "true"
    if mode not in FLAGS:
        raise ValueError(f"unknown mode {mode!r}: off, default, all or one of {', '.join(FLAGS)}")
    return '{"' + mode + '"}'


def summary(rows: list[dict[str, Any]]) -> list[str]:
    """Per mode: is it usable (the control still casts), then each case's verdict and what it means. Pure (tests)."""
    by: dict[str, dict[str, str]] = {}
    modes: list[str] = []
    for r in rows:
        by.setdefault(r["case"], {})[r["mode"]] = r["verdict"]
        if r["mode"] != "off" and r["mode"] not in modes:
            modes.append(r["mode"])
    out = []
    for mode in modes:
        ctrl = by.get("control", {}).get(mode)
        usable = ctrl == "cast"
        out.append(
            f"mode {mode}: control {ctrl or 'not run'} -> "
            + ("usable" if usable else "can't be dropped (it stops a normal cast)" if ctrl == "refused" else "unknown")
        )
        for case, m in by.items():
            if case == "control" or mode not in m:
                continue
            off, on = m.get("off"), m[mode]
            if not usable:
                meaning = "not read (the mode isn't usable)"
            elif on == "refused" and off != "refused":
                meaning = f"the engine re-checks once {mode} is gone"
            elif on == "cast":
                meaning = "the engine lets it through anyway: the guard's Lua check is the fix"
            elif on == "guard":
                meaning = "the harness refuses it before any order"
            else:
                meaning = "inconclusive (read the rows)"
            out.append(f"  {case:13} off={off or '-':9} {mode}={on:9} {meaning}")
    return out


def expected(rows: list[dict[str, Any]]) -> list[str]:
    """--plan live: PASS/FAIL per case against its expected verdict; a refusal says whether the engine's own reason
    was read. Pure (tests)."""
    out = []
    for r in rows:
        if "expect" not in r:
            continue
        v = r["verdict"]
        mark = "not run" if v == "not run" else "PASS" if v == r["expect"] else "FAIL"
        line = f"{mark:7} {r['case']:12} expected {r['expect']:7} got {v}"
        if v == "refused":
            first = str(r.get("text", "")).split("\n", 1)[0]
            line += " (engine reason read)" if "(the game's " in first else " (no engine reason read)"
        if r["case"] == "heavy_shove" and v == "cast":
            line += ": the engine lets it through; only the guard's CanShoveWeight stops it"
        out.append(line)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--plan", choices=("combat", "ooc", "live"), default="combat")
    ap.add_argument("--target", default="", help="combat: an undead / non-humanoid enemy (uuid or handle)")
    ap.add_argument("--heavy", default="", help="combat: an enemy too heavy to shove")
    ap.add_argument("--caster", default="Karlach")
    ap.add_argument("--hold-caster", default="Shadowheart")
    ap.add_argument("--attack", default="Main Hand Attack")
    ap.add_argument("--immobile", default="ENSNARED", help="a status that makes the caster Immobile")
    ap.add_argument("--rager", default="Karlach")
    ap.add_argument("--surger", default="Lae'zel")
    ap.add_argument("--area", default="Thunderwave")
    ap.add_argument("--area-caster", default="Gale")
    ap.add_argument("--two-step", default="Revivify")
    ap.add_argument("--two-step-who", default="Shadowheart")
    ap.add_argument("--two-step-target", default="", help="live: the first pick (a body for Revivify)")
    ap.add_argument(
        "--modes",
        default="",
        help="off, default, all, or flag names (one per mode); default: off + each flag, live: default",
    )
    ap.add_argument("--only", default="")
    ap.add_argument("--turn-wait", type=float, default=120.0)
    ap.add_argument("--out", type=Path, default=REPO / "runtime" / "probes" / "cast-checks.jsonl")
    a = ap.parse_args(argv)

    from bg3ai import turn
    from bg3ai.luadebug import LuaDebugClient
    from bg3ai.tools import combat, core, plumbing, turn_plan

    dbg = LuaDebugClient(connect_timeout=5.0)
    dbg.connect()

    def lua(code: str) -> str:
        return str(dbg.evaluate(code, context="server", timeout=30.0, statement=True))

    def in_combat() -> bool:
        return bool((core.game.try_rpc("state.combat") or {}).get("active"))

    def reach_turn(uuid: str) -> bool:
        end = time.monotonic() + a.turn_wait
        while time.monotonic() < end:
            cs = core.game.try_rpc("state.combat") or {}
            now = turn.party_acting(cs, turn_plan._party_set())
            if uuid in now:
                return True
            if now:
                combat.end_turn(who=now[0])
            time.sleep(2.0)
        return False

    if not a.modes:
        a.modes = "default" if a.plan == "live" else "off," + ",".join(FLAGS)
    only = {x for x in a.only.split(",") if x}
    cases = [c for c in plan(a.plan, a) if not only or c["case"] in only]
    rows: list[dict[str, Any]] = []
    a.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        lua(f"{G_LUA}.RULES_OFF = true return 'ok'")  # the engine's verdict is measured, not the guard's rules
        for c in cases:
            for mode in [m for m in a.modes.split(",") if m]:
                row: dict[str, Any] = {"case": c["case"], "mode": mode, "who": c["who"], "ability": c["ability"]}
                if "expect" in c:
                    row["expect"] = c["expect"]
                who = plumbing._who(c["who"])
                why = ""
                if c["need"] == "no_combat" and in_combat():
                    why = "in combat"
                elif c["need"] == "combat" and (not in_combat() or not reach_turn(who)):
                    why = "not its turn (or no fight)"
                elif c["need"] == "any" and in_combat() and not reach_turn(who):
                    why = "not its turn"
                st = c.get("status")
                if not why and st:
                    lua(f"Osi.ApplyStatus({json.dumps(who)}, {json.dumps(st)}, 12.0, 1, {json.dumps(who)}) return 'ok'")
                    time.sleep(0.5)
                    if lua(f"return tostring(Osi.HasActiveStatus({json.dumps(who)}, {json.dumps(st)}))") != "1":
                        why = f"status {st} didn't apply"
                if why:
                    row.update(verdict="not run", text=why)
                else:
                    lua(f"{A_LUA}.CAST_DROP_EXTRA = {switch(mode)} return 'ok'")
                    lua(REFILL % json.dumps(who))
                    tgt = c.get("target")
                    if tgt:
                        lua(f"Osi.SetHitpointsPercentage({json.dumps(plumbing._uuid(tgt))}, 100.0) return 'ok'")
                    if c.get("pos2"):
                        lua(f"{A_LUA}.TASK_POS2 = true return 'ok'")
                    try:
                        if c.get("landing_m"):
                            x, y, z = (
                                float(v)
                                for v in lua(
                                    f"local x, y, z = Osi.GetPosition({json.dumps(who)}) "
                                    "return string.format('%f,%f,%f', x, y, z)"
                                ).split(",")
                            )
                            text = combat.use_ability(
                                c["ability"], target=tgt, x=x + c["landing_m"], y=y, z=z, who=c["who"]
                            )
                        elif c.get("jump_m"):
                            x, y, z = (
                                float(v)
                                for v in lua(
                                    f"local x, y, z = Osi.GetPosition({json.dumps(who)}) "
                                    "return string.format('%f,%f,%f', x, y, z)"
                                ).split(",")
                            )
                            text = combat.use_ability(c["ability"], x=x + c["jump_m"], y=y, z=z, who=c["who"])
                        else:
                            text = combat.use_ability(c["ability"], target=tgt or "", who=c["who"])
                    except Exception as e:  # a probe reports, it doesn't stop the other cases
                        text = f"use_ability: FAILED — {e}"
                    finally:
                        if c.get("pos2"):
                            lua(f"{A_LUA}.TASK_POS2 = false return 'ok'")
                    row.update(text=str(text)[:500], verdict=verdict(str(text)), options=lua(OPTIONS % json.dumps(who)))
                if st and not why:
                    lua(f"Osi.RemoveStatus({json.dumps(who)}, {json.dumps(st)}) return 'ok'")
                rows.append(row)
                with a.out.open("a") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({k: row.get(k) for k in ("case", "mode", "verdict", "options")}))
    finally:
        lua(f"{A_LUA}.CAST_DROP_EXTRA = {A_LUA}.CAST_DROP_CHECKS {G_LUA}.RULES_OFF = false return 'ok'")
    for line in summary(rows) if a.plan != "live" else expected(rows):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
