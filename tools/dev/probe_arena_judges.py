"""The two-team probe's verdicts (tools/dev/probe_arena.py): pure functions over what it read, tested offline in
tools/dev/test_probe_arena.py."""

from __future__ import annotations

import itertools
import json
import math
import re
from typing import Any

SIDES = ("A", "B")
NULL = "NULL_00000000-0000-0000-0000-000000000000"
AT_SPOT_M = 3.0  # a member counts as placed within this of its slot
SETTLE_S = 2.0
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


SPARE_FACTION = re.compile(r"Hero Player(\d+)")  # multiplayer's per-player factions; the bare "Hero" is their parent


def pick_factions(factions: list[dict[str, Any]], held: set[str | None]) -> tuple[str, str] | None:
    """auto: one faction per team (A, B), the two lowest-numbered "Hero Player<N>" factions that nobody in the level
    uses and no team member holds. Never the bare "Hero": the companions' factions descend from it, so a relation set
    on it reaches a team's own members."""
    spare = sorted(
        (int(m.group(1)), f["osi"])
        for f in factions
        if (m := SPARE_FACTION.fullmatch(str(f.get("name") or "")))
        and not (f.get("used") or 0)
        and f.get("osi") not in held
    )
    if len(spare) < 2:
        return None
    return spare[0][1], spare[1][1]


def stat_changes(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per member: level and max HP before and after the setup ([before, after]), and whether either changed."""
    out = {}
    for u, b in before.items():
        a = after.get(u) or {}
        lv, hp = [b.get("level"), a.get("level")], [b.get("max_hp"), a.get("max_hp")]
        out[u] = {
            "name": b.get("name") or a.get("name"),
            "level": lv,
            "max_hp": hp,
            "changed": lv[0] != lv[1] or hp[0] != hp[1],
        }
    return out


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


def enemy_counts(teams: dict[str, Any], enemy: dict[str, Any]) -> list[str]:
    """What is wrong in one IsEnemy matrix: every ordered pair across the teams (AB:, BA:) an enemy, none within a
    team (AA:, BB:), and every pair read."""
    n = {s: len(teams[s]) for s in SIDES}
    want_cross, want_within = 2 * n["A"] * n["B"], sum(k * (k - 1) for k in n.values())
    cross = [v for k, v in enemy.items() if k[:3] in ("AB:", "BA:")]
    within = [k for k, v in enemy.items() if k[:3] in ("AA:", "BB:")]
    hits = [k for k in within if truthy(enemy[k])]
    why = []
    if len(cross) != want_cross or not all(truthy(v) for v in cross):
        why.append(f"IsEnemy across the teams: {sum(truthy(v) for v in cross)}/{want_cross}")
    if len(within) != want_within or hits:
        why.append(
            f"IsEnemy within a team: {len(hits)}/{len(within)} read of {want_within}"
            + (f" ({', '.join(hits)})" if hits else "")
        )
    return why


def judge_hostile(teams: dict[str, Any], facts: dict[str, dict[str, Any]], after: dict[str, Any]) -> Verdict:
    """q3: the teams enemies of each other and not within once the combat has started (the last of the probe's
    reads), all 8 in one combat. The read before the combat is informational: live 2026-11-02 load1 read B->A 0/16
    there, then 16/16 in combat."""
    combats = {f.get("combat") for f in facts.values()}
    why = [f"in combat: {w}" for w in enemy_counts(teams, after)]
    if len(combats) != 1 or None in combats or NULL in combats:
        why.append(f"combat guids: {sorted(str(c) for c in combats)}")
    if why:
        return "FAIL", "; ".join(why)
    return "PASS", "teams enemies of each other (not within) in combat, all 8 in one combat"


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


def judge_sides(r: dict[str, Any]) -> Verdict:
    """q9: the client's Lua state holds the teams after the server's BG3AI.Arena.begin and drops them on reset."""
    begin, after, reset_ = r.get("begin") or {}, r.get("client_after_begin") or {}, r.get("client_after_reset") or {}
    if not (isinstance(begin, dict) and begin.get("ok")):
        err = begin.get("error") if isinstance(begin, dict) else begin
        return "OPEN", f"BG3AI.Arena.begin refused: {err}"
    if not (isinstance(after, dict) and after.get("state") is True):
        return "FAIL", f"client BG3AI.Arena.state not set after begin ({after}): the client is side-blind"
    if not after.get("active"):
        return "FAIL", "client has the teams but Arena.active() is false (the switch not seen by the client)"
    if not (isinstance(reset_, dict) and reset_.get("state") is False):
        return "FAIL", f"client still holds the teams after reset ({reset_})"
    return "PASS", f"client got the teams (names {after.get('names')}) after begin and dropped them on reset"


def rolled_order(groups: dict[str, Any] | None, teams: dict[str, Any]) -> list[str]:
    """The 8 in the order the game held them for the next round before any split (NextRoundGroups, else
    CurrentGroups, group by group): the order as rolled."""
    groups = groups or {}
    return [
        m["uuid"]
        for g in groups.get("next") or groups.get("current") or []
        for m in g.get("members") or []
        if side_of(teams, m.get("uuid"))
    ]


def judge_groups(teams: dict[str, Any], r: dict[str, Any]) -> Verdict:
    """q10: in the round after the split, every open turn holds one side only, and the turns come in the order as
    rolled: each turn is the next block of the rolled order, and all 8 got one."""
    write = r.get("write")
    if isinstance(write, dict) and write.get("error"):
        return "FAIL", f"the split write failed: {write['error']}"
    rolled = rolled_order(r.get("before"), teams)
    if len(rolled) != len(teams["A"]) + len(teams["B"]):
        return (
            "OPEN",
            f"the turn order read holds {len(rolled)} of the teams' members: {(r.get('before') or {}).get('error')}",
        )
    want = r.get("judged_round")
    turns = [t for t in r.get("turns") or [] if want is not None and t.get("round") == want]
    if not turns:
        return "OPEN", f"no turn of round {want} watched"
    why = []
    mixed = [t for t in turns if len({side_of(teams, u) for u in t["open"]}) > 1]
    if mixed:
        why.append(
            f"round {want}: {len(mixed)} turn(s) open for both sides: "
            + "; ".join(", ".join(t.get("names") or t["open"]) for t in mixed)
        )
    i = 0
    for t in turns:
        block = rolled[i : i + len(t["open"])]
        if set(block) != set(t["open"]):
            why.append(f"round {want}: turn {', '.join(t.get('names') or t['open'])} out of the rolled order")
            break
        i += len(t["open"])
    else:
        if i != len(rolled):
            why.append(f"round {want}: {len(rolled) - i} of the 8 had no turn")
    if why:
        return "FAIL", "; ".join(why)
    seq = " ".join("".join(sorted({side_of(teams, u) or "?" for u in t["open"]})) for t in turns)
    return "PASS", f"round {want}: {len(turns)} turns, one side each, in the rolled order ({seq})"
