import json
from pathlib import Path

import pytest

from bg3bench import facts, graders

ROOT = Path(__file__).resolve().parents[3]
FACTS_SCHEMA = ROOT / "packages" / "harness" / "schemas" / "facts.v1.schema.json"
TL = Path(__file__).resolve().parent / "fixtures" / "timelines"

TAV = {"name": "Tav", "level": 2, "dead": False, "downed": False, "avatar": True}


def member(name, level=2, **kw):
    return {"name": name, "level": level, "dead": False, "downed": False, **kw}


def snap(g, gold=0, party=None, combat=False, region="Wilderness", areas=(), inv=(), quests=None, area=None):
    return {
        "v": 1,
        "kind": "snapshot",
        "t": 1000 + g,
        "game_t": g,
        "gold": gold,
        "party": [TAV] if party is None else party,
        "region": region,
        "area": area,
        "areas": list(areas),
        "combat": combat,
        "inv": list(inv),
        "quests": quests or {},
    }


def ev(g, kind, **kw):
    return {"v": 1, "kind": "event", "t": 1000 + g, "game_t": g, "ev": kind, **kw}


def mark(g, name):
    return {"v": 1, "kind": "mark", "t": 1000 + g, "game_t": g, "mark": name}


def run(*records):
    return [mark(0, "run_start"), *records]


def grade(goal, timeline, **kw):
    return graders.grade(goal, timeline, allow_passive=True, **kw)


def test_gold_passes_at_first_instant_and_reports_time():
    tl = run(snap(10, gold=50), ev(20, "gold", gold=150, delta=100), snap(30, gold=90))
    out = grade({"gold": {"gte": 100}}, tl)
    assert out["passed"] and out["t_pass_s"] == 20
    out = grade({"state": {"gold": {"gte": 100}}}, tl)
    assert out["passed"] and out["t_pass_s"] == 20
    assert not grade({"gold": {"gte": 200}}, tl)["passed"]
    assert grade({"gold": {"gte": 200}}, tl)["t_pass_s"] is None


def test_item_by_name_and_template():
    gem = {"name": "Umbral Gem", "tpl": "tpl-gem", "count": 1}
    tl = run(
        snap(10), ev(20, "item_added", name="Arrow", tpl="tpl-arrow", count=5), snap(30, inv=[gem]), snap(40, inv=[])
    )
    assert grade({"item": {"name": "umbral gem"}}, tl)["t_pass_s"] == 30
    assert grade({"item": {"tpl": "tpl-gem"}}, tl)["t_pass_s"] == 30
    assert grade({"item": {"name": "Arrow", "gte": 5}}, tl)["t_pass_s"] == 20
    assert not grade({"item": {"name": "Arrow", "gte": 6}}, tl)["passed"]
    # Held at the same instant as being in the camp: the gem was sold before the party reached it.
    tl2 = run(snap(10, inv=[gem]), snap(20, inv=[], area="Campsite", areas=["Campsite"]))
    assert grade({"all": [{"item": {"name": "Umbral Gem"}}, {"area": "camp"}]}, tl2)["passed"]
    assert not grade({"ever": {"all": [{"item": {"name": "Umbral Gem"}}, {"area": "camp"}]}}, tl2)["passed"]


def test_party_level_is_the_minimum_over_real_members():
    wolf = member("Wolf", level=1, summon=True)
    tl = run(
        snap(10, party=[TAV, member("Gale", 1), wolf]),
        snap(20, party=[TAV, member("Gale", 2), wolf]),
        ev(30, "level_up", name="Tav", level=3),
    )
    assert grade({"party_level": {"gte": 2}}, tl)["t_pass_s"] == 20  # the level-1 summon doesn't count
    assert grade({"party_level": {"gte": 3, "agg": "max"}}, tl)["t_pass_s"] == 30
    assert not grade({"party_level": {"gte": 3}}, tl)["passed"]
    assert not grade({"party_level": {"lte": 5}}, run(snap(10, party=[])))["passed"]


