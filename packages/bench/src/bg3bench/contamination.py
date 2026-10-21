"""Contamination tooling: the closed-book quiz per scenario, and keeping the private holdout out of what is published.

Quiz (`quizzes/<scenario>.json`, v1): questions a model could only answer from prior knowledge of the game (a
walkthrough, the wiki), asked with no tools through the same client profile as the run. A high score next to a high
pass rate says the scenario may be memorised rather than solved.

    {"schema": 1, "scenario": "nautiloid",
     "questions": [{"id": "q1", "q": "...", "kind": "choice", "choices": ["A ...", "B ..."], "answer": "B"},
                   {"id": "q2", "q": "...", "kind": "number", "answer": 3},
                   {"id": "q3", "q": "...", "kind": "text", "answer": "Us", "accept": ["\\\\bus\\\\b", "intellect devourer"]}]}

The model answers one JSON object `{"q1": "B", "q2": 3, ...}` (`prompt` asks for it, `parse_answers` reads it from
the reply, `score` grades it). Holdout scenarios (scenario v2 `holdout: true`) are run, never published:
`holdout_names` lists them for results.build_results(..., holdout=...).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = 1
KINDS = ("choice", "number", "text")
_LETTERS = "ABCDEFGH"


def check_quiz(quiz: Any) -> list[str]:
    if not isinstance(quiz, dict) or quiz.get("schema") != SCHEMA or not isinstance(quiz.get("scenario"), str):
        return [f"not a v{SCHEMA} quiz with a scenario"]
    qs = quiz.get("questions")
    if not isinstance(qs, list) or not qs:
        return ["questions must be a non-empty list"]
    errs, ids = [], []
    for i, q in enumerate(qs):
        qid = q.get("id") if isinstance(q, dict) else None
        if not isinstance(qid, str) or not qid or not isinstance(q.get("q"), str) or q.get("kind") not in KINDS:
            errs.append(f"question {i}: needs id, q and kind ({', '.join(KINDS)})")
            continue
        ids.append(qid)
        if q["kind"] == "choice":
            ch = q.get("choices")
            if not isinstance(ch, list) or not 2 <= len(ch) <= len(_LETTERS):
                errs.append(f"{qid}: a choice question needs 2-{len(_LETTERS)} choices")
            elif q.get("answer") not in _LETTERS[: len(ch)]:
                errs.append(f"{qid}: answer must be one of the choice letters")
        elif q["kind"] == "number" and not isinstance(q.get("answer"), int | float):
            errs.append(f"{qid}: a number question needs a numeric answer")
        elif q["kind"] == "text" and not (isinstance(q.get("answer"), str) and q["answer"].strip()):
            errs.append(f"{qid}: a text question needs an answer")
    if len(set(ids)) != len(ids):
        errs.append("question ids repeat")
    return errs


def load_quiz(path: str | Path) -> dict:
    quiz = json.loads(Path(path).read_text(encoding="utf-8"))
    if errs := check_quiz(quiz):
        raise ValueError(f"{path}: " + "; ".join(errs))
    return quiz


def prompt(quiz: Mapping) -> str:
    """The closed-book prompt: the questions and the answer format, nothing about the scenario's run."""
    lines = [
        "Answer from memory about Baldur's Gate 3; you have no tools. If you don't know, give your best guess.",
        'Reply with one JSON object mapping each question id to your answer, e.g. {"q1": "B", "q2": 3}.',
        "",
    ]
    for q in quiz["questions"]:
        lines.append(f"{q['id']}. {q['q']}")
        if q["kind"] == "choice":
            lines += [f"   {_LETTERS[i]}) {c}" for i, c in enumerate(q["choices"])]
        lines.append(
            f"   ({'a letter' if q['kind'] == 'choice' else 'a number' if q['kind'] == 'number' else 'a short answer'})"
        )
    return "\n".join(lines)


def parse_answers(reply: str) -> dict:
    """The last JSON object in a model's reply (code fences and prose around it are fine); {} when there is none."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", reply))):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        return obj
    return {}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def _correct(q: Mapping, given: Any) -> bool:
    if given is None:
        return False
    if q["kind"] == "choice":
        return str(given).strip().upper()[:1] == q["answer"]
    if q["kind"] == "number":
        try:
            return float(str(given).strip()) == float(q["answer"])
        except ValueError:
            return False
    text = str(given)
    if _norm(text) == _norm(q["answer"]):
        return True
    return any(re.search(pat, text, re.IGNORECASE) for pat in q.get("accept") or ())


def score(quiz: Mapping, answers: Mapping) -> dict:
    per = {q["id"]: _correct(q, answers.get(q["id"])) for q in quiz["questions"]}
    n, k = len(per), sum(per.values())
    return {"scenario": quiz["scenario"], "n": n, "correct": k, "rate": k / n, "per_question": per}


def holdout_names(scenarios: Iterable[Mapping]) -> frozenset[str]:
    """The names of the holdout scenarios among loaded scenario v2 dicts."""
    return frozenset(s["name"] for s in scenarios if s.get("holdout"))
