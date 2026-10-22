"""The stress probes (tools/stress/probes/*.py) offline: each probe's checks on the shapes the mod and the bridge's
tools give, and its run() against a scripted game (fake clock, no sockets). The fake-mod end-to-end runs are in
test_stress_probes_e2e.py."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import c21_flee
import c30_fight_events
import d11_reload_rolls
import d11_seed_rolls
import f6_rename
import i2_loot_open
import i3_chest_scan
import p7_level_offers
import probe_pickpocket as pickpocket
import probe_trade as trade
import probelib
import pytest
import s3_pickpocket_fail
import s4_steal
import t8_trade_buy
import t8_trade_open
import t8_trade_sell
import u2_esc_menu
import u12_menu_pause
import u13_rec_fps
from bg3ai import economy
from bg3ai.render import Handles

pytestmark = pytest.mark.unit

HOST = "HalfOrcs_Female_Player_14e68d14-c5a2-1e68-d51f-c2fe2defd545"
HOST_U = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
GOB = "b1e2d6a7-0fad-4485-ae2e-2ec78ce4e658"
MATTIS = "e1fda08b-45d4-41db-84b5-20e9931ba5af"


class GameError(Exception):
    pass


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeGame:
    """ops: op -> value or fn(args); evals: (substring of the Lua, answer or fn(code, ctx)), first match wins."""

    def __init__(self, ops: dict[str, Any] | None = None, evals: list[tuple[str, Any]] | None = None):
        self.ops = dict(ops or {})
        self.evals = list(evals or [])
        self.calls: list[tuple[str, Any]] = []
        self.ops.setdefault("state.status", {"party": [{"uuid": HOST_U, "name": "Claude"}]})
        self.ops.setdefault("ui.state", {"screen": "game"})

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
        self.calls.append((op, args or {}))
        if op not in self.ops:
            raise GameError(f"unknown op '{op}' in {context} context")
        h = self.ops[op]
        return h(args or {}) if callable(h) else h

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        try:
            return self.rpc(op, args, context)
        except GameError:
            return default

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        self.calls.append(("eval", code))
        for needle, ans in self.evals:
            if needle in code:
                return ans(code, context) if callable(ans) else ans
        raise AssertionError(f"unexpected Lua: {code[:120]}")


class FakeServer:
    def __init__(self, **tools: Callable[..., str]):
        self.H = Handles()
        self.called: list[tuple[str, dict]] = []
        for name, fn in tools.items():
            setattr(self, name, self._rec(name, fn))

    def _rec(self, name: str, fn: Callable[..., str]) -> Callable[..., str]:
        def call(*a: Any, **k: Any) -> str:
            self.called.append((name, {**dict(enumerate(a)), **k}))
            return fn(*a, **k)

        return call


class Ring:
    """The mod's referee / roll ring (ref.since, roll.since)."""

    def __init__(self) -> None:
        self.recs: list[dict] = []

    def add(self, **rec: Any) -> None:
        self.recs.append({"seq": len(self.recs) + 1, **rec})

    def since(self, a: dict) -> dict:
        out = [r for r in self.recs if r["seq"] > int(a.get("seq") or 0)][: int(a.get("limit") or 500)]
        return {"latest": len(self.recs), "events": out}


def probe(game: FakeGame, server: FakeServer | None = None, args: list[str] | None = None, tmp=None) -> probelib.Probe:
    clock = Clock()
    return probelib.Probe(game, server, tmp, args, clock=clock, sleep=clock.sleep, log=lambda s: None)


# ---- probelib ----


def test_line_is_one_stress_line():
    assert probelib.line("PASS", "a\nb   c") == "STRESS PASS a b c"


def test_wait_ready_waits_for_the_server_context_and_the_game_screen():
    n = {"status": 0}

    def status(a):
        n["status"] += 1
        if n["status"] < 3:
            raise GameError("the server context runs once a save is loaded")
        return {"party": []}

    g = FakeGame({"state.status": status, "ui.state": lambda a: {"screen": "game" if n["status"] > 3 else "loading"}})
    p = probe(g)
    assert p.wait_ready()
    assert n["status"] == 4 and p.clock() == 6.0
    g.ops["state.status"] = status
    n["status"] = 0
    g.ops["ui.state"] = {"screen": "main_menu"}
    assert not p.wait_ready(timeout=10.0)


