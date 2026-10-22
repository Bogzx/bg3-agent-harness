# pyright: basic
"""The baseline rule in practice (D8; BENCHMARK-PLAN.md: a baseline above ~5% means the grader is broken): the
do-nothing and random baselines play every v2 scenario through the matrix runner, the real bridge and the fake mod
(bg3harness.testing), and every scenario's grader fails them. The oracle check (Phase E.2) is the other half: a
scripted solve (ORACLE: the fake world steps a solve would cause, one per progress read) through the same bridge and
facts path passes every scenario's grader, so a grader that fails everything is caught too.

The fake mod hosts each scenario's start as its needs_save describes it (party, area, gold, quests, who is near);
it has no combat, trade, travel or dialogue engine, so these runs prove that the grader and the start state don't
pass a run that makes no progress, not that the real game can't hand a random agent the goal. LIVE says, per
scenario, what only a live baseline series can show. Likewise the oracle proves the grader passes the end state a
solve leaves (read as the real game reports it), not that the scenario can be solved from its start save: that is
the live oracle run (D12).
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable, Sequence
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
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Gale", "Wizard", 2)],
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
        "party": [("Tav", "Paladin", 3), ("Wyll", "Warlock", 3), ("Gale", "Wizard", 3), ("Shadowheart", "Cleric", 3)],
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
    "fight_act1_grove_gate": {
        "area": "Emerald Grove Environs",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Gale", "Wizard", 1)],
        "gold": 88,
        "quests": [],
        "nearby": ["Goblin", "Zevlor"],
    },
    "fight_act1_dror_ragzlin": {
        "area": "Shattered Sanctum",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 3), ("Wyll", "Warlock", 3), ("Gale", "Wizard", 3), ("Shadowheart", "Cleric", 3)],
        "gold": 156,
        "quests": ["Save the Refugees"],
        "nearby": ["Dror Ragzlin", "Goblin"],
    },
    "fight_act1_gith_patrol": {
        "area": "Mountain Pass",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 4), ("Karlach", "Barbarian", 4), ("Gale", "Wizard", 4), ("Shadowheart", "Cleric", 4)],
        "gold": 178,
        "quests": ["The Githyanki Warrior"],
        "nearby": ["Githyanki Warrior"],
    },
    "fight_act2_moonrise_assault": {
        "area": "Shadow-Cursed Lands",
        "level": "SCL_Main_A",
        "party": [("Tav", "Paladin", 6), ("Wyll", "Warlock", 6), ("Karlach", "Barbarian", 6), ("Shadowheart", "Cleric", 6)],
        "gold": 152,
        "quests": ["Infiltrate Moonrise Towers"],
        "nearby": ["Jaheira", "Harper"],
    },
    "fight_act2_ketheric": {
        "area": "Mind Flayer Colony",
        "level": "SCL_Main_A",
        "party": [("Tav", "Paladin", 7), ("Wyll", "Warlock", 7), ("Karlach", "Barbarian", 7), ("Shadowheart", "Cleric", 7)],
        "gold": 379,
        "quests": ["Defeat Ketheric Thorm"],
        "nearby": ["Ketheric Thorm", "Dame Aylin"],
    },
    "fight_act3_sarevok": {
        "area": "Murder Tribunal",
        "level": "BGO_Main_A",
        "party": [("Tav", "Paladin", 8), ("Wyll", "Warlock", 8), ("Karlach", "Barbarian", 8), ("Shadowheart", "Cleric", 8)],
        "gold": 410,
        "quests": ["Impress the Murder Tribunal"],
        "nearby": ["Sarevok Anchev"],
    },
    "fight_act3_cazador": {
        "area": "Cazador's Dungeon",
        "level": "BGO_Main_A",
        "party": [("Tav", "Paladin", 11), ("Gale", "Wizard", 11), ("Karlach", "Barbarian", 11), ("Shadowheart", "Cleric", 11)],
        "gold": 671,
        "quests": ["Investigate Cazador's Palace"],
        "nearby": ["Cazador Szarr", "Predator Werewolf"],
    },
    "act1_level_3": {
        "area": "Overgrown Ruins",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Gale", "Wizard", 1)],
        "gold": 88,
        "quests": [],
        "nearby": ["Bandit"],
    },
    "act1_recruit_3": {
        "area": "Roadside Cliffs",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2)],
        "gold": 88,
        "quests": [],
        "nearby": ["Gale"],
    },
    "act1_goblin_camp_no_fight": {
        "area": "Blighted Village",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 3), ("Wyll", "Warlock", 3), ("Gale", "Wizard", 3), ("Shadowheart", "Cleric", 3)],
        "gold": 156,
        "quests": [],
        "nearby": ["Olak", "Goblin"],
    },
    "act1_get_item": {
        "area": "Emerald Grove",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Gale", "Wizard", 1)],
        "gold": 133,
        "quests": ["Find a Cure"],
        "nearby": ["Druid", "Idol of Silvanus"],
    },
    "choice_act1_arabella": {
        "area": "Emerald Grove",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2), ("Gale", "Wizard", 1)],
        "gold": 133,
        "quests": ["Save Arabella"],
        "nearby": ["Kagha", "Arabella"],
    },
    "choice_act2_nightsong": {
        "area": "Nightsong's Prison",
        "level": "SCL_Main_A",
        "party": [("Tav", "Paladin", 6), ("Wyll", "Warlock", 6), ("Karlach", "Barbarian", 6), ("Shadowheart", "Cleric", 6)],
        "gold": 60,
        "quests": ["The Chosen of Shar", "Daughter of Darkness"],
        "nearby": ["The Nightsong"],
    },
    "choice_act3_wyll_pact": {
        "area": "Camp",
        "level": "BGO_Main_A",
        "party": [("Tav", "Paladin", 10), ("Karlach", "Barbarian", 10), ("Wyll", "Warlock", 10), ("Shadowheart", "Cleric", 10)],
        "gold": 1561,
        "quests": ["Rescue the Grand Duke"],
        "nearby": ["Mizora"],
    },
    "crash_site_grove": {
        "area": "Ravaged Beach",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 2), ("Shadowheart", "Cleric", 2)],
        "gold": 33,
        "quests": ["Find a Cure"],
        "nearby": ["Intellect Devourer"],
    },
    "act2_last_light": {
        "area": "Rosymorn Monastery Trail",
        "level": "WLD_Main_A",
        "party": [("Tav", "Paladin", 5), ("Karlach", "Barbarian", 5), ("Gale", "Wizard", 5), ("Shadowheart", "Cleric", 5)],
        "gold": 738,
        "quests": [],
        "nearby": ["Githyanki"],
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
    "fight_act1_grove_gate": "random attacks winning the gate raid (the tiefling defenders fight too)",
    "fight_act1_dror_ragzlin": "random attacks winning the throne-room fight",
    "fight_act1_gith_patrol": "random attacks winning the patrol fight",
    "fight_act2_moonrise_assault": "random attacks winning the main-floor assault (the Harpers fight too)",
    "fight_act2_ketheric": "random attacks winning the Ketheric fight and the journal closing its quest",
    "fight_act3_sarevok": "random attacks winning the tribunal fight",
    "fight_act3_cazador": "random attacks winning the Cazador fight",
    "act1_level_3": "random fights and the level-up screen levelling everyone to 3",
    "act1_recruit_3": "random dialogue recruiting two companions (Gale is next to the start)",
    "act1_goblin_camp_no_fight": "random travel or talk getting into the Sanctum with no combat",
    "act1_get_item": "random pick_up taking the idol off its altar",
    "choice_act1_arabella": "a random fight won in the druids' chamber, or random dialogue closing the quest",
    "choice_act2_nightsong": "a random fight won, or random dialogue closing the Nightsong choice",
    "choice_act3_wyll_pact": "a random fight won, or random dialogue closing the Grand Duke's quest",
    "crash_site_grove": "random travel reaching the Emerald Grove",
    "act2_last_light": "random travel crossing the curse to Last Light",
}

# Scenarios the baselines pass today, and why: strict, so the fix shows as XPASS and this entry must go.
BROKEN: dict[str, str] = {
    "choice_act1_karlach": (
        "bg3harness.facts._member drops the mod's `avatar` flag (StateOps.lua), so State.companions() counts Tav: "
        "Tav + Wyll + Shadowheart = 3 meets companions >= 3 at the start"
    ),
}


def fight(*enemies: str) -> list[Callable]:
    """A fight the party wins: it starts at one read and every enemy is dead and the combat over at the next."""

    def end(m) -> None:
        for e in enemies:
            m.kill(e)
        m.end_fight()

    return [lambda m: m.start_fight(list(enemies)), end]


def recruit(*names: str) -> Callable:
    """Companions join the party (each at the party's level)."""

    def step(m) -> None:
        from bg3harness.testing.scripted import Member

        for n in names:
            i = len(m.party)
            m.party.append(
                Member(f"00000000-0000-0000-0000-{i + 1:012d}", n, level=m.party[0].level, pos=(10.0 + i, 0.0, 10.0))
            )

    return step


def finish_quest(title: str) -> Callable:
    """The journal shows the quest done (added first if the start doesn't have it)."""

    def step(m) -> None:
        if not any(q["title"] == title for q in m.journal):
            m.add_quest(title, f"{title}.")
        next(q for q in m.journal if q["title"] == title)["done"] = True
        m.push("quest_done", {"title": title})

    return step


def area(region: str, level: str = "WLD_Main_A") -> Callable:
    return lambda m: m.change_area(region, level)


def levels(n: int) -> Callable:
    def step(m) -> None:
        for p in m.party:
            p.level = n

    return step


# name -> the scripted solve: the fake world's steps, one per progress read, ending in the state the goal asks for.
ORACLE: dict[str, list[Callable]] = {
    "nautiloid": [finish_quest("Escape the Nautiloid")],
    "act1_gold_1000": [lambda m: m.add_gold(1000 - m.gold)],
    "act1_get_item": [lambda m: m.add_item("Idol of Silvanus")],
    "act1_level_3": [*fight("Bandit"), levels(3)],
    "act1_recruit_3": [recruit("Gale"), recruit("Karlach")],
    "act1_goblin_camp_no_fight": [area("Shattered Sanctum")],
    "act2_last_light": [area("Last Light Inn", "SCL_Main_A")],
    "crash_site_grove": [area("Emerald Grove")],
    "choice_act1_karlach": [recruit("Karlach")],
    "choice_act1_minthara": [area("Emerald Grove")],
    "choice_act1_arabella": [finish_quest("Save Arabella")],
    "choice_act2_nightsong": [finish_quest("The Chosen of Shar")],
    "choice_act3_wyll_pact": [finish_quest("Rescue the Grand Duke")],
    "fight_act1_windmill": fight("Goblin", "Worg"),
    "fight_act1_grove_gate": fight("Goblin"),
    "fight_act1_dror_ragzlin": fight("Dror Ragzlin", "Goblin"),
    "fight_act1_gith_patrol": fight("Githyanki Warrior"),
    "fight_act2_balthazar": fight("Balthazar", "Flesh Golem"),
    "fight_act2_house_of_healing": fight("Malus Thorm", "Nurse"),
    "fight_act2_moonrise_assault": fight("Moonrise Guard"),
    "fight_act2_ketheric": [*fight("Ketheric Thorm"), finish_quest("Defeat Ketheric Thorm")],
    "fight_act3_sarevok": fight("Sarevok Anchev"),
    "fight_act3_cazador": fight("Cazador Szarr", "Predator Werewolf"),
}

# Ends one step short of the goal, which the grader must fail: (name, the steps).
NEAR_MISS: dict[str, list[Callable]] = {
    "act1_recruit_3": [recruit("Gale")],  # one companion recruited, the goal wants two more
    "fight_act1_windmill": fight("Goblin", "Worg")[:1],  # the fight started, never won
    "act1_gold_1000": [lambda m: m.add_gold(999 - m.gold)],
    "fight_act2_ketheric": fight("Ketheric Thorm"),  # the fight won, the quest still open
}

# Near misses the grader passes today, and why: strict, as BROKEN.
NEAR_MISS_BROKEN: dict[str, str] = {
    "act1_recruit_3": "BROKEN['choice_act1_karlach']'s cause: Tav + Shadowheart + Gale = 3 meets companions >= 3 "
    "after one recruit of the two the goal wants",
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


def play_job(job: dict, sessions: Path, solve: Sequence[Callable] = ()) -> matrix.RunResult:
    """One baseline run of a matrix job: fake mod at the scenario's start, the real bridge, facts, the grade. `solve`:
    world steps applied one per progress read (ORACLE), the do-nothing agent playing meanwhile."""
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

        steps = list(solve)

        def tick(now: float) -> None:
            if steps:
                steps.pop(0)(model)
            poll(now)

        writer.mark("run_start", {"playthrough": job["id"]}, now=T0)
        poll(T0)
        clock = Clock(tick)

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
        assert not steps, f"the run ended before the solve's last {len(steps)} steps"
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
    assert sorted(START) == names and sorted(LIVE) == names and sorted(ORACLE) == names
    assert set(NEAR_MISS) <= set(names)
    assert set(BROKEN) <= set(names)


def test_each_fake_start_hosts_the_scenarios_party():
    for name, st in START.items():
        note = json.loads((SCENARIOS / f"{name}.json").read_text())["start"].get("note", {})
        if "party" in note:
            assert [m.split(" (")[0] for m in note["party"]] == [n for n, _, _ in st["party"]], name


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


def solve_job(tmp_path: Path, name: str, solve: Sequence[Callable]) -> dict:
    """The scripted solve's run (the do-nothing agent plays while the world takes the steps); its result.json."""
    job = {"id": f"{name}-oracle", "scenario": name, "model": "baseline:donothing", "condition": "rules"}
    play_job(job, tmp_path / "sessions", solve)
    return json.loads((tmp_path / "sessions" / job["id"] / run_artifacts.RESULT).read_text())


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_the_scripted_solve_passes_every_grader(tmp_path, name):
    res = solve_job(tmp_path, name, ORACLE[name])
    assert res["passed"] is True, res["grade"]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(strict=True, reason=NEAR_MISS_BROKEN[n])) if n in NEAR_MISS_BROKEN else n
        for n in sorted(NEAR_MISS)
    ],
)  # fmt: skip
def test_a_solve_one_step_short_fails(tmp_path, name):
    res = solve_job(tmp_path, name, NEAR_MISS[name])
    assert res["passed"] is False, res["grade"]
