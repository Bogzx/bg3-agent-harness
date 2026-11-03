# pyright: basic
"""Arena results: pairings, the pairwise table, side A's rate, the gated Bradley-Terry fit and the Elo display."""

import math
import random

import pytest

from bg3bench import arena, stats


def m(pairing, a, b, result, valid=True, arena_name="open_field"):
    return {"pairing": pairing, "arena": arena_name, "valid": valid, "sides": {"A": a, "B": b}, "result": result}


def pairing(pid, x, y, r1, r2):
    """Two matches with the sides swapped: x on A then y on A; r1/r2 the side that won each (or draw)."""
    return [m(pid, x, y, r1), m(pid, y, x, r2)]


def league(seed, n=12, side_a=0.0):
    """Synthetic pairings of three players of increasing strength, drawn from a fixed seed; side_a: side A's edge."""
    rng, strength, out = random.Random(seed), {"p1": 1.0, "p2": 2.0, "p3": 4.0}, []
    names = sorted(strength)
    for k in range(n):
        x, y = rng.sample(names, 2)
        for on_a, on_b in ((x, y), (y, x)):
            p = strength[on_a] / (strength[on_a] + strength[on_b]) + side_a
            out.append(m(f"q{k:03d}", on_a, on_b, "A" if rng.random() < p else "B"))
    return out


def test_pairings_count_invalid_self_and_incomplete():
    ms = [
        *pairing("p1", "x", "y", "A", "B"),
        m("p2", "x", "y", "A"),
        m("p2", "y", "x", None, valid=False),
        *pairing("p3", "x", "x", "A", "A"),
    ]
    complete, counts = arena.pairings(ms)
    assert sorted(complete) == ["p1"]
    assert counts == {"matches": 6, "invalid": 1, "self": 2, "incomplete": 1}


@pytest.mark.parametrize(
    "bad",
    [
        [m("p", "x", "y", "A"), m("p", "x", "y", "B")],  # not swapped
        [m("p", "x", "y", "A"), m("p", "y", "z", "B")],  # another player
        [m("p", "x", "y", "A"), m("p", "y", "x", "B", arena_name="other")],
        [*pairing("p", "x", "y", "A", "B"), m("p", "x", "y", "A")],  # three valid matches
    ],
)
def test_a_pairing_whose_valid_matches_are_not_one_swap_raises(bad):
    with pytest.raises(arena.ArenaError, match="pairing p"):
        arena.pairings(bad)


MIXED = [
    *pairing("a", "x", "y", "A", "B"),  # x wins both: 2-0
    *pairing("b", "x", "y", "draw", "A"),  # a draw, then y wins on side A: x 0.5-1.5, lost
    *pairing("c", "x", "y", "A", "A"),  # each wins on side A: split
    *pairing("d", "x", "z", "A", "B"),  # x wins both
    *pairing("e", "y", "z", "A", "draw"),  # y 1.5-0.5: won
    m("f", "y", "z", "B"),  # incomplete: out of the pairwise table and the ratings
    *pairing("g", "y", "y", "A", "B"),  # a self-pairing: only side A's rate counts it
]


def test_the_pairwise_table():
    r = arena.results(MIXED, seed=3, b=100)
    assert r["counts"] == {"matches": 13, "invalid": 0, "self": 2, "incomplete": 1, "pairings": 5}
    xy, xz, yz = r["pairs"]
    assert (xy["a"], xy["b"]) == ("x", "y")
    assert (xy["won"], xy["split"], xy["lost"]) == (1, 1, 1)
    assert (xy["w"], xy["d"], xy["l"], xy["n_matches"], xy["n_pairings"]) == (3, 1, 2, 6, 3)
    assert xy["win_rate"] == pytest.approx(3.5 / 6)
    assert xy["wilson"] == pytest.approx(list(stats.wilson(3.5, 6)))  # pyright: ignore[reportArgumentType]
    assert (xz["won"], xz["n_pairings"], xz["win_rate"]) == (1, 1, 1.0)
    assert (yz["a"], yz["b"], yz["won"], yz["d"]) == ("y", "z", 1, 1)
    assert not any(row["rated"] for row in r["pairs"])  # under 15 pairings: pilot rows