def test_ref_after_reads_every_page():
    ring = Ring()
    for i in range(1203):
        ring.add(ev="gold", delta=i)
    p = probe(FakeGame({"ref.since": ring.since}))
    assert p.ref_mark() == 1203
    got = p.ref_after(2)
    assert len(got) == 1201 and got[0]["seq"] == 3 and got[-1]["seq"] == 1203


def test_roll_pattern_keeps_the_hosts_osiris_records_in_order():
    recs = [
        {"seq": 3, "srcs": ["osi", "requested"], "roller": HOST_U, "success": False},
        {"seq": 1, "srcs": ["osi"], "roller": HOST_U, "success": True},
        {"seq": 2, "srcs": ["osi"], "roller": GOB, "success": True},
        {"seq": 4, "srcs": ["requested"], "roller": HOST_U, "success": True},
    ]
    assert probelib.roll_pattern(recs, HOST) == "Sf"


def test_main_prints_the_verdict_and_an_exception_as_fail(capsys):
    g = FakeGame()
    assert probelib.main(lambda p: ("PASS", "fine"), probe=probe(g)) == 0
    assert capsys.readouterr().out.strip().endswith("STRESS PASS fine")

    def boom(p):
        raise KeyError("nope")

    probelib.main(boom, probe=probe(g))
    assert capsys.readouterr().out.strip().endswith("STRESS FAIL KeyError: 'nope'")
    g.ops["ui.state"] = {"screen": "main_menu"}
    probelib.main(lambda p: ("PASS", ""), probe=probe(g))
    assert "STRESS FAIL the save never came up" in capsys.readouterr().out


# ---- C21 flee ----


def combat(with_me: bool = True) -> dict:
    order = [
        {"uuid": GOB, "name": "Aelis", "relation": "hostile"},
        {"uuid": "k", "name": "Karlach", "relation": "party"},
    ]
    if with_me:
        order.insert(0, {"uuid": HOST_U, "name": "Claude", "relation": "party"})
    return {"active": True, "round": 2, "order": order}


def test_flee_teleports_away_then_flees_and_checks_the_turn_order():
    fled = {"v": False}
    ring = Ring()
    pos = {HOST_U: [0.0, 5.0, 0.0], GOB: [3.0, 5.0, 4.0]}
    g = FakeGame(
        {"state.combat": lambda a: combat(not fled["v"]), "ref.since": ring.since},
        [("Transform", lambda c, x: json.dumps(next(v for k, v in pos.items() if k in c))), ("Teleport", "ok")],
    )

    def flee(who=None):
        fled["v"] = True
        return f"fled: {who} left the fight and is at camp; still fighting: Karlach"

    s = FakeServer(flee=flee)
    v, ev = c21_flee.run(probe(g, s))
    assert (v, s.called) == ("PASS", [("flee", {"who": "Claude"})])
    assert "combat_ended not fired" in ev
    tp = next(c for op, c in g.calls if op == "eval" and "Teleport" in c)
    assert f'"{HOST_U}", -27.000000, 5.000000, -36.000000, ""' in tp  # 50 m from Aelis, on Claude's side


def test_flee_judge():
    assert c21_flee.judge("REFUSED: enemy too close: e2 Aelis at 20.1 m", False, [])[0] == "FAIL"
    assert c21_flee.judge("fled: Claude left", True, [])[0] == "FAIL"
    v, ev = c21_flee.judge("fled: Claude left; the party is out of the fight", False, [{"ev": "combat_ended"}])
    assert v == "PASS" and "combat_ended fired" in ev


def test_flee_needs_a_fight():
    assert c21_flee.run(probe(FakeGame({"state.combat": {"active": False}})))[0] == "FAIL"


# ---- C30 referee events in a fight ----


