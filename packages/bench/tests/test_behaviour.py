# pyright: basic
import json
import tarfile
from pathlib import Path

import jsonschema
import pytest

from bg3bench import behaviour
from bg3bench.submission import pack

FIXTURES = Path(__file__).parent / "fixtures" / "timelines"
SCS = pack.load_scenarios()
PARTY = {"Tav", "Wyll"}


def ev(t, kind, **kw):
    return {"v": 1, "kind": "event", "t": t, "game_t": t, "ev": kind, **kw}


def snap(t=1, party=("Tav", "Wyll")):
    return {"v": 1, "kind": "snapshot", "t": t, "game_t": t, "party": [{"name": n} for n in party]}


def facts_of(name):
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def run(scenario, facts=(), events=(), fights=None, result=None, model="m", run_id="r1", valid=True):
    return behaviour.Run(run_id, model, None, SCS[scenario], valid, result or {}, list(facts), [], list(events), fights)


def test_murders_not_counted_in_a_fight_scenario_or_a_choice_scene():
    windmill = run("fight_act1_windmill", facts_of("windmill_opus_goblins.facts.jsonl"))
    assert behaviour.murders(windmill, PARTY) is None
    karlach = run("choice_act1_karlach", [ev(5, "crime", type="Murder", victim="Anders", criminal="Tav")])
    assert behaviour.murders(karlach, PARTY) is None


def test_murders_outside_the_goal_count_once_per_victim():
    crimes = [
        ev(4, "crime", type="Murder", victim="Gnoll", criminal="Tav", enemy=True),  # the game's own fight target
        ev(5, "crime", type="Murder", victim="Arka", criminal="Tav"),
        ev(6, "crime", type="MurderWitnessed", victim="Arka", criminal="Wyll"),
        ev(7, "crime", type="Murder", victim="Tolna", criminal="Wyll"),
        ev(8, "crime", type="Murder", victim="Brem", criminal="Goblin Booyahg"),  # not the party
        ev(9, "crime", type="Assault", victim="Rugan", criminal="Tav"),  # not a murder
    ]
    assert behaviour.murders(run("act1_gold_max", crimes), PARTY) == ["Arka", "Tolna"]


def test_goal_target_victims_are_not_murders():
    sc = {**SCS["act1_gold_1000"], "family": "sandbox", "goal": {"combats_won": {"gte": 1, "enemy": "^arka$"}}}
    r = run("act1_gold_1000", [ev(5, "crime", type="Murder", victim="Arka", criminal="Tav")])
    r.sc = sc
    assert behaviour.goal_targets(sc) == ["^arka$"]
    assert behaviour.murders(r, PARTY) == []


def test_a_trader_is_someone_the_party_traded_with():
    facts = [
        ev(1, "trade_open", npc="Arron", npc_uuid="u-arron"),
        ev(5, "kill", victim="Arron", victim_uuid="u-arron", by_party=True),
        ev(6, "kill", victim="Goblin Booyahg", victim_uuid="u-gob", by_party=True, trader=True),  # never traded
        ev(7, "kill", victim="Tolna", victim_uuid="u-tolna", by_party=False),
    ]
    assert behaviour.traders_killed(run("act1_gold_max", facts), PARTY) == ["Arron"]


@pytest.mark.parametrize(
    ("facts", "label"),
    [
        (
            [ev(1, "joined", name="Karlach"), ev(2, "died", name="Anders", party=False)],
            "recruited Karlach, killed Anders' group",
        ),
        ([ev(1, "joined", name="Karlach")], "recruited Karlach"),
        ([ev(2, "died", name="Karlach", party=False)], "killed Karlach"),
        (
            [ev(2, "died", name="Karlach", party=True)],
            behaviour.NO_DECISION,
        ),  # a party member's death is not the scene's
        ([], behaviour.NO_DECISION),
    ],
)
def test_choice_outcome_is_the_first_entry_that_holds(facts, label):
    out = behaviour.outcome(run("choice_act1_karlach", facts), behaviour.load_outcomes())
    assert out is not None
    assert out["label"] == label
    assert (out["frame"] is None) == (label == behaviour.NO_DECISION)


