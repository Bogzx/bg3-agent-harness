# pyright: basic
"""Arena results view: pentanomial counts, the sign test, provisional ratings, rank ranges, the prior sensitivity and
the seat term's self-matches (docs/ARENA-RESULTS.md)."""

import math

import pytest
from test_arena_ratings import MIXED, league, m, pairing

from bg3bench import arena


def test_each_pair_counts_its_pairings_by_points_and_its_cap_draws():
    ms = [*MIXED, dict(m("h", "x", "y", "draw"), reason="round_cap"), dict(m("h", "y", "x", "draw"), reason="wall_cap")]
    xy, _, yz = arena.results(ms, seed=3, b=20)["pairs"]
    # x-y: a 2-0, a 0.5-1.5, a split by side A and a split of two draws; the 1-1 bucket holds both splits
    assert xy["pentanomial"] == {"2": 1, "1.5": 0, "1": 2, "0.5": 1, "0": 0}
    assert (xy["won"], xy["split"], xy["lost"]) == (1, 2, 1)
    assert xy["draws"] == {"round_cap": 1, "wall_cap": 1}  # pairing b's draw has no reason: in d only
    assert xy["d"] == 3
    assert yz["pentanomial"] == {"2": 0, "1.5": 1, "1": 0, "0.5": 0, "0": 0}


def test_the_sign_test_is_exact_and_two_sided():
    assert arena.sign_test(9, 1) == pytest.approx(2 * (1 + 10) / 2**10)  # 0.02148
    assert arena.sign_test(1, 9) == arena.sign_test(9, 1)
    assert arena.sign_test(6, 0) == pytest.approx(2 / 64)
    assert arena.sign_test(3, 3) == 1.0
    assert arena.sign_test(0, 0) == 1.0
    sweep = [x for k in range(6) for x in pairing(f"s{k}", "x", "y", "A", "B")]  # x wins all six 2-0
    splits = [x for k in range(4) for x in pairing(f"t{k}", "x", "y", "A", "A")]  # splits don't count
    (row,) = arena.results(sweep + splits, seed=1, b=10)["pairs"]
    assert row["sign_p"] == pytest.approx(2 / 64)


def two_players(n):
    """n pairings of x and y, each a split by side A except every fourth, which x wins 2-0."""
    return [x for k in range(n) for x in pairing(f"q{k:03d}", "x", "y", "A", "B" if k % 4 == 0 else "A")]


@pytest.mark.parametrize(("n", "provisional"), [(49, True), (50, False)])
def test_a_rating_is_provisional_below_50_pairings(n, provisional):
    r = arena.results(two_players(n), seed=1, b=20)
    assert [(row["rated"], row["provisional"]) for row in r["pairs"]] == [(True, provisional)]
    assert [(row["rated"], row["provisional"]) for row in r["models"]] == [(True, provisional)] * 2


def test_a_pilot_row_is_not_provisional_but_unrated():
    r = arena.results(MIXED, seed=3, b=20)
    assert not any(row["provisional"] for row in r["pairs"] + r["models"])
    assert all((row["rank"], row["rank_ci"], row["prior_shift"]) == (None, None, None) for row in r["models"])
    assert r["prior"] is None


def test_rated_models_get_ranks_with_bootstrap_ranges():
    r = arena.results(league(seed=2, n=90), seed=1, b=200)
    assert [(row["player"], row["rank"]) for row in r["models"]] == [("p3", 1), ("p2", 2), ("p1", 3)]
    for row in r["models"]:
        lo, hi = row["rank_ci"]
        assert 1 <= lo <= row["rank"] <= hi <= 3
    assert r["models"][0]["rank_ci"][1] < r["models"][-1]["rank_ci"][0]  # p3 and p1 never tie
    level = arena.results(two_players(20) + pairing("z", "x", "y", "B", "A"), seed=1, b=200)
    assert [row["rank_ci"] for row in level["models"]] == [[1, 2], [1, 2]]  # close models share the ranks


