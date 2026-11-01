"""The two-team probe's verdicts (tools/dev/probe_arena.py): pure functions over what it read, tested offline in
tools/dev/test_probe_arena.py."""

from __future__ import annotations

import itertools
import json
import math
from typing import Any

SIDES = ("A", "B")
NULL = "NULL_00000000-0000-0000-0000-000000000000"
AT_SPOT_M = 3.0  # a member counts as placed within this of its slot
MOVED_M = 0.5  # an idle turn's character moved: the game acted for it


def dist(a: Any, b: Any) -> float:
    if not (isinstance(a, list) and isinstance(b, list) and len(a) >= 3 and len(b) >= 3):
        return math.inf
    return math.dist(a[:3], b[:3])


def truthy(v: Any) -> bool:
    return v not in (None, 0, False, "0", "")


Verdict = tuple[str, str]


def side_of(teams: dict[str, Any], u: str | None) -> str | None:
    return next((s for s in SIDES if u in teams[s]), None)


def pick_faction_b(factions: list[dict[str, Any]], fa: str | None) -> str | None:
    """auto: a Hero faction other than team A's, preferring one nobody in the level uses (multiplayer's spare player
    factions), then the lowest relation to A."""
    hero = [f for f in factions if "hero" in str(f.get("name") or "").lower() and f.get("osi") != fa]
    if not hero:
        return None
    hero.sort(key=lambda f: (f.get("used") or 0, f.get("to_a") if isinstance(f.get("to_a"), int) else 100))
    return hero[0]["osi"]


def judge_setup(
    teams: dict[str, Any], facts: dict[str, dict[str, Any]], slots: dict[str, list[list[float]]]
) -> Verdict:
    bad = []
    for s in SIDES:
        for u, slot in zip(teams[s], slots[s], strict=True):
            f = facts.get(u) or {}
            who = f.get("name") or u[:8]
            if not truthy(f.get("in_party_db")):
                bad.append(f"{who} not in DB_PartyMembers")
            if dist(f.get("pos"), slot) > AT_SPOT_M:
                bad.append(f"{who} {dist(f.get('pos'), slot):.1f} m from its slot")
            if f.get("hp") is None or f.get("hp") != f.get("max_hp"):
                bad.append(f"{who} hp {f.get('hp')}/{f.get('max_hp')}")
    if bad:
        return "FAIL", "; ".join(bad)
    return "PASS", "8 party members, each at its slot with full HP"


def judge_conns(rows: list[dict[str, Any]], want: int) -> Verdict:
    """q1: every connection opened and answered while all were open."""
    ok = [r for r in rows if r.get("connected") and r.get("answer") is not None]
    if len(ok) == want:
        return "PASS", f"{want} connections open at once, each answered"
    bad = "; ".join(f"#{r['i']}: {r.get('error') or 'no answer'}" for r in rows if r not in ok)
    return "FAIL", f"{len(ok)}/{want} answered: {bad}"


def judge_party(
    teams: dict[str, Any], facts: dict[str, dict[str, Any]], mods: list[dict[str, Any]], pinned: list[str] | None
) -> Verdict:
    """q2: all 8 in DB_PartyMembers at once, with no mod loaded beyond the pinned set (when one is given)."""
    out = [
        f.get("name") or u[:8]
        for u in teams["A"] + teams["B"]
        if not truthy((f := facts.get(u) or {}).get("in_party_db"))
    ]
    extra = [
        m.get("name") or m.get("uuid") for m in mods if pinned and not {m.get("name"), m.get("uuid")} & set(pinned)
    ]
    why = []
    if out:
        why.append(f"not in DB_PartyMembers: {', '.join(out)}")
    if extra:
        why.append(f"mods beyond the pinned set: {', '.join(map(str, extra))}")
    if why:
        return "FAIL", "; ".join(why)
    names = ", ".join(str(m.get("name") or m.get("uuid")) for m in mods)
    return "PASS", "8 in DB_PartyMembers at once; " + (
        "only the pinned mods loaded" if pinned else f"{len(mods)} mods loaded (no pinned list given): {names}"
    )


