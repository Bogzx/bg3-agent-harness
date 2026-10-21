"""Spend safety for paid clients (pi through an API key; claude, codex and gemini run on subscriptions).

- A paid run needs a per-run USD cap (--max-cost); the runner kills the play process at the cap from the billed
  cost as it streams (runs.UsageMeter) and records cap_hit.
- runtime/budget.json {"cap": USD, "spent": USD} is the global budget across runs: a paid run refuses to start when
  it is used up, its per-run cap is cut to what is left, and every paid play process adds its cost when it ends.

    python3 -m unittest runner/test_spend.py
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

PAID_CLIENTS = ("pi",)


class BudgetError(ValueError):
    pass


def is_paid(client: str) -> bool:
    return client in PAID_CLIENTS


def read_budget(path: Path) -> dict | None:
    """budget.json as {"cap", "spent", ...}, None when there is none. A file that is there but unreadable raises:
    a broken budget must stop paid runs, not lift the cap."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise BudgetError(f"{path}: unreadable ({e})") from None
    if not isinstance(raw, dict):
        raise BudgetError(f"{path}: not a JSON object")
    for k in ("cap", "spent"):
        v = raw.get(k, 0.0 if k == "spent" else None)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise BudgetError(f"{path}: {k} must be a number >= 0")
    return {**raw, "spent": float(raw.get("spent", 0.0)), "cap": float(raw["cap"])}


def run_cap(client: str, max_cost: float | None, budget: dict | None) -> tuple[float | None, str | None]:
    """(the run's cost cap, refusal). A paid client needs --max-cost; the global budget's remainder cuts it."""
    if not is_paid(client):
        return max_cost, None
    if max_cost is None:
        return None, f"--client {client} spends money: it needs --max-cost USD (a per-run cap)"
    if budget is None:
        return max_cost, None
    left = round(budget["cap"] - budget["spent"], 6)
    if left <= 0:
        return None, f"the global budget is used up (${budget['spent']:.2f} of ${budget['cap']:g})"
    return min(max_cost, left), None


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def add_spend(path: Path, usd: float, now: float | None = None, **what) -> dict | None:
    """Adds one paid process's cost to budget.json (locked, atomic); None when there is no budget file."""
    if usd <= 0:
        return read_budget(path)
    with _locked(path):
        b = read_budget(path)
        if b is None:
            return None
        b["spent"] = round(b["spent"] + usd, 6)
        b["updated"] = round(time.time() if now is None else now, 1)
        b["last"] = {"usd": round(usd, 6), **what}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(b, indent=1) + "\n")
        os.replace(tmp, path)
        return b


class ProcessCost:
    """One play process's cost from its events: the running cost on pi's usage events (billed when the endpoint
    reports it), then the result's exact total. What add_spend gets when the process ends, also when it was killed
    before its result."""

    def __init__(self):
        self.usd = 0.0

    def on_event(self, ev) -> None:
        if ev.kind in ("usage", "result") and ev.cost_usd is not None:
            self.usd = max(self.usd, float(ev.cost_usd))