def test_below_the_pairing_floor_every_model_is_a_pilot_row():
    r = arena.results(MIXED, seed=3, b=100)
    by = {row["player"]: row for row in r["models"]}
    assert {p: (row["rated"], row["bt"], row["elo"], row["elo_ci"]) for p, row in by.items()} == {
        p: (False, None, None, None) for p in ("x", "y", "z")
    }
    assert (by["x"]["n_matches"], by["x"]["n_pairings"], by["x"]["points"]) == (8, 4, 5.5)
    assert [row["player"] for row in r["models"]] == ["x", "y", "z"]  # by points per match


def test_side_a_rate_counts_every_valid_match_but_a_self_pairs_replay_and_resamples_pairings():
    # REVIEW-7 f2: a self-pairing's second match (same seed, mirror twins, same model) replays the first: one sample
    r = arena.results(MIXED, seed=3, b=200)
    a_points = sum(arena._points(x, "A") for x in MIXED[:-1])
    assert (r["side_a"]["n_matches"], r["side_a"]["n_pairings"]) == (12, 7)
    assert r["side_a"]["rate"] == pytest.approx(a_points / 12)
    lo, hi = r["side_a"]["ci"]
    assert lo <= r["side_a"]["rate"] <= hi
    skewed = arena.results(league(seed=4, n=60, side_a=0.3), seed=1, b=200)["side_a"]
    assert skewed["ci"][0] > 0.5  # an arena that favours side A shows it


def test_rated_pairs_get_a_bradley_terry_fit_and_elo():
    r = arena.results(MIXED, seed=3, b=200, min_pairings=3)  # only x-y has 3 pairings
    assert [row["rated"] for row in r["pairs"]] == [True, False, False]
    by = {row["player"]: row for row in r["models"]}
    assert (by["x"]["rated"], by["y"]["rated"], by["z"]["rated"]) == (True, True, False)
    assert [row["player"] for row in r["models"]] == ["x", "y", "z"]  # rated rows first, by elo
    x, y = by["x"], by["y"]
    assert (x["elo"] + y["elo"]) / 2 == pytest.approx(1500.0)
    assert x["elo"] - y["elo"] == pytest.approx(400 * (x["bt"] - y["bt"]) / math.log(10))
    complete, _ = arena.pairings(MIXED)
    only_xy = arena.bradley_terry(arena._games(complete[p] for p in ("a", "b", "c")))
    assert x["bt"] == pytest.approx(only_xy["x"])  # the fit is over the rated pair's pairings only
    for row in (x, y):
        lo, hi = row["elo_ci"]
        assert lo <= row["elo"] <= hi


def test_side_swapped_results_cancel():
    level = arena.results(pairing("p", "x", "y", "A", "A"), seed=1, b=50, min_pairings=1)
    assert [row["elo"] for row in level["models"]] == pytest.approx([1500.0, 1500.0])


def test_bradley_terry_meets_its_score_equations():
    games = [("x", "y", 1.0), ("x", "y", 1.0), ("y", "x", 1.0), ("x", "z", 0.5), ("y", "z", 0.0), ("z", "y", 1.0)]
    theta = arena.bradley_terry(games)
    p = {k: math.exp(v) for k, v in theta.items()}
    for k, pk in p.items():  # at the maximum each player's points equal its expected points (the virtual draw included)
        pts = 0.5 + sum(s if i == k else 1 - s for i, j, s in games if k in (i, j))
        exp = pk / (pk + 1) + sum(pk / (p[i] + p[j]) for i, j, _ in games if k in (i, j))
        assert pts == pytest.approx(exp, rel=1e-8)


def test_an_undefeated_player_has_a_finite_rating():
    theta = arena.bradley_terry([("x", "y", 1.0)] * 6)
    assert all(math.isfinite(v) for v in theta.values())
    assert theta["x"] > 0 > theta["y"]