def test_bradley_terry_meets_its_score_equations_with_any_prior():
    games = [("x", "y", 1.0, 1), ("y", "x", 1.0, 0), ("x", "z", 0.5, -1), ("z", "y", 1.0, 1)]
    for prior in (0.5, 2.0):
        theta, lg = arena.bradley_terry_seat(games, prior=prior)
        p, g = {k: math.exp(v) for k, v in theta.items()}, math.exp(lg)

        def win(i, j, f, p=p, g=g):
            ci, cj = (g if f > 0 else 1.0), (g if f < 0 else 1.0)
            return ci * p[i] / (ci * p[i] + cj * p[j])

        for k, pk in p.items():
            pts = prior / 2 + sum(s if i == k else 1 - s for i, j, s, _ in games if k in (i, j))
            exp = prior * pk / (pk + 1) + sum(win(i, j, f) if i == k else 1 - win(i, j, f) for i, j, _, f in games if k in (i, j))  # fmt: skip
            assert pts == pytest.approx(exp, rel=1e-8)
        seat_pts = prior / 2 + sum(s if f > 0 else 1 - s for _, _, s, f in games if f)
        seat_exp = prior * g / (g + 1) + sum(win(i, j, f) if f > 0 else 1 - win(i, j, f) for i, j, _, f in games if f)  # fmt: skip
        assert seat_pts == pytest.approx(seat_exp, rel=1e-8)


def test_the_prior_sensitivity_flags_a_sweep_and_not_a_balanced_league():
    sweep = arena.results([x for k in range(15) for x in pairing(f"s{k}", "x", "y", "A", "B")], seed=1, b=50)
    assert sweep["prior"]["priors"] == [0.5, 2.0]
    by = {row["player"]: row for row in sweep["models"]}
    # one virtual draw sets an undefeated model's rating: half or twice that moves it by about 400 log10(2) = 120 Elo
    assert by["x"]["prior_shift"] == pytest.approx(sweep["prior"]["max_shift"])
    assert sweep["prior"]["max_shift"] > 50
    assert by["x"]["prior_flag"] and by["y"]["prior_flag"]  # every replicate is the same sweep: a zero-width interval
    level = arena.results(league(seed=2, n=90), seed=1, b=200)
    assert level["prior"]["max_shift"] < 10
    assert not any(row["prior_flag"] for row in level["models"])


def test_self_matches_fit_the_seat_and_move_no_strength():
    games = [("x", "y", 1.0, 1), ("y", "x", 0.0, 1), ("x", "y", 0.5, 0)]
    _, lg = arena.bradley_terry_seat(games)
    with_self, lg_self = arena.bradley_terry_seat(games + [("x", "x", 1.0, 1), ("s", "s", 0.0, -1), ("s", "s", 1.0, 0)])
    assert sorted(with_self) == ["x", "y"]  # a player seen only against itself gets no strength
    # both flagged self games went to the seat side (s's on side B with B the seat); the unflagged one counts nothing
    assert lg_self > lg
    alone, lg_alone = arena.bradley_terry_seat([("x", "y", 1.0, 0), ("x", "x", 1.0, 1), ("x", "x", 1.0, -1)])
    assert alone == pytest.approx(arena.bradley_terry_seat([("x", "y", 1.0, 0)])[0])  # no pattern in x-y: unmoved
    assert lg_alone == pytest.approx(0.0)  # one seat win, one seat loss


def test_results_fit_the_seat_on_each_self_pairings_first_match():
    rated = [dict(x, r1_pattern=None) for x in two_players(15)]  # no pattern: no seat games among the rated
    selfs = [dict(m(f"s{k}", "d", "d", "B"), r1_pattern="A1|B2|A1") for k in range(6)]  # B holds the pair, wins
    selfs.append(dict(m("s0", "d", "d", "A"), r1_pattern="A1|B2|A1"))  # s0's replay: left out (REVIEW-7 f2)
    plain = arena.results(rated, seed=1, b=50)
    r = arena.results(rated + selfs, seed=1, b=50)
    assert (r["seat"]["n_games"], r["seat"]["n_self_games"]) == (6, 6)
    assert r["seat"]["p_favoured"] == pytest.approx((6 + 0.5) / (6 + 1))  # 6 seat wins and the virtual draw
    assert plain["seat"]["p_favoured"] == pytest.approx(0.5)
    assert [(x["player"], x["elo"]) for x in r["models"] if x["rated"]] == pytest.approx(
        [(x["player"], x["elo"]) for x in plain["models"]]
    )  # the ratings don't move
    assert {x["player"]: x["rated"] for x in r["models"]} == {"x": True, "y": True}  # d: no complete pairing
