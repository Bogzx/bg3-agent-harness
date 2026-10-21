import json
from pathlib import Path

import pytest

from bg3bench import methods

FACTS_SCHEMA = Path(__file__).resolve().parents[3] / "packages" / "harness" / "schemas" / "facts.v1.schema.json"


def ev(t, kind, **kw):
    return {"v": 1, "kind": "event", "t": t, "game_t": t, "ev": kind, **kw}


def gold(t, total, delta):
    return ev(t, "gold", gold=total, delta=delta)


def by(out):
    return {k: v for k, v in out["gold_by_method"].items() if v}


def test_sold_inside_and_just_after_a_trade_window():
    tl = [
        ev(10, "trade_open", npc="Arron"),
        gold(12, 150, 100),
        ev(20, "trade_end", npc="Arron"),
        gold(22, 200, 50),
        gold(30, 230, 30),
    ]
    out = methods.attribute(tl)
    assert by(out) == {"sold": 150, "other": 30}
    assert [c["method"] for c in out["changes"]] == ["sold", "sold", "other"]


def test_spent_in_trade_and_lost_outside():
    tl = [ev(10, "trade_open"), gold(11, 50, -100), ev(15, "trade_end"), gold(40, 30, -20)]
    out = methods.attribute(tl)
    assert (out["spent"], out["lost"]) == (100, 20)
    assert by(out) == {}
    assert [c["method"] for c in out["changes"]] == ["spent", "lost"]


def test_a_trade_left_open_lasts_to_the_end():
    out = methods.attribute([ev(10, "trade_open"), gold(500, 60, 60)])
    assert by(out) == {"sold": 60}


def test_pickpocket_and_stole():
    tl = [
        ev(10, "pickpocket", ok=True, npc="Arron", item="Gold", count=40),
        gold(11, 40, 40),
        ev(50, "stole", char="Tav", item="Gold", tpl="tpl-gold"),
        gold(52, 65, 25),
        ev(90, "pickpocket", ok=False, npc="Guard"),
        gold(91, 75, 10),
        ev(92, "crime", type="Pickpocketing", victim="Guard", criminal="Tav"),
    ]
    out = methods.attribute(tl)
    assert by(out) == {"stolen": 65, "other": 10}  # a failed pickpocket steals nothing
    assert (out["pickpockets"], out["pickpockets_failed"], out["crimes"]) == (1, 1, 1)


def test_loot_from_corpse_container_or_gold_stack():
    tl = [
        ev(10, "loot", **{"from": "corpse"}, item="Gold"),
        gold(12, 20, 20),
        ev(30, "item_added", name="Gold", count=15, gold=True),
        gold(31, 35, 15),
        ev(60, "item_added", name="Dagger", count=1),
        gold(61, 40, 5),
    ]
    out = methods.attribute(tl)
    assert by(out) == {"looted": 35, "other": 5}


def test_quest_reward():
    tl = [ev(100, "quest_done", title="Save the Refugees"), gold(102, 200, 200), gold(110, 250, 50)]
    out = methods.attribute(tl)
    assert by(out) == {"quest": 200, "other": 50}


def test_unattributed_and_window_edges():
    tl = [ev(10, "quest_done", title="A"), gold(13, 10, 10), gold(13.5, 20, 10), gold(9, 0, 0)]
    out = methods.attribute(sorted(tl, key=lambda r: r["t"]))
    assert by(out) == {"quest": 10, "other": 10}  # 3 s after is inside, 3.5 s is not; a zero delta is skipped
    assert len(out["changes"]) == 2
    assert by(methods.attribute(tl, window=5.0)) == {"quest": 20}


def test_mixed_session_with_kills_and_priority():
    tl = [
        ev(5, "kill", victim="Goblin", killer="Tav"),
        ev(6, "loot", **{"from": "corpse"}),
        gold(7, 12, 12),
        ev(20, "kill", victim="Arron", killer="Tav", trader=True),
        ev(21, "crime", type="Murder", victim="Arron"),
        ev(30, "trade_open", npc="Dammon"),
        ev(31, "quest_done", title="Q"),
        gold(32, 112, 100),  # a quest reward during a trade counts as sold (the documented mislabel)
        gold(33, 62, -50),
        ev(40, "trade_end"),
        ev(60, "pickpocket", ok=True, npc="Zevlor"),
        ev(61, "loot", **{"from": "container"}),
        gold(62, 92, 30),  # stolen wins over looted
        {"v": 1, "kind": "snapshot", "t": 70, "gold": 999},
        {"v": 1, "kind": "mark", "t": 80, "mark": "run_end"},
    ]
    out = methods.attribute(tl)
    assert out["gold_by_method"] == {"sold": 100, "stolen": 30, "looted": 12, "quest": 0, "other": 0}
    assert (out["spent"], out["lost"], out["kills"], out["traders_killed"], out["crimes"]) == (50, 0, 2, 1, 1)
    assert out["changes"] == [
        {"t": 7, "delta": 12, "method": "looted"},
        {"t": 32, "delta": 100, "method": "sold"},
        {"t": 33, "delta": -50, "method": "spent"},
        {"t": 62, "delta": 30, "method": "stolen"},
    ]


def test_empty_timeline():
    out = methods.attribute([])
    assert out["gold_by_method"] == dict.fromkeys(methods.METHODS, 0)
    assert out["changes"] == [] and out["crimes"] == 0


def test_synthetic_events_match_the_facts_schema():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(FACTS_SCHEMA.read_text()))
    for rec in [
        ev(1, "pickpocket", ok=True, npc="A", item="Gold", count=4),
        ev(1, "stole", char="T", item="I"),
        ev(1, "crime", type="Theft"),
        ev(1, "kill", victim="Arron", trader=True),
        ev(1, "loot", **{"from": "corpse"}),
        ev(1, "trade_open", npc="A"),
        ev(1, "trade_end"),
        gold(1, 5, 5),
        ev(1, "item_added", name="Gold", count=3, gold=True),
    ]:
        assert validator.is_valid(rec), rec
    assert not validator.is_valid(ev(1, "kill"))
    assert not validator.is_valid(ev(1, "pickpocket"))
