#!/usr/bin/env python3
# pyright: basic
"""A `party_down` from the referee ring in a match: it reaches the match's facts.jsonl, passes the facts schema and
changes nothing the arena's grade reads (the match ends on arena.status)."""

from __future__ import annotations

from pathlib import Path

from bg3harness.arena import match as am
from test_arena_match import ARENA, SIDES, arena_events, attacker, grade, passer, records, setup


def play(out: Path, downs: bool) -> am.ArenaOutcome:
    """A full match to a wipe; downs=True: the referee ring also records every side's character going down."""
    model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    if downs:
        damage = model.arena_damage

        def noted(name: str, by: str, n: int) -> None:
            damage(name, by, n)
            model.ref_record("party_down", name=name, party=True, uuid=f"{name}_0000")

        model.arena_damage = noted  # pyright: ignore[reportAttributeAccessIssue]
    return am.run_arena(spec, SIDES, game, agents, out, clock)


def test_a_rings_party_down_rows_change_no_arena_result(tmp_path: Path) -> None:
    plain = play(tmp_path / "plain", False)
    downed = play(tmp_path / "downed", True)
    rows = [(r["ev"], r["name"]) for r in records(tmp_path / "downed") if r.get("ev") == "party_down"]
    assert rows == [("party_down", "Wyll"), ("party_down", "Halsin")]
    assert (plain.end, plain.invalid) == (downed.end, downed.invalid)
    assert [{k: v for k, v in e.items() if k not in ("seq",)} for e in arena_events(tmp_path / "plain")] == [
        {k: v for k, v in e.items() if k not in ("seq",)} for e in arena_events(tmp_path / "downed")
    ]
    assert grade(tmp_path / "plain", ARENA) == grade(tmp_path / "downed", ARENA)  # grade() validates every record
