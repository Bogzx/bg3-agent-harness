# pyright: basic
"""The first scenario set (packages/bench/scenarios/*.json, v2) and its closed-book quizzes (scenarios/quizzes/)."""

import json
import re
from pathlib import Path

import pytest

from bg3bench import contamination, predicates, scenario

HERE = Path(__file__).resolve().parent
SET = HERE.parent / "scenarios"
FILES = sorted(SET.glob("*.json"))
V1 = HERE.parents[2] / "scenarios"
FAMILIES = {
    "nautiloid": "exploration",
    "act1_gold_1000": "sandbox",
    "fight_act1_windmill": "fight",
    "fight_act2_house_of_healing": "fight",
    "fight_act2_balthazar": "fight",
    "choice_act1_karlach": "choice",
    "choice_act1_minthara": "choice",
    "fight_act1_grove_gate": "fight",
    "fight_act1_dror_ragzlin": "fight",
    "fight_act1_gith_patrol": "fight",
    "fight_act2_moonrise_assault": "fight",
    "fight_act2_ketheric": "fight",
    "fight_act3_sarevok": "fight",
    "fight_act3_cazador": "fight",
    "act1_level_3": "sandbox",
    "act1_recruit_3": "sandbox",
    "act1_goblin_camp_no_fight": "sandbox",
    "act1_get_item": "sandbox",
    "choice_act1_arabella": "choice",
    "choice_act2_nightsong": "choice",
    "choice_act3_wyll_pact": "choice",
    "crash_site_grove": "exploration",
    "act2_last_light": "exploration",
}
FIRST_SET = {
    "nautiloid", "act1_gold_1000", "fight_act1_windmill", "fight_act2_house_of_healing", "fight_act2_balthazar",
    "choice_act1_karlach", "choice_act1_minthara",
}  # fmt: skip
# The private holdout (BENCHMARK-PLAN.md): run, never published; about a fifth of the set, one per family but puzzle.
HOLDOUT = {"fight_act1_gith_patrol", "fight_act3_cazador", "act1_get_item", "choice_act3_wyll_pact", "act2_last_light"}
# Scenarios the legacy runner can play today: their goal is one milestone (scenario.resolve).
RESOLVABLE = {"nautiloid", "crash_site_grove", "act2_last_light"}


def validator():
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = HERE.parent / "schema"
    resources = []
    for name in ("goal.v1.schema.json", "scenario.v2.schema.json"):
        doc = json.loads((schemas / name).read_text())
        resources.append((doc["$id"], referencing.Resource.from_contents(doc)))
    return jsonschema.Draft202012Validator(
        json.loads((schemas / "scenario.v2.schema.json").read_text()),
        registry=referencing.Registry().with_resources(resources),
    )


