# pyright: basic
"""Arena results: pairings, win/draw/loss, the Bradley-Terry fit, its bootstrap interval and the Elo display."""

import math
import random

import pytest

from bg3bench import arena


def m(pairing, a, b, result, valid=True, arena_name="open_field"):
    return {"pairing": pairing, "arena": arena_name, "valid": valid, "sides": {"A": a, "B": b}, "result": result}


def pairing(pid, x, y, r1, r2):
    """Two matches with the sides swapped: x on A then y on A; r1/r2 the side that won each (or draw)."""
    return [m(pid, x, y, r1), m(pid, y, x, r2)]


def league(seed, n=12):
    """Synthetic pairings of three players of increasing strength, drawn from a fixed seed."""
    rng, strength, out = random.Random(seed), {"p1": 1.0, "p2": 2.0, "p3": 4.0}, []
    names = sorted(strength)
    for k in range(n):
        x, y = rng.sample(names, 2)
        for side_a, side_b in ((x, y), (y, x)):
            p = strength[side_a] / (strength[side_a] + strength[side_b])
            out.append(m(f"q{k}", side_a, side_b, "A" if rng.random() < p else "B"))
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


def test_side_swapped_results_cancel():
    # x wins both of p1 (once on each side); in p2 the A side wins both: one win each, so x and y are level there
    r = arena.results([*pairing("p1", "x", "y", "A", "B"), *pairing("p2", "x", "y", "A", "A")], seed=1, b=50)
    by = {row["player"]: row for row in r["models"]}
    assert (by["x"]["w"], by["x"]["l"], by["y"]["w"], by["y"]["l"]) == (3, 1, 1, 3)
    level = arena.results(pairing("p2", "x", "y", "A", "A"), seed=1, b=50)
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


def test_results_pairs_models_and_elo():
    ms = [
        *pairing("a", "x", "y", "A", "B"),  # x wins both
        *pairing("b", "x", "y", "draw", "A"),  # a draw, then y wins on side A
        *pairing("c", "x", "z", "A", "B"),  # x wins both
        *pairing("d", "y", "z", "A", "draw"),  # y wins, a draw
        m("e", "y", "z", "B"),  # incomplete: left out
    ]
    r = arena.results(ms, seed=3, b=200)
    assert r["counts"] == {"matches": 9, "invalid": 0, "self": 0, "incomplete": 1, "pairings": 4}
    assert r["pairs"] == [
        {"a": "x", "b": "y", "w": 2, "d": 1, "l": 1, "n_matches": 4, "n_pairings": 2},
        {"a": "x", "b": "z", "w": 2, "d": 0, "l": 0, "n_matches": 2, "n_pairings": 1},
        {"a": "y", "b": "z", "w": 1, "d": 1, "l": 0, "n_matches": 2, "n_pairings": 1},
    ]
    assert [row["player"] for row in r["models"]] == ["x", "y", "z"]
    x, y, z = r["models"]
    assert (x["n_matches"], x["n_pairings"], x["points"], x["w"], x["d"], x["l"]) == (6, 3, 4.5, 4, 1, 1)
    assert (z["n_matches"], z["n_pairings"], z["points"]) == (4, 2, 0.5)
    assert sum(row["elo"] for row in r["models"]) / 3 == pytest.approx(1500.0)
    for a, b in ((x, y), (y, z)):  # the Elo display is 400 log10 of the strength ratio
        assert a["elo"] - b["elo"] == pytest.approx(400 * (a["bt"] - b["bt"]) / math.log(10))
    for row in r["models"]:
        lo, hi = row["elo_ci"]
        assert lo <= row["elo"] <= hi


def test_results_are_reproducible_from_the_seed():
    ms = league(seed=11)
    one = arena.results(ms, seed=5, b=300)
    assert arena.results(ms, seed=5, b=300) == one
    assert arena.results(ms, seed=6, b=300)["models"] != one["models"]  # another seed, another bootstrap


def test_a_stronger_player_rates_higher_over_many_pairings():
    r = arena.results(league(seed=2, n=60), seed=1, b=200)
    assert [row["player"] for row in r["models"]] == ["p3", "p2", "p1"]
    assert r["models"][0]["elo_ci"][0] > r["models"][-1]["elo_ci"][1]  # p3 and p1 separate


def test_no_complete_pairing_rates_nobody():
    r = arena.results([m("p", "x", "y", "A"), *pairing("s", "x", "x", "A", "B")], seed=1, b=10)
    assert (r["models"], r["pairs"]) == ([], [])
    assert r["counts"] == {"matches": 3, "invalid": 0, "self": 2, "incomplete": 1, "pairings": 0}
