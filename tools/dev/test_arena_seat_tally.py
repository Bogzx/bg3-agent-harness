# pyright: basic
"""arena_seat_tally over synthetic run folders: replays count once, void checks, verdicts.

uv run -q pytest tools/dev/test_arena_seat_tally.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bg3bench import arena

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arena_seat_tally as seat

BASE = json.loads(seat.DEFAULT_ARENA.read_text())
TEAMS = arena.team_names(arena.with_defaults(BASE))
DS = "pi/deepseek-flash"


def ev(t, event, **kw):
    return {"v": 1, "kind": "event", "t": float(t), "ev": event, **kw}


def o1_phases(plan, turn_order="alternate", t=10.0):
    """O1-shaped phase records as the mod writes them (arena/turn-order a55109d9), with arena_round.

    plan: per round, a list of slots: a side ("A"), or (side, who, kind, group) with kind "turn" | "passed" (default
    turn, group g1). Each round's opening is its first slot's side unless the round is a dict {opening, slots}. rule
    as the mod's pick gives it: a group's first phase "opening" (its side the opening's) or "only_side_left"; then
    "alternate" (the other side) or "only_side_left" (the same side); the same character again keeps its rule."""
    recs = []
    for rnd, r in enumerate(plan, 1):
        slots = r["slots"] if isinstance(r, dict) else r
        slots = [x if isinstance(x, tuple) else (x,) for x in slots]
        opening = r["opening"] if isinstance(r, dict) else slots[0][0]
        seen, prev, n = {"A": 0, "B": 0}, {}, 0
        recs.append(ev(t, "arena_round", round=rnd, first=opening, how="turn"))
        for x in slots:
            side = x[0]
            who = x[1] if len(x) > 1 and x[1] else TEAMS[side][seen[side] % len(TEAMS[side])]
            kind = x[2] if len(x) > 2 else "turn"
            group = x[3] if len(x) > 3 else "g1"
            seen[side] += 1
            last = prev.get(group)
            if last is None:
                rule = "opening" if side == opening else "only_side_left"
            elif last[1] == who:
                rule = last[2]
            else:
                rule = "alternate" if side != last[0] else "only_side_left"
            prev[group] = (side, who, rule)
            o1 = {"order": turn_order, "opening": opening, "rule": rule}
            if kind == "turn":
                n += 1
                recs.append(ev(t, "arena_turn", side=side, character=who, round=rnd, members=[who], names=[who],
                               phase=n, group=group, shared=True, **o1))  # fmt: skip
            else:
                recs.append(ev(t, "arena_turn_passed", side=side, character=who, name=who, why="downed", round=rnd,
                               **o1))  # fmt: skip
            t += 1
    return recs


def write_run(root: Path, name: str, seed: int, plan, winner: str, rounds=2, sides=(DS, DS), turn_order="alternate",
              tamper=None):  # fmt: skip
    """A run folder; plan: o1_phases's, or one round's sides played `rounds` times. tamper(records) edits them."""
    if plan and isinstance(plan[0], str):
        plan = [plan] * rounds
    recs = [{"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}]
    recs.append(ev(1, "arena_start", arena=BASE["name"], seed=seed, turn_order=turn_order, teams=TEAMS,
                   sides=dict(zip("AB", sides, strict=True))))  # fmt: skip
    recs += o1_phases(plan, turn_order)
    if tamper:
        tamper(recs)
    t = recs[-1]["t"]
    end = {"winner": winner, "rounds": len(plan), "hp_left_A": 0.5 if winner == "A" else 0.0,
           "hp_left_B": 0.5 if winner == "B" else 0.0, "reason": "wipe"}  # fmt: skip
    recs.append(ev(t + 1, "arena_end", **end))
    recs.append({"v": 1, "kind": "mark", "t": t + 2, "mark": "run_end", "detail": None})
    d = root / name
    d.mkdir()
    (d / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return d


def problems(tmp_path, name, plan, turn_order="alternate", tamper=None):
    d = write_run(tmp_path, name, 1, plan, "A", turn_order=turn_order, tamper=tamper)
    [m] = seat.graded([d], BASE)
    assert m["valid"], m["invalid_reason"]
    return m["o1_problems"]


ALT = ["A", "B", "A", "B"]


def test_a_self_pairs_replay_counts_once_and_the_opener_and_letters_are_tallied(tmp_path):
    write_run(tmp_path, "s1m1", 1, ALT, "A")
    write_run(tmp_path, "s1m2", 1, ALT, "B")  # replay of seed 1: left out
    write_run(tmp_path, "s2m1", 2, ["B", "A", "B", "A"], "A")
    (tmp_path / "no-arena").mkdir()
    (tmp_path / "no-arena" / "facts.jsonl").write_text(
        json.dumps({"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}) + "\n"
    )
    ms = seat.graded([tmp_path], BASE)
    assert [m["dir"] for m in ms] == ["s1m1", "s1m2", "s2m1"] and all(m["valid"] for m in ms)
    t = seat.tally(ms, snake_at=28, ambiguous_from=25, min_n=2)
    assert (t["n_valid"], t["n_counted"]) == (3, 2)
    assert t["opener_points"] == 1.0 and t["side_points"] == {"A": 2.0, "B": 0.0}
    assert t["void_dirs"] == [] and t["verdict"] == "keep_o1"
    assert t["first_actor"]["n_matches"] == 2


def test_o1_alternate_snake_a_dead_opener_passed_slots_and_two_groups_are_not_void(tmp_path):
    assert problems(tmp_path, "alt", [ALT, ALT, ALT]) == []
    # snake: the opening flips every round (the old first_constant check voided every snake run)
    snake = [ALT, ["B", "A", "B", "A"], ALT]
    assert problems(tmp_path, "snake", snake, turn_order="snake") == []
    # alternate, A's first member (Ash) dead after round 1: round 2 opens with B, its first living member
    ash, cedar = TEAMS["A"]
    dead = [ALT, ["B", ("A", cedar), "B"]]
    assert problems(tmp_path, "dead", dead) == []
    # a downed member's slot is passed and still counts: B | A passed | B | A reads B1|B1|A1 from arena_turn alone
    passed = [ALT, ["B", ("A", ash, "passed"), "B", ("A", cedar)]]
    assert problems(tmp_path, "passed", passed) == []
    # a round opened by a passed slot (the opening side's first member downed)
    first_passed = [ALT, [("A", ash, "passed"), "B", ("A", cedar), "B"]]
    assert problems(tmp_path, "first-passed", first_passed) == []
    # two turn groups in a round, each alternating on its own; a side with nobody left plays on (only_side_left)
    two = [[("A", ash, "turn", "g1"), ("B", None, "turn", "g1"), ("B", None, "turn", "g2"), ("A", cedar, "turn", "g2")]]
    assert problems(tmp_path, "two", two) == []
    left = [["A", "B", ("B", None)]]
    assert problems(tmp_path, "left", left) == []
    # a passed member readmitted (stood up) acts in its own phase: the same character again continues it
    again = [[("A", ash, "passed"), ("A", ash), "B", ("A", cedar), "B"]]
    assert problems(tmp_path, "again", again) == []


def test_o1_void_checks_name_what_breaks_the_order(tmp_path):
    def edit(fn):
        return lambda recs: fn([r for r in recs if r.get("ev") == "arena_turn"])

    def side(i, s):
        who = TEAMS[s][1]
        return edit(lambda ts: ts[i].update(side=s, character=who, members=[who], names=[who]))

    def two_members(ts):
        ts[0]["members"] = ts[0]["names"] = [ts[0]["character"], TEAMS["A"][1]]

    def strip(recs):
        for r in recs:
            for k in ("order", "opening", "rule"):
                r.pop(k, None)

    def first_b(recs):
        next(r for r in recs if r.get("ev") == "arena_round" and r["round"] == 2)["first"] = "A"

    # two of B's back to back under rule alternate (the old pair)
    assert problems(tmp_path, "pair", [["A", "B", "A"]], tamper=side(2, "B")) == [
        "round 1 group g1: B alternate after B"
    ]
    assert problems(tmp_path, "two", [ALT], tamper=edit(two_members)) == ["round 1: a phase of 2 characters"]
    assert problems(tmp_path, "old", [ALT], tamper=strip) == ["round 1: a phase without O1's order/opening/rule"]
    assert problems(tmp_path, "stuck", [ALT, ALT], turn_order="snake") == ["round 2: snake opens with A again"]
    assert problems(tmp_path, "start", [ALT], tamper=lambda recs: recs[1].update(turn_order="snake")) == [
        "turn order ['alternate'] (arena_start snake)"
    ]
    assert problems(tmp_path, "round", [ALT, ["B", "A", "B", "A"]], tamper=first_b) == [
        "round 2: arena_round first A, opening B"
    ]
    # an old-order match on record (no O1 fields) is void, as the baseline expects
    ms = seat.graded([write_run(tmp_path, "void", 3, [ALT], "B", tamper=strip)], BASE)
    assert seat.tally(ms, 28, 25, 1)["verdict"] == "void"


def test_verdicts_follow_the_plans_thresholds(tmp_path):
    for s in range(1, 5):  # four seeds, A opens each and wins three
        write_run(tmp_path, f"s{s}", s, ALT, "A" if s < 4 else "B")
    ms = seat.graded([tmp_path], BASE)
    assert seat.tally(ms, 3, 2, 5)["verdict"] == "too_few"
    assert seat.tally(ms, 3, 2, 4)["verdict"] == "snake"  # opener = A here, so also letter: snake is checked first
    assert seat.tally(ms, 4, 3, 4)["verdict"] == "ambiguous"
    assert seat.tally(ms, 4, 3.5, 4)["verdict"] == "keep_o1"
    # opener split across letters: B opens seeds 5-8 and loses, so the opener wins 3/8 but A wins 7/8
    for s in range(5, 9):
        write_run(tmp_path, f"s{s}", s, ["B", "A", "B", "A"], "A")
    ms = seat.graded([tmp_path], BASE)
    t = seat.tally(ms, 7, 6, 8)
    assert (t["opener_points"], t["side_points"]["A"], t["verdict"]) == (3.0, 7.0, "letter")


def test_main_prints_one_row_per_folder_and_the_tally(tmp_path, capsys):
    write_run(tmp_path, "s1", 1, ALT, "A")
    out_json = tmp_path / "t.json"
    assert seat.main([str(tmp_path), "--min-n", "1", "--json", str(out_json)]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["dir"] == "s1" and lines[0]["round_first"] == "AA" and lines[0]["counted"]
    assert lines[0]["opener"] == "A" and lines[0]["o1_problems"] == []
    assert lines[-1]["verdict"] == "keep_o1"
    assert json.loads(out_json.read_text())["tally"]["n_counted"] == 1
    assert seat.main([str(tmp_path / "missing")]) == 2