def tie_info(order: dict[str, Any], teams: dict[str, Any]) -> dict[str, Any]:
    """From a raw turn order: groups holding both sides, cross-side members with equal initiative, and cross-side
    neighbours in the order (where a tie can be forced)."""
    groups = order.get("groups") or []
    mixed = [
        [m.get("name") or m.get("uuid") for m in g]
        for g in groups
        if len({side_of(teams, m.get("uuid")) for m in g} - {None}) > 1
    ]
    flat = [m for g in groups for m in g]
    equal = [
        [a.get("uuid"), b.get("uuid")]
        for a, b in itertools.combinations(flat, 2)
        if a.get("initiative") is not None
        and a.get("initiative") == b.get("initiative")
        and {side_of(teams, a.get("uuid")), side_of(teams, b.get("uuid"))} == {"A", "B"}
    ]
    adjacent = [
        [a.get("uuid"), b.get("uuid"), a.get("initiative")]
        for a, b in itertools.pairwise(flat)
        if {side_of(teams, a.get("uuid")), side_of(teams, b.get("uuid"))} == {"A", "B"}
    ]
    return {"mixed_groups": mixed, "equal": equal, "adjacent": adjacent}


def judge_tie(r: dict[str, Any]) -> Verdict:
    """q5: with a cross-team tie in the order, do both sides hold one turn (a mixed group, or both sides' members
    open at once)?"""
    shared = r.get("shared_turns") or []
    mixed = (r.get("after") or {}).get("mixed_groups") or (r.get("before") or {}).get("mixed_groups")
    if shared or mixed:
        return "FAIL", "the teams shared a turn (" + "; ".join(
            [f"group {g}" for g in mixed or []] + [f"open together: {t}" for t in shared]
        ) + "): needs serialising"
    if not ((r.get("after") or {}).get("equal") or (r.get("before") or {}).get("equal")):
        return "OPEN", "no cross-team initiative tie seen or forced: " + str(r.get("force") or "not forced")
    return "PASS", "a cross-team initiative tie, and each side took its own turn"


def judge_hostile(teams: dict[str, Any], facts: dict[str, dict[str, Any]], enemy: dict[str, Any]) -> Verdict:
    combats = {f.get("combat") for f in facts.values()}
    cross = [v for k, v in enemy.items() if k.startswith("AB:")]
    within = [k for k, v in enemy.items() if not k.startswith("AB:") and truthy(v)]
    why = []
    if not cross or not all(truthy(v) for v in cross):
        why.append(f"IsEnemy across the teams: {sum(truthy(v) for v in cross)}/{len(cross)}")
    if within:
        why.append(f"enemies within a team: {', '.join(within)}")
    if len(combats) != 1 or None in combats or NULL in combats:
        why.append(f"combat guids: {sorted(str(c) for c in combats)}")
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", "teams hostile to each other (not within), all 8 in one combat"


def judge_turns(teams: dict[str, Any], turns: list[dict[str, Any]], downed: dict[str, Any] | None) -> Verdict:
    """q3: both sides got turns, no turn group held both sides, each round started in initiative order, and the
    fight went on after a member was downed."""
    why = []
    seen = {side_of(teams, t.get("uuid")) for t in turns}
    if not {"A", "B"} <= seen:
        why.append(f"turns seen for sides {sorted(s for s in seen if s)} only")
    mixed = [t for t in turns if len({side_of(teams, u) for u in t.get("group") or []}) > 1]
    if mixed:
        why.append(f"{len(mixed)} turn group(s) held both sides: " + ", ".join(t.get("name") or "?" for t in mixed))
    rounds: dict[Any, list[float]] = {}
    for t in turns:
        if isinstance(t.get("initiative"), (int, float)):
            rounds.setdefault(t.get("round"), []).append(t["initiative"])
    out_of_order = [r for r, ini in rounds.items() if any(b > a for a, b in itertools.pairwise(ini))]
    if out_of_order:
        why.append(f"rounds {out_of_order} not in initiative order")
    if downed is not None:
        if not downed.get("downed"):
            why.append(f"the downing didn't take ({downed.get('who')})")
        elif not downed.get("combat_after") or not downed.get("turns_after"):
            why.append(
                f"after {downed.get('who')} was downed: combat {downed.get('combat_after')}, "
                f"{downed.get('turns_after')} turns"
            )
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", f"{len(turns)} turns, both sides, separate groups, initiative order" + (
        ", the fight went on after a downing" if downed else ""
    )


def judge_ai(turns: list[dict[str, Any]]) -> Verdict:
    """q3: on no idle turn of a standing member did the game act (move, cast, end the turn) by itself."""
    acted = [
        t
        for t in turns
        if not t.get("downed_at_start")
        and (
            (t.get("idle") or {}).get("moved_m", 0) > MOVED_M
            or (t.get("idle") or {}).get("casts")
            or (t.get("idle") or {}).get("ended_by_itself")
        )
    ]
    if not turns:
        return "OPEN", "no turns watched"
    if acted:
        return "FAIL", "the game acted on idle turns of: " + ", ".join(
            f"{t.get('name')} ({json.dumps(t.get('idle'))})" for t in acted
        )
    return "PASS", f"{len(turns)} idle turns, the game acted for nobody"