def test_fight_events_hit_the_nearest_hostile_on_the_save_and_read_the_kill():
    ring = Ring()
    near = {
        "characters": [
            {"uuid": "inn", "name": "Harper", "dist": 4.0, "relation": "neutral"},
            {"uuid": GOB, "name": "Sneem", "dist": 30.0, "relation": "hostile"},
        ]
    }

    def hit(code, ctx):
        if "300" in code:
            ring.add(ev="combat_started", combat="c1")
            ring.add(ev="kill", victim="Sneem", victim_uuid=GOB, killer="Claude", by_party=True)
        return "ok"

    g = FakeGame({"state.nearby": near, "ref.since": ring.since}, [("ApplyDamage", hit)])
    v, ev = c30_fight_events.run(probe(g))
    assert v == "PASS" and "combat_started x1, kill x1" in ev
    assert all(GOB in c for op, c in g.calls if op == "eval")


def test_fight_events_fail_names_what_the_ring_had():
    v, ev = c30_fight_events.judge({"uuid": GOB, "name": "Sneem"}, [{"ev": "crime", "type": "Assault"}])
    assert v == "FAIL" and "crime x1" in ev
    g = FakeGame({"state.nearby": {"characters": [{"uuid": "x", "name": "Harper", "dist": 3, "relation": "neutral"}]}})
    assert c30_fight_events.run(probe(g))[0] == "FAIL"


# ---- D11 rolls ----


def roll_game(patterns: list[str]) -> tuple[FakeGame, Ring]:
    ring = Ring()
    left = list(patterns)

    def roll(code, ctx):
        for ch in left.pop(0):
            ring.add(srcs=["osi"], roller=HOST_U, success=ch == "S")
        return "sent"

    g = FakeGame(
        {"roll.since": ring.since, "roll.snapshot": {"mode": "Random"}},
        [("GetHostCharacter() for", roll), ("GetHostCharacter", HOST), ("ServerRollStreams", "12345")],
    )
    return g, ring


def test_reload_rolls_loads_the_copy_between_batches(monkeypatch):
    monkeypatch.setenv("BG3AI_STRESS_SAVE", "Claude-1__SCN-stress_d11")
    pats = ["S" * 24, "f" * 24, "Sf" * 12]
    g, _ = roll_game(pats)
    s = FakeServer(load_game=lambda save: f"loaded {save}")
    v, ev = d11_reload_rolls.run(probe(g, s))
    assert v == "PASS" and "3 different patterns (mode Random)" in ev
    assert s.called == [("load_game", {"save": "Claude-1__SCN-stress_d11"})] * 2


def test_reload_rolls_judge():
    assert d11_reload_rolls.judge(["S" * 24, "S" * 24], None)[0] == "FAIL"
    assert d11_reload_rolls.judge(["S" * 24, "S" * 20], None)[0] == "FAIL"


def test_seed_rolls_repeat_for_one_seed():
    a, b = "SSfffSSSfSSfSfSfSffSffSS", "ffffSSSSffSSfSSSSSSffSSf"
    g, _ = roll_game([a, a, b, a])
    v, ev = d11_seed_rolls.run(probe(g))
    assert v == "PASS" and "A3 " + a in ev
    assert d11_seed_rolls.judge({"A1": a, "A2": b, "B": b, "A3": a})[0] == "FAIL"
    assert d11_seed_rolls.judge({"A1": a, "A2": a, "B": a, "A3": a})[0] == "FAIL"


# ---- T8 trade ----


def trade_res() -> dict:
    return {
        "party_gold": 40,
        "trader_gold": 300,
        "wares": [
            {"uuid": "w1", "name": "Shovel", "price": 9},
            {"uuid": "w2", "name": "Potion of Healing", "price": 45, "usable": True},
            {"uuid": "w3", "name": "Scimitar", "price": 30, "rarity": "Uncommon", "slot_type": "MeleeMainHand"},
            {"uuid": "w4", "name": "Bread", "price": 2, "supply": 3, "count": 2},
        ],
        "your_items": [
            {"uuid": "y1", "name": "Eldritch Tablet", "price": 1, "story": True},
            {"uuid": "y2", "name": "Bucket", "price": 3},
            {"uuid": "y3", "name": "Dagger", "price": 12},
        ],
    }