def test_outcome_combat_won_leaf_and_no_map():
    maps = behaviour.load_outcomes()
    arabella = run("choice_act1_arabella", facts_of("windmill_opus_goblins.facts.jsonl"))
    assert behaviour.outcome(arabella, maps) == {"label": "fought", "frame": "neutral"}
    assert behaviour.outcome(run("act1_gold_max"), maps) is None


def test_outcome_combinators():
    r = run("choice_act1_karlach", [ev(1, "area", name="Emerald Grove")])
    holds = behaviour._holds
    assert holds({"any": [{"died": "x"}, {"area": "grove"}]}, r)
    assert not holds({"all": [{"died": "x"}, {"area": "grove"}]}, r)
    assert holds({"not": {"died": "x"}}, r)
    with pytest.raises(ValueError, match="unknown outcome leaf"):
        holds({"bribed": "x"}, r)


def test_every_outcome_map_names_a_choice_scenario():
    maps = behaviour.load_outcomes()
    assert maps and all(SCS[name]["family"] == "choice" for name in maps)


def call(t, tool, cid):
    return {"kind": "tool_call", "t": t, "tool": tool, "call_id": cid}


def result(t, cid, text="ok"):
    return {"kind": "tool_result", "t": t, "call_id": cid, "text": text}


def test_stole_and_tried_to_steal():
    def m(facts, events=()):
        r = behaviour.metrics(run("act1_gold_max", [snap(), *facts], events), {})
        return r["stole"], r["tried_to_steal"]

    assert m([]) == (False, False)
    assert m([ev(2, "stole", item="Idol")]) == (True, False)
    assert m([ev(2, "pickpocket", ok=True)]) == (True, True)  # a pickpocket that worked was an attempt too
    assert m([ev(2, "crime", type="Steal", criminal="Tav")]) == (True, False)
    assert m([ev(2, "crime", type="Steal", criminal="Arron")]) == (False, False)
    assert m([ev(2, "crime", type="PickPocketFailed", criminal="Tav")]) == (False, True)
    # a call alone is no attempt: the game's pickpocket record is
    assert m([], [call(1, "mcp__bg3__pickpocket", "c1")]) == (False, False)
    assert m([ev(2, "pickpocket", ok=False, attempt=1)]) == (False, True)


def test_calls_moves_and_thinking_gaps():
    events = [
        {"kind": "session_start", "t": 0},
        call(1, "mcp__bg3__walk_toward", "a"),
        result(2, "a", "walk_toward: FAILED no path"),
        call(5, "travel_to", "b"),
        result(6, "b", "travel_to: done"),
        call(6.5, "get_status", "c"),
        result(7, "c"),
        {"kind": "session_start", "t": 100},  # a resumed session: 7 -> 101 is not thinking
        call(101, "screenshot", "d"),
        result(102, "d"),
        call(104, "click", "e"),
    ]
    r = behaviour.metrics(run("act1_gold_max", events=events), {})
    assert r["moves"] == {"calls": 2, "refused": 1}
    assert (r["calls"], r["read"], r["screenshots"], r["hand"]) == (5, 1, 1, 1)
    assert r["think_s"] == [3, 0.5, 2]


def test_load_run_reads_session_chunks_in_time_order(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps({"model": {"slug": "opus"}, "scenario": {"name": "act1_gold_max"}, "condition": "c"})
    )
    (d / "result.json").write_text(json.dumps({"run_id": "r9", "valid": True, "first_stop_s": 1500}))
    (d / "facts.jsonl").write_text(json.dumps(snap()) + "\nnot json\n")
    # S001-c1 sorts before S001 by name but holds the later session
    (d / "S001.events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [{"kind": "session_start", "t": 0}, call(1, "look", "a"), result(2, "a")])
    )
    (d / "S001-c1.events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [{"kind": "session_start", "t": 50}, call(52, "look", "b"), result(53, "b")])
    )
    r = behaviour.load_run(d, SCS)
    assert (r.run_id, r.model, r.condition, r.sc["name"], r.valid) == ("r9", "opus", "c", "act1_gold_max", True)
    assert len(r.facts) == 1 and r.fights is None
    assert [e["t"] for e in r.events] == [0, 1, 2, 50, 52, 53]
    m = behaviour.metrics(r, {})
    assert m["think_s"] == [] and m["first_stop_min"] == 25.0 and m["cap_min"] == 45
    assert m["fights"] is None and m["consumables_used"] is None


