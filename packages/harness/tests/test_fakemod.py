# pyright: basic
# (basic, not strict: these tests drive the legacy bridge modules, which are untyped and on pyright's ignore list)
"""The fake mod against the bridge's real LuaDebug client and Game: wire protocol, responders, fixture replay, the
scripted game model and the recorder round trip (record a session, replay it, same answers)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from bg3ai import game as game_mod
from bg3ai import referee, rpc_record
from bg3ai.game import Game, GameError, NotReady
from bg3ai.luadebug import LuaDebugClient
from bg3harness.testing import wire
from bg3harness.testing.fakemod import FakeMod, Ops
from bg3harness.testing.fixtures import FixtureStore
from bg3harness.testing.scripted import FakeClock, GameModel

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpc"

pytestmark = pytest.mark.integration


def game_for(fake: FakeMod) -> Game:
    g = Game(port=fake.port, pool_max=2)
    g._ev_synced = True  # the events cursor starts at 0 (the ring is fresh in every test)
    g.job_push = False
    return g


@pytest.fixture
def model() -> GameModel:
    return GameModel(clock=FakeClock())


@pytest.fixture
def fake(model: GameModel) -> Iterator[FakeMod]:
    with FakeMod(model) as f:
        yield f


@pytest.fixture
def game(fake: FakeMod) -> Iterator[Game]:
    g = game_for(fake)
    yield g
    g.close()


# ---- wire ----


@pytest.mark.unit
@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 2**31, 2**63])
def test_varint_round_trip(n: int) -> None:
    msg = wire.fields(wire.uint_field(5, n) + wire.bytes_field(6, b"xy"))
    assert msg == {5: [n], 6: [b"xy"]}


@pytest.mark.unit
def test_parse_requests() -> None:
    assert wire.parse_request(wire.uint_field(1, 7) + wire.bytes_field(3, wire.uint_field(1, 5))).kind == "connect"
    ev = wire.uint_field(1, 1) + wire.bytes_field(2, b"return 1") + wire.uint_field(4, 3)
    req = wire.parse_request(wire.uint_field(1, 9) + wire.bytes_field(7, ev))
    assert (req.kind, req.seq, req.context, req.code) == ("evaluate", 9, "client", "return 1")
    req = wire.parse_request(wire.uint_field(1, 2) + wire.bytes_field(7, wire.bytes_field(2, b"x")))
    assert req.context == "server"
    assert wire.parse_request(wire.uint_field(1, 3) + wire.bytes_field(4, b"")).kind == "other"


def test_real_client_connects_and_evaluates(fake: FakeMod) -> None:
    with LuaDebugClient("127.0.0.1", fake.port) as c:
        assert c.connect() == wire.PROTOCOL_VERSION
        assert c.evaluate("return tostring(Ext.Utils.GetGameState())", "client") == "Running"
        reply = json.loads(c.evaluate('return Mods.BG3AI.BG3AI.rpc([=[{"op":"sys.ping","args":{}}]=])', "server"))
        assert reply == {"ok": True, "result": "pong"}
        with pytest.raises(Exception, match="fake mod can't run"):
            c.evaluate("return 1+", "server")


def test_broadcast_reaches_attached_clients(fake: FakeMod) -> None:
    with LuaDebugClient("127.0.0.1", fake.port) as c:
        c.connect()
        fake.broadcast("BG3AI.job server 3 done")
        line = c.output.get(timeout=5)
    assert line.message == "BG3AI.job server 3 done"


# ---- Game over the fake mod ----


def test_rpc_multi_and_refusals(game: Game, fake: FakeMod) -> None:
    assert game.rpc("state.status")["gold"] == 100
    assert game.multi([("state.combat", {}), ("no.such", {})]) == [{"active": False}, None]
    with pytest.raises(GameError, match=r"unknown op 'no\.such' in server context"):
        game.rpc("no.such")
    assert fake.ops() == ["state.status", "state.combat", "no.such", "no.such"]


def test_lua_error_reply_is_not_ready() -> None:
    boot = Ops({"x.y": lambda c, a: 1})

    class Booting:
        def reply(self, ctx, op, args):
            return {"lua_error": "attempt to index a nil value (field 'BG3AI')"} if op == "sys.ping" else None

        def eval_raw(self, ctx, code):
            return None

    with FakeMod(Booting(), boot) as f:
        g = game_for(f)
        with pytest.raises(NotReady, match="still loading"):
            g.rpc("sys.ping")
        assert g.rpc("x.y") == 1
        g.close()


def test_ops_responder_error_is_a_reply() -> None:
    def boom(ctx: str, args: dict) -> None:
        raise RuntimeError("no dialogue on screen")

    with FakeMod(Ops({"ui.x": boom})) as f:
        g = game_for(f)
        with pytest.raises(GameError, match="no dialogue on screen"):
            g.rpc("ui.x", context="client")
        g.close()


# ---- scripted model ----


def test_events_ring_and_scenario_steps(game: Game, model: GameModel) -> None:
    model.add_gold(25)
    model.start_fight(["Goblin"])
    kinds = [e["kind"] for e in game.drain_events("server")]
    assert kinds == ["gold_changed", "combat_started", "round_started", "turn_started"]
    st = game.rpc("state.status")
    assert (st["gold"], st["in_combat"], st["combat"]["round"]) == (125, True, 1)
    model.kill("Goblin")
    model.end_fight()
    assert [e["kind"] for e in game.drain_events("server")] == ["died", "combat_ended"]
    assert game.rpc("state.combat") == {"active": False}


def test_dialogue_choose_job(game: Game, model: GameModel) -> None:
    model.open_dialogue("Arron", "What do you want?", ["Trade.", "Nothing."])
    ui = game.rpc("ui.state", context="client")
    assert ui["screen"] == "dialogue" and [o["text"] for o in ui["dialogue"]["options"]] == ["Trade.", "Nothing."]
    with pytest.raises(GameError, match="no option 3"):
        game.rpc("ui.dialogue_choose", {"i": 3}, "client")
    r = game.run_job("ui.dialogue_choose", {"i": 2}, "client")
    assert (r.status, r.result["ended"], model.chosen) == ("done", True, [2])
    assert game.rpc("ui.state", context="client")["screen"] == "game"


def test_referee_and_rolls_through_ring_reader(game: Game, model: GameModel) -> None:
    rpc = lambda op, args: game.rpc(op, args)  # noqa: E731
    wall = iter([500.0, 600.0, 700.0, 800.0]).__next__
    ref = referee.referee(rpc, wall)
    ref.sync()
    model.clock.advance(2000)
    model.referee("trade_end", npc="Mattis", gold_in=0, gold_out=1, bought=[{"name": "Shovel", "count": 1}])
    model.clock.advance(1000)
    recs = ref.poll()
    assert [r["ev"] for r in recs] == ["trade_end"]
    assert recs[0]["t"] == 499.0  # recorded 1 s (fake clock) before the poll at wall 500
    model.load_save()  # a new boot: the reader restarts its cursor
    model.add_gold(-1)
    assert [r["ev"] for r in ref.poll()] == ["gold"]
    rolls = referee.rolls(rpc, wall)
    model.roll(True, natural=20, dc=10)
    rows = [referee.to_roll(r) for r in rolls.poll()]
    assert rows[0]["crit"] == "nat20" and rows[0]["srcs"] == ["finished", "osi"]


def test_scenario_ops(game: Game, model: GameModel) -> None:
    with pytest.raises(GameError, match="seed must be an integer"):
        game.rpc("scn.seed", {"seed": -1})
    assert game.rpc("scn.seed", {"seed": 12345})["seed"] == 12345
    tav = model.party[0].uuid
    assert game.rpc("scn.rename", {"renames": [{"uuid": tav, "name": "Test Name"}]})[0]["original"] == "Tav"
    model.load_save()
    st = game.rpc("scn.status")
    assert st["reapplied"] == {"seed": {"seed": 12345, "base_seed": 12345, "streams": 4}, "renames": 1}
    assert game.rpc("scn.undo")["restored"][0]["original"] == "Tav"
    assert game.rpc("scn.status")["renames"] == []
    assert game.rpc("scn.karmic", {"set": True}) == {
        "mode": "WeightedRandom", "karmic": True, "component_mode": "WeightedRandom", "before": "Random"
    }  # fmt: skip
    assert game.rpc("scn.karmic")["karmic"] is True
    assert game.rpc("scn.difficulty")["difficulty"] == "Balanced"
    with pytest.raises(GameError, match="read-only"):
        game.rpc("scn.difficulty", {"set": "Explorer"})
    assert game.rpc("scn.rename_avatar", {"name": "Ana"})["before"] == "Tav"
    assert game.rpc("state.status")["party"][0]["name"] == "Ana"
    model.scenario_setup = False
    with pytest.raises(GameError, match="BG3AI_SCENARIO_SETUP=1"):
        game.rpc("scn.status")


# ---- fixtures ----


def line(op: str, args: dict, reply: dict, ctx: str = "server") -> dict:
    return {"v": 1, "t": 0, "ms": 1, "ctx": ctx, "calls": [{"op": op, "args": args}], "replies": [reply]}


@pytest.mark.unit
def test_store_replays_in_order_then_repeats() -> None:
    store = FixtureStore(
        [
            line("a.b", {"n": 1}, {"ok": True, "result": 1}),
            line("a.b", {"n": 1}, {"ok": True, "result": 2}),
            line("a.b", {"n": 2}, {"ok": True, "result": 9}),
        ]
    )
    assert [store.reply("server", "a.b", {"n": 1}) for _ in range(3)] == [
        {"ok": True, "result": 1},
        {"ok": True, "result": 2},
        {"ok": True, "result": 2},
    ]
    assert store.reply("server", "a.b", {"n": 3}) is None
    assert store.reply("client", "a.b", {"n": 1}) is None
    loose = FixtureStore([line("a.b", {"n": 1}, {"ok": True, "result": 1})], match="op")
    assert loose.reply("server", "a.b", {"n": 3}) == {"ok": True, "result": 1}


@pytest.mark.unit
def test_store_errors_raw_and_validation(tmp_path: Path) -> None:
    store = FixtureStore(
        [
            {"v": 1, "t": 0, "ms": 1, "ctx": "server", "calls": [{"op": "x", "args": {}}], "error": "boom"},
            {"v": 1, "t": 0, "ms": 1, "ctx": "client", "code": "return 1", "reply": "1"},
            {"v": 1, "t": 0, "ms": 1, "ctx": "server", "calls": [{"op": "y", "args": {}}], "reply": "nil"},
        ]
    )
    assert store.reply("server", "x", {}) == {"lua_error": "boom"}
    assert store.eval_raw("client", "return 1") == "1"
    assert store.reply("server", "y", {}) is None
    with pytest.raises(ValueError, match="version"):
        FixtureStore([{"v": 2}])
    with pytest.raises(ValueError, match="2 calls but 1 replies"):
        FixtureStore([{"v": 1, "calls": [{"op": "a"}, {"op": "b"}], "replies": [{}]}])
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{nope\n")
    with pytest.raises(ValueError, match=r"bad\.jsonl:1"):
        FixtureStore.load(bad)


def test_seed_fixtures_replay_through_ring_reader() -> None:
    """The live1 referee session, replayed: RingReader sees the same records in the same order as the real ring."""
    store = FixtureStore.load(FIXTURES / "ref_live1.jsonl", FIXTURES / "roll_live1.jsonl", FIXTURES / "scn_live1.jsonl")
    with FakeMod(store) as f:
        g = game_for(f)
        rpc = lambda op, args: g.rpc(op, args)  # noqa: E731
        ref = referee.referee(rpc, lambda: 1000.0)
        ref.sync()
        evs: list[str] = []
        for _ in range(100):  # one poll per recorded step; the last one is empty at seq 27
            got = ref.poll()
            evs += [r["ev"] for r in got]
            if ref.seq >= 27 and not got:
                break
        assert evs[:5] == ["trade_open", "item_added", "item_added", "gold", "trade_end"]
        assert {"pickpocket", "crime", "combat_started", "kill", "died", "combat_ended"} <= set(evs)
        snap = g.rpc("ref.snapshot")
        assert referee.selfcheck(snap)["fired"]["RequestTrade/4"] == 3
        rolls = referee.rolls(rpc, lambda: 0.0)
        rolls.sync()
        recs = rolls.poll()
        pattern = "".join("S" if r["success"] else "f" for r in recs)
        assert pattern == "SffSSfSfffffSfSSSfffSSff"  # live1 l3 run 0
        with pytest.raises(GameError, match="BG3AI_SCENARIO_SETUP=1"):
            g.rpc("scn.status")
        assert g.rpc("scn.status") == {"renames": {}}
        assert g.rpc("scn.seed", {"seed": 12345})["base_seed"] == 12345
        g.close()


def test_record_then_replay_gives_the_same_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BG3AI_RPC_RECORD on a session against the scripted game, then the file alone answers the same requests."""
    path = tmp_path / "session.jsonl"
    monkeypatch.setattr(game_mod, "RECORD", rpc_record.Recorder(path))
    model = GameModel(clock=FakeClock())
    calls = [("state.status", {}, "server"), ("state.inventory", {}, "server"), ("ui.state", {}, "client")]
    with FakeMod(model) as f:
        g = game_for(f)
        first = [g.rpc(op, args, ctx) for op, args, ctx in calls]
        first.append(g.multi([("state.combat", {}), ("state.journal", {})]))
        assert g.eval("return tostring(Ext.Utils.GetGameState())", "client") == "Running"
        g.close()
    monkeypatch.setattr(game_mod, "RECORD", None)
    lines = [json.loads(t) for t in path.read_text().splitlines()]
    assert len(lines) == 5 and lines[3]["multi"] is True and lines[4]["reply"] == "Running"
    with FakeMod(FixtureStore.load(path)) as f:
        g = game_for(f)
        again = [g.rpc(op, args, ctx) for op, args, ctx in calls]
        again.append(g.multi([("state.combat", {}), ("state.journal", {})]))
        assert again == first
        assert g.eval("return tostring(Ext.Utils.GetGameState())", "client") == "Running"
        g.close()
