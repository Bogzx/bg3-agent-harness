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


def ev(t, name, **kw):
    return {"v": 1, "kind": "event", "t": float(t), "ev": name, **kw}


def write_run(root: Path, name: str, seed: int, order: list[str], winner: str, rounds=2, sides=(DS, DS), marks=True):
    """A run folder: each round plays `order` (sides, one member a phase); arena_round records when marks."""
    recs = [{"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}]
    recs.append(
        ev(1, "arena_start", arena=BASE["name"], seed=seed, teams=TEAMS, sides=dict(zip("AB", sides, strict=True)))
    )
    t = 10.0
    for rnd in range(1, rounds + 1):
        if marks:
            recs.append(ev(t, "arena_round", round=rnd, first=order[0], how="turn"))
        for i, side in enumerate(order):
            who = TEAMS[side][i // 2 % len(TEAMS[side])]
            recs.append(ev(t, "arena_turn", side=side, character=who, round=rnd, members=[who], names=[who],
                           phase=i + 1, group="g1", shared=True))  # fmt: skip
            t += 1
    end = {"winner": winner, "rounds": rounds, "hp_left_A": 0.5 if winner == "A" else 0.0,
           "hp_left_B": 0.5 if winner == "B" else 0.0, "reason": "wipe"}  # fmt: skip
    recs.append(ev(t + 1, "arena_end", **end))
    recs.append({"v": 1, "kind": "mark", "t": t + 2, "mark": "run_end", "detail": None})
    d = root / name
    d.mkdir()
    (d / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return d


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


def test_void_checks_catch_a_pair_phase_and_a_flipping_first_side(tmp_path):
    assert seat.alternating("A1|B1|A1|B1") and seat.alternating("B1|A1")
    assert not seat.alternating("A1|B2|A1") and not seat.alternating("A1|A1|B1") and not seat.alternating(None)
    assert seat.first_constant([{"first": "A"}, {"first": "A"}]) and not seat.first_constant(
        [{"first": "A"}, {"first": "B"}]
    )
    write_run(tmp_path, "old", 3, ["A", "B", "B", "A"], "B")  # old order: B's two members back to back
    ms = seat.graded([tmp_path], BASE)
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
    assert lines[-1]["verdict"] == "keep_o1"
    assert json.loads(out_json.read_text())["tally"]["n_counted"] == 1
    assert seat.main([str(tmp_path / "missing")]) == 2
