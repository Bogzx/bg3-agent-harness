# pyright: basic
import json

import pytest
from test_manifest_results import naut2_pairs
from test_scenario import gold_save

from bg3bench import conditions, contamination, results, scenario

QUIZ = {
    "schema": 1,
    "scenario": "nautiloid",
    "questions": [
        {"id": "q1", "q": "Which companion is first met in a pod?", "kind": "choice",
         "choices": ["Gale", "Shadowheart", "Wyll"], "answer": "B"},
        {"id": "q2", "q": "How many tadpoles?", "kind": "number", "answer": 1},
        {"id": "q3", "q": "Who is the brain in the jar?", "kind": "text", "answer": "Us",
         "accept": [r"intellect devourer"]},
    ],
}  # fmt: skip


def test_quiz_prompt_parse_and_score(tmp_path):
    assert contamination.check_quiz(QUIZ) == []
    text = contamination.prompt(QUIZ)
    assert "q1. Which companion" in text and "   B) Shadowheart" in text and "(a number)" in text
    assert "nautiloid" not in text.lower()  # nothing about the scenario's run
    reply = 'Sure.\n```json\n{"q1": "b) Shadowheart", "q2": "1", "q3": "An intellect devourer"}\n```'
    answers = contamination.parse_answers(reply)
    assert contamination.score(QUIZ, answers) == {
        "scenario": "nautiloid",
        "n": 3,
        "correct": 3,
        "rate": 1.0,
        "per_question": {"q1": True, "q2": True, "q3": True},
    }
    wrong = contamination.score(QUIZ, {"q1": "A", "q2": "two", "q3": "us!"})
    assert wrong["per_question"] == {"q1": False, "q2": False, "q3": True} and wrong["correct"] == 1
    assert contamination.score(QUIZ, {})["rate"] == 0.0
    assert contamination.parse_answers("no json {here") == {}
    assert contamination.parse_answers('{"a": 1} then {bad} ') == {"a": 1}
    (tmp_path / "q.json").write_text(json.dumps(QUIZ))
    assert contamination.load_quiz(tmp_path / "q.json") == QUIZ
    (tmp_path / "bad.json").write_text(json.dumps({**QUIZ, "questions": []}))
    with pytest.raises(ValueError, match="non-empty"):
        contamination.load_quiz(tmp_path / "bad.json")


@pytest.mark.parametrize(
    "q,msg",
    [
        ({"id": "x", "q": "?", "kind": "essay"}, "needs id, q and kind"),
        ({"id": "x", "q": "?", "kind": "choice", "choices": ["a"], "answer": "A"}, "2-8 choices"),
        ({"id": "x", "q": "?", "kind": "choice", "choices": ["a", "b"], "answer": "C"}, "choice letters"),
        ({"id": "x", "q": "?", "kind": "number", "answer": "3"}, "numeric answer"),
        ({"id": "x", "q": "?", "kind": "text", "answer": ""}, "needs an answer"),
    ],
)
def test_bad_questions(q, msg):
    assert any(msg in e for e in contamination.check_quiz({**QUIZ, "questions": [q]}))


def test_bad_quizzes():
    assert contamination.check_quiz({"schema": 1}) == ["not a v1 quiz with a scenario"]
    twice = {**QUIZ, "questions": QUIZ["questions"][:1] * 2}
    assert contamination.check_quiz(twice) == ["question ids repeat"]


def test_conditions_default_to_wiki_off():
    rules = conditions.Condition()
    assert rules.id == "rules" and rules.removed_tools() == ["wiki_search", "wiki_page", "load_game"]
    full = conditions.Condition(coaching="1a2b3c4d", wiki=True, persona="evil", reloads=2)
    assert full.id == "rules+coaching@1a2b3c4d+wiki+persona:evil+reloads:2" and full.removed_tools() == []
    for c in (rules, full, conditions.Condition(wiki=True), conditions.Condition(persona="evil")):
        assert conditions.parse(c.id) == c
    assert conditions.parse("rules+coaching@1a2b3c4d").wiki is False
    for bad, msg in [("coaching", "start with"), ("rules+wikis", "unknown part"), ("rules+wiki+coaching@1a2b3c4d", "canonical"),
                     ("rules+persona:neutral", "canonical")]:  # fmt: skip
        with pytest.raises(ValueError, match=msg):
            conditions.parse(bad)
    # the scenario default condition is wiki-off
    assert conditions.parse(scenario.DEFAULT_CONDITIONS[0]).wiki is False


def test_holdout_scenarios_are_never_published():
    pairs = naut2_pairs()
    gold = {**gold_save(), "holdout": True}
    open_one = {**gold_save(), "name": "nautiloid", "holdout": False}
    held = contamination.holdout_names([gold, open_one])
    assert held == {"act1_gold_1000"}
    (m1, r1), (m2, r2), (m3, r3) = pairs
    hidden = {**m3, "run_id": "h-r001", "scenario": {**m3["scenario"], "name": "act1_gold_1000"}}
    mixed = [(m1, r1), (m2, r2), (hidden, {**r3, "run_id": "h-r001"})]
    public = results.build_results(mixed, seed=0, created=0.0, b=50, holdout=held)
    assert [r["run_id"] for r in public["runs"]] == ["naut2-r001", "naut2-r002"]
    assert "act1_gold_1000" not in json.dumps(public)
    private = results.build_results(mixed, seed=0, created=0.0, b=50)
    assert "act1_gold_1000" in private["groups"][0]["stats"]["per_scenario"]