def res_amount(res: Any, name: str) -> float | None:
    """A resource's level-0 amount (or the sum over levels) from G.resources' {name: {level: amount}}."""
    lv = (res or {}).get(name) if isinstance(res, dict) else None
    if isinstance(lv, dict) and lv:
        vals = [v for v in lv.values() if isinstance(v, (int, float))]
        return float(sum(vals)) if vals else None
    if isinstance(lv, list):
        vals = [v for v in lv if isinstance(v, (int, float))]
        return float(sum(vals)) if vals else None
    return None


def judge_act(runs: dict[str, dict[str, Any]]) -> Verdict:
    """q4: for each side, the mod's own move and attack jobs ran on its member and the game charged them (Movement
    down after the move, ActionPoint down after the attack), and its end_turn ended the turn."""
    why = []
    for s in SIDES:
        r = runs.get(s)
        if not r:
            why.append(f"side {s}: no turn reached")
            continue
        for step, res_name in (("move", "Movement"), ("cast", "ActionPoint")):
            st = r.get(step) or {}
            b, a = res_amount(st.get("res_before"), res_name), res_amount(st.get("res_after"), res_name)
            if st.get("status") != "done":
                why.append(f"side {s} {step}: {st.get('status')} {st.get('error') or ''}".strip())
            elif b is None or a is None or not a < b:
                why.append(f"side {s} {step}: {res_name} {b} -> {a} (not charged)")
        if (r.get("end") or {}).get("status") != "done":
            why.append(f"side {s} end_turn: {(r.get('end') or {}).get('status')}")
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", "both sides: move and attack through act.* charged as in play, end_turn ended the turn"


def judge_react(r: dict[str, Any]) -> Verdict:
    """q7: did a reaction come up for the enemy the mover left, and could the mod answer it (ui.react)?"""
    if r.get("error"):
        return "OPEN", r["error"]
    ev = {e.get("ev") for e in r.get("events") or []}
    popup = bool(r.get("popup_seen"))
    if popup and (r.get("answer") or {}).get("ok"):
        return "PASS", f"prompt for {r.get('reactor_name')} (side {r.get('reactor_side')}), answered by ui.react"
    if popup:
        return "FAIL", f"prompt came up, ui.react failed: {r.get('answer')}"
    if "ReactionInterruptUsed" in ev or "AttackedBy" in ev:
        return "OPEN", "the reaction fired without a prompt (the reaction setting answers it): set it to ask and rerun"
    return "FAIL", "no prompt and no reaction when the mover left melee"


def judge_end(r: dict[str, Any]) -> Verdict:
    """q6: with the loser team all downed, can a turn boundary read it (every member downed or dead)? What the game
    does then (death-save turns, combat end, Game Over) is recorded; a Game Over in a team wipe is that team's loss."""
    if not r.get("all_down_read"):
        return "FAIL", "the loser team's downed state didn't read as downed/dead for every member"
    seen = [f"death-save turns: {len(r.get('death_save_turns') or [])}"]
    seen.append(
        f"combat ended at the {r['ended_at']} stage ({r.get('end_event')})"
        if r.get("ended_at")
        else "the combat never ended by itself"
    )
    if r.get("game_over"):
        seen.append("Game Over screen")
    return "PASS", "the wipe reads at a turn boundary; " + "; ".join(seen)


def judge_gameover(r: dict[str, Any]) -> Verdict:
    if r.get("game_over_downed") or r.get("game_over"):
        return "FAIL", "the avatar " + (
            "downed" if r.get("game_over_downed") else "dead"
        ) + " alone (others standing) is a Game Over: the avatar can't play"
    if not r.get("dead"):
        return "OPEN", "the avatar didn't die"
    return "PASS", "the avatar downed, then dead, with others standing: no Game Over"


def judge_npc(r: dict[str, Any]) -> Verdict:
    """q8 (option N, documented): an NPC team member under the mod's ops and the game AI."""
    why = []
    if not (r.get("act") or {}).get("refused"):
        why.append("act.* accepted the NPC (unexpected)")
    if (r.get("idle") or {}).get("acted"):
        why.append("the game AI acted on its turn")
    return ("FAIL" if why else "OPEN"), ("; ".join(why) or "the NPC kept still and act.* refused it") + (
        " (N needs a second action path either way)"
    )
