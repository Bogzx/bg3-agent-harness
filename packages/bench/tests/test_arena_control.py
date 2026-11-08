# pyright: basic
"""The turns the game ran itself in an arena match's facts (bg3bench.arena_control, grade's lost_control): live.md
"## Goaded read", the game's AI ran Goaded turns with no order and a player can't control them either."""

import json
from pathlib import Path

import jsonschema
import pytest
from test_arena import ARENA, FORFEIT_A, SIX, match

from bg3bench import arena, arena_control

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
EVENT_SCHEMA = json.loads((PKG / "schema" / "arena-facts.v1.schema.json").read_text())
FACTS_SCHEMA = json.loads((PKG.parents[1] / "packages" / "harness" / "schemas" / "facts.v1.schema.json").read_text())
ASH = "aaaaaaaa-0000-0000-0000-000000000001"


def ev(t, name, **kw):
    return {"v": 1, "kind": "event", "t": float(t), "ev": name, **kw}


def lost(t, **kw):
    return ev(
        t,
        "arena_control_lost",
        **{"side": "A", "character": "Red Ash", "uuid": ASH, "status": "Goaded", "round": 5, **kw},
    )


def test_the_event_fits_both_schemas_and_a_malformed_one_doesnt():
    for rec in (lost(1), lost(1, status=None)):
        jsonschema.validate(rec, EVENT_SCHEMA)
        jsonschema.validate(rec, FACTS_SCHEMA)
    for bad in ({k: v for k, v in lost(1).items() if k != "uuid"}, lost(1, side="C"), lost(1, round=-1)):
        for schema in (EVENT_SCHEMA, FACTS_SCHEMA):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)


def test_lost_control_lists_each_games_turn_with_its_characters_rolls_in_it():
    records = [
        ev(10, "arena_turn", side="A", character="Red Ash"),
        lost(10.5),
        ev(30, "arena_turn", side="A", character="Red Dune"),
        lost(50, round=6),
        ev(60, "arena_end"),
    ]
    rolls = [
        {"t": 12, "roller": ASH},
        {"t": 20, "roller": ASH},
        {"t": 21, "roller": "other"},
        {"t": 31, "roller": ASH},  # after its turn: a reaction on Red Dune's turn
        {"t": 55, "roller": ASH},
    ]
    out = arena_control.lost_control(records, rolls, 60)
    assert out == [
        {"side": "A", "character": "Red Ash", "round": 5, "status": "Goaded", "rolls": 2},
        {"side": "A", "character": "Red Ash", "round": 6, "status": "Goaded", "rolls": 1},
    ]
    assert [x["rolls"] for x in arena_control.lost_control(records, None, 60)] == [None, None]
    assert arena_control.lost_control(records, rolls, 40) == out[:1]  # none after the end


def test_a_turn_capped_under_a_charge_breaks_the_forced_streak():
    """A's 0, 2, 4 forced, turn 2 under a charge (arena_turn_forced's charged_cap): no forfeit is due, so a match that
    goes on is whole, and a forfeit claimed on that streak isn't."""
    draw = {"winner": None, "reason": "round_cap", "hp": (1.0, 1.0)}

    def marked(recs):
        return [{**r, "charged_cap": True} if r.get("ev") == "arena_turn_forced" and r["character"] == "Gale" else r
                for r in recs]  # fmt: skip

    g = arena.grade(marked(match(turns=SIX, forced=(0, 2, 4), **draw)), ARENA)
    assert g["valid"] is True and g["forced"]["A"] == 3
    g = arena.grade(marked(match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A)), ARENA)
    assert g["invalid_reason"] == "inconsistent_end"
    row = ev(1, "arena_turn_forced", side="A", character="Gale", charged_cap=True)
    for schema in (EVENT_SCHEMA, FACTS_SCHEMA):
        jsonschema.validate(row, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**row, "charged_cap": "yes"}, schema)