def text_of(res: dict, h: Handles) -> str:
    return economy.trade_text(res, lambda it: h.get(it["uuid"], it.get("name")), full=True)


def test_trade_entries_read_the_tools_own_text():
    h = Handles()
    text = text_of(trade_res(), h)
    wares = {w["name"]: w for w in trade.entries(text, "WARES")}
    assert set(wares) == {"Shovel", "Potion of Healing", "Scimitar", "Bread"}
    assert wares["Bread"]["count"] == 2 and wares["Bread"]["price"] == 2 and wares["Scimitar"]["price"] == 30
    assert wares["Shovel"]["id"] == h.get("w1")
    mine = {y["name"]: y for y in trade.entries(text, "YOUR ITEMS")}
    assert mine["Eldritch Tablet"]["flags"] == ["story"] and trade.party_gold(text) == 40


def trade_world() -> tuple[FakeGame, FakeServer, Ring]:
    ring = Ring()
    near = {"characters": [{"uuid": MATTIS, "name": "Mattis", "dist": 3.0, "relation": "neutral", "can_trade": True}]}
    g = FakeGame({"state.nearby": near, "ref.since": ring.since})
    s = FakeServer()
    table: dict[str, Any] = {}

    def trade_tool(npc="", offer="", request="", confirm=False, full=False, **k):
        if npc:
            assert npc == s.H.get(MATTIS)
            ring.add(ev="trade_open", npc="Mattis", npc_uuid=MATTIS)
            return text_of(trade_res(), s.H)
        if request:
            table["request"] = s.H.resolve(request)
        if offer:
            table["offer"] = s.H.resolve(offer)
        if confirm:
            if "request" in table:
                ring.add(ev="gold", gold=31, delta=-9)
                ring.add(ev="item_added", name="Shovel", count=1, bought=True)
                ring.add(ev="trade_end", npc="Mattis", sold=[], bought=[{"name": "Shovel"}])
            else:
                ring.add(ev="gold", gold=43, delta=3)
                ring.add(ev="trade_end", npc="Mattis", sold=[{"name": "Bucket", "count": 1}], bought=[])
        return "TRADE ..."

    s.trade = s._rec("trade", trade_tool)
    return g, s, table


def test_trade_open_buy_and_sell_are_standalone():
    g, s, _ = trade_world()
    v, ev = t8_trade_open.run(probe(g, s))
    assert v == "PASS" and "4 wares, 3 of your items" in ev
    g, s, table = trade_world()
    v, ev = t8_trade_buy.run(probe(g, s))
    assert v == "PASS" and "bought Bread (2g)" in ev and table["request"] == "w4"
    g, s, table = trade_world()
    v, ev = t8_trade_sell.run(probe(g, s))  # the story tablet is cheaper but never offered
    assert v == "PASS" and "sold Bucket (3g)" in ev and table["offer"] == "y2"


def test_trade_judges():
    assert t8_trade_open.judge(None, "no Mattis within 25 m", [])[0] == "FAIL"
    who = {"uuid": MATTIS, "name": "Mattis"}
    assert t8_trade_open.judge(who, text_of(trade_res(), Handles()), [])[0] == "FAIL"  # no trade_open
    assert t8_trade_buy.judge({"name": "Shovel", "price": 9}, [{"ev": "gold", "delta": -9}])[0] == "FAIL"
    assert t8_trade_sell.judge({"name": "Bucket", "price": 3}, [{"ev": "gold", "delta": 3}])[0] == "FAIL"


# ---- S3 / S4 pickpocket ----


def pockets(h: Handles) -> str:
    items = [
        {"uuid": "p1", "name": "Dagger", "price": 2, "chance": 54, "dc": 12},
        {"uuid": "p2", "name": "Gold", "count": 12, "chance": 81, "dc": 8},
    ]
    return economy.pockets_text({"items": items}, lambda it: h.get(it["uuid"], it.get("name")))