def raw(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_set_is_complete():
    assert {p.stem: raw(p)["family"] for p in FILES} == FAMILIES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_scenario_is_valid_v2(path):
    sc = raw(path)
    assert sc["schema"] == 2 and "think_pause" not in sc
    assert scenario.validate(sc, path.stem) == []
    errors = list(validator().iter_errors(sc))
    assert not errors, errors[0].message
    loaded = scenario.load(path)
    assert loaded["game_settings"]["karmic_dice"] is False and loaded["game_settings"]["difficulty"] == "Balanced"
    assert isinstance(loaded["game_settings"]["seed"], int)  # D9: every run of it starts from the same rolls
    assert loaded["timing"] == ("turn_based" if loaded["family"] == "fight" else "realtime")
    assert loaded["methods"] is (loaded["family"] == "sandbox")
    assert predicates.positive(predicates.parse(scenario.goal_of(loaded)))  # D8
    assert loaded["caps"]["run_hours"] <= 4 and loaded["conditions"] == ["rules"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_save_starts_wait_for_their_save(path):
    st = raw(path)["start"]
    if st["kind"] == "new_game":
        return
    assert st["save"] is None and st["save_sha1"] is None
    assert re.search(r"\bAct [123]\b", st["needs_save"]) and "Tav" in st["needs_save"]
    assert raw(path)["profile"]["character"]["name"] == "Tav"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_needs_save_agrees_with_the_note(path):
    st = raw(path)["start"]
    if st["kind"] == "new_game":
        return
    found = [re.fullmatch(r"(.+) \((\w+)(?: (\d+))?\)", m) for m in st["note"]["party"]]
    assert all(found), st["note"]["party"]
    party = [m.groups() for m in found if m]
    assert party[0][0] == "Tav"
    if level := re.search(r"party level (\d+)", st["needs_save"]):
        assert {lv for _, _, lv in party} == {level[1]}, path.stem
    if named := re.search(r"companions \(([^)]+)\)", st["needs_save"]):
        assert sorted(re.split(r", | and ", named[1])) == sorted(n for n, _, _ in party[1:]), path.stem


def test_starts_match_the_claude_run_2_saves():
    """research/ovh/bench.md "Start saves to make": run 2 never recruited Lae'zel; the Sanctum save is level 3."""
    gold = raw(SET / "act1_gold_1000.json")["start"]
    assert "Lae'zel never recruited): two companions (Shadowheart, Gale)" in gold["needs_save"]
    assert not any("Lae'zel" in m for m in gold["note"]["party"])
    minthara = raw(SET / "choice_act1_minthara.json")["start"]
    assert "S017-worgpens" in minthara["needs_save"] and "party level 3" in minthara["needs_save"]


def test_seeds_differ():
    seeds = [raw(p)["game_settings"]["seed"] for p in FILES]
    assert len(set(seeds)) == len(seeds)


def test_nautiloid_is_the_v1_file_converted():
    v1, v2 = raw(V1 / "nautiloid.json"), scenario.load(SET / "nautiloid.json")
    conv = scenario.with_defaults(scenario.from_v1(v1))
    same = ("name", "title", "start", "until", "runs", "caps", "profile", "family", "timing", "reset")
    assert {k: v2[k] for k in same} == {k: conv[k] for k in same}
    assert conv["game_settings"]["difficulty"] is None  # v1 left it to the game; v2 fixes it
    assert v2["game_settings"] == {**conv["game_settings"], "seed": 1001, "difficulty": "Balanced"}
    assert scenario.digest(v2) != scenario.digest(conv)  # dice seeded: a new digest, old series stay legacy


def test_sandbox_gold_target_and_cap():
    sc = scenario.load(SET / "act1_gold_1000.json")
    assert sc["goal"] == {"gold": {"gte": 1000}} and sc["caps"]["run_hours"] == 0.75 and sc["methods"] is True


def test_choice_scenes_use_a_neutral_brief():
    for name in (n for n, f in FAMILIES.items() if f == "choice"):
        brief = raw(SET / f"{name}.json")["profile"]["character"]["brief"].lower()
        assert not re.search(r"devotion|protects|good|evil|oath", brief), name


def test_the_set_covers_the_plan():
    """BENCHMARK-PLAN.md: 20-30 scenarios, fights the core, every act's fights, sandbox goals, choices, exploration."""
    fams = list(FAMILIES.values())
    assert 20 <= len(FAMILIES) <= 30 and fams.count("fight") == 10 and fams.count("fight") > len(fams) / 3
    assert fams.count("sandbox") == 5 and fams.count("choice") == 5 and fams.count("exploration") == 3
    fights = [n for n, f in FAMILIES.items() if f == "fight"]
    assert {re.match(r"fight_act(\d)_", n)[1] for n in fights} == {"1", "2", "3"}


def test_the_holdout_is_about_a_fifth_and_spans_the_families():
    loaded = [scenario.load(p) for p in FILES]
    assert contamination.holdout_names(loaded) == HOLDOUT
    assert 0.15 <= len(HOLDOUT) / len(FILES) <= 0.25
    assert {FAMILIES[n] for n in HOLDOUT} == {"fight", "sandbox", "choice", "exploration"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_scenario_resolves_only_when_its_goal_is_one_milestone(path):
    sc = scenario.load(path)
    if path.stem in RESOLVABLE:
        resolved = scenario.resolve(sc)
        assert resolved["game_settings"]["seed"] == sc["game_settings"]["seed"]
        assert "needs_save" not in resolved["start"]
    else:
        with pytest.raises(scenario.ScenarioError, match="not one milestone"):
            scenario.resolve(sc)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_requirements(path):
    assert scenario.load(path)["requires"] == {"vision": False, "min_context": None}  # no scenario reads the screen yet
    if path.stem not in FIRST_SET:  # the round-11 files say so; the first seven keep their digests (default requires)
        assert raw(path)["requires"] == {"vision": False, "min_context": None}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_scenario_has_a_closed_book_quiz(path):
    quiz = contamination.load_quiz(SET / "quizzes" / path.name)
    assert quiz["scenario"] == path.stem and 3 <= len(quiz["questions"]) <= 5
    perfect = {q["id"]: q["answer"] for q in quiz["questions"]}
    assert contamination.score(quiz, perfect)["rate"] == 1.0  # the answer key passes its own grading
    assert contamination.score(quiz, {})["correct"] == 0
    prompt = contamination.prompt(quiz)
    assert all(q["q"] in prompt for q in quiz["questions"])
    for q in quiz["questions"]:  # no answer leaks into the prompt
        if q["kind"] == "text":
            assert not any(re.search(p, prompt, re.IGNORECASE) for p in q["accept"]), (path.stem, q["id"])


def test_no_stray_quiz():
    assert {p.stem for p in (SET / "quizzes").glob("*.json")} == {p.stem for p in FILES}
