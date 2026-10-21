"""A fake judge model for bg3harness.judge: both a client (complete) and a transport (`call=`), answering from a list.

fake = FakeJudge([verdict_json, RuntimeError("overloaded")], cost=0.01)
judge_decisions(fake, character, decisions)              # as the client
judge_decisions(None, character, decisions, call=fake)   # as the transport
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class FakeJudge:
    """Answers in order: a str is the model's text, an exception is raised. Records every prompt; each answer costs
    `cost` (meta cost_usd)."""

    paid = False

    def __init__(self, answers: Iterable[str | Exception], model: str = "judge-model-x", cost: float = 0.01):
        self.answers, self.model, self.cost = list(answers), model, cost
        self.prompts: list[str] = []

    def complete(self, prompt: str, timeout: float) -> tuple[str, dict[str, Any]]:
        self.prompts.append(prompt)
        if not self.answers:
            raise RuntimeError("FakeJudge: no answer left")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, {"cost_usd": self.cost}

    def __call__(self, client: object, prompt: str) -> tuple[str, dict[str, Any]]:
        return self.complete(prompt, 0)