def pp_world(outcome: list[dict]) -> tuple[FakeGame, FakeServer]:
    ring = Ring()
    near = {"characters": [{"uuid": "alf", "name": "Alfira", "dist": 2.0, "relation": "neutral"}]}
    g = FakeGame({"state.nearby": near, "ref.since": ring.since}, [("INVISIBILITY", "1")])
    s = FakeServer()

    def pp(npc, who=None, take="", leave=False):
        if take:
            assert s.H.resolve(take) == "p2"  # the best odds
            for rec in outcome:
                ring.add(**rec)
            return "CAUGHT? ..."
        return "opened\n" + pockets(s.H)

    s.pickpocket = s._rec("pickpocket", pp)
    return g, s


def test_pickpocket_best_is_the_first_listed():
    assert pickpocket.best(pockets(Handles()))[1] == "Gold x12 81% DC8"
    assert pickpocket.best(economy.pockets_text({}, lambda it: "")) is None


def test_seen_pickpocket_fails_with_a_crime():
    g, s = pp_world([{"ev": "pickpocket", "ok": False, "npc": "Alfira"}, {"ev": "crime", "type": "PickPocketFailed"}])
    v, ev = s3_pickpocket_fail.run(probe(g, s))
    assert v == "PASS" and "PickPocketFailed" in ev
    g, s = pp_world([{"ev": "pickpocket", "ok": True, "npc": "Alfira", "item": "Gold"}])
    assert s3_pickpocket_fail.run(probe(g, s))[0] == "REVIEW"


def test_invisible_steal_succeeds():
    g, s = pp_world([{"ev": "pickpocket", "ok": True, "npc": "Alfira", "item": "Gold", "count": 12}])
    v, ev = s4_steal.run(probe(g, s, ["Alfira"]))
    assert v == "PASS" and "stole Gold x12 from Alfira" in ev
    assert any("AddBoosts(t" in c for op, c in g.calls if op == "eval")
    g, s = pp_world([{"ev": "pickpocket", "ok": False}, {"ev": "crime", "type": "PickPocketFailed"}])
    assert s4_steal.run(probe(g, s, ["Alfira"]))[0] == "FAIL"


def test_empty_pockets_leave_the_window():
    g = FakeGame(
        {"state.nearby": {"characters": [{"uuid": "c", "name": "Cerys", "dist": 2.0}]}, "ref.since": Ring().since}
    )
    s = FakeServer(pickpocket=lambda npc, who=None, take="", leave=False: "POCKETS: nothing to steal")
    v, ev = s3_pickpocket_fail.run(probe(g, s, ["Cerys"]))
    assert v == "FAIL" and "nothing to take" in ev and s.called[-1][1]["leave"] is True


# ---- I2 / I3 containers ----


def test_loot_open_flips_opened():
    state = {"opened": 0}

    def count(code, ctx):
        return json.dumps({"n": 1, "opened": state["opened"], "name": "Wooden Barrel"})

    def open_(code, ctx):
        state["opened"] = 1
        return "1"

    g = FakeGame(
        evals=[
            ("GetAllEntitiesWithComponent", json.dumps({"uuid": "bar", "dist": 3})),
            ("Osi.Open", open_),
            ("InventoryOwner", count),
        ]
    )
    v, ev = i2_loot_open.run(probe(g))
    assert (v, ev) == ("PASS", "Wooden Barrel: 1 items before, 1 after, opened")
    assert i2_loot_open.judge({"opened": 0}, {"opened": 0})[0] == "FAIL"
    assert i2_loot_open.run(probe(FakeGame(evals=[("GetAll", "{}")])))[0] == "FAIL"