def test_companions_do_not_count_the_avatar_or_summons():
    party = [TAV, member("Lae'zel"), member("Shadowheart"), member("Wolf", summon=True)]
    tl = run(snap(10, party=party))
    assert grade({"companions": {"gte": 2}}, tl)["passed"]
    assert not grade({"companions": {"gte": 3}}, tl)["passed"]
    legacy = run(ev(5, "joined", name="Lae'zel"), ev(9, "joined", name="Shadowheart"), ev(12, "joined", name="Gale"))
    assert grade({"companions": {"gte": 3}}, legacy)["t_pass_s"] == 12


def test_the_settings_marks_avatar_is_no_companion():
    # live snapshots carry no avatar flag (the mod's state.status): the settings mark names the avatar
    party = [member("Wyll"), member("Shadowheart"), member("Tav")]
    named = {"v": 1, "kind": "mark", "t": 1000.2, "game_t": 0.2, "mark": "settings", "detail": {"avatar_name": "Tav"}}
    assert grade({"companions": {"gte": 3}}, run(snap(10, party=party)))["passed"]  # the old count
    assert not grade({"companions": {"gte": 3}}, run(named, snap(10, party=party)))["passed"]
    assert grade({"companions": {"gte": 3}}, run(named, snap(10, party=[*party, member("Karlach")])))["t_pass_s"] == 10


def test_a_merchant_dialogue_does_not_end_the_karlach_choice():
    # launch-v1-haiku choice_act1_karlach (2026-10-30): reached at game_t 22.6 after talking to Cyrel only, since
    # companions >= 3 counted Tav (no avatar flag) beside Wyll and Shadowheart
    goal = json.loads((ROOT / "packages" / "bench" / "scenarios" / "choice_act1_karlach.json").read_text())["goal"]
    tl = facts.load(TL / "karlach_haiku_merchant.facts.jsonl")
    assert [r["scene"][:5] for r in tl if r.get("ev") == "choice"] == ["Cyrel"] * 3
    assert not grade(goal, tl)["passed"] and graders.verdict(goal, tl) is None


def test_combat_outcome_fixture_one_won_one_wipe():
    tl = facts.load(TL / "fights.facts.jsonl")
    assert grade({"combats_won": {"gte": 1}}, tl)["t_pass_s"] == 30
    assert not grade({"combats_won": {"gte": 2}}, tl)["passed"]
    assert grade({"combats_started": {"gte": 2}}, tl)["t_pass_s"] == 40
    assert grade({"wipe": True}, tl)["t_pass_s"] == 46
    assert not grade({"party_dead": {"gte": 1}}, tl)["passed"]  # downed is not dead; the goblin isn't party


def test_wipe_versus_a_single_death():
    two = [TAV, member("Gale")]
    one_dead = run(snap(10, party=two, combat=True), ev(20, "died", name="Gale", party=True), ev(30, "combat_ended"))
    assert grade({"party_dead": {"gte": 1}}, one_dead)["t_pass_s"] == 20
    assert not grade({"wipe": True}, one_dead)["passed"]
    assert grade({"combats_won": {"gte": 1}}, one_dead)["passed"]
    wiped = run(
        snap(10, party=two, combat=True),
        ev(20, "died", name="Gale", party=True),
        ev(25, "downed", name="Tav", party=True),
        ev(26, "revived", name="Tav", party=True),
        ev(30, "combat_ended"),
    )
    assert grade({"wipe": True}, wiped)["t_pass_s"] == 25
    assert not grade({"combats_won": {"gte": 1}}, wiped)["passed"]  # a wipe during the fight, even if revived


