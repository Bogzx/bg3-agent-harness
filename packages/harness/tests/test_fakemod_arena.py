"""The fake mod in a two-team match: a call's envelope side reaches the scripted game, which answers from that team's
view (party, hostiles it sees, the acting combatant only when seen, per-side events) and refuses acting out of turn
or with the other team's character; a call without a side gets the answers it always got."""

from __future__ import annotations

import json
from typing import Any

from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.scripted import FakeClock, GameModel


def rpc(fake: FakeMod, op: str, args: dict[str, Any] | None = None, side: str | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"op": op, "args": args or {}}
    if side:
        req["side"] = side
    return json.loads(fake.answer("server", f"return Mods.BG3AI.BG3AI.rpc([=[{json.dumps(req)}]=])"))


def match() -> tuple[GameModel, FakeMod]:
    m = GameModel(clock=FakeClock())
    m.start_arena(["Tav", "Shadowheart"], ["Lae'zel", "Karlach"])
    return m, FakeMod(m)


def party(reply: dict[str, Any]) -> list[str]:
    return [r["name"] for r in reply["result"]["party"]]


def test_each_side_sees_its_own_team() -> None:
    _, fake = match()
    assert party(rpc(fake, "state.status", side="A")) == ["Tav", "Shadowheart"]
    assert party(rpc(fake, "state.status", side="B")) == ["Lae'zel", "Karlach"]
    rows = rpc(fake, "state.combat", side="B")["result"]["order"]
    assert {r["name"]: r["relation"] for r in rows} == {
        "Tav": "hostile",
        "Lae'zel": "party",
        "Shadowheart": "hostile",
        "Karlach": "party",
    }
    assert fake.sides() == {"A": 1, "B": 2}


def test_no_side_answers_as_before() -> None:
    _, fake = match()
    plain = GameModel(clock=FakeClock())
    assert rpc(fake, "state.status") == json.loads(
        FakeMod(plain).answer("server", 'return Mods.BG3AI.BG3AI.rpc([=[{"op":"state.status","args":{}}]=])')
    )
    assert fake.sides() == {None: 1}


def test_turns_and_refusals() -> None:
    m, fake = match()
    assert m.arena is not None
    tav, lae = m.arena.teams["A"][0].uuid, m.arena.teams["B"][0].uuid
    refused = rpc(fake, "act.move_to", {"char": lae, "pos": [1, 0, 0]}, side="B")["result"]
    assert (refused["status"], refused["error"]) == ("failed", "not your turn: Tav (side A) is acting")
    other = rpc(fake, "act.end_turn", {"char": tav}, side="B")["result"]
    assert other["error"] == "Tav is not on your side"
    assert rpc(fake, "act.end_turn", {"char": tav}, side="A")["result"]["status"] == "done"
    assert rpc(fake, "act.move_to", {"char": lae, "pos": [1, 0, 0]}, side="B")["result"]["status"] == "done"
    assert m.moves == [("B", lae, [1.0, 0.0, 0.0])]
    assert rpc(fake, "act.end_turn", {"char": lae}, side="B")["result"]["status"] == "done"
    m.hide("Shadowheart")
    view = rpc(fake, "state.combat", side="B")["result"]
    assert view["current"] is None and view["turn_group"] == []
    assert "Shadowheart" not in [r["name"] for r in view["order"]]
    hidden = rpc(fake, "act.end_turn", {"char": lae}, side="B")["result"]
    assert hidden["error"] == "not your turn: the other side is acting"


def test_events_follow_what_a_side_sees() -> None:
    m, fake = match()
    m.arena_end_turn()  # Tav -> Lae'zel
    m.hide("Shadowheart")
    m.arena_end_turn()  # Lae'zel -> Shadowheart (hidden from B)

    def turns(side: str) -> list[tuple[str, str]]:
        evs = rpc(fake, "events.since", {"seq": 0}, side=side)["result"]["events"]
        assert all("seen_by" not in e for e in evs)
        return [(e["kind"], e["data"]["name"]) for e in evs if e["kind"].startswith("turn_")]

    assert ("turn_started", "Shadowheart") in turns("A")
    assert all(name != "Shadowheart" for _, name in turns("B"))