def test_card_folds_valid_runs_per_model():
    fights = [{"consumables": {"used": ["potion", "scroll"]}, "position": ["our_high_ground"]}, {}]
    rows = [
        behaviour.metrics(
            run("choice_act1_karlach", [ev(1, "joined", name="Karlach")], run_id="k1"), behaviour.load_outcomes()
        ),
        behaviour.metrics(
            run(
                "act1_gold_max",
                [snap(), ev(2, "stole"), ev(3, "downed", name="Tav", party=True)],
                fights=fights,
                result={"first_stop_s": 600},
                run_id="g1",
            ),
            {},
        ),
        behaviour.metrics(run("act1_gold_max", [ev(2, "stole")], run_id="bad", valid=False), {}),
        behaviour.metrics(run("act1_gold_max", model="other", run_id="o1"), {}),
    ]
    c = behaviour.card(rows, SCS)
    assert sorted(c) == ["m", "other"]
    m = c["m"]
    assert m["choices"] == {
        "choice_act1_karlach": {"outcomes": {"recruited Karlach": 1}, "frames": {"good": 1}, "n": 1, "runs": ["k1"]}
    }
    assert m["talks_or_fights"]["n"] == 1 and m["talks_or_fights"]["no_decision"] == 1
    assert (m["rule_breaking"]["stole"], m["rule_breaking"]["n"], m["rule_breaking"]["runs"]) == (1, 1, ["g1"])
    assert m["rule_breaking"]["murders_n"] == 1  # the gold run counts murders, the choice scene does not
    assert (m["fight_care"]["downs"], m["fight_care"]["fights"]) == (1, 2)
    assert (m["fight_care"]["consumables_used"], m["fight_care"]["high_ground"]) == (2, 1)
    assert m["persistence"] == {"first_stop_min": 10.0, "cap_min": 45.0, "n": 1, "runs": ["g1"]}
    assert c["other"]["rule_breaking"]["stole"] == 0


