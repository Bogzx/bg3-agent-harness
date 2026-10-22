"""Spend safety for paid clients (pi through an API key; claude, codex and gemini run on subscriptions).

- A paid run needs a per-run USD cap (--max-cost); the runner kills the play process at the cap from the billed
  cost as it streams (runs.UsageMeter) and records cap_hit.
- runtime/budget.json {"cap": USD, "spent": USD} is the global budget across runs: a paid run refuses to start
  without one (`run --budget USD` creates it) or when it is used up, its per-run cap is cut to what is left, and
  every paid play process adds its cost when it ends.

    uv run pytest packages/harness/tests/test_spend.py
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .clients import Event

PAID_CLIENTS = ("pi",)
Budget = dict[str, Any]  # budget.json: {"cap", "spent", "created"?, "updated"?, "last"?}


class BudgetError(ValueError):
    pass


def is_paid(client: str) -> bool:
    return client in PAID_CLIENTS


def read_budget(path: Path) -> Budget | None:
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
    raw = cast(Budget, raw)
    for k in ("cap", "spent"):
        v = raw.get(k, 0.0 if k == "spent" else None)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise BudgetError(f"{path}: {k} must be a number >= 0")
    return {**raw, "spent": float(raw.get("spent", 0.0)), "cap": float(raw["cap"])}


def run_cap(client: str, max_cost: float | None, budget: Budget | None) -> tuple[float | None, str | None]:
    """(the run's cost cap, refusal). A paid client needs --max-cost; the global budget's remainder cuts it."""
    if not is_paid(client):
        return max_cost, None
    if max_cost is None:
        return None, f"--client {client} spends money: it needs --max-cost USD (a per-run cap)"
    if budget is None:
        return None, (
            f"--client {client} spends money: it needs a global budget, runtime/budget.json (pass "
            '--budget USD to create it, or write {"cap": USD, "spent": 0})'
        )
    left: float = round(budget["cap"] - budget["spent"], 6)
    if left <= 0:
        return None, f"the global budget is used up (${budget['spent']:.2f} of ${budget['cap']:g})"
    return min(max_cost, left), None


def ensure_budget(path: Path, usd: float | None, write: bool = True) -> Budget | None:
    """read_budget, and `--budget USD` creates budget.json {"cap": USD, "spent": 0} when there is none (write=False:
    the budget it would create, nothing written). A budget that is there with another cap raises: a live budget
    changes by editing the file, never by a flag."""
    if usd is not None and not usd > 0:
        raise BudgetError(f"--budget must be more than 0 USD (got {usd:g})")
    b = read_budget(path)
    if usd is None:
        return b
    if b is not None:
        if b["cap"] != usd:
            raise BudgetError(
                f"{path} already caps the spend at ${b['cap']:g} (${b['spent']:.2f} spent); "
                f"--budget {usd:g} differs: edit the file to change the cap"
            )
        return b
    new: Budget = {"cap": float(usd), "spent": 0.0}
    if write:
        with _locked(path):
            if path.exists():  # another runner created it meanwhile: that one counts
                return ensure_budget(path, usd)
            new["created"] = round(time.time(), 1)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(new, indent=1) + "\n")
            os.replace(tmp, path)
    return new


@contextmanager
def _locked(path: Path) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def add_spend(path: Path, usd: float, now: float | None = None, **what: Any) -> Budget | None:
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

    def __init__(self) -> None:
        self.usd = 0.0

    def on_event(self, ev: Event) -> None:
        if ev.kind in ("usage", "result") and ev.cost_usd is not None:
            self.usd = max(self.usd, float(ev.cost_usd))