def prologue(downed: bool = True) -> list[dict]:
    """bench live S2 Sonnet nautiloid r1: Tav and Lae'zel downed in the helm fight, "Us" (the intellect devourer, a
    real party member in the prologue) standing at 20/21, and the game at its Game Over screen."""
    party = [
        member("Lae'zel", 1, downed=downed, hp=0 if downed else 12, max_hp=12),
        member("Us", 1, hp=20, max_hp=21),
        member("Tav", 1, downed=downed, hp=0 if downed else 9, max_hp=12, avatar=True),
    ]
    return [snap(800, party=party, combat=True, region="Nautiloid")]


def test_the_game_over_screen_is_a_wipe_with_a_member_standing():
    """bench live S2: the prologue's Game Over with "Us" alive was no wipe for the bench, so the run was resumed on
    the Game Over screen, its agent loaded a save and the run was voided (foreign_save) and rerun."""
    before = run(*prologue())
    st = facts.State()
    for r in before:
        st.apply(r)
    assert not st.wiped()  # Us stands: the party isn't all down
    over = [*before, ev(805, "game_over")]
    st.apply(over[-1])
    assert st.wiped()
    assert graders.verdict(None, before) is None and graders.verdict(None, over) == "wiped"
    assert graders.verdict({"quest_done": "Escape the Nautiloid"}, over) == "wiped"
    assert graders.grade({"wipe": True}, over, allow_passive=True)["t_pass_s"] == 805
    assert facts.party_losses(over)["wipes"] == 1
    # the screen comes back on a later read: one wipe; a load starts a new game state, its Game Over is another
    again = [*over, ev(806, "game_over"), mark(820, "load"), *prologue(downed=False)]
    st = facts.State()
    for r in again:
        st.apply(r)
    assert not st.wiped()
    assert facts.party_losses([*again, ev(900, "game_over")])["wipes"] == 2
    # a full party wipe and its Game Over screen are one wipe
    two = [TAV, member("Gale")]
    full = run(
        snap(10, party=two, combat=True),
        ev(20, "died", name="Gale", party=True),
        ev(25, "downed", name="Tav", party=True),
    )
    assert facts.party_losses([*full, ev(27, "game_over")])["wipes"] == 1


def test_a_fight_goal_names_its_enemies():
    # diverse-2026-10-25 x fight_act1_windmill: Haiku won a 9-s combat against a sleeping Scrawny Bugbear near the
    # windmill and passed {"combats_won": {"gte": 1}}; Opus killed the goblins (one Goblin Tracker out of combat first).
    goal = json.loads((ROOT / "packages/bench/scenarios/fight_act1_windmill.json").read_text())["goal"]
    haiku = facts.load(TL / "windmill_haiku_bugbear.facts.jsonl")
    opus = facts.load(TL / "windmill_opus_goblins.facts.jsonl")
    assert grade({"combats_won": {"gte": 1}}, haiku)["passed"]
    assert not grade(goal, haiku)["passed"]
    assert grade(goal, opus)["t_pass_s"] == 249.9  # the combat_ended after Goblin Brawler's kill
    # Balthazar died, then his guards wiped the party: the fight was not won.
    wiped = facts.load(TL / "balthazar_haiku_wipe.facts.jsonl")
    assert not grade({"combats_won": {"gte": 1, "enemy": "^balthazar$"}}, wiped)["passed"]


