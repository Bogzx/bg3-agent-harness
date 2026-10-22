# pyright: basic
# (basic, not strict: these tests drive the legacy bridge modules, which are untyped and on pyright's ignore list)
"""The live6 golden RPC fixtures (research/ovh/live6.md §1b: BG3AI_RPC_RECORD sessions against the real game,
2026-10-22): trade right after the dialogue opens it, the se run (ui.hotbar_slot after a load, set_reactions), two
load_game picks on the crowded Load page, and GameMenu's Resume. Each file is replayed eval by eval through the
bridge's own Game and the fake mod's FixtureStore, and must answer what the game answered.

Unlike live4 these hold the mod's busy answers ("bg3le: UI busy"): the bridge asks such a call again itself
(bg3ai/busywait.py), so a busy line and the recorded re-asks of the same call up to the first other answer replay
as one call that must end with that answer (a busy answer inside a multi isn't asked again: its caller gets None,
as trade's line 493 did). The game ran the mod of ovh-e2e, without c3a6c26 (GameMenu's Resume)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from bg3ai import busywait, saves
from bg3ai.game import Game, GameError
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.fixtures import FixtureStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpc"
NAMES = ["trade", "se", "load_s027", "load_s049", "gamemenu"]
# absolute paths, Steam ids, mail addresses, the Larian folder, X auth, Windows paths (names: tools/ci/scrub.py)
UNSCRUBBED = re.compile(r"/home|/data/|7656119|@|Larian Studios|\.steam|xauth|C:\\\\", re.IGNORECASE)

pytestmark = pytest.mark.integration


def lines(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / f"{name}_live6.jsonl"
    return [json.loads(t) for t in path.read_text(encoding="utf-8").splitlines() if t.strip()]


def results(name: str, op: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(args, reply) of every recorded call of `op`, in order."""
    return [
        (c["args"], r)
        for ln in lines(name)
        if "replies" in ln  # a Lua error line has none
        for c, r in zip(ln["calls"], ln["replies"], strict=True)
        if c["op"] == op
    ]


def settled(name: str, kind: str) -> list[dict[str, Any]]:
    """The settled job.poll views of jobs of this kind, in order."""
    return [
        r["result"]
        for _, r in results(name, "job.poll")
        if r.get("ok") and r["result"].get("kind") == kind and r["result"]["status"] != "running"
    ]


def busy(ln: dict[str, Any]) -> bool:
    """A single call the bridge asks again: its answer is a retryable busy one (busywait.retryable)."""
    if ln.get("multi") or len(ln.get("replies", [])) != 1 or ln["replies"][0].get("ok"):
        return False
    return busywait.retryable(ln["replies"][0]["error"], ln["calls"][0]["op"] in busywait.RETRY_OPS)


def chains(recorded: list[dict[str, Any]]) -> dict[int, list[int]]:
    """Per busy line that starts a wait (0-based), the lines the bridge's re-asks answered with, the last not busy."""
    out: dict[int, list[int]] = {}
    taken: set[int] = set()
    for i, ln in enumerate(recorded):
        if i in taken or not busy(ln):
            continue
        same = (ln["ctx"], ln["calls"])
        chain = [j for j in range(i + 1, len(recorded)) if (recorded[j]["ctx"], recorded[j].get("calls")) == same]
        end = next(k for k, j in enumerate(chain) if not busy(recorded[j]))
        out[i] = chain[: end + 1]
        taken.update(out[i])
    return out


def replay_one(g: Game, ln: dict[str, Any], n: int) -> None:
    ctx = ln["ctx"]
    if "code" in ln:
        if "error" in ln:
            with pytest.raises(GameError):
                g.eval(ln["code"], ctx)
        else:
            assert g.eval(ln["code"], ctx) == ln["reply"], n
    elif "error" in ln:  # a Lua error: the bridge's own words for it (NotReady for "not up yet")
        with pytest.raises(GameError):
            g.multi([(c["op"], c["args"]) for c in ln["calls"]], ctx) if ln.get("multi") else g.rpc(
                ln["calls"][0]["op"], ln["calls"][0]["args"], ctx
            )
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


def replay(name: str) -> tuple[int, float]:
    """Replays the file; (number of busy waits, seconds the bridge waited on its fake clock)."""
    recorded = lines(name)
    waits = chains(recorded)
    skip = {j for chain in waits.values() for j in chain}
    now = [0.0]
    with FakeMod(FixtureStore(recorded)) as fake:
        g = Game(port=fake.port, pool_max=2)
        g._ev_synced = True  # the recorded sync is replayed as the plain events.since it was
        g.job_push = False
        g._busy_clock = lambda: now[0]  # pyright: ignore[reportAttributeAccessIssue]  (a fake clock: no real waits)
        g._busy_sleep = lambda s: now.__setitem__(0, now[0] + s)  # pyright: ignore[reportAttributeAccessIssue]
        for i, ln in enumerate(recorded):
            if i not in skip:
                replay_one(g, recorded[waits[i][-1]] if i in waits else ln, i + 1)
        g.close()
        assert len(fake.calls) == sum(len(ln.get("calls", [])) for ln in recorded)  # nothing extra was asked
    return len(waits), now[0]


