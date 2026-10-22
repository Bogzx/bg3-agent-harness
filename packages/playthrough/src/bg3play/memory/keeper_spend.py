"""The keeper pass's spend for a paid client (bg3harness.spend PAID_CLIENTS: pi through an API key).

Like a play session (bg3harness.spend): the pass needs its own USD cap (playthrough.toml [keeper] max_cost_usd) and the
global budget runtime/budget.json; it is refused without either or when the budget is used up, and its cap is cut to
what the budget has left. The cost is metered live from the client's events as they stream (runs.UsageMeter, the play
session's fold: pi's billed running cost, then the result's total), across the pass and its fix attempt; the stream
watcher trips at the cap and the keeper kills the process. The pass's cost is added to budget.json when it ends.
Unpaid clients (claude, codex, gemini on subscriptions) pass through: no cap, no budget.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

spend: Any = importlib.import_module("bg3harness.spend")  # Any to pyright: untyped legacy modules
runs: Any = importlib.import_module("bg3harness.runs")


class KeeperSpend:
    def __init__(self, client: str, max_cost: float | None, budget: Path | None):
        self.paid = bool(spend.is_paid(client))
        self.client, self.budget = client, budget
        self.cap: float | None = None
        self.refusal: str | None = None
        self.meter: Any = runs.UsageMeter() if self.paid else None
        if not self.paid:
            return
        try:
            b = spend.read_budget(budget) if budget is not None else None
            cap, why = spend.run_cap(client, max_cost if max_cost and max_cost > 0 else None, b)
        except spend.BudgetError as e:
            cap, why = None, str(e)
        if why:
            why = why.replace("--max-cost USD (a per-run cap)", "a keeper cost cap ([keeper] max_cost_usd)")
            self.refusal = f"keeper refused: {why}"
        self.cap = cap

    def watcher(self, cli: Any) -> Callable[[str], bool] | None:
        """A stream line callback for _run_claude: True once the pass's cost reaches the cap (None: no cap)."""
        cap = self.cap
        if self.meter is None or cap is None:
            return None

        def over(line: str) -> bool:
            for ev in cli.parse(line):
                self.meter.on_event(ev)
            return self.cost >= cap

        return over

    @property
    def cost(self) -> float:
        return float(self.meter.cost) if self.meter is not None else 0.0

    @property
    def cap_hit(self) -> bool:
        return self.cap is not None and self.cost >= self.cap

    def record(self, playthrough: str, sid: str) -> str:
        """Adds the pass's cost to budget.json; a note for the result's detail ("" for an unpaid client)."""
        if not self.paid or self.budget is None:
            return ""
        try:
            b = spend.add_spend(self.budget, self.cost, playthrough=playthrough, session=sid, role="keeper")
        except (OSError, spend.BudgetError) as e:
            return f"keeper cost ${self.cost:.4f}; budget update failed ({e})"
        return f"keeper cost ${self.cost:.4f}" + (f"; budget ${b['spent']:.2f} of ${b['cap']:g}" if b else "")