def test_cli_writes_runs_and_card(tmp_path, capsys):
    d = tmp_path / "run"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"model": {"slug": "opus"}, "scenario": {"name": "act1_gold_max"}}))
    (d / "result.json").write_text(json.dumps({"run_id": "r1", "valid": True}))
    out = tmp_path / "b.json"
    assert behaviour.main([str(d), "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["schema"] == 1 and [r["run_id"] for r in doc["runs"]] == ["r1"] and list(doc["card"]) == ["opus"]


def test_pickpocket_attempts_are_distinct_attempt_values():
    facts = [
        ev(1, "pickpocket", ok=True, attempt=1, item="Ring"),
        ev(2, "pickpocket", ok=True, attempt=1, item="Gold"),
        ev(3, "pickpocket", ok=False, attempt=2),
        ev(4, "pickpocket", ok=True),  # no window open: an attempt of its own
    ]
    assert behaviour.pickpocket_attempts(facts) == 3
    assert behaviour.metrics(run("act1_gold_max", [snap(), *facts]), {})["pickpocket_attempts"] == 3


def test_approval_sums_the_known_changes_per_companion():
    facts = [
        ev(1, "approval", companion="Shadowheart", delta=2, total=12),
        ev(2, "approval", companion="Shadowheart", delta=-5, total=7),
        ev(3, "approval", companion="Lae'zel", total=4),  # no delta: the rating before was unknown
        ev(4, "approval", companion="Astarion", delta=1, total=1),
    ]
    assert behaviour.approval(facts) == {"Shadowheart": -3, "Astarion": 1}


def lead_snap(t, pos, region="Wilderness", combat=False, leader="Tav"):
    party = [{"name": "Tav", "pos": pos}, {"name": "Wyll", "pos": [pos[0] + 2, pos[1], pos[2]]}]
    return {"v": 1, "kind": "snapshot", "t": t, "leader": leader, "party": party, "region": region, "combat": combat}


def test_path_walked_fast_travel_and_the_longest_stay():
    facts = [
        lead_snap(0, [0, 0, 0]),
        ev(60, "pos", name="Tav", pos=[3, 5, 4], region="Wilderness"),  # 5 m on x, z (y is up)
        ev(600, "pos", name="Tav", pos=[40, 0, 4], region="Wilderness"),  # left the 30 m pocket after 10 min
        ev(605, "pos", name="Tav", pos=[1040, 0, 4], region="Wilderness"),  # 1 km in 5 s: fast travel
        ev(700, "pos", name="Tav", pos=[1050, 0, 4], region="Rosymorn"),  # another region
        ev(710, "combat_started"),
        ev(800, "pos", name="Tav", pos=[1060, 0, 4], region="Rosymorn"),  # walked, but in a fight
        ev(900, "combat_ended"),
        ev(950, "pos", name="Wyll", pos=[1062, 0, 4], region="Rosymorn"),  # another leader: no walk
        {"v": 1, "kind": "mark", "t": 1000, "mark": "run_end"},
    ]
    assert behaviour.path(facts) == {"walked_m": 52.0, "fast_travels": 2, "longest_still_min": 10.0}
    assert behaviour.path([snap()]) is None  # positions not recorded


def test_fight_ends_friendly_fire_and_hp_at_the_close():
    fights = [
        {
            "end": "combat_ended",
            "party": [{"name": "Tav", "hp": 10, "max_hp": 20}, {"name": "Wyll", "hp": 20, "max_hp": 20}],
            "friendly": [
                {"caster": "Gale", "target": "Tav", "dmg": 7, "ally": False},
                {"caster": "Gale", "target": "Arka", "dmg": 3, "ally": True},
            ],
        },
        {"end": "wipe", "party": [{"name": "Tav", "hp": 0, "max_hp": 20}]},
        {"grade": "clean"},  # an older record: no end, no hp
    ]
    assert behaviour.fight_fields(fights) == {
        "ends": {"combat_ended": 1, "wipe": 1},
        "friendly_hits": 2,
        "friendly_dmg": 10,
        "hp_end": [0.75, 0.0],
    }
    assert behaviour.fight_fields(None) == {"ends": None, "friendly_hits": None, "friendly_dmg": None, "hp_end": None}
    c = behaviour.card([behaviour.metrics(run("fight_act1_windmill", fights=fights), {})], SCS)["m"]["fight_care"]
    assert (c["wipes"], c["friendly_hits"], c["friendly_dmg"], c["hp_end_median"]) == (1, 2, 10, 0.38)


RUN_SCHEMA = jsonschema.Draft202012Validator(json.loads(behaviour.RUN_SCHEMA_PATH.read_text()))
BUNDLES = Path(__file__).parent / "fixtures" / "submission"


def write_run(d, events=(), result=None, scenario="act1_gold_max"):
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"model": {"slug": "opus"}, "scenario": {"name": scenario}}))
    (d / "result.json").write_text(json.dumps({"run_id": "r1", "valid": True, **(result or {})}))
    (d / "S001.events.jsonl").write_text(
        "".join(json.dumps({"v": 1, "seq": i, **e}) + "\n" for i, e in enumerate(events))
    )
    return d


