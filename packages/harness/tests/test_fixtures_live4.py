# pyright: basic
# (basic, not strict: these tests drive the legacy bridge modules, which are untyped and on pyright's ignore list)
"""The live4 golden RPC fixtures (research/ovh/live4.md §1: BG3AI_RPC_RECORD sessions against the real game,
2026-10-22): trade, dialogue, pickpocket, load and area. Each file is replayed eval by eval, in recorded order, through
the bridge's own Game (rpc / multi / raw eval) and the fake mod's FixtureStore, and must answer exactly what the game
answered; then each scenario's outcome is read off the replies. fight.jsonl is not here: it mixes two games."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from bg3ai.game import Game, GameError
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.fixtures import FixtureStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpc"
SCENARIOS = ["trade", "dialogue", "pickpocket", "load", "area"]
# live4's scrub check: absolute paths, the user, Steam ids, mail addresses, the Larian folder, X auth, Windows paths
UNSCRUBBED = re.compile(r"/home|/data/|bogdan|Truta|7656119|@|Larian Studios|\.steam|xauth|C:\\\\", re.IGNORECASE)

pytestmark = pytest.mark.integration


def lines(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / f"{name}_live4.jsonl"
    return [json.loads(t) for t in path.read_text(encoding="utf-8").splitlines() if t.strip()]


def results(name: str, op: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(args, reply) of every recorded call of `op`, in order."""
    return [
        (c["args"], r)
        for ln in lines(name)
        for c, r in zip(ln.get("calls", []), ln.get("replies", []), strict=True)
        if c["op"] == op
    ]


def settled(name: str, kind: str) -> list[dict[str, Any]]:
    """The settled job.poll views of jobs of this kind, in order."""
    return [
        r["result"]
        for _, r in results(name, "job.poll")
        if r.get("ok") and r["result"].get("kind") == kind and r["result"]["status"] != "running"
    ]


@pytest.mark.parametrize("name", SCENARIOS)
def test_scrubbed(name: str) -> None:
    text = (FIXTURES / f"{name}_live4.jsonl").read_text(encoding="utf-8")
    assert UNSCRUBBED.findall(text) == []


@pytest.mark.parametrize("name", SCENARIOS)
def test_replays_what_the_game_answered(name: str) -> None:
    recorded = lines(name)
    with FakeMod(FixtureStore(recorded)) as fake:
        g = Game(port=fake.port, pool_max=2)
        g._ev_synced = True  # the recorded sync is replayed as the plain events.since it was
        g.job_push = False
        for n, ln in enumerate(recorded, 1):
            ctx = ln["ctx"]
            if "code" in ln:
                assert g.eval(ln["code"], ctx) == ln["reply"], n
            elif ln.get("multi"):
                want = [r.get("result") if r.get("ok") else None for r in ln["replies"]]
                assert g.multi([(c["op"], c["args"]) for c in ln["calls"]], ctx) == want, n
            else:
                (call,), (rep,) = ln["calls"], ln["replies"]
                if rep.get("ok"):
                    assert g.rpc(call["op"], call["args"], ctx) == rep.get("result"), n
                else:
                    with pytest.raises(GameError, match=re.escape(rep["error"][:40])):
                        g.rpc(call["op"], call["args"], ctx)
        g.close()
        assert len(fake.calls) == sum(len(ln.get("calls", [])) for ln in recorded)  # nothing extra was asked


def test_trade_sold_for_30_gold() -> None:
    gold = [r["result"]["gold"] for _, r in results("trade", "state.status")]
    assert (gold[0], gold[-1]) == (1093, 1123)
    offer, confirm = settled("trade", "ui.trade")
    assert offer["result"]["can_confirm"] is True and confirm["result"]["confirmed"] is True


def test_dialogue_with_jaheira_ends() -> None:
    choose = settled("dialogue", "ui.dialogue_choose")
    assert choose[0]["result"]["dialogue"]["speaker"] == "Jaheira"
    assert choose[-1]["result"] == {"screen": "game", "lines": [], "ended": True}


def test_pickpocket_saw_gold_then_empty_pockets() -> None:
    views = settled("pickpocket", "ui.pickpocket")
    assert views[0]["result"]["items"][0]["name"] == "Gold" and views[0]["result"]["items"][0]["count"] == 15
    assert any(v["result"].get("items") == [] for v in views)
    assert any(v["status"] == "interrupted" for v in settled("pickpocket", "act.pickpocket"))


def test_load_loads_the_probe_save() -> None:
    loads = settled("load", "ui.menu.load")
    assert len(loads) == 2
    assert all(v["status"] == "done" and v["result"]["name"].endswith("__PROBE-l3base") for v in loads)


def test_area_fast_travel_arrives() -> None:
    (trip,) = settled("area", "ui.fast_travel")
    assert trip["result"]["arrived"] == "Moonrise Towers"
