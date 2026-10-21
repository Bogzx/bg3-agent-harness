"""tools/stress/matrix.toml (the real file) and stress_matrix's validation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import stress_core as core
import stress_matrix as sm
from bg3ai.milestones import MILESTONES
from bg3bench.predicates import parse as parse_goal
from bg3bench.scenario_v1 import validate_scenario
from stress_io import acceptance_sections

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MILESTONE_IDS = [m[0] for m in MILESTONES]
HEAD = """
[defaults]
profile = "profiles/opus-5.5-claude-code.toml"
max_cost = 1.0
minutes = 20
character_brief = "b"
min_free_gb = 4.0
max_cpu_c = 90
guard_wait_s = 900
guard_poll_s = 30
"""


@pytest.fixture(scope="module")
def matrix() -> sm.Matrix:
    return sm.parse((HERE / "matrix.toml").read_text())


def one(row: str, head: str = HEAD) -> str:
    return head + f'\n[[group]]\nkey = "g"\ntitle = "G"\nrows = [\n  {row},\n]\n'


def acceptance_strings() -> set[str]:
    out = set()
    for p in (REPO / "tools" / "dev" / "acceptance").glob("*.py"):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.add(n.value)
            elif isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant):
                out.add(str(n.values[0].value))
    return out


def test_the_matrix_has_every_row_of_the_live_workers_table(matrix):
    # research/ovh/STRESS-TEST.md: 14 + 29 + 30 + 12 + 8 + 9 + 9 + 15 + 9 + 13 + 6 = 154 rows
    sizes = [sum(r.group == k for r in matrix.rows) for k, _ in matrix.groups]
    assert sizes == [14, 29, 30, 12, 8, 9, 9, 15, 9, 13, 6]
    assert {c.kind for r in matrix.rows for c in r.checks} == set(sm.KINDS)


def test_the_matrix_names_real_sections_and_milestones(matrix):
    assert sm.problems(matrix, acceptance_sections(REPO), MILESTONE_IDS) == []


def test_every_acc_item_is_an_item_the_acceptance_writes(matrix):
    # an item name the acceptance no longer writes would be an ERROR on every live run
    strings = acceptance_strings()
    missing = [
        c.key
        for r in matrix.rows
        for c in r.checks
        if c.kind == "acc" and not any(s.startswith(c.spec["item"]) for s in strings)
    ]
    assert missing == []


def test_every_goal_is_a_valid_predicate(matrix):
    goals = [c.spec["goal"] for r in matrix.rows for c in r.checks if c.kind == "agent" and "goal" in c.spec]
    assert len(goals) > 10
    for g in goals:
        parse_goal(g)  # raises on a bad predicate


def test_every_agent_check_resolves_to_a_valid_v1_scenario(matrix):
    n = 0
    for r in matrix.rows:
        for c in r.checks:
            if c.kind != "agent":
                continue
            start = (f"Claude-4810126690__{core.copy_label(r)}", "Claude") if r.save else None
            sc = core.resolve_scenario(r, c, matrix.defaults, start, None)
            assert validate_scenario(sc, sc["name"], MILESTONE_IDS, ("strict",), ()) == [], c.key
            n += 1
    assert n > 70


def test_a_probe_on_a_new_game_or_an_agent_needs_a_start(matrix):
    for r in matrix.rows:
        if any(c.kind in ("probe", "agent") for c in r.checks) and not r.save:
            assert r.act == "P", r.id


@pytest.mark.parametrize(
    ("row", "why"),
    [
        ('{id = "X1", situation = "s", act = "4", save = "", check = [{kind = "manual", what = "w"}]}', "act must"),
        ('{id = "x1", situation = "s", act = "1", save = "", check = [{kind = "manual", what = "w"}]}', "id must"),
        ('{id = "X1", situation = "s", act = "1", save = "a b", check = [{kind = "manual", what = "w"}]}', "save"),
        ('{id = "X1", situation = "s", act = "1", save = "", check = []}', "non-empty"),
        ('{id = "X1", situation = "s", act = "1", save = "", check = [{kind = "nope"}]}', "kind must"),
        ('{id = "X1", situation = "s", act = "1", save = "", check = [{kind = "acc", section = "se"}]}', "item is"),
        ('{id = "X1", situation = "s", act = "1", save = "", check = [{kind = "manual", what = "w", x = 1}]}', "'x'"),
        ('{id = "X1", situation = "s", act = "1", save = "", hm = 1, check = [{kind = "manual", what = "w"}]}', "hm"),
        (
            '{id = "X1", situation = "s", act = "1", save = "S1", check = [{kind = "probe", script = "p", '
            "ready = false}]}",
            "needs a why",
        ),
        (
            '{id = "X1", situation = "s", act = "1", save = "S1", check = [{kind = "probe", script = "p", '
            "args = [1]}]}",
            "list of strings",
        ),
        (
            '{id = "X1", situation = "s", act = "1", save = "S1", check = [{kind = "agent", where = "w", '
            'objective = "o", minutes = -1}]}',
            "positive",
        ),
        (
            '{id = "X1", situation = "s", act = "1", save = "S1", check = [{kind = "agent", where = "w", '
            'objective = "o", goal = "x"}]}',
            "predicate table",
        ),
        (
            '{id = "X1", situation = "s", act = "1", save = "", check = [{kind = "agent", where = "w", '
            'objective = "o"}]}',
            "belongs to act P",
        ),
    ],
)
def test_bad_rows_are_refused_with_the_reason(row, why):
    with pytest.raises(sm.MatrixError, match=why):
        sm.parse(one(row))


def test_duplicate_ids_bad_toml_and_missing_defaults_are_refused():
    row = '{id = "X1", situation = "s", act = "1", save = "", check = [{kind = "manual", what = "w"}]}'
    with pytest.raises(sm.MatrixError, match="duplicate id"):
        sm.parse(one(row + ",\n  " + row))
    with pytest.raises(sm.MatrixError, match=r"matrix\.toml"):
        sm.parse("[[group]\n")
    with pytest.raises(sm.MatrixError, match=r"defaults\.max_cpu_c is required"):
        sm.parse(one(row, HEAD.replace("max_cpu_c = 90\n", "")))
    with pytest.raises(sm.MatrixError, match="no \\[\\[group\\]\\]"):
        sm.parse(HEAD)


def test_problems_names_unknown_sections_and_milestones():
    m = sm.parse(
        one(
            '{id = "X1", situation = "s", act = "1", save = "S1", check = [{kind = "acc", section = "nope", '
            'item = "i"}, {kind = "agent", where = "w", objective = "o", until = "mars"}]}'
        )
    )
    assert sm.problems(m, ["se"], ["game_end"]) == [
        "X1#0: no acceptance section 'nope' (have se)",
        "X1#1: until 'mars' is not a milestone",
    ]


def test_check_ids_resolve_and_unknown_ones_are_refused(matrix):
    assert matrix.check("G6#1").kind == "manual"
    for bad in ("G6#9", "G6", "Q1#0", "G6#x"):
        with pytest.raises(sm.MatrixError, match="no check"):
            matrix.check(bad)