def test_a_won_combat_counts_the_enemies_who_died_in_it():
    party = [TAV, member("Gale")]
    boss = {"combats_won": {"gte": 1, "enemy": "^boss$"}}
    tl = run(
        snap(10, party=party),
        ev(15, "died", name="Boss", party=False),  # before the combat: not this fight
        ev(20, "combat_started"),
        ev(25, "kill", victim="Gale", killer="Boss"),
        ev(30, "kill", victim="Rat", killer="Tav"),
        ev(31, "combat_ended"),
        ev(32, "died", name="Boss", party=False),  # after it ended
    )
    assert grade({"combats_won": {"gte": 1, "enemy": "rat"}}, tl)["t_pass_s"] == 31
    assert not grade(boss, tl)["passed"]
    assert not grade({"combats_won": {"gte": 1, "enemy": "gale"}}, tl)["passed"]  # a party member isn't an enemy
    rat = {"combats_won": {"gte": 1, "enemy": "^rat$"}}
    assert grade({"all": [{"gold": {"gte": 1}}, {"never": rat}]}, tl)["violations"] == [{"t_s": 31, "node": rat}]
    # The last kill's Died comes after combat_ended; its KilledBy (the kill event) before.
    tl = run(
        snap(10, party=party),
        ev(20, "combat_started"),
        ev(30, "kill", victim="Boss"),
        ev(31, "combat_ended"),
        ev(32, "died", name="Boss", party=False),
    )
    assert grade(boss, tl)["t_pass_s"] == 31
    # One poll's diff (no referee ring): combat_ended, then the death, at the same instant.
    tl = run(
        snap(10, party=party),
        ev(20, "combat_started"),
        ev(30, "combat_ended"),
        ev(30, "died", name="Boss", party=False),
    )
    assert grade(boss, tl)["t_pass_s"] == 30
    assert not grade(boss, tl[:-1] + [ev(31, "died", name="Boss", party=False)])["passed"]


def test_a_combat_seen_only_in_snapshots_counts():
    tl = run(snap(10), snap(20, combat=True), snap(30, combat=True), snap(40))
    assert grade({"combats_won": {"gte": 1}}, tl)["t_pass_s"] == 40
    assert grade({"combats_started": {"lte": 1}}, tl)["passed"]


def test_never_violated_then_recovered_stays_false():
    tl = run(snap(10, gold=0), ev(20, "combat_started"), ev(30, "combat_ended"), snap(40, gold=500))
    out = grade({"all": [{"gold": {"gte": 100}}, {"no_combat": True}]}, tl)
    assert not out["passed"]
    assert out["violations"] == [{"t_s": 20, "node": {"combat": True}}]
    assert grade({"all": [{"gold": {"gte": 100}}, {"no_combat": True}]}, run(snap(40, gold=500)))["passed"]


def test_violations_after_the_verdict_are_not_reported():
    tl = run(snap(10, gold=500), ev(20, "combat_started"))
    out = grade({"all": [{"gold": {"gte": 100}}, {"no_combat": True}]}, tl)
    assert out["passed"] and out["t_pass_s"] == 10 and out["violations"] == []


def test_not_of_a_bare_leaf_is_never():
    tl = run(snap(10, gold=0), snap(20, gold=200), snap(30, gold=0))
    assert grade({"all": [{"region": "wild"}, {"not": {"gold": {"gte": 100}}}]}, tl)["t_pass_s"] == 10
    out = grade({"all": [{"elapsed_s": {"gte": 25}}, {"not": {"gold": {"gte": 100}}}]}, tl)
    assert not out["passed"]
    out = grade({"all": [{"elapsed_s": {"gte": 25}}, {"not": {"state": {"gold": {"gte": 100}}}}]}, tl)
    assert out["passed"] and out["t_pass_s"] == 30


def test_places_and_quests():
    tl = run(
        snap(10, region="Nautiloid"),
        ev(20, "region", name="Wilderness"),
        snap(30, area="Ravaged Beach", areas=["Ravaged Beach"]),
        ev(40, "area", name="Emerald Grove"),
        ev(50, "quest_done", title="Escape the Nautiloid"),
        snap(60, quests={"Find a Cure": True}),
        ev(70, "game_end"),
    )
    assert grade({"region": "nautiloid"}, tl)["t_pass_s"] == 10
    assert grade({"region": "wilderness"}, tl)["t_pass_s"] == 20
    assert grade({"milestone": "beach"}, tl)["t_pass_s"] == 30
    assert grade({"milestone": "grove"}, tl)["t_pass_s"] == 40
    assert grade({"milestone": "nautiloid_escaped"}, tl)["t_pass_s"] == 50
    assert grade({"quest_done": "cure"}, tl)["t_pass_s"] == 60
    assert grade({"milestone": "game_end"}, tl)["t_pass_s"] == 70
    assert not grade({"area": "underdark"}, tl)["passed"]