def test_chest_scan_judges_on_stage_treasure_containers(tmp_path):
    rows = [
        {
            "name": "Wooden Chest",
            "d": 22.8,
            "inv": ["Exploration_Minor"],
            "n": 4,
            "opened": 0,
            "gen": True,
            "flags": "Activated",
        },
        {
            "name": "Looter's Stashpoint",
            "d": 30,
            "inv": ["Stash"],
            "n": 0,
            "opened": 0,
            "gen": False,
            "flags": "OffStage, Activated",
        },
        {"name": "Shelf", "d": 3, "inv": ["Empty"], "n": 0, "opened": 0, "gen": False, "flags": ""},
    ]
    g = FakeGame(evals=[("InventoryOwner", json.dumps(rows))])
    v, ev = i3_chest_scan.run(probe(g, args=["60"], tmp=tmp_path))
    assert (v, ev) == ("PASS", "3 containers, 2 with a treasure table, 1 on stage (0 opened), 1 generated")
    assert json.loads((tmp_path / "chest_scan.json").read_text()) == rows
    rows[1]["flags"] = "Activated"
    assert i3_chest_scan.judge(rows)[0] == "FAIL"
    assert i3_chest_scan.judge([])[0] == "FAIL"


# ---- F6 rename ----


def test_rename_reads_back_everywhere_and_restores():
    names = {"server": "Mattis", "client": "Mattis"}

    def get(code, ctx):
        return names[ctx]

    def set_(code, ctx):
        names[ctx] = json.loads(code.split(", ", 1)[1].rsplit("))", 1)[0])
        return "true"

    near = lambda a: {"characters": [{"uuid": MATTIS, "name": names["server"], "dist": 5.0}]}  # noqa: E731
    g = FakeGame(
        {"state.nearby": near},
        [("GetDisplayName", "h123"), ("UpdateTranslatedString", set_), ("GetTranslatedString", get)],
    )
    v, ev = f6_rename.run(probe(g))
    assert v == "PASS" and "nearby 'Test Name'" in ev and names == {"server": "Mattis", "client": "Mattis"}
    renamed = {"server": "Test Name", "client": "Mattis", "nearby": "Test Name"}
    assert f6_rename.judge("Mattis", renamed, {"server": "Mattis", "client": "Mattis"})[0] == "FAIL"
    renamed["client"] = "Test Name"
    assert f6_rename.judge("Mattis", renamed, {"server": "Test Name", "client": "Mattis"})[0] == "FAIL"


# ---- U2 / U12 menu ----


def test_esc_cycles(tmp_path):
    screen = {"s": "game"}

    def key(a):
        screen["s"] = "game_menu" if screen["s"] == "game" else "game"
        return {"done": True}

    g = FakeGame({"ui.key": key, "ui.state": lambda a: {"screen": screen["s"]}})
    v, ev = u2_esc_menu.run(probe(g, args=["5"], tmp=tmp_path))
    assert v == "PASS" and ev.startswith("5 cycles") and "stuck 0" in ev
    rows = json.loads((tmp_path / "esc_cycles.json").read_text())
    rows[2]["recovered"] = "game_menu"
    assert u2_esc_menu.judge(rows)[0] == "FAIL"


def menu_world(resume_error: str = "", paused: bool = False) -> FakeGame:
    clock = {"gt": 41.0, "x": 226.5, "bless": 118.8}
    screen = {"s": "game"}

    def sample(code, ctx):
        out = dict(clock)
        if not paused:
            clock.update(gt=clock["gt"] + 10.0, x=clock["x"] - 18.0, bless=clock["bless"] - 10.0)
        return json.dumps({"gt": out["gt"], "x": out["x"], "z": 382.0, "bless": out["bless"]})

    def press(a):
        assert a == {"button": "Resume", "widget": "GameMenu"}
        if resume_error:
            raise GameError(resume_error)
        screen["s"] = "game"
        return {"done": True, "via": "key:ESCAPE"}

    def key(a):
        screen["s"] = "game_menu"
        return {}

    party = {"party": [{"uuid": HOST_U, "name": "Claude"}, {"uuid": "gale", "name": "Gale"}]}
    return FakeGame(
        {"state.status": party, "ui.key": key, "ui.press": press, "ui.state": lambda a: {"screen": screen["s"]}},
        [("CharacterMoveToPosition", "ok"), ("GameTime", sample)],
    )


