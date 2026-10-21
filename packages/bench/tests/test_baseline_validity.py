"""The baseline rule in practice (D8; BENCHMARK-PLAN.md: a baseline above ~5% means the grader is broken): the
do-nothing and random baselines play every v2 scenario through the matrix runner, the real bridge and the fake mod
(bg3harness.testing), and every scenario's grader fails them.

The fake mod hosts each scenario's start as its needs_save describes it (party, area, gold, quests, who is near);
it has no combat, trade, travel or dialogue engine, so these runs prove that the grader and the start state don't
pass a run that makes no progress, not that the real game can't hand a random agent the goal. LIVE says, per
scenario, what only a live baseline series can show.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from bg3bench import matrix, run_artifacts, scenario
from bg3bench.baselines import donothing, random_agent
from bg3bench.baselines.agent import McpSession, Player

pytestmark = pytest.mark.e2e

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
T0 = 1_800_000_000.0
POLL_S = 300.0  # fake seconds between the progress poller's reads
RANDOM_STEPS = 25
RANDOM_PERIOD_S = 60.0
BASELINES = ({"slug": "baseline:donothing", "client": "baseline"}, {"slug": "baseline:random:7", "client": "baseline"})

# name -> the fake mod's start: (area, region level, party [(name, class, level)], gold, quests, nearby characters)
START: dict[str, dict] = {
    "nautiloid": {
        "area": "Nautiloid",
        "level": "TUT_Avernus_C",
        "party": [("Tav", "Fighter", 1)],
        "gold": 0,
        "quests": ["Escape the Nautiloid"],
        "nearby": ["Lae'zel", "Imp", "Mind Flayer"],
    },
    "act1_gold_1000": {
        "area": "Emerald Grove",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Lae'zel", "Fighter", 2), ("Gale", "Wizard", 2)],
        "gold": 150,
        "quests": ["Find a Cure"],
        "nearby": ["Arron", "Dammon", "Alfira"],
    },
    "choice_act1_karlach": {
        "area": "Risen Road",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 4), ("Wyll", "Warlock", 4), ("Shadowheart", "Cleric", 4)],
        "gold": 300,
        "quests": [],
        "nearby": ["Anders", "Paladin of Tyr"],
    },
    "choice_act1_minthara": {
        "area": "Shattered Sanctum",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 4), ("Wyll", "Warlock", 4), ("Gale", "Wizard", 4), ("Shadowheart", "Cleric", 4)],
        "gold": 300,
        "quests": [],
        "nearby": ["Minthara", "Drow Guard"],
    },
    "fight_act1_windmill": {
        "area": "Blighted Village",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 3), ("Wyll", "Warlock", 3), ("Gale", "Wizard", 3), ("Shadowheart", "Cleric", 3)],
        "gold": 200,
        "quests": [],
        "nearby": ["Goblin", "Worg"],
    },
    "fight_act2_balthazar": {
        "area": "Gauntlet of Shar",
        "level": "SCL_Main_A",
        "party": [("Tav", "Paladin", 6), ("Wyll", "Warlock", 6), ("Karlach", "Barbarian", 6), ("Shadowheart", "Cleric", 6)],
        "gold": 800,
        "quests": [],
        "nearby": ["Balthazar", "Flesh Golem"],
    },
    "fight_act2_house_of_healing": {
        "area": "House of Healing",
        "level": "SCL_Main_A",
        "party": [("Tav", "Paladin", 5), ("Gale", "Wizard", 5), ("Karlach", "Barbarian", 5), ("Shadowheart", "Cleric", 5)],
        "gold": 600,
        "quests": [],
        "nearby": ["Malus Thorm", "Nurse"],
    },
}  # fmt: skip

# What a live baseline series must show for each scenario (the fake mod can't: no combat, trade or travel engine).
LIVE: dict[str, str] = {
    "nautiloid": "the new-game start (character creation, intro) isn't hosted: the fake starts after it",
    "act1_gold_1000": "random pick_up/loot/interact gathering gold in the real Grove",
    "choice_act1_karlach": "a random fight with Anders' group won, or random dialogue recruiting Karlach",
    "choice_act1_minthara": "a random fight won in the Sanctum, or random travel to the Grove",
    "fight_act1_windmill": "random attacks winning the windmill fight",
    "fight_act2_balthazar": "random attacks winning the Balthazar fight",
    "fight_act2_house_of_healing": "random attacks winning the surgery fight",
}

# Scenarios the baselines pass today, and why: strict, so the fix shows as XPASS and this entry must go.
BROKEN: dict[str, str] = {
    "choice_act1_karlach": (
        "bg3harness.facts._member drops the mod's `avatar` flag (StateOps.lua), so State.companions() counts Tav: "
        "Tav + Wyll + Shadowheart = 3 meets companions >= 3 at the start"
    ),
}


class Clock:
    def __init__(self, on_tick: Callable[[float], None]):
        self.t, self.on_tick, self.next_poll = T0, on_tick, T0 + POLL_S

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        if self.t >= self.next_poll:
            self.on_tick(self.t)
            self.next_poll = self.t + POLL_S


def game_model(name: str):
    from bg3harness.testing.scripted import FakeClock, GameModel, Member

    st = START[name]
    model = GameModel(clock=FakeClock(), region=st["area"], level=st["level"], gold=st["gold"])
    model.party = [
        Member(f"00000000-0000-0000-0000-{i + 1:012d}", n, cls=c, level=lv, pos=(10.0 + i, 0.0, 10.0))
        for i, (n, c, lv) in enumerate(st["party"])
    ]
    model.active = model.party[0].uuid
    for q in st["quests"]:
        model.add_quest(q, f"{q}.")
    for n in st["nearby"]:
        model.add_nearby(n)
    return model


class Avatar:
    """state.status with the party's `avatar` flag, as the real mod sends it (StateOps.lua); the scripted model has
    none."""

    def __init__(self, model):
        self.model = model

    def reply(self, ctx, op, args):
        if op != "state.status":
            return None
        st = self.model.op_status(ctx, args)
        for m in st["party"]:
            m["avatar"] = m["uuid"] == self.model.party[0].uuid
        return {"ok": True, "result": st}

    def eval_raw(self, ctx, code):
        return None


def play_job(job: dict, sessions: Path) -> matrix.RunResult:
    """One baseline run of a matrix job: fake mod at the scenario's start, the real bridge, facts, the grade."""
    from bg3ai import progress
    from bg3ai.luadebug import LuaDebugClient
    from bg3harness import facts as facts_w
    from bg3harness.testing.fakemod import FakeMod
    from bg3harness.testing.mcp_bridge import bridge_session

    sc = scenario.load(SCENARIOS / f"{job['scenario']}.json")
    run_dir = sessions / job["id"]
    run_dir.mkdir(parents=True)
    writer = facts_w.FactsWriter(run_dir / "facts.jsonl", clock=lambda: T0)
    model = game_model(job["scenario"])
    deadline = T0 + sc["caps"]["run_hours"] * 3600
    out = io.StringIO()
    with FakeMod(Avatar(model), model) as fake:

        def lua_eval(code: str, ctx: str = "client") -> str:
            with LuaDebugClient(port=fake.port) as c:
                return c.evaluate(code, context=ctx, timeout=10)

        def poll(now: float) -> None:
            writer.observe(
                progress.read_game(lua_eval), facts_w.read_inventory(lua_eval, progress.combined_code), now=now
            )

        writer.mark("run_start", {"playthrough": job["id"]}, now=T0)
        poll(T0)
        clock = Clock(poll)

        async def play() -> None:
            async with bridge_session(fake.port, run_dir / "bridge") as client:
                s = McpSession(client)
                if job["model"] == "baseline:donothing":
                    p = Player(s, donothing.ALLOWED, out)
                    await donothing.play(s, p, deadline, now=clock.now, sleep=clock.sleep)
                else:
                    p = Player(s, random_agent.ALLOWED, out)
                    await random_agent.play(
                        s, p, deadline, seed=int(job["model"].rsplit(":", 1)[1]), now=clock.now, sleep=clock.sleep,
                        period_s=RANDOM_PERIOD_S, max_steps=RANDOM_STEPS,
                    )  # fmt: skip

        asyncio.run(play())
        poll(clock.t)
        writer.mark("run_end", "gave_up", now=clock.t)
    calls = [json.loads(line) for line in out.getvalue().splitlines() if '"type": "call"' in line]
    tools = {c["tool"] for c in calls}
    want = {"session_start"} if job["model"] == "baseline:donothing" else set(random_agent.ACTIONS)
    assert tools & want, f"{job['model']} called only {tools}"  # its calls reached the bridge
    series = {
        "series": job["id"],
        "client": "baseline",
        "scenario": {"name": sc["name"], "digest": scenario.digest(sc), "start_save_sha1": None, "spec": sc},
        "caps": sc["caps"],
        "pins": {"game_build": "fake"},
    }
    record = {
        "series": job["id"],
        "run": 1,
        "started": T0,
        "client": "baseline",
        "model": job["model"],
        "effort": None,
        "outcome": "gave_up",
        "cost_usd": 0.0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "wall_min": (clock.t - T0) / 60,
        "play_min": (clock.t - T0) / 60,
    }
    _, res = run_artifacts.write_run_artifacts(run_dir, series, record, "0.1.0", None, condition=job["condition"])
    assert res["grade"] is not None, "no grade: facts.jsonl missing"
    return matrix.RunResult(res["run_id"], True, res["passed"], 0.0, charged=True)


def test_every_scenario_has_a_fake_start_and_a_live_note():
    names = sorted(p.stem for p in SCENARIOS.glob("*.json"))
    assert sorted(START) == names and sorted(LIVE) == names
    assert set(BROKEN) <= set(names)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(strict=True, reason=BROKEN[n])) if n in BROKEN else n
        for n in sorted(START)
    ],
)
def test_baselines_fail_every_grader(tmp_path, name):
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"cap": 0.0, "spent": 0.0}))
    sessions = tmp_path / "sessions"
    clock = Clock(lambda t: None)
    mx = matrix.Matrix(
        tmp_path / "matrix", lambda job: play_job(job, sessions), budget, now=clock.now, sleep=lambda s: None
    )
    mx.create("baselines", matrix.plan(BASELINES, [name], ["rules"], 1))
    assert mx.run() == matrix.FINISHED
    ends = {jid: st["attempts"][-1]["result"] for jid, st in mx.state().items()}
    assert len(ends) == 2 and all(r["valid"] for r in ends.values()), ends
    passed = sorted(jid for jid, r in ends.items() if r["passed"])
    assert passed == [], f"a baseline passed {name}: {passed}"