def test_survive_uses_game_time_and_fails_on_a_death():
    party = [TAV, member("Gale")]
    ok = run(snap(600, party=party), snap(1800, party=party))
    assert grade({"survive_s": 1800}, ok)["t_pass_s"] == 1800
    assert not grade({"survive_s": 1801}, ok)["passed"]
    dead = run(snap(600, party=party), ev(900, "died", name="Gale", party=True), snap(1800, party=party))
    out = grade({"survive_s": 1800}, dead)
    assert not out["passed"] and out["violations"] == [{"t_s": 900, "node": {"party_dead": {"gte": 1}}}]


def test_game_clock_versus_wall_clock_on_the_gold_fixture():
    tl = facts.load(TL / "gold_spend.facts.jsonl")
    game = grade({"gold": {"gte": 1000}}, tl)
    wall = grade({"gold": {"gte": 1000}}, tl, clock="wall")
    assert game["t_pass_s"] == 1799  # the gold event a game second before the snapshot
    assert wall["t_pass_s"] == 1799 * 1.5  # wall time runs 1.5x game time (think-pause)
    with pytest.raises(ValueError):
        grade({"gold": {"gte": 1}}, tl, clock="cpu")


def test_curve_with_spending_and_progress():
    tl = facts.load(TL / "gold_spend.facts.jsonl")
    out = grade({"gold": {"gte": 5000}}, tl, progress={"fact": "gold", "target": 2000})
    assert out["curve"] == {
        "fact": "gold",
        "points": [
            {"at_s": 600, "value": 400, "peak": 400},
            {"at_s": 1200, "value": 300, "peak": 700},
            {"at_s": 1800, "value": 1100, "peak": 1100},
        ],
    }
    assert out["progress"] == pytest.approx(0.55)
    capped = grade({"gold": {"gte": 5000}}, tl, progress={"fact": "gold", "target": 1000})
    assert capped["progress"] == 1.0
    assert out["passed"] is False


def test_curve_points_after_the_run_ended_are_null():
    out = grade(
        {"gold": {"gte": 1}},
        run(snap(10, gold=5), snap(700, gold=8)),
        curve={"fact": "companions", "at_s": [5, 600, 1200]},
    )
    assert out["curve"]["points"] == [
        {"at_s": 5, "value": 0, "peak": 0},
        {"at_s": 600, "value": 0, "peak": 0},
        {"at_s": 1200, "value": None, "peak": None},
    ]
    assert out["progress"] is None
    lvl = grade({"gold": {"gte": 1}}, run(snap(10, gold=5)), curve={"fact": "party_level", "at_s": [10]})
    assert lvl["curve"]["points"] == [{"at_s": 10, "value": 2, "peak": 2}]


def test_bad_curve_or_progress_spec():
    tl = run(snap(10))
    with pytest.raises(ValueError):
        grade({"gold": {"gte": 1}}, tl, curve={"fact": "hp", "at_s": [10]})
    with pytest.raises(ValueError):
        grade({"gold": {"gte": 1}}, tl, progress={"fact": "gold", "target": 0})
    with pytest.raises(ValueError):
        grade({"gold": {"gte": 1}}, tl, progress={"fact": "mana", "target": 1})


def test_window_folds_setup_and_ignores_after_run_end():
    tl = [snap(-50, gold=300), mark(0, "run_start"), snap(10, gold=300), mark(20, "run_end"), snap(30, gold=900)]
    tl[0]["game_t"] = None
    out = grade({"gold": {"gte": 300}}, tl)
    assert out["passed"] and out["t_pass_s"] == 0  # the state before the start is the state at time zero
    assert not grade({"gold": {"gte": 900}}, tl)["passed"]


