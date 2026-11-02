"""The fake mod in a two-team match: a call's envelope side reaches the scripted game, which answers from that team's
view (party, hostiles it sees, the acting combatant only when seen, per-side events, its own jobs) and refuses acting
out of turn, with the other team's character or on the other side's reaction prompt; a call without a side gets the
answers it always got."""

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
    assert (refused["status"], refused["error"]) == ("failed", "not your turn (side A is acting)")
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
    assert hidden["error"] == "not your turn (side A is acting)"


def test_events_follow_what_a_side_sees() -> None:
    m, fake = match()
    assert m.arena is not None
    tav = m.arena.teams["A"][0].uuid
    m.arena_end_turn()  # Tav -> Lae'zel
    m.hide("Shadowheart")
    m.arena_end_turn()  # Lae'zel -> Shadowheart (hidden from B)
    m.arena_cast("Tav", "Projectile_FireBolt", "Fire Bolt")
    m.arena_cast("Shadowheart", "Target_Bless", "Bless")
    m.arena_damage("Lae'zel", "Shadowheart", 4)

    def events(side: str | None) -> list[dict[str, Any]]:
        evs = rpc(fake, "events.since", {"seq": 0}, side=side)["result"]["events"]
        assert all("views" not in e for e in evs)
        return evs

    a, b, harness = events("A"), events("B"), events(None)
    assert ("turn_started", "Shadowheart") in [(e["kind"], e["data"]["name"]) for e in a if e["kind"] == "turn_started"]
    assert "Shadowheart" not in json.dumps(b) and "Target_Bless" not in json.dumps(b) and "Bless" not in json.dumps(b)
    casts_b = [e["data"] for e in b if e["kind"] == "cast"]
    assert casts_b == [{"caster": {"uuid": tav, "name": "Tav"}, "name": "Fire Bolt"}]
    assert [e["data"]["source"] for e in b if e["kind"] == "damage"] == [{"name": "Someone"}]
    assert [e["data"].get("spell") for e in a if e["kind"] == "cast"] == ["Projectile_FireBolt", "Target_Bless"]
    assert [e["data"]["source"]["name"] for e in a if e["kind"] == "damage"] == ["Shadowheart"]
    assert "Target_Bless" in json.dumps(harness) and "Shadowheart" in json.dumps(harness)


def test_jobs_belong_to_their_side() -> None:
    m, fake = match()
    assert m.arena is not None
    job = rpc(fake, "act.end_turn", {"char": m.arena.teams["A"][0].uuid}, side="A")["result"]["job"]
    assert rpc(fake, "job.poll", {"id": job}, side="A")["result"]["status"] == "done"
    assert rpc(fake, "job.poll", {"id": job}, side="B") == {"ok": False, "error": "not your job"}
    assert rpc(fake, "job.poll", {"id": job})["result"]["status"] == "done"  # the harness: no side


def test_reaction_prompt_is_its_sides() -> None:
    m, fake = match()
    m.open_reaction("Tav")
    ui = rpc(fake, "ui.state", side="B")["result"]  # marked as the other side's, no rows (the bridge: WAITING)
    assert ui["screen"] == "reaction" and ui["reaction"] == {"other_side": True}
    own = rpc(fake, "ui.state", side="A")["result"]
    assert own["reaction"]["options"][0]["who"] == "Tav"
    assert rpc(fake, "ui.state")["result"]["reaction"]["options"][0]["who"] == "Tav"  # no side: as the screen shows
    refused = rpc(fake, "ui.react", {"use": True}, side="B")["result"]
    assert (refused["status"], refused["error"]) == ("failed", "not your reaction")
    assert rpc(fake, "ui.react", {"use": False}, side="A")["result"]["status"] == "done"
    assert m.reacted == [("Tav", False)] and m.reaction is None


def test_a_round_change_in_the_games_order() -> None:
    """Events.lua tags turn events with their turn's round; at a round change the game starts the new round's turn
    before it ends the old round's (phases4 load1: CombatRoundStarted, TurnStarted, then the old TurnEnded)."""
    m, fake = match()  # order: Tav, Lae'zel, Shadowheart, Karlach
    for _ in range(4):
        m.arena_end_turn()
    evs = rpc(fake, "events.since", {"seq": 0}, side="A")["result"]["events"]
    turns = [
        (e["kind"], e["data"].get("name"), e["data"]["round"])
        for e in evs
        if e["kind"] in ("round_started", "turn_started", "turn_ended")
    ]
    assert turns[-5:] == [
        ("turn_ended", "Shadowheart", 1),
        ("turn_started", "Karlach", 1),
        ("round_started", None, 2),
        ("turn_started", "Tav", 2),
        ("turn_ended", "Karlach", 1),
    ]
    assert turns[:2] == [("round_started", None, 1), ("turn_started", "Tav", 1)]


def test_a_downed_member_reads_downed_to_both_sides() -> None:
    """A player character at 0 HP is down where both sides see it (S.combat: the game's DOWNED status, any relation)."""
    m, fake = match()
    m.arena_damage("Tav", "Lae'zel", 99)

    def tav_row(side: str) -> dict[str, Any]:
        rows = rpc(fake, "state.combat", side=side)["result"]["order"]
        return next(r for r in rows if r["name"] == "Tav")

    assert tav_row("A")["relation"] == "party" and tav_row("A")["downed"] is True
    assert tav_row("B")["relation"] == "hostile" and tav_row("B")["downed"] is True
    rows = rpc(fake, "state.combat", side="B")["result"]["order"]
    assert [r["name"] for r in rows if r.get("downed")] == ["Tav"]