@pytest.mark.parametrize("name", NAMES)
def test_scrubbed(name: str) -> None:
    text = (FIXTURES / f"{name}_live6.jsonl").read_text(encoding="utf-8")
    assert UNSCRUBBED.findall(text) == []


@pytest.mark.parametrize(
    ("name", "waits"), [("trade", 4), ("se", 6), ("load_s027", 0), ("load_s049", 0), ("gamemenu", 0)]
)
def test_replays_what_the_game_answered(name: str, waits: int) -> None:
    n, waited = replay(name)
    assert n == waits
    assert (waited > 0) == (waits > 0)


def test_every_busy_answer_was_waited_out() -> None:
    for name in ("trade", "se"):
        recorded = lines(name)
        for chain in chains(recorded).values():
            assert recorded[chain[-1]]["replies"][0].get("ok"), (name, chain)


def test_trade_after_the_dialogue_confirms_every_offer() -> None:
    chosen = settled("trade", "ui.dialogue_choose")
    assert [v["result"]["screen"] for v in chosen] == ["trade", "trade"]
    trades = settled("trade", "ui.trade")
    assert len(trades) == 5 and all(v["status"] == "done" and v["result"]["confirmed"] for v in trades)
    assert [v["result"]["party_gold"] for v in trades] == [681, 839, 843, 687, 575]


def test_se_selects_dimension_door_after_a_busy_hotbar() -> None:
    picks = [r for a, r in results("se", "ui.hotbar_slot") if a["spell"] == "Teleportation_DimensionDoor"]
    assert any("UI busy" in r.get("error", "") for r in picks)
    assert picks[-1]["ok"] and picks[-1]["result"]["selected"] is True
    ((_, reactions),) = results("se", "ui.reactions")
    assert reactions["ok"]


@pytest.mark.parametrize(("name", "hint", "picked"), [("load_s027", 377, 381), ("load_s049", 271, 275)])
def test_load_finds_the_save_four_past_its_hint(name: str, hint: int, picked: int) -> None:
    # live6.md §1b: the Load page groups saves by playthrough, and 4 newer start saves sit at the top of the Claude
    # group, so saves.load_args' LeaderName count is 4 low; the mod's outward scan still finds the save
    ((args, _),) = results(name, "ui.menu")
    assert (args["action"], args["index"]) == ("load", hint)
    (job,) = settled(name, "ui.menu.load")
    assert job["status"] == "done" and job["result"]["picked"] == picked and job["result"]["loading"] is True
    assert job["result"]["name"] == args["name"]


@pytest.mark.parametrize("name", ["load_s027", "load_s049"])
def test_load_args_hint_is_the_recorded_pick(name: str, tmp_path: Path) -> None:
    # The Load page as live6 had it, around each recorded load: the save's playthrough (claude-run-2's
    # GameSessionID) holds picked-1 newer saves, picked-hint of them start saves under the leader Tav; another
    # playthrough's newer Tav and Claude saves are on the page too. Counted by playthrough the hint is the pick.
    ((args, _),) = results(name, "ui.menu")
    (job,) = settled(name, "ui.menu.load")
    hint, picked = args["index"], job["result"]["picked"]
    d = args["date"]
    when = datetime(d["y"], d["m"], d["d"], d["H"], d["M"]).timestamp()
    run2, metas = "df58a4c1-run2", {}
    metas[args["name"]] = ("Claude", when, run2)
    for i in range(1, picked):
        leader = "Tav" if i <= picked - hint else "Claude"
        metas[f"{leader}-{i}__newer"] = (leader, when + 86400 - i, run2)
    metas["Tav-0__S001-start"] = ("Tav", when + 99999, "0e4b554e-other")
    metas["Claude-0__S001-start"] = ("Claude", when + 99999, "41ce6db9-other")
    for folder in metas:
        (tmp_path / folder).mkdir()

    def meta(lsv: Path) -> tuple[str, float, str]:
        return metas[lsv.parent.name]

    def by_leader(lsv: Path) -> tuple[str, float, str]:  # the old grouping
        leader, saved, _ = metas[lsv.parent.name]
        return leader, saved, leader

    got = saves.load_args(tmp_path, args["name"], meta)
    assert (got["owner"], got["index"], got["date"]) == ("Claude", picked, d)
    assert saves.load_args(tmp_path, args["name"], by_leader)["index"] == hint + 1  # + the other Claude save


def test_gamemenu_resume_was_refused_before_c3a6c26() -> None:
    ((args, reply),) = results("gamemenu", "ui.press")
    assert args == {"button": "Resume", "widget": "GameMenu"}
    assert reply == {"ok": False, "error": "parameter is a string (CloseGameMenu); bg3le can only pass Noesis objects"}