def test_no_run_start_mark_means_the_first_record_is_time_zero():
    tl = [snap(5, gold=1), snap(15, gold=200)]
    assert grade({"gold": {"gte": 100}}, tl, clock="wall")["t_pass_s"] == 10
    assert grade({"gold": {"gte": 100}}, tl)["t_pass_s"] == 15


def test_game_clock_without_game_time_raises():
    tl = [{"v": 1, "kind": "mark", "t": 0, "mark": "run_start"}, {**snap(10, gold=5), "game_t": None}]
    assert grade({"gold": {"gte": 5}}, tl, clock="wall")["t_pass_s"] == 1010
    with pytest.raises(facts.FactsError, match="wall"):
        grade({"gold": {"gte": 5}}, tl)
    gap = run(snap(10, gold=1), {**ev(20, "gold", gold=5, delta=4), "game_t": None})
    with pytest.raises(facts.FactsError, match="t=1020"):
        grade({"gold": {"gte": 5}}, gap)


def test_bad_timelines_raise():
    with pytest.raises(facts.FactsError):
        grade({"gold": {"gte": 1}}, [])
    with pytest.raises(facts.FactsError, match="back in time"):
        grade({"gold": {"gte": 1}}, [snap(10), snap(5)])
    with pytest.raises(facts.FactsError):
        grade({"gold": {"gte": 1}}, [{**snap(1), "v": 2}])
    with pytest.raises(facts.FactsError):
        grade({"gold": {"gte": 1}}, [{**snap(1), "kind": "other"}])
    with pytest.raises(facts.FactsError):
        grade({"gold": {"gte": 1}}, [{**snap(1), "t": "x"}])


def test_grade_applies_d8_by_default_and_accepts_a_parsed_node():
    from bg3bench.predicates import GoalError, parse

    with pytest.raises(GoalError):
        graders.grade({"no_combat": True}, run(snap(10)))
    node = parse({"gold": {"gte": 1}})
    assert graders.grade(node, run(snap(10, gold=1)))["passed"]


def test_real_naut2_run_reaches_the_milestone_at_the_runner_time():
    # naut2 run 1 (2026-10-13): runs.jsonl says time_to_milestone_s 545.9 from started 1792169546.4.
    tl = facts.load(TL / "naut2_S001.facts.jsonl")
    out = grade({"milestone": "nautiloid_escaped"}, tl, clock="wall")
    assert out["passed"] and out["t_pass_s"] == pytest.approx(545.9, abs=0.1)
    assert grade({"milestone": "first_companion"}, tl, clock="wall")["t_pass_s"] == pytest.approx(138.8, abs=0.1)
    assert grade({"milestone": "crash_site"}, tl, clock="wall")["passed"]
    assert not grade({"milestone": "beach"}, tl, clock="wall")["passed"]
    with pytest.raises(facts.FactsError):
        grade({"milestone": "nautiloid_escaped"}, tl)


def test_naut2_fixture_is_the_conversion_of_the_progress_file():
    progress = [json.loads(line) for line in (TL / "naut2_S001.progress.jsonl").read_text().splitlines()]
    converted = list(facts.from_progress(progress, started=1792169546.4))
    assert converted == facts.load(TL / "naut2_S001.facts.jsonl")[:-1]  # the fixture adds the run_end mark


def test_from_progress_kinds():
    recs = [
        {"type": "poll", "t": 1.0, "ok": True},
        {"type": "progress", "t": 2.0, "kind": "level_up", "what": "Tav -> level 2"},
        {"type": "progress", "t": 2.0, "kind": "level_up", "what": "garbled"},
        {"type": "progress", "t": 3.0, "kind": "game_end", "what": "credits"},
        {"type": "progress", "t": 3.0, "kind": "talk", "what": "Lae'zel"},
    ]
    out = list(facts.from_progress(recs))
    assert out == [
        {"v": 1, "kind": "mark", "t": 1.0, "mark": "run_start", "detail": "from_progress"},
        {"v": 1, "kind": "event", "t": 2.0, "ev": "level_up", "name": "Tav", "level": 2},
        {"v": 1, "kind": "event", "t": 3.0, "ev": "game_end"},
    ]
    assert grade({"milestone": "level_2"}, out, clock="wall")["t_pass_s"] == 1.0