def test_results_are_reproducible_from_the_seed():
    ms = league(seed=11, n=40)
    one = arena.results(ms, seed=5, b=300, min_pairings=5)
    assert arena.results(ms, seed=5, b=300, min_pairings=5) == one
    assert arena.results(ms, seed=6, b=300, min_pairings=5)["models"] != one["models"]  # another bootstrap


def test_a_stronger_player_rates_higher_over_many_pairings():
    r = arena.results(league(seed=2, n=90), seed=1, b=200)
    assert all(row["rated"] for row in r["pairs"])  # 90 pairings over 3 pairs: each pair past 15
    assert [row["player"] for row in r["models"]] == ["p3", "p2", "p1"]
    assert r["models"][0]["elo_ci"][0] > r["models"][-1]["elo_ci"][1]  # p3 and p1 separate


def test_no_valid_match_rates_nobody():
    r = arena.results([m("p", "x", "y", None, valid=False)], seed=1, b=10)
    assert (r["models"], r["pairs"]) == ([], [])
    assert r["side_a"] == {"n_matches": 0, "n_pairings": 0, "rate": None, "ci": None}


def test_side_a_rate_by_round_1_pattern_with_n():
    # fairness audit 2026-11-03: if one round-1 pattern decides most matches, initiative outweighs the models
    ms = [
        dict(m("p1", "x", "y", "B"), r1_pattern="A1|B2|A1"),
        dict(m("p1", "y", "x", "B"), r1_pattern="A1|B2|A1"),
        dict(m("p2", "x", "y", "A"), r1_pattern="B1|A2|B1"),
        dict(m("p2", "y", "x", "draw"), r1_pattern="B1|A2|B1"),
        dict(m("p3", "x", "y", "A", valid=False), r1_pattern="A1|B2|A1"),  # invalid: not counted
        m("p3", "y", "x", "A"),  # no pattern (graded before it existed)
    ]
    assert arena.results(ms, seed=1, b=10)["side_a_by_pattern"] == {
        "A1|B2|A1": {"n_matches": 2, "rate": 0.0},
        "B1|A2|B1": {"n_matches": 2, "rate": 0.75},
        "none": {"n_matches": 1, "rate": 1.0},
    }


def test_side_a_by_pattern_and_favoured_count_a_self_pairs_replay_once():
    ms = [
        dict(m("s", "x", "x", "B"), r1_pattern="A1|B2|A1"),
        dict(m("s", "x", "x", "B"), r1_pattern="A1|B2|A1"),  # the replay
        dict(m("s", "x", "x", "A", valid=False), r1_pattern="A1|B2|A1"),
    ]
    r = arena.results(ms, seed=1, b=10)
    assert r["side_a_by_pattern"] == {"A1|B2|A1": {"n_matches": 1, "rate": 0.0}}
    assert (r["favoured"]["n_matches"], r["favoured"]["rate"]) == (1, 1.0)


def test_the_favoured_sides_win_rate_overall_and_by_why():
    # REVIEW-7 (a): the six DeepSeek self-matches of 2026-11-03, as pairings (each self-pair's replay left out)
    ms = [
        dict(m("s1", "d", "d", "B"), r1_pattern="A1|B2|A1"),
        dict(m("s1", "d", "d", "B"), r1_pattern="A1|B2|A1"),
        dict(m("s3", "d", "d", "A"), r1_pattern="B1|A2|B1"),
        dict(m("s3", "d", "d", "A"), r1_pattern="B1|A2|B1"),
        dict(m("s5", "d", "d", "B"), r1_pattern="B1|A1|B1|A1"),
        dict(m("s5", "d", "d", "B"), r1_pattern="B1|A1|B1|A1"),
        dict(m("p", "x", "y", "draw"), r1_pattern="A1|B1|A1|B1"),  # the first actor gets half a point
        m("q", "x", "y", "A"),  # no pattern: not counted
    ]
    fav = arena.results(ms, seed=1, b=10)["favoured"]
    assert (fav["n_matches"], fav["rate"]) == (4, pytest.approx(3.5 / 4))
    assert fav["wilson"] == list(stats.wilson(3.5, 4))  # pyright: ignore[reportArgumentType]
    assert fav["by"]["pair"]["n_matches"] == 2 and fav["by"]["pair"]["rate"] == 1.0
    assert fav["by"]["first"]["n_matches"] == 2 and fav["by"]["first"]["rate"] == 0.75