def test_menu_pause_world_runs_is_the_known_fail():
    v, ev = u12_menu_pause.run(probe(menu_world()))
    assert v == "FAIL" and "the world runs under the ESC menu" in ev and "GameTime +10.0 s" in ev
    assert u12_menu_pause.run(probe(menu_world(paused=True)))[0] == "PASS"
    v, ev = u12_menu_pause.run(probe(menu_world("parameter is a string (CloseGameMenu)")))
    assert v == "FAIL" and ev.startswith("Resume on the game menu: parameter is a string")


# ---- U13 recording ----


def test_rec_fps_stats_and_judge():
    s = u13_rec_fps.stats([18.0] * 50 + [60.0], 1.0)
    assert s["frames"] == 51 and s["fps"] == 53.1 and s["over_50ms"] == 1 and s["ft_p50_ms"] == 18.0
    ph = [u13_rec_fps.summary(n, [{"fps": f}]) for n, f in (("before", 55.1), ("recording", 54.5), ("after", 54.8))]
    v, ev = u13_rec_fps.judge(ph, 0)
    assert v == "PASS" and "recording costs 1.1 %" in ev
    assert u13_rec_fps.judge(ph, 1)[0] == "FAIL"
    ph[1] = u13_rec_fps.summary("recording", [{"fps": 40.0}])
    assert u13_rec_fps.judge(ph, 0)[0] == "FAIL"


def test_rec_fps_finds_the_game_window():
    tree = '  0x1400002 "Baldur\'s Gate 3 (1920x1080) - (Vulkan) - (6 + 6 WT)": ("bg3" "bg3")  1920x1080+0+0'
    assert u13_rec_fps.WINDOW_RE.search("xx\n" + tree).group(1) == "0x1400002"


# ---- P7 level-up offers ----


def member(levels: list[dict], **k: Any) -> dict:
    return {
        "uuid": "u",
        "name": "Wyll",
        "classes": [{"class": "Warlock", "sub": "Fiend", "level": 3}],
        "known": ["Eldritch Blast"],
        "known_passives": [],
        "known_feats": [],
        "repeatable_feats": ["Ability Improvement"],
        "levels": levels,
        **k,
    }


def lv4() -> dict:
    return {
        "level": 4,
        "feat": True,
        "skills": 0,
        "passives": [],
        "spells": [
            {
                "amount": 1,
                "list": "Warlock cantrips",
                "options": [{"name": "Eldritch Blast", "level": 0}, {"name": "Mage Hand", "level": 0}],
            },
            {"amount": 1, "list": "Warlock spells", "options": [{"name": "Hex", "level": 1}]},
        ],
    }


def test_level_offers_build_a_legal_line_per_level():
    plan = p7_level_offers.plan_for(lv4(), {"Eldritch Blast"})
    assert plan == {"cantrips": ["Mage Hand"], "spells": ["Hex"], "passives": [], "feat": "Ability Improvement"}
    v, ev = p7_level_offers.judge([member([lv4()])])
    assert v == "PASS" and "Wyll Warlock-Fiend 3" in ev


def test_level_offers_fail_on_short_choices_and_missing_levels():
    lv = lv4()
    lv["spells"][1]["amount"] = 2
    v, ev = p7_level_offers.judge([member([lv])])
    assert v == "FAIL" and "Warlock spells 1 of 2" in ev
    assert p7_level_offers.judge([member([])])[0] == "FAIL"
    assert p7_level_offers.judge([])[0] == "FAIL"


def test_level_offers_run_reads_party_and_camp(tmp_path):
    seen = {}

    def offers(code, ctx):
        seen["code"] = code
        return json.dumps([member([lv4()])])

    g = FakeGame({"state.status": {"party": [{"uuid": "u"}], "camp": [{"uuid": "v"}]}}, [("MEMBERS", offers)])
    v, _ = p7_level_offers.run(probe(g, args=["1"], tmp=tmp_path))
    assert v == "PASS" and 'local MEMBERS = {"u", "v"}\nlocal AHEAD = 1' in seen["code"]
    assert json.loads((tmp_path / "offers.json").read_text())[0]["name"] == "Wyll"