def test_fixtures_match_the_facts_schema():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(FACTS_SCHEMA.read_text()))
    files = sorted(TL.glob("*.facts.jsonl"))
    assert len(files) >= 3
    for path in files:
        for i, rec in enumerate(facts.load(path)):
            errors = list(validator.iter_errors(rec))
            assert not errors, f"{path.name}:{i + 1}: {errors[0].message}"
    assert not validator.is_valid({"v": 1, "kind": "event", "t": 1, "ev": "gold", "gold": 5})  # delta missing
    assert validator.is_valid(ev(805, "game_over"))  # the harness's Game Over event, as the wipe tests write it
    assert not validator.is_valid({"v": 1, "kind": "snapshot", "t": 1, "gold": 5})
    assert not validator.is_valid({"v": 1, "kind": "event", "t": 1, "ev": "died", "name": "Gale"})  # party?


def test_every_child_is_stepped_so_latches_see_each_instant():
    # all() and any() must not short-circuit: the ever(combat) latch has to see the snapshot at 10 while the elapsed
    # test before it is false (all) and the gold test before it is true (any).
    tl = run(snap(10, gold=100, combat=True), snap(20, gold=0))
    assert grade({"all": [{"elapsed_s": {"gte": 20}}, {"ever": {"combat": True}}]}, tl)["t_pass_s"] == 20
    goal = {
        "all": [{"elapsed_s": {"gte": 20}}, {"any": [{"state": {"gold": {"gte": 100}}}, {"ever": {"combat": True}}]}]
    }
    assert grade(goal, tl)["t_pass_s"] == 20


def test_violation_reports_describe_the_node():
    gem = {"name": "Umbral Gem", "count": 1}
    tl = run(snap(10, inv=[gem], region="Nautiloid"))
    never = {
        "any": [
            {"all": [{"item": {"name": "Umbral Gem"}}, {"not": {"combat": True}}]},
            {"ever": {"region": "x"}},
            {"item": {"tpl": "t"}},
        ]
    }
    out = grade({"all": [{"gold": {"gte": 1}}, {"never": never}]}, tl)
    assert out["violations"] == [
        {
            "t_s": 10,
            "node": {
                "any": [
                    {"all": [{"item": {"name": "Umbral Gem", "gte": 1}}, {"not": {"combat": True}}]},
                    {"ever": {"region": "x"}},
                    {"item": {"tpl": "t", "gte": 1}},
                ]
            },
        }
    ]


def test_unknown_nodes_raise_type_error():
    with pytest.raises(TypeError):
        graders._Eval(None).step("nope", facts.State(), 0)
    with pytest.raises(TypeError):
        graders._describe("nope")


def test_the_live_verdict_passes_fails_or_waits_as_grade_would():
    goal = {"gold": {"gte": 100}}
    assert graders.verdict(goal, run(snap(10, gold=50))) is None  # still playing
    assert graders.verdict(goal, [snap(10, gold=500)]) is None  # no run_start yet: not this run's facts
    tl = run(snap(10, gold=50), ev(20, "gold", gold=150, delta=100))
    assert graders.verdict(goal, tl) == "passed" and grade(goal, tl)["passed"]
    sneak = {"all": [{"area": "shattered sanctum"}, {"no_combat": True}]}
    fought = run(snap(10, combat=True), snap(20, area="Shattered Sanctum", areas=["Shattered Sanctum"]))
    assert graders.verdict(sneak, fought) == "failed" and not grade(sneak, fought)["passed"]  # lost for good
    either = {"any": [{"gold": {"gte": 100}}, {"all": [{"area": "x"}, {"no_combat": True}]}]}
    assert graders.verdict(either, run(snap(10, combat=True))) is None  # the gold branch can still pass
    after_end = run(snap(10), mark(20, "run_end"), ev(30, "gold", gold=150, delta=150))
    assert graders.verdict(goal, after_end) is None  # nothing after run_end counts, as in grade


