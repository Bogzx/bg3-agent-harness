import json
from pathlib import Path

import pytest

from bg3bench import predicates as p

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "goal.v1.schema.json"

VALID = [
    {"gold": {"gte": 1000}},
    {"item": {"name": "Umbral Gem"}},
    {"item": {"tpl": "abc-123", "gte": 2}},
    {"party_level": {"gte": 3}},
    {"party_level": {"gte": 3, "agg": "max"}},
    {"companions": {"gte": 3}},
    {"combats_won": {"gte": 1}},
    {"area": "goblin camp|shattered sanctum"},
    {"region": "wilderness"},
    {"quest_done": "escape the nautiloid"},
    {"game_end": True},
    {"milestone": "nautiloid_escaped"},
    {"all": [{"gold": {"gte": 100}}, {"no_combat": True}]},
    {"all": [{"area": "grove"}, {"survive_s": 1800}]},
    {"ever": {"all": [{"gold": {"gte": 100}}, {"area": "camp"}]}},
    {"all": [{"region": "x"}, {"never": {"party_dead": {"gte": 1}}}, {"not": {"wipe": True}}]},
    {"any": [{"gold": {"gte": 100}}, {"item": {"name": "Idol"}}]},
    {"all": [{"state": {"gold": {"gte": 10, "lte": 20}}}, {"combats_started": {"lte": 0}}, {"elapsed_s": {"gte": 5}}]},
    {"all": [{"region": "x"}, {"state": {"combat": True}}]},
]

MALFORMED = [
    {},
    {"gold": {"gte": 1}, "area": "x"},
    {"bogus": 1},
    {"gold": 1000},
    {"gold": {}},
    {"gold": {"gte": -1}},
    {"gold": {"gte": True}},
    {"gold": {"gt": 1}},
    {"gold": {"gte": 1, "agg": "max"}},
    {"party_level": {"agg": "max"}},
    {"party_level": {"gte": 2, "agg": "mean"}},
    {"area": ""},
    {"area": 3},
    {"item": {"name": "a", "tpl": "b"}},
    {"item": {}},
    {"item": {"name": ""}},
    {"item": {"name": "a", "count": 2}},
    {"combat": False},
    {"no_combat": False},
    {"all": []},
    {"any": {"gold": {"gte": 1}}},
    {"survive_s": -5},
    [{"gold": {"gte": 1}}],
]


@pytest.mark.parametrize("goal", VALID)
def test_valid_goals_parse(goal):
    p.parse(goal)


@pytest.mark.parametrize("goal", MALFORMED + [{"area": "("}, {"milestone": "nope"}])
def test_malformed_goals_raise(goal):
    with pytest.raises(p.GoalError):
        p.parse(goal, allow_passive=True)


def test_schema_agrees_with_parser():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    for goal in VALID:
        assert validator.is_valid(goal), goal
    for goal in MALFORMED:
        assert not validator.is_valid(goal), goal


def test_bare_leaf_means_ever_and_mode_children_are_instant():
    node = p.parse({"gold": {"gte": 5}})
    assert isinstance(node, p.Mode) and node.mode == "ever" and isinstance(node.child, p.Leaf)
    node = p.parse({"never": {"all": [{"combat": True}, {"wipe": True}]}}, allow_passive=True)
    assert all(isinstance(c, p.Leaf) for c in node.child.children)
    node = p.parse({"all": [{"gold": {"gte": 5}}, {"not": {"combat": True}}]})
    assert node.children[1].child == p.Mode("ever", p.Leaf("combat"))


def test_shorthands_expand():
    assert p.parse({"milestone": "nautiloid_escaped"}) == p.parse({"quest_done": "escape the nautiloid"})
    assert p.parse({"no_combat": True}, allow_passive=True) == p.Mode("never", p.Leaf("combat"))
    survive = p.parse({"survive_s": 60}, allow_passive=True)
    assert survive == p.All(
        (p.Mode("state", p.Leaf("elapsed_s", gte=60)), p.Mode("never", p.Leaf("party_dead", gte=1)))
    )


@pytest.mark.parametrize(
    "goal",
    [
        {"no_combat": True},
        {"survive_s": 1800},
        {"elapsed_s": {"gte": 60}},
        {"gold": {"lte": 100}},
        {"gold": {"gte": 0}},
        {"companions": {"gte": 0}},
        {"item": {"name": "Idol", "gte": 0}},
        {"not": {"gold": {"gte": 100}}},
        {"never": {"combat": True}},
        {"all": [{"no_combat": True}, {"survive_s": 600}]},
        {"any": [{"gold": {"gte": 100}}, {"no_combat": True}]},
        {"combats_started": {"gte": 1}},
        {"party_dead": {"gte": 1}},
        {"wipe": True},
        {"combat": True},
    ],
)
def test_d8_rejects_goals_a_do_nothing_agent_could_pass(goal):
    with pytest.raises(p.GoalError, match="D8"):
        p.parse(goal)
    p.parse(goal, allow_passive=True)


@pytest.mark.parametrize(
    "goal",
    [
        {"all": [{"gold": {"gte": 100}}, {"no_combat": True}]},
        {"all": [{"survive_s": 1800}, {"party_level": {"gte": 3}}]},
        {"any": [{"gold": {"gte": 100}}, {"area": "grove"}]},
        {"state": {"item": {"name": "Idol"}}},
        {"milestone": "level_2"},
        {"milestone": "game_end"},
    ],
)
def test_d8_accepts_goals_with_a_positive_conjunct(goal):
    assert p.positive(p.parse(goal))


def test_milestones_cover_the_old_list():
    assert len(p.MILESTONES) == 31
    assert p.MILESTONES["first_companion"] == {"companions": {"gte": 1}}
    assert p.MILESTONES["level_4"] == {"party_level": {"gte": 4, "agg": "max"}}
    for goal in p.MILESTONES.values():
        p.parse(goal)


def test_milestones_match_the_bridge():
    old = pytest.importorskip("bg3ai.milestones")
    assert [(m[0], m[3], m[4]) for m in old.MILESTONES] == p._MILESTONE_ROWS


def test_positive_rejects_non_nodes():
    with pytest.raises(TypeError):
        p.positive("nope")