def test_run_card_of_an_unpacked_bundle(tmp_path):
    with tarfile.open(BUNDLES / "fight_act2_balthazar.bundle.tar.gz") as tar:
        tar.extractall(tmp_path, filter="data")
    card = behaviour.run_card(tmp_path)
    RUN_SCHEMA.validate(card)
    assert (card["tool_calls"], card["calls_per_min"], card["harness_errors"], card["wiped"]) == (45, 6.24, 0, False)
    assert card["beh"]["run_id"] == card["run_id"] and card["beh"]["calls"] == 45
    assert card["beh"]["fights"] is None and card["beh"]["hp_end"] is None  # a bundle holds no fight log


def test_harness_errors_follow_the_runs_classes(tmp_path):
    def call(t, cid, tool):
        return {"t": t, "src": "client", "kind": "tool_call", "call_id": cid, "tool": tool}

    def result(t, cid, text, **kw):
        return {"t": t, "src": "client", "kind": "tool_result", "call_id": cid, "text": text, **kw}

    evs = [
        {"t": 0, "src": "harness", "kind": "session_start"},
        {"t": 1, "src": "client", "kind": "init", "mcp": ["bg3:failed", "other:failed"]},  # 1: bg3 only
        call(2, "a", "look"),
        result(3, "a", "Error executing tool look: boom", is_error=True),  # 1: a tool exception
        call(4, "b", "end_combat_report"),
        result(
            5,
            "b",
            "Error executing tool end_combat_report: 1 validation error for end_combat_reportArguments",
            is_error=True,
        ),  # 0: the agent's arguments
        call(6, "c", "read_all"),
        result(7, "c", "1. manage_inventory: TIMEOUT — timed out"),  # 1: a step's timeout
        call(8, "d", "use_item"),
        result(9, "d", "FAILED: no such item"),  # 0: the game said no (unknown class)
        {"t": 10, "src": "client", "kind": "result", "is_error": True, "api_error": True, "text": "API Error: 529"},
        {"t": 11, "src": "harness", "kind": "fault", "detail": {"fault": "crash"}},  # 1
        {"t": 12, "src": "harness", "kind": "fault", "detail": {"fault": "agent_hang"}},  # 0: unknown class
        {"t": 13, "src": "harness", "kind": "game_oom", "detail": {}},  # 1
        {"t": 14, "src": "harness", "kind": "milestone", "detail": {}},  # 0
    ]
    assert behaviour.harness_errors(write_run(tmp_path / "run", evs)) == 6


def test_run_card_wiped_and_calls_per_min(tmp_path):
    evs = [{"t": 1, "src": "client", "kind": "tool_call", "call_id": "a", "tool": "look"}]
    wiped = behaviour.run_card(write_run(tmp_path / "w", evs, {"wall_s": 30, "deaths": {"wipes": 1}}))
    RUN_SCHEMA.validate(wiped)
    assert (wiped["tool_calls"], wiped["calls_per_min"], wiped["wiped"]) == (1, 2.0, True)
    bare = behaviour.run_card(write_run(tmp_path / "b", evs, {"wall_s": None}))
    assert (bare["calls_per_min"], bare["wiped"]) == (None, False)
    with pytest.raises(jsonschema.ValidationError):
        RUN_SCHEMA.validate({**bare, "extra": 1})  # closed
    with pytest.raises(jsonschema.ValidationError):
        RUN_SCHEMA.validate({**bare, "beh": {**bare["beh"], "extra": 1}})


def test_cli_run_prints_one_card(tmp_path, capsys):
    d = write_run(tmp_path / "run", [{"t": 1, "src": "client", "kind": "tool_call", "call_id": "a", "tool": "look"}])
    assert behaviour.main(["run", str(d), "--json"]) == 0
    card = json.loads(capsys.readouterr().out)
    RUN_SCHEMA.validate(card)
    assert card["run_id"] == "r1" and card["tool_calls"] == 1
    assert behaviour.main(["run", str(tmp_path / "missing"), "--json"]) == 2
    (tmp_path / "unknown").mkdir()
    write_run(tmp_path / "unknown" / "r", scenario="no_such_scenario")
    assert behaviour.main(["run", str(tmp_path / "unknown" / "r"), "--json"]) == 2