def test_measure_scores_the_gain_from_the_first_snapshot_to_run_end():
    tl = run(snap(5, gold=133), ev(60, "gold", gold=400, delta=267), snap(700, gold=900), snap(900, gold=650),
             mark(2700, "run_end"), snap(2710, gold=5000))  # fmt: skip
    m = graders.measure("gold", tl)
    assert {k: m[k] for k in ("fact", "start", "end", "gain", "peak")} == {
        "fact": "gold",
        "start": 133,
        "end": 650,
        "gain": 517,
        "peak": 900,
    }  # the snapshot after run_end is not the run's
    assert [p["value"] for p in m["curve"]["points"]] == [400, 650, 650]


def test_measure_starts_from_the_state_carried_into_run_start_not_from_zero():
    # before any snapshot State.gold is 0, not the save's gold: a gain from 0 would count the start gold as made
    carried = [snap(-5, gold=133), mark(0, "run_start"), snap(10, gold=200)]
    assert graders.measure("gold", carried)["start"] == 133
    assert graders.measure("gold", run(ev(1, "trade_open"), snap(10, gold=133), snap(20, gold=150)))["gain"] == 17
    none = graders.measure("gold", run(ev(1, "trade_open")))
    assert (none["start"], none["end"], none["gain"], none["peak"]) == (None, None, None, None)


def test_measure_refuses_an_unknown_fact():
    with pytest.raises(ValueError, match="unknown numeric fact"):
        graders.measure("xp", run(snap(1, gold=1)))


BALTHAZAR = [member(n, 6) for n in ("Wyll", "Karlach", "Shadowheart")] + [{**member("Tav", 6), "avatar": True}]


def balthazar_wipe():
    """Bench B3 Opus x fight_act2_balthazar: the party wiped by round 3 (Game Over), then the agent took the main
    menu's Continue into another run's Act 1 save, where the fight's goal could have passed."""
    return run(
        snap(15, gold=60, party=BALTHAZAR, region="Shadow-Cursed Lands"),
        ev(33, "combat_started"),
        ev(226, "died", name="Wyll", party=True),
        ev(227, "downed", name="Karlach", party=True),
        ev(248, "died", name="Shadowheart", party=True),
        ev(302, "downed", name="Tav", party=True),
        snap(515, gold=1200, party=[member("Gale", 3), {**member("Tav", 3), "avatar": True}], region="Wilderness"),
        ev(520, "combat_ended"),
    )


def test_a_wipe_fails_a_goal_run_and_nothing_after_it_counts():
    tl = balthazar_wipe()
    assert graders.verdict({"combats_won": {"gte": 1}}, tl[:6]) is None  # Tav still standing
    assert graders.verdict({"combats_won": {"gte": 1}}, tl) == "wiped"
    assert graders.verdict({"gold": {"gte": 1000}}, tl) == "wiped"  # the 1200 gold after it never passes
    for goal in ({"combats_won": {"gte": 1}}, {"gold": {"gte": 1000}}):
        out = grade(goal, tl)
        assert (out["passed"], out["violations"]) == (False, [{"t_s": 302, "node": {"wipe": True}}])
    assert grade({"gold": {"gte": 50}}, tl)["t_pass_s"] == 15  # a pass before the wipe stands
    assert graders.verdict({"gold": {"gte": 50}}, tl) == "passed"


def test_a_wipe_ends_a_maximise_run_at_its_gain_so_far():
    tl = balthazar_wipe()
    assert graders.verdict(None, tl) == "wiped" and graders.verdict(None, tl[:6]) is None
    m = graders.measure("gold", tl)
    assert (m["start"], m["end"], m["gain"], m["peak"]) == (60, 60, 0, 60)  # not the other save's 1200