def test_bradley_terry_with_a_seat_meets_its_score_equations():
    # REVIEW-7 f1: the favoured player's strength counts times the seat factor g in its game
    games = [("x", "y", 1.0, 1), ("y", "x", 1.0, 1), ("x", "y", 0.0, -1), ("x", "z", 0.5, 0), ("z", "y", 1.0, 1)]
    theta, lg = arena.bradley_terry_seat(games)
    p, g = {k: math.exp(v) for k, v in theta.items()}, math.exp(lg)

    def win(i, j, f):  # i's expected points
        ci, cj = (g if f > 0 else 1.0), (g if f < 0 else 1.0)
        return ci * p[i] / (ci * p[i] + cj * p[j])

    for k, pk in p.items():
        pts = 0.5 + sum(s if i == k else 1 - s for i, j, s, _ in games if k in (i, j))
        exp = pk / (pk + 1) + sum(win(i, j, f) if i == k else 1 - win(i, j, f) for i, j, _, f in games if k in (i, j))
        assert pts == pytest.approx(exp, rel=1e-8)
    seat_pts = 0.5 + sum(s if f > 0 else 1 - s for _, _, s, f in games if f)
    seat_exp = g / (g + 1) + sum(win(i, j, f) if f > 0 else 1 - win(i, j, f) for i, j, _, f in games if f)
    assert seat_pts == pytest.approx(seat_exp, rel=1e-8)


def test_without_a_favoured_side_the_seat_fit_is_the_plain_fit():
    games = [("x", "y", 1.0), ("x", "y", 1.0), ("y", "x", 1.0), ("x", "z", 0.5)]
    theta, lg = arena.bradley_terry_seat(games)
    assert lg == pytest.approx(0.0)
    assert theta == pytest.approx(arena.bradley_terry(games))


def league_seat(seed, n, seat):
    """Pairings of two equal players where the favoured side (from the pattern) wins with probability seat."""
    rng, out = random.Random(seed), []
    for k in range(n):
        pattern = rng.choice(["A1|B2|A1", "B1|A2|B1", "A1|B1|A1|B1"])
        fav = (arena.favoured(pattern) or {})["side"]
        for on_a, on_b in (("x", "y"), ("y", "x")):
            won = fav if rng.random() < seat else ("B" if fav == "A" else "A")
            out.append(dict(m(f"q{k:03d}", on_a, on_b, won), r1_pattern=pattern))
    return out


def test_results_report_the_seat_with_its_bootstrap_interval_and_keep_equal_models_level():
    r = arena.results(league_seat(seed=7, n=40, seat=0.8), seed=1, b=200)
    seat = r["seat"]
    assert seat["n_games"] == 80
    assert seat["ci"][0] > 0.6 and seat["ci"][0] <= seat["p_favoured"] <= seat["ci"][1]
    assert seat["p_favoured"] == pytest.approx(0.8, abs=0.08)
    always = arena.results(league_seat(seed=7, n=20, seat=1.0), seed=1, b=50)  # every pairing splits 1-1 by seat
    assert [row["elo"] for row in always["models"]] == pytest.approx([1500.0, 1500.0])  # the seat is not skill
    assert always["seat"]["p_favoured"] > 0.95
    assert (
        arena.results(league(seed=2, n=20), seed=1, b=10, min_pairings=1)["seat"]["n_games"] == 0
    )  # no pattern, no seat games
    assert arena.results([m("p", "x", "y", "A")], seed=1, b=10)["seat"] is None  # nothing rated
